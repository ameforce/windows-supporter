"""Immutable weekly Wrike timelog snapshots and a strict last-good cache.

The caller owns the cache location (normally below ``%APPDATA%``).  This
module never reads environment variables and persists only the normalized
weekly totals needed by the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import Enum
import json
import os
import tempfile
import threading
from collections.abc import Iterable
from typing import Any


CACHE_VERSION = 2
LEGACY_CACHE_VERSION = 1
SOURCE_SCOPE_ALL_MY_TIMELOGS = "all_my_timelogs"

_LEGACY_CACHE_FIELDS = {
    "version",
    "days",
    "display_name",
    "fetched_at",
    "source_scope",
}
_CACHE_FIELDS = _LEGACY_CACHE_FIELDS | {"account_fingerprint"}
_DAY_FIELDS = {"date", "recorded_minutes"}
_FINGERPRINT_LENGTH = 64


class TimelogSnapshotState(str, Enum):
    """UI state for one weekly timelog snapshot."""

    UNCONFIGURED = "unconfigured"
    LOADING = "loading"
    FRESH = "fresh"
    STALE = "stale"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TimelogDay:
    """Recorded time for one local calendar day."""

    date: date
    recorded_minutes: int

    def __post_init__(self) -> None:
        if type(self.date) is not date:
            raise TypeError("date must be a datetime.date value")
        if type(self.recorded_minutes) is not int or self.recorded_minutes < 0:
            raise ValueError("recorded_minutes must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class WeeklyTimelogSnapshot:
    """A deeply immutable Monday-through-Sunday UI snapshot.

    ``days=()`` means that no successful payload is available.  Consequently,
    a fresh all-zero week has ``total_recorded_minutes == 0``, while an error
    without last-good data has ``total_recorded_minutes is None``.
    """

    days: tuple[TimelogDay, ...]
    display_name: str
    fetched_at: datetime | None
    state: TimelogSnapshotState
    error_code: str | None
    source_scope: str
    generation: int
    partial: bool

    def __post_init__(self) -> None:
        if not isinstance(self.days, tuple):
            raise TypeError("days must be an immutable tuple")
        if type(self.display_name) is not str:
            raise TypeError("display_name must be a string")
        if not isinstance(self.state, TimelogSnapshotState):
            raise TypeError("state must be a TimelogSnapshotState")
        if self.source_scope != SOURCE_SCOPE_ALL_MY_TIMELOGS:
            raise ValueError("source_scope must be all_my_timelogs")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a nonnegative integer")
        if type(self.partial) is not bool:
            raise TypeError("partial must be a bool")
        if self.error_code is not None:
            if (
                type(self.error_code) is not str
                or not self.error_code
                or self.error_code.strip() != self.error_code
            ):
                raise ValueError("error_code must be a nonempty trimmed string or None")

        if self.fetched_at is not None:
            _require_naive_local_datetime(self.fetched_at, name="fetched_at")

        has_data = bool(self.days)
        if has_data:
            _validate_week(self.days)
            if self.fetched_at is None:
                raise ValueError("weekly data requires fetched_at")
        elif self.fetched_at is not None:
            raise ValueError("fetched_at requires weekly data")

        if self.partial and not has_data:
            raise ValueError("partial requires weekly data")

        if self.state in {
            TimelogSnapshotState.FRESH,
            TimelogSnapshotState.STALE,
        }:
            if not has_data:
                raise ValueError("fresh and stale snapshots require weekly data")
            if self.error_code is not None:
                raise ValueError("fresh and stale snapshots cannot have error_code")
        elif self.state is TimelogSnapshotState.ERROR:
            if self.error_code is None:
                raise ValueError("error snapshots require error_code")
        else:
            if self.error_code is not None:
                raise ValueError("only error snapshots may have error_code")

        if self.state is TimelogSnapshotState.UNCONFIGURED:
            if has_data or self.display_name or self.partial:
                raise ValueError("unconfigured snapshots cannot retain account data")

    @property
    def has_last_good_data(self) -> bool:
        """Whether this snapshot carries a successful weekly payload."""

        return bool(self.days)

    @property
    def total_recorded_minutes(self) -> int | None:
        """Return the weekly total, or ``None`` when no good payload exists."""

        if not self.days:
            return None
        return sum(item.recorded_minutes for item in self.days)

    def get_day(self, target_date: date) -> TimelogDay | None:
        """Look up one day without conflating a recorded zero with no data."""

        if type(target_date) is not date:
            raise TypeError("target_date must be a datetime.date value")
        for item in self.days:
            if item.date == target_date:
                return item
        return None

    def recorded_minutes_for(self, target_date: date) -> int | None:
        """Return one day's minutes, or ``None`` when that day is unavailable."""

        item = self.get_day(target_date)
        return None if item is None else item.recorded_minutes


