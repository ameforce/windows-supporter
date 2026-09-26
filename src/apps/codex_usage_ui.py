from __future__ import annotations

from datetime import datetime
import re
import threading
from typing import Any

from src.apps.ai_usage_contracts import (
    TASKBAR_PANE_SIZE,
    TaskbarSidePriority,
    normalize_taskbar_side_priority,
    plan_taskbar_drop,
    resolve_taskbar_pane_assignment,
)
from src.apps.codex_usage_multi_monitor import TASKBAR_PROFILE_LIMIT
from src.apps.codex_usage_taskbar_overlay import draw_provider_mark
from src.apps.profile_detail_canvas import ProfileDetailCanvas
from src.apps.profile_controls import PROFILE_MENU_GLYPH, ProfileActionBinding, ProfileCardMenu
from src.apps.profile_board import ProfileBoard


class CodexUsageSettingsView:
    def __init__(
        self,
        root: Any,
        codex_monitor: Any,
        ui_post=None,
        on_external_settings_reconciled=None,
    ) -> None:
        self._root = root
        self._codex = codex_monitor
        self._ui_post = ui_post if callable(ui_post) else None
        self._on_external_settings_reconciled = (
            on_external_settings_reconciled
            if callable(on_external_settings_reconciled)
            else None
        )

        self._tk = None
        self._ttk = None
        self._win = None
        self._parent = None

        self._enabled_var = None
        self._taskbar_overlay_var = None
        self._taskbar_side_priority_var = None
        self._interval_var = None
        self._tooltip_var = None
        self._status_var = None
        self._status_label = None
        self._login_button = None
        self._logout_button = None
        self._account_enabled_vars = {}
        self._account_provider_vars = {}
        self._account_provider_marks = {}
        self._account_taskbar_selected_vars = {}
        self._account_query_buttons = {}
        self._account_move_buttons = {}
        self._account_login_buttons = {}
        self._account_logout_buttons = {}
        self._account_labels: dict[str, str] = {}
        self._account_label_vars = {}
        self._account_rendered_providers: dict[str, str] = {}
        self._account_status_vars = {}
        self._account_snapshot_vars = {}
        self._account_metric_vars = {}
        self._account_metric_display_vars = {}
        self._account_metric_cells = {}
        self._account_metric_visibility = {}
        self._account_detail_canvases = {}
        self._account_order: list[str] = []
        self._pane_card_parent = None
        self._profile_menu = None
        self._profile_board = None
        self._active_account_id = None
        self._pane_boxes: dict[str, Any] = {}
        self._pane_lists: dict[str, Any] = {}
        self._pane_titles: dict[str, Any] = {}
        self._pane_hints: dict[str, Any] = {}
        self._pane_card_widgets: dict[str, Any] = {}
        self._drop_indicators: dict[str, Any] = {}
        self._drag_state: dict[str, Any] | None = None
        self._rendered_side_priority: str | None = None
        self._rendered_pane_assignment: dict[str, list[str]] | None = None
        self._profile_deletions_inflight: set[str] = set()
        self._profile_actions_inflight: set[str] = set()
        self._scroll_canvas = None
        self._scroll_body = None
        self._scroll_pending_canvas = None
        self._scroll_pending_units = 0
        self._scroll_after_id = None
        self._scroll_root_bindings: list[tuple[str, Any]] = []
        self._scroll_window_id = None
        self._scroll_window_width = None
        self._header_card = None
        self._content_card = None
        self._scrollbar = None
        self._runtime_value_rows: list[tuple[Any, Any]] = []
        self._live_spark_visible = None
        self._button_enabled_states: dict[int, bool] = {}
        self._autosave_after_id = None
        self._preserve_status_after_next_autosave = False
        self._external_settings_result_lock = threading.Lock()
        self._pending_external_settings_result: tuple[
            bool,
            str | None,
            dict[str, Any] | None,
        ] | None = None
        self._profile_add_result_lock = threading.Lock()
        self._pending_profile_add_result: tuple[bool, str | None, bool] | None = None
        self._profile_delete_result_lock = threading.Lock()
        self._pending_profile_delete_result: tuple[
            str,
            bool,
            str | None,
            bool,
            dict[str, Any] | None,
        ] | None = None
        self._profile_release_result_lock = threading.Lock()
        self._pending_profile_release_result: tuple[str, bool, str] | None = None
        self._profile_add_settings_changed = False
        self._blocked_mutation_settings_changed = False
        self._loading_settings = False
        self._runtime_after_id = None
        self._collect_state_var = None
        self._next_collect_var = None
        self._live_time_var = None
        self._live_five_hour_var = None
        self._live_five_hour_reset_var = None
        self._live_weekly_var = None
        self._live_weekly_reset_var = None
        self._live_monthly_var = None
        self._live_monthly_reset_var = None
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
        self._parent = parent
        self._scroll_window_width = None
        self._lazy_import_tk()
        self._stop_runtime_refresh()
        self._cancel_pending_autosave()
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
        accounts = settings.get("profiles")
        if not isinstance(accounts, list):
            accounts = settings.get("accounts")
        has_multi_accounts = isinstance(accounts, list)
        self._login_button = None
        self._logout_button = None
        self._account_query_buttons = {}
        self._account_move_buttons = {}
        self._account_login_buttons = {}
        self._account_logout_buttons = {}
        self._account_labels = {}
        self._account_label_vars = {}
        self._account_rendered_providers = {}
        self._account_status_vars = {}
        self._account_snapshot_vars = {}
        self._account_metric_vars = {}
        self._account_metric_display_vars = {}
        self._account_metric_cells = {}
        self._account_metric_visibility = {}
        self._account_detail_canvases = {}
        self._account_provider_vars = {}
        self._account_provider_marks = {}
        self._account_taskbar_selected_vars = {}
        self._pane_boxes = {}
        self._pane_lists = {}
        self._pane_titles = {}
        self._pane_hints = {}
        self._pane_card_widgets = {}
        self._drop_indicators = {}
        self._drag_state = None
        self._rendered_side_priority = None
        self._rendered_pane_assignment = None
        self._taskbar_side_priority_var = None
        self._runtime_value_rows = []
        self._live_spark_visible = None
        self._button_enabled_states = {}
        self._cancel_pending_scroll()
        self._unbind_scroll_root_bindings()

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
        self._header_card = header_card
        header_card.pack(fill="x", padx=8, pady=(8, 6))

        header_inner = tk.Frame(header_card, bg=card_bg)
        header_inner.pack(anchor="w", padx=12, pady=8) if has_multi_accounts else header_inner.pack(fill="x", padx=12, pady=8)

        title_row = tk.Frame(header_inner, bg=card_bg)
        title_row.pack(fill="x")

        tk.Label(
            title_row,
            text="AI 사용량 설정",
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")

        btn_row = tk.Frame(title_row, bg=card_bg)
        btn_row.pack(side="right")
        if not has_multi_accounts:
            self._logout_button = ttk.Button(
                btn_row,
                text="연결 해제",
                command=self._on_release_profile,
            )
            self._logout_button.pack(
                side="right", padx=(0, 8)
            )
            self._login_button = ttk.Button(btn_row, text="연결", command=self._on_login)
            self._login_button.pack(
                side="right", padx=(0, 8)
            )

        tk.Label(
            header_inner,
            text=(
                "제목을 끌어 순서와 표시 위치를 바꿉니다. "
                f"프로필 동작은 카드의 {PROFILE_MENU_GLYPH} 메뉴나 우클릭으로 실행합니다."
            ),
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
        self._content_card = content_card
        content_card.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        viewport = tk.Frame(content_card, bg=card_bg)
        viewport.pack(fill="both", expand=True, padx=3, pady=3)
        canvas = tk.Canvas(
            viewport,
            bg=card_bg,
            borderwidth=0,
            highlightthickness=0,
            takefocus=True,
        )
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
        self._scrollbar = scrollbar
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=card_bg)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        self._scroll_canvas = canvas
        self._scroll_body = body
        self._scroll_window_id = body_window
        try:
            body.bind(
                "<Configure>",
                self._on_scroll_body_configure,
            )
            canvas.bind(
                "<Configure>",
                self._on_scroll_canvas_configure,
            )
        except Exception:
            pass
        body.configure(padx=9, pady=4)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(3, weight=1)

        self._enabled_var = tk.BooleanVar(value=False)
        self._taskbar_overlay_var = tk.BooleanVar(value=True)
        self._taskbar_side_priority_var = tk.StringVar(
            value=TaskbarSidePriority.LEFT.value
        )
        self._interval_var = tk.StringVar(value="")
        self._tooltip_var = tk.StringVar(value="")
        self._collect_state_var = tk.StringVar(value="-")
        self._next_collect_var = tk.StringVar(value="-")
        self._live_time_var = tk.StringVar(value="-")
        self._live_five_hour_var = tk.StringVar(value="-")
        self._live_five_hour_reset_var = tk.StringVar(value="-")
        self._live_weekly_var = tk.StringVar(value="-")
        self._live_weekly_reset_var = tk.StringVar(value="-")
        self._live_monthly_var = tk.StringVar(value="-")
        self._live_monthly_reset_var = tk.StringVar(value="-")
        self._live_spark_five_hour_var = tk.StringVar(value="-")
        self._live_spark_five_hour_reset_var = tk.StringVar(value="-")
        self._live_spark_weekly_var = tk.StringVar(value="-")
        self._live_spark_weekly_reset_var = tk.StringVar(value="-")
        self._live_credit_var = tk.StringVar(value="-")
        self._live_spark_cells: list[Any] = []

        row = 0

        # 기본 설정은 별도 responsive row로 분리한다. 이전에는 두 체크박스가
        # body의 4열 grid를 직접 점유해, 아래의 긴 섹션 제목이 첫 열의
        # 최소 폭을 키우고 URL 입력칸을 화면 오른쪽으로 밀어냈다.
        options = tk.Frame(body, bg=card_bg)
        options.grid(row=row, column=0, columnspan=4, sticky="we", pady=3)
        option_widgets = []
        for checkbox_text, target_var in (
            ("모니터링 사용", self._enabled_var),
            ("작업표시줄 오버레이", self._taskbar_overlay_var),
        ):
            option_widgets.append(
                tk.Checkbutton(
                    options,
                    text=checkbox_text,
                    variable=target_var,
                    bg=card_bg,
                    activebackground=card_bg,
                    selectcolor=card_bg,
                    fg="#111827",
                    activeforeground="#111827",
                    font=("Segoe UI", 9),
                )
            )
        self._bind_responsive_widget_row(options, option_widgets, columns=2)
        row += 1

        placement = tk.Frame(body, bg=card_bg)
        placement.grid(row=row, column=0, columnspan=4, sticky="we", pady=(0, 3))
        tk.Label(
            placement,
            text="작업표시줄 배치 우선순위",
            bg=card_bg,
            fg="#374151",
            font=("Segoe UI", 9),
        ).pack(side="left")
        # 기본 ttk.Radiobutton 스타일은 테마 배경(회색)을 칠해 흰 카드 위에
        # 떠 보인다. 표시 모양은 그대로 두고 배경만 카드 색으로 맞춘다.
        radio_style = self._card_radio_style(card_bg)
        for label, value in (
            ("왼쪽 우선", TaskbarSidePriority.LEFT.value),
            ("오른쪽 우선", TaskbarSidePriority.RIGHT.value),
        ):
            radio = None
            if radio_style and callable(getattr(ttk, "Radiobutton", None)):
                radio = ttk.Radiobutton(
                    placement,
                    text=label,
                    variable=self._taskbar_side_priority_var,
                    value=value,
                    style=radio_style,
                )
            elif callable(getattr(tk, "Radiobutton", None)):
                radio = tk.Radiobutton(
                    placement,
                    text=label,
                    variable=self._taskbar_side_priority_var,
                    value=value,
                    bg=card_bg,
                    activebackground=card_bg,
                    selectcolor=card_bg,
                    fg="#111827",
                    activeforeground="#111827",
                    font=("Segoe UI", 9),
                )
            if radio is not None:
                radio.pack(side="left", padx=(12, 0))
        row += 1

        tk.Label(
            body,
            text="조회 주기(초)",
            bg=card_bg,
            fg="#374151",
            font=("Segoe UI", 9),
        ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(body, textvariable=self._interval_var, width=10).grid(
            row=row,
            column=1,
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

        if isinstance(accounts, list):
            row = self._add_account_sections(
                body,
                row,
                accounts,
                card_bg,
                border,
                text_muted,
                side_priority=(
                    settings.get("taskbar_side_priority")
                    if isinstance(settings, dict)
                    else None
                ),
            )

        if not has_multi_accounts:
            row = self._add_realtime_status_section(body, row, card_bg, border)

        self._bind_scroll_navigation(canvas, body)
        self._bind_mousewheel_tree(body, canvas)

        self._load_settings()
        self._register_autosave_traces()
        self._start_runtime_refresh()
        return

    def preferred_size(self) -> tuple[int, int]:
        """Expose the mounted AI content requirement to the main shell.

        A canvas reports only its small viewport request, while the embedded
        scroll body owns the actual profile/status grid. Measuring the body
        and adding the scrollbar plus the surrounding card insets keeps the
        first AI-tab geometry wide enough before the user has to resize it.
        """

        body = self._scroll_body
        if body is None:
            return (0, 0)
        # Geometry settling belongs to the shell, not a size getter. Nested
        # idle drains here repaint intermediate layouts repeatedly while the
        # shell queries preferred/minimum sizes in the same transaction.
        try:
            body_width = int(body.winfo_reqwidth())
        except Exception:
            return (0, 0)

        scrollbar_width = 0
        scrollbar = self._scrollbar
        if scrollbar is not None:
            try:
                scrollbar_width = max(0, int(scrollbar.winfo_reqwidth()))
            except Exception:
                pass
        try:
            container_width = int(self._win.winfo_reqwidth())
            container_height = int(self._win.winfo_reqheight())
        except Exception:
            container_width = 0
            container_height = 0

        # content_card has 8px outer padding and viewport has 3px inner
        # padding on both sides. The embedded body's own padding is already
        # included in winfo_reqwidth().
        measured_width = body_width + scrollbar_width + 22
        # 세로 스택 상태의 body 요구 폭은 상자 하나분이라 좁게 보고된다.
        # 의도한 나란히 배치의 요구 폭을 하한으로 보고해야 스택 상태의
        # 측정값이 창을 좁게 고착시키는 순환이 끊긴다. body의 좌우
        # padding(padx=9)과 카드 경계선(1px씩)을 더한다.
        side_by_side = self._pane_side_by_side_min_width()
        if side_by_side > 0:
            measured_width = max(
                measured_width,
                side_by_side + 18 + scrollbar_width + 24,
            )
        width = max(1, measured_width, container_width)
        # The body is intentionally scrollable, so its full content height
        # must not turn the first AI tab open into a very tall window.
        height = max(640 if self._profile_board is not None else 1, container_height)
        return width, height

    def minimum_size(self) -> tuple[int, int]:
        """Expose the narrowest usable AI-tab size to the main shell.

        나란히 배치가 불가능한 폭에서는 상자가 세로로 쌓이는 것이 의도된
        폴백이다. minsize가 나란히 배치 요구 폭까지 올라가면 사용자가 창을
        좁혀도 그 폴백에 도달할 수 없으므로, 스택 상태의 최소 폭(가장 넓은
        상자 하나분)을 별도로 보고한다.
        """
        body = self._scroll_body
        if body is None:
            return (0, 0)
        scrollbar_width = 0
        scrollbar = self._scrollbar
        if scrollbar is not None:
            try:
                scrollbar_width = max(0, int(scrollbar.winfo_reqwidth()))
            except Exception:
                pass
        box_width = 0
        for box in (self._pane_boxes or {}).values():
            box_width = max(box_width, self._pane_box_unwrapped_width(box))
        if box_width <= 0:
            return (0, 0)
        width = box_width + 18 + scrollbar_width + 24
        _, height = self.preferred_size()
        return (max(1, width), max(1, height))

    def _add_account_sections(
        self,
        body: Any,
        row: int,
        accounts: list[Any],
        card_bg: str,
        border: str,
        text_muted: str,
        side_priority: object = None,
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
        section_header = tk.Frame(body, bg=card_bg)
        section_header.grid(
            row=row,
            column=0,
            columnspan=4,
            sticky="we",
            pady=(0, 2),
        )
        try:
            section_header.columnconfigure(0, weight=1)
        except Exception:
            pass
        section_title = tk.Label(
            section_header,
            text=(
                "사용량 프로필 "
                f"(저장 제한 없음 · 작업표시줄 표시 최대 {TASKBAR_PROFILE_LIMIT}개)"
            ),
            bg=card_bg,
            fg="#111827",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            justify="left",
        )
        section_title.grid(row=0, column=0, sticky="we")
        add_profile_button = ttk.Button(
            section_header,
            text="프로필 추가",
            command=self._on_add_profile,
        )
        add_profile_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        try:
            section_header.bind(
                "<Configure>",
                lambda event: self._fit_section_title(
                    section_title,
                    add_profile_button,
                    int(getattr(event, "width", 0) or 0),
                ),
            )
        except Exception:
            pass
        row += 1
        ordered_accounts = [
            raw
            for raw in accounts
            if isinstance(raw, dict) and str(raw.get("id", "") or "").strip()
        ]
        self._account_order = [
            str(raw.get("id", "") or "").strip()
            for raw in ordered_accounts
        ]
        # 왼쪽/오른쪽 상자는 같은 순서·표시 모델을 보여주는 창이다. 오버레이가
        # 우선 순위에 따라 선택 1·2번을 한쪽, 3·4번을 반대쪽에 두므로, 상자도
        # 그 매핑을 그대로 보여준다. 설정 스키마 변경은 없다.
        # 우선순위는 mount 시점의 저장 스냅샷에서 읽는다. 화면을 그릴 때는
        # _load_settings() 전이라 우선순위 변수가 아직 기본값이므로, 변수에서
        # 읽으면 저장된 오른쪽 우선 첫 화면이 뒤바뀌어 보인다.
        if side_priority is None and self._taskbar_side_priority_var is not None:
            try:
                side_priority = self._taskbar_side_priority_var.get()
            except Exception:
                side_priority = None
        priority_value = normalize_taskbar_side_priority(side_priority).value
        assignment = resolve_taskbar_pane_assignment(
            self._account_order,
            [
                str(raw.get("id", "") or "").strip()
                for raw in ordered_accounts
                if bool(raw.get("taskbar_selected", True))
            ],
            priority_value,
        )
        self._rendered_side_priority = priority_value
        self._rendered_pane_assignment = {
            side: list(ids) for side, ids in assignment.items()
        }
        box_of: dict[str, str] = {}
        for side in ("left", "right", "pool"):
            for profile_id in assignment.get(side, []):
                box_of[str(profile_id)] = side
        row += 1
        board = ProfileBoard(tk, body, self, assignment, priority_value)
        self._profile_board = board
        # Per-profile actions live in one shared popup menu opened from each
        # card; cards themselves stay windowless canvas regions.
        self._profile_menu = ProfileCardMenu(self, board.canvas)
        self._pane_card_parent = board.canvas
        # Profiles share the existing viewport; only visible rows are rasterized.
        self._pane_boxes = dict(board.groups)
        self._pane_lists = dict(board.groups)
        self._pane_titles = {side:group.title for side,group in board.groups.items()}
        self._pane_hints = {side:group.hint for side,group in board.groups.items()}
        self._drop_indicators = dict(board.indicators)
        self._pane_card_widgets = {}
        self._drag_state = None
        box_rows = {"left": 0, "right": 0, "pool": 0}
        for index, raw in enumerate(ordered_accounts):
            if not isinstance(raw, dict):
                continue
            account_id = str(raw.get("id", "") or "").strip()
            if not account_id:
                continue
            label = str(raw.get("label", "") or account_id).strip()
            self._account_labels[account_id] = label
            provider = str(raw.get("provider", "codex") or "codex").strip().lower()
            self._account_rendered_providers[account_id] = provider
            label_var = tk.StringVar(value=label)
            self._account_label_vars[account_id] = label_var
            enabled_var = tk.BooleanVar(value=bool(raw.get("enabled", True)))
            provider_var = tk.StringVar(value=provider if provider in {"codex", "cursor", "claude"} else "codex")
            selected_var = tk.BooleanVar(value=bool(raw.get("taskbar_selected", True)))
            self._account_enabled_vars[account_id] = enabled_var
            self._account_provider_vars[account_id] = provider_var
            self._account_taskbar_selected_vars[account_id] = selected_var
            card_side = box_of.get(account_id, "pool")
            card_host = self._pane_lists.get(card_side)
            if card_host is None:
                card_host = self._pane_lists.get("pool", body)
                card_side = "pool"
            card = board.create_region(account_id, card_side, box_rows.get(card_side, 0))
            detail = ProfileDetailCanvas(tk, None, bg=card_bg, canvas=card)
            card.detail = detail
            card.configure(highlightthickness=1, highlightbackground=border, takefocus=True)
            card._windows_supporter_unwrapped_reqwidth = 280
            card.grid(in_=card_host, row=box_rows.get(card_side, 0), column=0,
                      sticky="nwe", pady=(0, 6))
            box_rows[card_side] = box_rows.get(card_side, 0) + 1
            self._pane_card_widgets[account_id] = card
            header_var = tk.StringVar(value=f"{provider.title()} · {label}")
            detail._header_var = header_var
            def update_header(*_args, label=label_var, provider=provider_var, target=header_var):
                target.set(f"{str(provider.get()).title()} · {label.get()}")
            label_var.trace_add("write", update_header)
            provider_var.trace_add("write", update_header)
            header_item = detail.add_line(
                section="top", variable=header_var, wraplength=320, fill="#111827",
                font=("Segoe UI", 10, "bold"), trailing_text=PROFILE_MENU_GLYPH,
                trailing_fill="#4B5563", trailing_font=("Segoe UI", 9, "bold"),
                on_trailing_click=lambda event, aid=account_id: self._open_profile_menu(aid, event),
            )
            card.tag_bind(header_item, "<ButtonPress-1>", lambda event, aid=account_id:
                          self._select_and_drag_profile(aid, event))
            # Release, not press: the popup must not take the same button's
            # release as a choice of its first entry.
            card.tag_bind(card.tag, "<ButtonRelease-3>", lambda event, aid=account_id:
                          self._open_profile_menu(aid, event))
            card.bind("<B1-Motion>", self._on_pane_drag_motion)
            card.bind("<ButtonRelease-1>", self._on_pane_drag_release)
            card.bind("<Escape>", self._cancel_pane_drag)
            card.bind("<Unmap>", self._cancel_pane_drag)
            if len(ordered_accounts) > 1:
                up, down = ProfileActionBinding(), ProfileActionBinding()
                self._account_move_buttons[account_id] = (up, down)
                self._set_button_enabled(up, index > 0)
                self._set_button_enabled(down, index < len(ordered_accounts)-1)
            self._account_query_buttons[account_id] = ProfileActionBinding()
            self._account_login_buttons[account_id] = ProfileActionBinding()
            self._account_logout_buttons[account_id] = ProfileActionBinding()
            status_var = tk.StringVar(value="조회 상태: -")
            snapshot_var = tk.StringVar(value="값 상태: -")
            self._account_status_vars[account_id] = status_var
            self._account_snapshot_vars[account_id] = snapshot_var
            # 상태 줄·지표 표·경로 줄을 캔버스 하나에 그린다. 각각 Label로
            # 두면 카드당 네이티브 창이 수십 개가 되어 탭 열기와 창 크기
            # 조절이 프로필 수에 비례해 멈춘다.
            for value_var in (status_var, snapshot_var):
                detail.add_line(
                    section="top",
                    variable=value_var,
                    wraplength=self._scaled_wrap_length(260),
                )
            metric_vars, display_vars = self._build_account_metric_rows(
                detail,
                provider=provider,
                account_id=account_id,
            )
            self._account_metric_vars[account_id] = metric_vars
            self._account_metric_display_vars[account_id] = display_vars
            self._account_detail_canvases[account_id] = detail
            for prefix, key, clickable in (
                ("설정 파일", "settings_path", True),
                ("상태 파일", "state_path", False),
                ("프로필 경로", "profile_dir", False),
            ):
                value = str(raw.get(key, "") or "").strip()
                detail.add_line(
                    section="bottom",
                    text=(
                        f"{prefix}: {self._shorten_path(value, max_chars=48)}"
                        if value
                        else f"{prefix}: (알 수 없음)"
                    ),
                    fill="#2563EB" if clickable and value else text_muted,
                    wraplength=self._scaled_wrap_length(300),
                    on_click=(
                        (lambda path=value: self._open_path(path))
                        if clickable and value
                        else None
                    ),
                )
        active = self._active_account_id
        if active not in self._account_order:
            active = self._account_order[0] if self._account_order else None
        if active is not None:
            self._select_profile(active)
        board.request_layout()
        row += 1
        return row

    def _add_realtime_status_section(self, body: Any, row: int, card_bg: str, border: str) -> int:
        tk = self._tk
        if tk is None:
            return row
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
        runtime_grid.grid(row=row, column=0, columnspan=4, sticky="we", pady=(0, 0))
        runtime_grid.columnconfigure(1, weight=1)
        runtime_grid.columnconfigure(3, weight=1)
        self._runtime_value_rows = []

        runtime_pairs = (
            (
                ("조회 상태", self._collect_state_var),
                ("다음 모니터링까지", self._next_collect_var),
            ),
            (
                ("최근 확인 시각", self._live_time_var),
                ("남은 크레딧", self._live_credit_var),
            ),
            (
                ("5시간 사용 한도", self._live_five_hour_var),
                ("5시간 한도 초기화", self._live_five_hour_reset_var),
            ),
            (
                ("주간 사용 한도", self._live_weekly_var),
                ("주간 한도 초기화", self._live_weekly_reset_var),
            ),
            (
                ("월간 사용 한도", self._live_monthly_var),
                ("월간 한도 초기화", self._live_monthly_reset_var),
            ),
            (
                ("Spark 5시간 한도", self._live_spark_five_hour_var),
                ("Spark 5시간 초기화", self._live_spark_five_hour_reset_var),
            ),
            (
                ("Spark 주간 한도", self._live_spark_weekly_var),
                ("Spark 주간 초기화", self._live_spark_weekly_reset_var),
            ),
        )
        self._live_spark_cells = []
        for runtime_row, pairs in enumerate(runtime_pairs):
            for pair_index, (label, value_var) in enumerate(pairs):
                cells = self._add_value_row(
                    runtime_grid,
                    runtime_row,
                    label,
                    value_var,
                    card_bg,
                    column=pair_index * 2,
                )
                self._runtime_value_rows.append(cells)
                if runtime_row >= 4 and cells is not None:
                    self._live_spark_cells.extend(cells)
        try:
            runtime_grid.bind(
                "<Configure>",
                lambda event, grid=runtime_grid: self._reflow_runtime_grid(
                    grid,
                    available_width=int(getattr(event, "width", 0) or 0),
                ),
            )
        except Exception:
            pass
        self._reflow_runtime_grid(runtime_grid)
        return row + 1

    def _fit_section_title(self, title: Any, action: Any, width: int) -> None:
        if title is None:
            return
        available = int(width or 0)
        if available <= 1:
            available = 520
        action_width = self._widget_requested_width(action)
        self._set_wraplength(title, max(120, available - action_width - 16))
        return

    def _card_radio_style(self, card_bg: str) -> str:
        ttk = self._ttk
        style_factory = getattr(ttk, "Style", None)
        if not callable(style_factory):
            return ""
        name = "WS.Card.TRadiobutton"
        try:
            style = style_factory()
            style.configure(
                name,
                background=card_bg,
                foreground="#111827",
                font=("Segoe UI", 9),
            )
            style.map(name, background=[("active", card_bg)])
        except Exception:
            return ""
        return name

    @staticmethod
    def _set_wraplength(widget: Any, wraplength: int) -> None:
        # 같은 값을 다시 configure하면 Tk가 요구 크기를 재계산하고 배치를
        # 다시 예약해, <Configure> 처리기가 변화 없이 연쇄를 키운다.
        if widget is None:
            return
        try:
            if int(widget.cget("wraplength") or 0) == int(wraplength):
                return
        except Exception:
            pass
        try:
            widget.configure(wraplength=wraplength)
        except Exception:
            pass
        return

    def _redraw_provider_mark(self, account_id: str) -> None:
        canvas = self._account_provider_marks.get(str(account_id or ""))
        provider_var = self._account_provider_vars.get(str(account_id or ""))
        if canvas is None or provider_var is None:
            return
        try:
            provider = str(provider_var.get() or "codex").strip().lower()
            canvas.delete("all")
            draw_provider_mark(
                canvas,
                provider,
                1,
                7,
                size=12,
                background=str(canvas.cget("bg") or "#FFFFFF"),
            )
        except Exception:
            pass
        return

    def _fit_profile_header(self, label: Any, provider: Any, mark: Any, width: int) -> None:
        available = int(width or 0)
        if available <= 1:
            available = 360
        provider_width = self._widget_requested_width(provider)
        mark_width = self._widget_requested_width(mark) + 4
        self._set_wraplength(
            label,
            max(90, available - provider_width - mark_width - 28),
        )
        return

    @staticmethod
    def _widget_requested_width(widget: Any) -> int:
        if widget is None:
            return 0
        try:
            return max(1, int(widget.winfo_reqwidth()))
        except Exception:
            pass
        try:
            configured = int(widget.cget("width"))
            return max(1, configured * 8)
        except Exception:
            return 0

    def _bind_responsive_widget_row(
        self,
        container: Any,
        widgets: list[Any],
        *,
        columns: int,
    ) -> None:
        if container is None:
            return
        try:
            container.bind(
                "<Configure>",
                lambda event, host=container, children=list(widgets), max_columns=columns: self._reflow_widget_row(
                    host,
                    children,
                    max_columns=int(max_columns),
                    available_width=int(getattr(event, "width", 0) or 0),
                ),
            )
        except Exception:
            pass
        self._reflow_widget_row(container, widgets, max_columns=columns)
        return

    def _reflow_widget_row(
        self,
        container: Any,
        widgets: list[Any],
        *,
        max_columns: int,
        available_width: int | None = None,
    ) -> None:
        children = [widget for widget in widgets if widget is not None]
        if not children:
            return
        width = int(available_width or 0)
        if width <= 1:
            try:
                width = int(container.winfo_width())
            except Exception:
                width = 0
        # 균등 열로 나누지 않고 고정 간격으로 왼쪽부터 놓는다. 행 폭을
        # 넘는 위젯만 다음 행으로 넘기므로, 열 너비가 만드는 빈 공간과
        # 넘칠 때 전부 1열로 접히는 폴백이 없다. 랩된 각 행은 별도
        # 프레임에 pack으로 놓는다. 한 그리드에 여러 행을 두면 열 폭이
        # 모든 행의 최댓값으로 잡혀 간격이 깨지고 클리핑이 생긴다.
        gap = 8
        column_cap = max(1, int(max_columns))
        widget_widths = [
            self._widget_requested_width(widget) for widget in children
        ]
        # 랩이 적용되면 컨테이너의 winfo_reqwidth()가 줄어 상자 측정 기준이
        # 흔들린다. 랩 없는 상태의 요구 폭을 별도로 저장해 두면 판정이
        # 현재 배치 상태와 무관하게 안정된다.
        try:
            container._windows_supporter_unwrapped_reqwidth = (
                sum(widget_widths) + gap * max(0, len(children) - 1)
            )
        except Exception:
            pass
        row = 0
        column = 0
        cursor = 0
        placements = []
        for widget, widget_width in zip(children, widget_widths):
            if column > 0 and (
                column >= column_cap
                or (width > 1 and cursor + widget_width > width)
            ):
                row += 1
                column = 0
                cursor = 0
            placements.append((widget, row, column))
            cursor += widget_width + gap
            column += 1
        rows_needed = placements[-1][1] + 1
        # 폭이 조금 바뀌어도 줄바꿈 결과가 같으면 grid_remove/pack을 반복하지
        # 않는다. 반복하면 위젯이 매번 다시 배치되어 배치 연쇄가 커진다.
        placement_signature = tuple(
            (id(widget), row_index, column_index)
            for widget, row_index, column_index in placements
        )
        if (
            getattr(container, "_windows_supporter_row_placement", None)
            == placement_signature
        ):
            return
        try:
            container._windows_supporter_row_placement = placement_signature
        except Exception:
            pass
        frames = self._widget_row_frames(container, rows_needed)
        use_frames = len(frames) >= rows_needed
        for index, frame in enumerate(frames):
            try:
                if use_frames and index < rows_needed:
                    frame.grid(row=index, column=0, sticky="w")
                else:
                    frame.grid_remove()
            except Exception:
                pass
        for index, (widget, row_index, column_index) in enumerate(placements):
            last_in_row = (
                index + 1 >= len(placements)
                or placements[index + 1][1] != row_index
            )
            padx = (0, 0) if last_in_row else (0, gap)
            try:
                if use_frames:
                    try:
                        widget.grid_remove()
                    except Exception:
                        pass
                    widget.pack(
                        in_=frames[row_index],
                        side="left",
                        padx=padx,
                        pady=(0, 2),
                    )
                    # Keep controls above their younger geometry-host siblings.
                    if callable(getattr(widget, 'lift', None)):
                        widget.lift()
                else:
                    widget.grid(
                        row=row_index,
                        column=column_index,
                        sticky="w",
                        padx=padx,
                        pady=(0, 2),
                    )
            except Exception:
                pass
        for column_index in range(column_cap):
            try:
                container.columnconfigure(column_index, weight=0)
            except Exception:
                pass
        return

    def _widget_row_frames(self, container: Any, count: int) -> list[Any]:
        frames = list(
            getattr(container, "_windows_supporter_row_frames", []) or []
        )
        while len(frames) < count:
            frame = None
            tk = self._tk
            bg = None
            try:
                bg = container.cget("bg")
            except Exception:
                bg = None
            try:
                if tk is not None:
                    if bg is None:
                        frame = tk.Frame(container)
                    else:
                        frame = tk.Frame(container, bg=bg)
            except Exception:
                frame = None
            if frame is None:
                break
            frames.append(frame)
        try:
            container._windows_supporter_row_frames = list(frames)
        except Exception:
            pass
        return frames

    def _configure_stable_value_grid(
        self,
        grid: Any,
        rows: list[Any],
        *,
        pair_columns: int,
    ) -> None:
        # 값 문자열의 요청 폭이나 선택 행의 표시 여부가 바뀌어도 열 경계가
        # 움직이지 않도록, 모든 정적 라벨의 최대 폭과 값 열의 균등 지분을
        # 먼저 예약한다. 숨긴 행도 rows에는 남아 있어 같은 예약 폭을 쓴다.
        label_reserve = 0
        for cells in rows:
            widgets = self._metric_cell_widgets(cells)
            if not widgets:
                continue
            label_reserve = max(
                label_reserve,
                self._widget_requested_width(widgets[0]),
            )
        value_uniform = "ai_usage_value_columns" if pair_columns > 1 else ""
        for column in range(4):
            active = column // 2 < pair_columns
            is_value = column % 2 == 1
            try:
                grid.columnconfigure(
                    column,
                    weight=1 if active and is_value else 0,
                    minsize=label_reserve if active and not is_value else 0,
                    uniform=value_uniform if active and is_value else "",
                )
            except Exception:
                pass
        return

    def _reflow_runtime_grid(self, runtime_grid: Any, *, available_width: int | None = None) -> None:
        rows = [row for row in self._runtime_value_rows if isinstance(row, (tuple, list))]
        if not rows:
            return
        width = int(available_width or 0)
        if width <= 1:
            try:
                width = int(runtime_grid.winfo_width())
            except Exception:
                width = 0
        pair_columns = 2 if width <= 1 or width >= 700 else 1
        self._configure_stable_value_grid(
            runtime_grid,
            rows,
            pair_columns=pair_columns,
        )
        hidden_spark = {
            id(widget)
            for widget in self._live_spark_cells
        } if self._live_spark_visible is False else set()
        for index, cells in enumerate(rows):
            pair_column = index % pair_columns
            row = index // pair_columns
            for widget_index, widget in enumerate(cells):
                if widget is None:
                    continue
                try:
                    if id(widget) in hidden_spark:
                        widget.grid_remove()
                        continue
                    widget.grid(
                        row=row,
                        column=pair_column * 2 + widget_index,
                        sticky="we" if widget_index else "w",
                        padx=(0, 6) if widget_index == 0 else (0, 8),
                        pady=1,
                    )
                    if widget_index == 1:
                        widget.configure(
                            wraplength=max(120, (width // pair_columns) - 120)
                        )
                except Exception:
                    pass
        return

    def _on_scroll_body_configure(self, _event: Any = None) -> None:
        canvas = self._scroll_canvas
        if canvas is None:
            return
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass
        return

    def _on_scroll_canvas_configure(self, event: Any = None) -> None:
        canvas = self._scroll_canvas
        window_id = self._scroll_window_id
        if canvas is None or window_id is None:
            return
        width = max(1, int(getattr(event, "width", 1) or 1))
        if self._profile_board is not None:
            # Board graphics fill the viewport, but the native toolbar should
            # retain its intrinsic width instead of resizing every control.
            try:
                width = min(width, max(620, int(self._scroll_body.winfo_reqwidth())))
            except Exception:
                pass
        if width == self._scroll_window_width:
            return
        try:
            canvas.itemconfigure(window_id, width=width)
            self._scroll_window_width = width
        except Exception:
            pass
        return

    def _pane_side_by_side_min_width(self) -> int:
        # 넓은 상태의 두 상자는 같은 폭을 예약한다. 따라서 각 열이 가장
        # 넓은 상자의 "랩 없는" 요청 폭을 수용할 때만 나란히 배치한다.
        # 현재 배치(랩·스택) 상태를 읽지 않아 임계값 자체도 흔들리지 않는다.
        widths = [
            self._pane_box_unwrapped_width((self._pane_boxes or {}).get(side))
            for side in ("left", "right")
        ]
        if any(item <= 0 for item in widths):
            return 0
        required = (2 * max(widths)) + 10
        return max(620, required) if self._profile_board is not None else required

    def _pane_box_unwrapped_width(self, box: Any) -> int:
        # 상자의 요구 폭은 자식 행이 랩되면 작아지고 wraplength 라벨은
        # 할당 폭을 따라가므로 둘 다 측정 기준으로 쓸 수 없다. 대신 상자
        # 안의 "줄일 수 없는" 리프 콘텐츠만 본다: 반응형 행은 저장된
        # 랩 없는 요구 폭, 그 외 리프는 고정 요구 폭. wraplength가 있는
        # 라벨은 좁아져도 줄바꿈되므로 요구 폭에서 제외한다. 컨테이너
        # 자체 폭은 자식 상태를 그대로 반영해 탄력적이므로 제외한다.
        if box is None:
            return 0
        base = 0
        try:
            stack = list(box.winfo_children())
        except Exception:
            stack = []
        # Cards share a stable ancestor so grid(in_=...) can move them without
        # destroying widgets. Include their logical children in pane measurement.
        for side, candidate in (self._pane_boxes or {}).items():
            if candidate is box:
                stack.extend(
                    card for profile_id in (self._rendered_pane_assignment or {}).get(side, [])
                    if (card := self._pane_card_widgets.get(profile_id)) is not None
                    and getattr(card, "master", None) is self._pane_card_parent
                )
        while stack:
            node = stack.pop()
            try:
                node_children = list(node.winfo_children())
            except Exception:
                node_children = []
            stack.extend(node_children)
            try:
                unwrapped = int(
                    getattr(node, "_windows_supporter_unwrapped_reqwidth", 0)
                    or 0
                )
            except Exception:
                unwrapped = 0
            if unwrapped > 0:
                base = max(
                    base, unwrapped + self._horizontal_inset(node, box)
                )
                continue
            if node_children:
                continue
            try:
                wraplength = int(node.cget("wraplength") or 0)
            except Exception:
                wraplength = 0
            if wraplength > 0 or getattr(node, "_windows_supporter_wraps", False):
                continue
            leaf_width = self._widget_requested_width(node)
            if leaf_width > 0:
                base = max(
                    base, leaf_width + self._horizontal_inset(node, box)
                )
        if base <= 0:
            # 측정 가능한 자식이 없으면 상자 자체 요구 폭으로 폴백한다.
            base = self._widget_requested_width(box)
        return base

    def _horizontal_inset(self, widget: Any, ancestor: Any) -> int:
        # 위젯이 상자 안에서 차지하는 양쪽 여백 합계: 각 hop의 grid/pack padx와
        # 중간 프레임·상자의 경계선 두께다. pack -in 배치는 name 계층을 바꾸지
        # 않으므로 master 기준으로만 오른다. 측정 대상 리프 자신의 경계선은
        # 이미 요구 폭에 포함되므로 첫 hop은 제외한다.
        inset = 0
        node = widget
        first = True
        while node is not None and node is not ancestor:
            padx = 0
            try:
                padx = node.grid_info().get("padx", 0) or 0
            except Exception:
                padx = 0
            if not padx:
                try:
                    padx = node.pack_info().get("padx", 0) or 0
                except Exception:
                    padx = 0
            if isinstance(padx, (tuple, list)):
                try:
                    padx = sum(int(item) for item in padx)
                except Exception:
                    padx = 0
            else:
                try:
                    # Tk의 스칼라 padx는 양쪽에 적용되므로 두 배로 센다.
                    padx = 2 * int(padx)
                except Exception:
                    padx = 0
            inset += max(0, padx)
            if not first:
                try:
                    # 마크된 반응형 행 컨테이너는 현재 hl=0/bd=0이므로 자체
                    # 경계선이 요구 폭에 포함되지 않는다는 전제가 성립한다.
                    inset += 2 * int(node.cget("highlightthickness") or 0)
                except Exception:
                    pass
            first = False
            parent = getattr(node, "master", None)
            try:
                geometry_parent = node.grid_info().get("in") or node.pack_info().get("in")
                if geometry_parent is not None:
                    parent = geometry_parent if hasattr(geometry_parent, "master") else node.nametowidget(str(geometry_parent))
            except Exception:
                pass
            node = parent
        try:
            inset += 2 * int(ancestor.cget("highlightthickness") or 0)
        except Exception:
            pass
        return inset

    def _reflow_pane_boxes(
        self,
        panes: Any,
        side_row: Any,
        *,
        available_width: int | None = None,
    ) -> None:
        # 왼쪽/오른쪽 상자는 두 상자가 실제로 들어갈 폭이 되면 나란히,
        # 그보다 좁으면 세로로 쌓는다. 세로일 때는 비는 열이 남은 폭을
        # 먹지 않게 column weight도 함께 맞춘다.
        width = int(available_width or 0)
        if width <= 1:
            try:
                width = int(panes.winfo_width())
            except Exception:
                width = 0
        needed = self._pane_side_by_side_min_width()
        # 필요 폭을 알 수 없으면 스택이 fail-safe다. 좁은 창에서 나란히
        # 두면 양쪽이 잘리지만, 세로로 쌓으면 전체 내용이 보인다.
        columns = 1 if needed <= 0 or (width > 1 and width < needed) else 2
        if getattr(panes, "_windows_supporter_pane_columns", None) == columns:
            return
        try:
            panes._windows_supporter_pane_columns = columns
        except Exception:
            pass
        pane_uniform = "ai_usage_pane_columns" if columns > 1 else ""
        for column_index in range(2):
            try:
                side_row.columnconfigure(
                    column_index,
                    weight=1 if column_index < columns else 0,
                    uniform=pane_uniform,
                )
            except Exception:
                pass
        for index, side in enumerate(("left", "right")):
            box = (self._pane_boxes or {}).get(side)
            if box is None:
                continue
            try:
                if columns > 1:
                    box.grid(
                        row=0,
                        column=index,
                        sticky="nwe",
                        padx=(0, 5) if index == 0 else (5, 0),
                        pady=0,
                    )
                else:
                    # grid()는 생략한 옵션을 유지하므로 나란히 배치 때의
                    # 좌우 padx가 스택에서도 남지 않게 명시적으로 둔다.
                    box.grid(
                        row=index,
                        column=0,
                        sticky="we",
                        padx=0,
                        pady=(0, 6) if index == 0 else 0,
                    )
            except Exception:
                pass
        return

    def _pane_ui_state(self) -> tuple[list[str], list[str], str]:
        order = [str(item or "") for item in (self._account_order or [])]
        order = [item for item in order if item]
        selected: list[str] = []
        for profile_id in order:
            var = (self._account_taskbar_selected_vars or {}).get(profile_id)
            try:
                flagged = bool(var.get()) if var is not None else False
            except Exception:
                flagged = False
            if flagged:
                selected.append(profile_id)
        try:
            priority = normalize_taskbar_side_priority(
                self._taskbar_side_priority_var.get()
                if self._taskbar_side_priority_var is not None
                else None
            ).value
        except Exception:
            priority = TaskbarSidePriority.LEFT.value
        return order, selected, priority

    def _pane_assignment_rendered_stale(self) -> bool:
        rendered = self._rendered_pane_assignment
        if not isinstance(rendered, dict) or not self._pane_lists:
            return False
        try:
            order, selected, priority = self._pane_ui_state()
        except Exception:
            return False
        if priority != (self._rendered_side_priority or priority):
            return True
        try:
            current = resolve_taskbar_pane_assignment(order, selected, priority)
        except Exception:
            return False
        for side in ("left", "right", "pool"):
            if list(current.get(side, [])) != list(rendered.get(side, []) or []):
                return True
        return False

    def _sync_pane_assignment(self) -> bool:
        """Reposition existing cards; preserve controls, focus and scroll state."""
        if self._pane_card_parent is None:
            return False
        order, selected, priority = self._pane_ui_state()
        if set(order) != set(self._pane_card_widgets):
            return False
        assignment = resolve_taskbar_pane_assignment(order, selected, priority)
        previous = self._rendered_pane_assignment or {}
        for side in ("left", "right", "pool"):
            host = self._pane_lists.get(side)
            if host is None:
                return False
            old = list(previous.get(side, []))
            for index, profile_id in enumerate(assignment[side]):
                if index < len(old) and old[index] == profile_id:
                    continue
                card = self._pane_card_widgets[profile_id]
                card.grid(in_=host, row=index, column=0, sticky="nwe", pady=(0, 6))
                if callable(getattr(card, "lift", None)):
                    card.lift()
            title = self._pane_titles.get(side)
            hint = self._pane_hints.get(side)
            if side == "pool":
                text = f"표시 안 함 (보관함) ({len(assignment[side])})"
            else:
                name = "왼쪽 영역" if side == "left" else "오른쪽 영역"
                text = f"{name} ({len(assignment[side])}/{TASKBAR_PANE_SIZE})"
                slots = "1·2번 슬롯" if side == priority else "3·4번 슬롯"
                if hint is not None:
                    hint.configure(text=f"작업표시줄 {slots} · 카드 위: 교환 · 사이: 이동")
            if title is not None:
                title.configure(text=text)
        self._rendered_pane_assignment = assignment
        self._rendered_side_priority = priority
        self._refresh_move_buttons()
        return True

    def _refresh_move_buttons(self) -> None:
        for index, profile_id in enumerate(self._account_order):
            buttons = self._account_move_buttons.get(profile_id)
            if buttons is not None:
                self._set_button_enabled(buttons[0], index > 0)
                self._set_button_enabled(buttons[1], index < len(self._account_order) - 1)

    def _apply_pane_drop(
        self, dragged_id: str, target_side: str, target_index: int,
        *, target_profile_id: str | None = None,
    ) -> bool:
        order, selected, priority = self._pane_ui_state()
        plan = plan_taskbar_drop(order, selected, priority, dragged_id, target_side, target_index,
                                 target_profile_id=target_profile_id)
        if plan["order"] == order and set(plan["selected"]) == set(selected):
            return True

        def apply_ui(new_order: list[str], new_selected: list[str]) -> None:
            self._account_order = list(new_order)
            selected_set = set(new_selected)
            was_loading = self._loading_settings
            self._loading_settings = True
            try:
                for profile_id, variable in self._account_taskbar_selected_vars.items():
                    self._set_var_if_changed(variable, profile_id in selected_set)
            finally:
                self._loading_settings = was_loading

        self._cancel_pending_autosave()
        apply_ui(plan["order"], plan["selected"])
        try:
            saved = bool(self._autosave_now())
        except Exception:
            apply_ui(order, selected)
            self._cancel_pending_autosave()
            raise
        if not saved:
            self._cancel_pending_autosave()
            apply_ui(order, selected)
            return False
        self._set_status("교환하여 저장됨" if target_profile_id else "저장됨", level="ok")
        return True

    def _restore_pane_card_rows(self) -> None:
        rendered = self._rendered_pane_assignment
        if not isinstance(rendered, dict):
            return
        for side in ("left", "right", "pool"):
            host = (self._pane_lists or {}).get(side)
            if host is None:
                continue
            row = 0
            try:
                for profile_id in rendered.get(side, []):
                    card = (self._pane_card_widgets or {}).get(profile_id)
                    if card is None:
                        continue
                    card.grid(row=row, column=0, sticky="nwe", pady=(0, 6))
                    row += 1
            except Exception:
                pass
        return

    def _show_drop_indicator(self, side: str, position: int) -> None:
        host = self._pane_lists.get(side)
        indicator = self._drop_indicators.get(side)
        if host is None or indicator is None:
            return
        dragged = (self._drag_state or {}).get("id")
        visible = [item for item in (self._rendered_pane_assignment or {}).get(side, []) if item != dragged]
        position = max(0, min(int(position), len(visible)))
        try:
            y = 0
            if position < len(visible):
                card = self._pane_card_widgets[visible[position]]
                y = card.winfo_rooty() - host.winfo_rooty()
            elif visible:
                card = self._pane_card_widgets[visible[-1]]
                y = card.winfo_rooty() - host.winfo_rooty() + card.winfo_height()
            indicator.place(x=0, y=max(0, y), relwidth=1, height=3)
            indicator.lift()
        except Exception:
            pass

    def _pane_swap_target_at(self, side: str, y_root: int) -> str | None:
        if side == "pool":
            return None
        for profile_id in (self._rendered_pane_assignment or {}).get(side, []):
            card = self._pane_card_widgets.get(profile_id)
            try:
                top, height = int(card.winfo_rooty()), int(card.winfo_height())
                margin = min(12, max(2, height // 5))
                if top + margin <= y_root < top + height - margin:
                    return profile_id
            except Exception:
                continue
        return None

    def _clear_pane_drag_feedback(self, state: dict[str, Any]) -> None:
        side = state.get("target")
        for widget in (self._pane_boxes.get(side), self._pane_card_widgets.get(state.get("swap"))):
            try:
                if widget is not None:
                    widget.configure(highlightbackground="#E5E7EB")
            except Exception:
                pass
        try:
            indicator = self._drop_indicators.get(side)
            if indicator is not None:
                indicator.place_forget()
        except Exception:
            pass
        state.update(target=None, index=0, swap=None)

    def _cancel_pane_drag(self, _event: Any = None) -> str:
        state = self._drag_state
        self._drag_state = None
        if isinstance(state, dict):
            self._clear_pane_drag_feedback(state)
            try:
                self._pane_card_widgets[state["id"]].configure(highlightbackground="#E5E7EB")
            except Exception:
                pass
            try:
                if state.get("grab") is not None:
                    state["grab"].grab_release()
            except Exception:
                pass
        return "break"

    def _pane_drop_target_at(self, x_root: int, y_root: int) -> tuple[str, int] | None:
        if not self._pane_boxes:
            return None
        canvas = self._scroll_canvas
        if canvas is not None:
            try:
                if not (canvas.winfo_rootx() <= x_root < canvas.winfo_rootx() + canvas.winfo_width()
                        and canvas.winfo_rooty() <= y_root < canvas.winfo_rooty() + canvas.winfo_height()):
                    return None
            except Exception:
                return None
        rendered = self._rendered_pane_assignment
        if not isinstance(rendered, dict):
            return None
        dragged = (self._drag_state or {}).get("id") if isinstance(self._drag_state, dict) else None
        for side in ("left", "right", "pool"):
            box = self._pane_boxes.get(side)
            if box is None:
                continue
            try:
                left = int(box.winfo_rootx())
                top = int(box.winfo_rooty())
                width = int(box.winfo_width())
                height = int(box.winfo_height())
            except Exception:
                continue
            if not (left <= int(x_root) <= left + width and top <= int(y_root) <= top + height):
                continue
            position = 0
            for profile_id in rendered.get(side, []):
                if profile_id == dragged:
                    continue
                card = (self._pane_card_widgets or {}).get(profile_id)
                if card is None:
                    position += 1
                    continue
                try:
                    middle = int(card.winfo_rooty()) + (int(card.winfo_height()) // 2)
                except Exception:
                    position += 1
                    continue
                if int(y_root) < middle:
                    break
                position += 1
            return side, position
        return None

    def _select_profile(self, profile_id: str) -> bool:
        """Highlight one card; keyboard navigation and Shift+F10 follow it."""
        normalized = str(profile_id or "")
        if normalized not in self._account_order:
            return False
        self._active_account_id = normalized
        board = self._profile_board
        if board is not None:
            board.select(normalized)
        return True

    def _select_and_drag_profile(self, profile_id: str, event: Any) -> None:
        self._select_profile(profile_id)
        self._on_pane_drag_start(profile_id, event)

    def _open_profile_menu(self, profile_id: str, event: Any = None) -> str:
        menu = self._profile_menu
        normalized = str(profile_id or "")
        if menu is None or normalized not in self._account_order:
            return "break"
        self._cancel_pane_drag()
        try:
            x_root, y_root = int(event.x_root), int(event.y_root)
        except (AttributeError, TypeError, ValueError):
            x_root, y_root = self._profile_menu_anchor(normalized)
        self._select_profile(normalized)
        try:
            menu.open(normalized, x_root, y_root)
        except Exception:
            pass
        return "break"

    def _open_profile_menu_for_selection(self) -> str:
        """Keyboard route (Shift+F10 / Menu key) to the selected card's menu."""
        order = self._account_order
        if not order:
            return "break"
        profile_id = self._active_account_id
        if profile_id not in order:
            profile_id = order[0]
        board = self._profile_board
        if board is not None:
            try:
                board.ensure_visible(profile_id)
            except Exception:
                pass
        return self._open_profile_menu(profile_id)

    def _profile_menu_anchor(self, profile_id: str) -> tuple[int, int]:
        card = self._pane_card_widgets.get(profile_id)
        try:
            x = int(card.winfo_rootx()) + max(0, int(card.winfo_width()) - 24)
            y = int(card.winfo_rooty()) + 24
        except Exception:
            x = y = 0
        return x, y

    def _on_pane_drag_start(self, account_id: str, event: Any = None) -> None:
        self._cancel_pane_drag()
        if self._profile_settings_mutation_blocked():
            return
        normalized = str(account_id or "").strip()
        card = self._pane_card_widgets.get(normalized)
        if card is None:
            return
        widget = getattr(event, "widget", None) or card
        start = (int(event.x_root), int(event.y_root)) if event is not None else None
        self._drag_state = {"id": normalized, "target": None, "index": 0, "swap": None,
                            "start": start, "active": False, "grab": widget}
        try:
            widget.grab_set()
            widget.focus_set()
            card.configure(highlightbackground="#2563EB")
        except Exception:
            pass

    def _on_pane_drag_motion(self, event: Any = None) -> None:
        state = self._drag_state
        if not isinstance(state, dict) or not state.get("id"):
            return
        if self._profile_deletions_inflight or self._profile_actions_inflight:
            if state.get("target") is not None:
                self._clear_pane_drag_feedback(state)
            return
        try:
            x_root, y_root = int(event.x_root), int(event.y_root)
        except (AttributeError, TypeError, ValueError):
            return
        start = state.get("start")
        if not state.get("active") and start is not None:
            if max(abs(x_root - start[0]), abs(y_root - start[1])) < 5:
                return
        state["active"] = True
        found = self._pane_drop_target_at(x_root, y_root)
        if found is None:
            if state.get("target") is not None:
                self._clear_pane_drag_feedback(state)
            return
        side, position = found
        swap_id = self._pane_swap_target_at(side, y_root)
        assignment = self._rendered_pane_assignment or {}
        if swap_id is None and side != "pool" and state["id"] in assignment.get("pool", []):
            selected_count = len(assignment.get("left", [])) + len(assignment.get("right", []))
            occupants = assignment.get(side, [])
            if selected_count >= TASKBAR_PROFILE_LIMIT and occupants:
                swap_id = occupants[max(0, min(position, len(occupants) - 1))]
        if (state.get("target"), state.get("index"), state.get("swap")) == (side, position, swap_id):
            return
        self._clear_pane_drag_feedback(state)
        state.update(target=side, index=position, swap=swap_id)
        try:
            self._pane_boxes[side].configure(highlightbackground="#2563EB")
            self._pane_card_widgets[state["id"]].configure(highlightbackground="#2563EB")
            if swap_id and swap_id != state["id"]:
                self._pane_card_widgets[swap_id].configure(highlightbackground="#059669")
        except Exception:
            pass
        if swap_id is None:
            self._show_drop_indicator(side, position)

    def _on_pane_drag_release(self, event: Any = None) -> None:
        if event is not None:
            # Release coordinates are authoritative, not the last motion event.
            self._on_pane_drag_motion(event)
        state = dict(self._drag_state or {})
        self._cancel_pane_drag()
        if not state.get("id") or not state.get("active", True):
            return
        target = state.get("target")
        if target not in ("left", "right", "pool") or self._profile_settings_mutation_blocked():
            return
        try:
            self._apply_pane_drop(str(state["id"]), str(target), int(state.get("index", 0)),
                                  target_profile_id=state.get("swap"))
        except Exception as exc:
            self._set_status(f"이동 실패: {exc}", level="error")
        return

    def _wrap_scale(self) -> float:
        """Tk scaling을 96dpi 기준 상대 배율로 바꾼다. wraplength 등 고정
        픽셀값이 고배율 디스플레이에서 너무 일찍 줄바꿈하지 않게 한다."""
        widget = self._scroll_body or self._win or self._parent
        try:
            scaling = float(widget.tk.call("tk", "scaling"))
        except Exception:
            return 1.0
        base = 96.0 / 72.0
        if scaling <= 0 or base <= 0:
            return 1.0
        return max(1.0, min(3.0, scaling / base))

    def _scaled_wrap_length(self, base_length: int) -> int:
        try:
            scale = self._wrap_scale()
        except Exception:
            scale = 1.0
        return max(1, int(round(int(base_length) * scale)))

    def _build_account_metric_rows(
        self,
        table: Any,
        *,
        provider: str = "codex",
        account_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        tk = self._tk
        if tk is None:
            return {}, {}
        if str(provider or "").lower() == "cursor":
            rows = (
                (("captured_at", "최근 확인 시각"), ("included_usage", "포함 사용량")),
                (("billing_reset_at", "결제 주기 초기화"), ("on_demand_status", "온디맨드")),
            )
        elif str(provider or "").lower() == "claude":
            rows = (
                (("captured_at", "최근 확인 시각"), ("five_hour_limit", "5시간 사용 한도")),
                (("five_hour_limit_reset_at", "5시간 한도 초기화"), ("weekly_limit", "주간 사용 한도")),
                (("weekly_limit_reset_at", "주간 한도 초기화"), ("weekly_scoped_limit", "모델별 주간 한도")),
                (("weekly_scoped_limit_reset_at", "모델별 주간 초기화"), ("on_demand_status", "추가 사용량")),
            )
        else:
            rows = (
                (("captured_at", "최근 확인 시각"), ("remaining_credit", "남은 크레딧")),
                (("five_hour_limit", "5시간 사용 한도"), ("five_hour_limit_reset_at", "5시간 한도 초기화")),
                (("weekly_limit", "주간 사용 한도"), ("weekly_limit_reset_at", "주간 한도 초기화")),
                (("monthly_limit", "월간 사용 한도"), ("monthly_limit_reset_at", "월간 한도 초기화")),
                (
                    ("gpt_5_3_codex_spark_five_hour_limit", "Spark 5시간 한도"),
                    ("gpt_5_3_codex_spark_five_hour_limit_reset_at", "Spark 5시간 초기화"),
                ),
                (
                    ("gpt_5_3_codex_spark_weekly_limit", "Spark 주간 한도"),
                    ("gpt_5_3_codex_spark_weekly_limit_reset_at", "Spark 주간 초기화"),
                ),
            )
        metric_vars: dict[str, Any] = {}
        display_vars: dict[str, Any] = {}
        for row in rows:
            for key, label in row:
                value_var = tk.StringVar(value="-")
                display_var = tk.StringVar(value="-")
                metric_vars[key] = value_var
                display_vars[key] = display_var
                self._bind_metric_display_value(
                    value_var,
                    display_var,
                )
                cell = table.add(
                    key,
                    label,
                    display_var,
                    wraplength=self._scaled_wrap_length(
                        320 if key == "on_demand_status" else 200
                    ),
                )
                if account_id and cell is not None:
                    self._account_metric_cells.setdefault(account_id, {})[key] = cell
        return metric_vars, display_vars

    def _bind_metric_display_value(
        self,
        value_var: Any,
        display_var: Any,
    ) -> None:
        def sync(*_args: Any) -> None:
            try:
                raw = str(value_var.get() or "").strip()
            except Exception:
                raw = ""
            try:
                self._set_var_if_changed(display_var, raw if raw else "-")
            except Exception:
                pass

        try:
            value_var.trace_add("write", sync)
        except Exception:
            pass
        sync()
        return

    @staticmethod
    def _set_var_if_changed(variable: Any, value: Any) -> bool:
        if variable is None:
            return False
        try:
            if variable.get() == value:
                return False
        except Exception:
            pass
        try:
            variable.set(value)
            return True
        except Exception:
            return False

    def _add_value_row(
        self,
        parent: Any,
        row: int,
        label: str,
        value_var,
        bg: str,
        column: int = 0,
    ) -> tuple[Any, Any] | None:
        tk = self._tk
        if tk is None:
            return
        label_pad = (0, 6) if column == 0 else (18, 6)
        value_pad = (0, 8)
        label_cell = tk.Label(
            parent,
            text=label,
            bg=bg,
            fg="#6B7280",
            font=("Segoe UI", 9),
        )
        label_cell.grid(row=row, column=column, sticky="w", padx=label_pad, pady=1)
        value_cell = tk.Label(
            parent,
            textvariable=value_var,
            bg=bg,
            fg="#111827",
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=self._scaled_wrap_length(250 if column else 220),
        )
        value_cell.grid(row=row, column=column + 1, sticky="w", padx=value_pad, pady=1)
        return (label_cell, value_cell)

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
        self._loading_settings = True
        settings = self._safe_get_settings()
        try:
            try:
                self._enabled_var.set(bool(settings.get("enabled", True)))
            except Exception:
                pass
            try:
                self._taskbar_overlay_var.set(bool(settings.get("taskbar_overlay_enabled", True)))
            except Exception:
                pass
            try:
                self._taskbar_side_priority_var.set(
                    normalize_taskbar_side_priority(
                        settings.get("taskbar_side_priority")
                    ).value
                )
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
            accounts = settings.get("profiles")
            if not isinstance(accounts, list):
                accounts = settings.get("accounts")
            if isinstance(accounts, list):
                loaded_order = []
                for raw in accounts:
                    if not isinstance(raw, dict):
                        continue
                    account_id = str(raw.get("id", "") or "")
                    if account_id:
                        loaded_order.append(account_id)
                    var = self._account_enabled_vars.get(account_id)
                    if var is not None:
                        try:
                            var.set(bool(raw.get("enabled", True)))
                        except Exception:
                            pass
                    provider_var = self._account_provider_vars.get(account_id)
                    if provider_var is not None:
                        try:
                            provider_var.set(str(raw.get("provider", "codex") or "codex"))
                        except Exception:
                            pass
                    selected_var = self._account_taskbar_selected_vars.get(account_id)
                    if selected_var is not None:
                        try:
                            selected_var.set(bool(raw.get("taskbar_selected", True)))
                        except Exception:
                            pass
                if loaded_order:
                    self._account_order = loaded_order
            self._set_status("", level="info")
        finally:
            self._loading_settings = False
        return

    def _on_reload(self) -> None:
        self._load_settings()
        self._set_status("로드 완료", level="ok")
        return

    def _on_login(self) -> None:
        if self._profile_settings_mutation_blocked():
            return
        if not hasattr(self._codex, "show_current_status"):
            self._set_status("연결 기능을 사용할 수 없습니다.", level="error")
            return
        try:
            runtime = self._safe_get_runtime()
            if bool(runtime.get("logout_in_progress", False)):
                self._set_status("연결 해제 진행 중입니다. 완료 후 다시 시도해 주세요.", level="info")
                return
            can_login = bool(runtime.get("can_login", True))
            if not can_login:
                self._set_status("현재 상태에서는 연결 요청을 시작할 수 없습니다.", level="info")
                return
        except Exception:
            pass
        self._set_status("연결 창을 여는 중입니다...", level="info")
        try:
            self._codex.show_current_status(force_refresh=True, source="manual_login")
        except Exception:
            self._set_status("연결 요청 중 오류가 발생했습니다.", level="error")
            return
        return

    def _on_account_login(self, account_id: str) -> None:
        if self._profile_settings_mutation_blocked():
            return
        account_label = self._account_display_label(account_id)
        try:
            runtime = self._safe_get_runtime()
            entry = self._find_account_runtime_entry(runtime, account_id)
            if entry is not None:
                can_login, _can_logout = self._account_action_permissions(entry)
                if not can_login:
                    self._set_status(
                        "현재 상태에서는 해당 프로필 연결 요청을 시작할 수 없습니다.",
                        level="info",
                    )
                    return
        except Exception:
            pass
        login = getattr(self._codex, "login_account", None)
        if callable(login):
            self._set_status(f"{account_label} 연결 창을 여는 중입니다...", level="info")
            try:
                login(str(account_id))
            except Exception:
                self._set_status("연결 요청 중 오류가 발생했습니다.", level="error")
            return
        show = getattr(self._codex, "show_account_status", None)
        if callable(show):
            try:
                show(str(account_id), force_refresh=True, source="manual_login")
                self._set_status(f"{account_label} 연결 창을 여는 중입니다...", level="info")
            except Exception:
                self._set_status("연결 요청 중 오류가 발생했습니다.", level="error")
            return
        self._set_status("프로필별 연결 기능을 사용할 수 없습니다.", level="error")
        return

    def _on_account_query(self, account_id: str) -> None:
        if self._profile_settings_mutation_blocked():
            return
        account_label = self._account_display_label(account_id)
        show = getattr(self._codex, "show_account_status", None)
        if callable(show):
            try:
                show(str(account_id), force_refresh=True, source="manual_query")
                self._set_status(f"{account_label} 사용량 조회를 시작했습니다.", level="info")
            except Exception:
                self._set_status("사용량 조회 요청 중 오류가 발생했습니다.", level="error")
            return
        self._set_status("프로필별 조회 기능을 사용할 수 없습니다.", level="error")
        return

    def _on_release_profile(self) -> None:
        if self._profile_settings_mutation_blocked():
            return
        tk = self._tk
        if tk is None:
            return
        if not hasattr(self._codex, "release_profile_session"):
            self._set_status("연결 해제 기능을 사용할 수 없습니다.", level="error")
            return
        confirmed = True
        try:
            from tkinter import messagebox

            confirmed = bool(
                messagebox.askyesno(
                    "연결 해제",
                    "현재 AI 사용량 연결을 해제하시겠습니까?\n"
                    "연결 해제 후에는 연결 버튼 또는 Ctrl+Alt+C로 다시 연결할 수 있습니다.",
                    parent=self._win,
                )
            )
        except Exception:
            confirmed = False
        if not confirmed:
            return

        action_id = "__global_release__"
        self._profile_actions_inflight.add(action_id)
        self._set_status("연결 해제 중...", level="info")

        def worker() -> None:
            ok = False
            message = ""
            try:
                ok, message = self._codex.release_profile_session()
            except Exception:
                ok = False
                message = "연결 해제 중 오류가 발생했습니다."
            if not message:
                message = "연결 해제가 완료되었습니다." if ok else "연결 해제에 실패했습니다."

            def done() -> None:
                self._finish_profile_release_on_ui(action_id, ok, message)
                return

            if not self._post_ui(done):
                self._record_pending_profile_release_result(action_id, ok, message)
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._finish_profile_release_on_ui(
                action_id,
                False,
                "연결 해제 작업을 시작하지 못했습니다.",
            )
        return

    def _on_account_release_profile(self, account_id: str) -> None:
        if self._profile_settings_mutation_blocked():
            return
        normalized = str(account_id or "")
        if not normalized:
            return
        account_label = self._account_display_label(account_id)
        tk = self._tk
        if tk is None:
            return
        try:
            runtime = self._safe_get_runtime()
            entry = self._find_account_runtime_entry(runtime, account_id)
            if entry is not None:
                _can_login, can_logout = self._account_action_permissions(entry)
                if not can_logout:
                    self._set_status(
                        "현재 상태에서는 해당 프로필 연결 해제를 시작할 수 없습니다.",
                        level="info",
                    )
                    return
        except Exception:
            pass
        release = getattr(self._codex, "release_account_profile_session", None)
        if not callable(release):
            self._set_status("프로필별 연결 해제 기능을 사용할 수 없습니다.", level="error")
            return
        confirmed = True
        try:
            from tkinter import messagebox

            confirmed = bool(
                messagebox.askyesno(
                    "연결 해제",
                    f"{account_label} AI 사용량 연결을 해제하시겠습니까?",
                    parent=self._win,
                )
            )
        except Exception:
            confirmed = False
        if not confirmed:
            return
        self._profile_actions_inflight.add(normalized)
        self._set_status(f"{account_label} 연결 해제 중...", level="info")

        def worker() -> None:
            ok = False
            message = ""
            try:
                ok, message = release(normalized)
            except Exception:
                ok = False
                message = "연결 해제 중 오류가 발생했습니다."
            if not message:
                message = "연결 해제가 완료되었습니다." if ok else "연결 해제에 실패했습니다."

            def done() -> None:
                self._finish_profile_release_on_ui(normalized, ok, message)
                return

            if not self._post_ui(done):
                self._record_pending_profile_release_result(normalized, ok, message)
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._finish_profile_release_on_ui(
                normalized,
                False,
                "연결 해제 작업을 시작하지 못했습니다.",
            )
        return

    def _finish_profile_release_on_ui(
        self,
        action_id: str,
        ok: bool,
        message: str,
    ) -> None:
        retry_prepared = self._consume_blocked_mutation_settings()
        self._profile_actions_inflight.discard(str(action_id or ""))
        if retry_prepared is None and bool(ok):
            self._load_settings()
        elif retry_prepared is not None:
            self._schedule_captured_autosave_retry(retry_prepared)
        if bool(ok):
            self._refresh_runtime_status()
            self._set_status(message, level="ok")
            return
        self._set_status(message, level="error")
        return

    def _record_pending_profile_release_result(
        self,
        action_id: str,
        ok: bool,
        message: str,
    ) -> None:
        with self._profile_release_result_lock:
            self._pending_profile_release_result = (
                str(action_id or ""),
                bool(ok),
                str(message or ""),
            )
        return

    def _reconcile_pending_profile_release_result(self) -> bool:
        with self._profile_release_result_lock:
            result = self._pending_profile_release_result
            self._pending_profile_release_result = None
        if result is None:
            return False
        self._finish_profile_release_on_ui(*result)
        return True

    def _post_ui(self, fn) -> bool:
        if not callable(fn):
            return False
        ui_post = self._ui_post
        if callable(ui_post):
            try:
                return ui_post(fn) is not False
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
        self._save_settings()
        return

    def _register_autosave_traces(self) -> None:
        for var in (
            self._enabled_var,
            self._taskbar_overlay_var,
            self._taskbar_side_priority_var,
            self._interval_var,
            *self._account_enabled_vars.values(),
            *self._account_provider_vars.values(),
            *self._account_taskbar_selected_vars.values(),
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

    def _schedule_autosave(self, *, immediate_fallback: bool = True) -> None:
        if bool(self._loading_settings):
            return
        if self._profile_settings_mutation_blocked():
            if "__add_profile__" in self._profile_deletions_inflight:
                self._profile_add_settings_changed = True
            else:
                self._blocked_mutation_settings_changed = True
            return
        self._cancel_pending_autosave()
        win = self._win
        after = getattr(win, "after", None)
        if callable(after):
            try:
                self._autosave_after_id = after(350, self._autosave_now)
                return
            except Exception:
                self._autosave_after_id = None
        if bool(immediate_fallback):
            self._autosave_now()
        return

    def _cancel_pending_autosave(self) -> None:
        win = self._win
        after_cancel = getattr(win, "after_cancel", None)
        if self._autosave_after_id is not None and callable(after_cancel):
            try:
                after_cancel(self._autosave_after_id)
            except Exception:
                pass
        self._autosave_after_id = None
        return

    def _autosave_now(self) -> bool:
        self._cancel_pending_autosave()
        return self._save_settings(reschedule_transient=True)

    def _parse_positive_seconds_strict(self, text: str, label: str) -> tuple[float, str | None]:
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

    def _build_settings_update(self) -> dict[str, Any] | None:
        before_settings = self._safe_get_settings()
        before_profiles = before_settings.get("profiles")
        if not isinstance(before_profiles, list):
            before_profiles = before_settings.get("accounts")
        before_providers = {
            str(raw.get("id") or ""): str(raw.get("provider") or "codex").lower()
            for raw in before_profiles or []
            if isinstance(raw, dict) and str(raw.get("id") or "")
        }
        enabled = bool(self._enabled_var.get())
        interval_sec, parse_error = self._parse_positive_seconds_strict(
            self._interval_var.get(),
            "주기(초)",
        )
        if parse_error:
            self._set_status(f"저장 실패: {parse_error}", level="error")
            return None
        tooltip_sec = self._parse_seconds(self._tooltip_var.get(), default=7.0)
        accounts = self._build_account_settings_payload()
        selected_profile_ids = [
            str(item.get("id") or "")
            for item in accounts
            if bool(item.get("taskbar_selected"))
        ]
        if len(selected_profile_ids) > TASKBAR_PROFILE_LIMIT:
            self._set_status(
                f"저장 실패: 작업표시줄 표시 프로필은 최대 {TASKBAR_PROFILE_LIMIT}개입니다.",
                level="error",
            )
            return None
        payload = {
            "enabled": enabled,
            "taskbar_overlay_enabled": bool(self._taskbar_overlay_var.get()),
            "taskbar_side_priority": normalize_taskbar_side_priority(
                self._taskbar_side_priority_var.get()
                if self._taskbar_side_priority_var is not None
                else None
            ).value,
            "interval_sec": interval_sec,
            "tooltip_duration_ms": int(round(tooltip_sec * 1000.0)),
            "profiles": accounts,
            "accounts": accounts,
            "profile_order": [str(item.get("id") or "") for item in accounts],
            "selected_profile_ids": selected_profile_ids,
        }
        if accounts:
            payload["default_account_id"] = str(accounts[0].get("id") or "")
        return {
            "payload": payload,
            "before_providers": before_providers,
        }

    def _apply_settings_update(
        self,
        prepared: dict[str, Any],
        *,
        update_ui: bool = True,
    ) -> tuple[bool, str | None, bool]:
        payload = prepared.get("payload") if isinstance(prepared, dict) else None
        if not isinstance(payload, dict):
            return False, "invalid settings", False
        try:
            ok, err = self._codex.update_settings(payload)
        except Exception as exc:
            ok = False
            err = str(exc)
        provider_changed = self._prepared_settings_changes_provider(prepared)
        if ok:
            if update_ui:
                # 상자 배치가 바뀌는 저장(우선순위 뒤집기·표시 체크 변경)은
                # 다시 그려야 상자 제목과 슬롯 안내가 실제 배치와 일치한다.
                # 저장이 끝난 뒤 다시 그리므로 입력 손실이 없다.
                if provider_changed:
                    self._remount()
                elif self._pane_assignment_rendered_stale():
                    if not self._sync_pane_assignment():
                        self._remount()
                if self._preserve_status_after_next_autosave:
                    self._preserve_status_after_next_autosave = False
                else:
                    self._set_status("저장됨", level="ok")
            return True, None, provider_changed
        if update_ui:
            self._preserve_status_after_next_autosave = False
            self._set_status(f"저장 실패: {err}", level="error")
        return False, str(err or "settings_save_failed"), provider_changed

    def _prepared_settings_changes_provider(self, prepared: dict[str, Any]) -> bool:
        payload = prepared.get("payload") if isinstance(prepared, dict) else None
        before_providers = (
            prepared.get("before_providers") if isinstance(prepared, dict) else None
        )
        if not isinstance(payload, dict) or not isinstance(before_providers, dict):
            return False
        accounts = payload.get("profiles")
        if not isinstance(accounts, list):
            accounts = []
        return any(
            before_providers.get(str(item.get("id") or ""))
            != str(item.get("provider") or "codex").lower()
            for item in accounts
            if isinstance(item, dict)
            and str(item.get("id") or "") in before_providers
        )

    def _flush_provider_changing_settings_before_worker(
        self,
        prepared: dict[str, Any] | None,
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        if not isinstance(prepared, dict) or not self._prepared_settings_changes_provider(
            prepared
        ):
            return True, None, prepared
        ok, error, _provider_changed = self._apply_settings_update(
            prepared,
            update_ui=False,
        )
        return bool(ok), error, None if ok else prepared

    def _save_settings(self, *, reschedule_transient: bool = False) -> bool:
        if self._profile_settings_mutation_blocked():
            return False
        prepared = self._build_settings_update()
        if prepared is None:
            return False
        ok, error, _provider_changed = self._apply_settings_update(prepared)
        if not ok and bool(reschedule_transient) and error == "profile_refresh_busy":
            self._schedule_autosave(immediate_fallback=False)
        return bool(ok)

    def _build_account_settings_payload(self) -> list[dict[str, Any]]:
        settings = self._safe_get_settings()
        accounts = settings.get("profiles")
        if not isinstance(accounts, list):
            accounts = settings.get("accounts")
        if not isinstance(accounts, list):
            return []
        accounts_by_id = {
            str(raw.get("id", "") or ""): raw
            for raw in accounts
            if isinstance(raw, dict) and str(raw.get("id", "") or "")
        }
        order = [
            account_id
            for account_id in self._account_order
            if account_id in accounts_by_id
        ]
        order.extend(account_id for account_id in accounts_by_id if account_id not in order)
        payload = []
        for account_id in order:
            raw = accounts_by_id.get(account_id, {})
            if not account_id:
                continue
            item = dict(raw)
            var = self._account_enabled_vars.get(account_id)
            if var is not None:
                try:
                    item["enabled"] = bool(var.get())
                except Exception:
                    pass
            provider_var = self._account_provider_vars.get(account_id)
            if provider_var is not None:
                try:
                    provider = str(provider_var.get() or "codex").strip().lower()
                    item["provider"] = provider if provider in {"codex", "cursor", "claude"} else "codex"
                except Exception:
                    pass
            selected_var = self._account_taskbar_selected_vars.get(account_id)
            if selected_var is not None:
                try:
                    item["taskbar_selected"] = bool(selected_var.get())
                except Exception:
                    pass
            payload.append(item)
        return payload

    def _on_add_profile(self) -> None:
        if self._profile_settings_mutation_blocked():
            return
        creator = getattr(self._codex, "add_profile", None)
        if not callable(creator):
            self._set_status("프로필 추가 기능을 사용할 수 없습니다.", level="error")
            return
        prepared = None
        if self._autosave_after_id is not None:
            self._cancel_pending_autosave()
            prepared = self._build_settings_update()
            if prepared is None:
                return
        ok, error, prepared = self._flush_provider_changing_settings_before_worker(
            prepared
        )
        if not ok:
            if prepared is not None:
                self._schedule_captured_autosave_retry(prepared)
            self._set_status(
                f"프로필 추가 전 provider 변경사항을 저장하지 못했습니다: {error}",
                level="error",
            )
            return
        mutation_id = "__add_profile__"
        self._profile_add_settings_changed = False
        self._profile_deletions_inflight.add(mutation_id)
        self._set_status("프로필 추가 중...", level="info")

        if prepared is None:
            self._finish_profile_add_on_ui(True, None, False)
            return

        def worker() -> None:
            ok, error, _provider_changed = self._apply_settings_update(
                prepared,
                update_ui=False,
            )
            if not self._post_ui(
                lambda: self._finish_profile_add_on_ui(ok, error, not ok)
            ):
                self._record_pending_profile_add_result(ok, error, not ok)
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._profile_deletions_inflight.discard(mutation_id)
            if prepared is not None:
                self._schedule_autosave()
            self._set_status("프로필 추가 작업을 시작하지 못했습니다.", level="error")
        return

    def _bind_scroll_navigation(self, canvas: Any, body: Any) -> None:
        def scroll_units(units: int):
            try:
                canvas.yview_scroll(int(units), "units")
            except Exception:
                pass
            return "break"

        def scroll_pages(pages: int):
            try:
                canvas.yview_scroll(int(pages), "pages")
            except Exception:
                pass
            return "break"

        def scroll_edge(fraction: float):
            try:
                canvas.yview_moveto(float(fraction))
            except Exception:
                pass
            return "break"

        bindings = {
            "<Up>": lambda _event: scroll_units(-1),
            "<Down>": lambda _event: scroll_units(1),
            "<Prior>": lambda _event: scroll_pages(-1),
            "<Next>": lambda _event: scroll_pages(1),
            "<Home>": lambda _event: scroll_edge(0.0),
            "<End>": lambda _event: scroll_edge(1.0),
        }
        pending = [body, canvas]
        seen: set[int] = set()
        native_input_sequences = {"<Up>", "<Down>", "<Home>", "<End>"}
        native_input_classes = {
            "entry",
            "tentry",
            "combobox",
            "tcombobox",
            "spinbox",
            "tspinbox",
        }
        while pending:
            current = pending.pop()
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                widget_class = str(current.winfo_class() or "").strip().lower()
            except Exception:
                widget_class = ""
            for sequence, callback in bindings.items():
                if (
                    widget_class in native_input_classes
                    and sequence in native_input_sequences
                ):
                    continue
                try:
                    current.bind(sequence, callback, add="+")
                except TypeError:
                    try:
                        current.bind(sequence, callback)
                    except Exception:
                        pass
                except Exception:
                    pass
            try:
                pending.extend(list(current.winfo_children()))
            except Exception:
                pass
        return

    def _bind_mousewheel_tree(self, widget: Any, canvas: Any) -> None:
        def on_mousewheel(event):
            if not self._event_is_inside_scroll_canvas(event, canvas):
                return None
            delta = int(getattr(event, "delta", 0) or 0)
            if delta == 0:
                return None
            units = max(1, abs(delta) // 120)
            self._queue_scroll_units(canvas, -units if delta > 0 else units)
            return "break"

        callbacks = {
            "<MouseWheel>": on_mousewheel,
            "<Button-4>": lambda event: self._scroll_button_event(canvas, event, -1),
            "<Button-5>": lambda event: self._scroll_button_event(canvas, event, 1),
        }
        # Bind the canvas itself for direct events (and synthetic QA events),
        # then use the toplevel binding for child widgets. Binding every label,
        # button and entry made each wheel event traverse a large binding tree.
        for sequence, callback in callbacks.items():
            try:
                canvas.bind(sequence, callback, add="+")
            except TypeError:
                try:
                    canvas.bind(sequence, callback)
                except Exception:
                    pass
            except Exception:
                pass
        root = self._root
        if root is not None and root is not canvas:
            for sequence, callback in callbacks.items():
                try:
                    binding_id = root.bind(sequence, callback, add="+")
                    if binding_id:
                        self._scroll_root_bindings.append((sequence, binding_id))
                except TypeError:
                    try:
                        root.bind(sequence, callback)
                    except Exception:
                        pass
                except Exception:
                    pass
        return

    def _scroll_button_event(self, canvas: Any, event: Any, units: int) -> str | None:
        if not self._event_is_inside_scroll_canvas(event, canvas):
            return None
        return self._queue_scroll_units(canvas, units)

    def _unbind_scroll_root_bindings(self) -> None:
        root = self._root
        bindings = self._scroll_root_bindings
        self._scroll_root_bindings = []
        if root is None:
            return
        for sequence, binding_id in bindings:
            try:
                root.unbind(sequence, binding_id)
            except Exception:
                pass
        return

    def _event_is_inside_scroll_canvas(self, event: Any, canvas: Any) -> bool:
        event_widget = getattr(event, "widget", None)
        if event_widget is canvas:
            return True
        try:
            x_root = int(getattr(event, "x_root"))
            y_root = int(getattr(event, "y_root"))
            left = int(canvas.winfo_rootx())
            top = int(canvas.winfo_rooty())
            right = left + int(canvas.winfo_width())
            bottom = top + int(canvas.winfo_height())
            return left <= x_root < right and top <= y_root < bottom
        except Exception:
            # Synthetic unit events do not carry screen coordinates. They are
            # only delivered through the canvas binding, so treating them as
            # inside keeps the test and Tk fallback paths deterministic.
            return event_widget is None or event_widget is canvas

    def _queue_scroll_units(self, canvas: Any, units: int) -> str:
        amount = int(units)
        if amount == 0:
            return "break"
        self._scroll_pending_canvas = canvas
        self._scroll_pending_units += amount
        if self._scroll_after_id is not None:
            return "break"
        host = self._win or canvas
        after_idle = getattr(host, "after_idle", None)
        if callable(after_idle):
            try:
                self._scroll_after_id = after_idle(self._flush_pending_scroll)
                return "break"
            except Exception:
                self._scroll_after_id = None
        self._flush_pending_scroll()
        return "break"

    def _flush_pending_scroll(self) -> None:
        canvas = self._scroll_pending_canvas
        units = int(self._scroll_pending_units)
        self._scroll_pending_canvas = None
        self._scroll_pending_units = 0
        self._scroll_after_id = None
        if canvas is None or units == 0:
            return
        try:
            canvas.yview_scroll(units, "units")
        except Exception:
            pass
        return

    def _cancel_pending_scroll(self) -> None:
        after_id = self._scroll_after_id
        self._scroll_after_id = None
        self._scroll_pending_canvas = None
        self._scroll_pending_units = 0
        if not after_id:
            return
        host = self._win or self._scroll_canvas
        try:
            host.after_cancel(after_id)
        except Exception:
            pass
        return

    def _scroll_canvas_units(self, canvas: Any, units: int):
        try:
            canvas.yview_scroll(int(units), "units")
        except Exception:
            pass
        return "break"

    def _on_delete_profile(self, account_id: str, label: str = "") -> None:
        normalized = str(account_id or "")
        if not normalized:
            return
        if self._profile_actions_inflight:
            self._set_status(
                "프로필 연결 작업이 진행 중이므로 삭제할 수 없습니다.",
                level="info",
            )
            return
        if self._profile_deletions_inflight:
            message = (
                "이 프로필은 이미 삭제 중입니다."
                if normalized in self._profile_deletions_inflight
                else "다른 프로필 삭제 작업이 진행 중입니다."
            )
            self._set_status(message, level="info")
            return
        try:
            from tkinter import messagebox

            confirmed = bool(
                messagebox.askyesno(
                    "AI 사용량 프로필 삭제",
                    f"'{str(label or normalized)}' 프로필과 이 앱이 관리하는 해당 프로필 데이터만 삭제할까요?",
                    parent=self._win,
                )
            )
        except Exception:
            confirmed = False
        if not confirmed:
            return
        deleter = getattr(self._codex, "delete_profile", None)
        if not callable(deleter):
            self._set_status("프로필 삭제 기능을 사용할 수 없습니다.", level="error")
            return
        prepared = None
        if self._autosave_after_id is not None:
            self._cancel_pending_autosave()
            prepared = self._build_settings_update()
            if prepared is None:
                return
        ok, error, prepared = self._flush_provider_changing_settings_before_worker(
            prepared
        )
        if not ok:
            if prepared is not None:
                self._schedule_captured_autosave_retry(prepared)
            self._set_status(
                f"프로필 삭제 전 provider 변경사항을 저장하지 못했습니다: {error}",
                level="error",
            )
            return
        self._blocked_mutation_settings_changed = False
        self._profile_deletions_inflight.add(normalized)
        self._set_status("프로필 삭제 중...", level="info")

        def worker() -> None:
            ok = True
            error = None
            save_failed = False
            if prepared is not None:
                ok, error, _provider_changed = self._apply_settings_update(
                    prepared,
                    update_ui=False,
                )
                save_failed = not ok
            if ok:
                try:
                    ok, error = deleter(normalized, confirmed=True)
                except Exception as exc:
                    ok = False
                    error = str(exc)

            def done() -> None:
                self._finish_profile_delete_on_ui(
                    normalized,
                    ok,
                    error,
                    save_failed,
                    prepared,
                )
                return

            if not self._post_ui(done):
                self._record_pending_profile_delete_result(
                    normalized,
                    ok,
                    error,
                    save_failed,
                    prepared,
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._profile_deletions_inflight.discard(normalized)
            self._blocked_mutation_settings_changed = False
            if prepared is not None:
                self._schedule_autosave()
            self._set_status("프로필 삭제 작업을 시작하지 못했습니다.", level="error")
        return

    def _on_taskbar_selection_changed(self, account_id: str) -> None:
        if self._profile_settings_mutation_blocked():
            return
        normalized = str(account_id or "")
        selected = [
            profile_id
            for profile_id, var in self._account_taskbar_selected_vars.items()
            if bool(var.get())
        ]
        if len(selected) > TASKBAR_PROFILE_LIMIT:
            current = self._account_taskbar_selected_vars.get(normalized)
            if current is not None:
                self._loading_settings = True
                try:
                    current.set(False)
                finally:
                    self._loading_settings = False
            self._cancel_pending_autosave()
            rejection = (
                f"작업표시줄 표시 프로필은 최대 {TASKBAR_PROFILE_LIMIT}개입니다. "
                "다섯 번째 선택은 저장하지 않았습니다."
            )
            self._preserve_status_after_next_autosave = True
            self._schedule_autosave()
            self._set_status(rejection, level="error")
            return
        self._schedule_autosave()
        return

    def _on_move_account(self, account_id: str, direction: int) -> None:
        if self._profile_settings_mutation_blocked():
            return
        normalized = str(account_id or "")
        if not normalized:
            return
        order = list(self._account_order)
        if normalized not in order:
            order.append(normalized)
        index = order.index(normalized)
        new_index = max(0, min(len(order) - 1, index + int(direction)))
        if new_index == index:
            return
        order[index], order[new_index] = order[new_index], order[index]
        self._account_order = order
        if self._autosave_now():
            # 순서가 바뀌면 _apply_settings_update 안에서 상자 배치가
            # 함께 다시 그려지므로 중복 호출하지 않는다.
            self._set_status("저장됨", level="ok")
        return

    def _profile_settings_mutation_blocked(self) -> bool:
        if not self._profile_deletions_inflight and not self._profile_actions_inflight:
            return False
        self._set_status(
            "프로필 변경 중에는 다른 AI 사용량 설정을 변경할 수 없습니다.",
            level="info",
        )
        return True

    def _begin_external_settings_mutation(self) -> tuple[bool, dict[str, Any] | None]:
        if self._profile_settings_mutation_blocked():
            return False, None
        prepared = None
        if self._autosave_after_id is not None:
            self._cancel_pending_autosave()
            prepared = self._build_settings_update()
            if prepared is None:
                return False, None
        self._blocked_mutation_settings_changed = False
        self._profile_deletions_inflight.add("__external_settings__")
        return True, prepared

    def _finish_external_settings_mutation(
        self,
        ok: bool,
        error: str | None,
    ) -> None:
        retry_prepared = self._consume_blocked_mutation_settings()
        self._profile_deletions_inflight.discard("__external_settings__")
        self._preserve_status_after_next_autosave = False
        if bool(ok):
            self._remount()
            if retry_prepared is not None:
                self._schedule_captured_autosave_retry(retry_prepared)
            return
        if retry_prepared is not None:
            self._schedule_captured_autosave_retry(retry_prepared)
        self._set_status(f"설정 변경 실패: {error}", level="error")
        return

    def _release_external_settings_mutation_without_ui(self) -> None:
        """Release the state-only guard when the UI callback queue is unavailable."""
        self._profile_deletions_inflight.discard("__external_settings__")
        self._preserve_status_after_next_autosave = False
        self._blocked_mutation_settings_changed = False
        return

    def _consume_blocked_mutation_settings(self) -> dict[str, Any] | None:
        changed = bool(self._blocked_mutation_settings_changed)
        self._blocked_mutation_settings_changed = False
        if not changed:
            return None
        return self._build_settings_update()

    def _prepared_settings_without_profile(
        self,
        prepared: dict[str, Any],
        profile_id: str,
    ) -> dict[str, Any]:
        normalized = str(profile_id or "")
        result = dict(prepared)
        payload = prepared.get("payload")
        if not isinstance(payload, dict):
            return result
        updated_payload = dict(payload)
        remaining_ids: list[str] = []
        for key in ("profiles", "accounts"):
            raw_items = payload.get(key)
            if not isinstance(raw_items, list):
                continue
            filtered = [
                dict(item)
                for item in raw_items
                if isinstance(item, dict) and str(item.get("id") or "") != normalized
            ]
            updated_payload[key] = filtered
            if key == "profiles":
                remaining_ids = [str(item.get("id") or "") for item in filtered]
        for key in ("profile_order", "selected_profile_ids"):
            raw_ids = payload.get(key)
            if isinstance(raw_ids, list):
                updated_payload[key] = [
                    str(item or "") for item in raw_ids if str(item or "") != normalized
                ]
        if str(payload.get("default_account_id") or "") == normalized:
            updated_payload["default_account_id"] = remaining_ids[0] if remaining_ids else ""
        result["payload"] = updated_payload
        before_providers = prepared.get("before_providers")
        if isinstance(before_providers, dict):
            updated_before = dict(before_providers)
            updated_before.pop(normalized, None)
            result["before_providers"] = updated_before
        return result

    def _finish_profile_delete_on_ui(
        self,
        profile_id: str,
        ok: bool,
        error: str | None,
        save_failed: bool,
        prepared: dict[str, Any] | None,
    ) -> None:
        retry_prepared = self._consume_blocked_mutation_settings()
        if retry_prepared is None and bool(save_failed) and isinstance(prepared, dict):
            retry_prepared = prepared
        if bool(ok) and retry_prepared is not None:
            retry_prepared = self._prepared_settings_without_profile(
                retry_prepared,
                profile_id,
            )
        self._profile_deletions_inflight.discard(str(profile_id or ""))
        self._preserve_status_after_next_autosave = False
        if not bool(ok):
            if not bool(save_failed):
                self._remount()
            if retry_prepared is not None:
                self._schedule_captured_autosave_retry(retry_prepared)
            message = (
                "프로필 삭제 전 변경사항을 저장하지 못해 삭제를 시작하지 않았습니다."
                if bool(save_failed)
                else f"프로필 삭제 실패: {error}"
            )
            self._set_status(message, level="error")
            return
        self._remount()
        if retry_prepared is not None:
            self._schedule_captured_autosave_retry(retry_prepared)
        self._set_status("프로필을 삭제했습니다.", level="ok")
        return

    def _record_pending_profile_delete_result(
        self,
        profile_id: str,
        ok: bool,
        error: str | None,
        save_failed: bool,
        prepared: dict[str, Any] | None,
    ) -> None:
        captured = prepared if isinstance(prepared, dict) else None
        with self._profile_delete_result_lock:
            self._pending_profile_delete_result = (
                str(profile_id or ""),
                bool(ok),
                str(error) if error is not None else None,
                bool(save_failed),
                captured,
            )
        return

    def _reconcile_pending_profile_delete_result(self) -> bool:
        with self._profile_delete_result_lock:
            result = self._pending_profile_delete_result
            self._pending_profile_delete_result = None
        if result is None:
            return False
        self._finish_profile_delete_on_ui(*result)
        return True

    def _finish_profile_add_on_ui(
        self,
        save_ok: bool,
        save_error: str | None,
        save_failed: bool,
    ) -> bool:
        creator = getattr(self._codex, "add_profile", None)
        ok = bool(save_ok and callable(creator))
        error = save_error if callable(creator) else "profile_add_unavailable"
        if ok:
            try:
                ok, error, _profile = creator("codex")
            except Exception as exc:
                ok = False
                error = str(exc)
        settings_changed = bool(self._profile_add_settings_changed)
        self._profile_add_settings_changed = False
        retry_prepared = None
        if settings_changed and not save_failed:
            retry_prepared = self._build_settings_update()
        retry_capture_failed = bool(settings_changed and retry_prepared is None)
        self._profile_deletions_inflight.discard("__add_profile__")
        self._preserve_status_after_next_autosave = False
        if not ok:
            if save_failed:
                self._schedule_autosave()
                self._set_status(f"프로필 추가 실패: {error}", level="error")
                return False
            self._remount()
            if retry_prepared is not None:
                self._schedule_captured_autosave_retry(retry_prepared)
            if retry_capture_failed:
                self._set_status(
                    "프로필 추가 실패 후 보류된 설정을 저장하지 못했습니다.",
                    level="error",
                )
                return True
            self._set_status(f"프로필 추가 실패: {error}", level="error")
            return True
        self._remount()
        if retry_prepared is not None:
            self._schedule_captured_autosave_retry(retry_prepared)
        if retry_capture_failed:
            self._set_status(
                "프로필은 추가했지만 보류된 설정을 저장하지 못했습니다.",
                level="error",
            )
            return True
        self._set_status("프로필을 추가했습니다.", level="ok")
        return True

    def _record_pending_profile_add_result(
        self,
        save_ok: bool,
        save_error: str | None,
        save_failed: bool,
    ) -> None:
        with self._profile_add_result_lock:
            self._pending_profile_add_result = (
                bool(save_ok),
                str(save_error) if save_error is not None else None,
                bool(save_failed),
            )
        return

    def _reconcile_pending_profile_add_result(self) -> bool:
        with self._profile_add_result_lock:
            result = self._pending_profile_add_result
            self._pending_profile_add_result = None
        if result is None:
            return False
        return self._finish_profile_add_on_ui(*result)

    def _record_external_settings_result_without_ui(
        self,
        ok: bool,
        error: str | None,
        prepared: dict[str, Any] | None,
    ) -> None:
        """Hand a worker result to the next Tk-owned runtime refresh."""
        captured = prepared if isinstance(prepared, dict) else None
        with self._external_settings_result_lock:
            self._pending_external_settings_result = (
                bool(ok),
                str(error) if error is not None else None,
                captured,
            )
        return

    def _reconcile_external_settings_result(self) -> bool:
        with self._external_settings_result_lock:
            result = self._pending_external_settings_result
            self._pending_external_settings_result = None
        if result is None:
            return False
        ok, error, prepared = result
        retry_prepared = self._consume_blocked_mutation_settings()
        self._profile_deletions_inflight.discard("__external_settings__")
        self._preserve_status_after_next_autosave = False
        if bool(ok):
            self._remount()
            if retry_prepared is not None:
                self._schedule_captured_autosave_retry(retry_prepared)
            self._notify_external_settings_reconciled()
            return True
        self._set_status(f"설정 변경 실패: {error}", level="error")
        retry_prepared = retry_prepared or prepared
        if retry_prepared is not None:
            self._schedule_captured_autosave_retry(retry_prepared)
        self._notify_external_settings_reconciled()
        return False

    def _notify_external_settings_reconciled(self) -> None:
        callback = self._on_external_settings_reconciled
        if not callable(callback):
            return
        try:
            callback()
        except Exception:
            pass
        return

    def _schedule_captured_autosave_retry(self, prepared: dict[str, Any]) -> None:
        self._preserve_status_after_next_autosave = True
        self._cancel_pending_autosave()
        win = self._win
        after = getattr(win, "after", None)
        if callable(after):
            try:
                self._autosave_after_id = after(
                    350,
                    lambda: self._retry_captured_autosave(prepared),
                )
                return
            except Exception:
                self._autosave_after_id = None
        self._retry_captured_autosave(prepared)
        return

    def _retry_captured_autosave(self, prepared: dict[str, Any]) -> None:
        self._autosave_after_id = None
        ok, error, _provider_changed = self._apply_settings_update(prepared)
        if not ok and error == "profile_refresh_busy":
            self._schedule_captured_autosave_retry(prepared)
        return

    def _resume_pending_autosave_after_external_failure(self) -> None:
        self._preserve_status_after_next_autosave = True
        self._schedule_autosave()
        return

    def _remount(self) -> None:
        parent = self._parent
        if parent is None:
            return
        try:
            self.mount(parent)
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

    def _safe_get_runtime(self) -> dict[str, Any]:
        try:
            payload = self._codex.get_runtime_status()
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _snapshot_payload_from_any(self, snapshot: Any) -> dict[str, Any]:
        if isinstance(snapshot, dict):
            return dict(snapshot)
        try:
            if snapshot is not None and hasattr(snapshot, "to_dict"):
                payload = snapshot.to_dict()
                if isinstance(payload, dict):
                    return dict(payload)
        except Exception:
            return {}
        return {}

    def _format_captured_at_value(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or raw == "-":
            return "-"
        try:
            formatter = getattr(self._codex, "format_captured_at_for_display", None)
            if callable(formatter):
                rendered = str(formatter(raw) or "").strip()
                return rendered if rendered else "-"
        except Exception:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        return raw

    def _runtime_state_text(self, runtime: dict[str, Any] | None) -> str:
        if not isinstance(runtime, dict):
            runtime = {}
        session_state = str(runtime.get("session_state", "logged_out") or "logged_out")
        monitor_state = str(runtime.get("monitor_state", "idle") or "idle")
        logout_in_progress = bool(runtime.get("logout_in_progress", False))
        profile_in_use = bool(runtime.get("profile_in_use", False))
        pending_login_poll = bool(runtime.get("pending_login_poll_active", False))
        auth_attention_required = bool(runtime.get("auth_attention_required", False))
        auth_attention_reason = str(runtime.get("auth_attention_reason", "") or "")
        pending_login_reason = str(runtime.get("pending_login_poll_reason", "") or "")
        browser_state = str(runtime.get("browser_state", "stopped") or "stopped")
        browser_last_error = str(runtime.get("browser_last_error", "") or "")
        retry_attempt = max(0, int(runtime.get("browser_retry_attempt", 0) or 0))
        retry_max = max(0, int(runtime.get("browser_retry_max", 0) or 0))
        login_window_open = bool(runtime.get("login_window_open", False))
        try:
            inflight = bool(runtime.get("collect_inflight", False))
        except Exception:
            inflight = False
        source = str(runtime.get("collect_source", "") or "")
        provider_state = str(runtime.get("provider_state") or runtime.get("state") or "")
        if logout_in_progress or monitor_state == "cancelling":
            return "연결 해제 중"
        if provider_state in {"rate_limited", "rate_limit"} or monitor_state == "rate_limited":
            try:
                retry_after = int(float(runtime.get("next_collect_in_sec")))
            except (TypeError, ValueError):
                retry_after = 0
            if retry_after > 0:
                return f"요청 제한 · {retry_after}초 후 재시도"
            return "요청 제한 · 재시도 대기"
        if provider_state in {"schema_incompatible", "dom_drift"}:
            return "페이지 형식 변경 · 조회 불가"
        if provider_state in {"stale", "cache_stale"}:
            return "이전 값 · 갱신 대기"
        if browser_last_error == "command_timeout":
            if browser_state == "recovering" or inflight:
                progress = f" ({retry_attempt}/{retry_max})" if retry_max > 0 else ""
                return f"조회 시간 초과 · 연결 복구 중{progress}"
            return "조회 시간 초과 · 자동 재시도 종료"
        if inflight:
            if source == "manual_login":
                return "연결 창 여는 중"
            if source == "manual_query":
                return "수동 조회 중"
            if source in {"auto_monitor", "monitor_tick"}:
                return "자동 조회 중"
            return "조회 중"
        if profile_in_use or monitor_state == "paused_profile_in_use" or browser_state == "profile_in_use":
            return "프로필 사용 중 (자동 일시중지)"
        if pending_login_poll or login_window_open:
            is_cloudflare_auth = (
                auth_attention_reason == "cloudflare_challenge"
                or pending_login_reason == "cloudflare_challenge"
            )
            return "인증 완료 대기 중" if is_cloudflare_auth else "연결 완료 대기 중"
        if browser_state == "starting":
            return "브라우저 시작 중"
        if browser_state == "recovering":
            return "브라우저 복구 중"
        if browser_state == "failed" and browser_last_error == "browser_channel_unavailable":
            return "Google Chrome 필요"
        if auth_attention_required or monitor_state == "paused_auth_required":
            return "브라우저 인증 필요"
        if session_state == "logged_out":
            return "연결 필요"
        return "대기 중"

    def _captured_at_is_stale(self, value: Any, stale_after_sec: float) -> bool:
        raw = str(value or "").strip()
        if not raw or raw == "-":
            return False
        try:
            threshold = float(stale_after_sec)
        except Exception:
            threshold = 300.0
        if threshold < 60.0:
            threshold = 60.0
        try:
            normalized = raw.replace("Z", "+00:00")
            captured_at = datetime.fromisoformat(normalized)
        except Exception:
            return False
        try:
            if captured_at.tzinfo is not None:
                now = datetime.now(captured_at.tzinfo)
            else:
                now = datetime.now()
            age_sec = (now - captured_at).total_seconds()
        except Exception:
            return False
        return age_sec > threshold

    def _account_snapshot_stale_after_sec(self, entry: dict[str, Any]) -> float:
        settings = entry.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        runtime = entry.get("runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        raw_interval = settings.get("interval_sec", runtime.get("interval_sec", 90.0))
        try:
            interval = float(raw_interval)
        except Exception:
            interval = 90.0
        if interval <= 0.0:
            interval = 90.0
        return max(300.0, interval * 3.0)

    def _runtime_snapshot_is_previous(
        self,
        runtime: dict[str, Any] | None,
        *,
        captured_at: Any = "",
        stale_after_sec: float = 300.0,
    ) -> bool:
        if not isinstance(runtime, dict):
            runtime = {}
        monitor_state = str(runtime.get("monitor_state") or "idle")
        session_state = str(runtime.get("session_state") or "")
        failure_count = int(runtime.get("failure_count") or 0)
        browser_last_error = str(runtime.get("browser_last_error") or "").strip()
        if self._captured_at_is_stale(captured_at, stale_after_sec):
            return True
        if failure_count > 0 or browser_last_error:
            return True
        if bool(runtime.get("collect_inflight", False)):
            return True
        if bool(runtime.get("auth_attention_required", False)):
            return True
        if monitor_state in {
            "running",
            "cancelling",
            "paused_auth_required",
            "paused_profile_in_use",
        }:
            return True
        return session_state == "logged_out"

    def _format_account_snapshot_summary(self, entry: dict[str, Any]) -> str:
        runtime = entry.get("runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        payload = self._snapshot_payload_from_any(entry.get("last_snapshot"))

        def _val(key: str) -> str:
            raw = str(payload.get(key, "") or "").strip()
            return raw if raw else "-"

        parts: list[str] = []
        five_hour = _val("five_hour_limit")
        weekly = _val("weekly_limit")
        if five_hour != "-" or weekly != "-":
            parts.append(f"5시간 {five_hour} / 주간 {weekly}")
        monthly = _val("monthly_limit")
        if monthly != "-":
            parts.append(f"월간 {monthly}")
        spark_five_hour = _val("gpt_5_3_codex_spark_five_hour_limit")
        spark_weekly = _val("gpt_5_3_codex_spark_weekly_limit")
        if spark_five_hour != "-" or spark_weekly != "-":
            parts.append(f"Spark 5시간 {spark_five_hour} / 주간 {spark_weekly}")
        captured_at_raw = _val("captured_at")
        captured_at = self._format_captured_at_value(captured_at_raw)
        if captured_at != "-":
            parts.append(f"확인 {captured_at}")
        prefix = (
            "이전 값"
            if self._runtime_snapshot_is_previous(
                runtime,
                captured_at=captured_at_raw,
                stale_after_sec=self._account_snapshot_stale_after_sec(entry),
            )
            else "최근 값"
        )
        body = ", ".join(parts) if parts else "-"
        return f"{prefix}: {body}"

    def _account_snapshot_state_text(self, entry: dict[str, Any]) -> str:
        runtime = entry.get("runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        payload = self._snapshot_payload_from_any(entry.get("last_snapshot"))
        captured_at_raw = str(payload.get("captured_at", "") or "").strip()
        prefix = (
            "이전 값"
            if self._runtime_snapshot_is_previous(
                runtime,
                captured_at=captured_at_raw,
                stale_after_sec=self._account_snapshot_stale_after_sec(entry),
            )
            else "최근 값"
        )
        return f"값 상태: {prefix}"

    def _account_display_label(self, account_id: str) -> str:
        normalized = str(account_id or "").strip()
        return str(self._account_labels.get(normalized) or normalized or "프로필")

    def _localize_usage_metric_value(self, value: str) -> str:
        localized = str(value or "").strip()
        localized = re.sub(r"\bEnabled\b", "활성화", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bDisabled\b", "비활성화", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bused\b", "사용", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\b(?:left|remaining)\b", "남음", localized, flags=re.IGNORECASE)
        return localized

    def _format_account_metric_value(self, key: str, payload: dict[str, Any]) -> str:
        raw = str(payload.get(key, "") or "").strip()
        if not raw:
            return "-"
        if key == "captured_at":
            return self._format_captured_at_value(raw)
        if key.endswith("_reset_at"):
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
        localized = self._localize_usage_metric_value(raw)
        return re.sub(r" (?=(?:남음|사용)$)", "\u00a0", localized)

    def _refresh_account_runtime_summaries(self, runtime: dict[str, Any]) -> None:
        accounts = runtime.get("profiles") if isinstance(runtime, dict) else None
        if not isinstance(accounts, list) and isinstance(runtime, dict):
            accounts = runtime.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            account_id = str(raw.get("id") or "").strip()
            provider = str(raw.get("provider") or "").strip().lower()
            rendered_provider = self._account_rendered_providers.get(account_id)
            if rendered_provider and provider and rendered_provider != provider:
                self._remount()
                return
        seen: set[str] = set()
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            account_id = str(raw.get("id") or "").strip()
            if not account_id:
                continue
            seen.add(account_id)
            label = str(raw.get("label") or "").strip()
            if label:
                self._account_labels[account_id] = label
                label_var = self._account_label_vars.get(account_id)
                self._set_var_if_changed(label_var, label)
            child_runtime = raw.get("runtime", {})
            if not isinstance(child_runtime, dict):
                child_runtime = {}
            if bool(raw.get("enabled", True)):
                state = self._runtime_state_text(child_runtime)
            else:
                state = "비활성"
            status_var = self._account_status_vars.get(account_id)
            self._set_var_if_changed(status_var, f"조회 상태: {state}")
            snapshot_var = self._account_snapshot_vars.get(account_id)
            self._set_var_if_changed(snapshot_var, self._account_snapshot_state_text(raw))
            metric_vars = self._account_metric_vars.get(account_id)
            if isinstance(metric_vars, dict):
                payload = self._snapshot_payload_from_any(raw.get("last_snapshot"))
                descriptors = raw.get("metrics")
                descriptor_keys: set[str] = set()
                if isinstance(descriptors, list):
                    for descriptor in descriptors:
                        if not isinstance(descriptor, dict):
                            continue
                        key = str(descriptor.get("key") or "")
                        if not key:
                            continue
                        descriptor_keys.add(key)
                        payload.setdefault(key, descriptor.get("value_text", ""))
                        if key == "included_usage":
                            payload.setdefault("billing_reset_at", descriptor.get("reset_at", ""))
                self._update_account_metric_visibility(
                    account_id,
                    provider=str(raw.get("provider") or "codex"),
                    descriptor_keys=descriptor_keys,
                    payload=payload,
                )
                for key, value_var in metric_vars.items():
                    self._set_var_if_changed(
                        value_var,
                        self._format_account_metric_value(key, payload),
                    )
        for account_id, status_var in self._account_status_vars.items():
            if account_id in seen:
                continue
            self._set_var_if_changed(status_var, "조회 상태: -")
        for account_id, snapshot_var in self._account_snapshot_vars.items():
            if account_id in seen:
                continue
            self._set_var_if_changed(snapshot_var, "값 상태: -")
        for account_id, metric_vars in self._account_metric_vars.items():
            if account_id in seen or not isinstance(metric_vars, dict):
                continue
            for value_var in metric_vars.values():
                self._set_var_if_changed(value_var, "-")
        return

    @staticmethod
    def _metric_cell_widgets(cell: Any) -> tuple[Any, ...]:
        if isinstance(cell, (list, tuple)):
            return tuple(item for item in cell if item is not None)
        if cell is None:
            return ()
        return (cell,)

    def _update_account_metric_visibility(
        self,
        account_id: str,
        *,
        provider: str,
        descriptor_keys: set[str],
        payload: dict[str, Any],
    ) -> None:
        cells = self._account_metric_cells.get(str(account_id or ""), {})
        if not isinstance(cells, dict):
            return
        visibility: dict[str, bool] = {}
        if str(provider or "").lower() == "cursor":
            on_demand_visible = (
                "on_demand" in descriptor_keys
                or payload.get("on_demand_enabled") is not False
                and bool(str(payload.get("on_demand_status") or "").strip())
            )
            visibility["on_demand_status"] = bool(on_demand_visible)
        elif str(provider or "").lower() == "claude":
            five_hour_visible = (
                "five_hour_limit" in descriptor_keys
                or bool(str(payload.get("five_hour_limit") or "").strip())
            )
            visibility["five_hour_limit"] = bool(five_hour_visible)
            visibility["five_hour_limit_reset_at"] = bool(five_hour_visible)
            weekly_visible = (
                "weekly_limit" in descriptor_keys
                or bool(str(payload.get("weekly_limit") or "").strip())
            )
            visibility["weekly_limit"] = bool(weekly_visible)
            visibility["weekly_limit_reset_at"] = bool(weekly_visible)
            scoped_weekly_visible = bool(
                str(payload.get("weekly_scoped_limit") or "").strip()
            )
            visibility["weekly_scoped_limit"] = bool(scoped_weekly_visible)
            visibility["weekly_scoped_limit_reset_at"] = bool(scoped_weekly_visible)
            visibility["on_demand_status"] = bool(
                payload.get("on_demand_enabled") is not None
                and str(payload.get("on_demand_status") or "").strip()
            )
        else:
            five_hour_visible = (
                "five_hour_limit" in descriptor_keys
                or bool(str(payload.get("five_hour_limit") or "").strip())
            )
            visibility["five_hour_limit"] = bool(five_hour_visible)
            visibility["five_hour_limit_reset_at"] = bool(five_hour_visible)
            spark_five_hour_visible = (
                "gpt_5_3_codex_spark_five_hour_limit" in descriptor_keys
                or bool(
                    str(
                        payload.get("gpt_5_3_codex_spark_five_hour_limit") or ""
                    ).strip()
                )
            )
            visibility["gpt_5_3_codex_spark_five_hour_limit"] = bool(
                spark_five_hour_visible
            )
            visibility["gpt_5_3_codex_spark_five_hour_limit_reset_at"] = bool(
                spark_five_hour_visible
            )
            spark_weekly_visible = (
                "gpt_5_3_codex_spark_weekly_limit" in descriptor_keys
                or bool(
                    str(payload.get("gpt_5_3_codex_spark_weekly_limit") or "").strip()
                )
            )
            visibility["gpt_5_3_codex_spark_weekly_limit"] = bool(spark_weekly_visible)
            visibility["gpt_5_3_codex_spark_weekly_limit_reset_at"] = bool(
                spark_weekly_visible
            )
        state_key = str(account_id or "")
        previous = self._account_metric_visibility.setdefault(state_key, {})
        for key, visible in visibility.items():
            cell = cells.get(key)
            if cell is None:
                continue
            if previous.get(key) is visible:
                continue
            previous[key] = visible
            for widget in self._metric_cell_widgets(cell):
                try:
                    if visible:
                        widget.grid()
                    else:
                        widget.grid_remove()
                except Exception:
                    pass
        return

    def _apply_live_spark_visibility(self, payload: dict[str, Any]) -> None:
        # Spark quota rows are provider-optional: hide them entirely for
        # accounts whose usage page never reports Spark limits instead of
        # showing permanent "-" placeholders.
        visible = bool(
            str(payload.get("gpt_5_3_codex_spark_five_hour_limit") or "").strip()
            or str(payload.get("gpt_5_3_codex_spark_weekly_limit") or "").strip()
        )
        if self._live_spark_visible is visible:
            return
        self._live_spark_visible = visible
        for widget in self._metric_cell_widgets(self._live_spark_cells):
            try:
                if visible:
                    widget.grid()
                else:
                    widget.grid_remove()
            except Exception:
                pass
        return

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
        self._stop_runtime_refresh()
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
        for reconcile in (
            self._reconcile_pending_profile_release_result,
            self._reconcile_pending_profile_add_result,
            self._reconcile_pending_profile_delete_result,
            self._reconcile_external_settings_result,
        ):
            if reconcile():
                self._schedule_runtime_refresh(1000)
                return
        if self._drag_state is not None:
            self._schedule_runtime_refresh(300)
            return
        runtime = self._safe_get_runtime()
        session_state = str(runtime.get("session_state", "logged_out") or "logged_out")
        profile_in_use = bool(runtime.get("profile_in_use", False))
        pending_login_poll = bool(runtime.get("pending_login_poll_active", False))
        try:
            inflight = bool(runtime.get("collect_inflight", False))
        except Exception:
            inflight = False
        state = self._runtime_state_text(runtime)

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
        payload = self._snapshot_payload_from_any(snapshot)

        def _val(key: str) -> str:
            raw = str(payload.get(key, "") or "").strip()
            return raw if raw else "-"

        def _fmt_time(value: str) -> str:
            return self._format_captured_at_value(value)

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
            self._set_var_if_changed(self._collect_state_var, state)
            self._set_var_if_changed(self._next_collect_var, remain_text)
            self._set_var_if_changed(self._live_time_var, _fmt_time(_val("captured_at")))
            self._set_var_if_changed(self._live_five_hour_var, _val("five_hour_limit"))
            if self._live_five_hour_reset_var is not None:
                self._set_var_if_changed(
                    self._live_five_hour_reset_var,
                    _fmt_reset("five_hour_limit_reset_at"),
                )
            self._set_var_if_changed(self._live_weekly_var, _val("weekly_limit"))
            if self._live_weekly_reset_var is not None:
                self._set_var_if_changed(
                    self._live_weekly_reset_var,
                    _fmt_reset("weekly_limit_reset_at"),
                )
            self._set_var_if_changed(self._live_monthly_var, _val("monthly_limit"))
            if self._live_monthly_reset_var is not None:
                self._set_var_if_changed(
                    self._live_monthly_reset_var,
                    _fmt_reset("monthly_limit_reset_at"),
                )
            self._set_var_if_changed(
                self._live_spark_five_hour_var,
                _val("gpt_5_3_codex_spark_five_hour_limit"),
            )
            if self._live_spark_five_hour_reset_var is not None:
                self._set_var_if_changed(
                    self._live_spark_five_hour_reset_var,
                    _fmt_reset("gpt_5_3_codex_spark_five_hour_limit_reset_at"),
                )
            self._set_var_if_changed(
                self._live_spark_weekly_var,
                _val("gpt_5_3_codex_spark_weekly_limit"),
            )
            if self._live_spark_weekly_reset_var is not None:
                self._set_var_if_changed(
                    self._live_spark_weekly_reset_var,
                    _fmt_reset("gpt_5_3_codex_spark_weekly_limit_reset_at"),
                )
            self._set_var_if_changed(self._live_credit_var, _val("remaining_credit"))
            self._apply_live_spark_visibility(payload)
        except Exception:
            pass

        self._refresh_account_runtime_summaries(runtime=runtime)
        self._refresh_action_buttons(runtime=runtime)
        self._schedule_runtime_refresh(1000)
        return

    def _refresh_action_buttons(self, runtime: dict[str, Any]) -> None:
        login_button = self._login_button
        logout_button = self._logout_button
        actions_blocked = bool(
            self._profile_deletions_inflight or self._profile_actions_inflight
        )
        try:
            can_login = bool(runtime.get("can_login", False)) and not actions_blocked
        except Exception:
            can_login = False
        try:
            can_logout = bool(runtime.get("can_logout", False)) and not actions_blocked
        except Exception:
            can_logout = False
        self._set_button_enabled(login_button, can_login)
        self._set_button_enabled(logout_button, can_logout)
        runtime_entries = self._runtime_profile_map(runtime)
        for account_id, button in self._account_query_buttons.items():
            entry = runtime_entries.get(str(account_id or ""))
            account_can_query = self._account_query_permission(entry) and not actions_blocked
            self._set_button_enabled(button, account_can_query)
        for account_id, button in self._account_login_buttons.items():
            entry = runtime_entries.get(str(account_id or ""))
            account_can_login, _account_can_logout = self._account_action_permissions(entry)
            self._set_button_enabled(button, account_can_login and not actions_blocked)
        for account_id, button in self._account_logout_buttons.items():
            entry = runtime_entries.get(str(account_id or ""))
            _account_can_login, account_can_logout = self._account_action_permissions(entry)
            self._set_button_enabled(button, account_can_logout and not actions_blocked)
        return

    def _runtime_profile_map(self, runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
        accounts = runtime.get("profiles") if isinstance(runtime, dict) else None
        if not isinstance(accounts, list) and isinstance(runtime, dict):
            accounts = runtime.get("accounts")
        if not isinstance(accounts, list):
            return {}
        entries: dict[str, dict[str, Any]] = {}
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            account_id = str(raw.get("id") or "").strip()
            if account_id:
                entries[account_id] = raw
        return entries

    def _find_account_runtime_entry(
        self,
        runtime: dict[str, Any],
        account_id: str,
    ) -> dict[str, Any] | None:
        accounts = runtime.get("profiles") if isinstance(runtime, dict) else None
        if not isinstance(accounts, list) and isinstance(runtime, dict):
            accounts = runtime.get("accounts")
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
    ) -> tuple[bool, bool]:
        if not isinstance(entry, dict):
            # Missing per-account runtime should not dead-end manual login recovery;
            # the monitor still validates the actual action.
            return True, False
        if not bool(entry.get("enabled", True)):
            return False, False
        runtime = entry.get("runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        raw_session_state = runtime.get("session_state")
        session_state = str(raw_session_state or "")
        if session_state == "logged_out":
            return True, False
        if bool(runtime.get("collect_inflight", False)) or bool(
            runtime.get("logout_in_progress", False)
        ):
            return False, False
        monitor_state = str(runtime.get("monitor_state") or "idle")
        if monitor_state in {"running", "cancelling", "paused_profile_in_use"}:
            return False, False
        if bool(runtime.get("profile_in_use", False)):
            return False, False
        can_login_value = runtime.get("can_login")
        can_logout_value = runtime.get("can_logout")
        if session_state == "logged_in":
            can_login = bool(can_login_value) if can_login_value is not None else False
            return can_login, True
        if raw_session_state is None and (
            can_login_value is not None or can_logout_value is not None
        ):
            return bool(can_login_value), bool(can_logout_value)
        if raw_session_state is None:
            return True, False
        can_login = (
            bool(can_login_value)
            if can_login_value is not None
            else session_state in {"logged_out", "unknown"}
        )
        can_logout = (
            bool(can_logout_value)
            if can_logout_value is not None
            else session_state == "logged_in"
        )
        return can_login, can_logout

    def _account_query_permission(self, entry: dict[str, Any] | None) -> bool:
        if not isinstance(entry, dict):
            return True
        if not bool(entry.get("enabled", True)):
            return False
        runtime = entry.get("runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        if bool(runtime.get("collect_inflight", False)) or bool(
            runtime.get("logout_in_progress", False)
        ):
            return False
        monitor_state = str(runtime.get("monitor_state") or "idle")
        if monitor_state in {"running", "cancelling", "paused_profile_in_use"}:
            return False
        if bool(runtime.get("profile_in_use", False)):
            return False
        session_state = str(runtime.get("session_state") or "")
        return bool(
            session_state in {"logged_in", "logged_out", "unknown", ""}
            or bool(runtime.get("auth_attention_required", False))
            or monitor_state == "paused_auth_required"
        )

    def _set_button_enabled(self, button: Any, enabled: bool) -> None:
        if button is None:
            return
        normalized = bool(enabled)
        identity = id(button)
        if self._button_enabled_states.get(identity) is normalized:
            return
        self._button_enabled_states[identity] = normalized
        try:
            if normalized:
                button.state(["!disabled"])
            else:
                button.state(["disabled"])
            return
        except Exception:
            pass
        try:
            button.configure(state="normal" if normalized else "disabled")
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
