from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import src.apps.Wrike as wrike_module
from src.apps.Wrike import Wrike
from src.apps.wrike_timelog_snapshot import TimelogSnapshotState


class _Root:
    def __init__(self):
        self.calls = []
        self.cancelled = []

    def after(self, delay, callback):
        identity = f"after-{len(self.calls) + 1}"
        self.calls.append((identity, delay, callback))
        return identity

    def after_cancel(self, identity):
        self.cancelled.append(identity)


class _Thread:
    created = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.created.append(self)

    def start(self):
        pass

    def run(self):
        self.target(*self.args)


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


class _FlexClient:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.open_login_page = Mock()
        self.open_work_record_page = Mock()
        self.focus_window = Mock()
        self.close = Mock()
        self.instances.append(self)

    def has_open_page(self):
        return True

    def login_in_progress(self):
        return True


class WrikeDisabledLifecycleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {"APPDATA": temporary.name}, clear=False):
            self.wrike = Wrike()
        self.root = _Root()
        self.wrike._Wrike__root = self.root
        self.wrike._Wrike__background_active = True
        self.panel = _Panel()
        self.wrike._Wrike__worktime_panel = self.panel
        self.wrike._Wrike__worktime_panel_root = self.root
        self.tooltip_type = self.enterContext(patch("src.apps.Wrike.ToolTip"))
        self.watcher_type = self.enterContext(patch("src.apps.Wrike.WorktimeActivityWatcher"))
        for method in (
            "__start_ical_polling", "__start_vacation_ical_polling",
            "__start_flex_polling", "__schedule_flex_poll",
        ):
            self.enterContext(patch.object(self.wrike, f"_Wrike{method}"))
        _Thread.created = []
        _FlexClient.instances = []

    def _set_enabled(self, enabled):
        ok, error = self.wrike.update_settings({"monitor_enabled": enabled})
        self.assertTrue(ok, error)

    def _drain(self):
        while True:
            try:
                callback = self.wrike._Wrike__ui_queue.get_nowait()
            except queue.Empty:
                return
            callback()

    def test_disabled_first_start_keeps_readonly_background_without_watcher(self):
        self.wrike._Wrike__monitor_enabled = False
        self.wrike.start_background()
        self.watcher_type.assert_not_called()
        self.assertTrue(self.wrike._Wrike__background_active)
        self.wrike._Wrike__start_ical_polling.assert_called_once_with()
        self.wrike._Wrike__start_vacation_ical_polling.assert_called_once_with()
        self.wrike._Wrike__start_flex_polling.assert_called_once_with()

    def test_disabled_blocks_manual_open_without_any_reaction(self):
        # The worktime window is part of the switched-off feature, so
        # Ctrl+Alt+W neither opens it nor reads Wrike (v0.36.2), and since
        # v0.36.5 it does not explain itself on screen either.
        self.wrike._Wrike__monitor_enabled = False
        with (
            patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh") as refresh,
            patch.object(self.wrike, "_Wrike__show_tooltip") as show_tooltip,
            patch.object(self.wrike, "_Wrike__log") as log,
        ):
            self.assertFalse(self.wrike._Wrike__show_activity_panel())
            self.wrike.show_weekly_timelog_summary(self.root)
        self.assertFalse(self.panel.is_visible())
        self.panel.show.assert_not_called()
        refresh.assert_not_called()
        show_tooltip.assert_not_called()
        log.assert_not_called()
        self.tooltip_type.assert_not_called()
        self.assertIsNone(self.wrike._Wrike__active_tooltip)
        self.assertFalse(self.wrike._Wrike__active_tooltip_automatic)
        self.assertEqual(self.root.calls, [])
        # Nothing is queued for the UI thread either (a deferred notice would be).
        self.assertTrue(self.wrike._Wrike__ui_queue.empty())
        # The notice and its text are gone.
        self.assertFalse(hasattr(wrike_module, "WRIKE_DISABLED_NOTICE"))

    def test_reenabling_restores_manual_open(self):
        self._set_enabled(False)
        self.wrike.show_weekly_timelog_summary(self.root)
        self.assertFalse(self.panel.is_visible())
        self._set_enabled(True)
        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh") as refresh:
            self.wrike.show_weekly_timelog_summary(self.root)
        self.assertTrue(self.panel.is_visible())
        refresh.assert_called_once_with(force=True)

    def test_disabled_activity_and_idle_callbacks_have_no_automatic_effect(self):
        self.wrike._Wrike__monitor_enabled = False
        with (
            patch.object(self.wrike, "_Wrike__maybe_resume_overtime_for_activity") as resume,
            patch.object(self.wrike, "_Wrike__overtime_active_state") as overtime,
        ):
            self.wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 5))
            self.wrike._Wrike__on_overtime_idle_input(datetime(2026, 4, 6, 19, 5))
        resume.assert_not_called()
        overtime.assert_not_called()
        self.panel.show.assert_not_called()

    def test_background_stop_also_blocks_automatic_ui(self):
        self.wrike._Wrike__background_active = False
        self.assertFalse(self.wrike._Wrike__show_activity_panel())
        self.wrike._Wrike__start_overtime_notice_timer(show_now=True)
        self.wrike._Wrike__show_tooltip(self.root, "automatic", automatic=True)
        self.tooltip_type.assert_not_called()

    def test_disable_stops_watcher_timers_and_hides_existing_automatic_ui(self):
        watcher = Mock()
        self.wrike._Wrike__activity_watcher = watcher
        self.panel.visible = True
        self.wrike._Wrike__monitor_after_id = "monitor"
        self.wrike._Wrike__overtime_notice_after_id = "overtime"
        self.wrike._Wrike__show_tooltip(self.root, "automatic", automatic=True)
        tooltip = self.tooltip_type.return_value
        self._set_enabled(False)
        watcher.stop.assert_called_once_with()
        self.assertFalse(self.panel.visible)
        self.assertIn("monitor", self.root.cancelled)
        self.assertIn("overtime", self.root.cancelled)
        tooltip.hide_tooltip.assert_called_once_with()
        self.assertTrue(self.wrike._Wrike__background_active)
        stored = json.loads(Path(self.wrike._Wrike__settings_path).read_text(encoding="utf-8"))
        self.assertIs(stored["monitor_enabled"], False)

    def test_failed_save_preserves_enabled_state_and_ui_lifecycle(self):
        watcher = Mock()
        self.wrike._Wrike__activity_watcher = watcher
        self.panel.visible = True
        with patch.object(self.wrike, "_Wrike__save_settings", return_value=False):
            ok, error = self.wrike.update_settings({"monitor_enabled": False})
        self.assertFalse(ok)
        self.assertEqual(error, "settings save failed")
        self.assertTrue(self.wrike._Wrike__monitor_enabled)
        self.assertTrue(self.panel.visible)
        watcher.stop.assert_not_called()
        self.panel.hide.assert_not_called()

    def test_reenable_replaces_watcher_and_discards_its_old_activity_callback(self):
        self.wrike._Wrike__start_activity_watcher()
        old_activity = self.watcher_type.call_args.args[1]
        old_idle = self.watcher_type.call_args.kwargs["idle_callback"]
        self._set_enabled(False)
        self._set_enabled(True)
        new_activity = self.watcher_type.call_args.args[1]
        self.assertEqual(self.watcher_type.call_count, 2)
        with (
            patch.object(self.wrike, "_Wrike__on_worktime_activity") as activity,
            patch.object(self.wrike, "_Wrike__on_overtime_idle_input") as idle,
        ):
            detected_at = datetime(2026, 4, 6, 8, 5)
            old_activity(detected_at)
            old_idle(detected_at)
            activity.assert_not_called()
            idle.assert_not_called()
            new_activity(detected_at)
            activity.assert_called_once_with(detected_at)

    def test_disk_reload_applies_disable_lifecycle(self):
        self._set_enabled(True)
        watcher = Mock()
        self.wrike._Wrike__activity_watcher = watcher
        self.panel.visible = True
        path = Path(self.wrike._Wrike__settings_path)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["monitor_enabled"] = False
        path.write_text(json.dumps(stored), encoding="utf-8")
        ok, error = self.wrike.reload_settings_from_disk()
        self.assertTrue(ok, error)
        watcher.stop.assert_called_once_with()
        self.assertFalse(self.panel.visible)

    def test_disable_preserves_manual_tooltip_feedback(self):
        self.wrike._Wrike__show_tooltip(self.root, "manual")
        self._set_enabled(False)
        self.tooltip_type.return_value.hide_tooltip.assert_not_called()

    def test_overtime_notices_stay_off_and_old_tick_cannot_clear_new_timer(self):
        with (
            patch.object(self.wrike, "_Wrike__overtime_tooltip_lines", return_value=[("active", None)]),
            patch.object(self.wrike, "_Wrike__overtime_active_state", return_value={"status": "active"}),
        ):
            self.wrike._Wrike__start_overtime_notice_timer(show_now=False)
            self.assertEqual(self.root.calls[-1][1], 600_000)
            old_tick = self.root.calls[-1][2]
            self._set_enabled(False)
            self.wrike._Wrike__start_overtime_notice_timer(show_now=True)
            self.tooltip_type.assert_not_called()
            self._set_enabled(True)
            self.wrike._Wrike__start_overtime_notice_timer(show_now=False)
            new_timer = self.wrike._Wrike__overtime_notice_after_id
            old_tick()
            self.assertEqual(self.wrike._Wrike__overtime_notice_after_id, new_timer)
            self.tooltip_type.assert_not_called()

    def test_cancelled_monitor_tick_cannot_run_after_off_on(self):
        self.wrike._Wrike__restart_monitor()
        old_tick = self.root.calls[-1][2]
        self._set_enabled(False)
        self._set_enabled(True)
        with patch.object(self.wrike, "_Wrike__request_timelog_snapshot_refresh") as refresh:
            old_tick()
        refresh.assert_not_called()

    def test_queued_timelog_result_keeps_data_without_old_automatic_notification(self):
        self.wrike._Wrike__wrike_api_token_session = "isolated-test-token"
        with (
            patch("src.apps.Wrike.threading.Thread", _Thread),
            patch.object(self.wrike, "_Wrike__resolve_contact_identity", return_value=("contact", "Test", None)),
            patch.object(self.wrike, "_Wrike__query_authoritative_timelogs_week", return_value=([], None)),
            patch.object(self.wrike, "_Wrike__start_timelog_title_lookup"),
            patch.object(self.wrike, "_Wrike__update_monitor_from_snapshot") as notify,
        ):
            self.wrike._Wrike__request_timelog_snapshot_refresh(force=True)
            _Thread.created[-1].run()
            self._set_enabled(False)
            self._set_enabled(True)
            self._drain()
        notify.assert_not_called()
        self.assertIs(self.wrike._Wrike__timelog_snapshot.state, TimelogSnapshotState.FRESH)
        self.assertFalse(self.wrike._Wrike__timelog_refresh_running)

    def test_flex_automatic_notice_is_suppressed_but_manual_feedback_allowed(self):
        self.wrike._Wrike__monitor_enabled = False
        arguments = dict(open_login_window=False, login_not_confirmed=True, announce=False)
        self.wrike._Wrike__announce_flex_sync_result("periodic", {}, "", "", **arguments)
        self.tooltip_type.assert_not_called()
        self.wrike._Wrike__announce_flex_sync_result("manual", {}, "", "", **arguments)
        self.tooltip_type.assert_called_once()

    def test_pending_flex_sync_keeps_readonly_result_without_off_on_notice(self):
        self.wrike._Wrike__flex_enabled = True
        with (
            patch("src.apps.Wrike.threading.Thread", _Thread),
            patch.object(self.wrike, "_Wrike__submit_flex_browser_job", return_value=(True, {})),
        ):
            self.wrike._Wrike__request_flex_sync(source="after_login")
            pending = _Thread.created[-1]
            self._set_enabled(False)
            self._set_enabled(True)
            pending.run()
            self._drain()
        self.assertEqual(self.wrike._Wrike__flex_state, "fresh")
        self.tooltip_type.assert_not_called()

    def test_queued_flex_window_failure_cannot_notify_after_off_on(self):
        self.wrike._Wrike__notify_flex_window_event(
            "open_failed", "login", {"source": "periodic", "code": "browser_error"},
        )
        self._set_enabled(False)
        self._set_enabled(True)
        self._drain()
        self.tooltip_type.assert_not_called()

    def test_pending_automatic_flex_login_cannot_open_after_off_on(self):
        with patch.object(self.wrike, "_Wrike__submit_flex_browser_job", return_value=(True, None)) as submit:
            self.wrike._Wrike__request_flex_login_window("periodic")
        payload = submit.call_args.args[1]
        self._set_enabled(False)
        self._set_enabled(True)
        response = queue.Queue()
        self.wrike._Wrike__flex_browser_queue.put(("login", payload, response))
        self.wrike._Wrike__flex_browser_queue.put(("close", None, queue.Queue()))
        with (
            patch("src.apps.Wrike.FlexBrowserClient", _FlexClient),
            patch.object(self.wrike, "_Wrike__ensure_playwright_ready", return_value=True),
        ):
            self.wrike._Wrike__flex_browser_worker_loop()
        self.assertEqual(_FlexClient.instances, [])
        self.assertFalse(response.get_nowait()[0])

    def test_disabled_flex_auth_failure_does_not_consume_next_enabled_login_notice(self):
        self.wrike._Wrike__flex_enabled = True
        self.wrike._Wrike__monitor_enabled = False
        self.wrike._Wrike__flex_sync_generation = 1
        self.wrike._Wrike__apply_flex_sync_result(
            1, None, "", ("login required", "login_required"), source="periodic",
        )
        self.assertFalse(self.wrike._Wrike__flex_login_notice_shown)
        self.assertFalse(self.wrike._Wrike__flex_login_window_open)
        self.assertNotIn("여는 중", self.wrike._Wrike__flex_last_error)
        self._set_enabled(True)
        self.wrike._Wrike__flex_sync_generation = 2
        with patch.object(self.wrike, "_Wrike__submit_flex_browser_job", return_value=(True, None)) as submit:
            self.wrike._Wrike__apply_flex_sync_result(
                2, None, "", ("login required", "login_required"), source="periodic",
            )
        self.assertEqual(submit.call_args.args[0], "login")
        self.assertTrue(self.wrike._Wrike__flex_login_notice_shown)

    def test_stale_flex_open_event_preserves_new_window_identity(self):
        old_generation = self.wrike._Wrike__automatic_ui_generation
        self._set_enabled(False)
        self._set_enabled(True)
        current_generation = self.wrike._Wrike__automatic_ui_generation
        self.wrike._Wrike__flex_enabled = True
        self.wrike._Wrike__flex_login_window_open = True
        self.wrike._Wrike__flex_login_window_automatic = True
        self.wrike._Wrike__flex_login_window_automatic_generation = current_generation
        self.wrike._Wrike__flex_browser_worker_thread = Mock(is_alive=Mock(return_value=True))
        self.wrike._Wrike__apply_flex_window_event(
            "opened", "login",
            {"source": "periodic", "result": "opened", "automatic_generation": old_generation},
        )
        self.assertTrue(self.wrike._Wrike__flex_login_window_open)
        self.assertEqual(self.wrike._Wrike__flex_login_window_automatic_generation, current_generation)
        kind, payload, _response = self.wrike._Wrike__flex_browser_queue.get_nowait()
        self.assertEqual(kind, "close_login")
        self.assertEqual(payload["automatic_generation"], old_generation)
        self.tooltip_type.assert_not_called()

    def test_flex_close_request_checks_window_generation_and_manual_ownership(self):
        for source in ("periodic", "manual"):
            with self.subTest(source=source):
                generation = self.wrike._Wrike__automatic_ui_generation
                stale_close = queue.Queue()
                current_close = queue.Queue()
                jobs = [
                    ("login", {"source": source, "automatic_generation": generation}, queue.Queue()),
                    ("close_login", {"automatic_only": True, "automatic_generation": generation - 1}, stale_close),
                    ("close_login", {"automatic_only": True, "automatic_generation": generation}, current_close),
                    ("close", None, queue.Queue()),
                ]
                for job in jobs:
                    self.wrike._Wrike__flex_browser_queue.put(job)
                with (
                    patch("src.apps.Wrike.FlexBrowserClient", _FlexClient),
                    patch.object(self.wrike, "_Wrike__ensure_playwright_ready", return_value=True),
                ):
                    self.wrike._Wrike__flex_browser_worker_loop()
                client = _FlexClient.instances[-1]
                client.open_login_page.assert_called_once_with(activate=False)
                self.assertEqual(stale_close.get_nowait(), (True, False))
                self.assertEqual(current_close.get_nowait(), (True, source == "periodic"))
                client.close.assert_called_once_with()

    def test_explicit_sync_or_login_adopts_existing_automatic_flex_window(self):
        for kind in ("sync", "login", "open"):
            with self.subTest(kind=kind):
                generation = self.wrike._Wrike__automatic_ui_generation
                self.wrike._Wrike__flex_enabled = True
                automatic_close = queue.Queue()
                manual_payload = (
                    (datetime(2026, 4, 6).date(), datetime(2026, 4, 12).date(), "E-42", datetime(2026, 4, 6, 9), False)
                    if kind == "sync"
                    else {"source": "manual", "activate": True, "automatic_generation": generation}
                )
                for job in (
                    ("login", {"source": "periodic", "automatic_generation": generation}, queue.Queue()),
                    (kind, manual_payload, queue.Queue()),
                    ("close_login", {"automatic_only": True, "automatic_generation": generation}, automatic_close),
                    ("close", None, queue.Queue()),
                ):
                    self.wrike._Wrike__flex_browser_queue.put(job)
                with (
                    patch("src.apps.Wrike.FlexBrowserClient", _FlexClient),
                    patch.object(self.wrike, "_Wrike__ensure_playwright_ready", return_value=True),
                ):
                    self.wrike._Wrike__flex_browser_worker_loop()
                self._drain()
                self.assertEqual(automatic_close.get_nowait(), (True, False))
                self.assertFalse(self.wrike._Wrike__flex_login_window_automatic)
                self.assertIsNone(self.wrike._Wrike__flex_login_window_automatic_generation)
                if kind != "open":
                    _FlexClient.instances[-1].focus_window.assert_called_once_with()

    def test_failed_disable_save_never_exposes_candidate_flag_to_queued_login(self):
        generation = self.wrike._Wrike__automatic_ui_generation
        self.wrike._Wrike__flex_enabled = True
        self.wrike._Wrike__flex_login_notice_shown = True
        self.wrike._Wrike__flex_login_window_open = True
        response = queue.Queue()
        observed = []

        def failed_save(**_kwargs):
            for job in (
                ("login", {"source": "periodic", "automatic_generation": generation}, response),
                ("close", None, queue.Queue()),
            ):
                self.wrike._Wrike__flex_browser_queue.put(job)

            def consume_queued_login():
                observed.append(self.wrike._Wrike__automatic_ui_allowed(generation))
                self.wrike._Wrike__flex_browser_worker_loop()

            worker = threading.Thread(target=consume_queued_login, daemon=True)
            worker.start()
            worker.join(timeout=2.0)
            self.assertFalse(worker.is_alive(), "persistence must not deadlock the browser worker")
            return False

        with (
            patch("src.apps.Wrike.FlexBrowserClient", _FlexClient),
            patch.object(self.wrike, "_Wrike__ensure_playwright_ready", return_value=True),
            patch.object(self.wrike, "_Wrike__save_settings", side_effect=failed_save),
        ):
            ok, error = self.wrike.update_settings({"monitor_enabled": False})
        self.assertFalse(ok)
        self.assertEqual(error, "settings save failed")
        self.assertEqual(observed, [True])
        self.assertEqual(response.get_nowait(), (True, "opened"))
        self.assertTrue(self.wrike._Wrike__monitor_enabled)
        self.assertTrue(self.wrike._Wrike__flex_login_notice_shown)
        self.assertTrue(self.wrike._Wrike__flex_login_window_open)

    def test_manual_adoption_cannot_be_demoted_by_older_opened_event(self):
        generation = self.wrike._Wrike__automatic_ui_generation
        self.wrike._Wrike__flex_enabled = True
        self.wrike._Wrike__flex_login_window_open = True
        self.wrike._Wrike__flex_login_window_automatic = True
        self.wrike._Wrike__flex_login_window_automatic_generation = generation
        with patch.object(self.wrike, "_Wrike__submit_flex_browser_job", return_value=(True, None)):
            self.wrike._Wrike__request_flex_login_window("manual")
        self.wrike._Wrike__apply_flex_window_event(
            "opened", "login",
            {"source": "periodic", "result": "opened", "automatic_generation": generation},
        )
        self.assertFalse(self.wrike._Wrike__flex_login_window_automatic)
        self.assertIsNone(self.wrike._Wrike__flex_login_window_automatic_generation)

    def test_successful_disable_publishes_flag_only_after_persistence(self):
        observed = []
        original_save = self.wrike._Wrike__save_settings

        def observe_save(**kwargs):
            observed.append((self.wrike._Wrike__monitor_enabled, self.wrike._Wrike__automatic_ui_allowed()))
            return original_save(**kwargs)

        with patch.object(self.wrike, "_Wrike__save_settings", side_effect=observe_save):
            self._set_enabled(False)
        self.assertEqual(observed, [(True, True)])
        self.assertFalse(self.wrike._Wrike__monitor_enabled)
        self.assertFalse(self.wrike._Wrike__automatic_ui_allowed())
        stored = json.loads(Path(self.wrike._Wrike__settings_path).read_text(encoding="utf-8"))
        self.assertIs(stored["monitor_enabled"], False)

    def test_first_load_and_repair_reload_publish_saved_monitor_setting(self):
        self._set_enabled(False)
        with patch.dict(os.environ, {"APPDATA": str(Path(self.wrike._Wrike__settings_path).parent.parent)}, clear=False):
            loaded = Wrike()
        self.assertFalse(loaded._Wrike__monitor_enabled)
        self.wrike._Wrike__monitor_enabled = True
        path = Path(self.wrike._Wrike__settings_path)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored.pop("settings_version")
        path.write_text(json.dumps(stored), encoding="utf-8")
        observed = []
        original_save = self.wrike._Wrike__save_settings

        def observe_save(**kwargs):
            observed.append(self.wrike._Wrike__monitor_enabled)
            return original_save(**kwargs)

        with patch.object(self.wrike, "_Wrike__save_settings", side_effect=observe_save):
            ok, error = self.wrike.reload_settings_from_disk()
        self.assertTrue(ok, error)
        self.assertEqual(observed, [True])
        self.assertFalse(self.wrike._Wrike__monitor_enabled)


if __name__ == "__main__":
    unittest.main()
