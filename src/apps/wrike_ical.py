"""Minimal Google Calendar private-iCal (.ics) feed reader.

Supports the Wrike break and vacation features: VEVENT DTSTART/DTEND with
``Z`` UTC conversion, all-day events, daily/weekly recurrence, EXDATE
exclusion, keyword filtering, and calendar metadata. Standard-library only;
TZID params besides UTC are treated as local wall-clock times.
"""

from __future__ import annotations

import re
import socket
import ssl
import unicodedata
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Generic, TypeAlias, TypeVar, final

DEFAULT_POLL_TIMEOUT_SEC = 15.0
MAX_ICAL_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class CalendarErrorCode(str, Enum):
    """Stable, privacy-safe failures for calendar decoding and validation."""

    INVALID_ENDPOINT = "invalid_endpoint"
    REDIRECT_REJECTED = "redirect_rejected"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    AUTHENTICATION_REQUIRED = "authentication_required"
    UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"
    DNS_OR_CONNECT = "dns_or_connect"
    TIMEOUT = "timeout"
    TLS_VALIDATION = "tls_validation"
    BODY_TOO_LARGE = "body_too_large"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    UTF8_DECODE = "utf8_decode"
    EMPTY_BODY = "empty_body"
    INVALID_ICAL = "invalid_ical"

    def __str__(self) -> str:
        return self.value


_ResultValue = TypeVar("_ResultValue")


@final
@dataclass(frozen=True, slots=True)
class CalendarSuccess(Generic[_ResultValue]):
    """Successful typed result whose representation never includes its value."""

    value: _ResultValue = field(repr=False)


@final
@dataclass(frozen=True, slots=True)
class CalendarError:
    """Closed error result containing only a stable, non-sensitive code."""

    code: CalendarErrorCode


CalendarResponseDecodeResult: TypeAlias = CalendarSuccess[str] | CalendarError
CalendarDocumentParseResult: TypeAlias = CalendarSuccess[dict] | CalendarError


