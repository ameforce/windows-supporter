import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.apps.dashboard_ui import DashboardView
from src.apps.main_ui import WindowsSupporterMainUI
from src.apps.wrike_ui import WrikeSettingsView


class _Var:
    def __init__(self, value):
        self.value = value
        self.callbacks = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for callback in self.callbacks:
            callback()

    def trace_add(self, _mode, callback):
        self.callbacks.append(callback)


class _Backend:
    def __init__(self):
        self.settings = {"monitor_enabled": True, "daily_target_minutes": 480}
        self.updates = []
        self.fail = False

    def get_settings_snapshot(self):
        return dict(self.settings)

    def update_settings(self, payload):
        self.updates.append(dict(payload))
        if self.fail:
            return False, "settings save failed"
        self.settings.update(payload)
        return True, None


class WrikeSettingsToggleTest(unittest.TestCase):
    def setUp(self):
        self.backend = _Backend()
        self.view = WrikeSettingsView(None, self.backend)
        for name, value in {
            "monitor_enabled": True, "token": "", "daily": "8",
            "tooltip": "2", "monitor_interval": "5",
        }.items():
            setattr(self.view, f"_{name}_var", _Var(value))
        self.view._set_status = Mock()
        self.ui = object.__new__(WindowsSupporterMainUI)
        self.ui._monitor = SimpleNamespace(get_wrike=lambda: self.backend)
        self.ui._wrike_view = self.view
        self.ui._refresh_dashboard_status = Mock()

    def test_dashboard_only_changes_enable_flag_and_syncs_open_form(self):
        self.view._daily_var.set("unfinished draft")
        self.view._autosave_after_id = "pending-other-fields"
        self.ui._dashboard_wrike_toggle_enabled()
        self.assertEqual(self.backend.updates, [{"monitor_enabled": False}])
        self.assertFalse(self.view._monitor_enabled_var.get())
        self.assertEqual(self.view._daily_var.get(), "unfinished draft")
        self.assertEqual(self.view._autosave_after_id, "pending-other-fields")
        self.ui._refresh_dashboard_status.assert_called_once_with()

    def test_dashboard_save_failure_keeps_state_and_reports_error(self):
        self.backend.fail = True
        self.ui._dashboard_wrike_toggle_enabled()
        self.assertTrue(self.backend.settings["monitor_enabled"])
        self.assertTrue(self.view._monitor_enabled_var.get())
        status = self.ui._get_wrike_dashboard_status()
        self.assertTrue(status.get("settings_error"))
        enabled, parts = DashboardView(
            None, status_provider=lambda: {}, callbacks={}
        )._format_wrike(status)
        self.assertTrue(enabled)
        self.assertTrue(any("저장" in text and "실패" in text for text, _kind in parts))

    def test_checkbox_saves_immediately_despite_invalid_unrelated_draft(self):
        self.view._daily_var.set("not a number")
        self.view._register_autosave_traces()
        self.view._monitor_enabled_var.set(False)
        self.assertEqual(self.backend.updates, [{"monitor_enabled": False}])
        self.assertFalse(self.backend.settings["monitor_enabled"])

    def test_checkbox_failure_restores_committed_value_without_recursion(self):
        self.backend.fail = True
        self.view._register_autosave_traces()
        self.view._monitor_enabled_var.set(False)
        self.assertTrue(self.view._monitor_enabled_var.get())
        self.assertEqual(self.backend.updates, [{"monitor_enabled": False}])
        self.assertEqual(self.view._set_status.call_args.kwargs["level"], "error")

    def test_pending_form_save_cannot_overwrite_external_disable(self):
        self.backend.settings["monitor_enabled"] = False
        self.assertTrue(self.view._monitor_enabled_var.get())
        self.view._save_settings()
        self.assertNotIn("monitor_enabled", self.backend.updates[-1])
        self.assertFalse(self.backend.settings["monitor_enabled"])

    def test_loading_checkbox_does_not_save(self):
        self.view._register_autosave_traces()
        self.view._loading_settings = True
        self.view._monitor_enabled_var.set(False)
        self.assertEqual(self.backend.updates, [])

    def test_dashboard_post_save_read_failure_does_not_restore_old_snapshot(self):
        original = self.backend.get_settings_snapshot
        calls = []

        def snapshot():
            calls.append(None)
            if len(calls) == 2:
                raise OSError("read unavailable")
            return original()

        self.backend.get_settings_snapshot = snapshot
        self.ui._dashboard_wrike_toggle_enabled()
        self.assertFalse(self.backend.settings["monitor_enabled"])
        self.assertFalse(self.view._monitor_enabled_var.get())
        self.assertIn("확인", self.ui._get_wrike_dashboard_status()["settings_error"])

    def test_settings_success_clears_prior_dashboard_save_error(self):
        self.view._on_monitor_enabled_changed_callback = (
            self.ui._on_wrike_monitor_enabled_changed
        )
        self.backend.fail = True
        self.ui._dashboard_wrike_toggle_enabled()
        self.backend.fail = False
        self.view._register_autosave_traces()
        self.view._monitor_enabled_var.set(False)
        self.assertFalse(self.ui._get_wrike_dashboard_status()["settings_error"])


if __name__ == "__main__":
    unittest.main()
