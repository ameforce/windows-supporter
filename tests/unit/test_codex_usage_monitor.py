import json
import os
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.apps.codex_local_usage import LocalCodexUsageSnapshot
from src.apps.codex_usage_browser_types import (
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
)
from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    UsageSnapshot,
    are_equivalent_codex_usage_urls,
    build_codex_login_entry_url,
    canonicalize_codex_usage_url,
    compute_usage_changes,
    extract_usage_metrics_from_semantic_blocks,
    merge_snapshot_with_previous,
    normalize_usage_value,
    parse_usage_metrics_from_text,
    sanitize_profile_name,
)


class CodexUsageMonitorUnitTest(unittest.TestCase):
    class _BrowserSession:
        def __init__(self) -> None:
            self.collect_result = BrowserOperationResult()
            self.open_login_result = BrowserOperationResult()
            self.poll_login_result = BrowserOperationResult()
            self.calls: list[str] = []
            self.status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

        def collect(self) -> BrowserOperationResult:
            self.calls.append("collect")
            return self.collect_result

        def open_login(self) -> BrowserOperationResult:
            self.calls.append("open_login")
            return self.open_login_result

        def poll_login(self) -> BrowserOperationResult:
            self.calls.append("poll_login")
            return self.poll_login_result

        def close_session(self) -> None:
            self.calls.append("close_session")

        def shutdown(self) -> bool:
            self.calls.append("shutdown")
            return True

        def get_runtime_status(self) -> BrowserRuntimeStatus:
            return self.status

    def test_shutdown_closes_playwright_session(self) -> None:
        class _BrowserSession:
            def __init__(self) -> None:
                self.shutdown_calls = 0

            def shutdown(self) -> bool:
                self.shutdown_calls += 1
                return True

        with tempfile.TemporaryDirectory() as tmp:
            session = _BrowserSession()
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: session,
            )
            monitor.shutdown()

            self.assertEqual(session.shutdown_calls, 1)

    def test_worker_does_not_run_tk_cleanup_directly_when_ui_queue_post_fails(self) -> None:
        class _FailingQueue:
            def put(self, _fn):
                raise RuntimeError("ui queue unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            monitor._CodexUsageMonitor__ui_thread_id = threading.get_ident()
            monitor._CodexUsageMonitor__event_queue = _FailingQueue()
            callback_thread_ids = []

            worker = threading.Thread(
                target=lambda: monitor._CodexUsageMonitor__post_tk_cleanup(
                    lambda: callback_thread_ids.append(threading.get_ident())
                )
            )
            worker.start()
            worker.join(1.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(callback_thread_ids, [])

    def test_usage_url_change_rejects_unconfirmed_old_session_shutdown(self) -> None:
        class _UnsettledBrowserSession:
            def __init__(self) -> None:
                self.shutdown_calls = 0

            def shutdown(self) -> bool:
                self.shutdown_calls += 1
                return False

        with tempfile.TemporaryDirectory() as tmp:
            sessions = []

            def factory(_config):
                session = _UnsettledBrowserSession()
                sessions.append(session)
                return session

            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=factory,
            )
            before = monitor.get_settings_snapshot()
            old_session = monitor._CodexUsageMonitor__browser_session

            result = monitor.update_settings(
                {"usage_url": "https://example.invalid/different-usage"}
            )

            self.assertEqual(result, (False, "browser_session_shutdown_failed"))
            self.assertEqual(len(sessions), 1)
            self.assertEqual(old_session.shutdown_calls, 1)
            self.assertIs(monitor._CodexUsageMonitor__browser_session, old_session)
            self.assertEqual(
                monitor.get_settings_snapshot()["usage_url"],
                before["usage_url"],
            )

    def test_usage_url_change_never_restores_a_shutdown_session_when_factories_fail(self) -> None:
        class _BrowserSession:
            def __init__(self) -> None:
                self.shutdown_calls = 0

            def shutdown(self) -> bool:
                self.shutdown_calls += 1
                return True

        with tempfile.TemporaryDirectory() as tmp:
            old_session = _BrowserSession()
            factory_calls = 0

            def factory(_config):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls == 1:
                    return old_session
                raise RuntimeError("session factory failed")

            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=factory,
            )
            before = monitor.get_settings_snapshot()["usage_url"]

            result = monitor.update_settings(
                {"usage_url": "https://example.invalid/different-usage"}
            )

            self.assertEqual(result, (False, "browser_session_create_failed"))
            self.assertEqual(old_session.shutdown_calls, 1)
            self.assertIsNot(
                monitor._CodexUsageMonitor__browser_session,
                old_session,
            )
            self.assertEqual(
                monitor.get_settings_snapshot()["usage_url"],
                before,
            )
            self.assertEqual(
                monitor._CodexUsageMonitor__browser_session.get_runtime_status().state,
                BrowserState.FAILED,
            )

    def test_collect_cancel_interrupts_active_browser_session(self) -> None:
        class _BlockingBrowserSession:
            def __init__(self) -> None:
                self.collect_started = threading.Event()
                self.collect_release = threading.Event()
                self.shutdown_calls = 0

            def collect(self) -> BrowserOperationResult:
                self.collect_started.set()
                self.collect_release.wait(2.0)
                return BrowserOperationResult(error="collect_failed")

            def open_login(self) -> BrowserOperationResult:
                return BrowserOperationResult()

            def poll_login(self) -> BrowserOperationResult:
                return BrowserOperationResult()

            def close_session(self) -> None:
                return None

            def shutdown(self) -> bool:
                self.shutdown_calls += 1
                self.collect_release.set()
                return True

            def get_runtime_status(self) -> BrowserRuntimeStatus:
                return BrowserRuntimeStatus(BrowserState.HEADLESS_READY, False, "")

        with tempfile.TemporaryDirectory() as tmp:
            session = _BlockingBrowserSession()
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: session,
            )
            monitor.attach(object(), None, start_monitor=False)
            monitor._CodexUsageMonitor__set_session_state("logged_in")
            collect_thread = threading.Thread(
                target=lambda: monitor.show_current_status(
                    force_refresh=True,
                    source="auto_monitor",
                ),
                daemon=True,
            )
            collect_thread.start()
            self.assertTrue(session.collect_started.wait(1.0))

            monitor.request_collect_cancel()
            collect_thread.join(1.0)
            completed_after_cancel = not collect_thread.is_alive()
            if not completed_after_cancel:
                session.collect_release.set()
                collect_thread.join(2.0)

            self.assertTrue(completed_after_cancel)
            self.assertEqual(session.shutdown_calls, 1)

    def test_ui_thread_monitor_pause_cancels_timer_without_requeueing_cleanup(self) -> None:
        class _Root:
            def __init__(self) -> None:
                self.after_cancel_calls = []

            def after_cancel(self, after_id) -> None:
                self.after_cancel_calls.append(after_id)

        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            root = _Root()
            event_queue = queue.Queue()
            monitor.attach(root, event_queue, start_monitor=False)
            monitor._CodexUsageMonitor__monitor_after_id = "manual-query-timer"

            monitor._CodexUsageMonitor__pause_monitor_countdown_for_manual_query()

            self.assertEqual(root.after_cancel_calls, ["manual-query-timer"])
            self.assertTrue(event_queue.empty())

    def test_stale_monitor_tick_does_not_start_after_worker_thread_shutdown(self) -> None:
        class _Root:
            def after_cancel(self, _after_id) -> None:
                return

        class _RecordingThread:
            starts = 0

            def __init__(self, target=None, daemon=None):
                _ = (target, daemon)

            def start(self):
                type(self).starts += 1

        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            event_queue = queue.Queue()
            monitor.attach(_Root(), event_queue, start_monitor=False)
            monitor._CodexUsageMonitor__set_session_state("logged_in")
            monitor._CodexUsageMonitor__monitor_after_id = "stale-timer"

            shutdown_thread = threading.Thread(target=monitor.shutdown, daemon=True)
            shutdown_thread.start()
            shutdown_thread.join(1.0)
            self.assertFalse(shutdown_thread.is_alive())
            self.assertEqual(event_queue.qsize(), 1)

            with patch("src.apps.codex_usage_monitor.threading.Thread", _RecordingThread):
                monitor._CodexUsageMonitor__monitor_tick()

            self.assertEqual(_RecordingThread.starts, 0)
            self.assertFalse(monitor._CodexUsageMonitor__monitor_running)

    def test_collect_snapshot_routes_only_through_playwright_session_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._BrowserSession()
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: session,
            )
            session.collect_result = BrowserOperationResult(
                probe=self._usage_probe("")
            )
            session.open_login_result = BrowserOperationResult(error="login_required")
            session.poll_login_result = BrowserOperationResult(error="login_window_closed")

            snapshot, collect_error = monitor._CodexUsageMonitor__collect_snapshot(
                source="manual_query"
            )
            _login_snapshot, login_error = monitor._CodexUsageMonitor__collect_snapshot(
                source="manual_login"
            )
            _poll_snapshot, poll_error = monitor._CodexUsageMonitor__collect_snapshot(
                source="pending_login_poll"
            )

            self.assertIsNotNone(snapshot)
            self.assertIsNone(collect_error)
            self.assertEqual(login_error, "login_required")
            self.assertEqual(poll_error, "login_window_closed")
            self.assertEqual(session.calls, ["collect", "open_login", "poll_login"])

    def test_runtime_status_exposes_transport_neutral_browser_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._BrowserSession()
            session.status = BrowserRuntimeStatus(
                BrowserState.HEADED_LOGIN,
                True,
                "login_required",
            )
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: session,
            )

            runtime = monitor.get_runtime_status()

            self.assertEqual(runtime["collection_mode"], "playwright")
            self.assertEqual(runtime["browser_state"], "headed_login")
            self.assertTrue(runtime["login_window_open"])
            self.assertEqual(runtime["browser_last_error"], "login_required")
            self.assertEqual(runtime["browser_retry_attempt"], 0)
            self.assertEqual(runtime["browser_retry_max"], 0)

    def test_runtime_status_exposes_timeout_retry_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._BrowserSession()
            session.status = BrowserRuntimeStatus(
                BrowserState.RECOVERING,
                False,
                "command_timeout",
                2,
                3,
            )
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: session,
            )

            runtime = monitor.get_runtime_status()

            self.assertEqual(runtime["browser_state"], "recovering")
            self.assertEqual(runtime["browser_last_error"], "command_timeout")
            self.assertEqual(runtime["browser_retry_attempt"], 2)
            self.assertEqual(runtime["browser_retry_max"], 3)

    def test_command_timeout_user_message_explains_automatic_connection_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )

            message = monitor._CodexUsageMonitor__describe_collect_error_for_user(
                "command_timeout"
            )

            self.assertIn("시간이 초과", message)
            self.assertIn("연결을 복구", message)
            self.assertIn("자동 재시도", message)

    def test_browser_channel_failure_keeps_last_successful_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._BrowserSession()
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: session,
            )
            previous = UsageSnapshot.from_metrics(
                {"weekly_limit": "48%"},
                captured_at="2026-07-14T15:00:00+09:00",
            )
            monitor.handle_snapshot(previous)

            monitor._CodexUsageMonitor__handle_collect_error(
                "browser_channel_unavailable",
                source="monitor_tick",
            )

            self.assertEqual(monitor.get_last_snapshot().weekly_limit, "48%")

    def test_profile_in_use_pauses_background_collection_until_manual_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._BrowserSession()
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: session,
            )
            monitor._CodexUsageMonitor__set_session_state("logged_in")
            monitor._CodexUsageMonitor__profile_in_use_detected = True

            runtime = monitor.get_runtime_status()

            self.assertEqual(runtime["monitor_state"], "paused_profile_in_use")
            self.assertFalse(runtime["auto_monitoring_active"])

    def test_logout_stops_browser_session_before_deleting_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events: list[str] = []
            session = self._BrowserSession()

            def shutdown() -> bool:
                events.append("shutdown")
                return True

            session.shutdown = shutdown
            replacement = self._BrowserSession()
            sessions = iter((session, replacement))
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: next(sessions),
            )

            def clear_profile() -> tuple[bool, str]:
                events.append("clear_profile")
                return True, "로그아웃되었습니다."

            with patch.object(
                monitor,
                "_CodexUsageMonitor__clear_profile_directory",
                side_effect=clear_profile,
            ):
                ok, _message = monitor.release_profile_session()

            self.assertTrue(ok)
            self.assertEqual(events, ["shutdown", "shutdown", "clear_profile"])

    def test_logout_hard_cancels_active_browser_collect_before_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor_ref = []

            class _CancellingBrowserSession(self._BrowserSession):
                def __init__(self) -> None:
                    super().__init__()
                    self.request_cancel_calls = 0

                def request_cancel(self) -> bool:
                    self.request_cancel_calls += 1
                    monitor_ref[0]._CodexUsageMonitor__collect_lock.release()
                    return True

            session = _CancellingBrowserSession()
            replacement = self._BrowserSession()
            sessions = iter((session, replacement))
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: next(sessions),
            )
            monitor_ref.append(monitor)
            self.assertTrue(monitor._CodexUsageMonitor__collect_lock.acquire(False))
            monitor._CodexUsageMonitor__release_wait_timeout_sec = 0.2

            with patch.object(
                monitor,
                "_CodexUsageMonitor__clear_profile_directory",
                return_value=(True, "로그아웃되었습니다."),
            ):
                ok, message = monitor.release_profile_session()

            self.assertTrue(ok, message)
            self.assertEqual(session.request_cancel_calls, 1)
            self.assertIn("shutdown", session.calls)

    def test_logout_replaces_terminal_cancelled_browser_session_before_reconnect(self) -> None:
        class _TerminalOnCancelSession(self._BrowserSession):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled = False

            def request_cancel(self) -> bool:
                self.calls.append("request_cancel")
                self.cancelled = True
                return True

            def open_login(self) -> BrowserOperationResult:
                self.calls.append("open_login")
                if self.cancelled:
                    return BrowserOperationResult(error="collect_failed")
                return BrowserOperationResult()

        with tempfile.TemporaryDirectory() as tmp:
            sessions: list[_TerminalOnCancelSession] = []

            def factory(_config):
                session = _TerminalOnCancelSession()
                sessions.append(session)
                return session

            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=factory,
            )
            with patch.object(
                monitor,
                "_CodexUsageMonitor__clear_profile_directory",
                return_value=(True, "로그아웃되었습니다."),
            ):
                ok, message = monitor.release_profile_session()

            self.assertTrue(ok, message)
            self.assertEqual(len(sessions), 2)
            self.assertIn("shutdown", sessions[0].calls)
            replacement = monitor._CodexUsageMonitor__browser_session
            self.assertIs(replacement, sessions[1])
            self.assertIsNone(replacement.open_login().error)

    def test_logout_does_not_delete_profile_when_replacement_session_creation_fails(self) -> None:
        class _TerminalSession(self._BrowserSession):
            def request_cancel(self) -> bool:
                self.calls.append("request_cancel")
                return True

        with tempfile.TemporaryDirectory() as tmp:
            session = _TerminalSession()
            factory_calls = 0

            def factory(_config):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls == 1:
                    return session
                raise RuntimeError("replacement unavailable")

            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=factory,
            )
            with patch.object(
                monitor,
                "_CodexUsageMonitor__clear_profile_directory",
            ) as clear_profile:
                ok, message = monitor.release_profile_session()

            self.assertFalse(ok)
            self.assertIn("다시 준비하지 못했습니다", message)
            clear_profile.assert_not_called()
            self.assertTrue(monitor._CodexUsageMonitor__browser_session_recovery_required)

    def test_logout_removes_dynamic_app_owned_profile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(
                tmp,
                "windows-supporter",
                "ai-profiles",
                f"profile_{'a' * 32}",
                "codex",
            )
            os.makedirs(profile_dir, exist_ok=True)
            with open(os.path.join(profile_dir, "marker.txt"), "w", encoding="utf-8") as fp:
                fp.write("managed")
            session = self._BrowserSession()
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                monitor = CodexUsageMonitor(
                    config_dir=os.path.join(tmp, "config"),
                    profile_dir=profile_dir,
                    browser_session_factory=lambda _config: session,
                )

            ok, message = monitor.release_profile_session()

            self.assertTrue(ok, message)
            self.assertFalse(os.path.exists(profile_dir))

    def test_logout_honors_explicit_managed_profile_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            managed_root = os.path.join(tmp, "custom-local", "windows-supporter")
            profile_dir = os.path.join(
                managed_root,
                "ai-profiles",
                f"profile_{'b' * 32}",
                "codex",
            )
            os.makedirs(profile_dir, exist_ok=True)
            with open(os.path.join(profile_dir, "marker.txt"), "w", encoding="utf-8") as fp:
                fp.write("managed")
            session = self._BrowserSession()
            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": os.path.join(tmp, "default-local")},
            ):
                monitor = CodexUsageMonitor(
                    config_dir=os.path.join(tmp, "config"),
                    profile_dir=profile_dir,
                    managed_profile_root=managed_root,
                    browser_session_factory=lambda _config: session,
                )

            ok, message = monitor.release_profile_session()

            self.assertTrue(ok, message)
            self.assertFalse(os.path.exists(profile_dir))

    def test_logout_rejects_dynamic_profile_resolving_outside_app_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(
                tmp,
                "windows-supporter",
                "ai-profiles",
                f"profile_{'c' * 32}",
                "codex",
            )
            os.makedirs(profile_dir, exist_ok=True)
            marker = os.path.join(profile_dir, "preserve.txt")
            with open(marker, "w", encoding="utf-8") as fp:
                fp.write("outside-backed")
            outside = os.path.join(tmp, "outside", "codex")
            real_realpath = os.path.realpath

            def junction_realpath(path):
                if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
                    os.path.abspath(profile_dir)
                ):
                    return outside
                return real_realpath(path)

            session = self._BrowserSession()
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                monitor = CodexUsageMonitor(
                    config_dir=os.path.join(tmp, "config"),
                    profile_dir=profile_dir,
                    browser_session_factory=lambda _config: session,
                )

            with patch(
                "src.apps.codex_usage_monitor.os.path.realpath",
                side_effect=junction_realpath,
            ):
                ok, _message = monitor.release_profile_session()

            self.assertFalse(ok)
            self.assertTrue(os.path.isfile(marker))

    def test_pending_login_timeout_closes_headed_session_after_fifteen_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._BrowserSession()
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: session,
            )
            monitor._CodexUsageMonitor__set_session_state("logged_in")
            monitor._CodexUsageMonitor__pending_login_poll_until_ts = 900.0

            with patch.object(
                monitor._CodexUsageMonitor__lib.time,
                "monotonic",
                return_value=900.0,
            ):
                monitor._CodexUsageMonitor__pending_login_poll_tick()

            self.assertEqual(session.calls, ["close_session"])
            self.assertEqual(
                monitor.get_runtime_status()["session_state"],
                "logged_out",
            )

    def test_canonicalize_codex_usage_url_promotes_legacy_usage_path_to_analytics_hash(self) -> None:
        self.assertEqual(
            canonicalize_codex_usage_url("https://chatgpt.com/codex/settings/usage"),
            "https://chatgpt.com/codex/cloud/settings/analytics#usage",
        )

    def test_build_codex_login_entry_url_targets_analytics_hash_path(self) -> None:
        self.assertEqual(
            build_codex_login_entry_url("https://chatgpt.com/codex/settings/usage"),
            "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
        )

    def test_build_codex_login_entry_url_preserves_analytics_fragment_for_direct_input(self) -> None:
        self.assertEqual(
            build_codex_login_entry_url(
                "https://chatgpt.com/codex/cloud/settings/analytics#usage"
            ),
            "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
        )

    def test_are_equivalent_codex_usage_urls_treats_fragmentless_analytics_variant_as_same_target(self) -> None:
        self.assertTrue(
            are_equivalent_codex_usage_urls(
                "https://chatgpt.com/codex/cloud/settings/analytics",
                "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            )
        )

    def test_normalize_usage_value_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_usage_value("""  12 / 40 

  left """),
            "12 / 40 left",
        )

    def test_sanitize_profile_name_rejects_menu_button_labels(self) -> None:
        for value in (
            "메뉴 열기",
            "프로필 메뉴 열기",
            "Open menu",
            "profile",
            "설정",
            "사용자 지정",
            "그룹화 기준: 일별",
        ):
            with self.subTest(value=value):
                self.assertEqual(sanitize_profile_name(value), "")

    def test_sanitize_profile_name_rejects_account_chrome_aria_labels(self) -> None:
        for value in (
            "Account menu",
            "Profile menu",
            "User menu",
            "My Account",
            "Your account",
            "Edit profile",
            "Switch account",
            "View account",
            "Open account menu",
            "Open profile",
            "내 계정",
            "나의 계정",
            "내 프로필",
        ):
            with self.subTest(value=value):
                self.assertEqual(sanitize_profile_name(value), "")

    def test_sanitize_profile_name_keeps_name_after_account_menu_prefix(self) -> None:
        # Full chrome prefix must strip so the display name remains.
        self.assertEqual(sanitize_profile_name("Account menu: Alice"), "Alice")
        self.assertEqual(sanitize_profile_name("Account menu: Daeng"), "Daeng")
        self.assertEqual(sanitize_profile_name("Profile menu - Daeng"), "Daeng")
        self.assertEqual(sanitize_profile_name("계정 메뉴: Alice"), "Alice")
        self.assertEqual(sanitize_profile_name("프로필 메뉴: Bob"), "Bob")
        self.assertEqual(sanitize_profile_name("My Account: Alice"), "Alice")
        self.assertEqual(sanitize_profile_name("Your profile: Bob"), "Bob")
        self.assertEqual(sanitize_profile_name("내 계정: Alice"), "Alice")

    def test_sanitize_profile_name_keeps_real_profile_name(self) -> None:
        self.assertEqual(sanitize_profile_name("Profile: Daeng"), "Daeng")
        self.assertEqual(sanitize_profile_name("이니미니"), "이니미니")

    def test_sanitize_profile_name_strips_plan_badge_suffix(self) -> None:
        self.assertEqual(sanitize_profile_name("이 PRO"), "이")

    def _usage_probe(self, profile_name: str) -> dict:
        return {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            "mainText": "Analytics usage 5-hour usage limit 99% weekly usage limit 96%",
            "profileName": profile_name,
            "metricBlocks": [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "value_candidates": ["99%"],
                    "block_text": "5-hour usage limit 99%",
                },
                {
                    "metric_key": "weekly_limit",
                    "label_text": "weekly usage limit",
                    "value_candidates": ["96%"],
                    "block_text": "weekly usage limit 96%",
                },
            ],
        }

    def test_build_snapshot_from_probe_binds_first_profile_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("Kim Jong")
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(
                monitor.get_runtime_status().get("profile_name"),
                "Kim Jong",
            )

    def test_build_snapshot_from_probe_adopts_renamed_profile_name(self) -> None:
        # Given: a bound profile name collected from the same logged-in session.
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            monitor._CodexUsageMonitor__profile_name = "Kim Jong"

            # When: the provider page reports the same session under a renamed
            # display name, the fresh scrape must be adopted, not rejected.
            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("Kim J.")
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(
                monitor.get_runtime_status().get("profile_name"),
                "Kim J.",
            )

    def test_build_snapshot_from_probe_rejects_conflicting_bound_account_id(self) -> None:
        # Given: an account identity is bound from a previous successful scrape.
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            monitor._CodexUsageMonitor__account_id = "acct-bound"

            # When: the probe presents a different account identity.
            probe = self._usage_probe("Kim Jong")
            probe["accountId"] = "acct-other"

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(probe)

            # Then: the snapshot is rejected as a cross-account payload.
            self.assertIsNone(snapshot)
            self.assertEqual(
                monitor.get_runtime_status().get("profile_name"),
                "",
            )

    def test_build_snapshot_from_probe_accepts_rename_with_matching_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            monitor._CodexUsageMonitor__account_id = "acct-bound"

            probe = self._usage_probe("Kim Jong Park")
            probe["accountId"] = "acct-bound"

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(probe)

            self.assertIsNotNone(snapshot)
            self.assertEqual(
                monitor.get_runtime_status().get("profile_name"),
                "Kim Jong Park",
            )

    def test_account_identity_binding_survives_state_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            probe = self._usage_probe("Kim Jong")
            probe["accountId"] = "acct-bound"
            self.assertIsNotNone(
                monitor._CodexUsageMonitor__build_snapshot_from_probe(probe)
            )
            monitor._CodexUsageMonitor__save_state()

            reloaded = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
            )

            conflicting = self._usage_probe("Kim Jong")
            conflicting["accountId"] = "acct-other"
            self.assertIsNone(
                reloaded._CodexUsageMonitor__build_snapshot_from_probe(conflicting)
            )
            matching = self._usage_probe("Kim J.")
            matching["accountId"] = "acct-bound"
            self.assertIsNotNone(
                reloaded._CodexUsageMonitor__build_snapshot_from_probe(matching)
            )
            self.assertEqual(
                reloaded.get_runtime_status().get("profile_name"),
                "Kim J.",
            )

    def test_build_snapshot_from_probe_keeps_bound_profile_name_when_probe_name_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            monitor._CodexUsageMonitor__profile_name = "Kim Jong"

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("")
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(
                monitor.get_runtime_status().get("profile_name"),
                "Kim Jong",
            )

    def test_build_snapshot_from_probe_accepts_empty_profile_name_when_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("")
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(monitor.get_runtime_status().get("profile_name"), "")

    def test_build_snapshot_keeps_web_value_when_local_provider_fails(self) -> None:
        # Given: web collection succeeds while the optional local adapter raises.
        def broken_local_provider():
            raise OSError("rollout unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                local_usage_provider=broken_local_provider,
            )

            # When: a valid web probe crosses the acquisition boundary.
            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("Kim Jong")
            )

        # Then: the optional adapter failure cannot discard authoritative web data.
        if snapshot is None:
            self.fail("valid web snapshot was discarded")
        self.assertEqual(snapshot.weekly_limit, "96%")

    def test_build_snapshot_applies_local_usage_to_matching_web_account(self) -> None:
        # Given: the web session and Windows Codex auth expose the same stable account ID.
        local = LocalCodexUsageSnapshot(
            captured_at="2026-07-13T00:52:19.258Z",
            account_id="acct-local",
            plan_type="pro",
            weekly_limit="95%",
            weekly_limit_reset_at="2026-07-20T04:01:12+09:00",
            reported_metric_keys=("weekly_limit",),
        )
        probe = self._usage_probe("Kim Jong")
        probe["accountId"] = "acct-local"
        probe["planType"] = "pro"
        probe["metricBlocks"][1]["reset_at_candidates"] = [
            "2026-07-20T04:01:00+09:00"
        ]
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                local_usage_provider=lambda: local,
            )
            monitor._CodexUsageMonitor__now_iso = lambda: "2026-07-13T09:52:20+09:00"

            # When: the same-account probe crosses the acquisition boundary.
            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(probe)

        # Then: the fresher local remaining value replaces lagging web analytics.
        if snapshot is None:
            self.fail("valid same-account snapshot was discarded")
        self.assertEqual(snapshot.weekly_limit, "95%")
        self.assertEqual(snapshot.five_hour_limit, "")

    def test_parse_usage_metrics_from_inline_lines(self) -> None:
        raw = """
        5시간 사용 한도: 12 / 40
        주간 사용 한도: 111 / 300
        gpt-5.3-codex-spark 5시간 사용 한도: 8 / 10
        gpt-5.3-codex-spark 주간 사용 한도: 80 / 100
        남은 크레딧: 320
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "70%")
        self.assertEqual(parsed.get("weekly_limit"), "63%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "20%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "20%")
        self.assertEqual(parsed.get("remaining_credit"), "320")

    def test_parse_usage_percent_converts_explicit_used_value_to_remaining(self) -> None:
        # Given: Codex reports a weekly window as an explicit used percentage.
        raw = "weekly usage limit: 5% used"

        # When: the external value crosses the usage parser boundary.
        parsed = parse_usage_metrics_from_text(raw)

        # Then: the snapshot contract contains remaining percentage.
        self.assertEqual(parsed.get("weekly_limit"), "95%")

    def test_parse_usage_ratio_converts_used_over_limit_to_remaining(self) -> None:
        # Given: Codex reports a five-hour window as used tokens over its limit.
        raw = "5-hour usage limit: 17 / 40"

        # When: the external ratio crosses the usage parser boundary.
        parsed = parse_usage_metrics_from_text(raw)

        # Then: the snapshot contract contains remaining percentage.
        self.assertEqual(parsed.get("five_hour_limit"), "57.5%")

    def test_semantic_usage_block_converts_explicit_used_value_to_remaining(self) -> None:
        # Given: the live DOM candidate explicitly qualifies its percentage as used.
        blocks = [
            {
                "metric_key": "weekly_limit",
                "label_text": "weekly usage limit",
                "value_candidates": ["5% used"],
                "block_text": "weekly usage limit 5% used",
            }
        ]

        # When: the semantic DOM contract is parsed.
        parsed = extract_usage_metrics_from_semantic_blocks(blocks)

        # Then: all acquisition paths expose the same remaining-percentage contract.
        self.assertEqual(parsed.get("weekly_limit"), "95%")

    def test_snapshot_from_dict_migrates_legacy_used_ratio_to_remaining(self) -> None:
        # Given: persisted state contains the legacy used-over-limit representation.
        payload = {
            "five_hour_limit": "17 / 40",
            "captured_at": "2026-07-13T09:48:03+09:00",
        }

        # When: the cache payload crosses the snapshot boundary.
        snapshot = UsageSnapshot.from_dict(payload)

        # Then: loaded state follows the canonical remaining-percentage contract.
        self.assertEqual(snapshot.five_hour_limit, "57.5%")

    def test_parse_usage_metrics_from_multiline_blocks(self) -> None:
        raw = """
        5시간 사용 한도
        15 / 40
        주간 사용 한도
        123 / 300
        gpt-5.3-codex-spark 5시간 사용 한도
        10 / 12
        gpt-5.3-codex-spark 주간 사용 한도
        84 / 100
        남은 크레딧
        287
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "62.5%")
        self.assertEqual(parsed.get("weekly_limit"), "59%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "16.6667%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "16%")
        self.assertEqual(parsed.get("remaining_credit"), "287")

    def test_parse_usage_metrics_prefers_spark_specific_labels_over_generic_suffix_matches(self) -> None:
        raw = """
        5시간 사용 한도
        80%
        주간 사용 한도
        68%
        gpt-5.3-codex-spark 5시간 사용 한도
        83%
        gpt-5.3-codex-spark 주간 사용 한도
        95%
        남은 크레딧
        903
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "80%")
        self.assertEqual(parsed.get("weekly_limit"), "68%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "83%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "95%")
        self.assertEqual(parsed.get("remaining_credit"), "903")

    def test_extract_usage_metrics_from_semantic_blocks_ignores_unknown_block(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "value_candidates": ["26%"],
                    "block_text": "5-hour usage limit 26%",
                },
                {
                    "metric_key": "experimental_metric",
                    "label_text": "Experimental",
                    "value_candidates": ["999"],
                    "block_text": "Experimental 999",
                },
            ]
        )

        self.assertEqual(parsed.get("five_hour_limit"), "26%")
        self.assertNotIn("experimental_metric", parsed)

    def test_extract_usage_metrics_from_semantic_blocks_prefers_specific_metric_label(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "gpt-5.3-codex-spark 5-hour usage limit",
                    "value_candidates": ["83%"],
                    "block_text": "gpt-5.3-codex-spark 5-hour usage limit 83%",
                },
                {
                    "metric_key": "weekly_limit",
                    "label_text": "gpt-5.3-codex-spark weekly usage limit",
                    "value_candidates": ["95%"],
                    "block_text": "gpt-5.3-codex-spark weekly usage limit 95%",
                },
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "value_candidates": ["80%"],
                    "block_text": "5-hour usage limit 80%",
                },
                {
                    "metric_key": "weekly_limit",
                    "label_text": "weekly usage limit",
                    "value_candidates": ["68%"],
                    "block_text": "weekly usage limit 68%",
                },
                {
                    "metric_key": "remaining_credit",
                    "label_text": "remaining credit",
                    "value_candidates": ["903"],
                    "block_text": "remaining credit 903",
                },
            ]
        )

        self.assertEqual(parsed.get("five_hour_limit"), "80%")
        self.assertEqual(parsed.get("weekly_limit"), "68%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "83%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "95%")
        self.assertEqual(parsed.get("remaining_credit"), "903")

    def test_extract_usage_metrics_from_semantic_blocks_requires_recognized_label_or_key(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "label_text": "Random number",
                    "value_candidates": ["123"],
                    "block_text": "Random number 123",
                }
            ]
        )

        self.assertEqual(parsed, {})

    def test_merge_snapshot_with_previous_preserves_missing_values(self) -> None:
        prev = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 12",
                "gpt_5_3_codex_spark_weekly_limit": "84 / 100",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        partial = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "",
                "gpt_5_3_codex_spark_five_hour_limit": "",
                "gpt_5_3_codex_spark_weekly_limit": "",
                "remaining_credit": "",
            },
            captured_at="2026-03-30T10:10:00",
        )
        merged = merge_snapshot_with_previous(partial, prev)

        self.assertEqual(merged.five_hour_limit, "52.5%")
        self.assertEqual(merged.weekly_limit, "60%")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit, "16.6667%")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit, "16%")
        self.assertEqual(merged.remaining_credit, "260")

    def test_merge_snapshot_with_previous_preserves_missing_values_after_semantic_partial_snapshot(self) -> None:
        prev = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "26%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "83%",
                "gpt_5_3_codex_spark_weekly_limit": "95%",
                "remaining_credit": "959",
            },
            captured_at="2026-03-30T10:00:00",
        )
        partial_metrics = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "value_candidates": ["25%"],
                    "block_text": "5-hour usage limit 25%",
                }
            ]
        )

        merged = merge_snapshot_with_previous(
            UsageSnapshot.from_metrics(partial_metrics, captured_at="2026-03-30T10:10:00"),
            prev,
        )

        self.assertEqual(merged.five_hour_limit, "25%")
        self.assertEqual(merged.weekly_limit, "28%")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit, "83%")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit, "95%")
        self.assertEqual(merged.remaining_credit, "959")

    def test_merge_snapshot_drops_stale_limits_absent_from_current_usage_page(self) -> None:
        # Given: an older page reported 5-hour and Spark limits, while the current
        # authoritative page reports only the weekly limit and credits.
        previous = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "0%",
                "weekly_limit": "98%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "0",
            },
            captured_at="2026-07-13T09:47:33+09:00",
            reset_info={
                "five_hour_limit_reset_at": "2026-07-14T02:39:00+09:00",
                "weekly_limit_reset_at": "2026-07-20T04:01:00+09:00",
            },
        )
        current = UsageSnapshot.from_metrics(
            {
                "weekly_limit": "97%",
                "remaining_credit": "0",
            },
            captured_at="2026-07-13T09:48:03+09:00",
            reset_info={
                "weekly_limit_reset_at": "2026-07-20T04:01:00+09:00",
            },
        )
        current.reported_metric_keys = ("weekly_limit", "remaining_credit")

        # When: the current snapshot is merged with the previous successful one.
        merged = merge_snapshot_with_previous(current, previous)

        # Then: metrics absent from the current authoritative page are not
        # relabeled with the current capture time as if they were fresh.
        self.assertEqual(merged.five_hour_limit, "")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit, "")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit, "")
        self.assertEqual(merged.five_hour_limit_reset_at, "")
        self.assertEqual(merged.weekly_limit, "97%")
        self.assertEqual(merged.remaining_credit, "0")
        self.assertEqual(merged.captured_at, "2026-07-13T09:48:03+09:00")

    def test_snapshot_from_dict_drops_implausible_day_scale_five_hour_reset(self) -> None:
        snapshot = UsageSnapshot.from_dict(
            {
                "five_hour_limit": "100%",
                "weekly_limit": "0%",
                "captured_at": "2026-06-03T20:48:37+09:00",
                "five_hour_limit_reset_at": "2026-06-07T15:39:00+09:00",
                "weekly_limit_reset_at": "2026-06-07T15:39:00+09:00",
            }
        )

        self.assertEqual(snapshot.five_hour_limit_reset_at, "")
        self.assertEqual(snapshot.weekly_limit_reset_at, "2026-06-07T15:39:00+09:00")

    def test_merge_snapshot_does_not_restore_previous_implausible_five_hour_reset(self) -> None:
        previous = UsageSnapshot.from_dict(
            {
                "five_hour_limit": "100%",
                "weekly_limit": "0%",
                "captured_at": "2026-06-03T20:48:37+09:00",
                "five_hour_limit_reset_at": "2026-06-07T15:39:00+09:00",
                "weekly_limit_reset_at": "2026-06-07T15:39:00+09:00",
            }
        )
        current = UsageSnapshot.from_metrics(
            {"five_hour_limit": "100%", "weekly_limit": "0%"},
            captured_at="2026-06-03T21:48:37+09:00",
        )

        merged = merge_snapshot_with_previous(current, previous)

        self.assertEqual(merged.five_hour_limit_reset_at, "")
        self.assertEqual(merged.weekly_limit_reset_at, "2026-06-07T15:39:00+09:00")

    def test_snapshot_from_dict_drops_cross_metric_cloned_five_hour_resets(self) -> None:
        snapshot = UsageSnapshot.from_dict(
            {
                "five_hour_limit": "89%",
                "weekly_limit": "35%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "captured_at": "2026-06-03T22:13:42+09:00",
                "five_hour_limit_reset_at": "2026-06-03T22:24:00+09:00",
                "weekly_limit_reset_at": "2026-06-08T00:38:00+09:00",
                "gpt_5_3_codex_spark_five_hour_limit_reset_at": "2026-06-03T22:24:00+09:00",
                "gpt_5_3_codex_spark_weekly_limit_reset_at": "2026-06-03T22:24:00+09:00",
            }
        )

        self.assertEqual(snapshot.five_hour_limit_reset_at, "2026-06-03T22:24:00+09:00")
        self.assertEqual(snapshot.weekly_limit_reset_at, "2026-06-08T00:38:00+09:00")
        self.assertEqual(snapshot.gpt_5_3_codex_spark_five_hour_limit_reset_at, "")
        self.assertEqual(snapshot.gpt_5_3_codex_spark_weekly_limit_reset_at, "")

    def test_merge_snapshot_does_not_restore_cross_metric_cloned_reset_values(self) -> None:
        previous = UsageSnapshot.from_dict(
            {
                "five_hour_limit": "89%",
                "weekly_limit": "35%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "captured_at": "2026-06-03T22:13:42+09:00",
                "five_hour_limit_reset_at": "2026-06-03T22:24:00+09:00",
                "weekly_limit_reset_at": "2026-06-08T00:38:00+09:00",
                "gpt_5_3_codex_spark_five_hour_limit_reset_at": "2026-06-03T22:24:00+09:00",
                "gpt_5_3_codex_spark_weekly_limit_reset_at": "2026-06-03T22:24:00+09:00",
            }
        )
        current = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "88%",
                "weekly_limit": "35%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
            },
            captured_at="2026-06-03T22:14:42+09:00",
        )

        merged = merge_snapshot_with_previous(current, previous)

        self.assertEqual(merged.five_hour_limit_reset_at, "2026-06-03T22:24:00+09:00")
        self.assertEqual(merged.weekly_limit_reset_at, "2026-06-08T00:38:00+09:00")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit_reset_at, "")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit_reset_at, "")

    def test_compute_usage_changes_detects_only_changed_fields(self) -> None:
        prev = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 12",
                "gpt_5_3_codex_spark_weekly_limit": "84 / 100",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        curr = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "9 / 12",
                "gpt_5_3_codex_spark_weekly_limit": "84 / 100",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:10:00",
        )

        changes = compute_usage_changes(prev, curr)
        labels = [c.label for c in changes]

        self.assertEqual(len(changes), 2)
        self.assertIn("5시간 사용 한도", labels)
        self.assertIn("gpt-5.3-codex-spark 5시간 사용 한도", labels)
        self.assertNotIn("주간 사용 한도", labels)
        self.assertNotIn("gpt-5.3-codex-spark 주간 사용 한도", labels)
        self.assertNotIn("남은 크레딧", labels)

    def test_handle_snapshot_persists_compact_usage_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            reset_info = {"five_hour_limit_reset_at": "2026-06-01T11:00:00+09:00"}

            monitor.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {"five_hour_limit": "80%", "weekly_limit": "70%"},
                    captured_at="2026-06-01T10:00:00+09:00",
                    reset_info=reset_info,
                )
            )
            monitor.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {"five_hour_limit": "78%", "weekly_limit": "69%"},
                    captured_at="2026-06-01T10:02:00+09:00",
                    reset_info=reset_info,
                )
            )

            state_path = os.path.join(tmp, "codex_usage_state.json")
            with open(state_path, encoding="utf-8") as fp:
                state = json.load(fp)
            history = state.get("usage_history")

            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["captured_at"], "2026-06-01T10:00:00+09:00")
            self.assertEqual(history[1]["five_hour_limit"], "78%")
            self.assertEqual(
                history[1]["five_hour_limit_reset_at"],
                "2026-06-01T11:00:00+09:00",
            )
            self.assertEqual(monitor.get_runtime_status()["usage_history"], history)
            self.assertEqual(state.get("snapshot_contract_version"), 2)

    def test_load_state_invalidates_ambiguous_legacy_percent_cache(self) -> None:
        # Given: v0.6.60 persisted bare percentages after erasing used/remaining meaning.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "codex_usage_state.json")
            with open(state_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "session_state": "logged_in",
                        "last_snapshot": {
                            "five_hour_limit": "5%",
                            "weekly_limit": "17 / 40",
                            "remaining_credit": "320",
                            "captured_at": "2026-07-13T09:48:03+09:00",
                        },
                        "usage_history": [
                            {
                                "captured_at": "2026-07-13T09:46:03+09:00",
                                "five_hour_limit": "5%",
                            },
                            {
                                "captured_at": "2026-07-13T09:48:03+09:00",
                                "weekly_limit": "17 / 40",
                            },
                        ],
                    },
                    fp,
                )

            # When: the unversioned cache crosses the v2 snapshot contract boundary.
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
            )
            snapshot = monitor.get_last_snapshot()
            history = monitor.get_runtime_status()["usage_history"]
            with open(state_path, encoding="utf-8") as fp:
                migrated = json.load(fp)

        # Then: ambiguous bare percentages disappear; unambiguous ratios migrate.
        self.assertEqual(snapshot.five_hour_limit, "")
        self.assertEqual(snapshot.weekly_limit, "57.5%")
        self.assertEqual(snapshot.remaining_credit, "320")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["weekly_limit"], "57.5%")
        self.assertEqual(migrated.get("snapshot_contract_version"), 2)

    def test_load_state_normalizes_old_or_oversized_usage_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "codex_usage_state.json")
            raw_history = [
                {"captured_at": "bad", "five_hour_limit": "99%"},
                {"captured_at": "2026-06-01T09:00:00+09:00", "five_hour_limit": "90%"},
                {"captured_at": "2026-06-01T10:00:00+09:00", "five_hour_limit": "80%"},
                {"captured_at": "2026-06-01T10:02:00+09:00", "five_hour_limit": "79%"},
                {"captured_at": "2026-06-01T10:04:00+09:00", "five_hour_limit": "78%"},
                {"captured_at": "2026-06-01T10:06:00+09:00", "five_hour_limit": "77%"},
                {"captured_at": "2026-06-01T10:08:00+09:00", "five_hour_limit": "76%"},
                {"captured_at": "2026-06-01T10:10:00+09:00", "five_hour_limit": "75%"},
            ]
            with open(state_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "session_state": "logged_in",
                        "snapshot_contract_version": 2,
                        "last_snapshot": {"five_hour_limit": "75%"},
                        "usage_history": raw_history,
                    },
                    fp,
                )

            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            history = monitor.get_runtime_status()["usage_history"]

            self.assertEqual(len(history), 5)
            self.assertEqual(history[0]["captured_at"], "2026-06-01T10:02:00+09:00")
            self.assertEqual(history[-1]["captured_at"], "2026-06-01T10:10:00+09:00")
            self.assertNotIn("bad", [item["captured_at"] for item in history])


    def test_runtime_projects_cache_auth_paused_retry_and_terminal_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            monitor.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {"weekly_limit": "48%"},
                    captured_at="2026-07-19T10:00:00+09:00",
                )
            )
            monitor._CodexUsageMonitor__failure_count = 1
            monitor._CodexUsageMonitor__handle_collect_error(
                "command_timeout",
                source="auto_monitor",
            )
            cached = monitor.get_runtime_status()
            self.assertEqual(cached["provider_status"], "stale")
            self.assertEqual(cached["freshness"], "stale")

            monitor._CodexUsageMonitor__handle_collect_error(
                "login_required",
                source="auto_monitor",
            )
            cached_auth = monitor.get_runtime_status()
            self.assertEqual(cached_auth["provider_status"], "login")
            self.assertEqual(cached_auth["freshness"], "stale")
            self.assertTrue(cached_auth["last_snapshot_is_stale"])

        for error, expected in (
            ("command_timeout", "retrying"),
            ("login_required", "login"),
            ("profile_in_use", "paused"),
            ("parse_failed", "error"),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                monitor = CodexUsageMonitor(
                    config_dir=tmp,
                    profile_dir=os.path.join(tmp, "profile"),
                    browser_session_factory=lambda _config: self._BrowserSession(),
                )
                monitor._CodexUsageMonitor__set_session_state("logged_in")
                monitor._CodexUsageMonitor__failure_count = 1
                monitor._CodexUsageMonitor__handle_collect_error(
                    error,
                    source="auto_monitor",
                )
                runtime = monitor.get_runtime_status()
                self.assertEqual(runtime["provider_status"], expected)

    def test_runtime_projects_persisted_auth_attention_as_login_with_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            original = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=profile_dir,
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            original.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {"weekly_limit": "48%"},
                    captured_at="2026-07-19T10:00:00+09:00",
                )
            )
            original._CodexUsageMonitor__set_session_state("logged_in")
            original._CodexUsageMonitor__set_auth_attention(
                "login_required",
                source="auto_monitor",
            )
            original._CodexUsageMonitor__save_state()

            restored = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=profile_dir,
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            runtime = restored.get_runtime_status()

            self.assertEqual(runtime["provider_status"], "login")
            self.assertEqual(runtime["freshness"], "stale")
            self.assertTrue(runtime["last_snapshot_is_stale"])
            self.assertEqual(runtime["monitor_state"], "paused_auth_required")

    def test_runtime_projects_logged_out_session_as_login_without_fresh_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )

            runtime = monitor.get_runtime_status()

            self.assertEqual(runtime["session_state"], "logged_out")
            self.assertEqual(runtime["provider_status"], "login")
            self.assertTrue(runtime["can_login"])

    def test_external_scheduler_auto_monitor_call_waits_for_collection_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            monitor.attach(object(), None, start_monitor=False)
            monitor._CodexUsageMonitor__set_session_state("logged_in")
            started = threading.Event()
            release = threading.Event()

            def collect_guarded(*, source, on_acquired=None):
                _ = source, on_acquired
                started.set()
                release.wait(2.0)
                return (
                    UsageSnapshot.from_metrics(
                        {"weekly_limit": "50%"},
                        captured_at="2026-07-19T10:00:00+09:00",
                    ),
                    None,
                )

            with patch.object(
                monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                side_effect=collect_guarded,
            ):
                caller = threading.Thread(
                    target=lambda: monitor.show_current_status(
                        force_refresh=True,
                        source="auto_monitor",
                    )
                )
                caller.start()
                self.assertTrue(started.wait(1.0))
                self.assertTrue(caller.is_alive())
                release.set()
                caller.join(2.0)

            self.assertFalse(caller.is_alive())
            self.assertEqual(monitor.get_last_snapshot().weekly_limit, "50%")

    def test_external_scheduler_manual_query_waits_for_collection_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            monitor.attach(object(), None, start_monitor=False)
            monitor._CodexUsageMonitor__set_session_state("logged_in")
            started = threading.Event()
            release = threading.Event()

            def collect_guarded(*, source, on_acquired=None):
                _ = source, on_acquired
                started.set()
                release.wait(2.0)
                return (
                    UsageSnapshot.from_metrics(
                        {"weekly_limit": "50%"},
                        captured_at="2026-07-19T10:00:00+09:00",
                    ),
                    None,
                )

            with patch.object(
                monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                side_effect=collect_guarded,
            ):
                caller = threading.Thread(
                    target=lambda: monitor.show_current_status(
                        force_refresh=True,
                        source="manual_query",
                    )
                )
                caller.start()
                self.assertTrue(started.wait(1.0))
                self.assertTrue(caller.is_alive())
                release.set()
                caller.join(2.0)

            self.assertFalse(caller.is_alive())
            self.assertEqual(monitor.get_last_snapshot().weekly_limit, "50%")

    def test_external_scheduler_success_clears_transient_failure_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _config: self._BrowserSession(),
            )
            monitor.attach(object(), None, start_monitor=False)
            monitor._CodexUsageMonitor__set_session_state("logged_in")
            monitor.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {"weekly_limit": "48%"},
                    captured_at="2026-07-19T10:00:00+09:00",
                )
            )

            with patch.object(
                monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                side_effect=[
                    (None, "command_timeout"),
                    (
                        UsageSnapshot.from_metrics(
                            {"weekly_limit": "42%"},
                            captured_at="2026-07-19T10:05:00+09:00",
                        ),
                        None,
                    ),
                ],
            ):
                monitor.show_current_status(force_refresh=True, source="auto_monitor")
                failed = monitor.get_runtime_status()
                monitor.show_current_status(force_refresh=True, source="auto_monitor")
                recovered = monitor.get_runtime_status()

            self.assertEqual(failed["provider_status"], "stale")
            self.assertEqual(failed["failure_count"], 1)
            self.assertEqual(failed["last_error_type"], "timeout")
            self.assertEqual(monitor.get_last_snapshot().weekly_limit, "42%")
            self.assertEqual(recovered["provider_status"], "ready")
            self.assertEqual(recovered["failure_count"], 0)
            self.assertEqual(recovered["last_error_type"], "")


if __name__ == "__main__":
    unittest.main()
