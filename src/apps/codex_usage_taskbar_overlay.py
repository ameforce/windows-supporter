from __future__ import annotations

import ctypes
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.apps.codex_usage_taskbar_targets import (
    TaskbarMonitorSnapshot,
    TaskbarOverlayTarget,
    TaskbarWindowSnapshot,
    build_taskbar_overlay_targets,
    globalize_geometry,
    local_work_area,
    monitor_size,
    normalize_rect,
    parse_display_num,
    target_cache_key,
)

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

_GEOMETRY_COORDINATE_BASIS = "physical_px"
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
_CONTENT_TICK_MS = 1000
_KEEPALIVE_TICK_MS = 2000
_GEOMETRY_MONITOR_TICK_MS = 500
_GEOMETRY_MONITOR_HARD_RESAMPLE_SEC = 0.5
_GEOMETRY_CHANGE_TOLERANCE_PX = 2
_GEOMETRY_TRANSIENT_X_SHIFT_TOLERANCE_PX = _OCCUPIED_DILATION_PX
_RIGHT_TO_LEFT_SWITCH_DWELL_SEC = 2.0
_LEFT_TO_RIGHT_SWITCH_DWELL_SEC = 1.0
_SLOT_SIDE_LEFT = "left"
_SLOT_SIDE_RIGHT = "right"
_FULLSCREEN_POLL_MS = 500
_GWLP_HWNDPARENT = -8
# The overlay must never be owned by a shell tray window: explorer destroys
# Shell_SecondaryTrayWnd/Shell_TrayWnd on RDP connect/disconnect and monitor
# add/remove, and Win32 cascade-destroys owned windows with their owner. A
# process-local hidden owner keeps the owned-window semantics without exposing
# the overlay to shell teardown.
_NATIVE_OWNER_CLASS_PREFIX = "WindowsSupporterOverlayOwner_"
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
_TASKBAR_STRUCTURAL_CHILD_CLASSES = {
    "MSTaskListWClass",
    "MSTaskSwWClass",
    "ReBarWindow32",
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
_METRIC_CONTEXT_SEPARATOR_COLOR = "#64748b"
_NORMAL_GUIDANCE_COLOR = "#4ade80"
_VALUE_COLUMN_MIN_WIDTH_PX = 22
_VALUE_COLUMN_MAX_WIDTH_PX = 28
_SEGMENT_RIGHT_PADDING_PX = 2
_OVERLAY_RIGHT_PADDING_PX = 10
_METRIC_PROGRESS_MIN_WIDTH_PX = 28
_METRIC_PROGRESS_PREFERRED_WIDTH_PX = 36
_METRIC_PROGRESS_MAX_WIDTH_PX = 48
# Display contract: the % value and reset text must stay visible even when the
# taskbar slot is narrow. The progress bar is the last element allowed to
# shrink, down to this text-priority floor, before any text is omitted.
_METRIC_PROGRESS_TEXT_PRIORITY_MIN_WIDTH_PX = 14
# Below this equal-split segment width, per-metric reservation is impossible;
# the compact shrink path already runs inside each segment.
_MIN_COMPACT_SEGMENT_FOR_TEXT_PX = 140
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
    "id",
    "profile_id",
    "provider",
    "enabled",
    "taskbar_selected",
    "label",
    "freshness",
    "provider_status",
    "status_state",
    "status_text",
    "status_color",
    "state",
    "color",
    "percent",
    "value_text",
)
_METRIC_RENDER_SIGNATURE_KEYS = (
    "id",
    "provider",
    "profile_id",
    "freshness",
    "provider_status",
    "metric_key",
    "key",
    "short_label",
    "percent",
    "value_text",
    "reset_at",
    "color",
    "state",
    "reset_text",
    "reset_short_text",
    "normal_min_percent",
    "normal_max_percent",
    "normal_transition_seconds",
    "normal_guidance_text",
    "normal_guidance_short_text",
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
    # Per-segment layout. `segment_width`/`progress_width` stay as the uniform
    # fallback for callers that assume equal split; when the text-first
    # allocation applies, these lists carry the real per-segment geometry.
    segment_offsets: tuple[int, ...] = ()
    segment_widths: tuple[int, ...] = ()
    progress_widths: tuple[int, ...] = ()

    def segment_geometry(self, index: int) -> tuple[int, int, int]:
        """Return (offset, width, progress_width) for one segment."""
        if index < len(self.segment_widths):
            return (
                self.segment_offsets[index],
                self.segment_widths[index],
                self.progress_widths[index],
            )
        step = self.segment_width + self.segment_gap
        return index * step, self.segment_width, self.progress_width


def _taskbar_profile_source(runtime: dict[str, Any]) -> list[Any]:
    profiles = runtime.get("profiles")
    if isinstance(profiles, list):
        return profiles
    accounts = runtime.get("accounts")
    if isinstance(accounts, list):
        return accounts
    return []


def _selected_taskbar_profiles(profiles: list[Any]) -> list[dict[str, Any]]:
    selected = []
    for raw in profiles:
        if not isinstance(raw, dict):
            continue
        if not bool(raw.get("taskbar_selected", True)):
            continue
        selected.append(raw)
        if len(selected) >= 2:
            break
    return selected


def build_codex_usage_taskbar_overlay_model(
    runtime_status: dict[str, Any],
    geometry: dict[str, int | str] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    runtime = runtime_status if isinstance(runtime_status, dict) else {}
    profiles = _taskbar_profile_source(runtime)
    selected_profiles = _selected_taskbar_profiles(profiles)

    bars = []
    manager_enabled = bool(runtime.get("enabled", True))
    for index, raw in enumerate(selected_profiles, start=1):
        profile_enabled = bool(raw.get("enabled", True))
        provider = str(raw.get("provider") or "codex").strip().lower() or "codex"
        profile_id = str(raw.get("id") or raw.get("profile_id") or f"profile_{index}")
        freshness = str(raw.get("freshness") or "").strip().lower()
        provider_status = raw.get("provider_status", "")
        snapshot = raw.get("last_snapshot", {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        runtime_info = raw.get("runtime", {})
        if not isinstance(runtime_info, dict):
            runtime_info = {}
        raw_metrics = raw.get("metrics")
        status = _profile_status(
            profile_enabled,
            runtime_info,
            snapshot,
            provider_status=provider_status,
            freshness=freshness,
            metric_descriptors=raw_metrics,
        )
        metrics: list[dict[str, Any]] = []
        if isinstance(raw_metrics, list):
            for metric_index, descriptor in enumerate(raw_metrics, start=1):
                if not isinstance(descriptor, dict):
                    continue
                metrics.append(
                    _build_provider_metric(
                        descriptor,
                        provider=provider,
                        profile_id=profile_id,
                        freshness=freshness,
                        provider_status=provider_status,
                        account_state=status["state"],
                        captured_at_value=snapshot.get("captured_at"),
                        fallback_index=metric_index,
                        now=now,
                    )
                )
                if len(metrics) >= 2:
                    break
        else:
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
        credit_metric = _credit_metric_descriptor(snapshot)
        if credit_metric is not None and len(metrics) < 3:
            # Display contract: credit always occupies its own compact slot
            # (5h, weekly, credit) whenever the account reports a usable
            # balance. Credit without a percent never replaces a reported
            # usage limit's slot order.
            metrics.append(credit_metric)
        primary_metric = metrics[0] if metrics else {
            "percent": None,
            "value_text": "--",
            "state": "unknown",
            "color": _bar_color(profile_enabled, None),
        }
        row_state = (
            str(primary_metric["state"])
            if status["state"] == "ready"
            else str(status["state"])
        )
        bars.append(
            {
                "id": profile_id,
                "profile_id": profile_id,
                "provider": provider,
                "label": str(raw.get("label") or f"{provider.title()} {index}"),
                "enabled": bool(profile_enabled),
                "taskbar_selected": True,
                "freshness": freshness,
                "provider_status": provider_status,
                "status_state": status["state"],
                "percent": int(primary_metric.get("percent") or 0),
                "value_text": str(primary_metric.get("value_text") or "--"),
                "state": row_state,
                "color": str(primary_metric.get("color") or "#6b7280"),
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
        _account_collecting(profile) for profile in profiles if isinstance(profile, dict)
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
    previous_geometry: dict[str, Any] | None = None,
    include_telemetry: bool = False,
) -> dict[str, Any]:
    screen_width = max(320, int(screen_width or 0))
    screen_height = max(240, int(screen_height or 0))
    work_area_rect, work_area_telemetry = _normalize_work_area_with_metadata(
        work_area,
        screen_width,
        screen_height,
    )
    left, top, right, bottom = work_area_rect
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
            "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
            "_geometry_basis": "monitor_local_physical_px",
        }
        return _fit_horizontal_geometry_to_empty_slot(
            geometry,
            int(screen_width),
            occupied_spans,
            preferred_width=preferred_width,
            previous_geometry=previous_geometry,
            work_area_telemetry=work_area_telemetry,
            include_telemetry=include_telemetry,
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
            "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
            "_geometry_basis": "monitor_local_physical_px",
        }
        return _fit_horizontal_geometry_to_empty_slot(
            geometry,
            int(screen_width),
            occupied_spans,
            preferred_width=preferred_width,
            previous_geometry=previous_geometry,
            work_area_telemetry=work_area_telemetry,
            include_telemetry=include_telemetry,
        )
    if left > 0:
        width = max(1, min(320, left - 2))
        height = min(86, max(58, int(screen_height * 0.12)))
        geometry = {
            "x": max(0, min(left - width, (left - width) // 2)),
            "y": max(8, screen_height - height - 8),
            "width": int(width),
            "height": int(height),
            "orientation": "left",
            "visible": True,
            "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
            "_geometry_basis": "monitor_local_physical_px",
        }
        if include_telemetry:
            geometry["_telemetry"] = _geometry_telemetry(
                screen_width,
                preferred_width,
                work_area_telemetry=work_area_telemetry,
                occupied_spans=None,
                free_spans=[],
                selected_slot=None,
                chosen_geometry=geometry,
            )
        return geometry
    if right < screen_width:
        band_width = max(1, screen_width - right)
        width = max(1, min(320, band_width - 2))
        height = min(86, max(58, int(screen_height * 0.12)))
        geometry = {
            "x": int(right + max(0, min(band_width - width, (band_width - width) // 2))),
            "y": max(8, screen_height - height - 8),
            "width": int(width),
            "height": int(height),
            "orientation": "right",
            "visible": True,
            "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
            "_geometry_basis": "monitor_local_physical_px",
        }
        if include_telemetry:
            geometry["_telemetry"] = _geometry_telemetry(
                screen_width,
                preferred_width,
                work_area_telemetry=work_area_telemetry,
                occupied_spans=None,
                free_spans=[],
                selected_slot=None,
                chosen_geometry=geometry,
            )
        return geometry

    x = max(8, (screen_width - width) // 2)
    geometry = {
        "x": int(x),
        "y": int(screen_height - height - 2),
        "width": int(width),
        "height": int(height),
        "orientation": "bottom",
        "visible": True,
        "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
        "_geometry_basis": "monitor_local_physical_px",
    }
    return _fit_horizontal_geometry_to_empty_slot(
        geometry,
        int(screen_width),
        occupied_spans,
        preferred_width=preferred_width,
        previous_geometry=previous_geometry,
        work_area_telemetry=work_area_telemetry,
        include_telemetry=include_telemetry,
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
    return tuple(metrics[:3])


def _metric_guidance_texts(metric: dict[str, Any]) -> tuple[str, str]:
    metric_dict = metric if isinstance(metric, dict) else {}
    raw_reset_detail_text = str(metric_dict.get("reset_text") or "")
    raw_reset_short_text = str(
        metric_dict.get("reset_short_text")
        or metric_dict.get("reset_text")
        or ""
    )
    guidance_detail_text = str(metric_dict.get("normal_guidance_text") or "")
    guidance_short_text = str(metric_dict.get("normal_guidance_short_text") or "")
    if not (guidance_detail_text or guidance_short_text):
        return raw_reset_detail_text, raw_reset_short_text

    detail_text = _join_metric_context(
        raw_reset_detail_text,
        guidance_detail_text,
    )
    short_text = _join_metric_context(
        raw_reset_short_text,
        guidance_short_text,
    )
    return detail_text, short_text


def _join_metric_context(reset_text: str, guidance_text: str) -> str:
    return " | ".join(part for part in (reset_text, guidance_text) if part)


def _split_metric_context_text(text: str) -> tuple[str, str]:
    reset_text, separator, guidance_text = str(text or "").partition(" | ")
    if not separator:
        return reset_text, ""
    return reset_text, guidance_text


def _inline_text_width(text: str) -> int:
    return sum(9 if ord(character) > 127 else 5 for character in str(text or ""))


def _draw_metric_context_text(
    canvas: Any,
    text: str,
    *,
    x: int,
    center_y: int,
    reset_color: str,
) -> int:
    reset_text, guidance_text = _split_metric_context_text(text)
    cursor_x = int(x)
    font = ("Segoe UI", 6, "bold")
    if reset_text:
        canvas.create_text(
            cursor_x,
            center_y,
            anchor="w",
            fill=reset_color,
            font=font,
            text=reset_text,
        )
        cursor_x += _inline_text_width(reset_text)
    if guidance_text:
        separator_x = cursor_x + 5
        canvas.create_text(
            separator_x,
            center_y,
            anchor="w",
            fill=_METRIC_CONTEXT_SEPARATOR_COLOR,
            font=font,
            text="|",
        )
        cursor_x = separator_x + _inline_text_width("|") + 5
        canvas.create_text(
            cursor_x,
            center_y,
            anchor="w",
            fill=_NORMAL_GUIDANCE_COLOR,
            font=font,
            text=guidance_text,
        )
        cursor_x += _inline_text_width(guidance_text)
    return cursor_x


def _metric_fits_badge_mode(
    metric: dict[str, Any],
    segment_width: int,
    progress_width: int,
    badge_mode: str,
) -> bool:
    metric_dict = metric if isinstance(metric, dict) else {}
    mode = _normalized_badge_mode(badge_mode)
    detail_text, short_text = _metric_guidance_texts(metric_dict)
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
    if int(layout.get("progress_width") or 0) < _METRIC_PROGRESS_TEXT_PRIORITY_MIN_WIDTH_PX:
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
    visible_metrics = tuple(metric for metric in metrics[:3] if isinstance(metric, dict))
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


def _row_fits_badge_mode_for_layout(
    row_layout: _MetricRowLayout,
    badge_mode: str,
) -> bool:
    visible_metrics = tuple(
        metric for metric in row_layout.visible_metrics[:3] if isinstance(metric, dict)
    )
    if not visible_metrics:
        return True
    for index, metric in enumerate(visible_metrics):
        _offset, segment_width_value, segment_progress = row_layout.segment_geometry(
            index
        )
        if not _metric_fits_badge_mode(
            metric,
            int(segment_width_value),
            int(segment_progress),
            badge_mode,
        ):
            return False
    return True


def _row_fits_badge_mode_for_overlay_width(
    width: int,
    metrics: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    badge_mode: str,
) -> bool:
    row_layout = _metric_row_layout_for_overlay_width(width, metrics)
    return _row_fits_badge_mode_for_layout(row_layout, badge_mode)


def _resolve_overlay_badge_mode(row_layouts: tuple[_MetricRowLayout, ...]) -> str:
    if all(
        _row_fits_badge_mode_for_layout(row_layout, "full")
        for row_layout in row_layouts
    ):
        return "full"
    return "short"


def _metric_row_layout_for_overlay_width(
    width: int,
    metrics: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> _MetricRowLayout:
    overlay_width = int(width)
    visible_metrics = tuple(metric for metric in metrics[:3] if isinstance(metric, dict))
    label_width = _label_width_for_overlay_width(overlay_width)
    status_width = _status_width_for_overlay_width(overlay_width)
    metrics_x = 6 + label_width + status_width + _STATUS_TO_METRICS_GAP_PX
    metrics_width = max(0, overlay_width - metrics_x - _OVERLAY_RIGHT_PADDING_PX)
    segment_gap = _metric_segment_gap_for_overlay_width(overlay_width)
    segment_width = _metric_segment_width_for_metrics_width(
        metrics_width,
        len(visible_metrics),
        segment_gap,
    )
    progress_width = _metric_progress_width_for_segment(segment_width)

    # Text-first allocation: every visible metric must keep its reset
    # countdown, normal-usage guidance, and (when applicable) transition time.
    # Blind equal split starves the second metric's texts whenever a row mixes
    # one- and two-metric profiles, so instead reserve each metric's required
    # full-text width and hand the leftover to the progress bars.
    counts = len(visible_metrics)
    required_widths = [
        min(
            _required_metric_segment_width(metric, badge_mode="short"),
            _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX,
        )
        for metric in visible_metrics
    ]
    total_required = sum(required_widths) + segment_gap * max(0, counts - 1)
    layout: _MetricRowLayout | None = None
    if counts and total_required <= metrics_width:
        leftover = metrics_width - total_required
        extra = leftover // counts
        widths: list[int] = []
        offsets: list[int] = []
        progresses: list[int] = []
        cursor = 0
        for index, metric in enumerate(visible_metrics):
            segment = required_widths[index] + extra
            widths.append(int(segment))
            offsets.append(int(cursor))
            progresses.append(
                int(
                    max(
                        _METRIC_PROGRESS_TEXT_PRIORITY_MIN_WIDTH_PX,
                        min(
                            _METRIC_PROGRESS_MAX_WIDTH_PX,
                            _metric_progress_width_for_segment(segment),
                        ),
                    )
                )
            )
            cursor += segment + segment_gap
        layout = _MetricRowLayout(
            label_width=label_width,
            status_width=status_width,
            metrics_x=metrics_x,
            metrics_width=metrics_width,
            segment_gap=segment_gap,
            segment_width=max(widths),
            progress_width=max(progresses),
            visible_metrics=visible_metrics,
            segment_offsets=tuple(offsets),
            segment_widths=tuple(widths),
            progress_widths=tuple(progresses),
        )
    if layout is not None:
        return layout

    # Fallback: keep every metric's countdown by clamping the progress bar to
    # the text-priority floor in each equal segment before any text is dropped.
    uniform_layout = _MetricRowLayout(
        label_width=label_width,
        status_width=status_width,
        metrics_x=metrics_x,
        metrics_width=metrics_width,
        segment_gap=segment_gap,
        segment_width=segment_width,
        progress_width=progress_width,
        visible_metrics=visible_metrics,
    )
    fits = all(
        _metric_fits_badge_mode(
            metric,
            segment_width,
            _METRIC_PROGRESS_TEXT_PRIORITY_MIN_WIDTH_PX,
            "short",
        )
        for metric in visible_metrics
    )
    if fits or segment_width < _MIN_COMPACT_SEGMENT_FOR_TEXT_PX:
        return uniform_layout
    return _MetricRowLayout(
        label_width=label_width,
        status_width=status_width,
        metrics_x=metrics_x,
        metrics_width=metrics_width,
        segment_gap=segment_gap,
        segment_width=segment_width,
        progress_width=_METRIC_PROGRESS_TEXT_PRIORITY_MIN_WIDTH_PX,
        visible_metrics=visible_metrics,
    )


def _required_metric_segment_width(
    metric: dict[str, Any],
    *,
    badge_mode: str = "any",
) -> int:
    metric_dict = metric if isinstance(metric, dict) else {}
    detail_text, short_text = _metric_guidance_texts(metric_dict)
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
        if int(layout.get("progress_width") or 0) < _METRIC_PROGRESS_TEXT_PRIORITY_MIN_WIDTH_PX:
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
                return int(candidate_width)
    return _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX


def _render_signature_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(key), _render_signature_value(item))
                for key, item in value.items()
            )
        )
    if isinstance(value, (list, tuple)):
        return tuple(_render_signature_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_render_signature_value(item) for item in value))
    return value


def _metric_render_signature(metric: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        _render_signature_value(metric.get(key))
        for key in _METRIC_RENDER_SIGNATURE_KEYS
    )


def _bar_render_signature(bar: dict[str, Any]) -> tuple[Any, ...]:
    metrics = tuple(
        _metric_render_signature(metric)
        for metric in _visible_metrics_for_taskbar_bar(bar)
    )
    bar_fields = tuple(
        _render_signature_value(bar.get(key))
        for key in _BAR_RENDER_SIGNATURE_KEYS
    )
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
    """Return content-fit width only; do not inflate into unused empty-slot space."""
    _ = model
    return int(minimum_width)


def _model_geometry(model: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(model, dict):
        return None
    geometry = model.get("geometry")
    if not isinstance(geometry, dict):
        return None
    return dict(geometry)


def _local_previous_geometry_for_target(
    previous_geometry: dict[str, Any] | None,
    target: TaskbarOverlayTarget,
) -> dict[str, Any] | None:
    if not isinstance(previous_geometry, dict) or not bool(
        previous_geometry.get("visible", True)
    ):
        return None
    try:
        previous_taskbar_hwnd = int(previous_geometry.get("_taskbar_hwnd", 0) or 0)
    except (TypeError, ValueError):
        previous_taskbar_hwnd = 0
    target_taskbar_hwnd = int(target.taskbar_hwnd)
    if (
        previous_taskbar_hwnd > 0
        and target_taskbar_hwnd > 0
        and previous_taskbar_hwnd != target_taskbar_hwnd
    ):
        return None
    geometry_rect = _geometry_screen_rect(previous_geometry)
    if geometry_rect is not None and not _rects_overlap(
        geometry_rect,
        target.monitor.monitor,
    ):
        return None
    local_geometry = dict(previous_geometry)
    try:
        local_geometry["x"] = int(previous_geometry.get("x", 0)) - int(
            target.monitor.monitor[0]
        )
    except (TypeError, ValueError):
        return None
    return local_geometry


def _root_previous_geometry(
    previous_geometry: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(previous_geometry, dict):
        return None
    if "_taskbar_hwnd" in previous_geometry:
        return None
    if str(previous_geometry.get("_geometry_basis") or "") == "global_physical_px":
        return None
    return dict(previous_geometry)


def _rects_overlap(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> bool:
    left_x1, left_y1, left_x2, left_y2 = left
    right_x1, right_y1, right_x2, right_y2 = right
    return (
        min(int(left_x2), int(right_x2)) > max(int(left_x1), int(right_x1))
        and min(int(left_y2), int(right_y2)) > max(int(left_y1), int(right_y1))
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
        taskbar_target_getter: Callable[[], tuple[TaskbarOverlayTarget, ...]] | None = None,
    ) -> None:
        self._root = root
        self._runtime_getter = runtime_getter
        self._window_factory = window_factory
        self._work_area_getter = work_area_getter or _get_primary_work_area
        self._occupied_span_getter = (
            occupied_span_getter or _detect_horizontal_taskbar_occupied_spans
        )
        self._fullscreen_detector = fullscreen_detector
        self._taskbar_target_getter = taskbar_target_getter
        self._window = None
        self._canvas = None
        self._last_metric_values: dict[str, str] = {}
        self._flash_until: dict[str, float] = {}
        self._last_model: dict[str, Any] | None = None
        self._flash_after_id = None
        self._content_after_id = None
        self._keepalive_after_id = None
        self._geometry_after_id = None
        self._last_geometry_hard_resample_at = 0.0
        self._geometry_invalidated = True
        self._cached_geometry_context = None
        self._cached_geometry: dict[str, int | str] | None = None
        self._pending_regression_geometry: dict[str, int | str] | None = None
        self._pending_regression_context = None
        self._pending_regression_count = 0
        self._pending_side_transition: tuple[str, str] | None = None
        self._pending_side_transition_context = None
        self._pending_side_transition_started_at = 0.0
        self._fullscreen_suppressed = False
        self._window_visible = False
        self._active_taskbar_hwnd = 0
        self._native_owner_hwnd = 0
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
        previous_geometry_context = self._cached_geometry_context
        previous_geometry = _model_geometry(self._last_model) or {}
        geometry = self._calculate_geometry(
            preferred_width=preferred_width,
            previous_geometry=previous_geometry,
        )
        geometry = self._stabilize_transient_geometry_regression(
            previous_geometry,
            geometry,
            previous_context=previous_geometry_context,
            candidate_context=self._cached_geometry_context,
        )
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
        previous_model = self._last_model
        previous_geometry = (
            previous_model.get("geometry", {}) if isinstance(previous_model, dict) else {}
        )
        if not isinstance(previous_geometry, dict):
            previous_geometry = {}
        if (
            bool(self._window_visible)
            and isinstance(previous_model, dict)
            and not self._has_active_metric_flash()
            and not _geometry_changed(previous_geometry, geometry)
            and _overlay_render_signature(previous_model) == _overlay_render_signature(model)
        ):
            self._last_model = model
            self._schedule_content_tick()
            self._schedule_keepalive_tick()
            self._schedule_geometry_monitor_tick()
            return True
        try:
            self._apply_geometry(window, geometry)
            self._update_metric_change_flash(model)
            self._draw(model)
        except Exception:
            # A destroyed native surface must never kill the caller or the tick
            # loops; drop the surface and retry on the next geometry tick.
            self._recover_broken_surface()
            self._last_model = model
            self._schedule_geometry_monitor_tick(
                delay_ms=max(100, int(_GEOMETRY_MONITOR_TICK_MS))
            )
            return False
        self._last_model = model
        if self._last_geometry_hard_resample_at <= 0.0:
            try:
                self._last_geometry_hard_resample_at = time.monotonic()
            except Exception:
                self._last_geometry_hard_resample_at = 0.0
        self._schedule_keepalive_tick()
        self._schedule_content_tick()
        self._schedule_geometry_monitor_tick()
        self._schedule_flash_tick_if_needed()
        try:
            window.deiconify()
            self._window_visible = True
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
        self._window_visible = False
        self._cancel_flash_tick()
        self._cancel_content_tick()
        self._cancel_keepalive_tick()
        self._cancel_geometry_monitor_tick()
        self._clear_pending_regression_geometry()
        self._clear_pending_side_transition()
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
        self._window_visible = False
        self._cancel_flash_tick()
        self._cancel_content_tick()
        self._clear_pending_regression_geometry()
        self._clear_pending_side_transition()
        self._schedule_geometry_monitor_tick()
        return

    def invalidate_geometry(self) -> None:
        self._geometry_invalidated = True
        self._cached_geometry_context = None
        self._cached_geometry = None
        self._clear_pending_regression_geometry()
        self._clear_pending_side_transition()
        return

    def prepare_for_display_topology_change(self) -> None:
        """Drop every cached placement/render state derived from the old display space.

        RDP connect/disconnect and monitor add/remove can change the effective DPI
        scale of this (DPI-unaware) process mid-flight, so coordinates captured
        before the change must never feed slot selection or regression stabilization.
        """
        self.invalidate_geometry()
        self._last_metric_values.clear()
        self._flash_until.clear()
        self._last_model = None
        return

    def invalidate_native_owner(self) -> None:
        self._active_taskbar_hwnd = self._native_owner_taskbar_hwnd_for_rebind()
        window = self._window
        if window is None or not self._window_is_alive(window):
            return
        self._prepare_native_window(window)
        return

    def rebind_native_owner(self) -> None:
        self.invalidate_native_owner()
        return

    def _native_owner_taskbar_hwnd_for_rebind(self) -> int:
        model_geometry = _model_geometry(self._last_model)
        selected_target = (
            model_geometry.get("selected_target")
            if isinstance(model_geometry, dict)
            else None
        )
        monitor_rect = None
        if isinstance(selected_target, dict):
            monitor_rect = normalize_rect(selected_target.get("monitor_rect"))
        if monitor_rect is None and isinstance(model_geometry, dict):
            try:
                overlay_rect = (
                    int(model_geometry.get("x", 0) or 0),
                    int(model_geometry.get("y", 0) or 0),
                    int(model_geometry.get("x", 0) or 0)
                    + max(1, int(model_geometry.get("width", 0) or 0)),
                    int(model_geometry.get("y", 0) or 0)
                    + max(1, int(model_geometry.get("height", 0) or 0)),
                )
            except (TypeError, ValueError):
                overlay_rect = None
        else:
            overlay_rect = None
        for target in self._collect_taskbar_targets_for_geometry():
            taskbar_hwnd = int(target.taskbar_hwnd or 0)
            if taskbar_hwnd <= 0:
                continue
            target_monitor_rect = tuple(target.monitor.monitor)
            if monitor_rect is not None and target_monitor_rect == tuple(monitor_rect):
                return taskbar_hwnd
            if overlay_rect is not None and _rects_overlap(overlay_rect, target_monitor_rect):
                return taskbar_hwnd
        return 0

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

    def _has_active_metric_flash(self) -> bool:
        now = time.monotonic()
        active = False
        for identity, expires_at in list(self._flash_until.items()):
            try:
                if float(expires_at) > now:
                    active = True
                    continue
            except (TypeError, ValueError):
                pass
            self._flash_until.pop(identity, None)
        return active

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

    def _schedule_content_tick(self, delay_ms: int | None = None) -> None:
        if self._content_after_id is not None:
            return
        scheduler = getattr(self._root, "after", None)
        if not callable(scheduler):
            return
        try:
            self._content_after_id = scheduler(
                _CONTENT_TICK_MS if delay_ms is None else int(delay_ms),
                self._content_tick,
            )
        except Exception:
            self._content_after_id = None
        return

    def _content_tick(self) -> None:
        self._content_after_id = None
        previous_model = self._last_model
        if not isinstance(previous_model, dict):
            return
        if not bool(previous_model.get("visible", True)):
            return
        window = self._window
        if window is None or not bool(self._window_visible):
            return
        try:
            try:
                runtime = self._runtime_getter()
            except Exception:
                runtime = {}
            geometry = previous_model.get("geometry", {})
            if not isinstance(geometry, dict):
                geometry = {}
            updated_model = build_codex_usage_taskbar_overlay_model(
                runtime,
                geometry=geometry,
                now=_current_overlay_datetime(),
            )
            if not bool(updated_model.get("visible", True)):
                self._last_model = updated_model
                self.hide()
                return
            if self._is_fullscreen_active(window, geometry):
                self._last_model = updated_model
                self._suppress_for_fullscreen()
                return
            if _overlay_render_signature(previous_model) != _overlay_render_signature(
                updated_model
            ):
                self._update_metric_change_flash(updated_model)
                self._draw(updated_model)
                self._last_model = updated_model
                self._schedule_flash_tick_if_needed()
                self._force_native_repaint(window)
            else:
                self._last_model = updated_model
        except Exception:
            self._recover_broken_surface()
        finally:
            if self._window is not None and not self._window_is_alive(self._window):
                self._discard_dead_window(self._window)
        self._schedule_content_tick(delay_ms=_CONTENT_TICK_MS)
        return

    def _cancel_content_tick(self) -> None:
        after_id = self._content_after_id
        self._content_after_id = None
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
        try:
            if self._is_fullscreen_active(window, model.get("geometry")):
                self.invalidate_geometry()
                self.refresh()
                return
            if bool(self._fullscreen_suppressed):
                self._fullscreen_suppressed = False
                self.refresh()
                return
            window = self._window
            if window is None:
                return
            native_visible = self._is_native_z_order_visible(window)
            if not native_visible:
                self._reassert_native_z_order(window)
                self._force_native_repaint(window)
        except Exception:
            self._recover_broken_surface()
        finally:
            if self._window is not None and not self._window_is_alive(self._window):
                self._discard_dead_window(self._window)
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
        previous_geometry = model.get("geometry", {})
        if not isinstance(previous_geometry, dict):
            previous_geometry = {}
        geometry = self._calculate_geometry(
            force_resample=True,
            withdraw_for_sampling=False,
            preferred_width=preferred_width,
            previous_geometry=previous_geometry,
        )
        candidate_geometry_context = self._cached_geometry_context
        geometry = self._stabilize_transient_geometry_regression(
            previous_geometry,
            geometry,
            previous_context=previous_geometry_context,
            candidate_context=candidate_geometry_context,
        )
        if self._cached_geometry_context is not None:
            self._geometry_invalidated = False
        geometry_changed = _geometry_changed(previous_geometry, geometry)
        updated_model = build_codex_usage_taskbar_overlay_model(
            runtime,
            geometry=geometry,
            now=model_now,
        )
        if bool(updated_model.get("visible", True)) and self._is_fullscreen_active(
            window,
            geometry,
        ):
            self._suppress_for_fullscreen()
            return
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
                try:
                    if geometry_changed:
                        self._apply_geometry(window, geometry)
                    self._update_metric_change_flash(updated_model)
                    self._draw(updated_model)
                    self._last_model = updated_model
                    self._schedule_flash_tick_if_needed()
                    self._schedule_content_tick()
                except Exception:
                    # Shell teardown can destroy the native surface mid-tick;
                    # recover the surface and retry instead of dying silently.
                    self._recover_broken_surface()
                    self._last_model = updated_model
                    self._schedule_geometry_monitor_tick(delay_ms=_GEOMETRY_MONITOR_TICK_MS)
                    return
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
        self._schedule_content_tick()
        self._schedule_geometry_monitor_tick()
        return

    def _clear_pending_regression_geometry(self) -> None:
        self._pending_regression_geometry = None
        self._pending_regression_context = None
        self._pending_regression_count = 0
        return

    def _clear_pending_side_transition(self) -> None:
        self._pending_side_transition = None
        self._pending_side_transition_context = None
        self._pending_side_transition_started_at = 0.0
        return

    def _stabilize_transient_geometry_regression(
        self,
        previous_geometry: dict[str, Any],
        candidate_geometry: dict[str, int | str],
        *,
        previous_context: Any,
        candidate_context: Any,
    ) -> dict[str, int | str]:
        previous_side = _horizontal_geometry_slot_side(previous_geometry)
        candidate_side = _horizontal_geometry_slot_side(candidate_geometry)
        if previous_side and candidate_side and previous_side != candidate_side:
            previous_stable_context = _transient_geometry_context_key(previous_context)
            candidate_stable_context = _transient_geometry_context_key(candidate_context)
            if (
                previous_stable_context is None
                or previous_stable_context != candidate_stable_context
            ):
                self._clear_pending_side_transition()
                self._clear_pending_regression_geometry()
                return candidate_geometry
            transition = (previous_side, candidate_side)
            now = time.monotonic()
            if (
                self._pending_side_transition != transition
                or self._pending_side_transition_context != candidate_stable_context
            ):
                self._pending_side_transition = transition
                self._pending_side_transition_context = candidate_stable_context
                self._pending_side_transition_started_at = float(now)
                self._clear_pending_regression_geometry()
                return dict(previous_geometry)
            dwell_seconds = (
                _RIGHT_TO_LEFT_SWITCH_DWELL_SEC
                if transition == (_SLOT_SIDE_RIGHT, _SLOT_SIDE_LEFT)
                else _LEFT_TO_RIGHT_SWITCH_DWELL_SEC
            )
            if float(now) - float(self._pending_side_transition_started_at) < float(
                dwell_seconds
            ):
                self._clear_pending_regression_geometry()
                return dict(previous_geometry)
            self._clear_pending_side_transition()
            self._clear_pending_regression_geometry()
            return candidate_geometry
        self._clear_pending_side_transition()
        if not _is_transient_geometry_regression(previous_geometry, candidate_geometry):
            self._clear_pending_regression_geometry()
            return candidate_geometry
        previous_stable_context = _transient_geometry_context_key(previous_context)
        candidate_stable_context = _transient_geometry_context_key(candidate_context)
        if previous_stable_context is None or previous_stable_context != candidate_stable_context:
            self._clear_pending_regression_geometry()
            return candidate_geometry
        x_shift_delta = _same_width_geometry_x_shift_delta(
            previous_geometry,
            candidate_geometry,
        )
        if x_shift_delta is not None:
            if x_shift_delta <= _GEOMETRY_TRANSIENT_X_SHIFT_TOLERANCE_PX:
                self._pending_regression_geometry = dict(candidate_geometry)
                self._pending_regression_context = candidate_context
                self._pending_regression_count = int(self._pending_regression_count) + 1
                return dict(previous_geometry)
            if (
                isinstance(self._pending_regression_geometry, dict)
                and self._pending_regression_context == candidate_context
                and self._pending_regression_geometry == dict(candidate_geometry)
                and int(self._pending_regression_count) >= 1
            ):
                self._clear_pending_regression_geometry()
                return candidate_geometry
            self._pending_regression_geometry = dict(candidate_geometry)
            self._pending_regression_context = candidate_context
            self._pending_regression_count = 1
            return dict(previous_geometry)
        if (
            isinstance(self._pending_regression_geometry, dict)
            and _transient_geometry_context_key(self._pending_regression_context)
            == candidate_stable_context
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
        previous_geometry: dict[str, Any] | None = None,
    ) -> dict[str, int | str]:
        target_geometry = self._calculate_monitor_target_geometry(
            force_resample=force_resample,
            withdraw_for_sampling=withdraw_for_sampling,
            preferred_width=preferred_width,
            previous_geometry=previous_geometry,
        )
        if target_geometry is not None:
            return target_geometry
        return self._calculate_root_geometry(
            force_resample=force_resample,
            withdraw_for_sampling=withdraw_for_sampling,
            preferred_width=preferred_width,
            previous_geometry=previous_geometry,
        )

    def _calculate_monitor_target_geometry(
        self,
        *,
        force_resample: bool,
        withdraw_for_sampling: bool,
        preferred_width: int | None,
        previous_geometry: dict[str, Any] | None,
    ) -> dict[str, int | str] | None:
        targets = self._collect_taskbar_targets_for_geometry()
        if not targets:
            return None
        primary = next((target for target in targets if target.is_primary), targets[0])
        target_decisions = _target_decisions_telemetry(targets)
        fullscreen_by_key: dict[tuple[Any, ...], bool] = {}
        overlay_hwnd = _get_window_handle(self._window) if self._window is not None else 0
        if overlay_hwnd <= 0:
            overlay_hwnd = _get_window_handle(self._root)
        primary_fullscreen = _fullscreen_for_target(
            primary,
            fullscreen_by_key,
            int(overlay_hwnd),
            self._root,
        )
        if not primary_fullscreen:
            return None
        hidden_fallback_reason = ""
        hidden_rca_class = ""
        displayable_secondary_found = False
        for target in targets:
            if target.is_primary or not bool(target.displayable):
                continue
            displayable_secondary_found = True
            if _fullscreen_for_target(target, fullscreen_by_key, int(overlay_hwnd), self._root):
                continue
            geometry = self._calculate_geometry_for_target(
                target,
                force_resample=force_resample,
                withdraw_for_sampling=withdraw_for_sampling,
                preferred_width=preferred_width,
                previous_geometry=previous_geometry,
            )
            if bool(geometry.get("visible", True)):
                return _attach_target_placement_telemetry(
                    geometry,
                    targets,
                    target_decisions,
                    target,
                    fullscreen_by_key,
                    int(overlay_hwnd),
                    self._root,
                    fallback_reason="",
                    rca_class=str(target.rca_class or "displayable_horizontal_taskbar"),
                )
            if not hidden_fallback_reason:
                hidden_fallback_reason = str(geometry.get("fallback_reason") or "")
                hidden_rca_class = str(geometry.get("rca_class") or "")
        if not hidden_fallback_reason:
            if displayable_secondary_found:
                hidden_fallback_reason = "all_candidate_targets_fullscreen"
                hidden_rca_class = "all_targets_fullscreen"
            else:
                hidden_fallback_reason = "no_displayable_secondary_target"
                hidden_rca_class = "target_unavailable"
        if not hidden_rca_class:
            hidden_rca_class = "target_unavailable"
        fullscreen_decisions = _fullscreen_decisions_telemetry(
            targets,
            fullscreen_by_key,
            int(overlay_hwnd),
            self._root,
        )
        hidden = {
            "x": int(primary.monitor.monitor[0]),
            "y": int(primary.monitor.monitor[1]),
            "width": 0,
            "height": 0,
            "orientation": "bottom",
            "visible": False,
            "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
            "_geometry_basis": "global_physical_px",
            "target_decisions": target_decisions,
            "fullscreen_decisions": fullscreen_decisions,
            "selected_target": None,
            "fallback_reason": hidden_fallback_reason,
            "rca_class": hidden_rca_class,
        }
        self._cached_geometry_context = (
            "monitor-target-hidden",
            tuple(target_cache_key(target) for target in targets),
            tuple(
                (int(item["taskbar_hwnd"]), bool(item["fullscreen"]))
                for item in fullscreen_decisions
            ),
            int(preferred_width or 0),
            hidden_fallback_reason,
            hidden_rca_class,
        )
        self._cached_geometry = dict(hidden)
        self._geometry_invalidated = False
        return hidden

    def _collect_taskbar_targets_for_geometry(self) -> tuple[TaskbarOverlayTarget, ...]:
        if callable(self._taskbar_target_getter):
            try:
                return tuple(self._taskbar_target_getter())
            except Exception:
                return ()
        if (
            self._work_area_getter is not _get_primary_work_area
            or self._occupied_span_getter is not _detect_horizontal_taskbar_occupied_spans
        ):
            return ()
        return _collect_taskbar_overlay_targets()

    def _calculate_geometry_for_target(
        self,
        target: TaskbarOverlayTarget,
        *,
        force_resample: bool,
        withdraw_for_sampling: bool,
        preferred_width: int | None,
        previous_geometry: dict[str, Any] | None,
    ) -> dict[str, int | str]:
        width, height = monitor_size(target.monitor)
        work_area = local_work_area(target.monitor)
        local_previous_geometry = _local_previous_geometry_for_target(
            previous_geometry,
            target,
        )
        geometry = calculate_taskbar_overlay_geometry(
            width,
            height,
            work_area,
            preferred_width=preferred_width,
            previous_geometry=local_previous_geometry,
        )
        occupied_spans: list[tuple[int, int]] | None = None
        if str(geometry.get("orientation") or "") in {"bottom", "top"}:
            sampling_geometry = self._sampling_geometry_for_target(geometry, target)
            occupied_spans = self._calculate_occupied_spans(
                width,
                height,
                work_area,
                sampling_geometry,
                withdraw_window=withdraw_for_sampling,
                fallback_exclude_geometry=local_previous_geometry,
            )
            if occupied_spans is not None:
                geometry = calculate_taskbar_overlay_geometry(
                    width,
                    height,
                    work_area,
                    occupied_spans=occupied_spans,
                    preferred_width=preferred_width,
                    previous_geometry=local_previous_geometry,
                )
        context = self._target_geometry_context(
            target,
            width,
            height,
            work_area,
            geometry,
            preferred_width=preferred_width,
            occupied_spans=occupied_spans,
            coordinate_basis=_GEOMETRY_COORDINATE_BASIS,
        )
        if (
            not bool(force_resample)
            and not bool(self._geometry_invalidated)
            and self._cached_geometry_context == context
            and isinstance(self._cached_geometry, dict)
        ):
            return dict(self._cached_geometry)
        fitted = globalize_geometry(geometry, target.monitor)
        fitted["_taskbar_hwnd"] = int(target.taskbar_hwnd)
        fitted["coordinate_basis"] = _GEOMETRY_COORDINATE_BASIS
        fitted["_geometry_basis"] = "global_physical_px"
        self._cached_geometry_context = context
        self._cached_geometry = dict(fitted)
        self._geometry_invalidated = False
        return fitted

    def _target_geometry_context(
        self,
        target: TaskbarOverlayTarget,
        width: int,
        height: int,
        work_area: tuple[int, int, int, int],
        geometry: dict[str, int | str],
        *,
        preferred_width: int | None = None,
        occupied_spans: list[tuple[int, int]] | None = None,
        coordinate_basis: str = _GEOMETRY_COORDINATE_BASIS,
    ) -> tuple[Any, ...]:
        return (
            "monitor-target",
            target_cache_key(target),
            self._geometry_context(
                width,
                height,
                work_area,
                geometry,
                preferred_width=preferred_width,
                occupied_spans=occupied_spans,
                coordinate_basis=coordinate_basis,
            ),
        )

    def _sampling_geometry_for_target(
        self,
        geometry: dict[str, int | str],
        target: TaskbarOverlayTarget,
    ) -> dict[str, int | str]:
        sampling_geometry = dict(geometry)
        sampling_geometry["_screen_origin_x"] = int(target.monitor.monitor[0])
        sampling_geometry["_screen_origin_y"] = int(target.monitor.monitor[1])
        sampling_geometry["_taskbar_hwnd"] = int(target.taskbar_hwnd)
        return sampling_geometry

    def _calculate_root_geometry(
        self,
        *,
        force_resample: bool = False,
        withdraw_for_sampling: bool = True,
        preferred_width: int | None = None,
        previous_geometry: dict[str, Any] | None = None,
    ) -> dict[str, int | str]:
        width = _root_int(self._root, "winfo_screenwidth", 1920)
        height = _root_int(self._root, "winfo_screenheight", 1080)
        try:
            work_area = self._work_area_getter()
        except Exception:
            work_area = None
        root_previous_geometry = _root_previous_geometry(previous_geometry)
        geometry = calculate_taskbar_overlay_geometry(
            width,
            height,
            work_area,
            preferred_width=preferred_width,
            previous_geometry=root_previous_geometry,
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
            occupied_spans=None,
            coordinate_basis=_GEOMETRY_COORDINATE_BASIS,
        )
        occupied_spans = self._calculate_occupied_spans(
            width,
            height,
            work_area,
            geometry,
            withdraw_window=withdraw_for_sampling,
            fallback_exclude_geometry=root_previous_geometry,
        )
        if occupied_spans is None:
            if (
                not bool(force_resample)
                and not bool(self._geometry_invalidated)
                and self._cached_geometry_context == context
                and isinstance(self._cached_geometry, dict)
            ):
                return dict(self._cached_geometry)
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
            previous_geometry=root_previous_geometry,
        )
        context = self._geometry_context(
            width,
            height,
            work_area,
            fitted,
            preferred_width=preferred_width,
            occupied_spans=occupied_spans,
            coordinate_basis=_GEOMETRY_COORDINATE_BASIS,
        )
        if (
            not bool(force_resample)
            and not bool(self._geometry_invalidated)
            and self._cached_geometry_context == context
            and isinstance(self._cached_geometry, dict)
        ):
            return dict(self._cached_geometry)
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
        occupied_spans: list[tuple[int, int]] | None = None,
        coordinate_basis: str = _GEOMETRY_COORDINATE_BASIS,
    ) -> tuple[Any, ...]:
        normalized_spans = _normalized_occupied_spans(int(width), occupied_spans)
        free_spans = tuple(
            _free_spans_from_occupied_spans(
                int(width),
                list(normalized_spans),
                padding_px=_EMPTY_SLOT_PADDING_PX,
            )
        )
        return (
            int(width),
            int(height),
            _normalize_work_area(work_area, int(width), int(height)),
            str(geometry.get("orientation") or ""),
            ("preferred_width", int(preferred_width or 0)),
            ("coordinate_basis", str(coordinate_basis or _GEOMETRY_COORDINATE_BASIS)),
            ("occupied_spans", tuple(normalized_spans)),
            ("free_spans", free_spans),
        )

    def _cache_geometry(
        self,
        width: int,
        height: int,
        work_area: tuple[int, int, int, int] | dict[str, int] | None,
        geometry: dict[str, int | str],
        *,
        preferred_width: int | None = None,
        occupied_spans: list[tuple[int, int]] | None = None,
    ) -> None:
        self._cached_geometry_context = self._geometry_context(
            width,
            height,
            work_area,
            geometry,
            preferred_width=preferred_width,
            occupied_spans=occupied_spans,
            coordinate_basis=str(geometry.get("coordinate_basis") or _GEOMETRY_COORDINATE_BASIS),
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
        fallback_exclude_geometry: dict[str, Any] | None = None,
    ) -> list[tuple[int, int]] | None:
        window = self._window
        sampling_geometry = dict(geometry)
        try:
            origin_x = int(sampling_geometry.get("_screen_origin_x", 0) or 0)
        except Exception:
            origin_x = 0
        exclude_span = (
            _current_horizontal_window_span(window)
            if window is not None and bool(self._window_visible)
            else None
        )
        if exclude_span is not None:
            sampling_geometry["_exclude_spans"] = [
                (int(exclude_span[0]) - origin_x, int(exclude_span[1]) - origin_x)
            ]
        elif window is not None and bool(self._window_visible):
            fallback_span = _horizontal_geometry_exclude_span(fallback_exclude_geometry)
            if fallback_span is not None:
                sampling_geometry["_exclude_spans"] = [fallback_span]
        elif (
            window is not None
            and bool(withdraw_window)
            and self._occupied_span_getter is _detect_horizontal_taskbar_occupied_spans
        ):
            try:
                window.withdraw()
                updater = getattr(self._root, "update_idletasks", None)
                if callable(updater):
                    updater()
                time.sleep(0.035)
            except Exception:
                pass
        try:
            return self._occupied_span_getter(width, height, work_area, sampling_geometry)
        except Exception:
            return None

    def _window_is_alive(self, window: Any) -> bool:
        if window is None:
            return False
        exists = getattr(window, "winfo_exists", None)
        if not callable(exists):
            return True
        try:
            return bool(exists())
        except Exception:
            return False

    def _discard_dead_window(self, window: Any | None = None) -> None:
        target = self._window if window is None else window
        if target is not None:
            destroy = getattr(target, "destroy", None)
            if callable(destroy):
                try:
                    destroy()
                except Exception:
                    pass
        if self._window is target or window is None:
            self._window = None
            self._canvas = None
            self._window_visible = False
        return

    def _recover_broken_surface(self) -> None:
        """Drop a dead or unrenderable overlay surface so the next refresh rebuilds it.

        Shell tray teardown (RDP connect/disconnect, monitor add/remove) can destroy
        the native HWND behind Tk's back; without this recovery every subsequent
        draw raised TclError and permanently killed the tick loops until restart.
        """
        window = self._window
        if window is None:
            return
        if not self._window_is_alive(window):
            self._discard_dead_window(window)
            return
        self._discard_dead_window(window)
        return

    def _ensure_window(self):
        if self._window is not None and not self._window_is_alive(self._window):
            self._discard_dead_window(self._window)
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
        try:
            self._active_taskbar_hwnd = int(geometry.get("_taskbar_hwnd", 0) or 0)
        except Exception:
            self._active_taskbar_hwnd = 0
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
                segment_offset, segment_width_value, segment_progress = (
                    row_layout.segment_geometry(metric_index)
                )
                segment_x = row_layout.metrics_x + segment_offset
                self._draw_metric_segment(
                    canvas,
                    metric,
                    segment_x,
                    y,
                    segment_width_value,
                    row_height,
                    progress_width=segment_progress,
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
        reset_text, reset_short_text = _metric_guidance_texts(metric)
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
        is_credit_metric = metric_key == "credit" and metric.get("percent") is None
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
        if bool(layout["placeholder_visible"]) and not is_credit_metric:
            reset_color = "#4b5563"
        # Credit carries no reset semantics: the "--" placeholder would land
        # right after the amount and obscure it.
        show_reset = bool(display_reset_text) and not is_credit_metric
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
        if not is_credit_metric:
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
                _draw_metric_context_text(
                    canvas,
                    time_text,
                    x=badge_right_x + _RESET_BADGE_TIME_GAP_PX,
                    center_y=center_y,
                    reset_color=reset_color,
                )
        elif show_reset:
            _draw_metric_context_text(
                canvas,
                display_reset_text,
                x=reset_text_x,
                center_y=center_y,
                reset_color=reset_color,
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
        """Bind the overlay to a process-local owner, never to a shell tray window.

        `_active_taskbar_hwnd` stays the logical taskbar target for telemetry and
        rebind resolution only. Owning the overlay with Shell_TrayWnd /
        Shell_SecondaryTrayWnd let explorer's tray teardown (RDP connect/disconnect,
        monitor add/remove) cascade-destroy the overlay behind Tk's back.
        """
        if hwnd <= 0 or win32gui is None or not hasattr(ctypes, "windll"):
            return
        owner_hwnd = self._ensure_native_owner_window()
        if owner_hwnd <= 0 or owner_hwnd == int(hwnd):
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
            setter(int(hwnd), int(_GWLP_HWNDPARENT), int(owner_hwnd))
        except Exception:
            pass
        return

    def _ensure_native_owner_window(self) -> int:
        if int(self._native_owner_hwnd) > 0:
            is_window = getattr(win32gui, "IsWindow", None)
            if callable(is_window):
                try:
                    if bool(is_window(int(self._native_owner_hwnd))):
                        return int(self._native_owner_hwnd)
                except Exception:
                    pass
                else:
                    self._native_owner_hwnd = 0
            else:
                return int(self._native_owner_hwnd)
        if win32gui is None or win32con is None or win32api is None:
            return 0
        class_name = f"{_NATIVE_OWNER_CLASS_PREFIX}{os.getpid()}"
        instance = 0
        atom = 0
        hwnd = 0
        try:
            instance = int(win32api.GetModuleHandle(None) or 0)
            wc = win32gui.WNDCLASS()
            wc.hInstance = instance
            wc.lpszClassName = class_name
            wc.lpfnWndProc = win32gui.DefWindowProc
            try:
                atom = int(win32gui.RegisterClass(wc) or 0)
            except Exception:
                already_registered = getattr(win32gui, "GetClassInfo", None)
                if callable(already_registered):
                    try:
                        registered = already_registered(instance, class_name)
                        atom = int(registered[0] or 0)
                    except Exception:
                        atom = 0
                else:
                    atom = 0
            ex_style = int(getattr(win32con, "WS_EX_TOOLWINDOW", 0x00000080)) | int(
                getattr(win32con, "WS_EX_NOACTIVATE", 0x08000000)
            )
            style = int(getattr(win32con, "WS_POPUP", 0x80000000))
            creator = getattr(win32gui, "CreateWindowEx", None)
            if not callable(creator):
                return 0
            if atom > 0:
                hwnd = int(creator(ex_style, atom, "", style, 0, 0, 0, 0, 0, 0, instance, None) or 0)
            else:
                hwnd = int(creator(ex_style, class_name, "", style, 0, 0, 0, 0, 0, 0, instance, None) or 0)
        except Exception:
            return 0
        self._native_owner_hwnd = int(hwnd)
        return self._native_owner_hwnd

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
        self._window_visible = False
        self._cancel_flash_tick()
        self._cancel_content_tick()
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


def _target_decision_telemetry(
    target: TaskbarOverlayTarget,
    *,
    fullscreen: bool | None = None,
) -> dict[str, Any]:
    payload = {
        "monitor_rect": list(target.monitor.monitor),
        "work_area_rect": list(target.monitor.work),
        "is_primary": bool(target.is_primary),
        "taskbar_hwnd": int(target.taskbar_hwnd),
        "taskbar_class": str(target.taskbar_class or ""),
        "taskbar_rect": list(target.taskbar_rect) if target.taskbar_rect is not None else None,
        "taskbar_visible": bool(target.taskbar_visible),
        "orientation": str(target.orientation or ""),
        "orientation_source": str(target.orientation_source or ""),
        "orientation_confidence": str(target.orientation_confidence or ""),
        "displayable": bool(target.displayable),
        "displayable_reason": str(target.displayable_reason or ""),
        "fallback_reason": str(target.fallback_reason or ""),
        "rca_class": str(target.rca_class or ""),
    }
    if fullscreen is not None:
        payload["fullscreen"] = bool(fullscreen)
    return payload


def _target_decisions_telemetry(
    targets: tuple[TaskbarOverlayTarget, ...],
) -> list[dict[str, Any]]:
    return [_target_decision_telemetry(target) for target in targets]


def _fullscreen_for_target(
    target: TaskbarOverlayTarget,
    fullscreen_by_key: dict[tuple[Any, ...], bool],
    overlay_hwnd: int,
    root: Any | None,
) -> bool:
    key = target_cache_key(target)
    if key not in fullscreen_by_key:
        fullscreen_by_key[key] = bool(
            _is_monitor_fullscreen(
                target.monitor.monitor,
                int(overlay_hwnd),
                root,
            )
        )
    return bool(fullscreen_by_key[key])


def _fullscreen_decisions_telemetry(
    targets: tuple[TaskbarOverlayTarget, ...],
    fullscreen_by_key: dict[tuple[Any, ...], bool],
    overlay_hwnd: int,
    root: Any | None,
) -> list[dict[str, Any]]:
    return [
        _target_decision_telemetry(
            target,
            fullscreen=_fullscreen_for_target(
                target,
                fullscreen_by_key,
                int(overlay_hwnd),
                root,
            ),
        )
        for target in targets
    ]


def _attach_target_placement_telemetry(
    geometry: dict[str, Any],
    targets: tuple[TaskbarOverlayTarget, ...],
    target_decisions: list[dict[str, Any]],
    selected_target: TaskbarOverlayTarget,
    fullscreen_by_key: dict[tuple[Any, ...], bool],
    overlay_hwnd: int,
    root: Any | None,
    *,
    fallback_reason: str,
    rca_class: str,
) -> dict[str, Any]:
    fitted = dict(geometry)
    selected_fullscreen = bool(fullscreen_by_key.get(target_cache_key(selected_target), False))
    fitted["target_decisions"] = list(target_decisions)
    fitted["fullscreen_decisions"] = _fullscreen_decisions_telemetry(
        targets,
        fullscreen_by_key,
        int(overlay_hwnd),
        root,
    )
    fitted["selected_target"] = _target_decision_telemetry(
        selected_target,
        fullscreen=selected_fullscreen,
    )
    fitted["fallback_reason"] = str(fallback_reason or "")
    fitted["rca_class"] = str(rca_class or selected_target.rca_class or "")
    return fitted


def _rca_class_summary(*items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}

    def add(value: Any) -> None:
        if isinstance(value, dict):
            rca_class = str(value.get("rca_class") or "")
            if rca_class:
                counts[rca_class] = counts.get(rca_class, 0) + 1
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                add(nested)

    for item in items:
        add(item)
    return counts


def _fit_horizontal_geometry_to_empty_slot(
    geometry: dict[str, int | str],
    screen_width: int,
    occupied_spans: list[tuple[int, int]] | None,
    *,
    preferred_width: int | None = None,
    previous_geometry: dict[str, Any] | None = None,
    work_area_telemetry: dict[str, Any] | None = None,
    include_telemetry: bool = False,
) -> dict[str, Any]:
    fitted = dict(geometry)
    fitted.setdefault("fallback_reason", "")
    fitted.setdefault("rca_class", "displayable_horizontal_taskbar")
    fitted.setdefault("_slot_side", "")
    if occupied_spans is None:
        if include_telemetry:
            fitted["_telemetry"] = _geometry_telemetry(
                int(screen_width),
                preferred_width,
                work_area_telemetry=work_area_telemetry,
                occupied_spans=None,
                free_spans=[],
                selected_slot=None,
                chosen_geometry=fitted,
            )
        return fitted

    desired_width = max(1, int(fitted.get("width", 0) or 0))
    normalized_spans = _normalized_occupied_spans(int(screen_width), occupied_spans)
    free_spans = _free_spans_from_occupied_spans(
        int(screen_width),
        list(normalized_spans),
        padding_px=_EMPTY_SLOT_PADDING_PX,
    )
    if preferred_width is None:
        target_width = desired_width
    else:
        target_width = min(
            max(_MIN_COMPACT_EMPTY_SLOT_WIDTH_PX, int(preferred_width)),
            desired_width,
        )
    selected_slot = _selected_free_slot(
        free_spans,
        previous_geometry=previous_geometry,
        target_width=target_width,
    )
    if selected_slot is not None:
        fallback_slot = _wider_left_fallback_slot(
            free_spans,
            selected_slot,
            target_width=target_width,
        )
        if fallback_slot is not None:
            selected_slot = fallback_slot
    if not free_spans:
        fitted["visible"] = False
        fitted["width"] = 0
        fitted["height"] = 0
        fitted["_slot_side"] = ""
        fitted["fallback_reason"] = "no_taskbar_empty_slot"
        fitted["rca_class"] = "taskbar_slot_unavailable"
        if include_telemetry:
            fitted["_telemetry"] = _geometry_telemetry(
                int(screen_width),
                preferred_width,
                work_area_telemetry=work_area_telemetry,
                occupied_spans=occupied_spans,
                free_spans=free_spans,
                selected_slot=selected_slot,
                chosen_geometry=fitted,
            )
        return fitted

    start, end = selected_slot if selected_slot is not None else (0, 0)
    available = max(0, int(end) - int(start))
    if available < _MIN_COMPACT_EMPTY_SLOT_WIDTH_PX:
        fitted["visible"] = False
        fitted["width"] = 0
        fitted["height"] = 0
        fitted["_slot_side"] = ""
        fitted["fallback_reason"] = "taskbar_empty_slot_too_narrow"
        fitted["rca_class"] = "taskbar_slot_unavailable"
        if include_telemetry:
            fitted["_telemetry"] = _geometry_telemetry(
                int(screen_width),
                preferred_width,
                work_area_telemetry=work_area_telemetry,
                occupied_spans=occupied_spans,
                free_spans=free_spans,
                selected_slot=selected_slot,
                chosen_geometry=fitted,
            )
        return fitted

    width = min(target_width, available)
    fitted["width"] = int(width)
    fitted["x"] = int(max(start, end - width))
    fitted["_slot_side"] = _slot_side_for_geometry(
        fitted,
        int(screen_width),
    )
    fitted["visible"] = True
    fitted["fallback_reason"] = ""
    fitted["rca_class"] = "displayable_horizontal_taskbar"
    if include_telemetry:
        fitted["_telemetry"] = _geometry_telemetry(
            int(screen_width),
            preferred_width,
            work_area_telemetry=work_area_telemetry,
            occupied_spans=occupied_spans,
            free_spans=free_spans,
            selected_slot=selected_slot,
            chosen_geometry=fitted,
        )
    return fitted


def _selected_free_slot(
    free_spans: list[tuple[int, int]],
    *,
    previous_geometry: dict[str, Any] | None = None,
    target_width: int | None = None,
) -> tuple[int, int] | None:
    if not free_spans:
        return None
    usable_spans = [
        span
        for span in free_spans
        if int(span[1]) - int(span[0]) >= _MIN_COMPACT_EMPTY_SLOT_WIDTH_PX
    ]
    candidates = usable_spans or free_spans
    rightmost_slot = max(candidates, key=lambda span: (int(span[1]), int(span[0])))
    previous_slot = _previous_geometry_free_slot(
        [rightmost_slot],
        previous_geometry=previous_geometry,
        target_width=target_width,
    )
    if previous_slot is not None:
        return previous_slot
    return rightmost_slot


def _wider_left_fallback_slot(
    free_spans: list[tuple[int, int]],
    rightmost_slot: tuple[int, int],
    *,
    target_width: int,
) -> tuple[int, int] | None:
    """Return a left slot that fits the full content width when the right slot cannot.

    Display contract: the overlay prefers the rightmost taskbar slot, but a
    slot narrower than the content width silently hides the overlay. When the
    right slot cannot fit `target_width` and a left free span can, use the
    left span instead of disappearing.
    """
    right_width = int(rightmost_slot[1]) - int(rightmost_slot[0])
    if right_width >= int(target_width):
        return None
    left_candidates = [
        span
        for span in free_spans
        if int(span[1]) - int(span[0]) >= int(target_width)
        and int(span[0]) < int(rightmost_slot[0])
    ]
    if not left_candidates:
        return None
    return max(left_candidates, key=lambda span: (int(span[1]) - int(span[0]), int(span[0])))


def _slot_side_for_geometry(geometry: dict[str, Any], screen_width: int) -> str:
    try:
        x = int(geometry.get("x", 0) or 0)
        width = int(geometry.get("width", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if width <= 0:
        return ""
    midpoint_twice = x * 2 + width
    if midpoint_twice >= int(screen_width):
        return _SLOT_SIDE_RIGHT
    return _SLOT_SIDE_LEFT


def _previous_geometry_free_slot(
    free_spans: list[tuple[int, int]],
    *,
    previous_geometry: dict[str, Any] | None,
    target_width: int | None,
) -> tuple[int, int] | None:
    if not isinstance(previous_geometry, dict) or not bool(
        previous_geometry.get("visible", True)
    ):
        return None
    if str(previous_geometry.get("orientation") or "") not in {"bottom", "top"}:
        return None
    try:
        previous_x = int(previous_geometry.get("x", 0))
        previous_width = int(previous_geometry.get("width", 0))
    except (TypeError, ValueError):
        return None
    if previous_width <= 0:
        return None
    previous_center = previous_x + max(1, previous_width) // 2
    min_slot_width = _MIN_COMPACT_EMPTY_SLOT_WIDTH_PX
    if target_width is not None:
        try:
            min_slot_width = min(
                max(_MIN_COMPACT_EMPTY_SLOT_WIDTH_PX, int(target_width)),
                max(_MIN_COMPACT_EMPTY_SLOT_WIDTH_PX, previous_width),
            )
        except (TypeError, ValueError):
            min_slot_width = _MIN_COMPACT_EMPTY_SLOT_WIDTH_PX
    for start, end in free_spans:
        span_width = int(end) - int(start)
        if span_width < min_slot_width:
            continue
        if int(start) <= previous_center <= int(end):
            return int(start), int(end)
    return None


def _slot_classification(width: int) -> str:
    width = int(width)
    if width < _MIN_COMPACT_EMPTY_SLOT_WIDTH_PX:
        return "hidden"
    if width < _STATUS_TEXT_MIN_OVERLAY_WIDTH_PX:
        return "compact"
    if width < _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX:
        return "status_text"
    return "text_friendly"


def _geometry_telemetry(
    screen_width: int,
    preferred_width: int | None,
    *,
    work_area_telemetry: dict[str, Any] | None,
    occupied_spans: list[tuple[int, int]] | None,
    free_spans: list[tuple[int, int]],
    selected_slot: tuple[int, int] | None,
    chosen_geometry: dict[str, Any],
) -> dict[str, Any]:
    normalized_spans = (
        _normalized_occupied_spans(int(screen_width), occupied_spans)
        if occupied_spans is not None
        else tuple()
    )
    visible = bool(chosen_geometry.get("visible", True))
    try:
        chosen_width = int(chosen_geometry.get("width", 0) or 0)
    except Exception:
        chosen_width = 0
    available_width = (
        max(0, int(selected_slot[1]) - int(selected_slot[0]))
        if selected_slot is not None
        else 0
    )
    classification_width = chosen_width if visible else available_width
    fallback_reason = str(chosen_geometry.get("fallback_reason") or "")
    rca_class = str(chosen_geometry.get("rca_class") or "")
    if not fallback_reason and not visible:
        if not free_spans:
            fallback_reason = "no_taskbar_empty_slot"
        elif available_width < _MIN_COMPACT_EMPTY_SLOT_WIDTH_PX:
            fallback_reason = "taskbar_empty_slot_too_narrow"
        else:
            fallback_reason = "taskbar_geometry_hidden"
    if not rca_class:
        rca_class = (
            "displayable_horizontal_taskbar"
            if visible
            else "taskbar_slot_unavailable"
        )
    telemetry_geometry = {
        key: value
        for key, value in chosen_geometry.items()
        if key != "_telemetry"
    }
    return {
        "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
        "screen_width": int(screen_width),
        "work_area": work_area_telemetry or {},
        "raw_occupied_spans": list(occupied_spans or []),
        "merged_occupied_spans": tuple(normalized_spans),
        "normalized_occupied_spans": tuple(normalized_spans),
        "free_spans": tuple((int(start), int(end)) for start, end in free_spans),
        "padded_free_spans": tuple((int(start), int(end)) for start, end in free_spans),
        "preferred_width": None if preferred_width is None else int(preferred_width),
        "fallback_reason": fallback_reason,
        "rca_class": rca_class,
        "selected_slot": {
            "span": None
            if selected_slot is None
            else (int(selected_slot[0]), int(selected_slot[1])),
            "available_width": int(available_width),
            "classification": _slot_classification(int(classification_width)),
        },
        "chosen_geometry": telemetry_geometry,
        "conversions": {
            "work_area": (work_area_telemetry or {}).get("conversion", {}),
            "occupied_spans": {
                "raw_basis": _GEOMETRY_COORDINATE_BASIS,
                "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
            },
        },
    }


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
    previous_side = _horizontal_geometry_slot_side(previous)
    current_side = _horizontal_geometry_slot_side(current)
    if previous_side and current_side and previous_side != current_side:
        return False
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
    return (
        _same_width_geometry_x_shift_delta(
            previous,
            current,
            tolerance_px=tolerance_px,
        )
        is not None
    )


def _horizontal_geometry_slot_side(geometry: dict[str, Any]) -> str:
    if not bool(geometry.get("visible", True)):
        return ""
    if str(geometry.get("orientation") or "") not in {"bottom", "top"}:
        return ""
    side = str(geometry.get("_slot_side") or "")
    if side in {_SLOT_SIDE_LEFT, _SLOT_SIDE_RIGHT}:
        return side
    return ""


def _transient_geometry_context_key(context: Any) -> Any:
    if context is None:
        return None
    if isinstance(context, tuple):
        if (
            len(context) == 2
            and isinstance(context[0], str)
            and context[0] in {"occupied_spans", "free_spans"}
        ):
            return None
        items = []
        for item in context:
            normalized = _transient_geometry_context_key(item)
            if normalized is not None:
                items.append(normalized)
        return tuple(items)
    if isinstance(context, list):
        items = []
        for item in context:
            normalized = _transient_geometry_context_key(item)
            if normalized is not None:
                items.append(normalized)
        return tuple(items)
    return context


def _is_transient_geometry_x_shift(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance_px: int = _GEOMETRY_CHANGE_TOLERANCE_PX,
) -> bool:
    x_delta = _same_width_geometry_x_shift_delta(
        previous,
        current,
        tolerance_px=tolerance_px,
    )
    return (
        x_delta is not None
        and x_delta <= _GEOMETRY_TRANSIENT_X_SHIFT_TOLERANCE_PX
    )


def _same_width_geometry_x_shift_delta(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance_px: int = _GEOMETRY_CHANGE_TOLERANCE_PX,
) -> int | None:
    if not bool(previous.get("visible", True)):
        return None
    if not bool(current.get("visible", True)):
        return None
    if str(previous.get("orientation") or "") != str(current.get("orientation") or ""):
        return None
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
        return None
    if abs(current_width - previous_width) > int(tolerance_px):
        return None
    if abs(current_y - previous_y) > int(tolerance_px):
        return None
    if abs(current_height - previous_height) > int(tolerance_px):
        return None
    x_delta = abs(current_x - previous_x)
    if x_delta <= int(tolerance_px):
        return None
    return int(x_delta)


def _horizontal_geometry_exclude_span(
    geometry: dict[str, Any] | None,
) -> tuple[int, int] | None:
    if not isinstance(geometry, dict) or not bool(geometry.get("visible", True)):
        return None
    if str(geometry.get("orientation") or "") not in {"bottom", "top"}:
        return None
    try:
        x = int(geometry.get("x", 0))
        width = int(geometry.get("width", 0))
    except (TypeError, ValueError):
        return None
    if width <= 0:
        return None
    return int(x), int(x + width)


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
    normalized = list(_normalized_occupied_spans(int(screen_width), occupied_spans))
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


def _normalized_occupied_spans(
    screen_width: int,
    occupied_spans: list[tuple[int, int]] | tuple[tuple[int, int], ...] | None,
) -> tuple[tuple[int, int], ...]:
    if not occupied_spans:
        return tuple()
    normalized: list[tuple[int, int]] = []
    for span in occupied_spans:
        try:
            start, end = span
            start_i = max(0, min(int(screen_width), int(start)))
            end_i = max(0, min(int(screen_width), int(end)))
        except Exception:
            continue
        if end_i > start_i:
            normalized.append((start_i, end_i))
    return tuple(_merge_spans(normalized))


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
    spans, _telemetry = _detect_horizontal_taskbar_occupied_spans_with_debug(
        screen_width,
        screen_height,
        work_area,
        geometry,
    )
    return spans


def _detect_horizontal_taskbar_occupied_spans_with_debug(
    screen_width: int,
    screen_height: int,
    work_area: tuple[int, int, int, int] | dict[str, int] | None,
    geometry: dict[str, Any],
) -> tuple[list[tuple[int, int]] | None, dict[str, Any]]:
    telemetry: dict[str, Any] = {
        "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
        "child_spans_by_class": {},
        "child_spans": [],
        "pixel_spans": [],
        "edge_guards": [],
        "merged_occupied_spans": [],
        "free_spans": [],
        "padded_free_spans": [],
        "conversions": {},
    }
    if not hasattr(ctypes, "windll"):
        return None, telemetry
    try:
        origin_x = int(geometry.get("_screen_origin_x", 0) or 0)
    except Exception:
        origin_x = 0
    try:
        origin_y = int(geometry.get("_screen_origin_y", 0) or 0)
    except Exception:
        origin_y = 0
    try:
        taskbar_hwnd = int(geometry.get("_taskbar_hwnd", 0) or 0)
    except Exception:
        taskbar_hwnd = 0
    (left, top, right, bottom), work_area_debug = _normalize_work_area_with_metadata(
        work_area,
        screen_width,
        screen_height,
    )
    telemetry["work_area"] = work_area_debug
    telemetry["conversions"]["work_area"] = work_area_debug.get("conversion", {})
    orientation = str(geometry.get("orientation") or "")
    if orientation == "bottom":
        band_top = max(0, min(int(screen_height), int(bottom)))
        band_bottom = int(screen_height)
    elif orientation == "top":
        band_top = 0
        band_bottom = max(0, min(int(screen_height), int(top)))
    else:
        return None, telemetry
    if band_bottom - band_top < 8:
        return None, telemetry

    excluded_spans = _geometry_exclude_spans(geometry, int(screen_width))
    child_records = _taskbar_child_occupied_span_records(
        int(screen_width),
        int(band_top) + origin_y,
        int(band_bottom) + origin_y,
        taskbar_hwnd=taskbar_hwnd,
        origin_x=origin_x,
    )
    telemetry["child_spans"] = child_records
    child_spans_by_class: dict[str, list[dict[str, Any]]] = {}
    for record in child_records:
        class_name = str(record.get("class_name") or "")
        child_spans_by_class.setdefault(class_name, []).append(record)
    telemetry["child_spans_by_class"] = child_spans_by_class
    telemetry["conversions"]["child_window_rects"] = {
        "raw_basis": "global_physical_px",
        "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
        "origin_x": int(origin_x),
        "origin_y": int(origin_y),
    }
    suppressed_child_records = [
        record
        for record in child_records
        if _child_span_is_structural_overlay_container(record, excluded_spans)
    ]
    if suppressed_child_records:
        telemetry["suppressed_child_spans"] = suppressed_child_records
    occupied = [
        tuple(record["span"])
        for record in child_records
        if record not in suppressed_child_records
    ]
    if excluded_spans:
        occupied = _subtract_spans(occupied, excluded_spans)
    telemetry["excluded_spans"] = excluded_spans
    sample_rows = _taskbar_sample_rows(band_top, band_bottom)
    telemetry["sample_rows"] = sample_rows
    columns = _sample_taskbar_columns(
        int(screen_width),
        sample_rows,
        origin_x=origin_x,
        origin_y=origin_y,
    )
    if not columns and not occupied:
        return None, telemetry
    if columns:
        sampled_columns = [
            (x, colors)
            for x, colors in columns
            if not excluded_spans
            or not _span_overlaps_any(
                int(x),
                int(x) + _TASKBAR_SAMPLE_STEP_PX,
                excluded_spans,
            )
        ]
        background = _median_background_color(sampled_columns or columns)
        telemetry["pixel_background"] = background
        for x, colors in sampled_columns:
            if _column_looks_occupied(colors, background):
                span = (
                    max(0, x - _OCCUPIED_DILATION_PX),
                    min(int(screen_width), x + _TASKBAR_SAMPLE_STEP_PX + _OCCUPIED_DILATION_PX),
                )
                telemetry["pixel_spans"].append(
                    {
                        "sample_x": int(x),
                        "span": span,
                        "raw_basis": "monitor_local_physical_px",
                        "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
                        "colors": list(colors),
                    }
                )
                occupied.append(
                    span
                )

    # Keep the reserved taskbar edge controls out of the overlay even when the
    # sampled pixels happen to be close to the background color.
    edge_guard = max(72, min(180, int(screen_width * 0.04)))
    edge_spans = [(0, edge_guard), (int(screen_width) - edge_guard, int(screen_width))]
    telemetry["edge_guards"] = [
        {
            "span": span,
            "raw_basis": "monitor_local_physical_px",
            "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
        }
        for span in edge_spans
    ]
    occupied.extend(edge_spans)
    merged = _merge_spans(occupied)
    free_spans = _free_spans_from_occupied_spans(
        int(screen_width),
        merged,
        padding_px=_EMPTY_SLOT_PADDING_PX,
    )
    telemetry["merged_occupied_spans"] = merged
    telemetry["free_spans"] = free_spans
    telemetry["padded_free_spans"] = free_spans
    telemetry["conversions"]["pixel_samples"] = {
        "raw_basis": "monitor_local_physical_px",
        "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
        "origin_x": int(origin_x),
        "origin_y": int(origin_y),
    }
    return merged, telemetry


def _child_span_is_structural_overlay_container(
    record: dict[str, Any],
    excluded_spans: list[tuple[int, int]],
) -> bool:
    if not excluded_spans:
        return False
    class_name = str(record.get("class_name") or "")
    if class_name not in _TASKBAR_STRUCTURAL_CHILD_CLASSES:
        return False
    try:
        start, end = tuple(record.get("span") or ())[:2]
        span_start = int(start)
        span_end = int(end)
    except (TypeError, ValueError):
        return False
    span_width = max(0, span_end - span_start)
    if span_width <= 0:
        return False
    for excluded_start, excluded_end in excluded_spans:
        excluded_width = max(0, int(excluded_end) - int(excluded_start))
        if not _span_overlaps_any(span_start, span_end, [(excluded_start, excluded_end)]):
            continue
        structural_width = max(
            _TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX,
            excluded_width + _MIN_COMPACT_EMPTY_SLOT_WIDTH_PX,
        )
        if span_width >= structural_width:
            return True
    return False


def _taskbar_child_occupied_span_records(
    screen_width: int,
    band_top: int,
    band_bottom: int,
    *,
    taskbar_hwnd: int = 0,
    origin_x: int = 0,
) -> list[dict[str, Any]]:
    if win32gui is None:
        return []
    if int(taskbar_hwnd) <= 0:
        try:
            taskbar_hwnd = int(win32gui.FindWindow("Shell_TrayWnd", None) or 0)
        except Exception:
            return []
    if taskbar_hwnd <= 0:
        return []

    records: list[dict[str, Any]] = []

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
        start = max(0, min(int(screen_width), int(left) - int(origin_x)))
        end = max(0, min(int(screen_width), int(right) - int(origin_x)))
        if end - start >= 8:
            records.append(
                {
                    "hwnd": int(hwnd),
                    "class_name": class_name,
                    "raw_rect": (int(left), int(top), int(right), int(bottom)),
                    "raw_basis": "global_physical_px",
                    "span": (int(start), int(end)),
                    "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
                    "conversion": {
                        "origin_x": int(origin_x),
                        "band_top": int(band_top),
                        "band_bottom": int(band_bottom),
                    },
                }
            )
        return True

    try:
        win32gui.EnumChildWindows(taskbar_hwnd, visit, None)
    except Exception:
        return records
    return records


def _taskbar_child_occupied_spans(
    screen_width: int,
    band_top: int,
    band_bottom: int,
    *,
    taskbar_hwnd: int = 0,
    origin_x: int = 0,
) -> list[tuple[int, int]]:
    return [
        tuple(record["span"])
        for record in _taskbar_child_occupied_span_records(
            screen_width,
            band_top,
            band_bottom,
            taskbar_hwnd=taskbar_hwnd,
            origin_x=origin_x,
        )
    ]


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
    *,
    origin_x: int = 0,
    origin_y: int = 0,
) -> list[tuple[int, list[tuple[int, int, int]]]]:
    if not sample_rows or screen_width <= 0:
        return []
    capture_top = min(sample_rows)
    capture_bottom = max(sample_rows) + 1
    capture_height = max(1, capture_bottom - capture_top)
    pixels = _capture_screen_region_bgra(
        int(origin_x),
        int(origin_y) + capture_top,
        int(screen_width),
        capture_height,
    )
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


def _collect_taskbar_overlay_targets() -> tuple[TaskbarOverlayTarget, ...]:
    if win32api is None or win32gui is None or win32con is None:
        return ()
    enum_monitors = getattr(win32api, "EnumDisplayMonitors", None)
    if not callable(enum_monitors):
        return ()
    monitors: list[TaskbarMonitorSnapshot] = []
    try:
        for hmon, _hdc, _rect in enum_monitors(None, None):
            info = win32api.GetMonitorInfo(hmon)
            monitor_rect = normalize_rect(info.get("Monitor"))
            work_rect = normalize_rect(info.get("Work"))
            if monitor_rect is None or work_rect is None:
                continue
            device = str(info.get("Device") or "")
            flags = int(info.get("Flags", 0) or 0)
            monitors.append(
                TaskbarMonitorSnapshot(
                    handle=int(hmon or 0),
                    device=device,
                    display_num=parse_display_num(device),
                    is_primary=bool(
                        flags & int(getattr(win32con, "MONITORINFOF_PRIMARY", 1))
                    ),
                    monitor=monitor_rect,
                    work=work_rect,
                )
            )
    except Exception:
        return ()
    if not monitors:
        return ()
    return build_taskbar_overlay_targets(monitors, _collect_taskbar_windows())


def capture_local_taskbar_overlay_geometry_snapshot(
    sample_count: int = 5,
    sample_interval_sec: float = 0.5,
) -> dict[str, Any]:
    count = max(1, int(sample_count or 1))
    interval = max(0.0, float(sample_interval_sec or 0.0))
    samples: list[dict[str, Any]] = []
    for index in range(count):
        samples.append(_collect_local_taskbar_overlay_geometry_sample())
        if interval > 0.0 and index < count - 1:
            time.sleep(interval)
    latest = dict(samples[-1])
    signatures = [_taskbar_geometry_sample_signature(sample) for sample in samples]
    stable = all(signature == signatures[0] for signature in signatures)
    latest["samples"] = samples
    latest["repeated_sample_stability"] = {
        "stable": bool(stable),
        "sample_count": int(count),
        "signatures": signatures,
    }
    return latest


def _collect_local_taskbar_overlay_geometry_sample() -> dict[str, Any]:
    targets = _collect_taskbar_overlay_targets()
    target = _select_local_debug_taskbar_target(targets)
    target_decisions = _target_decisions_telemetry(targets)
    fullscreen_by_key: dict[tuple[Any, ...], bool] = {}
    fullscreen_decisions = _fullscreen_decisions_telemetry(
        targets,
        fullscreen_by_key,
        0,
        None,
    )
    if target is not None:
        width, height = monitor_size(target.monitor)
        work_area = local_work_area(target.monitor)
        monitor_rect = tuple(target.monitor.monitor)
        taskbar_rect = target.taskbar_rect
        taskbar_hwnd = int(target.taskbar_hwnd)
        origin_x = int(target.monitor.monitor[0])
        origin_y = int(target.monitor.monitor[1])
    else:
        width, height = _fallback_screen_size()
        work_area = _get_primary_work_area() or (0, 0, width, height)
        monitor_rect = (0, 0, width, height)
        taskbar_rect = None
        taskbar_hwnd = 0
        origin_x = 0
        origin_y = 0
    base_geometry = calculate_taskbar_overlay_geometry(
        width,
        height,
        work_area,
        include_telemetry=True,
    )
    sampling_geometry = dict(base_geometry)
    sampling_geometry["_screen_origin_x"] = int(origin_x)
    sampling_geometry["_screen_origin_y"] = int(origin_y)
    sampling_geometry["_taskbar_hwnd"] = int(taskbar_hwnd)
    occupied_spans: list[tuple[int, int]] | None = None
    occupancy_telemetry: dict[str, Any] = {
        "child_spans_by_class": {},
        "pixel_spans": [],
        "edge_guards": [],
        "merged_occupied_spans": [],
        "free_spans": [],
        "padded_free_spans": [],
        "conversions": {},
    }
    if str(base_geometry.get("orientation") or "") in {"bottom", "top"}:
        occupied_spans, occupancy_telemetry = (
            _detect_horizontal_taskbar_occupied_spans_with_debug(
                width,
                height,
                work_area,
                sampling_geometry,
            )
        )
    chosen_geometry = (
        calculate_taskbar_overlay_geometry(
            width,
            height,
            work_area,
            occupied_spans=occupied_spans,
            include_telemetry=True,
        )
        if occupied_spans is not None
        else base_geometry
    )
    geometry_telemetry = dict(chosen_geometry.get("_telemetry", {}))
    merged_spans = occupancy_telemetry.get(
        "merged_occupied_spans",
        geometry_telemetry.get("merged_occupied_spans", []),
    )
    free_spans = occupancy_telemetry.get(
        "padded_free_spans",
        geometry_telemetry.get("padded_free_spans", []),
    )
    conversions = dict(geometry_telemetry.get("conversions", {}))
    conversions.update(dict(occupancy_telemetry.get("conversions", {})))
    selected_target = (
        _target_decision_telemetry(
            target,
            fullscreen=bool(fullscreen_by_key.get(target_cache_key(target), False)),
        )
        if target is not None
        else None
    )
    fallback_reason = str(chosen_geometry.get("fallback_reason") or "")
    if not fallback_reason and target is not None:
        fallback_reason = str(target.fallback_reason or "")
    rca_class = str(chosen_geometry.get("rca_class") or "")
    if not rca_class and target is not None:
        rca_class = str(target.rca_class or "")
    return {
        "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
        "monitor_rect": list(monitor_rect),
        "work_area": geometry_telemetry.get("work_area", {}),
        "taskbar_rect": list(taskbar_rect) if taskbar_rect is not None else None,
        "taskbar_hwnd": int(taskbar_hwnd),
        "child_spans_by_class": occupancy_telemetry.get("child_spans_by_class", {}),
        "pixel_spans": occupancy_telemetry.get("pixel_spans", []),
        "edge_guards": occupancy_telemetry.get("edge_guards", []),
        "merged_occupied_spans": merged_spans,
        "free_spans": free_spans,
        "padded_free_spans": free_spans,
        "preferred_width": geometry_telemetry.get("preferred_width"),
        "chosen_geometry": {
            key: value
            for key, value in chosen_geometry.items()
            if key != "_telemetry"
        },
        "dpi": _taskbar_debug_dpi(int(taskbar_hwnd)),
        "theme": _windows_theme_snapshot(),
        "icon_alignment": _taskbar_icon_alignment(),
        "conversions": conversions,
        "target_decisions": target_decisions,
        "selected_target": selected_target,
        "fullscreen_decisions": fullscreen_decisions,
        "fallback_reason": fallback_reason,
        "rca_class": rca_class,
        "rca_class_summary": _rca_class_summary(
            target_decisions,
            selected_target,
            {"rca_class": rca_class},
        ),
    }


def _select_local_debug_taskbar_target(
    targets: tuple[TaskbarOverlayTarget, ...],
) -> TaskbarOverlayTarget | None:
    if not targets:
        return None
    for target in targets:
        if target.is_primary and bool(target.displayable):
            return target
    for target in targets:
        if bool(target.displayable):
            return target
    return targets[0]


def _local_debug_taskbar_target() -> TaskbarOverlayTarget | None:
    targets = _collect_taskbar_overlay_targets()
    return _select_local_debug_taskbar_target(targets)


def _fallback_screen_size() -> tuple[int, int]:
    if win32api is not None:
        try:
            return int(win32api.GetSystemMetrics(0)), int(win32api.GetSystemMetrics(1))
        except Exception:
            pass
    return 1920, 1080


def _taskbar_geometry_sample_signature(sample: dict[str, Any]) -> tuple[Any, ...]:
    geometry = sample.get("chosen_geometry")
    if not isinstance(geometry, dict):
        geometry = {}
    return (
        tuple(sample.get("merged_occupied_spans") or ()),
        tuple(sample.get("padded_free_spans") or ()),
        bool(geometry.get("visible", True)),
        int(geometry.get("x", 0) or 0),
        int(geometry.get("width", 0) or 0),
        str(sample.get("coordinate_basis") or ""),
    )


def _taskbar_debug_dpi(taskbar_hwnd: int) -> dict[str, Any]:
    dpi = 96
    if hasattr(ctypes, "windll"):
        try:
            getter = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
            if callable(getter) and int(taskbar_hwnd) > 0:
                dpi = int(getter(int(taskbar_hwnd)) or 96)
        except Exception:
            dpi = 96
    scale = max(0.1, float(dpi) / 96.0)
    return {"dpi": int(dpi), "scale_x": scale, "scale_y": scale}


def _windows_theme_snapshot() -> dict[str, Any]:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            apps_light, _value_type = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return {
            "name": "light" if int(apps_light) else "dark",
            "apps_use_light_theme": bool(apps_light),
        }
    except Exception:
        return {"name": "unknown"}


def _taskbar_icon_alignment() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
        ) as key:
            value, _value_type = winreg.QueryValueEx(key, "TaskbarAl")
        return "left" if int(value) == 0 else "center"
    except Exception:
        return "unknown"


def _collect_taskbar_windows() -> tuple[TaskbarWindowSnapshot, ...]:
    if win32gui is None:
        return ()
    taskbars: dict[int, TaskbarWindowSnapshot] = {}

    def add(hwnd: int) -> None:
        snapshot = _taskbar_window_snapshot(hwnd)
        if snapshot is not None:
            taskbars[int(snapshot.hwnd)] = snapshot

    try:
        add(int(win32gui.FindWindow("Shell_TrayWnd", None) or 0))
    except Exception:
        pass
    enum_windows = getattr(win32gui, "EnumWindows", None)
    if callable(enum_windows):
        def visit(hwnd: int, _extra: Any) -> bool:
            add(int(hwnd))
            return True

        try:
            enum_windows(visit, None)
        except Exception:
            pass
    return tuple(taskbars.values())


def _taskbar_window_snapshot(hwnd: int) -> TaskbarWindowSnapshot | None:
    hwnd = int(hwnd or 0)
    if hwnd <= 0 or win32gui is None:
        return None
    try:
        class_name = str(win32gui.GetClassName(hwnd) or "")
    except Exception:
        return None
    if class_name not in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
        return None
    rect = normalize_rect(_safe_window_rect(hwnd))
    if rect is None:
        return None
    try:
        visible = bool(win32gui.IsWindowVisible(hwnd))
    except Exception:
        visible = False
    return TaskbarWindowSnapshot(
        hwnd=hwnd,
        class_name=class_name,
        rect=rect,
        visible=visible,
    )


def _safe_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        return tuple(int(v) for v in win32gui.GetWindowRect(int(hwnd)))[:4]
    except Exception:
        return None


def _is_monitor_fullscreen(
    monitor_rect: tuple[int, int, int, int],
    overlay_hwnd: int,
    root: Any | None,
) -> bool:
    if win32gui is None or win32con is None:
        return False
    return _visible_fullscreen_window_exists(
        int(overlay_hwnd),
        root,
        tuple(int(v) for v in monitor_rect),
    )


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
    normalized, _metadata = _normalize_work_area_with_metadata(
        work_area,
        screen_width,
        screen_height,
    )
    return normalized


def _normalize_work_area_with_metadata(
    work_area: tuple[int, int, int, int] | dict[str, int] | None,
    screen_width: int,
    screen_height: int,
) -> tuple[tuple[int, int, int, int], dict[str, Any]]:
    screen_width = max(1, int(screen_width or 0))
    screen_height = max(1, int(screen_height or 0))
    if isinstance(work_area, dict):
        raw_basis = str(work_area.get("coordinate_basis") or "physical_px")
        try:
            scale_x = float(work_area.get("scale_x", work_area.get("scale", 1)) or 1)
        except Exception:
            scale_x = 1.0
        try:
            scale_y = float(work_area.get("scale_y", work_area.get("scale", 1)) or 1)
        except Exception:
            scale_y = 1.0
        raw = (
            int(work_area.get("left", 0)),
            int(work_area.get("top", 0)),
            int(work_area.get("right", screen_width)),
            int(work_area.get("bottom", screen_height)),
        )
        if raw_basis == "logical_px":
            converted = (
                int(round(raw[0] * scale_x)),
                int(round(raw[1] * scale_y)),
                int(round(raw[2] * scale_x)),
                int(round(raw[3] * scale_y)),
            )
        else:
            converted = raw
    elif isinstance(work_area, tuple) and len(work_area) == 4:
        raw_basis = "physical_px"
        scale_x = 1.0
        scale_y = 1.0
        raw = tuple(int(v) for v in work_area)
        converted = raw
    else:
        raw_basis = "default_full_screen"
        scale_x = 1.0
        scale_y = 1.0
        raw = (0, 0, int(screen_width), int(screen_height))
        converted = raw

    left, top, right, bottom = converted
    left = max(0, min(int(left), screen_width))
    top = max(0, min(int(top), screen_height))
    right = max(0, min(int(right), screen_width))
    bottom = max(0, min(int(bottom), screen_height))
    if right <= left or bottom <= top:
        normalized = (0, 0, int(screen_width), int(screen_height))
    else:
        normalized = (int(left), int(top), int(right), int(bottom))
    metadata = {
        "raw": raw,
        "raw_basis": raw_basis,
        "converted": converted,
        "normalized": normalized,
        "coordinate_basis": _GEOMETRY_COORDINATE_BASIS,
        "conversion": {
            "from": raw_basis,
            "to": _GEOMETRY_COORDINATE_BASIS,
            "scale_x": float(scale_x),
            "scale_y": float(scale_y),
            "applied": raw_basis == "logical_px",
        },
    }
    return normalized, metadata


def _descriptor_percent(descriptor: dict[str, Any]) -> int | None:
    if "percent" in descriptor:
        value = descriptor.get("percent")
    elif "remaining_percent" in descriptor:
        value = descriptor.get("remaining_percent")
    else:
        value = descriptor.get("value_text")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(round(float(value)))))
    return _parse_percent(value)


def _credit_metric_descriptor(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Build the taskbar credit metric when the account reports a usable credit.

    "0" is a real reported balance and still shows. Missing, empty, or
    placeholder payloads mean the provider page does not expose credit and the
    metric must not exist (same presence contract as other optional metrics).
    """
    raw = str(snapshot.get("remaining_credit") or "").strip()
    if not raw or raw.lower() in {"n/a", "unavailable", "조회 불가"}:
        return None
    normalized = raw
    if raw.lower().startswith("$"):
        normalized = raw[1:].strip() or raw
    try:
        amount = float(normalized.replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    if amount >= 1000:
        display = f"${int(round(amount)):,}"
    else:
        display = f"${amount:g}"
    return {
        "metric_key": "credit",
        "key": "CR",
        "short_label": "CR",
        "percent": None,
        "value_text": display,
        "short_value_text": display,
        "reset_at": "",
        "reset_precision": "",
        "state": "ready",
    }


def _build_provider_metric(
    descriptor: dict[str, Any],
    *,
    provider: str,
    profile_id: str,
    freshness: str,
    provider_status: Any,
    account_state: str,
    captured_at_value: Any,
    fallback_index: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    metric_key = str(
        descriptor.get("key")
        or descriptor.get("metric_key")
        or descriptor.get("id")
        or f"metric_{int(fallback_index)}"
    )
    short_label = str(
        descriptor.get("short_label")
        or descriptor.get("label")
        or descriptor.get("display_label")
        or metric_key
    )
    percent = _descriptor_percent(descriptor)
    reset_at = descriptor.get("reset_at")
    if reset_at is None:
        reset_at = descriptor.get("reset_at_value", descriptor.get("reset", ""))
    descriptor_captured_at = descriptor.get("captured_at", captured_at_value)
    reset_precision = str(descriptor.get("reset_precision") or "").strip().lower()
    metric = _build_metric(
        key=metric_key,
        short_label=short_label,
        raw_value="" if percent is None else f"{int(percent)}%",
        reset_at_value=reset_at,
        reset_precision=reset_precision,
        captured_at_value=descriptor_captured_at,
        account_state=account_state,
        now=now,
    )
    explicit_value_text = descriptor.get("value_text")
    explicit_short_value_text = descriptor.get("short_value_text")
    explicit_state = str(descriptor.get("state") or "").strip()
    explicit_color = str(descriptor.get("color") or "").strip()
    metric.update(
        {
            "id": str(descriptor.get("id") or metric_key),
            "provider": str(provider),
            "profile_id": str(profile_id),
            "freshness": str(freshness),
            "provider_status": provider_status,
            "metric_key": metric_key,
            "key": short_label,
            "short_label": short_label,
            "percent": percent,
            "value_text": (
                str(explicit_short_value_text)
                if explicit_short_value_text is not None
                else str(explicit_value_text)
                if explicit_value_text is not None
                else str(metric.get("value_text") or "--")
            ),
            "detail_value_text": (
                str(explicit_value_text)
                if explicit_value_text is not None
                else str(metric.get("value_text") or "--")
            ),
            "reset_at": str(reset_at or ""),
            "reset_precision": reset_precision,
        }
    )
    if explicit_state:
        metric["state"] = explicit_state
    if explicit_color:
        metric["color"] = explicit_color
    return metric


def _build_metric(
    *,
    key: str,
    short_label: str,
    raw_value: Any,
    reset_at_value: Any = "",
    reset_precision: str = "",
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
        reset_precision=reset_precision,
        now=now,
    )
    normal_guidance = _build_normal_guidance(
        metric_key=key,
        current_percent=percent,
        reset_at_value=reset_at_value,
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
        guidance_direction = str(normal_guidance.get("direction") or "")
        if guidance_direction in known_reset_directions:
            reset_direction = guidance_direction
        elif snapshot_reset_direction in known_reset_directions:
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
        "normal_min_percent": normal_guidance.get("normal_min_percent"),
        "normal_max_percent": normal_guidance.get("normal_max_percent"),
        "normal_transition_seconds": normal_guidance.get(
            "normal_transition_seconds"
        ),
        "normal_guidance_text": str(normal_guidance.get("text") or ""),
        "normal_guidance_short_text": str(normal_guidance.get("short_text") or ""),
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


def _build_normal_guidance(
    *,
    metric_key: str,
    current_percent: int | None,
    reset_at_value: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    unavailable = {
        "direction": _RESET_DIRECTION_UNKNOWN,
        "normal_min_percent": None,
        "normal_max_percent": None,
        "normal_transition_seconds": None,
        "text": "",
        "short_text": "",
    }
    if current_percent is None:
        return unavailable
    reset_at = _parse_reset_datetime(reset_at_value)
    window_seconds = _snapshot_window_seconds(metric_key)
    if reset_at is None or window_seconds is None:
        return unavailable
    current = _reset_now(reset_at, now)
    remaining_seconds = (reset_at - current).total_seconds()
    if remaining_seconds < 0.0 or remaining_seconds > float(window_seconds):
        return unavailable

    remaining_ratio = remaining_seconds / float(window_seconds)
    lower_bound = 100.0 * remaining_ratio
    upper_bound = (
        _SNAPSHOT_ON_TRACK_MAX_PROJECTED_REMAINING_PERCENT
        + (
            100.0 - _SNAPSHOT_ON_TRACK_MAX_PROJECTED_REMAINING_PERCENT
        )
        * remaining_ratio
    )
    normal_min_percent = max(
        0,
        min(100, int(math.ceil(lower_bound - 1e-9))),
    )
    normal_max_percent = max(
        normal_min_percent,
        min(100, int(math.floor(upper_bound + 1e-9))),
    )
    remaining_percent = max(0, min(100, int(current_percent)))
    normal_transition_seconds: int | None = None
    if remaining_percent < normal_min_percent:
        direction = _RESET_DIRECTION_SHORTAGE
        transition_seconds = remaining_seconds - (
            float(remaining_percent) / 100.0 * float(window_seconds)
        )
        normal_transition_seconds = max(0, int(math.ceil(transition_seconds)))
        transition_text = _format_guidance_duration(normal_transition_seconds)
    elif remaining_percent > normal_max_percent:
        direction = _RESET_DIRECTION_SURPLUS
    else:
        direction = _RESET_DIRECTION_ON_TRACK

    range_text = (
        f"{normal_min_percent}%"
        if normal_min_percent == normal_max_percent
        else f"{normal_min_percent}~{normal_max_percent}%"
    )
    if direction == _RESET_DIRECTION_SHORTAGE:
        text = f"N {range_text} / {transition_text}"
        short_text = text
    else:
        text = f"N {range_text}"
        short_text = text
    return {
        "direction": direction,
        "normal_min_percent": normal_min_percent,
        "normal_max_percent": normal_max_percent,
        "normal_transition_seconds": normal_transition_seconds,
        "text": text,
        "short_text": short_text,
    }


def _format_guidance_duration(seconds: int) -> str:
    total_minutes = max(1, int(math.ceil(max(0, int(seconds)) / 60.0)))
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    return f"{days:02d}d {hours:02d}h {minutes:02d}m"


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
    reset_precision: str = "",
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
    precision = str(reset_precision or "").strip().lower()
    if precision == "date":
        days = max(0, (parsed.date() - current.date()).days)
        text = f"{days:02d}d 00h 00m 00s"
        direction = _reset_action_direction(metric_key, percent, days * 86400)
        profile = _reset_direction_profile(direction)
        return {
            "text": text,
            "short_text": text,
            "state": profile["state"],
            "color": profile["color"],
            "direction": profile["direction"],
            "marker": profile["marker"],
        }
    seconds = int((parsed - current).total_seconds())
    if seconds <= 0:
        text = _format_reset_remaining_detail(
            0,
            metric_key=metric_key,
            reset_precision=precision,
        )
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
        "text": _format_reset_remaining_detail(
            seconds,
            metric_key=metric_key,
            reset_precision=precision,
        ),
        "short_text": _format_reset_remaining_compact(
            seconds,
            metric_key=metric_key,
            reset_precision=precision,
        ),
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


def _format_reset_remaining_detail(
    seconds: int,
    *,
    metric_key: str = "",
    reset_precision: str = "",
) -> str:
    _ = metric_key
    _ = reset_precision
    seconds = max(0, int(seconds))
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{days:02d}d {hours:02d}h {minutes:02d}m {secs:02d}s"


def _format_reset_remaining_compact(
    seconds: int,
    *,
    metric_key: str = "",
    reset_precision: str = "",
) -> str:
    return _format_reset_remaining_detail(
        seconds,
        metric_key=metric_key,
        reset_precision=reset_precision,
    )


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
        min(
            _METRIC_PROGRESS_TEXT_PRIORITY_MIN_WIDTH_PX,
            int(max_progress_width),
        ),
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
    # Display contract: the reset countdown outranks the reset badge. In short
    # mode the badge becomes optional so the time text wins when space is
    # tight; the metric identity ("5h"/"7d") is already drawn at the segment
    # start, so a hidden badge never loses the metric.
    force_visible_badge = normalized_badge_mode == "full"

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


def _metric_descriptors_have_data(metric_descriptors: Any) -> bool:
    if not isinstance(metric_descriptors, list):
        return False
    for descriptor in metric_descriptors:
        if not isinstance(descriptor, dict):
            continue
        if _descriptor_percent(descriptor) is not None:
            return True
        metric_state = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(descriptor.get("state") or "").strip().lower(),
        ).strip("_")
        if metric_state in {
            "unknown",
            "unavailable",
            "unsupported",
            "unsupported_contract",
            "logged_out",
            "login_required",
            "dom_drift",
            "timeout",
            "crash",
            "recycle",
            "error",
            "failed",
        }:
            continue
        value_text = str(descriptor.get("value_text") or "").strip()
        if value_text.lower() in {"", "-", "--", "n/a", "unavailable", "조회 불가"}:
            continue
        if value_text:
            return True
    return False


def _profile_status(
    enabled: bool,
    runtime: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    provider_status: Any,
    freshness: str,
    metric_descriptors: Any,
) -> dict[str, str]:
    base = _account_status(enabled, runtime, snapshot)
    if not bool(enabled):
        return base
    if base["state"] == "nodata" and _metric_descriptors_have_data(metric_descriptors):
        base = {"state": "ready", "text": "OK", "color": "#22c55e"}

    status_text_override = ""
    status_color_override = ""
    if isinstance(provider_status, dict):
        raw_state = (
            provider_status.get("state")
            or provider_status.get("status")
            or provider_status.get("key")
            or ""
        )
        status_text_override = str(provider_status.get("text") or "").strip()
        status_color_override = str(provider_status.get("color") or "").strip()
    else:
        raw_state = provider_status
    normalized = re.sub(r"[^a-z0-9]+", "_", str(raw_state or "").strip().lower()).strip("_")
    if normalized in {"", "idle", "unknown"} and base["state"] == "nodata":
        for descriptor in metric_descriptors if isinstance(metric_descriptors, list) else ():
            if not isinstance(descriptor, dict):
                continue
            metric_state = re.sub(
                r"[^a-z0-9]+",
                "_",
                str(descriptor.get("state") or "").strip().lower(),
            ).strip("_")
            if metric_state in {
                "unavailable",
                "unsupported",
                "unsupported_contract",
                "logged_out",
                "login_required",
                "dom_drift",
                "timeout",
                "stale",
                "crash",
                "recycle",
                "error",
                "failed",
            }:
                normalized = metric_state
                break

    status_by_state = {
        "ready": {"state": "ready", "text": "OK", "color": "#22c55e"},
        "ok": {"state": "ready", "text": "OK", "color": "#22c55e"},
        "success": {"state": "ready", "text": "OK", "color": "#22c55e"},
        "running": {"state": "sync", "text": "SYNC", "color": "#38bdf8"},
        "collecting": {"state": "sync", "text": "SYNC", "color": "#38bdf8"},
        "sync": {"state": "sync", "text": "SYNC", "color": "#38bdf8"},
        "cancelling": {"state": "sync", "text": "SYNC", "color": "#38bdf8"},
        "retrying": {"state": "retrying", "text": "RETRY", "color": "#f59e0b"},
        "login": {"state": "login", "text": "OUT", "color": "#f59e0b"},
        "logged_out": {"state": "login", "text": "OUT", "color": "#f59e0b"},
        "login_required": {"state": "login", "text": "OUT", "color": "#f59e0b"},
        "not_authenticated": {"state": "login", "text": "OUT", "color": "#f59e0b"},
        "auth_required": {"state": "login", "text": "OUT", "color": "#f59e0b"},
        "unauthorized": {"state": "login", "text": "OUT", "color": "#f59e0b"},
        "paused_auth_required": {"state": "login", "text": "OUT", "color": "#f59e0b"},
        "paused": {"state": "profile_busy", "text": "WAIT", "color": "#f59e0b"},
        "profile_busy": {"state": "profile_busy", "text": "WAIT", "color": "#f59e0b"},
        "paused_profile_in_use": {"state": "profile_busy", "text": "WAIT", "color": "#f59e0b"},
        "rate_limited": {"state": "rate_limited", "text": "RATE", "color": "#f59e0b"},
        "rate_limit": {"state": "rate_limited", "text": "RATE", "color": "#f59e0b"},
        "stale": {"state": "stale", "text": "OLD", "color": "#f59e0b"},
        "cache_stale": {"state": "stale", "text": "OLD", "color": "#f59e0b"},
        "expired_cache": {"state": "stale", "text": "OLD", "color": "#f59e0b"},
        "timeout": {"state": "timeout", "text": "TIME", "color": "#f59e0b"},
        "command_timeout": {"state": "timeout", "text": "TIME", "color": "#f59e0b"},
        "navigation_timeout": {"state": "timeout", "text": "TIME", "color": "#f59e0b"},
        "unsupported": {"state": "unsupported", "text": "N/A", "color": "#94a3b8"},
        "unavailable": {"state": "unsupported", "text": "N/A", "color": "#94a3b8"},
        "unsupported_contract": {"state": "unsupported", "text": "N/A", "color": "#94a3b8"},
        "error": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "failed": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "dom_drift": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "parse_failed": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "schema_incompatible": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "crash": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "renderer_crashed": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "transport_closed": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "recycle": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "worker_recycle": {"state": "error", "text": "ERR", "color": "#f59e0b"},
        "page_recycling": {"state": "error", "text": "ERR", "color": "#f59e0b"},
    }
    if normalized == "idle":
        status = dict(base)
    else:
        status = dict(status_by_state.get(normalized, base))
    if str(freshness or "").strip().lower() == "stale" and status["state"] == "ready":
        status = dict(status_by_state["stale"])
    if status_text_override:
        status["text"] = status_text_override
    if status_color_override:
        status["color"] = status_color_override
    return status


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
    browser_last_error = str(runtime.get("browser_last_error") or "").strip()
    if browser_last_error == "command_timeout":
        return {"state": "timeout", "text": "TIME", "color": "#f59e0b"}
    if collect_inflight and not has_metric:
        return {"state": "sync", "text": "SYNC", "color": "#38bdf8"}
    if monitor_state == "paused_profile_in_use":
        return {"state": "profile_busy", "text": "WAIT", "color": "#f59e0b"}
    if monitor_state == "paused_auth_required" or session_state == "logged_out":
        return {"state": "login", "text": "OUT", "color": "#f59e0b"}
    failure_count = int(runtime.get("failure_count") or 0)
    if failure_count > 0 or browser_last_error:
        return {"state": "error", "text": "ERR", "color": "#f59e0b"}
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


AiUsageTaskbarOverlay = CodexUsageTaskbarOverlay
build_ai_usage_taskbar_overlay_model = build_codex_usage_taskbar_overlay_model