def make_unconfigured_snapshot(*, generation: int = 0) -> WeeklyTimelogSnapshot:
    """Build an unconfigured snapshot with no cached account data."""

    return WeeklyTimelogSnapshot(
        days=(),
        display_name="",
        fetched_at=None,
        state=TimelogSnapshotState.UNCONFIGURED,
        error_code=None,
        source_scope=SOURCE_SCOPE_ALL_MY_TIMELOGS,
        generation=generation,
        partial=False,
    )


def make_loading_snapshot(
    *,
    generation: int,
    display_name: str = "",
) -> WeeklyTimelogSnapshot:
    """Build an initial loading snapshot when no last-good value exists."""

    return WeeklyTimelogSnapshot(
        days=(),
        display_name=display_name,
        fetched_at=None,
        state=TimelogSnapshotState.LOADING,
        error_code=None,
        source_scope=SOURCE_SCOPE_ALL_MY_TIMELOGS,
        generation=generation,
        partial=False,
    )


def make_error_snapshot(
    *,
    generation: int,
    error_code: str,
    display_name: str = "",
) -> WeeklyTimelogSnapshot:
    """Build an error snapshot when no last-good value exists."""

    return WeeklyTimelogSnapshot(
        days=(),
        display_name=display_name,
        fetched_at=None,
        state=TimelogSnapshotState.ERROR,
        error_code=error_code,
        source_scope=SOURCE_SCOPE_ALL_MY_TIMELOGS,
        generation=generation,
        partial=False,
    )


def make_fresh_snapshot(
    *,
    days: Iterable[TimelogDay],
    display_name: str,
    fetched_at: datetime,
    generation: int,
    partial: bool = False,
) -> WeeklyTimelogSnapshot:
    """Build and validate a fresh Monday-through-Sunday snapshot."""

    return WeeklyTimelogSnapshot(
        days=tuple(days),
        display_name=display_name,
        fetched_at=fetched_at,
        state=TimelogSnapshotState.FRESH,
        error_code=None,
        source_scope=SOURCE_SCOPE_ALL_MY_TIMELOGS,
        generation=generation,
        partial=partial,
    )


def loading_from_last_good(
    last_good: WeeklyTimelogSnapshot,
    *,
    generation: int,
) -> WeeklyTimelogSnapshot:
    """Start a refresh while retaining the last successful values."""

    _require_last_good(last_good)
    return replace(
        last_good,
        state=TimelogSnapshotState.LOADING,
        error_code=None,
        generation=generation,
    )


def error_from_last_good(
    last_good: WeeklyTimelogSnapshot,
    *,
    generation: int,
    error_code: str,
) -> WeeklyTimelogSnapshot:
    """Report a failed refresh while retaining the last successful values."""

    _require_last_good(last_good)
    return replace(
        last_good,
        state=TimelogSnapshotState.ERROR,
        error_code=error_code,
        generation=generation,
    )


def apply_stale_threshold(
    snapshot: WeeklyTimelogSnapshot,
    *,
    now: datetime,
    stale_after: timedelta,
) -> WeeklyTimelogSnapshot:
    """Classify cached last-good data as fresh or stale for ``now``.

    A value becomes stale at the threshold boundary.  Future wall-clock values
    remain fresh so a small local clock correction does not discard the cache.
    """

    _require_last_good(snapshot)
    _require_naive_local_datetime(now, name="now")
    if not isinstance(stale_after, timedelta) or stale_after < timedelta(0):
        raise ValueError("stale_after must be a nonnegative timedelta")
    assert snapshot.fetched_at is not None
    target_state = (
        TimelogSnapshotState.STALE
        if now - snapshot.fetched_at >= stale_after
        else TimelogSnapshotState.FRESH
    )
    return replace(snapshot, state=target_state, error_code=None)


class CacheWriteBlockedError(RuntimeError):
    """Raised when replacing an unreadable or unsupported cache is unsafe."""


