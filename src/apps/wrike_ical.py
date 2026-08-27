"""Minimal Google Calendar private-iCal (.ics) feed reader.

Supports what the Wrike break feature needs: VEVENT DTSTART/DTEND with ``Z``
UTC conversion, all-day events (``VALUE=DATE``, ignored for break math),
weekly/daily RRULE expansion for recurring gym schedules, EXDATE exclusion and
keyword filtering. Standard-library only; TZID params besides UTC are treated
as local wall-clock times.
"""

from __future__ import annotations

import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_POLL_TIMEOUT_SEC = 15.0


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
            raw = resp.read()
    except Exception:
        return None
    try:
        return raw.decode("utf-8", errors="replace")
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


def parse_ics(text: str) -> list[dict]:
    events: list[dict] = []
    current: dict | None = None
    nested_component = ""
    for line in unfold_ics_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith("BEGIN:"):
            component = stripped[len("BEGIN:"):].strip().upper()
            if component == "VEVENT" and current is None:
                current = {"summary": "", "dtstart": None, "dtend": None,
                           "all_day": False, "rrule": {}, "exdates": []}
            elif current is not None:
                nested_component = component
            continue
        if upper.startswith("END:"):
            component = stripped[len("END:"):].strip().upper()
            if component == "VEVENT" and not nested_component:
                if current is not None and current.get("dtstart") is not None:
                    events.append(current)
                current = None
            elif nested_component and component == nested_component:
                nested_component = ""
            continue
        if current is None or nested_component:
            continue
        parsed = _split_property(stripped)
        if parsed is None:
            continue
        name, params, value = parsed
        escaped = (
            value.replace("\\n", " ").replace("\\N", " ")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
        )
        if name == "SUMMARY":
            current["summary"] = escaped.strip()
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
    return events


_ICS_BYDAY_MAP = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6,
}


def _rule_int(rule: dict[str, str], key: str, default: int | None) -> int | None:
    raw = rule.get(key, "")
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _parse_until_rule(rule: dict[str, str]) -> datetime | None:
    raw = rule.get("UNTIL", "")
    if not raw:
        return None
    parsed, _mode = _parse_ics_date_value(raw)
    return parsed


def _occurrence_end(event: dict, start_date) -> tuple[datetime, datetime] | None:
    dtstart = event.get("dtstart")
    dtend = event.get("dtend")
    if dtstart is None:
        return None
    occ_start = datetime.combine(start_date, dtstart.time())
    if not isinstance(dtend, datetime):
        return occ_start, None
    try:
        span_days = (dtend.date() - dtstart.date()).days
    except Exception:
        span_days = 0
    occ_end = datetime.combine(start_date + timedelta(days=max(0, span_days)), dtend.time())
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


def occurrences_for_day(event: dict, day) -> list[tuple[datetime, datetime]]:
    """Concrete intervals of ``event`` relevant to ``day`` (date/datetime).

    RRULE coverage: FREQ=DAILY/WEEKLY with INTERVAL, BYDAY (weekly), UNTIL,
    COUNT, EXDATE.  COUNT follows RFC order including EXDATE-skipped slots;
    the day scan is bounded to 900 iterations.  All-day events yield nothing.
    """
    dtstart = event.get("dtstart")
    if not isinstance(dtstart, datetime) or bool(event.get("all_day")):
        return []
    target_day = day.date() if isinstance(day, datetime) else day
    exdates = list(event.get("exdates") or [])
    result: list[tuple[datetime, datetime]] = []

    rule = event.get("rrule") or {}
    freq = str(rule.get("FREQ", "")).strip().upper()
    if freq not in ("DAILY", "WEEKLY"):
        pair = _occurrence_end(event, dtstart.date())
        if pair is None:
            return result
        start_dt, end_dt = pair
        if end_dt is None:
            hit = start_dt.date() == target_day
        else:
            hit = start_dt.date() <= target_day <= end_dt.date()
        if hit and not _excluded(exdates, start_dt):
            result.append(pair)
        return result

    interval = max(1, _rule_int(rule, "INTERVAL", 1) or 1)
    until_dt = _parse_until_rule(rule)
    until_day = until_dt.date() if isinstance(until_dt, datetime) else None
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
    scan_cap = min(
        max((target_day - anchor).days, 0) + 1,
        900,
    )
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
            exceeds_until = until_day is not None and current > until_day
            reaches_count = count_limit is not None and matched_consumed >= count_limit
            if exceeds_until or reaches_count:
                break
            matched_consumed += 1
            if not _excluded(exdates, cand_start):
                pair = _occurrence_end(event, current)
                if pair is not None:
                    start_dt, end_dt = pair
                    if end_dt is None:
                        hit = start_dt.date() == target_day
                    else:
                        hit = start_dt.date() <= target_day <= end_dt.date()
                    if hit:
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


