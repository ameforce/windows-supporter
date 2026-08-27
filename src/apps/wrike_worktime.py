"""Work-time computation helpers for the Wrike timelog summary.

All datetimes handled here are naive local wall-clock values.  This module is
UI-free and dependency-free so it can be unit tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


COLOR_TEXT = "#111111"
COLOR_ACCENT = "#2563EB"
COLOR_OK = "#10B981"
COLOR_WARN = "#DC2626"
COLOR_MUTED = "#6B7280"

DEFAULT_LUNCH_START_MIN = 12 * 60
DEFAULT_LUNCH_END_MIN = 13 * 60


@dataclass(frozen=True)
class BreakInterval:
    """One pause during the workday; ``end=None`` means still ongoing."""

    start: datetime
    end: datetime | None
    label: str = ""

    def resolved_end(self, now: datetime) -> datetime:
        if self.end is not None:
            return self.end
        return max(self.start, now)


def overlap_minutes(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> int:
    latest_start = max(start_a, start_b)
    earliest_end = min(end_a, end_b)
    delta = (earliest_end - latest_start).total_seconds()
    if delta <= 0:
        return 0
    return int(delta // 60)


def total_break_minutes_within(
    intervals: list[BreakInterval],
    clock_in: datetime | None,
    until: datetime,
    now: datetime | None = None,
) -> int:
    resolved_now = now if now is not None else until
    if clock_in is None:
        return 0
    total = 0
    for item in intervals:
        try:
            total += overlap_minutes(item.start, item.resolved_end(resolved_now), clock_in, until)
        except Exception:
            continue
    return int(total)


def merge_intervals(intervals: list[BreakInterval], now: datetime) -> list[tuple[datetime, datetime]]:
    spans: list[tuple[datetime, datetime]] = []
    for item in intervals:
        try:
            start = item.start
            end = item.resolved_end(now)
        except Exception:
            continue
        if end <= start:
            continue
        spans.append((start, end))
    spans.sort(key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def compute_net_elapsed_minutes(
    now: datetime,
    clock_in: datetime | None,
    intervals: list[BreakInterval],
) -> int:
    if clock_in is None:
        return 0
    gross = (now - clock_in).total_seconds() // 60
    if gross < 0:
        return 0
    breaks = total_break_minutes_within(intervals, clock_in, now, now)
    return int(max(0, int(gross) - breaks))


def project_quit_at(
    now: datetime,
    clock_in: datetime | None,
    target_minutes: int,
    intervals: list[BreakInterval],
) -> datetime | None:
    """Predicted quit time assuming accrual pauses only during planned breaks.

    Breaks with unknown end time (running manual breaks) are excluded so the
    prediction reflects resume-known plans; callers show "-" while a manual
    pause is running because the resume moment is unknown.
    """
    if clock_in is None or int(target_minutes) <= 0:
        return None
    fixed = [item for item in intervals if item.end is not None]
    candidate = clock_in + timedelta(minutes=int(target_minutes))
    for _ in range(8):
        added = total_break_minutes_within(fixed, clock_in, candidate)
        nxt = clock_in + timedelta(minutes=int(target_minutes) + added)
        if abs((nxt - candidate).total_seconds()) < 30.0:
            candidate = nxt
            break
        candidate = nxt
    for _ in range(6):
        moved = False
        for start, end in merge_intervals(fixed, now):
            if start <= candidate < end:
                candidate = end
                moved = True
        if not moved:
            break
    return candidate


def format_hhmm(value: datetime | None) -> str:
    if value is None:
        return "-"
    try:
        return value.strftime("%H:%M")
    except Exception:
        return "-"


def format_minutes(minutes: int) -> str:
    minutes = int(minutes)
    if minutes <= 0:
        return "0분"
    hours = minutes // 60
    remain = minutes % 60
    if hours and remain:
        return f"{hours}시간 {remain}분"
    if hours:
        return f"{hours}시간"
    return f"{remain}분"


def parse_iso_datetime(raw) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    try:
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
    except Exception:
        pass
    return parsed


def clock_in_candidate(item: dict) -> datetime | None:
    """Best arrival-time hint from one raw Wrike timelog entry.

    A non-midnight ``trackedDate`` carries the tracked moment and wins;
    otherwise ``createdDate`` (entry creation) is the fallback signal.
    """
    informative = None
    created = None
    try:
        tracked = parse_iso_datetime(item.get("trackedDate") or item.get("date"))
        if tracked is not None and (tracked.hour or tracked.minute or tracked.second):
            informative = tracked
    except Exception:
        informative = None
    try:
        created = parse_iso_datetime(item.get("createdDate"))
    except Exception:
        created = None
    candidates = [value for value in (informative, created) if value is not None]
    if not candidates:
        return None
    return min(candidates)


def earliest_clock_in_from_items(items) -> datetime | None:
    best: datetime | None = None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        candidate = clock_in_candidate(item)
        if candidate is None:
            continue
        if best is None or candidate < best:
            best = candidate
    return best


def build_lunch_interval(now: datetime, enabled: bool, start_min: int, end_min: int) -> BreakInterval | None:
    if not bool(enabled):
        return None
    try:
        start_min = int(start_min)
        end_min = int(end_min)
    except Exception:
        start_min = DEFAULT_LUNCH_START_MIN
        end_min = DEFAULT_LUNCH_END_MIN
    if start_min < 0:
        start_min = 0
    if end_min <= start_min:
        return None
    day_start = datetime.combine(now.date(), datetime.min.time())
    return BreakInterval(
        start=day_start + timedelta(minutes=start_min),
        end=day_start + timedelta(minutes=end_min),
        label="점심",
    )


@dataclass(frozen=True)
class WorkdayOverview:
    clock_in: datetime | None
    break_total_minutes: int
    break_labels: str
    net_elapsed_minutes: int
    projected_quit: datetime | None
    recorded_minutes: int
    remaining_recorded_minutes: int
    manual_break_active: bool

    def as_lines(self, now: datetime) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        if self.clock_in is not None:
            head = (
                f"출근 {format_hhmm(self.clock_in)} · 실근무 경과 "
                f"{format_minutes(self.net_elapsed_minutes)}"
            )
        else:
            head = "출근 - · 실근무 경과 -"
        lines.append((head, COLOR_ACCENT))

        breakdown = self.break_labels if self.break_labels else "없음"
        active_note = " · 휴게 진행 중" if self.manual_break_active else ""
        lines.append((
            f"휴게 {format_minutes(self.break_total_minutes)} ({breakdown}){active_note}",
            COLOR_MUTED,
        ))

        quit_text = format_hhmm(self.projected_quit)
        lines.append((f"예상 퇴근 {quit_text}", COLOR_ACCENT))

        remain = int(self.remaining_recorded_minutes)
        basis = f" ({format_hhmm(now)} 기준)"
        if remain > 0:
            lines.append((f"잔여 부족 {format_minutes(remain)}{basis}", COLOR_WARN))
        elif remain == 0:
            lines.append(("목표 달성", COLOR_OK))
        else:
            lines.append((f"초과 {format_minutes(-remain)}{basis}", COLOR_OK))
        return lines


def build_workday_overview(
    *,
    now: datetime,
    clock_in: datetime | None,
    recorded_minutes: int,
    target_minutes: int,
    intervals: list[BreakInterval],
) -> WorkdayOverview:
    break_total = total_break_minutes_within(intervals, clock_in, now, now)
    net = compute_net_elapsed_minutes(now, clock_in, intervals)

    labels: list[str] = []
    active = False
    spans_by_label: dict[str, int] = {}
    ordered = sorted(
        [item for item in intervals if isinstance(item, BreakInterval)],
        key=lambda iv: iv.start,
    )
    for item in ordered:
        if item.end is None:
            active = True
            span = total_break_minutes_within([item], item.start, now, now)
            label_text = f"{item.label or '휴게'} 진행 중"
        else:
            span = overlap_minutes(item.start, item.end, item.start, item.end)
            label_text = item.label or "휴게"
        if span <= 0:
            continue
        spans_by_label[label_text] = spans_by_label.get(label_text, 0) + span
    for key, value in spans_by_label.items():
        labels.append(f"{key} {format_minutes(value)}")

    remaining = int(target_minutes) - int(recorded_minutes)
    projected = project_quit_at(now, clock_in, target_minutes, intervals)
    return WorkdayOverview(
        clock_in=clock_in,
        break_total_minutes=int(break_total),
        break_labels=" + ".join(labels),
        net_elapsed_minutes=net,
        projected_quit=projected,
        recorded_minutes=int(recorded_minutes),
        remaining_recorded_minutes=remaining,
        manual_break_active=active,
    )


class RefreshableLines(list):
    """Tooltip rows re-rendered by ToolTip's refresh provider contract."""

    def __init__(self, rows, refresh) -> None:
        super().__init__(rows)
        self._refresh_provider = refresh

    def refresh(self):
        try:
            refreshed = list(self._refresh_provider() or [])
        except Exception:
            refreshed = list(self)
        if len(refreshed) != len(self):
            return list(self)
        return refreshed


