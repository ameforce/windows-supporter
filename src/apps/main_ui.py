from __future__ import annotations

from typing import Any


class WindowsSupporterMainUI:
    _TAB_STARTUP = "startup_apps"
    _TAB_KAKAO = "kakao_monitor"
    _TAB_WRIKE = "wrike"
    _TAB_CODEX = "codex_usage"

    def __init__(self, root: Any, startup_manager: Any, monitor: Any) -> None:
        self._root = root
        self._startup_manager = startup_manager
        self._monitor = monitor

        self._tk = None
        self._ttk = None

        self._notebook = None
        self._tab_startup = None
        self._tab_kakao = None
        self._tab_wrike = None
        self._tab_codex = None

        self._startup_view = None
        self._startup_built = False
        self._kakao_built = False
        self._wrike_view = None
        self._wrike_built = False
        self._codex_view = None
        self._codex_built = False
        self._current_tab = None
        self._tab_sizes = {
            self._TAB_STARTUP: (1000, 560),
            self._TAB_KAKAO: (700, 340),
            self._TAB_WRIKE: (840, 580),
            self._TAB_CODEX: (840, 630),
        }
        self._tab_minsizes = {
            self._TAB_STARTUP: (940, 520),
            self._TAB_KAKAO: (620, 300),
            self._TAB_WRIKE: (800, 520),
            self._TAB_CODEX: (800, 590),
        }

        self._lazy_import_tk()
        self._build_shell()
        return

    def show(self, tab: str | None = None) -> None:
        root = self._root
        try:
            root.deiconify()
        except Exception:
            pass
        try:
            root.lift()
            root.focus_force()
        except Exception:
            pass

        if tab:
            self._select_tab(str(tab))
        self._ensure_selected_tab_built()
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

    def show_kakao_monitor(self) -> None:
        self.show(self._TAB_KAKAO)
        return

    def show_codex_usage(self) -> None:
        self.show(self._TAB_CODEX)
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

    def _build_shell(self) -> None:
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return

        root = self._root
        try:
            root.title("Windows Supporter")
        except Exception:
            pass
        try:
            w, h = self._tab_sizes.get(self._TAB_STARTUP, (1000, 560))
            root.geometry(f"{int(w)}x{int(h)}")
        except Exception:
            pass
        try:
            mw, mh = self._tab_minsizes.get(self._TAB_STARTUP, (940, 520))
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

        notebook = ttk.Notebook(root)
        self._notebook = notebook
        try:
            notebook.pack(fill="both", expand=True)
        except Exception:
            pass

        tab_startup = ttk.Frame(notebook)
        tab_kakao = ttk.Frame(notebook)
        tab_wrike = ttk.Frame(notebook)
        tab_codex = ttk.Frame(notebook)
        self._tab_startup = tab_startup
        self._tab_kakao = tab_kakao
        self._tab_wrike = tab_wrike
        self._tab_codex = tab_codex

        notebook.add(tab_startup, text="Startup Apps")
        notebook.add(tab_kakao, text="KakaoTalk")
        notebook.add(tab_wrike, text="Wrike")
        notebook.add(tab_codex, text="Codex")

        try:
            notebook.bind("<<NotebookTabChanged>>", lambda _e: self._ensure_selected_tab_built())
        except Exception:
            pass

        try:
            ttk.Label(tab_startup, text="Startup Apps 설정을 여는 중...").pack(padx=12, pady=12)
            ttk.Label(tab_kakao, text="KakaoTalk 모니터 설정을 여는 중...").pack(padx=12, pady=12)
            ttk.Label(tab_wrike, text="Wrike 설정을 여는 중...").pack(padx=12, pady=12)
            ttk.Label(tab_codex, text="Codex 사용량 설정을 여는 중...").pack(padx=12, pady=12)
        except Exception:
            pass
        return

    def _select_tab(self, tab: str) -> None:
        nb = self._notebook
        if nb is None:
            return
        t = str(tab).strip().lower()
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
        if t in {"codex", "codex_usage", "codex_usage_monitor"}:
            try:
                nb.select(self._tab_codex)
            except Exception:
                pass
            return
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
        self._tab_sizes[tab_key] = (w, h)
        return

    def _apply_tab_geometry(self, tab_key: str) -> None:
        root = self._root
        try:
            size = self._tab_sizes.get(tab_key)
        except Exception:
            size = None
        if not size:
            return
        w, h = size
        if int(w) <= 0 or int(h) <= 0:
            return
        try:
            cur_w = int(root.winfo_width())
            cur_h = int(root.winfo_height())
        except Exception:
            cur_w = -1
            cur_h = -1
        if cur_w != int(w) or cur_h != int(h):
            try:
                root.geometry(f"{int(w)}x{int(h)}")
            except Exception:
                pass
        try:
            min_size = self._tab_minsizes.get(tab_key)
            if min_size:
                mw, mh = min_size
                root.minsize(int(mw), int(mh))
        except Exception:
            pass
        return

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
            if self._tab_startup is not None and cur == str(self._tab_startup):
                new_tab = self._TAB_STARTUP
            elif self._tab_kakao is not None and cur == str(self._tab_kakao):
                new_tab = self._TAB_KAKAO
            elif self._tab_wrike is not None and cur == str(self._tab_wrike):
                new_tab = self._TAB_WRIKE
            elif self._tab_codex is not None and cur == str(self._tab_codex):
                new_tab = self._TAB_CODEX

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

            if new_tab == self._TAB_STARTUP:
                self._ensure_startup_built()
            elif new_tab == self._TAB_KAKAO:
                self._ensure_kakao_built()
            elif new_tab == self._TAB_WRIKE:
                self._ensure_wrike_built()
            elif new_tab == self._TAB_CODEX:
                self._ensure_codex_built()

            self._apply_tab_geometry(new_tab)
            self._current_tab = new_tab
            return
        except Exception:
            return
        return

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

    def _ensure_kakao_built(self) -> None:
        if self._tab_kakao is None:
            return

        kakao = None
        try:
            kakao = self._monitor.get_kakao_manager()
        except Exception:
            kakao = None
        if kakao is None:
            return

        try:
            kakao.open_monitor_selector(self._root, embedded_parent=self._tab_kakao)
            self._kakao_built = True
        except Exception:
            self._kakao_built = False
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
            self._wrike_view = WrikeSettingsView(self._root, wrike)
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

    def _ensure_codex_built(self) -> None:
        if self._codex_built or self._tab_codex is None:
            return

        codex = None
        try:
            codex = self._monitor.get_codex_usage_monitor()
        except Exception:
            codex = None
        if codex is None:
            return

        try:
            from src.apps.codex_usage_ui import CodexUsageSettingsView
        except Exception:
            return

        try:
            self._codex_view = CodexUsageSettingsView(self._root, codex)
            self._codex_view.mount(self._tab_codex)
            self._codex_built = True
        except Exception:
            self._codex_built = False
            try:
                for w in list(self._tab_codex.winfo_children()):
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
                        self._tab_codex,
                        text="Codex 설정 UI 로딩 실패",
                    ).pack(padx=12, pady=12)
            except Exception:
                pass
        return
