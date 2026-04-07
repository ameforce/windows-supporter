from __future__ import annotations

import threading
from typing import Any


class CodexUsageSettingsView:
    def __init__(self, root: Any, codex_monitor: Any) -> None:
        self._root = root
        self._codex = codex_monitor

        self._tk = None
        self._ttk = None
        self._win = None

        self._enabled_var = None
        self._interval_var = None
        self._tooltip_var = None
        self._usage_url_var = None
        self._status_var = None
        self._status_label = None
        self._login_button = None
        self._logout_button = None
        self._runtime_after_id = None
        self._collect_state_var = None
        self._next_collect_var = None
        self._live_time_var = None
        self._live_five_hour_var = None
        self._live_weekly_var = None
        self._live_code_review_var = None
        self._live_credit_var = None
        self._status_colors = {
            "info": "#6B7280",
            "ok": "#10B981",
            "error": "#DC2626",
        }
        return

    def mount(self, parent: Any) -> None:
        if parent is None:
            return
        self._lazy_import_tk()
        self._stop_runtime_refresh()
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return
        try:
            for w in list(parent.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    continue
        except Exception:
            pass

        bg = "#F3F4F6"
        card_bg = "#FFFFFF"
        border = "#E5E7EB"
        text_muted = "#6B7280"

        container = tk.Frame(parent, bg=bg)
        try:
            container.pack(fill="both", expand=True)
        except Exception:
            return
        self._win = container

        self._status_var = tk.StringVar(value="")

        header_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        header_card.pack(fill="x", padx=12, pady=(12, 8))

        header_inner = tk.Frame(header_card, bg=card_bg)
        header_inner.pack(fill="x", padx=14, pady=12)

        title_row = tk.Frame(header_inner, bg=card_bg)
        title_row.pack(fill="x")

        tk.Label(
            title_row,
            text="Codex Usage Monitoring 설정",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        btn_row = tk.Frame(title_row, bg=card_bg)
        btn_row.pack(side="right")
        ttk.Button(btn_row, text="저장", command=self._on_save).pack(side="right")
        ttk.Button(btn_row, text="로드하기", command=self._on_reload).pack(
            side="right", padx=(0, 8)
        )
        self._logout_button = ttk.Button(btn_row, text="로그아웃", command=self._on_release_profile)
        self._logout_button.pack(
            side="right", padx=(0, 8)
        )
        self._login_button = ttk.Button(btn_row, text="로그인", command=self._on_login)
        self._login_button.pack(
            side="right", padx=(0, 8)
        )

        tk.Label(
            header_inner,
            text="Codex 사용량 자동 모니터링 동작을 설정합니다.",
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(6, 0))

        self._status_label = tk.Label(
            header_inner,
            textvariable=self._status_var,
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
        )
        self._status_label.pack(anchor="w", pady=(6, 0))

        content_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        content_card.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        body = tk.Frame(content_card, bg=card_bg)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        body.columnconfigure(1, weight=1)

        self._enabled_var = tk.BooleanVar(value=False)
        self._interval_var = tk.StringVar(value="")
        self._tooltip_var = tk.StringVar(value="")
        self._usage_url_var = tk.StringVar(value="")
        self._collect_state_var = tk.StringVar(value="-")
        self._next_collect_var = tk.StringVar(value="-")
        self._live_time_var = tk.StringVar(value="-")
        self._live_five_hour_var = tk.StringVar(value="-")
        self._live_weekly_var = tk.StringVar(value="-")
        self._live_code_review_var = tk.StringVar(value="-")
        self._live_credit_var = tk.StringVar(value="-")

        row = 0

        def add_label(text: str) -> None:
            nonlocal row
            tk.Label(
                body,
                text=text,
                bg=card_bg,
                fg="#111827",
                font=("Segoe UI", 9),
            ).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=6)

        def add_entry(var, width: int = 50):
            nonlocal row
            entry = ttk.Entry(body, textvariable=var, width=width)
            entry.grid(row=row, column=1, sticky="we", pady=6)
            return entry

        add_label("모니터링 활성화")
        tk.Checkbutton(
            body,
            variable=self._enabled_var,
            bg=card_bg,
            activebackground=card_bg,
            selectcolor=card_bg,
            fg="#111827",
            activeforeground="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=1, sticky="w", pady=6)
        row += 1

        add_label("모니터링 주기(초)")
        add_entry(self._interval_var)
        row += 1

        add_label("툴팁 표시 시간(초)")
        add_entry(self._tooltip_var)
        row += 1

        add_label("Usage URL")
        add_entry(self._usage_url_var, width=64)
        row += 1

        settings = self._safe_get_settings()
        settings_path = str(settings.get("settings_path", "") or "").strip()
        state_path = str(settings.get("state_path", "") or "").strip()
        profile_dir = str(settings.get("profile_dir", "") or "").strip()

        settings_label = tk.Label(
            body,
            text=f"설정 파일: {settings_path}" if settings_path else "설정 파일: (알 수 없음)",
            bg=card_bg,
            fg="#2563EB" if settings_path else text_muted,
            font=("Segoe UI", 9),
            cursor="hand2" if settings_path else "",
        )
        settings_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        if settings_path:
            try:
                settings_label.bind("<Button-1>", lambda _e: self._open_path(settings_path))
            except Exception:
                pass
        row += 1

        tk.Label(
            body,
            text=f"상태 파일: {state_path}" if state_path else "상태 파일: (알 수 없음)",
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=860,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        row += 1

        tk.Label(
            body,
            text=f"프로필 경로: {profile_dir}" if profile_dir else "프로필 경로: (알 수 없음)",
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=860,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        row += 1

        tk.Frame(body, bg=border, height=1).grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="we",
            pady=(10, 8),
        )
        row += 1

        tk.Label(
            body,
            text="실시간 상태",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        row += 1

        self._add_value_row(body, row, "조회 상태", self._collect_state_var, card_bg)
        row += 1
        self._add_value_row(body, row, "다음 모니터링까지", self._next_collect_var, card_bg)
        row += 1
        self._add_value_row(body, row, "최근 확인 시각", self._live_time_var, card_bg)
        row += 1
        self._add_value_row(body, row, "5시간 사용 한도", self._live_five_hour_var, card_bg)
        row += 1
        self._add_value_row(body, row, "주간 사용 한도", self._live_weekly_var, card_bg)
        row += 1
        self._add_value_row(body, row, "코드 검토", self._live_code_review_var, card_bg)
        row += 1
        self._add_value_row(body, row, "남은 크레딧", self._live_credit_var, card_bg)

        self._load_settings()
        self._start_runtime_refresh()
        return

    def _add_value_row(self, parent: Any, row: int, label: str, value_var, bg: str) -> None:
        tk = self._tk
        if tk is None:
            return
        tk.Label(
            parent,
            text=label,
            bg=bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
        tk.Label(
            parent,
            textvariable=value_var,
            bg=bg,
            fg="#1F2937",
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=row, column=1, sticky="we", pady=2)
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

    def _safe_get_settings(self) -> dict[str, Any]:
        try:
            settings = self._codex.get_settings_snapshot()
        except Exception:
            settings = {}
        return settings if isinstance(settings, dict) else {}

    def _format_seconds(self, seconds: float) -> str:
        try:
            seconds = float(seconds)
        except Exception:
            return "0"
        if seconds <= 0:
            return "0"
        if abs(seconds - int(seconds)) < 1e-6:
            return str(int(seconds))
        return f"{seconds:.1f}".rstrip("0").rstrip(".")

    def _load_settings(self) -> None:
        settings = self._safe_get_settings()
        try:
            self._enabled_var.set(bool(settings.get("enabled", True)))
        except Exception:
            pass
        try:
            interval = float(settings.get("interval_sec", 90.0))
            self._interval_var.set(self._format_seconds(interval))
        except Exception:
            pass
        try:
            tooltip_ms = int(settings.get("tooltip_duration_ms", 7000))
            self._tooltip_var.set(self._format_seconds(float(tooltip_ms) / 1000.0))
        except Exception:
            pass
        try:
            self._usage_url_var.set(str(settings.get("usage_url", "") or ""))
        except Exception:
            pass
        self._set_status("", level="info")
        return

    def _on_reload(self) -> None:
        self._load_settings()
        self._set_status("로드 완료", level="ok")
        return

    def _on_login(self) -> None:
        if not hasattr(self._codex, "show_current_status"):
            self._set_status("로그인 기능을 사용할 수 없습니다.", level="error")
            return
        try:
            runtime = self._safe_get_runtime()
            if bool(runtime.get("logout_in_progress", False)):
                self._set_status("로그아웃 진행 중입니다. 완료 후 다시 시도해 주세요.", level="info")
                return
            can_login = bool(runtime.get("can_login", True))
            if not can_login:
                self._set_status("현재 상태에서는 로그인 요청을 시작할 수 없습니다.", level="info")
                return
        except Exception:
            pass
        self._set_status("로그인/조회 요청을 시작합니다...", level="info")
        try:
            self._codex.show_current_status(force_refresh=True)
        except Exception:
            self._set_status("로그인 요청 중 오류가 발생했습니다.", level="error")
            return
        return

    def _on_release_profile(self) -> None:
        tk = self._tk
        if tk is None:
            return
        if not hasattr(self._codex, "release_profile_session"):
            self._set_status("로그아웃 기능을 사용할 수 없습니다.", level="error")
            return
        confirmed = True
        try:
            from tkinter import messagebox

            confirmed = bool(
                messagebox.askyesno(
                    "로그아웃",
                    "현재 Codex 로그인 세션에서 로그아웃하시겠습니까?\n"
                    "로그아웃 후에는 로그인 버튼 또는 Ctrl+Alt+C로 다시 로그인할 수 있습니다.",
                    parent=self._win,
                )
            )
        except Exception:
            confirmed = False
        if not confirmed:
            return

        self._set_status("로그아웃 중...", level="info")

        def worker() -> None:
            ok = False
            message = ""
            try:
                ok, message = self._codex.release_profile_session()
            except Exception:
                ok = False
                message = "로그아웃 중 오류가 발생했습니다."
            if not message:
                message = "로그아웃이 완료되었습니다." if ok else "로그아웃에 실패했습니다."

            def done() -> None:
                if ok:
                    self._load_settings()
                    self._refresh_runtime_status()
                    self._set_status(message, level="ok")
                    return
                self._set_status(message, level="error")
                return

            win = self._win
            if win is not None:
                try:
                    win.after(0, done)
                    return
                except Exception:
                    pass
            done()
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._set_status("로그아웃 작업을 시작하지 못했습니다.", level="error")
        return

    def _parse_seconds(self, text: str, default: float) -> float:
        raw = str(text or "").strip()
        if not raw:
            return float(default)
        try:
            value = float(raw)
        except Exception:
            return float(default)
        if value <= 0:
            return float(default)
        return float(value)

    def _on_save(self) -> None:
        enabled = bool(self._enabled_var.get())
        interval_sec = self._parse_seconds(self._interval_var.get(), default=90.0)
        tooltip_sec = self._parse_seconds(self._tooltip_var.get(), default=7.0)
        usage_url = str(self._usage_url_var.get() or "").strip()

        ok, err = self._codex.update_settings(
            {
                "enabled": enabled,
                "interval_sec": interval_sec,
                "tooltip_duration_ms": int(round(tooltip_sec * 1000.0)),
                "usage_url": usage_url,
            }
        )
        if ok:
            self._set_status("저장 완료", level="ok")
            self._hide_main_ui()
            return
        self._set_status(f"저장 실패: {err}", level="error")
        return

    def _set_status(self, text: str, level: str = "info") -> None:
        label = self._status_label
        if label is None or self._status_var is None:
            return
        try:
            self._status_var.set(str(text or ""))
        except Exception:
            return
        color = self._status_colors.get(level, self._status_colors["info"])
        try:
            label.configure(fg=color)
        except Exception:
            pass
        return

    def _safe_get_runtime(self) -> dict[str, Any]:
        try:
            payload = self._codex.get_runtime_status()
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _start_runtime_refresh(self) -> None:
        self._stop_runtime_refresh()
        self._refresh_runtime_status()
        return

    def _stop_runtime_refresh(self) -> None:
        after_id = self._runtime_after_id
        self._runtime_after_id = None
        if not after_id:
            return
        win = self._win
        if win is None:
            return
        try:
            win.after_cancel(after_id)
        except Exception:
            pass
        return

    def _schedule_runtime_refresh(self, delay_ms: int = 1000) -> None:
        win = self._win
        if win is None:
            return
        try:
            self._runtime_after_id = win.after(int(max(300, delay_ms)), self._refresh_runtime_status)
        except Exception:
            self._runtime_after_id = None
        return

    def _refresh_runtime_status(self) -> None:
        win = self._win
        if win is None:
            return
        runtime = self._safe_get_runtime()
        session_state = str(runtime.get("session_state", "logged_out") or "logged_out")
        monitor_state = str(runtime.get("monitor_state", "idle") or "idle")
        logout_in_progress = bool(runtime.get("logout_in_progress", False))
        profile_in_use = bool(runtime.get("profile_in_use", False))
        try:
            inflight = bool(runtime.get("collect_inflight", False))
        except Exception:
            inflight = False
        source = str(runtime.get("collect_source", "") or "")
        if logout_in_progress or monitor_state == "cancelling":
            state = "로그아웃 중"
        elif inflight:
            state = "조회 중"
            if source:
                state = f"조회 중 ({source})"
        elif profile_in_use or monitor_state == "paused_profile_in_use":
            state = "프로필 사용 중 (자동 일시중지)"
        elif session_state == "logged_out":
            state = "로그인 필요"
        else:
            state = "대기 중"

        remain_text = "-"
        remain = runtime.get("next_collect_in_sec", None)
        is_estimated = bool(runtime.get("next_collect_estimated", False))
        try:
            if (
                remain is not None
                and session_state != "logged_out"
                and not profile_in_use
                and not inflight
            ):
                seconds = float(remain)
                if seconds < 0:
                    seconds = 0.0
                remain_text = f"{int(seconds)}초"
                if is_estimated:
                    remain_text = f"약 {remain_text}"
        except Exception:
            remain_text = "-"

        snapshot = None
        try:
            snapshot = self._codex.get_last_snapshot()
        except Exception:
            snapshot = None
        payload = {}
        try:
            if snapshot is not None and hasattr(snapshot, "to_dict"):
                payload = snapshot.to_dict()
        except Exception:
            payload = {}

        def _val(key: str) -> str:
            raw = str(payload.get(key, "") or "").strip()
            return raw if raw else "-"

        def _fmt_time(value: str) -> str:
            raw = str(value or "").strip()
            if not raw:
                return "-"
            try:
                formatter = getattr(self._codex, "format_captured_at_for_display", None)
                if callable(formatter):
                    rendered = str(formatter(raw) or "").strip()
                    return rendered if rendered else "-"
            except Exception:
                pass
            return raw

        def _metric_text(key: str) -> str:
            if inflight:
                return "조회 중..."
            return _val(key)

        try:
            self._collect_state_var.set(state)
            self._next_collect_var.set(remain_text)
            if inflight:
                self._live_time_var.set("조회 중...")
            else:
                self._live_time_var.set(_fmt_time(_val("captured_at")))
            self._live_five_hour_var.set(_metric_text("five_hour_limit"))
            self._live_weekly_var.set(_metric_text("weekly_limit"))
            self._live_code_review_var.set(_metric_text("code_review"))
            self._live_credit_var.set(_metric_text("remaining_credit"))
        except Exception:
            pass

        self._refresh_action_buttons(runtime=runtime)
        self._schedule_runtime_refresh(1000)
        return

    def _refresh_action_buttons(self, runtime: dict[str, Any]) -> None:
        login_button = self._login_button
        logout_button = self._logout_button
        try:
            can_login = bool(runtime.get("can_login", False))
        except Exception:
            can_login = False
        try:
            can_logout = bool(runtime.get("can_logout", False))
        except Exception:
            can_logout = False
        self._set_button_enabled(login_button, can_login)
        self._set_button_enabled(logout_button, can_logout)
        return

    def _set_button_enabled(self, button: Any, enabled: bool) -> None:
        if button is None:
            return
        try:
            if bool(enabled):
                button.state(["!disabled"])
            else:
                button.state(["disabled"])
            return
        except Exception:
            pass
        try:
            button.configure(state="normal" if bool(enabled) else "disabled")
        except Exception:
            pass
        return

    def _open_path(self, path: str) -> None:
        try:
            import os

            if path and os.path.isfile(path):
                os.startfile(path)
        except Exception:
            return

    def _hide_main_ui(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            ui = getattr(root, "_ws_main_ui", None)
        except Exception:
            ui = None
        if ui is not None:
            try:
                ui.hide()
                return
            except Exception:
                pass
        try:
            root.withdraw()
        except Exception:
            pass
        return