def _response_header_values(response, name: str) -> list[str]:
    try:
        headers = response.headers
    except Exception:
        return []
    if headers is None:
        return []

    def as_values(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return [str(value)]

    try:
        get_all = getattr(headers, "get_all", None)
        values = as_values(get_all(name)) if callable(get_all) else []
        if values:
            return values
    except Exception:
        pass
    try:
        value = headers.get(name)
        if value is not None:
            return as_values(value)
    except Exception:
        pass
    try:
        return [
            item
            for key, value in headers.items()
            if str(key).strip().lower() == name.lower()
            for item in as_values(value)
        ]
    except Exception:
        return []


def _declared_body_too_large(response, limit: int) -> bool:
    for raw_value in _response_header_values(response, "Content-Length"):
        for raw_piece in raw_value.split(","):
            try:
                declared = int(raw_piece.strip())
            except (TypeError, ValueError):
                continue
            if declared > limit:
                return True
    return False


def _response_content_encoding(response) -> str | None:
    values = _response_header_values(response, "Content-Encoding")
    if not values:
        return "identity"
    if len(values) != 1:
        return None
    encodings = [piece.strip().lower() for piece in values[0].split(",")]
    if len(encodings) != 1:
        return None
    encoding = encodings[0] or "identity"
    return encoding if encoding in {"identity", "gzip"} else None


def _response_media_type(response) -> str | None:
    values = _response_header_values(response, "Content-Type")
    if not values:
        return ""
    if len(values) != 1:
        return None
    return values[0].split(";", 1)[0].strip().lower()


def _response_content_type_error(media_type: str | None) -> CalendarErrorCode | None:
    if media_type is None:
        return CalendarErrorCode.UNEXPECTED_CONTENT_TYPE
    if media_type in {
        "",
        "text/html",
        "application/xhtml+xml",
        "text/calendar",
        "application/calendar",
        "application/ics",
        "text/plain",
        "application/octet-stream",
    }:
        return None
    return CalendarErrorCode.UNEXPECTED_CONTENT_TYPE


def _html_authentication_required(payload: bytes) -> bool:
    lowered = payload.lower()
    if re.search(
        br"\btype\s*=\s*['\"]?password(?:['\"\s/>]|$)",
        lowered,
    ):
        return True
    if any(
        marker in lowered
        for marker in (
            b"accounts.google.com",
            b"login.microsoftonline.com",
            b"login.live.com",
        )
    ):
        return True
    return b"<form" in lowered and any(
        marker in lowered
        for marker in (b"login", b"log in", b"signin", b"sign-in", b"sign in")
    )


def decode_calendar_response(
    response,
    limit_bytes: int = MAX_ICAL_BYTES,
) -> CalendarResponseDecodeResult:
    """Incrementally read and strictly decode one bounded calendar response."""
    try:
        limit = min(MAX_ICAL_BYTES, max(0, int(limit_bytes)))
    except Exception:
        return CalendarError(CalendarErrorCode.INVALID_ICAL)
    media_type = _response_media_type(response)
    content_type_error = _response_content_type_error(media_type)
    if content_type_error is not None:
        return CalendarError(content_type_error)
    if _declared_body_too_large(response, limit):
        return CalendarError(CalendarErrorCode.BODY_TOO_LARGE)

    encoding = _response_content_encoding(response)
    if encoding is None:
        return CalendarError(CalendarErrorCode.UNSUPPORTED_ENCODING)

    decompressor = (
        zlib.decompressobj(16 + zlib.MAX_WBITS)
        if encoding == "gzip"
        else None
    )
    decoded_parts: list[bytes] = []
    wire_size = 0
    decoded_size = 0

    while True:
        read_size = min(_READ_CHUNK_BYTES, limit - wire_size + 1)
        try:
            raw_chunk = response.read(read_size)
        except Exception as exc:
            if isinstance(exc, (TimeoutError, socket.timeout)):
                code = CalendarErrorCode.TIMEOUT
            elif isinstance(
                exc,
                (ssl.SSLCertVerificationError, ssl.CertificateError, ssl.SSLError),
            ):
                code = CalendarErrorCode.TLS_VALIDATION
            elif isinstance(exc, urllib.error.HTTPError):
                try:
                    status = int(getattr(exc, "code", 0))
                except Exception:
                    status = 0
                if 400 <= status <= 499:
                    code = CalendarErrorCode.HTTP_4XX
                elif 500 <= status <= 599:
                    code = CalendarErrorCode.HTTP_5XX
                else:
                    code = CalendarErrorCode.DNS_OR_CONNECT
            elif isinstance(exc, urllib.error.URLError):
                reason = getattr(exc, "reason", None)
                if isinstance(reason, (TimeoutError, socket.timeout)):
                    code = CalendarErrorCode.TIMEOUT
                elif isinstance(
                    reason,
                    (
                        ssl.SSLCertVerificationError,
                        ssl.CertificateError,
                        ssl.SSLError,
                    ),
                ):
                    code = CalendarErrorCode.TLS_VALIDATION
                else:
                    code = CalendarErrorCode.DNS_OR_CONNECT
            else:
                code = CalendarErrorCode.DNS_OR_CONNECT
            return CalendarError(code)
        if not isinstance(raw_chunk, (bytes, bytearray, memoryview)):
            return CalendarError(CalendarErrorCode.INVALID_ICAL)
        chunk = bytes(raw_chunk)
        if not chunk:
            break
        wire_size += len(chunk)
        if wire_size > limit:
            return CalendarError(CalendarErrorCode.BODY_TOO_LARGE)

        if decompressor is None:
            decoded_parts.append(chunk)
            decoded_size += len(chunk)
            continue

        pending = chunk
        while pending:
            remaining = limit - decoded_size
            try:
                decoded_chunk = decompressor.decompress(pending, remaining + 1)
            except zlib.error:
                return CalendarError(CalendarErrorCode.INVALID_ICAL)
            if len(decoded_chunk) > remaining:
                return CalendarError(CalendarErrorCode.BODY_TOO_LARGE)
            if decoded_chunk:
                decoded_parts.append(decoded_chunk)
                decoded_size += len(decoded_chunk)
            if decompressor.unused_data:
                return CalendarError(CalendarErrorCode.INVALID_ICAL)
            next_pending = decompressor.unconsumed_tail
            if next_pending and len(next_pending) >= len(pending) and not decoded_chunk:
                return CalendarError(CalendarErrorCode.INVALID_ICAL)
            pending = next_pending

    if wire_size == 0:
        return CalendarError(CalendarErrorCode.EMPTY_BODY)

    if decompressor is not None:
        remaining = limit - decoded_size
        try:
            decoded_tail = decompressor.flush(remaining + 1)
        except zlib.error:
            return CalendarError(CalendarErrorCode.INVALID_ICAL)
        if len(decoded_tail) > remaining:
            return CalendarError(CalendarErrorCode.BODY_TOO_LARGE)
        if decoded_tail:
            decoded_parts.append(decoded_tail)
            decoded_size += len(decoded_tail)
        if not decompressor.eof or decompressor.unused_data:
            return CalendarError(CalendarErrorCode.INVALID_ICAL)

    payload = b"".join(decoded_parts)
    if not payload:
        return CalendarError(CalendarErrorCode.EMPTY_BODY)
    if media_type in {"text/html", "application/xhtml+xml"}:
        code = (
            CalendarErrorCode.AUTHENTICATION_REQUIRED
            if _html_authentication_required(payload)
            else CalendarErrorCode.UNEXPECTED_CONTENT_TYPE
        )
        return CalendarError(code)
    try:
        text = payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return CalendarError(CalendarErrorCode.UTF8_DECODE)
    if not text:
        return CalendarError(CalendarErrorCode.EMPTY_BODY)
    return CalendarSuccess(text)


def read_calendar_response_text(
    response,
    limit_bytes: int = MAX_ICAL_BYTES,
) -> str | None:
    """Compatibility adapter returning decoded calendar text or ``None``."""
    result = decode_calendar_response(response, limit_bytes=limit_bytes)
    return result.value if isinstance(result, CalendarSuccess) else None


def fetch_calendar_text(url: str, timeout_sec: float = DEFAULT_POLL_TIMEOUT_SEC) -> str | None:
    cleaned = str(url or "").strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        return None
    try:
        request = urllib.request.Request(
            cleaned,
            headers={"User-Agent": "windows-supporter/ical", "Accept": "text/calendar"},
        )
        with urllib.request.urlopen(request, timeout=max(5.0, float(timeout_sec))) as resp:
            return read_calendar_response_text(resp)
    except Exception:
        return None


def unfold_ics_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw_line:
            lines.append("")
            continue
        if raw_line[:1] in (" ", "\t"):
            if lines:
                lines[-1] += raw_line[1:]
            continue
        lines.append(raw_line)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    depth_quote = False
    colon_idx = -1
    for idx, ch in enumerate(line):
        if ch == '"':
            depth_quote = not depth_quote
        elif ch == ":" and not depth_quote:
            colon_idx = idx
            break
    if colon_idx < 0:
        return None
    head, value = line[:colon_idx], line[colon_idx + 1:]
    segments = head.split(";")
    name = str(segments[0]).strip().upper()
    params: dict[str, str] = {}
    for segment in segments[1:]:
        if "=" not in segment:
            params[str(segment).strip().upper()] = ""
            continue
        key, _, val = segment.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def _unescape_ics_text(value: str) -> str:
    return (
        str(value or "").replace("\\n", " ").replace("\\N", " ")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def _parse_ics_date_value(raw: str) -> tuple[datetime | None, bool]:
    text = str(raw or "").strip()
    if not text:
        return None, True
    utc = text.endswith("Z")
    compact = text[:-1] if utc else text
    date_mode = False
    try:
        normalized = compact.replace("t", "T") if ("T" not in compact and "t" in compact) else compact
        if "T" in normalized.upper():
            parsed = datetime.strptime(normalized, "%Y%m%dT%H%M%S")
        else:
            parsed = datetime.strptime(normalized, "%Y%m%d")
            date_mode = True
    except Exception:
        return None, True
    if utc and not date_mode:
        parsed = parsed.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return parsed, date_mode


def parse_calendar(text: str) -> dict:
    """Permissively parse metadata and VEVENT values for legacy callers."""
    result = {"calendar_name": "", "timezone": "", "events": []}
    stack: list[str] = []
    current: dict | None = None
    saw_x_wr_calname = False

    for line in unfold_ics_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith("BEGIN:"):
            component = stripped[len("BEGIN:"):].strip().upper()
            stack.append(component)
            if component == "VEVENT" and current is None:
                current = {
                    "uid": "",
                    "summary": "",
                    "status": "",
                    "recurrence_id": None,
                    "dtstart": None,
                    "dtend": None,
                    "all_day": False,
                    "rrule": {},
                    "exdates": [],
                }
            continue
        if upper.startswith("END:"):
            component = stripped[len("END:"):].strip().upper()
            if component == "VEVENT" and current is not None:
                if (
                    current.get("dtstart") is not None
                    or current.get("recurrence_id") is not None
                ):
                    result["events"].append(current)
                current = None
            if stack:
                if stack[-1] == component:
                    stack.pop()
                else:
                    try:
                        reverse_idx = stack[::-1].index(component)
                        del stack[len(stack) - reverse_idx - 1:]
                    except ValueError:
                        pass
            continue

        parsed = _split_property(stripped)
        if parsed is None:
            continue
        name, params, value = parsed
        escaped = _unescape_ics_text(value)

        if current is None:
            if stack and stack[-1] != "VCALENDAR":
                continue
            if name == "X-WR-CALNAME":
                saw_x_wr_calname = True
                result["calendar_name"] = escaped.strip()
            elif name == "NAME" and stack == ["VCALENDAR"] and not saw_x_wr_calname:
                result["calendar_name"] = escaped.strip()
            elif name == "X-WR-TIMEZONE":
                result["timezone"] = escaped.strip()
            continue

        if stack and stack[-1] != "VEVENT":
            continue
        if name == "UID":
            current["uid"] = escaped.strip()
        elif name == "SUMMARY":
            current["summary"] = escaped.strip()
        elif name == "STATUS":
            current["status"] = escaped.strip().upper()
        elif name == "RECURRENCE-ID":
            recurrence_id, _mode = _parse_ics_date_value(value)
            if recurrence_id is not None:
                current["recurrence_id"] = recurrence_id
        elif name == "DTSTART":
            dt_value, date_only = _parse_ics_date_value(value)
            if dt_value is not None:
                current["dtstart"] = dt_value
                current["all_day"] = bool(date_only) or params.get("VALUE", "").upper() == "DATE"
        elif name == "DTEND":
            dt_value, _mode = _parse_ics_date_value(value)
            if dt_value is not None:
                current["dtend"] = dt_value
        elif name == "RRULE":
            rule: dict[str, str] = {}
            for chunk in value.split(";"):
                key, _, val = chunk.partition("=")
                if key.strip():
                    rule[key.strip().upper()] = val.strip().upper()
            current["rrule"] = rule
        elif name == "EXDATE":
            for piece in value.split(","):
                ex_dt, _mode = _parse_ics_date_value(piece.strip())
                if ex_dt is not None:
                    current["exdates"].append(ex_dt)
    return result


_COMPONENT_NAME = re.compile(r"^[A-Z0-9-]+$")


def _strict_unfold_ics_lines(text: str) -> list[str] | None:
    lines: list[str] = []
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for raw_line in raw_lines:
        if raw_line[:1] in (" ", "\t"):
            if not lines or not lines[-1]:
                return None
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)
    return lines


def _is_strict_calendar_document(text: str) -> bool:
    lines = _strict_unfold_ics_lines(text)
    if lines is None:
        return False
    stack: list[str] = []
    root_seen = False
    root_closed = False
    for line in lines:
        if not line.strip():
            continue
        parsed = _split_property(line)
        if parsed is None:
            return False
        name, params, value = parsed
        if not name:
            return False
        if name not in {"BEGIN", "END"}:
            if not stack or root_closed:
                return False
            continue
        component = value.strip().upper()
        if params or not _COMPONENT_NAME.fullmatch(component):
            return False
        if name == "BEGIN":
            if not stack:
                if root_seen or root_closed or component != "VCALENDAR":
                    return False
                root_seen = True
            elif component == "VCALENDAR":
                return False
            stack.append(component)
            continue
        if not stack or stack[-1] != component:
            return False
        if len(stack) == 1:
            if component != "VCALENDAR":
                return False
            root_closed = True
        stack.pop()
    return root_seen and root_closed and not stack


def parse_calendar_document(text: str) -> CalendarDocumentParseResult:
    """Validate one complete VCALENDAR document, then parse it permissively."""
    if not isinstance(text, str):
        return CalendarError(CalendarErrorCode.INVALID_ICAL)
    document = text[1:] if text.startswith("\ufeff") else text
    if not document.strip():
        return CalendarError(CalendarErrorCode.EMPTY_BODY)
    if not _is_strict_calendar_document(document):
        return CalendarError(CalendarErrorCode.INVALID_ICAL)
    try:
        return CalendarSuccess(parse_calendar(document))
    except Exception:
        return CalendarError(CalendarErrorCode.INVALID_ICAL)


def parse_ics(text: str) -> list[dict]:
    """Return DTSTART-bearing VEVENT dictionaries for legacy callers."""
    return [
        event
        for event in (parse_calendar(text).get("events") or [])
        if isinstance(event.get("dtstart"), datetime)
    ]


_ICS_BYDAY_MAP = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6,
}


