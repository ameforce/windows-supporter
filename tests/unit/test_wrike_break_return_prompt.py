"""Manual-break return prompt: idle detection, unlock, panel actions and store."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.apps.Wrike import Wrike
from src.apps.wrike_worktime_panel import WorktimeBreakReturnPrompt
from src.apps.wrike_worktime_state import WorktimeStateStore


DAY = "2026-04-06"


class _Clock(datetime):
    current = datetime(2026, 4, 6, 12, 0)

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return tz.fromutc(cls.current.replace(tzinfo=tz))
        return cls.current


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 4, 6, hour, minute, second)


class _Root:
    def __init__(self):
        self.calls = []

    def after(self, delay, callback):
        identity = f"after-{len(self.calls) + 1}"
        self.calls.append((identity, delay, callback))
        return identity

    def after_cancel(self, _identity):
        pass


class _Panel:
    def __init__(self):
        self.visible = False
        self.show = Mock(side_effect=self._show)
        self.hide = Mock(side_effect=self._hide)
        self.refresh_now = Mock()
        self.set_idle_timeout_ms = Mock()

    def _show(self, **_kwargs):
        self.visible = True
        return True

    def _hide(self):
        self.visible = False

    def is_visible(self):
        return self.visible


class EndManualBreakStoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        self.store = WorktimeStateStore(self.path, now_provider=_Clock.now)
        _Clock.current = _at(12, 0)
        self.assertTrue(self.store.toggle_manual_break(now=_at(12, 0))["ok"])

    def test_ends_active_break_at_requested_time(self):
        state = self.store.end_manual_break(
            _at(12, 40, 30),
            expected_started_at="2026-04-06T12:00:00",
            now=_at(12, 47),
        )

        self.assertTrue(state["ok"], state)
        self.assertFalse(state["active"])
        self.assertIn("휴게 종료 12:40 (40분)", state["message"])
        breaks = WorktimeStateStore(self.path).get_completed_manual_breaks(DAY)["breaks"]
        self.assertEqual(
            [(item["start"], item["end"]) for item in breaks],
            [("2026-04-06T12:00:00", "2026-04-06T12:40:30")],
        )

    def test_rejects_without_mutating_state(self):
        before = self.path.read_bytes()
        cases = {
            "before start": (_at(11, 59), None, _at(12, 30)),
            "after now": (_at(12, 31), None, _at(12, 30)),
            "other break": (_at(12, 20), "2026-04-06T11:00:00", _at(12, 30)),
        }
        for name, (end_at, expected, now) in cases.items():
            with self.subTest(name):
                state = self.store.end_manual_break(
                    end_at,
                    expected_started_at=expected,
                    now=now,
                )
                self.assertFalse(state["ok"])
                self.assertTrue(state["active"])
                self.assertEqual(self.path.read_bytes(), before)

    def test_never_starts_a_break(self):
        self.assertTrue(self.store.end_manual_break(_at(12, 10), now=_at(12, 10))["ok"])
        before = self.path.read_bytes()

        state = self.store.end_manual_break(_at(12, 20), now=_at(12, 20))

        self.assertFalse(state["ok"])
        self.assertEqual(state["message"], "진행 중인 휴게가 없습니다.")
        self.assertFalse(self.store.get_manual_break_state(now=_at(12, 20))["active"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_write_restores_active_break(self):
        with patch("src.apps.wrike_worktime_state.os.replace", side_effect=OSError("injected")):
            state = self.store.end_manual_break(_at(12, 10), now=_at(12, 10))

        self.assertFalse(state["ok"])
        self.assertTrue(self.store.get_manual_break_state(now=_at(12, 10))["active"])
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["days"][DAY]["active_break_started_at"],
            "2026-04-06T12:00:00",
        )


class BreakReturnPromptTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        _Clock.current = _at(11, 0)
        with patch.dict(os.environ, {"APPDATA": temporary.name}, clear=False):
            self.wrike = Wrike()
        self.wrike._Wrike__lib.datetime = _Clock
        self.store = WorktimeStateStore(
            Path(temporary.name) / "break-return-state.json",
            now_provider=_Clock.now,
        )
        self.wrike._Wrike__worktime_state_store = self.store
        self.root = _Root()
        self.wrike._Wrike__root = self.root
        self.wrike._Wrike__background_active = True
        self.panel = _Panel()
        self.wrike._Wrike__worktime_panel = self.panel
        self.wrike._Wrike__worktime_panel_root = self.root
        self.enterContext(patch("src.apps.Wrike.ToolTip"))
        self.watcher_type = self.enterContext(patch("src.apps.Wrike.WorktimeActivityWatcher"))
        for method in (
            "__start_ical_polling",
            "__start_vacation_ical_polling",
            "__start_flex_polling",
            "__schedule_flex_poll",
        ):
            self.enterContext(patch.object(self.wrike, f"_Wrike{method}"))
        # The clock-in and overtime prompts are separate flows of the same
        # activity callback. A day off with a clock-in keeps both inert.
        self.enterContext(
            patch.object(
                self.wrike,
                "_Wrike__plan_for_date",
                return_value={"explicit": True, "clock_in": "09:00", "target_net_minutes": 0},
            )
        )
        self.errors = self.enterContext(patch.object(self.wrike, "_Wrike__show_panel_action_error"))

    def start_break(self, at):
        _Clock.current = at
        self.assertTrue(self.wrike.toggle_manual_break()["ok"])

    def activity(self, at):
        _Clock.current = at
        self.wrike._Wrike__on_worktime_activity(at)

    def unlock(self, at):
        _Clock.current = at
        self.wrike.on_session_unlock()

    def pending(self):
        return self.wrike._Wrike__break_return_prompt

    def visible_prompt(self):
        return self.wrike._Wrike__visible_break_return_prompt(
            self.store.get_manual_break_state(now=_Clock.current)
        )

    def completed_breaks(self):
        return [
            (item["start"], item["end"])
            for item in self.store.get_completed_manual_breaks(DAY)["breaks"]
        ]

    def drain(self):
        while True:
            try:
                callback = self.wrike._Wrike__ui_queue.get_nowait()
            except queue.Empty:
                return
            callback()

    def test_idle_return_during_break_surfaces_prompt_once(self):
        self.start_break(_at(12, 0, 5))
        self.activity(_at(12, 0, 6))
        self.panel.show.assert_not_called()

        self.activity(_at(12, 30))

        self.assertEqual(self.pending()["detected_at"], _at(12, 30))
        self.assertEqual(self.visible_prompt(), WorktimeBreakReturnPrompt("12:00", "12:30"))
        self.panel.show.assert_called_once_with(activate=False)
        self.activity(_at(12, 30, 1))
        self.assertEqual(self.panel.show.call_count, 1)

    def test_panel_model_carries_prompt_until_answered(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 30))
        _Clock.current = _at(12, 31)

        model = self.wrike._Wrike__build_worktime_panel_model()

        self.assertTrue(model.break_active)
        self.assertEqual(
            model.break_return_prompt,
            WorktimeBreakReturnPrompt("12:00", "12:30"),
        )
        self.wrike._Wrike__panel_break_return_keep()
        self.assertIsNone(self.wrike._Wrike__build_worktime_panel_model().break_return_prompt)

    def test_no_prompt_without_break_or_without_long_absence(self):
        self.activity(_at(11, 0))
        self.activity(_at(11, 30))
        self.assertIsNone(self.pending())

        self.start_break(_at(11, 30, 5))
        self.activity(_at(11, 30, 6))
        self.activity(_at(11, 34, 59))

        self.assertIsNone(self.pending())
        self.panel.show.assert_not_called()

    def test_quick_click_starting_break_after_absence_is_not_a_return(self):
        # Away without a break; the first input after coming back is a quick
        # click on "휴게 시작", seen by the watcher only after the break began.
        self.activity(_at(11, 0))
        self.start_break(_at(11, 30))
        self.activity(_at(11, 30, 0))

        self.assertIsNone(self.pending())
        self.panel.show.assert_not_called()
        self.activity(_at(11, 50))
        self.assertEqual(self.pending()["detected_at"], _at(11, 50))

    def test_threshold_uses_idle_minutes_even_with_overtime_autopause_off(self):
        self.wrike._Wrike__overtime_idle_pause_enabled = False
        self.wrike._Wrike__overtime_idle_pause_min = 10
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 9))
        self.assertIsNone(self.pending())

        self.activity(_at(12, 19, 30))

        self.assertEqual(self.pending()["detected_at"], _at(12, 19, 30))

    def test_accept_ends_break_at_return_time_not_click_time(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 40))
        self.drain()
        _Clock.current = _at(12, 47)

        self.wrike._Wrike__panel_break_return_accept("12:40")

        self.assertEqual(self.completed_breaks(), [("2026-04-06T12:00:00", "2026-04-06T12:40:00")])
        self.assertFalse(self.store.get_manual_break_state(now=_at(12, 47))["active"])
        self.assertIsNone(self.pending())
        self.errors.assert_not_called()
        with patch.object(self.wrike, "_Wrike__show_tooltip") as tooltip:
            self.drain()
        self.assertIn("휴게 종료 12:40 (40분)", tooltip.call_args.args[1])

    def test_edit_rejects_invalid_times_then_ends_at_chosen_time(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 40))
        _Clock.current = _at(12, 50)

        self.assertFalse(self.wrike._Wrike__panel_break_return_edit("12:40", "11:59"))
        self.assertFalse(self.wrike._Wrike__panel_break_return_edit("12:40", "12:51"))
        self.assertEqual(self.errors.call_count, 2)
        self.assertIsNotNone(self.pending())
        self.assertTrue(self.store.get_manual_break_state(now=_at(12, 50))["active"])

        self.assertTrue(self.wrike._Wrike__panel_break_return_edit("12:40", "12:35"))

        self.assertEqual(self.completed_breaks(), [("2026-04-06T12:00:00", "2026-04-06T12:35:00")])
        self.assertIsNone(self.pending())

    def test_expired_detected_time_is_rejected(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 40))
        _Clock.current = _at(12, 45)

        self.wrike._Wrike__panel_break_return_accept("12:39")

        self.errors.assert_called_once()
        self.assertTrue(self.store.get_manual_break_state(now=_at(12, 45))["active"])
        self.assertIsNotNone(self.pending())

    def test_keep_clears_prompt_until_next_absence(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 30))

        self.wrike._Wrike__panel_break_return_keep()
        self.activity(_at(12, 30, 5))

        self.assertIsNone(self.pending())
        self.assertTrue(self.store.get_manual_break_state(now=_at(12, 30, 5))["active"])
        self.activity(_at(12, 50))
        self.assertEqual(self.pending()["detected_at"], _at(12, 50))

    def test_later_return_keeps_first_unanswered_time_and_resurfaces(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 30))
        self.activity(_at(13, 0))

        self.assertEqual(self.pending()["detected_at"], _at(12, 30))
        self.assertEqual(self.panel.show.call_count, 2)

    def test_manual_toggle_clears_and_stale_prompt_cannot_end_new_break(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 30))
        self.start_break(_at(12, 31))
        self.assertIsNone(self.pending())

        self.activity(_at(12, 59, 59))
        self.start_break(_at(13, 0))
        self.activity(_at(13, 0, 1))
        self.assertIsNone(self.pending())
        self.activity(_at(13, 30))
        # Another surface ends and restarts the break without this app.
        self.assertTrue(self.store.toggle_manual_break(now=_at(13, 31))["ok"])
        self.assertTrue(self.store.toggle_manual_break(now=_at(13, 32))["ok"])
        _Clock.current = _at(13, 33)

        self.assertIsNone(self.visible_prompt())
        self.wrike._Wrike__panel_break_return_accept("13:30")

        self.errors.assert_called_once()
        state = self.store.get_manual_break_state(now=_at(13, 33))
        self.assertTrue(state["active"])
        self.assertEqual(state["started_at"], "2026-04-06T13:32:00")
        self.assertIsNone(self.pending())

    def test_unlock_after_long_lock_prompts_at_unlock_time(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))

        self.unlock(_at(12, 45))

        self.assertEqual(self.pending()["detected_at"], _at(12, 45))
        self.panel.show.assert_called_once_with(activate=False)
        self.activity(_at(12, 45, 2))
        self.assertEqual(self.panel.show.call_count, 1)

    def test_unlock_resurfaces_prompt_raised_while_locked(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 40))

        self.unlock(_at(12, 40, 10))

        self.assertEqual(self.pending()["detected_at"], _at(12, 40))
        self.assertEqual(self.panel.show.call_count, 2)

    def test_short_lock_does_not_prompt(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))

        self.unlock(_at(12, 3))

        self.assertIsNone(self.pending())
        self.panel.show.assert_not_called()

    def test_disabled_automatic_notices_block_and_clear_prompt(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 30))
        self.assertIsNotNone(self.pending())

        ok, error = self.wrike.update_settings({"monitor_enabled": False})
        self.assertTrue(ok, error)

        self.assertIsNone(self.pending())
        self.activity(_at(13, 0))
        self.unlock(_at(13, 30))
        self.assertIsNone(self.pending())
        self.assertEqual(self.panel.show.call_count, 1)

    def test_watcher_start_seeds_absence_after_restart(self):
        self.start_break(_at(11, 50))
        self.assertIsNone(self.wrike._Wrike__last_worktime_activity_at)
        _Clock.current = _at(12, 0)

        self.wrike._Wrike__start_activity_watcher()
        self.assertEqual(self.wrike._Wrike__last_worktime_activity_at, _at(12, 0))
        self.activity(_at(12, 20))

        self.assertEqual(self.visible_prompt(), WorktimeBreakReturnPrompt("11:50", "12:20"))


if __name__ == "__main__":
    unittest.main()
