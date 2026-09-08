"""Reusable nonmodal Tk panel for Wrike worktime progress."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
import math
import re
import time
from typing import Any, Callable

from src.apps.wrike_timelog_details import TimelogDayDetails


_REFRESH_INTERVAL_MS = 1_000
_COUNTDOWN_INTERVAL_MS = 200
_DEFAULT_IDLE_TIMEOUT_MS = 6_000
_MIN_IDLE_TIMEOUT_MS = 1_200
_POINTER_OFFSET_PX = 16
_DATE_KEY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_TARGET_HHMM_PATTERN = re.compile(r"(?:[01]\d|2[0-4]):[0-5]\d")
_INLINE_EDITOR_TARGET = "target_minutes"
_INLINE_EDITOR_CLOCK_IN = "clock_in"
_INLINE_EDITOR_PROMPT = "prompt_clock_in"
_INLINE_EDITOR_MANUAL_BREAK = "manual_break"

_BG = "#F3F4F6"
_CARD_BG = "#FFFFFF"
_BORDER = "#E5E7EB"
_HOVER_BORDER = "#2563EB"
_TEXT = "#111827"
_MUTED = "#6B7280"
_TODAY_BG = "#DBEAFE"
_SELECTED_BG = "#BFDBFE"
_PROMPT_BG = "#FFF7ED"
_PROMPT_BORDER = "#FDBA74"
_SYNC_COLORS = {
    "ok": "#059669",
    "synced": "#059669",
    "fresh": "#059669",
    "syncing": "#2563EB",
    "loading": "#2563EB",
    "stale": "#D97706",
    "warning": "#D97706",
    "error": "#DC2626",
}


@dataclass(frozen=True, slots=True)
class WorktimePanelLine:
    """One colored detail line in the today's-work section."""

    text: str
    color: str

    def __post_init__(self) -> None:
        _require_string(self.text, name="text")
        _require_color(self.color)


@dataclass(frozen=True, slots=True)
class WorktimePanelDayRow:
    """One immutable Monday-through-Sunday summary row."""

    weekday: str
    date: str
    date_key: str
    target_minutes: int
    summary: str
    today: bool
    color: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.weekday, name="weekday")
        _require_nonempty_string(self.date, name="date")
        _require_iso_date_key(self.date_key)
        if type(self.target_minutes) is not int:
            raise TypeError("target_minutes must be an int")
        if not 0 <= self.target_minutes <= 1440:
            raise ValueError("target_minutes must be between 0 and 1440")
        _require_string(self.summary, name="summary")
        if type(self.today) is not bool:
            raise TypeError("today must be a bool")
        _require_color(self.color)


@dataclass(frozen=True, slots=True)
class WorktimeActivityPrompt:
    """Optional prompt created from activity detected at a local HH:MM time."""

    detected_time: str

    def __post_init__(self) -> None:
        if type(self.detected_time) is not str or _HHMM_PATTERN.fullmatch(
            self.detected_time
        ) is None:
            raise ValueError("detected_time must use 24-hour HH:MM format")

    @property
    def detected_hhmm(self) -> str:
        """Return the detected time under an explicit display-format name."""

        return self.detected_time


@dataclass(frozen=True, slots=True)
class WorktimePanelManualBreak:
    """One selected-week break row; only raw manual rows are editable."""

    date_key: str
    label: str
    start_time: str
    end_time: str
    editable: bool
    index: int | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        _require_iso_date_key(self.date_key)
        _require_nonempty_string(self.label, name="label")
        if _HHMM_PATTERN.fullmatch(self.start_time) is None:
            raise ValueError("start_time must use HH:MM format")
        if _TARGET_HHMM_PATTERN.fullmatch(self.end_time) is None:
            raise ValueError("end_time must use HH:MM or 24:00 format")
        if type(self.editable) is not bool:
            raise TypeError("editable must be a bool")
        if self.editable:
            if type(self.index) is not int or self.index < 0 or not self.fingerprint:
                raise ValueError("editable manual break requires index and fingerprint")
        elif self.index is not None or self.fingerprint:
            raise ValueError("readonly break must not expose edit identity")


@dataclass(frozen=True, slots=True)
class WorktimePanelModel:
    """Deeply immutable snapshot rendered by :class:`WorktimeQuickPanel`.

    ``rows`` is always a Monday-through-Sunday tuple with exactly seven items.
    The provider owns the localized weekday/date text and their ordering.
    """

    week_range: str
    sync_text: str
    sync_state: str
    today_lines: tuple[WorktimePanelLine, ...]
    target_minutes: int
    clock_in_time: str | None
    break_active: bool
    rows: tuple[WorktimePanelDayRow, ...]
    prompt: WorktimeActivityPrompt | None = None
    day_details: tuple[TimelogDayDetails, ...] = ()
    manual_breaks: tuple[WorktimePanelManualBreak, ...] = ()

    def __post_init__(self) -> None:
        _require_string(self.week_range, name="week_range")
        _require_string(self.sync_text, name="sync_text")
        _require_nonempty_string(self.sync_state, name="sync_state")
        if not isinstance(self.today_lines, tuple):
            raise TypeError("today_lines must be an immutable tuple")
        if any(not isinstance(item, WorktimePanelLine) for item in self.today_lines):
            raise TypeError("today_lines must contain only WorktimePanelLine values")
        if type(self.target_minutes) is not int:
            raise TypeError("target_minutes must be an int")
        if not 0 <= self.target_minutes <= 1440:
            raise ValueError("target_minutes must be between 0 and 1440")
        if self.clock_in_time is not None and (
            type(self.clock_in_time) is not str
            or _HHMM_PATTERN.fullmatch(self.clock_in_time) is None
        ):
            raise ValueError("clock_in_time must be None or use 24-hour HH:MM format")
        if type(self.break_active) is not bool:
            raise TypeError("break_active must be a bool")
        if not isinstance(self.rows, tuple):
            raise TypeError("rows must be an immutable tuple")
        if len(self.rows) != 7:
            raise ValueError("rows must contain exactly seven Monday-through-Sunday entries")
        if any(not isinstance(item, WorktimePanelDayRow) for item in self.rows):
            raise TypeError("rows must contain only WorktimePanelDayRow values")
        if len({item.date_key for item in self.rows}) != len(self.rows):
            raise ValueError("rows must contain unique date_key values")
        if self.prompt is not None and not isinstance(
            self.prompt, WorktimeActivityPrompt
        ):
            raise TypeError("prompt must be a WorktimeActivityPrompt or None")
        if not isinstance(self.day_details, tuple):
            raise TypeError("day_details must be an immutable tuple")
        if self.day_details and (
            len(self.day_details) != len(self.rows)
            or any(not isinstance(item, TimelogDayDetails) for item in self.day_details)
            or {item.date_key for item in self.day_details}
            != {item.date_key for item in self.rows}
        ):
            raise ValueError("day_details must cover exactly the displayed rows")
        if not isinstance(self.manual_breaks, tuple) or any(
            not isinstance(item, WorktimePanelManualBreak)
            for item in self.manual_breaks
        ):
            raise TypeError("manual_breaks must contain WorktimePanelManualBreak values")
        if any(item.date_key not in {row.date_key for row in self.rows} for item in self.manual_breaks):
            raise ValueError("manual_breaks must belong to displayed rows")

    @property
    def has_clock_in(self) -> bool:
        """Return whether today's editable clock-in value exists."""

        return self.clock_in_time is not None


@dataclass(frozen=True, slots=True)
class _DetailScrollAnchor:
    date_key: str
    text_index: str | None
    fraction: float


