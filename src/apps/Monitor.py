import json

from src.utils.LibConnector import LibConnector
import threading

from src.apps.Notion import Notion
from src.apps.Wrike import Wrike
from src.apps.KakaoManager import KakaoManager
from src.apps.LiJaMong import LiJaMong
from src.apps.codex_usage_multi_monitor import CodexUsageMultiMonitor


class Monitor:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        self.__notion = None
        self.__wrike = None
        self.__kakao = None
        self.__lijamong = None
        self.__codex_usage = None

        self.__root = None
        self.__event_queue = None
        self.__background_settings_path = self.__get_background_settings_path()
        self.__background_enabled = self.__load_background_enabled()
        self.__hotkeys_registered = False
        self.__features_warmup_started = False
        self.__features_warmup_done = False
        self.__wrike_attached = False
        self.__lijamong_attached = False
        self.__codex_attached = False
        self.__component_lock = threading.Lock()

        self.__kakao_after_id = None
        self.__kakao_tick_ms = 200
        self.__foreground_hotkey_after_id = None
        self.__foreground_hotkey_tick_ms = 200
        self.__foreground_hotkey_profile = None
        self.__foreground_hotkey_handles = []

        return

    def attach(self, root, event_queue) -> None:
        self.__root = root
        self.__event_queue = event_queue
        if not self.__background_enabled:
            return
        if not self.__hotkeys_registered:
            try:
                self.__register_hotkeys()
                self.__hotkeys_registered = True
            except Exception:
                self.__hotkeys_registered = False
        self.__start_foreground_hotkey_poll()
        self.__start_feature_warmup_async()
        return

    def on_session_unlock(self) -> None:
        if not self.__background_enabled:
            return
        self.__reset_hotkeys()
        self.on_display_topology_changed("session_unlock")
        return

    def on_display_topology_changed(self, reason: str = "display_change") -> None:
        if not self.__background_enabled:
            return
        root = self.__root
        normalized_reason = str(reason or "display_change")
        try:
            codex = self.__codex_usage
            handler = getattr(codex, "on_display_topology_changed", None)
            if callable(handler):
                handler(normalized_reason)
        except Exception:
            pass
        try:
            self.__ensure_kakao().invalidate_display_topology(
                root=root,
                reason=normalized_reason,
            )
        except Exception:
            pass
        return

    def __ui_post(self, fn) -> None:
        q = self.__event_queue
        if q is None:
            return
        try:
            q.put(fn)
        except Exception:
            return
        return

    def __ensure_notion(self):
        if self.__notion is not None:
            try:
                self.__notion.set_ui_post(self.__ui_post)
            except Exception:
                pass
            return self.__notion
        with self.__component_lock:
            if self.__notion is None:
                self.__notion = Notion()
            try:
                self.__notion.set_ui_post(self.__ui_post)
            except Exception:
                pass
        return self.__notion

    def __ensure_wrike(self):
        if self.__wrike is not None:
            return self.__wrike
        with self.__component_lock:
            if self.__wrike is None:
                self.__wrike = Wrike()
        return self.__wrike

    def __ensure_kakao(self):
        if self.__kakao is not None:
            try:
                self.__kakao.set_ui_post(self.__ui_post)
            except Exception:
                pass
            return self.__kakao
        with self.__component_lock:
            if self.__kakao is None:
                self.__kakao = KakaoManager()
            try:
                self.__kakao.set_ui_post(self.__ui_post)
            except Exception:
                pass
        return self.__kakao

    def __ensure_lijamong(self):
        if self.__lijamong is not None:
            return self.__lijamong
        with self.__component_lock:
            if self.__lijamong is None:
                self.__lijamong = LiJaMong()
        return self.__lijamong

    def __ensure_codex_usage(self):
        if self.__codex_usage is not None:
            return self.__codex_usage
        with self.__component_lock:
            if self.__codex_usage is None:
                self.__codex_usage = CodexUsageMultiMonitor()
        return self.__codex_usage

    def __attach_features_on_ui_thread(self) -> None:
        if not self.__background_enabled:
            return
        root = self.__root
        if root is None:
            return
        try:
            self.__start_kakao_tick()
        except Exception:
            pass
        try:
            self.__ensure_kakao().request_refresh(root)
        except Exception:
            pass
        try:
            if not self.__lijamong_attached:
                self.__ensure_lijamong().attach(root, self.__event_queue)
                self.__lijamong_attached = True
        except Exception:
            pass
        try:
            if not self.__wrike_attached:
                self.__ensure_wrike().attach(root)
                self.__wrike_attached = True
        except Exception:
            pass
        try:
            if not self.__codex_attached:
                self.__ensure_codex_usage().attach(root, self.__event_queue)
                self.__codex_attached = True
        except Exception:
            pass
        return

    def __start_feature_warmup_async(self) -> None:
        if not self.__background_enabled:
            return
        if self.__features_warmup_started:
            self.__ui_post(self.__attach_features_on_ui_thread)
            return
        self.__features_warmup_started = True

        def worker() -> None:
            try:
                self.__ensure_wrike()
                self.__ensure_kakao()
                self.__ensure_lijamong()
                self.__ensure_codex_usage()
            except Exception:
                pass
            finally:
                self.__features_warmup_done = True
                self.__ui_post(self.__attach_features_on_ui_thread)
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self.__features_warmup_done = False
            self.__features_warmup_started = False
            self.__ui_post(self.__attach_features_on_ui_thread)
        return

    def __register_hotkeys(self) -> None:
        if not self.__background_enabled:
            return
        kb = self.__lib.keyboard

        def safe(cb):
            def _inner(*_args, **_kwargs):
                try:
                    cb()
                except Exception:
                    return

            return _inner

        kb.add_hotkey("ctrl+alt+c", safe(self.__on_ctrl_alt_c), suppress=False)
        kb.add_hotkey("ctrl+alt+k", safe(self.__on_ctrl_alt_k), suppress=False)
        kb.add_hotkey("ctrl+alt+w", safe(self.__on_ctrl_alt_w), suppress=False)
        return

    def __reset_hotkeys(self) -> None:
        kb = self.__lib.keyboard
        try:
            kb.unhook_all()
        except Exception:
            pass
        self.__clear_keyboard_state(kb)
        self.__foreground_hotkey_handles = []
        self.__foreground_hotkey_profile = None
        self.__hotkeys_registered = False
        if not self.__background_enabled:
            return
        try:
            self.__register_hotkeys()
            self.__hotkeys_registered = True
        except Exception:
            self.__hotkeys_registered = False
        try:
            self.__sync_foreground_hotkeys()
        except Exception:
            pass
        return

    def __start_foreground_hotkey_poll(self) -> None:
        if not self.__background_enabled:
            return
        root = self.__root
        if root is None:
            return
        if self.__foreground_hotkey_after_id is not None:
            return
        try:
            self.__foreground_hotkey_after_id = root.after(
                self.__foreground_hotkey_tick_ms,
                self.__poll_foreground_hotkeys,
            )
        except Exception:
            self.__foreground_hotkey_after_id = None
        return

    def __poll_foreground_hotkeys(self) -> None:
        self.__foreground_hotkey_after_id = None
        if not self.__background_enabled:
            return
        try:
            self.__sync_foreground_hotkeys()
        except Exception:
            pass
        self.__start_foreground_hotkey_poll()
        return

    def __detect_foreground_hotkey_profile(self) -> str | None:
        try:
            wrike = self.__ensure_wrike()
            if wrike is not None and wrike.is_wrike_active():
                return "wrike"
        except Exception:
            pass
        try:
            notion = self.__ensure_notion()
            if notion is not None and notion.is_notion_active():
                return "notion"
        except Exception:
            pass
        return None

    def __sync_foreground_hotkeys(self, profile="__detect__") -> None:
        if profile == "__detect__":
            profile = self.__detect_foreground_hotkey_profile()
        if profile not in ("wrike", "notion", None):
            profile = None
        if profile == self.__foreground_hotkey_profile:
            return

        kb = self.__lib.keyboard
        for _combo, handle in list(self.__foreground_hotkey_handles):
            try:
                kb.remove_hotkey(handle)
            except Exception:
                try:
                    kb.remove_hotkey(_combo)
                except Exception:
                    pass
        self.__foreground_hotkey_handles = []
        self.__foreground_hotkey_profile = profile

        def add(combo, cb) -> None:
            try:
                handle = kb.add_hotkey(combo, self.__safe_hotkey(cb), suppress=False)
            except Exception:
                return
            self.__foreground_hotkey_handles.append((str(combo), handle))
            return

        if profile == "wrike":
            add("alt+q", self.__on_alt_q)
            add("ctrl+q", self.__on_ctrl_q)
        elif profile == "notion":
            add("ctrl+s", self.__on_ctrl_s)
        return

    def get_dashboard_status_snapshot(self) -> dict:
        return {
            "enabled": bool(self.__background_enabled),
            "root_attached": self.__root is not None,
            "event_queue_attached": self.__event_queue is not None,
            "hotkeys_registered": bool(self.__hotkeys_registered),
            "features_warmup_started": bool(self.__features_warmup_started),
            "features_warmup_done": bool(self.__features_warmup_done),
            "wrike_attached": bool(self.__wrike_attached),
            "lijamong_attached": bool(self.__lijamong_attached),
            "codex_attached": bool(self.__codex_attached),
            "foreground_hotkey_profile": str(self.__foreground_hotkey_profile or ""),
            "foreground_hotkey_count": len(list(self.__foreground_hotkey_handles)),
            "foreground_hotkey_poll_active": self.__foreground_hotkey_after_id is not None,
            "kakao_tick_active": self.__kakao_after_id is not None,
        }

    def set_background_enabled(self, enabled: bool) -> bool:
        next_enabled = bool(enabled)
        changed = next_enabled != bool(self.__background_enabled)
        self.__background_enabled = next_enabled
        self.__save_background_enabled()
        if not changed:
            return bool(self.__background_enabled)
        if self.__background_enabled:
            self.__start_background_tasks()
        else:
            self.__stop_background_tasks()
        return bool(self.__background_enabled)

    def __get_background_settings_path(self) -> str:
        try:
            appdata = self.__lib.os.environ.get("APPDATA")
            base_dir = appdata if appdata else self.__lib.os.path.expanduser("~")
            return self.__lib.os.path.join(
                base_dir,
                "windows-supporter",
                "background_settings.json",
            )
        except Exception:
            return "background_settings.json"

    def __load_background_enabled(self) -> bool:
        try:
            path = str(self.__background_settings_path or "")
            if not path or not self.__lib.os.path.exists(path):
                return True
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return bool(data.get("enabled", True)) if isinstance(data, dict) else True
        except Exception:
            return True

    def __save_background_enabled(self) -> None:
        try:
            path = str(self.__background_settings_path or "")
            if not path:
                return
            self.__lib.os.makedirs(self.__lib.os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"enabled": bool(self.__background_enabled)}, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
        except Exception:
            return
        return

    def __start_background_tasks(self) -> None:
        if self.__root is None:
            return
        if not self.__hotkeys_registered:
            try:
                self.__register_hotkeys()
                self.__hotkeys_registered = True
            except Exception:
                self.__hotkeys_registered = False
        self.__start_foreground_hotkey_poll()
        self.__start_feature_warmup_async()
        self.__ui_post(self.__attach_features_on_ui_thread)
        return

    def __stop_background_tasks(self) -> None:
        root = self.__root
        if root is not None and self.__foreground_hotkey_after_id is not None:
            try:
                root.after_cancel(self.__foreground_hotkey_after_id)
            except Exception:
                pass
        if root is not None and self.__kakao_after_id is not None:
            try:
                root.after_cancel(self.__kakao_after_id)
            except Exception:
                pass
        self.__foreground_hotkey_after_id = None
        self.__kakao_after_id = None
        try:
            self.__lib.keyboard.unhook_all()
        except Exception:
            pass
        self.__clear_keyboard_state(self.__lib.keyboard)
        self.__foreground_hotkey_handles = []
        self.__foreground_hotkey_profile = None
        self.__hotkeys_registered = False
        return

    def __safe_hotkey(self, cb):
        def _inner(*_args, **_kwargs):
            try:
                cb()
            except Exception:
                return

        return _inner

    def __clear_keyboard_state(self, kb) -> None:
        try:
            kb.stash_state()
        except Exception:
            pass
        try:
            pressed = getattr(kb, "_pressed_events", None)
            if isinstance(pressed, dict):
                pressed.clear()
        except Exception:
            pass
        try:
            listener = getattr(kb, "_listener", None)
            if listener is None:
                return
            active_modifiers = getattr(listener, "active_modifiers", None)
            if isinstance(active_modifiers, set):
                active_modifiers.clear()
            modifier_states = getattr(listener, "modifier_states", None)
            if isinstance(modifier_states, dict):
                modifier_states.clear()
            filtered_modifiers = getattr(listener, "filtered_modifiers", None)
            if hasattr(filtered_modifiers, "clear"):
                filtered_modifiers.clear()
        except Exception:
            pass
        return

    def __on_ctrl_alt_c(self) -> None:
        def ui_task() -> None:
            root = self.__root
            if root is None:
                return
            try:
                codex = self.__ensure_codex_usage()
                if codex is None:
                    return
                if not self.__codex_attached:
                    codex.attach(root, self.__event_queue)
                    self.__codex_attached = True
                codex.show_current_status(force_refresh=True)
            except Exception:
                return
            return

        self.__ui_post(ui_task)
        return

    def __on_ctrl_alt_k(self) -> None:
        kakao = self.__ensure_kakao()

        def ui_task() -> None:
            root = self.__root
            if root is None:
                return
            try:
                ui = getattr(root, "_ws_main_ui", None)
            except Exception:
                ui = None
            try:
                if ui is not None:
                    ui.show()
                else:
                    kakao.open_monitor_selector(root)
            except Exception:
                return
            return

        self.__ui_post(ui_task)
        return

    def __on_ctrl_alt_w(self) -> None:
        wrike = self.__ensure_wrike()

        def ui_task() -> None:
            root = self.__root
            if root is None:
                return
            try:
                if not self.__wrike_attached:
                    wrike.attach(root)
                    self.__wrike_attached = True
                wrike.show_weekly_timelog_summary(root)
            except Exception:
                return
            return

        self.__ui_post(ui_task)
        return

    def __on_alt_q(self) -> None:
        wrike = self.__ensure_wrike()

        def ui_task() -> None:
            root = self.__root
            if root is None:
                return
            try:
                if not self.__wrike_attached:
                    wrike.attach(root)
                    self.__wrike_attached = True
                if wrike.is_wrike_active():
                    wrike.run_action_async(root)
            except Exception:
                return
            return

        self.__ui_post(ui_task)
        return

    def __on_ctrl_q(self) -> None:
        wrike = self.__ensure_wrike()

        def ui_task() -> None:
            root = self.__root
            if root is None:
                return
            try:
                if not self.__wrike_attached:
                    wrike.attach(root)
                    self.__wrike_attached = True
                if wrike.is_wrike_active():
                    wrike.open_in_separate_tab_async(root)
            except Exception:
                return
            return

        self.__ui_post(ui_task)
        return

    def __on_ctrl_s(self) -> None:
        notion = self.__ensure_notion()

        def ui_task() -> None:
            root = self.__root
            if root is None:
                return
            try:
                if notion.is_notion_active():
                    notion.run_action_async(root)
            except Exception:
                return
            return

        self.__ui_post(ui_task)
        return

    def __start_kakao_tick(self) -> None:
        if not self.__background_enabled:
            return
        root = self.__root
        if root is None:
            return
        if self.__kakao_after_id is not None:
            return
        try:
            self.__kakao_after_id = root.after(self.__kakao_tick_ms, self.__tick_kakao)
        except Exception:
            self.__kakao_after_id = None
        return

    def __tick_kakao(self) -> None:
        root = self.__root
        if root is None:
            return
        self.__kakao_after_id = None
        if not self.__background_enabled:
            return
        try:
            self.__ensure_kakao().tick(root)
        except Exception:
            pass
        try:
            self.__kakao_after_id = root.after(self.__kakao_tick_ms, self.__tick_kakao)
        except Exception:
            self.__kakao_after_id = None
        return

    def open_kakao_monitor_selector(self, root) -> None:
        kakao = self.__ensure_kakao()
        try:
            ui = getattr(root, "_ws_main_ui", None)
            if ui is not None:
                ui.show()
                return
            kakao.open_monitor_selector(root)
        except Exception:
            return
        return

    def get_kakao_manager(self):
        return self.__ensure_kakao()

    def get_wrike(self):
        return self.__ensure_wrike()

    def get_codex_usage_monitor(self):
        return self.__ensure_codex_usage()
