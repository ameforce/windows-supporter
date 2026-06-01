from __future__ import annotations

import threading
from typing import Any


class CodexUsageSettingsView:
    def __init__(self, root: Any, codex_monitor: Any, ui_post=None) -> None:
        self._root = root
        self._codex = codex_monitor
        self._ui_post = ui_post if callable(ui_post) else None

        self._tk = None
        self._ttk = None
        self._win = None

        self._enabled_var = None
        self._taskbar_overlay_var = None
        self._interval_var = None
        self._tooltip_var = None
        self._usage_url_var = None
        self._status_var = None
        self._status_label = None
        self._login_button = None
        self._logout_button = None
        self._account_enabled_vars = {}
        self._account_login_buttons = {}
        self._account_logout_buttons = {}
        self._runtime_after_id = None
        self._collect_state_var = None
        self._next_collect_var = None
        self._live_time_var = None
        self._live_five_hour_var = None
        self._live_five_hour_reset_var = None
        self._live_weekly_var = None
        self._live_weekly_reset_var = None
        self._live_spark_five_hour_var = None
        self._live_spark_five_hour_reset_var = None
        self._live_spark_weekly_var = None
        self._live_spark_weekly_reset_var = None
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
        settings = self._safe_get_settings()
        accounts = settings.get("accounts")
        has_multi_accounts = isinstance(accounts, list) and bool(accounts)
        self._login_button = None
        self._logout_button = None
        self._account_login_buttons = {}
        self._account_logout_buttons = {}

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
        header_card.pack(fill="x", padx=8, pady=(8, 6))

        header_inner = tk.Frame(header_card, bg=card_bg)
        header_inner.pack(fill="x", padx=12, pady=8)

        title_row = tk.Frame(header_inner, bg=card_bg)
        title_row.pack(fill="x")

        tk.Label(
            title_row,
            text="Codex Usage Monitoring 설정",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")

        btn_row = tk.Frame(title_row, bg=card_bg)
        btn_row.pack(side="right")
        ttk.Button(btn_row, text="저장", command=self._on_save).pack(side="right")
        ttk.Button(btn_row, text="로드하기", command=self._on_reload).pack(
            side="right", padx=(0, 8)
        )
        if not has_multi_accounts:
            self._logout_button = ttk.Button(
                btn_row,
                text="로그아웃",
                command=self._on_release_profile,
            )
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
        ).pack(anchor="w", pady=(4, 0))

        self._status_label = tk.Label(
            header_inner,
            textvariable=self._status_var,
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
        )
        self._status_label.pack(anchor="w", pady=(3, 0))

        content_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        content_card.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        body = tk.Frame(content_card, bg=card_bg)
        body.pack(fill="both", expand=True, padx=12, pady=7)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(3, weight=1)

        self._enabled_var = tk.BooleanVar(value=False)
        self._taskbar_overlay_var = tk.BooleanVar(value=True)
        self._interval_var = tk.StringVar(value="")
        self._tooltip_var = tk.StringVar(value="")
        self._usage_url_var = tk.StringVar(value="")
        self._collect_state_var = tk.StringVar(value="-")
        self._next_collect_var = tk.StringVar(value="-")
        self._live_time_var = tk.StringVar(value="-")
        self._live_five_hour_var = tk.StringVar(value="-")
        self._live_five_hour_reset_var = tk.StringVar(value="-")
        self._live_weekly_var = tk.StringVar(value="-")
        self._live_weekly_reset_var = tk.StringVar(value="-")
        self._live_spark_five_hour_var = tk.StringVar(value="-")
        self._live_spark_five_hour_reset_var = tk.StringVar(value="-")
        self._live_spark_weekly_var = tk.StringVar(value="-")
        self._live_spark_weekly_reset_var = tk.StringVar(value="-")
        self._live_credit_var = tk.StringVar(value="-")

        row = 0

        tk.Label(
            body,
            text="모니터링",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        tk.Checkbutton(
            body,
            variable=self._enabled_var,
            bg=card_bg,
            activebackground=card_bg,
            selectcolor=card_bg,
            fg="#111827",
            activeforeground="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=1, sticky="w", pady=2)
        tk.Label(
            body,
            text="작업표시줄 표시",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=row, column=2, sticky="e", padx=(18, 8), pady=2)
        tk.Checkbutton(
            body,
            variable=self._taskbar_overlay_var,
            bg=card_bg,
            activebackground=card_bg,
            selectcolor=card_bg,
            fg="#111827",
            activeforeground="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=3, sticky="w", pady=2)
        row += 1

        tk.Label(
            body,
            text="주기(초)",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(body, textvariable=self._interval_var, width=12).grid(
            row=row,
            column=1,
            sticky="w",
            pady=2,
        )
        tk.Label(
            body,
            text="툴팁(초)",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=2, sticky="e", padx=(18, 8), pady=2)
        ttk.Entry(body, textvariable=self._tooltip_var, width=12).grid(
            row=row,
            column=3,
            sticky="w",
            pady=2,
        )
        row += 1

        tk.Label(
            body,
            text="Usage URL",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(body, textvariable=self._usage_url_var, width=64).grid(
            row=row,
            column=1,
            columnspan=3,
            sticky="we",
            pady=2,
        )
        row += 1

        settings_path = str(settings.get("settings_path", "") or "").strip()
        state_path = str(settings.get("state_path", "") or "").strip()
        profile_dir = str(settings.get("profile_dir", "") or "").strip()

        settings_label = tk.Label(
            body,
            text=(
                f"파일: 설정 {self._path_name(settings_path)}  |  "
                f"상태 {self._path_name(state_path)}  |  "
                f"프로필 {self._path_name(profile_dir)}"
            ),
            bg=card_bg,
            fg="#2563EB" if settings_path else text_muted,
            font=("Segoe UI", 8),
            cursor="hand2" if settings_path else "",
            anchor="w",
        )
        settings_label.grid(row=row, column=0, columnspan=4, sticky="we", pady=(2, 0))
        if settings_path:
            try:
                settings_label.bind("<Button-1>", lambda _e: self._open_path(settings_path))
            except Exception:
                pass
        row += 1

        if isinstance(accounts, list) and accounts:
            row = self._add_account_sections(body, row, accounts, card_bg, border, text_muted)

        tk.Frame(body, bg=border, height=1).grid(
            row=row,
            column=0,
            columnspan=4,
            sticky="we",
            pady=(6, 5),
        )
        row += 1

        tk.Label(
            body,
            text="실시간 상태",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 2))
        row += 1

        runtime_grid = tk.Frame(body, bg=card_bg)
        runtime_grid.grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 0))
        runtime_grid.columnconfigure(1, minsize=210)
        runtime_grid.columnconfigure(3, minsize=250)

        runtime_row = 0
        self._add_value_row(runtime_grid, runtime_row, "조회 상태", self._collect_state_var, card_bg)
        self._add_value_row(
            runtime_grid,
            runtime_row,
            "다음 모니터링까지",
            self._next_collect_var,
            card_bg,
            column=2,
        )
        runtime_row += 1
        self._add_value_row(runtime_grid, runtime_row, "최근 확인 시각", self._live_time_var, card_bg)
        self._add_value_row(
            runtime_grid,
            runtime_row,
            "남은 크레딧",
            self._live_credit_var,
            card_bg,
            column=2,
        )
        runtime_row += 1
        self._add_value_row(runtime_grid, runtime_row, "5시간 사용 한도", self._live_five_hour_var, card_bg)
        self._add_value_row(
            runtime_grid,
            runtime_row,
            "5시간 한도 초기화",
            self._live_five_hour_reset_var,
            card_bg,
            column=2,
        )
        runtime_row += 1
        self._add_value_row(runtime_grid, runtime_row, "주간 사용 한도", self._live_weekly_var, card_bg)
        self._add_value_row(
            runtime_grid,
            runtime_row,
            "주간 한도 초기화",
            self._live_weekly_reset_var,
            card_bg,
            column=2,
        )
        runtime_row += 1
        self._add_value_row(
            runtime_grid,
            runtime_row,
            "Spark 5시간 한도",
            self._live_spark_five_hour_var,
            card_bg,
        )
        self._add_value_row(
            runtime_grid,
            runtime_row,
            "Spark 5시간 초기화",
            self._live_spark_five_hour_reset_var,
            card_bg,
            column=2,
        )
        runtime_row += 1
        self._add_value_row(
            runtime_grid,
            runtime_row,
            "Spark 주간 한도",
            self._live_spark_weekly_var,
            card_bg,
        )
        self._add_value_row(
            runtime_grid,
            runtime_row,
            "Spark 주간 초기화",
            self._live_spark_weekly_reset_var,
            card_bg,
            column=2,
        )

        self._load_settings()
        self._start_runtime_refresh()
        return

    def _add_account_sections(
        self,
        body: Any,
        row: int,
        accounts: list[Any],
        card_bg: str,
        border: str,
        text_muted: str,
    ) -> int:
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return row
        tk.Frame(body, bg=border, height=1).grid(
            row=row,
            column=0,
            columnspan=4,
            sticky="we",
            pady=(5, 4),
        )
        row += 1
        tk.Label(
            body,
            text="계정",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=row, column=0, sticky="w", pady=(0, 2))
        row += 1
        cards = tk.Frame(body, bg=card_bg)
        cards.grid(row=row, column=0, columnspan=4, sticky="we", pady=(0, 2))
        try:
            cards.columnconfigure(0, weight=1)
            cards.columnconfigure(1, weight=1)
        except Exception:
            pass
        for index, raw in enumerate(accounts[:2]):
            if not isinstance(raw, dict):
                continue
            account_id = str(raw.get("id", "") or "").strip()
            if not account_id:
                continue
            label = str(raw.get("label", "") or account_id).strip()
            enabled_var = tk.BooleanVar(value=bool(raw.get("enabled", True)))
            self._account_enabled_vars[account_id] = enabled_var
            card = tk.Frame(
                cards,
                bg=card_bg,
                highlightthickness=1,
                highlightbackground=border,
            )
            card.grid(
                row=0,
                column=index,
                sticky="nwe",
                padx=(0, 5) if index == 0 else (5, 0),
                pady=0,
            )
            try:
                card.columnconfigure(0, weight=1)
            except Exception:
                pass
            header = tk.Frame(card, bg=card_bg)
            header.grid(row=0, column=0, sticky="we", padx=8, pady=(4, 1))
            tk.Label(
                header,
                text=label,
                bg=card_bg,
                fg="#111827",
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left")
            tk.Checkbutton(
                header,
                variable=enabled_var,
                bg=card_bg,
                activebackground=card_bg,
                selectcolor=card_bg,
                fg="#111827",
                activeforeground="#111827",
                font=("Segoe UI", 9),
            ).pack(side="left", padx=(5, 3))
            login_button = ttk.Button(
                header,
                text="로그인",
                command=lambda aid=account_id: self._on_account_login(aid),
            )
            login_button.pack(side="right", padx=(4, 0))
            logout_button = ttk.Button(
                header,
                text="로그아웃",
                command=lambda aid=account_id: self._on_account_release_profile(aid),
            )
            logout_button.pack(side="right")
            self._account_login_buttons[account_id] = login_button
            self._account_logout_buttons[account_id] = logout_button
            detail_row = 1
            for prefix, key, clickable in (
                ("설정 파일", "settings_path", True),
                ("상태 파일", "state_path", False),
                ("프로필 경로", "profile_dir", False),
            ):
                value = str(raw.get(key, "") or "").strip()
                path_label = tk.Label(
                    card,
                    text=(
                        f"{prefix}: {self._shorten_path(value, max_chars=48)}"
                        if value
                        else f"{prefix}: (알 수 없음)"
                    ),
                    bg=card_bg,
                    fg="#2563EB" if clickable and value else text_muted,
                    font=("Segoe UI", 8),
                    anchor="w",
                    justify="left",
                )
                path_label.grid(
                    row=detail_row,
                    column=0,
                    sticky="we",
                    padx=8,
                    pady=(0, 1),
                )
                if clickable and value:
                    try:
                        path_label.configure(cursor="hand2")
                        path_label.bind("<Button-1>", lambda _e, path=value: self._open_path(path))
                    except Exception:
                        pass
                detail_row += 1
        row += 1
        return row

    def _add_value_row(
        self,
        parent: Any,
        row: int,
        label: str,
        value_var,
        bg: str,
        column: int = 0,
    ) -> None:
        tk = self._tk
        if tk is None:
            return
        label_pad = (0, 6) if column == 0 else (18, 6)
        value_pad = (0, 8)
        tk.Label(
            parent,
            text=label,
            bg=bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=column, sticky="w", padx=label_pad, pady=1)
        tk.Label(
            parent,
            textvariable=value_var,
            bg=bg,
            fg="#1F2937",
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=250 if column else 220,
        ).grid(row=row, column=column + 1, sticky="w", padx=value_pad, pady=1)
        return

    def _shorten_path(self, value: str, max_chars: int = 84) -> str:
        text = str(value or "").strip()
        if not text:
            return "(알 수 없음)"
        if len(text) <= int(max_chars):
            return text
        keep = max(12, (int(max_chars) - 3) // 2)
        return f"{text[:keep]}...{text[-keep:]}"

    def _path_name(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return "(없음)"
        normalized = text.replace("\\", "/").rstrip("/")
        if not normalized:
            return text
        return normalized.rsplit("/", 1)[-1] or text

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
            self._taskbar_overlay_var.set(bool(settings.get("taskbar_overlay_enabled", True)))
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
        accounts = settings.get("accounts")
        if isinstance(accounts, list):
            for raw in accounts:
                if not isinstance(raw, dict):
                    continue
                account_id = str(raw.get("id", "") or "")
                var = self._account_enabled_vars.get(account_id)
                if var is None:
                    continue
                try:
                    var.set(bool(raw.get("enabled", True)))
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
        self._set_status("로그인 창을 여는 중입니다...", level="info")
        try:
            self._codex.show_current_status(force_refresh=True, source="manual_login")
        except Exception:
            self._set_status("로그인 요청 중 오류가 발생했습니다.", level="error")
            return
        return

    def _on_account_login(self, account_id: str) -> None:
        try:
            runtime = self._safe_get_runtime()
            entry = self._find_account_runtime_entry(runtime, account_id)
            if entry is not None:
                can_login, _can_logout = self._account_action_permissions(
                    entry,
                    manager_enabled=bool(runtime.get("enabled", True)),
                )
                if not can_login:
                    self._set_status(
                        "현재 상태에서는 해당 계정 로그인 요청을 시작할 수 없습니다.",
                        level="info",
                    )
                    return
        except Exception:
            pass
        login = getattr(self._codex, "login_account", None)
        if callable(login):
            self._set_status(f"{account_id} 로그인 창을 여는 중입니다...", level="info")
            try:
                login(str(account_id))
            except Exception:
                self._set_status("로그인 요청 중 오류가 발생했습니다.", level="error")
            return
        show = getattr(self._codex, "show_account_status", None)
        if callable(show):
            try:
                show(str(account_id), force_refresh=True, source="manual_login")
                self._set_status(f"{account_id} 로그인 창을 여는 중입니다...", level="info")
            except Exception:
                self._set_status("로그인 요청 중 오류가 발생했습니다.", level="error")
            return
        self._set_status("계정별 로그인 기능을 사용할 수 없습니다.", level="error")
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

            self._post_ui(done)
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._set_status("로그아웃 작업을 시작하지 못했습니다.", level="error")
        return

    def _on_account_release_profile(self, account_id: str) -> None:
        tk = self._tk
        if tk is None:
            return
        try:
            runtime = self._safe_get_runtime()
            entry = self._find_account_runtime_entry(runtime, account_id)
            if entry is not None:
                _can_login, can_logout = self._account_action_permissions(
                    entry,
                    manager_enabled=bool(runtime.get("enabled", True)),
                )
                if not can_logout:
                    self._set_status(
                        "현재 상태에서는 해당 계정 로그아웃을 시작할 수 없습니다.",
                        level="info",
                    )
                    return
        except Exception:
            pass
        release = getattr(self._codex, "release_account_profile_session", None)
        if not callable(release):
            self._set_status("계정별 로그아웃 기능을 사용할 수 없습니다.", level="error")
            return
        confirmed = True
        try:
            from tkinter import messagebox

            confirmed = bool(
                messagebox.askyesno(
                    "로그아웃",
                    f"{account_id} Codex 로그인 세션에서 로그아웃하시겠습니까?",
                    parent=self._win,
                )
            )
        except Exception:
            confirmed = False
        if not confirmed:
            return
        self._set_status(f"{account_id} 로그아웃 중...", level="info")

        def worker() -> None:
            ok = False
            message = ""
            try:
                ok, message = release(str(account_id))
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

            self._post_ui(done)
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._set_status("로그아웃 작업을 시작하지 못했습니다.", level="error")
        return

    def _post_ui(self, fn) -> bool:
        if not callable(fn):
            return False
        ui_post = self._ui_post
        if callable(ui_post):
            try:
                ui_post(fn)
                return True
            except Exception:
                return False
        return False

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
                "taskbar_overlay_enabled": bool(self._taskbar_overlay_var.get()),
                "interval_sec": interval_sec,
                "tooltip_duration_ms": int(round(tooltip_sec * 1000.0)),
                "usage_url": usage_url,
                "accounts": self._build_account_settings_payload(),
            }
        )
        if ok:
            self._set_status("저장 완료", level="ok")
            self._hide_main_ui()
            return
        self._set_status(f"저장 실패: {err}", level="error")
        return

    def _build_account_settings_payload(self) -> list[dict[str, Any]]:
        settings = self._safe_get_settings()
        accounts = settings.get("accounts")
        if not isinstance(accounts, list):
            return []
        payload = []
        for raw in accounts[:2]:
            if not isinstance(raw, dict):
                continue
            account_id = str(raw.get("id", "") or "")
            if not account_id:
                continue
            item = dict(raw)
            var = self._account_enabled_vars.get(account_id)
            if var is not None:
                try:
                    item["enabled"] = bool(var.get())
                except Exception:
                    pass
            payload.append(item)
        return payload

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
        pending_login_poll = bool(runtime.get("pending_login_poll_active", False))
        auth_attention_required = bool(runtime.get("auth_attention_required", False))
        auth_attention_reason = str(runtime.get("auth_attention_reason", "") or "")
        pending_login_reason = str(runtime.get("pending_login_poll_reason", "") or "")
        system_chrome_cdp_available = bool(runtime.get("system_chrome_cdp_available", False))
        try:
            pending_cdp_misses = int(runtime.get("pending_login_no_cdp_miss_count", 0) or 0)
        except Exception:
            pending_cdp_misses = 0
        try:
            pending_cdp_max_misses = int(runtime.get("pending_login_no_cdp_max_misses", 0) or 0)
        except Exception:
            pending_cdp_max_misses = 0
        try:
            inflight = bool(runtime.get("collect_inflight", False))
        except Exception:
            inflight = False
        source = str(runtime.get("collect_source", "") or "")
        if logout_in_progress or monitor_state == "cancelling":
            state = "로그아웃 중"
        elif inflight:
            state = "로그인 창 여는 중" if source == "manual_login" else "조회 중"
            if source and source != "manual_login":
                state = f"{state} ({source})"
        elif profile_in_use or monitor_state == "paused_profile_in_use":
            state = "프로필 사용 중 (자동 일시중지)"
        elif pending_login_poll:
            is_cloudflare_auth = (
                auth_attention_reason == "cloudflare_challenge"
                or pending_login_reason == "cloudflare_challenge"
            )
            if pending_cdp_misses > 0:
                if pending_cdp_max_misses > 0:
                    label = "인증 창" if is_cloudflare_auth else "로그인 창"
                    state = f"{label} 감지 대기 중 ({pending_cdp_misses}/{pending_cdp_max_misses})"
                else:
                    label = "인증 창" if is_cloudflare_auth else "로그인 창"
                    state = f"{label} 감지 대기 중 ({pending_cdp_misses})"
            else:
                state = "인증 완료 대기 중" if is_cloudflare_auth else "로그인 완료 대기 중"
        elif auth_attention_required or monitor_state == "paused_auth_required":
            state = "브라우저 인증 필요"
        elif session_state == "logged_out" and system_chrome_cdp_available:
            state = "기존 Chrome 세션 감지됨"
        elif session_state == "logged_out":
            state = "로그인 필요"
        else:
            state = "대기 중"

        remain_text = "-"
        remain = runtime.get("next_collect_in_sec", None)
        is_estimated = bool(runtime.get("next_collect_estimated", False))
        try:
            if pending_login_poll:
                pending_remaining = runtime.get("pending_login_poll_remaining_sec", None)
                if pending_remaining is not None:
                    seconds = float(pending_remaining)
                    if seconds < 0:
                        seconds = 0.0
                    remain_text = f"최대 {int(seconds)}초"
            elif (
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

        def _fmt_reset(key: str) -> str:
            raw = str(payload.get(key, "") or "").strip()
            if not raw:
                return "-"
            try:
                formatter = getattr(self._codex, "format_reset_at_for_display", None)
                if callable(formatter):
                    try:
                        rendered = str(formatter(raw, key) or "").strip()
                    except TypeError:
                        rendered = str(formatter(raw) or "").strip()
                    return rendered if rendered else "-"
            except Exception:
                pass
            return raw

        try:
            self._collect_state_var.set(state)
            self._next_collect_var.set(remain_text)
            self._live_time_var.set(_fmt_time(_val("captured_at")))
            self._live_five_hour_var.set(_val("five_hour_limit"))
            if self._live_five_hour_reset_var is not None:
                self._live_five_hour_reset_var.set(_fmt_reset("five_hour_limit_reset_at"))
            self._live_weekly_var.set(_val("weekly_limit"))
            if self._live_weekly_reset_var is not None:
                self._live_weekly_reset_var.set(_fmt_reset("weekly_limit_reset_at"))
            self._live_spark_five_hour_var.set(
                _val("gpt_5_3_codex_spark_five_hour_limit")
            )
            if self._live_spark_five_hour_reset_var is not None:
                self._live_spark_five_hour_reset_var.set(
                    _fmt_reset("gpt_5_3_codex_spark_five_hour_limit_reset_at")
                )
            self._live_spark_weekly_var.set(
                _val("gpt_5_3_codex_spark_weekly_limit")
            )
            if self._live_spark_weekly_reset_var is not None:
                self._live_spark_weekly_reset_var.set(
                    _fmt_reset("gpt_5_3_codex_spark_weekly_limit_reset_at")
                )
            self._live_credit_var.set(_val("remaining_credit"))
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
        manager_enabled = bool(runtime.get("enabled", True))
        for account_id, button in self._account_login_buttons.items():
            entry = self._find_account_runtime_entry(runtime, account_id)
            account_can_login, _account_can_logout = self._account_action_permissions(
                entry,
                manager_enabled=manager_enabled,
            )
            self._set_button_enabled(button, account_can_login)
        for account_id, button in self._account_logout_buttons.items():
            entry = self._find_account_runtime_entry(runtime, account_id)
            _account_can_login, account_can_logout = self._account_action_permissions(
                entry,
                manager_enabled=manager_enabled,
            )
            self._set_button_enabled(button, account_can_logout)
        return

    def _find_account_runtime_entry(
        self,
        runtime: dict[str, Any],
        account_id: str,
    ) -> dict[str, Any] | None:
        accounts = runtime.get("accounts") if isinstance(runtime, dict) else None
        if not isinstance(accounts, list):
            return None
        normalized = str(account_id or "")
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("id") or "") == normalized:
                return raw
        return None

    def _account_action_permissions(
        self,
        entry: dict[str, Any] | None,
        *,
        manager_enabled: bool = True,
    ) -> tuple[bool, bool]:
        if (
            not bool(manager_enabled)
            or not isinstance(entry, dict)
            or not bool(entry.get("enabled", True))
        ):
            return False, False
        runtime = entry.get("runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        if bool(runtime.get("collect_inflight", False)) or bool(
            runtime.get("logout_in_progress", False)
        ):
            return False, False
        monitor_state = str(runtime.get("monitor_state") or "")
        if monitor_state in {"running", "cancelling"}:
            return False, False
        return bool(runtime.get("can_login", False)), bool(runtime.get("can_logout", False))

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
