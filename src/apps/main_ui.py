from __future__ import annotations

import ctypes
import threading
from typing import Any

from src.apps.main_ui_state import load_last_tab, save_last_tab
from src.utils.app_version import get_app_version_label


class WindowsSupporterMainUI:
    _TAB_DASHBOARD = "dashboard"
    _TAB_STARTUP = "startup_apps"
    _TAB_KAKAO = "kakao_monitor"
    _TAB_WRIKE = "wrike"
    _TAB_AI_USAGE = "ai_usage"
    _TAB_CODEX = "codex_usage"
    _TAB_UPDATE = "update"
    _TAB_POWER = "power"
    _KAKAO_RETRY_DELAY_MS = 500
    # Tk가 Windows 배율을 이미 반영한 상태에서 앱 배율을 다시 올리면
    # 모든 탭과 보조 창이 함께 커져 작업 영역을 쉽게 넘는다. 96dpi 기준
    # 1.0을 앱 내부 상한으로 두고, 사용자가 지정한 시스템 배율보다 크게
    # 만들지 않는다.
    _UI_BASE_SCALE = 1.0

    def __init__(
        self,
        root: Any,
        startup_manager: Any,
        monitor: Any,
        event_queue: Any = None,
        updater: Any = None,
        state_path: str | None = None,
        lid_power_policy: Any = None,
    ) -> None:
        self._root = root
        self._startup_manager = startup_manager
        self._monitor = monitor
        self._event_queue = event_queue
        self._updater = updater
        self._lid_power_policy = lid_power_policy
        self._state_path = state_path
        self._power_status_refresh_lock = threading.Lock()
        self._power_status_refresh_pending = False

        self._tk = None
        self._ttk = None

        self._notebook = None
        self._shell_frame = None
        self._footer_frame = None
        self._version_label = None
        self._tab_dashboard = None
        self._tab_startup = None
        self._tab_kakao = None
        self._tab_wrike = None
        self._tab_ai_usage = None
        self._tab_codex = None
        self._tab_update = None
        self._tab_power = None

        self._dashboard_view = None
        self._dashboard_built = False
        self._startup_view = None
        self._startup_built = False
        self._kakao_built = False
        self._kakao_retry_after_id = None
        self._wrike_view = None
        self._wrike_built = False
        self._ai_usage_view = None
        self._ai_usage_built = False
        self._codex_view = None
        self._codex_built = False
        self._update_view = None
        self._update_built = False
        self._power_view = None
        self._power_built = False
        self._current_tab = None
        # 탭 크기는 실제 콘텐츠 요구 크기를 우선한다. 이 값들은 콘텐츠가
        # 아직 mount되지 않았거나 요청 크기를 측정할 수 없는 탭의 compact
        # fallback이며, _apply_tab_geometry가 작업 영역 상한을 적용한다.
        self._tab_sizes = {
            self._TAB_DASHBOARD: (1000, 480),
            self._TAB_STARTUP: (1000, 560),
            self._TAB_KAKAO: (700, 340),
            self._TAB_WRIKE: (840, 580),
            self._TAB_AI_USAGE: (1000, 560),
            self._TAB_UPDATE: (760, 420),
            self._TAB_POWER: (800, 420),
        }
        self._tab_minsizes = {
            self._TAB_DASHBOARD: (760, 400),
            self._TAB_STARTUP: (820, 440),
            self._TAB_KAKAO: (600, 300),
            self._TAB_WRIKE: (720, 440),
            self._TAB_AI_USAGE: (820, 480),
            self._TAB_UPDATE: (640, 340),
            self._TAB_POWER: (680, 340),
        }

        self._lazy_import_tk()
        self._build_shell()
        self._attach_updater_status_callback()
        self._attach_lid_power_status_callback()
        return

    def _ui_post(self, fn) -> bool:
        if not callable(fn):
            return False
        queue_obj = self._event_queue
        if queue_obj is None:
            return False
        try:
            queue_obj.put(fn)
            return True
        except Exception:
            return False

    def show(self, tab: str | None = None) -> None:
        root = self._root
        hidden = False
        try:
            hidden = str(root.state()).lower() in {"withdrawn", "iconic"}
        except Exception:
            pass

        # 더블클릭/트레이 재진입 때 먼저 창을 보이게 하면 fallback geometry가
        # 한 프레임 노출된 뒤 콘텐츠 측정 결과로 다시 튀는 flash가 생긴다.
        # 숨겨진 창은 콘텐츠를 만들고 fit한 다음 한 번만 deiconify한다.
        if hidden:
            try:
                root.withdraw()
            except Exception:
                pass

        if tab:
            self._select_tab(str(tab))
        else:
            self._select_tab(self._load_last_tab())
        self._ensure_selected_tab_built()

        try:
            root.deiconify()
        except Exception:
            pass
        try:
            root.lift()
            root.focus_force()
        except Exception:
            pass
        return

    def hide(self) -> None:
        # KakaoTalk 탭에서만 모니터 번호(오버레이)가 보이도록,
        # UI가 숨겨질 때는 항상 오버레이를 정리한다.
        try:
            kakao = self._monitor.get_kakao_manager()
        except Exception:
            kakao = None
        if kakao is not None:
            try:
                kakao.hide_monitor_overlays()
            except Exception:
                pass
        try:
            self._root.withdraw()
        except Exception:
            pass
        return

    def show_startup_apps(self) -> None:
        self.show(self._TAB_STARTUP)
        return

    def show_dashboard(self) -> None:
        self.show(self._TAB_DASHBOARD)
        return

    def show_kakao_monitor(self) -> None:
        self.show(self._TAB_KAKAO)
        return

    def show_wrike(self) -> None:
        self.show(self._TAB_WRIKE)
        return

    def show_ai_usage(self) -> None:
        self.show(self._TAB_AI_USAGE)
        return

    def show_codex_usage(self) -> None:
        self.show_ai_usage()
        return

    def show_update_settings(self) -> None:
        self.show(self._TAB_UPDATE)
        return

    def show_power_settings(self) -> None:
        if self._tab_power is not None:
            self.show(self._TAB_POWER)
        return

    def _lazy_import_tk(self) -> None:
        if self._tk is not None and self._ttk is not None:
            return
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            self._tk = None
            self._ttk = None
            return
        self._tk = tk
        self._ttk = ttk
        return

    def _apply_base_ui_scaling(self) -> None:
        """Tk 기본 scaling이 앱의 compact 상한을 넘지 않게 한다.

        폰트는 위젯 생성 시점의 scaling으로 픽셀 크기가 정해지므로 어떤
        위젯도 만들기 전에 호출해야 한다. Windows의 배율을 그대로 한 번
        더 적용해 전체 UI가 확대되는 것을 막는다.
        """
        root = self._root
        try:
            current = float(root.tk.call("tk", "scaling"))
        except Exception:
            return
        base = 96.0 / 72.0
        if base <= 0:
            return
        target = min(current, base * self._UI_BASE_SCALE)
        if target >= current - 1e-9:
            return
        try:
            root.tk.call("tk", "scaling", target)
        except Exception:
            pass
        return

    def _build_shell(self) -> None:
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return

        root = self._root
        self._apply_base_ui_scaling()
        try:
            root.title("Windows Supporter")
        except Exception:
            pass
        try:
            w, h = self._scaled_size(
                self._tab_sizes.get(self._TAB_DASHBOARD, (1000, 480))
            )
            work_width, work_height = self._work_area_size()
            w = min(int(w), max(320, int(work_width) - 32))
            h = min(int(h), max(280, int(work_height) - 48))
            root.geometry(f"{int(w)}x{int(h)}")
        except Exception:
            pass
        try:
            mw, mh = self._scaled_size(
                self._tab_minsizes.get(self._TAB_DASHBOARD, (760, 400))
            )
            work_width, work_height = self._work_area_size()
            mw = min(int(mw), max(320, int(work_width) - 32))
            mh = min(int(mh), max(280, int(work_height) - 48))
            root.minsize(int(mw), int(mh))
        except Exception:
            pass

        try:
            root.protocol("WM_DELETE_WINDOW", self.hide)
        except Exception:
            pass
        try:
            root.bind("<Escape>", lambda _e: self.hide())
        except Exception:
            pass

        shell = ttk.Frame(root)
        self._shell_frame = shell
        try:
            shell.pack(fill="both", expand=True)
        except Exception:
            pass

        footer = ttk.Frame(shell)
        self._footer_frame = footer
        try:
            footer.pack(side="bottom", fill="x")
        except Exception:
            pass
        try:
            self._version_label = ttk.Label(
                footer,
                text=get_app_version_label(),
                anchor="e",
            )
            self._version_label.pack(side="right", padx=(8, 10), pady=(2, 4))
        except Exception:
            self._version_label = None

        notebook = ttk.Notebook(shell)
        self._notebook = notebook
        try:
            notebook.pack(side="top", fill="both", expand=True)
        except Exception:
            pass

        tab_dashboard = ttk.Frame(notebook)
        tab_startup = ttk.Frame(notebook)
        tab_kakao = ttk.Frame(notebook)
        tab_wrike = ttk.Frame(notebook)
        tab_ai_usage = ttk.Frame(notebook)
        tab_update = ttk.Frame(notebook)
        tab_power = None
        if bool(getattr(self._lid_power_policy, "is_supported", False)):
            tab_power = ttk.Frame(notebook)
        self._tab_dashboard = tab_dashboard
        self._tab_startup = tab_startup
        self._tab_kakao = tab_kakao
        self._tab_wrike = tab_wrike
        self._tab_ai_usage = tab_ai_usage
        self._tab_codex = tab_ai_usage
        self._tab_update = tab_update
        self._tab_power = tab_power

        notebook.add(tab_dashboard, text="Dashboard")
        notebook.add(tab_startup, text="Startup Apps")
        notebook.add(tab_kakao, text="KakaoTalk")
        notebook.add(tab_wrike, text="Wrike")
        notebook.add(tab_ai_usage, text="AI 사용량")
        notebook.add(tab_update, text="Update")
        if tab_power is not None:
            notebook.add(tab_power, text="전원")

        try:
            notebook.bind("<<NotebookTabChanged>>", lambda _e: self._ensure_selected_tab_built())
        except Exception:
            pass

        try:
            ttk.Label(tab_dashboard, text="Dashboard를 여는 중...").pack(padx=12, pady=12)
            ttk.Label(tab_startup, text="Startup Apps 설정을 여는 중...").pack(padx=12, pady=12)
            ttk.Label(tab_kakao, text="KakaoTalk 모니터 설정을 여는 중...").pack(padx=12, pady=12)
            ttk.Label(tab_wrike, text="Wrike 설정을 여는 중...").pack(padx=12, pady=12)
            ttk.Label(tab_ai_usage, text="AI 사용량 설정을 여는 중...").pack(padx=12, pady=12)
            ttk.Label(tab_update, text="Update 설정을 여는 중...").pack(padx=12, pady=12)
            if tab_power is not None:
                ttk.Label(tab_power, text="클램쉘 전원 설정을 여는 중...").pack(
                    padx=12,
                    pady=12,
                )
        except Exception:
            pass
        return

    def _select_tab(self, tab: str) -> None:
        nb = self._notebook
        if nb is None:
            return
        t = str(tab).strip().lower()
        if t in {"dashboard", "home", "main"}:
            try:
                nb.select(self._tab_dashboard)
            except Exception:
                pass
            return
        if t in {"startup", "startup_apps", "startupapps"}:
            try:
                nb.select(self._tab_startup)
            except Exception:
                pass
            return
        if t in {"kakao", "kakao_monitor", "kakaotalk"}:
            try:
                nb.select(self._tab_kakao)
            except Exception:
                pass
            return
        if t in {"wrike", "wrike_timelog", "timelog"}:
            try:
                nb.select(self._tab_wrike)
            except Exception:
                pass
            return
        if t in {
            "ai",
            "ai_usage",
            "ai_usage_monitor",
            "codex",
            "codex_usage",
            "codex_usage_monitor",
        }:
            try:
                nb.select(self._tab_ai_usage or self._tab_codex)
            except Exception:
                pass
            return
        if t in {"update", "updates", "auto_update", "updater"}:
            try:
                nb.select(self._tab_update)
            except Exception:
                pass
            return
        if t in {"power", "lid_power", "clamshell"} and self._tab_power is not None:
            try:
                nb.select(self._tab_power)
            except Exception:
                pass
            return
        return

    def _valid_tab_keys(self) -> tuple[str, ...]:
        values = [
            self._TAB_DASHBOARD,
            self._TAB_STARTUP,
            self._TAB_KAKAO,
            self._TAB_WRIKE,
            self._TAB_AI_USAGE,
            self._TAB_UPDATE,
        ]
        if self._tab_power is not None:
            values.append(self._TAB_POWER)
        return tuple(values)

    def _load_last_tab(self) -> str:
        return load_last_tab(
            valid_tabs=self._valid_tab_keys(),
            default=self._TAB_DASHBOARD,
            path=self._state_path,
        )

    def _save_last_tab(self, tab_key: str) -> None:
        save_last_tab(
            str(tab_key or ""),
            valid_tabs=self._valid_tab_keys(),
            path=self._state_path,
        )
        return

    def _remember_tab_size(self, tab_key: str | None) -> None:
        if not tab_key:
            return
        try:
            w = int(self._root.winfo_width())
            h = int(self._root.winfo_height())
        except Exception:
            return
        if w <= 1 or h <= 1:
            return
        # winfo_width/height는 이미 scaling이 적용된 physical pixel이다.
        # fallback은 logical pixel로 관리하므로 저장 시 다시 logical 단위로
        # 환산해야 다음 탭 전환에서 배율을 중복 적용하지 않는다.
        scale = self._ui_scale()
        self._tab_sizes[tab_key] = (
            max(1, int(round(w / scale))),
            max(1, int(round(h / scale))),
        )
        return

    def _ui_scale(self) -> float:
        """Tk scaling(포인트→픽셀) 비율을 96dpi 기준 상대 배율로 바꾼다.

        앱은 Windows scaling을 별도로 다시 확대하지 않으므로 compact 상한을
        적용한다. Tk를 읽을 수 없는 테스트 더블에서는 1.0으로 둔다.
        """
        root = self._root
        try:
            scaling = float(root.tk.call("tk", "scaling"))
        except Exception:
            return 1.0
        base = 96.0 / 72.0
        if scaling <= 0 or base <= 0:
            return 1.0
        return max(1.0, min(self._UI_BASE_SCALE, scaling / base))

    def _scaled_size(self, size: tuple[int, int] | list[int]) -> tuple[int, int]:
        scale = self._ui_scale()
        try:
            width, height = int(size[0]), int(size[1])
        except Exception:
            return (1, 1)
        return (max(1, int(round(width * scale))), max(1, int(round(height * scale))))

    def _apply_tab_geometry(self, tab_key: str) -> None:
        root = self._root
        try:
            root.update_idletasks()
        except Exception:
            pass

        try:
            min_size = self._tab_minsizes.get(tab_key) or (1, 1)
        except Exception:
            min_size = (1, 1)
        fallback_min_width, fallback_min_height = self._scaled_size(min_size)
        last_geometry = None

        # A narrow window changes wrapping and therefore the requested height of
        # the content. Two passes settle that feedback loop without resizing on
        # every status refresh.
        for _ in range(2):
            preferred_width, preferred_height = self._preferred_window_size(tab_key)
            work_width, work_height = self._work_area_size()
            max_width = max(320, int(work_width) - 32)
            max_height = max(280, int(work_height) - 48)
            width = min(max(1, int(preferred_width)), max_width)
            height = min(max(1, int(preferred_height)), max_height)

            # A measured dashboard can be smaller than the historical fallback
            # minimum. Do not reintroduce the old blank area by forcing that
            # minimum back above the content-fit size.
            min_width = min(int(fallback_min_width), max_width, width)
            min_height = min(int(fallback_min_height), max_height, height)
            width = max(width, min_width)
            height = max(height, min_height)
            geometry = self._centered_geometry(
                width,
                height,
                work_width=work_width,
                work_height=work_height,
            )

            if geometry == last_geometry:
                break
            last_geometry = geometry
            try:
                root.minsize(max(1, min_width), max(1, min_height))
            except Exception:
                pass
            try:
                root.geometry(geometry)
            except Exception:
                pass
            try:
                root.update_idletasks()
            except Exception:
                pass
        return

    def _tab_widget(self, tab_key: str):
        return {
            self._TAB_DASHBOARD: self._tab_dashboard,
            self._TAB_STARTUP: self._tab_startup,
            self._TAB_KAKAO: self._tab_kakao,
            self._TAB_WRIKE: self._tab_wrike,
            self._TAB_AI_USAGE: self._tab_ai_usage or self._tab_codex,
            self._TAB_UPDATE: self._tab_update,
            self._TAB_POWER: self._tab_power,
        }.get(str(tab_key))

    def _tab_view(self, tab_key: str):
        return {
            self._TAB_DASHBOARD: self._dashboard_view,
            self._TAB_STARTUP: self._startup_view,
            self._TAB_WRIKE: self._wrike_view,
            self._TAB_AI_USAGE: self._ai_usage_view or self._codex_view,
            self._TAB_UPDATE: self._update_view,
            self._TAB_POWER: self._power_view,
        }.get(str(tab_key))

    @staticmethod
    def _widget_requested_size(widget: Any) -> tuple[int, int] | None:
        if widget is None:
            return None
        try:
            width = int(widget.winfo_reqwidth())
            height = int(widget.winfo_reqheight())
        except Exception:
            return None
        if width <= 1 or height <= 1:
            return None
        return width, height

    def _window_chrome_size(self, tab: Any) -> tuple[int, int]:
        notebook = self._notebook
        notebook_size = self._widget_requested_size(notebook)
        tab_size = self._widget_requested_size(tab)
        footer_size = self._widget_requested_size(self._footer_frame)
        chrome_width = 0
        chrome_height = footer_size[1] if footer_size else 0
        if notebook_size and tab_size:
            chrome_width = max(0, notebook_size[0] - tab_size[0])
            chrome_height += max(0, notebook_size[1] - tab_size[1])
        return chrome_width, chrome_height

    def _preferred_window_size(self, tab_key: str) -> tuple[int, int]:
        try:
            fallback = self._scaled_size(
                self._tab_sizes.get(tab_key) or (1000, 560)
            )
        except Exception:
            fallback = (1000, 560)

        tab = self._tab_widget(tab_key)
        if tab is None:
            return fallback

        measured = None
        view = self._tab_view(tab_key)
        getter = getattr(view, "preferred_size", None)
        if callable(getter):
            try:
                value = getter()
                if isinstance(value, (tuple, list)) and len(value) >= 2:
                    measured = (int(value[0]), int(value[1]))
            except Exception:
                measured = None
        if measured is None:
            measured = self._widget_requested_size(tab)
        if not measured or measured[0] <= 1 or measured[1] <= 1:
            return fallback

        chrome_width, chrome_height = self._window_chrome_size(tab)
        return (
            max(1, int(measured[0]) + chrome_width),
            max(1, int(measured[1]) + chrome_height),
        )

    def _centered_geometry(
        self,
        width: int,
        height: int,
        *,
        work_width: int,
        work_height: int,
    ) -> str:
        base = f"{int(width)}x{int(height)}"
        root = self._root
        if not callable(getattr(root, "winfo_x", None)) or not callable(
            getattr(root, "winfo_y", None)
        ):
            return base
        left, top = self._work_area_origin()
        x = int(left) + max(0, (int(work_width) - int(width)) // 2)
        y = int(top) + max(0, (int(work_height) - int(height)) // 2)
        # Keep the explicit '+' separator for negative absolute coordinates;
        # Tk otherwise interprets '-10' as a right/bottom offset.
        return f"{base}+{x}+{y}"

    def _work_area_origin(self) -> tuple[int, int]:
        left, top, _right, _bottom = self._work_area_rect()
        return left, top

    def _work_area_rect(self) -> tuple[int, int, int, int]:
        root = self._root
        try:
            hwnd = int(root.winfo_id())

            class _Rect(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            class _MonitorInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_ulong),
                    ("rcMonitor", _Rect),
                    ("rcWork", _Rect),
                    ("dwFlags", ctypes.c_ulong),
                ]

            user32 = ctypes.windll.user32
            monitor_from_window = user32.MonitorFromWindow
            monitor_from_window.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            monitor_from_window.restype = ctypes.c_void_p
            get_monitor_info = user32.GetMonitorInfoW
            get_monitor_info.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_MonitorInfo),
            ]
            get_monitor_info.restype = ctypes.c_int
            monitor = monitor_from_window(hwnd, 2)
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(_MonitorInfo)
            if monitor and get_monitor_info(monitor, ctypes.byref(info)):
                left = int(info.rcWork.left)
                top = int(info.rcWork.top)
                right = int(info.rcWork.right)
                bottom = int(info.rcWork.bottom)
                if right > left and bottom > top:
                    return left, top, right, bottom
        except Exception:
            pass
        try:
            width = int(root.winfo_screenwidth())
            height = int(root.winfo_screenheight())
            if width > 0 and height > 0:
                return 0, 0, width, height
        except Exception:
            pass
        return 0, 0, 1920, 1080

    def _work_area_size(self) -> tuple[int, int]:
        left, top, right, bottom = self._work_area_rect()
        return max(1, int(right - left)), max(1, int(bottom - top))

    def _ensure_selected_tab_built(self) -> None:
        nb = self._notebook
        if nb is None:
            return
        try:
            cur = nb.select()
        except Exception:
            return

        try:
            new_tab = None
            if self._tab_dashboard is not None and cur == str(self._tab_dashboard):
                new_tab = self._TAB_DASHBOARD
            elif self._tab_startup is not None and cur == str(self._tab_startup):
                new_tab = self._TAB_STARTUP
            elif self._tab_kakao is not None and cur == str(self._tab_kakao):
                new_tab = self._TAB_KAKAO
            elif self._tab_wrike is not None and cur == str(self._tab_wrike):
                new_tab = self._TAB_WRIKE
            elif self._tab_ai_usage is not None and cur == str(self._tab_ai_usage):
                new_tab = self._TAB_AI_USAGE
            elif self._tab_codex is not None and cur == str(self._tab_codex):
                new_tab = self._TAB_AI_USAGE
            elif self._tab_update is not None and cur == str(self._tab_update):
                new_tab = self._TAB_UPDATE
            elif self._tab_power is not None and cur == str(self._tab_power):
                new_tab = self._TAB_POWER

            if new_tab is None:
                return

            old_tab = self._current_tab
            if new_tab != old_tab:
                self._remember_tab_size(old_tab)

                if old_tab == self._TAB_KAKAO and new_tab != self._TAB_KAKAO:
                    try:
                        kakao = self._monitor.get_kakao_manager()
                    except Exception:
                        kakao = None
                    if kakao is not None:
                        try:
                            kakao.hide_monitor_overlays()
                        except Exception:
                            pass

            if new_tab == self._TAB_DASHBOARD:
                self._ensure_dashboard_built()
            elif new_tab == self._TAB_STARTUP:
                self._ensure_startup_built()
            elif new_tab == self._TAB_KAKAO:
                self._ensure_kakao_built()
            elif new_tab == self._TAB_WRIKE:
                self._ensure_wrike_built()
            elif new_tab == self._TAB_AI_USAGE:
                self._ensure_ai_usage_built()
            elif new_tab == self._TAB_UPDATE:
                self._ensure_update_built()
            elif new_tab == self._TAB_POWER:
                self._ensure_power_built()
                self._refresh_power_view()

            self._apply_tab_geometry(new_tab)
            self._current_tab = new_tab
            self._save_last_tab(new_tab)
            return
        except Exception:
            return
        return

    def _ensure_dashboard_built(self) -> None:
        if self._dashboard_built or self._tab_dashboard is None:
            return
        try:
            from src.apps.dashboard_ui import DashboardView
        except Exception:
            return
        try:
            self._dashboard_view = DashboardView(
                self._root,
                status_provider=self._get_dashboard_status_snapshot,
                callbacks=self._get_dashboard_callbacks(),
            )
            self._dashboard_view.mount(self._tab_dashboard)
            self._dashboard_built = True
        except Exception:
            self._dashboard_built = False
        return

    def _attach_updater_status_callback(self) -> None:
        updater = self._updater
        if updater is None:
            return
        setter = getattr(updater, "set_status_changed_callback", None)
        if not callable(setter):
            return
        try:
            setter(self._refresh_update_surfaces)
        except Exception:
            pass
        return

    def _attach_lid_power_status_callback(self) -> None:
        policy = self._lid_power_policy
        setter = getattr(policy, "set_status_changed_callback", None)
        if not callable(setter):
            return
        try:
            setter(self._queue_power_status_refresh)
        except Exception:
            pass
        return

    def _queue_power_status_refresh(self) -> None:
        queue_obj = self._event_queue
        if queue_obj is None:
            return
        with self._power_status_refresh_lock:
            if self._power_status_refresh_pending:
                return
            self._power_status_refresh_pending = True
        try:
            queue_obj.put(self._drain_power_status_refresh)
        except Exception:
            with self._power_status_refresh_lock:
                self._power_status_refresh_pending = False
        return

    def _drain_power_status_refresh(self) -> None:
        with self._power_status_refresh_lock:
            self._power_status_refresh_pending = False
        self._refresh_power_view()
        return

    def _refresh_power_view(self) -> None:
        view = self._power_view
        refresh = getattr(view, "refresh", None)
        if not callable(refresh):
            return
        try:
            refresh()
        except Exception:
            pass
        return

    def _refresh_update_surfaces(self) -> None:
        self._refresh_dashboard_status()
        update_view = self._update_view
        if update_view is None:
            return
        refresh = getattr(update_view, "refresh", None)
        if not callable(refresh):
            return
        try:
            refresh()
        except Exception:
            pass
        return

    def _refresh_dashboard_status(self) -> None:
        dashboard = self._dashboard_view
        if dashboard is None:
            return
        try:
            dashboard.refresh()
        except Exception:
            pass
        return

    def _get_dashboard_callbacks(self) -> dict[str, Any]:
        return {
            "startup.toggle": self._dashboard_startup_toggle,
            "startup.settings": self.show_startup_apps,
            "ai_usage.toggle": self._dashboard_ai_usage_toggle_enabled,
            "ai_usage.settings": self.show_ai_usage,
            "codex.toggle": self._dashboard_ai_usage_toggle_enabled,
            "codex.settings": self.show_codex_usage,
            "kakao.toggle": self._dashboard_kakao_toggle_enabled,
            "kakao.settings": self.show_kakao_monitor,
            "wrike.toggle": self._dashboard_wrike_toggle_enabled,
            "wrike.settings": self.show_wrike,
            "background.toggle": self._dashboard_background_toggle_enabled,
            "update.check": self._dashboard_update_check,
            "update.settings": self.show_update_settings,
        }

    def _run_bg(self, fn) -> bool:
        if not callable(fn):
            return False
        try:
            threading.Thread(target=fn, daemon=True).start()
            return True
        except Exception:
            return False

    def _dashboard_startup_toggle(self) -> None:
        def task() -> None:
            try:
                self._startup_manager.toggle_enabled()
                self._startup_manager.start(self._root)
            except Exception:
                pass
            return

        self._run_bg(task)
        return

    def _dashboard_startup_apply(self) -> None:
        self._run_bg(lambda: self._startup_manager.start(self._root))
        return

    def _dashboard_startup_rescan_apply(self) -> None:
        def task() -> None:
            try:
                self._startup_manager.rescan_defaults_merge()
                self._startup_manager.start(self._root)
            except Exception:
                pass
            return

        self._run_bg(task)
        return

    def _get_ai_usage_monitor(self):
        getter = getattr(self._monitor, "get_ai_usage_monitor", None)
        if callable(getter):
            try:
                monitor = getter()
                if monitor is not None:
                    return monitor
            except Exception:
                pass
        try:
            return self._monitor.get_codex_usage_monitor()
        except Exception:
            return None

    def _get_codex_usage_monitor(self):
        return self._get_ai_usage_monitor()

    def _dashboard_ai_usage_toggle_enabled(self) -> None:
        usage = self._get_ai_usage_monitor()
        if usage is None:
            return
        settings_view = self._ai_usage_view
        prepared_settings = None
        view_mutation_started = False
        if settings_view is not None and self._event_queue is not None:
            begin_mutation = getattr(
                settings_view,
                "_begin_external_settings_mutation",
                None,
            )
            if callable(begin_mutation):
                try:
                    view_mutation_started, prepared_settings = begin_mutation()
                except Exception:
                    view_mutation_started = False
                    prepared_settings = None
                if not view_mutation_started:
                    return

        def resume_pending_autosave() -> None:
            if prepared_settings is None or settings_view is None:
                return
            resume = getattr(
                settings_view,
                "_resume_pending_autosave_after_external_failure",
                None,
            )
            if callable(resume):
                resume()
            return

        if prepared_settings is not None and settings_view is not None:
            flush_provider_change = getattr(
                settings_view,
                "_flush_provider_changing_settings_before_worker",
                None,
            )
            if callable(flush_provider_change):
                try:
                    flush_ok, flush_error, prepared_settings = flush_provider_change(
                        prepared_settings
                    )
                except Exception as exc:
                    flush_ok = False
                    flush_error = str(exc)
                if not bool(flush_ok):
                    finish_mutation = getattr(
                        settings_view,
                        "_finish_external_settings_mutation",
                        None,
                    )
                    if callable(finish_mutation):
                        finish_mutation(False, flush_error)
                    resume_pending_autosave()
                    self._refresh_dashboard_status()
                    return

        def task() -> None:
            ok = True
            error = None
            try:
                if prepared_settings is not None and settings_view is not None:
                    apply_settings = getattr(
                        settings_view,
                        "_apply_settings_update",
                        None,
                    )
                    if callable(apply_settings):
                        ok, error, _provider_changed = apply_settings(
                            prepared_settings,
                            update_ui=False,
                        )
                if ok:
                    toggle = getattr(usage, "toggle_enabled", None)
                    if callable(toggle):
                        result = toggle()
                    else:
                        settings = usage.get_settings_snapshot()
                        if not isinstance(settings, dict):
                            settings = {}
                        result = usage.update_settings(
                            {"enabled": not bool(settings.get("enabled", True))}
                        )
                    if isinstance(result, tuple):
                        ok = bool(result[0])
                        error = result[1] if len(result) > 1 else None
            except Exception as exc:
                ok = False
                error = str(exc)

            def done() -> None:
                if view_mutation_started and settings_view is not None:
                    finish_mutation = getattr(
                        settings_view,
                        "_finish_external_settings_mutation",
                        None,
                    )
                    if callable(finish_mutation):
                        finish_mutation(ok, error)
                    if not bool(ok):
                        resume_pending_autosave()
                self._refresh_dashboard_status()
                return

            if not self._ui_post(done):
                if threading.current_thread() is threading.main_thread():
                    done()
                elif view_mutation_started and settings_view is not None:
                    record_result = getattr(
                        settings_view,
                        "_record_external_settings_result_without_ui",
                        None,
                    )
                    if callable(record_result):
                        record_result(ok, error, prepared_settings)
                    else:
                        release_mutation = getattr(
                            settings_view,
                            "_release_external_settings_mutation_without_ui",
                            None,
                        )
                        if callable(release_mutation):
                            release_mutation()
            return

        worker_started = self._run_bg(task)
        if worker_started is False:
            if view_mutation_started and settings_view is not None:
                finish_mutation = getattr(
                    settings_view,
                    "_finish_external_settings_mutation",
                    None,
                )
                if callable(finish_mutation):
                    finish_mutation(False, "background_worker_start_failed")
                resume_pending_autosave()
            self._refresh_dashboard_status()
        return

    def _dashboard_codex_toggle_enabled(self) -> None:
        self._dashboard_ai_usage_toggle_enabled()
        return

    def _dashboard_codex_current_usage(self) -> None:
        codex = self._get_ai_usage_monitor()
        if codex is None:
            return
        try:
            codex.show_current_status(force_refresh=True)
        except Exception:
            pass
        return

    def _dashboard_codex_login(self) -> None:
        codex = self._get_ai_usage_monitor()
        if codex is None:
            return
        try:
            codex.show_current_status(force_refresh=True, source="manual_login")
        except Exception:
            pass
        return

    def _dashboard_kakao_show_numbers(self) -> None:
        try:
            kakao = self._monitor.get_kakao_manager()
        except Exception:
            kakao = None
        if kakao is None:
            return
        try:
            kakao.show_monitor_overlays(self._root, duration_ms=1500)
        except Exception:
            pass
        return

    def _dashboard_kakao_toggle_enabled(self) -> None:
        try:
            kakao = self._monitor.get_kakao_manager()
        except Exception:
            kakao = None
        if kakao is None:
            return
        try:
            settings = kakao.get_settings_snapshot()
            if not isinstance(settings, dict):
                settings = {}
            settings["enabled"] = not bool(settings.get("enabled", True))
            kakao.update_settings(settings)
        except Exception:
            pass
        return

    def _dashboard_wrike_weekly_timelog(self) -> None:
        try:
            wrike = self._monitor.get_wrike()
        except Exception:
            wrike = None
        if wrike is None:
            return
        try:
            wrike.show_weekly_timelog_summary(self._root)
        except Exception:
            pass
        return

    def _dashboard_wrike_toggle_enabled(self) -> None:
        try:
            wrike = self._monitor.get_wrike()
        except Exception:
            wrike = None
        if wrike is None:
            return
        try:
            settings = wrike.get_settings_snapshot()
            if not isinstance(settings, dict):
                settings = {}
            settings["monitor_enabled"] = not bool(settings.get("monitor_enabled", False))
            wrike.update_settings(settings)
        except Exception:
            pass
        return

    def _dashboard_background_toggle_enabled(self) -> None:
        try:
            status = self._monitor.get_dashboard_status_snapshot()
            current = bool(status.get("enabled", True)) if isinstance(status, dict) else True
            self._monitor.set_background_enabled(not current)
        except Exception:
            pass
        return

    def _dashboard_update_check(self) -> None:
        updater = self._updater
        if updater is None:
            return
        try:
            updater.check_now(manual=True)
        except Exception:
            pass
        return

    def _get_dashboard_status_snapshot(self) -> dict[str, Any]:
        ai_usage = self._get_ai_usage_dashboard_status()
        return {
            "startup": self._get_startup_dashboard_status(),
            "ai_usage": ai_usage,
            "codex": ai_usage,
            "kakao": self._get_kakao_dashboard_status(),
            "wrike": self._get_wrike_dashboard_status(),
            "background": self._get_background_dashboard_status(),
            "update": self._get_update_dashboard_status(),
        }

    def _get_update_dashboard_status(self) -> dict[str, Any]:
        updater = self._updater
        if updater is None:
            return {"state": "unavailable"}
        try:
            snapshot = updater.get_status_snapshot()
            return dict(snapshot) if isinstance(snapshot, dict) else {"state": "unknown"}
        except Exception:
            return {"state": "unknown"}

    def _get_startup_dashboard_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"enabled": True}
        try:
            out["enabled"] = bool(self._startup_manager.get_enabled_state())
        except Exception:
            pass
        try:
            cfg = self._startup_manager.load_config()
            instances = cfg.get("instances", []) if isinstance(cfg, dict) else []
            if isinstance(instances, list):
                runtime = self._startup_manager.get_instances_runtime(instances)
                out["total_count"] = len(instances)
                out["running_count"] = sum(1 for value in runtime.values() if bool(value[0]))
        except Exception:
            pass
        return out

    def _get_ai_usage_dashboard_status(self) -> dict[str, Any]:
        usage = self._get_ai_usage_monitor()
        if usage is None:
            return {}
        out: dict[str, Any] = {}
        try:
            settings = usage.get_settings_snapshot()
            if isinstance(settings, dict):
                out.update(settings)
        except Exception:
            pass
        try:
            runtime = usage.get_runtime_status()
            if isinstance(runtime, dict):
                out.update(runtime)
        except Exception:
            pass
        return out

    def _get_codex_dashboard_status(self) -> dict[str, Any]:
        return self._get_ai_usage_dashboard_status()

    def _get_kakao_dashboard_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            kakao = self._monitor.get_kakao_manager()
        except Exception:
            kakao = None
        if kakao is not None:
            try:
                settings = kakao.get_settings_snapshot()
                if isinstance(settings, dict):
                    out.update(settings)
            except Exception:
                pass
        status = self._get_background_dashboard_status()
        out["tick_active"] = bool(status.get("kakao_tick_active", False))
        return out

    def _get_wrike_dashboard_status(self) -> dict[str, Any]:
        try:
            wrike = self._monitor.get_wrike()
        except Exception:
            wrike = None
        if wrike is None:
            return {}
        try:
            data = wrike.get_settings_snapshot()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            pass
        return {}

    def _get_background_dashboard_status(self) -> dict[str, Any]:
        try:
            data = self._monitor.get_dashboard_status_snapshot()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            pass
        return {}

    def _ensure_startup_built(self) -> None:
        if self._startup_built or self._tab_startup is None:
            return
        try:
            from src.apps.startup_apps_ui import StartupAppsWindow
        except Exception:
            return
        try:
            self._startup_view = StartupAppsWindow(self._root, self._startup_manager)
            self._startup_view.mount(self._tab_startup)
            self._startup_built = True
        except Exception:
            self._startup_built = False
        return

    def _ensure_update_built(self) -> None:
        if self._update_built or self._tab_update is None or self._updater is None:
            return
        try:
            from src.apps.update_settings_ui import UpdateSettingsView
        except Exception:
            return
        try:
            self._update_view = UpdateSettingsView(self._root, self._updater)
            self._update_view.mount(self._tab_update)
            self._update_built = True
        except Exception:
            self._update_built = False
        return

    def _ensure_power_built(self) -> None:
        if (
            self._power_built
            or self._tab_power is None
            or self._lid_power_policy is None
        ):
            return
        try:
            from src.apps.lid_power_settings_ui import LidPowerSettingsView

            self._power_view = LidPowerSettingsView(
                self._root,
                self._lid_power_policy,
            )
            self._power_view.mount(self._tab_power)
            self._power_built = True
        except Exception:
            self._power_built = False
        return

    def _ensure_kakao_built(self) -> None:
        if self._kakao_built or self._tab_kakao is None:
            return

        kakao = None
        try:
            kakao = self._monitor.get_kakao_manager()
        except Exception:
            kakao = None
        if kakao is None:
            return

        try:
            self._kakao_built = bool(
                kakao.open_monitor_selector(self._root, embedded_parent=self._tab_kakao)
            )
        except Exception:
            self._kakao_built = False
        if not self._kakao_built:
            self._schedule_kakao_build_retry()
        return

    def _schedule_kakao_build_retry(self) -> None:
        if self._kakao_retry_after_id is not None:
            return

        def retry() -> None:
            self._kakao_retry_after_id = None
            if self._current_tab is not None and self._current_tab != self._TAB_KAKAO:
                return
            self._ensure_kakao_built()
            return

        try:
            delay = max(500, int(self._KAKAO_RETRY_DELAY_MS))
        except Exception:
            delay = 500
        try:
            after_id = self._root.after(delay, retry)
        except Exception:
            self._kakao_retry_after_id = None
            return
        if after_id:
            self._kakao_retry_after_id = after_id
        else:
            self._kakao_retry_after_id = None
        return

    def _ensure_wrike_built(self) -> None:
        if self._tab_wrike is None:
            return

        wrike = None
        try:
            wrike = self._monitor.get_wrike()
        except Exception:
            wrike = None
        if wrike is None:
            return

        try:
            from src.apps.wrike_ui import WrikeSettingsView
        except Exception:
            return

        try:
            self._wrike_view = WrikeSettingsView(
                self._root,
                wrike,
                ui_post=self._ui_post,
            )
            self._wrike_view.mount(self._tab_wrike)
            self._wrike_built = True
        except Exception:
            self._wrike_built = False
            try:
                for w in list(self._tab_wrike.winfo_children()):
                    try:
                        w.destroy()
                    except Exception:
                        continue
            except Exception:
                pass
            try:
                ttk = self._ttk
                if ttk is not None:
                    ttk.Label(
                        self._tab_wrike,
                        text="Wrike 설정 UI 로딩 실패 (wrike.log 확인)",
                    ).pack(padx=12, pady=12)
            except Exception:
                pass
            try:
                if hasattr(wrike, "log_info"):
                    wrike.log_info("wrike ui build failed")
            except Exception:
                pass
        return

    def _ensure_ai_usage_built(self) -> None:
        tab = self._tab_ai_usage or self._tab_codex
        if self._ai_usage_built or tab is None:
            return

        usage = self._get_ai_usage_monitor()
        if usage is None:
            return

        try:
            from src.apps.ai_usage_ui import AIUsageSettingsView
        except Exception:
            return

        try:
            self._ai_usage_view = AIUsageSettingsView(
                self._root,
                usage,
                ui_post=self._ui_post,
                on_external_settings_reconciled=self._refresh_dashboard_status,
            )
            self._ai_usage_view.mount(tab)
            self._ai_usage_built = True
            self._codex_view = self._ai_usage_view
            self._codex_built = True
        except Exception:
            self._ai_usage_built = False
            self._codex_built = False
            try:
                for w in list(tab.winfo_children()):
                    try:
                        w.destroy()
                    except Exception:
                        continue
            except Exception:
                pass
            try:
                ttk = self._ttk
                if ttk is not None:
                    ttk.Label(
                        tab,
                        text="AI 사용량 설정 UI 로딩 실패",
                    ).pack(padx=12, pady=12)
            except Exception:
                pass
        return

    def _ensure_codex_built(self) -> None:
        self._ensure_ai_usage_built()
        return
