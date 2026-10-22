"""Manual-break return prompt: idle detection, unlock, panel actions and store."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import src.apps.Wrike as wrike_module
from src.apps.Wrike import Wrike
from src.apps.wrike_worktime_panel import WorktimeBreakReturnPrompt
from src.apps.wrike_worktime_state import WorktimeStateStore
from src.utils.runtime_lifecycle import DEFAULT_MAX_LOG_BYTES


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

    def toggle(self, **kwargs):
        if self.visible:
            self._hide()
        else:
            self._show(**kwargs)


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
        # Keep the existing flow scenarios on their prior five-minute
        # threshold; the new 30-second shared default has a focused test below.
        self.wrike._Wrike__overtime_idle_pause_sec = 5 * 60
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

    def test_30_second_shared_idle_threshold_applies_with_autopause_off(self):
        self.wrike._Wrike__overtime_idle_pause_sec = 30
        self.wrike._Wrike__overtime_idle_pause_enabled = False
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))

        self.activity(_at(12, 0, 31))

        self.assertEqual(self.pending()["detected_at"], _at(12, 0, 31))
        self.assertEqual(self.wrike._Wrike__break_return_idle_seconds(), 30)

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

    def test_threshold_uses_shared_idle_seconds_even_with_overtime_autopause_off(self):
        self.wrike._Wrike__overtime_idle_pause_enabled = False
        self.wrike._Wrike__overtime_idle_pause_sec = 10 * 60
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
        for minute in range(31, 41):
            self.activity(_at(12, minute))
        self.activity(_at(13, 0))

        self.assertEqual(self.pending()["detected_at"], _at(12, 30))
        self.assertEqual(self.panel.show.call_count, 2)

    def _first_return(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 30))
        self.assertEqual(self.panel.show.call_count, 1)

    def test_missed_question_is_shown_again_on_later_input_after_pacing_window(self):
        self._first_return()
        self.panel.visible = False  # the panel closed itself unanswered

        self.activity(_at(12, 30, 30))
        self.assertEqual(self.panel.show.call_count, 1)

        self.activity(_at(12, 32, 1))
        self.assertEqual(self.panel.show.call_count, 2)
        self.assertEqual(self.pending()["detected_at"], _at(12, 30))
        self.assertEqual(self.pending()["surfaced_at"], _at(12, 32, 1))

        self.activity(_at(12, 32, 30))
        self.assertEqual(self.panel.show.call_count, 2)

    def test_question_on_screen_counts_as_seen_and_is_not_shown_again(self):
        self._first_return()

        self.activity(_at(12, 33))  # panel still on screen
        self.assertEqual(self.panel.show.call_count, 1)
        self.assertEqual(self.pending()["surfaced_at"], _at(12, 33))

        self.panel.visible = False
        self.activity(_at(12, 34))
        self.assertEqual(self.panel.show.call_count, 1)
        self.activity(_at(12, 35, 1))
        self.assertEqual(self.panel.show.call_count, 2)

    def test_answered_question_is_never_shown_again(self):
        self._first_return()
        self.panel.visible = False

        self.wrike._Wrike__panel_break_return_keep()
        self.activity(_at(12, 33))
        self.activity(_at(12, 36))

        self.assertEqual(self.panel.show.call_count, 1)
        self.assertIsNone(self.pending())

    def test_question_of_a_finished_break_is_dropped_instead_of_shown(self):
        self._first_return()
        self.panel.visible = False
        # The break is ended behind the question's back (another surface).
        self.assertTrue(self.store.end_manual_break(_at(12, 31), now=_at(12, 31))["ok"])

        self.activity(_at(12, 33))

        self.assertEqual(self.panel.show.call_count, 1)
        self.assertIsNone(self.pending())

    def test_failed_display_still_waits_for_the_pacing_window(self):
        self._first_return()
        self.panel.visible = False
        self.panel.show.side_effect = lambda **_kwargs: False  # cannot map

        self.activity(_at(12, 32, 1))
        self.assertEqual(self.panel.show.call_count, 2)
        for second in range(5, 60, 5):
            self.activity(_at(12, 32, second))
        self.assertEqual(self.panel.show.call_count, 2)
        self.assertIsNotNone(self.pending())

    def test_unlock_marks_the_display_time_for_pacing(self):
        self._first_return()
        self.panel.visible = False

        self.unlock(_at(12, 31))
        self.assertEqual(self.panel.show.call_count, 2)
        self.assertEqual(self.pending()["surfaced_at"], _at(12, 31))

        self.activity(_at(12, 32))
        self.assertEqual(self.panel.show.call_count, 2)

    def test_display_count_is_paced_with_a_panel_that_closes_itself(self):
        # Unlike the fake that stays open, a real panel closes after a few
        # seconds: ten minutes of steady input shows the question once per
        # pacing window, not on every input.
        self._first_return()
        for step in range(1, 21):
            self.panel.visible = False
            self.activity(_at(12, 30) + timedelta(seconds=30 * step))

        self.assertEqual(self.panel.show.call_count, 6)

    def test_input_inside_the_pacing_window_writes_no_log_lines(self):
        self._first_return()
        self.panel.visible = False

        with patch.object(self.wrike, "_Wrike__log") as log:
            for second in range(5, 100, 5):
                self.activity(_at(12, 30) + timedelta(seconds=second))

        self.assertEqual([call.args[0] for call in log.call_args_list], [])
        self.assertEqual(self.panel.show.call_count, 1)

    def test_clock_moved_back_does_not_block_the_next_display(self):
        self._first_return()
        self.panel.visible = False
        self.activity(_at(12, 32, 1))
        self.assertEqual(self.panel.show.call_count, 2)

        self.panel.visible = False
        self.activity(_at(12, 30, 40))  # the system clock was set back
        self.assertEqual(self.panel.show.call_count, 3)

        self.panel.visible = False
        self.activity(_at(12, 30, 50))  # pacing restarts from the new clock
        self.assertEqual(self.panel.show.call_count, 3)

    def test_input_while_on_screen_restarts_the_pacing_window(self):
        self._first_return()  # shown at 12:30:00 and still on screen
        self.activity(_at(12, 31, 30))  # inside the window, still on screen
        self.assertEqual(self.pending()["surfaced_at"], _at(12, 31, 30))

        self.panel.visible = False  # the user dismissed it
        self.activity(_at(12, 32, 10))  # 130 s after the first display, 40 s after it was seen
        self.assertEqual(self.panel.show.call_count, 1)
        self.activity(_at(12, 33, 31))  # 121 s after it was last seen
        self.assertEqual(self.panel.show.call_count, 2)

    def test_question_of_a_finished_break_is_dropped_while_the_panel_is_on_screen(self):
        self._first_return()  # the panel stays on screen
        self.assertTrue(self.store.end_manual_break(_at(12, 31), now=_at(12, 31))["ok"])

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.activity(_at(12, 33))  # on screen and a pacing window has passed

        self.assertIsNone(self.pending())
        self.assertEqual(self.panel.show.call_count, 1)
        lines = [call.args[0] for call in log.call_args_list]
        self.assertFalse(any("counted as shown" in line for line in lines), lines)
        self.assertTrue(any("is no longer active" in line for line in lines), lines)

    def test_panel_opened_by_hand_counts_as_seen(self):
        self._first_return()
        self.panel.visible = False
        _Clock.current = _at(12, 31)

        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh"):
            self.wrike.show_weekly_timelog_summary(self.root)

        self.assertTrue(self.panel.visible)
        self.assertEqual(self.pending()["surfaced_at"], _at(12, 31))
        self.panel.visible = False  # the user closed it again
        self.activity(_at(12, 32, 30))  # 150 s after the first display
        self.assertEqual(self.panel.show.call_count, 1)

    def test_hotkey_in_the_moment_of_the_resurface_keeps_the_panel(self):
        # The watcher raises the question for a key chord and the hotkey task
        # of the same chord runs right after: the toggle must not hide it.
        self._first_return()
        self.panel.visible = False  # closed itself unanswered
        self.activity(_at(12, 33))
        self.assertTrue(self.panel.visible)
        self.assertEqual(self.panel.show.call_count, 2)

        _Clock.current = _at(12, 33) + timedelta(milliseconds=300)
        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh") as refresh:
            self.wrike.show_weekly_timelog_summary(self.root)

        self.assertTrue(self.panel.visible)
        self.assertEqual(self.panel.show.call_count, 2)
        self.assertEqual(self.pending()["surfaced_at"], _Clock.current)
        refresh.assert_called_once_with(force=True)

    def test_hotkey_after_the_grace_window_toggles_the_panel_away(self):
        self._first_return()
        self.panel.visible = False
        self.activity(_at(12, 33))
        self.assertTrue(self.panel.visible)

        _Clock.current = _at(12, 33, 3)  # seen first, then closed by the hotkey
        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh") as refresh:
            self.wrike.show_weekly_timelog_summary(self.root)

        self.assertFalse(self.panel.visible)
        refresh.assert_not_called()

    def test_hotkey_inside_the_window_still_opens_a_hidden_panel(self):
        self._first_return()  # raised at 12:30:00
        self.panel.visible = False  # gone again
        _Clock.current = _at(12, 30) + timedelta(milliseconds=500)

        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh"):
            self.wrike.show_weekly_timelog_summary(self.root)

        self.assertTrue(self.panel.visible)

    def test_grace_covers_only_the_first_direct_open(self):
        # The watcher and the hotkey answer one key chord, once each. A second
        # press inside the window is the user closing the panel again.
        self._first_return()  # raised at 12:30:00 and on screen

        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh") as refresh:
            _Clock.current = _at(12, 30) + timedelta(milliseconds=300)
            self.wrike.show_weekly_timelog_summary(self.root)
            self.assertTrue(self.panel.visible)
            self.assertIsNone(self.wrike._Wrike__break_return_raised_at)
            refresh.assert_called_once_with(force=True)

            _Clock.current = _at(12, 30) + timedelta(milliseconds=800)
            self.wrike.show_weekly_timelog_summary(self.root)

        self.assertFalse(self.panel.visible)
        self.assertEqual(self.panel.show.call_count, 1)
        refresh.assert_called_once_with(force=True)

    def test_hotkey_with_the_clock_set_back_before_the_raise_toggles(self):
        # A clock set back puts "now" before the raise. That is not "just
        # after" it, so the open is a plain toggle that hides the panel.
        self._first_return()  # raised at 12:30:00 and on screen
        _Clock.current = _at(12, 29, 59)

        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh") as refresh:
            self.wrike.show_weekly_timelog_summary(self.root)

        self.assertFalse(self.panel.visible)
        refresh.assert_not_called()

    def _direct_open_after(self, answer):
        # Raise the question, answer it a moment later, then open by hand
        # inside what used to be the grace window.
        self._first_return()  # raised at 12:30:00 and on screen
        _Clock.current = _at(12, 30) + timedelta(milliseconds=200)
        answer()
        self.assertIsNone(self.wrike._Wrike__break_return_raised_at)
        _Clock.current = _at(12, 30) + timedelta(milliseconds=300)
        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh"):
            self.wrike.show_weekly_timelog_summary(self.root)

    def test_answer_that_keeps_the_break_leaves_no_grace(self):
        self._direct_open_after(self.wrike._Wrike__panel_break_return_keep)

        self.assertFalse(self.panel.visible)

    def test_answer_that_ends_the_break_leaves_no_grace(self):
        self._direct_open_after(
            lambda: self.wrike._Wrike__panel_break_return_accept("12:30")
        )

        self.assertEqual(self.completed_breaks(), [("2026-04-06T12:00:00", "2026-04-06T12:30:00")])
        self.assertFalse(self.panel.visible)

    def test_break_toggled_by_hand_leaves_no_grace(self):
        self._direct_open_after(lambda: self.start_break(_Clock.current))

        self.assertFalse(self.panel.visible)

    def test_disabling_drops_the_grace_marker(self):
        self._first_return()
        self.assertIsNotNone(self.wrike._Wrike__break_return_raised_at)

        ok, error = self.wrike.update_settings({"monitor_enabled": False})

        self.assertTrue(ok, error)
        self.assertIsNone(self.wrike._Wrike__break_return_raised_at)

    def test_hotkey_before_the_watcher_leaves_one_open_panel(self):
        self._first_return()
        self.panel.visible = False  # closed itself unanswered
        _Clock.current = _at(12, 33)
        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh"):
            self.wrike.show_weekly_timelog_summary(self.root)
        self.assertTrue(self.panel.visible)

        self.activity(_at(12, 33))  # the watcher notices the same chord later

        self.assertTrue(self.panel.visible)
        self.assertEqual(self.panel.show.call_count, 1)

    def test_hotkey_first_for_the_raising_input_leaves_no_grace(self):
        # The hotkey task of the chord that raises the question can run before
        # the watcher notices the input. The panel is open when the question
        # arrives, so nothing was raised that a direct open must keep: the next
        # press of the chord is the user closing the panel.
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        _Clock.current = _at(12, 30)
        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh") as refresh:
            self.wrike.show_weekly_timelog_summary(self.root)
            self.assertTrue(self.panel.visible)
            self.assertIsNone(self.pending())

            noticed_at = _at(12, 30) + timedelta(milliseconds=200)
            self.activity(noticed_at)  # the watcher notices the same chord

            self.assertEqual(self.pending()["detected_at"], noticed_at)
            self.assertEqual(self.pending()["surfaced_at"], noticed_at)
            self.assertEqual(self.visible_prompt(), WorktimeBreakReturnPrompt("12:00", "12:30"))
            self.assertTrue(self.panel.visible)
            self.panel.show.assert_called_once_with(activate=False)
            self.assertIsNone(self.wrike._Wrike__break_return_raised_at)

            _Clock.current = _at(12, 30) + timedelta(milliseconds=800)
            self.wrike.show_weekly_timelog_summary(self.root)

        self.assertFalse(self.panel.visible)
        # The hotkey's open and the watcher's display each read Wrike once; the
        # closing press reads nothing.
        self.assertEqual(
            refresh.call_args_list,
            [call(force=True), call(force=False)],
        )

    def test_unlock_display_of_an_open_panel_leaves_the_earlier_raise_as_it_was(self):
        # Only the display that brought a hidden panel up is a raise. Showing
        # the question again on the open panel neither writes nor clears it.
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.unlock(_at(12, 45))  # raises the question on the hidden panel
        self.assertEqual(self.wrike._Wrike__break_return_raised_at, _at(12, 45))
        self.assertTrue(self.panel.visible)

        self.unlock(_at(12, 45, 1))  # shown again while it is still open

        self.assertEqual(self.panel.show.call_count, 2)
        self.assertEqual(self.wrike._Wrike__break_return_raised_at, _at(12, 45))
        # The grace belongs to the raise 1.6 s ago, which is over.
        _Clock.current = _at(12, 45) + timedelta(milliseconds=1600)
        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh"):
            self.wrike.show_weekly_timelog_summary(self.root)
        self.assertFalse(self.panel.visible)

    def test_panel_that_cannot_be_asked_is_taken_as_hidden(self):
        # A failing visibility check must not cost the grace of a real raise.
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        shown_state = self.panel.is_visible

        def is_visible_after_show():
            if not self.panel.show.called:
                raise RuntimeError("window state unavailable")
            return shown_state()

        self.panel.is_visible = is_visible_after_show

        self.activity(_at(12, 30))

        self.assertEqual(self.wrike._Wrike__break_return_raised_at, _at(12, 30))
        self.assertTrue(self.panel.visible)

    def test_first_raise_before_the_panel_exists_still_starts_a_grace(self):
        # The panel is created on first use, so the first question after the
        # app started finds none. A missing panel is a hidden one.
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.wrike._Wrike__worktime_panel = None

        def create_panel(_root):
            self.wrike._Wrike__worktime_panel = self.panel
            return self.panel

        with patch.object(
            self.wrike, "_Wrike__ensure_worktime_panel", side_effect=create_panel
        ) as ensure:
            self.activity(_at(12, 30))

        ensure.assert_called_once_with(self.root)
        self.assertTrue(self.panel.visible)
        self.assertEqual(self.wrike._Wrike__break_return_raised_at, _at(12, 30))

    def test_steady_input_on_screen_leaves_no_card_for_a_finished_break(self):
        # While the panel stays up and input never pauses, the pacing window
        # keeps moving, so the stale question is only cleared later. It must
        # not reach the screen: the card follows the break, not the question.
        self._first_return()
        self.assertTrue(self.store.end_manual_break(_at(12, 31), now=_at(12, 31))["ok"])

        for step in range(1, 11):
            self.activity(_at(12, 30) + timedelta(seconds=30 * step))

        self.assertIsNotNone(self.pending())
        self.assertIsNone(self.visible_prompt())
        self.assertEqual(self.panel.show.call_count, 1)

    def test_on_screen_decision_is_logged_once_per_pacing_window(self):
        self._first_return()

        with patch.object(self.wrike, "_Wrike__log") as log:
            for minute in (31, 34, 35, 38):  # the panel stays on screen
                self.activity(_at(12, minute))

        lines = [call.args[0] for call in log.call_args_list]
        on_screen = [line for line in lines if "already on screen" in line]
        self.assertEqual(len(on_screen), 2, lines)
        self.assertEqual(self.panel.show.call_count, 1)

    def test_exception_after_display_still_records_the_attempt(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))

        with (
            patch.object(
                self.wrike,
                "_Wrike__request_timelog_snapshot_refresh",
                side_effect=RuntimeError("refresh failed"),
            ),
            patch.object(self.wrike, "_Wrike__log") as log,
        ):
            with self.assertRaises(RuntimeError):
                self.activity(_at(12, 30))

        self.assertEqual(self.pending()["surfaced_at"], _at(12, 30))
        lines = [call.args[0] for call in log.call_args_list]
        self.assertTrue(
            any("panel_shown=False" in line and "reason=raised:RuntimeError" in line for line in lines),
            lines,
        )

    def test_flow_steps_are_written_to_the_log(self):
        with patch.object(self.wrike, "_Wrike__log") as log:
            self._first_return()
            self.panel.visible = False
            self.activity(_at(12, 33))
            self.unlock(_at(12, 34))
            self.wrike._Wrike__panel_break_return_keep()

        lines = [call.args[0] for call in log.call_args_list]
        recorded = [line for line in lines if "question recorded" in line]
        self.assertEqual(len(recorded), 1, lines)
        self.assertIn("break_started=2026-04-06T12:00:00", recorded[0])
        self.assertIn("input=2026-04-06T12:30:00", recorded[0])
        self.assertIn("panel_shown=True", recorded[0])
        self.assertIn("reason=shown", recorded[0])
        self.assertTrue(any("pending question shown again" in line for line in lines), lines)
        self.assertTrue(any(line.startswith("break return: unlock;") for line in lines), lines)
        self.assertTrue(
            any("question cleared; answered, break kept" in line for line in lines), lines
        )

    def test_failed_display_reason_is_logged(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.panel.show.side_effect = lambda **_kwargs: False

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.activity(_at(12, 30))

        lines = [call.args[0] for call in log.call_args_list]
        self.assertTrue(
            any("panel_shown=False" in line and "reason=not_visible" in line for line in lines),
            lines,
        )

    def test_ignored_returns_are_logged_with_their_reason(self):
        with patch.object(self.wrike, "_Wrike__log") as log:
            self.activity(_at(11, 0))
            self.activity(_at(11, 30))  # long absence without a break
            self.start_break(_at(11, 30, 5))
            self.wrike._Wrike__last_worktime_activity_at = _at(11, 0)
            self.activity(_at(11, 40))  # absence began before the break

        lines = [call.args[0] for call in log.call_args_list]
        self.assertTrue(any("no active manual break" in line for line in lines), lines)
        self.assertTrue(any("absence began before the break" in line for line in lines), lines)
        self.panel.show.assert_not_called()

    def test_dropped_activity_callback_is_noted_once(self):
        self.wrike._Wrike__start_activity_watcher()
        on_activity = self.watcher_type.call_args.args[1]
        self.wrike._Wrike__automatic_ui_generation += 1  # lifecycle moved on

        with patch.object(self.wrike, "_Wrike__log") as log:
            on_activity(_at(12, 0))
            on_activity(_at(12, 1))

        notes = [call.args[0] for call in log.call_args_list if "activity callback dropped" in call.args[0]]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("monitor_enabled=True", notes[0])

    def test_watcher_creation_failure_is_logged(self):
        self.watcher_type.side_effect = OSError("last input unavailable")

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.wrike._Wrike__start_activity_watcher()

        lines = [call.args[0] for call in log.call_args_list]
        self.assertTrue(any("activity watcher unavailable" in line for line in lines), lines)
        self.assertIsNone(self.wrike._Wrike__activity_watcher)

    def test_value_error_from_watcher_creation_is_logged_as_unavailable(self):
        # The constructor validates its arguments with ValueError; like OSError
        # it means "no input source here", not an unexpected failure.
        self.watcher_type.side_effect = ValueError("idle threshold")

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.wrike._Wrike__start_activity_watcher()

        lines = [call.args[0] for call in log.call_args_list]
        unavailable = [line for line in lines if line.startswith("activity watcher unavailable:")]
        self.assertEqual(len(unavailable), 1, lines)
        self.assertIn("ValueError('idle threshold')", unavailable[0])
        self.assertFalse(any("creation failed" in line for line in lines), lines)
        self.assertIsNone(self.wrike._Wrike__activity_watcher)

    def test_unexpected_watcher_creation_failure_is_logged_once(self):
        # Only OSError, ValueError and LastInputUnavailableError mean "no input
        # source here"; anything else is a creation failure and is named so.
        self.watcher_type.side_effect = RuntimeError("tk refused")

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.wrike._Wrike__start_activity_watcher()

        lines = [call.args[0] for call in log.call_args_list]
        failed = [line for line in lines if line.startswith("activity watcher creation failed:")]
        self.assertEqual(len(failed), 1, lines)
        self.assertIn("RuntimeError('tk refused')", failed[0])
        self.assertFalse(any("activity watcher unavailable" in line for line in lines), lines)
        self.assertIsNone(self.wrike._Wrike__activity_watcher)
        self.watcher_type.return_value.start.assert_not_called()

    def test_watcher_start_exception_is_logged_once(self):
        self.watcher_type.return_value.start.side_effect = RuntimeError("poll refused")

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.wrike._Wrike__start_activity_watcher()

        lines = [call.args[0] for call in log.call_args_list]
        failed = [line for line in lines if line.startswith("activity watcher start failed:")]
        self.assertEqual(len(failed), 1, lines)
        self.assertIn("RuntimeError('poll refused')", failed[0])
        self.assertFalse(any("not running after start" in line for line in lines), lines)
        self.watcher_type.return_value.start.assert_called_once_with()

    def test_watcher_that_cannot_poll_after_start_is_logged(self):
        # start() swallows a polling schedule that Tk refused and leaves the
        # watcher not running; that is a start failure too.
        self.watcher_type.return_value.is_running = False

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.wrike._Wrike__start_activity_watcher()

        lines = [call.args[0] for call in log.call_args_list]
        self.assertTrue(
            any("activity watcher start failed: not running" in line for line in lines), lines
        )

    def test_running_watcher_does_not_log_a_start_failure(self):
        self.watcher_type.return_value.is_running = True

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.wrike._Wrike__start_activity_watcher()

        lines = [call.args[0] for call in log.call_args_list]
        self.assertFalse(any("start failed" in line for line in lines), lines)

    def test_answer_that_ends_the_break_is_logged(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 40))
        _Clock.current = _at(12, 47)

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.wrike._Wrike__panel_break_return_accept("12:40")

        lines = [call.args[0] for call in log.call_args_list]
        ended = [line for line in lines if "answered, break ended" in line]
        self.assertEqual(len(ended), 1, lines)
        self.assertIn("end_at=2026-04-06T12:40:00", ended[0])
        self.assertIsNone(self.pending())

    def test_manual_toggle_logs_why_the_question_is_cleared(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 30))

        with patch.object(self.wrike, "_Wrike__log") as log:
            self.start_break(_at(12, 31))  # toggles the running break off

        lines = [call.args[0] for call in log.call_args_list]
        self.assertTrue(
            any("question cleared; break toggled by hand" in line for line in lines), lines
        )
        self.assertIsNone(self.pending())

    def test_disabling_logs_why_the_question_is_cleared(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 30))

        with patch.object(self.wrike, "_Wrike__log") as log:
            ok, error = self.wrike.update_settings({"monitor_enabled": False})

        self.assertTrue(ok, error)
        lines = [call.args[0] for call in log.call_args_list]
        self.assertTrue(
            any("question cleared; automatic UI stopped" in line for line in lines), lines
        )
        self.assertIsNone(self.pending())

    def test_brief_input_then_absence_moves_return_to_later_input(self):
        # A stray input (or the lock keystroke) after idling is followed by
        # another long absence; the later input is the real return.
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 10))
        self.activity(_at(12, 10, 30))
        self.activity(_at(13, 0))

        self.assertEqual(self.pending()["detected_at"], _at(13, 0))
        self.assertEqual(self.visible_prompt(), WorktimeBreakReturnPrompt("12:00", "13:00"))

    def test_lock_keystroke_after_idle_is_superseded_by_unlock(self):
        self.start_break(_at(12, 0))
        self.activity(_at(12, 0, 1))
        self.activity(_at(12, 10))

        self.unlock(_at(13, 0))

        self.assertEqual(self.pending()["detected_at"], _at(13, 0))
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

    def test_watcher_start_without_break_seeds_absence_at_start(self):
        _Clock.current = _at(12, 0)

        self.wrike._Wrike__start_activity_watcher()

        self.assertEqual(self.wrike._Wrike__last_worktime_activity_at, _at(12, 0))

    def test_break_running_over_restart_asks_on_first_input(self):
        # The break was left on before a shutdown; the app starts at login.
        self.start_break(_at(18, 30))
        self.assertIsNone(self.wrike._Wrike__last_worktime_activity_at)
        next_morning = datetime(2026, 4, 7, 8, 31)
        _Clock.current = next_morning

        self.wrike._Wrike__start_activity_watcher()
        self.assertEqual(self.wrike._Wrike__last_worktime_activity_at, _at(18, 30))
        self.activity(datetime(2026, 4, 7, 8, 31, 5))

        self.assertEqual(self.pending()["detected_at"], datetime(2026, 4, 7, 8, 31, 5))
        self.assertEqual(self.visible_prompt(), WorktimeBreakReturnPrompt("18:30", "08:31"))

    def test_edit_across_midnight_uses_latest_date_within_break(self):
        self.start_break(_at(23, 50))
        self.activity(_at(23, 50, 1))
        self.activity(datetime(2026, 4, 7, 0, 30))
        _Clock.current = datetime(2026, 4, 7, 0, 35)

        self.assertFalse(self.wrike._Wrike__panel_break_return_edit("00:30", "23:45"))
        self.assertFalse(self.wrike._Wrike__panel_break_return_edit("00:30", "00:36"))
        self.assertIsNotNone(self.pending())
        self.assertTrue(self.wrike._Wrike__panel_break_return_edit("00:30", "23:55"))

        self.assertEqual(self.completed_breaks(), [("2026-04-06T23:50:00", "2026-04-06T23:55:00")])
        self.assertIsNone(self.pending())


class WrikeLogRotationTest(unittest.TestCase):
    """wrike.log follows the size cap and backup count of the runtime log."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {"APPDATA": temporary.name}, clear=False):
            self.wrike = Wrike()
        self.log_path = Path(temporary.name) / "windows-supporter" / "wrike.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def backup(self, index: int) -> Path:
        return Path(f"{self.log_path}.{index}")

    def test_line_under_the_cap_is_appended_without_rotation(self):
        self.log_path.write_text("old line\n", encoding="utf-8")

        self.wrike.log_info("next line")

        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[0], "old line")
        self.assertRegex(lines[1], r"^\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\] next line$")
        self.assertFalse(self.backup(1).exists())

    def test_log_that_would_pass_the_cap_moves_to_a_backup(self):
        old = "x" * DEFAULT_MAX_LOG_BYTES
        self.log_path.write_text(old, encoding="utf-8")

        self.wrike.log_info("first line after the cap")

        self.assertEqual(self.backup(1).read_text(encoding="utf-8"), old)
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(
            lines[0], r"^\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\] first line after the cap$"
        )

    def test_failed_rotation_does_not_cost_the_line(self):
        self.log_path.write_text("old line\n", encoding="utf-8")

        with patch("src.apps.Wrike.rotate_log_file", side_effect=PermissionError("in use")):
            self.wrike.log_info("kept anyway")

        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[1].endswith("] kept anyway"), lines)

    def test_rotation_gets_the_size_the_line_takes_on_disk_under_the_log_lock(self):
        seen = []

        def spy(path, incoming_bytes):
            seen.append((path, incoming_bytes, wrike_module._WRIKE_LOG_LOCK.locked()))

        with patch("src.apps.Wrike.rotate_log_file", side_effect=spy):
            # Multi-byte text and an embedded newline: the text-mode write
            # turns each newline into os.linesep.
            self.wrike.log_info("한글\n둘째 줄")

        self.assertEqual(len(seen), 1, seen)
        path, incoming_bytes, locked = seen[0]
        self.assertEqual(Path(path), self.log_path)
        self.assertTrue(locked)
        self.assertEqual(incoming_bytes, self.log_path.stat().st_size)
        self.assertFalse(wrike_module._WRIKE_LOG_LOCK.locked())

    def test_line_is_appended_while_the_log_lock_is_held(self):
        # A writer that appends outside the lock keeps the file open while
        # another thread tries to rotate it.
        opened = []
        real_open = open

        def spy_open(path, *args, **kwargs):
            opened.append((Path(path), wrike_module._WRIKE_LOG_LOCK.locked()))
            return real_open(path, *args, **kwargs)

        with patch("src.apps.Wrike.open", side_effect=spy_open, create=True):
            self.wrike.log_info("appended under the lock")

        self.assertEqual(opened, [(self.log_path, True)])
        self.assertIn("appended under the lock", self.log_path.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "an open file is only refused a rename on Windows")
    def test_log_held_open_by_another_program_keeps_backups_and_lines(self):
        old = "x" * DEFAULT_MAX_LOG_BYTES
        self.log_path.write_text(old, encoding="utf-8")
        for index in (1, 2, 3):
            self.backup(index).write_text(f"backup {index}", encoding="utf-8")

        with self.log_path.open("rb"):
            self.wrike.log_info("first while held")
            self.wrike.log_info("second while held")

        self.assertEqual(
            [self.backup(index).read_text(encoding="utf-8") for index in (1, 2, 3)],
            ["backup 1", "backup 2", "backup 3"],
        )
        text = self.log_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(old), "the oversized log was not moved")
        self.assertEqual(
            [line.split("] ", 1)[-1] for line in text[len(old):].splitlines()],
            ["first while held", "second while held"],
        )

        self.wrike.log_info("after the handle closed")

        # The first free rotation shifts every backup exactly once.
        self.assertEqual(self.backup(1).read_text(encoding="utf-8"), text)
        self.assertEqual(self.backup(2).read_text(encoding="utf-8"), "backup 1")
        self.assertEqual(self.backup(3).read_text(encoding="utf-8"), "backup 2")
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split("] ", 1)[-1] for line in lines], ["after the handle closed"])


if __name__ == "__main__":
    unittest.main()
