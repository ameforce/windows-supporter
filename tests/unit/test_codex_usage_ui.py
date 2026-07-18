import unittest
from unittest.mock import patch

from src.apps.codex_usage_ui import CodexUsageSettingsView


class _FakeLabel:
    def __init__(self, owner, *args, **kwargs):
        _ = args
        self._owner = owner
        self.kwargs = dict(kwargs)
        self.pack_kwargs = {}
        self.grid_kwargs = {}
        self.bind_calls = []
        owner.labels.append(self)

    def pack(self, **kwargs):
        self.pack_kwargs = dict(kwargs)
        return None

    def grid(self, **kwargs):
        self.grid_kwargs = dict(kwargs)
        return None

    def bind(self, event, callback):
        self.bind_calls.append((event, callback))
        return None


class _FakeWidget:
    def __init__(self, owner=None, *args, **kwargs):
        _ = args
        self._owner = owner
        self.kwargs = dict(kwargs)
        self.pack_kwargs = {}
        self.grid_kwargs = {}
        self.bind_calls = []
        self.configure_calls = []
        self.children = []
        self.after_calls = []
        self.after_cancel_calls = []

    def pack(self, **kwargs):
        self.pack_kwargs = dict(kwargs)
        return None

    def grid(self, **kwargs):
        self.grid_kwargs = dict(kwargs)
        return None

    def bind(self, event, callback):
        self.bind_calls.append((event, callback))
        return None

    def configure(self, **kwargs):
        self.configure_calls.append(dict(kwargs))
        return None

    def columnconfigure(self, *_args, **_kwargs):
        return None

    def rowconfigure(self, *_args, **_kwargs):
        return None

    def winfo_children(self):
        return list(self.children)

    def destroy(self):
        return None

    def after(self, delay_ms, callback):
        self.after_calls.append((int(delay_ms), callback))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, after_id):
        self.after_cancel_calls.append(after_id)
        return None


class _FakeCanvas(_FakeWidget):
    def __init__(self, owner=None, *args, **kwargs):
        super().__init__(owner, *args, **kwargs)
        self.windows = []
        self.itemconfigure_calls = []
        if owner is not None:
            owner.canvases.append(self)

    def yview(self, *_args, **_kwargs):
        return None

    def create_window(self, *args, **kwargs):
        self.windows.append((args, kwargs))
        return len(self.windows)

    def itemconfigure(self, item, **kwargs):
        self.itemconfigure_calls.append((item, dict(kwargs)))
        return None

    def bbox(self, *_args):
        return (0, 0, 100, 100)


class _FakeVar:
    def __init__(self, value=None):
        self.value = value
        self.callbacks = []

    def set(self, value):
        self.value = value
        for callback in list(self.callbacks):
            callback("", "", "write")
        return None

    def get(self):
        return self.value

    def trace_add(self, _mode, callback):
        self.callbacks.append(callback)
        return f"trace-{len(self.callbacks)}"


class _FakeTk:
    def __init__(self):
        self.labels = []
        self.canvases = []
        self.checkbuttons = []

    def Label(self, *args, **kwargs):
        return _FakeLabel(self, *args, **kwargs)

    def Frame(self, *args, **kwargs):
        return _FakeWidget(self, *args, **kwargs)

    def Canvas(self, *args, **kwargs):
        return _FakeCanvas(self, *args, **kwargs)

    def StringVar(self, value=""):
        return _FakeVar(value=value)

    def BooleanVar(self, value=False):
        return _FakeVar(value=value)

    def Checkbutton(self, *args, **kwargs):
        widget = _FakeWidget(self, *args, **kwargs)
        self.checkbuttons.append(widget)
        return widget


class _FakeButton(_FakeWidget):
    def __init__(self, owner=None, *args, **kwargs):
        super().__init__(owner, *args, **kwargs)
        if owner is not None:
            owner.buttons.append(self)

    def state(self, _tokens):
        return None


class _FakeScrollbar(_FakeWidget):
    def __init__(self, owner=None, *args, **kwargs):
        super().__init__(owner, *args, **kwargs)
        if owner is not None:
            owner.scrollbars.append(self)

    def set(self, *_args):
        return None


class _FakeTtk:
    def __init__(self):
        self.buttons = []
        self.scrollbars = []

    def Entry(self, *args, **kwargs):
        return _FakeWidget(self, *args, **kwargs)

    def Button(self, *args, **kwargs):
        return _FakeButton(self, *args, **kwargs)

    def Scrollbar(self, *args, **kwargs):
        return _FakeScrollbar(self, *args, **kwargs)


