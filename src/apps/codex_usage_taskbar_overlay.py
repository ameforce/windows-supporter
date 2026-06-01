from __future__ import annotations

import ctypes
import re
import time
from datetime import datetime
from typing import Any, Callable

try:
    from ctypes import wintypes
except Exception:  # pragma: no cover - non-CPython fallback.
    wintypes = None

try:
    import tkinter as tk
except Exception:  # pragma: no cover - exercised on GUI-less imports only.
    tk = None

try:
    import win32con
    import win32gui
except Exception:  # pragma: no cover - non-Windows fallback.
    win32con = None
    win32gui = None


_DEFAULT_GEOMETRY = {
    "x": 0,
    "y": 0,
    "width": 760,
    "height": 38,
    "orientation": "bottom",
    "visible": True,
}

_EMPTY_SLOT_PADDING_PX = 8
_MIN_EMPTY_SLOT_WIDTH_PX = 220
_TASKBAR_SAMPLE_STEP_PX = 4
_OCCUPIED_DILATION_PX = 24
_FLASH_DURATION_SEC = 2.8
_FLASH_TICK_MS = 320
_KEEPALIVE_TICK_MS = 250
_GWLP_HWNDPARENT = -8
_TASKBAR_METRICS = (
    ("five_hour_limit", "5h"),
    ("weekly_limit", "7d"),
)
_RESET_KEY_BY_METRIC = {
    "five_hour_limit": "five_hour_limit_reset_at",
    "weekly_limit": "weekly_limit_reset_at",
}
_RESET_WINDOW_BY_METRIC = {
    "five_hour_limit": {
        "urgent_seconds": 30 * 60,
        "soon_seconds": 60 * 60,
        "far_seconds": 2 * 60 * 60,
        "very_far_seconds": 4 * 60 * 60,
    },
    "weekly_limit": {
        "urgent_seconds": 6 * 60 * 60,
        "soon_seconds": 24 * 60 * 60,
        "far_seconds": 3 * 24 * 60 * 60,
        "very_far_seconds": 5 * 24 * 60 * 60,
    },
}
_RESET_DETAIL_COLUMN_WIDTH_PX = 42
_RESET_SHORT_COLUMN_WIDTH_PX = 24


