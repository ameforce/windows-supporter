from __future__ import annotations

from typing import Any, Callable


class DashboardView:
    def __init__(
        self,
        root: Any,
        *,
        status_provider: Callable[[], dict[str, Any]],
        callbacks: dict[str, Callable[[], Any]],
    ) -> None:
        self._root = root
        self._status_provider = status_provider
        self._callbacks = dict(callbacks)
        self._parent = None
        self._status_frames: dict[str, Any] = {}
        self._toggle_buttons: dict[str, Any] = {}
        self._tk = None
        self._ttk = None
        return

    def mount(self, parent: Any) -> None:
        self._parent = parent
        if not self._lazy_import_tk():
            return

        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return
        self._status_frames = {}
        self._toggle_buttons = {}

        try:
            for child in list(parent.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    continue
        except Exception:
            pass

        bg = "#F3F4F6"
        card_bg = "#FFFFFF"
        border = "#E5E7EB"
        text = "#111827"

        try:
            parent.configure(bg=bg)
        except Exception:
            pass

        container = tk.Frame(parent, bg=bg)
        container.pack(fill="both", expand=True)

        header_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        header_card.pack(fill="x", padx=12, pady=(12, 8))

        header_inner = tk.Frame(header_card, bg=card_bg)
        header_inner.pack(fill="x", padx=14, pady=10)

        tk.Label(
            header_inner,
            text="Dashboard",
            bg=card_bg,
            fg=text,
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")
        ttk.Button(header_inner, text="새로고침", command=self.refresh).pack(side="right")

        content_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        content_card.pack(fill="x", expand=False, padx=12, pady=(0, 12))

        body = tk.Frame(content_card, bg=card_bg)
        body.pack(fill="x", expand=False, padx=14, pady=10)

        self._add_startup_section(body, text=text, bg=card_bg, border=border)
        self._add_separator(body, border=border, bg=card_bg)
        self._add_codex_section(body, text=text, bg=card_bg, border=border)
        self._add_separator(body, border=border, bg=card_bg)
        self._add_kakao_section(body, text=text, bg=card_bg, border=border)
        self._add_separator(body, border=border, bg=card_bg)
        self._add_wrike_section(body, text=text, bg=card_bg, border=border)
        self._add_separator(body, border=border, bg=card_bg)
        self._add_background_section(body, text=text, bg=card_bg, border=border)

        self.refresh()
        return

    def refresh(self) -> None:
        try:
            snapshot = self._status_provider()
        except Exception:
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}

        self._set_feature_status("startup", self._format_startup(snapshot.get("startup")))
        self._set_feature_status("codex", self._format_codex(snapshot.get("codex")))
        self._set_feature_status("kakao", self._format_kakao(snapshot.get("kakao")))
        self._set_feature_status("wrike", self._format_wrike(snapshot.get("wrike")))
        self._set_feature_status("background", self._format_background(snapshot.get("background")))
        return

    def _lazy_import_tk(self) -> bool:
        if self._tk is not None and self._ttk is not None:
            return True
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            self._tk = None
            self._ttk = None
            return False
        self._tk = tk
        self._ttk = ttk
        return True

    def _add_startup_section(
        self,
        parent: Any,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            key="startup",
            title="Startup Apps",
            text=text,
            bg=bg,
            border=border,
            settings_callback="startup.settings",
            toggle_callback="startup.toggle",
        )
        return

    def _add_codex_section(
        self,
        parent: Any,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            key="codex",
            title="Codex",
            text=text,
            bg=bg,
            border=border,
            settings_callback="codex.settings",
            toggle_callback="codex.toggle",
        )
        return

    def _add_kakao_section(
        self,
        parent: Any,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            key="kakao",
            title="KakaoTalk",
            text=text,
            bg=bg,
            border=border,
            settings_callback="kakao.settings",
            toggle_callback="kakao.toggle",
        )
        return

    def _add_wrike_section(
        self,
        parent: Any,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            key="wrike",
            title="Wrike",
            text=text,
            bg=bg,
            border=border,
            settings_callback="wrike.settings",
            toggle_callback="wrike.toggle",
        )
        return

    def _add_background_section(
        self,
        parent: Any,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            key="background",
            title="Background",
            text=text,
            bg=bg,
            border=border,
            settings_callback=None,
            toggle_callback="background.toggle",
        )
        return

    def _add_section(
        self,
        parent: Any,
        *,
        key: str,
        title: str,
        text: str,
        bg: str,
        border: str,
        settings_callback: str | None,
        toggle_callback: str,
    ) -> None:
        tk = self._tk
        ttk = self._ttk
        frame = tk.Frame(parent, bg=bg, cursor="hand2" if settings_callback else "")
        frame.pack(fill="x")
        title_label = tk.Label(
            frame,
            text=str(title),
            bg=bg,
            fg=text,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2" if settings_callback else "",
        )
        title_label.pack(anchor="w")
        row = tk.Frame(frame, bg=bg)
        row.pack(fill="x", pady=(3, 0))
        try:
            row.columnconfigure(0, weight=1)
        except Exception:
            pass
        status_frame = tk.Frame(row, bg=bg, cursor="hand2" if settings_callback else "")
        status_frame.grid(row=0, column=0, sticky="w", padx=(0, 10))
        btn = ttk.Button(
            row,
            text="활성화",
            width=10,
            command=lambda n=toggle_callback: self._invoke(n),
        )
        btn.grid(row=0, column=1, sticky="e")
        self._status_frames[str(key)] = status_frame
        self._toggle_buttons[str(key)] = btn
        if settings_callback:
            for widget in (frame, title_label, row, status_frame):
                self._bind_click(widget, settings_callback)
        return

    def _add_separator(self, parent: Any, *, border: str, bg: str) -> None:
        tk = self._tk
        tk.Frame(parent, bg=border, height=1).pack(fill="x", pady=7)
        return

    def _bind_click(self, widget: Any, callback_name: str) -> None:
        try:
            widget.bind("<Button-1>", lambda _e, n=callback_name: self._invoke(n))
        except Exception:
            pass
        return

    def _set_feature_status(self, key: str, formatted: tuple[bool, list[tuple[str, str]]]) -> None:
        try:
            enabled, parts = formatted
        except Exception:
            enabled = False
            parts = [("비활성화", "disabled"), ("상태 확인 불가", "normal")]
        self._set_toggle_button(key, bool(enabled))
        self._set_status_parts(key, parts)
        return

    def _set_toggle_button(self, key: str, enabled: bool) -> None:
        button = self._toggle_buttons.get(str(key))
        if button is None:
            return
        try:
            button.configure(text="비활성화" if bool(enabled) else "활성화")
        except Exception:
            pass
        return

    def _set_status_parts(self, key: str, parts: list[tuple[str, str]]) -> None:
        tk = self._tk
        frame = self._status_frames.get(str(key))
        if frame is None or tk is None:
            return
        try:
            for child in list(frame.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    continue
        except Exception:
            return
        for idx, (raw_text, kind) in enumerate(list(parts or [])):
            if idx > 0:
                tk.Label(
                    frame,
                    text=" | ",
                    bg="#FFFFFF",
                    fg="#6B7280",
                    font=("Segoe UI", 9),
                ).pack(side="left")
            fg = "#111827"
            if kind == "enabled":
                fg = "#059669"
            elif kind == "disabled":
                fg = "#DC2626"
            label = tk.Label(
                frame,
                text=str(raw_text),
                bg="#FFFFFF",
                fg=fg,
                font=("Segoe UI", 9, "bold") if kind in {"enabled", "disabled"} else ("Segoe UI", 9),
            )
            label.pack(
                side="left",
            )
            callback_name = f"{key}.settings"
            if callback_name in self._callbacks:
                try:
                    label.configure(cursor="hand2")
                except Exception:
                    pass
                self._bind_click(label, callback_name)
        return

    def _invoke(self, name: str) -> None:
        cb = self._callbacks.get(str(name))
        if not callable(cb):
            return
        try:
            cb()
        except Exception:
            pass
        self._schedule_refresh()
        return

    def _schedule_refresh(self) -> None:
        try:
            self._root.after(250, self.refresh)
            return
        except Exception:
            pass
        self.refresh()
        return

    def _format_startup(self, data: Any) -> tuple[bool, list[tuple[str, str]]]:
        if not isinstance(data, dict):
            return False, [("비활성화", "disabled"), ("상태 확인 불가", "normal")]
        is_enabled = bool(data.get("enabled", True))
        total = data.get("total_count", None)
        running = data.get("running_count", None)
        parts = [self._enabled_part(is_enabled)]
        if total is not None and running is not None:
            parts.append((f"실행 중: {running}/{total}", "normal"))
        return is_enabled, parts

    def _format_codex(self, data: Any) -> tuple[bool, list[tuple[str, str]]]:
        if not isinstance(data, dict):
            return False, [("비활성화", "disabled"), ("상태 확인 불가", "normal")]
        is_enabled = bool(data.get("enabled", True))
        runtime = str(data.get("monitor_state", "unknown") or "unknown")
        session = str(data.get("session_state", "unknown") or "unknown")
        return is_enabled, [
            self._enabled_part(is_enabled),
            (f"상태: {runtime}", "normal"),
            (f"세션: {session}", "normal"),
        ]

    def _format_kakao(self, data: Any) -> tuple[bool, list[tuple[str, str]]]:
        if not isinstance(data, dict):
            return False, [("비활성화", "disabled"), ("상태 확인 불가", "normal")]
        is_enabled = bool(data.get("enabled", True))
        tick = "실행 중" if bool(data.get("tick_active", False)) else "대기"
        target = data.get("target_display_num", None) or data.get("resolved_target_display_num", None)
        parts = [
            self._enabled_part(is_enabled),
            (f"모니터 감시: {tick}", "normal"),
        ]
        if target is not None:
            parts.append((f"대상 모니터: {target}", "normal"))
        return is_enabled, parts

    def _format_wrike(self, data: Any) -> tuple[bool, list[tuple[str, str]]]:
        if not isinstance(data, dict):
            return False, [("비활성화", "disabled"), ("상태 확인 불가", "normal")]
        is_enabled = bool(data.get("monitor_enabled", False))
        token = "설정됨" if bool(data.get("api_token_configured", False)) else "미설정"
        target = data.get("daily_target_minutes", 0)
        return is_enabled, [
            self._enabled_part(is_enabled),
            (f"API 토큰: {token}", "normal"),
            (f"일 목표: {self._format_minutes(target)}", "normal"),
        ]

    def _format_background(self, data: Any) -> tuple[bool, list[tuple[str, str]]]:
        if not isinstance(data, dict):
            return False, [("비활성화", "disabled"), ("상태 확인 불가", "normal")]
        is_enabled = bool(data.get("enabled", True))
        hotkeys = "등록됨" if bool(data.get("hotkeys_registered", False)) else "미등록"
        warmup = "완료" if bool(data.get("features_warmup_done", False)) else "진행/대기"
        profile = str(data.get("foreground_hotkey_profile", "") or "없음")
        attached = []
        for key, label in (
            ("wrike_attached", "Wrike"),
            ("codex_attached", "Codex"),
            ("lijamong_attached", "LiJaMong"),
        ):
            if bool(data.get(key, False)):
                attached.append(label)
        attached_text = ", ".join(attached) if attached else "없음"
        return is_enabled, [
            self._enabled_part(is_enabled),
            ("범위: 핫키/자동화", "normal"),
            (f"핫키: {hotkeys}", "normal"),
            (f"기능 준비: {warmup}", "normal"),
            (f"전경 프로필: {profile}", "normal"),
            (f"연결된 기능: {attached_text}", "normal"),
        ]

    def _enabled_part(self, enabled: bool) -> tuple[str, str]:
        if bool(enabled):
            return "활성화", "enabled"
        return "비활성화", "disabled"

    def _format_minutes(self, value: Any) -> str:
        try:
            minutes = max(0, int(round(float(value))))
        except Exception:
            minutes = 0
        hours = minutes // 60
        remain = minutes % 60
        if hours > 0 and remain > 0:
            return f"{hours}시간 {remain}분"
        if hours > 0:
            return f"{hours}시간"
        return f"{remain}분"
