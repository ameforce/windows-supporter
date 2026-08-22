import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.apps.codex_usage_ui import CodexUsageSettingsView


class _FakeLabel:
    def __init__(self, owner, *args, **kwargs):
        _ = args
        self._owner = owner
        self.kwargs = dict(kwargs)
        self.pack_kwargs = {}
        self.grid_kwargs = {}
        self.bind_calls = []
        self.grid_remove_calls = 0
        owner.labels.append(self)

    def pack(self, **kwargs):
        self.pack_kwargs = dict(kwargs)
        return None

    def grid(self, **kwargs):
        if kwargs:
            self.grid_kwargs = dict(kwargs)
        return None

    def grid_remove(self):
        self.grid_remove_calls += 1
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
        self.yview_scroll_calls = []
        self.yview_moveto_calls = []
        if owner is not None:
            owner.canvases.append(self)

    def yview(self, *_args, **_kwargs):
        return None

    def yview_scroll(self, amount, unit):
        self.yview_scroll_calls.append((int(amount), str(unit)))
        return None

    def yview_moveto(self, fraction):
        self.yview_moveto_calls.append(float(fraction))
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
        self.entries = []
        self.scrollbars = []

    def Entry(self, *args, **kwargs):
        widget = _FakeWidget(self, *args, **kwargs)
        self.entries.append(widget)
        return widget

    def Button(self, *args, **kwargs):
        return _FakeButton(self, *args, **kwargs)

    def Scrollbar(self, *args, **kwargs):
        return _FakeScrollbar(self, *args, **kwargs)


