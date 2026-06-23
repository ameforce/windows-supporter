from __future__ import annotations

import unittest

from src.apps.update_settings_ui import UpdateSettingsView


class _FakeVar:
    def __init__(self, value) -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, **kwargs) -> None:
        if "text" in kwargs:
            self.text = str(kwargs["text"])


class _FakeUpdater:
    def __init__(self) -> None:
        self.update_calls = []
        self.settings = {
            "auto_check_enabled": True,
            "check_interval_minutes": 10,
            "auto_update_available": True,
            "unavailable_reason": "",
        }
        self.status = {"state": "idle"}

    def get_settings_snapshot(self):
        return dict(self.settings)

    def get_status_snapshot(self):
        return dict(self.status)

    def update_settings(self, data):
        self.update_calls.append(dict(data))
        self.settings.update(data)
        return True, None


class UpdateSettingsViewUnitTest(unittest.TestCase):
    def test_save_settings_rejects_interval_below_supported_range(self) -> None:
        updater = _FakeUpdater()
        view = UpdateSettingsView(root=object(), updater=updater)
        view._enabled_var = _FakeVar(True)
        view._interval_var = _FakeVar("1")
        view._status_label = _FakeLabel()

        self.assertFalse(view._save_settings())

        self.assertEqual(updater.update_calls, [])
        self.assertIn("3분 이상", view._status_label.text)

    def test_refresh_status_reflects_latest_updater_state(self) -> None:
        updater = _FakeUpdater()
        updater.settings["check_interval_minutes"] = 15
        updater.status["state"] = "checking"
        view = UpdateSettingsView(root=object(), updater=updater)
        view._status_label = _FakeLabel()

        view.refresh()

        self.assertIn("주기: 15분", view._status_label.text)
        self.assertIn("상태: checking", view._status_label.text)


if __name__ == "__main__":
    unittest.main()
