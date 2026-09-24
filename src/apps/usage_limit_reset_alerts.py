from __future__ import annotations

import ctypes
from typing import Any, Callable


RESET_LINE_COLOR = "#16A34A"
DEFAULT_INPUT_POLL_MS = 1000


def _last_input_tick() -> int | None:
    try:

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwTime", ctypes.c_uint),
            ]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return int(info.dwTime)
    except Exception:
        return None


def _play_fanfare() -> bool:
    from src.utils.reset_fanfare import play_reset_fanfare

    return bool(play_reset_fanfare())


def _show_tooltip(root: Any, lines: list[tuple[str, str | None]], duration_ms: int) -> Any:
    from src.utils.ToolTip import ToolTip

    tooltip = ToolTip(
        root,
        "",
        bind_events=False,
        auto_hide_ms=max(1200, int(duration_ms)),
        keep_on_hover=True,
        lines=lines,
    )
    tooltip.show_tooltip()
    return tooltip


class UsageLimitResetAlert:
    """Present usage-limit reset alerts (fanfare + alert tooltip) once.

    Mirrors the Codex monitor's presentation contract for providers that do
    not own it: alerts are merged per metric, shown on the UI thread, and
    held until the user is active again (a new input tick) so a reset that
    happens while the user is away is not missed.
    """

    def __init__(
        self,
        *,
        title: str,
        post_ui: Callable[[Callable[[], None]], bool],
        get_root: Callable[[], Any],
        get_duration_ms: Callable[[], int],
        sound_enabled: Callable[[], bool] = lambda: True,
        input_tick: Callable[[], int | None] = _last_input_tick,
        play_sound: Callable[[], bool] = _play_fanfare,
        show_tooltip: Callable[[Any, list[tuple[str, str | None]], int], Any] = _show_tooltip,
        poll_ms: int = DEFAULT_INPUT_POLL_MS,
    ) -> None:
        self._title = str(title or "")
        self._post_ui = post_ui
        self._get_root = get_root
        self._get_duration_ms = get_duration_ms
        self._sound_enabled = sound_enabled
        self._input_tick = input_tick
        self._play_sound = play_sound
        self._show_tooltip = show_tooltip
        self._poll_ms = max(50, int(poll_ms))
        self._pending: dict[str, Any] = {}
        self._baseline_tick: int | None = None
        self._after_id: Any = None
        self._active_tooltip: Any = None

    def submit(self, resets: list[Any]) -> bool:
        """Queue resets from any thread; presentation happens on the UI thread."""
        items = [item for item in (resets or []) if str(getattr(item, "key", "") or "")]
        if not items:
            return False
        try:
            return bool(self._post_ui(lambda: self._queue(items)))
        except Exception:
            return False

    def _queue(self, resets: list[Any]) -> None:
        was_empty = not self._pending
        for item in resets:
            self._pending[str(item.key)] = item
        tick = self._input_tick()
        if tick is None:
            self._flush()
            return
        if was_empty or self._baseline_tick is None:
            self._baseline_tick = int(tick)
        elif int(tick) != int(self._baseline_tick):
            self._flush()
            return
        self._schedule_poll()
        return

    def _schedule_poll(self) -> None:
        if self._after_id is not None:
            return
        root = self._get_root()
        if root is None:
            self._flush()
            return
        try:
            self._after_id = root.after(self._poll_ms, self._poll)
        except Exception:
            self._after_id = None
            self._flush()
        return

    def _poll(self) -> None:
        self._after_id = None
        if not self._pending:
            return
        tick = self._input_tick()
        if tick is None or self._baseline_tick is None or int(tick) != int(self._baseline_tick):
            self._flush()
            return
        self._schedule_poll()
        return

    def _flush(self) -> None:
        resets = list(self._pending.values())
        self._pending = {}
        self._baseline_tick = None
        if not resets:
            return
        if bool(self._sound_enabled()):
            try:
                self._play_sound()
            except Exception:
                pass
        root = self._get_root()
        if root is None:
            return
        lines: list[tuple[str, str | None]] = [(f"{self._title} 사용 한도 초기화", None)]
        for item in resets:
            label = str(getattr(item, "label", "") or getattr(item, "key", ""))
            lines.append((f"{label} 초기화됨", RESET_LINE_COLOR))
        previous = self._active_tooltip
        self._active_tooltip = None
        if previous is not None:
            try:
                previous.hide_tooltip()
            except Exception:
                pass
        try:
            self._active_tooltip = self._show_tooltip(root, lines, int(self._get_duration_ms()))
        except Exception:
            self._active_tooltip = None
        return