class WorktimeQuickPanel:
    """Singleton-style nonmodal panel backed by one reusable ``Toplevel``.

    Prompt accept callbacks receive the detected ``HH:MM`` string. Clock-in
    and selected-date target save callbacks return whether persistence succeeded;
    prompt edit saves receive both the detected context and edited value. Callback
    completion is followed by an immediate provider read so synchronous state
    changes become visible without waiting for the next one-second refresh.
    """

    def __init__(
        self,
        root: Any,
        model_provider: Callable[[], WorktimePanelModel],
        refresh: Callable[[], None],
        clock_in_now: Callable[[], None],
        edit_clock_in: Callable[[str], bool],
        edit_plan: Callable[[str, int], bool],
        toggle_break: Callable[[], None],
        open_settings: Callable[[], None],
        prompt_accept: Callable[[str], None],
        prompt_edit: Callable[[str, str], bool],
        prompt_snooze: Callable[[], None],
        prompt_skip: Callable[[], None],
        edit_manual_break: Callable[[WorktimePanelManualBreak, str, str], tuple[bool, str | None]] | None = None,
        tk_module: Any | None = None,
        idle_timeout_ms: int = _DEFAULT_IDLE_TIMEOUT_MS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        callbacks = {
            "model_provider": model_provider,
            "refresh": refresh,
            "clock_in_now": clock_in_now,
            "edit_clock_in": edit_clock_in,
            "edit_plan": edit_plan,
            "toggle_break": toggle_break,
            "open_settings": open_settings,
            "prompt_accept": prompt_accept,
            "prompt_edit": prompt_edit,
            "prompt_snooze": prompt_snooze,
            "prompt_skip": prompt_skip,
            "monotonic": monotonic,
        }
        for name, callback in callbacks.items():
            if not callable(callback):
                raise TypeError(f"{name} must be callable")

        self._root = root
        self._model_provider = model_provider
        self._on_refresh = refresh
        self._on_clock_in_now = clock_in_now
        self._on_edit_clock_in = edit_clock_in
        self._on_edit_plan = edit_plan
        self._on_toggle_break = toggle_break
        self._on_open_settings = open_settings
        self._on_prompt_accept = prompt_accept
        self._on_prompt_edit = prompt_edit
        self._on_prompt_snooze = prompt_snooze
        self._on_prompt_skip = prompt_skip
        self._on_edit_manual_break = edit_manual_break
        self._monotonic = monotonic
        self._tk = tk_module

        self._window = None
        self._shell = None
        self._content = None
        self._model: WorktimePanelModel | None = None
        self._selected_date_key: str | None = None
        self._rendered_detail_date_key: str | None = None
        self._pending_detail_scroll: _DetailScrollAnchor | None = None
        self._structure_signature: tuple[Any, ...] | None = None
        self._widgets: dict[str, Any] = {}
        self._refresh_after_id = None
        self._refresh_token: object | None = None
        self._dismiss_after_id = None
        self._dismiss_token: object | None = None
        self._countdown_after_id = None
        self._countdown_token: object | None = None
        self._dismiss_deadline: float | None = None
        self._dismiss_expired_while_hovered = False
        self._interaction_depth = 0
        self._inline_editor_active = False
        self._inline_editor_kind: str | None = None
        self._inline_editor_context: str | None = None
        self._manual_break_edit: WorktimePanelManualBreak | None = None
        self._detail_view = "timelog"
        self._idle_timeout_ms = self._clamp_idle_timeout_ms(idle_timeout_ms)
        self._pointer_inside = False
        self._geometry_retry_pending = False
        self._visible = False
        self._placed = False
        self._destroyed = False

    def show(self, activate: bool = False) -> bool:
        """Show, re-anchor at the pointer, and verify native mapped state."""

        if type(activate) is not bool:
            raise TypeError("activate must be a bool")
        if self._destroyed:
            return False
        window = self._ensure_window()
        if window is None:
            return False

        was_visible = self._visible
        self.refresh_now()
        if not (not self._placed and self._geometry_retry_pending):
            self._geometry_retry_pending = not self._reconcile_geometry(
                anchor_to_pointer=True
            )
        if activate:
            if not _show_window_activated(window):
                return self._fail_show(window)
        elif not _show_window_without_activation(window):
            return self._fail_show(window)
        _safe_call(window, "update_idletasks")
        if not self._geometry_retry_pending:
            self._geometry_retry_pending = not self._reconcile_geometry(
                anchor_to_pointer=True
            )
        _safe_call(window, "update_idletasks")
        if not self._geometry_retry_pending:
            self._restore_pending_detail_scroll()
        if not self._window_is_mapped(window):
            return self._fail_show(window)

        self._visible = True
        if not was_visible:
            self._set_pointer_inside(self._pointer_is_within_window())
        self._schedule_refresh()
        self._reset_dismiss_timer()
        return True

    def _fail_show(self, window: Any) -> bool:
        self._visible = False
        self._set_pointer_inside(False)
        self._cancel_timers()
        if self._window_exists(window):
            _safe_call(window, "withdraw")
        return False

    def hide(self) -> None:
        """Hide the panel without destroying its ``Toplevel``."""

        self._visible = False
        self._close_inline_editor(reconcile=False)
        self._set_pointer_inside(False)
        self._cancel_timers()
        window = self._window
        if window is not None and self._window_exists(window):
            _safe_call(window, "withdraw")

    def toggle(self, activate: bool = False) -> None:
        """Hide a foreground panel, or surface/reopen it for the hotkey."""

        if self.is_visible():
            if activate and not _window_is_foreground(self._window):
                self.show(activate=True)
            else:
                self.hide()
        else:
            self.show(activate=activate)

    def set_idle_timeout_ms(self, idle_timeout_ms: int) -> None:
        """Set the dismissal delay, clamped to the minimum safe duration."""

        self._idle_timeout_ms = self._clamp_idle_timeout_ms(idle_timeout_ms)
        if self._visible and not self._destroyed:
            self._reset_dismiss_timer()

    def refresh_now(self) -> bool:
        """Read and render the latest model immediately.

        Provider failures or invalid values leave the last good model visible.
        An exactly equal immutable snapshot returns before any Tk mutation.
        """

        if self._destroyed:
            return False
        try:
            model = self._model_provider()
        except Exception:
            return False
        if not isinstance(model, WorktimePanelModel):
            return False
        if model == self._model:
            return True

        self._reconcile_selection_for_model(model)
        self._reconcile_inline_editor_for_model(model)
        if self._content is None or not self._window_exists(self._window):
            # ``_model`` represents the successfully rendered snapshot. Keeping
            # it unset here ensures a pre-show refresh cannot make show blank.
            return True

        signature = self._model_structure_signature(model)
        rebuilt = signature != self._structure_signature or not self._widgets
        if rebuilt:
            self._render_structure(model)
            self._structure_signature = signature
        else:
            self._update_rendered_model(model)

        self._model = model
        if rebuilt:
            self._geometry_retry_pending = not self._reconcile_geometry()
            if not self._geometry_retry_pending:
                self._restore_pending_detail_scroll()
        return True

    def destroy(self) -> None:
        """Cancel pending work and permanently destroy the panel once."""

        if self._destroyed:
            return
        self._destroyed = True
        self._visible = False
        self._inline_editor_active = False
        self._inline_editor_kind = None
        self._inline_editor_context = None
        self._set_pointer_inside(False)
        self._cancel_timers()

        window = self._window
        self._window = None
        self._shell = None
        self._content = None
        self._model = None
        self._selected_date_key = None
        self._rendered_detail_date_key = None
        self._pending_detail_scroll = None
        self._structure_signature = None
        self._widgets = {}
        self._geometry_retry_pending = False
        if window is not None:
            _safe_call(window, "destroy")

    def is_visible(self) -> bool:
        """Return whether the panel is alive and mapped by the window manager."""

        return bool(
            self._visible
            and not self._destroyed
            and self._window is not None
            and self._window_exists(self._window)
            and self._window_is_mapped(self._window)
        )

    @staticmethod
    def _clamp_idle_timeout_ms(idle_timeout_ms: int) -> int:
        if type(idle_timeout_ms) is not int:
            raise TypeError("idle_timeout_ms must be an int")
        return max(_MIN_IDLE_TIMEOUT_MS, idle_timeout_ms)

    @staticmethod
    def _default_selected_date_key(model: WorktimePanelModel) -> str:
        for row in model.rows:
            if row.today:
                return row.date_key
        return model.rows[0].date_key

    @staticmethod
    def _row_for_date(
        model: WorktimePanelModel,
        date_key: str | None,
    ) -> WorktimePanelDayRow | None:
        if date_key is None:
            return None
        return next(
            (row for row in model.rows if row.date_key == date_key),
            None,
        )

    def _reconcile_selection_for_model(self, model: WorktimePanelModel) -> None:
        if self._row_for_date(model, self._selected_date_key) is None:
            self._selected_date_key = self._default_selected_date_key(model)

    def _selected_row(
        self,
        model: WorktimePanelModel | None = None,
    ) -> WorktimePanelDayRow | None:
        current = self._model if model is None else model
        if current is None:
            return None
        return self._row_for_date(current, self._selected_date_key)

    def _reconcile_inline_editor_for_model(self, model: WorktimePanelModel) -> None:
        if not self._inline_editor_active:
            return
        stale = False
        if self._inline_editor_kind == _INLINE_EDITOR_TARGET:
            stale = self._row_for_date(model, self._inline_editor_context) is None
        elif self._inline_editor_kind == _INLINE_EDITOR_CLOCK_IN:
            stale = model.clock_in_time is None
        elif self._inline_editor_kind == _INLINE_EDITOR_PROMPT:
            stale = (
                model.prompt is None
                or model.prompt.detected_time != self._inline_editor_context
            )
        elif self._inline_editor_kind == _INLINE_EDITOR_MANUAL_BREAK:
            # Keep same-day drafts with their original conflict precondition.
            # Moving the selection must not leave a hidden day's editor active.
            stale = (
                self._manual_break_edit is None
                or self._manual_break_edit.date_key != self._selected_date_key
                or self._row_for_date(model, self._manual_break_edit.date_key) is None
            )
        if stale:
            self._close_inline_editor(reconcile=self._visible)

    @staticmethod
    def _model_structure_signature(model: WorktimePanelModel) -> tuple[Any, ...]:
        return model.prompt is not None, len(model.today_lines)

    def _uses_compact_density(self) -> bool:
        """Fit all controls in the work area used for the first placement."""

        window = self._window
        if window is None:
            return False
        current = self._window_rect(window)
        pointer_x, pointer_y = _pointer_position(window, self._root)
        use_pointer = not self._placed or current is None
        if use_pointer:
            work_area = _work_area_for_point(pointer_x, pointer_y, window, self._root)
        else:
            work_area = _work_area_for_window(window, self._root)
        try:
            work_height = int(work_area[3]) - int(work_area[1])
        except (IndexError, TypeError, ValueError):
            return False
        return 480 <= work_height <= 720

    @staticmethod
    def _detail_for_selected_date(
        model: WorktimePanelModel,
        date_key: str | None,
    ) -> TimelogDayDetails | None:
        if not date_key:
            return None
        return next(
            (detail for detail in model.day_details if detail.date_key == date_key),
            None,
        )

    def _selected_detail_parts(
        self, model: WorktimePanelModel,
    ) -> tuple[tuple[str, str], ...]:
        detail = self._detail_for_selected_date(model, self._selected_date_key)
        if detail is None or detail.state == "loading":
            return (("상세 기록을 불러오는 중입니다.", "detail_comment"),)
        if detail.state == "unavailable":
            return (("상세 기록을 확인할 수 없습니다.", "detail_comment"),)
        if not detail.rows:
            return (("해당 날짜에 Wrike 기록이 없습니다. · 합계 0분", "detail_comment"),)
        parts = [(f"실제 기록 합계 {self._format_actual_minutes(detail.total_minutes)}", "detail_heading")]
        for row in detail.rows:
            parts.append((
                f"\n{row.ticket_text} · {self._format_actual_minutes(row.minutes)}",
                "detail_heading",
            ))
            parts.append((f"\n{row.comment.strip() or '코멘트 없음'}", "detail_comment"))
        return tuple(parts)

    def _selected_detail_text(self, model: WorktimePanelModel) -> str:
        return "".join(text for text, _tag in self._selected_detail_parts(model))

    def _selected_manual_breaks(
        self,
        model: WorktimePanelModel,
    ) -> tuple[WorktimePanelManualBreak, ...]:
        return tuple(
            row for row in model.manual_breaks
            if row.date_key == self._selected_date_key
        )

    def _selected_manual_break_text(self, model: WorktimePanelModel) -> str:
        rows = self._selected_manual_breaks(model)
        if not rows:
            return "표시할 수동 또는 캘린더 휴게가 없습니다."
        return "\n".join(
            f"{row.label} · {row.start_time}–{row.end_time}"
            + (" · 읽기 전용" if not row.editable else "")
            for row in rows
        )

    def _selected_detail_view_text(self, model: WorktimePanelModel) -> str:
        if self._detail_view == "breaks":
            return self._selected_manual_break_text(model)
        return self._selected_detail_text(model)

    def _update_detail_presentation(
        self,
        model: WorktimePanelModel,
        *,
        reset_scroll: bool = False,
    ) -> None:
        widgets = self._widgets
        breaks_view = self._detail_view == "breaks"
        _safe_call(
            widgets.get("detail_title"),
            "configure",
            text=(
                "선택 날짜 휴게 기록"
                if breaks_view
                else "선택 날짜 실제 기록 · 티켓 / 시간 / 코멘트"
            ),
        )
        _safe_call(
            widgets.get("detail_timelog_button"),
            "configure",
            relief="sunken" if not breaks_view else "solid",
        )
        _safe_call(
            widgets.get("detail_breaks_button"),
            "configure",
            relief="sunken" if breaks_view else "solid",
        )
        rows = self._selected_manual_breaks(model)
        menu = widgets.get("manual_break_menu")
        menu_button = widgets.get("manual_break_menu_button")
        if menu is not None:
            _safe_call(menu, "delete", 0, "end")
            for row in rows:
                if row.editable:
                    _safe_call(
                        menu,
                        "add_command",
                        label=f"{row.start_time}–{row.end_time} 수정",
                        command=lambda row=row: self._edit_manual_break_command(row),
                    )
        editable_rows = tuple(row for row in rows if row.editable)
        _safe_call(
            menu_button,
            "configure",
            state="normal" if editable_rows else "disabled",
        )
        self._set_detail_text(
            widgets.get("detail_text"),
            self._selected_detail_view_text(model),
            date_key=self._selected_date_key,
            reset_scroll=reset_scroll,
            tagged_parts=None if breaks_view else self._selected_detail_parts(model),
        )

    def _set_detail_view(self, view: str) -> None:
        if view not in {"timelog", "breaks"}:
            return
        self._detail_view = view
        if self._model is not None:
            self._update_detail_presentation(self._model, reset_scroll=True)

    def _ensure_tk(self) -> Any | None:
        if self._tk is not None:
            return self._tk
        try:
            import tkinter as tk
        except Exception:
            return None
        self._tk = tk
        return tk

    def _ensure_window(self) -> Any | None:
        if self._window is not None:
            if self._window_exists(self._window):
                return self._window
            self._window = None
            self._shell = None
            self._content = None
            self._model = None
            self._selected_date_key = None
            self._rendered_detail_date_key = None
            self._pending_detail_scroll = None
            self._structure_signature = None
            self._widgets = {}
            self._placed = False
            self._geometry_retry_pending = False
            self._destroyed = True
            self._visible = False
            self._inline_editor_active = False
            self._inline_editor_kind = None
            self._inline_editor_context = None
            self._pointer_inside = False
            self._cancel_timers()
            return None

        tk = self._ensure_tk()
        if tk is None:
            return None
        try:
            window = tk.Toplevel(self._root)
        except Exception:
            return None

        self._window = window
        _safe_call(window, "withdraw")
        _safe_call(window, "title", "Wrike 근무시간")
        _safe_call(window, "configure", bg=_BG)
        _safe_call(window, "overrideredirect", True)
        _safe_call(window, "attributes", "-topmost", True)
        _safe_call(window, "resizable", False, False)
        _safe_call(window, "protocol", "WM_DELETE_WINDOW", self.hide)
        self._bind_additive(window, "<Escape>", self._on_escape)
        self._bind_additive(window, "<Enter>", self._on_pointer_enter)
        self._bind_additive(window, "<Leave>", self._on_pointer_leave)
        self._bind_additive(window, "<Motion>", self._on_pointer_activity)
        self._bind_additive(window, "<Button>", self._on_pointer_activity)
        self._bind_additive(window, "<MouseWheel>", self._on_pointer_activity)
        self._bind_additive(window, "<Button-4>", self._on_pointer_activity)
        self._bind_additive(window, "<Button-5>", self._on_pointer_activity)
        self._bind_additive(window, "<KeyPress>", self._on_key_activity)

        try:
            shell = tk.Frame(
                window,
                bg=_BG,
                highlightthickness=1,
                highlightbackground=_BORDER,
                highlightcolor=_BORDER,
            )
            shell.pack(fill="both", expand=True)
            content = tk.Frame(shell, bg=_BG)
            content.pack(fill="both", expand=True, padx=7, pady=7)
        except Exception:
            _safe_call(window, "destroy")
            self._window = None
            return None
        self._shell = shell
        self._content = content
        return window

    @staticmethod
    def _bind_additive(widget: Any, sequence: str, callback: Callable[..., Any]) -> None:
        binder = getattr(widget, "bind", None)
        if not callable(binder):
            return
        try:
            binder(sequence, callback, add="+")
        except TypeError:
            try:
                binder(sequence, callback)
            except Exception:
                pass
        except Exception:
            pass

    def _on_escape(self, _event: Any = None) -> str:
        self.hide()
        return "break"

    def _set_pointer_inside(self, inside: bool) -> None:
        changed = self._pointer_inside is not bool(inside)
        self._pointer_inside = bool(inside)
        shell = self._shell
        if shell is not None:
            color = _HOVER_BORDER if self._pointer_inside else _BORDER
            _safe_call(
                shell,
                "configure",
                highlightbackground=color,
                highlightcolor=color,
            )
        if not self._visible or self._destroyed:
            self._update_countdown_label()
            return
        if (
            changed
            and not self._pointer_inside
            and (
                self._dismiss_expired_while_hovered
                or (
                    self._dismiss_deadline is not None
                    and self._monotonic() >= self._dismiss_deadline
                )
            )
        ):
            self.hide()
            return
        self._update_countdown_label()

    def _pointer_is_within_window(self, event: Any = None) -> bool:
        window = self._window
        rect = self._window_rect(window)
        if rect is None:
            return False
        try:
            pointer_x = int(window.winfo_pointerx())
            pointer_y = int(window.winfo_pointery())
        except Exception:
            try:
                pointer_x = int(getattr(event, "x_root"))
                pointer_y = int(getattr(event, "y_root"))
            except Exception:
                return False
        x, y, width, height = rect
        return x <= pointer_x < x + width and y <= pointer_y < y + height

    def _on_pointer_enter(self, _event: Any = None) -> None:
        self._set_pointer_inside(True)

    def _on_pointer_leave(self, event: Any = None) -> None:
        self._set_pointer_inside(self._pointer_is_within_window(event))

    def _on_pointer_activity(self, _event: Any = None) -> None:
        self._set_pointer_inside(True)

    def _on_key_activity(self, event: Any = None) -> None:
        if str(getattr(event, "keysym", "")).lower() == "escape":
            return
        if self._inline_editor_active:
            self._update_countdown_label()
        else:
            self._reset_dismiss_timer()

    def _schedule_refresh(self) -> None:
        if (
            self._destroyed
            or not self._visible
            or self._refresh_after_id is not None
        ):
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        token = object()
        self._refresh_token = token
        try:
            self._refresh_after_id = scheduler(
                _REFRESH_INTERVAL_MS,
                lambda current=token: self._refresh_tick(current),
            )
        except Exception:
            self._refresh_after_id = None
            self._refresh_token = None

    def _refresh_tick(self, token: object) -> None:
        if token is not self._refresh_token:
            return
        self._refresh_after_id = None
        self._refresh_token = None
        if not self.is_visible():
            return
        try:
            self._retry_geometry_if_pending()
            self.refresh_now()
        finally:
            self._schedule_refresh()

    def _cancel_refresh(self) -> None:
        after_id = self._refresh_after_id
        self._refresh_after_id = None
        self._refresh_token = None
        if after_id is None:
            return
        canceller = getattr(self._root, "after_cancel", None)
        if callable(canceller):
            try:
                canceller(after_id)
            except Exception:
                pass

    def _reset_dismiss_timer(self) -> None:
        self._cancel_dismiss_timer()
        if (
            self._interaction_depth > 0
            or self._inline_editor_active
            or self._destroyed
            or not self._visible
        ):
            self._update_countdown_label()
            return
        self._dismiss_deadline = self._monotonic() + self._idle_timeout_ms / 1000.0
        self._dismiss_expired_while_hovered = False
        self._schedule_dismiss_callback(self._idle_timeout_ms)
        self._update_countdown_label()
        self._schedule_countdown()

    def _schedule_dismiss_callback(self, delay_ms: int | None = None) -> None:
        deadline = self._dismiss_deadline
        if (
            deadline is None
            or self._dismiss_after_id is not None
            or self._destroyed
            or not self._visible
        ):
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        if delay_ms is None:
            delay_ms = max(
                1,
                math.ceil((deadline - self._monotonic()) * 1000.0),
            )
        token = object()
        self._dismiss_token = token
        try:
            self._dismiss_after_id = scheduler(
                delay_ms,
                lambda current=token: self._dismiss_tick(current),
            )
        except Exception:
            self._dismiss_after_id = None
            self._dismiss_token = None
            self._dismiss_deadline = None

    def _dismiss_tick(self, token: object) -> None:
        if token is not self._dismiss_token:
            return
        self._dismiss_after_id = None
        self._dismiss_token = None
        deadline = self._dismiss_deadline
        if deadline is None:
            return
        self._cancel_countdown()
        if self._destroyed or not self._visible:
            self._dismiss_deadline = None
            self._dismiss_expired_while_hovered = False
            return
        if self._interaction_depth > 0 or self._inline_editor_active:
            self._dismiss_deadline = None
            self._dismiss_expired_while_hovered = False
            self._update_countdown_label()
            return
        if self._pointer_inside:
            self._dismiss_expired_while_hovered = True
            self._update_countdown_label()
            return
        self.hide()

    def _schedule_countdown(self) -> None:
        if (
            self._countdown_after_id is not None
            or self._dismiss_deadline is None
            or self._destroyed
            or not self._visible
        ):
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        token = object()
        self._countdown_token = token
        try:
            self._countdown_after_id = scheduler(
                _COUNTDOWN_INTERVAL_MS,
                lambda current=token: self._countdown_tick(current),
            )
        except Exception:
            self._countdown_after_id = None
            self._countdown_token = None

    def _countdown_tick(self, token: object) -> None:
        if token is not self._countdown_token:
            return
        self._countdown_after_id = None
        self._countdown_token = None
        self._update_countdown_label()
        if self._dismiss_deadline is not None and self.is_visible():
            self._schedule_countdown()

    def _countdown_text(self) -> str:
        if not self._visible or self._destroyed:
            return ""
        if self._inline_editor_active:
            return "편집 중 · 자동 닫힘 일시정지"
        if self._interaction_depth > 0:
            return "작업 중 · 자동 닫힘 일시정지"
        if self._dismiss_deadline is None:
            return "자동 닫힘 대기 중"
        if self._dismiss_expired_while_hovered and self._pointer_inside:
            return "마우스 호버 중 · 이동 시 닫힘"
        remaining = self._dismiss_deadline - self._monotonic()
        if remaining > 0:
            return f"{max(1, math.ceil(remaining))}초 후 닫힘"
        if self._pointer_inside:
            return "마우스 호버 중 · 이동 시 닫힘"
        return "0초 후 닫힘"

    def _update_countdown_label(self) -> None:
        label = self._widgets.get("countdown")
        if label is not None:
            _safe_call(label, "configure", text=self._countdown_text())

    def _cancel_countdown(self) -> None:
        after_id = self._countdown_after_id
        self._countdown_after_id = None
        self._countdown_token = None
        if after_id is None:
            return
        canceller = getattr(self._root, "after_cancel", None)
        if callable(canceller):
            try:
                canceller(after_id)
            except Exception:
                pass

    def _cancel_dismiss_timer(self) -> None:
        after_id = self._dismiss_after_id
        self._dismiss_after_id = None
        self._dismiss_token = None
        self._dismiss_deadline = None
        self._dismiss_expired_while_hovered = False
        self._cancel_countdown()
        if after_id is not None:
            canceller = getattr(self._root, "after_cancel", None)
            if callable(canceller):
                try:
                    canceller(after_id)
                except Exception:
                    pass
        self._update_countdown_label()

    def _cancel_timers(self) -> None:
        self._cancel_refresh()
        self._cancel_dismiss_timer()

    def _retry_geometry_if_pending(self) -> None:
        if not self._geometry_retry_pending or not self.is_visible():
            return
        self._geometry_retry_pending = not self._reconcile_geometry()
        if not self._geometry_retry_pending:
            self._restore_pending_detail_scroll()

    def _render_structure(self, model: WorktimePanelModel) -> None:
        tk = self._tk
        content = self._content
        if tk is None or content is None:
            return
        preserved_inline_value = self._current_inline_editor_value()
        preserved_detail_scroll = self._current_detail_scroll()
        self._clear_content()
        self._widgets = {}
        self._rendered_detail_date_key = None
        self._pending_detail_scroll = preserved_detail_scroll

        compact = model.prompt is not None or self._uses_compact_density()
        section_gap = 3 if compact else 8
        title_padding = (3, 0) if compact else (7, 2)
        row_padding = 0 if compact else 2
        _safe_call(content, "pack_configure", padx=6 if compact else 8, pady=3 if compact else 8)

        header = tk.Frame(
            content,
            bg=_CARD_BG,
            highlightthickness=1,
            highlightbackground=_BORDER,
        )
        header.pack(fill="x", pady=(0, section_gap))
        title_row = tk.Frame(header, bg=_CARD_BG)
        title_row.pack(fill="x", padx=10, pady=title_padding)
        tk.Label(
            title_row,
            text="Wrike 근무시간",
            bg=_CARD_BG,
            fg=_TEXT,
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")
        week_range_label = tk.Label(
            title_row,
            text=model.week_range,
            bg=_CARD_BG,
            fg=_MUTED,
            font=("Segoe UI", 9),
        )
        week_range_label.pack(side="right")
        sync_label = tk.Label(
            header,
            text=f"동기화 · {model.sync_text}",
            bg=_CARD_BG,
            fg=self._sync_color(model),
            anchor="w",
            font=("Segoe UI", 9),
        )
        sync_label.pack(fill="x", padx=10, pady=(0, 3 if compact else 7))

        today_card = tk.Frame(
            content,
            bg=_CARD_BG,
            highlightthickness=1,
            highlightbackground=_BORDER,
        )
        today_card.pack(fill="x", pady=(0, section_gap))
        tk.Label(
            today_card,
            text="오늘 상세",
            bg=_CARD_BG,
            fg=_TEXT,
            anchor="w",
            font=("Segoe UI", 10, "bold"),
        ).pack(fill="x", padx=10, pady=title_padding)
        today_line_labels = []
        if model.today_lines:
            for line in model.today_lines:
                label = tk.Label(
                    today_card,
                    text=line.text,
                    bg=_CARD_BG,
                    fg=line.color,
                    anchor="w",
                    justify="left",
                    font=("Segoe UI", 9),
                )
                label.pack(fill="x", padx=10, pady=0)
                today_line_labels.append(label)
        else:
            label = tk.Label(
                today_card,
                text="표시할 오늘 상세가 없습니다.",
                bg=_CARD_BG,
                fg=_MUTED,
                anchor="w",
                font=("Segoe UI", 9),
            )
            label.pack(fill="x", padx=10, pady=0)
            today_line_labels.append(label)
        tk.Frame(today_card, bg=_CARD_BG, height=1 if compact else 5).pack(fill="x")

        week_card = tk.Frame(
            content,
            bg=_CARD_BG,
            highlightthickness=1,
            highlightbackground=_BORDER,
        )
        week_card.pack(fill="x", pady=(0, section_gap))
        tk.Label(
            week_card,
            text="월요일 - 일요일",
            bg=_CARD_BG,
            fg=_TEXT,
            anchor="w",
            font=("Segoe UI", 10, "bold"),
        ).pack(
            fill="x",
            padx=10,
            pady=(2, 0) if compact else (7, 3),
        )
        row_widgets = []
        for row_index, row in enumerate(model.rows):
            selected = row.date_key == self._selected_date_key
            row_bg = _SELECTED_BG if selected else (_TODAY_BG if row.today else _CARD_BG)
            emphasis = "bold" if row.today or selected else "normal"
            row_frame = tk.Frame(week_card, bg=row_bg, cursor="hand2")
            row_frame.pack(fill="x", padx=8, pady=0)
            weekday_label = tk.Label(
                row_frame,
                text=row.weekday,
                width=4,
                bg=row_bg,
                fg=_TEXT,
                anchor="w",
                cursor="hand2",
                font=("Segoe UI", 9, emphasis),
            )
            weekday_label.pack(side="left", padx=(4, 2), pady=row_padding)
            date_label = tk.Label(
                row_frame,
                text=row.date,
                width=8,
                bg=row_bg,
                fg=_MUTED,
                anchor="w",
                cursor="hand2",
                font=("Segoe UI", 9),
            )
            date_label.pack(side="left", padx=(0, 8), pady=row_padding)
            summary_label = tk.Label(
                row_frame,
                text=row.summary,
                bg=row_bg,
                fg=row.color,
                anchor="w",
                justify="left",
                cursor="hand2",
                font=("Segoe UI", 9, emphasis),
            )
            summary_label.pack(side="left", fill="x", expand=True, pady=row_padding)
            today_label = tk.Label(
                row_frame,
                text="오늘" if row.today else "",
                bg=row_bg,
                fg="#1D4ED8",
                cursor="hand2",
                font=("Segoe UI", 8, "bold"),
            )
            today_label.pack(side="right", padx=4, pady=row_padding)
            widgets_for_row = (
                row_frame,
                weekday_label,
                date_label,
                summary_label,
                today_label,
            )
            for widget in widgets_for_row:
                self._bind_additive(
                    widget,
                    "<Button-1>",
                    lambda _event=None, index=row_index: self._select_row_command(
                        index
                    ),
                )
            row_widgets.append(widgets_for_row)

        detail_card = tk.Frame(
            content,
            bg=_CARD_BG,
            highlightthickness=1,
            highlightbackground=_BORDER,
        )
        detail_card.pack(fill="x", pady=(0, section_gap))
        detail_header = tk.Frame(detail_card, bg=_CARD_BG)
        detail_header.pack(fill="x", padx=10, pady=title_padding)
        detail_title = tk.Label(
            detail_header,
            text="선택 날짜 실제 기록 · 티켓 / 시간 / 코멘트",
            bg=_CARD_BG,
            fg=_TEXT,
            anchor="w",
            font=("Segoe UI", 10, "bold"),
        )
        detail_title.pack(side="left", fill="x", expand=True)
        detail_timelog_button = tk.Button(
            detail_header,
            text="타임로그",
            command=lambda: self._set_detail_view("timelog"),
            bg=_CARD_BG,
            fg=_TEXT,
            activebackground="#E5E7EB",
            relief="sunken",
            borderwidth=1,
            padx=5,
            pady=2,
            font=("Segoe UI", 8),
        )
        detail_timelog_button.pack(side="right", padx=(3, 0))
        detail_breaks_button = tk.Button(
            detail_header,
            text="휴게 기록",
            command=lambda: self._set_detail_view("breaks"),
            bg=_CARD_BG,
            fg=_TEXT,
            activebackground="#E5E7EB",
            relief="solid",
            borderwidth=1,
            padx=5,
            pady=2,
            font=("Segoe UI", 8),
        )
        detail_breaks_button.pack(side="right", padx=(3, 0))
        manual_break_menu = None
        menu_factory = getattr(tk, "Menu", None)
        menu_button_factory = getattr(tk, "Menubutton", None)
        if callable(menu_factory) and callable(menu_button_factory):
            manual_break_menu = menu_factory(detail_header, tearoff=False)
            manual_break_menu_button = menu_button_factory(
                detail_header,
                text="수동 휴게 수정",
                menu=manual_break_menu,
                bg=_CARD_BG,
                fg=_TEXT,
                activebackground="#E5E7EB",
                relief="solid",
                borderwidth=1,
                padx=5,
                pady=2,
                font=("Segoe UI", 8),
            )
        else:
            # Production Tk has a menu. This compact fallback keeps headless
            # widget doubles usable without creating another vertical card.
            manual_break_menu_button = tk.Button(
                detail_header,
                text="수동 휴게 수정",
                command=lambda: self._edit_first_manual_break(),
                bg=_CARD_BG,
                fg=_TEXT,
                activebackground="#E5E7EB",
                relief="solid",
                borderwidth=1,
                padx=5,
                pady=2,
                font=("Segoe UI", 8),
            )
        manual_break_menu_button.pack(side="right", padx=(3, 0))
        detail_text_factory = getattr(tk, "Text", None)
        if callable(detail_text_factory):
            detail_area = tk.Frame(detail_card, bg=_CARD_BG)
            detail_area.pack(fill="x", padx=10, pady=(0, 8))
            detail_text = detail_text_factory(
                detail_area,
                height=5,
                bg=_CARD_BG,
                fg=_TEXT,
                wrap="word",
                relief="solid",
                highlightthickness=1,
                highlightbackground=_BORDER,
                font=("Segoe UI", 10),
            )
            detail_scroll = tk.Scrollbar(detail_area, command=detail_text.yview)
            detail_text.configure(yscrollcommand=detail_scroll.set, state="disabled")
            detail_text.pack(side="left", fill="both", expand=True)
            detail_scroll.pack(side="right", fill="y")
        else:
            # Headless test doubles do not implement Text.  Production Tk uses
            # the bounded, scrollable branch above.
            detail_text = tk.Label(
                detail_card,
                text="",
                bg=_CARD_BG,
                fg=_MUTED,
                anchor="w",
                justify="left",
                font=("Segoe UI", 9),
            )
            detail_text.pack(fill="x", padx=10, pady=(0, 6))
        inline_editor = tk.Frame(
            content,
            bg=_CARD_BG,
            highlightthickness=1,
            highlightbackground=_HOVER_BORDER,
        )
        inline_row = tk.Frame(inline_editor, bg=_CARD_BG)
        inline_row.pack(fill="x", padx=10, pady=(7, 3))
        inline_title = tk.Label(
            inline_row,
            text="",
            bg=_CARD_BG,
            fg=_TEXT,
            font=("Segoe UI", 9, "bold"),
        )
        inline_title.pack(side="left")
        inline_entry = tk.Entry(
            inline_row,
            width=8,
            bg=_CARD_BG,
            fg=_TEXT,
            justify="center",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
        )
        inline_entry.pack(side="left", padx=(10, 4))
        inline_hint = tk.Label(
            inline_row,
            text="",
            bg=_CARD_BG,
            fg=_MUTED,
            font=("Segoe UI", 8),
        )
        inline_hint.pack(side="left", padx=(2, 8))
        self._button(inline_row, "저장", self._save_inline_editor_command)
        self._button(inline_row, "취소", self._cancel_inline_editor_command)
        inline_error = tk.Label(
            inline_editor,
            text="",
            bg=_CARD_BG,
            fg="#DC2626",
            anchor="w",
            font=("Segoe UI", 8),
        )
        inline_error.pack(fill="x", padx=10, pady=(0, 6))
        self._bind_additive(inline_entry, "<Return>", self._save_inline_editor_event)
        self._bind_additive(inline_entry, "<Escape>", self._cancel_inline_editor_event)

        actions = tk.Frame(content, bg=_BG)
        actions.pack(fill="x", pady=(0, 1))
        refresh_button = self._button(actions, "새로고침", self._refresh_command)
        clock_button = self._button(
            actions,
            "출근 수정" if model.has_clock_in else "지금 출근",
            self._clock_action_command,
        )
        break_button = self._button(
            actions,
            "휴게 종료" if model.break_active else "휴게 시작",
            self._toggle_break_command,
        )
        plan_button = self._button(actions, "목표 수정", self._edit_plan_command)
        settings_button = self._button(actions, "설정", self._settings_command)
        countdown_label = tk.Label(
            content,
            text=self._countdown_text(),
            bg=_BG,
            fg=_MUTED,
            anchor="e",
            font=("Segoe UI", 8),
        )
        countdown_label.pack(fill="x", pady=(0, section_gap))

        prompt_label = None
        prompt_buttons: tuple[Any, ...] = ()
        if model.prompt is not None:
            prompt = model.prompt
            prompt_card = tk.Frame(
                content,
                bg=_PROMPT_BG,
                highlightthickness=1,
                highlightbackground=_PROMPT_BORDER,
            )
            prompt_card.pack(fill="x")
            prompt_label = tk.Label(
                prompt_card,
                text=f"{prompt.detected_time} 활동을 출근으로 반영할까요?",
                bg=_PROMPT_BG,
                fg="#9A3412",
                anchor="w",
                font=("Segoe UI", 9, "bold"),
            )
            prompt_label.pack(fill="x", padx=10, pady=(4, 2))
            prompt_actions = tk.Frame(prompt_card, bg=_PROMPT_BG)
            prompt_actions.pack(fill="x", padx=8, pady=(0, 4))
            accept_button = self._button(
                prompt_actions,
                f"{prompt.detected_time}으로 출근",
                self._prompt_accept_current_command,
            )
            edit_button = self._button(
                prompt_actions,
                "시간 수정",
                self._prompt_edit_current_command,
            )
            snooze_button = self._button(
                prompt_actions,
                "30분 후",
                self._prompt_snooze_command,
            )
            skip_button = self._button(
                prompt_actions,
                "오늘 건너뛰기",
                self._prompt_skip_command,
            )
            prompt_buttons = (
                accept_button,
                edit_button,
                snooze_button,
                skip_button,
            )

        self._widgets = {
            "week_range": week_range_label,
            "sync": sync_label,
            "today_lines": tuple(today_line_labels),
            "rows": tuple(row_widgets),
            "detail_title": detail_title,
            "detail_text": detail_text,
            "detail_timelog_button": detail_timelog_button,
            "detail_breaks_button": detail_breaks_button,
            "manual_break_menu": manual_break_menu,
            "manual_break_menu_button": manual_break_menu_button,
            "refresh_button": refresh_button,
            "clock_button": clock_button,
            "break_button": break_button,
            "plan_button": plan_button,
            "settings_button": settings_button,
            "inline_editor": inline_editor,
            "inline_title": inline_title,
            "inline_entry": inline_entry,
            "inline_hint": inline_hint,
            "inline_error": inline_error,
            "actions": actions,
            "countdown": countdown_label,
            "prompt_label": prompt_label,
            "prompt_buttons": prompt_buttons,
        }
        self._update_detail_presentation(model)
        if self._inline_editor_active and self._inline_editor_kind is not None:
            initial_value = preserved_inline_value
            if initial_value is None:
                initial_value = self._inline_editor_initial_value(
                    self._inline_editor_kind,
                    model,
                    self._inline_editor_context,
                )
            self._show_inline_editor(
                self._inline_editor_kind,
                initial_value,
                context=self._inline_editor_context,
            )
        self._update_countdown_label()

    def _update_rendered_model(self, model: WorktimePanelModel) -> None:
        widgets = self._widgets
        widgets["week_range"].configure(text=model.week_range)
        widgets["sync"].configure(
            text=f"동기화 · {model.sync_text}",
            fg=self._sync_color(model),
        )

        today_labels = widgets["today_lines"]
        if model.today_lines:
            for label, line in zip(today_labels, model.today_lines):
                label.configure(text=line.text, fg=line.color)
        else:
            today_labels[0].configure(
                text="표시할 오늘 상세가 없습니다.",
                fg=_MUTED,
            )

        for row_widgets, row in zip(widgets["rows"], model.rows):
            row_frame, weekday_label, date_label, summary_label, today_label = (
                row_widgets
            )
            selected = row.date_key == self._selected_date_key
            row_bg = _SELECTED_BG if selected else (_TODAY_BG if row.today else _CARD_BG)
            emphasis = "bold" if row.today or selected else "normal"
            row_frame.configure(bg=row_bg)
            weekday_label.configure(
                text=row.weekday,
                bg=row_bg,
                font=("Segoe UI", 9, emphasis),
            )
            date_label.configure(text=row.date, bg=row_bg)
            summary_label.configure(
                text=row.summary,
                bg=row_bg,
                fg=row.color,
                font=("Segoe UI", 9, emphasis),
            )
            today_label.configure(text="오늘" if row.today else "", bg=row_bg)

        self._update_detail_presentation(model)

        widgets["clock_button"].configure(
            text="출근 수정" if model.has_clock_in else "지금 출근"
        )
        widgets["break_button"].configure(
            text="휴게 종료" if model.break_active else "휴게 시작"
        )
        self._update_countdown_label()

        if model.prompt is not None:
            prompt = model.prompt
            widgets["prompt_label"].configure(
                text=f"{prompt.detected_time} 활동을 출근으로 반영할까요?"
            )
            accept_button = widgets["prompt_buttons"][0]
            accept_button.configure(text=f"{prompt.detected_time}으로 출근")

    def _update_row_selection(self, model: WorktimePanelModel) -> None:
        for row_widgets, row in zip(self._widgets.get("rows", ()), model.rows):
            row_frame, weekday_label, date_label, summary_label, today_label = (
                row_widgets
            )
            selected = row.date_key == self._selected_date_key
            row_bg = _SELECTED_BG if selected else (_TODAY_BG if row.today else _CARD_BG)
            emphasis = "bold" if row.today or selected else "normal"
            _safe_call(row_frame, "configure", bg=row_bg)
            _safe_call(
                weekday_label,
                "configure",
                bg=row_bg,
                font=("Segoe UI", 9, emphasis),
            )
            _safe_call(date_label, "configure", bg=row_bg)
            _safe_call(
                summary_label,
                "configure",
                bg=row_bg,
                font=("Segoe UI", 9, emphasis),
            )
            _safe_call(today_label, "configure", bg=row_bg)
        self._update_detail_presentation(model, reset_scroll=True)

    def _set_detail_text(
        self,
        widget: Any,
        value: str,
        *,
        date_key: str | None,
        reset_scroll: bool = False,
        tagged_parts: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        if widget is None:
            return
        if (
            callable(getattr(widget, "delete", None))
            and callable(getattr(widget, "insert", None))
            and callable(getattr(widget, "yview", None))
        ):
            same_date = date_key == self._rendered_detail_date_key
            try:
                current = str(widget.get("1.0", "end-1c"))
            except Exception:
                current = None
            if same_date and current == value and not reset_scroll:
                return
            scroll_start = 0.0
            if same_date and not reset_scroll:
                try:
                    yview = widget.yview()
                    scroll_start = float(yview[0])
                except Exception:
                    scroll_start = 0.0
            _safe_call(widget, "configure", state="normal")
            _safe_call(widget, "delete", "1.0", "end")
            tag_configure = getattr(widget, "tag_configure", None)
            if callable(tag_configure) and tagged_parts is not None:
                try:
                    tag_configure(
                        "detail_heading",
                        foreground=_TEXT,
                        font=("Segoe UI", 10, "bold"),
                        spacing3=2,
                    )
                    tag_configure(
                        "detail_comment",
                        foreground=_MUTED,
                        font=("Segoe UI", 9),
                        lmargin1=12,
                        lmargin2=12,
                        spacing3=4,
                    )
                    for text, tag in tagged_parts:
                        widget.insert("end", text, tag)
                except Exception:
                    _safe_call(widget, "delete", "1.0", "end")
                    _safe_call(widget, "insert", "1.0", value)
            else:
                _safe_call(widget, "insert", "1.0", value)
            _safe_call(widget, "configure", state="disabled")
            _safe_call(widget, "yview_moveto", scroll_start)
            self._rendered_detail_date_key = date_key
            return
        _safe_call(widget, "configure", text=value)
        self._rendered_detail_date_key = date_key

    def _current_detail_scroll(self) -> _DetailScrollAnchor | None:
        """Capture a live detail reading position before its widget is rebuilt."""

        date_key = self._rendered_detail_date_key
        if not date_key or date_key != self._selected_date_key:
            return None
        widget = self._widgets.get("detail_text")
        if not callable(getattr(widget, "yview", None)):
            return None
        try:
            yview = widget.yview()
            scroll_start = float(yview[0])
        except Exception:
            return None
        if not math.isfinite(scroll_start):
            return None
        text_index = None
        try:
            candidate = str(widget.index("@0,0"))
            if re.fullmatch(r"\d+\.\d+", candidate) is not None:
                text_index = candidate
        except Exception:
            pass
        return _DetailScrollAnchor(
            date_key=date_key,
            text_index=text_index,
            fraction=max(0.0, min(1.0, scroll_start)),
        )

    @staticmethod
    def _restore_detail_scroll(
        widget: Any,
        preserved: _DetailScrollAnchor | None,
        *,
        date_key: str | None,
    ) -> None:
        """Restore only the selected date's prior detail reading position."""

        if preserved is None or preserved.date_key != date_key:
            return
        if preserved.text_index and WorktimeQuickPanel._restore_detail_text_index(
            widget,
            preserved.text_index,
        ):
            return
        _safe_call(widget, "yview_moveto", preserved.fraction)

    @staticmethod
    def _restore_detail_text_index(widget: Any, text_index: str) -> bool:
        """Put a captured Text display line back at the top after layout settles."""

        try:
            count = widget.count("1.0", text_index, "displaylines")
            display_lines = int(count[0] if isinstance(count, tuple) else count)
            if display_lines < 0:
                return False
            widget.yview_moveto(0.0)
            widget.yview_scroll(display_lines, "units")
        except Exception:
            return False
        return True

    def _restore_pending_detail_scroll(self) -> None:
        """Restore detail position only after the rebuilt panel has its geometry."""

        preserved = self._pending_detail_scroll
        self._pending_detail_scroll = None
        self._restore_detail_scroll(
            self._widgets.get("detail_text"),
            preserved,
            date_key=self._selected_date_key,
        )

    def _retarget_target_editor(self, row: WorktimePanelDayRow) -> None:
        if (
            not self._inline_editor_active
            or self._inline_editor_kind != _INLINE_EDITOR_TARGET
        ):
            return
        self._inline_editor_context = row.date_key
        title, hint = self._inline_editor_copy(
            _INLINE_EDITOR_TARGET,
            row.date_key,
        )
        _safe_call(self._widgets.get("inline_title"), "configure", text=title)
        _safe_call(self._widgets.get("inline_hint"), "configure", text=hint)
        entry = self._widgets.get("inline_entry")
        _set_entry_text(entry, self._format_target_minutes(row.target_minutes))
        _safe_call(self._widgets.get("inline_error"), "configure", text="")
        _safe_call(entry, "focus_set")
        _safe_call(entry, "selection_range", 0, "end")
        self._update_countdown_label()

    def _select_row_command(self, row_index: int) -> str:
        model = self._model
        if (
            model is None
            or type(row_index) is not int
            or not 0 <= row_index < len(model.rows)
        ):
            return "break"
        row = model.rows[row_index]
        changed = row.date_key != self._selected_date_key
        self._selected_date_key = row.date_key
        self._reconcile_inline_editor_for_model(model)
        self._update_row_selection(model)
        if (
            changed
            and self._inline_editor_active
            and self._inline_editor_kind == _INLINE_EDITOR_TARGET
        ):
            self._retarget_target_editor(row)
        elif not self._inline_editor_active and self._visible:
            self._reset_dismiss_timer()
        return "break"

    @staticmethod
    def _sync_color(model: WorktimePanelModel) -> str:
        return _SYNC_COLORS.get(model.sync_state.strip().lower(), _MUTED)

    def _clear_content(self) -> None:
        content = self._content
        if content is None:
            return
        children_getter = getattr(content, "winfo_children", None)
        if not callable(children_getter):
            return
        try:
            children = tuple(children_getter())
        except Exception:
            return
        for child in children:
            _safe_call(child, "destroy")

    def _button(self, parent: Any, text: str, command: Callable[[], None]) -> Any:
        button = self._tk.Button(
            parent,
            text=text,
            command=command,
            bg=_CARD_BG,
            fg=_TEXT,
            activebackground="#E5E7EB",
            activeforeground=_TEXT,
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=4,
            font=("Segoe UI", 9),
        )
        button.pack(side="left", padx=(0, 6), pady=2)
        return button

    @staticmethod
    def _format_target_minutes(minutes: int) -> str:
        total = max(0, min(1440, int(minutes)))
        return f"{total // 60:02d}:{total % 60:02d}"

    @staticmethod
    def _format_actual_minutes(minutes: int) -> str:
        """Format authoritative timelog durations without target-editor bounds."""

        total = max(0, int(minutes))
        return f"{total // 60:02d}:{total % 60:02d}"

    @staticmethod
    def _parse_target_minutes(value: object) -> tuple[int | None, str | None]:
        text = str(value or "").strip()
        if _TARGET_HHMM_PATTERN.fullmatch(text) is None:
            return None, "HH:MM 형식으로 입력해 주세요."
        hours_text, minutes_text = text.split(":", 1)
        hours = int(hours_text)
        minutes = int(minutes_text)
        if hours == 24 and minutes != 0:
            return None, "24시간은 24:00으로만 입력할 수 있습니다."
        total = hours * 60 + minutes
        if not 0 <= total <= 1440:
            return None, "00:00부터 24:00 사이로 입력해 주세요."
        return total, None

    @staticmethod
    def _parse_clock_time(value: object) -> tuple[str | None, str | None]:
        text = str(value or "").strip()
        if _HHMM_PATTERN.fullmatch(text) is None:
            return None, "HH:MM (00:00–23:59) 형식으로 입력해 주세요."
        return text, None

    @staticmethod
    def _parse_manual_break_times(
        value: object,
    ) -> tuple[str | None, str | None, str | None]:
        pieces = [part.strip() for part in str(value or "").split("-", 1)]
        if (
            len(pieces) != 2
            or _HHMM_PATTERN.fullmatch(pieces[0]) is None
            or _TARGET_HHMM_PATTERN.fullmatch(pieces[1]) is None
        ):
            return None, None, "HH:MM - HH:MM 또는 24:00 형식으로 입력해 주세요."
        if pieces[1] <= pieces[0]:
            return None, None, "휴게 종료 시간은 시작 시간보다 뒤여야 합니다."
        return pieces[0], pieces[1], None

    def _current_inline_editor_value(self) -> str | None:
        if not self._inline_editor_active:
            return None
        entry = self._widgets.get("inline_entry")
        getter = getattr(entry, "get", None)
        if not callable(getter):
            return None
        try:
            return str(getter())
        except Exception:
            return None

    def _inline_editor_initial_value(
        self,
        kind: str,
        model: WorktimePanelModel,
        context: str | None,
    ) -> str:
        if kind == _INLINE_EDITOR_TARGET:
            row = self._row_for_date(model, context)
            return self._format_target_minutes(
                model.target_minutes if row is None else row.target_minutes
            )
        if kind == _INLINE_EDITOR_CLOCK_IN:
            return str(model.clock_in_time or "")
        if kind == _INLINE_EDITOR_PROMPT:
            return str(context or "")
        if kind == _INLINE_EDITOR_MANUAL_BREAK and self._manual_break_edit is not None:
            return (
                f"{self._manual_break_edit.start_time} - "
                f"{self._manual_break_edit.end_time}"
            )
        raise ValueError(f"unsupported inline editor kind: {kind}")

    @staticmethod
    def _inline_editor_copy(
        kind: str,
        context: str | None = None,
    ) -> tuple[str, str]:
        if kind == _INLINE_EDITOR_TARGET:
            return f"{context or '선택 날짜'} 목표 순근무 시간", "HH:MM (00:00–24:00)"
        if kind == _INLINE_EDITOR_CLOCK_IN:
            return "오늘 출근 시간", "HH:MM (00:00–23:59)"
        if kind == _INLINE_EDITOR_PROMPT:
            return "감지된 출근 시간", "HH:MM (00:00–23:59)"
        if kind == _INLINE_EDITOR_MANUAL_BREAK:
            return "완료한 수동 휴게", "HH:MM - HH:MM"
        raise ValueError(f"unsupported inline editor kind: {kind}")

    def _show_inline_editor(
        self,
        kind: str,
        initial_value: str,
        *,
        context: str | None = None,
    ) -> None:
        editor = self._widgets.get("inline_editor")
        entry = self._widgets.get("inline_entry")
        actions = self._widgets.get("actions")
        if editor is None or entry is None:
            return
        title, hint = self._inline_editor_copy(kind, context)
        self._inline_editor_active = True
        self._inline_editor_kind = kind
        self._inline_editor_context = context
        _safe_call(self._widgets.get("inline_title"), "configure", text=title)
        _safe_call(self._widgets.get("inline_hint"), "configure", text=hint)
        _safe_call(entry, "configure", width=17 if kind == _INLINE_EDITOR_MANUAL_BREAK else 8)
        _set_entry_text(entry, str(initial_value))
        _safe_call(self._widgets.get("inline_error"), "configure", text="")
        _safe_call(
            editor,
            "pack",
            fill="x",
            pady=(0, 6),
            before=actions,
        )
        _safe_call(entry, "focus_set")
        _safe_call(entry, "selection_range", 0, "end")
        self._cancel_dismiss_timer()
        self._update_countdown_label()
        self._geometry_retry_pending = not self._reconcile_geometry(
            resize_to_request=True
        )

    def _focus_or_show_inline_editor(
        self,
        kind: str,
        initial_value: str,
        *,
        context: str | None = None,
    ) -> None:
        if (
            self._inline_editor_active
            and self._inline_editor_kind == kind
            and self._inline_editor_context == context
        ):
            entry = self._widgets.get("inline_entry")
            _safe_call(entry, "focus_set")
            _safe_call(entry, "selection_range", 0, "end")
            return
        self._show_inline_editor(kind, initial_value, context=context)

    def _close_inline_editor(self, *, reconcile: bool = True) -> None:
        if not self._inline_editor_active:
            return
        self._inline_editor_active = False
        self._inline_editor_kind = None
        self._inline_editor_context = None
        self._manual_break_edit = None
        _safe_call(self._widgets.get("inline_entry"), "configure", width=8)
        _safe_call(self._widgets.get("inline_editor"), "pack_forget")
        _safe_call(self._widgets.get("inline_error"), "configure", text="")
        if reconcile and self._visible:
            self._geometry_retry_pending = not self._reconcile_geometry(
                resize_to_request=True
            )
            self._reset_dismiss_timer()
        else:
            self._update_countdown_label()

    def _save_inline_editor_command(self) -> None:
        kind = self._inline_editor_kind
        entry = self._widgets.get("inline_entry")
        getter = getattr(entry, "get", None)
        raw_value = getter() if callable(getter) else ""
        callback: Callable[[], bool] | None = None
        failure_message = "근무시간을 저장하지 못했습니다."
        manual_result: dict[str, str | None] = {"error": None}

        if kind == _INLINE_EDITOR_TARGET:
            target_minutes, error = self._parse_target_minutes(raw_value)
            context = self._inline_editor_context
            if context is None:
                error = "수정할 날짜가 만료되었습니다."
            elif error is None and target_minutes is not None:
                callback = lambda date_key=context, minutes=target_minutes: (
                    self._on_edit_plan(date_key, minutes) is True
                )
                failure_message = "선택 날짜 목표를 저장하지 못했습니다."
        elif kind in {_INLINE_EDITOR_CLOCK_IN, _INLINE_EDITOR_PROMPT}:
            clock_value, error = self._parse_clock_time(raw_value)
            if error is None and clock_value is not None:
                if kind == _INLINE_EDITOR_CLOCK_IN:
                    callback = lambda: self._on_edit_clock_in(clock_value) is True
                else:
                    context = self._inline_editor_context
                    if context is None:
                        error = "수정할 활동 시간이 만료되었습니다."
                    else:
                        callback = (
                            lambda: self._on_prompt_edit(context, clock_value) is True
                        )
                        failure_message = (
                            "출근 시간을 저장하지 못했거나 요청이 만료되었습니다."
                        )
        elif kind == _INLINE_EDITOR_MANUAL_BREAK:
            start_time, end_time, error = self._parse_manual_break_times(raw_value)
            break_row = self._manual_break_edit
            if break_row is None:
                error = "수정할 수동 휴게가 만료되었습니다. 새로고침 후 다시 시도해 주세요."
            elif self._on_edit_manual_break is None:
                error = "수동 휴게 수정 기능을 사용할 수 없습니다."
            elif error is None and start_time is not None and end_time is not None:
                def save_manual_break() -> bool:
                    ok, result_error = self._on_edit_manual_break(
                        break_row,
                        start_time,
                        end_time,
                    )
                    manual_result["error"] = str(result_error or "") or None
                    return ok is True

                callback = save_manual_break
                failure_message = "수동 휴게를 저장하지 못했습니다."
        else:
            error = "편집 상태가 만료되었습니다."

        if error is not None or callback is None:
            _safe_call(
                self._widgets.get("inline_error"),
                "configure",
                text=error or "근무시간을 확인해 주세요.",
            )
            return

        self._interaction_depth += 1
        self._cancel_dismiss_timer()
        saved = False
        try:
            saved = callback()
            self.refresh_now()
        except Exception:
            saved = False
        finally:
            self._interaction_depth = max(0, self._interaction_depth - 1)
        if not saved and manual_result["error"]:
            failure_message = str(manual_result["error"])
        if saved:
            if self._inline_editor_active:
                self._close_inline_editor(reconcile=True)
            elif self._visible:
                self._reset_dismiss_timer()
        elif self._inline_editor_active:
            _safe_call(
                self._widgets.get("inline_error"),
                "configure",
                text=failure_message,
            )
            self._update_countdown_label()

    def _cancel_inline_editor_command(self) -> None:
        self._close_inline_editor(reconcile=True)

    def _save_inline_editor_event(self, _event: Any = None) -> str:
        self._save_inline_editor_command()
        return "break"

    def _cancel_inline_editor_event(self, _event: Any = None) -> str:
        self._cancel_inline_editor_command()
        return "break"

    def _run_command(self, callback: Callable[..., None], *args: Any) -> None:
        self._interaction_depth += 1
        self._cancel_dismiss_timer()
        try:
            callback(*args)
            self.refresh_now()
        finally:
            self._interaction_depth = max(0, self._interaction_depth - 1)
            if self._interaction_depth == 0:
                self._reset_dismiss_timer()

    def _clock_action_command(self) -> None:
        model = self._model
        if model is None:
            return
        if model.has_clock_in:
            self._edit_clock_in_command()
        else:
            self._clock_in_command()

    def _prompt_accept_current_command(self) -> None:
        model = self._model
        if model is None or model.prompt is None:
            return
        self._prompt_accept_command(model.prompt.detected_time)

    def _prompt_edit_current_command(self) -> None:
        model = self._model
        if model is None or model.prompt is None:
            return
        self._prompt_edit_command(model.prompt.detected_time)

    def _refresh_command(self) -> None:
        self._run_command(self._on_refresh)

    def _clock_in_command(self) -> None:
        self._run_command(self._on_clock_in_now)

    def _edit_clock_in_command(self) -> None:
        model = self._model
        if model is None or model.clock_in_time is None:
            return
        self._focus_or_show_inline_editor(
            _INLINE_EDITOR_CLOCK_IN,
            model.clock_in_time,
        )

    def _edit_plan_command(self) -> None:
        model = self._model
        if model is None:
            return
        row = self._selected_row(model)
        if row is None:
            self._reconcile_selection_for_model(model)
            row = self._selected_row(model)
        if row is None:
            return
        self._focus_or_show_inline_editor(
            _INLINE_EDITOR_TARGET,
            self._format_target_minutes(row.target_minutes),
            context=row.date_key,
        )

    def _edit_manual_break_command(
        self,
        break_row: WorktimePanelManualBreak,
    ) -> None:
        if not break_row.editable or self._on_edit_manual_break is None:
            return
        context = f"{break_row.date_key}:{break_row.index}"
        restart = (
            self._inline_editor_active
            and self._inline_editor_kind == _INLINE_EDITOR_MANUAL_BREAK
            and self._inline_editor_context == context
            and self._manual_break_edit is not None
            and self._manual_break_edit.fingerprint != break_row.fingerprint
        )
        self._manual_break_edit = break_row
        # Explicitly reopening a changed row must replace its draft and
        # precondition together; ordinary refresh keeps both unchanged.
        show_editor = self._show_inline_editor if restart else self._focus_or_show_inline_editor
        show_editor(
            _INLINE_EDITOR_MANUAL_BREAK,
            f"{break_row.start_time} - {break_row.end_time}",
            context=context,
        )

    def _edit_first_manual_break(self) -> None:
        model = self._model
        if model is None:
            return
        row = next(
            (row for row in self._selected_manual_breaks(model) if row.editable),
            None,
        )
        if row is not None:
            self._edit_manual_break_command(row)

    def _toggle_break_command(self) -> None:
        self._run_command(self._on_toggle_break)

    def _settings_command(self) -> None:
        self._run_command(self._on_open_settings)

    def _prompt_accept_command(self, detected_time: str) -> None:
        self._run_command(self._on_prompt_accept, detected_time)

    def _prompt_edit_command(self, detected_time: str) -> None:
        self._focus_or_show_inline_editor(
            _INLINE_EDITOR_PROMPT,
            detected_time,
            context=detected_time,
        )

    def _prompt_snooze_command(self) -> None:
        self._run_command(self._on_prompt_snooze)

    def _prompt_skip_command(self) -> None:
        self._run_command(self._on_prompt_skip)

    def _reconcile_geometry(
        self,
        *,
        anchor_to_pointer: bool = False,
        resize_to_request: bool = False,
    ) -> bool:
        """Size safely and optionally anchor the panel beside the pointer."""

        window = self._window
        if window is None or not self._window_exists(window):
            return False
        _safe_call(window, "update_idletasks")

        requested_width = _positive_int_call(window, "winfo_reqwidth", 680)
        requested_height = _positive_int_call(window, "winfo_reqheight", 480)
        current = self._window_rect(window)
        pointer_x, pointer_y = _pointer_position(window, self._root)
        use_pointer = bool(anchor_to_pointer or not self._placed or current is None)
        if use_pointer:
            work_area = _work_area_for_point(
                pointer_x,
                pointer_y,
                window,
                self._root,
            )
        else:
            work_area = _work_area_for_window(window, self._root)
        work_left, work_top, work_right, work_bottom = work_area
        work_width = max(1, work_right - work_left)
        work_height = max(1, work_bottom - work_top)

        current_width = current[2] if current is not None else 1
        current_height = current[3] if current is not None else 1
        if resize_to_request:
            width = min(max(requested_width, 680), work_width)
            height = min(max(requested_height, 480), work_height)
        else:
            width = min(max(current_width, requested_width, 680), work_width)
            height = min(max(current_height, requested_height, 480), work_height)

        if use_pointer:
            x = pointer_x + _POINTER_OFFSET_PX
            y = pointer_y + _POINTER_OFFSET_PX
            if x + width > work_right:
                x = pointer_x - width - _POINTER_OFFSET_PX
            if y + height > work_bottom:
                y = pointer_y - height - _POINTER_OFFSET_PX
        else:
            x = current[0]
            y = current[1]

        x = min(max(x, work_left), work_right - width)
        y = min(max(y, work_top), work_bottom - height)
        desired = (x, y, width, height)
        if current == desired:
            self._placed = True
            return True

        setter = getattr(window, "geometry", None)
        if not callable(setter):
            return False
        try:
            setter(f"{width}x{height}{x:+d}{y:+d}")
            _safe_call(window, "update_idletasks")
        except Exception:
            return False

        applied = self._window_rect(window)
        if applied is None:
            return False
        applied_x, applied_y, applied_width, applied_height = applied
        valid = bool(
            applied_width >= width
            and applied_height >= height
            and applied_x >= work_left
            and applied_y >= work_top
            and applied_x + applied_width <= work_right
            and applied_y + applied_height <= work_bottom
        )
        if valid:
            self._placed = True
        return valid

    @staticmethod
    def _window_rect(window: Any) -> tuple[int, int, int, int] | None:
        try:
            width = int(window.winfo_width())
            height = int(window.winfo_height())
            x = int(window.winfo_x())
            y = int(window.winfo_y())
        except Exception:
            return None
        if width <= 0 or height <= 0:
            return None
        return x, y, width, height

    @staticmethod
    def _window_exists(window: Any) -> bool:
        if window is None:
            return False
        getter = getattr(window, "winfo_exists", None)
        if not callable(getter):
            return True
        try:
            return bool(getter())
        except Exception:
            return False

    @staticmethod
    def _window_is_mapped(window: Any) -> bool:
        if window is None:
            return False
        getter = getattr(window, "winfo_ismapped", None)
        if not callable(getter):
            return False
        try:
            return bool(getter())
        except Exception:
            return False


def _fallback_work_area(window: Any) -> tuple[int, int, int, int]:
    screen_width = _positive_int_call(window, "winfo_screenwidth", 1280)
    screen_height = _positive_int_call(window, "winfo_screenheight", 720)
    left = _int_call(window, "winfo_vrootx", 0)
    top = _int_call(window, "winfo_vrooty", 0)
    width = _int_call(window, "winfo_vrootwidth", screen_width)
    height = _int_call(window, "winfo_vrootheight", screen_height)
    if width <= 0:
        width = screen_width
    if height <= 0:
        height = screen_height
    return left, top, left + width, top + height


def _monitor_work_area(user32: Any, monitor: Any) -> tuple[int, int, int, int] | None:
    try:
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32.GetMonitorInfoW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(MonitorInfo),
        ]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(MonitorInfo)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            result = (
                int(work.left),
                int(work.top),
                int(work.right),
                int(work.bottom),
            )
            if result[2] > result[0] and result[3] > result[1]:
                return result
    except Exception:
        pass
    return None


def _work_area_for_window(window: Any, root: Any) -> tuple[int, int, int, int]:
    """Return the nearest monitor work area in virtual-screen coordinates."""

    try:
        import ctypes
        from ctypes import wintypes

        winfo_id = getattr(window, "winfo_id", None)
        if callable(winfo_id):
            hwnd = int(winfo_id())
            if hwnd > 0:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.MonitorFromWindow.argtypes = [
                    wintypes.HWND,
                    wintypes.DWORD,
                ]
                user32.MonitorFromWindow.restype = wintypes.HANDLE
                monitor = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)
                result = _monitor_work_area(user32, monitor)
                if result is not None:
                    return result
    except Exception:
        pass
    _ = root
    return _fallback_work_area(window)


def _work_area_for_point(
    x: int,
    y: int,
    window: Any,
    root: Any,
) -> tuple[int, int, int, int]:
    """Return the work area containing the current pointer."""

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        monitor = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)
        result = _monitor_work_area(user32, monitor)
        if result is not None:
            return result
    except Exception:
        pass
    return _work_area_for_window(window, root)