def _rule_int(rule: dict[str, str], key: str, default: int | None) -> int | None:
    raw = rule.get(key, "")
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _parse_until_rule(rule: dict[str, str]) -> tuple[datetime | None, bool]:
    raw = rule.get("UNTIL", "")
    if not raw:
        return None, False
    parsed, date_mode = _parse_ics_date_value(raw)
    return parsed, bool(date_mode)


def _occurrence_end(event: dict, start_date) -> tuple[datetime, datetime | None] | None:
    dtstart = event.get("dtstart")
    dtend = event.get("dtend")
    if dtstart is None:
        return None
    occ_start = datetime.combine(start_date, dtstart.time())
    if not isinstance(dtend, datetime):
        if bool(event.get("all_day")):
            return occ_start, occ_start + timedelta(days=1)
        return occ_start, None
    try:
        span_days = (dtend.date() - dtstart.date()).days
    except Exception:
        span_days = 0
    occ_end = datetime.combine(start_date + timedelta(days=max(0, span_days)), dtend.time())
    if bool(event.get("all_day")) and occ_end <= occ_start:
        occ_end = occ_start + timedelta(days=1)
    return occ_start, occ_end


def _excluded(exdates: list, occ_start: datetime) -> bool:
    probe = occ_start.replace(second=0, microsecond=0)
    for ex in exdates:
        try:
            if isinstance(ex, datetime) and ex.replace(second=0, microsecond=0) == probe:
                return True
        except Exception:
            continue
    return False


