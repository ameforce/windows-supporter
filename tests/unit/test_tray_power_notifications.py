from __future__ import annotations

import ctypes
import unittest
from unittest.mock import patch

from src.utils import tray_icon
from src.utils.tray_icon import SystemTrayIcon
from src.utils.windows_power import (
    GUID,
    GUID_ACDC_POWER_SOURCE,
    GUID_LIDSWITCH_STATE_CHANGE,
    PowerNotificationRegistration,
)


class _FakePowerNotificationRegistration:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.registered_hwnd = None
        self.unregister_count = 0
        self.messages: list[int] = []

    def register(self, hwnd: int) -> None:
        self.registered_hwnd = int(hwnd)

    def unregister(self) -> None:
        self.unregister_count += 1

    def handle_message(self, lparam: int) -> bool:
        self.messages.append(int(lparam))
        return True


class TrayPowerNotificationTest(unittest.TestCase):
    def _tray(self, callback):
        return SystemTrayIcon(
            tooltip="test",
            on_open_settings=lambda: None,
            on_exit=lambda: None,
            on_power_setting_change=callback,
            power_notifications_enabled=True,
        )

    def test_hidden_window_registers_and_destroy_unregisters_exactly_once(self) -> None:
        holder = {}

        def factory(callback):
            registration = _FakePowerNotificationRegistration(callback)
            holder["registration"] = registration
            return registration

        tray = self._tray(lambda _kind, _value: None)
        tray._hwnd = 77
        with patch.object(tray_icon, "PowerNotificationRegistration", factory):
            tray._register_power_notifications()
            tray._unregister_power_notifications()
            tray._unregister_power_notifications()
        registration = holder["registration"]
        self.assertEqual(77, registration.registered_hwnd)
        self.assertEqual(1, registration.unregister_count)

    def test_power_setting_change_is_forwarded_without_rdp_dependency(self) -> None:
        holder = {}

        def factory(callback):
            registration = _FakePowerNotificationRegistration(callback)
            holder["registration"] = registration
            return registration

        tray = self._tray(lambda _kind, _value: None)
        tray._hwnd = 88
        with patch.object(tray_icon, "PowerNotificationRegistration", factory):
            tray._register_power_notifications()
            result = tray._on_power_broadcast(
                88,
                tray_icon._WM_POWERBROADCAST,
                tray_icon._PBT_POWERSETTINGCHANGE,
                ctypes.c_void_p(123).value,
            )
        self.assertEqual(1, result)
        self.assertEqual([123], holder["registration"].messages)

    def test_remote_disconnect_never_reaches_power_policy_callback(self) -> None:
        power_events: list[tuple[str, object]] = []
        display_events: list[str] = []
        tray = SystemTrayIcon(
            tooltip="test",
            on_open_settings=lambda: None,
            on_exit=lambda: None,
            on_power_setting_change=lambda kind, value: power_events.append(
                (kind, value)
            ),
            on_display_topology_change=lambda reason: display_events.append(reason),
            power_notifications_enabled=True,
        )
        tray._on_session_change(
            1,
            tray_icon._WM_WTSSESSION_CHANGE,
            tray_icon._WTS_REMOTE_DISCONNECT,
            0,
        )
        self.assertEqual([], power_events)
        self.assertEqual(["remote_disconnect"], display_events)


class _FakeUser32:
    def __init__(self) -> None:
        self.registered: list[tuple[int, object]] = []
        self.unregistered: list[int] = []

    def RegisterPowerSettingNotification(self, hwnd, guid_pointer, _flags):
        guid = guid_pointer._obj.to_uuid()
        self.registered.append((int(hwnd.value), guid))
        return 100 + len(self.registered)

    def UnregisterPowerSettingNotification(self, handle):
        self.unregistered.append(int(handle.value))
        return 1


class PowerNotificationRegistrationTest(unittest.TestCase):
    def test_registers_required_guids_and_unregisters_every_handle_once(self) -> None:
        registration = PowerNotificationRegistration(lambda _kind, _value: None)
        fake = _FakeUser32()
        registration._user32 = fake
        registration.register(55)
        guids = [guid for _hwnd, guid in fake.registered]
        self.assertEqual(4, len(guids))
        self.assertIn(GUID_LIDSWITCH_STATE_CHANGE, guids)
        self.assertIn(GUID_ACDC_POWER_SOURCE, guids)
        registration.unregister()
        registration.unregister()
        self.assertEqual([104, 103, 102, 101], fake.unregistered)

    def test_parses_power_setting_payload(self) -> None:
        seen: list[tuple[str, object]] = []
        registration = PowerNotificationRegistration(
            lambda kind, value: seen.append((kind, value))
        )
        guid = GUID.from_uuid(GUID_LIDSWITCH_STATE_CHANGE)
        data = (0).to_bytes(4, "little")
        payload = bytes(guid) + len(data).to_bytes(4, "little") + data
        buffer = ctypes.create_string_buffer(payload)
        self.assertTrue(registration.handle_message(ctypes.addressof(buffer)))
        self.assertEqual([("lid", 0)], seen)


if __name__ == "__main__":
    unittest.main()