def _pointer_position(window: Any, root: Any) -> tuple[int, int]:
    try:
        return int(window.winfo_pointerx()), int(window.winfo_pointery())
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        point = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(point)):
            return int(point.x), int(point.y)
    except Exception:
        pass
    return (
        _int_call(root, "winfo_rootx", 0) + _POINTER_OFFSET_PX,
        _int_call(root, "winfo_rooty", 0) + _POINTER_OFFSET_PX,
    )


def _native_window_handle(window: Any) -> int:
    try:
        import ctypes
        from ctypes import wintypes

        winfo_id = getattr(window, "winfo_id", None)
        if not callable(winfo_id):
            return 0
        client_hwnd = int(winfo_id())
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        return int(user32.GetAncestor(wintypes.HWND(client_hwnd), 2) or client_hwnd)
    except Exception:
        return 0


def _window_is_foreground(window: Any) -> bool:
    hwnd = _native_window_handle(window)
    if hwnd <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground <= 0:
            return False
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        foreground_root = int(
            user32.GetAncestor(wintypes.HWND(foreground), 2) or foreground
        )
        return foreground_root == hwnd
    except Exception:
        return False


def _show_window_activated(window: Any) -> bool:
    """Show a short-lived topmost panel and request native foreground ownership."""

    _safe_call(window, "deiconify")
    _safe_call(window, "attributes", "-topmost", True)
    _safe_call(window, "lift")
    _safe_call(window, "focus_force")
    _safe_call(window, "update_idletasks")
    hwnd = _native_window_handle(window)
    if hwnd <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND
        user32.SetFocus.argtypes = [wintypes.HWND]
        user32.SetFocus.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        sw_show = 5
        swp_nosize = 0x0001
        swp_nomove = 0x0002
        swp_showwindow = 0x0040
        swp_noownerzorder = 0x0200
        native = wintypes.HWND(hwnd)
        user32.ShowWindow(native, sw_show)
        user32.SetWindowPos(
            native,
            wintypes.HWND(-1),
            0,
            0,
            0,
            0,
            swp_nosize | swp_nomove | swp_showwindow | swp_noownerzorder,
        )

        foreground = int(user32.GetForegroundWindow() or 0)
        foreground_thread = 0
        current_thread = int(kernel32.GetCurrentThreadId() or 0)
        if foreground > 0:
            process_id = wintypes.DWORD()
            foreground_thread = int(
                user32.GetWindowThreadProcessId(
                    wintypes.HWND(foreground),
                    ctypes.byref(process_id),
                )
                or 0
            )
        attached = False
        if (
            foreground_thread > 0
            and current_thread > 0
            and foreground_thread != current_thread
        ):
            attached = bool(
                user32.AttachThreadInput(
                    wintypes.DWORD(current_thread),
                    wintypes.DWORD(foreground_thread),
                    True,
                )
            )
        try:
            user32.BringWindowToTop(native)
            user32.SetActiveWindow(native)
            user32.SetFocus(native)
            user32.SetForegroundWindow(native)
        finally:
            if attached:
                user32.AttachThreadInput(
                    wintypes.DWORD(current_thread),
                    wintypes.DWORD(foreground_thread),
                    False,
                )
    except Exception:
        return False
    _safe_call(window, "focus_force")
    return _window_is_foreground(window)


