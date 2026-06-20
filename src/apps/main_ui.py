from __future__ import annotations

import threading
from typing import Any

from src.apps.main_ui_state import load_last_tab, save_last_tab
from src.utils.app_version import get_app_version_label


class WindowsSupporterMainUI:
    _TAB_DASHBOARD = "dashboard"
    _TAB_STARTUP = "startup_apps"
    _TAB_KAKAO = "kakao_monitor"
    _TAB_WRIKE = "wrike"
    _TAB_CODEX = "codex_usage"
    _KAKAO_RETRY_DELAY_MS = 500

    def __init__(
        self,
        root: Any,
        startup_manager: Any,
        monitor: Any,
        event_queue: Any = None,
        updater: Any = None,
        state_path: str | None = None,
    ) -> None:
        self._root = root
        self._startup_manager = startup_manager
        self._monitor = monitor
        self._event_queue = event_queue
        self._updater = updater
        self._state_path = state_path

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
        self._tab_codex = None

        self._dashboard_view = None
        self._dashboard_built = False
        self._startup_view = None
        self._startup_built = False
        self._kakao_built = False
        self._kakao_retry_after_id = None
        self._wrike_view = None
        self._wrike_built = False
        self._codex_view = None
        self._codex_built = False
        self._current_tab = None
        self._tab_sizes = {
            self._TAB_DASHBOARD: (1000, 480),
            self._TAB_STARTUP: (1000, 560),
            self._TAB_KAKAO: (700, 340),
            self._TAB_WRIKE: (840, 580),
            self._TAB_CODEX: (900, 760),
        }
        self._tab_minsizes = {
            self._TAB_DASHBOARD: (940, 480),
            self._TAB_STARTUP: (940, 520),
            self._TAB_KAKAO: (620, 300),
            self._TAB_WRIKE: (800, 520),
            self._TAB_CODEX: (860, 720),
        }

        self._lazy_import_tk()
        self._build_shell()
        self._attach_updater_status_callback()
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
        else:
            self._select_tab(self._load_last_tab())
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

    def show_dashboard(self) -> None:
        self.show(self._TAB_DASHBOARD)
        return

    def show_kakao_monitor(self) -> None:
        self.show(self._TAB_KAKAO)
        return

    def show_wrike(self) -> None:
        self.show(self._TAB_WRIKE)
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
            w, h = self._tab_sizes.get(self._TAB_DASHBOARD, (1000, 480))
            root.geometry(f"{int(w)}x{int(h)}")
        except Exception:
            pass
        try:
            mw, mh = self._tab_minsizes.get(self._TAB_DASHBOARD, (940, 480))
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
        tab_codex = ttk.Frame(notebook)
        self._tab_dashboard = tab_dashboard
        self._tab_startup = tab_startup
        self._tab_kakao = tab_kakao
        self._tab_wrike = tab_wrike
        self._tab_codex = tab_codex

        notebook.add(tab_dashboard, text="Dashboard")
        notebook.add(tab_startup, text="Startup Apps")
        notebook.add(tab_kakao, text="KakaoTalk")
        notebook.add(tab_wrike, text="Wrike")
        notebook.add(tab_codex, text="Codex")

        try:
            notebook.bind("<<NotebookTabChanged>>", lambda _e: self._ensure_selected_tab_built())
        except Exception:
            pass

        try:
            ttk.Label(tab_dashboard, text="Dashboard를 여는 중...").pack(padx=12, pady=12)
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
        if t in {"codex", "codex_usage", "codex_usage_monitor"}:
            try:
                nb.select(self._tab_codex)
            except Exception:
                pass
            return
        return

    def _valid_tab_keys(self) -> tuple[str, ...]:
        return (
            self._TAB_DASHBOARD,
            self._TAB_STARTUP,
            self._TAB_KAKAO,
            self._TAB_WRIKE,
            self._TAB_CODEX,
        )

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
            if self._tab_dashboard is not None and cur == str(self._tab_dashboard):
                new_tab = self._TAB_DASHBOARD
            elif self._tab_startup is not None and cur == str(self._tab_startup):
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

            if new_tab == self._TAB_DASHBOARD:
                self._ensure_dashboard_built()
            elif new_tab == self._TAB_STARTUP:
                self._ensure_startup_built()
            elif new_tab == self._TAB_KAKAO:
                self._ensure_kakao_built()
            elif new_tab == self._TAB_WRIKE:
                self._ensure_wrike_built()
            elif new_tab == self._TAB_CODEX:
                self._ensure_codex_built()

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
            setter(self._refresh_dashboard_status)
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
            "codex.toggle": self._dashboard_codex_toggle_enabled,
            "codex.settings": self.show_codex_usage,
            "kakao.toggle": self._dashboard_kakao_toggle_enabled,
            "kakao.settings": self.show_kakao_monitor,
            "wrike.toggle": self._dashboard_wrike_toggle_enabled,
            "wrike.settings": self.show_wrike,
            "background.toggle": self._dashboard_background_toggle_enabled,
            "update.check": self._dashboard_update_check,
        }

    def _run_bg(self, fn) -> None:
        if not callable(fn):
            return
        try:
            threading.Thread(target=fn, daemon=True).start()
        except Exception:
            pass
        return

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

    def _get_codex_usage_monitor(self):
        try:
            return self._monitor.get_codex_usage_monitor()
        except Exception:
            return None

    def _dashboard_codex_toggle_enabled(self) -> None:
        codex = self._get_codex_usage_monitor()
        if codex is None:
            return
        try:
            settings = codex.get_settings_snapshot()
            if not isinstance(settings, dict):
                settings = {}
            settings["enabled"] = not bool(settings.get("enabled", True))
            codex.update_settings(settings)
        except Exception:
            pass
        return

    def _dashboard_codex_current_usage(self) -> None:
        codex = self._get_codex_usage_monitor()
        if codex is None:
            return
        try:
            codex.show_current_status(force_refresh=True)
        except Exception:
            pass
        return

    def _dashboard_codex_login(self) -> None:
        codex = self._get_codex_usage_monitor()
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
        return {
            "startup": self._get_startup_dashboard_status(),
            "codex": self._get_codex_dashboard_status(),
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

    def _get_codex_dashboard_status(self) -> dict[str, Any]:
        codex = self._get_codex_usage_monitor()
        if codex is None:
            return {}
        out: dict[str, Any] = {}
        try:
            settings = codex.get_settings_snapshot()
            if isinstance(settings, dict):
                out.update(settings)
        except Exception:
            pass
        try:
            runtime = codex.get_runtime_status()
            if isinstance(runtime, dict):
                out.update(runtime)
        except Exception:
            pass
        return out

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
            self._codex_view = CodexUsageSettingsView(
                self._root,
                codex,
                ui_post=self._ui_post,
            )
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
