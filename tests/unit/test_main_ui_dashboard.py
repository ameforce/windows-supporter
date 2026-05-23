import json
import os
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

    def deiconify(self):
        return None

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
        self.show_calls = []

    def get_settings_snapshot(self):
        return dict(self.settings)

    def update_settings(self, data):
        self.update_calls.append(dict(data))
        self.settings.update(data)
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
        return self.background_enabled


class MainUiDashboardUnitTest(unittest.TestCase):
    def _build_ui(self, state_path):
        root = _FakeRoot()
        startup = _FakeStartupManager()
        monitor = _FakeMonitor()
        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", return_value=None):
            with patch.object(WindowsSupporterMainUI, "_build_shell", return_value=None):
                ui = WindowsSupporterMainUI(
                    root=root,
                    startup_manager=startup,
                    monitor=monitor,
                    state_path=state_path,
                )
        ui._notebook = _FakeNotebook()
        ui._tab_dashboard = "tab-dashboard"
        ui._tab_startup = "tab-startup"
        ui._tab_kakao = "tab-kakao"
        ui._tab_wrike = "tab-wrike"
        ui._tab_codex = "tab-codex"
        return ui, root, startup, monitor

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

    def test_first_show_defaults_to_dashboard_and_persists_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _ = self._build_ui(path)

            with patch.object(ui, "_ensure_dashboard_built") as ensure_dashboard:
                ui.show()

            self.assertEqual(ui._notebook.select_calls, ["tab-dashboard"])
            self.assertEqual(ui._current_tab, ui._TAB_DASHBOARD)
            ensure_dashboard.assert_called_once()
            self.assertEqual(load_last_tab(valid_tabs=ui._valid_tab_keys(), path=path), "dashboard")

    def test_dashboard_uses_compact_default_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _ = self._build_ui(path)

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
            ui, _, _, _ = self._build_ui(path)

            with patch.object(ui, "_ensure_codex_built") as ensure_codex:
                ui.show()

            self.assertEqual(ui._notebook.select_calls, ["tab-codex"])
            self.assertEqual(ui._current_tab, ui._TAB_CODEX)
            ensure_codex.assert_called_once()

    def test_dashboard_controls_delegate_to_existing_managers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, root, startup, monitor = self._build_ui(path)
            ui._run_bg = lambda fn: fn()

            ui._dashboard_startup_toggle()
            ui._dashboard_codex_toggle_enabled()
            ui._dashboard_kakao_toggle_enabled()
            ui._dashboard_wrike_toggle_enabled()
            ui._dashboard_background_toggle_enabled()

            self.assertEqual(startup.toggle_calls, 1)
            self.assertEqual(startup.rescan_calls, 0)
            self.assertEqual(startup.start_calls, [root])
            self.assertEqual(monitor.codex.update_calls[-1]["enabled"], False)
            self.assertEqual(monitor.codex.show_calls, [])
            self.assertEqual(monitor.kakao.update_calls[-1]["enabled"], False)
            self.assertEqual(monitor.kakao.overlay_calls, [])
            self.assertEqual(monitor.wrike.update_calls[-1]["monitor_enabled"], False)
            self.assertEqual(monitor.wrike.weekly_calls, [])
            self.assertFalse(monitor.background_enabled)

    def test_dashboard_callbacks_expose_only_navigation_and_toggles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _ = self._build_ui(path)

            callbacks = ui._get_dashboard_callbacks()

            self.assertEqual(
                set(callbacks),
                {
                    "startup.toggle",
                    "startup.settings",
                    "codex.toggle",
                    "codex.settings",
                    "kakao.toggle",
                    "kakao.settings",
                    "wrike.toggle",
                    "wrike.settings",
                    "background.toggle",
                },
            )

    def test_dashboard_status_snapshot_combines_safe_feature_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main_ui_state.json")
            ui, _, _, _ = self._build_ui(path)

            snapshot = ui._get_dashboard_status_snapshot()

            self.assertEqual(snapshot["startup"]["total_count"], 2)
            self.assertEqual(snapshot["startup"]["running_count"], 1)
            self.assertEqual(snapshot["codex"]["monitor_state"], "idle")
            self.assertEqual(snapshot["kakao"]["target_display_num"], 2)
            self.assertTrue(snapshot["kakao"]["tick_active"])
            self.assertTrue(snapshot["wrike"]["api_token_configured"])
            self.assertTrue(snapshot["background"]["enabled"])
            self.assertTrue(snapshot["background"]["hotkeys_registered"])


class DashboardViewFormattingUnitTest(unittest.TestCase):
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

    def test_minutes_are_displayed_as_hours_and_minutes(self):
        view = DashboardView(object(), status_provider=lambda: {}, callbacks={})

        self.assertEqual(view._format_minutes(480), "8시간")
        self.assertEqual(view._format_minutes(490), "8시간 10분")
        self.assertEqual(view._format_minutes(30), "30분")


if __name__ == "__main__":
    unittest.main()
