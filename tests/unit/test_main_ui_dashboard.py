import json
import os
import queue
import tempfile
import unittest
from unittest.mock import patch

from src.apps.dashboard_ui import DashboardView
from src.apps.main_ui import WindowsSupporterMainUI
from src.apps.main_ui_state import load_last_tab, save_last_tab


class _FakeRoot:
    def __init__(self):
        self.geometry_calls = []
        self.minsize_calls = []
        self.after_calls = []
        self.after_cancel_calls = []
        self.iconify_calls = 0
        self.withdraw_calls = 0

    def deiconify(self):
        return None

    def iconify(self):
        self.iconify_calls += 1

    def withdraw(self):
        self.withdraw_calls += 1

    def lift(self):
        return None

    def focus_force(self):
        return None

    def winfo_width(self):
        return 1000

    def winfo_height(self):
        return 620

    def geometry(self, value):
        self.geometry_calls.append(str(value))

    def minsize(self, width, height):
        self.minsize_calls.append((int(width), int(height)))

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, after_id):
        self.after_cancel_calls.append(after_id)


class _FakeNotebook:
    def __init__(self, selected="tab-dashboard"):
        self._selected = str(selected)
        self.select_calls = []

    def select(self, item=None):
        if item is not None:
            self._selected = str(item)
            self.select_calls.append(str(item))
        return self._selected


class _FakeStartupManager:
    def __init__(self):
        self.enabled = True
        self.toggle_calls = 0
        self.start_calls = []
        self.rescan_calls = 0

    def get_enabled_state(self):
        return self.enabled

    def load_config(self):
        return {"instances": [{"id": "a"}, {"id": "b"}]}

    def get_instances_runtime(self, _instances):
        return {"a": (True, 1), "b": (False, None)}

    def toggle_enabled(self):
        self.toggle_calls += 1
        self.enabled = not self.enabled
        return self.enabled

    def start(self, root):
        self.start_calls.append(root)

    def rescan_defaults_merge(self):
        self.rescan_calls += 1


class _FakeCodex:
    def __init__(self):
        self.settings = {
            "enabled": True,
            "interval_sec": 10.0,
            "tooltip_duration_ms": 7000,
            "usage_url": "https://example.test",
        }
        self.update_calls = []
        self.toggle_calls = 0
        self.show_calls = []

    def get_settings_snapshot(self):
        return dict(self.settings)

    def update_settings(self, data):
        self.update_calls.append(dict(data))
        self.settings.update(data)
        return True, None

    def toggle_enabled(self):
        self.toggle_calls += 1
        self.settings["enabled"] = not bool(self.settings.get("enabled", True))
        return True, None

    def get_runtime_status(self):
        return {"monitor_state": "idle", "session_state": "logged_in"}

    def show_current_status(self, **kwargs):
        self.show_calls.append(dict(kwargs))


class _FakeKakao:
    def __init__(self):
        self.settings = {"enabled": True, "target_display_num": 2}
        self.update_calls = []
        self.overlay_calls = []

    def get_settings_snapshot(self):
        return dict(self.settings)

    def update_settings(self, data):
        self.update_calls.append(dict(data))
        self.settings.update(data)
        return True, None

    def show_monitor_overlays(self, root, duration_ms=1500, selected_display_num=None):
        self.overlay_calls.append((root, duration_ms, selected_display_num))

    def hide_monitor_overlays(self):
        return None


class _FakeWrike:
    def __init__(self):
        self.settings = {
            "api_token_configured": True,
            "daily_target_minutes": 480,
            "monitor_enabled": True,
        }
        self.update_calls = []
        self.weekly_calls = []

    def get_settings_snapshot(self):
        return dict(self.settings)

    def update_settings(self, data):
        self.update_calls.append(dict(data))
        self.settings.update(data)
        return True, None

    def show_weekly_timelog_summary(self, root):
        self.weekly_calls.append(root)


