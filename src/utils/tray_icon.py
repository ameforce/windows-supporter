from __future__ import annotations

import os
import threading
import time
from typing import Callable

import ctypes
from ctypes import wintypes

import win32api
import win32con
import win32gui
try:
    import win32ts
except Exception:
    win32ts = None

_WM_WTSSESSION_CHANGE = getattr(win32con, "WM_WTSSESSION_CHANGE", 0x02B1)
_WTS_SESSION_UNLOCK = 0x7
_WTS_NOTIFY_THIS_SESSION = 0
if win32ts is not None:
    try:
        _WTS_SESSION_UNLOCK = int(win32ts.WTS_SESSION_UNLOCK)
    except Exception:
        pass
    try:
        _WTS_NOTIFY_THIS_SESSION = int(win32ts.NOTIFY_FOR_THIS_SESSION)
    except Exception:
        pass

_WTS_API = None


def _get_wtsapi32():
    global _WTS_API
    if _WTS_API is None:
        try:
            _WTS_API = ctypes.WinDLL("wtsapi32", use_last_error=True)
            _WTS_API.WTSRegisterSessionNotification.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
            ]
            _WTS_API.WTSRegisterSessionNotification.restype = wintypes.BOOL
            _WTS_API.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
            _WTS_API.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
        except Exception:
            _WTS_API = False
    return _WTS_API if _WTS_API not in (None, False) else None


