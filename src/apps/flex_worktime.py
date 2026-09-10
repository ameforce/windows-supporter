"""Read-only Flex worktime synchronization through the employee's browser session.

The module deliberately keeps browser/session handling and schedule parsing
independent from the Tk application.  It never attempts to create or modify a
Flex work record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import os
import re
import time
from typing import Any
from zoneinfo import ZoneInfo


# The work-record route preserves the destination through Flex login when the
# browser session is not authenticated.
FLEX_WEB_URL = "https://flex.team/time-tracking/work-record/my"
FLEX_BROWSER_PROFILE_DIR_NAME = "flex-profile"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BREAK_WORDS = ("휴게", "break", "rest")
_OVERTIME_WORDS = (
    "연장",
    "초과",
    "overtime",
    "extra",
    "야간",
    "night",
    "휴일",
    "holiday",
)
_CLOCK_TYPES = frozenset({"WORK_CLOCK", "CLOCK", "WORKCLOCK"})
_RECORD_TYPES = frozenset({"WORK_RECORD", "RECORD", "SCHEDULE"})


class FlexScheduleError(RuntimeError):
    """A safe failure while parsing a browser-captured Flex schedule."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "schedule_error",
    ) -> None:
        super().__init__(str(message))
        self.code = str(code or "schedule_error")


class FlexBrowserError(RuntimeError):
    """A safe, user-facing failure from the interactive Flex browser session."""

    def __init__(self, message: str, *, code: str = "browser_error") -> None:
        super().__init__(str(message))
        self.code = str(code or "browser_error")


@dataclass(frozen=True, slots=True)
class FlexBrowserSyncResult:
    """A browser sync result plus the employee number found in Flex."""

    schedules: dict[date, "FlexDaySchedule"]
    employee_number: str = ""


@dataclass(frozen=True, slots=True)
class FlexWorkBlock:
    """One planned or actual work/rest block returned by Flex."""

    form_name: str
    start: datetime
    end: datetime | None
    source_type: str

    @property
    def is_break(self) -> bool:
        value = f"{self.form_name} {self.source_type}".casefold()
        return any(word.casefold() in value for word in _BREAK_WORDS)

    @property
    def is_overtime(self) -> bool:
        value = f"{self.form_name} {self.source_type}".casefold()
        return any(word.casefold() in value for word in _OVERTIME_WORDS)

    @property
    def is_clock(self) -> bool:
        return self.source_type in _CLOCK_TYPES

    @property
    def is_record(self) -> bool:
        return self.source_type in _RECORD_TYPES


@dataclass(frozen=True, slots=True)
class FlexDaySchedule:
    """Normalized work schedule for one local calendar date."""

    date: date
    blocks: tuple[FlexWorkBlock, ...]
    break_intervals: tuple[tuple[datetime, datetime], ...]
    scheduled_start: datetime | None
    regular_quit: datetime | None
    scheduled_quit: datetime | None
    actual_start: datetime | None
    actual_quit: datetime | None
    target_minutes: int
    overtime_assigned_minutes: int
    overtime_scheduled_quit: datetime | None
    fetched_at: datetime

    @property
    def has_data(self) -> bool:
        return bool(self.blocks)


def _parse_datetime(value: Any) -> datetime | None:
    has_explicit_zone = False
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, dict):
        raw_timestamp = _first_value(
            value,
            "timestamp",
            "epochMillis",
            "epochMilliseconds",
            "milliseconds",
        )
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError):
            return None
        if abs(timestamp) >= 100_000_000_000:
            timestamp /= 1000.0
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            zone_name = str(
                _first_value(value, "zoneId", "timezone", "timeZone") or ""
            ).strip()
            if zone_name:
                has_explicit_zone = True
                try:
                    parsed = parsed.astimezone(ZoneInfo(zone_name))
                except Exception:
                    parsed = parsed.astimezone()
            else:
                parsed = parsed.astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except Exception:
            return None
    else:
        return None
    if has_explicit_zone and parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if len(raw) >= 10:
        raw = raw[:10]
    if not _DATE_RE.fullmatch(raw):
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None
    return parsed if parsed.isoformat() == raw else None


def _first_value(source: dict, *names: str) -> Any:
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return []


_EMPLOYEE_NUMBER_KEYS = (
    "employeeNumber",
    "employeeNo",
    "employeeCode",
    "personnelNumber",
    "personnelNo",
    "staffNumber",
    "staffNo",
    "memberNumber",
    "userNumber",
    "employee_number",
    "employee_no",
    "employee_code",
    "personnel_number",
    "personnel_no",
    "staff_number",
    "staff_no",
    "member_number",
    "user_number",
    "사번",
    "사원번호",
    "직원번호",
)
_EMPLOYEE_ID_KEYS = ("employeeId", "employee_id")