class _FakeMonitor:
    def __init__(self):
        self.codex = _FakeCodex()
        self.kakao = _FakeKakao()
        self.wrike = _FakeWrike()
        self.background_enabled = True

    def get_codex_usage_monitor(self):
        return self.codex

    def get_ai_usage_monitor(self):
        return self.codex

    def get_kakao_manager(self):
        return self.kakao

    def get_wrike(self):
        return self.wrike

    def get_dashboard_status_snapshot(self):
        return {
            "enabled": self.background_enabled,
            "hotkeys_registered": True,
            "features_warmup_done": True,
            "foreground_hotkey_profile": "wrike",
            "wrike_attached": True,
            "codex_attached": True,
            "lijamong_attached": False,
            "kakao_tick_active": True,
        }

    def set_background_enabled(self, enabled):
        self.background_enabled = bool(enabled)


class _FakeUpdater:
    def __init__(self):
        self.check_calls = []
        self.status_callback = None
        self.status = {
            "state": "update_available",
            "current_tag": "v0.5.6",
            "latest_tag": "v0.5.7",
        }

    def set_status_changed_callback(self, callback):
        self.status_callback = callback

    def get_status_snapshot(self):
        return dict(self.status)

    def check_now(self, *, manual=False):
        self.check_calls.append(bool(manual))


class _RefreshRecorder:
    def __init__(self):
        self.refresh_calls = 0

    def refresh(self):
        self.refresh_calls += 1