class SystemTrayIcon:
    _MENU_OPEN = 1001
    _MENU_APPLY = 1002
    _MENU_RESCAN = 1003
    _MENU_OPEN_LOG = 1004
    _MENU_OPEN_CONFIG = 1005
    _MENU_OPEN_CONFIG_DIR = 1006
    _MENU_TOGGLE_ENABLED = 1007
    _MENU_KAKAO_MONITOR = 1010
    _MENU_EXIT = 1099

    def __init__(
        self,
        tooltip: str,
        on_open_settings: Callable[[], None],
        on_exit: Callable[[], None],
        on_apply: Callable[[], None] | None = None,
        on_rescan: Callable[[], None] | None = None,
        on_open_log: Callable[[], None] | None = None,
        on_open_config: Callable[[], None] | None = None,
        on_open_config_dir: Callable[[], None] | None = None,
        on_toggle_enabled: Callable[[], None] | None = None,
        is_enabled: Callable[[], bool] | None = None,
        on_open_kakao_monitor: Callable[[], None] | None = None,
        icon_path: str | None = None,
        on_session_unlock: Callable[[], None] | None = None,
    ) -> None:
        self._tooltip = str(tooltip) if tooltip else "Windows Supporter"
        self._on_open_settings = on_open_settings
        self._on_exit = on_exit
        self._on_apply = on_apply
        self._on_rescan = on_rescan
        self._on_open_log = on_open_log
        self._on_open_config = on_open_config
        self._on_open_config_dir = on_open_config_dir
        self._on_toggle_enabled = on_toggle_enabled
        self._is_enabled = is_enabled
        self._on_open_kakao_monitor = on_open_kakao_monitor
        self._icon_path = str(icon_path).strip() if icon_path else None
        self._on_session_unlock = on_session_unlock

        self._hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wm_notify = win32con.WM_USER + 20
        self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        self._class_name = f"WindowsSupporterTray_{os.getpid()}"
        self._hicon: int | None = None
        self._hicon_owned = False
        self._last_menu_time = 0.0
        self._wts_registered = False
        return

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        t = threading.Thread(
            target=self._run,
            name="windows-supporter-tray",
            daemon=True,
        )
        self._thread = t
        t.start()
        return

    def stop(self) -> None:
        self._stop_event.set()
        hwnd = self._hwnd
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        return

    def _run(self) -> None:
        try:
            self._create_window()
            win32gui.PumpMessages()
        except Exception:
            try:
                self._hwnd = None
            except Exception:
                pass
        return

    def _create_window(self) -> None:
        message_map = {
            self._wm_notify: self._on_notify,
            self._taskbar_created: self._on_taskbar_restart,
            win32con.WM_COMMAND: self._on_command,
            win32con.WM_CLOSE: self._on_close,
            win32con.WM_DESTROY: self._on_destroy,
        }
        try:
            message_map[_WM_WTSSESSION_CHANGE] = self._on_session_change
        except Exception:
            pass

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = self._class_name
        wc.lpfnWndProc = message_map
        try:
            class_atom = win32gui.RegisterClass(wc)
        except Exception:
            class_atom = win32gui.RegisterClass(wc)

        hwnd = win32gui.CreateWindow(
            class_atom,
            self._class_name,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            wc.hInstance,
            None,
        )
        self._hwnd = hwnd
        win32gui.UpdateWindow(hwnd)
        self._add_icon()
        self._register_session_notifications()
        return

    def _add_icon(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        hicon = self._get_hicon()
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (hwnd, 0, flags, self._wm_notify, hicon, self._tooltip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        except Exception:
            return
        return

    def _remove_icon(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        try:
            nid = (hwnd, 0)
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, nid)
        except Exception:
            return
        return

    def _resolve_icon_path(self) -> str | None:
        if self._icon_path and os.path.isfile(self._icon_path):
            return self._icon_path
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        except Exception:
            return None
        candidate = os.path.join(base_dir, "windows_supporter.ico")
        return candidate if os.path.isfile(candidate) else None

    def _get_hicon(self) -> int:
        if self._hicon is not None:
            return int(self._hicon)

        icon_path = self._resolve_icon_path()
        if icon_path:
            try:
                flags = win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
                hicon = win32gui.LoadImage(
                    0,
                    icon_path,
                    win32con.IMAGE_ICON,
                    0,
                    0,
                    flags,
                )
                if hicon:
                    self._hicon = int(hicon)
                    self._hicon_owned = True
                    return int(hicon)
            except Exception:
                pass

        try:
            hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        except Exception:
            hicon = 0
        self._hicon = int(hicon) if hicon else 0
        self._hicon_owned = False
        return int(self._hicon)

    def _destroy_hicon(self) -> None:
        if self._hicon_owned and self._hicon:
            try:
                win32gui.DestroyIcon(int(self._hicon))
            except Exception:
                pass
        self._hicon = None
        self._hicon_owned = False
        return

    def _on_taskbar_restart(self, hwnd: int, msg: int, wparam: int, lparam: int):
        self._add_icon()
        return 0

    def _on_close(self, hwnd: int, msg: int, wparam: int, lparam: int):
        try:
            win32gui.DestroyWindow(hwnd)
        except Exception:
            pass
        return 0

    def _on_destroy(self, hwnd: int, msg: int, wparam: int, lparam: int):
        self._unregister_session_notifications()
        try:
            self._remove_icon()
        except Exception:
            pass
        try:
            self._destroy_hicon()
        except Exception:
            pass
        try:
            win32gui.PostQuitMessage(0)
        except Exception:
            pass
        return 0

    def _register_session_notifications(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        try:
            if win32ts is not None:
                win32ts.WTSRegisterSessionNotification(
                    hwnd,
                    _WTS_NOTIFY_THIS_SESSION,
                )
                self._wts_registered = True
                return
        except Exception:
            self._wts_registered = False
        wts = _get_wtsapi32()
        if wts is None:
            return
        try:
            if wts.WTSRegisterSessionNotification(hwnd, _WTS_NOTIFY_THIS_SESSION):
                self._wts_registered = True
        except Exception:
            self._wts_registered = False
        return

    def _unregister_session_notifications(self) -> None:
        if not self._wts_registered:
            return
        hwnd = self._hwnd
        if not hwnd:
            return
        try:
            if win32ts is not None:
                win32ts.WTSUnRegisterSessionNotification(hwnd)
            else:
                wts = _get_wtsapi32()
                if wts is not None:
                    wts.WTSUnRegisterSessionNotification(hwnd)
        except Exception:
            pass
        self._wts_registered = False
        return

    def _on_session_change(self, hwnd: int, msg: int, wparam: int, lparam: int):
        cb = self._on_session_unlock
        if cb is None:
            return 0
        try:
            if int(wparam) == int(_WTS_SESSION_UNLOCK):
                cb()
        except Exception:
            pass
        return 0

    def _on_notify(self, hwnd: int, msg: int, wparam: int, lparam: int):
        if self._stop_event.is_set():
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
            return 0

        if lparam == win32con.WM_LBUTTONDBLCLK:
            try:
                self._on_open_settings()
            except Exception:
                pass
            return 0

        if lparam in (
            win32con.WM_RBUTTONUP,
            win32con.WM_CONTEXTMENU,
            win32con.WM_RBUTTONDOWN,
        ):
            now = time.monotonic()
            if (now - float(self._last_menu_time)) < 0.20:
                return 0
            self._last_menu_time = float(now)
            try:
                self._show_menu()
            except Exception:
                pass
            return 0

        return 0

    def _show_menu(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return

        menu = win32gui.CreatePopupMenu()
        enabled = True
        if self._is_enabled is not None:
            try:
                enabled = bool(self._is_enabled())
            except Exception:
                enabled = True

        header = "Windows Supporter"
        header_state = "ON" if enabled else "OFF"
        win32gui.AppendMenu(
            menu,
            win32con.MF_STRING | win32con.MF_GRAYED,
            0,
            f"{header}  (Startup Apps: {header_state})",
        )
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")

        win32gui.AppendMenu(
            menu,
            win32con.MF_STRING,
            self._MENU_OPEN,
            "Startup Apps 설정 열기...",
        )

        if self._on_apply is not None:
            flags = win32con.MF_STRING
            if not enabled:
                flags |= win32con.MF_GRAYED
            win32gui.AppendMenu(menu, flags, self._MENU_APPLY, "Startup Apps 지금 적용")

        if self._on_rescan is not None:
            flags = win32con.MF_STRING
            if not enabled:
                flags |= win32con.MF_GRAYED
            win32gui.AppendMenu(
                menu,
                flags,
                self._MENU_RESCAN,
                "바로가기 재스캔 후 적용",
            )

        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")

        if self._on_toggle_enabled is not None:
            flags = win32con.MF_STRING
            if enabled:
                flags |= win32con.MF_CHECKED
            win32gui.AppendMenu(menu, flags, self._MENU_TOGGLE_ENABLED, "Startup Apps 활성화")

        if self._on_open_config is not None:
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING,
                self._MENU_OPEN_CONFIG,
                "설정 파일 열기",
            )

        if self._on_open_config_dir is not None:
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING,
                self._MENU_OPEN_CONFIG_DIR,
                "설정 폴더 열기",
            )

        if self._on_open_log is not None:
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING,
                self._MENU_OPEN_LOG,
                "로그 열기",
            )

        if self._on_open_kakao_monitor is not None:
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING,
                self._MENU_KAKAO_MONITOR,
                "KakaoTalk 모니터 선택...",
            )

        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self._MENU_EXIT, "종료")

        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

        try:
            x, y = win32gui.GetCursorPos()
        except Exception:
            x, y = (0, 0)
        win32gui.TrackPopupMenu(
            menu,
            win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN | win32con.TPM_RIGHTBUTTON,
            int(x),
            int(y),
            0,
            hwnd,
            None,
        )
        win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        try:
            win32gui.DestroyMenu(menu)
        except Exception:
            pass
        return

    def _on_command(self, hwnd: int, msg: int, wparam: int, lparam: int):
        cmd_id = win32api.LOWORD(wparam)
        if cmd_id == self._MENU_OPEN:
            try:
                self._on_open_settings()
            except Exception:
                pass
            return 0

        if cmd_id == self._MENU_APPLY and self._on_apply is not None:
            try:
                self._on_apply()
            except Exception:
                pass
            return 0

        if cmd_id == self._MENU_RESCAN and self._on_rescan is not None:
            try:
                self._on_rescan()
            except Exception:
                pass
            return 0

        if cmd_id == self._MENU_OPEN_LOG and self._on_open_log is not None:
            try:
                self._on_open_log()
            except Exception:
                pass
            return 0

        if cmd_id == self._MENU_OPEN_CONFIG and self._on_open_config is not None:
            try:
                self._on_open_config()
            except Exception:
                pass
            return 0

        if cmd_id == self._MENU_OPEN_CONFIG_DIR and self._on_open_config_dir is not None:
            try:
                self._on_open_config_dir()
            except Exception:
                pass
            return 0

        if cmd_id == self._MENU_TOGGLE_ENABLED and self._on_toggle_enabled is not None:
            try:
                self._on_toggle_enabled()
            except Exception:
                pass
            return 0

        if cmd_id == self._MENU_KAKAO_MONITOR and self._on_open_kakao_monitor is not None:
            try:
                self._on_open_kakao_monitor()
            except Exception:
                pass
            return 0

        if cmd_id == self._MENU_EXIT:
            try:
                self._on_exit()
            except Exception:
                pass
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
            return 0

        return 0
