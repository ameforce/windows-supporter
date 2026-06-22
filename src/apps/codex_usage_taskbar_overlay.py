from __future__ import annotations

import ctypes
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    import win32api
    import win32con
    import win32gui
except Exception:  # pragma: no cover - non-Windows fallback.
    win32api = None
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
_MIN_EMPTY_SLOT_WIDTH_PX = 300
_MIN_COMPACT_EMPTY_SLOT_WIDTH_PX = 176
_TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX = 560
# Wide taskbar slots should breathe up to the same target used by the default overlay.
_WIDE_EMPTY_SLOT_TARGET_WIDTH_PX = int(_DEFAULT_GEOMETRY["width"])
_TASKBAR_SAMPLE_STEP_PX = 4
_OCCUPIED_DILATION_PX = 24
_FLASH_DURATION_SEC = 1.0
_FLASH_TICK_MS = 1000
_KEEPALIVE_TICK_MS = 250
_GEOMETRY_MONITOR_TICK_MS = 400
_GEOMETRY_MONITOR_HARD_RESAMPLE_SEC = 3.0
_GEOMETRY_CHANGE_TOLERANCE_PX = 2
_GEOMETRY_TRANSIENT_X_SHIFT_TOLERANCE_PX = _OCCUPIED_DILATION_PX
_FULLSCREEN_POLL_MS = 500
_GWLP_HWNDPARENT = -8
_TASKBAR_METRICS = (
    ("five_hour_limit", "5h"),
    ("weekly_limit", "7d"),
)
_TASKBAR_OCCUPIED_CHILD_CLASSES = {
    "Button",
    "MSTaskListWClass",
    "MSTaskSwWClass",
    "ReBarWindow32",
    "Start",
    "TrayNotifyWnd",
}
_FULLSCREEN_EXCLUDED_WINDOW_CLASSES = {
    "Dwm",
    "Progman",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "WorkerW",
}
_FULLSCREEN_EXCLUDED_WINDOW_TITLES = {
    "Windows Input Experience",
    "Windows 입력 환경",
}
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
_RESET_DETAIL_COLUMN_WIDTH_PX = 48
_RESET_WEEKLY_COLUMN_WIDTH_PX = 52
_RESET_FIVE_HOUR_COLUMN_WIDTH_PX = 40
_RESET_SHORT_COLUMN_WIDTH_PX = 28
_RESET_PLACEHOLDER_TEXT = "--"
_RESET_DIRECTION_SHORTAGE = "shortage"
_RESET_DIRECTION_ON_TRACK = "on_track"
_RESET_DIRECTION_SURPLUS = "surplus"
_RESET_DIRECTION_UNKNOWN = "unknown"
_RESET_DIRECTION_MARKERS = {
    _RESET_DIRECTION_SHORTAGE: "↓",
    _RESET_DIRECTION_ON_TRACK: "=",
    _RESET_DIRECTION_SURPLUS: "↑",
    _RESET_DIRECTION_UNKNOWN: "",
}
_RESET_DIRECTION_STATES = {
    _RESET_DIRECTION_SHORTAGE: "urgent",
    _RESET_DIRECTION_ON_TRACK: "stable",
    _RESET_DIRECTION_SURPLUS: "warning",
    _RESET_DIRECTION_UNKNOWN: "unknown",
}
_RESET_BADGE_LABELS = {
    _RESET_DIRECTION_SHORTAGE: "부족",
    _RESET_DIRECTION_ON_TRACK: "정상",
    _RESET_DIRECTION_SURPLUS: "남음",
    _RESET_DIRECTION_UNKNOWN: "",
}
_RESET_BADGE_SHORT_LABELS = {
    _RESET_DIRECTION_SHORTAGE: "부",
    _RESET_DIRECTION_ON_TRACK: "정",
    _RESET_DIRECTION_SURPLUS: "남",
    _RESET_DIRECTION_UNKNOWN: "",
}
_RESET_BADGE_FILLS = {
    _RESET_DIRECTION_SHORTAGE: "#7f1d1d",
    _RESET_DIRECTION_ON_TRACK: "#064e3b",
    _RESET_DIRECTION_SURPLUS: "#78350f",
    _RESET_DIRECTION_UNKNOWN: "",
}
_RESET_BADGE_OUTLINES = {
    _RESET_DIRECTION_SHORTAGE: "#ef4444",
    _RESET_DIRECTION_ON_TRACK: "#22c55e",
    _RESET_DIRECTION_SURPLUS: "#f59e0b",
    _RESET_DIRECTION_UNKNOWN: "",
}
_RESET_BADGE_TEXT_COLORS = {
    _RESET_DIRECTION_SHORTAGE: "#fee2e2",
    _RESET_DIRECTION_ON_TRACK: "#dcfce7",
    _RESET_DIRECTION_SURPLUS: "#fef3c7",
    _RESET_DIRECTION_UNKNOWN: "",
}
_RESET_BADGE_HORIZONTAL_PADDING_PX = 2
_RESET_BADGE_HEIGHT_PX = 11
_RESET_BADGE_MIN_WIDTH_PX = 14
_RESET_BADGE_TIME_GAP_PX = 5
_RESET_BADGE_OUTLINE_WIDTH_PX = 1
_VALUE_COLUMN_MIN_WIDTH_PX = 22
_VALUE_COLUMN_MAX_WIDTH_PX = 28
_SEGMENT_RIGHT_PADDING_PX = 2
_METRIC_PROGRESS_MIN_WIDTH_PX = 28
_METRIC_PROGRESS_PREFERRED_WIDTH_PX = 36
_METRIC_PROGRESS_MAX_WIDTH_PX = 48
_METRIC_SEGMENT_GAP_COMPACT_PX = 6
_METRIC_SEGMENT_GAP_WIDE_PX = 12
_PROFILE_LABEL_COLUMN_MIN_WIDTH_PX = 64
_PROFILE_LABEL_COLUMN_COMPACT_MIN_WIDTH_PX = 28
_PROFILE_LABEL_COLUMN_MAX_WIDTH_PX = 76
_PROFILE_LABEL_COLUMN_WIDTH_RATIO = 0.17
_STATUS_DOT_ONLY_WIDTH_PX = 14
_STATUS_WITH_TEXT_WIDTH_PX = 24
_STATUS_TEXT_MIN_OVERLAY_WIDTH_PX = 420
_STATUS_TO_METRICS_GAP_PX = 6
_FIVE_HOUR_RESET_MAX_SECONDS = 36 * 60 * 60
_SNAPSHOT_WINDOW_SECONDS_BY_METRIC = {
    "five_hour_limit": 5 * 60 * 60,
    "weekly_limit": 7 * 24 * 60 * 60,
}
_SNAPSHOT_ON_TRACK_MAX_PROJECTED_REMAINING_PERCENT = 10.0
# CodexUsageMonitor.__now_iso stores captured_at as a naive KST wall-clock
# string, so snapshot tag projection must interpret naive captured_at values
# the same way while keeping local overlay "now" out of tag decisions.
_SNAPSHOT_CAPTURED_AT_FALLBACK_TZ = timezone(timedelta(hours=9))
_BAR_RENDER_SIGNATURE_KEYS = (
    "enabled",
    "label",
    "status_text",
    "status_color",
)
_METRIC_RENDER_SIGNATURE_KEYS = (
    "metric_key",
    "key",
    "percent",
    "value_text",
    "color",
    "state",
    "reset_text",
    "reset_short_text",
    "reset_color",
    "reset_state",
    "reset_direction",
    "reset_marker",
    "reset_badge_label",
    "reset_badge_short_label",
    "reset_badge_fill",
    "reset_badge_outline",
    "reset_badge_text_color",
)


@dataclass(frozen=True)
class _MetricRowLayout:
    label_width: int
    status_width: int
    metrics_x: int
    metrics_width: int
    segment_gap: int
    segment_width: int
    progress_width: int
    visible_metrics: tuple[dict[str, Any], ...]


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
                    captured_at_value=snapshot.get("captured_at"),
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
    preferred_width: int | None = None,
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
            preferred_width=preferred_width,
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
            preferred_width=preferred_width,
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
        preferred_width=preferred_width,
    )


def _label_width_for_overlay_width(width: int) -> int:
    overlay_width = max(0, int(width))
    if overlay_width < _MIN_EMPTY_SLOT_WIDTH_PX:
        return min(
            _PROFILE_LABEL_COLUMN_MIN_WIDTH_PX,
            max(
                _PROFILE_LABEL_COLUMN_COMPACT_MIN_WIDTH_PX,
                int(overlay_width * _PROFILE_LABEL_COLUMN_WIDTH_RATIO),
            ),
        )
    return min(
        _PROFILE_LABEL_COLUMN_MAX_WIDTH_PX,
        max(
            _PROFILE_LABEL_COLUMN_MIN_WIDTH_PX,
            int(overlay_width * _PROFILE_LABEL_COLUMN_WIDTH_RATIO),
        ),
    )


def _status_width_for_overlay_width(width: int) -> int:
    if int(width) >= _STATUS_TEXT_MIN_OVERLAY_WIDTH_PX:
        return _STATUS_WITH_TEXT_WIDTH_PX
    return _STATUS_DOT_ONLY_WIDTH_PX


def _metric_segment_gap_for_overlay_width(width: int) -> int:
    if int(width) < 640:
        return _METRIC_SEGMENT_GAP_COMPACT_PX
    return _METRIC_SEGMENT_GAP_WIDE_PX


def _metric_segment_width_for_metrics_width(
    metrics_width: int,
    metric_count: int,
    segment_gap: int,
) -> int:
    count = max(1, int(metric_count))
    return max(
        48,
        (int(metrics_width) - int(segment_gap) * max(0, count - 1)) // count,
    )


