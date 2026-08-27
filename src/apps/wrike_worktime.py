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


def union_datetime_intervals(intervals) -> list[tuple[datetime, datetime]]:
    """Return sorted, non-overlapping spans, merging adjacent boundaries."""
    spans: list[tuple[datetime, datetime]] = []
    for item in intervals or []:
        try:
            if isinstance(item, BreakInterval):
                start, end = item.start, item.end
            else:
                start, end = item
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                continue
            if end <= start:
                continue
            spans.append((start, end))
        except Exception:
            continue
    spans.sort(key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


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
    for start, end in merge_intervals(intervals, resolved_now):
        try:
            total += overlap_minutes(start, end, clock_in, until)
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
    return union_datetime_intervals(spans)


def vacation_credit_minutes(
    target_minutes: int,
    all_day: bool | dict = False,
    intervals=None,
) -> int:
    """Return vacation credit, capped at the day's target net minutes.

    ``all_day`` may also be the result dictionary returned by
    ``vacation_events_for_day``. Timed spans are unioned before their duration
    is calculated so overlapping or adjacent calendar events are credited once.
    """
    try:
        target = max(0, int(target_minutes))
    except Exception:
        return 0
    if isinstance(all_day, dict):
        vacation = all_day
        all_day = bool(vacation.get("all_day"))
        if intervals is None:
            intervals = vacation.get("intervals")
    if bool(all_day):
        return target
    total_seconds = sum(
        max(0.0, (end - start).total_seconds())
        for start, end in union_datetime_intervals(intervals or [])
    )
    return min(target, int(total_seconds // 60))


# Descriptive aliases retained for callers that prefer an explicit verb.
compute_vacation_credit_minutes = vacation_credit_minutes
calculate_vacation_credit_minutes = vacation_credit_minutes
merge_datetime_intervals = union_datetime_intervals


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
    """Predict quit time while all known fixed breaks pause work accrual.

    A currently active break has no known resume moment, so no prediction is
    returned until that break is closed.
    """
    try:
        target = int(target_minutes)
    except Exception:
        return None
    if clock_in is None or target <= 0:
        return None
    fixed: list[BreakInterval] = []
    for item in intervals or []:
        try:
            if item.end is None:
                if item.start <= now:
                    return None
                continue
            fixed.append(item)
        except Exception:
            continue
    candidate = clock_in + timedelta(minutes=target)
    for _ in range(8):
        added = total_break_minutes_within(fixed, clock_in, candidate)
        nxt = clock_in + timedelta(minutes=target + added)
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


def _merged_break_labels(
    intervals: list[BreakInterval],
    clock_in: datetime | None,
    now: datetime,
) -> tuple[str, bool]:
    active = False
    spans: list[tuple[datetime, datetime, str]] = []
    for item in intervals or []:
        if not isinstance(item, BreakInterval):
            continue
        try:
            if item.end is None and item.start <= now:
                active = True
            if clock_in is None:
                continue
            start = max(item.start, clock_in)
            end = min(item.resolved_end(now), now)
        except Exception:
            continue
        if end <= start:
            continue
        spans.append((start, end, item.label or "휴게"))
    spans.sort(key=lambda value: value[0])

    merged: list[dict] = []
    for start, end, label in spans:
        if merged and start <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], end)
            if label not in merged[-1]["labels"]:
                merged[-1]["labels"].append(label)
        else:
            merged.append({"start": start, "end": end, "labels": [label]})

    labels: list[str] = []
    for item in merged:
        span = int(max(0.0, (item["end"] - item["start"]).total_seconds()) // 60)
        if span <= 0:
            continue
        label = "/".join(item["labels"])
        labels.append(f"{label} {format_minutes(span)}")
    return " + ".join(labels), active


@dataclass(frozen=True)
class WorkdayOverview:
    clock_in: datetime | None
    break_total_minutes: int
    break_labels: str
    net_elapsed_minutes: int
    projected_quit: datetime | None
    remaining_net_minutes: int
    manual_break_active: bool
    target_minutes: int = 0
    vacation_minutes: int = 0
    effective_target_minutes: int = 0

    def as_lines(self, now: datetime) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        if self.clock_in is not None:
            head = (
                f"출근 {format_hhmm(self.clock_in)} · 실시간 순근무 "
                f"{format_minutes(self.net_elapsed_minutes)}"
            )
        else:
            head = "출근 - · 실시간 순근무 -"
        lines.append((head, COLOR_ACCENT))

        breakdown = self.break_labels if self.break_labels else "없음"
        active_note = " · 휴게 진행 중" if self.manual_break_active else ""
        lines.append((
            f"병합 휴게 {format_minutes(self.break_total_minutes)} ({breakdown}){active_note}",
            COLOR_MUTED,
        ))

        lines.append((
            f"휴가 차감 {format_minutes(self.vacation_minutes)} · "
            f"적용 목표 {format_minutes(self.effective_target_minutes)}",
            COLOR_MUTED,
        ))

        quit_text = format_hhmm(self.projected_quit)
        lines.append((f"예상 퇴근 {quit_text}", COLOR_ACCENT))

        remain = int(self.remaining_net_minutes)
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
    target_minutes: int,
    intervals: list[BreakInterval],
    vacation_minutes: int = 0,
    recorded_minutes: int | None = None,
) -> WorkdayOverview:
    # Compatibility only: live calculations deliberately ignore Wrike-recorded
    # time and derive remaining work directly from elapsed net work.
    _ = recorded_minutes
    try:
        target = max(0, int(target_minutes))
    except Exception:
        target = 0
    try:
        vacation = max(0, int(vacation_minutes))
    except Exception:
        vacation = 0
    effective_target = max(0, target - vacation)

    break_total = total_break_minutes_within(intervals, clock_in, now, now)
    net = compute_net_elapsed_minutes(now, clock_in, intervals)
    break_labels, active = _merged_break_labels(intervals, clock_in, now)
    remaining = effective_target - net
    projected = project_quit_at(now, clock_in, effective_target, intervals)
    if active:
        projected = None
    return WorkdayOverview(
        clock_in=clock_in,
        break_total_minutes=int(break_total),
        break_labels=break_labels,
        net_elapsed_minutes=net,
        projected_quit=projected,
        remaining_net_minutes=remaining,
        manual_break_active=active,
        target_minutes=target,
        vacation_minutes=vacation,
        effective_target_minutes=effective_target,
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


