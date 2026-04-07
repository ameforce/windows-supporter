import unittest
from unittest.mock import patch

from src.apps.codex_usage_ui import CodexUsageSettingsView


class _FakeLabel:
    def __init__(self, owner, *args, **kwargs):
        _ = args
        self._owner = owner
        self.kwargs = dict(kwargs)
        self.grid_kwargs = {}
        owner.labels.append(self)

    def grid(self, **kwargs):
        self.grid_kwargs = dict(kwargs)
        return None


class _FakeTk:
    def __init__(self):
        self.labels = []

    def Label(self, *args, **kwargs):
        return _FakeLabel(self, *args, **kwargs)


class CodexUsageUiUnitTest(unittest.TestCase):
    def test_add_value_row_uses_wrapping_and_fill_for_runtime_values(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        fake_tk = _FakeTk()
        view._tk = fake_tk

        view._add_value_row(
            parent=object(),
            row=0,
            label="조회 상태",
            value_var=object(),
            bg="#FFFFFF",
        )

        self.assertEqual(len(fake_tk.labels), 2)
        value_label = fake_tk.labels[1]
        self.assertEqual(value_label.grid_kwargs.get("sticky"), "we")
        self.assertGreater(int(value_label.kwargs.get("wraplength", 0)), 0)

    def test_on_release_profile_calls_monitor_and_sets_ok_status(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.called = False

            def release_profile_session(self):
                self.called = True
                return True, "로그아웃 완료"

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        class _FakeWin:
            def after(self, _delay, fn):
                fn()
                return None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._tk = object()
        view._win = _FakeWin()

        status_calls: list[tuple[str, str]] = []

        def fake_set_status(text: str, level: str = "info") -> None:
            status_calls.append((str(text), str(level)))

        view._set_status = fake_set_status
        view._load_settings = lambda: None
        view._refresh_runtime_status = lambda: None

        with patch("src.apps.codex_usage_ui.threading.Thread", _InlineThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_release_profile()

        self.assertTrue(monitor.called)
        self.assertTrue(status_calls)
        self.assertEqual(status_calls[-1][1], "ok")

    def test_on_login_triggers_show_current_status(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.args = []

            def show_current_status(self, force_refresh: bool = True):
                self.args.append(bool(force_refresh))
                return None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)

        statuses: list[tuple[str, str]] = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))
        view._safe_get_runtime = lambda: {"can_login": True, "logout_in_progress": False}

        view._on_login()

        self.assertEqual(monitor.args, [True])
        self.assertTrue(statuses)
        self.assertEqual(statuses[-1][1], "info")

    def test_refresh_action_buttons_applies_runtime_permissions(self) -> None:
        class _FakeButton:
            def __init__(self):
                self.disabled = False

            def state(self, tokens):
                if list(tokens) == ["disabled"]:
                    self.disabled = True
                    return None
                if list(tokens) == ["!disabled"]:
                    self.disabled = False
                    return None
                return None

        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        login_btn = _FakeButton()
        logout_btn = _FakeButton()
        view._login_button = login_btn
        view._logout_button = logout_btn

        view._refresh_action_buttons({"can_login": True, "can_logout": False})
        self.assertFalse(login_btn.disabled)
        self.assertTrue(logout_btn.disabled)

        view._refresh_action_buttons({"can_login": False, "can_logout": True})
        self.assertTrue(login_btn.disabled)
        self.assertFalse(logout_btn.disabled)

    def test_refresh_runtime_status_shows_profile_in_use_pause_state(self) -> None:
        class _Var:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value
                return None

        class _FakeWin:
            def after(self, _delay, _fn):
                return "after-token"

        class _FakeMonitor:
            def get_last_snapshot(self):
                return None

        view = CodexUsageSettingsView(root=None, codex_monitor=_FakeMonitor())
        view._win = _FakeWin()
        view._collect_state_var = _Var()
        view._next_collect_var = _Var()
        view._live_time_var = _Var()
        view._live_five_hour_var = _Var()
        view._live_weekly_var = _Var()
        view._live_code_review_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_in",
            "monitor_state": "paused_profile_in_use",
            "profile_in_use": True,
            "collect_inflight": False,
            "next_collect_in_sec": 8,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "프로필 사용 중 (자동 일시중지)")
        self.assertEqual(view._next_collect_var.value, "-")

    def test_refresh_runtime_status_hides_countdown_while_collecting(self) -> None:
        class _Var:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value
                return None

        class _FakeWin:
            def after(self, _delay, _fn):
                return "after-token"

        class _FakeMonitor:
            def get_last_snapshot(self):
                return None

        view = CodexUsageSettingsView(root=None, codex_monitor=_FakeMonitor())
        view._win = _FakeWin()
        view._collect_state_var = _Var()
        view._next_collect_var = _Var()
        view._live_time_var = _Var()
        view._live_five_hour_var = _Var()
        view._live_weekly_var = _Var()
        view._live_code_review_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_in",
            "monitor_state": "running",
            "profile_in_use": False,
            "collect_inflight": True,
            "collect_source": "manual_query",
            "next_collect_in_sec": 44,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "조회 중 (manual_query)")
        self.assertEqual(view._next_collect_var.value, "-")


if __name__ == "__main__":
    unittest.main()