def _normalized_badge_mode(badge_mode: str | None) -> str:
    mode = str(badge_mode or "any").strip().lower()
    if mode in {"full", "short"}:
        return mode
    return "any"


def _expected_badge_label_for_mode(
    badge_label: str,
    badge_short_label: str,
    badge_mode: str,
) -> str:
    if _normalized_badge_mode(badge_mode) == "full":
        return str(badge_label or badge_short_label or "")
    return str(badge_short_label or badge_label or "")


def _visible_metrics_for_taskbar_bar(bar: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    bar_dict = bar if isinstance(bar, dict) else {}
    metrics = [
        metric
        for metric in bar_dict.get("metrics", [])
        if isinstance(metric, dict)
    ]
    if not metrics:
        metrics = [
            {
                "key": "5h",
                "percent": int(bar_dict.get("percent") or 0),
                "value_text": str(bar_dict.get("value_text") or "--"),
                "color": str(bar_dict.get("color") or "#6b7280"),
            }
        ]
    return tuple(metrics[:2])


def _metric_fits_badge_mode(
    metric: dict[str, Any],
    segment_width: int,
    progress_width: int,
    badge_mode: str,
) -> bool:
    metric_dict = metric if isinstance(metric, dict) else {}
    mode = _normalized_badge_mode(badge_mode)
    detail_text = str(metric_dict.get("reset_text") or "")
    short_text = str(metric_dict.get("reset_short_text") or detail_text)
    badge_label = str(metric_dict.get("reset_badge_label") or "")
    badge_short_label = str(metric_dict.get("reset_badge_short_label") or "")
    has_reset_badge = bool(badge_label or badge_short_label)
    has_reset_time = bool(detail_text or short_text)
    metric_key = str(metric_dict.get("metric_key") or "")
    reset_marker = str(metric_dict.get("reset_marker") or "")
    layout = _fit_metric_segment_layout(
        segment_width,
        detail_text,
        short_text,
        badge_label=badge_label,
        badge_short_label=badge_short_label,
        metric_key=metric_key,
        reset_marker=reset_marker,
        has_reset_badge=has_reset_badge,
        progress_width=progress_width,
        badge_mode=mode,
    )
    if int(layout.get("progress_width") or 0) < _METRIC_PROGRESS_MIN_WIDTH_PX:
        return False
    badge_fit = layout.get("badge_fit")
    if not isinstance(badge_fit, dict):
        badge_fit = {}
    if has_reset_badge:
        if not bool(badge_fit.get("badge_visible")):
            return False
        expected_label = _expected_badge_label_for_mode(
            badge_label,
            badge_short_label,
            mode,
        )
        if expected_label and str(badge_fit.get("badge_label") or "") != expected_label:
            return False
        if has_reset_time and not str(badge_fit.get("time_text") or ""):
            return False
    elif has_reset_time and not (
        str(badge_fit.get("time_text") or "")
        or str(layout.get("display_reset_text") or "")
    ):
        return False
    return True


def _row_fits_badge_mode(
    metrics: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    segment_width: int,
    progress_width: int,
    badge_mode: str,
) -> bool:
    visible_metrics = tuple(metric for metric in metrics[:2] if isinstance(metric, dict))
    if not visible_metrics:
        return True
    return all(
        _metric_fits_badge_mode(
            metric,
            int(segment_width),
            int(progress_width),
            badge_mode,
        )
        for metric in visible_metrics
    )


def _row_fits_badge_mode_for_overlay_width(
    width: int,
    metrics: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    badge_mode: str,
) -> bool:
    row_layout = _metric_row_layout_for_overlay_width(width, metrics)
    return _row_fits_badge_mode(
        row_layout.visible_metrics,
        row_layout.segment_width,
        row_layout.progress_width,
        badge_mode,
    )


def _resolve_overlay_badge_mode(row_layouts: tuple[_MetricRowLayout, ...]) -> str:
    if all(
        _row_fits_badge_mode(
            row_layout.visible_metrics,
            row_layout.segment_width,
            row_layout.progress_width,
            "full",
        )
        for row_layout in row_layouts
    ):
        return "full"
    return "short"


def _metric_row_layout_for_overlay_width(
    width: int,
    metrics: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> _MetricRowLayout:
    overlay_width = int(width)
    visible_metrics = tuple(metric for metric in metrics[:2] if isinstance(metric, dict))
    label_width = _label_width_for_overlay_width(overlay_width)
    status_width = _status_width_for_overlay_width(overlay_width)
    metrics_x = 6 + label_width + status_width + _STATUS_TO_METRICS_GAP_PX
    metrics_width = max(0, overlay_width - metrics_x - 4)
    segment_gap = _metric_segment_gap_for_overlay_width(overlay_width)
    segment_width = _metric_segment_width_for_metrics_width(
        metrics_width,
        len(visible_metrics),
        segment_gap,
    )
    progress_width = _metric_progress_width_for_segment(segment_width)
    return _MetricRowLayout(
        label_width=label_width,
        status_width=status_width,
        metrics_x=metrics_x,
        metrics_width=metrics_width,
        segment_gap=segment_gap,
        segment_width=segment_width,
        progress_width=progress_width,
        visible_metrics=visible_metrics,
    )


def _required_metric_segment_width(
    metric: dict[str, Any],
    *,
    badge_mode: str = "any",
) -> int:
    metric_dict = metric if isinstance(metric, dict) else {}
    detail_text = str(metric_dict.get("reset_text") or "")
    short_text = str(metric_dict.get("reset_short_text") or detail_text)
    badge_label = str(metric_dict.get("reset_badge_label") or "")
    badge_short_label = str(metric_dict.get("reset_badge_short_label") or "")
    has_reset_badge = bool(badge_label or badge_short_label)
    has_reset_time = bool(detail_text or short_text)
    metric_key = str(metric_dict.get("metric_key") or "")
    reset_marker = str(metric_dict.get("reset_marker") or "")
    mode = _normalized_badge_mode(badge_mode)

    for candidate_width in range(48, _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX + 1):
        layout = _fit_metric_segment_layout(
            candidate_width,
            detail_text,
            short_text,
            badge_label=badge_label,
            badge_short_label=badge_short_label,
            metric_key=metric_key,
            reset_marker=reset_marker,
            has_reset_badge=has_reset_badge,
            progress_width=_metric_progress_width_for_segment(candidate_width),
            badge_mode=mode,
        )
        if int(layout.get("progress_width") or 0) < _METRIC_PROGRESS_MIN_WIDTH_PX:
            continue
        badge_fit = layout.get("badge_fit")
        if not isinstance(badge_fit, dict):
            badge_fit = {}
        if has_reset_badge:
            if not bool(badge_fit.get("badge_visible")):
                continue
            expected_label = _expected_badge_label_for_mode(
                badge_label,
                badge_short_label,
                mode,
            )
            if mode in {"full", "short"} and str(
                badge_fit.get("badge_label") or ""
            ) != expected_label:
                continue
            if has_reset_time and not str(badge_fit.get("time_text") or ""):
                continue
        elif has_reset_time:
            if not (
                str(badge_fit.get("time_text") or "")
                or str(layout.get("display_reset_text") or "")
            ):
                continue
        return int(candidate_width)
    return _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX


def _preferred_taskbar_overlay_width_for_model(model: dict[str, Any]) -> int | None:
    if not isinstance(model, dict) or not bool(model.get("visible")):
        return None

    rows: list[tuple[dict[str, Any], ...]] = []
    bars = model.get("bars")
    if not isinstance(bars, list):
        return None
    for bar in bars[:2]:
        if not isinstance(bar, dict) or not bool(bar.get("enabled", True)):
            continue
        visible_metrics = _visible_metrics_for_taskbar_bar(bar)
        rows.append(visible_metrics)

    if not rows:
        return None

    for badge_mode in ("full", "short"):
        for candidate_width in range(
            _MIN_EMPTY_SLOT_WIDTH_PX,
            _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX + 1,
        ):
            if all(
                _row_fits_badge_mode_for_overlay_width(
                    candidate_width,
                    visible_metrics,
                    badge_mode,
                )
                for visible_metrics in rows
            ):
                return _wide_slot_preferred_width(model, int(candidate_width))
    return _wide_slot_preferred_width(model, _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX)


def _metric_render_signature(metric: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(metric.get(key) for key in _METRIC_RENDER_SIGNATURE_KEYS)


def _bar_render_signature(bar: dict[str, Any]) -> tuple[Any, ...]:
    metrics = tuple(
        _metric_render_signature(metric)
        for metric in _visible_metrics_for_taskbar_bar(bar)
    )
    bar_fields = tuple(bar.get(key) for key in _BAR_RENDER_SIGNATURE_KEYS)
    return bar_fields + (metrics,)


def _overlay_render_signature(model: dict[str, Any] | None) -> tuple[Any, ...]:
    if not isinstance(model, dict):
        return tuple()
    bars_signature = []
    bars = model.get("bars")
    if isinstance(bars, list):
        for bar in bars[:2]:
            if not isinstance(bar, dict):
                continue
            bars_signature.append(_bar_render_signature(bar))
    return (
        bool(model.get("visible", True)),
        model.get("state"),
        tuple(bars_signature),
    )


def _wide_slot_preferred_width(model: dict[str, Any], minimum_width: int) -> int:
    geometry = model.get("geometry") if isinstance(model, dict) else None
    if not isinstance(geometry, dict):
        return int(minimum_width)
    try:
        geometry_width = int(geometry.get("width", 0) or 0)
    except (TypeError, ValueError):
        geometry_width = 0
    if geometry_width <= _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX:
        return int(minimum_width)
    return min(
        max(int(minimum_width), _WIDE_EMPTY_SLOT_TARGET_WIDTH_PX),
        int(geometry_width),
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
        fullscreen_detector: Callable[[Any | None], bool] | None = None,
    ) -> None:
        self._root = root
        self._runtime_getter = runtime_getter
        self._window_factory = window_factory
        self._work_area_getter = work_area_getter or _get_primary_work_area
        self._occupied_span_getter = (
            occupied_span_getter or _detect_horizontal_taskbar_occupied_spans
        )
        self._fullscreen_detector = fullscreen_detector
        self._window = None
        self._canvas = None
        self._last_metric_values: dict[str, str] = {}
        self._flash_until: dict[str, float] = {}
        self._last_model: dict[str, Any] | None = None
        self._flash_after_id = None
        self._keepalive_after_id = None
        self._geometry_after_id = None
        self._last_geometry_hard_resample_at = 0.0
        self._geometry_invalidated = True
        self._cached_geometry_context = None
        self._cached_geometry: dict[str, int | str] | None = None
        self._pending_regression_geometry: dict[str, int | str] | None = None
        self._pending_regression_context = None
        self._pending_regression_count = 0
        self._fullscreen_suppressed = False
        return

    def refresh(self) -> bool:
        try:
            runtime = self._runtime_getter()
        except Exception:
            runtime = {}
        now = _current_overlay_datetime()
        pre_model = build_codex_usage_taskbar_overlay_model(
            runtime,
            geometry=_DEFAULT_GEOMETRY,
            now=now,
        )
        preferred_width = _preferred_taskbar_overlay_width_for_model(pre_model)
        geometry = self._calculate_geometry(preferred_width=preferred_width)
        model = build_codex_usage_taskbar_overlay_model(
            runtime,
            geometry=geometry,
            now=now,
        )
        if not bool(model.get("visible")):
            if not bool(geometry.get("visible", True)):
                self._withdraw_for_geometry_gap(model)
                return True
            self.hide()
            return True
        if self._is_fullscreen_active(self._window, geometry):
            self._last_model = model
            self._suppress_for_fullscreen()
            return True
        self._fullscreen_suppressed = False
        window = self._ensure_window()
        if window is None:
            return False
        self._apply_geometry(window, geometry)
        self._update_metric_change_flash(model)
        self._draw(model)
        self._last_model = model
        if self._last_geometry_hard_resample_at <= 0.0:
            try:
                self._last_geometry_hard_resample_at = time.monotonic()
            except Exception:
                self._last_geometry_hard_resample_at = 0.0
        self._schedule_keepalive_tick()
        self._schedule_geometry_monitor_tick()
        self._schedule_flash_tick_if_needed()
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
        self._cancel_geometry_monitor_tick()
        self._clear_pending_regression_geometry()
        self._fullscreen_suppressed = False
        return

    def _withdraw_for_geometry_gap(self, model: dict[str, Any]) -> None:
        self._last_model = model
        window = self._window
        if window is not None:
            try:
                window.withdraw()
            except Exception:
                pass
        self._cancel_flash_tick()
        self._clear_pending_regression_geometry()
        self._schedule_geometry_monitor_tick()
        return

    def invalidate_geometry(self) -> None:
        self._geometry_invalidated = True
        self._cached_geometry_context = None
        self._cached_geometry = None
        self._clear_pending_regression_geometry()
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
                previous = self._last_metric_values.get(identity)
                if previous is not None and previous != value:
                    try:
                        self._flash_until[identity] = time.monotonic() + _FLASH_DURATION_SEC
                    except Exception:
                        self._flash_until[identity] = 0.0
                self._last_metric_values[identity] = value
        for identity in list(self._last_metric_values):
            if identity not in active_keys:
                self._last_metric_values.pop(identity, None)
                self._flash_until.pop(identity, None)
        self._decorate_model_flash(model)
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
        delay_ms = _FLASH_TICK_MS
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

    def _schedule_keepalive_tick(self, delay_ms: int | None = None) -> None:
        if self._keepalive_after_id is not None:
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        try:
            self._keepalive_after_id = scheduler(
                _KEEPALIVE_TICK_MS if delay_ms is None else int(delay_ms),
                self._keepalive_tick,
            )
        except Exception:
            self._keepalive_after_id = None
        return

    def _keepalive_tick(self) -> None:
        self._keepalive_after_id = None
        window = self._window
        model = self._last_model
        if not isinstance(model, dict):
            return
        if not bool(model.get("visible", True)):
            return
        if self._is_fullscreen_active(window, model.get("geometry")):
            self._suppress_for_fullscreen()
            return
        if bool(self._fullscreen_suppressed):
            self._fullscreen_suppressed = False
            self.refresh()
            return
        if window is None:
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

    def _schedule_geometry_monitor_tick(self, delay_ms: int | None = None) -> None:
        if self._geometry_after_id is not None:
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        try:
            self._geometry_after_id = scheduler(
                _GEOMETRY_MONITOR_TICK_MS if delay_ms is None else int(delay_ms),
                self._geometry_monitor_tick,
            )
        except Exception:
            self._geometry_after_id = None
        return

    def _geometry_monitor_tick(self) -> None:
        self._geometry_after_id = None
        model = self._last_model
        if not isinstance(model, dict):
            return
        window = self._window
        if bool(model.get("visible", True)) and self._is_fullscreen_active(
            window,
            model.get("geometry"),
        ):
            self._suppress_for_fullscreen()
            return
        now = time.monotonic()
        hard_resample = (
            now - float(self._last_geometry_hard_resample_at)
            >= _GEOMETRY_MONITOR_HARD_RESAMPLE_SEC
        )
        if hard_resample:
            self._last_geometry_hard_resample_at = now
        try:
            runtime = self._runtime_getter()
        except Exception:
            runtime = {}
        model_now = _current_overlay_datetime()
        pre_model = build_codex_usage_taskbar_overlay_model(
            runtime,
            geometry=_DEFAULT_GEOMETRY,
            now=model_now,
        )
        preferred_width = _preferred_taskbar_overlay_width_for_model(pre_model)
        previous_geometry_context = self._cached_geometry_context
        geometry = self._calculate_geometry(
            force_resample=True,
            withdraw_for_sampling=False,
            preferred_width=preferred_width,
        )
        candidate_geometry_context = self._cached_geometry_context
        previous_geometry = model.get("geometry", {})
        if not isinstance(previous_geometry, dict):
            previous_geometry = {}
        geometry = self._stabilize_transient_geometry_regression(
            previous_geometry,
            geometry,
            previous_context=previous_geometry_context,
            candidate_context=candidate_geometry_context,
        )
        if self._cached_geometry_context is not None:
            self._cached_geometry = dict(geometry)
            self._geometry_invalidated = False
        geometry_changed = _geometry_changed(previous_geometry, geometry)
        updated_model = build_codex_usage_taskbar_overlay_model(
            runtime,
            geometry=geometry,
            now=model_now,
        )
        content_changed = _overlay_render_signature(model) != _overlay_render_signature(
            updated_model
        )
        if geometry_changed or content_changed:
            if bool(updated_model.get("visible")):
                if window is None:
                    window = self._ensure_window()
                if window is None:
                    self._last_model = updated_model
                    self._schedule_geometry_monitor_tick()
                    return
                if geometry_changed:
                    self._apply_geometry(window, geometry)
                self._update_metric_change_flash(updated_model)
                self._draw(updated_model)
                self._last_model = updated_model
                self._schedule_flash_tick_if_needed()
                try:
                    window.deiconify()
                except Exception:
                    pass
                self._force_native_repaint(window)
            else:
                if not bool(geometry.get("visible", True)):
                    self._withdraw_for_geometry_gap(updated_model)
                else:
                    self.hide()
                return
        self._schedule_geometry_monitor_tick()
        return

    def _clear_pending_regression_geometry(self) -> None:
        self._pending_regression_geometry = None
        self._pending_regression_context = None
        self._pending_regression_count = 0
        return

    def _stabilize_transient_geometry_regression(
        self,
        previous_geometry: dict[str, Any],
        candidate_geometry: dict[str, int | str],
        *,
        previous_context: Any,
        candidate_context: Any,
    ) -> dict[str, int | str]:
        if not _is_transient_geometry_regression(previous_geometry, candidate_geometry):
            self._clear_pending_regression_geometry()
            return candidate_geometry
        if previous_context is None or previous_context != candidate_context:
            self._clear_pending_regression_geometry()
            return candidate_geometry
        if _is_transient_geometry_x_shift(previous_geometry, candidate_geometry):
            self._pending_regression_geometry = dict(candidate_geometry)
            self._pending_regression_context = candidate_context
            self._pending_regression_count = int(self._pending_regression_count) + 1
            return dict(previous_geometry)
        if (
            isinstance(self._pending_regression_geometry, dict)
            and self._pending_regression_context == candidate_context
            and int(self._pending_regression_count) >= 1
        ):
            self._clear_pending_regression_geometry()
            return candidate_geometry
        self._pending_regression_geometry = dict(candidate_geometry)
        self._pending_regression_context = candidate_context
        self._pending_regression_count = 1
        return dict(previous_geometry)

    def _cancel_geometry_monitor_tick(self) -> None:
        after_id = self._geometry_after_id
        self._geometry_after_id = None
        if after_id is None:
            return
        canceller = getattr(self._root, "after_cancel", None)
        if callable(canceller):
            try:
                canceller(after_id)
            except Exception:
                pass
        return

    def _calculate_geometry(
        self,
        *,
        force_resample: bool = False,
        withdraw_for_sampling: bool = True,
        preferred_width: int | None = None,
    ) -> dict[str, int | str]:
        width = _root_int(self._root, "winfo_screenwidth", 1920)
        height = _root_int(self._root, "winfo_screenheight", 1080)
        try:
            work_area = self._work_area_getter()
        except Exception:
            work_area = None
        geometry = calculate_taskbar_overlay_geometry(
            width,
            height,
            work_area,
            preferred_width=preferred_width,
        )
        if str(geometry.get("orientation") or "") not in {"bottom", "top"}:
            self._cache_geometry(
                width,
                height,
                work_area,
                geometry,
                preferred_width=preferred_width,
            )
            return geometry
        context = self._geometry_context(
            width,
            height,
            work_area,
            geometry,
            preferred_width=preferred_width,
        )
        if (
            not bool(force_resample)
            and not bool(self._geometry_invalidated)
            and self._cached_geometry_context == context
            and isinstance(self._cached_geometry, dict)
        ):
            return dict(self._cached_geometry)
        occupied_spans = self._calculate_occupied_spans(
            width,
            height,
            work_area,
            geometry,
            withdraw_window=withdraw_for_sampling,
        )
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
            preferred_width=preferred_width,
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
        *,
        preferred_width: int | None = None,
    ) -> tuple[int, int, tuple[int, int, int, int], str, int]:
        return (
            int(width),
            int(height),
            _normalize_work_area(work_area, int(width), int(height)),
            str(geometry.get("orientation") or ""),
            int(preferred_width or 0),
        )

    def _cache_geometry(
        self,
        width: int,
        height: int,
        work_area: tuple[int, int, int, int] | dict[str, int] | None,
        geometry: dict[str, int | str],
        *,
        preferred_width: int | None = None,
    ) -> None:
        self._cached_geometry_context = self._geometry_context(
            width,
            height,
            work_area,
            geometry,
            preferred_width=preferred_width,
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
        *,
        withdraw_window: bool = True,
    ) -> list[tuple[int, int]] | None:
        window = self._window
        sampling_geometry = dict(geometry)
        if window is not None and bool(withdraw_window):
            try:
                window.withdraw()
                updater = getattr(self._root, "update_idletasks", None)
                if callable(updater):
                    updater()
                time.sleep(0.035)
            except Exception:
                pass
        elif window is not None:
            exclude_span = _current_horizontal_window_span(window)
            if exclude_span is not None:
                sampling_geometry["_exclude_spans"] = [exclude_span]
        try:
            return self._occupied_span_getter(width, height, work_area, sampling_geometry)
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
        row_entries = []
        for bar in bars[:2]:
            if not isinstance(bar, dict):
                continue
            row_entries.append(
                (
                    bar,
                    _metric_row_layout_for_overlay_width(
                        width,
                        _visible_metrics_for_taskbar_bar(bar),
                    ),
                )
            )
        overlay_badge_mode = _resolve_overlay_badge_mode(
            tuple(row_layout for _bar, row_layout in row_entries)
        )
        for index, (bar, row_layout) in enumerate(row_entries):
            y = 4 + index * row_height
            center_y = y + row_height // 2
            label = str(bar.get("label") or "")
            status_text = str(bar.get("status_text") or "")
            status_color = str(bar.get("status_color") or "#6b7280")
            canvas.create_text(
                6,
                center_y,
                anchor="w",
                fill="#e5e7eb",
                font=("Segoe UI", 8, "bold"),
                text=label[:8],
            )
            dot_x = 6 + row_layout.label_width + 1
            canvas.create_oval(
                dot_x,
                center_y - 4,
                dot_x + 8,
                center_y + 4,
                fill=status_color,
                outline=status_color,
            )
            if row_layout.status_width > 20:
                canvas.create_text(
                    dot_x + 10,
                    center_y,
                    anchor="w",
                    fill=status_color,
                    font=("Segoe UI", 6, "bold"),
                    text=status_text[:5],
                )
            for metric_index, metric in enumerate(row_layout.visible_metrics):
                segment_x = row_layout.metrics_x + metric_index * (
                    row_layout.segment_width + row_layout.segment_gap
                )
                self._draw_metric_segment(
                    canvas,
                    metric,
                    segment_x,
                    y,
                    row_layout.segment_width,
                    row_height,
                    progress_width=row_layout.progress_width,
                    badge_mode=overlay_badge_mode,
                )
        return

    def _draw_metric_segment(
        self,
        canvas: Any,
        metric: dict[str, Any],
        x: int,
        y: int,
        width: int,
        row_height: int,
        progress_width: int | None = None,
        badge_mode: str = "any",
    ) -> None:
        center_y = y + row_height // 2
        label = str(metric.get("key") or "")
        value_text = str(metric.get("value_text") or "--")
        reset_text = str(metric.get("reset_text") or "")
        reset_short_text = str(metric.get("reset_short_text") or reset_text)
        reset_color = str(metric.get("reset_color") or "#94a3b8")
        reset_marker = str(metric.get("reset_marker") or "")
        reset_badge_label = str(metric.get("reset_badge_label") or "")
        reset_badge_short_label = str(metric.get("reset_badge_short_label") or "")
        reset_badge_fill = str(metric.get("reset_badge_fill") or "")
        reset_badge_outline = str(metric.get("reset_badge_outline") or reset_badge_fill)
        reset_badge_text_color = str(metric.get("reset_badge_text_color") or "#f9fafb")
        has_reset_badge = bool(reset_badge_label or reset_badge_short_label)
        metric_key = str(metric.get("metric_key") or "")
        percent = int(metric.get("percent") or 0)
        color = str(metric.get("color") or "#6b7280")
        flash = bool(metric.get("flash"))
        flash_phase = bool(metric.get("flash_phase"))
        layout = _fit_metric_segment_layout(
            width,
            reset_text,
            reset_short_text,
            badge_label=reset_badge_label,
            badge_short_label=reset_badge_short_label,
            metric_key=metric_key,
            reset_marker=reset_marker,
            has_reset_badge=has_reset_badge,
            progress_width=progress_width,
            badge_mode=badge_mode,
        )
        bar_x = x + int(layout["bar_x"])
        bar_width = int(layout["progress_width"])
        value_x = x + int(layout["value_x"])
        reset_text_x = x + int(layout["reset_text_x"])
        badge_fit = dict(layout["badge_fit"])
        display_reset_text = str(layout["display_reset_text"])
        if bool(layout["placeholder_visible"]):
            reset_color = "#4b5563"
        show_reset = bool(display_reset_text)
        bar_y = y + max(4, (row_height - 7) // 2)
        if flash:
            _ = flash_phase
            canvas.create_rectangle(
                x - 2,
                y + 1,
                x + width + 1,
                y + row_height - 1,
                fill="#16181d",
                outline="#f59e0b",
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
        if badge_fit["badge_visible"] and has_reset_badge:
            badge_width = int(badge_fit["badge_width"])
            badge_y = center_y - (_RESET_BADGE_HEIGHT_PX // 2)
            badge_right_x = reset_text_x + badge_width
            # A visible badge fit is the drawability contract; default styling keeps
            # partial badge payloads from falling back to tiny marker-only text.
            badge_fill = reset_badge_fill or "#374151"
            badge_outline = reset_badge_outline or reset_color or badge_fill
            badge_text_color = reset_badge_text_color or "#f9fafb"
            canvas.create_rectangle(
                reset_text_x,
                badge_y,
                badge_right_x,
                badge_y + _RESET_BADGE_HEIGHT_PX,
                fill=badge_fill,
                outline=badge_outline,
            )
            canvas.create_text(
                reset_text_x + badge_width // 2,
                center_y,
                anchor="center",
                fill=badge_text_color,
                font=("Segoe UI", 6, "bold"),
                text=str(badge_fit["badge_label"]),
            )
            time_text = str(badge_fit["time_text"] or "")
            if time_text:
                canvas.create_text(
                    badge_right_x + _RESET_BADGE_TIME_GAP_PX,
                    center_y,
                    anchor="w",
                    fill=reset_color,
                    font=("Segoe UI", 6, "bold"),
                    text=time_text,
                )
        elif show_reset:
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

    def _is_fullscreen_active(
        self,
        window: Any | None,
        geometry: dict[str, Any] | None = None,
    ) -> bool:
        detector = self._fullscreen_detector
        if callable(detector):
            try:
                return bool(detector(window))
            except Exception:
                return False
        if window is None and _get_window_handle(self._root) <= 0:
            return False
        overlay_hwnd = _get_window_handle(window) if window is not None else 0
        if window is not None and int(overlay_hwnd) <= 0:
            return False
        return _is_foreground_fullscreen(int(overlay_hwnd), self._root, geometry)

    def _suppress_for_fullscreen(self) -> None:
        self._fullscreen_suppressed = True
        window = self._window
        if window is not None:
            try:
                window.withdraw()
            except Exception:
                pass
        self._cancel_flash_tick()
        self._schedule_keepalive_tick(delay_ms=_FULLSCREEN_POLL_MS)
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
    *,
    preferred_width: int | None = None,
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
    if not free_spans:
        fitted["visible"] = False
        fitted["width"] = 0
        fitted["height"] = 0
        return fitted

    start, end = max(free_spans, key=lambda span: (int(span[1]), int(span[0])))
    available = max(0, int(end) - int(start))
    if available < _MIN_COMPACT_EMPTY_SLOT_WIDTH_PX:
        fitted["visible"] = False
        fitted["width"] = 0
        fitted["height"] = 0
        return fitted

    if preferred_width is None:
        target_width = desired_width
    else:
        target_width = min(
            max(_MIN_COMPACT_EMPTY_SLOT_WIDTH_PX, int(preferred_width)),
            desired_width,
        )
    width = min(target_width, available)
    fitted["width"] = int(width)
    fitted["x"] = int(max(start, end - width))
    fitted["visible"] = True
    return fitted


def _geometry_changed(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance_px: int = _GEOMETRY_CHANGE_TOLERANCE_PX,
) -> bool:
    if bool(previous.get("visible", True)) != bool(current.get("visible", True)):
        return True
    if str(previous.get("orientation") or "") != str(current.get("orientation") or ""):
        return True
    try:
        previous_width = int(previous.get("width", 0))
        current_width = int(current.get("width", 0))
    except Exception:
        return True
    if previous_width != current_width:
        if _crosses_overlay_status_text_threshold(previous_width, current_width):
            return True
        if abs(current_width - previous_width) > int(tolerance_px):
            return True
    for key in ("x", "y", "height"):
        try:
            before = int(previous.get(key, 0))
            after = int(current.get(key, 0))
        except Exception:
            return True
        if abs(after - before) > int(tolerance_px):
            return True
    return False


def _is_transient_geometry_regression(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance_px: int = _GEOMETRY_CHANGE_TOLERANCE_PX,
) -> bool:
    if not bool(previous.get("visible", True)):
        return False
    if str(previous.get("orientation") or "") != str(current.get("orientation") or ""):
        return False
    try:
        previous_width = int(previous.get("width", 0))
    except Exception:
        return False
    if previous_width <= 0:
        return False
    if not bool(current.get("visible", True)):
        return True
    try:
        current_width = int(current.get("width", 0))
        previous_y = int(previous.get("y", 0))
        current_y = int(current.get("y", 0))
        previous_height = int(previous.get("height", 0))
        current_height = int(current.get("height", 0))
    except Exception:
        return False
    if abs(current_y - previous_y) > int(tolerance_px):
        return False
    if abs(current_height - previous_height) > int(tolerance_px):
        return False
    if current_width < previous_width - int(tolerance_px):
        return True
    return _is_transient_geometry_x_shift(
        previous,
        current,
        tolerance_px=tolerance_px,
    )


def _is_transient_geometry_x_shift(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance_px: int = _GEOMETRY_CHANGE_TOLERANCE_PX,
) -> bool:
    if not bool(previous.get("visible", True)):
        return False
    if not bool(current.get("visible", True)):
        return False
    if str(previous.get("orientation") or "") != str(current.get("orientation") or ""):
        return False
    try:
        previous_width = int(previous.get("width", 0))
        current_width = int(current.get("width", 0))
        previous_x = int(previous.get("x", 0))
        current_x = int(current.get("x", 0))
        previous_y = int(previous.get("y", 0))
        current_y = int(current.get("y", 0))
        previous_height = int(previous.get("height", 0))
        current_height = int(current.get("height", 0))
    except Exception:
        return False
    if abs(current_width - previous_width) > int(tolerance_px):
        return False
    if abs(current_y - previous_y) > int(tolerance_px):
        return False
    if abs(current_height - previous_height) > int(tolerance_px):
        return False
    x_delta = abs(current_x - previous_x)
    return int(tolerance_px) < x_delta <= _GEOMETRY_TRANSIENT_X_SHIFT_TOLERANCE_PX


def _crosses_overlay_status_text_threshold(previous_width: int, current_width: int) -> bool:
    threshold = int(_STATUS_TEXT_MIN_OVERLAY_WIDTH_PX)
    return (
        (int(previous_width) < threshold <= int(current_width))
        or (int(current_width) < threshold <= int(previous_width))
    )


def _current_horizontal_window_span(window: Any) -> tuple[int, int] | None:
    hwnd = _get_window_handle(window)
    if hwnd <= 0 or win32gui is None:
        return None
    try:
        left, _top, right, _bottom = [int(v) for v in win32gui.GetWindowRect(hwnd)]
    except Exception:
        return None
    if right <= left:
        return None
    return (int(left), int(right))


def _geometry_exclude_spans(
    geometry: dict[str, Any],
    screen_width: int,
) -> list[tuple[int, int]]:
    raw = geometry.get("_exclude_spans") if isinstance(geometry, dict) else None
    if not isinstance(raw, list):
        return []
    spans = []
    for item in raw:
        try:
            start, end = item
        except Exception:
            continue
        start_i = max(0, min(int(screen_width), int(start)))
        end_i = max(0, min(int(screen_width), int(end)))
        if end_i > start_i:
            spans.append((start_i, end_i))
    return _merge_spans(spans)


def _span_overlaps_any(
    start: int,
    end: int,
    spans: list[tuple[int, int]],
) -> bool:
    return any(int(start) < span_end and int(end) > span_start for span_start, span_end in spans)


def _subtract_spans(
    spans: list[tuple[int, int]],
    excluded: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if not spans or not excluded:
        return list(spans)
    output: list[tuple[int, int]] = []
    for start, end in spans:
        fragments = [(int(start), int(end))]
        for ex_start, ex_end in excluded:
            next_fragments: list[tuple[int, int]] = []
            for frag_start, frag_end in fragments:
                if ex_end <= frag_start or ex_start >= frag_end:
                    next_fragments.append((frag_start, frag_end))
                    continue
                if ex_start > frag_start:
                    next_fragments.append((frag_start, ex_start))
                if ex_end < frag_end:
                    next_fragments.append((ex_end, frag_end))
            fragments = next_fragments
            if not fragments:
                break
        output.extend(
            (frag_start, frag_end)
            for frag_start, frag_end in fragments
            if frag_end > frag_start
        )
    return output


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
    geometry: dict[str, Any],
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

    excluded_spans = _geometry_exclude_spans(geometry, int(screen_width))
    occupied = _taskbar_child_occupied_spans(
        int(screen_width),
        int(band_top),
        int(band_bottom),
    )
    if excluded_spans:
        occupied = _subtract_spans(occupied, excluded_spans)
    sample_rows = _taskbar_sample_rows(band_top, band_bottom)
    columns = _sample_taskbar_columns(int(screen_width), sample_rows)
    if not columns and not occupied:
        return None
    if columns:
        background = _median_background_color(columns)
        for x, colors in columns:
            if excluded_spans and _span_overlaps_any(
                int(x),
                int(x) + _TASKBAR_SAMPLE_STEP_PX,
                excluded_spans,
            ):
                continue
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


def _taskbar_child_occupied_spans(
    screen_width: int,
    band_top: int,
    band_bottom: int,
) -> list[tuple[int, int]]:
    if win32gui is None:
        return []
    try:
        taskbar_hwnd = int(win32gui.FindWindow("Shell_TrayWnd", None) or 0)
    except Exception:
        return []
    if taskbar_hwnd <= 0:
        return []

    spans: list[tuple[int, int]] = []

    def visit(hwnd, _extra) -> bool:
        try:
            class_name = str(win32gui.GetClassName(hwnd) or "")
        except Exception:
            return True
        if class_name not in _TASKBAR_OCCUPIED_CHILD_CLASSES:
            return True
        try:
            if not bool(win32gui.IsWindowVisible(hwnd)):
                return True
        except Exception:
            return True
        try:
            left, top, right, bottom = [int(v) for v in win32gui.GetWindowRect(hwnd)]
        except Exception:
            return True
        if right <= left or bottom <= top:
            return True
        vertical_overlap = min(int(bottom), int(band_bottom)) - max(int(top), int(band_top))
        if vertical_overlap < 8:
            return True
        start = max(0, min(int(screen_width), int(left)))
        end = max(0, min(int(screen_width), int(right)))
        if end - start >= 8:
            spans.append((start, end))
        return True

    try:
        win32gui.EnumChildWindows(taskbar_hwnd, visit, None)
    except Exception:
        return spans
    return spans


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
    background: tuple[int, int, int],
) -> bool:
    if not colors:
        return False
    vertical_spread = 0
    for left_color in colors:
        for right_color in colors:
            vertical_spread = max(vertical_spread, _rgb_distance(left_color, right_color))
    if vertical_spread >= 38:
        return True
    average = tuple(
        int(round(sum(color[index] for color in colors) / len(colors)))
        for index in range(3)
    )
    return _rgb_distance(average, background) >= 52


def _is_foreground_fullscreen(
    overlay_hwnd: int,
    root: Any | None = None,
    target_geometry: dict[str, Any] | None = None,
) -> bool:
    if win32gui is None or win32con is None:
        return False
    target_monitor_rect = _target_monitor_rect(int(overlay_hwnd), target_geometry, root)
    try:
        hwnd = int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        hwnd = 0
    if _window_covers_target_monitor(
        hwnd,
        int(overlay_hwnd),
        root,
        target_monitor_rect,
    ):
        return True
    return _visible_fullscreen_window_exists(
        int(overlay_hwnd),
        root,
        target_monitor_rect,
    )


def _target_monitor_rect(
    overlay_hwnd: int,
    target_geometry: dict[str, Any] | None,
    root: Any | None,
) -> tuple[int, int, int, int] | None:
    geometry_rect = _geometry_screen_rect(target_geometry)
    if geometry_rect is not None:
        rect_monitor = _monitor_rect_from_rect(geometry_rect)
        if rect_monitor is not None:
            return rect_monitor
    if int(overlay_hwnd) > 0:
        hwnd_monitor = _foreground_monitor_rect(int(overlay_hwnd), root)
        if hwnd_monitor is not None:
            return hwnd_monitor
    if geometry_rect is not None:
        return geometry_rect
    return None


def _visible_fullscreen_window_exists(
    overlay_hwnd: int,
    root: Any | None,
    target_monitor_rect: tuple[int, int, int, int] | None,
) -> bool:
    enum_windows = getattr(win32gui, "EnumWindows", None)
    if not callable(enum_windows):
        return False
    found = False

    def visit(hwnd: int, _extra: Any) -> bool:
        nonlocal found
        if _window_covers_target_monitor(
            int(hwnd),
            int(overlay_hwnd),
            root,
            target_monitor_rect,
        ):
            found = True
        return True

    try:
        enum_windows(visit, None)
    except Exception:
        return False
    return bool(found)


def _window_covers_target_monitor(
    hwnd: int,
    overlay_hwnd: int,
    root: Any | None,
    target_monitor_rect: tuple[int, int, int, int] | None,
) -> bool:
    hwnd = int(hwnd or 0)
    if hwnd <= 0 or hwnd == int(overlay_hwnd):
        return False
    try:
        root_hwnd = int(win32gui.GetAncestor(hwnd, 2) or hwnd)
    except Exception:
        root_hwnd = hwnd
    if root_hwnd == int(overlay_hwnd):
        return False
    try:
        class_name = str(win32gui.GetClassName(hwnd) or "")
    except Exception:
        class_name = ""
    if class_name in _FULLSCREEN_EXCLUDED_WINDOW_CLASSES:
        return False
    try:
        title = str(win32gui.GetWindowText(hwnd) or "")
    except Exception:
        title = ""
    if title in _FULLSCREEN_EXCLUDED_WINDOW_TITLES:
        return False
    try:
        if not bool(win32gui.IsWindowVisible(hwnd)):
            return False
        if bool(win32gui.IsIconic(hwnd)):
            return False
    except Exception:
        pass
    try:
        left, top, right, bottom = [int(v) for v in win32gui.GetWindowRect(hwnd)]
    except Exception:
        return False
    if right <= left or bottom <= top:
        return False

    monitor_rect = _foreground_monitor_rect(hwnd, root)
    if monitor_rect is None:
        return False
    if target_monitor_rect is not None and not _rects_close(
        monitor_rect,
        target_monitor_rect,
    ):
        return False
    tolerance = 2
    candidate_rects = [monitor_rect]
    root_screen_rect = _root_screen_rect(root)
    if root_screen_rect is not None and not _rects_close(
        root_screen_rect,
        monitor_rect,
        tolerance=tolerance,
    ):
        candidate_rects.append(root_screen_rect)
    for m_left, m_top, m_right, m_bottom in candidate_rects:
        if (
            left <= m_left + tolerance
            and top <= m_top + tolerance
            and right >= m_right - tolerance
            and bottom >= m_bottom - tolerance
        ):
            return _window_is_top_at_probe_points(
                root_hwnd,
                (m_left, m_top, m_right, m_bottom),
            )
    return False


def _root_screen_rect(root: Any | None) -> tuple[int, int, int, int] | None:
    if root is None:
        return None
    width = _root_int(root, "winfo_screenwidth", 0)
    height = _root_int(root, "winfo_screenheight", 0)
    if width <= 0 or height <= 0:
        return None
    return 0, 0, int(width), int(height)


def _window_is_top_at_probe_points(
    root_hwnd: int,
    rect: tuple[int, int, int, int],
) -> bool:
    window_from_point = getattr(win32gui, "WindowFromPoint", None)
    if not callable(window_from_point):
        return True
    left, top, right, bottom = [int(v) for v in rect]
    width = max(1, right - left)
    height = max(1, bottom - top)
    probe_points = [
        (left + width // 2, top + height // 2),
        (left + width // 4, top + height // 3),
        (right - width // 4, top + height // 3),
    ]
    for x, y in probe_points:
        try:
            hit = int(window_from_point((int(x), int(y))) or 0)
        except Exception:
            continue
        if hit <= 0:
            continue
        try:
            hit_root = int(win32gui.GetAncestor(hit, 2) or hit)
        except Exception:
            hit_root = hit
        if hit_root == int(root_hwnd):
            return True
    return False


def _geometry_screen_rect(
    geometry: dict[str, Any] | None,
) -> tuple[int, int, int, int] | None:
    if not isinstance(geometry, dict):
        return None
    try:
        x = int(geometry.get("x", 0))
        y = int(geometry.get("y", 0))
        width = int(geometry.get("width", 0))
        height = int(geometry.get("height", 0))
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (x, y, x + width, y + height)


def _monitor_rect_from_rect(
    rect: tuple[int, int, int, int],
) -> tuple[int, int, int, int] | None:
    if win32api is None or win32con is None:
        return None
    monitor_from_rect = getattr(win32api, "MonitorFromRect", None)
    if not callable(monitor_from_rect):
        return None
    try:
        monitor = monitor_from_rect(
            tuple(int(v) for v in rect),
            int(getattr(win32con, "MONITOR_DEFAULTTONEAREST", 2)),
        )
        info = win32api.GetMonitorInfo(monitor)
        monitor_rect = info.get("Monitor")
        if monitor_rect is not None and len(monitor_rect) == 4:
            return tuple(int(v) for v in monitor_rect)
    except Exception:
        return None
    return None


def _rects_close(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
    *,
    tolerance: int = 2,
) -> bool:
    return all(abs(int(a) - int(b)) <= int(tolerance) for a, b in zip(left, right))


def _foreground_monitor_rect(hwnd: int, root: Any | None) -> tuple[int, int, int, int] | None:
    if win32api is not None and win32con is not None:
        try:
            monitor = win32api.MonitorFromWindow(
                int(hwnd),
                int(getattr(win32con, "MONITOR_DEFAULTTONEAREST", 2)),
            )
            info = win32api.GetMonitorInfo(monitor)
            rect = info.get("Monitor")
            if rect is not None and len(rect) == 4:
                return tuple(int(v) for v in rect)
        except Exception:
            pass
    width = _root_int(root, "winfo_screenwidth", 0) if root is not None else 0
    height = _root_int(root, "winfo_screenheight", 0) if root is not None else 0
    if width > 0 and height > 0:
        return 0, 0, int(width), int(height)
    return None


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
    screen_width = max(1, int(screen_width or 0))
    screen_height = max(1, int(screen_height or 0))
    if isinstance(work_area, dict):
        raw = (
            int(work_area.get("left", 0)),
            int(work_area.get("top", 0)),
            int(work_area.get("right", screen_width)),
            int(work_area.get("bottom", screen_height)),
        )
    elif isinstance(work_area, tuple) and len(work_area) == 4:
        raw = tuple(int(v) for v in work_area)
    else:
        raw = (0, 0, int(screen_width), int(screen_height))

    left, top, right, bottom = raw
    left = max(0, min(int(left), screen_width))
    top = max(0, min(int(top), screen_height))
    right = max(0, min(int(right), screen_width))
    bottom = max(0, min(int(bottom), screen_height))
    if right <= left or bottom <= top:
        return 0, 0, int(screen_width), int(screen_height)
    return int(left), int(top), int(right), int(bottom)


def _build_metric(
    *,
    key: str,
    short_label: str,
    raw_value: Any,
    reset_at_value: Any = "",
    captured_at_value: Any = "",
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
    snapshot_reset_direction = _snapshot_reset_direction(
        metric_key=key,
        current_percent=percent,
        reset_at_value=reset_at_value,
        captured_at_value=captured_at_value,
    )
    state = _bar_state(enabled, percent)
    color = _bar_color(enabled, percent)
    reset_state = reset_info["state"]
    reset_color = reset_info["color"]
    reset_direction = _RESET_DIRECTION_UNKNOWN
    known_reset_directions = {
        _RESET_DIRECTION_SHORTAGE,
        _RESET_DIRECTION_ON_TRACK,
        _RESET_DIRECTION_SURPLUS,
    }
    reset_info_direction = str(reset_info.get("direction") or "")
    if enabled:
        if snapshot_reset_direction in known_reset_directions:
            reset_direction = snapshot_reset_direction
        elif reset_info_direction in known_reset_directions:
            reset_direction = reset_info_direction
    reset_profile = _reset_direction_profile(reset_direction)
    reset_marker = reset_profile["marker"]
    if reset_direction != _RESET_DIRECTION_UNKNOWN:
        reset_state = reset_profile["state"]
        reset_color = reset_profile["color"]
    # `reset_direction` is the semantic source; state/color stay for presentation compatibility.
    return {
        "metric_key": str(key),
        "key": str(short_label),
        "percent": 0 if percent is None else int(percent),
        "value_text": "--" if percent is None else f"{int(percent)}%",
        "state": state,
        "color": color,
        "reset_text": reset_info["text"],
        "reset_short_text": reset_info["short_text"],
        "reset_state": reset_state,
        "reset_color": reset_color,
        "reset_direction": reset_direction,
        "reset_marker": reset_marker,
        "reset_badge_label": reset_profile["badge_label"],
        "reset_badge_short_label": reset_profile["badge_short_label"],
        "reset_badge_fill": reset_profile["badge_fill"],
        "reset_badge_outline": reset_profile["badge_outline"],
        "reset_badge_text_color": reset_profile["badge_text_color"],
        "flash": False,
        "flash_phase": False,
    }


def _snapshot_reset_direction(
    *,
    metric_key: str,
    current_percent: int | None,
    reset_at_value: Any,
    captured_at_value: Any,
) -> str | None:
    if current_percent is None:
        return None
    reset_at = _parse_reset_datetime(reset_at_value)
    if reset_at is None:
        return None
    captured_at = _parse_reset_datetime(captured_at_value)
    if captured_at is None:
        return None
    captured_at = _align_snapshot_datetime(captured_at, reset_at)
    window_seconds = _snapshot_window_seconds(metric_key)
    if window_seconds is None:
        return None
    window_start = reset_at - timedelta(seconds=int(window_seconds))
    elapsed_seconds = (captured_at - window_start).total_seconds()
    remaining_seconds = (reset_at - captured_at).total_seconds()
    if elapsed_seconds <= 0.0 or remaining_seconds < 0.0:
        return None
    current_remaining = float(max(0, min(100, int(current_percent))))
    consumed = 100.0 - current_remaining
    rate_per_second = consumed / float(elapsed_seconds)
    projected_remaining = current_remaining - (rate_per_second * float(remaining_seconds))
    if projected_remaining < 0.0:
        return _RESET_DIRECTION_SHORTAGE
    if projected_remaining <= _SNAPSHOT_ON_TRACK_MAX_PROJECTED_REMAINING_PERCENT:
        return _RESET_DIRECTION_ON_TRACK
    return _RESET_DIRECTION_SURPLUS


def _snapshot_window_seconds(metric_key: str) -> int | None:
    value = _SNAPSHOT_WINDOW_SECONDS_BY_METRIC.get(str(metric_key or ""))
    if value is None:
        return None
    return int(value)


def _align_snapshot_datetime(value: datetime, reference: datetime) -> datetime:
    if reference.tzinfo is None:
        if value.tzinfo is None:
            return value
        return value.astimezone().replace(tzinfo=None)
    if value.tzinfo is None:
        return value.replace(tzinfo=_SNAPSHOT_CAPTURED_AT_FALLBACK_TZ).astimezone(
            reference.tzinfo
        )
    return value.astimezone(reference.tzinfo)


def _reset_direction_profile(direction: str) -> dict[str, str]:
    normalized = str(direction or _RESET_DIRECTION_UNKNOWN)
    if normalized not in _RESET_DIRECTION_MARKERS:
        normalized = _RESET_DIRECTION_UNKNOWN
    state = _RESET_DIRECTION_STATES.get(normalized, "unknown")
    return {
        "direction": normalized,
        "marker": _RESET_DIRECTION_MARKERS.get(normalized, ""),
        "state": state,
        "color": _reset_color(state),
        "badge_label": _RESET_BADGE_LABELS.get(normalized, ""),
        "badge_short_label": _RESET_BADGE_SHORT_LABELS.get(normalized, ""),
        "badge_fill": _RESET_BADGE_FILLS.get(normalized, ""),
        "badge_outline": _RESET_BADGE_OUTLINES.get(normalized, ""),
        "badge_text_color": _RESET_BADGE_TEXT_COLORS.get(normalized, ""),
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
        profile = _reset_direction_profile(_RESET_DIRECTION_UNKNOWN)
        return {
            "text": "",
            "short_text": "",
            "state": "unknown",
            "color": "#6b7280",
            "direction": profile["direction"],
            "marker": profile["marker"],
        }
    current = _reset_now(parsed, now)
    seconds = int((parsed - current).total_seconds())
    if seconds <= 0:
        text = _format_reset_remaining_detail(0, metric_key=metric_key)
        if str(metric_key or "") == "five_hour_limit":
            direction = (
                _RESET_DIRECTION_SURPLUS
                if percent is not None and int(percent) >= 60
                else _RESET_DIRECTION_SHORTAGE
            )
            profile = _reset_direction_profile(direction)
        else:
            profile = _reset_direction_profile(
                _reset_action_direction(metric_key, percent, 0)
            )
        return {
            "text": text,
            "short_text": text,
            "state": profile["state"],
            "color": profile["color"],
            "direction": profile["direction"],
            "marker": profile["marker"],
        }
    if not _reset_remaining_is_plausible(metric_key, seconds):
        profile = _reset_direction_profile(_RESET_DIRECTION_UNKNOWN)
        return {
            "text": "",
            "short_text": "",
            "state": "unknown",
            "color": "#6b7280",
            "direction": profile["direction"],
            "marker": profile["marker"],
        }
    direction = _reset_action_direction(metric_key, percent, seconds)
    profile = _reset_direction_profile(direction)
    return {
        "text": _format_reset_remaining_detail(seconds, metric_key=metric_key),
        "short_text": _format_reset_remaining_compact(seconds, metric_key=metric_key),
        "state": profile["state"],
        "color": profile["color"],
        "direction": profile["direction"],
        "marker": profile["marker"],
    }


def _parse_reset_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _reset_remaining_is_plausible(metric_key: str, seconds: int) -> bool:
    if str(metric_key or "").endswith("five_hour_limit"):
        return int(seconds) <= _FIVE_HOUR_RESET_MAX_SECONDS
    return True


def _reset_now(reset_at: datetime, now: datetime | None) -> datetime:
    current = datetime.now(reset_at.tzinfo) if now is None else now
    if reset_at.tzinfo is None:
        return current.replace(tzinfo=None)
    if current.tzinfo is None:
        return current.astimezone(reset_at.tzinfo)
    return current.astimezone(reset_at.tzinfo)


def _current_overlay_datetime() -> datetime:
    return datetime.now().astimezone()


def _format_reset_remaining_detail(seconds: int, *, metric_key: str = "") -> str:
    seconds = max(0, int(seconds))
    total_minutes = max(0, (seconds + 59) // 60)
    days = int(total_minutes // 1440)
    hours = int((total_minutes % 1440) // 60)
    minutes = int(total_minutes % 60)
    if str(metric_key or "") == "five_hour_limit":
        total_hours = int(total_minutes // 60)
        return f"{total_hours:02d}h {minutes:02d}m"
    return f"{days}d {hours:02d}h {minutes:02d}m"


def _format_reset_remaining_compact(seconds: int, *, metric_key: str = "") -> str:
    return _format_reset_remaining_detail(seconds, metric_key=metric_key)


def _display_reset_text_for_width(
    detail_text: str,
    short_text: str,
    width: int,
    *,
    reset_marker: str = "",
) -> str:
    return _display_reset_text_for_space(
        detail_text,
        short_text,
        reset_marker=reset_marker,
        available_px=max(0, int(width) // 2),
    )


def _display_reset_text_for_space(
    detail_text: str,
    short_text: str,
    *,
    metric_key: str = "",
    reset_marker: str = "",
    available_px: int,
) -> str:
    candidates = []
    detail = str(detail_text or "")
    short = str(short_text or "")
    marker = str(reset_marker or "")
    if marker:
        if detail:
            candidates.append(f"{marker} {detail}")
        if short and short != detail:
            candidates.append(f"{marker} {short}")
    else:
        if detail:
            candidates.append(detail)
        if short and short != detail:
            candidates.append(short)
    for text in candidates:
        if _reset_column_width_for_text(text, metric_key=metric_key) <= int(available_px):
            return text
    return ""


def _fit_metric_segment_layout(
    width: int,
    detail_text: str,
    short_text: str,
    *,
    badge_label: str,
    badge_short_label: str,
    metric_key: str = "",
    reset_marker: str = "",
    has_reset_badge: bool = False,
    progress_width: int | None = None,
    badge_mode: str = "any",
) -> dict[str, Any]:
    segment_width = max(0, int(width))
    label_width = 14
    value_width = _VALUE_COLUMN_MAX_WIDTH_PX
    label_to_bar_gap = 3
    bar_to_value_gap = 3
    reset_gap = 4
    bar_x = label_width + label_to_bar_gap
    reset_right_x = segment_width - _SEGMENT_RIGHT_PADDING_PX
    max_progress_width = max(
        6,
        reset_right_x - reset_gap - value_width - bar_to_value_gap - bar_x,
    )
    requested_progress_width = (
        _metric_progress_width_for_segment(segment_width)
        if progress_width is None
        else int(progress_width)
    )
    target_progress_width = max(
        6,
        min(
            int(requested_progress_width),
            int(max_progress_width),
            _METRIC_PROGRESS_MAX_WIDTH_PX,
        ),
    )
    minimum_progress_width = max(
        6,
        min(_METRIC_PROGRESS_MIN_WIDTH_PX, int(max_progress_width)),
    )

    hidden_badge = {
        "badge_visible": False,
        "badge_label": "",
        "badge_width": 0,
        "time_text": "",
        "total_width": 0,
        "variant": "hidden",
    }
    best: dict[str, Any] | None = None
    best_score: tuple[int, int, int, int, int] | None = None
    detail = str(detail_text or "")
    short = str(short_text or "")
    full_badge_label = str(badge_label or "")
    compact_badge_label = str(badge_short_label or full_badge_label)
    normalized_badge_mode = _normalized_badge_mode(badge_mode)

    def score_candidate(
        *,
        progress: int,
        badge_fit: dict[str, Any],
        display_text: str,
    ) -> tuple[int, int, int, int, int]:
        time_text = str(badge_fit.get("time_text") or display_text or "")
        badge_text = str(badge_fit.get("badge_label") or "")
        time_quality = 0
        if time_text:
            time_quality = 2 if time_text == detail else 1
        badge_quality = 0
        if bool(badge_fit.get("badge_visible")):
            badge_quality = 2 if badge_text == full_badge_label else 1
        return (
            1 if time_text else 0,
            1 if time_text and badge_quality else 0,
            badge_quality,
            time_quality,
            int(progress),
        )

    for candidate_progress in range(
        int(target_progress_width),
        int(minimum_progress_width) - 1,
        -1,
    ):
        reset_text_x = (
            bar_x
            + int(candidate_progress)
            + bar_to_value_gap
            + value_width
            + reset_gap
        )
        value_x = bar_x + int(candidate_progress) + bar_to_value_gap + value_width
        reset_available_px = max(0, reset_right_x - reset_text_x)
        badge_fit = _fit_reset_badge_for_space(
            detail,
            short,
            badge_label=full_badge_label,
            badge_short_label=compact_badge_label,
            metric_key=metric_key,
            available_px=reset_available_px,
            badge_mode=normalized_badge_mode,
        )
        display_reset_text = ""
        placeholder_visible = False
        if not badge_fit["badge_visible"] and badge_fit["time_text"]:
            display_reset_text = str(badge_fit["time_text"])
        elif not badge_fit["badge_visible"] and not has_reset_badge:
            display_reset_text = _display_reset_text_for_space(
                detail,
                short,
                metric_key=metric_key,
                reset_marker="",
                available_px=reset_available_px,
            )
            if not display_reset_text and not detail:
                display_reset_text = _display_reset_text_for_space(
                    _RESET_PLACEHOLDER_TEXT,
                    _RESET_PLACEHOLDER_TEXT,
                    metric_key=metric_key,
                    reset_marker="",
                    available_px=reset_available_px,
                )
                placeholder_visible = bool(display_reset_text)

        score = score_candidate(
            progress=int(candidate_progress),
            badge_fit=badge_fit,
            display_text=display_reset_text,
        )
        if best_score is None or score > best_score:
            best_score = score
            best = {
                "progress_width": int(candidate_progress),
                "bar_x": int(bar_x),
                "value_x": int(value_x),
                "reset_text_x": int(reset_text_x),
                "badge_fit": badge_fit,
                "display_reset_text": display_reset_text,
                "placeholder_visible": placeholder_visible,
                "variant": str(badge_fit.get("variant") or "reset_text"),
            }

    if best is not None:
        return best
    fallback_progress_width = max(
        6,
        min(int(target_progress_width), int(max_progress_width)),
    )
    return {
        "progress_width": fallback_progress_width,
        "bar_x": int(bar_x),
        "value_x": int(
            bar_x
            + fallback_progress_width
            + bar_to_value_gap
            + value_width
        ),
        "reset_text_x": int(
            bar_x
            + fallback_progress_width
            + bar_to_value_gap
            + value_width
            + reset_gap
        ),
        "badge_fit": hidden_badge,
        "display_reset_text": "",
        "placeholder_visible": False,
        "variant": "hidden",
    }


def _fit_reset_badge_for_space(
    detail_text: str,
    short_text: str,
    *,
    badge_label: str,
    badge_short_label: str,
    metric_key: str = "",
    available_px: int,
    badge_mode: str = "any",
) -> dict[str, Any]:
    available = max(0, int(available_px))
    full_label = str(badge_label or badge_short_label or "")
    compact_label = str(badge_short_label or full_label)
    detail = str(detail_text or "")
    short = str(short_text or "")
    normalized_badge_mode = _normalized_badge_mode(badge_mode)

    hidden = {
        "badge_visible": False,
        "badge_label": "",
        "badge_width": 0,
        "time_text": "",
        "total_width": 0,
        "variant": "hidden",
    }
    if not full_label:
        return hidden

    full_badge_width = _reset_badge_width_for_label(full_label)
    short_badge_width = _reset_badge_width_for_label(compact_label)
    candidates: list[tuple[str, str, int, str, int]] = []
    allow_full_badge = normalized_badge_mode in {"any", "full"}
    allow_short_badge = normalized_badge_mode in {"any", "short"}
    force_visible_badge = normalized_badge_mode in {"full", "short"}

    def add_candidate(
        variant: str,
        label: str,
        badge_width: int,
        time_text: str,
        total_width: int,
    ) -> None:
        candidate = (variant, label, badge_width, time_text, total_width)
        if candidate not in candidates:
            candidates.append(candidate)

    if detail:
        detail_width = _reset_column_width_for_text(detail, metric_key=metric_key)
        if allow_full_badge:
            add_candidate(
                "badge_detail",
                full_label,
                full_badge_width,
                detail,
                full_badge_width + _RESET_BADGE_TIME_GAP_PX + detail_width,
            )
        if allow_short_badge and compact_label:
            add_candidate(
                "badge_short_detail",
                compact_label,
                short_badge_width,
                detail,
                short_badge_width + _RESET_BADGE_TIME_GAP_PX + detail_width,
            )
    if short and short != detail:
        short_width = _reset_column_width_for_text(short, metric_key=metric_key)
        if allow_full_badge:
            add_candidate(
                "badge_short",
                full_label,
                full_badge_width,
                short,
                full_badge_width + _RESET_BADGE_TIME_GAP_PX + short_width,
            )
        if allow_short_badge and compact_label:
            add_candidate(
                "badge_short_time",
                compact_label,
                short_badge_width,
                short,
                short_badge_width + _RESET_BADGE_TIME_GAP_PX + short_width,
            )
    if force_visible_badge:
        if allow_full_badge:
            add_candidate("badge_only", full_label, full_badge_width, "", full_badge_width)
        if allow_short_badge and compact_label:
            add_candidate(
                "badge_short_only",
                compact_label,
                short_badge_width,
                "",
                short_badge_width,
            )
    if not force_visible_badge:
        if detail:
            add_candidate("time_detail", "", 0, detail, detail_width)
        if short and short != detail:
            add_candidate("time_short", "", 0, short, short_width)
        if allow_full_badge:
            add_candidate("badge_only", full_label, full_badge_width, "", full_badge_width)
        if allow_short_badge and compact_label:
            add_candidate(
                "badge_short_only",
                compact_label,
                short_badge_width,
                "",
                short_badge_width,
            )

    for variant, label, badge_width, time_text, total_width in candidates:
        if total_width <= available:
            badge_visible = bool(label and badge_width > 0)
            return {
                "badge_visible": badge_visible,
                "badge_label": label,
                "badge_width": badge_width if badge_visible else 0,
                "time_text": time_text,
                "total_width": total_width,
                "variant": variant,
            }
    return hidden


def _reset_badge_width_for_label(label: str) -> int:
    text = str(label or "")
    if not text:
        return 0
    # Keep this helper pure for deterministic unit tests. The estimate is
    # deliberately conservative so unknown DPI/font details bias toward hiding
    # reset time instead of overlapping the badge.
    label_width = sum(9 if ord(ch) > 127 else 6 for ch in text)
    return max(
        _RESET_BADGE_MIN_WIDTH_PX,
        label_width
        + _RESET_BADGE_HORIZONTAL_PADDING_PX * 2
        + _RESET_BADGE_OUTLINE_WIDTH_PX * 2,
    )


def _metric_progress_width_for_segment(width: int) -> int:
    width = max(0, int(width))
    fixed_columns = 14 + 3 + 3 + _VALUE_COLUMN_MAX_WIDTH_PX + 4 + _RESET_WEEKLY_COLUMN_WIDTH_PX + _SEGMENT_RIGHT_PADDING_PX
    available = max(6, width - fixed_columns)
    return max(
        _METRIC_PROGRESS_MIN_WIDTH_PX,
        min(
            _METRIC_PROGRESS_MAX_WIDTH_PX,
            _METRIC_PROGRESS_PREFERRED_WIDTH_PX,
            int(available),
        ),
    )


def _reset_column_width_for_text(text: str, *, metric_key: str = "") -> int:
    value = str(text or "")
    if not value:
        return 0
    if value in set(_RESET_DIRECTION_MARKERS.values()) - {""}:
        return max(8, len(value) * 7 + 2)
    if value == _RESET_PLACEHOLDER_TEXT:
        return max(12, len(value) * 5 + 2)
    minimum = _RESET_DETAIL_COLUMN_WIDTH_PX
    if str(metric_key or "") == "five_hour_limit":
        minimum = _RESET_FIVE_HOUR_COLUMN_WIDTH_PX
    elif str(metric_key or "") == "weekly_limit":
        minimum = _RESET_WEEKLY_COLUMN_WIDTH_PX
    return max(minimum, len(value) * 5 + 2)


def _value_column_width_for_text(value_text: str) -> int:
    text = str(value_text or "")
    estimated_width = len(text) * 7 + 2
    return min(
        _VALUE_COLUMN_MAX_WIDTH_PX,
        max(_VALUE_COLUMN_MIN_WIDTH_PX, int(estimated_width)),
    )


def _reset_action_direction(metric_key: str, percent: int | None, seconds: int) -> str:
    if percent is None:
        return _RESET_DIRECTION_UNKNOWN
    window = _RESET_WINDOW_BY_METRIC.get(str(metric_key))
    if not isinstance(window, dict):
        window = _RESET_WINDOW_BY_METRIC["five_hour_limit"]
    remaining_percent = max(0, min(100, int(percent)))
    urgent_seconds = int(window["urgent_seconds"])
    soon_seconds = int(window["soon_seconds"])
    far_seconds = int(window["far_seconds"])
    very_far_seconds = int(window["very_far_seconds"])

    if seconds <= urgent_seconds:
        if remaining_percent >= 60:
            return _RESET_DIRECTION_SURPLUS
        return _RESET_DIRECTION_ON_TRACK
    if seconds <= soon_seconds:
        if remaining_percent >= 60:
            return _RESET_DIRECTION_SURPLUS
        return _RESET_DIRECTION_ON_TRACK

    if seconds >= far_seconds:
        if remaining_percent < 25:
            return _RESET_DIRECTION_SHORTAGE
        if remaining_percent >= 75:
            return _RESET_DIRECTION_SURPLUS
        if remaining_percent < 60 and seconds >= very_far_seconds:
            return _RESET_DIRECTION_SHORTAGE
        return _RESET_DIRECTION_ON_TRACK

    if remaining_percent < 25:
        return _RESET_DIRECTION_SHORTAGE
    return _RESET_DIRECTION_ON_TRACK


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
    has_metric = any(
        _parse_percent(snapshot.get(metric_key)) is not None
        for metric_key, _label in _TASKBAR_METRICS
    )
    if collect_inflight and not has_metric:
        return {"state": "sync", "text": "SYNC", "color": "#38bdf8"}
    if monitor_state == "paused_profile_in_use":
        return {"state": "profile_busy", "text": "WAIT", "color": "#f59e0b"}
    if monitor_state == "paused_auth_required" or session_state == "logged_out":
        return {"state": "login", "text": "OUT", "color": "#f59e0b"}
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
    return _bar_color_for_state(enabled, state)


def _bar_color_for_state(enabled: bool, state: str) -> str:
    if not bool(enabled):
        return "#6b7280"
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
