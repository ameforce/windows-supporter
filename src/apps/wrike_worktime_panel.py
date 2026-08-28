"""Reusable nonmodal Tk panel for Wrike worktime progress."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Any, Callable


_REFRESH_INTERVAL_MS = 1_000
_COUNTDOWN_INTERVAL_MS = 200
_DEFAULT_IDLE_TIMEOUT_MS = 6_000
_MIN_IDLE_TIMEOUT_MS = 1_200
_POINTER_OFFSET_PX = 16
_HHMM_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_TARGET_HHMM_PATTERN = re.compile(r"(?:[01]\d|2[0-4]):[0-5]\d")

_BG = "#F3F4F6"
_CARD_BG = "#FFFFFF"
_BORDER = "#E5E7EB"
_HOVER_BORDER = "#2563EB"
_TEXT = "#111827"
_MUTED = "#6B7280"
_TODAY_BG = "#DBEAFE"
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
    summary: str
    today: bool
    color: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.weekday, name="weekday")
        _require_nonempty_string(self.date, name="date")
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
    has_clock_in: bool
    break_active: bool
    rows: tuple[WorktimePanelDayRow, ...]
    prompt: WorktimeActivityPrompt | None = None

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
        if type(self.has_clock_in) is not bool:
            raise TypeError("has_clock_in must be a bool")
        if type(self.break_active) is not bool:
            raise TypeError("break_active must be a bool")
        if not isinstance(self.rows, tuple):
            raise TypeError("rows must be an immutable tuple")
        if len(self.rows) != 7:
            raise ValueError("rows must contain exactly seven Monday-through-Sunday entries")
        if any(not isinstance(item, WorktimePanelDayRow) for item in self.rows):
            raise TypeError("rows must contain only WorktimePanelDayRow values")
        if self.prompt is not None and not isinstance(
            self.prompt, WorktimeActivityPrompt
        ):
            raise TypeError("prompt must be a WorktimeActivityPrompt or None")


class WorktimeQuickPanel:
    """Singleton-style nonmodal panel backed by one reusable ``Toplevel``.

    Prompt accept/edit callbacks receive the detected ``HH:MM`` string. All
    other callbacks are called without arguments. Callback completion is
    followed by an immediate provider read so synchronous state changes become
    visible without waiting for the next one-second refresh.
    """

    def __init__(
        self,
        root: Any,
        model_provider: Callable[[], WorktimePanelModel],
        refresh: Callable[[], None],
        clock_in_now: Callable[[], None],
        edit_clock_in: Callable[[], None],
        edit_plan: Callable[[int], bool],
        toggle_break: Callable[[], None],
        open_settings: Callable[[], None],
        prompt_accept: Callable[[str], None],
        prompt_edit: Callable[[str], None],
        prompt_snooze: Callable[[], None],
        prompt_skip: Callable[[], None],
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
        self._monotonic = monotonic
        self._tk = tk_module

        self._window = None
        self._shell = None
        self._content = None
        self._model: WorktimePanelModel | None = None
        self._structure_signature: tuple[bool, int] | None = None
        self._widgets: dict[str, Any] = {}
        self._refresh_after_id = None
        self._refresh_token: object | None = None
        self._dismiss_after_id = None
        self._dismiss_token: object | None = None
        self._countdown_after_id = None
        self._countdown_token: object | None = None
        self._dismiss_deadline: float | None = None
        self._interaction_depth = 0
        self._target_editor_active = False
        self._idle_timeout_ms = self._clamp_idle_timeout_ms(idle_timeout_ms)
        self._pointer_inside = False
        self._geometry_retry_pending = False
        self._visible = False
        self._placed = False
        self._destroyed = False

    def show(self, activate: bool = True) -> bool:
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
        self._close_target_editor(reconcile=False)
        self._set_pointer_inside(False)
        self._cancel_timers()
        window = self._window
        if window is not None and self._window_exists(window):
            _safe_call(window, "withdraw")

    def toggle(self, activate: bool = True) -> None:
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
        return True

    def destroy(self) -> None:
        """Cancel pending work and permanently destroy the panel once."""

        if self._destroyed:
            return
        self._destroyed = True
        self._visible = False
        self._target_editor_active = False
        self._set_pointer_inside(False)
        self._cancel_timers()

        window = self._window
        self._window = None
        self._shell = None
        self._content = None
        self._model = None
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
    def _model_structure_signature(model: WorktimePanelModel) -> tuple[bool, int]:
        return model.prompt is not None, len(model.today_lines)

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
            self._structure_signature = None
            self._widgets = {}
            self._placed = False
            self._geometry_retry_pending = False
            self._destroyed = True
            self._visible = False
            self._target_editor_active = False
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
        if self._pointer_inside:
            self._cancel_dismiss_timer()
        elif changed:
            self._reset_dismiss_timer()
        else:
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
        if self._target_editor_active:
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
            or self._target_editor_active
            or self._destroyed
            or not self._visible
            or self._pointer_inside
        ):
            self._update_countdown_label()
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            self._update_countdown_label()
            return
        token = object()
        self._dismiss_token = token
        self._dismiss_deadline = self._monotonic() + self._idle_timeout_ms / 1000.0
        try:
            self._dismiss_after_id = scheduler(
                self._idle_timeout_ms,
                lambda current=token: self._dismiss_tick(current),
            )
        except Exception:
            self._dismiss_after_id = None
            self._dismiss_token = None
            self._dismiss_deadline = None
        self._update_countdown_label()
        self._schedule_countdown()

    def _dismiss_tick(self, token: object) -> None:
        if token is not self._dismiss_token:
            return
        self._dismiss_after_id = None
        self._dismiss_token = None
        self._dismiss_deadline = None
        self._cancel_countdown()
        if (
            self._interaction_depth > 0
            or self._target_editor_active
            or self._destroyed
            or not self._visible
            or self._pointer_inside
        ):
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
        if self._target_editor_active:
            return "편집 중 · 자동 닫힘 일시정지"
        if self._interaction_depth > 0:
            return "작업 중 · 자동 닫힘 일시정지"
        if self._pointer_inside:
            return "마우스 호버 중 · 자동 닫힘 일시정지"
        if self._dismiss_deadline is None:
            return "자동 닫힘 대기 중"
        remaining = max(0.0, self._dismiss_deadline - self._monotonic())
        return f"{max(1, math.ceil(remaining))}초 후 닫힘"

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

    def _render_structure(self, model: WorktimePanelModel) -> None:
        tk = self._tk
        content = self._content
        if tk is None or content is None:
            return
        self._clear_content()
        self._widgets = {}

        compact = model.prompt is not None
        section_gap = 4 if compact else 8
        title_padding = (4, 1) if compact else (7, 2)
        row_padding = 1 if compact else 2
        _safe_call(content, "pack_configure", padx=8, pady=4 if compact else 8)

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
        sync_label.pack(fill="x", padx=10, pady=(0, 4 if compact else 7))

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
        tk.Frame(today_card, bg=_CARD_BG, height=2 if compact else 5).pack(fill="x")

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
            pady=(4, 1) if compact else (7, 3),
        )
        row_widgets = []
        for row in model.rows:
            row_bg = _TODAY_BG if row.today else _CARD_BG
            row_frame = tk.Frame(week_card, bg=row_bg)
            row_frame.pack(fill="x", padx=8, pady=0)
            weekday_label = tk.Label(
                row_frame,
                text=row.weekday,
                width=4,
                bg=row_bg,
                fg=_TEXT,
                anchor="w",
                font=("Segoe UI", 9, "bold" if row.today else "normal"),
            )
            weekday_label.pack(side="left", padx=(4, 2), pady=row_padding)
            date_label = tk.Label(
                row_frame,
                text=row.date,
                width=8,
                bg=row_bg,
                fg=_MUTED,
                anchor="w",
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
                font=("Segoe UI", 9, "bold" if row.today else "normal"),
            )
            summary_label.pack(side="left", fill="x", expand=True, pady=row_padding)
            today_label = tk.Label(
                row_frame,
                text="오늘" if row.today else "",
                bg=row_bg,
                fg="#1D4ED8",
                font=("Segoe UI", 8, "bold"),
            )
            today_label.pack(side="right", padx=4, pady=row_padding)
            row_widgets.append(
                (row_frame, weekday_label, date_label, summary_label, today_label)
            )

        target_editor = tk.Frame(
            content,
            bg=_CARD_BG,
            highlightthickness=1,
            highlightbackground=_HOVER_BORDER,
        )
        target_row = tk.Frame(target_editor, bg=_CARD_BG)
        target_row.pack(fill="x", padx=10, pady=(7, 3))
        tk.Label(
            target_row,
            text="오늘 목표 순근무 시간",
            bg=_CARD_BG,
            fg=_TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        target_entry = tk.Entry(
            target_row,
            width=8,
            bg=_CARD_BG,
            fg=_TEXT,
            justify="center",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
        )
        target_entry.pack(side="left", padx=(10, 4))
        _set_entry_text(target_entry, self._format_target_minutes(model.target_minutes))
        tk.Label(
            target_row,
            text="HH:MM (00:00–24:00)",
            bg=_CARD_BG,
            fg=_MUTED,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(2, 8))
        self._button(target_row, "저장", self._save_target_editor_command)
        self._button(target_row, "취소", self._cancel_target_editor_command)
        target_error = tk.Label(
            target_editor,
            text="",
            bg=_CARD_BG,
            fg="#DC2626",
            anchor="w",
            font=("Segoe UI", 8),
        )
        target_error.pack(fill="x", padx=10, pady=(0, 6))
        self._bind_additive(target_entry, "<Return>", self._save_target_editor_event)
        self._bind_additive(target_entry, "<Escape>", self._cancel_target_editor_event)

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
        plan_button = self._button(actions, "계획 수정", self._edit_plan_command)
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
            "refresh_button": refresh_button,
            "clock_button": clock_button,
            "break_button": break_button,
            "plan_button": plan_button,
            "settings_button": settings_button,
            "target_editor": target_editor,
            "target_entry": target_entry,
            "target_error": target_error,
            "actions": actions,
            "countdown": countdown_label,
            "prompt_label": prompt_label,
            "prompt_buttons": prompt_buttons,
        }
        if self._target_editor_active:
            self._show_target_editor(reset_value=False)
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
            row_bg = _TODAY_BG if row.today else _CARD_BG
            emphasis = "bold" if row.today else "normal"
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

        widgets["clock_button"].configure(
            text="출근 수정" if model.has_clock_in else "지금 출근"
        )
        widgets["break_button"].configure(
            text="휴게 종료" if model.break_active else "휴게 시작"
        )
        if not self._target_editor_active:
            _set_entry_text(
                widgets["target_entry"],
                self._format_target_minutes(model.target_minutes),
            )
        self._update_countdown_label()

        if model.prompt is not None:
            prompt = model.prompt
            widgets["prompt_label"].configure(
                text=f"{prompt.detected_time} 활동을 출근으로 반영할까요?"
            )
            accept_button = widgets["prompt_buttons"][0]
            accept_button.configure(text=f"{prompt.detected_time}으로 출근")

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

    def _show_target_editor(self, *, reset_value: bool = True) -> None:
        editor = self._widgets.get("target_editor")
        entry = self._widgets.get("target_entry")
        actions = self._widgets.get("actions")
        if editor is None or entry is None:
            return
        self._target_editor_active = True
        if reset_value and self._model is not None:
            _set_entry_text(
                entry,
                self._format_target_minutes(self._model.target_minutes),
            )
        _safe_call(self._widgets.get("target_error"), "configure", text="")
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

    def _close_target_editor(self, *, reconcile: bool = True) -> None:
        if not self._target_editor_active:
            return
        self._target_editor_active = False
        _safe_call(self._widgets.get("target_editor"), "pack_forget")
        _safe_call(self._widgets.get("target_error"), "configure", text="")
        if reconcile and self._visible:
            self._geometry_retry_pending = not self._reconcile_geometry(
                resize_to_request=True
            )
            self._reset_dismiss_timer()
        else:
            self._update_countdown_label()

    def _save_target_editor_command(self) -> None:
        entry = self._widgets.get("target_entry")
        getter = getattr(entry, "get", None)
        raw_value = getter() if callable(getter) else ""
        target_minutes, error = self._parse_target_minutes(raw_value)
        if error is not None or target_minutes is None:
            _safe_call(
                self._widgets.get("target_error"),
                "configure",
                text=error or "근무시간을 확인해 주세요.",
            )
            return

        self._interaction_depth += 1
        self._cancel_dismiss_timer()
        saved = False
        try:
            saved = self._on_edit_plan(target_minutes) is True
            self.refresh_now()
        except Exception:
            saved = False
        finally:
            self._interaction_depth = max(0, self._interaction_depth - 1)
        if saved:
            self._close_target_editor(reconcile=True)
        else:
            _safe_call(
                self._widgets.get("target_error"),
                "configure",
                text="오늘 목표를 저장하지 못했습니다.",
            )
            self._update_countdown_label()

    def _cancel_target_editor_command(self) -> None:
        self._close_target_editor(reconcile=True)

    def _save_target_editor_event(self, _event: Any = None) -> str:
        self._save_target_editor_command()
        return "break"

    def _cancel_target_editor_event(self, _event: Any = None) -> str:
        self._cancel_target_editor_command()
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
        self._run_command(self._on_edit_clock_in)

    def _edit_plan_command(self) -> None:
        if self._target_editor_active:
            entry = self._widgets.get("target_entry")
            _safe_call(entry, "focus_set")
            _safe_call(entry, "selection_range", 0, "end")
            return
        self._show_target_editor(reset_value=True)

    def _toggle_break_command(self) -> None:
        self._run_command(self._on_toggle_break)

    def _settings_command(self) -> None:
        self._run_command(self._on_open_settings)

    def _prompt_accept_command(self, detected_time: str) -> None:
        self._run_command(self._on_prompt_accept, detected_time)

    def _prompt_edit_command(self, detected_time: str) -> None:
        self._run_command(self._on_prompt_edit, detected_time)

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