class MainUiDashboardUnitTest(unittest.TestCase):
    def _build_ui(self, state_path):
        root = _FakeRoot()
        startup = _FakeStartupManager()
        monitor = _FakeMonitor()
        updater = _FakeUpdater()
        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", return_value=None):
            with patch.object(WindowsSupporterMainUI, "_build_shell", return_value=None):
                ui = WindowsSupporterMainUI(
                    root=root,
                    startup_manager=startup,
                    monitor=monitor,
                    updater=updater,
                    state_path=state_path,
                )
        ui._notebook = _FakeNotebook()
        ui._tab_dashboard = "tab-dashboard"
        ui._tab_startup = "tab-startup"
        ui._tab_kakao = "tab-kakao"
        ui._tab_wrike = "tab-wrike"
        ui._tab_codex = "tab-codex"
        ui._tab_update = "tab-update"
        return ui, root, startup, monitor, updater

    def test_state_load_save_validates_tab_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            valid = ("dashboard", "startup_apps")

            self.assertEqual(load_last_tab(valid_tabs=valid, path=path), "dashboard")
            self.assertTrue(save_last_tab("startup_apps", valid_tabs=valid, path=path))
            self.assertEqual(load_last_tab(valid_tabs=valid, path=path), "startup_apps")

            with open(path, "w", encoding="utf-8") as fp:
                fp.write("{not-json")
            self.assertEqual(load_last_tab(valid_tabs=valid, path=path), "dashboard")

            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"last_tab": "missing"}, fp)
            self.assertEqual(load_last_tab(valid_tabs=valid, path=path), "dashboard")
            self.assertFalse(save_last_tab("missing", valid_tabs=valid, path=path))

    def test_state_maps_legacy_codex_usage_tab_to_primary_ai_usage_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"last_tab": "codex_usage"}, fp)

            self.assertEqual(
                load_last_tab(valid_tabs=("dashboard", "ai_usage"), path=path),
                "ai_usage",
            )
            self.assertTrue(
                save_last_tab(
                    "codex_usage",
                    valid_tabs=("dashboard", "ai_usage"),
                    path=path,
                )
            )
            with open(path, encoding="utf-8") as fp:
                self.assertEqual(json.load(fp)["last_tab"], "ai_usage")

    def test_first_show_defaults_to_dashboard_and_persists_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _, _ = self._build_ui(path)

            with patch.object(ui, "_ensure_dashboard_built") as ensure_dashboard:
                ui.show()

            self.assertEqual(ui._notebook.select_calls, ["tab-dashboard"])
            self.assertEqual(ui._current_tab, ui._TAB_DASHBOARD)
            ensure_dashboard.assert_called_once()
            self.assertEqual(load_last_tab(valid_tabs=ui._valid_tab_keys(), path=path), "dashboard")

    def test_dashboard_uses_compact_default_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _, _ = self._build_ui(path)

            self.assertEqual(ui._tab_sizes.get(ui._TAB_DASHBOARD), (1000, 480))
            self.assertEqual(ui._tab_minsizes.get(ui._TAB_DASHBOARD), (940, 480))

    def test_show_restores_persisted_valid_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            save_last_tab(
                "codex_usage",
                valid_tabs=("dashboard", "startup_apps", "kakao_monitor", "wrike", "codex_usage"),
                path=path,
            )
            ui, _, _, _, _ = self._build_ui(path)

            with patch.object(ui, "_ensure_ai_usage_built") as ensure_ai_usage:
                ui.show()

            self.assertEqual(ui._notebook.select_calls, ["tab-codex"])
            self.assertEqual(ui._current_tab, ui._TAB_AI_USAGE)
            ensure_ai_usage.assert_called_once()

    def test_hide_withdraws_main_window_without_taskbar_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, root, _, monitor, _ = self._build_ui(path)

            ui.hide()

            self.assertEqual(root.iconify_calls, 0)
            self.assertEqual(root.withdraw_calls, 1)
            self.assertEqual(monitor.kakao.overlay_calls, [])

    def test_dashboard_controls_delegate_to_existing_managers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, root, startup, monitor, updater = self._build_ui(path)
            ui._run_bg = lambda fn: fn()

            ui._dashboard_startup_toggle()
            ui._dashboard_codex_toggle_enabled()
            ui._dashboard_kakao_toggle_enabled()
            ui._dashboard_wrike_toggle_enabled()
            ui._dashboard_background_toggle_enabled()
            ui._dashboard_update_check()

            self.assertEqual(startup.toggle_calls, 1)
            self.assertEqual(startup.rescan_calls, 0)
            self.assertEqual(startup.start_calls, [root])
            self.assertEqual(monitor.codex.toggle_calls, 1)
            self.assertFalse(monitor.codex.settings["enabled"])
            self.assertEqual(monitor.codex.show_calls, [])
            self.assertEqual(monitor.kakao.update_calls[-1]["enabled"], False)
            self.assertEqual(monitor.kakao.overlay_calls, [])
            self.assertEqual(monitor.wrike.update_calls[-1]["monitor_enabled"], False)
            self.assertEqual(monitor.wrike.weekly_calls, [])
            self.assertFalse(monitor.background_enabled)
            self.assertEqual(updater.check_calls, [True])

    def test_dashboard_ai_toggle_runs_atomic_manager_mutation_in_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, monitor, _ = self._build_ui(path)
            pending = []
            ui._run_bg = lambda fn: pending.append(fn)

            ui._dashboard_ai_usage_toggle_enabled()

            self.assertEqual(len(pending), 1)
            self.assertEqual(monitor.codex.toggle_calls, 0)
            self.assertEqual(monitor.codex.update_calls, [])

            pending[0]()

            self.assertEqual(monitor.codex.toggle_calls, 1)
            self.assertFalse(monitor.codex.settings["enabled"])
            self.assertEqual(monitor.codex.update_calls, [])

    def test_dashboard_ai_toggle_flushes_pending_view_settings_then_remounts(self):
        class _PendingView:
            def __init__(self):
                self.events = []

            def _begin_external_settings_mutation(self):
                self.events.append(("begin",))
                return True, {"payload": "dirty"}

            def _apply_settings_update(self, prepared, update_ui=False):
                self.events.append(("save", prepared, update_ui))
                return True, None, False

            def _finish_external_settings_mutation(self, ok, error):
                self.events.append(("finish", ok, error))

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, monitor, _ = self._build_ui(path)
            pending_view = _PendingView()
            ui._ai_usage_view = pending_view
            ui._event_queue = queue.Queue()
            workers = []
            ui._run_bg = lambda fn: workers.append(fn)

            ui._dashboard_ai_usage_toggle_enabled()

            self.assertEqual(pending_view.events, [("begin",)])
            self.assertEqual(monitor.codex.toggle_calls, 0)
            self.assertEqual(len(workers), 1)

            workers[0]()

            self.assertEqual(
                pending_view.events[:2],
                [("begin",), ("save", {"payload": "dirty"}, False)],
            )
            self.assertEqual(monitor.codex.toggle_calls, 1)
            self.assertFalse(monitor.codex.settings["enabled"])
            self.assertEqual(ui._event_queue.qsize(), 1)

            ui._event_queue.get_nowait()()

            self.assertEqual(pending_view.events[-1], ("finish", True, None))

    def test_update_status_callback_refreshes_existing_dashboard(self):
        class FakeDashboard:
            def __init__(self):
                self.refresh_calls = 0

            def refresh(self):
                self.refresh_calls += 1

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _, updater = self._build_ui(path)
            dashboard = FakeDashboard()
            ui._dashboard_view = dashboard

            self.assertIsNotNone(updater.status_callback)

            updater.status_callback()

            self.assertEqual(dashboard.refresh_calls, 1)

    def test_dashboard_callbacks_expose_only_navigation_and_toggles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _, _ = self._build_ui(path)

            callbacks = ui._get_dashboard_callbacks()

            self.assertEqual(
                set(callbacks),
                {
                    "startup.toggle",
                    "startup.settings",
                    "ai_usage.toggle",
                    "ai_usage.settings",
                    "codex.toggle",
                    "codex.settings",
                    "kakao.toggle",
                    "kakao.settings",
                    "wrike.toggle",
                    "wrike.settings",
                    "background.toggle",
                    "update.check",
                    "update.settings",
                },
            )

    def test_update_settings_tab_is_registered_from_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _, _ = self._build_ui(path)

            callbacks = ui._get_dashboard_callbacks()
            callbacks["update.settings"]()

            self.assertEqual(ui._notebook.select_calls, ["tab-update"])
            self.assertEqual(ui._current_tab, ui._TAB_UPDATE)
            self.assertIn("update", ui._valid_tab_keys())
            self.assertEqual(load_last_tab(valid_tabs=ui._valid_tab_keys(), path=path), "update")

    def test_updater_status_callback_refreshes_dashboard_and_update_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _, updater = self._build_ui(path)
            dashboard = _RefreshRecorder()
            update_view = _RefreshRecorder()
            ui._dashboard_view = dashboard
            ui._update_view = update_view

            updater.status_callback()

            self.assertEqual(dashboard.refresh_calls, 1)
            self.assertEqual(update_view.refresh_calls, 1)

    def test_dashboard_status_snapshot_combines_safe_feature_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _, _ = self._build_ui(path)

            snapshot = ui._get_dashboard_status_snapshot()

            self.assertEqual(snapshot["startup"]["total_count"], 2)
            self.assertEqual(snapshot["startup"]["running_count"], 1)
            self.assertEqual(snapshot["ai_usage"]["monitor_state"], "idle")
            self.assertEqual(snapshot["codex"]["monitor_state"], "idle")
            self.assertIs(snapshot["ai_usage"], snapshot["codex"])
            self.assertEqual(snapshot["kakao"]["target_display_num"], 2)
            self.assertTrue(snapshot["kakao"]["tick_active"])
            self.assertTrue(snapshot["wrike"]["api_token_configured"])
            self.assertTrue(snapshot["background"]["enabled"])
            self.assertTrue(snapshot["background"]["hotkeys_registered"])
            self.assertEqual(snapshot["update"]["latest_tag"], "v0.5.7")


