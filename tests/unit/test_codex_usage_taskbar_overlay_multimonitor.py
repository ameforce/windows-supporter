import unittest
from unittest.mock import patch

import src.apps.codex_usage_taskbar_overlay as taskbar_overlay
from src.apps.codex_usage_taskbar_overlay import CodexUsageTaskbarOverlay


class _Root:
    def __init__(self):
        self.after_calls = []

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def after(self, delay_ms, callback):
        self.after_calls.append((int(delay_ms), callback))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, _after_id):
        return None

    def update_idletasks(self):
        return None


class _Window:
    def __init__(self):
        self.geometry_calls = []
        self.draw_calls = []
        self.withdraw_calls = 0
        self.deiconify_calls = 0
        self.lift_calls = 0

    def winfo_id(self):
        return 200

    def geometry(self, value):
        self.geometry_calls.append(str(value))

    def draw_model(self, model):
        self.draw_calls.append(dict(model))

    def withdraw(self):
        self.withdraw_calls += 1

    def deiconify(self):
        self.deiconify_calls += 1

    def lift(self):
        self.lift_calls += 1


class _FakeUser32:
    def GetAncestor(self, hwnd, _flag):
        return int(hwnd)

    def SetWindowLongPtrW(self, *_args):
        return 0


class _FakeWindll:
    user32 = _FakeUser32()


class _FakeWin32Con:
    GWL_EXSTYLE = -20
    HWND_TOPMOST = -1
    MONITOR_DEFAULTTONEAREST = 2
    MONITORINFOF_PRIMARY = 1
    SWP_NOACTIVATE = 0x0010
    SWP_NOOWNERZORDER = 0x0200
    SWP_SHOWWINDOW = 0x0040
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080


class _Desktop:
    def __init__(self):
        self.primary_fullscreen = True
        self.monitor_infos = {
            1: {
                "Device": r"\\.\DISPLAY1",
                "Flags": _FakeWin32Con.MONITORINFOF_PRIMARY,
                "Monitor": (0, 0, 1920, 1080),
                "Work": (0, 0, 1920, 1040),
            },
            2: {
                "Device": r"\\.\DISPLAY2",
                "Flags": 0,
                "Monitor": (1920, 0, 3840, 1080),
                "Work": (1920, 0, 3840, 1040),
            },
        }
        self.taskbars = {
            10: ("Shell_TrayWnd", (0, 1040, 1920, 1080), True),
            20: ("Shell_SecondaryTrayWnd", (1920, 1040, 3840, 1080), True),
        }

    def add_secondary(self, handle, *, work, taskbar_rect, visible=True):
        left = int(taskbar_rect[0])
        self.monitor_infos[int(handle)] = {
            "Device": rf"\\.\DISPLAY{handle}",
            "Flags": 0,
            "Monitor": (left, 0, left + 1920, 1080),
            "Work": tuple(work),
        }
        self.taskbars[int(handle) * 10] = (
            "Shell_SecondaryTrayWnd",
            tuple(taskbar_rect),
            bool(visible),
        )

    def monitor_for_rect(self, rect):
        left, _top, right, _bottom = [int(v) for v in rect]
        center_x = (left + right) // 2
        for handle, info in self.monitor_infos.items():
            m_left, _m_top, m_right, _m_bottom = info["Monitor"]
            if m_left <= center_x < m_right:
                return handle
        return 1

    def monitor_for_window(self, hwnd):
        if int(hwnd) == 100:
            return 1
        if int(hwnd) in self.taskbars:
            return self.monitor_for_rect(self.taskbars[int(hwnd)][1])
        return 1


class _FakeWin32Api:
    def __init__(self, desktop):
        self._desktop = desktop

    def EnumDisplayMonitors(self, _dc, _clip):
        return [(handle, None, None) for handle in self._desktop.monitor_infos]

    def GetMonitorInfo(self, monitor):
        return self._desktop.monitor_infos[int(monitor)]

    def MonitorFromRect(self, rect, _default):
        return self._desktop.monitor_for_rect(rect)

    def MonitorFromWindow(self, hwnd, _default):
        return self._desktop.monitor_for_window(hwnd)


class _FakeWin32Gui:
    def __init__(self, desktop):
        self._desktop = desktop

    def GetForegroundWindow(self):
        return 100 if self._desktop.primary_fullscreen else 0

    def FindWindow(self, class_name, _title):
        for hwnd, (name, _rect, visible) in self._desktop.taskbars.items():
            if name == class_name and visible:
                return hwnd
        return 0

    def EnumWindows(self, callback, extra):
        windows = dict(self._desktop.taskbars)
        if self._desktop.primary_fullscreen:
            windows[100] = ("GameWindow", (0, 0, 1920, 1080), True)
        for hwnd in windows:
            if not callback(hwnd, extra):
                break

    def EnumChildWindows(self, _hwnd, _callback, _extra):
        return None

    def GetAncestor(self, hwnd, _flag):
        return int(hwnd)

    def GetClassName(self, hwnd):
        if int(hwnd) == 100:
            return "GameWindow"
        return self._desktop.taskbars[int(hwnd)][0]

    def GetWindowText(self, _hwnd):
        return ""

    def IsWindowVisible(self, hwnd):
        if int(hwnd) == 100:
            return self._desktop.primary_fullscreen
        return self._desktop.taskbars[int(hwnd)][2]

    def IsIconic(self, _hwnd):
        return False

    def GetWindowRect(self, hwnd):
        if int(hwnd) == 100:
            return (0, 0, 1920, 1080)
        return self._desktop.taskbars[int(hwnd)][1]

    def WindowFromPoint(self, point):
        x, _y = point
        if self._desktop.primary_fullscreen and 0 <= int(x) < 1920:
            return 100
        return 0


