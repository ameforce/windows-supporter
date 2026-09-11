from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.apps.Wrike import Wrike
from src.apps.wrike_ui import WrikeSettingsView


REPO_ROOT = Path(__file__).resolve().parents[2]


class FlexBrowserArchitectureTests(unittest.TestCase):
    def test_settings_ui_exposes_employee_number_and_browser_actions_only(self) -> None:
        source = (REPO_ROOT / "src" / "apps" / "wrike_ui.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("Flex 사번 (자동 감지)", source)
        self.assertIn("Flex 로그인 · 지금 동기화", source)
        self.assertIn("브라우저 세션", source)
        self.assertIn("confirm_flex_employee_number", source)
        self.assertNotIn("_flex_refresh_token_var", source)
        self.assertNotIn("_flex_client_id_var", source)
        self.assertNotIn("_flex_client_secret_var", source)
        self.assertNotIn("인증정보 지우기", source)

    def test_runtime_has_no_administrator_flex_client(self) -> None:
        flex_source = (REPO_ROOT / "src" / "apps" / "flex_worktime.py").read_text(
            encoding="utf-8"
        )
        wrike_source = (REPO_ROOT / "src" / "apps" / "Wrike.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("class FlexBrowserClient", flex_source)
        self.assertIn("class FlexBrowserSyncResult", flex_source)
        self.assertIn("extract_flex_employee_number", flex_source)
        self.assertNotIn("class FlexOpenApiClient", flex_source)
        self.assertNotIn("FlexOpenApiClient", wrike_source)
        self.assertNotIn("FlexCredentials", flex_source)
        self.assertIn("FLEX_BROWSER_PROFILE_DIR_NAME", wrike_source)

    def test_settings_migration_drops_legacy_flex_secrets_on_save(self) -> None:
        source = (REPO_ROOT / "src" / "apps" / "Wrike.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("legacy_flex_keys", source)
        save_start = source.index("def __save_settings")
        save_source = source[save_start:]
        self.assertNotIn('"flex_refresh_token"', save_source)
        self.assertNotIn('"flex_client_id"', save_source)
        self.assertNotIn('"flex_client_secret"', save_source)

    def test_startup_does_not_open_flex_without_saved_employee_number(self) -> None:
        app = Wrike.__new__(Wrike)
        app._Wrike__root = object()
        app._Wrike__background_active = True
        app._Wrike__flex_enabled = True
        app._Wrike__flex_employee_number = ""
        app._Wrike__flex_after_id = None
        app._Wrike__flex_state = "unknown"
        app._Wrike__flex_last_error = ""
        request_sync = Mock()
        app._Wrike__request_flex_sync = request_sync

        app._Wrike__start_flex_polling()

        request_sync.assert_not_called()
        self.assertEqual(app._Wrike__flex_state, "unconfigured")
        self.assertIn("지금 동기화", app._Wrike__flex_last_error)

    def test_sync_browser_job_closes_context_and_uses_headless_session(self) -> None:
        created = []

        class _Client:
            def __init__(self, *_args, **kwargs):
                self.headless = bool(kwargs.get("headless"))
                self.closed = False
                created.append(self)

            def fetch_schedule_period(self, *_args, **_kwargs):
                return {}

            def close(self):
                self.closed = True

        app = Wrike.__new__(Wrike)
        app._Wrike__flex_browser_queue = queue.Queue()
        app._Wrike__flex_browser_profile_dir = "C:/temp/flex-profile-test"
        app._Wrike__time_log_login_timeout_sec = 10.0
        app._Wrike__flex_browser_stop_event = threading.Event()
        app._Wrike__ensure_playwright_ready = lambda: True
        app._Wrike__log_exception = lambda *_args: None

        sync_response = queue.Queue(maxsize=1)
        close_response = queue.Queue(maxsize=1)
        app._Wrike__flex_browser_queue.put(
            (
                "sync",
                (date(2026, 9, 10), date(2026, 9, 10), "E-42", datetime(2026, 9, 10, 12)),
                sync_response,
            )
        )
        app._Wrike__flex_browser_queue.put(("close", None, close_response))

        with patch("src.apps.Wrike.FlexBrowserClient", _Client):
            worker = threading.Thread(target=app._Wrike__flex_browser_worker_loop)
            worker.start()
            worker.join(timeout=3.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(sync_response.get_nowait(), (True, {}))
        self.assertEqual(close_response.get_nowait(), (True, None))
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].headless)
        self.assertTrue(created[0].closed)

    def test_open_browser_job_is_the_explicit_headed_path(self) -> None:
        created = []

        class _Client:
            def __init__(self, *_args, **kwargs):
                self.headless = bool(kwargs.get("headless"))
                self.opened = False
                self.closed = False
                created.append(self)

            def open_work_record_page(self):
                self.opened = True

            def close(self):
                self.closed = True

        app = Wrike.__new__(Wrike)
        app._Wrike__flex_browser_queue = queue.Queue()
        app._Wrike__flex_browser_profile_dir = "C:/temp/flex-profile-test"
        app._Wrike__time_log_login_timeout_sec = 10.0
        app._Wrike__flex_browser_stop_event = threading.Event()
        app._Wrike__ensure_playwright_ready = lambda: True
        app._Wrike__log_exception = lambda *_args: None

        open_response = queue.Queue(maxsize=1)
        close_response = queue.Queue(maxsize=1)
        app._Wrike__flex_browser_queue.put(("open", None, open_response))
        app._Wrike__flex_browser_queue.put(("close", None, close_response))

        with patch("src.apps.Wrike.FlexBrowserClient", _Client):
            worker = threading.Thread(target=app._Wrike__flex_browser_worker_loop)
            worker.start()
            worker.join(timeout=3.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(open_response.get_nowait(), (True, None))
        self.assertEqual(close_response.get_nowait(), (True, None))
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].headless)
        self.assertTrue(created[0].opened)
        self.assertTrue(created[0].closed)

    def test_sync_failure_closes_headless_session(self) -> None:
        created = []

        class _Client:
            def __init__(self, *_args, **kwargs):
                self.headless = bool(kwargs.get("headless"))
                self.closed = False
                created.append(self)

            def fetch_schedule_period(self, *_args, **_kwargs):
                raise RuntimeError("navigation failed")

            def close(self):
                self.closed = True

        app = Wrike.__new__(Wrike)
        app._Wrike__flex_browser_queue = queue.Queue()
        app._Wrike__flex_browser_profile_dir = "C:/temp/flex-profile-test"
        app._Wrike__time_log_login_timeout_sec = 10.0
        app._Wrike__flex_browser_stop_event = threading.Event()
        app._Wrike__ensure_playwright_ready = lambda: True
        app._Wrike__log_exception = lambda *_args: None

        sync_response = queue.Queue(maxsize=1)
        close_response = queue.Queue(maxsize=1)
        app._Wrike__flex_browser_queue.put(
            (
                "sync",
                (date(2026, 9, 10), date(2026, 9, 10), "", datetime(2026, 9, 10, 12)),
                sync_response,
            )
        )

        with patch("src.apps.Wrike.FlexBrowserClient", _Client):
            worker = threading.Thread(target=app._Wrike__flex_browser_worker_loop)
            worker.start()
            self.assertEqual(
                sync_response.get(timeout=3.0),
                (False, ("Flex 브라우저 동기화에 실패했습니다.", "unexpected_error")),
            )
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].headless)
            self.assertTrue(created[0].closed)
            app._Wrike__flex_browser_queue.put(("close", None, close_response))
            worker.join(timeout=3.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(close_response.get_nowait(), (True, None))
        self.assertTrue(created[0].closed)

    def test_manual_sync_requests_background_mode(self) -> None:
        app = Wrike.__new__(Wrike)
        app._Wrike__flex_enabled = True
        app._Wrike__background_active = True
        app._Wrike__root = object()
        request_sync = Mock(return_value=True)
        app._Wrike__request_flex_sync = request_sync

        result = app.sync_flex_now()

        self.assertEqual(result, (True, None))
        request_sync.assert_called_once_with(force=True, announce=True)


class FlexEmployeeNumberUiTests(unittest.TestCase):
    class _Var:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class _MessageBox:
        def askyesno(self, *_args, **_kwargs):
            return True

    class _Label:
        def __init__(self):
            self.configure_calls = []

        def configure(self, **kwargs):
            self.configure_calls.append(dict(kwargs))

    class _Backend:
        def __init__(self):
            self.confirmed = ""

        def get_settings_snapshot(self):
            return {
                "flex_employee_number": "",
                "flex_detected_employee_number": "E-42",
                "flex_status": {
                    "state": "fresh",
                    "detected_employee_number": "E-42",
                },
            }

        def confirm_flex_employee_number(self, employee_number):
            self.confirmed = employee_number
            return True, None

    def test_detected_employee_number_is_shown_and_confirmed_before_field_update(self):
        backend = self._Backend()
        view = WrikeSettingsView(None, backend)
        view._flex_employee_number_var = self._Var()
        view._flex_status_var = self._Var()
        view._messagebox = self._MessageBox()

        view._refresh_flex_status_from_backend()

        self.assertEqual(backend.confirmed, "E-42")
        self.assertEqual(view._flex_employee_number_var.get(), "E-42")
        self.assertIn("감지된 사번 E-42", view._flex_status_var.get())

    def test_completed_sync_is_reported_in_main_status(self):
        class _Backend:
            def get_settings_snapshot(self):
                return {
                    "flex_employee_number": "E-42",
                    "flex_status": {
                        "state": "fresh",
                        "schedule_days": 5,
                    },
                }

        status = self._Var()
        view = WrikeSettingsView(None, _Backend())
        view._status_var = status
        view._status_label = self._Label()
        view._flex_status_var = self._Var()
        view._flex_sync_feedback_active = True
        view._flex_status_poll_started_at = time.monotonic()

        view._poll_flex_status()

        self.assertEqual(status.get(), "Flex 동기화 완료 · 근무 일정 5일 반영")
        self.assertFalse(view._flex_sync_feedback_active)
        self.assertEqual(view._status_label.configure_calls[-1]["fg"], "#10B981")


if __name__ == "__main__":
    unittest.main()
