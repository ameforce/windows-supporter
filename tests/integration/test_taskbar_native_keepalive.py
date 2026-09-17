import tkinter as tk
import unittest

import win32con
import win32gui

from src.apps.codex_usage_taskbar_overlay import CodexUsageTaskbarOverlay, _get_window_handle


class TaskbarNativeKeepaliveTest(unittest.TestCase):
    def test_keepalive_restores_externally_hidden_hwnd_without_geometry_tick(self):
        root = tk.Tk()
        root.withdraw()
        runtime = {"enabled": True, "profiles": [{
            "id": "fixture", "label": "Fixture", "enabled": True,
            "taskbar_selected": True, "last_snapshot": {"weekly_limit": "20%"},
        }]}
        overlay = CodexUsageTaskbarOverlay(
            root, lambda: runtime,
            work_area_getter=lambda: (0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 48),
            occupied_span_getter=lambda *args: [],
            fullscreen_detector=lambda window: False,
        )
        try:
            self.assertTrue(overlay.refresh())
            root.update()
            window = overlay._window
            hwnd = _get_window_handle(window)
            rect = win32gui.GetWindowRect(hwnd)
            self.assertTrue(win32gui.IsWindowVisible(hwnd))
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            self.assertFalse(win32gui.IsWindowVisible(hwnd))
            self.assertTrue(overlay._window_visible)
            # No root.update or geometry tick may mask the keepalive result.
            overlay._keepalive_tick()
            self.assertTrue(win32gui.IsWindowVisible(hwnd))
            self.assertEqual(_get_window_handle(window), hwnd)
            self.assertEqual(win32gui.GetWindowRect(hwnd), rect)
            self.assertIsNotNone(overlay._keepalive_after_id)
        finally:
            overlay.hide()
            owner_hwnd = overlay._native_owner_hwnd
            try:
                root.destroy()
            finally:
                if owner_hwnd and win32gui.IsWindow(owner_hwnd):
                    win32gui.DestroyWindow(owner_hwnd)
