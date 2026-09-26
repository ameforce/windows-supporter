from __future__ import annotations

from typing import Any, Callable

from src.utils.update_monitor import format_update_status_parts


class DashboardView:
    # Two cards must retain enough width for their action buttons and status
    # copy. Below this threshold a single column is narrower overall and lets
    # the outer vertical scroll handle the additional height.
    _TWO_COLUMN_MIN_WIDTH = 760
    # The card grid is packed with this horizontal inset inside the scrolled
    # container, so the viewport must be wider than the grid by twice this.
    _GRID_PADX = 10
    _STATUS_PART_CHROME = 8
    _CALLBACK_ALIASES = {
        "ai_usage.settings": "codex.settings",
        "ai_usage.toggle": "codex.toggle",
        "codex.settings": "ai_usage.settings",
        "codex.toggle": "ai_usage.toggle",
    }

    def __init__(
        self,
        root: Any,
        *,
        status_provider: Callable[[], dict[str, Any]],
        callbacks: dict[str, Callable[[], Any]],
        on_layout_changed: Callable[[], Any] | None = None,
    ) -> None:
        self._root = root
        self._status_provider = status_provider
        self._callbacks = dict(callbacks)
        # Called after a live column switch so the shell can re-fit its
        # content-height ceiling to the layout that is actually shown.
        self._on_layout_changed = (
            on_layout_changed if callable(on_layout_changed) else None
        )
        self._scroll_sync_after_id = None
        self._scroll_sync_running = False
        self._scroll_sync_dirty = False
        self._dashboard_scroll_item_size = None
        self._parent = None
        self._status_frames: dict[str, Any] = {}
        self._toggle_buttons: dict[str, Any] = {}
        self._dashboard_scroll_canvas = None
        self._dashboard_scroll_container = None
        self._dashboard_scrollbar = None
        self._dashboard_scroll_window_id = None
        self._dashboard_grid = None
        self._dashboard_section_cards: list[Any] = []
        self._status_fonts: dict[str, Any] = {}
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
        self._cancel_dashboard_scroll_sync()
        self._dashboard_scroll_item_size = None

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

        canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        container = tk.Frame(canvas, bg=bg)
        window_id = canvas.create_window((0, 0), window=container, anchor="nw")
        self._dashboard_scroll_canvas = canvas
        self._dashboard_scroll_container = container
        self._dashboard_scrollbar = scrollbar
        self._dashboard_scroll_window_id = window_id

        container.bind(
            "<Configure>",
            lambda _event: self._request_dashboard_scroll_sync(),
        )
        canvas.bind(
            "<Configure>",
            lambda _event: self._on_dashboard_viewport_configure(),
        )
        canvas.bind("<Enter>", lambda _event: canvas.focus_set())

        header_card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        header_card.pack(fill="x", padx=10, pady=(2, 6))

        header_inner = tk.Frame(header_card, bg=card_bg)
        header_inner.pack(fill="x", padx=12, pady=7)

        tk.Label(
            header_inner,
            text="Dashboard",
            bg=card_bg,
            fg=text,
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")
        ttk.Button(header_inner, text="새로고침", command=self.refresh).pack(side="right")

        # 기능 섹션은 2열 카드 그리드로 배치한다. 세로 나열은 요약 화면을
        # 스크롤 없이 한눈에 보려는 대시보드 목적과 맞지 않았다.
        grid = tk.Frame(container, bg=bg)
        grid.pack(fill="both", expand=True, padx=self._GRID_PADX, pady=(0, 2))
        section_cards: list[Any] = []
        self._dashboard_grid = grid
        self._dashboard_section_cards = section_cards
        for column in (0, 1):
            grid.columnconfigure(column, weight=1, uniform="dashboard_section")

        self._add_startup_section(
            grid, section_cards, text=text, bg=card_bg, border=border
        )
        self._add_ai_usage_section(
            grid, section_cards, text=text, bg=card_bg, border=border
        )
        self._add_kakao_section(
            grid, section_cards, text=text, bg=card_bg, border=border
        )
        self._add_wrike_section(
            grid, section_cards, text=text, bg=card_bg, border=border
        )
        self._add_background_section(
            grid, section_cards, text=text, bg=card_bg, border=border
        )
        self._add_update_section(
            grid, section_cards, text=text, bg=card_bg, border=border
        )
        self._layout_dashboard_cards(grid, section_cards, notify=False)

        def relayout_for_width(_event: Any) -> None:
            self._on_dashboard_viewport_configure()
            return

        try:
            grid.bind("<Configure>", relayout_for_width, add="+")
        except TypeError:
            try:
                grid.bind("<Configure>", relayout_for_width)
            except Exception:
                pass
        except Exception:
            pass

        self.refresh()
        self._bind_dashboard_scroll_targets()
        self._request_dashboard_scroll_sync()
        return

    def preferred_size(self) -> tuple[int, int]:
        """Return the dashboard content size before the outer shell chrome.

        The canvas itself intentionally has a small Tk requested size, so the
        root window cannot infer the embedded frame's real requirement. Expose
        that requirement to the shell's fit policy instead of reserving a large
        fixed dashboard window.
        """
        container = self._dashboard_scroll_container
        if container is None:
            return (0, 0)
        # A withdrawn Tk root is still 1x1 while its first view is built. In
        # that state a Configure event can incorrectly collapse the dashboard
        # to one column, making the measurement itself too narrow and too tall.
        # Seed the intended two-column intrinsic layout before measuring; the
        # normal Configure binding will switch to one column after a truly
        # narrow window is applied.
        try:
            if (
                self._dashboard_scroll_canvas is not None
                and int(self._dashboard_scroll_canvas.winfo_width()) <= 1
                and self._dashboard_grid is not None
            ):
                self._layout_dashboard_cards(
                    self._dashboard_grid,
                    self._dashboard_section_cards,
                    available_width=self._TWO_COLUMN_MIN_WIDTH,
                    notify=False,
                )
        except Exception:
            pass
        try:
            container.update_idletasks()
        except Exception:
            pass
        try:
            width = int(container.winfo_reqwidth())
            height = int(container.winfo_reqheight())
        except Exception:
            return (0, 0)
        # A two-column measurement is only a truthful fit when the viewport it
        # produces keeps two columns. Two compact cards are usually narrower
        # than the column threshold, so the fitted window used to flip to one
        # (about twice as tall) column on the next Configure while the shell
        # still capped the height at the two-column content.
        columns = getattr(
            self._dashboard_grid, "_windows_supporter_dashboard_columns", None
        )
        if columns == 2:
            width = max(width, self._TWO_COLUMN_MIN_WIDTH + 2 * self._GRID_PADX)
        scrollbar = self._dashboard_scrollbar
        if scrollbar is not None:
            try:
                width += max(0, int(scrollbar.winfo_reqwidth()))
            except Exception:
                pass
        return max(1, width), max(1, height)

    def _sync_dashboard_scroll_geometry(self) -> None:
        """Keep the embedded dashboard content matching the viewport.

        The canvas window tracks the viewport width and stretches to the
        viewport height whenever the content is shorter, so the card grid
        fills the window instead of leaving a gray band below it. When the
        content is taller the natural height wins and the scrollbar takes
        over as before.
        """

        canvas = self._dashboard_scroll_canvas
        container = self._dashboard_scroll_container
        window_id = self._dashboard_scroll_window_id
        if canvas is None or container is None or window_id is None:
            return
        try:
            view_width = int(canvas.winfo_width())
            view_height = int(canvas.winfo_height())
            required_height = int(container.winfo_reqheight())
        except Exception:
            return
        size = (max(1, view_width), max(1, required_height, view_height))
        try:
            if size != self._dashboard_scroll_item_size:
                canvas.itemconfigure(window_id, width=size[0], height=size[1])
                self._dashboard_scroll_item_size = size
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass
        return

    def _on_dashboard_viewport_configure(self) -> None:
        self._relayout_for_viewport()
        self._request_dashboard_scroll_sync()
        return

    def _relayout_for_viewport(self) -> None:
        """Choose the column count from the scroll viewport, never the grid.

        Until the scroll window is pinned to the viewport, the grid is laid
        out at the container's natural width, and that width depends on the
        current column count. Deciding from it flipped a two-column layout to
        one column before the window was even mapped. An unmapped viewport
        keeps the current (seeded) layout.
        """
        canvas = self._dashboard_scroll_canvas
        grid = self._dashboard_grid
        if canvas is None or grid is None:
            return
        try:
            viewport = int(canvas.winfo_width())
        except Exception:
            return
        if viewport <= 1:
            return
        self._layout_dashboard_cards(
            grid,
            self._dashboard_section_cards,
            available_width=max(2, viewport - 2 * self._GRID_PADX),
        )
        return

    def _request_dashboard_scroll_sync(self) -> None:
        """Schedule one scroll-geometry sync after pending layout has settled.

        The canvas window pins the embedded frame to the height it is given.
        A column switch or a status re-wrap changes the frame's requirement
        only in later idle geometry passes, so a sync run directly from the
        triggering Configure read the old requirement. The pinned frame then
        never received another Configure, and every card stayed squeezed
        below its own requested height.
        """
        if self._scroll_sync_running:
            self._scroll_sync_dirty = True
            return
        if self._scroll_sync_after_id is not None:
            return
        canvas = self._dashboard_scroll_canvas
        after_idle = getattr(canvas, "after_idle", None)
        if not callable(after_idle):
            self._flush_dashboard_scroll_sync()
            return
        try:
            self._scroll_sync_after_id = after_idle(self._flush_dashboard_scroll_sync)
        except Exception:
            self._scroll_sync_after_id = None
            self._flush_dashboard_scroll_sync()
        return

    def _flush_dashboard_scroll_sync(self) -> None:
        self._scroll_sync_after_id = None
        if self._scroll_sync_running:
            self._scroll_sync_dirty = True
            return
        self._scroll_sync_running = True
        try:
            container = self._dashboard_scroll_container
            settle = getattr(container, "update_idletasks", None)
            if callable(settle):
                try:
                    # Status rows -> card -> grid -> container each publish
                    # their requirement in a separate idle pass.
                    settle()
                except Exception:
                    pass
            self._sync_dashboard_scroll_geometry()
        finally:
            self._scroll_sync_running = False
        if self._scroll_sync_dirty:
            self._scroll_sync_dirty = False
            self._request_dashboard_scroll_sync()
        return

    def _cancel_dashboard_scroll_sync(self) -> None:
        pending, self._scroll_sync_after_id = self._scroll_sync_after_id, None
        self._scroll_sync_dirty = False
        if pending is None:
            return
        after_cancel = getattr(self._dashboard_scroll_canvas, "after_cancel", None)
        if callable(after_cancel):
            try:
                after_cancel(pending)
            except Exception:
                pass
        return

    def _notify_layout_changed(self) -> None:
        callback = self._on_layout_changed
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass
        return

    def _layout_dashboard_cards(
        self,
        grid: Any,
        cards: list[Any],
        *,
        available_width: int | None = None,
        notify: bool = True,
    ) -> None:
        width = int(available_width or 0)
        if width <= 1:
            try:
                width = int(grid.winfo_width())
            except Exception:
                width = 0
        columns = 1 if width > 1 and width < self._TWO_COLUMN_MIN_WIDTH else 2
        previous_columns = getattr(grid, "_windows_supporter_dashboard_columns", None)
        if previous_columns == columns:
            return
        try:
            grid._windows_supporter_dashboard_columns = columns
        except Exception:
            pass
        for column in (0, 1):
            try:
                grid.columnconfigure(
                    column,
                    weight=1 if column < columns else 0,
                    uniform="dashboard_section" if column < columns else "",
                )
            except Exception:
                pass
        row_count = (len(cards) + columns - 1) // columns
        previous_rows = int(
            getattr(grid, "_windows_supporter_dashboard_rows", 0) or 0
        )
        for row in range(max(row_count, previous_rows)):
            try:
                # Weight still stretches rows into a genuinely taller viewport,
                # but a uniform row group would also inflate the requested
                # content height to the tallest card and bake blank space into
                # every card in a content-fit window.
                grid.rowconfigure(
                    row,
                    weight=1 if row < row_count else 0,
                    uniform="",
                )
            except Exception:
                pass
        try:
            grid._windows_supporter_dashboard_rows = row_count
        except Exception:
            pass
        for index, card in enumerate(cards):
            try:
                card.grid(
                    row=index // columns,
                    column=index % columns,
                    sticky="nsew",
                    padx=(0, 5)
                    if columns > 1 and index % columns == 0
                    else (5, 0)
                    if columns > 1
                    else 0,
                    pady=(0, 6),
                )
            except Exception:
                pass
        if previous_columns is not None:
            # The new column count changes the content height only after the
            # grid re-measures, so the scroll window and the shell's height
            # ceiling must follow once that has happened.
            self._request_dashboard_scroll_sync()
            if notify:
                self._notify_layout_changed()
        return

    @staticmethod
    def _dashboard_scroll_units(delta: int | float | None) -> int:
        try:
            value = float(delta or 0)
        except (TypeError, ValueError):
            return 0
        if value > 0:
            return -1
        if value < 0:
            return 1
        return 0

    def _scroll_dashboard_units(self, canvas: Any, units: int, *, pages: bool = False) -> str:
        if not int(units):
            return "break"
        try:
            canvas.yview_scroll(int(units), "pages" if pages else "units")
        except Exception:
            pass
        return "break"

    def _scroll_dashboard(self, canvas: Any, event: Any) -> str:
        units = self._dashboard_scroll_units(getattr(event, "delta", 0))
        if not units:
            button = getattr(event, "num", None)
            units = -1 if button == 4 else 1 if button == 5 else 0
        return self._scroll_dashboard_units(canvas, units)

    def _bind_dashboard_scroll_targets(self) -> None:
        canvas = self._dashboard_scroll_canvas
        container = self._dashboard_scroll_container
        if canvas is None or container is None:
            return
        visited: set[int] = set()

        def bind_widget(widget: Any) -> None:
            marker = id(widget)
            if marker in visited:
                return
            visited.add(marker)
            try:
                widget.bind("<MouseWheel>", lambda event: self._scroll_dashboard(canvas, event))
                widget.bind("<Button-4>", lambda event: self._scroll_dashboard(canvas, event))
                widget.bind("<Button-5>", lambda event: self._scroll_dashboard(canvas, event))
                widget.bind("<Up>", lambda _event: self._scroll_dashboard_units(canvas, -1))
                widget.bind("<Down>", lambda _event: self._scroll_dashboard_units(canvas, 1))
                widget.bind("<Prior>", lambda _event: self._scroll_dashboard_units(canvas, -1, pages=True))
                widget.bind("<Next>", lambda _event: self._scroll_dashboard_units(canvas, 1, pages=True))
            except Exception:
                pass
            try:
                children = list(widget.winfo_children())
            except Exception:
                children = []
            for child in children:
                bind_widget(child)
            return

        bind_widget(canvas)
        bind_widget(container)
        return

    def refresh(self) -> None:
        try:
            snapshot = self._status_provider()
        except Exception:
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}

        self._set_feature_status("startup", self._format_startup(snapshot.get("startup")))
        ai_usage = snapshot.get("ai_usage")
        if ai_usage is None:
            ai_usage = snapshot.get("codex")
        self._set_feature_status("ai_usage", self._format_ai_usage(ai_usage))
        self._set_feature_status("kakao", self._format_kakao(snapshot.get("kakao")))
        self._set_feature_status("wrike", self._format_wrike(snapshot.get("wrike")))
        self._set_feature_status("background", self._format_background(snapshot.get("background")))
        self._set_feature_status("update", self._format_update(snapshot.get("update")))
        self._bind_dashboard_scroll_targets()
        self._request_dashboard_scroll_sync()
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
        section_cards: list[Any] | None = None,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            section_cards,
            key="startup",
            title="Startup Apps",
            text=text,
            bg=bg,
            border=border,
            settings_callback="startup.settings",
            toggle_callback="startup.toggle",
        )
        return

    def _add_ai_usage_section(
        self,
        parent: Any,
        section_cards: list[Any] | None = None,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            section_cards,
            key="ai_usage",
            title="AI 사용량",
            text=text,
            bg=bg,
            border=border,
            settings_callback="ai_usage.settings",
            toggle_callback="ai_usage.toggle",
        )
        return

    def _add_codex_section(
        self,
        parent: Any,
        section_cards: list[Any] | None = None,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_ai_usage_section(parent, section_cards, text=text, bg=bg, border=border)
        return

    def _add_kakao_section(
        self,
        parent: Any,
        section_cards: list[Any] | None = None,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            section_cards,
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
        section_cards: list[Any] | None = None,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            section_cards,
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
        section_cards: list[Any] | None = None,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        self._add_section(
            parent,
            section_cards,
            key="background",
            title="Background",
            text=text,
            bg=bg,
            border=border,
            settings_callback=None,
            toggle_callback="background.toggle",
        )
        return

    def _add_update_section(
        self,
        parent: Any,
        section_cards: list[Any] | None = None,
        *,
        text: str,
        bg: str,
        border: str,
    ) -> None:
        tk = self._tk
        ttk = self._ttk
        card = tk.Frame(
            parent,
            bg=bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        if section_cards is not None:
            section_cards.append(card)
        inner = tk.Frame(card, bg=bg)
        inner.pack(fill="x", padx=12, pady=10)
        title_row = tk.Frame(inner, bg=bg)
        title_row.pack(fill="x")
        tk.Label(
            title_row,
            text="Update",
            bg=bg,
            fg=text,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        buttons = tk.Frame(title_row, bg=bg)
        buttons.pack(side="right")
        ttk.Button(
            buttons,
            text="업데이트 확인",
            width=12,
            command=lambda: self._invoke("update.check"),
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            buttons,
            text="자동 업데이트",
            width=12,
            command=lambda: self._invoke("update.settings"),
        ).pack(side="left")
        row = tk.Frame(inner, bg=bg)
        row.pack(fill="x", pady=(4, 0))
        try:
            row.columnconfigure(0, weight=1)
        except Exception:
            pass
        status_frame = tk.Frame(row, bg=bg)
        status_frame.grid(row=0, column=0, sticky="ew")
        try:
            status_frame.configure(cursor="hand2")
            self._bind_click(status_frame, "update.settings")
        except Exception:
            pass
        self._status_frames["update"] = status_frame
        return

    def _add_section(
        self,
        parent: Any,
        section_cards: list[Any] | None = None,
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
        card = tk.Frame(
            parent,
            bg=bg,
            highlightthickness=1,
            highlightbackground=border,
            cursor="hand2" if settings_callback else "",
        )
        if section_cards is not None:
            section_cards.append(card)
        inner = tk.Frame(card, bg=bg)
        inner.pack(fill="x", padx=12, pady=10)
        title_row = tk.Frame(inner, bg=bg)
        title_row.pack(fill="x")
        title_label = tk.Label(
            title_row,
            text=str(title),
            bg=bg,
            fg=text,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2" if settings_callback else "",
        )
        title_label.pack(side="left")
        btn = ttk.Button(
            title_row,
            text="활성화",
            width=10,
            command=lambda n=toggle_callback: self._invoke(n),
        )
        btn.pack(side="right")
        row = tk.Frame(inner, bg=bg)
        row.pack(fill="x", pady=(4, 0))
        try:
            row.columnconfigure(0, weight=1)
        except Exception:
            pass
        status_frame = tk.Frame(row, bg=bg, cursor="hand2" if settings_callback else "")
        status_frame.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._status_frames[str(key)] = status_frame
        self._toggle_buttons[str(key)] = btn
        if settings_callback:
            for widget in (card, title_label, row, status_frame):
                self._bind_click(widget, settings_callback)
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
        logical_rows = self._status_part_rows(key, parts)
        applied = {"signature": None}

        def measure(text_value: str, kind: str) -> int:
            return self._measure_status_part_width(str(text_value), str(kind))

        def rebuild(event: Any = None) -> None:
            try:
                width = int(getattr(event, "width", 0) or frame.winfo_width())
            except Exception:
                width = 0
            signature = (width, id(logical_rows))
            if signature == applied["signature"]:
                return
            applied["signature"] = signature
            try:
                for child in list(frame.winfo_children()):
                    try:
                        child.destroy()
                    except Exception:
                        continue
            except Exception:
                return
            for line in self._status_lines_for_width(logical_rows, width, measure):
                row = tk.Frame(frame, bg="#FFFFFF")
                row.pack(anchor="w", fill="x")
                for idx, (raw_text, kind, wrap) in enumerate(line):
                    if idx > 0:
                        self._make_status_label(row, " | ", "separator").pack(
                            side="left", anchor="n"
                        )
                    label = self._make_status_label(row, raw_text, kind)
                    label.pack(side="left", anchor="n")
                    if wrap and width > 1:
                        try:
                            label.configure(wraplength=width)
                        except Exception:
                            pass
                    callback_name = f"{key}.settings"
                    if callable(self._get_callback(callback_name)):
                        try:
                            label.configure(cursor="hand2")
                        except Exception:
                            pass
                        self._bind_click(label, callback_name)
            # Re-wrapped rows change the card height; the pinned scroll
            # window has to follow the new requirement.
            self._request_dashboard_scroll_sync()
            return

        rebuild()
        try:
            frame.bind("<Configure>", rebuild)
            frame.after_idle(rebuild)
        except Exception:
            pass
        return

    def _make_status_label(self, row: Any, raw_text: str, kind: str) -> Any:
        tk = self._tk
        fg = "#111827"
        font = ("Segoe UI", 9)
        if kind == "enabled":
            fg = "#059669"
            font = ("Segoe UI", 9, "bold")
        elif kind == "disabled":
            fg = "#DC2626"
            font = ("Segoe UI", 9, "bold")
        elif kind == "separator":
            fg = "#6B7280"
        return tk.Label(
            row,
            text=str(raw_text),
            bg="#FFFFFF",
            fg=fg,
            font=font,
            anchor="w",
            justify="left",
        )

    @staticmethod
    def _status_lines_for_width(
        row_groups: list[list[tuple[str, str]]],
        width: int,
        measure: Callable[[str, str], int],
    ) -> list[list[tuple[str, str, bool]]]:
        """Group status parts into rendered lines that fit `width` pixels.

        `measure(text, kind)` returns the rendered pixel width of one part
        including label chrome. Parts that overflow the current line move to
        a new line; a part wider than a whole line is flagged to wrap in
        place. A non-positive or unknown width keeps every logical row on a
        single line.
        """
        lines: list[list[tuple[str, str, bool]]] = []
        try:
            separator_width = int(measure(" | ", "separator"))
        except Exception:
            separator_width = 0
        for group in row_groups or []:
            current: list[tuple[str, str, bool]] = []
            current_width = 0
            for raw_text, kind in group or []:
                try:
                    part_width = int(measure(str(raw_text), str(kind)))
                except Exception:
                    part_width = 0
                extra = part_width + (separator_width if current else 0)
                if current and width > 1 and current_width + extra > width:
                    lines.append(current)
                    current = []
                    current_width = 0
                    extra = part_width
                current.append((raw_text, kind, bool(width > 1 and part_width > width)))
                current_width += extra
            if current:
                lines.append(current)
        return lines

    def _status_measure_fonts(self) -> dict[str, Any]:
        if self._status_fonts:
            return self._status_fonts
        fonts: dict[str, Any] = {}
        try:
            from tkinter import font as tkfont
        except Exception:
            tkfont = None
        if tkfont is not None:
            for name, weight in (("normal", "normal"), ("bold", "bold")):
                try:
                    fonts[name] = tkfont.Font(
                        family="Segoe UI", size=9, weight=weight
                    )
                except Exception:
                    continue
        self._status_fonts = fonts
        return fonts

    def _measure_status_part_width(self, text: str, kind: str) -> int:
        font = self._status_measure_fonts().get(
            "bold" if kind in {"enabled", "disabled"} else "normal"
        )
        try:
            width = int(font.measure(str(text))) if font is not None else 0
        except Exception:
            width = 0
        if width <= 0:
            width = max(1, len(str(text))) * 10
        return width + self._STATUS_PART_CHROME

    @staticmethod
    def _status_part_rows(key: str, parts: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
        normalized = list(parts or [])
        if str(key) == "background":
            return [[part] for part in normalized]
        if str(key) != "ai_usage":
            return [normalized] if normalized else []
        summary = normalized[:3]
        profiles = [[part] for part in normalized[3:]]
        return ([summary] if summary else []) + profiles

    def _get_callback(self, name: str) -> Callable[[], Any] | None:
        callback_name = str(name)
        callback = self._callbacks.get(callback_name)
        if callable(callback):
            return callback
        alias = self._CALLBACK_ALIASES.get(callback_name)
        if alias:
            callback = self._callbacks.get(alias)
            if callable(callback):
                return callback
        return None

    def _invoke(self, name: str) -> None:
        cb = self._get_callback(name)
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

    def _format_ai_usage(self, data: Any) -> tuple[bool, list[tuple[str, str]]]:
        if not isinstance(data, dict):
            return False, [("비활성화", "disabled"), ("상태 확인 불가", "normal")]
        is_enabled = bool(data.get("enabled", True))
        runtime = str(data.get("monitor_state", "unknown") or "unknown")
        session = str(data.get("session_state", "unknown") or "unknown")
        parts = [
            self._enabled_part(is_enabled),
            (f"상태: {runtime}", "normal"),
            (f"세션: {session}", "normal"),
        ]
        accounts = data.get("profiles")
        if not isinstance(accounts, list):
            accounts = data.get("accounts")
        if isinstance(accounts, list):
            valid_accounts = [raw for raw in accounts if isinstance(raw, dict)]
            for raw in valid_accounts[:2]:
                label = str(raw.get("label") or raw.get("id") or "프로필").strip()
                provider = str(raw.get("provider") or "codex").strip().title()
                account_enabled = bool(raw.get("enabled", True))
                selected = bool(raw.get("taskbar_selected", True))
                account_runtime = raw.get("runtime", {})
                if not isinstance(account_runtime, dict):
                    account_runtime = {}
                account_session = str(account_runtime.get("session_state", "unknown") or "unknown")
                account_state = str(account_runtime.get("monitor_state", "unknown") or "unknown")
                if account_enabled:
                    display_state = f"{account_session} / {account_state}"
                    if not selected:
                        display_state += " / 표시 안 함"
                    parts.append((f"{label} ({provider}): {display_state}", "normal"))
                else:
                    parts.append((f"{label} ({provider}): 비활성 / {account_session}", "disabled"))
        return is_enabled, parts

    def _format_codex(self, data: Any) -> tuple[bool, list[tuple[str, str]]]:
        return self._format_ai_usage(data)

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
        if bool(data.get("wrike_attached", False)):
            attached.append("Wrike")
        if bool(data.get("ai_usage_attached", False)) or bool(
            data.get("codex_attached", False)
        ):
            attached.append("AI 사용량")
        if bool(data.get("lijamong_attached", False)):
            attached.append("LiJaMong")
        attached_text = ", ".join(attached) if attached else "없음"
        return is_enabled, [
            self._enabled_part(is_enabled),
            (f"핫키 {hotkeys} · 준비 {warmup}", "normal"),
            (f"전경 {profile} · 연결 {attached_text}", "normal"),
        ]

    def _format_update(self, data: Any) -> tuple[bool, list[tuple[str, str]]]:
        return format_update_status_parts(data)

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
