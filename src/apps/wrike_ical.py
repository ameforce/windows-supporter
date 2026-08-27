"""Minimal Google Calendar private-iCal (.ics) feed reader.

Supports the Wrike break and vacation features: VEVENT DTSTART/DTEND with
``Z`` UTC conversion, all-day events, daily/weekly recurrence, EXDATE
exclusion, keyword filtering, and calendar metadata. Standard-library only;
TZID params besides UTC are treated as local wall-clock times.
"""

from __future__ import annotations

import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_POLL_TIMEOUT_SEC = 15.0
MAX_ICAL_BYTES = 2 * 1024 * 1024


def read_calendar_response_text(
    response,
    limit_bytes: int = MAX_ICAL_BYTES,
) -> str | None:
    """Read one bounded iCal response and decode strict UTF-8 with BOM support."""
    try:
        limit = max(0, int(limit_bytes))
    except Exception:
        return None
    try:
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > limit:
            return None
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        raw = response.read(limit + 1)
    except Exception:
        return None
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None
    payload = bytes(raw)
    if len(payload) > limit:
        return None
    try:
        return payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return None


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
    """Parse calendar metadata and VEVENT values without external I/O."""
    result = {"calendar_name": "", "timezone": "", "events": []}
    stack: list[str] = []
    current: dict | None = None

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


def vacation_events_for_day(calendar, expected_calendar_name: str, day) -> dict:
    """Return matching vacation occurrences clipped to one local day.

    The calendar name must match exactly after normal iCal text decoding.
    Cancelled events and summaries without the normalized ``휴가`` substring
    are ignored. Recurrence exceptions suppress their original master slot;
    a non-cancelled moved exception contributes its replacement interval.
    Returned intervals intentionally remain unmerged because the work-time
    helper owns overlap and adjacency union semantics.
    """
    parsed = parse_calendar(calendar) if isinstance(calendar, str) else calendar
    if not isinstance(parsed, dict):
        parsed = {}
    calendar_name = str(parsed.get("calendar_name") or "").strip()
    timezone_name = str(parsed.get("timezone") or "").strip()
    result = {
        "calendar_name": calendar_name,
        "timezone": timezone_name,
        "calendar_matched": False,
        "all_day": False,
        "intervals": [],
        "event_count": 0,
    }
    expected = str(expected_calendar_name or "").strip()
    if not expected or calendar_name != expected:
        return result
    result["calendar_matched"] = True

    try:
        target_day = day.date() if isinstance(day, datetime) else day
        datetime.combine(target_day, datetime.min.time())
    except Exception:
        return result

    events = [event for event in (parsed.get("events") or []) if isinstance(event, dict)]
    master_summaries: dict[str, str] = {}
    exception_starts: dict[str, list[datetime]] = {}
    for event in events:
        uid = str(event.get("uid") or "").strip()
        recurrence_id = event.get("recurrence_id")
        if uid and isinstance(recurrence_id, datetime):
            exception_starts.setdefault(uid, []).append(recurrence_id)
        elif uid:
            summary = str(event.get("summary") or "").strip()
            if summary:
                master_summaries.setdefault(uid, summary)

    for event in events:
        if str(event.get("status") or "").strip().upper() == "CANCELLED":
            continue
        uid = str(event.get("uid") or "").strip()
        recurrence_id = event.get("recurrence_id")
        summary = str(event.get("summary") or "").strip()
        if not summary and uid:
            summary = master_summaries.get(uid, "")
        if "휴가" not in _normalized_summary(summary):
            continue
        try:
            occurrences = occurrences_for_day(event, target_day, include_all_day=True)
        except Exception:
            continue
        if uid and not isinstance(recurrence_id, datetime):
            suppressed = exception_starts.get(uid, [])
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
