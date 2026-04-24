import os
import types
import unittest
from unittest.mock import patch

from src.apps.Monitor import Monitor


class _DummyKeyboard:
    def __init__(self) -> None:
        self.add_hotkey_calls: list[tuple[str, bool]] = []
        self.on_press_key_calls: list[tuple[str, bool]] = []

    def add_hotkey(self, combo, _callback, suppress=False):
        self.add_hotkey_calls.append((str(combo), bool(suppress)))
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
                with patch("src.apps.Monitor.OneNote", return_value=object()):
                    with patch("src.apps.Monitor.Notion", return_value=object()):
                        with patch("src.apps.Monitor.Skype", return_value=object()):
                            with patch("src.apps.Monitor.Wrike", return_value=object()):
                                with patch("src.apps.Monitor.KakaoManager", return_value=object()):
                                    with patch("src.apps.Monitor.LiJaMong", return_value=object()):
                                        monitor = Monitor()
                                        monitor._Monitor__register_hotkeys()

        press_keys = [key for key, _suppress in lib.keyboard.on_press_key_calls]
        hotkeys = [combo for combo, _suppress in lib.keyboard.add_hotkey_calls]

        self.assertEqual([], press_keys)
        self.assertEqual(
            ["ctrl+alt+c", "ctrl+alt+k", "ctrl+alt+w"],
            hotkeys,
        )
        self.assertNotIn("ctrl+c", hotkeys)
        self.assertNotIn("enter", hotkeys)

    def test_legacy_env_keeps_previous_broad_hooks_available(self) -> None:
        lib = _DummyLib()

        with patch.dict(
            os.environ,
            {"WINDOWS_SUPPORTER_LEGACY_KEYBOARD_HOOKS": "1"},
            clear=True,
        ):
            with patch("src.apps.Monitor.OneNote", return_value=object()):
                with patch("src.apps.Monitor.Notion", return_value=object()):
                    with patch("src.apps.Monitor.Skype", return_value=object()):
                        with patch("src.apps.Monitor.Wrike", return_value=object()):
                            with patch("src.apps.Monitor.KakaoManager", return_value=object()):
                                with patch("src.apps.Monitor.LiJaMong", return_value=object()):
                                    with patch(
                                        "src.apps.Monitor.LibConnector",
                                        return_value=lib,
                                    ):
                                        monitor = Monitor()
                                        monitor._Monitor__register_hotkeys()

        press_keys = [key for key, _suppress in lib.keyboard.on_press_key_calls]
        hotkeys = [combo for combo, _suppress in lib.keyboard.add_hotkey_calls]

        self.assertIn("ctrl+alt+c", hotkeys)
        self.assertIn("ctrl+c", hotkeys)
        self.assertIn("enter", hotkeys)
        self.assertIn("q", press_keys)
        self.assertIn("s", press_keys)
        self.assertNotIn("v", press_keys)
        self.assertNotIn("ctrl+v", hotkeys)
        self.assertFalse(hasattr(monitor, "_Monitor__last_ctrl_v_ts"))


if __name__ == "__main__":
    unittest.main()
