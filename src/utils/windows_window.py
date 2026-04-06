from __future__ import annotations

import win32api
import win32con
import win32gui
import win32process


def is_tool_window(hwnd: int) -> bool:
    try:
        exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    except Exception:
        return False
    return bool(exstyle & win32con.WS_EX_TOOLWINDOW)


def get_window_text(hwnd: int) -> str:
    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        return ""
    return title.strip() if title else ""


def get_window_pid(hwnd: int) -> int:
    try:
        _, pid_val = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid_val) if pid_val else 0
    except Exception:
        return 0


def apply_window_action(hwnd: int, action: str) -> bool:
    act = str(action).strip().lower()
    if act == "hide":
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return False
        except Exception:
            return False
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            return True
        except Exception:
            return False

    if act == "minimize":
        try:
            if win32gui.IsIconic(hwnd):
                return False
        except Exception:
            return False
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWMINNOACTIVE)
            return True
        except Exception:
            return False

    if act == "show":
        try:
            if (
                win32gui.IsWindowVisible(hwnd)
                and (not win32gui.IsIconic(hwnd))
                and (not win32gui.IsZoomed(hwnd))
            ):
                return False
            if win32gui.IsIconic(hwnd) or win32gui.IsZoomed(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            return True
        except Exception:
            return False

    if act == "close":
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return True
        except Exception:
            return False

    return False


def resize_window_to_monitor(hwnd: int, use_work_area: bool = False) -> bool:
    try:
        mon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(mon)
        rect = info.get("Work") if use_work_area else info.get("Monitor")
        if not rect:
            return False
        left, top, right, bottom = rect
    except Exception:
        return False

    w = int(right - left)
    h = int(bottom - top)
    if w <= 0 or h <= 0:
        return False

    try:
        cur_left, cur_top, cur_right, cur_bottom = win32gui.GetWindowRect(hwnd)
        if (
            int(cur_left) == int(left)
            and int(cur_top) == int(top)
            and int(cur_right - cur_left) == int(w)
            and int(cur_bottom - cur_top) == int(h)
        ):
            return False
    except Exception:
        pass

    try:
        if win32gui.IsIconic(hwnd) or win32gui.IsZoomed(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass

    flags = (
        win32con.SWP_NOZORDER
        | win32con.SWP_NOOWNERZORDER
        | win32con.SWP_NOACTIVATE
        | win32con.SWP_SHOWWINDOW
        | win32con.SWP_ASYNCWINDOWPOS
    )
    try:
        win32gui.SetWindowPos(hwnd, 0, int(left), int(top), int(w), int(h), flags)
        return True
    except Exception:
        return False
