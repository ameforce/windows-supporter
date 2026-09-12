"""Atomic per-day state for the local overtime prompt and timer."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timedelta


STATE_VERSION = 2
_SUPPORTED_VERSIONS = frozenset({1, STATE_VERSION})
_MAX_PAUSED_SECONDS = 3 * 24 * 3600
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$"
)
_STATUSES = frozenset({"pending", "active", "skipped", "completed"})
_V1_ENTRY_FIELDS = frozenset(
    {
        "status",
        "detected_at",
        "scheduled_quit",
        "assigned_minutes",
        "started_at",
        "ended_at",
    }
)


def _parse_iso_value(value) -> datetime | None:
    if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def net_elapsed_seconds(entry, at) -> int:
    """Return worked seconds excluding finished and open pause intervals."""

    started = _parse_iso_value(entry.get("started_at") if isinstance(entry, dict) else None)
    if started is None or not isinstance(at, datetime) or at.tzinfo is not None:
        return 0
    ended = _parse_iso_value(entry.get("ended_at"))
    effective_end = ended if ended is not None else at
    paused_seconds = 0
    try:
        paused_seconds = max(0, int(entry.get("paused_seconds") or 0))
    except Exception:
        paused_seconds = 0
    paused_at = _parse_iso_value(entry.get("paused_at"))
    open_pause = 0
    if paused_at is not None and effective_end > paused_at:
        open_pause = int((effective_end - paused_at).total_seconds())
    return max(0, int((effective_end - started).total_seconds()) - paused_seconds - open_pause)


def paused_total_seconds(entry, at) -> int:
    """Return accumulated pause seconds including a still-open pause."""

    paused_seconds = 0
    try:
        paused_seconds = max(0, int(entry.get("paused_seconds") or 0))
    except Exception:
        paused_seconds = 0
    paused_at = _parse_iso_value(entry.get("paused_at") if isinstance(entry, dict) else None)
    ended = _parse_iso_value(entry.get("ended_at") if isinstance(entry, dict) else None)
    effective_end = ended if ended is not None else at
    if paused_at is not None and isinstance(effective_end, datetime) and effective_end > paused_at:
        paused_seconds += int((effective_end - paused_at).total_seconds())
    return paused_seconds


class OvertimeStateStore:
    """Persist one bounded overtime state entry per local calendar day."""

    def __init__(self, path, now_provider=None) -> None:
        try:
            path_value = os.fspath(path)
        except Exception as exc:
            raise ValueError("초과근무 상태 파일 경로가 올바르지 않습니다.") from exc
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("초과근무 상태 파일 경로가 올바르지 않습니다.")
        self._path = os.path.abspath(path_value)
        self._now_provider = now_provider if now_provider is not None else datetime.now
        if not callable(self._now_provider):
            raise ValueError("현재 시간 제공자가 올바르지 않습니다.")
        self._lock = threading.RLock()
        self._state = self._empty_state()
        self._write_blocked_reason: str | None = None
        self._load()

    @property
    def path(self) -> str:
        return self._path

    @staticmethod
    def _empty_state() -> dict:
        return {"state_version": STATE_VERSION, "days": {}}

    @staticmethod
    def _parse_day(value) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                raise ValueError("날짜는 시간대 정보가 없는 값이어야 합니다.")
            value = value.date()
        if isinstance(value, date):
            return value.isoformat()
        if not isinstance(value, str) or not _DAY_RE.fullmatch(value):
            raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except Exception as exc:
            raise ValueError("날짜가 올바르지 않습니다.") from exc
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError("날짜가 올바르지 않습니다.")
        return value

    def _now(self, value=None) -> datetime:
        resolved = self._now_provider() if value is None else value
        if not isinstance(resolved, datetime) or resolved.tzinfo is not None:
            raise ValueError("시간은 시간대 정보가 없는 datetime 값이어야 합니다.")
        return resolved

    @staticmethod
    def _parse_iso(value) -> datetime | None:
        return _parse_iso_value(value)

    @staticmethod
    def _format_iso(value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    @staticmethod
    def _validate_minutes(value) -> int:
        if isinstance(value, bool):
            raise ValueError("초과근무 배정 시간은 정수여야 합니다.")
        try:
            parsed = int(value)
        except Exception as exc:
            raise ValueError("초과근무 배정 시간이 올바르지 않습니다.") from exc
        if parsed < 0 or parsed > 1440:
            raise ValueError("초과근무 배정 시간은 0분 이상 1440분 이하여야 합니다.")
        return parsed

    @staticmethod
    def _validate_paused_seconds(value) -> int:
        if isinstance(value, bool):
            raise ValueError("초과근무 일시정지 누적은 정수여야 합니다.")
        try:
            parsed = int(value)
        except Exception as exc:
            raise ValueError("초과근무 일시정지 누적이 올바르지 않습니다.") from exc
        if parsed < 0 or parsed > _MAX_PAUSED_SECONDS:
            raise ValueError("초과근무 일시정지 누적이 올바르지 않습니다.")
        return parsed

    @classmethod
    def _day_entry(
        cls,
        *,
        status: str,
        detected_at: datetime,
        scheduled_quit: datetime,
        assigned_minutes: int,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        paused_at: datetime | None = None,
        paused_seconds: int = 0,
    ) -> dict:
        return {
            "status": status,
            "detected_at": cls._format_iso(detected_at),
            "scheduled_quit": cls._format_iso(scheduled_quit),
            "assigned_minutes": int(assigned_minutes),
            "started_at": cls._format_iso(started_at) if started_at else None,
            "ended_at": cls._format_iso(ended_at) if ended_at else None,
            "paused_at": cls._format_iso(paused_at) if paused_at else None,
            "paused_seconds": int(paused_seconds),
        }

    @classmethod
    def _decode_entry(cls, key: str, raw) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("초과근무 날짜별 상태가 객체가 아닙니다.")
        expected = {
            "status",
            "detected_at",
            "scheduled_quit",
            "assigned_minutes",
            "started_at",
            "ended_at",
            "paused_at",
            "paused_seconds",
        }
        if set(raw) != expected:
            raise ValueError("초과근무 날짜별 상태 필드가 올바르지 않습니다.")
        status = raw.get("status")
        if status not in _STATUSES:
            raise ValueError("초과근무 상태가 올바르지 않습니다.")
        detected = cls._parse_iso(raw.get("detected_at"))
        scheduled = cls._parse_iso(raw.get("scheduled_quit"))
        started = cls._parse_iso(raw.get("started_at")) if raw.get("started_at") else None
        ended = cls._parse_iso(raw.get("ended_at")) if raw.get("ended_at") else None
        paused_at = cls._parse_iso(raw.get("paused_at")) if raw.get("paused_at") else None
        paused_seconds = cls._validate_paused_seconds(raw.get("paused_seconds"))
        if detected is None or scheduled is None:
            raise ValueError("초과근무 기준 시간이 올바르지 않습니다.")
        if detected.date().isoformat() != key:
            raise ValueError("초과근무 감지 날짜가 상태 키와 다릅니다.")
        day_date = datetime.strptime(key, "%Y-%m-%d").date()
        if scheduled.date() not in {day_date, day_date + timedelta(days=1)}:
            raise ValueError("초과근무 퇴근 예정일이 올바르지 않습니다.")
        assigned = cls._validate_minutes(raw.get("assigned_minutes"))
        if status == "active" and started is None:
            raise ValueError("활성 초과근무에는 시작 시간이 필요합니다.")
        if status == "completed" and (started is None or ended is None):
            raise ValueError("완료된 초과근무에는 시작·종료 시간이 필요합니다.")
        if status in {"pending", "skipped"} and (started is not None or ended is not None):
            raise ValueError("대기·건너뛴 초과근무에는 시작·종료 시간이 없어야 합니다.")
        if status in {"pending", "skipped"} and paused_seconds:
            raise ValueError("대기·건너뛴 초과근무에는 일시정지 누적이 없어야 합니다.")
        if status != "active" and paused_at is not None:
            raise ValueError("진행 중이 아닌 초과근무에는 열린 일시정지가 없어야 합니다.")
        if started is not None and started.date().isoformat() != key:
            raise ValueError("초과근무 시작 날짜가 올바르지 않습니다.")
        if ended is not None and ended < started:
            raise ValueError("초과근무 종료 시간이 시작보다 빠릅니다.")
        if paused_at is not None and started is not None and paused_at < started:
            raise ValueError("초과근무 일시정지 시작이 시작보다 빠릅니다.")
        return cls._day_entry(
            status=status,
            detected_at=detected,
            scheduled_quit=scheduled,
            assigned_minutes=assigned,
            started_at=started,
            ended_at=ended,
            paused_at=paused_at,
            paused_seconds=paused_seconds,
        )

    @staticmethod
    def _reject_duplicate_json_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"중복 JSON 키는 허용되지 않습니다: {key!r}")
            value[key] = item
        return value

    @staticmethod
    def _reject_json_constant(value: str):
        raise ValueError(f"JSON 상수는 허용되지 않습니다: {value!r}")

    def _decode_state(self, raw) -> dict:
        if not isinstance(raw, dict) or set(raw) != {"state_version", "days"}:
            raise ValueError("초과근무 상태 최상위 필드가 올바르지 않습니다.")
        version = raw.get("state_version")
        if version not in _SUPPORTED_VERSIONS:
            raise ValueError("지원하지 않는 초과근무 상태 버전입니다.")
        days = raw.get("days")
        if not isinstance(days, dict):
            raise ValueError("초과근무 상태 days 값이 객체가 아닙니다.")
        decoded = self._empty_state()
        normalized_raw = {"state_version": STATE_VERSION, "days": {}}
        for raw_key, raw_value in days.items():
            key = self._parse_day(raw_key)
            upgraded = raw_value
            if version == 1:
                if not isinstance(upgraded, dict):
                    raise ValueError("초과근무 날짜별 상태가 객체가 아닙니다.")
                if set(upgraded) != _V1_ENTRY_FIELDS:
                    raise ValueError("초과근무 날짜별 상태 필드가 올바르지 않습니다.")
                # v1 files predate pause support; decode them with v2 defaults.
                upgraded = {
                    **raw_value,
                    "paused_at": None,
                    "paused_seconds": 0,
                }
            normalized_raw["days"][key] = upgraded
            decoded["days"][key] = self._decode_entry(key, upgraded)
        if decoded != normalized_raw:
            raise ValueError("초과근무 상태 파일이 정규 구조가 아닙니다.")
        return decoded

    def _load(self) -> None:
        with self._lock:
            if not os.path.isfile(self._path):
                return
            try:
                with open(self._path, "r", encoding="utf-8") as fp:
                    raw = json.load(
                        fp,
                        object_pairs_hook=self._reject_duplicate_json_keys,
                        parse_constant=self._reject_json_constant,
                    )
                self._state = self._decode_state(raw)
            except Exception:
                self._state = self._empty_state()
                self._write_blocked_reason = (
                    "기존 초과근무 상태 파일을 읽지 못해 덮어쓰기를 차단했습니다."
                )

    def _save_locked(self) -> bool:
        if self._write_blocked_reason:
            return False
        parent = os.path.dirname(self._path) or os.curdir
        temp_path = None
        fd = -1
        try:
            os.makedirs(parent, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(self._path)}.",
                suffix=".tmp",
                dir=parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
                fd = -1
                json.dump(self._state, fp, ensure_ascii=False, indent=2, sort_keys=True)
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temp_path, self._path)
            temp_path = None
            return True
        except Exception:
            return False
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def get(self, day=None) -> dict | None:
        key = self._parse_day(day if day is not None else self._now())
        with self._lock:
            value = self._state["days"].get(key)
            return copy.deepcopy(value) if value is not None else None

    def _set(self, key: str, value: dict) -> tuple[bool, str | None]:
        with self._lock:
            if self._write_blocked_reason:
                return False, self._write_blocked_reason
            previous = self._state["days"].get(key)
            self._state["days"][key] = value
            if self._save_locked():
                return True, None
            if previous is None:
                self._state["days"].pop(key, None)
            else:
                self._state["days"][key] = previous
            return False, "초과근무 상태를 저장하지 못했습니다."

    def set_pending(self, day, detected_at, scheduled_quit, assigned_minutes=0):
        key = self._parse_day(day)
        detected = self._now(detected_at)
        scheduled = self._now(scheduled_quit)
        if detected.date().isoformat() != key:
            raise ValueError("초과근무 감지 날짜가 상태 키와 다릅니다.")
        return self._set(
            key,
            self._day_entry(
                status="pending",
                detected_at=detected,
                scheduled_quit=scheduled,
                assigned_minutes=self._validate_minutes(assigned_minutes),
            ),
        )

    def start(self, day=None, started_at=None):
        key = self._parse_day(day if day is not None else self._now())
        started = self._now(started_at)
        with self._lock:
            current = copy.deepcopy(self._state["days"].get(key))
        if not current or current.get("status") != "pending":
            return False, "초과근무 측정 대기 상태가 없습니다."
        value = self._day_entry(
            status="active",
            detected_at=self._parse_iso(current["detected_at"]),
            scheduled_quit=self._parse_iso(current["scheduled_quit"]),
            assigned_minutes=int(current["assigned_minutes"]),
            started_at=started,
        )
        return self._set(key, value)

    def pause(self, day=None, paused_at=None):
        key = self._parse_day(day if day is not None else self._now())
        at = self._now(paused_at)
        with self._lock:
            current = copy.deepcopy(self._state["days"].get(key))
        if not current or current.get("status") != "active":
            return False, "진행 중인 초과근무가 없습니다."
        if current.get("paused_at"):
            return False, "이미 일시정지 중입니다."
        started = self._parse_iso(current.get("started_at"))
        if started is None or at < started:
            return False, "초과근무 일시정지 시간이 올바르지 않습니다."
        value = self._day_entry(
            status="active",
            detected_at=self._parse_iso(current["detected_at"]),
            scheduled_quit=self._parse_iso(current["scheduled_quit"]),
            assigned_minutes=int(current["assigned_minutes"]),
            started_at=started,
            paused_at=at,
            paused_seconds=int(current.get("paused_seconds") or 0),
        )
        return self._set(key, value)

    def resume(self, day=None, resumed_at=None):
        key = self._parse_day(day if day is not None else self._now())
        at = self._now(resumed_at)
        with self._lock:
            current = copy.deepcopy(self._state["days"].get(key))
        if not current or current.get("status") != "active":
            return False, "진행 중인 초과근무가 없습니다."
        paused_at = self._parse_iso(current.get("paused_at"))
        if paused_at is None:
            return False, "일시정지 중인 초과근무가 없습니다."
        if at < paused_at:
            return False, "초과근무 재개 시간이 올바르지 않습니다."
        paused_seconds = int(current.get("paused_seconds") or 0) + int(
            (at - paused_at).total_seconds()
        )
        value = self._day_entry(
            status="active",
            detected_at=self._parse_iso(current["detected_at"]),
            scheduled_quit=self._parse_iso(current["scheduled_quit"]),
            assigned_minutes=int(current["assigned_minutes"]),
            started_at=self._parse_iso(current["started_at"]),
            paused_seconds=paused_seconds,
        )
        return self._set(key, value)

    def update_started(self, day=None, started_at=None):
        key = self._parse_day(day if day is not None else self._now())
        started = self._now(started_at)
        now = self._now()
        with self._lock:
            current = copy.deepcopy(self._state["days"].get(key))
        if not current or current.get("status") != "active":
            return False, "진행 중인 초과근무가 없습니다."
        if started.date().isoformat() != key:
            return False, "초과근무 시작 날짜가 올바르지 않습니다."
        paused_at = self._parse_iso(current.get("paused_at"))
        upper_bound = paused_at if paused_at is not None and paused_at < now else now
        if started > upper_bound:
            return False, "초과근무 시작 시간이 올바르지 않습니다."
        value = self._day_entry(
            status="active",
            detected_at=self._parse_iso(current["detected_at"]),
            scheduled_quit=self._parse_iso(current["scheduled_quit"]),
            assigned_minutes=int(current["assigned_minutes"]),
            started_at=started,
            paused_at=paused_at,
            paused_seconds=int(current.get("paused_seconds") or 0),
        )
        return self._set(key, value)

    def skip(self, day=None):
        key = self._parse_day(day if day is not None else self._now())
        with self._lock:
            current = copy.deepcopy(self._state["days"].get(key))
        if not current:
            return False, "초과근무 측정 대기 상태가 없습니다."
        value = self._day_entry(
            status="skipped",
            detected_at=self._parse_iso(current["detected_at"]),
            scheduled_quit=self._parse_iso(current["scheduled_quit"]),
            assigned_minutes=int(current["assigned_minutes"]),
        )
        return self._set(key, value)

    def complete(self, day=None, ended_at=None):
        key = self._parse_day(day if day is not None else self._now())
        ended = self._now(ended_at)
        with self._lock:
            current = copy.deepcopy(self._state["days"].get(key))
        if not current or current.get("status") != "active":
            return False, "진행 중인 초과근무가 없습니다."
        started = self._parse_iso(current["started_at"])
        if started is None or ended < started:
            return False, "초과근무 종료 시간이 올바르지 않습니다."
        paused_seconds = int(current.get("paused_seconds") or 0)
        paused_at = self._parse_iso(current.get("paused_at"))
        if paused_at is not None and ended > paused_at:
            paused_seconds += int((ended - paused_at).total_seconds())
        value = self._day_entry(
            status="completed",
            detected_at=self._parse_iso(current["detected_at"]),
            scheduled_quit=self._parse_iso(current["scheduled_quit"]),
            assigned_minutes=int(current["assigned_minutes"]),
            started_at=started,
            ended_at=ended,
            paused_seconds=min(paused_seconds, _MAX_PAUSED_SECONDS),
        )
        return self._set(key, value)