class CodexUsageUiUnitTest(unittest.TestCase):
    def test_runtime_refresh_keeps_only_one_scheduled_callback(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        win = _FakeWidget()
        view._win = win

        view._schedule_runtime_refresh(1000)
        first_after_id = view._runtime_after_id
        view._schedule_runtime_refresh(1000)

        self.assertEqual(len(win.after_calls), 2)
        self.assertEqual(win.after_cancel_calls, [first_after_id])

    def test_rate_limit_status_uses_user_facing_retry_countdown(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        text = view._runtime_state_text(
            {
                "provider_state": "rate_limited",
                "next_collect_in_sec": 120,
            }
        )

        self.assertEqual(text, "요청 제한 · 120초 후 재시도")

    def test_add_value_row_uses_wrapping_without_forcing_wide_columns(self) -> None:
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
        self.assertEqual(value_label.grid_kwargs.get("sticky"), "w")
        self.assertGreater(int(value_label.kwargs.get("wraplength", 0)), 0)

    def test_account_metric_rows_use_compact_two_cell_layout(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        fake_tk = _FakeTk()
        view._tk = fake_tk

        metric_vars, _display_vars = view._build_account_metric_rows(
            parent=object(),
            bg="#FFFFFF",
        )

        self.assertIn("captured_at", metric_vars)
        metric_columns = {label.grid_kwargs.get("column") for label in fake_tk.labels}
        self.assertEqual(metric_columns, {0, 1})
        self.assertTrue(all(label.grid_kwargs.get("sticky") == "we" for label in fake_tk.labels))
        self.assertTrue(
            all(int(label.kwargs.get("wraplength", 0)) <= 190 for label in fake_tk.labels)
        )

        captured_display = fake_tk.labels[0].kwargs.get("textvariable")
        metric_vars["captured_at"].set("2026-06-25 09:07:55")
        self.assertEqual(captured_display.get(), "최근 확인 시각: 2026-06-25 09:07:55")

    def test_cursor_metric_rows_use_included_reset_and_on_demand_contract(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        fake_tk = _FakeTk()
        view._tk = fake_tk

        metric_vars, display_vars = view._build_account_metric_rows(
            parent=object(),
            bg="#FFFFFF",
            provider="cursor",
        )

        self.assertEqual(
            set(metric_vars),
            {"captured_at", "included_usage", "billing_reset_at", "on_demand_status"},
        )
        self.assertEqual(display_vars["included_usage"].get(), "포함 사용량: -")
        self.assertEqual(display_vars["billing_reset_at"].get(), "결제 주기 초기화: -")
        self.assertEqual(display_vars["on_demand_status"].get(), "온디맨드: -")

    def test_usage_metric_values_are_localized_without_changing_amounts(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        self.assertEqual(view._localize_usage_metric_value("68% left"), "68% 남음")
        self.assertEqual(view._localize_usage_metric_value("42% used"), "42% 사용")
        self.assertEqual(
            view._localize_usage_metric_value("Enabled · US$8.20 used"),
            "활성화 · US$8.20 사용",
        )

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
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=monitor,
            ui_post=lambda fn: fn(),
        )
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

    def test_mount_keeps_codex_content_unscrolled(self) -> None:
        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        parent = _FakeWidget()
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._safe_get_settings = lambda: {
            "settings_path": "",
            "state_path": "",
            "profile_dir": "",
        }
        view._load_settings = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(parent)

        self.assertEqual(len(fake_tk.canvases), 0)
        self.assertEqual(len(fake_ttk.scrollbars), 0)

    def test_mount_keeps_two_account_settings_visible_without_scroll_canvas(self) -> None:
        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        parent = _FakeWidget()
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._safe_get_settings = lambda: {
            "settings_path": "",
            "state_path": "",
            "profile_dir": "",
            "accounts": [
                {"id": "account_1", "label": "Codex 1", "enabled": True},
                {"id": "account_2", "label": "Codex 2", "enabled": True},
            ],
        }
        view._load_settings = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(parent)

        self.assertEqual(len(fake_tk.canvases), 0)
        self.assertEqual(len(fake_ttk.scrollbars), 0)
        texts = [label.kwargs.get("text") for label in fake_tk.labels]
        self.assertIn("작업표시줄 표시", texts)
        self.assertIn("사용량 프로필 (전체 최대 2개)", texts)
        self.assertNotIn("실시간 상태", texts)
        self.assertNotIn("다음 모니터링까지", texts)

    def test_mount_lays_runtime_values_out_in_two_columns(self) -> None:
        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        parent = _FakeWidget()
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._safe_get_settings = lambda: {
            "settings_path": "",
            "state_path": "",
            "profile_dir": "",
        }
        view._load_settings = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(parent)

        runtime_labels = {
            label.kwargs.get("text"): label.grid_kwargs
            for label in fake_tk.labels
            if label.kwargs.get("text")
            in {
                "조회 상태",
                "다음 모니터링까지",
                "남은 크레딧",
                "5시간 사용 한도",
                "5시간 한도 초기화",
            }
        }
        self.assertEqual(runtime_labels["조회 상태"].get("column"), 0)
        self.assertEqual(runtime_labels["다음 모니터링까지"].get("column"), 2)
        self.assertEqual(runtime_labels["남은 크레딧"].get("column"), 2)
        self.assertEqual(runtime_labels["5시간 사용 한도"].get("column"), 0)
        self.assertEqual(runtime_labels["5시간 한도 초기화"].get("column"), 2)

    def test_mount_renders_two_account_sections(self) -> None:
        class _FakeMonitor:
            def get_settings_snapshot(self):
                return {
                    "enabled": True,
                    "taskbar_overlay_enabled": True,
                    "interval_sec": 90,
                    "tooltip_duration_ms": 7000,
                    "usage_url": "https://example.test",
                    "settings_path": "",
                    "state_path": "",
                    "profile_dir": "",
                    "accounts": [
                        {
                            "id": "account_1",
                            "label": "Codex 1",
                            "enabled": True,
                            "settings_path": "s1.json",
                            "state_path": "st1.json",
                            "profile_dir": "profile-1",
                        },
                        {
                            "id": "account_2",
                            "label": "Codex 2",
                            "enabled": False,
                            "settings_path": "s2.json",
                            "state_path": "st2.json",
                            "profile_dir": "profile-2",
                        },
                    ],
                }

            def get_runtime_status(self):
                return {"accounts": []}

            def get_last_snapshot(self):
                return None

        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        parent = _FakeWidget()
        view = CodexUsageSettingsView(root=None, codex_monitor=_FakeMonitor())
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(parent)

        texts = [label.kwargs.get("text") for label in fake_tk.labels]
        self.assertIn("Codex 1", texts)
        self.assertIn("Codex 2", texts)
        self.assertIn("프로필 경로: profile-1", texts)
        self.assertIn("프로필 경로: profile-2", texts)

    def test_save_includes_taskbar_overlay_toggle(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.update_payloads = []

            def get_settings_snapshot(self):
                return {
                    "accounts": [],
                    "taskbar_overlay_enabled": True,
                }

            def update_settings(self, payload):
                self.update_payloads.append(dict(payload))
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=False)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        statuses = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))
        view._hide_main_ui = lambda: self.fail("autosave/manual save must not hide main UI")

        view._on_save()

        self.assertEqual(monitor.update_payloads[-1]["taskbar_overlay_enabled"], False)
        self.assertEqual(statuses[-1], ("저장됨", "ok"))

    def test_invalid_autosave_value_does_not_update_settings(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.update_payloads = []

            def get_settings_snapshot(self):
                return {"accounts": []}

            def update_settings(self, payload):
                self.update_payloads.append(dict(payload))
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="invalid")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        statuses = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))

        view._autosave_now()

        self.assertEqual(monitor.update_payloads, [])
        self.assertTrue(statuses)
        self.assertEqual(statuses[-1][1], "error")

    def test_mount_hides_legacy_global_login_logout_buttons_for_multi_account_settings(self) -> None:
        class _FakeMonitor:
            def get_settings_snapshot(self):
                return {
                    "enabled": True,
                    "interval_sec": 90,
                    "tooltip_duration_ms": 7000,
                    "usage_url": "https://example.test",
                    "accounts": [
                        {"id": "account_1", "label": "Codex 1", "enabled": True},
                        {"id": "account_2", "label": "Codex 2", "enabled": True},
                    ],
                }

            def get_runtime_status(self):
                return {
                    "can_login": False,
                    "can_logout": True,
                    "accounts": [
                        {
                            "id": "account_1",
                            "runtime": {"can_logout": False, "session_state": "logged_out"},
                        },
                        {
                            "id": "account_2",
                            "runtime": {"can_logout": True, "session_state": "logged_in"},
                        },
                    ],
                }

            def get_last_snapshot(self):
                return None

        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        parent = _FakeWidget()
        view = CodexUsageSettingsView(root=None, codex_monitor=_FakeMonitor())
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(parent)

        self.assertIsNone(view._login_button)
        self.assertIsNone(view._logout_button)
        button_texts = [button.kwargs.get("text") for button in fake_ttk.buttons]
        self.assertNotIn("저장", button_texts)
        self.assertNotIn("로드하기", button_texts)
        self.assertNotIn("툴팁(초)", [label.kwargs.get("text") for label in fake_tk.labels])
        self.assertEqual(button_texts.count("연결"), 2)
        self.assertEqual(button_texts.count("연결 해제"), 2)
        self.assertEqual(button_texts.count("새로고침"), 2)
        self.assertIn("아래로", button_texts)
        self.assertIn("위로", button_texts)

    def test_refresh_runtime_status_updates_each_account_status_independently(self) -> None:
        class _FakeMonitor:
            def get_settings_snapshot(self):
                return {
                    "enabled": True,
                    "interval_sec": 90,
                    "tooltip_duration_ms": 7000,
                    "usage_url": "https://example.test",
                    "accounts": [
                        {
                            "id": "account_1",
                            "label": "Codex 1",
                            "enabled": True,
                            "profile_dir": "profile-1",
                        },
                        {
                            "id": "account_2",
                            "label": "이니미니",
                            "enabled": True,
                            "profile_dir": "profile-2",
                        },
                    ],
                }

            def get_runtime_status(self):
                return {
                    "monitor_state": "paused_auth_required",
                    "session_state": "mixed",
                    "accounts": [
                        {
                            "id": "account_1",
                            "label": "Codex 1",
                            "enabled": True,
                            "runtime": {
                                "monitor_state": "idle",
                                "session_state": "logged_in",
                                "collect_inflight": False,
                            },
                            "last_snapshot": {
                                "captured_at": "2999-01-01T00:00:00+09:00",
                                "five_hour_limit": "92%",
                                "weekly_limit": "86%",
                            },
                        },
                        {
                            "id": "account_2",
                            "label": "이니미니",
                            "enabled": True,
                            "runtime": {
                                "monitor_state": "paused_auth_required",
                                "session_state": "logged_out",
                                "collect_inflight": False,
                                "auth_attention_required": True,
                                "auth_attention_reason": "login_required",
                            },
                            "last_snapshot": {
                                "captured_at": "2026-06-24T08:41:00+09:00",
                                "five_hour_limit": "100%",
                                "weekly_limit": "58%",
                            },
                        },
                    ],
                }

            def get_last_snapshot(self):
                return None

        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        parent = _FakeWidget()
        view = CodexUsageSettingsView(root=None, codex_monitor=_FakeMonitor())
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(parent)
        view._refresh_runtime_status()

        self.assertEqual(view._account_status_vars["account_1"].get(), "조회 상태: 대기 중")
        self.assertEqual(
            view._account_status_vars["account_2"].get(),
            "조회 상태: 브라우저 인증 필요",
        )
        self.assertEqual(view._account_snapshot_vars["account_1"].get(), "값 상태: 최근 값")
        self.assertEqual(view._account_snapshot_vars["account_2"].get(), "값 상태: 이전 값")
        self.assertEqual(
            view._account_metric_vars["account_1"]["five_hour_limit"].get(),
            "92%",
        )
        self.assertEqual(
            view._account_metric_vars["account_1"]["weekly_limit"].get(),
            "86%",
        )
        self.assertEqual(
            view._account_metric_vars["account_2"]["captured_at"].get(),
            "2026-06-24 08:41:00",
        )
        self.assertEqual(
            view._account_metric_vars["account_2"]["five_hour_limit"].get(),
            "100%",
        )

    def test_account_snapshot_summary_marks_old_logged_in_snapshot_as_previous(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        summary = view._format_account_snapshot_summary(
            {
                "runtime": {
                    "monitor_state": "idle",
                    "session_state": "logged_in",
                    "collect_inflight": False,
                },
                "settings": {"interval_sec": 30},
                "last_snapshot": {
                    "captured_at": "2000-01-01T00:00:00",
                    "five_hour_limit": "100%",
                    "weekly_limit": "58%",
                },
            }
        )

        self.assertIn("이전 값:", summary)
        self.assertIn("100%", summary)

    def test_command_timeout_shows_recovery_progress_and_marks_snapshot_previous(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        runtime = {
            "monitor_state": "idle",
            "session_state": "logged_in",
            "collect_inflight": False,
            "browser_state": "recovering",
            "browser_last_error": "command_timeout",
            "browser_retry_attempt": 1,
            "browser_retry_max": 3,
        }

        self.assertEqual(
            view._runtime_state_text(runtime),
            "조회 시간 초과 · 연결 복구 중 (1/3)",
        )
        self.assertTrue(
            view._runtime_snapshot_is_previous(
                runtime,
                captured_at="2999-01-01T00:00:00+09:00",
                stale_after_sec=300.0,
            )
        )

    def test_profile_order_swap_autosaves_without_moving_profile_dirs(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.update_payloads = []
                self.settings = {
                    "enabled": True,
                    "taskbar_overlay_enabled": True,
                    "interval_sec": 90,
                    "usage_url": "https://example.test",
                    "default_account_id": "account_1",
                    "accounts": [
                        {
                            "id": "account_1",
                            "label": "Codex 1",
                            "enabled": True,
                            "profile_dir": "profile-1",
                        },
                        {
                            "id": "account_2",
                            "label": "Codex 2",
                            "enabled": True,
                            "profile_dir": "profile-2",
                        },
                    ],
                }

            def get_settings_snapshot(self):
                return dict(self.settings)

            def update_settings(self, payload):
                self.update_payloads.append(dict(payload))
                self.settings.update(payload)
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        view._account_enabled_vars = {
            "account_1": _FakeVar(value=True),
            "account_2": _FakeVar(value=True),
        }
        view._account_order = ["account_1", "account_2"]
        view._set_status = lambda *_args, **_kwargs: None

        view._on_move_account("account_2", -1)

        payload = monitor.update_payloads[-1]
        self.assertEqual(payload["default_account_id"], "account_2")
        self.assertEqual([item["id"] for item in payload["accounts"]], ["account_2", "account_1"])
        self.assertEqual([item["profile_dir"] for item in payload["accounts"]], ["profile-2", "profile-1"])

    def test_save_emits_provider_neutral_profiles_and_taskbar_selection(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.payload = None

            def get_settings_snapshot(self):
                return {
                    "profiles": [
                        {"id": "account_1", "provider": "codex", "enabled": True},
                        {"id": "account_2", "provider": "codex", "enabled": True},
                    ]
                }

            def update_settings(self, payload):
                self.payload = dict(payload)
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        view._account_order = ["account_1", "account_2"]
        view._account_enabled_vars = {
            "account_1": _FakeVar(value=True),
            "account_2": _FakeVar(value=True),
        }
        view._account_provider_vars = {
            "account_1": _FakeVar(value="codex"),
            "account_2": _FakeVar(value="cursor"),
        }
        view._account_taskbar_selected_vars = {
            "account_1": _FakeVar(value=False),
            "account_2": _FakeVar(value=True),
        }
        view._set_status = lambda *_args, **_kwargs: None

        self.assertTrue(view._save_settings())

        assert monitor.payload is not None
        self.assertEqual(
            [item["provider"] for item in monitor.payload["profiles"]],
            ["codex", "cursor"],
        )
        self.assertEqual(monitor.payload["selected_profile_ids"], ["account_2"])
        self.assertEqual(monitor.payload["profile_order"], ["account_1", "account_2"])

    def test_account_login_and_release_call_account_specific_manager_methods(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.login_calls = []
                self.release_calls = []

            def login_account(self, account_id):
                self.login_calls.append(account_id)

            def release_account_profile_session(self, account_id):
                self.release_calls.append(account_id)
                return True, "released"

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor, ui_post=lambda fn: fn())
        view._tk = object()
        view._win = object()
        view._set_status = lambda *_args, **_kwargs: None
        view._load_settings = lambda: None
        view._refresh_runtime_status = lambda: None

        with patch("src.apps.codex_usage_ui.threading.Thread", _InlineThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_account_login("account_2")
                view._on_account_release_profile("account_1")

        self.assertEqual(monitor.login_calls, ["account_2"])
        self.assertEqual(monitor.release_calls, ["account_1"])

    def test_account_query_calls_account_specific_manual_query(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.show_calls = []

            def show_account_status(self, account_id, force_refresh=True, source=""):
                self.show_calls.append((account_id, bool(force_refresh), source))

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._account_labels = {"account_2": "Cursor 개발"}
        statuses: list[tuple[str, str]] = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))

        view._on_account_query("account_2")

        self.assertEqual(monitor.show_calls, [("account_2", True, "manual_query")])
        self.assertTrue(statuses)
        self.assertEqual(statuses[-1][0], "Cursor 개발 사용량 조회를 시작했습니다.")
        self.assertEqual(statuses[-1][1], "info")

    def test_on_login_triggers_show_current_status(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.args = []

            def show_current_status(self, force_refresh: bool = True, source: str = ""):
                self.args.append((bool(force_refresh), str(source)))
                return None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)

        statuses: list[tuple[str, str]] = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))
        view._safe_get_runtime = lambda: {"can_login": True, "logout_in_progress": False}

        view._on_login()

        self.assertEqual(monitor.args, [(True, "manual_login")])
        self.assertTrue(statuses)
        self.assertEqual(statuses[-1][0], "연결 창을 여는 중입니다...")
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
        account_1_login = _FakeButton()
        account_1_logout = _FakeButton()
        account_2_login = _FakeButton()
        account_2_logout = _FakeButton()
        view._login_button = login_btn
        view._logout_button = logout_btn
        view._account_login_buttons = {
            "account_1": account_1_login,
            "account_2": account_2_login,
        }
        view._account_logout_buttons = {
            "account_1": account_1_logout,
            "account_2": account_2_logout,
        }

        view._refresh_action_buttons(
            {
                "can_login": True,
                "can_logout": False,
                "accounts": [
                    {
                        "id": "account_1",
                        "enabled": True,
                        "runtime": {"can_login": True, "can_logout": False},
                    },
                    {
                        "id": "account_2",
                        "enabled": True,
                        "runtime": {"can_login": False, "can_logout": True},
                    },
                ],
            }
        )
        self.assertFalse(login_btn.disabled)
        self.assertTrue(logout_btn.disabled)
        self.assertFalse(account_1_login.disabled)
        self.assertTrue(account_1_logout.disabled)
        self.assertTrue(account_2_login.disabled)
        self.assertFalse(account_2_logout.disabled)

        view._refresh_action_buttons(
            {
                "can_login": False,
                "can_logout": True,
                "enabled": False,
                "accounts": [
                    {
                        "id": "account_1",
                        "enabled": True,
                        "runtime": {"can_login": True, "can_logout": True},
                    },
                    {
                        "id": "account_2",
                        "enabled": True,
                        "runtime": {"can_login": True, "can_logout": True},
                    },
                ],
            }
        )
        self.assertTrue(login_btn.disabled)
        self.assertFalse(logout_btn.disabled)
        self.assertFalse(account_1_login.disabled)
        self.assertFalse(account_1_logout.disabled)
        self.assertFalse(account_2_login.disabled)
        self.assertFalse(account_2_logout.disabled)

    def test_refresh_action_buttons_keeps_idle_accounts_login_actionable_without_runtime(self) -> None:
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
        missing_runtime_login = _FakeButton()
        missing_runtime_logout = _FakeButton()
        empty_runtime_login = _FakeButton()
        empty_runtime_logout = _FakeButton()
        disabled_account_login = _FakeButton()
        disabled_account_logout = _FakeButton()
        view._login_button = _FakeButton()
        view._logout_button = _FakeButton()
        view._account_login_buttons = {
            "account_1": missing_runtime_login,
            "account_2": empty_runtime_login,
            "account_3": disabled_account_login,
        }
        view._account_logout_buttons = {
            "account_1": missing_runtime_logout,
            "account_2": empty_runtime_logout,
            "account_3": disabled_account_logout,
        }

        view._refresh_action_buttons(
            {
                "can_login": False,
                "can_logout": False,
                "enabled": False,
                "accounts": [
                    {"id": "account_2", "enabled": True, "runtime": {}},
                    {"id": "account_3", "enabled": False, "runtime": {}},
                ],
            }
        )

        self.assertFalse(missing_runtime_login.disabled)
        self.assertTrue(missing_runtime_logout.disabled)
        self.assertFalse(empty_runtime_login.disabled)
        self.assertTrue(empty_runtime_logout.disabled)
        self.assertTrue(disabled_account_login.disabled)
        self.assertTrue(disabled_account_logout.disabled)

    def test_refresh_action_buttons_uses_session_state_to_recover_from_stale_false_flags(self) -> None:
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
        logged_out_login = _FakeButton()
        logged_out_logout = _FakeButton()
        logged_in_login = _FakeButton()
        logged_in_logout = _FakeButton()
        view._login_button = _FakeButton()
        view._logout_button = _FakeButton()
        view._account_login_buttons = {
            "account_1": logged_out_login,
            "account_2": logged_in_login,
        }
        view._account_logout_buttons = {
            "account_1": logged_out_logout,
            "account_2": logged_in_logout,
        }

        view._refresh_action_buttons(
            {
                "can_login": False,
                "can_logout": False,
                "accounts": [
                    {
                        "id": "account_1",
                        "enabled": True,
                        "runtime": {
                            "session_state": "logged_out",
                            "monitor_state": "idle",
                            "can_login": False,
                            "can_logout": False,
                        },
                    },
                    {
                        "id": "account_2",
                        "enabled": True,
                        "runtime": {
                            "session_state": "logged_in",
                            "monitor_state": "idle",
                            "can_login": False,
                            "can_logout": False,
                        },
                    },
                ],
            }
        )

        self.assertFalse(logged_out_login.disabled)
        self.assertTrue(logged_out_logout.disabled)
        self.assertTrue(logged_in_login.disabled)
        self.assertFalse(logged_in_logout.disabled)

    def test_refresh_action_buttons_keeps_logged_out_login_actionable_while_querying(self) -> None:
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
        login_button = _FakeButton()
        logout_button = _FakeButton()
        view._login_button = _FakeButton()
        view._logout_button = _FakeButton()
        view._account_login_buttons = {"account_1": login_button}
        view._account_logout_buttons = {"account_1": logout_button}

        view._refresh_action_buttons(
            {
                "can_login": False,
                "can_logout": False,
                "collect_inflight": True,
                "accounts": [
                    {
                        "id": "account_1",
                        "enabled": True,
                        "runtime": {
                            "session_state": "logged_out",
                            "monitor_state": "running",
                            "collect_inflight": True,
                            "can_login": False,
                            "can_logout": False,
                        },
                    }
                ],
            }
        )

        self.assertFalse(login_button.disabled)
        self.assertTrue(logout_button.disabled)

    def test_account_login_guard_uses_account_runtime_permissions(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.login_calls = []

            def get_runtime_status(self):
                return {
                    "enabled": False,
                    "accounts": [
                        {
                            "id": "account_1",
                            "enabled": True,
                            "runtime": {"can_login": True, "can_logout": True},
                        }
                    ]
                }

            def login_account(self, account_id):
                self.login_calls.append(account_id)

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        statuses: list[tuple[str, str]] = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))

        view._on_account_login("account_1")

        self.assertEqual(monitor.login_calls, ["account_1"])
        self.assertTrue(statuses)
        self.assertEqual(statuses[-1][1], "info")

    def test_account_logout_guard_uses_account_runtime_permissions(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.release_calls = []

            def get_runtime_status(self):
                return {
                    "enabled": False,
                    "accounts": [
                        {
                            "id": "account_1",
                            "enabled": True,
                            "runtime": {"can_login": True, "can_logout": True},
                        }
                    ]
                }

            def release_account_profile_session(self, account_id):
                self.release_calls.append(account_id)
                return True, "released"

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._tk = object()
        statuses: list[tuple[str, str]] = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))

        with patch("tkinter.messagebox.askyesno", return_value=True):
            view._on_account_release_profile("account_1")

        self.assertEqual(monitor.release_calls, ["account_1"])
        self.assertTrue(statuses)
        self.assertEqual(statuses[-1][1], "info")

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
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
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

    def test_refresh_runtime_status_shows_pending_login_poll_state(self) -> None:
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
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_out",
            "monitor_state": "idle",
            "profile_in_use": False,
            "pending_login_poll_active": True,
            "pending_login_poll_remaining_sec": 482.7,
            "collect_inflight": False,
            "next_collect_in_sec": None,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "연결 완료 대기 중")
        self.assertEqual(view._next_collect_var.value, "최대 482초")

    def test_refresh_runtime_status_shows_open_login_window_wait_state(self) -> None:
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
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_out",
            "monitor_state": "idle",
            "profile_in_use": False,
            "pending_login_poll_active": True,
            "pending_login_poll_remaining_sec": 321.2,
            "browser_state": "headed_login",
            "login_window_open": True,
            "collect_inflight": False,
            "next_collect_in_sec": None,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "연결 완료 대기 중")
        self.assertEqual(view._next_collect_var.value, "최대 321초")

    def test_refresh_runtime_status_shows_missing_chrome_channel_state(self) -> None:
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
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_out",
            "monitor_state": "idle",
            "profile_in_use": False,
            "browser_state": "failed",
            "browser_last_error": "browser_channel_unavailable",
            "pending_login_poll_active": False,
            "collect_inflight": False,
            "next_collect_in_sec": None,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "Google Chrome 필요")
        self.assertEqual(view._next_collect_var.value, "-")

    def test_refresh_runtime_status_shows_auth_attention_state(self) -> None:
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
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_in",
            "monitor_state": "paused_auth_required",
            "auth_attention_required": True,
            "auth_attention_reason": "cloudflare_challenge",
            "profile_in_use": False,
            "pending_login_poll_active": False,
            "collect_inflight": False,
            "next_collect_in_sec": None,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "브라우저 인증 필요")
        self.assertEqual(view._next_collect_var.value, "-")

    def test_refresh_runtime_status_shows_cloudflare_pending_auth_state(self) -> None:
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
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_in",
            "monitor_state": "paused_auth_required",
            "auth_attention_required": True,
            "auth_attention_reason": "cloudflare_challenge",
            "profile_in_use": False,
            "pending_login_poll_active": True,
            "pending_login_poll_reason": "cloudflare_challenge",
            "pending_login_poll_remaining_sec": 88.8,
            "browser_state": "headed_login",
            "login_window_open": True,
            "collect_inflight": False,
            "next_collect_in_sec": None,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "인증 완료 대기 중")
        self.assertEqual(view._next_collect_var.value, "최대 88초")

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
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
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

        self.assertEqual(view._collect_state_var.value, "수동 조회 중")
        self.assertEqual(view._next_collect_var.value, "-")

    def test_refresh_runtime_status_shows_manual_login_window_opening_state(self) -> None:
        class _Var:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value
                return None

        class _FakeWin:
            def after(self, _delay, _fn):
                return "after"

        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._win = _FakeWin()
        view._collect_state_var = _Var()
        view._next_collect_var = _Var()
        view._live_time_var = _Var()
        view._live_five_hour_var = _Var()
        view._live_weekly_var = _Var()
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_out",
            "monitor_state": "running",
            "profile_in_use": False,
            "collect_inflight": True,
            "collect_source": "manual_login",
            "next_collect_in_sec": None,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "연결 창 여는 중")
        self.assertEqual(view._next_collect_var.value, "-")

    def test_refresh_runtime_status_preserves_last_values_while_collecting(self) -> None:
        class _Var:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value
                return None

        class _FakeWin:
            def after(self, _delay, _fn):
                return "after-token"

        class _FakeSnapshot:
            def to_dict(self):
                return {
                    "five_hour_limit": "24%",
                    "weekly_limit": "27%",
                    "gpt_5_3_codex_spark_five_hour_limit": "98%",
                    "gpt_5_3_codex_spark_weekly_limit": "99%",
                    "remaining_credit": "958",
                    "captured_at": "2026-03-30T12:58:00",
                    "five_hour_limit_reset_at": "2026-03-30T15:00:00+09:00",
                    "weekly_limit_reset_at": "2026-04-02T12:00:00+09:00",
                    "gpt_5_3_codex_spark_five_hour_limit_reset_at": (
                        "2026-03-30T13:08:00+09:00"
                    ),
                    "gpt_5_3_codex_spark_weekly_limit_reset_at": (
                        "2026-04-01T12:14:00+09:00"
                    ),
                }

        class _FakeMonitor:
            reset_keys: list[str]

            def __init__(self):
                self.reset_keys = []

            def get_last_snapshot(self):
                return _FakeSnapshot()

            def format_captured_at_for_display(self, value: str) -> str:
                self.captured_at = value
                return "2026-03-30 12:58:00"

            def format_reset_at_for_display(self, value: str, key: str = "") -> str:
                self.reset_keys.append(key)
                if "15:00:00" in value:
                    return "2026-03-30 15:00:00 (02h 02m 00s)"
                if "13:08:00" in value:
                    return "2026-03-30 13:08:00 (00h 10m 00s)"
                if "12:14:00" in value:
                    return "2026-04-01 12:14:00 (1d 23h 16m 00s)"
                return "2026-04-02 12:00:00 (2d 23h 02m 00s)"

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._win = _FakeWin()
        view._collect_state_var = _Var()
        view._next_collect_var = _Var()
        view._live_time_var = _Var()
        view._live_five_hour_var = _Var()
        view._live_five_hour_reset_var = _Var()
        view._live_weekly_var = _Var()
        view._live_weekly_reset_var = _Var()
        view._live_spark_five_hour_var = _Var()
        view._live_spark_five_hour_reset_var = _Var()
        view._live_spark_weekly_var = _Var()
        view._live_spark_weekly_reset_var = _Var()
        view._live_credit_var = _Var()
        view._refresh_action_buttons = lambda runtime: runtime
        view._safe_get_runtime = lambda: {
            "session_state": "logged_in",
            "monitor_state": "running",
            "profile_in_use": False,
            "collect_inflight": True,
            "collect_source": "monitor_tick",
            "next_collect_in_sec": 44,
            "next_collect_estimated": False,
        }

        view._refresh_runtime_status()

        self.assertEqual(view._collect_state_var.value, "자동 조회 중")
        self.assertEqual(view._next_collect_var.value, "-")
        self.assertEqual(view._live_time_var.value, "2026-03-30 12:58:00")
        self.assertEqual(view._live_five_hour_var.value, "24%")
        self.assertEqual(
            view._live_five_hour_reset_var.value,
            "2026-03-30 15:00:00 (02h 02m 00s)",
        )
        self.assertEqual(view._live_weekly_var.value, "27%")
        self.assertEqual(
            view._live_weekly_reset_var.value,
            "2026-04-02 12:00:00 (2d 23h 02m 00s)",
        )
        self.assertEqual(view._live_spark_five_hour_var.value, "98%")
        self.assertEqual(
            view._live_spark_five_hour_reset_var.value,
            "2026-03-30 13:08:00 (00h 10m 00s)",
        )
        self.assertEqual(view._live_spark_weekly_var.value, "99%")
        self.assertEqual(
            view._live_spark_weekly_reset_var.value,
            "2026-04-01 12:14:00 (1d 23h 16m 00s)",
        )
        self.assertEqual(view._live_credit_var.value, "958")
        self.assertEqual(monitor.captured_at, "2026-03-30T12:58:00")
        self.assertEqual(
            monitor.reset_keys,
            [
                "five_hour_limit_reset_at",
                "weekly_limit_reset_at",
                "gpt_5_3_codex_spark_five_hour_limit_reset_at",
                "gpt_5_3_codex_spark_weekly_limit_reset_at",
            ],
        )

    def test_refresh_runtime_status_shows_dash_while_collecting_without_snapshot(self) -> None:
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
        view._live_spark_five_hour_var = _Var()
        view._live_spark_weekly_var = _Var()
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

        self.assertEqual(view._collect_state_var.value, "수동 조회 중")
        self.assertEqual(view._next_collect_var.value, "-")
        self.assertEqual(view._live_time_var.value, "-")
        self.assertEqual(view._live_five_hour_var.value, "-")
        self.assertEqual(view._live_weekly_var.value, "-")
        self.assertEqual(view._live_spark_five_hour_var.value, "-")
        self.assertEqual(view._live_spark_weekly_var.value, "-")
        self.assertEqual(view._live_credit_var.value, "-")


if __name__ == "__main__":
    unittest.main()
