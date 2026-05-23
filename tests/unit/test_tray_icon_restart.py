import unittest
from unittest.mock import patch

from src.utils.tray_icon import SystemTrayIcon


class SystemTrayIconRestartUnitTest(unittest.TestCase):
    def _build_tray(self, *, on_restart=None) -> SystemTrayIcon:
        return SystemTrayIcon(
            tooltip="Windows Supporter",
            on_open_settings=lambda: None,
            on_exit=lambda: None,
            on_restart=on_restart,
        )

    def test_restart_menu_is_shown_before_exit_when_callback_exists(self) -> None:
        tray = self._build_tray(on_restart=lambda: None)
        tray._hwnd = 100
        appended: list[tuple[int, int, str]] = []

        def append_menu(menu, flags, item_id, text):
            appended.append((int(flags), int(item_id), str(text)))

        with patch("src.utils.tray_icon.win32gui.CreatePopupMenu", return_value=1):
            with patch("src.utils.tray_icon.win32gui.AppendMenu", side_effect=append_menu):
                with patch("src.utils.tray_icon.win32gui.SetForegroundWindow"):
                    with patch("src.utils.tray_icon.win32gui.GetCursorPos", return_value=(0, 0)):
                        with patch("src.utils.tray_icon.win32gui.TrackPopupMenu"):
                            with patch("src.utils.tray_icon.win32gui.PostMessage"):
                                with patch("src.utils.tray_icon.win32gui.DestroyMenu"):
                                    tray._show_menu()

        menu_ids = [item_id for _, item_id, _ in appended]
        self.assertIn(SystemTrayIcon._MENU_RESTART, menu_ids)
        self.assertIn(SystemTrayIcon._MENU_EXIT, menu_ids)
        self.assertLess(
            menu_ids.index(SystemTrayIcon._MENU_RESTART),
            menu_ids.index(SystemTrayIcon._MENU_EXIT),
        )
        restart_text = [
            text for _, item_id, text in appended if item_id == SystemTrayIcon._MENU_RESTART
        ]
        self.assertEqual(restart_text, ["재시작"])

    def test_restart_command_calls_restart_and_destroys_window(self) -> None:
        calls: list[str] = []
        tray = self._build_tray(on_restart=lambda: calls.append("restart"))

        with patch("src.utils.tray_icon.win32gui.DestroyWindow") as destroy_window:
            result = tray._on_command(200, 0, SystemTrayIcon._MENU_RESTART, 0)

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["restart"])
        destroy_window.assert_called_once_with(200)


if __name__ == "__main__":
    unittest.main()