def build_codex_usage_taskbar_overlay_model(
    runtime_status: dict[str, Any],
    geometry: dict[str, int | str] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    runtime = runtime_status if isinstance(runtime_status, dict) else {}
    accounts = runtime.get("accounts")
    if not isinstance(accounts, list):
        accounts = []

    bars = []
    manager_enabled = bool(runtime.get("enabled", True))
    for index, raw in enumerate(accounts[:2], start=1):
        if not isinstance(raw, dict):
            continue
        account_enabled = bool(raw.get("enabled", True))
        snapshot = raw.get("last_snapshot", {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        runtime_info = raw.get("runtime", {})
        if not isinstance(runtime_info, dict):
            runtime_info = {}
        status = _account_status(account_enabled, runtime_info, snapshot)
        metrics = []
        for metric_key, short_label in _TASKBAR_METRICS:
            reset_key = _RESET_KEY_BY_METRIC.get(metric_key, "")
            metrics.append(
                _build_metric(
                    key=metric_key,
                    short_label=short_label,
                    raw_value=snapshot.get(metric_key),
                    reset_at_value=snapshot.get(reset_key),
                    account_state=status["state"],
                    now=now,
                )
            )
        five_hour_metric = metrics[0]
        row_state = (
            str(five_hour_metric["state"])
            if status["state"] == "ready"
            else str(status["state"])
        )
        bars.append(
            {
                "id": str(raw.get("id") or f"account_{index}"),
                "label": str(raw.get("label") or f"Codex {index}"),
                "enabled": bool(account_enabled),
                "percent": int(five_hour_metric["percent"]),
                "value_text": str(five_hour_metric["value_text"]),
                "state": row_state,
                "color": str(five_hour_metric["color"]),
                "status_text": status["text"],
                "status_color": status["color"],
                "metrics": metrics,
            }
        )

    geometry_dict = dict(_DEFAULT_GEOMETRY if geometry is None else geometry)
    geometry_visible = bool(geometry_dict.get("visible", True))
    visible = bool(
        geometry_visible and manager_enabled and any(bool(bar["enabled"]) for bar in bars)
    )
    collecting = bool(runtime.get("collect_inflight", False)) or any(
        _account_collecting(account) for account in accounts if isinstance(account, dict)
    )
    return {
        "visible": visible,
        "state": "collecting" if collecting else "ready",
        "geometry": geometry_dict,
        "bars": bars,
    }


def calculate_taskbar_overlay_geometry(
    screen_width: int,
    screen_height: int,
    work_area: tuple[int, int, int, int] | dict[str, int] | None,
    *,
    occupied_spans: list[tuple[int, int]] | None = None,
) -> dict[str, int | str]:
    screen_width = max(320, int(screen_width or 0))
    screen_height = max(240, int(screen_height or 0))
    left, top, right, bottom = _normalize_work_area(work_area, screen_width, screen_height)
    width = min(max(640, int(screen_width * 0.46)), 900, screen_width - 24)
    height = 38

    if bottom < screen_height:
        band_height = max(1, screen_height - bottom)
        height = min(height, max(1, band_height - 2))
        x = max(8, (screen_width - width) // 2)
        y = min(screen_height - height, bottom + max(0, (band_height - height) // 2))
        geometry = {
            "x": int(x),
            "y": int(y),
            "width": int(width),
            "height": int(height),
            "orientation": "bottom",
            "visible": True,
        }
        return _fit_horizontal_geometry_to_empty_slot(
            geometry,
            int(screen_width),
            occupied_spans,
        )
    if top > 0:
        band_height = max(1, top)
        height = min(height, max(1, band_height - 2))
        x = max(8, (screen_width - width) // 2)
        y = max(0, min(top - height, (band_height - height) // 2))
        geometry = {
            "x": int(x),
            "y": int(y),
            "width": int(width),
            "height": int(height),
            "orientation": "top",
            "visible": True,
        }
        return _fit_horizontal_geometry_to_empty_slot(
            geometry,
            int(screen_width),
            occupied_spans,
        )
    if left > 0:
        width = max(1, min(320, left - 2))
        height = min(86, max(58, int(screen_height * 0.12)))
        return {
            "x": max(0, min(left - width, (left - width) // 2)),
            "y": max(8, screen_height - height - 8),
            "width": int(width),
            "height": int(height),
            "orientation": "left",
            "visible": True,
        }
    if right < screen_width:
        band_width = max(1, screen_width - right)
        width = max(1, min(320, band_width - 2))
        height = min(86, max(58, int(screen_height * 0.12)))
        return {
            "x": int(right + max(0, min(band_width - width, (band_width - width) // 2))),
            "y": max(8, screen_height - height - 8),
            "width": int(width),
            "height": int(height),
            "orientation": "right",
            "visible": True,
        }

    x = max(8, (screen_width - width) // 2)
    geometry = {
        "x": int(x),
        "y": int(screen_height - height - 2),
        "width": int(width),
        "height": int(height),
        "orientation": "bottom",
        "visible": True,
    }
    return _fit_horizontal_geometry_to_empty_slot(
        geometry,
        int(screen_width),
        occupied_spans,
    )


class CodexUsageTaskbarOverlay:
    def __init__(
        self,
        root: Any,
        runtime_getter: Callable[[], dict[str, Any]],
        *,
        window_factory: Callable[[Any], Any] | None = None,
        work_area_getter: Callable[[], tuple[int, int, int, int] | None] | None = None,
        occupied_span_getter: Callable[
            [int, int, tuple[int, int, int, int] | dict[str, int] | None, dict[str, int | str]],
            list[tuple[int, int]] | None,
        ]
        | None = None,
    ) -> None:
        self._root = root
        self._runtime_getter = runtime_getter
        self._window_factory = window_factory
        self._work_area_getter = work_area_getter or _get_primary_work_area
        self._occupied_span_getter = (
            occupied_span_getter or _detect_horizontal_taskbar_occupied_spans
        )
        self._window = None
        self._canvas = None
        self._last_metric_values: dict[str, str] = {}
        self._flash_until: dict[str, float] = {}
        self._last_model: dict[str, Any] | None = None
        self._flash_after_id = None
        self._keepalive_after_id = None
        self._geometry_invalidated = True
        self._cached_geometry_context = None
        self._cached_geometry: dict[str, int | str] | None = None
        return

    def refresh(self) -> bool:
        try:
            runtime = self._runtime_getter()
        except Exception:
            runtime = {}
        geometry = self._calculate_geometry()
        model = build_codex_usage_taskbar_overlay_model(runtime, geometry=geometry)
        if not bool(model.get("visible")):
            self.hide()
            return True
        window = self._ensure_window()
        if window is None:
            return False
        self._apply_geometry(window, geometry)
        self._update_metric_change_flash(model)
        self._draw(model)
        self._last_model = model
        self._schedule_keepalive_tick()
        try:
            window.deiconify()
        except Exception:
            pass
        try:
            window.lift()
        except Exception:
            pass
        self._force_native_repaint(window)
        return True

    def hide(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.withdraw()
        except Exception:
            pass
        self._cancel_flash_tick()
        self._cancel_keepalive_tick()
        return

    def invalidate_geometry(self) -> None:
        self._geometry_invalidated = True
        self._cached_geometry_context = None
        self._cached_geometry = None
        return

    def _update_metric_change_flash(self, model: dict[str, Any]) -> None:
        active_keys: set[str] = set()
        for bar in model.get("bars", []):
            if not isinstance(bar, dict):
                continue
            account_id = str(bar.get("id") or "")
            for metric in bar.get("metrics", []):
                if not isinstance(metric, dict):
                    continue
                metric_key = str(metric.get("metric_key") or metric.get("key") or "")
                identity = f"{account_id}:{metric_key}"
                value = str(metric.get("value_text") or "")
                active_keys.add(identity)
                self._last_metric_values[identity] = value
                metric["flash"] = False
                metric["flash_phase"] = False
        for identity in list(self._last_metric_values):
            if identity not in active_keys:
                self._last_metric_values.pop(identity, None)
        self._flash_until.clear()
        return

    def _decorate_model_flash(self, model: dict[str, Any], *, now: float | None = None) -> bool:
        now_value = time.monotonic() if now is None else float(now)
        any_active = False
        for bar in model.get("bars", []):
            if not isinstance(bar, dict):
                continue
            account_id = str(bar.get("id") or "")
            for metric in bar.get("metrics", []):
                if not isinstance(metric, dict):
                    continue
                metric_key = str(metric.get("metric_key") or metric.get("key") or "")
                identity = f"{account_id}:{metric_key}"
                active = now_value < float(self._flash_until.get(identity, 0.0))
                metric["flash"] = bool(active)
                metric["flash_phase"] = False
                any_active = any_active or active
        return any_active

    def _schedule_flash_tick_if_needed(self) -> None:
        model = self._last_model
        if not isinstance(model, dict):
            return
        if not self._decorate_model_flash(model):
            self._cancel_flash_tick()
            return
        if self._flash_after_id is not None:
            return
        delay_ms = self._next_flash_expiry_delay_ms()
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        try:
            self._flash_after_id = scheduler(delay_ms, self._flash_tick)
        except Exception:
            self._flash_after_id = None
        return

    def _next_flash_expiry_delay_ms(self) -> int:
        now = time.monotonic()
        expiries = [float(value) for value in self._flash_until.values() if float(value) > now]
        if not expiries:
            return _FLASH_TICK_MS
        return max(50, min(5000, int((min(expiries) - now) * 1000) + 50))

    def _flash_tick(self) -> None:
        self._flash_after_id = None
        model = self._last_model
        if not isinstance(model, dict):
            return
        if not self._decorate_model_flash(model):
            self._draw(model)
            return
        self._draw(model)
        self._schedule_flash_tick_if_needed()
        return

    def _cancel_flash_tick(self) -> None:
        after_id = self._flash_after_id
        self._flash_after_id = None
        if after_id is None:
            return
        canceller = getattr(self._root, "after_cancel", None)
        if callable(canceller):
            try:
                canceller(after_id)
            except Exception:
                pass
        return

    def _schedule_keepalive_tick(self) -> None:
        if self._keepalive_after_id is not None:
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        try:
            self._keepalive_after_id = scheduler(
                _KEEPALIVE_TICK_MS,
                self._keepalive_tick,
            )
        except Exception:
            self._keepalive_after_id = None
        return

    def _keepalive_tick(self) -> None:
        self._keepalive_after_id = None
        window = self._window
        model = self._last_model
        if window is None or not isinstance(model, dict):
            return
        if not bool(model.get("visible", True)):
            return
        native_visible = self._is_native_z_order_visible(window)
        self._reassert_native_z_order(window)
        if not native_visible:
            self._force_native_repaint(window)
        self._schedule_keepalive_tick()
        return

    def _cancel_keepalive_tick(self) -> None:
        after_id = self._keepalive_after_id
        self._keepalive_after_id = None
        if after_id is None:
            return
        canceller = getattr(self._root, "after_cancel", None)
        if callable(canceller):
            try:
                canceller(after_id)
            except Exception:
                pass
        return

    def _calculate_geometry(self) -> dict[str, int | str]:
        width = _root_int(self._root, "winfo_screenwidth", 1920)
        height = _root_int(self._root, "winfo_screenheight", 1080)
        try:
            work_area = self._work_area_getter()
        except Exception:
            work_area = None
        geometry = calculate_taskbar_overlay_geometry(width, height, work_area)
        if str(geometry.get("orientation") or "") not in {"bottom", "top"}:
            self._cache_geometry(width, height, work_area, geometry)
            return geometry
        context = self._geometry_context(width, height, work_area, geometry)
        if (
            not bool(self._geometry_invalidated)
            and self._cached_geometry_context == context
            and isinstance(self._cached_geometry, dict)
        ):
            return dict(self._cached_geometry)
        occupied_spans = self._calculate_occupied_spans(width, height, work_area, geometry)
        if occupied_spans is None:
            self._cached_geometry_context = context
            self._cached_geometry = dict(geometry)
            self._geometry_invalidated = False
            return geometry
        fitted = calculate_taskbar_overlay_geometry(
            width,
            height,
            work_area,
            occupied_spans=occupied_spans,
        )
        self._cached_geometry_context = context
        self._cached_geometry = dict(fitted)
        self._geometry_invalidated = False
        return fitted

    def _geometry_context(
        self,
        width: int,
        height: int,
        work_area: tuple[int, int, int, int] | dict[str, int] | None,
        geometry: dict[str, int | str],
    ) -> tuple[int, int, tuple[int, int, int, int], str]:
        return (
            int(width),
            int(height),
            _normalize_work_area(work_area, int(width), int(height)),
            str(geometry.get("orientation") or ""),
        )

    def _cache_geometry(
        self,
        width: int,
        height: int,
        work_area: tuple[int, int, int, int] | dict[str, int] | None,
        geometry: dict[str, int | str],
    ) -> None:
        self._cached_geometry_context = self._geometry_context(
            width,
            height,
            work_area,
            geometry,
        )
        self._cached_geometry = dict(geometry)
        self._geometry_invalidated = False
        return

    def _calculate_occupied_spans(
        self,
        width: int,
        height: int,
        work_area: tuple[int, int, int, int] | dict[str, int] | None,
        geometry: dict[str, int | str],
    ) -> list[tuple[int, int]] | None:
        window = self._window
        if window is not None:
            try:
                window.withdraw()
                updater = getattr(self._root, "update_idletasks", None)
                if callable(updater):
                    updater()
                time.sleep(0.035)
            except Exception:
                pass
        try:
            return self._occupied_span_getter(width, height, work_area, geometry)
        except Exception:
            return None

    def _ensure_window(self):
        if self._window is not None:
            return self._window
        factory = self._window_factory
        if factory is not None:
            self._window = factory(self._root)
            return self._window
        if tk is None:
            return None
        window = tk.Toplevel(self._root)
        window.withdraw()
        window.overrideredirect(True)
        try:
            window.attributes("-topmost", True)
        except Exception:
            pass
        try:
            window.attributes("-alpha", 0.94)
        except Exception:
            pass
        try:
            window.update_idletasks()
        except Exception:
            pass
        canvas = tk.Canvas(
            window,
            borderwidth=0,
            highlightthickness=0,
            bg="#16181d",
        )
        canvas.pack(fill="both", expand=True)
        self._window = window
        self._canvas = canvas
        try:
            window.update_idletasks()
        except Exception:
            pass
        self._prepare_native_window(window)
        return window

    def _apply_geometry(self, window: Any, geometry: dict[str, int | str]) -> None:
        self._prepare_native_window(window)
        x = int(geometry.get("x", 0))
        y = int(geometry.get("y", 0))
        width = int(geometry.get("width", 760))
        height = int(geometry.get("height", 38))
        try:
            window.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass
        canvas = self._canvas
        if canvas is not None:
            try:
                canvas.configure(width=width, height=height)
            except Exception:
                pass
        self._set_native_position(window, x, y, width, height)
        return

    def _draw(self, model: dict[str, Any]) -> None:
        canvas = self._canvas
        if canvas is None:
            drawer = getattr(self._window, "draw_model", None)
            if callable(drawer):
                drawer(model)
            return
        geometry = model.get("geometry", {})
        if not isinstance(geometry, dict):
            geometry = {}
        width = int(geometry.get("width", 760))
        height = int(geometry.get("height", 38))
        bars = [bar for bar in model.get("bars", []) if isinstance(bar, dict)]
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#16181d", outline="#343946")
        if not bars:
            return
        row_count = min(2, len(bars))
        row_height = max(14, (height - 8) // max(1, row_count))
        label_width = min(46, max(40, int(width * 0.13)))
        status_width = 28 if width >= 300 else 10
        metrics_x = 8 + label_width + status_width + 18
        metrics_width = max(110, width - metrics_x - 8)
        for index, bar in enumerate(bars[:2]):
            y = 4 + index * row_height
            center_y = y + row_height // 2
            label = str(bar.get("label") or "")
            status_text = str(bar.get("status_text") or "")
            status_color = str(bar.get("status_color") or "#6b7280")
            canvas.create_text(
                8,
                center_y,
                anchor="w",
                fill="#e5e7eb",
                font=("Segoe UI", 8, "bold"),
                text=label[:10],
            )
            dot_x = 8 + label_width + 2
            canvas.create_oval(
                dot_x,
                center_y - 4,
                dot_x + 8,
                center_y + 4,
                fill=status_color,
                outline=status_color,
            )
            if status_width > 10:
                canvas.create_text(
                    dot_x + 10,
                    center_y,
                    anchor="w",
                    fill=status_color,
                    font=("Segoe UI", 6, "bold"),
                    text=status_text[:5],
                )
            metrics = [metric for metric in bar.get("metrics", []) if isinstance(metric, dict)]
            if not metrics:
                metrics = [
                    {
                        "key": "5h",
                        "percent": int(bar.get("percent") or 0),
                        "value_text": str(bar.get("value_text") or "--"),
                        "color": str(bar.get("color") or "#6b7280"),
                    }
                ]
            segment_gap = 20
            segment_width = max(
                48,
                (metrics_width - segment_gap * max(0, len(metrics) - 1)) // max(1, len(metrics)),
            )
            for metric_index, metric in enumerate(metrics[:2]):
                segment_x = metrics_x + metric_index * (segment_width + segment_gap)
                self._draw_metric_segment(canvas, metric, segment_x, y, segment_width, row_height)
        return

    def _draw_metric_segment(
        self,
        canvas: Any,
        metric: dict[str, Any],
        x: int,
        y: int,
        width: int,
        row_height: int,
    ) -> None:
        center_y = y + row_height // 2
        label = str(metric.get("key") or "")
        value_text = str(metric.get("value_text") or "--")
        reset_text = str(metric.get("reset_text") or "")
        reset_short_text = str(metric.get("reset_short_text") or reset_text)
        reset_color = str(metric.get("reset_color") or "#94a3b8")
        percent = int(metric.get("percent") or 0)
        color = str(metric.get("color") or "#6b7280")
        flash = bool(metric.get("flash"))
        flash_phase = bool(metric.get("flash_phase"))
        label_width = 14
        value_width = 24
        label_to_bar_gap = 3
        bar_to_value_gap = 3
        reset_gap = 4
        display_reset_text = _display_reset_text_for_width(
            reset_text,
            reset_short_text,
            width,
        )
        reset_width = _reset_column_width_for_width(width)
        show_reset = bool(display_reset_text)
        bar_x = x + label_width + label_to_bar_gap
        reset_x = x + width
        value_x = reset_x - reset_width - reset_gap if reset_width > 0 else x + width
        reset_text_x = value_x + reset_gap
        bar_width = max(16, value_x - value_width - bar_to_value_gap - bar_x)
        bar_y = y + max(4, (row_height - 7) // 2)
        if flash:
            outline = "#fde68a" if flash_phase else "#f59e0b"
            canvas.create_rectangle(
                x - 2,
                y + 1,
                x + width + 1,
                y + row_height - 1,
                fill="#2c2414" if flash_phase else "#16181d",
                outline=outline,
            )
        canvas.create_text(
            x,
            center_y,
            anchor="w",
            fill="#cbd5e1",
            font=("Segoe UI", 7, "bold"),
            text=label,
        )
        canvas.create_rectangle(
            bar_x,
            bar_y,
            bar_x + bar_width,
            bar_y + 7,
            fill="#2a2f38",
            outline="#3f4654",
        )
        fill_width = int(round(bar_width * max(0, min(100, percent)) / 100))
        if fill_width > 0:
            canvas.create_rectangle(
                bar_x,
                bar_y,
                bar_x + fill_width,
                bar_y + 7,
                fill=color,
                outline=color,
            )
        canvas.create_text(
            value_x,
            center_y,
            anchor="e",
            fill="#f9fafb",
            font=("Segoe UI", 7, "bold"),
            text=value_text,
        )
        if show_reset:
            canvas.create_text(
                reset_text_x,
                center_y,
                anchor="w",
                fill=reset_color,
                font=("Segoe UI", 6, "bold"),
                text=display_reset_text,
            )
        return

    def _prepare_native_window(self, window: Any) -> None:
        hwnd = _get_window_handle(window)
        if hwnd <= 0 or win32gui is None or win32con is None:
            return
        try:
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style |= int(win32con.WS_EX_TOOLWINDOW)
            ex_style |= int(getattr(win32con, "WS_EX_NOACTIVATE", 0x08000000))
            ex_style &= ~int(getattr(win32con, "WS_EX_APPWINDOW", 0x00040000))
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)
        except Exception:
            pass
        self._bind_native_owner_to_taskbar(hwnd)
        return

    def _bind_native_owner_to_taskbar(self, hwnd: int) -> None:
        if hwnd <= 0 or win32gui is None or not hasattr(ctypes, "windll"):
            return
        try:
            taskbar_hwnd = int(win32gui.FindWindow("Shell_TrayWnd", None) or 0)
        except Exception:
            taskbar_hwnd = 0
        if taskbar_hwnd <= 0 or taskbar_hwnd == int(hwnd):
            return
        try:
            setter = getattr(ctypes.windll.user32, "SetWindowLongPtrW", None)
            if setter is None:
                setter = getattr(ctypes.windll.user32, "SetWindowLongW", None)
            if not callable(setter):
                return
            try:
                if wintypes is not None:
                    setter.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HWND]
                    setter.restype = wintypes.HWND
            except Exception:
                pass
            setter(int(hwnd), int(_GWLP_HWNDPARENT), int(taskbar_hwnd))
        except Exception:
            pass
        return

    def _set_native_position(
        self,
        window: Any,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        hwnd = _get_window_handle(window)
        if hwnd <= 0 or win32gui is None or win32con is None:
            return
        try:
            flags = (
                int(win32con.SWP_NOACTIVATE)
                | int(win32con.SWP_NOOWNERZORDER)
                | int(win32con.SWP_SHOWWINDOW)
            )
            win32gui.SetWindowPos(
                hwnd,
                int(getattr(win32con, "HWND_TOPMOST", -1)),
                int(x),
                int(y),
                int(width),
                int(height),
                flags,
            )
        except Exception:
            pass
        return

    def _is_native_z_order_visible(self, window: Any) -> bool:
        hwnd = _get_window_handle(window)
        if hwnd <= 0 or not hasattr(ctypes, "windll") or wintypes is None:
            return True

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        try:
            rect = RECT()
            if not ctypes.windll.user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
                return True
            width = int(rect.right) - int(rect.left)
            height = int(rect.bottom) - int(rect.top)
            if width <= 0 or height <= 0:
                return True
            y = int(rect.top) + max(1, height // 2)
            probe_xs = [
                int(rect.left) + max(1, min(12, width - 1)),
                int(rect.left) + max(1, width // 2),
                int(rect.right) - max(1, min(12, width - 1)),
            ]
            for x in probe_xs:
                hit = ctypes.windll.user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
                if int(hit) <= 0:
                    continue
                root = ctypes.windll.user32.GetAncestor(int(hit), 2)
                if int(hit) == int(hwnd) or int(root) == int(hwnd):
                    return True
        except Exception:
            return True
        return False

    def _reassert_native_z_order(self, window: Any) -> None:
        hwnd = _get_window_handle(window)
        if hwnd <= 0 or not hasattr(ctypes, "windll"):
            return
        try:
            self._prepare_native_window(window)
            hwnd_topmost = -1
            swp_nosize = 0x0001
            swp_nomove = 0x0002
            swp_noactivate = 0x0010
            swp_showwindow = 0x0040
            swp_noownerzorder = 0x0200
            ctypes.windll.user32.SetWindowPos(
                int(hwnd),
                hwnd_topmost,
                0,
                0,
                0,
                0,
                swp_nomove
                | swp_nosize
                | swp_noactivate
                | swp_showwindow
                | swp_noownerzorder,
            )
        except Exception:
            pass
        return

    def _force_native_repaint(self, window: Any) -> None:
        hwnd = _get_window_handle(window)
        if hwnd <= 0 or not hasattr(ctypes, "windll"):
            return
        try:
            redraw_window = getattr(ctypes.windll.user32, "RedrawWindow", None)
            if callable(redraw_window):
                redraw_invalidate = 0x0001
                redraw_updatenow = 0x0100
                redraw_allchildren = 0x0080
                redraw_window(
                    int(hwnd),
                    None,
                    None,
                    redraw_invalidate | redraw_updatenow | redraw_allchildren,
                )
                return
        except Exception:
            pass
        try:
            ctypes.windll.user32.InvalidateRect(int(hwnd), None, True)
            ctypes.windll.user32.UpdateWindow(int(hwnd))
        except Exception:
            pass
        return


def _fit_horizontal_geometry_to_empty_slot(
    geometry: dict[str, int | str],
    screen_width: int,
    occupied_spans: list[tuple[int, int]] | None,
) -> dict[str, int | str]:
    fitted = dict(geometry)
    if occupied_spans is None:
        return fitted

    desired_width = max(1, int(fitted.get("width", 0) or 0))
    free_spans = _free_spans_from_occupied_spans(
        int(screen_width),
        occupied_spans,
        padding_px=_EMPTY_SLOT_PADDING_PX,
    )
    candidates = []
    for start, end in free_spans:
        available = max(0, int(end) - int(start))
        width = min(desired_width, available)
        if width < _MIN_EMPTY_SLOT_WIDTH_PX:
            continue
        candidates.append((end, width, available, start))

    if not candidates:
        fitted["visible"] = False
        fitted["width"] = 0
        fitted["height"] = 0
        return fitted

    end, width, _available, start = max(candidates)
    fitted["width"] = int(width)
    fitted["x"] = int(max(start, end - width))
    fitted["visible"] = True
    return fitted


def _free_spans_from_occupied_spans(
    screen_width: int,
    occupied_spans: list[tuple[int, int]],
    *,
    padding_px: int,
) -> list[tuple[int, int]]:
    normalized = _merge_spans(
        [
            (
                max(0, min(int(screen_width), int(start))),
                max(0, min(int(screen_width), int(end))),
            )
            for start, end in occupied_spans
            if int(end) > int(start)
        ]
    )
    free_spans: list[tuple[int, int]] = []
    cursor = 0
    for start, end in normalized:
        if start > cursor:
            free_start = cursor + int(padding_px)
            free_end = start - int(padding_px)
            if free_end > free_start:
                free_spans.append((free_start, free_end))
        cursor = max(cursor, end)
    if cursor < screen_width:
        free_start = cursor + int(padding_px)
        free_end = int(screen_width) - int(padding_px)
        if free_end > free_start:
            free_spans.append((free_start, free_end))
    return free_spans


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _detect_horizontal_taskbar_occupied_spans(
    screen_width: int,
    screen_height: int,
    work_area: tuple[int, int, int, int] | dict[str, int] | None,
    geometry: dict[str, int | str],
) -> list[tuple[int, int]] | None:
    if not hasattr(ctypes, "windll"):
        return None
    left, top, right, bottom = _normalize_work_area(work_area, screen_width, screen_height)
    orientation = str(geometry.get("orientation") or "")
    if orientation == "bottom":
        band_top = max(0, min(int(screen_height), int(bottom)))
        band_bottom = int(screen_height)
    elif orientation == "top":
        band_top = 0
        band_bottom = max(0, min(int(screen_height), int(top)))
    else:
        return None
    if band_bottom - band_top < 8:
        return None

    sample_rows = _taskbar_sample_rows(band_top, band_bottom)
    columns = _sample_taskbar_columns(int(screen_width), sample_rows)
    if not columns:
        return None
    background = _median_background_color(columns)
    occupied = []
    for x, colors in columns:
        if _column_looks_occupied(colors, background):
            occupied.append(
                (
                    max(0, x - _OCCUPIED_DILATION_PX),
                    min(int(screen_width), x + _TASKBAR_SAMPLE_STEP_PX + _OCCUPIED_DILATION_PX),
                )
            )

    # Keep the reserved taskbar edge controls out of the overlay even when the
    # sampled pixels happen to be close to the background color.
    edge_guard = max(72, min(180, int(screen_width * 0.04)))
    occupied.extend([(0, edge_guard), (int(screen_width) - edge_guard, int(screen_width))])
    return _merge_spans(occupied)


def _taskbar_sample_rows(band_top: int, band_bottom: int) -> list[int]:
    band_height = max(1, int(band_bottom) - int(band_top))
    rows = []
    for ratio in (0.18, 0.34, 0.5, 0.66, 0.82):
        y = int(band_top + round(band_height * ratio))
        rows.append(max(int(band_top), min(int(band_bottom) - 1, y)))
    return sorted(set(rows))


def _sample_taskbar_columns(
    screen_width: int,
    sample_rows: list[int],
) -> list[tuple[int, list[tuple[int, int, int]]]]:
    if not sample_rows or screen_width <= 0:
        return []
    capture_top = min(sample_rows)
    capture_bottom = max(sample_rows) + 1
    capture_height = max(1, capture_bottom - capture_top)
    pixels = _capture_screen_region_bgra(0, capture_top, int(screen_width), capture_height)
    if pixels is None:
        return []

    columns: list[tuple[int, list[tuple[int, int, int]]]] = []
    row_offsets = [max(0, min(capture_height - 1, y - capture_top)) for y in sample_rows]
    for x in range(0, int(screen_width), _TASKBAR_SAMPLE_STEP_PX):
        colors = []
        for row_offset in row_offsets:
            offset = ((row_offset * int(screen_width)) + int(x)) * 4
            blue = pixels[offset]
            green = pixels[offset + 1]
            red = pixels[offset + 2]
            colors.append((int(red), int(green), int(blue)))
        columns.append((int(x), colors))
    return columns


def _capture_screen_region_bgra(
    x: int,
    y: int,
    width: int,
    height: int,
) -> bytes | None:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    screen_dc = user32.GetDC(0)
    if not screen_dc:
        return None
    memory_dc = 0
    bitmap = 0
    old_object = 0
    try:
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            return None
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, int(width), int(height))
        if not bitmap:
            return None
        old_object = gdi32.SelectObject(memory_dc, bitmap)
        srccopy = 0x00CC0020
        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            int(width),
            int(height),
            screen_dc,
            int(x),
            int(y),
            srccopy,
        ):
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        class RGBQUAD(ctypes.Structure):
            _fields_ = [
                ("rgbBlue", ctypes.c_ubyte),
                ("rgbGreen", ctypes.c_ubyte),
                ("rgbRed", ctypes.c_ubyte),
                ("rgbReserved", ctypes.c_ubyte),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [
                ("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", RGBQUAD * 1),
            ]

        bitmap_info = BITMAPINFO()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bitmap_info.bmiHeader.biWidth = int(width)
        bitmap_info.bmiHeader.biHeight = -int(height)
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(int(width) * int(height) * 4)
        dib_rgb_colors = 0
        scan_lines = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            int(height),
            buffer,
            ctypes.byref(bitmap_info),
            dib_rgb_colors,
        )
        if scan_lines == 0:
            return None
        return bytes(buffer)
    finally:
        if memory_dc and old_object:
            gdi32.SelectObject(memory_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)


def _median_background_color(
    columns: list[tuple[int, list[tuple[int, int, int]]]],
) -> tuple[int, int, int]:
    values = [color for _x, colors in columns for color in colors]
    if not values:
        return 0, 0, 0
    channels = []
    for index in range(3):
        sorted_channel = sorted(color[index] for color in values)
        channels.append(sorted_channel[len(sorted_channel) // 2])
    return int(channels[0]), int(channels[1]), int(channels[2])


def _column_looks_occupied(
    colors: list[tuple[int, int, int]],
    _background: tuple[int, int, int],
) -> bool:
    if not colors:
        return False
    vertical_spread = 0
    for left_color in colors:
        for right_color in colors:
            vertical_spread = max(vertical_spread, _rgb_distance(left_color, right_color))
    return vertical_spread >= 38


def _rgb_distance(
    left: tuple[int, int, int],
    right: tuple[int, int, int],
) -> int:
    return max(
        abs(int(left[0]) - int(right[0])),
        abs(int(left[1]) - int(right[1])),
        abs(int(left[2]) - int(right[2])),
    )


def _colorref_to_rgb(colorref: int) -> tuple[int, int, int]:
    return (
        int(colorref & 0xFF),
        int((colorref >> 8) & 0xFF),
        int((colorref >> 16) & 0xFF),
    )


def _normalize_work_area(
    work_area: tuple[int, int, int, int] | dict[str, int] | None,
    screen_width: int,
    screen_height: int,
) -> tuple[int, int, int, int]:
    if isinstance(work_area, dict):
        return (
            int(work_area.get("left", 0)),
            int(work_area.get("top", 0)),
            int(work_area.get("right", screen_width)),
            int(work_area.get("bottom", screen_height)),
        )
    if isinstance(work_area, tuple) and len(work_area) == 4:
        return tuple(int(v) for v in work_area)
    return 0, 0, int(screen_width), int(screen_height)


def _build_metric(
    *,
    key: str,
    short_label: str,
    raw_value: Any,
    reset_at_value: Any = "",
    account_state: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    percent = _parse_percent(raw_value)
    enabled = str(account_state) not in {"disabled", "login", "profile_busy"}
    reset_info = _build_reset_info(
        reset_at_value,
        metric_key=key,
        percent=percent,
        now=now,
    )
    return {
        "metric_key": str(key),
        "key": str(short_label),
        "percent": 0 if percent is None else int(percent),
        "value_text": "--" if percent is None else f"{int(percent)}%",
        "state": _bar_state(enabled, percent),
        "color": _bar_color(enabled, percent),
        "reset_text": reset_info["text"],
        "reset_short_text": reset_info["short_text"],
        "reset_state": reset_info["state"],
        "reset_color": reset_info["color"],
        "flash": False,
        "flash_phase": False,
    }


def _build_reset_info(
    value: Any,
    *,
    metric_key: str,
    percent: int | None,
    now: datetime | None = None,
) -> dict[str, str]:
    parsed = _parse_reset_datetime(value)
    if parsed is None:
        return {"text": "", "short_text": "", "state": "unknown", "color": "#6b7280"}
    current = _reset_now(parsed, now)
    seconds = int((parsed - current).total_seconds())
    if seconds <= 0:
        return {"text": "now", "short_text": "now", "state": "overdue", "color": "#ef4444"}
    state = _reset_action_state(metric_key, percent, seconds)
    return {
        "text": _format_reset_remaining_detail(seconds),
        "short_text": _format_reset_remaining_compact(seconds),
        "state": state,
        "color": _reset_color(state),
    }


def _parse_reset_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _reset_now(reset_at: datetime, now: datetime | None) -> datetime:
    current = datetime.now(reset_at.tzinfo) if now is None else now
    if reset_at.tzinfo is None:
        return current.replace(tzinfo=None)
    if current.tzinfo is None:
        return current.replace(tzinfo=reset_at.tzinfo)
    return current.astimezone(reset_at.tzinfo)


def _format_reset_remaining_detail(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        hours = max(1, seconds // 3600)
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m" if minutes > 0 else f"{hours}h"
    days = max(1, seconds // 86400)
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = [f"{days}d"]
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _format_reset_remaining_compact(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{max(1, seconds // 3600)}h"
    return f"{max(1, seconds // 86400)}d"


def _display_reset_text_for_width(
    detail_text: str,
    short_text: str,
    width: int,
) -> str:
    if detail_text and width >= 118:
        return detail_text
    if short_text and width >= 108:
        return short_text
    return ""


def _reset_column_width_for_width(width: int) -> int:
    if width >= 118:
        return _RESET_DETAIL_COLUMN_WIDTH_PX
    if width >= 108:
        return _RESET_SHORT_COLUMN_WIDTH_PX
    return 0


def _reset_action_state(metric_key: str, percent: int | None, seconds: int) -> str:
    if percent is None:
        return "unknown"
    window = _RESET_WINDOW_BY_METRIC.get(str(metric_key))
    if not isinstance(window, dict):
        window = _RESET_WINDOW_BY_METRIC["five_hour_limit"]
    remaining_percent = max(0, min(100, int(percent)))
    urgent_seconds = int(window["urgent_seconds"])
    soon_seconds = int(window["soon_seconds"])
    far_seconds = int(window["far_seconds"])
    very_far_seconds = int(window["very_far_seconds"])

    if seconds <= urgent_seconds:
        if remaining_percent >= 80:
            return "urgent"
        if remaining_percent >= 60:
            return "warning"
        return "stable"
    if seconds <= soon_seconds:
        if remaining_percent >= 60:
            return "warning"
        return "stable"

    if seconds >= far_seconds:
        if remaining_percent < 25:
            return "urgent"
        if remaining_percent < 60:
            return "warning"
        if remaining_percent < 75 and seconds >= very_far_seconds:
            return "warning"
        return "stable"

    if remaining_percent < 25:
        return "warning"
    return "stable"


def _reset_color(state: str) -> str:
    if state == "stable":
        return "#22c55e"
    if state == "warning":
        return "#f59e0b"
    if state in {"urgent", "overdue"}:
        return "#ef4444"
    return "#6b7280"


def _account_status(
    enabled: bool,
    runtime: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, str]:
    if not bool(enabled):
        return {"state": "disabled", "text": "OFF", "color": "#6b7280"}
    monitor_state = str(runtime.get("monitor_state") or "idle")
    session_state = str(runtime.get("session_state") or "")
    collect_inflight = bool(runtime.get("collect_inflight")) or monitor_state in {
        "running",
        "cancelling",
    }
    if collect_inflight:
        return {"state": "sync", "text": "SYNC", "color": "#38bdf8"}
    if monitor_state == "paused_profile_in_use":
        return {"state": "profile_busy", "text": "WAIT", "color": "#f59e0b"}
    if monitor_state == "paused_auth_required" or session_state == "logged_out":
        return {"state": "login", "text": "OUT", "color": "#f59e0b"}
    has_metric = any(
        _parse_percent(snapshot.get(metric_key)) is not None
        for metric_key, _label in _TASKBAR_METRICS
    )
    if not has_metric:
        return {"state": "nodata", "text": "DATA", "color": "#94a3b8"}
    return {"state": "ready", "text": "OK", "color": "#22c55e"}


def _parse_percent(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    ratio_match = re.search(r"(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", text)
    if ratio_match is not None:
        try:
            used = float(ratio_match.group(1))
            limit = float(ratio_match.group(2))
            if limit > 0:
                return max(0, min(100, int(round((used / limit) * 100))))
        except Exception:
            return None
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return max(0, min(100, int(round(float(text)))))
    except Exception:
        return None


def _bar_state(enabled: bool, percent: int | None) -> str:
    if not bool(enabled):
        return "disabled"
    if percent is None:
        return "unknown"
    if percent >= 60:
        return "normal"
    if percent >= 40:
        return "warning"
    return "high"


def _bar_color(enabled: bool, percent: int | None) -> str:
    state = _bar_state(enabled, percent)
    if state == "high":
        return "#ef4444"
    if state == "warning":
        return "#f59e0b"
    if state == "normal":
        return "#22c55e"
    return "#6b7280"


def _account_collecting(account: dict[str, Any]) -> bool:
    runtime = account.get("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
    return bool(runtime.get("collect_inflight", False)) or str(
        runtime.get("monitor_state") or ""
    ) == "running"


def _root_int(root: Any, method_name: str, fallback: int) -> int:
    getter = getattr(root, method_name, None)
    if not callable(getter):
        return int(fallback)
    try:
        return int(getter())
    except Exception:
        return int(fallback)


def _get_primary_work_area() -> tuple[int, int, int, int] | None:
    if not hasattr(ctypes, "windll"):
        return None
    if wintypes is not None:
        rect = wintypes.RECT()
    else:
        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
    try:
        spi_getworkarea = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(
            spi_getworkarea,
            0,
            ctypes.byref(rect),
            0,
        ):
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return None
    return None


def _get_window_handle(window: Any) -> int:
    getter = getattr(window, "winfo_id", None)
    if not callable(getter):
        return 0
    try:
        hwnd = int(getter())
    except Exception:
        return 0
    if hwnd <= 0 or not hasattr(ctypes, "windll"):
        return int(hwnd)
    try:
        root = ctypes.windll.user32.GetAncestor(int(hwnd), 2)
        if int(root) > 0:
            return int(root)
    except Exception:
        pass
    return int(hwnd)