def _show_window_without_activation(window: Any) -> bool:
    """Show a native Tk top-level without moving keyboard focus."""

    try:
        import ctypes
        from ctypes import wintypes

        hwnd = _native_window_handle(window)
        if hwnd <= 0:
            return False
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL

        sw_shownoactivate = 4
        swp_nosize = 0x0001
        swp_nomove = 0x0002
        swp_noactivate = 0x0010
        swp_showwindow = 0x0040
        swp_noownerzorder = 0x0200
        native = wintypes.HWND(hwnd)
        user32.ShowWindow(native, sw_shownoactivate)
        return bool(
            user32.SetWindowPos(
                native,
                wintypes.HWND(-1),
                0,
                0,
                0,
                0,
                swp_nosize
                | swp_nomove
                | swp_noactivate
                | swp_showwindow
                | swp_noownerzorder,
            )
        )
    except Exception:
        return False


def _set_entry_text(entry: Any, text: str) -> None:
    _safe_call(entry, "delete", 0, "end")
    _safe_call(entry, "insert", 0, str(text))


def _require_string(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    return value


def _require_nonempty_string(value: object, *, name: str) -> str:
    text = _require_string(value, name=name)
    if not text.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return text


def _require_iso_date_key(value: object) -> str:
    text = _require_string(value, name="date_key")
    if _DATE_KEY_PATTERN.fullmatch(text) is None:
        raise ValueError("date_key must use strict YYYY-MM-DD format")
    try:
        parsed = date_type.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("date_key must be a valid calendar date") from exc
    if parsed.isoformat() != text:
        raise ValueError("date_key must use strict YYYY-MM-DD format")
    return text


def _require_color(value: object) -> str:
    return _require_nonempty_string(value, name="color")


def _safe_call(target: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(target, method_name, None)
    if not callable(method):
        return None
    try:
        return method(*args, **kwargs)
    except Exception:
        return None


def _positive_int_call(target: Any, method_name: str, default: int) -> int:
    value = _int_call(target, method_name, default)
    return value if value > 0 else int(default)


def _int_call(target: Any, method_name: str, default: int) -> int:
    method = getattr(target, method_name, None)
    if not callable(method):
        return int(default)
    try:
        return int(method())
    except Exception:
        return int(default)


__all__ = [
    "WorktimeActivityPrompt",
    "WorktimePanelDayRow",
    "WorktimePanelLine",
    "WorktimePanelModel",
    "WorktimeQuickPanel",
]