class DashboardViewFormattingUnitTest(unittest.TestCase):
    def test_ai_usage_callback_prefers_primary_and_falls_back_to_codex(self):
        calls = []
        view = DashboardView(
            object(),
            status_provider=lambda: {},
            callbacks={
                "ai_usage.toggle": lambda: calls.append("primary"),
                "codex.toggle": lambda: calls.append("legacy"),
            },
        )
        view._schedule_refresh = lambda: None

        view._invoke("ai_usage.toggle")
        self.assertEqual(calls, ["primary"])

        calls.clear()
        view._callbacks.pop("ai_usage.toggle")
        view._invoke("ai_usage.toggle")
        self.assertEqual(calls, ["legacy"])

    def test_ai_usage_status_prefers_primary_key_and_falls_back_to_codex(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})
        captured = []
        view._set_feature_status = lambda key, value: captured.append((key, value))
        view._format_ai_usage = lambda data: (True, [(str(data["source"]), "normal")])
        view._format_startup = lambda _data: (False, [])
        view._format_kakao = lambda _data: (False, [])
        view._format_wrike = lambda _data: (False, [])
        view._format_background = lambda _data: (False, [])
        view._format_update = lambda _data: (False, [])

        view._status_provider = lambda: {
            "ai_usage": {"source": "primary"},
            "codex": {"source": "legacy"},
        }
        view.refresh()
        self.assertIn(("ai_usage", (True, [("primary", "normal")])), captured)

        captured.clear()
        view._status_provider = lambda: {"codex": {"source": "legacy"}}
        view.refresh()
        self.assertIn(("ai_usage", (True, [("legacy", "normal")])), captured)

    def test_status_formatters_put_enabled_state_first(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        formatted = [
            view._format_startup({"enabled": True, "total_count": 3, "running_count": 2}),
            view._format_codex(
                {"enabled": True, "monitor_state": "idle", "session_state": "logged_in"}
            ),
            view._format_wrike(
                {
                    "api_token_configured": True,
                    "daily_target_minutes": 480,
                    "monitor_enabled": True,
                }
            ),
            view._format_kakao(
                {"enabled": False, "tick_active": True, "target_display_num": 2}
            ),
            view._format_background(
                {
                    "enabled": True,
                    "hotkeys_registered": True,
                    "features_warmup_done": True,
                    "foreground_hotkey_profile": "wrike",
                    "wrike_attached": True,
                    "codex_attached": True,
                }
            ),
        ]

        self.assertTrue(all(parts[0][0] in {"활성화", "비활성화"} for _, parts in formatted))
        self.assertTrue(all(parts[0][1] in {"enabled", "disabled"} for _, parts in formatted))

    def test_background_formatter_deduplicates_ai_usage_legacy_alias(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        _, parts = view._format_background(
            {
                "enabled": True,
                "ai_usage_attached": True,
                "codex_attached": True,
            }
        )

        attached_text = next(text for text, _style in parts if text.startswith("연결된 기능:"))
        self.assertEqual(attached_text, "연결된 기능: AI 사용량")

    def test_minutes_are_displayed_as_hours_and_minutes(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        self.assertEqual(view._format_minutes(480), "8시간")
        self.assertEqual(view._format_minutes(490), "8시간 10분")
        self.assertEqual(view._format_minutes(30), "30분")

    def test_codex_formatter_summarizes_two_accounts(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        _, parts = view._format_codex(
            {
                "enabled": True,
                "monitor_state": "mixed",
                "session_state": "mixed",
                "accounts": [
                    {
                        "label": "Codex 1",
                        "enabled": True,
                        "runtime": {"monitor_state": "idle", "session_state": "logged_in"},
                    },
                    {
                        "label": "Codex 2",
                        "enabled": False,
                        "runtime": {"monitor_state": "idle", "session_state": "logged_out"},
                    },
                ],
            }
        )

        labels = [text for text, _style in parts]
        self.assertIn("Codex 1 (Codex): logged_in / idle", labels)
        self.assertIn("Codex 2 (Codex): 비활성 / logged_out", labels)

    def test_ai_usage_formatter_prefers_the_two_taskbar_selected_profiles(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        _, parts = view._format_ai_usage(
            {
                "enabled": True,
                "profiles": [
                    {"id": "first", "label": "First", "taskbar_selected": False},
                    {"id": "second", "label": "Second", "taskbar_selected": True},
                    {"id": "third", "label": "Third", "taskbar_selected": True},
                    {"id": "fourth", "label": "Fourth", "taskbar_selected": False},
                ],
            }
        )

        labels = [text for text, _style in parts]
        self.assertTrue(any(text.startswith("Second (Codex):") for text in labels))
        self.assertTrue(any(text.startswith("Third (Codex):") for text in labels))
        self.assertFalse(any(text.startswith("First (Codex):") for text in labels))
        self.assertFalse(any(text.startswith("Fourth (Codex):") for text in labels))

    def test_update_formatter_shows_available_version(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        enabled, parts = view._format_update(
            {"state": "update_available", "current_tag": "v0.5.6", "latest_tag": "v0.5.7"}
        )

        self.assertTrue(enabled)
        self.assertEqual(parts[0], ("업데이트 가능", "enabled"))
        self.assertIn(("v0.5.6 -> v0.5.7", "normal"), parts)

    def test_update_formatter_shows_progress_label_and_percent(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        enabled, parts = view._format_update(
            {
                "state": "updating",
                "progress": {
                    "label": "빌드 실행 중",
                    "detail": "build.bat를 실행합니다.",
                    "percent": 74,
                },
            }
        )

        self.assertTrue(enabled)
        self.assertEqual(parts[0], ("업데이트 중", "enabled"))
        self.assertIn(("빌드 실행 중", "normal"), parts)
        self.assertIn(("74%", "normal"), parts)
        self.assertIn(("build.bat를 실행합니다.", "normal"), parts)

    def test_update_formatter_shows_git_gui_close_prompt_detail(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        enabled, parts = view._format_update(
            {
                "state": "updating",
                "progress": {
                    "label": "업데이트 사전 점검 중",
                    "detail": "Fork.exe가 실행 중입니다. 종료 승인 대기 중입니다.",
                    "percent": 28,
                },
            }
        )

        self.assertTrue(enabled)
        self.assertIn(("업데이트 사전 점검 중", "normal"), parts)
        self.assertIn(("28%", "normal"), parts)
        self.assertIn(("Fork.exe가 실행 중입니다. 종료 승인 대기 중입니다.", "normal"), parts)

    def test_update_formatter_shows_failure_step_and_detail(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        enabled, parts = view._format_update(
            {
                "state": "error",
                "progress": {
                    "label": "업데이트 실패",
                    "detail": "로그를 확인해 주세요.",
                    "failed_step": "build.bat 실행",
                },
            }
        )

        self.assertFalse(enabled)
        self.assertEqual(parts[0], ("확인 실패", "disabled"))
        self.assertIn(("실패 단계: build.bat 실행", "normal"), parts)
        self.assertIn(("로그를 확인해 주세요.", "normal"), parts)

    def test_update_formatter_shows_cancelled_force_clean(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        enabled, parts = view._format_update(
            {
                "state": "cancelled",
                "last_error": "강제정리가 취소되어 업데이트를 중단했습니다.",
            }
        )

        self.assertFalse(enabled)
        self.assertEqual(parts[0], ("취소됨", "disabled"))
        self.assertIn(("강제정리가 취소되어 업데이트를 중단했습니다.", "normal"), parts)

    def test_update_formatter_shows_git_checkout_requirement(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        enabled, parts = view._format_update({"state": "unavailable"})

        self.assertFalse(enabled)
        self.assertEqual(parts[0], ("지원 안 됨", "disabled"))
        self.assertIn(("Git checkout 필요", "normal"), parts)


if __name__ == "__main__":
    unittest.main()
