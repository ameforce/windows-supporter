from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Any
import threading


class WrikeSettingsView:
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
        self._vacation_ical_dirty = False
        self._vacation_ical_status: dict[str, Any] = {}
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

        add_label("휴게 캘린더 비공개 iCal URL")
        ical_entry_holder = add_entry(self._ical_url_var, show="*")
        try:
            ical_entry_holder.bind("<KeyRelease>", self._mark_ical_dirty)
        except Exception:
            pass
        ttk.Button(content, text="지우기", command=self._on_clear_ical).grid(
            row=row, column=2, sticky="w", padx=(8, 0)
        )
        row += 1

        add_label("휴게 캘린더 헬스장 키워드(쉼표 구분)")
        add_entry(self._ical_keywords_var)
        row += 1

        add_label("휴게 캘린더 조회 주기(분)")
        add_entry(self._ical_interval_var)
        row += 1

        tk.Label(
            content,
            text="── 휴가 캘린더 ──",
            bg=card_bg,
            fg="#2563EB",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(14, 4))
        row += 1

        add_label("휴가 캘린더 비공개 iCal URL")
        vacation_entry = add_entry(self._vacation_ical_url_var, show="*")
        try:
            vacation_entry.bind("<KeyRelease>", self._mark_vacation_ical_dirty)
        except Exception:
            pass
        ttk.Button(
            content, text="지우기", command=self._on_clear_vacation_ical
        ).grid(row=row, column=2, sticky="w", padx=(8, 0))
        row += 1

        tk.Label(
            content,
            text="휴가 캘린더 상태",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 9),
        ).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=6)

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

        vacation_status_label = tk.Label(
            content,
            textvariable=self._vacation_ical_status_var,
            bg=card_bg,
            fg=text_muted,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
        )
        bind_dynamic_wrap(vacation_status_label)
        vacation_status_label.grid(
            row=row, column=1, columnspan=2, sticky="we", pady=6
        )
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

    def _vacation_status_from_settings(self, settings: Any) -> dict[str, Any]:
        data = settings if isinstance(settings, dict) else {}
        nested = data.get("vacation_ical_status")
        if isinstance(nested, dict):
            sanitized = dict(nested)
            sanitized["expected_calendar_name"] = ""
            sanitized["observed_calendar_name"] = ""
            return sanitized
        configured = bool(
            data.get(
                "vacation_ical_configured",
                data.get("vacation_ical_url_configured", False),
            )
        )
        return {
            "secret_present": bool(
                data.get("vacation_ical_secret_present", configured)
            ),
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
            self._vacation_ical_status = dict(status)
        data = dict(self._vacation_ical_status)
        var = self._vacation_ical_status_var
        if var is None:
            return
        state = str(data.get("state") or "unconfigured").strip().lower()
        if state not in {"unconfigured", "loading", "fresh", "stale", "error"}:
            state = "error"
        last_success = str(data.get("last_success_ts") or "").strip()
        error_code = str(data.get("error_code") or "").strip()
        has_last_good = bool(data.get("has_last_good", False))
        state_labels = {
            "unconfigured": "미설정",
            "loading": "불러오는 중",
            "fresh": "정상",
            "stale": "마지막 성공값 사용 중",
            "error": "오류",
        }
        state_messages = {
            "unconfigured": "비공개 iCal URL을 입력해 주세요.",
            "loading": "캘린더를 확인하는 중입니다. 잠시 후 다시 확인해 주세요.",
            "fresh": "휴가 캘린더를 정상적으로 확인했습니다.",
            "stale": "연결 상태를 확인한 뒤 다시 시도해 주세요.",
        }
        error_labels = {
            "invalid_endpoint": "비공개 iCal URL 형식을 확인하고 다시 저장해 주세요.",
            "redirect_rejected": "허용되지 않은 이동이 차단되었습니다. 비공개 iCal URL을 다시 발급해 저장해 주세요.",
            "http_4xx": "접근이 거부되었거나 링크가 만료되었습니다. 비공개 iCal URL을 다시 발급해 주세요.",
            "http_5xx": "캘린더 서버 오류입니다. 잠시 후 다시 시도해 주세요.",
            "dns_or_connect": "네트워크 연결과 DNS 상태를 확인한 뒤 다시 시도해 주세요.",
            "timeout": "응답 시간이 초과되었습니다. 네트워크 상태를 확인하고 다시 시도해 주세요.",
            "tls_validation": "보안 연결을 확인하지 못했습니다. 시스템 시간과 인증서 환경을 확인해 주세요.",
            "body_too_large": "캘린더 데이터가 너무 큽니다. 이벤트 범위를 줄이거나 URL을 다시 발급해 주세요.",
            "unsupported_encoding": "지원하지 않는 압축 형식입니다. 비공개 iCal URL을 다시 발급해 주세요.",
            "utf8_decode": "캘린더 문자 인코딩을 읽지 못했습니다. 비공개 iCal URL을 다시 발급해 주세요.",
            "empty_body": "빈 캘린더 응답입니다. 잠시 후 다시 시도하거나 URL을 다시 발급해 주세요.",
            "invalid_ical": "캘린더 문서 형식이 올바르지 않습니다. 비공개 iCal URL을 다시 발급해 주세요.",
            "secret_unavailable": "저장된 비공개 URL을 사용할 수 없습니다. URL을 다시 입력해 주세요.",
            "calendar_name_mismatch": "휴가 캘린더가 아닌 피드입니다. 올바른 비공개 iCal URL을 다시 저장해 주세요.",
            "calendar_fetch_failed": "캘린더를 가져오지 못했습니다. 연결 상태를 확인하고 다시 시도해 주세요.",
        }
        message = (
            error_labels.get(
                error_code,
                "캘린더 상태를 확인하지 못했습니다. URL과 연결 상태를 확인해 주세요.",
            )
            if state == "error"
            else state_messages[state]
        )
        parts = [state_labels[state], message]
        if has_last_good and state in {"loading", "stale", "error"}:
            parts.append("마지막 성공값으로 계산 중입니다.")
        if last_success:
            parts.append(f"마지막 성공: {last_success}")
        try:
            var.set(" · ".join(parts))
        except Exception:
            pass
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
                self._refresh_vacation_ical_status(
                    self._vacation_status_from_settings(settings)
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
                self._set_status("저장됨", level="ok")
            else:
                self._set_status("저장 실패", level="error")
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
