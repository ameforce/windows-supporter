from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class TaskbarMonitorSnapshot:
    handle: int
    device: str
    display_num: int | None
    is_primary: bool
    monitor: Rect
    work: Rect


@dataclass(frozen=True)
class TaskbarWindowSnapshot:
    hwnd: int
    class_name: str
    rect: Rect
    visible: bool


@dataclass(frozen=True)
class TaskbarOverlayTarget:
    monitor: TaskbarMonitorSnapshot
    taskbar_hwnd: int
    taskbar_class: str
    taskbar_rect: Rect | None
    orientation: str
    displayable: bool

    @property
    def is_primary(self) -> bool:
        return bool(self.monitor.is_primary)


def parse_display_num(device: Any) -> int | None:
    match = re.search(r"DISPLAY(\d+)\s*$", str(device or ""), flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def normalize_rect(value: Any) -> Rect | None:
    try:
        left, top, right, bottom = tuple(value)[:4]
        rect = int(left), int(top), int(right), int(bottom)
    except Exception:
        return None
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        return None
    return rect


def sorted_monitors(
    monitors: list[TaskbarMonitorSnapshot] | tuple[TaskbarMonitorSnapshot, ...],
) -> tuple[TaskbarMonitorSnapshot, ...]:
    return tuple(
        sorted(
            monitors,
            key=lambda item: (
                item.display_num if item.display_num is not None else 9999,
                item.work[0],
            ),
        )
    )


def monitor_size(monitor: TaskbarMonitorSnapshot) -> tuple[int, int]:
    left, top, right, bottom = monitor.monitor
    return max(1, right - left), max(1, bottom - top)


def local_work_area(monitor: TaskbarMonitorSnapshot) -> Rect:
    m_left, m_top, m_right, m_bottom = monitor.monitor
    w_left, w_top, w_right, w_bottom = monitor.work
    return (
        max(0, min(m_right - m_left, w_left - m_left)),
        max(0, min(m_bottom - m_top, w_top - m_top)),
        max(0, min(m_right - m_left, w_right - m_left)),
        max(0, min(m_bottom - m_top, w_bottom - m_top)),
    )


def taskbar_orientation(monitor: TaskbarMonitorSnapshot) -> str:
    m_left, m_top, m_right, m_bottom = monitor.monitor
    w_left, w_top, w_right, w_bottom = monitor.work
    if w_bottom < m_bottom:
        return "bottom"
    if w_top > m_top:
        return "top"
    if w_left > m_left:
        return "left"
    if w_right < m_right:
        return "right"
    return ""


def globalize_geometry(
    geometry: dict[str, int | str],
    monitor: TaskbarMonitorSnapshot,
) -> dict[str, int | str]:
    output = dict(geometry)
    output["x"] = int(output.get("x", 0) or 0) + int(monitor.monitor[0])
    output["y"] = int(output.get("y", 0) or 0) + int(monitor.monitor[1])
    return output


def target_cache_key(target: TaskbarOverlayTarget) -> tuple[Any, ...]:
    monitor = target.monitor
    return (
        int(monitor.handle),
        str(monitor.device),
        bool(monitor.is_primary),
        tuple(monitor.monitor),
        tuple(monitor.work),
        int(target.taskbar_hwnd),
        str(target.orientation),
        bool(target.displayable),
    )


def build_taskbar_overlay_targets(
    monitors: list[TaskbarMonitorSnapshot] | tuple[TaskbarMonitorSnapshot, ...],
    taskbar_windows: list[TaskbarWindowSnapshot] | tuple[TaskbarWindowSnapshot, ...],
) -> tuple[TaskbarOverlayTarget, ...]:
    windows = tuple(taskbar_windows)
    targets = []
    for monitor in sorted_monitors(tuple(monitors)):
        taskbar = _best_taskbar_window_for_monitor(monitor, windows)
        orientation = taskbar_orientation(monitor)
        displayable = bool(
            taskbar is not None
            and taskbar.visible
            and orientation in {"bottom", "top"}
        )
        targets.append(
            TaskbarOverlayTarget(
                monitor=monitor,
                taskbar_hwnd=int(taskbar.hwnd) if taskbar is not None else 0,
                taskbar_class=str(taskbar.class_name) if taskbar is not None else "",
                taskbar_rect=taskbar.rect if taskbar is not None else None,
                orientation=orientation,
                displayable=displayable,
            )
        )
    return tuple(targets)


def _best_taskbar_window_for_monitor(
    monitor: TaskbarMonitorSnapshot,
    windows: tuple[TaskbarWindowSnapshot, ...],
) -> TaskbarWindowSnapshot | None:
    wanted_class = "Shell_TrayWnd" if monitor.is_primary else "Shell_SecondaryTrayWnd"
    candidates = [
        window
        for window in windows
        if window.class_name == wanted_class and _rect_overlap_area(window.rect, monitor.monitor) > 0
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda window: (
            bool(window.visible),
            _rect_overlap_area(window.rect, monitor.monitor),
        ),
    )


def _rect_overlap_area(left: Rect, right: Rect) -> int:
    x_overlap = max(0, min(left[2], right[2]) - max(left[0], right[0]))
    y_overlap = max(0, min(left[3], right[3]) - max(left[1], right[1]))
    return int(x_overlap * y_overlap)
