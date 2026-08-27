import inspect
import os
import types
import unittest
from unittest.mock import patch

from src.apps.Monitor import Monitor


class _DummyKeyboard:
    def __init__(self) -> None:
        self.add_hotkey_calls: list[tuple[str, bool]] = []
        self.on_press_key_calls: list[tuple[str, bool]] = []
        self.remove_hotkey_calls: list[str] = []
        self._next_handle = 0

    def add_hotkey(self, combo, _callback, suppress=False):
        self.add_hotkey_calls.append((str(combo), bool(suppress)))
        self._next_handle += 1
        return f"hotkey#{self._next_handle}:{combo}"

    def remove_hotkey(self, handle):
        self.remove_hotkey_calls.append(str(handle))
        return None

    def on_press_key(self, key, _callback, suppress=False):
        self.on_press_key_calls.append((str(key), bool(suppress)))
        return None

    def unhook_all(self):
        return None

    def stash_state(self):
        return None


class _DummyLib:
    def __init__(self) -> None:
        self.keyboard = _DummyKeyboard()
        self.os = os
        self.time = types.SimpleNamespace(monotonic=lambda: 0.0)


class MonitorHotkeyUnitTest(unittest.TestCase):
    def test_default_hotkeys_avoid_broad_global_key_hooks(self) -> None:
        lib = _DummyLib()

        with patch.dict(os.environ, {}, clear=True):
            with patch("src.apps.Monitor.LibConnector", return_value=lib):
                with patch("src.apps.Monitor.Wrike", return_value=object()):
                    with patch("src.apps.Monitor.KakaoManager", return_value=object()):
                        with patch("src.apps.Monitor.LiJaMong", return_value=object()):
                            monitor = Monitor()
                            monitor._Monitor__background_enabled = True
                            monitor._Monitor__register_hotkeys()

        press_keys = [key for key, _suppress in lib.keyboard.on_press_key_calls]
        hotkeys = [combo for combo, _suppress in lib.keyboard.add_hotkey_calls]

        self.assertEqual([], press_keys)
        self.assertEqual(
            ["ctrl+alt+c", "ctrl+alt+k", "ctrl+alt+w", "ctrl+alt+b"],
            hotkeys,
        )
        self.assertNotIn("ctrl+c", hotkeys)
        self.assertNotIn("enter", hotkeys)

    def test_legacy_env_does_not_change_registered_hotkeys(self) -> None:
        lib = _DummyLib()

        with patch.dict(
            os.environ,
            {"WINDOWS_SUPPORTER_" + "LEGACY_KEYBOARD_HOOKS": "1"},
            clear=True,
        ):
            with patch("src.apps.Monitor.Wrike", return_value=object()):
                with patch("src.apps.Monitor.KakaoManager", return_value=object()):
                    with patch("src.apps.Monitor.LiJaMong", return_value=object()):
                        with patch(
                            "src.apps.Monitor.LibConnector",
                            return_value=lib,
                        ):
                            monitor = Monitor()
                            monitor._Monitor__background_enabled = True
                            monitor._Monitor__register_hotkeys()

        press_keys = [key for key, _suppress in lib.keyboard.on_press_key_calls]
        hotkeys = [combo for combo, _suppress in lib.keyboard.add_hotkey_calls]

        self.assertEqual([], press_keys)
        self.assertEqual(
            ["ctrl+alt+c", "ctrl+alt+k", "ctrl+alt+w", "ctrl+alt+b"],
            hotkeys,
        )
        self.assertNotIn("ctrl+c", hotkeys)
        self.assertNotIn("ctrl+d", hotkeys)
        self.assertNotIn("enter", hotkeys)
        self.assertNotIn("q", press_keys)
        self.assertNotIn("s", press_keys)
        self.assertFalse(hasattr(monitor, "_Monitor__last_ctrl_s_ts"))

    def test_foreground_profile_registers_and_removes_app_hotkeys(self) -> None:
        lib = _DummyLib()

        with patch("src.apps.Monitor.LibConnector", return_value=lib):
            monitor = Monitor()
            monitor._Monitor__background_enabled = True
            monitor._Monitor__register_hotkeys()
            monitor._Monitor__sync_foreground_hotkeys("wrike")

            hotkeys = [combo for combo, _suppress in lib.keyboard.add_hotkey_calls]
            self.assertEqual(
                ["ctrl+alt+c", "ctrl+alt+k", "ctrl+alt+w", "ctrl+alt+b", "alt+q", "ctrl+q"],
                hotkeys,
            )

            monitor._Monitor__sync_foreground_hotkeys("notion")
            hotkeys = [combo for combo, _suppress in lib.keyboard.add_hotkey_calls]
            self.assertEqual(
                [
                    "ctrl+alt+c",
                    "ctrl+alt+k",
                    "ctrl+alt+w",
                    "ctrl+alt+b",
                    "alt+q",
                    "ctrl+q",
                    "ctrl+s",
                ],
                hotkeys,
            )
            self.assertEqual(
                ["hotkey#5:alt+q", "hotkey#6:ctrl+q"],
                lib.keyboard.remove_hotkey_calls,
            )

            monitor._Monitor__sync_foreground_hotkeys(None)

        self.assertEqual(
            ["hotkey#5:alt+q", "hotkey#6:ctrl+q", "hotkey#7:ctrl+s"],
            lib.keyboard.remove_hotkey_calls,
        )

    def test_legacy_keyboard_env_contract_is_removed_from_monitor(self) -> None:
        legacy_env_name = "WINDOWS_SUPPORTER_" + "LEGACY_KEYBOARD_HOOKS"
        source = inspect.getsource(Monitor)

        self.assertNotIn(legacy_env_name, source)
        self.assertFalse(hasattr(Monitor, "_Monitor__LEGACY_HOOKS_ENV"))
        self.assertFalse(hasattr(Monitor, "_Monitor__legacy_keyboard_hooks_enabled"))


if __name__ == "__main__":
    unittest.main()
