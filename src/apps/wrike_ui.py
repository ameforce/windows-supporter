from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Any
import threading


class WrikeSettingsView:
    _VACATION_PROVIDER_LABELS = {
        "private_ical": "비공개 iCal",
        "google_oauth": "Google 계정 OAuth",
    }
    _VACATION_PROVIDER_VALUES = {
        label: provider for provider, label in _VACATION_PROVIDER_LABELS.items()
    }

    def __init__(self, root: Any, wrike: Any, ui_post=None) -> None:
        self._root = root
        self._wrike = wrike
        self._ui_post = ui_post if callable(ui_post) else None

        self._tk = None
        self._ttk = None
        self._win = None
        self._scroll_canvas = None
        self._scroll_body = None
        self._scroll_window_id = None

        self._token_var = None
        self._daily_var = None
        self._tooltip_var = None
        self._monitor_enabled_var = None
        self._monitor_interval_var = None
        self._status_var = None
        self._status_label = None
        self._show_token_var = None
        self._token_entry = None
        self._workday_date_var = None
        self._workday_target_var = None
        self._workday_clock_in_var = None
        self._break_button_var = None
        self._manual_break_state: dict[str, Any] = {}
        self._break_timer_after_id = None
        self._break_timer_window = None
        self._lunch_enabled_var = None
        self._lunch_start_var = None
        self._lunch_end_var = None
        self._ical_url_var = None
        self._ical_keywords_var = None
        self._ical_interval_var = None
        self._ical_dirty = False
        self._vacation_ical_url_var = None
        self._vacation_ical_status_var = None
        self._vacation_provider_var = None
        self._vacation_provider_combo = None
        self._vacation_private_frame = None
        self._vacation_oauth_frame = None
        self._vacation_calendar_provider = "private_ical"
        self._vacation_ical_dirty = False
        self._vacation_ical_status: dict[str, Any] = {}
        self._google_calendar_status: dict[str, Any] = {}
        self._google_status_var = None
        self._google_action_var = None
        self._google_action_button = None
        self._google_break_var = None
        self._google_vacation_var = None
        self._google_break_combo = None
        self._google_vacation_combo = None
        self._google_handle_by_label: dict[str, str] = {}
        self._google_role_updating = False
        self._advanced_ical_frame = None
        self._advanced_ical_visible = False
        self._advanced_toggle_var = None
        self._folder_path_frame = None
        self._folder_levels: list[dict] = []
        self._folder_path_label = None
        self._folder_restoring = False
        self._autosave_after_id = None
        self._loading_settings = False
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
            text="근무시간 · Wrike · 휴가 설정",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        btn_row = tk.Frame(title_row, bg=card_bg)
        btn_row.pack(side="right")
        self._break_button_var = tk.StringVar(value="휴게 시작")
        self._ical_dirty = False
        ttk.Button(
            btn_row,
            textvariable=self._break_button_var,
            command=self._on_toggle_break,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(btn_row, text="토큰 지우기", command=self._on_clear_token).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(btn_row, text="토큰 검증", command=self._on_validate_token).pack(
            side="right", padx=(0, 8)
        )

        tk.Label(
            header_inner,
            text="근무 계획, Wrike API·모니터링, 휴게·휴가 캘린더를 설정합니다.",
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

        body = self._create_scroll_body(content_card, card_bg)

        content = tk.Frame(body, bg=card_bg)
        content.pack(fill="both", expand=True, padx=14, pady=12)
        content.columnconfigure(1, weight=1)

        self._token_var = tk.StringVar(value="")
        self._daily_var = tk.StringVar(value="")
        self._tooltip_var = tk.StringVar(value="")
        self._monitor_enabled_var = tk.BooleanVar(value=False)
        self._monitor_interval_var = tk.StringVar(value="")
        self._show_token_var = tk.BooleanVar(value=False)
        self._workday_date_var = tk.StringVar(value=date.today().isoformat())
        self._workday_target_var = tk.StringVar(value="")
        self._workday_clock_in_var = tk.StringVar(value="")

        row = 0

        def add_label(text: str) -> None:
            nonlocal row
            tk.Label(
                content,
                text=text,
                bg=card_bg,
                fg="#111827",
                font=("Segoe UI", 9),
            ).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=6
            )

        def add_entry(var, show: str | None = None):
            nonlocal row
            entry = ttk.Entry(content, textvariable=var, width=50)
            if show:
                try:
                    entry.configure(show=show)
                except Exception:
                    pass
            entry.grid(row=row, column=1, sticky="we", pady=6)
            return entry

        tk.Label(
            content,
            text="── 오늘·날짜별 근무 계획 ──",
            bg=card_bg,
            fg="#2563EB",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        row += 1

        add_label("날짜(YYYY-MM-DD)")
        add_entry(self._workday_date_var)
        row += 1

        add_label("목표 순근무 시간(시간)")
        add_entry(self._workday_target_var)
        row += 1

        add_label("출근 시각(HH:MM)")
        add_entry(self._workday_clock_in_var)
        row += 1

        workday_btn_row = tk.Frame(content, bg=card_bg)
        workday_btn_row.grid(row=row, column=1, columnspan=2, sticky="w", pady=(2, 10))
        ttk.Button(
            workday_btn_row, text="계획 불러오기", command=self._on_load_workday_plan
        ).pack(side="left")
        ttk.Button(
            workday_btn_row, text="계획 저장", command=self._on_save_workday_plan
        ).pack(side="left", padx=(4, 0))
        ttk.Button(
            workday_btn_row, text="계획 초기화", command=self._on_clear_workday_plan
        ).pack(side="left", padx=(4, 0))
        row += 1

        tk.Label(
            content,
            text="── 기본 설정 ──",
            bg=card_bg,
            fg="#2563EB",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 4))
        row += 1

        add_label("API 토큰")
        self._token_entry = add_entry(self._token_var, show="*")
        tk.Checkbutton(
            content,
            text="표시",
            variable=self._show_token_var,
            command=self._toggle_token_visibility,
            bg=card_bg,
            fg="#111827",
            activebackground=card_bg,
            activeforeground="#111827",
            selectcolor=card_bg,
            font=("Segoe UI", 9),
        ).grid(row=row, column=2, sticky="w", padx=(8, 0))
        row += 1

        add_label("기본 일 목표 시간(시간)")
        add_entry(self._daily_var)
        row += 1

        add_label("툴팁 표시 시간(초)")
        add_entry(self._tooltip_var)
        row += 1

        add_label("모니터링 활성화")
        tk.Checkbutton(
            content,
            variable=self._monitor_enabled_var,
            bg=card_bg,
            activebackground=card_bg,
            selectcolor=card_bg,
            fg="#111827",
            activeforeground="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=1, sticky="w", pady=6)
        row += 1

        add_label("모니터링 주기(초)")
        add_entry(self._monitor_interval_var)
        row += 1

        self._lunch_enabled_var = tk.BooleanVar(value=True)
        self._lunch_start_var = tk.StringVar(value="12:00")
        self._lunch_end_var = tk.StringVar(value="13:00")
        self._ical_url_var = tk.StringVar(value="")
        self._ical_keywords_var = tk.StringVar(value="")
        self._ical_interval_var = tk.StringVar(value="15")
        self._vacation_ical_url_var = tk.StringVar(value="")
        self._vacation_ical_status_var = tk.StringVar(value="미설정")
        self._google_status_var = tk.StringVar(value="Google 계정 미연결")
        self._google_action_var = tk.StringVar(value="Google 계정 연결")
        self._google_break_var = tk.StringVar(value="선택 안 함")
        self._google_vacation_var = tk.StringVar(value="선택 안 함")
        self._advanced_toggle_var = tk.StringVar(value="고급 설정 · 비공개 iCal 펼치기")
        self._vacation_provider_var = None

        tk.Label(
            content,
            text="── 휴게 시간 · 휴게 캘린더 ──",
            bg=card_bg,
            fg="#2563EB",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(14, 4))
        row += 1

        add_label("점심 휴게 자동 차감")
        tk.Checkbutton(
            content,
            variable=self._lunch_enabled_var,
            bg=card_bg,
            activebackground=card_bg,
            selectcolor=card_bg,
            fg="#111827",
            activeforeground="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=1, sticky="w", pady=6)
        row += 1

        add_label("점심 시간대(HH:MM~HH:MM)")
        lunch_frame = tk.Frame(content, bg=card_bg)
        lunch_frame.grid(row=row, column=1, sticky="we", pady=6)
        ttk.Entry(lunch_frame, textvariable=self._lunch_start_var, width=8).pack(side="left")
        tk.Label(lunch_frame, text="~", bg=card_bg, fg=text_muted, font=("Segoe UI", 9)).pack(
            side="left", padx=4
        )
        ttk.Entry(lunch_frame, textvariable=self._lunch_end_var, width=8).pack(side="left")
        row += 1

        add_label("휴게 캘린더 헬스장 키워드(쉼표 구분)")
        add_entry(self._ical_keywords_var)
        row += 1

        add_label("휴게 캘린더 조회 주기(분)")
        add_entry(self._ical_interval_var)
        row += 1

        def bind_dynamic_wrap(label: Any, fallback: int = 420) -> None:
            def sync_wrap(event: Any) -> None:
                try:
                    width = max(220, int(event.width) - 4)
                    current = int(float(label.cget("wraplength") or 0))
                    if current != width:
                        label.configure(wraplength=width)
                except Exception:
                    return

            try:
                label.configure(wraplength=max(220, int(fallback)))
                label.bind("<Configure>", sync_wrap, add="+")
            except Exception:
                pass
            return

        tk.Label(
            content,
            text="── Google Calendar ──",
            bg=card_bg,
            fg="#2563EB",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(14, 4))
        row += 1

        add_label("Google 계정")
        google_account_controls = tk.Frame(content, bg=card_bg)
        google_account_controls.grid(
            row=row, column=1, columnspan=2, sticky="we", pady=6
        )
        self._google_action_button = ttk.Button(
            google_account_controls,
            textvariable=self._google_action_var,
            command=self._on_google_account_action,
        )
        self._google_action_button.pack(side="left")
        ttk.Button(
            google_account_controls,
            text="캘린더 새로고침",
            command=self._on_refresh_google_calendar_catalog,
        ).pack(side="left", padx=(4, 0))
        row += 1

        add_label("연결 상태")
        google_status_label = tk.Label(
            content,
            textvariable=self._google_status_var,
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
        )
        bind_dynamic_wrap(google_status_label)
        google_status_label.grid(
            row=row, column=1, columnspan=2, sticky="we", pady=6
        )
        row += 1

        add_label("휴게 캘린더")
        self._google_break_combo = ttk.Combobox(
            content,
            textvariable=self._google_break_var,
            values=("선택 안 함",),
            state="readonly",
            width=48,
        )
        self._google_break_combo.grid(
            row=row, column=1, columnspan=2, sticky="we", pady=6
        )
        self._google_break_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._on_google_role_selected("break"),
        )
        row += 1

        add_label("휴가 캘린더")
        self._google_vacation_combo = ttk.Combobox(
            content,
            textvariable=self._google_vacation_var,
            values=("선택 안 함",),
            state="readonly",
            width=48,
        )
        self._google_vacation_combo.grid(
            row=row, column=1, columnspan=2, sticky="we", pady=6
        )
        self._google_vacation_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._on_google_role_selected("vacation"),
        )
        row += 1

        tk.Label(
            content,
            text=(
                "한 Google 계정의 읽기 전용 권한을 공유하고, 휴게와 휴가 역할에 "
                "사용할 캘린더만 각각 선택합니다. 캘린더 이름은 화면에만 표시되고 저장되지 않습니다."
            ),
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
            wraplength=620,
        ).grid(row=row, column=1, columnspan=2, sticky="we", pady=(0, 6))
        row += 1

        ttk.Button(
            content,
            textvariable=self._advanced_toggle_var,
            command=self._toggle_advanced_ical,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 4))
        row += 1

        self._advanced_ical_frame = tk.Frame(content, bg=card_bg)
        self._advanced_ical_frame.grid(
            row=row, column=0, columnspan=3, sticky="we", pady=(0, 8)
        )
        self._advanced_ical_frame.columnconfigure(1, weight=1)

        tk.Label(
            self._advanced_ical_frame,
            text="휴게 비공개 iCal URL",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=0, column=0, sticky="w", padx=(0, 10), pady=6)
        ical_entry_holder = ttk.Entry(
            self._advanced_ical_frame,
            textvariable=self._ical_url_var,
            show="*",
            width=44,
        )
        ical_entry_holder.grid(row=0, column=1, sticky="we", pady=6)
        try:
            ical_entry_holder.bind("<KeyRelease>", self._mark_ical_dirty)
        except Exception:
            pass
        ttk.Button(
            self._advanced_ical_frame,
            text="지우기",
            command=self._on_clear_ical,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        tk.Label(
            self._advanced_ical_frame,
            text="휴가 비공개 iCal URL",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=6)
        vacation_entry = ttk.Entry(
            self._advanced_ical_frame,
            textvariable=self._vacation_ical_url_var,
            show="*",
            width=44,
        )
        vacation_entry.grid(row=1, column=1, sticky="we", pady=6)
        try:
            vacation_entry.bind("<KeyRelease>", self._mark_vacation_ical_dirty)
        except Exception:
            pass
        vacation_buttons = tk.Frame(self._advanced_ical_frame, bg=card_bg)
        vacation_buttons.grid(row=1, column=2, sticky="w", padx=(8, 0))
        ttk.Button(
            vacation_buttons,
            text="다시 확인",
            command=self._on_retry_vacation_ical,
        ).pack(side="left")
        ttk.Button(
            vacation_buttons,
            text="지우기",
            command=self._on_clear_vacation_ical,
        ).pack(side="left", padx=(4, 0))

        tk.Label(
            self._advanced_ical_frame,
            text="휴가 계산 상태",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=2, column=0, sticky="w", padx=(0, 10), pady=6)
        vacation_status_label = tk.Label(
            self._advanced_ical_frame,
            textvariable=self._vacation_ical_status_var,
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
        )
        bind_dynamic_wrap(vacation_status_label)
        vacation_status_label.grid(
            row=2, column=1, columnspan=2, sticky="we", pady=6
        )
        try:
            self._advanced_ical_frame.grid_remove()
        except Exception:
            pass
        self._advanced_ical_visible = False
        row += 1

        tk.Label(
            content,
            text="모니터링 폴더",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=6)

        folder_outer = tk.Frame(content, bg=card_bg)
        folder_outer.grid(row=row, column=1, columnspan=2, sticky="nsew", pady=6)
        content.rowconfigure(row, weight=1)
        row += 1

        self._folder_path_frame = tk.Frame(folder_outer, bg=card_bg)
        self._folder_path_frame.pack(fill="both", expand=True)

        self._folder_path_label = tk.Label(
            folder_outer,
            text="",
            bg=card_bg,
            fg="#2563EB",
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
        )
        bind_dynamic_wrap(self._folder_path_label)
        self._folder_path_label.pack(fill="x", pady=(4, 0))

        folder_btn_row = tk.Frame(folder_outer, bg=card_bg)
        folder_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(
            folder_btn_row, text="경로 저장", command=self._on_save_folder_path
        ).pack(side="left")
        ttk.Button(
            folder_btn_row, text="경로 초기화", command=self._on_clear_folder_path
        ).pack(side="left", padx=(4, 0))
        ttk.Button(
            folder_btn_row, text="캐시 새로고침", command=self._on_refresh_cache
        ).pack(side="left", padx=(4, 0))
        folder_help_label = tk.Label(
            folder_outer,
            text="비워두면 전체 타임로그를 조회합니다.",
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
        )
        bind_dynamic_wrap(folder_help_label)
        folder_help_label.pack(fill="x", pady=(4, 0))

        path = ""
        try:
            path = str(self._wrike.get_settings_snapshot().get("settings_path", ""))
        except Exception:
            path = ""
        path_label = tk.Label(
            content,
            text=f"저장 위치: {path}" if path else "저장 위치: (알 수 없음)",
            bg=card_bg,
            fg="#2563EB" if path else text_muted,
            font=("Segoe UI", 9),
            cursor="hand2" if path else "",
        )
        path_label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 4))
        if path:
            try:
                path_label.bind("<Button-1>", lambda _e: self._open_settings_file(path))
            except Exception:
                pass
        row += 1

        self._load_settings()
        self._register_autosave_traces()
        self._bind_scroll_targets()
        if (
            bool(self._google_calendar_status.get("configured", False))
            and not list(self._google_calendar_status.get("catalog") or [])
        ):
            self._on_refresh_google_calendar_catalog()
        self._refresh_scroll_region()
        self._start_break_timer()
        try:
            if self._win is not None:
                self._win.after(120, self._auto_validate_token)
        except Exception:
            pass
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

    def _create_scroll_body(self, parent: Any, bg: str) -> Any:
        tk = self._tk
        ttk = self._ttk
        self._scroll_canvas = None
        self._scroll_body = None
        self._scroll_window_id = None

        def fallback_body() -> Any:
            body = tk.Frame(parent, bg=bg)
            body.pack(fill="both", expand=True)
            return body

        canvas_factory = getattr(tk, "Canvas", None)
        scrollbar_factory = getattr(ttk, "Scrollbar", None)
        if not callable(canvas_factory) or not callable(scrollbar_factory):
            return fallback_body()

        host = None
        try:
            host = tk.Frame(parent, bg=bg)
            canvas = canvas_factory(
                host,
                bg=bg,
                highlightthickness=0,
                borderwidth=0,
            )
            scrollbar = scrollbar_factory(host, orient="vertical", command=canvas.yview)
            body = tk.Frame(canvas, bg=bg)
            required = (
                callable(getattr(canvas, "bbox", None))
                and callable(getattr(canvas, "configure", None))
                and callable(getattr(canvas, "create_window", None))
                and callable(getattr(canvas, "itemconfigure", None))
                and callable(getattr(canvas, "yview_scroll", None))
                and callable(getattr(scrollbar, "set", None))
                and callable(getattr(body, "winfo_reqwidth", None))
            )
            if not required:
                raise RuntimeError("scroll widgets are incomplete")

            canvas.configure(yscrollcommand=scrollbar.set)
            host.pack(fill="both", expand=True)
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            window_id = canvas.create_window((0, 0), window=body, anchor="nw")

            def sync_scroll_region(_event: Any = None) -> None:
                self._refresh_scroll_region()
                return

            def sync_content_width(event: Any) -> None:
                try:
                    canvas.itemconfigure(window_id, width=max(1, int(event.width)))
                except Exception:
                    pass
                return

            body.bind("<Configure>", sync_scroll_region)
            canvas.bind("<Configure>", sync_content_width)
            self._scroll_canvas = canvas
            self._scroll_body = body
            self._scroll_window_id = window_id
            return body
        except Exception:
            if host is not None:
                try:
                    host.destroy()
                except Exception:
                    pass
            self._scroll_canvas = None
            self._scroll_body = None
            self._scroll_window_id = None
            return fallback_body()

    def _refresh_scroll_region(self) -> None:
        canvas = self._scroll_canvas
        body = self._scroll_body
        if canvas is None or body is None:
            return
        try:
            body.winfo_reqwidth()
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass
        return

    def _scroll_settings(self, canvas: Any, event: Any) -> str:
        if canvas is not self._scroll_canvas:
            return "break"
        win = self._win
        if win is None:
            return "break"
        exists = getattr(win, "winfo_exists", None)
        if callable(exists):
            try:
                if not bool(exists()):
                    return "break"
            except Exception:
                return "break"
        is_mapped = getattr(win, "winfo_ismapped", None)
        if callable(is_mapped):
            try:
                if not bool(is_mapped()):
                    return "break"
            except Exception:
                return "break"
        try:
            delta = int(getattr(event, "delta", 0) or 0)
        except Exception:
            delta = 0
        if delta:
            units = max(1, abs(delta) // 120)
            units = -units if delta > 0 else units
        else:
            button = getattr(event, "num", None)
            units = -1 if button == 4 else 1 if button == 5 else 0
        if units:
            try:
                canvas.yview_scroll(int(units), "units")
            except Exception:
                pass
        return "break"

    def _bind_scroll_targets(self) -> None:
        canvas = self._scroll_canvas
        body = self._scroll_body
        root = self._win
        if canvas is None or body is None or root is None:
            return
        visited: set[int] = set()

        def bind_widget(widget: Any) -> None:
            marker = id(widget)
            if marker in visited:
                return
            visited.add(marker)
            callbacks = (
                ("<MouseWheel>", lambda event: self._scroll_settings(canvas, event)),
                ("<Button-4>", lambda event: self._scroll_settings(canvas, event)),
                ("<Button-5>", lambda event: self._scroll_settings(canvas, event)),
            )
            for sequence, callback in callbacks:
                try:
                    widget.bind(sequence, callback, add="+")
                except TypeError:
                    try:
                        widget.bind(sequence, callback)
                    except Exception:
                        pass
                except Exception:
                    pass
            try:
                children = list(widget.winfo_children())
            except Exception:
                children = []
            for child in children:
                bind_widget(child)
            return

        bind_widget(root)
        return

    def _toggle_token_visibility(self) -> None:
        entry = self._token_entry
        if entry is None or self._show_token_var is None:
            return
        show = "" if bool(self._show_token_var.get()) else "*"
        try:
            entry.configure(show=show)
        except Exception:
            pass
        return

    def _format_hours(self, minutes: int) -> str:
        try:
            minutes = int(minutes)
        except Exception:
            return "0"
        if minutes <= 0:
            return "0"
        hours = minutes / 60.0
        if abs(hours - int(hours)) < 1e-6:
            return str(int(hours))
        return f"{hours:.2f}".rstrip("0").rstrip(".")

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

    def _parse_iso_day(self, text: str) -> tuple[str | None, str | None]:
        raw = str(text or "").strip()
        if not raw:
            return None, "날짜를 입력해 주세요."
        try:
            parsed = date.fromisoformat(raw)
        except Exception:
            return None, "날짜 형식은 YYYY-MM-DD 입니다."
        normalized = parsed.isoformat()
        if raw != normalized:
            return None, "날짜 형식은 YYYY-MM-DD 입니다."
        return normalized, None

    def _strict_nonnegative_float(self, text: str, label: str) -> tuple[float, str | None]:
        raw = str(text or "").strip()
        if not raw:
            return 0.0, f"{label} 값을 입력해 주세요."
        try:
            value = float(raw)
        except Exception:
            return 0.0, f"{label} 값이 숫자가 아닙니다."
        if not isfinite(value):
            return 0.0, f"{label} 값이 올바르지 않습니다."
        if value < 0:
            return 0.0, f"{label} 값은 0 이상이어야 합니다."
        return float(value), None

    def _apply_workday_plan(self, plan: Any, fallback_day: str | None = None) -> None:
        data = dict(plan) if isinstance(plan, dict) else {}
        day_text = str(data.get("date") or fallback_day or date.today().isoformat()).strip()
        try:
            target_minutes = max(0, int(data.get("target_net_minutes", 0) or 0))
        except Exception:
            target_minutes = 0
        clock_in = str(data.get("clock_in") or "").strip()
        try:
            if self._workday_date_var is not None:
                self._workday_date_var.set(day_text)
        except Exception:
            pass
        try:
            if self._workday_target_var is not None:
                self._workday_target_var.set(self._format_hours(target_minutes))
        except Exception:
            pass
        try:
            if self._workday_clock_in_var is not None:
                self._workday_clock_in_var.set(clock_in)
        except Exception:
            pass
        return

    def _read_workday_day(self) -> tuple[str | None, str | None]:
        value = ""
        try:
            if self._workday_date_var is not None:
                value = str(self._workday_date_var.get() or "")
        except Exception:
            value = ""
        return self._parse_iso_day(value)

    def _fetch_and_apply_workday_plan(self, day_text: str) -> tuple[bool, str | None]:
        try:
            plan = self._wrike.get_workday_plan(day_text)
        except Exception as exc:
            return False, str(exc) or "근무 계획을 불러오지 못했습니다."
        if not isinstance(plan, dict):
            return False, "근무 계획 응답이 올바르지 않습니다."
        self._apply_workday_plan(plan, fallback_day=day_text)
        return True, None

    def _on_load_workday_plan(self) -> None:
        day_text, error = self._read_workday_day()
        if error or day_text is None:
            self._set_status(f"계획 불러오기 실패: {error}", level="error")
            return
        ok, error = self._fetch_and_apply_workday_plan(day_text)
        if ok:
            self._set_status("근무 계획을 불러왔습니다.", level="ok")
        else:
            self._set_status(f"계획 불러오기 실패: {error}", level="error")
        return

    def _on_save_workday_plan(self) -> None:
        day_text, error = self._read_workday_day()
        if error or day_text is None:
            self._set_status(f"계획 저장 실패: {error}", level="error")
            return
        try:
            target_text = (
                str(self._workday_target_var.get() or "")
                if self._workday_target_var is not None
                else ""
            )
        except Exception:
            target_text = ""
        target_hours, error = self._strict_nonnegative_float(
            target_text, "목표 순근무 시간"
        )
        if error:
            self._set_status(f"계획 저장 실패: {error}", level="error")
            return
        try:
            clock_text = (
                str(self._workday_clock_in_var.get() or "")
                if self._workday_clock_in_var is not None
                else ""
            )
        except Exception:
            clock_text = ""
        clock_text = clock_text.strip()
        if (
            len(clock_text) != 5
            or clock_text[2:3] != ":"
            or not clock_text[:2].isdigit()
            or not clock_text[3:].isdigit()
        ):
            self._set_status(
                "계획 저장 실패: 출근 시각 형식은 HH:MM 입니다.",
                level="error",
            )
            return
        clock_minutes, error = self._parse_hhmm(clock_text, "출근 시각")
        if error or clock_minutes is None:
            self._set_status(f"계획 저장 실패: {error}", level="error")
            return
        target_minutes_value = float(target_hours) * 60.0
        if not isfinite(target_minutes_value):
            self._set_status(
                "계획 저장 실패: 목표 순근무 시간이 너무 큽니다.",
                level="error",
            )
            return
        target_minutes = max(0, int(round(target_minutes_value)))
        normalized_clock = self._format_minutes_as_hhmm(clock_minutes)
        try:
            ok, update_error = self._wrike.update_workday_plan(
                day_text,
                target_minutes,
                normalized_clock,
            )
        except Exception as exc:
            ok, update_error = False, str(exc) or "근무 계획 저장에 실패했습니다."
        if not ok:
            self._set_status(
                f"계획 저장 실패: {update_error or '알 수 없는 오류'}",
                level="error",
            )
            return
        refreshed, _refresh_error = self._fetch_and_apply_workday_plan(day_text)
        if not refreshed:
            self._apply_workday_plan(
                {
                    "date": day_text,
                    "target_net_minutes": target_minutes,
                    "clock_in": normalized_clock,
                }
            )
        self._set_status("근무 계획 저장 완료", level="ok")
        return

    def _on_clear_workday_plan(self) -> None:
        day_text, error = self._read_workday_day()
        if error or day_text is None:
            self._set_status(f"계획 초기화 실패: {error}", level="error")
            return
        try:
            ok, clear_error = self._wrike.clear_workday_plan(day_text)
        except Exception as exc:
            ok, clear_error = False, str(exc) or "근무 계획 초기화에 실패했습니다."
        if not ok:
            self._set_status(
                f"계획 초기화 실패: {clear_error or '알 수 없는 오류'}",
                level="error",
            )
            return
        refreshed, refresh_error = self._fetch_and_apply_workday_plan(day_text)
        if not refreshed:
            self._set_status(
                f"계획 초기화 후 불러오기 실패: {refresh_error}",
                level="error",
            )
            return
        self._set_status("근무 계획 초기화 완료", level="ok")
        return

    def _normalize_vacation_provider(self, value: Any) -> str:
        provider = str(value or "").strip()
        if provider in self._VACATION_PROVIDER_LABELS:
            return provider
        return "private_ical"

    def _selected_vacation_provider(self) -> str:
        var = self._vacation_provider_var
        if var is not None:
            try:
                label = str(var.get() or "").strip()
                provider = self._VACATION_PROVIDER_VALUES.get(label)
                if provider is not None:
                    return provider
            except Exception:
                pass
        return self._normalize_vacation_provider(
            self._vacation_calendar_provider
        )

    def _set_vacation_provider_control(self, provider: Any) -> None:
        normalized = self._normalize_vacation_provider(provider)
        self._vacation_calendar_provider = normalized
        var = self._vacation_provider_var
        if var is not None:
            try:
                var.set(self._VACATION_PROVIDER_LABELS[normalized])
            except Exception:
                pass
        private_frame = self._vacation_private_frame
        oauth_frame = self._vacation_oauth_frame
        for frame in (private_frame, oauth_frame):
            if frame is not None:
                try:
                    frame.pack_forget()
                except Exception:
                    pass
        selected_frame = (
            oauth_frame if normalized == "google_oauth" else private_frame
        )
        if selected_frame is not None:
            try:
                selected_frame.pack(fill="x", expand=True)
            except Exception:
                pass
        self._refresh_scroll_region()
        return

    def _on_vacation_provider_selected(self, _event: Any = None) -> None:
        if bool(self._loading_settings):
            return
        previous = self._normalize_vacation_provider(
            self._vacation_calendar_provider
        )
        selected = self._selected_vacation_provider()
        if selected == previous:
            self._set_vacation_provider_control(selected)
            return
        try:
            result = self._wrike.update_settings(
                {"vacation_calendar_provider": selected}
            )
            if not isinstance(result, tuple) or len(result) != 2:
                ok, _error = False, None
            else:
                ok, _error = bool(result[0]), result[1]
        except Exception:
            ok, _error = False, None
        if not ok:
            self._set_vacation_provider_control(previous)
            self._refresh_vacation_ical_status(self._vacation_ical_status)
            self._set_status("휴가 캘린더 연결 방식 저장 실패", level="error")
            return
        self._set_vacation_provider_control(selected)
        if not self._refresh_vacation_status_from_backend():
            fallback = dict(self._vacation_ical_status)
            configured = bool(
                fallback.get("oauth_configured", False)
                if selected == "google_oauth"
                else False
            )
            fallback.update({
                "provider": selected,
                "configured": configured,
                "state": "loading" if configured else "unconfigured",
                "last_success_ts": None,
                "error_code": "",
                "authorizing": False,
                "fetch_running": False,
                "has_last_good": False,
            })
            self._refresh_vacation_ical_status(fallback)
        self._set_status("휴가 캘린더 연결 방식 저장 완료", level="ok")
        return

    def _vacation_status_from_settings(self, settings: Any) -> dict[str, Any]:
        data = settings if isinstance(settings, dict) else {}
        nested = data.get("vacation_ical_status")
        provider = self._normalize_vacation_provider(
            data.get(
                "vacation_calendar_provider",
                nested.get("provider") if isinstance(nested, dict) else None,
            )
        )
        oauth_configured = bool(
            data.get(
                "oauth_configured",
                nested.get("oauth_configured", False)
                if isinstance(nested, dict)
                else False,
            )
        )
        if isinstance(nested, dict):
            sanitized = dict(nested)
            sanitized["provider"] = provider
            sanitized["oauth_configured"] = oauth_configured
            sanitized["expected_calendar_name"] = ""
            sanitized["observed_calendar_name"] = ""
            return sanitized
        configured = bool(
            data.get(
                "vacation_ical_configured",
                data.get("vacation_ical_url_configured", False),
            )
        )
        if provider == "google_oauth":
            configured = oauth_configured
        return {
            "provider": provider,
            "oauth_configured": oauth_configured,
            "secret_present": bool(
                data.get("vacation_ical_secret_present", configured)
            ) if provider == "private_ical" else False,
            "configured": configured,
            "expected_calendar_name": "",
            "observed_calendar_name": "",
            "state": str(
                data.get("vacation_ical_state")
                or ("loading" if configured else "unconfigured")
            ).strip(),
            "last_success_ts": data.get("vacation_ical_last_success_ts"),
            "error_code": str(
                data.get("vacation_ical_last_error") or ""
            ).strip(),
            "fetch_running": False,
            "has_last_good": bool(data.get("vacation_ical_has_last_good", False)),
        }

    def _read_vacation_status_snapshot(self) -> dict[str, Any] | None:
        getter = getattr(self._wrike, "get_vacation_ical_status_snapshot", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:
            return None
        return dict(value) if isinstance(value, dict) else None

    def _refresh_vacation_status_from_backend(self) -> bool:
        status = self._read_vacation_status_snapshot()
        if status is None:
            return False
        self._refresh_vacation_ical_status(status)
        return True

    def _refresh_vacation_ical_status(self, status: Any = None) -> None:
        if isinstance(status, dict):
            sanitized = dict(status)
            sanitized["expected_calendar_name"] = ""
            sanitized["observed_calendar_name"] = ""
            self._vacation_ical_status = sanitized
        data = dict(self._vacation_ical_status)
        provider = self._normalize_vacation_provider(
            data.get("provider", self._vacation_calendar_provider)
        )
        data["provider"] = provider
        self._vacation_ical_status = data
        var = self._vacation_ical_status_var
        if var is None:
            return
        state = str(data.get("state") or "unconfigured").strip().lower()
        if provider == "google_oauth" and bool(data.get("authorizing", False)):
            state = "authorizing"
        if state not in {
            "unconfigured", "authorizing", "disconnecting", "loading",
            "fresh", "stale", "error"
        }:
            state = "error"
        last_success = str(data.get("last_success_ts") or "").strip()
        error_code = str(data.get("error_code") or "").strip()
        has_last_good = bool(data.get("has_last_good", False))
        provider_label = self._VACATION_PROVIDER_LABELS[provider]
        state_labels = {
            "unconfigured": "미설정",
            "authorizing": "Google 인증 중",
            "disconnecting": "Google 연결 해제 중",
            "loading": "확인 중",
            "fresh": "정상",
            "stale": "마지막 성공값 사용 중",
            "error": "오류",
        }
        private_state_messages = {
            "unconfigured": "Google Calendar 비공개 주소(iCal 형식)를 입력해 주세요.",
            "authorizing": "연결 확인을 진행하고 있습니다.",
            "disconnecting": "연결 해제를 진행하고 있습니다.",
            "loading": "저장 완료 · 연결 단계 확인 중입니다.",
            "fresh": "연결·응답·문서·캘린더 확인을 모두 통과했습니다.",
            "stale": "연결 단계 재확인이 필요합니다. 다시 확인해 주세요.",
        }
        oauth_state_messages = {
            "unconfigured": (
                "Google 계정 연결을 시작해 읽기 전용 권한을 승인해 주세요."
            ),
            "authorizing": (
                "브라우저에서 Google 로그인과 읽기 전용 권한 동의를 완료해 주세요."
            ),
            "disconnecting": (
                "Google 권한 해제와 로컬 인증 정보 삭제를 진행하고 있습니다."
            ),
            "loading": "Google Calendar API 연결과 캘린더를 확인 중입니다.",
            "fresh": "Google Calendar API 읽기 전용 연결이 정상입니다.",
            "stale": "연결을 다시 확인하고 필요하면 Google 계정을 다시 연결해 주세요.",
        }
        private_errors = {
            "invalid_endpoint": (
                "Google Calendar 비공개 주소(iCal 형식)인지 확인해 주세요. "
                "브라우저·로그인·Microsoft 365 주소는 지원하지 않습니다."
            ),
            "redirect_rejected": (
                "연결 단계에서 허용되지 않은 이동을 차단했습니다. "
                "비공개 iCal 주소를 다시 발급해 저장해 주세요."
            ),
            "authentication_required": (
                "연결 단계에서 로그인이 필요한 응답을 받았습니다. "
                "로그인 주소가 아니라 인증 없이 읽히는 비공개 iCal 주소를 저장해 주세요."
            ),
            "http_4xx": (
                "연결 단계에서 링크 거부 또는 만료를 확인했습니다. "
                "비공개 iCal 주소를 다시 발급해 주세요."
            ),
            "http_5xx": "연결 단계의 캘린더 서버 오류입니다. 잠시 후 다시 확인해 주세요.",
            "dns_or_connect": "연결 단계 오류입니다. 네트워크와 DNS 상태를 확인해 주세요.",
            "timeout": "연결 단계 응답 시간이 초과되었습니다. 네트워크를 확인해 주세요.",
            "tls_validation": (
                "연결 단계의 보안 인증서를 확인하지 못했습니다. "
                "시스템 시간과 인증서 환경을 확인해 주세요."
            ),
            "body_too_large": (
                "응답 단계에서 캘린더 데이터가 너무 큽니다. "
                "이벤트 범위를 줄이거나 주소를 다시 발급해 주세요."
            ),
            "unsupported_encoding": (
                "응답 단계에서 지원하지 않는 압축 형식을 받았습니다. "
                "비공개 iCal 주소를 다시 발급해 주세요."
            ),
            "unexpected_content_type": (
                "응답 단계에서 캘린더가 아닌 형식을 받았습니다. "
                "브라우저 주소 대신 비공개 iCal 주소를 저장해 주세요."
            ),
            "utf8_decode": (
                "응답 단계에서 문자 인코딩을 읽지 못했습니다. "
                "비공개 iCal 주소를 다시 발급해 주세요."
            ),
            "empty_body": "응답 단계에서 빈 캘린더를 받았습니다. 다시 확인해 주세요.",
            "invalid_ical": (
                "문서 단계에서 올바른 iCal 문서를 확인하지 못했습니다. "
                "비공개 iCal 주소를 다시 발급해 주세요."
            ),
            "secret_unavailable": (
                "저장 단계의 암호화된 비공개 주소를 사용할 수 없습니다. "
                "주소를 다시 입력해 주세요."
            ),
            "calendar_name_mismatch": (
                "캘린더 확인 단계에서 회사 휴가 캘린더가 아닌 피드를 확인했습니다. "
                "올바른 캘린더의 비공개 iCal 주소를 저장해 주세요."
            ),
            "calendar_fetch_failed": (
                "연결 단계를 완료하지 못했습니다. 연결 상태를 확인하고 다시 시도해 주세요."
            ),
        }
        oauth_errors = {
            "client_config_invalid": (
                "내장 Google 연결 설정을 읽지 못했습니다. 앱을 다시 설치하거나 업데이트해 주세요."
            ),
            "browser_launch_failed": (
                "기본 브라우저를 열지 못했습니다. 브라우저 설정을 확인한 뒤 다시 연결해 주세요."
            ),
            "callback_timeout": (
                "Google 로그인과 동의 시간이 초과되었습니다. 연결을 다시 시작해 주세요."
            ),
            "authorization_denied": (
                "읽기 전용 권한 동의가 완료되지 않았습니다. 동의 후 다시 연결해 주세요."
            ),
            "authorization_cancelled": (
                "Google 계정 연결이 취소되었습니다. 필요하면 다시 연결해 주세요."
            ),
            "state_mismatch": (
                "인증 응답을 안전하게 확인하지 못했습니다. 연결을 다시 시작해 주세요."
            ),
            "token_exchange_failed": (
                "Google 인증 완료 정보를 저장하지 못했습니다. 다시 연결해 주세요."
            ),
            "token_refresh_failed": (
                "Google 인증이 만료되었거나 갱신되지 않았습니다. 계정을 다시 연결해 주세요."
            ),
            "token_revocation_failed": (
                "Google 권한을 해제하지 못했습니다. 네트워크를 확인한 뒤 연결 해제를 다시 시도해 주세요."
            ),
            "api_unauthorized": (
                "Google 인증이 유효하지 않습니다. 계정을 다시 연결해 주세요."
            ),
            "api_forbidden": (
                "Google Calendar 읽기 권한 또는 API 사용 설정을 확인해 주세요."
            ),
            "api_rate_limited": (
                "Google Calendar 요청 한도에 도달했습니다. 잠시 후 다시 확인해 주세요."
            ),
            "api_unavailable": (
                "Google Calendar API를 사용할 수 없습니다. 잠시 후 다시 확인해 주세요."
            ),
            "invalid_response": (
                "Google Calendar 응답을 확인하지 못했습니다. 잠시 후 다시 확인해 주세요."
            ),
            "calendar_not_found": (
                "사용 가능한 휴가 캘린더를 찾지 못했습니다. 캘린더 접근 권한을 확인해 주세요."
            ),
            "calendar_ambiguous": (
                "휴가 캘린더를 하나로 결정하지 못했습니다. 캘린더 구성을 확인해 주세요."
            ),
        }
        state_messages = (
            oauth_state_messages if provider == "google_oauth" else private_state_messages
        )
        error_labels = oauth_errors if provider == "google_oauth" else private_errors
        message = (
            error_labels.get(
                error_code,
                (
                    "Google 계정 연결을 확인하지 못했습니다. 다시 연결하거나 잠시 후 다시 시도해 주세요."
                    if provider == "google_oauth"
                    else "캘린더 확인 단계를 완료하지 못했습니다. 주소와 연결 상태를 확인해 주세요."
                ),
            )
            if state == "error"
            else state_messages[state]
        )
        parts = [provider_label, state_labels[state], message]
        if has_last_good and state in {
            "authorizing", "disconnecting", "loading", "stale", "error"
        }:
            parts.append("마지막 성공값으로 계산 중입니다.")
        if last_success:
            parts.append(f"마지막 성공: {last_success}")
        try:
            var.set(" · ".join(parts))
        except Exception:
            pass
        return

    def _toggle_advanced_ical(self) -> None:
        frame = self._advanced_ical_frame
        if frame is None:
            return
        self._advanced_ical_visible = not bool(self._advanced_ical_visible)
        try:
            if self._advanced_ical_visible:
                frame.grid()
            else:
                frame.grid_remove()
        except Exception:
            self._advanced_ical_visible = False
        try:
            self._advanced_toggle_var.set(
                "고급 설정 · 비공개 iCal 접기"
                if self._advanced_ical_visible
                else "고급 설정 · 비공개 iCal 펼치기"
            )
        except Exception:
            pass
        self._refresh_scroll_region()
        return

    def _google_status_from_settings(self, settings: Any) -> dict[str, Any]:
        data = settings if isinstance(settings, dict) else {}
        nested = data.get("google_calendar_status")
        if isinstance(nested, dict):
            return dict(nested)
        configured = bool(data.get("oauth_configured", False))
        return {
            "configured": configured,
            "secret_present": configured,
            "state": "fresh" if configured else "unconfigured",
            "error_code": "",
            "catalog_loading": False,
            "catalog": [],
            "break_role_configured": False,
            "vacation_role_configured": False,
            "break_role_handle": "",
            "vacation_role_handle": "",
        }

    def _read_google_calendar_status_snapshot(self) -> dict[str, Any] | None:
        getter = getattr(self._wrike, "get_google_calendar_status_snapshot", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:
            return None
        return dict(value) if isinstance(value, dict) else None

    def _refresh_google_calendar_status(self, status: Any = None) -> None:
        if isinstance(status, dict):
            self._google_calendar_status = dict(status)
        data = dict(self._google_calendar_status)
        configured = bool(data.get("configured", False))
        state = str(data.get("state") or "unconfigured").strip().lower()
        if state not in {"unconfigured", "authorizing", "disconnecting", "fresh", "error"}:
            state = "error"
        error_code = str(data.get("error_code") or "").strip()
        loading = bool(data.get("catalog_loading", False))
        state_messages = {
            "unconfigured": "연결되지 않음",
            "authorizing": "브라우저에서 Google 로그인과 읽기 전용 권한 동의를 완료해 주세요.",
            "disconnecting": "Google 권한과 로컬 인증 정보를 해제하고 있습니다.",
            "fresh": "연결됨 · 휴게/휴가 캘린더를 각각 선택하세요.",
            "error": "연결 상태를 확인하지 못했습니다. 다시 연결하거나 새로고침해 주세요.",
        }
        if loading:
            message = "사용 가능한 캘린더를 불러오는 중입니다."
        elif state == "error" and error_code == "client_config_invalid":
            message = "내장 Google 연결 설정을 읽지 못했습니다. 앱을 다시 설치하거나 업데이트해 주세요."
        elif state == "error" and error_code == "token_refresh_failed":
            message = "Google 인증을 갱신하지 못했습니다. 연결을 해제한 뒤 다시 연결해 주세요."
        else:
            message = state_messages[state]
        try:
            self._google_status_var.set(message)
            self._google_action_var.set(
                "Google 계정 연결 해제" if configured or state == "disconnecting" else "Google 계정 연결"
            )
        except Exception:
            pass

        handles: dict[str, str] = {}
        handle_to_label: dict[str, str] = {}
        used_display_labels: set[str] = {"선택 안 함", "현재 선택됨 · 새로고침 필요"}
        for item in data.get("catalog") or []:
            if not isinstance(item, dict):
                continue
            handle = str(item.get("handle") or "").strip()
            label = str(item.get("label") or "").strip()
            if not handle or not label:
                continue
            display = label
            suffix = 2
            while display in used_display_labels:
                display = f"{label} ({suffix})"
                suffix += 1
            used_display_labels.add(display)
            handles[display] = handle
            handle_to_label[handle] = display
        self._google_handle_by_label = handles
        values = ("선택 안 함", *handles.keys())
        break_handle = str(data.get("break_role_handle") or "").strip()
        vacation_handle = str(data.get("vacation_role_handle") or "").strip()
        break_value = handle_to_label.get(break_handle, "선택 안 함")
        vacation_value = handle_to_label.get(vacation_handle, "선택 안 함")
        if bool(data.get("break_role_configured", False)) and not break_handle:
            break_value = "현재 선택됨 · 새로고침 필요"
        if bool(data.get("vacation_role_configured", False)) and not vacation_handle:
            vacation_value = "현재 선택됨 · 새로고침 필요"
        if break_value not in values:
            values = (*values, break_value)
        if vacation_value not in values:
            values = (*values, vacation_value)
        selectors_enabled = configured and state == "fresh"
        try:
            self._google_break_combo.configure(
                values=values,
                state="readonly" if selectors_enabled else "disabled",
            )
            self._google_vacation_combo.configure(
                values=values,
                state="readonly" if selectors_enabled else "disabled",
            )
            self._google_break_var.set(break_value)
            self._google_vacation_var.set(vacation_value)
        except Exception:
            pass
        return

    def _refresh_google_status_from_backend(self) -> bool:
        status = self._read_google_calendar_status_snapshot()
        if status is None:
            return False
        self._refresh_google_calendar_status(status)
        return True

    def _on_google_account_action(self) -> None:
        data = self._read_google_calendar_status_snapshot() or self._google_calendar_status
        if bool(data.get("configured", False)) or str(data.get("state") or "") == "disconnecting":
            self._on_disconnect_google_calendar_oauth()
        else:
            self._on_connect_google_calendar_oauth()
        return

    def _on_refresh_google_calendar_catalog(self) -> None:
        refresh = getattr(self._wrike, "refresh_google_calendar_catalog", None)
        if not callable(refresh):
            self._set_status("Google 캘린더 새로고침을 지원하지 않습니다.", level="error")
            return
        current = dict(self._google_calendar_status)
        current["catalog_loading"] = True
        self._refresh_google_calendar_status(current)

        def worker():
            try:
                return refresh()
            except Exception:
                return False, "api_unavailable"

        def apply_result(result) -> None:
            ok = bool(isinstance(result, tuple) and len(result) == 2 and result[0])
            self._refresh_google_status_from_backend()
            self._set_status(
                "Google 캘린더 목록을 새로고쳤습니다." if ok else "Google 캘린더 목록을 불러오지 못했습니다.",
                level="ok" if ok else "error",
            )

        self._run_bg(worker, apply_result)
        return

    def _on_google_role_selected(self, role: str) -> None:
        if self._google_role_updating or role not in {"break", "vacation"}:
            return
        var = self._google_break_var if role == "break" else self._google_vacation_var
        try:
            label = str(var.get() or "").strip()
        except Exception:
            return
        if label == "현재 선택됨 · 새로고침 필요":
            return
        self._google_role_updating = True
        try:
            if label == "선택 안 함":
                action = getattr(self._wrike, "clear_google_calendar_role", None)
                result = action(role) if callable(action) else (False, None)
            else:
                handle = self._google_handle_by_label.get(label, "")
                action = getattr(self._wrike, "bind_google_calendar_role", None)
                result = action(role, handle) if callable(action) and handle else (False, None)
            ok = bool(isinstance(result, tuple) and len(result) == 2 and result[0])
        except Exception:
            ok = False
        finally:
            self._google_role_updating = False
        self._refresh_google_status_from_backend()
        role_label = "휴게" if role == "break" else "휴가"
        self._set_status(
            f"{role_label} 캘린더 선택을 저장했습니다." if ok else f"{role_label} 캘린더 선택을 저장하지 못했습니다.",
            level="ok" if ok else "error",
        )
        return

    def _sync_runtime_snapshots(self) -> None:
        break_getter = getattr(self._wrike, "get_manual_break_state", None)
        if callable(break_getter):
            try:
                break_state = break_getter()
            except Exception:
                break_state = None
            if isinstance(break_state, dict):
                self._refresh_break_button_label(break_state)
        self._refresh_google_status_from_backend()
        self._refresh_vacation_status_from_backend()
        return

    def _mark_ical_dirty(self, _event: Any = None) -> None:
        self._ical_dirty = True
        self._schedule_autosave()
        return

    def _mark_vacation_ical_dirty(self, _event: Any = None) -> None:
        self._vacation_ical_dirty = True
        self._schedule_autosave()
        return

    def _load_settings(self) -> None:
        self._loading_settings = True
        try:
            settings = self._wrike.get_settings_snapshot()
        except Exception:
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        try:
            try:
                self._token_var.set("")
            except Exception:
                pass
            try:
                minutes = int(settings.get("daily_target_minutes", 480))
                self._daily_var.set(self._format_hours(minutes))
            except Exception:
                pass
            try:
                tooltip_ms = int(settings.get("tooltip_duration_ms", 6000))
                self._tooltip_var.set(self._format_seconds(tooltip_ms / 1000.0))
            except Exception:
                pass
            try:
                self._monitor_enabled_var.set(bool(settings.get("monitor_enabled", False)))
            except Exception:
                pass
            try:
                interval = float(settings.get("monitor_interval_sec", 5))
                self._monitor_interval_var.set(str(int(interval)))
            except Exception:
                pass
            try:
                plan = settings.get("workday_plan")
                if not isinstance(plan, dict):
                    plan = {
                        "date": date.today().isoformat(),
                        "target_net_minutes": settings.get("daily_target_minutes", 480),
                        "clock_in": "",
                    }
                self._apply_workday_plan(plan, fallback_day=date.today().isoformat())
            except Exception:
                pass
            try:
                self._lunch_enabled_var.set(
                    bool(settings.get("lunch_break_enabled", True))
                )
                lunch_start = max(
                    0, min(1439, int(settings.get("lunch_start_min", 720)))
                )
                lunch_end = max(
                    1, min(1440, int(settings.get("lunch_end_min", 780)))
                )
                self._lunch_start_var.set(self._format_minutes_as_hhmm(lunch_start))
                self._lunch_end_var.set(self._format_minutes_as_hhmm(lunch_end))
                keywords = [
                    str(item or "").strip()
                    for item in list(settings.get("break_keywords") or [])
                    if str(item or "").strip()
                ]
                self._ical_keywords_var.set(", ".join(keywords))
                poll_minutes = int(
                    round(float(settings.get("ical_poll_interval_sec", 900)) / 60.0)
                )
                self._ical_interval_var.set(str(max(5, min(360, poll_minutes))))
                if self._ical_url_var is not None:
                    self._ical_url_var.set("")
                self._ical_dirty = False
                self._refresh_break_button_label(settings.get("manual_break_state"))
            except Exception:
                pass
            try:
                if self._vacation_ical_url_var is not None:
                    self._vacation_ical_url_var.set("")
                self._vacation_ical_dirty = False
                vacation_status = self._vacation_status_from_settings(settings)
                self._refresh_vacation_ical_status(vacation_status)
                self._refresh_google_calendar_status(
                    self._google_status_from_settings(settings)
                )
            except Exception:
                pass
            try:
                self._status_var.set("")
            except Exception:
                pass
            self._toggle_token_visibility()
            self._restore_folder_path()
        finally:
            self._loading_settings = False
        return

    def _on_clear_token(self) -> None:
        try:
            self._token_var.set("")
        except Exception:
            pass
        ok, err = self._wrike.update_settings({"api_token": "", "clear_api_token": True})
        if ok:
            self._set_status("토큰 삭제 완료", level="ok")
        else:
            self._set_status(f"토큰 삭제 실패: {err}", level="error")
        return

    def _format_minutes_as_hhmm(self, minutes: int) -> str:
        try:
            total = int(minutes)
        except Exception:
            return "12:00"
        total = max(0, min(1440, total))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _parse_hhmm(self, text: str, label: str) -> tuple[int | None, str | None]:
        raw = str(text or "").strip()
        parts = raw.split(":")
        if len(parts) != 2:
            return None, f"{label} 형식은 HH:MM 입니다."
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
        except Exception:
            return None, f"{label} 값이 올바르지 않습니다."
        if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
            return None, f"{label} 값이 올바르지 않습니다."
        return hours * 60 + minutes, None

    def _strict_positive_int(self, text: str, label: str) -> tuple[int, str | None]:
        raw = str(text or "").strip()
        if not raw:
            return 0, f"{label} 값을 입력해 주세요."
        try:
            value = int(raw)
        except Exception:
            return 0, f"{label} 값이 정수가 아닙니다."
        if value <= 0:
            return 0, f"{label} 값은 0보다 커야 합니다."
        return value, None

    def _manual_break_elapsed_seconds(self, state: dict[str, Any]) -> int:
        try:
            base_seconds = max(0, int(state.get("ongoing_seconds", 0) or 0))
        except Exception:
            base_seconds = 0
        if base_seconds <= 0:
            try:
                base_seconds = max(
                    0, int(state.get("ongoing_minutes", 0) or 0) * 60
                )
            except Exception:
                base_seconds = 0
        started_at = str(state.get("started_at") or "").strip()
        if not started_at:
            return base_seconds
        try:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            now = datetime.now(parsed.tzinfo) if parsed.tzinfo is not None else datetime.now()
            elapsed = max(0, int((now - parsed).total_seconds()))
            return max(base_seconds, elapsed)
        except Exception:
            return base_seconds

    def _render_break_button_label(self) -> None:
        var = self._break_button_var
        if var is None:
            return
        data = dict(self._manual_break_state)
        try:
            if bool(data.get("active")):
                started_at = str(data.get("started_at") or "")
                hhmm = started_at[11:16] if len(started_at) >= 16 else ""
                elapsed = self._manual_break_elapsed_seconds(data)
                duration = f"{elapsed // 60}분 {elapsed % 60:02d}초"
                details = [item for item in (hhmm, duration) if item]
                text = "휴게 종료"
                if details:
                    text += f"({' · '.join(details)})"
                var.set(text)
            else:
                completed = int(data.get("completed_minutes", 0) or 0)
                var.set(f"휴게 시작 · 누적 {completed}분")
        except Exception:
            try:
                var.set("휴게 시작")
            except Exception:
                pass
        return

    def _refresh_break_button_label(self, state: Any) -> None:
        self._manual_break_state = dict(state) if isinstance(state, dict) else {}
        self._render_break_button_label()
        return

    def _break_timer_is_alive(self, win: Any) -> bool:
        if win is None or self._win is not win:
            return False
        exists = getattr(win, "winfo_exists", None)
        if callable(exists):
            try:
                return bool(exists())
            except Exception:
                return False
        return True

    def _schedule_break_timer(self, win: Any) -> None:
        if not self._break_timer_is_alive(win):
            self._break_timer_after_id = None
            if self._break_timer_window is win:
                self._break_timer_window = None
            return
        after = getattr(win, "after", None)
        if not callable(after):
            self._break_timer_after_id = None
            return

        def tick() -> None:
            if self._break_timer_window is not win:
                return
            self._break_timer_after_id = None
            if not self._break_timer_is_alive(win):
                self._break_timer_window = None
                return
            self._sync_runtime_snapshots()
            if bool(self._manual_break_state.get("active")):
                self._render_break_button_label()
            self._schedule_break_timer(win)
            return

        self._break_timer_after_id = "pending"
        try:
            timer_id = after(1000, tick)
            if self._break_timer_after_id == "pending":
                self._break_timer_after_id = timer_id if timer_id is not None else "scheduled"
        except Exception:
            self._break_timer_after_id = None
        return

    def _start_break_timer(self) -> None:
        win = self._win
        if win is None:
            return
        if self._break_timer_window is win and self._break_timer_after_id is not None:
            return
        self._break_timer_window = win
        self._break_timer_after_id = None
        self._schedule_break_timer(win)
        return

    def _on_toggle_break(self) -> None:
        def worker():
            try:
                return self._wrike.toggle_manual_break()
            except Exception as exc:
                return {"ok": False, "message": f"휴게 토글 실패: {exc}"}

        def apply_result(result) -> None:
            state = result if isinstance(result, dict) else {}
            message = str(state.get("message") or "").strip()
            level = "ok" if state.get("ok") is True else "error"
            try:
                if message:
                    self._set_status(message, level=level)
            except Exception:
                pass
            self._refresh_break_button_label(state)

        self._run_bg(worker, apply_result)
        return

    def _on_clear_ical(self) -> None:
        ok, err = self._wrike.update_settings({"clear_ical_url": True})
        if ok:
            try:
                if self._ical_url_var is not None:
                    self._ical_url_var.set("")
            except Exception:
                pass
            self._ical_dirty = False
            self._set_status("휴게 캘린더 URL 삭제 완료", level="ok")
        else:
            self._set_status("휴게 캘린더 URL 삭제 실패", level="error")
        return

    @staticmethod
    def _vacation_save_error_message(error: object) -> str:
        code = str(error or "").strip()
        messages = {
            "vacation_ical_invalid_endpoint": (
                "휴가 캘린더 저장 실패: Google Calendar의 비공개 주소"
                "(iCal 형식)만 지원합니다."
            ),
            "vacation_ical_secret_protection_failed": (
                "휴가 캘린더 저장 실패: 비공개 주소를 안전하게 암호화하지 못했습니다."
            ),
        }
        return messages.get(code, f"저장 실패: {code or '알 수 없는 오류'}")

    def _request_vacation_ical_retry(self) -> tuple[bool, str | None]:
        retry = getattr(self._wrike, "retry_vacation_ical", None)
        if not callable(retry):
            return False, "calendar_fetch_failed"
        try:
            result = retry()
        except Exception:
            return False, "calendar_fetch_failed"
        if not isinstance(result, tuple) or len(result) != 2:
            return False, "calendar_fetch_failed"
        return bool(result[0]), str(result[1] or "") or None

    def _on_retry_vacation_ical(self) -> None:
        ok, error = self._request_vacation_ical_retry()
        if ok:
            if not self._refresh_vacation_status_from_backend():
                fallback = dict(self._vacation_ical_status)
                fallback.update({"state": "loading", "error_code": ""})
                self._refresh_vacation_ical_status(fallback)
            self._set_status("휴가 캘린더 연결 확인을 시작했습니다.", level="info")
            return
        fallback = dict(self._vacation_ical_status)
        fallback.update({"state": "error", "error_code": error or "calendar_fetch_failed"})
        self._refresh_vacation_ical_status(fallback)
        self._set_status("휴가 캘린더 연결 확인을 시작하지 못했습니다.", level="error")

    def _on_clear_vacation_ical(self) -> None:
        try:
            ok, err = self._wrike.update_settings(
                {"clear_vacation_ical_url": True}
            )
        except Exception as exc:
            ok, err = False, str(exc) or "알 수 없는 오류"
        if ok:
            try:
                if self._vacation_ical_url_var is not None:
                    self._vacation_ical_url_var.set("")
            except Exception:
                pass
            self._vacation_ical_dirty = False
            if not self._refresh_vacation_status_from_backend():
                fallback = dict(self._vacation_ical_status)
                fallback.update({
                    "secret_present": False,
                    "configured": False,
                    "observed_calendar_name": "",
                    "state": "unconfigured",
                    "last_success_ts": None,
                    "error_code": "",
                    "fetch_running": False,
                    "has_last_good": False,
                })
                self._refresh_vacation_ical_status(fallback)
            self._set_status("휴가 캘린더 URL 삭제 완료", level="ok")
        else:
            self._set_status("휴가 캘린더 URL 삭제 실패", level="error")
        return

    def _on_connect_google_calendar_oauth(self) -> None:
        begin = getattr(self._wrike, "begin_google_calendar_oauth", None)
        try:
            result = begin() if callable(begin) else (False, None)
        except Exception:
            result = (False, None)
        if not isinstance(result, tuple) or len(result) != 2:
            ok, error = False, None
        else:
            ok, error = bool(result[0]), str(result[1] or "").strip() or None
        fallback = dict(self._google_calendar_status)
        fallback.update({
            "state": "authorizing" if ok else "error",
            "error_code": "" if ok else (error or "client_config_invalid"),
            "catalog_loading": bool(ok),
        })
        self._refresh_google_calendar_status(fallback)
        if ok:
            self._set_status(
                "Google 계정 인증을 시작했습니다. 브라우저에서 로그인하고 동의해 주세요.",
                level="info",
            )
        else:
            self._set_status("Google 계정 연결을 시작하지 못했습니다.", level="error")
        return

    def _on_disconnect_google_calendar_oauth(self) -> None:
        disconnect = getattr(
            self._wrike, "disconnect_google_calendar_oauth", None
        )
        try:
            result = disconnect() if callable(disconnect) else (False, None)
        except Exception:
            result = (False, None)
        if not isinstance(result, tuple) or len(result) != 2:
            ok, error = False, None
        else:
            ok, error = bool(result[0]), str(result[1] or "").strip() or None
        if ok:
            status = self._read_google_calendar_status_snapshot()
            if isinstance(status, dict):
                self._refresh_google_calendar_status(status)
            else:
                fallback = dict(self._google_calendar_status)
                fallback.update({
                    "state": "disconnecting",
                    "error_code": "",
                    "catalog_loading": False,
                })
                self._refresh_google_calendar_status(fallback)
            current_state = str(
                self._google_calendar_status.get("state") or ""
            ).strip().lower()
            if current_state == "unconfigured":
                self._set_status("Google 계정 연결 해제 완료", level="ok")
            else:
                self._set_status(
                    "Google 권한과 로컬 연결 정보 해제를 시작했습니다.",
                    level="info",
                )
            return
        fallback = dict(self._google_calendar_status)
        fallback.update({
            "state": "error",
            "error_code": error or "api_unauthorized",
        })
        self._refresh_google_calendar_status(fallback)
        self._set_status("Google 계정 연결 해제 실패", level="error")
        return

    def _on_reload(self) -> None:
        try:
            self._set_status("로드 중...", level="info")
        except Exception:
            pass
        ok = False
        msg = None
        try:
            ok, msg = self._wrike.reload_settings_from_disk()
        except Exception:
            ok = False
            msg = None
        self._load_settings()
        if msg:
            if "복구" in msg or "실패" in msg:
                self._set_status(str(msg), level="error")
            else:
                self._set_status(str(msg), level="info")
        else:
            self._set_status("로드 완료" if ok else "로드 실패", level=("ok" if ok else "error"))
        return

    def _open_settings_file(self, path: str) -> None:
        try:
            import os
            if path and os.path.isfile(path):
                os.startfile(path)
        except Exception:
            return

    def _on_validate_token(self) -> None:
        token = str(self._token_var.get() or "").strip()
        if not token:
            self._set_status("API 토큰을 입력하세요", level="error")
            return
        self._set_status("토큰 검증 중...", level="info")

        def worker() -> tuple[bool, str | None, str | None]:
            try:
                return self._wrike.validate_api_token(token)
            except Exception:
                return False, None, "토큰 검증 실패"

        def apply_result(result: tuple[bool, str | None, str | None]) -> None:
            ok_val, name_val, msg_val = result
            if ok_val:
                label = name_val or "내 계정"
                self._set_status(f"{label}님 어서오세요.", level="ok")
            else:
                self._set_status(str(msg_val or "토큰 검증 실패"), level="error")

        self._run_bg(worker, apply_result)
        return

    def _auto_validate_token(self) -> None:
        token = str(self._token_var.get() or "").strip()
        if not token:
            return
        self._on_validate_token()
        return

    def _run_bg(self, fn, on_done) -> None:
        win = self._win
        if win is None:
            return

        def worker() -> None:
            result = fn()
            self._post_ui(lambda: on_done(result))

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
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

    def _on_save(self) -> None:
        self._save_settings()
        return

    def _register_autosave_traces(self) -> None:
        for var in (
            self._token_var,
            self._daily_var,
            self._tooltip_var,
            self._monitor_enabled_var,
            self._monitor_interval_var,
            self._lunch_enabled_var,
            self._lunch_start_var,
            self._lunch_end_var,
            self._ical_keywords_var,
            self._ical_interval_var,
        ):
            self._bind_autosave_var(var)
        return

    def _bind_autosave_var(self, var: Any) -> None:
        tracer = getattr(var, "trace_add", None)
        if not callable(tracer):
            return
        try:
            tracer("write", lambda *_args: self._schedule_autosave())
        except Exception:
            return

    def _schedule_autosave(self) -> None:
        if bool(self._loading_settings):
            return
        win = self._win
        after_cancel = getattr(win, "after_cancel", None)
        if self._autosave_after_id is not None and callable(after_cancel):
            try:
                after_cancel(self._autosave_after_id)
            except Exception:
                pass
        self._autosave_after_id = None
        after = getattr(win, "after", None)
        if callable(after):
            try:
                self._autosave_after_id = after(350, self._autosave_now)
                return
            except Exception:
                self._autosave_after_id = None
        self._autosave_now()
        return

    def _autosave_now(self) -> None:
        self._autosave_after_id = None
        self._save_settings()
        return

    def _strict_positive_float(self, text: str, label: str) -> tuple[float, str | None]:
        raw = str(text or "").strip()
        if not raw:
            return 0.0, f"{label} 값을 입력해 주세요."
        try:
            value = float(raw)
        except Exception:
            return 0.0, f"{label} 값이 숫자가 아닙니다."
        if value <= 0:
            return 0.0, f"{label} 값은 0보다 커야 합니다."
        return float(value), None

    def _save_settings(self) -> None:
        token = str(self._token_var.get() or "").strip()
        daily_text = str(self._daily_var.get() or "").strip()
        tooltip_text = str(self._tooltip_var.get() or "").strip()
        monitor_enabled = bool(self._monitor_enabled_var.get())
        interval_text = str(self._monitor_interval_var.get() or "").strip()

        lunch_enabled = (
            bool(self._lunch_enabled_var.get()) if self._lunch_enabled_var is not None else True
        )
        lunch_start_text = (
            str(self._lunch_start_var.get() or "") if self._lunch_start_var is not None else "12:00"
        )
        lunch_end_text = (
            str(self._lunch_end_var.get() or "") if self._lunch_end_var is not None else "13:00"
        )
        keywords_raw = (
            str(self._ical_keywords_var.get() or "")
            if self._ical_keywords_var is not None
            else ""
        )
        poll_text = (
            str(self._ical_interval_var.get() or "")
            if self._ical_interval_var is not None
            else "15"
        )
        ical_dirty = bool(self._ical_dirty)
        ical_url = ""
        if ical_dirty and self._ical_url_var is not None:
            try:
                ical_url = str(self._ical_url_var.get() or "").strip()
            except Exception:
                ical_url = ""
        vacation_dirty = bool(self._vacation_ical_dirty)
        vacation_ical_url = ""
        if vacation_dirty and self._vacation_ical_url_var is not None:
            try:
                vacation_ical_url = str(
                    self._vacation_ical_url_var.get() or ""
                ).strip()
            except Exception:
                vacation_ical_url = ""

        lunch_start_min, error = self._parse_hhmm(lunch_start_text, "점심 시작 시간")
        if error:
            self._set_status(f"저장 실패: {error}", level="error")
            return
        lunch_end_min, error = self._parse_hhmm(lunch_end_text, "점심 종료 시간")
        if error:
            self._set_status(f"저장 실패: {error}", level="error")
            return
        if int(lunch_end_min) <= int(lunch_start_min):
            self._set_status("저장 실패: 점심 종료 시간은 시작 시간 이후여야 합니다.", level="error")
            return
        parsed_keywords = [piece.strip() for piece in keywords_raw.split(",") if piece.strip()]
        poll_minutes, error = self._strict_positive_int(poll_text, "캘린더 조회 주기(분)")
        if error:
            self._set_status(f"저장 실패: {error}", level="error")
            return

        daily_hours, error = self._strict_positive_float(daily_text, "일 목표 시간")
        if error:
            self._set_status(f"저장 실패: {error}", level="error")
            return
        tooltip_sec, error = self._strict_positive_float(tooltip_text, "툴팁 표시 시간")
        if error:
            self._set_status(f"저장 실패: {error}", level="error")
            return
        interval_sec, error = self._strict_positive_float(interval_text, "모니터링 주기")
        if error:
            self._set_status(f"저장 실패: {error}", level="error")
            return

        daily_minutes = int(round(daily_hours * 60))
        tooltip_ms = int(round(tooltip_sec * 1000))

        save_payload = {
            "api_token": token,
            "daily_target_minutes": daily_minutes,
            "tooltip_duration_ms": tooltip_ms,
            "monitor_enabled": monitor_enabled,
            "monitor_interval_sec": interval_sec,
            "lunch_break_enabled": lunch_enabled,
            "lunch_start_min": int(lunch_start_min),
            "lunch_end_min": int(lunch_end_min),
            "break_keywords": parsed_keywords,
            "ical_poll_interval_sec": int(poll_minutes) * 60,
        }
        if ical_dirty:
            save_payload["ical_url"] = ical_url
        if vacation_dirty:
            save_payload["vacation_ical_url"] = vacation_ical_url

        ok, err = self._wrike.update_settings(save_payload)
        try:
            if ok:
                if ical_dirty:
                    self._ical_dirty = False
                if vacation_dirty:
                    try:
                        if self._vacation_ical_url_var is not None:
                            self._vacation_ical_url_var.set("")
                    except Exception:
                        pass
                    self._vacation_ical_dirty = False
                    if not self._refresh_vacation_status_from_backend():
                        fallback = dict(self._vacation_ical_status)
                        fallback.update({
                            "provider": "private_ical",
                            "secret_present": bool(vacation_ical_url),
                            "configured": bool(vacation_ical_url),
                            "observed_calendar_name": "",
                            "state": (
                                "loading"
                                if vacation_ical_url
                                else "unconfigured"
                            ),
                            "last_success_ts": None,
                            "error_code": "",
                            "fetch_running": False,
                            "has_last_good": False,
                        })
                        self._refresh_vacation_ical_status(fallback)
                    retry = getattr(self._wrike, "retry_vacation_ical", None)
                    if vacation_ical_url and callable(retry):
                        self._request_vacation_ical_retry()
                        self._refresh_vacation_status_from_backend()
                self._set_status(
                    "저장됨 · 휴가 캘린더 연결 확인 중"
                    if vacation_dirty and vacation_ical_url
                    else "저장됨",
                    level="ok",
                )
            else:
                if str(err or "") == "vacation_ical_invalid_endpoint":
                    fallback = dict(self._vacation_ical_status)
                    fallback.update({
                        "state": "error",
                        "error_code": "invalid_endpoint",
                        "has_last_good": False,
                    })
                    self._refresh_vacation_ical_status(fallback)
                self._set_status(
                    self._vacation_save_error_message(err),
                    level="error",
                )
        except Exception:
            pass
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

    def _parse_hours_to_minutes(self, text: str) -> int:
        if not text:
            return 480
        try:
            hours = float(text)
        except Exception:
            return 480
        if hours <= 0:
            return 480
        return int(round(hours * 60))

    def _parse_seconds_to_ms(self, text: str) -> int:
        if not text:
            return 6000
        try:
            seconds = float(text)
        except Exception:
            return 6000
        if seconds <= 0:
            return 6000
        return int(round(seconds * 1000))

    def _parse_seconds(self, text: str) -> float:
        if not text:
            return 5.0
        try:
            seconds = float(text)
        except Exception:
            return 5.0
        if seconds <= 0:
            return 5.0
        return seconds

    def _restore_folder_path(self) -> None:
        try:
            saved_path = self._wrike.get_monitor_folder_path()
        except Exception:
            saved_path = []
        self._clear_folder_levels(0)
        self._update_folder_path_label()
        if not saved_path:
            self._load_folder_level(0, None, saved_path)
            return
        self._folder_restoring = True
        self._load_folder_level(0, None, saved_path)
        return

    def _load_folder_level(
        self, level: int, parent_id: str | None, saved_path: list[dict] | None = None,
    ) -> None:
        is_space_level = (level == 0)

        def worker():
            if is_space_level:
                return self._wrike.fetch_spaces()
            return self._wrike.fetch_child_folders(str(parent_id or ""))

        def on_done(result):
            items, error = result
            if error:
                self._set_status(str(error), level="error")
                self._folder_restoring = False
                return
            if not items:
                self._folder_restoring = False
                return
            self._add_folder_combo(level, items, saved_path)

        self._run_bg(worker, on_done)
        return

    def _add_folder_combo(
        self, level: int, items: list[dict], saved_path: list[dict] | None = None,
    ) -> None:
        tk = self._tk
        ttk = self._ttk
        if tk is None or ttk is None:
            return
        frame = self._folder_path_frame
        if frame is None:
            return

        combo_frame = tk.Frame(frame, bg="#FFFFFF")
        combo_frame.pack(fill="x", pady=1)

        label_text = "스페이스:" if level == 0 else f"레벨 {level}:"
        tk.Label(
            combo_frame,
            text=label_text,
            bg="#FFFFFF",
            fg="#6B7280",
            font=("Segoe UI", 8),
            width=8,
            anchor="e",
        ).pack(side="left", padx=(0, 4))

        titles = [str(item.get("title") or "") for item in items]
        combo = ttk.Combobox(
            combo_frame,
            values=titles,
            state="readonly",
            width=50,
            font=("Segoe UI", 9),
        )
        combo.pack(side="left", fill="x", expand=True)

        suggest_idx = self._wrike.suggest_folder_index(items)

        level_info = {
            "frame": combo_frame,
            "combo": combo,
            "items": items,
            "level": level,
        }
        self._folder_levels.append(level_info)

        def on_select(_event=None):
            sel = combo.current()
            if sel < 0:
                return
            self._clear_folder_levels(level + 1)
            self._update_folder_path_label()
            selected = items[sel]
            has_children = bool(selected.get("has_children", True))
            if selected.get("type") == "space" or has_children:
                self._load_folder_level(level + 1, selected["id"])

        combo.bind("<<ComboboxSelected>>", on_select)

        pre_select_idx = None
        if saved_path and level < len(saved_path):
            saved_id = str(saved_path[level].get("id") or "")
            for i, item in enumerate(items):
                if str(item.get("id") or "") == saved_id:
                    pre_select_idx = i
                    break
            if pre_select_idx is None and suggest_idx is not None:
                pre_select_idx = suggest_idx
        elif suggest_idx is not None:
            pre_select_idx = suggest_idx

        if suggest_idx is not None and suggest_idx < len(titles):
            tag = " ← 추천"
            current_title = titles[suggest_idx]
            if tag not in current_title:
                titles[suggest_idx] = current_title + tag
                combo["values"] = titles

        if pre_select_idx is not None and 0 <= pre_select_idx < len(items):
            combo.current(pre_select_idx)
            selected = items[pre_select_idx]
            has_children = bool(selected.get("has_children", True))
            if selected.get("type") == "space" or has_children:
                next_saved = saved_path if (saved_path and level + 1 < len(saved_path)) else None
                self._load_folder_level(level + 1, selected["id"], next_saved)
            else:
                self._folder_restoring = False
                self._update_folder_path_label()
        else:
            self._folder_restoring = False
            self._update_folder_path_label()
        return

    def _clear_folder_levels(self, from_level: int) -> None:
        while len(self._folder_levels) > from_level:
            info = self._folder_levels.pop()
            try:
                info["frame"].destroy()
            except Exception:
                pass
        return

    def _get_current_path(self) -> list[dict]:
        path: list[dict] = []
        for info in self._folder_levels:
            combo = info.get("combo")
            items = info.get("items", [])
            if combo is None:
                break
            try:
                idx = combo.current()
            except Exception:
                break
            if idx < 0 or idx >= len(items):
                break
            item = items[idx]
            path.append({
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "type": str(item.get("type") or "folder"),
            })
        return path

    def _update_folder_path_label(self) -> None:
        label = self._folder_path_label
        if label is None:
            return
        path = self._get_current_path()
        if not path:
            try:
                label.configure(text="경로 미선택 (전체 타임로그 조회)")
            except Exception:
                pass
            return
        names = [str(p.get("title") or "?") for p in path]
        text = " / ".join(names)
        try:
            label.configure(text=text)
        except Exception:
            pass
        return

    def _on_save_folder_path(self) -> None:
        path = self._get_current_path()
        try:
            self._wrike.set_monitor_folder_path(path)
        except Exception:
            self._set_status("경로 저장 실패", level="error")
            return
        if path:
            self._set_status("폴더 경로 저장 완료", level="ok")
        else:
            self._set_status("폴더 경로 초기화됨 (전체 조회)", level="ok")
        return

    def _on_clear_folder_path(self) -> None:
        try:
            self._wrike.clear_monitor_folder_path()
        except Exception:
            pass
        self._clear_folder_levels(0)
        self._update_folder_path_label()
        self._load_folder_level(0, None, None)
        self._set_status("폴더 경로 초기화 완료", level="ok")
        return

    def _on_refresh_cache(self) -> None:
        try:
            self._wrike.invalidate_folder_cache()
        except Exception:
            pass
        saved_path = None
        try:
            saved_path = self._wrike.get_monitor_folder_path()
        except Exception:
            pass
        self._clear_folder_levels(0)
        self._load_folder_level(0, None, saved_path or [])
        self._set_status("캐시 새로고침 완료", level="ok")
        return