class CodexUsageUiUnitTest(unittest.TestCase):
    def test_post_ui_propagates_dispatch_rejection(self) -> None:
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=None,
            ui_post=lambda _fn: False,
        )

        self.assertFalse(view._post_ui(lambda: None))

    def test_external_mutation_state_only_release_does_not_touch_tk(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._profile_deletions_inflight.add("__external_settings__")
        view._preserve_status_after_next_autosave = True

        view._release_external_settings_mutation_without_ui()

        self.assertNotIn("__external_settings__", view._profile_deletions_inflight)
        self.assertFalse(view._preserve_status_after_next_autosave)

    def test_external_mutation_failed_result_retries_captured_payload_on_ui_tick(self) -> None:
        captured = {"payload": {"enabled": False}, "before_providers": {}}
        monitor = Mock()
        monitor.get_runtime_snapshot.return_value = {}
        monitor.get_last_snapshot.return_value = None
        monitor.update_settings.return_value = (True, None)
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._profile_deletions_inflight.add("__external_settings__")
        view._win = Mock()
        view._collect_state_var = Mock()
        view._next_collect_var = Mock()
        view._live_time_var = Mock()
        view._live_five_hour_var = Mock()
        view._live_weekly_var = Mock()
        view._live_credit_var = Mock()
        scheduled = []
        view._win.after.side_effect = lambda _delay, callback: scheduled.append(callback) or "after-retry"

        worker = threading.Thread(
            target=lambda: view._record_external_settings_result_without_ui(
                False,
                "settings_save_failed",
                captured,
            )
        )
        worker.start()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertIn("__external_settings__", view._profile_deletions_inflight)
        view._refresh_runtime_status()

        self.assertNotIn("__external_settings__", view._profile_deletions_inflight)
        self.assertGreaterEqual(len(scheduled), 1)
        self.assertTrue(view._preserve_status_after_next_autosave)
        scheduled[0]()
        monitor.update_settings.assert_called_once_with(captured["payload"])
        self.assertFalse(view._preserve_status_after_next_autosave)

    def test_external_mutation_success_reconciles_settings_and_dashboard_on_ui_tick(self) -> None:
        reconciled = Mock()
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=None,
            on_external_settings_reconciled=reconciled,
        )
        view._win = Mock()
        view._remount = Mock()
        view._profile_deletions_inflight.add("__external_settings__")

        view._record_external_settings_result_without_ui(True, None, None)
        view._refresh_runtime_status()

        self.assertNotIn("__external_settings__", view._profile_deletions_inflight)
        view._remount.assert_called_once_with()
        reconciled.assert_called_once_with()

    def test_metric_presence_hides_unreported_codex_five_hour_and_disabled_cursor_od(self) -> None:
        fake_tk = _FakeTk()
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        codex_value = _FakeLabel(fake_tk)
        codex_reset = _FakeLabel(fake_tk)
        cursor_od = _FakeLabel(fake_tk)
        view._account_metric_cells = {
            "codex-1": {
                "five_hour_limit": codex_value,
                "five_hour_limit_reset_at": codex_reset,
            },
            "cursor-1": {"on_demand_status": cursor_od},
        }

        view._update_account_metric_visibility(
            "codex-1",
            provider="codex",
            descriptor_keys={"weekly_limit"},
            payload={"weekly_limit": "80%"},
        )
        view._update_account_metric_visibility(
            "cursor-1",
            provider="cursor",
            descriptor_keys={"included_usage"},
            payload={"on_demand_enabled": False, "on_demand_status": "OFF"},
        )

        self.assertEqual(codex_value.grid_remove_calls, 1)
        self.assertEqual(codex_reset.grid_remove_calls, 1)
        self.assertEqual(cursor_od.grid_remove_calls, 1)

    def test_runtime_refresh_keeps_only_one_scheduled_callback(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        win = _FakeWidget()
        view._win = win

        view._schedule_runtime_refresh(1000)
        first_after_id = view._runtime_after_id
        view._schedule_runtime_refresh(1000)

        self.assertEqual(len(win.after_calls), 2)
        self.assertEqual(win.after_cancel_calls, [first_after_id])

    def test_runtime_refresh_reschedules_after_any_pending_result_reconciliation(self) -> None:
        reconciler_names = (
            "_reconcile_pending_profile_release_result",
            "_reconcile_pending_profile_add_result",
            "_reconcile_pending_profile_delete_result",
            "_reconcile_external_settings_result",
        )
        for target_name in reconciler_names:
            with self.subTest(reconciler=target_name):
                view = CodexUsageSettingsView(root=None, codex_monitor=None)
                view._win = _FakeWidget()
                view._schedule_runtime_refresh = Mock()
                for reconciler_name in reconciler_names:
                    setattr(
                        view,
                        reconciler_name,
                        Mock(return_value=reconciler_name == target_name),
                    )

                view._refresh_runtime_status()

                view._schedule_runtime_refresh.assert_called_once_with(1000)

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

    def test_account_metric_rows_align_values_in_label_value_grid(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        fake_tk = _FakeTk()
        view._tk = fake_tk

        metric_vars, _display_vars = view._build_account_metric_rows(
            parent=object(),
            bg="#FFFFFF",
        )

        self.assertIn("captured_at", metric_vars)
        # 라벨-값-라벨-값 4열: 값이 항상 홀수 열에 정렬되어 눈이 열을 따라
        # 훑을 수 있어야 한다.
        label_cells = [
            label
            for label in fake_tk.labels
            if label.kwargs.get("text")
        ]
        value_cells = [
            label
            for label in fake_tk.labels
            if label.kwargs.get("textvariable") is not None
        ]
        self.assertEqual(
            {label.grid_kwargs.get("column") for label in label_cells},
            {0, 2},
        )
        self.assertEqual(
            {label.grid_kwargs.get("column") for label in value_cells},
            {1, 3},
        )
        self.assertTrue(
            all(label.kwargs.get("anchor") == "e" for label in label_cells)
        )
        self.assertTrue(
            all(int(label.kwargs.get("wraplength", 0)) <= 260 for label in value_cells)
        )

        captured_display = value_cells[0].kwargs.get("textvariable")
        metric_vars["captured_at"].set("2026-06-25 09:07:55")
        self.assertEqual(captured_display.get(), "2026-06-25 09:07:55")

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
        self.assertEqual(display_vars["included_usage"].get(), "-")
        self.assertEqual(display_vars["billing_reset_at"].get(), "-")
        self.assertEqual(display_vars["on_demand_status"].get(), "-")
        self.assertGreaterEqual(
            int(fake_tk.labels[-1].kwargs.get("wraplength", 0)),
            300,
        )
        metric_vars["on_demand_status"].set("활성화 · US$8.20\u00a0사용")
        self.assertEqual(
            display_vars["on_demand_status"].get(),
            "활성화 · US$8.20\u00a0사용",
        )

    def test_usage_metric_values_are_localized_without_changing_amounts(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        self.assertEqual(view._localize_usage_metric_value("68% left"), "68% 남음")
        self.assertEqual(view._localize_usage_metric_value("42% used"), "42% 사용")
        self.assertEqual(
            view._localize_usage_metric_value("Enabled · US$8.20 used"),
            "활성화 · US$8.20 사용",
        )

    def test_metric_display_keeps_value_and_suffix_as_one_visual_unit(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        self.assertEqual(
            view._format_account_metric_value(
                "weekly_limit",
                {"weekly_limit": "73% remaining"},
            ),
            "73%\u00a0남음",
        )
        self.assertEqual(
            view._format_account_metric_value(
                "on_demand_status",
                {"on_demand_status": "Enabled · US$8.20 used"},
            ),
            "활성화 · US$8.20\u00a0사용",
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

    def test_mount_wraps_codex_content_in_scrollable_viewport(self) -> None:
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

        self.assertEqual(len(fake_tk.canvases), 1)
        self.assertEqual(len(fake_ttk.scrollbars), 1)
        canvas = fake_tk.canvases[0]
        self.assertEqual(len(canvas.windows), 1)
        sequences = {sequence for sequence, _callback in canvas.bind_calls}
        self.assertTrue(
            {"<Up>", "<Down>", "<Prior>", "<Next>", "<Home>", "<End>", "<MouseWheel>"}
            <= sequences
        )

    def test_mount_keeps_two_account_settings_visible_inside_scroll_canvas(self) -> None:
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

        self.assertEqual(len(fake_tk.canvases), 1)
        self.assertEqual(len(fake_ttk.scrollbars), 1)
        texts = [label.kwargs.get("text") for label in fake_tk.labels]
        checkbox_texts = [
            checkbutton.kwargs.get("text") for checkbutton in fake_tk.checkbuttons
        ]
        self.assertIn("모니터링 사용", checkbox_texts)
        self.assertIn("작업표시줄 오버레이", checkbox_texts)
        self.assertIn("사용량 프로필 (저장 제한 없음 · 작업표시줄 표시 최대 2개)", texts)
        self.assertNotIn("실시간 상태", texts)
        self.assertNotIn("다음 모니터링까지", texts)

    def test_scroll_navigation_handles_keyboard_and_mouse_wheel(self) -> None:
        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._safe_get_settings = lambda: {
            "settings_path": "",
            "state_path": "",
            "profile_dir": "",
            "profiles": [],
        }
        view._load_settings = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(_FakeWidget())

        canvas = fake_tk.canvases[0]
        callbacks = {sequence: callback for sequence, callback in canvas.bind_calls}
        callbacks["<Down>"](object())
        callbacks["<Next>"](object())
        callbacks["<Home>"](object())
        callbacks["<End>"](object())
        callbacks["<MouseWheel>"](type("Event", (), {"delta": -240})())

        self.assertEqual(
            canvas.yview_scroll_calls,
            [(1, "units"), (1, "pages"), (2, "units")],
        )
        self.assertEqual(canvas.yview_moveto_calls, [0.0, 1.0])

    def test_scroll_navigation_reaches_focused_child_controls(self) -> None:
        class _FocusedEntry(_FakeWidget):
            def winfo_class(self):
                return "TEntry"

        class _FocusedButton(_FakeWidget):
            def winfo_class(self):
                return "TButton"

        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        canvas = _FakeCanvas()
        body = _FakeWidget()
        focused_entry = _FocusedEntry()
        focused_button = _FocusedButton()
        body.children.extend([focused_entry, focused_button])

        view._bind_scroll_navigation(canvas, body)

        entry_callbacks = {
            sequence: callback for sequence, callback in focused_entry.bind_calls
        }
        button_callbacks = {
            sequence: callback for sequence, callback in focused_button.bind_calls
        }
        self.assertNotIn("<Down>", entry_callbacks)
        self.assertNotIn("<Home>", entry_callbacks)
        self.assertIn("<Next>", entry_callbacks)
        self.assertIn("<Down>", button_callbacks)
        entry_callbacks["<Next>"](object())
        button_callbacks["<Down>"](object())
        self.assertEqual(canvas.yview_scroll_calls, [(1, "pages"), (1, "units")])

    def test_profile_cards_collapse_to_one_column_at_150_percent_scaling(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        class _TkBridge:
            def call(self, *_args):
                return 2.0

        widget = type("Widget", (), {"tk": _TkBridge()})()

        self.assertEqual(view._profile_card_column_count(widget), 1)
        self.assertEqual(view._profile_card_column_count(object()), 2)

    def test_profile_cards_collapse_to_one_column_when_viewport_is_narrow(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        class _TkBridge:
            def call(self, *_args):
                return 4.0 / 3.0

        widget = type("Widget", (), {"tk": _TkBridge()})()

        self.assertEqual(
            view._profile_card_column_count(widget, available_width=700),
            1,
        )
        self.assertEqual(
            view._profile_card_column_count(widget, available_width=640),
            1,
        )
        self.assertEqual(
            view._profile_card_column_count(widget, available_width=768),
            2,
        )
        self.assertEqual(
            view._profile_card_column_count(widget, available_width=820),
            2,
        )

    def test_profile_cards_reflow_when_viewport_crosses_narrow_boundary(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        cards = _FakeWidget()
        cards.tk = type("TkBridge", (), {"call": lambda _self, *_args: 4.0 / 3.0})()
        card_widgets = [_FakeWidget(), _FakeWidget(), _FakeWidget()]

        view._reflow_profile_cards(cards, card_widgets, available_width=700)
        self.assertEqual(
            [(card.grid_kwargs["row"], card.grid_kwargs["column"]) for card in card_widgets],
            [(0, 0), (1, 0), (2, 0)],
        )

        view._reflow_profile_cards(cards, card_widgets, available_width=820)
        self.assertEqual(
            [(card.grid_kwargs["row"], card.grid_kwargs["column"]) for card in card_widgets],
            [(0, 0), (0, 1), (1, 0)],
        )

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

    def test_mount_hides_codex_url_when_every_profile_is_cursor(self) -> None:
        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        parent = _FakeWidget()

        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._safe_get_settings = lambda: {
            "usage_url": "https://example.test/codex",
            "settings_path": "",
            "state_path": "",
            "profile_dir": "",
            "profiles": [
                {
                    "id": "account_1",
                    "provider": "cursor",
                    "label": "Cursor 1",
                    "enabled": True,
                }
            ],
        }
        view._load_settings = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(parent)

        texts = [label.kwargs.get("text") for label in fake_tk.labels]
        self.assertNotIn("Codex 조회 URL", texts)
        self.assertNotIn("조회 URL", texts)
        self.assertEqual(len(fake_ttk.entries), 2)

    def test_mount_renders_all_saved_profiles_with_add_and_delete_actions(self) -> None:
        class _FakeMonitor:
            def get_settings_snapshot(self):
                profiles = []
                for index in range(3):
                    profiles.append(
                        {
                            "id": f"profile_{index:032x}",
                            "label": f"프로필 {index + 1}",
                            "provider": "codex",
                            "enabled": True,
                            "taskbar_selected": index < 2,
                            "settings_path": "",
                            "state_path": "",
                            "profile_dir": "",
                        }
                    )
                return {
                    "enabled": True,
                    "taskbar_overlay_enabled": True,
                    "interval_sec": 90,
                    "tooltip_duration_ms": 7000,
                    "usage_url": "https://example.test",
                    "profiles": profiles,
                }

            def get_runtime_status(self):
                return {"profiles": []}

            def get_last_snapshot(self):
                return None

        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        view = CodexUsageSettingsView(root=None, codex_monitor=_FakeMonitor())
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(_FakeWidget())

        texts = [label.kwargs.get("text") for label in fake_tk.labels]
        self.assertEqual(view._account_order, [f"profile_{index:032x}" for index in range(3)])
        self.assertIn("프로필 1", texts)
        self.assertIn("프로필 2", texts)
        self.assertIn("프로필 3", texts)
        button_texts = [button.kwargs.get("text") for button in fake_ttk.buttons]
        self.assertIn("프로필 추가", button_texts)
        self.assertEqual(button_texts.count("삭제"), 3)

    def test_mount_with_zero_profiles_still_exposes_add_action(self) -> None:
        fake_tk = _FakeTk()
        fake_ttk = _FakeTtk()
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._tk = fake_tk
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._safe_get_settings = lambda: {
            "profiles": [],
            "settings_path": "",
            "state_path": "",
            "profile_dir": "",
        }
        view._load_settings = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(_FakeWidget())

        button_texts = [button.kwargs.get("text") for button in fake_ttk.buttons]
        self.assertIn("프로필 추가", button_texts)
        self.assertNotIn("연결", button_texts)

    def test_add_and_delete_profile_actions_remount_after_confirmed_manager_change(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.add_calls = []
                self.delete_calls = []

            def add_profile(self, provider):
                self.add_calls.append(provider)
                return True, None, {"id": "profile_0"}

            def delete_profile(self, profile_id, confirmed=False):
                self.delete_calls.append((profile_id, confirmed))
                return True, None

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=monitor,
            ui_post=lambda fn: fn(),
        )
        remounts = []
        view._remount = lambda: remounts.append(True)
        view._set_status = lambda *_args, **_kwargs: None

        with patch("src.apps.codex_usage_ui.threading.Thread", _InlineThread):
            view._on_add_profile()
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_delete_profile("profile_0", "테스트 프로필")

        self.assertEqual(monitor.add_calls, ["codex"])
        self.assertEqual(monitor.delete_calls, [("profile_0", True)])
        self.assertEqual(len(remounts), 2)

    def test_delete_profile_runs_manager_shutdown_off_the_tk_callback(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.delete_calls = []

            def delete_profile(self, profile_id, confirmed=False):
                self.delete_calls.append((profile_id, confirmed))
                return True, None

        class _DeferredThread:
            targets = []

            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self.targets.append(self._target)
                return None

        monitor = _FakeMonitor()
        posted = []
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=monitor,
            ui_post=lambda fn: posted.append(fn),
        )
        remounts = []
        statuses = []
        view._remount = lambda: remounts.append(True)
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))

        with patch("src.apps.codex_usage_ui.threading.Thread", _DeferredThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_delete_profile("profile_0", "테스트 프로필")
                view._on_delete_profile("profile_0", "테스트 프로필")
                view._on_delete_profile("profile_1", "다른 프로필")

        self.assertEqual(monitor.delete_calls, [])
        self.assertEqual(remounts, [])
        self.assertEqual(len(_DeferredThread.targets), 1)
        self.assertIn("삭제 작업이 진행 중", statuses[-1][0])

        _DeferredThread.targets[0]()

        self.assertEqual(monitor.delete_calls, [("profile_0", True)])
        self.assertEqual(remounts, [])
        self.assertEqual(len(posted), 1)

        posted[0]()

        self.assertEqual(remounts, [True])
        self.assertEqual(statuses[-1][1], "ok")
        self.assertEqual(view._profile_deletions_inflight, set())

    def test_delete_profile_clears_inflight_guard_on_failure_and_thread_start_error(self) -> None:
        class _FailingMonitor:
            def delete_profile(self, _profile_id, confirmed=False):
                self.confirmed = bool(confirmed)
                return False, "cleanup failed"

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self._target()
                return None

        statuses = []
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=_FailingMonitor(),
            ui_post=lambda fn: fn(),
        )
        remounts = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))
        view._remount = lambda: remounts.append(True)
        with patch("src.apps.codex_usage_ui.threading.Thread", _InlineThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_delete_profile("profile_0", "테스트 프로필")

        self.assertEqual(view._profile_deletions_inflight, set())
        self.assertEqual(statuses[-1][1], "error")
        self.assertEqual(remounts, [True])

        class _StartFailThread:
            def __init__(self, target=None, daemon=None):
                _ = (target, daemon)

            def start(self):
                raise RuntimeError("thread start failed")

        with patch("src.apps.codex_usage_ui.threading.Thread", _StartFailThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_delete_profile("profile_0", "테스트 프로필")

        self.assertEqual(view._profile_deletions_inflight, set())
        self.assertIn("시작하지 못했습니다", statuses[-1][0])
        self.assertEqual(statuses[-1][1], "error")
        self.assertEqual(remounts, [True])

    def test_delete_queue_failure_reconciles_failed_presave_on_tk_tick(self) -> None:
        worker_finished = threading.Event()

        class _FakeMonitor:
            def delete_profile(self, _profile_id, confirmed=False):
                self.confirmed = confirmed
                self.fail("delete must not run after a failed pre-save")

        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=_FakeMonitor(),
            ui_post=lambda _fn: False,
        )
        view._win = _FakeWidget()
        view._autosave_after_id = "after-delete"
        prepared = {"payload": {"profiles": [{"id": "account_1"}]}}
        view._build_settings_update = lambda: prepared
        view._apply_settings_update = lambda _prepared, update_ui=False: (
            worker_finished.set() and False,
            "settings_save_failed",
            False,
        )
        view._set_status = lambda *_args, **_kwargs: None
        retries = []
        view._schedule_captured_autosave_retry = lambda payload: retries.append(payload)

        with patch("tkinter.messagebox.askyesno", return_value=True):
            view._on_delete_profile("account_1", "Codex 1")

        self.assertTrue(worker_finished.wait(1.0))
        self.assertIn("account_1", view._profile_deletions_inflight)
        self.assertTrue(view._reconcile_pending_profile_delete_result())
        self.assertEqual(view._profile_deletions_inflight, set())
        self.assertEqual(retries, [prepared])
        self.assertFalse(view._reconcile_pending_profile_delete_result())

    def test_delete_success_retries_edits_accepted_while_worker_was_running(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._profile_deletions_inflight.add("account_1")
        captured = {
            "payload": {
                "profiles": [
                    {"id": "account_1", "label": "deleted"},
                    {"id": "account_2", "label": "edited"},
                ],
                "accounts": [
                    {"id": "account_1", "label": "deleted"},
                    {"id": "account_2", "label": "edited"},
                ],
                "profile_order": ["account_1", "account_2"],
                "selected_profile_ids": ["account_1", "account_2"],
                "default_account_id": "account_1",
            },
            "before_providers": {
                "account_1": "codex",
                "account_2": "cursor",
            },
        }
        view._build_settings_update = lambda: captured
        view._set_status = lambda *_args, **_kwargs: None
        remounts = []
        retries = []
        view._remount = lambda: remounts.append(True)
        view._schedule_captured_autosave_retry = lambda payload: retries.append(payload)

        view._schedule_autosave()
        view._finish_profile_delete_on_ui(
            "account_1",
            True,
            None,
            False,
            None,
        )

        self.assertEqual(remounts, [True])
        self.assertEqual(len(retries), 1)
        retry_payload = retries[0]["payload"]
        self.assertEqual(retry_payload["profile_order"], ["account_2"])
        self.assertEqual(retry_payload["selected_profile_ids"], ["account_2"])
        self.assertEqual(retry_payload["default_account_id"], "account_2")
        self.assertEqual(
            [item["id"] for item in retry_payload["profiles"]],
            ["account_2"],
        )

    def test_external_toggle_success_retries_edits_accepted_while_blocked(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._profile_deletions_inflight.add("__external_settings__")
        captured = {"payload": {"profiles": [{"id": "account_1", "label": "edited"}]}}
        view._build_settings_update = lambda: captured
        view._set_status = lambda *_args, **_kwargs: None
        remounts = []
        retries = []
        view._remount = lambda: remounts.append(True)
        view._schedule_captured_autosave_retry = lambda payload: retries.append(payload)

        view._schedule_autosave()
        view._finish_external_settings_mutation(True, None)

        self.assertEqual(remounts, [True])
        self.assertEqual(retries, [captured])
        self.assertEqual(view._profile_deletions_inflight, set())

    def test_external_toggle_failure_retries_edits_accepted_while_blocked(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._profile_deletions_inflight.add("__external_settings__")
        captured = {"payload": {"profiles": [{"id": "account_1", "label": "edited"}]}}
        view._build_settings_update = lambda: captured
        view._set_status = lambda *_args, **_kwargs: None
        retries = []
        view._schedule_captured_autosave_retry = lambda payload: retries.append(payload)

        view._schedule_autosave()
        view._finish_external_settings_mutation(False, "toggle_failed")

        self.assertEqual(retries, [captured])
        self.assertEqual(view._profile_deletions_inflight, set())

    def test_delete_inflight_blocks_all_other_profile_settings_mutations(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.add_calls = []
                self.update_calls = []

            def get_settings_snapshot(self):
                return {"profiles": []}

            def add_profile(self, provider):
                self.add_calls.append(provider)
                return True, None, {"id": "profile_new"}

            def update_settings(self, payload):
                self.update_calls.append(payload)
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._profile_deletions_inflight.add("account_1")
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.invalid/usage")
        view._account_order = ["account_1", "account_2"]
        view._account_taskbar_selected_vars = {
            "account_1": _FakeVar(value=True),
        }
        remounts = []
        scheduled = []
        statuses = []
        view._remount = lambda: remounts.append(True)
        view._schedule_autosave = lambda: scheduled.append(True)
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))

        view._on_add_profile()
        view._on_move_account("account_1", 1)
        view._on_taskbar_selection_changed("account_1")
        saved = view._save_settings()

        self.assertFalse(saved)
        self.assertEqual(monitor.add_calls, [])
        self.assertEqual(monitor.update_calls, [])
        self.assertEqual(view._account_order, ["account_1", "account_2"])
        self.assertEqual(remounts, [])
        self.assertEqual(scheduled, [])
        self.assertTrue(statuses)
        self.assertTrue(all("변경 중" in text for text, _level in statuses))

    def test_delete_flushes_pending_autosave_inside_worker_before_deletion(self) -> None:
        class _FakeMonitor:
            def delete_profile(self, _profile_id, confirmed=False):
                return bool(confirmed), None

        class _DeferredThread:
            targets = []

            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self.targets.append(self._target)
                return None

        class _FakeWin:
            def __init__(self):
                self.after_cancel_calls = []

            def after_cancel(self, after_id):
                self.after_cancel_calls.append(after_id)

        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=_FakeMonitor(),
            ui_post=lambda fn: fn(),
        )
        view._win = _FakeWin()
        view._autosave_after_id = "after-1"
        events = []
        view._build_settings_update = lambda: {"payload": "dirty"}
        view._apply_settings_update = (
            lambda prepared, update_ui=False: events.append(("save", prepared, update_ui))
            or (True, None, False)
        )
        view._set_status = lambda *_args, **_kwargs: None

        with patch("src.apps.codex_usage_ui.threading.Thread", _DeferredThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_delete_profile("account_2", "Codex 2")

        self.assertEqual(view._win.after_cancel_calls, ["after-1"])
        self.assertEqual(len(_DeferredThread.targets), 1)
        self.assertEqual(events, [])

        _DeferredThread.targets[0]()

        self.assertEqual(events[0], ("save", {"payload": "dirty"}, False))

    def test_pending_provider_save_does_not_block_delete_tk_callback(self) -> None:
        save_started = threading.Event()
        release_save = threading.Event()
        delete_finished = threading.Event()

        class _FakeMonitor:
            def __init__(self):
                self.delete_calls = []

            def delete_profile(self, profile_id, confirmed=False):
                self.delete_calls.append((profile_id, confirmed))
                delete_finished.set()
                return True, None

        monitor = _FakeMonitor()
        posted = []
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=monitor,
            ui_post=lambda fn: posted.append(fn),
        )
        view._win = _FakeWidget()
        view._autosave_after_id = "after-provider-change"
        view._build_settings_update = lambda: {"payload": "provider-change"}

        def blocking_save(prepared, update_ui=False):
            self.assertEqual(prepared, {"payload": "provider-change"})
            self.assertFalse(update_ui)
            save_started.set()
            release_save.wait(2.0)
            return True, None, True

        view._apply_settings_update = blocking_save
        view._set_status = lambda *_args, **_kwargs: None
        started_at = time.monotonic()

        with patch("tkinter.messagebox.askyesno", return_value=True):
            view._on_delete_profile("account_2", "Codex 2")

        callback_elapsed = time.monotonic() - started_at
        self.assertLess(callback_elapsed, 0.25)
        self.assertTrue(save_started.wait(1.0))
        self.assertEqual(monitor.delete_calls, [])

        release_save.set()
        self.assertTrue(delete_finished.wait(1.0))
        self.assertEqual(monitor.delete_calls, [("account_2", True)])
        self.assertEqual(len(posted), 1)
        posted[0]()
        self.assertEqual(view._profile_deletions_inflight, set())

    def test_delete_aborts_when_pending_autosave_flush_fails(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.delete_calls = []

            def delete_profile(self, profile_id, confirmed=False):
                self.delete_calls.append((profile_id, confirmed))
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=monitor,
            ui_post=lambda fn: fn(),
        )
        view._win = _FakeWidget()
        view._autosave_after_id = "after-1"
        view._build_settings_update = lambda: {"payload": "dirty"}
        view._apply_settings_update = lambda _prepared, update_ui=False: (
            False,
            "save failed",
            False,
        )
        statuses = []
        scheduled = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))
        view._schedule_captured_autosave_retry = lambda payload: scheduled.append(payload)

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self._target()

        with patch("src.apps.codex_usage_ui.threading.Thread", _InlineThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_delete_profile("account_2", "Codex 2")

        self.assertEqual(monitor.delete_calls, [])
        self.assertEqual(view._profile_deletions_inflight, set())
        self.assertEqual(scheduled, [{"payload": "dirty"}])
        self.assertIn("삭제를 시작하지 않았습니다", statuses[-1][0])
        self.assertEqual(statuses[-1][1], "error")

    def test_add_reschedules_pending_autosave_when_flush_fails(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.add_calls = []

            def add_profile(self, provider):
                self.add_calls.append(provider)
                return True, None, {"id": "profile_new"}

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self._target()

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=monitor,
            ui_post=lambda fn: fn(),
        )
        view._win = _FakeWidget()
        view._autosave_after_id = "after-add"
        view._build_settings_update = lambda: {"payload": "dirty"}
        view._apply_settings_update = lambda _prepared, update_ui=False: (
            False,
            "save failed",
            False,
        )
        scheduled = []
        view._schedule_autosave = lambda: scheduled.append(True)
        view._set_status = lambda *_args, **_kwargs: None

        with patch("src.apps.codex_usage_ui.threading.Thread", _InlineThread):
            view._on_add_profile()

        self.assertEqual(monitor.add_calls, [])
        self.assertEqual(view._profile_deletions_inflight, set())
        self.assertEqual(scheduled, [True])

    def test_third_taskbar_selection_is_reverted_before_autosave(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.payloads = []

            def get_settings_snapshot(self):
                return {
                    "profiles": [
                        {"id": "account_1", "provider": "codex"},
                        {"id": "account_2", "provider": "cursor"},
                        {"id": "profile_0", "provider": "codex"},
                    ]
                }

            def update_settings(self, payload):
                self.payloads.append(payload)
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        view._account_order = ["account_1", "account_2", "profile_0"]
        view._account_enabled_vars = {
            profile_id: _FakeVar(value=True)
            for profile_id in view._account_order
        }
        view._account_provider_vars = {
            "account_1": _FakeVar(value="codex"),
            "account_2": _FakeVar(value="cursor"),
            "profile_0": _FakeVar(value="codex"),
        }
        view._account_taskbar_selected_vars = {
            "account_1": _FakeVar(value=True),
            "account_2": _FakeVar(value=True),
            "profile_0": _FakeVar(value=True),
        }
        view._win = _FakeWidget()
        statuses = []
        view._set_status = lambda text, level="info": statuses.append((text, level))
        view._autosave_after_id = "after-existing"

        view._on_taskbar_selection_changed("profile_0")

        self.assertFalse(view._account_taskbar_selected_vars["profile_0"].get())
        self.assertEqual(view._win.after_cancel_calls, ["after-existing"])
        self.assertIsNotNone(view._autosave_after_id)
        self.assertIn("최대 2개", statuses[-1][0])
        self.assertEqual(statuses[-1][1], "error")

        _delay, autosave = view._win.after_calls[-1]
        autosave()

        self.assertEqual(monitor.payloads[-1]["selected_profile_ids"], ["account_1", "account_2"])
        self.assertIn("최대 2개", statuses[-1][0])
        self.assertEqual(statuses[-1][1], "error")

    def test_add_flushes_pending_autosave_inside_worker_before_profile_creation(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.add_calls = []

            def add_profile(self, provider):
                self.add_calls.append(provider)
                return True, None, {"id": "profile_new"}

        class _DeferredThread:
            targets = []

            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self.targets.append(self._target)

        monitor = _FakeMonitor()
        posted = []
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=monitor,
            ui_post=lambda fn: posted.append(fn) or True,
        )
        view._win = _FakeWidget()
        view._autosave_after_id = "after-add"
        events = []
        view._build_settings_update = lambda: {"payload": "dirty"}
        view._apply_settings_update = (
            lambda prepared, update_ui=False: events.append(("save", prepared, update_ui))
            or (True, None, False)
        )
        view._set_status = lambda *_args, **_kwargs: None
        view._remount = lambda: events.append(("remount",))

        with patch("src.apps.codex_usage_ui.threading.Thread", _DeferredThread):
            view._on_add_profile()

        self.assertEqual(monitor.add_calls, [])
        self.assertEqual(events, [])
        self.assertEqual(view._win.after_cancel_calls, ["after-add"])
        self.assertEqual(len(_DeferredThread.targets), 1)

        _DeferredThread.targets[0]()

        self.assertEqual(events[0], ("save", {"payload": "dirty"}, False))
        self.assertEqual(monitor.add_calls, [])
        self.assertEqual(len(posted), 1)
        posted[0]()
        self.assertEqual(monitor.add_calls, ["codex"])
        self.assertEqual(events[-1], ("remount",))

    def test_pending_provider_change_is_flushed_on_ui_before_add_worker(self) -> None:
        ui_thread_id = threading.get_ident()
        update_thread_ids = []
        add_thread_ids = []

        class _FakeMonitor:
            def add_profile(self, _provider):
                add_thread_ids.append(threading.get_ident())
                return True, None, {"id": "profile_new"}

        class _DeferredThread:
            targets = []

            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self.targets.append(self._target)

        prepared = {
            "payload": {
                "profiles": [{"id": "account_1", "provider": "cursor"}],
            },
            "before_providers": {"account_1": "codex"},
        }
        view = CodexUsageSettingsView(root=None, codex_monitor=_FakeMonitor())
        view._win = _FakeWidget()
        view._autosave_after_id = "after-add"
        view._build_settings_update = lambda: prepared
        view._apply_settings_update = lambda _prepared, update_ui=False: (
            update_thread_ids.append(threading.get_ident()) or True,
            None,
            True,
        )
        view._set_status = lambda *_args, **_kwargs: None
        view._remount = lambda: None

        with patch("src.apps.codex_usage_ui.threading.Thread", _DeferredThread):
            view._on_add_profile()

        self.assertEqual(update_thread_ids, [ui_thread_id])
        self.assertEqual(add_thread_ids, [ui_thread_id])
        self.assertEqual(_DeferredThread.targets, [])

    def test_pending_provider_change_is_flushed_on_ui_before_delete_worker(self) -> None:
        ui_thread_id = threading.get_ident()
        update_thread_ids = []
        delete_thread_ids = []

        class _FakeMonitor:
            def delete_profile(self, _profile_id, confirmed=False):
                self.confirmed = confirmed
                delete_thread_ids.append(threading.get_ident())
                return True, None

        class _DeferredThread:
            targets = []

            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self.targets.append(self._target)

        prepared = {
            "payload": {
                "profiles": [{"id": "account_1", "provider": "cursor"}],
            },
            "before_providers": {"account_1": "codex"},
        }
        view = CodexUsageSettingsView(root=None, codex_monitor=_FakeMonitor())
        view._win = _FakeWidget()
        view._autosave_after_id = "after-delete"
        view._build_settings_update = lambda: prepared
        view._apply_settings_update = lambda _prepared, update_ui=False: (
            update_thread_ids.append(threading.get_ident()) or True,
            None,
            True,
        )
        view._set_status = lambda *_args, **_kwargs: None

        with patch("src.apps.codex_usage_ui.threading.Thread", _DeferredThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_delete_profile("account_1", "Codex 1")

        self.assertEqual(update_thread_ids, [ui_thread_id])
        self.assertEqual(delete_thread_ids, [])
        self.assertEqual(len(_DeferredThread.targets), 1)
        worker = threading.Thread(target=_DeferredThread.targets[0])
        worker.start()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(update_thread_ids, [ui_thread_id])
        self.assertEqual(len(delete_thread_ids), 1)
        self.assertNotEqual(delete_thread_ids[0], ui_thread_id)

    def test_add_profile_creator_runs_on_the_tk_thread_after_worker_save(self) -> None:
        ui_thread_id = threading.get_ident()
        creator_thread_ids = []
        posted = []

        class _FakeMonitor:
            def add_profile(self, _provider):
                creator_thread_ids.append(threading.get_ident())
                return True, None, {"id": "profile_new"}

        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=_FakeMonitor(),
            ui_post=lambda fn: posted.append(fn) or True,
        )
        view._win = _FakeWidget()
        view._autosave_after_id = "after-add"
        view._build_settings_update = lambda: {"payload": "dirty"}
        view._apply_settings_update = lambda _prepared, update_ui=False: (
            True,
            None,
            False,
        )
        view._set_status = lambda *_args, **_kwargs: None
        view._remount = lambda: None

        view._on_add_profile()
        deadline = time.monotonic() + 1.0
        while not posted and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertTrue(posted)
        self.assertEqual(creator_thread_ids, [])
        posted[0]()
        self.assertEqual(creator_thread_ids, [ui_thread_id])

    def test_add_profile_queue_failure_reconciles_creator_exactly_once_on_tk_tick(self) -> None:
        ui_thread_id = threading.get_ident()
        creator_thread_ids = []
        worker_finished = threading.Event()

        class _FakeMonitor:
            def add_profile(self, _provider):
                creator_thread_ids.append(threading.get_ident())
                return True, None, {"id": "profile_new"}

        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=_FakeMonitor(),
            ui_post=lambda _fn: False,
        )
        view._win = _FakeWidget()
        view._autosave_after_id = "after-add"
        view._build_settings_update = lambda: {"payload": "dirty"}
        view._apply_settings_update = lambda _prepared, update_ui=False: (
            worker_finished.set() or True,
            None,
            False,
        )
        view._set_status = lambda *_args, **_kwargs: None
        remounts = []
        captured_retries = []
        view._remount = lambda: remounts.append(True)
        view._schedule_captured_autosave_retry = (
            lambda prepared: captured_retries.append(prepared)
        )

        view._on_add_profile()

        self.assertTrue(worker_finished.wait(1.0))
        self.assertEqual(creator_thread_ids, [])
        self.assertIn("__add_profile__", view._profile_deletions_inflight)
        view._schedule_autosave()
        view._on_add_profile()
        self.assertEqual(creator_thread_ids, [])

        self.assertTrue(view._reconcile_pending_profile_add_result())
        self.assertEqual(creator_thread_ids, [ui_thread_id])
        self.assertEqual(remounts, [True])
        self.assertEqual(captured_retries, [{"payload": "dirty"}])
        self.assertNotIn("__add_profile__", view._profile_deletions_inflight)

        self.assertFalse(view._reconcile_pending_profile_add_result())
        self.assertEqual(creator_thread_ids, [ui_thread_id])

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

    def test_autosave_retries_after_profile_refresh_busy(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.update_payloads = []

            def get_settings_snapshot(self):
                return {"accounts": []}

            def update_settings(self, payload):
                self.update_payloads.append(dict(payload))
                if len(self.update_payloads) == 1:
                    return False, "profile_refresh_busy"
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._win = _FakeWidget()
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        view._set_status = lambda *_args, **_kwargs: None

        self.assertFalse(view._autosave_now())

        self.assertEqual(len(monitor.update_payloads), 1)
        self.assertEqual(len(view._win.after_calls), 1)
        self.assertIsNotNone(view._autosave_after_id)

        _delay_ms, retry = view._win.after_calls[-1]
        self.assertTrue(retry())
        self.assertEqual(len(monitor.update_payloads), 2)
        self.assertIsNone(view._autosave_after_id)

    def test_autosave_busy_retry_does_not_recurse_when_after_registration_fails(self) -> None:
        class _AlwaysBusyMonitor:
            def __init__(self):
                self.update_payloads = []

            def get_settings_snapshot(self):
                return {"accounts": []}

            def update_settings(self, payload):
                self.update_payloads.append(dict(payload))
                return False, "profile_refresh_busy"

        class _AfterFailureWidget:
            def after(self, _delay_ms, _callback):
                raise RuntimeError("window already destroyed")

        monitor = _AlwaysBusyMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._win = _AfterFailureWidget()
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        view._set_status = lambda *_args, **_kwargs: None

        result = view._autosave_now()

        self.assertFalse(result)
        self.assertEqual(len(monitor.update_payloads), 1)
        self.assertIsNone(view._autosave_after_id)

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
        self.assertIn("▲", button_texts)
        self.assertIn("▼", button_texts)

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

    def test_successful_provider_switch_remounts_provider_specific_metric_surface(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.settings = {
                    "profiles": [
                        {"id": "account_1", "provider": "codex", "enabled": True},
                    ]
                }

            def get_settings_snapshot(self):
                return self.settings

            def update_settings(self, payload):
                self.settings = {"profiles": [dict(payload["profiles"][0])]}
                return True, None

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        view._account_order = ["account_1"]
        view._account_enabled_vars = {"account_1": _FakeVar(value=True)}
        view._account_provider_vars = {"account_1": _FakeVar(value="cursor")}
        view._account_taskbar_selected_vars = {"account_1": _FakeVar(value=True)}
        view._set_status = lambda *_args, **_kwargs: None
        remounts = []
        view._remount = lambda: remounts.append("remounted")

        self.assertTrue(view._save_settings())

        self.assertEqual(remounts, ["remounted"])

    def test_failed_save_preserves_dirty_provider_selector_for_retry(self) -> None:
        class _FakeMonitor:
            def get_settings_snapshot(self):
                return {
                    "profiles": [
                        {"id": "account_1", "provider": "codex", "enabled": True},
                    ]
                }

            def update_settings(self, _payload):
                return False, "provider_switch_failed"

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._enabled_var = _FakeVar(value=True)
        view._taskbar_overlay_var = _FakeVar(value=True)
        view._interval_var = _FakeVar(value="90")
        view._tooltip_var = _FakeVar(value="7")
        view._usage_url_var = _FakeVar(value="https://example.test")
        view._account_order = ["account_1"]
        view._account_enabled_vars = {"account_1": _FakeVar(value=True)}
        provider_var = _FakeVar(value="cursor")
        view._account_provider_vars = {"account_1": provider_var}
        view._account_taskbar_selected_vars = {"account_1": _FakeVar(value=True)}
        view._set_status = lambda *_args, **_kwargs: None
        load_calls = []
        view._load_settings = lambda: load_calls.append(True)
        remounts = []
        view._remount = lambda: remounts.append("remounted")

        self.assertFalse(view._save_settings())

        self.assertEqual(provider_var.get(), "cursor")
        self.assertEqual(load_calls, [])
        self.assertEqual(remounts, [])

    def test_runtime_provider_mismatch_remounts_before_updating_old_metric_vars(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._account_rendered_providers = {"account_1": "codex"}
        old_metric = _FakeVar(value="old codex metric")
        view._account_metric_vars = {"account_1": {"five_hour_limit": old_metric}}
        remounts = []
        view._remount = lambda: remounts.append("remounted")

        view._refresh_account_runtime_summaries(
            {
                "profiles": [
                    {
                        "id": "account_1",
                        "provider": "cursor",
                        "enabled": True,
                        "runtime": {},
                        "last_snapshot": {"included_usage": "80% left"},
                        "metrics": [],
                    }
                ]
            }
        )

        self.assertEqual(remounts, ["remounted"])
        self.assertEqual(old_metric.get(), "old codex metric")

    def test_runtime_label_refreshes_profile_header_and_action_label(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        label_var = _FakeVar(value="Cursor fallback")
        view._account_label_vars = {"account_1": label_var}
        view._account_labels = {"account_1": "Cursor fallback"}

        view._refresh_account_runtime_summaries(
            {
                "profiles": [
                    {
                        "id": "account_1",
                        "provider": "cursor",
                        "label": "Stable Cursor",
                        "enabled": True,
                        "runtime": {},
                        "last_snapshot": {},
                        "metrics": [],
                    }
                ]
            }
        )

        self.assertEqual(label_var.get(), "Stable Cursor")
        self.assertEqual(view._account_labels["account_1"], "Stable Cursor")

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

    def test_account_release_inflight_blocks_same_profile_delete(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.release_calls = []
                self.delete_calls = []

            def get_runtime_status(self):
                return {
                    "accounts": [
                        {
                            "id": "account_1",
                            "enabled": True,
                            "runtime": {"can_login": False, "can_logout": True},
                        }
                    ]
                }

            def release_account_profile_session(self, account_id):
                self.release_calls.append(account_id)
                return True, "released"

            def delete_profile(self, profile_id, confirmed=False):
                self.delete_calls.append((profile_id, confirmed))
                return True, None

        class _DeferredThread:
            targets = []

            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self.targets.append(self._target)
                return None

        monitor = _FakeMonitor()
        posted = []
        statuses = []
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=monitor,
            ui_post=lambda fn: posted.append(fn),
        )
        view._tk = object()
        view._win = object()
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))
        view._load_settings = lambda: None
        view._refresh_runtime_status = lambda: None

        with patch("src.apps.codex_usage_ui.threading.Thread", _DeferredThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_account_release_profile("account_1")
                view._on_delete_profile("account_1", "Codex 1")

        self.assertEqual(len(_DeferredThread.targets), 1)
        self.assertEqual(monitor.delete_calls, [])
        self.assertIn("진행 중", statuses[-1][0])

        _DeferredThread.targets[0]()
        self.assertEqual(monitor.release_calls, ["account_1"])
        self.assertEqual(len(posted), 1)
        posted[0]()
        self.assertEqual(view._profile_actions_inflight, set())

    def test_account_release_retries_blocked_edits_without_reloading_stale_settings(self) -> None:
        class _FakeMonitor:
            def get_runtime_status(self):
                return {
                    "accounts": [
                        {
                            "id": "account_1",
                            "enabled": True,
                            "runtime": {"can_login": False, "can_logout": True},
                        }
                    ]
                }

            def release_account_profile_session(self, account_id):
                self.released = account_id
                return True, "released"

        class _DeferredThread:
            targets = []

            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self.targets.append(self._target)
                return None

        captured = {"payload": {"profiles": [{"id": "account_1", "enabled": False}]}}
        posted = []
        events = []
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=_FakeMonitor(),
            ui_post=lambda fn: posted.append(fn) or True,
        )
        view._tk = object()
        view._win = object()
        view._set_status = lambda *_args, **_kwargs: None
        view._build_settings_update = lambda: captured
        view._load_settings = lambda: events.append(("load", None))
        view._refresh_runtime_status = lambda: events.append(("refresh", None))
        view._schedule_captured_autosave_retry = (
            lambda prepared: events.append(("retry", prepared))
        )

        with patch("src.apps.codex_usage_ui.threading.Thread", _DeferredThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_account_release_profile("account_1")
        self.assertEqual(len(_DeferredThread.targets), 1)
        view._schedule_autosave()
        self.assertTrue(view._blocked_mutation_settings_changed)

        _DeferredThread.targets[0]()
        self.assertEqual(len(posted), 1)
        posted[0]()

        self.assertEqual(events.count(("retry", captured)), 1)
        self.assertNotIn(("load", None), events)
        self.assertIn(("refresh", None), events)
        self.assertFalse(view._blocked_mutation_settings_changed)

    def test_account_release_queue_failure_reconciles_blocked_edits_on_ui_tick(self) -> None:
        class _FakeMonitor:
            def get_runtime_status(self):
                return {
                    "accounts": [
                        {
                            "id": "account_1",
                            "enabled": True,
                            "runtime": {"can_login": False, "can_logout": True},
                        }
                    ]
                }

            def release_account_profile_session(self, _account_id):
                return True, "released"

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                self._target()
                return None

        captured = {"payload": {"profiles": [{"id": "account_1", "enabled": False}]}}
        retries = []
        loads = []
        view = CodexUsageSettingsView(
            root=None,
            codex_monitor=_FakeMonitor(),
            ui_post=lambda _fn: False,
        )
        view._tk = object()
        view._win = object()
        view._set_status = lambda *_args, **_kwargs: None
        view._build_settings_update = lambda: captured
        view._load_settings = lambda: loads.append(True)
        view._refresh_runtime_status = lambda: None
        view._schedule_captured_autosave_retry = lambda prepared: retries.append(prepared)

        with patch("src.apps.codex_usage_ui.threading.Thread", _InlineThread):
            with patch("tkinter.messagebox.askyesno", return_value=True):
                view._on_account_release_profile("account_1")

        self.assertEqual(view._profile_actions_inflight, {"account_1"})
        self.assertIsNotNone(view._pending_profile_release_result)
        view._schedule_autosave()
        self.assertTrue(view._blocked_mutation_settings_changed)

        self.assertTrue(view._reconcile_pending_profile_release_result())
        self.assertEqual(retries, [captured])
        self.assertEqual(loads, [])
        self.assertEqual(view._profile_actions_inflight, set())

    def test_delete_inflight_blocks_profile_query_login_and_release_actions(self) -> None:
        class _FakeMonitor:
            def __init__(self):
                self.calls = []

            def get_runtime_status(self):
                return {
                    "accounts": [
                        {
                            "id": "account_1",
                            "enabled": True,
                            "runtime": {"can_login": True, "can_logout": True},
                        }
                    ]
                }

            def login_account(self, account_id):
                self.calls.append(("login", account_id))

            def show_account_status(self, account_id, **_kwargs):
                self.calls.append(("query", account_id))

            def release_account_profile_session(self, account_id):
                self.calls.append(("release", account_id))
                return True, "released"

        monitor = _FakeMonitor()
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor, ui_post=lambda fn: fn())
        view._tk = object()
        view._win = object()
        view._profile_deletions_inflight.add("account_1")
        statuses = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))

        with patch("tkinter.messagebox.askyesno", return_value=True):
            view._on_account_query("account_1")
            view._on_account_login("account_1")
            view._on_account_release_profile("account_1")

        self.assertEqual(monitor.calls, [])
        self.assertEqual(len(statuses), 3)
        self.assertTrue(all("변경 중" in text for text, _level in statuses))

    def test_delete_inflight_disables_profile_action_buttons(self) -> None:
        class _StateButton:
            def __init__(self):
                self.disabled = False

            def state(self, tokens):
                self.disabled = list(tokens) == ["disabled"]

        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        query = _StateButton()
        login = _StateButton()
        logout = _StateButton()
        view._login_button = _StateButton()
        view._logout_button = _StateButton()
        view._account_query_buttons = {"account_1": query}
        view._account_login_buttons = {"account_1": login}
        view._account_logout_buttons = {"account_1": logout}
        view._profile_deletions_inflight.add("account_1")

        view._refresh_action_buttons(
            {
                "can_login": True,
                "can_logout": True,
                "accounts": [
                    {
                        "id": "account_1",
                        "enabled": True,
                        "runtime": {"can_login": True, "can_logout": True},
                    }
                ],
            }
        )

        self.assertTrue(query.disabled)
        self.assertTrue(login.disabled)
        self.assertTrue(logout.disabled)

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
