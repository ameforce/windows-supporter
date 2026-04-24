from src.utils.LibConnector import LibConnector
import threading

from src.apps.Notion import Notion
from src.apps.Wrike import Wrike
from src.apps.KakaoManager import KakaoManager
from src.apps.LiJaMong import LiJaMong
from src.apps.codex_usage_monitor import CodexUsageMonitor


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
        self.__hotkeys_registered = False
        self.__features_warmup_started = False
        self.__features_warmup_done = False
        self.__wrike_attached = False
        self.__lijamong_attached = False
        self.__codex_attached = False
        self.__component_lock = threading.Lock()

        self.__kakao_after_id = None
        self.__kakao_tick_ms = 200

        return

    def attach(self, root, event_queue) -> None:
        self.__root = root
        self.__event_queue = event_queue
        if not self.__hotkeys_registered:
            try:
                self.__register_hotkeys()
                self.__hotkeys_registered = True
            except Exception:
                self.__hotkeys_registered = False
        self.__start_feature_warmup_async()
        return

    def on_session_unlock(self) -> None:
        self.__reset_hotkeys()
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
            return self.__notion
        with self.__component_lock:
            if self.__notion is None:
                self.__notion = Notion()
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
                self.__codex_usage = CodexUsageMonitor()
        return self.__codex_usage

    def __attach_features_on_ui_thread(self) -> None:
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
        kb.add_hotkey("alt+q", safe(self.__on_alt_q), suppress=False)
        kb.add_hotkey("ctrl+q", safe(self.__on_ctrl_q), suppress=False)
        kb.add_hotkey("ctrl+s", safe(self.__on_ctrl_s), suppress=False)
        return

    def __reset_hotkeys(self) -> None:
        kb = self.__lib.keyboard
        try:
            kb.unhook_all()
        except Exception:
            pass
        self.__clear_keyboard_state(kb)
        self.__hotkeys_registered = False
        try:
            self.__register_hotkeys()
            self.__hotkeys_registered = True
        except Exception:
            self.__hotkeys_registered = False
        return

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
                    ui.show_kakao_monitor()
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
                    wrike.action(root)
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
                    wrike.open_in_separate_tab(root)
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
                    notion.action(root)
            except Exception:
                return
            return

        self.__ui_post(ui_task)
        return

    def __start_kakao_tick(self) -> None:
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
                ui.show_kakao_monitor()
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