def _pair_hits_day(
    pair: tuple[datetime, datetime | None],
    target_day,
    all_day: bool,
) -> bool:
    start_dt, end_dt = pair
    if end_dt is None:
        return start_dt.date() == target_day
    if all_day:
        day_start = datetime.combine(target_day, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        return start_dt < day_end and end_dt > day_start
    return start_dt.date() <= target_day <= end_dt.date()


def occurrences_for_day(
    event: dict,
    day,
    include_all_day: bool = False,
) -> list[tuple[datetime, datetime | None]]:
    """Concrete intervals of ``event`` relevant to ``day`` (date/datetime).

    RRULE coverage: FREQ=DAILY/WEEKLY with INTERVAL, BYDAY (weekly), UNTIL,
    COUNT, EXDATE. COUNT follows RFC order including EXDATE-skipped slots;
    the day scan is bounded to 900 iterations. All-day events remain excluded
    unless ``include_all_day`` is explicitly true.
    """
    dtstart = event.get("dtstart")
    all_day = bool(event.get("all_day"))
    if not isinstance(dtstart, datetime) or (all_day and not include_all_day):
        return []
    target_day = day.date() if isinstance(day, datetime) else day
    exdates = list(event.get("exdates") or [])
    result: list[tuple[datetime, datetime | None]] = []

    rule = event.get("rrule") or {}
    freq = str(rule.get("FREQ", "")).strip().upper()
    if freq not in ("DAILY", "WEEKLY"):
        pair = _occurrence_end(event, dtstart.date())
        if pair is None:
            return result
        if _pair_hits_day(pair, target_day, all_day) and not _excluded(exdates, pair[0]):
            result.append(pair)
        return result

    interval = max(1, _rule_int(rule, "INTERVAL", 1) or 1)
    until_dt, until_is_date = _parse_until_rule(rule)
    count_limit = _rule_int(rule, "COUNT", None)

    weekly_days: set[int] = set()
    if freq == "WEEKLY":
        raw_days = [
            piece.strip()
            for piece in str(rule.get("BYDAY", "")).split(",")
            if piece.strip()
        ]
        weekly_days = {_ICS_BYDAY_MAP[item] for item in raw_days if item in _ICS_BYDAY_MAP}
        if not weekly_days:
            weekly_days = {dtstart.weekday()}

    anchor = dtstart.date()
    scan_cap = min(max((target_day - anchor).days, 0) + 1, 900)
    matched_consumed = 0
    current = anchor
    for _offset in range(scan_cap):
        delta_days = (current - anchor).days
        if freq == "WEEKLY":
            period_match = (delta_days // 7) % interval == 0 and current.weekday() in weekly_days
        else:
            period_match = delta_days % interval == 0
        if period_match:
            cand_start = datetime.combine(current, dtstart.time())
            exceeds_until = False
            if until_dt is not None:
                exceeds_until = (
                    current > until_dt.date()
                    if until_is_date
                    else cand_start > until_dt
                )
            reaches_count = count_limit is not None and matched_consumed >= count_limit
            if exceeds_until or reaches_count:
                break
            matched_consumed += 1
            if not _excluded(exdates, cand_start):
                pair = _occurrence_end(event, current)
                if pair is not None and _pair_hits_day(pair, target_day, all_day):
                    result.append(pair)
        current = current + timedelta(days=1)
    return result


def matching_break_events(events, keywords, day) -> list[dict]:
    """Filter calendar events by keyword substrings and expand to intervals.

    An empty keyword list disables matching entirely (fail-closed) so a
    mis-typed keyword box cannot turn unrelated meetings into breaks.
    """
    terms = [
        str(term or "").strip().lower()
        for term in (keywords or [])
        if str(term or "").strip()
    ]
    matched: list[dict] = []
    if not terms:
        return matched
    for event in events or []:
        if not isinstance(event, dict):
            continue
        summary = str(event.get("summary") or "").strip()
        lowered = summary.lower()
        if not any(term in lowered for term in terms):
            continue
        intervals = occurrences_for_day(event, day)
        if intervals:
            matched.append({"label": summary, "intervals": intervals})
    return matched


def _normalized_summary(value) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split())


def _clip_to_day(
    span: tuple[datetime, datetime | None],
    target_day,
) -> tuple[datetime, datetime] | None:
    try:
        start_dt, end_dt = span
        if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime):
            return None
        day_start = datetime.combine(target_day, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        clipped_start = max(start_dt, day_start)
        clipped_end = min(end_dt, day_end)
        if clipped_end <= clipped_start:
            return None
        return clipped_start, clipped_end
    except Exception:
        return None


def _same_recurrence_start(left: datetime, right: datetime) -> bool:
    try:
        return left.replace(microsecond=0) == right.replace(microsecond=0)
    except Exception:
        return False


VACATION_CALENDAR_SCHEMA_VERSION = 1
_VACATION_RRULE_FIELDS = frozenset({"FREQ", "INTERVAL", "BYDAY", "UNTIL", "COUNT"})


def compile_vacation_calendar(calendar, expected_calendar_name: str) -> dict:
    """Compile a private vacation feed into calculation-only runtime data.

    Calendar identity and event titles are used only while validating and
    classifying the document. Raw UIDs are replaced with document-local integer
    keys so the long-lived cache cannot disclose feed identity or titles.
    """
    parsed = parse_calendar(calendar) if isinstance(calendar, str) else calendar
    compiled = {
        "vacation_schema_version": VACATION_CALENDAR_SCHEMA_VERSION,
        "calendar_matched": False,
        "events": [],
    }
    if not isinstance(parsed, dict):
        return compiled
    expected = str(expected_calendar_name or "").strip()
    observed = str(parsed.get("calendar_name") or "").strip()
    if not expected or observed != expected:
        return compiled
    compiled["calendar_matched"] = True

    events = [event for event in (parsed.get("events") or []) if isinstance(event, dict)]
    uid_keys: dict[str, int] = {}
    master_matches: dict[str, bool] = {}
    for event in events:
        uid = str(event.get("uid") or "").strip()
        if uid and uid not in uid_keys:
            uid_keys[uid] = len(uid_keys) + 1
        if uid and not isinstance(event.get("recurrence_id"), datetime):
            summary = str(event.get("summary") or "").strip()
            if summary:
                master_matches.setdefault(
                    uid,
                    "휴가" in _normalized_summary(summary),
                )

    sanitized_events: list[dict] = []
    for event in events:
        uid = str(event.get("uid") or "").strip()
        summary = str(event.get("summary") or "").strip()
        vacation_match = (
            "휴가" in _normalized_summary(summary)
            if summary
            else bool(uid and master_matches.get(uid, False))
        )
        rule = event.get("rrule")
        sanitized_rule = {
            str(key): str(value)
            for key, value in (rule.items() if isinstance(rule, dict) else ())
            if str(key) in _VACATION_RRULE_FIELDS
        }
        sanitized_events.append(
            {
                "event_key": uid_keys.get(uid),
                "vacation_match": vacation_match,
                "cancelled": (
                    str(event.get("status") or "").strip().upper() == "CANCELLED"
                ),
                "recurrence_id": event.get("recurrence_id"),
                "dtstart": event.get("dtstart"),
                "dtend": event.get("dtend"),
                "all_day": bool(event.get("all_day")),
                "rrule": sanitized_rule,
                "exdates": [
                    value
                    for value in (event.get("exdates") or [])
                    if isinstance(value, datetime)
                ],
            }
        )
    compiled["events"] = sanitized_events
    return compiled


def vacation_events_for_day(calendar, expected_calendar_name: str, day) -> dict:
    """Return matching vacation occurrences clipped to one local day.

    Raw calendars are compiled first; already-compiled runtime calendars never
    contain calendar identity, event titles, or raw UIDs. Cancelled events and
    events not classified with the normalized ``휴가`` substring are ignored.
    Recurrence exceptions suppress their original master slot; a non-cancelled
    moved exception contributes its replacement interval. Returned intervals
    remain unmerged because the work-time helper owns overlap/adjacency union.
    """
    if (
        isinstance(calendar, dict)
        and calendar.get("vacation_schema_version")
        == VACATION_CALENDAR_SCHEMA_VERSION
    ):
        compiled = calendar
    else:
        compiled = compile_vacation_calendar(calendar, expected_calendar_name)
    result = {
        "calendar_matched": bool(compiled.get("calendar_matched") is True),
        "all_day": False,
        "intervals": [],
        "event_count": 0,
    }
    if not result["calendar_matched"]:
        return result

    try:
        target_day = day.date() if isinstance(day, datetime) else day
        datetime.combine(target_day, datetime.min.time())
    except Exception:
        return result

    events = [event for event in (compiled.get("events") or []) if isinstance(event, dict)]
    exception_starts: dict[int, list[datetime]] = {}
    for event in events:
        event_key = event.get("event_key")
        recurrence_id = event.get("recurrence_id")
        if type(event_key) is int and isinstance(recurrence_id, datetime):
            exception_starts.setdefault(event_key, []).append(recurrence_id)

    for event in events:
        if event.get("cancelled") is True or event.get("vacation_match") is not True:
            continue
        event_key = event.get("event_key")
        recurrence_id = event.get("recurrence_id")
        try:
            occurrences = occurrences_for_day(event, target_day, include_all_day=True)
        except Exception:
            continue
        if type(event_key) is int and not isinstance(recurrence_id, datetime):
            suppressed = exception_starts.get(event_key, [])
            occurrences = [
                occurrence
                for occurrence in occurrences
                if not any(
                    _same_recurrence_start(occurrence[0], exception_start)
                    for exception_start in suppressed
                )
            ]
        clipped = []
        for occurrence in occurrences:
            value = _clip_to_day(occurrence, target_day)
            if value is not None:
                clipped.append(value)
        if not clipped:
            continue
        result["event_count"] += 1
        if bool(event.get("all_day")):
            result["all_day"] = True
        result["intervals"].extend(clipped)
    return result


# Additive aliases keep the vacation API discoverable without changing the
# established break-event function or its return shape.
matching_vacation_events = vacation_events_for_day
vacation_for_day = vacation_events_for_day
vacation_summary_for_day = vacation_events_for_day
