"""Reusable nonmodal Tk panel for Wrike worktime progress."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable


_REFRESH_INTERVAL_MS = 1_000
_DEFAULT_IDLE_TIMEOUT_MS = 6_000
_MIN_IDLE_TIMEOUT_MS = 1_200
_HHMM_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

_BG = "#F3F4F6"
_CARD_BG = "#FFFFFF"
_BORDER = "#E5E7EB"
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
        edit_plan: Callable[[], None],
        toggle_break: Callable[[], None],
        open_settings: Callable[[], None],
        prompt_accept: Callable[[str], None],
        prompt_edit: Callable[[str], None],
        prompt_snooze: Callable[[], None],
        prompt_skip: Callable[[], None],
        tk_module: Any | None = None,
        idle_timeout_ms: int = _DEFAULT_IDLE_TIMEOUT_MS,
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
        self._tk = tk_module

        self._window = None
        self._content = None
        self._model: WorktimePanelModel | None = None
        self._structure_signature: tuple[bool, int] | None = None
        self._widgets: dict[str, Any] = {}
        self._refresh_after_id = None
        self._refresh_token: object | None = None
        self._dismiss_after_id = None
        self._dismiss_token: object | None = None
        self._interaction_depth = 0
        self._idle_timeout_ms = self._clamp_idle_timeout_ms(idle_timeout_ms)
        self._pointer_inside = False
        self._geometry_retry_pending = False
        self._visible = False
        self._placed = False
        self._destroyed = False

    def show(self, activate: bool = True) -> bool:
        """Show the reusable panel and verify native mapped state."""

        if type(activate) is not bool:
            raise TypeError("activate must be a bool")
        if self._destroyed:
            return False
        window = self._ensure_window()
        if window is None:
            return False

        was_visible = self._visible
        self.refresh_now()
        if activate:
            _safe_call(window, "deiconify")
            _safe_call(window, "lift")
            _safe_call(window, "focus_force")
        elif not _show_window_without_activation(window):
            return self._fail_show(window)
        _safe_call(window, "update_idletasks")
        if not self._window_is_mapped(window):
            return self._fail_show(window)

        self._visible = True
        if not was_visible:
            self._pointer_inside = False
        self._schedule_refresh()
        self._reset_dismiss_timer()
        return True

    def _fail_show(self, window: Any) -> bool:
        self._visible = False
        self._pointer_inside = False
        self._cancel_timers()
        if self._window_exists(window):
            _safe_call(window, "withdraw")
        return False

    def hide(self) -> None:
        """Hide the panel without destroying its ``Toplevel``."""

        self._visible = False
        self._pointer_inside = False
        self._cancel_timers()
        window = self._window
        if window is not None and self._window_exists(window):
            _safe_call(window, "withdraw")

    def toggle(self, activate: bool = True) -> None:
        """Hide a visible panel or show its existing window."""

        if self.is_visible():
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
        self._pointer_inside = False
        self._cancel_timers()

        window = self._window
        self._window = None
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
            self._content = None
            self._model = None
            self._structure_signature = None
            self._widgets = {}
            self._placed = False
            self._geometry_retry_pending = False
            self._destroyed = True
            self._visible = False
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
        _safe_call(window, "resizable", True, True)
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
            content = tk.Frame(window, bg=_BG)
            content.pack(fill="both", expand=True, padx=8, pady=8)
        except Exception:
            _safe_call(window, "destroy")
            self._window = None
            return None
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

    def _on_pointer_enter(self, _event: Any = None) -> None:
        self._pointer_inside = True
        self._cancel_dismiss_timer()

    def _on_pointer_leave(self, _event: Any = None) -> None:
        self._pointer_inside = False
        self._reset_dismiss_timer()

    def _on_pointer_activity(self, _event: Any = None) -> None:
        self._pointer_inside = True
        self._cancel_dismiss_timer()

    def _on_key_activity(self, event: Any = None) -> None:
        if str(getattr(event, "keysym", "")).lower() == "escape":
            return
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
            or self._destroyed
            or not self._visible
            or self._pointer_inside
        ):
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        token = object()
        self._dismiss_token = token
        try:
            self._dismiss_after_id = scheduler(
                self._idle_timeout_ms,
                lambda current=token: self._dismiss_tick(current),
            )
        except Exception:
            self._dismiss_after_id = None
            self._dismiss_token = None

    def _dismiss_tick(self, token: object) -> None:
        if token is not self._dismiss_token:
            return
        self._dismiss_after_id = None
        self._dismiss_token = None
        if (
            self._interaction_depth > 0
            or self._destroyed
            or not self._visible
            or self._pointer_inside
        ):
            return
        self.hide()

    def _cancel_dismiss_timer(self) -> None:
        after_id = self._dismiss_after_id
        self._dismiss_after_id = None
        self._dismiss_token = None
        if after_id is None:
            return
        canceller = getattr(self._root, "after_cancel", None)
        if callable(canceller):
            try:
                canceller(after_id)
            except Exception:
                pass

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

        actions = tk.Frame(content, bg=_BG)
        actions.pack(fill="x", pady=(0, section_gap))
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
            "prompt_label": prompt_label,
            "prompt_buttons": prompt_buttons,
        }

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
        self._run_command(self._on_edit_plan)

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

    def _reconcile_geometry(self) -> bool:
        """Preserve a valid user rectangle while growing/reclamping as needed."""

        window = self._window
        if window is None or not self._window_exists(window):
            return False
        _safe_call(window, "update_idletasks")

        work_left, work_top, work_right, work_bottom = _work_area_for_window(
            window,
            self._root,
        )
        work_width = max(1, work_right - work_left)
        work_height = max(1, work_bottom - work_top)
        requested_width = _positive_int_call(window, "winfo_reqwidth", 680)
        requested_height = _positive_int_call(window, "winfo_reqheight", 480)
        current = self._window_rect(window)

        if self._placed and current is not None:
            current_x, current_y, current_width, current_height = current
            width = min(
                max(current_width, requested_width, 680),
                work_width,
            )
            height = min(
                max(current_height, requested_height, 480),
                work_height,
            )
            x = current_x
            y = current_y
        else:
            width = min(max(requested_width, 680), work_width)
            height = min(max(requested_height, 480), work_height)
            x = _int_call(self._root, "winfo_rootx", work_left) + 24
            y = _int_call(self._root, "winfo_rooty", work_top) + 24

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


def _work_area_for_window(window: Any, root: Any) -> tuple[int, int, int, int]:
    """Return the nearest monitor work area in virtual-screen coordinates."""

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
                user32.GetMonitorInfoW.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(MonitorInfo),
                ]
                user32.GetMonitorInfoW.restype = wintypes.BOOL
                monitor = user32.MonitorFromWindow(
                    wintypes.HWND(hwnd),
                    2,
                )
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
    _ = root
    return left, top, left + width, top + height


def _show_window_without_activation(window: Any) -> bool:
    """Show a native Tk top-level without moving focus or changing geometry."""

    try:
        import ctypes
        from ctypes import wintypes

        winfo_id = getattr(window, "winfo_id", None)
        if not callable(winfo_id):
            return False
        client_hwnd = int(winfo_id())
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
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

        hwnd = int(user32.GetAncestor(wintypes.HWND(client_hwnd), 2) or client_hwnd)
        if hwnd <= 0:
            return False
        sw_shownoactivate = 4
        swp_nosize = 0x0001
        swp_nomove = 0x0002
        swp_noactivate = 0x0010
        swp_showwindow = 0x0040
        swp_noownerzorder = 0x0200
        user32.ShowWindow(wintypes.HWND(hwnd), sw_shownoactivate)
        return bool(
            user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(0),
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