def _normalize_employee_number(value: Any) -> str:
    raw = str(value or "").strip().strip(".,;:)]}")
    if not raw or len(raw) > 120:
        return ""
    if "@" in raw or raw.casefold().startswith(("http://", "https://")):
        return ""
    return raw


def _employee_value(value: dict, *, include_id: bool = True) -> str:
    if not isinstance(value, dict):
        return ""
    candidate = _first_value(value, *_EMPLOYEE_NUMBER_KEYS)
    if candidate is None:
        for nested_name in (
            "employee",
            "employeeInfo",
            "staff",
            "person",
            "user",
            "member",
            "profile",
        ):
            nested = value.get(nested_name)
            if isinstance(nested, dict):
                nested_value = _employee_value(nested, include_id=include_id)
                if nested_value:
                    return nested_value
    if candidate is None and include_id:
        candidate = _first_value(value, *_EMPLOYEE_ID_KEYS)
    return _normalize_employee_number(candidate)


def _employee_number_from_mapping(value: dict) -> str:
    """Extract an employee-number field without treating a generic user id as one."""

    direct = _employee_value(value, include_id=False)
    if direct:
        return direct
    candidate = _normalize_employee_number(
        _first_value(value, *_EMPLOYEE_ID_KEYS)
    )
    if not candidate or re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f-]{27,}", candidate, re.IGNORECASE
    ):
        return ""
    return candidate


def _day_entries(user_schedule: dict) -> list[tuple[date | None, dict]]:
    raw_days = _first_value(
        user_schedule,
        "days",
        "workDays",
        "workSchedules",
        "schedules",
    )
    if isinstance(raw_days, dict):
        result = []
        for key, item in raw_days.items():
            if isinstance(item, dict):
                result.append((_parse_day(key), item))
        return result
    if isinstance(raw_days, list):
        result = []
        for item in raw_days:
            if isinstance(item, dict):
                result.append(
                    (
                        _parse_day(
                            _first_value(item, "date", "workDate", "day", "workDay")
                        ),
                        item,
                    )
                )
        return result
    day = _parse_day(
        _first_value(
            user_schedule,
            "date",
            "workDate",
            "day",
            "workDay",
        )
    )
    return [(day, user_schedule)] if day is not None else []


def _block_entries(day_entry: dict) -> list[dict]:
    raw_blocks = _first_value(
        day_entry,
        "workBlocks",
        "blocks",
        "workBlock",
        "timeBlocks",
    )
    if isinstance(raw_blocks, dict):
        nested = _first_value(raw_blocks, "workBlocks", "blocks", "items")
        raw_blocks = nested if nested is not None else list(raw_blocks.values())
    return [item for item in _as_list(raw_blocks) if isinstance(item, dict)]


def _normalize_source_type(raw: dict) -> str:
    value = _first_value(raw, "type", "blockType", "sourceType", "workType")
    return str(value or "").strip().upper().replace("-", "_")


def _normalize_block(raw: dict) -> FlexWorkBlock | None:
    nested = raw.get("block")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update({key: value for key, value in raw.items() if key != "block"})
        raw = merged
    start = _parse_datetime(
        _first_value(
            raw,
            "blockFrom",
            "from",
            "start",
            "startAt",
            "workFrom",
            "beginAt",
        )
    )
    if start is None:
        return None
    end = _parse_datetime(
        _first_value(
            raw,
            "blockTo",
            "to",
            "end",
            "endAt",
            "workTo",
            "finishAt",
        )
    )
    source_type = _normalize_source_type(raw)
    form_name = str(
        _first_value(
            raw,
            "formName",
            "workFormName",
            "name",
            "label",
            "title",
            "workTypeName",
        )
        or ""
    ).strip()
    if not form_name:
        form_name = source_type or "근무"
    return FlexWorkBlock(form_name, start, end, source_type)