class WrikeTimelogSnapshotStore:
    """Strict, atomic persistence for complete last-good snapshots.

    ``path`` is supplied by the caller and may point below ``%APPDATA%``.  A
    malformed, unreadable, or unsupported existing file is ignored by
    :meth:`load`, but permanently blocks writes on this store instance so the
    original bytes cannot be destroyed.  Repair the file and create a new
    store instance to resume writes.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        try:
            path_value = os.fspath(path)
        except TypeError as exc:
            raise ValueError("cache path must be a path-like string") from exc
        if type(path_value) is not str or not path_value.strip():
            raise ValueError("cache path must be a nonempty string")
        self._path = os.path.abspath(path_value)
        self._lock = threading.RLock()
        self._write_blocked_reason: str | None = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def write_blocked(self) -> bool:
        with self._lock:
            return self._write_blocked_reason is not None

    @property
    def write_blocked_reason(self) -> str | None:
        with self._lock:
            return self._write_blocked_reason

    def load(
        self,
        *,
        expected_account_fingerprint: str,
        generation: int = 0,
    ) -> WeeklyTimelogSnapshot | None:
        """Load only a cache atomically bound to the expected account.

        A valid legacy v1 cache is intentionally treated as unbound and
        ignored, but remains replaceable by a fresh v2 cache.
        """

        _require_generation(generation)
        expected_fingerprint = _require_account_fingerprint(
            expected_account_fingerprint
        )
        with self._lock:
            if self._write_blocked_reason is not None:
                return None
            try:
                return self._read_snapshot(
                    generation=generation,
                    expected_account_fingerprint=expected_fingerprint,
                )
            except FileNotFoundError:
                return None
            except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
                self._block_writes(
                    "existing cache is unreadable, malformed, or has an unsupported version"
                )
                _ = exc
                return None

    def load_with_freshness(
        self,
        *,
        expected_account_fingerprint: str,
        now: datetime,
        stale_after: timedelta,
        generation: int = 0,
    ) -> WeeklyTimelogSnapshot | None:
        """Load a matching account cache and apply an injected age threshold."""

        _require_naive_local_datetime(now, name="now")
        if not isinstance(stale_after, timedelta) or stale_after < timedelta(0):
            raise ValueError("stale_after must be a nonnegative timedelta")
        snapshot = self.load(
            expected_account_fingerprint=expected_account_fingerprint,
            generation=generation,
        )
        if snapshot is None:
            return None
        return apply_stale_threshold(snapshot, now=now, stale_after=stale_after)

    def save(
        self,
        snapshot: WeeklyTimelogSnapshot,
        *,
        account_fingerprint: str,
    ) -> None:
        """Atomically persist one complete account-bound last-good snapshot.

        Partial snapshots are deliberately excluded because ``partial`` is not
        part of the privacy-minimal cache schema and therefore cannot be
        restored faithfully.
        """

        if not isinstance(snapshot, WeeklyTimelogSnapshot):
            raise TypeError("snapshot must be a WeeklyTimelogSnapshot")
        if snapshot.state not in {
            TimelogSnapshotState.FRESH,
            TimelogSnapshotState.STALE,
        }:
            raise ValueError("only fresh or stale last-good snapshots can be cached")
        if not snapshot.has_last_good_data:
            raise ValueError("a cached snapshot must contain weekly data")
        if snapshot.partial:
            raise ValueError("partial snapshots are not persisted as last-good data")
        fingerprint = _require_account_fingerprint(account_fingerprint)

        payload = _encode_snapshot(snapshot, account_fingerprint=fingerprint)
        with self._lock:
            self._ensure_existing_cache_is_safe()
            parent = os.path.dirname(self._path) or os.curdir
            os.makedirs(parent, exist_ok=True)
            fd = -1
            temp_path: str | None = None
            try:
                prefix = f".{os.path.basename(self._path)}."
                fd, temp_path = tempfile.mkstemp(
                    prefix=prefix,
                    suffix=".tmp",
                    dir=parent,
                )
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    fd = -1
                    json.dump(
                        payload,
                        stream,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                        allow_nan=False,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, self._path)
                temp_path = None
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

    def _block_writes(self, reason: str) -> None:
        if self._write_blocked_reason is None:
            self._write_blocked_reason = reason

    def _ensure_existing_cache_is_safe(self) -> None:
        if self._write_blocked_reason is not None:
            raise CacheWriteBlockedError(self._write_blocked_reason)
        try:
            self._read_snapshot(
                generation=0,
                expected_account_fingerprint=None,
            )
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
            self._block_writes(
                "refusing to replace an unreadable, malformed, or unsupported cache"
            )
            raise CacheWriteBlockedError(self._write_blocked_reason) from exc

    def _read_snapshot(
        self,
        *,
        generation: int,
        expected_account_fingerprint: str | None,
    ) -> WeeklyTimelogSnapshot | None:
        with open(self._path, "r", encoding="utf-8", newline="") as stream:
            payload = json.load(
                stream,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        return _decode_snapshot(
            payload,
            generation=generation,
            expected_account_fingerprint=expected_account_fingerprint,
        )


def _require_naive_local_datetime(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not None:
        raise ValueError(f"{name} must be a naive local datetime")
    return value


def _require_generation(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("generation must be a nonnegative integer")
    return value


def _require_account_fingerprint(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != _FINGERPRINT_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("account_fingerprint must be a lowercase SHA-256 hex string")
    return value


def _validate_week(days: tuple[TimelogDay, ...]) -> None:
    if len(days) != 7:
        raise ValueError("days must contain exactly seven entries")
    if any(not isinstance(item, TimelogDay) for item in days):
        raise TypeError("days must contain only TimelogDay values")
    start = days[0].date
    if start.weekday() != 0:
        raise ValueError("the first day must be Monday")
    expected_dates = tuple(start + timedelta(days=index) for index in range(7))
    actual_dates = tuple(item.date for item in days)
    if actual_dates != expected_dates or len(set(actual_dates)) != 7:
        raise ValueError("days must be unique and consecutive from Monday to Sunday")


def _require_last_good(snapshot: WeeklyTimelogSnapshot) -> None:
    if not isinstance(snapshot, WeeklyTimelogSnapshot):
        raise TypeError("last_good must be a WeeklyTimelogSnapshot")
    if snapshot.state not in {
        TimelogSnapshotState.FRESH,
        TimelogSnapshotState.STALE,
    } or not snapshot.has_last_good_data:
        raise ValueError("last_good must be a fresh or stale successful snapshot")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_strict_date(value: object) -> date:
    if type(value) is not str:
        raise ValueError("cached date must be an ISO string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("cached date is not valid ISO") from exc
    if parsed.isoformat() != value:
        raise ValueError("cached date must use canonical YYYY-MM-DD ISO format")
    return parsed


def _parse_strict_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("cached fetched_at must be an ISO string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("cached fetched_at is not valid ISO") from exc
    _require_naive_local_datetime(parsed, name="cached fetched_at")
    if parsed.isoformat() != value:
        raise ValueError("cached fetched_at must use canonical ISO format")
    return parsed


def _encode_snapshot(
    snapshot: WeeklyTimelogSnapshot,
    *,
    account_fingerprint: str,
) -> dict[str, Any]:
    assert snapshot.fetched_at is not None
    return {
        "version": CACHE_VERSION,
        "account_fingerprint": account_fingerprint,
        "days": [
            {
                "date": item.date.isoformat(),
                "recorded_minutes": item.recorded_minutes,
            }
            for item in snapshot.days
        ],
        "display_name": snapshot.display_name,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "source_scope": snapshot.source_scope,
    }


def _decode_snapshot(
    payload: object,
    *,
    generation: int,
    expected_account_fingerprint: str | None,
) -> WeeklyTimelogSnapshot | None:
    if type(payload) is not dict:
        raise ValueError("cache must be a JSON object")
    version = payload.get("version")
    if type(version) is not int:
        raise ValueError("cache version must be an integer")
    if version == LEGACY_CACHE_VERSION:
        if set(payload) != _LEGACY_CACHE_FIELDS:
            raise ValueError("legacy cache must contain exactly the supported fields")
        account_fingerprint = None
    elif version == CACHE_VERSION:
        if set(payload) != _CACHE_FIELDS:
            raise ValueError("cache must contain exactly the supported fields")
        account_fingerprint = _require_account_fingerprint(
            payload["account_fingerprint"]
        )
    else:
        raise ValueError("unsupported cache version")
    if type(payload["display_name"]) is not str:
        raise ValueError("cached display_name must be a string")
    if payload["source_scope"] != SOURCE_SCOPE_ALL_MY_TIMELOGS:
        raise ValueError("unsupported cached source_scope")
    raw_days = payload["days"]
    if type(raw_days) is not list:
        raise ValueError("cached days must be an array")

    days: list[TimelogDay] = []
    for raw_day in raw_days:
        if type(raw_day) is not dict or set(raw_day) != _DAY_FIELDS:
            raise ValueError("cached day must contain exactly date and recorded_minutes")
        minutes = raw_day["recorded_minutes"]
        if type(minutes) is not int or minutes < 0:
            raise ValueError("cached recorded_minutes must be a nonnegative integer")
        days.append(
            TimelogDay(
                date=_parse_strict_date(raw_day["date"]),
                recorded_minutes=minutes,
            )
        )

    snapshot = make_fresh_snapshot(
        days=days,
        display_name=payload["display_name"],
        fetched_at=_parse_strict_datetime(payload["fetched_at"]),
        generation=generation,
        partial=False,
    )
    if version == LEGACY_CACHE_VERSION:
        return None
    if (
        expected_account_fingerprint is not None
        and account_fingerprint != expected_account_fingerprint
    ):
        return None
    return snapshot