def _runtime():
    return {
        "enabled": True,
        "accounts": [
            {
                "id": "account_1",
                "label": "Codex 1",
                "enabled": True,
                "runtime": {"session_state": "logged_in"},
                "last_snapshot": {"five_hour_limit": "47%", "weekly_limit": "52%"},
            }
        ],
    }


def _refresh_with_desktop(desktop, *, window=None):
    root = _Root()
    window = window or _Window()
    overlay = CodexUsageTaskbarOverlay(
        root,
        _runtime,
        window_factory=lambda _root: window,
        work_area_getter=lambda: (0, 0, 1920, 1040),
        occupied_span_getter=lambda *_args: [],
        taskbar_target_getter=taskbar_overlay._collect_taskbar_overlay_targets,
    )
    with patch.object(taskbar_overlay, "win32gui", _FakeWin32Gui(desktop)), patch.object(
        taskbar_overlay,
        "win32api",
        _FakeWin32Api(desktop),
    ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con), patch.object(
        taskbar_overlay.ctypes,
        "windll",
        _FakeWindll(),
        create=True,
    ):
        overlay.refresh()
        return overlay, root, window


class CodexUsageTaskbarOverlayMultiMonitorTest(unittest.TestCase):
    def test_primary_fullscreen_moves_overlay_to_displayable_secondary_taskbar(self):
        _overlay, _root, window = _refresh_with_desktop(_Desktop())

        geometry = window.draw_calls[-1]["geometry"]
        self.assertGreaterEqual(int(geometry["x"]), 1920)
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertEqual(window.withdraw_calls, 0)

    def test_invalid_first_secondary_falls_through_to_next_displayable_taskbar(self):
        desktop = _Desktop()
        desktop.monitor_infos[2]["Work"] = (1920, 0, 3840, 1080)
        desktop.taskbars[20] = ("Shell_SecondaryTrayWnd", (1920, 1040, 3840, 1080), False)
        desktop.add_secondary(3, work=(3840, 0, 5760, 1040), taskbar_rect=(3840, 1040, 5760, 1080))

        _overlay, _root, window = _refresh_with_desktop(desktop)

        self.assertGreaterEqual(int(window.draw_calls[-1]["geometry"]["x"]), 3840)

    def test_primary_fullscreen_with_no_displayable_secondary_withdraws_overlay(self):
        desktop = _Desktop()
        desktop.primary_fullscreen = False

        overlay, root, window = _refresh_with_desktop(desktop)
        self.assertTrue(window.draw_calls)

        desktop.primary_fullscreen = True
        desktop.monitor_infos[2]["Work"] = (1920, 0, 3840, 1080)
        desktop.taskbars[20] = ("Shell_SecondaryTrayWnd", (1920, 1040, 3840, 1080), False)

        with patch.object(taskbar_overlay, "win32gui", _FakeWin32Gui(desktop)), patch.object(
            taskbar_overlay,
            "win32api",
            _FakeWin32Api(desktop),
        ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con), patch.object(
            taskbar_overlay.ctypes,
            "windll",
            _FakeWindll(),
            create=True,
        ):
            overlay.invalidate_geometry()
            overlay.refresh()

        self.assertEqual(len(window.draw_calls), 1)
        self.assertGreaterEqual(window.withdraw_calls, 1)
        self.assertFalse(overlay._window_visible)
        self.assertTrue(any(call[1].__name__ == "_geometry_monitor_tick" for call in root.after_calls))

    def test_fullscreen_release_restores_overlay_to_primary_taskbar(self):
        desktop = _Desktop()
        overlay, _root, window = _refresh_with_desktop(desktop)
        self.assertGreaterEqual(int(window.draw_calls[-1]["geometry"]["x"]), 1920)

        desktop.primary_fullscreen = False
        with patch.object(taskbar_overlay, "win32gui", _FakeWin32Gui(desktop)), patch.object(
            taskbar_overlay,
            "win32api",
            _FakeWin32Api(desktop),
        ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con), patch.object(
            taskbar_overlay.ctypes,
            "windll",
            _FakeWindll(),
            create=True,
        ):
            overlay.invalidate_geometry()
            overlay.refresh()

        self.assertLess(int(window.draw_calls[-1]["geometry"]["x"]), 1920)


if __name__ == "__main__":
    unittest.main()