def _duration_minutes(start: datetime, end: datetime | None, now: datetime) -> int:
    resolved_end = end if end is not None else now
    if resolved_end <= start:
        return 0
    return max(0, int((resolved_end - start).total_seconds() // 60))


def _overlap_minutes(
    start: datetime,
    end: datetime | None,
    intervals: tuple[tuple[datetime, datetime], ...],
    now: datetime,
) -> int:
    resolved_end = end if end is not None else now
    total = 0
    for break_start, break_end in intervals:
        left = max(start, break_start)
        right = min(resolved_end, break_end)
        if right > left:
            total += int((right - left).total_seconds() // 60)
    return total


def _choose_planned_blocks(blocks: tuple[FlexWorkBlock, ...]) -> tuple[FlexWorkBlock, ...]:
    records = tuple(block for block in blocks if block.is_record)
    if records:
        return records
    non_clock = tuple(block for block in blocks if not block.is_clock)
    return non_clock or blocks


def _normalize_user_schedules(payload: dict) -> list[dict]:
    raw = payload.get("userWorkSchedules")
    if raw is None:
        raw = payload.get("userSchedules")
    if raw is None:
        raw = payload.get("data")
    if isinstance(raw, dict):
        if any(key in raw for key in ("days", "workDays", "workBlocks", "blocks")):
            return [raw]
        return [item for item in raw.values() if isinstance(item, dict)]
    return [item for item in _as_list(raw) if isinstance(item, dict)]


def parse_flex_schedule_response(
    payload: dict,
    *,
    employee_number: str = "",
    now: datetime | None = None,
) -> dict[date, FlexDaySchedule]:
    """Parse a browser-captured schedule response into local-day records.

    The parser accepts the minor envelope variations seen between Flex API
    versions while keeping the normalized model strict and deterministic.
    """

    if not isinstance(payload, dict):
        raise FlexScheduleError("Flex 근무 일정 응답 형식이 올바르지 않습니다.", code="invalid_response")
    current = now if now is not None else datetime.now()
    if current.tzinfo is not None:
        current = current.astimezone().replace(tzinfo=None)
    wanted_employee = str(employee_number or "").strip()
    result: dict[date, FlexDaySchedule] = {}
    for user_schedule in _normalize_user_schedules(payload):
        actual_employee = _employee_value(user_schedule)
        if wanted_employee and actual_employee and actual_employee != wanted_employee:
            continue
        for hinted_day, day_entry in _day_entries(user_schedule):
            raw_blocks = _block_entries(day_entry)
            blocks = tuple(
                block
                for block in (_normalize_block(item) for item in raw_blocks)
                if block is not None
            )
            if not blocks:
                continue
            target_day = hinted_day or min(block.start.date() for block in blocks)
            if target_day is None:
                continue
            day_start = datetime.combine(target_day, datetime.min.time())
            day_end = day_start + timedelta(days=1)
            day_blocks = tuple(
                block
                for block in blocks
                if block.start < day_end
                and (block.end is None or block.end > day_start)
            )
            if not day_blocks:
                continue
            break_blocks = tuple(block for block in day_blocks if block.is_break)
            break_intervals = tuple(
                (
                    max(day_start, block.start),
                    min(day_end, block.end if block.end is not None else current),
                )
                for block in break_blocks
                if min(day_end, block.end if block.end is not None else current)
                > max(day_start, block.start)
            )
            planned = _choose_planned_blocks(day_blocks)
            planned_work = tuple(block for block in planned if not block.is_break)
            scheduled_start = min(
                (block.start for block in planned_work),
                default=None,
            )
            regular_blocks = tuple(block for block in planned_work if not block.is_overtime)
            overtime_blocks = tuple(block for block in planned_work if block.is_overtime)
            regular_quit = max(
                (block.end for block in regular_blocks if block.end is not None),
                default=None,
            )
            scheduled_quit = max(
                (block.end for block in planned_work if block.end is not None),
                default=None,
            )
            actual_blocks = tuple(block for block in day_blocks if block.is_clock)
            actual_work = tuple(block for block in actual_blocks if not block.is_break)
            actual_start = min((block.start for block in actual_work), default=None)
            actual_quit = max(
                (block.end for block in actual_work if block.end is not None),
                default=None,
            )
            target_minutes = 0
            for block in planned_work:
                target_minutes += max(
                    0,
                    _duration_minutes(block.start, block.end, current)
                    - _overlap_minutes(
                        block.start,
                        block.end,
                        break_intervals,
                        current,
                    ),
                )
            overtime_minutes = 0
            for block in overtime_blocks:
                overtime_minutes += max(
                    0,
                    _duration_minutes(block.start, block.end, current)
                    - _overlap_minutes(
                        block.start,
                        block.end,
                        break_intervals,
                        current,
                    ),
                )
            overtime_quit = max(
                (block.end for block in overtime_blocks if block.end is not None),
                default=None,
            )
            result[target_day] = FlexDaySchedule(
                date=target_day,
                blocks=day_blocks,
                break_intervals=break_intervals,
                scheduled_start=scheduled_start,
                regular_quit=regular_quit,
                scheduled_quit=scheduled_quit,
                actual_start=actual_start,
                actual_quit=actual_quit,
                target_minutes=max(0, min(1440, int(target_minutes))),
                overtime_assigned_minutes=max(0, min(1440, int(overtime_minutes))),
                overtime_scheduled_quit=overtime_quit,
                fetched_at=current,
            )
    return result


_BROWSER_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)")
_BROWSER_DATE_RE = re.compile(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)")
_BROWSER_DURATION_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(시간|hour|hours|h|분|minute|minutes|min)",
    re.IGNORECASE,
)
_BROWSER_ACTUAL_WORDS = (
    "실제",
    "기록",
    "타각",
    "clock",
    "actual",
)
_BROWSER_START_WORDS = ("출근", "시작", "근무 시작", "start", "begin")
_BROWSER_END_WORDS = ("퇴근", "종료", "근무 종료", "end", "finish")
_BROWSER_RESPONSE_WORDS = (
    "work",
    "schedule",
    "clock",
    "attendance",
    "time-tracking",
    "overtime",
    "근무",
    "출퇴근",
    "근태",
)
_BROWSER_IDENTITY_WORDS = (
    "/account",
    "/auth",
    "/employee",
    "/employees",
    "/member",
    "/members",
    "/profile",
    "/staff",
    "/user",
    "/users",
    "identity",
    "profile",
    "employee",
    "member",
    "staff",
    "account",
    "사용자",
    "계정",
    "프로필",
    "사번",
)
_BROWSER_EMPLOYEE_RE = re.compile(
    r"(?:사번|사원번호|직원번호|"
    r"employee\s*(?:number|no|id|code)|"
    r"personnel\s*(?:number|no)|staff\s*(?:number|no))"
    r"\s*(?:[:#：]\s*|\s+)([A-Za-z0-9][A-Za-z0-9._/-]{0,119})",
    re.IGNORECASE,
)


def _browser_line_has(line: str, words: tuple[str, ...]) -> bool:
    value = str(line or "").casefold()
    return any(str(word).casefold() in value for word in words)


def _browser_datetime_for_time(
    raw_line: str,
    match: re.Match[str],
    target_day: date,
) -> datetime:
    hour = int(match.group(1))
    minute = int(match.group(2))
    prefix = str(raw_line[max(0, match.start() - 5):match.start()]).casefold()
    if "오후" in prefix or "pm" in prefix:
        if hour < 12:
            hour += 12
    elif ("오전" in prefix or "am" in prefix) and hour == 12:
        hour = 0
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    return datetime.combine(target_day, datetime.min.time()).replace(
        hour=hour,
        minute=minute,
    )


def _browser_line_times(line: str, target_day: date) -> list[datetime]:
    return [
        _browser_datetime_for_time(line, match, target_day)
        for match in _BROWSER_TIME_RE.finditer(str(line or ""))
    ]


def _browser_duration_minutes(line: str) -> int:
    for match in _BROWSER_DURATION_RE.finditer(str(line or "")):
        try:
            amount = float(match.group(1))
        except (TypeError, ValueError):
            continue
        unit = str(match.group(2) or "").casefold()
        if unit in {"분", "minute", "minutes", "min"}:
            return max(0, int(round(amount)))
        return max(0, int(round(amount * 60)))
    return 0


def _browser_date_from_text(text: str, fallback: date) -> date:
    match = _BROWSER_DATE_RE.search(str(text or ""))
    if match is None:
        return fallback
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except (TypeError, ValueError):
        return fallback


def extract_flex_employee_number(text: str) -> str:
    """Read an explicitly labelled employee number from visible Flex text."""

    for match in _BROWSER_EMPLOYEE_RE.finditer(str(text or "")):
        value = _normalize_employee_number(match.group(1))
        if value:
            return value
    return ""


def _browser_block_payload(
    form_name: str,
    source_type: str,
    start: datetime,
    end: datetime | None,
) -> dict[str, Any]:
    return {
        "formName": str(form_name or "근무"),
        "type": str(source_type or "WORK_RECORD"),
        "blockFrom": start.isoformat(),
        "blockTo": end.isoformat() if end is not None else None,
    }


def parse_flex_work_record_text(
    text: str,
    *,
    target_day: date,
    employee_number: str = "",
    now: datetime | None = None,
) -> dict[date, FlexDaySchedule]:
    """Parse the visible work-record page as a last-resort browser fallback.

    Flex can change its internal response shape without changing the visible
    work-record page.  Network JSON is preferred by ``FlexBrowserClient``;
    this conservative parser keeps the integration useful when the page only
    exposes accessible text.  It only creates a schedule when it can find
    labelled times or a clear start/end pair.
    """

    current = now if now is not None else datetime.now()
    if current.tzinfo is not None:
        current = current.astimezone().replace(tzinfo=None)
    day = _browser_date_from_text(text, target_day)
    blocks: list[dict[str, Any]] = []
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    seen: set[tuple[str, str, str | None]] = set()

    def add_block(
        form_name: str,
        source_type: str,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        if start is None:
            return
        key = (
            str(form_name),
            start.isoformat(),
            end.isoformat() if end is not None else None,
        )
        if key in seen:
            return
        seen.add(key)
        blocks.append(_browser_block_payload(form_name, source_type, start, end))

    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for line in lines:
        times = _browser_line_times(line, day)
        if not times:
            continue
        has_overtime = _browser_line_has(line, _OVERTIME_WORDS)
        has_break = _browser_line_has(line, _BREAK_WORDS)
        has_actual = _browser_line_has(line, _BROWSER_ACTUAL_WORDS)
        has_start = _browser_line_has(line, _BROWSER_START_WORDS)
        has_end = _browser_line_has(line, _BROWSER_END_WORDS)
        if len(times) >= 2:
            start, end = times[0], times[1]
            if has_overtime:
                add_block("연장 근무", "WORK_RECORD", start, end)
            elif has_break:
                add_block("휴게", "WORK_RECORD", start, end)
            elif has_actual or (has_start and has_end and "예정" not in line):
                add_block("출퇴근 기록", "WORK_CLOCK", start, end)
            else:
                add_block("기본 근무", "WORK_RECORD", start, end)
            continue

        point = times[0]
        if has_actual or (has_start and "예정" not in line) or (has_end and "예정" not in line):
            if has_end:
                actual_end = point
            elif has_start:
                actual_start = point
        elif has_start:
            planned_start = point
        elif has_end:
            planned_end = point

        if has_overtime:
            duration = _browser_duration_minutes(line)
            if duration > 0:
                add_block(
                    "연장 근무",
                    "WORK_RECORD",
                    point,
                    point + timedelta(minutes=duration),
                )

    if actual_start is not None:
        add_block("출퇴근 기록", "WORK_CLOCK", actual_start, actual_end)
    if planned_start is not None:
        add_block("기본 근무", "WORK_RECORD", planned_start, planned_end)

    if not blocks:
        all_times = [time_value for line in lines for time_value in _browser_line_times(line, day)]
        unique_times = sorted(set(all_times))
        if len(unique_times) >= 2:
            add_block("기본 근무", "WORK_RECORD", unique_times[0], unique_times[-1])

    if not blocks:
        return {}
    payload = {
        "userWorkSchedules": [
            {
                "employeeNumber": str(employee_number or "").strip(),
                "days": [{"date": day.isoformat(), "workBlocks": blocks}],
            }
        ]
    }
    return parse_flex_schedule_response(
        payload,
        employee_number=employee_number,
        now=current,
    )


def _iter_browser_json_dicts(value: Any, *, depth: int = 0):
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_browser_json_dicts(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_browser_json_dicts(child, depth=depth + 1)


def _extract_employee_number_from_payloads(payloads: list[Any]) -> str:
    for payload in payloads:
        if not isinstance(payload, (dict, list)):
            continue
        for candidate in _iter_browser_json_dicts(payload):
            employee_number = _employee_number_from_mapping(candidate)
            if employee_number:
                return employee_number
    return ""


def _extract_work_form_names_from_payloads(payloads: list[Any]) -> dict[str, str]:
    """Build the Flex work-form id to display-name map from browser responses."""

    result: dict[str, str] = {}
    for payload in payloads:
        if not isinstance(payload, (dict, list)):
            continue
        for candidate in _iter_browser_json_dicts(payload):
            raw_forms = candidate.get("workForms")
            for work_form in _as_list(raw_forms):
                if not isinstance(work_form, dict):
                    continue
                form_id = _first_value(
                    work_form,
                    "customerWorkFormId",
                    "workFormId",
                    "formId",
                    "idHash",
                    "id",
                )
                if form_id is None:
                    continue
                display = work_form.get("display")
                form_name = _first_value(
                    work_form,
                    "formName",
                    "workFormName",
                    "name",
                    "label",
                    "title",
                )
                if form_name is None and isinstance(display, dict):
                    form_name = _first_value(display, "name", "label", "title")
                if form_name is not None:
                    result[str(form_id).strip()] = str(form_name).strip()
    return {key: value for key, value in result.items() if key and value}


def _modern_flex_schedule_payload(
    payload: dict,
    *,
    work_form_names: dict[str, str],
) -> dict | None:
    """Translate Flex's current ``dailySchedules`` envelope to our parser model."""

    raw_days = payload.get("dailySchedules")
    if not isinstance(raw_days, list):
        return None
    normalized_days: list[dict[str, Any]] = []
    for raw_day in raw_days:
        if not isinstance(raw_day, dict):
            continue
        target_day = _parse_day(_first_value(raw_day, "date", "workDate", "day"))
        if target_day is None:
            continue
        normalized_blocks: list[dict[str, Any]] = []
        raw_blocks = _first_value(raw_day, "timeBlocks", "blocks")
        for raw_block in _as_list(raw_blocks):
            if not isinstance(raw_block, dict):
                continue
            value = raw_block.get("value")
            if not isinstance(value, dict):
                value = raw_block
            start = _first_value(
                value,
                "startTimestamp",
                "blockFrom",
                "from",
                "start",
                "startAt",
            )
            end = _first_value(
                value,
                "endTimestampExclusive",
                "blockTo",
                "to",
                "end",
                "endAt",
            )
            if _parse_datetime(start) is None:
                continue
            raw_type = str(
                _first_value(raw_block, "type", "blockType", "sourceType", "workType")
                or _first_value(value, "type", "blockType", "sourceType", "workType")
                or ""
            ).strip().upper().replace("-", "_")
            form_id = _first_value(
                value,
                "workFormId",
                "customerWorkFormId",
                "formId",
            )
            form_name = work_form_names.get(str(form_id or "").strip(), "")
            if not form_name:
                if raw_type == "REST":
                    form_name = "휴게"
                elif raw_type in {"WORK", "WORK_RECORD", "SCHEDULE"}:
                    form_name = "근무"
                else:
                    form_name = raw_type or "근무"
            normalized_blocks.append(
                {
                    "type": "WORK_RECORD",
                    "formName": form_name,
                    "blockFrom": start,
                    "blockTo": end,
                }
            )
        if normalized_blocks:
            normalized_days.append(
                {
                    "date": target_day.isoformat(),
                    "workBlocks": normalized_blocks,
                }
            )
    if not normalized_days:
        return None
    return {
        "userWorkSchedules": [
            {
                "employeeNumber": "",
                "days": normalized_days,
            }
        ]
    }


def _parse_browser_response_payloads(
    payloads: list[Any],
    *,
    employee_number: str,
    now: datetime,
) -> tuple[dict[date, FlexDaySchedule], str]:
    detected_employee_number = _extract_employee_number_from_payloads(payloads)
    work_form_names = _extract_work_form_names_from_payloads(payloads)
    wanted_employee_number = (
        str(employee_number or "").strip() or detected_employee_number
    )
    for payload in payloads:
        if not isinstance(payload, (dict, list)):
            continue
        candidates = list(_iter_browser_json_dicts(payload))
        if isinstance(payload, dict):
            modern_payload = _modern_flex_schedule_payload(
                payload,
                work_form_names=work_form_names,
            )
            if modern_payload is not None:
                candidates.insert(0, modern_payload)
            candidates.insert(0, payload)
        for candidate in candidates:
            try:
                parsed = parse_flex_schedule_response(
                    candidate,
                    employee_number=wanted_employee_number,
                    now=now,
                )
            except FlexScheduleError:
                continue
            if parsed:
                return (
                    parsed,
                    _employee_number_from_mapping(candidate)
                    or detected_employee_number,
                )
    return {}, detected_employee_number


class FlexBrowserClient:
    """Interactive, read-only Flex client backed by a persistent Playwright session.

    The browser is intentionally visible so a normal employee can complete
    the organisation's normal Flex login/SSO flow.  The app never receives or
    stores the password, refresh token, client secret, or browser cookies
    outside the Chromium profile created for this purpose.
    """

    def __init__(
        self,
        profile_dir: str,
        *,
        timeout_ms: int = 20_000,
        login_timeout_sec: float = 180.0,
        content_timeout_sec: float | None = None,
        work_url: str = FLEX_WEB_URL,
        headless: bool = False,
        stop_event: Any = None,
    ) -> None:
        self._profile_dir = os.path.abspath(str(profile_dir or "").strip())
        self._timeout_ms = max(5_000, int(timeout_ms))
        self._login_timeout_sec = max(10.0, float(login_timeout_sec))
        default_content_timeout = max(
            15.0,
            min(self._login_timeout_sec, 90.0),
        )
        self._content_timeout_sec = max(
            5.0,
            float(
                default_content_timeout
                if content_timeout_sec is None
                else content_timeout_sec
            ),
        )
        self._work_url = str(work_url or FLEX_WEB_URL).strip() or FLEX_WEB_URL
        self._headless = bool(headless)
        self._stop_event = stop_event
        self._playwright = None
        self._context = None
        self._page = None

    def _ensure_context(self):
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise FlexBrowserError(
                "Flex 브라우저 자동화 모듈을 사용할 수 없습니다.",
                code="playwright_unavailable",
            ) from exc
        try:
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
            os.makedirs(self._profile_dir, exist_ok=True)
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                self._profile_dir,
                headless=self._headless,
            )
        except Exception as exc:
            self.close()
            raise FlexBrowserError(
                "Flex 로그인 브라우저를 열지 못했습니다.",
                code="browser_launch_failed",
            ) from exc
        return self._context

    def _page_is_open(self, page) -> bool:
        if page is None:
            return False
        try:
            return not bool(page.is_closed())
        except Exception:
            return True

    def _get_page(self):
        context = self._ensure_context()
        if self._page_is_open(self._page):
            return self._page
        try:
            pages = list(getattr(context, "pages", []) or [])
        except Exception:
            pages = []
        for page in pages:
            if self._page_is_open(page):
                self._page = page
                break
        if not self._page_is_open(self._page):
            try:
                self._page = context.new_page()
            except Exception as exc:
                raise FlexBrowserError(
                    "Flex 브라우저 탭을 열지 못했습니다.",
                    code="page_create_failed",
                ) from exc
        try:
            self._page.set_default_timeout(self._timeout_ms)
        except Exception:
            pass
        return self._page

    def _body_text(self, page) -> str:
        try:
            return str(page.locator("body").inner_text(timeout=1000) or "")
        except Exception:
            return ""

    def _ready_schedule_content(
        self,
        page,
        payloads: list[Any],
        *,
        begin_date: date,
        end_date: date,
        employee_number: str,
        now: datetime,
    ) -> tuple[dict[date, FlexDaySchedule], str] | None:
        parsed, detected_employee_number = _parse_browser_response_payloads(
            payloads,
            employee_number=employee_number,
            now=now,
        )
        parsed = {
            target_day: schedule
            for target_day, schedule in parsed.items()
            if begin_date <= target_day <= end_date
        }
        visible_text = self._body_text(page)
        detected_employee_number = (
            detected_employee_number
            or extract_flex_employee_number(visible_text)
            or str(employee_number or "").strip()
        )
        if parsed:
            return parsed, detected_employee_number

        visible = parse_flex_work_record_text(
            visible_text,
            target_day=now.date(),
            employee_number=employee_number,
            now=now,
        )
        if visible:
            return visible, detected_employee_number
        return None

    def _wait_for_schedule_content(
        self,
        page,
        payloads: list[Any],
        *,
        begin_date: date,
        end_date: date,
        employee_number: str,
        now: datetime,
    ) -> tuple[dict[date, FlexDaySchedule], str] | None:
        """Wait until Flex's actual schedule content is parseable.

        DOMContentLoaded only means that the SPA shell exists.  Flex loads the
        work record asynchronously, so readiness is determined by a parsed
        schedule response or parsed visible work-record text.  The deadline is
        only a fail-safe; a successful content check returns immediately.
        """

        deadline = time.monotonic() + self._content_timeout_sec
        while True:
            if self._is_stop_requested():
                raise FlexBrowserError(
                    "Flex 브라우저 작업이 종료되었습니다.",
                    code="cancelled",
                )
            ready = self._ready_schedule_content(
                page,
                payloads,
                begin_date=begin_date,
                end_date=end_date,
                employee_number=employee_number,
                now=now,
            )
            if ready is not None:
                return ready
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            wait_ms = max(50, min(250, int(remaining * 1000)))
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:
                time.sleep(wait_ms / 1000.0)

    def _requires_login(self, page) -> bool:
        url = str(getattr(page, "url", "") or "").casefold()
        if "/auth/login" in url or "/login" in url or "/signin" in url:
            return True
        text = self._body_text(page).casefold()
        if "email address" in text and ("sign in" in text or "google" in text):
            return True
        if "로그인" in text and "flex" in text:
            return True
        try:
            if page.locator("input[type='password']").count() > 0:
                return True
        except Exception:
            pass
        return False

    def _wait_for_login(self, page) -> None:
        if not self._requires_login(page):
            return
        if self._headless:
            raise FlexBrowserError(
                "Flex 로그인이 필요합니다. 설정에서 'Flex 로그인 · 지금 동기화'를 눌러 로그인해 주세요.",
                code="login_required",
            )
        try:
            page.bring_to_front()
        except Exception:
            pass
        deadline = time.monotonic() + self._login_timeout_sec
        while time.monotonic() < deadline:
            if self._is_stop_requested():
                raise FlexBrowserError(
                    "Flex 브라우저 작업이 종료되었습니다.",
                    code="cancelled",
                )
            if not self._requires_login(page):
                return
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
        raise FlexBrowserError(
            "Flex 로그인 시간이 초과되었습니다. 열린 Flex 브라우저에서 로그인한 뒤 다시 시도해 주세요.",
            code="login_timeout",
        )

    def _is_stop_requested(self) -> bool:
        event = self._stop_event
        try:
            return bool(event is not None and event.is_set())
        except Exception:
            return False

    def _goto_work_page(self, page, *, wait_for_login: bool) -> None:
        try:
            page.goto(
                self._work_url,
                wait_until="domcontentloaded",
                timeout=self._timeout_ms,
            )
        except Exception as exc:
            raise FlexBrowserError(
                "Flex 근무 기록 페이지를 열지 못했습니다.",
                code="navigation_failed",
            ) from exc
        if wait_for_login:
            self._wait_for_login(page)
        elif self._requires_login(page):
            try:
                page.bring_to_front()
            except Exception:
                pass
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass

    def open_work_record_page(self) -> None:
        page = self._get_page()
        self._goto_work_page(page, wait_for_login=False)
        try:
            page.bring_to_front()
        except Exception:
            pass

    def fetch_schedule_period(
        self,
        begin_date: date,
        end_date: date,
        *,
        employee_number: str = "",
        now: datetime | None = None,
        return_metadata: bool = False,
    ) -> dict[date, FlexDaySchedule] | FlexBrowserSyncResult:
        if self._is_stop_requested():
            raise FlexBrowserError("Flex 브라우저 작업이 종료되었습니다.", code="cancelled")
        if not isinstance(begin_date, date) or not isinstance(end_date, date):
            raise FlexBrowserError("Flex 조회 기간이 올바르지 않습니다.", code="invalid_period")
        if end_date < begin_date or (end_date - begin_date).days >= 31:
            raise FlexBrowserError("Flex 근무 조회 기간은 31일 이내여야 합니다.", code="invalid_period")
        current = now if now is not None else datetime.now()
        if current.tzinfo is not None:
            current = current.astimezone().replace(tzinfo=None)
        page = self._get_page()
        payloads: list[Any] = []

        def collect_response(response) -> None:
            try:
                resource_type = str(response.request.resource_type or "")
                url = str(response.url or "").casefold()
                if resource_type not in {"xhr", "fetch"}:
                    return
                if not (
                    any(word in url for word in _BROWSER_RESPONSE_WORDS)
                    or any(word in url for word in _BROWSER_IDENTITY_WORDS)
                ):
                    return
                payload = response.json()
                if isinstance(payload, (dict, list)):
                    payloads.append(payload)
            except Exception:
                return

        try:
            page.on("response", collect_response)
            self._goto_work_page(page, wait_for_login=True)
            ready = self._wait_for_schedule_content(
                page,
                payloads,
                begin_date=begin_date,
                end_date=end_date,
                employee_number=employee_number,
                now=current,
            )
        finally:
            try:
                page.remove_listener("response", collect_response)
            except Exception:
                pass

        if ready is None:
            raise FlexBrowserError(
                "Flex 근무 기록 화면에서 근무 정보 콘텐츠를 확인하지 못했습니다. Flex의 본인 근무 기록 페이지가 완전히 로드된 뒤 다시 시도해 주세요.",
                code="schedule_not_found",
            )
        parsed, detected_employee_number = ready

        def make_result(schedules: dict[date, FlexDaySchedule]):
            if return_metadata:
                return FlexBrowserSyncResult(
                    schedules=schedules,
                    employee_number=detected_employee_number,
                )
            return schedules

        return make_result(parsed)

    def close(self) -> None:
        context = self._context
        playwright_obj = self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if playwright_obj is not None:
                playwright_obj.stop()
        except Exception:
            pass
