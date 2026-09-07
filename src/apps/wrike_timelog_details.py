"""Ephemeral, privacy-safe detail rows for the Wrike weekly quick panel.

This module deliberately has no persistence support.  The on-disk timelog
snapshot is a daily aggregate and must stay that way; task titles and comments
only exist for the lifetime of the authenticated application session.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_DATE_KEY = re.compile(r"\d{4}-\d{2}-\d{2}")
_TITLE_STATES = frozenset({"ready", "loading", "missing", "unavailable"})
_DAY_STATES = frozenset({"available", "loading", "unavailable"})


def _require_date_key(value: str) -> None:
    if type(value) is not str or _DATE_KEY.fullmatch(value) is None:
        raise ValueError("date_key must use YYYY-MM-DD format")


@dataclass(frozen=True, slots=True)
class TimelogDetailRow:
    """One authoritative timelog entry, held only in process memory."""

    date_key: str
    timelog_id: str
    task_id: str
    minutes: int
    comment: str
    task_title: str = ""
    title_state: str = "loading"

    def __post_init__(self) -> None:
        _require_date_key(self.date_key)
        if type(self.timelog_id) is not str or not self.timelog_id.strip():
            raise ValueError("timelog_id must be nonempty")
        if type(self.task_id) is not str:
            raise TypeError("task_id must be a string")
        if type(self.minutes) is not int or self.minutes < 0:
            raise ValueError("minutes must be a non-negative int")
        if type(self.comment) is not str or type(self.task_title) is not str:
            raise TypeError("comment and task_title must be strings")
        if self.title_state not in _TITLE_STATES:
            raise ValueError("unsupported title_state")
        if self.task_title and self.title_state != "ready":
            raise ValueError("only a ready title may include task_title")

    @property
    def ticket_text(self) -> str:
        if self.task_title:
            return self.task_title
        if not self.task_id:
            return "연결된 티켓 없음"
        if self.title_state == "loading":
            return "티켓 제목 조회 중"
        if self.title_state == "missing":
            return "삭제되었거나 접근할 수 없는 티켓"
        return "티켓 제목을 확인할 수 없음"


@dataclass(frozen=True, slots=True)
class TimelogDayDetails:
    """The authoritative details for a single displayed day."""

    date_key: str
    state: str
    total_minutes: int
    rows: tuple[TimelogDetailRow, ...] = ()

    def __post_init__(self) -> None:
        _require_date_key(self.date_key)
        if self.state not in _DAY_STATES:
            raise ValueError("unsupported detail state")
        if type(self.total_minutes) is not int or self.total_minutes < 0:
            raise ValueError("total_minutes must be a non-negative int")
        if not isinstance(self.rows, tuple):
            raise TypeError("rows must be an immutable tuple")
        if any(not isinstance(row, TimelogDetailRow) for row in self.rows):
            raise TypeError("rows must contain only TimelogDetailRow values")
        if any(row.date_key != self.date_key for row in self.rows):
            raise ValueError("rows must belong to the detail date")
        if self.state == "available" and sum(row.minutes for row in self.rows) != self.total_minutes:
            raise ValueError("available detail total must match its rows")


def unavailable_day_details(date_key: str) -> TimelogDayDetails:
    return TimelogDayDetails(date_key, "unavailable", 0)
