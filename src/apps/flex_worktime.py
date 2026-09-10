"""Small, read-only client for Flex Open API work schedules.

Flex exposes work schedules and clock events through its Open API.  The
client deliberately keeps this module independent from the Tk application so
that response parsing and authentication can be tested without a desktop
session.  It never attempts to create or modify a Flex work record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import re
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


FLEX_API_BASE_URL = "https://openapi.flex.team"
FLEX_TOKEN_PATH = "/v2/auth/realms/open-api/protocol/openid-connect/token"
FLEX_SCHEDULE_PATH = "/v2/users/work-schedules-with-work-clock/dates"
# The work-record route preserves the destination through Flex login when the
# browser session is not authenticated.
FLEX_WEB_URL = "https://flex.team/time-tracking/work-record/my"

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


class FlexApiError(RuntimeError):
    """A safe, user-facing Flex API failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "api_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(str(message))
        self.code = str(code or "api_error")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class FlexCredentials:
    """Open API credentials; values are kept outside UI model snapshots."""

    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""

    def normalized(self) -> "FlexCredentials":
        return FlexCredentials(
            refresh_token=str(self.refresh_token or "").strip(),
            client_id=str(self.client_id or "").strip(),
            client_secret=str(self.client_secret or "").strip(),
        )

    @property
    def configured(self) -> bool:
        value = self.normalized()
        return bool(value.refresh_token or (value.client_id and value.client_secret))


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
    if isinstance(value, datetime):
        parsed = value
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


def _employee_value(value: dict) -> str:
    return str(
        _first_value(
            value,
            "employeeNumber",
            "employeeNo",
            "employeeId",
            "memberNumber",
            "userNumber",
        )
        or ""
    ).strip()


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
    """Parse the documented schedule response into local-day records.

    The parser accepts the minor envelope variations seen between Flex API
    versions while keeping the normalized model strict and deterministic.
    """

    if not isinstance(payload, dict):
        raise FlexApiError("Flex 근무 일정 응답 형식이 올바르지 않습니다.", code="invalid_response")
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


Transport = Callable[[str, str, dict[str, str], bytes | None, float], Any]


class FlexOpenApiClient:
    """Authenticated, read-only Flex Open API client."""

    def __init__(
        self,
        credentials: FlexCredentials,
        *,
        base_url: str = FLEX_API_BASE_URL,
        timeout_sec: float = 15.0,
        transport: Transport | None = None,
    ) -> None:
        self._credentials = credentials.normalized()
        self._base_url = str(base_url or FLEX_API_BASE_URL).rstrip("/")
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._transport = transport or self._default_transport

    @property
    def credentials(self) -> FlexCredentials:
        return self._credentials

    @staticmethod
    def _default_transport(
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
        timeout_sec: float,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method.upper(),
        )
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            return int(getattr(response, "status", 200)), response.read()

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> dict:
        request_headers = {"Accept": "application/json"}
        request_headers.update(headers or {})
        try:
            response = self._transport(
                method,
                url,
                request_headers,
                data,
                self._timeout_sec,
            )
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            detail = self._error_detail(body)
            raise FlexApiError(
                detail or "Flex API 요청이 거부되었습니다.",
                code="http_error",
                status_code=int(getattr(exc, "code", 0) or 0),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FlexApiError(
                "Flex API에 연결하지 못했습니다.",
                code="network_error",
            ) from exc
        except FlexApiError:
            raise
        except Exception as exc:
            raise FlexApiError("Flex API 요청에 실패했습니다.", code="request_error") from exc

        status = 200
        body = response
        if isinstance(response, tuple) and len(response) == 2:
            status, body = response
        if int(status) < 200 or int(status) >= 300:
            raise FlexApiError(
                self._error_detail(body) or "Flex API 요청이 거부되었습니다.",
                code="http_error",
                status_code=int(status),
            )
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            value = json.loads(bytes(body or b"{}").decode("utf-8"))
        except Exception as exc:
            raise FlexApiError("Flex API 응답을 해석하지 못했습니다.", code="invalid_json") from exc
        if not isinstance(value, dict):
            raise FlexApiError("Flex API 응답 형식이 올바르지 않습니다.", code="invalid_response")
        return value

    @staticmethod
    def _error_detail(body: Any) -> str:
        try:
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            value = json.loads(str(body or "")) if body else {}
            if isinstance(value, dict):
                for key in ("message", "error_description", "error", "code"):
                    text = str(value.get(key) or "").strip()
                    if text:
                        return text[:180]
        except Exception:
            pass
        return ""

    def _issue_access_token(self) -> str:
        credentials = self._credentials.normalized()
        if credentials.refresh_token:
            form = {
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
                "client_id": credentials.client_id or "open-api",
            }
            if credentials.client_secret:
                form["client_secret"] = credentials.client_secret
        elif credentials.client_id and credentials.client_secret:
            form = {
                "grant_type": "client_credentials",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            }
        else:
            raise FlexApiError(
                "Flex Open API 인증정보가 설정되지 않았습니다.",
                code="credentials_missing",
            )
        response = self._request_json(
            "POST",
            f"{self._base_url}{FLEX_TOKEN_PATH}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=urllib.parse.urlencode(form).encode("utf-8"),
        )
        access_token = str(response.get("access_token") or "").strip()
        if not access_token:
            raise FlexApiError(
                "Flex Open API 액세스 토큰을 받지 못했습니다.",
                code="token_missing",
            )
        rotated_refresh = str(response.get("refresh_token") or "").strip()
        if rotated_refresh:
            self._credentials = FlexCredentials(
                refresh_token=rotated_refresh,
                client_id=credentials.client_id,
                client_secret=credentials.client_secret,
            )
        return access_token

    def fetch_schedule_period(
        self,
        begin_date: date,
        end_date: date,
        *,
        employee_number: str,
        now: datetime | None = None,
    ) -> dict[date, FlexDaySchedule]:
        if not isinstance(begin_date, date) or not isinstance(end_date, date):
            raise FlexApiError("Flex 조회 기간이 올바르지 않습니다.", code="invalid_period")
        if end_date < begin_date or (end_date - begin_date).days >= 31:
            raise FlexApiError("Flex 근무 조회 기간은 31일 이내여야 합니다.", code="invalid_period")
        employee = str(employee_number or "").strip()
        if not employee:
            raise FlexApiError("Flex 사번이 설정되지 않았습니다.", code="employee_missing")
        access_token = self._issue_access_token()
        query = urllib.parse.urlencode(
            [("employeeNumbers", employee)],
            doseq=True,
        )
        url = (
            f"{self._base_url}{FLEX_SCHEDULE_PATH}/"
            f"{begin_date.isoformat()}/{end_date.isoformat()}?{query}"
        )
        response = self._request_json(
            "GET",
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return parse_flex_schedule_response(
            response,
            employee_number=employee,
            now=now,
        )
