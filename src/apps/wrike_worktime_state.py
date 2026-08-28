"""Thread-safe persisted state for per-day Wrike work-time plans.

The store owns only explicit day plans, manual breaks, and optional activity
prompt state. Legacy ``first_seen_by_date`` values are tolerated while loading
but never become a manual clock-in value.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timedelta

from src.apps.wrike_worktime import BreakInterval, format_minutes, union_datetime_intervals


STATE_VERSION = 3
DEFAULT_TARGET_MINUTES = 8 * 60

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?$"
)
_ISO_SECONDS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$"
)
_ACTIVITY_PROMPT_STATUSES = frozenset({"pending", "snoozed", "skipped"})


class WorktimeStateStore:
    """Persist work-time state with atomic replacement and an ``RLock``."""

    def __init__(
        self,
        path,
        default_target_minutes: int = DEFAULT_TARGET_MINUTES,
        now_provider=None,
    ) -> None:
        try:
            path_value = os.fspath(path)
        except Exception as exc:
            raise ValueError("상태 파일 경로가 올바르지 않습니다.") from exc
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("상태 파일 경로가 올바르지 않습니다.")
        self._path = os.path.abspath(path_value)
        self._default_target_minutes = self._validate_target(default_target_minutes)
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

    @property
    def default_target_minutes(self) -> int:
        return int(self._default_target_minutes)

    @staticmethod
    def _empty_state() -> dict:
        return {"state_version": STATE_VERSION, "days": {}}

    @staticmethod
    def _empty_day() -> dict:
        return {
            "manual_breaks": [],
            "active_break_started_at": None,
        }

    @staticmethod
    def _validate_target(value) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("목표 시간은 0분 이상 1440분 이하의 정수여야 합니다.")
        if value < 0 or value > 1440:
            raise ValueError("목표 시간은 0분 이상 1440분 이하여야 합니다.")
        return int(value)

    def _now(self, value=None) -> datetime:
        try:
            resolved = self._now_provider() if value is None else value
        except Exception as exc:
            raise ValueError("현재 시간을 확인할 수 없습니다.") from exc
        if not isinstance(resolved, datetime) or resolved.tzinfo is not None:
            raise ValueError("시간은 시간대 정보가 없는 datetime 값이어야 합니다.")
        return resolved

    def _day_key(self, value=None, now: datetime | None = None) -> str:
        if value is None:
            resolved = now if now is not None else self._now()
            return resolved.strftime("%Y-%m-%d")
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                raise ValueError("날짜는 시간대 정보가 없는 값이어야 합니다.")
            value = value.date()
        if isinstance(value, date):
            return value.strftime("%Y-%m-%d")
        if not isinstance(value, str) or not _DAY_RE.fullmatch(value):
            raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except Exception as exc:
            raise ValueError("날짜가 올바르지 않습니다.") from exc
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError("날짜가 올바르지 않습니다.")
        return value

    @staticmethod
    def _clock_value(value) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not _CLOCK_RE.fullmatch(value):
            raise ValueError("출근 시간은 HH:MM 형식이어야 합니다.")
        return value

    @staticmethod
    def _parse_iso(value) -> datetime | None:
        if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except Exception:
            return None
        if parsed.tzinfo is not None:
            return None
        return parsed

    @staticmethod
    def _parse_iso_seconds(value) -> datetime | None:
        if not isinstance(value, str) or not _ISO_SECONDS_RE.fullmatch(value):
            return None
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None
        if parsed.strftime("%Y-%m-%dT%H:%M:%S") != value:
            return None
        return parsed

    @staticmethod
    def _format_iso(value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    def _prompt_time_value(
        self,
        value,
        field_label: str,
        *,
        use_now: bool = False,
    ) -> tuple[datetime, str]:
        if value is None and use_now:
            value = self._now()
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                raise ValueError(f"{field_label}은 시간대 정보가 없어야 합니다.")
            normalized = self._format_iso(value)
            parsed = self._parse_iso_seconds(normalized)
        elif isinstance(value, str):
            normalized = value
            parsed = self._parse_iso_seconds(value)
        else:
            parsed = None
            normalized = ""
        if parsed is None:
            raise ValueError(f"{field_label}은 초 단위 ISO 형식이어야 합니다.")
        return parsed, normalized

    @staticmethod
    def _split_span(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        if end <= start:
            return []
        result: list[tuple[datetime, datetime]] = []
        cursor = start
        while cursor < end:
            next_midnight = datetime.combine(
                cursor.date() + timedelta(days=1),
                datetime.min.time(),
            )
            segment_end = min(end, next_midnight)
            if segment_end > cursor:
                result.append((cursor, segment_end))
            cursor = segment_end
        return result

    def _ensure_day_locked(self, key: str) -> dict:
        days = self._state["days"]
        current = days.get(key)
        if not isinstance(current, dict):
            current = self._empty_day()
            days[key] = current
        return current

    def _drop_day_if_empty_locked(self, key: str) -> None:
        entry = self._state["days"].get(key)
        if not isinstance(entry, dict):
            return
        if entry.get("plan") is not None or entry.get("activity_prompt") is not None:
            return
        if entry.get("manual_breaks") or entry.get("active_break_started_at"):
            return
        self._state["days"].pop(key, None)

    def _decode_plan(self, raw_plan) -> dict:
        if not isinstance(raw_plan, dict):
            raise ValueError("상태 파일의 plan 값이 객체가 아닙니다.")
        if set(raw_plan) != {"target_net_minutes", "clock_in"}:
            raise ValueError("상태 파일의 plan 필드 구성이 올바르지 않습니다.")

        target = self._validate_target(raw_plan["target_net_minutes"])
        raw_clock_in = raw_plan["clock_in"]
        clock_in = self._clock_value(raw_clock_in)
        if raw_clock_in != clock_in:
            raise ValueError("상태 파일의 출근 시간이 정규 형식이 아닙니다.")
        return {
            "target_net_minutes": int(target),
            "clock_in": clock_in,
        }

    def _decode_flat_plan(self, raw_day: dict, has_break_state: bool) -> dict | None:
        marker_present = "plan_explicit" in raw_day
        if marker_present and not isinstance(raw_day["plan_explicit"], bool):
            raise ValueError("상태 파일의 plan_explicit 값이 bool이 아닙니다.")
        marker = raw_day.get("plan_explicit") is True
        target_present = "target_net_minutes" in raw_day
        clock_present = "clock_in" in raw_day
        if not marker and not target_present and not clock_present:
            return None

        target = (
            self._validate_target(raw_day["target_net_minutes"])
            if target_present
            else self._default_target_minutes
        )
        raw_clock_in = raw_day.get("clock_in")
        clock_in = self._clock_value(raw_clock_in) if clock_present else None
        if clock_present and raw_clock_in != clock_in:
            raise ValueError("상태 파일의 출근 시간이 정규 형식이 아닙니다.")

        # The unpublished flat v2 schema materialized a target whenever a
        # break-only day was created. A plan saved through the UI always has a
        # clock-in, so an unmarked break-bearing row without one is migration
        # residue regardless of the default target that was active then.
        explicit = marker or clock_in is not None or not has_break_state
        if not explicit:
            return None
        return {
            "target_net_minutes": int(target),
            "clock_in": clock_in,
        }

    def _decode_activity_prompt(
        self,
        raw_prompt,
        day_start: datetime,
        day_end: datetime,
    ) -> dict:
        if not isinstance(raw_prompt, dict):
            raise ValueError("상태 파일의 activity_prompt 값이 객체가 아닙니다.")
        status = raw_prompt.get("status")
        if status not in _ACTIVITY_PROMPT_STATUSES:
            raise ValueError("상태 파일의 activity prompt 상태가 올바르지 않습니다.")

        expected_fields = {"status", "detected_at"}
        if status == "snoozed":
            expected_fields.add("snooze_until")
        if set(raw_prompt) != expected_fields:
            raise ValueError("상태 파일의 activity prompt 필드 구성이 올바르지 않습니다.")

        detected_at = self._parse_iso_seconds(raw_prompt["detected_at"])
        if (
            detected_at is None
            or detected_at < day_start
            or detected_at >= day_end
        ):
            raise ValueError("activity prompt 감지 시간이 해당 날짜에 속하지 않습니다.")

        decoded = {
            "status": status,
            "detected_at": raw_prompt["detected_at"],
        }
        if status == "snoozed":
            snooze_until = self._parse_iso_seconds(raw_prompt["snooze_until"])
            if snooze_until is None or snooze_until <= detected_at:
                raise ValueError("activity prompt 다시 알림 시간이 올바르지 않습니다.")
            decoded["snooze_until"] = raw_prompt["snooze_until"]
        return decoded

    def _decode_state(self, raw) -> tuple[dict, bool]:
        state = self._empty_state()
        if not isinstance(raw, dict):
            raise ValueError("상태 파일의 최상위 값이 객체가 아닙니다.")

        # first_seen_by_date is a tolerated legacy hint, never an explicit
        # user plan. Unknown published versions and malformed current values
        # remain untouched and fail closed. Recognized legacy, v2, and the
        # unpublished flat-v2 shapes are migrated to canonical v3.
        version_present = "state_version" in raw
        if version_present:
            version = raw["state_version"]
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValueError("상태 파일 버전이 정수가 아닙니다.")
            if version not in {2, STATE_VERSION}:
                raise ValueError("지원하지 않는 상태 파일 버전입니다.")
            if set(raw) != {"state_version", "days"}:
                raise ValueError("상태 파일의 최상위 필드 구성이 올바르지 않습니다.")
        else:
            version = None
            if set(raw) == {"first_seen_by_date"}:
                first_seen = raw["first_seen_by_date"]
                if not isinstance(first_seen, dict):
                    raise ValueError("legacy 상태 값이 객체가 아닙니다.")
                for raw_key, raw_value in first_seen.items():
                    self._day_key(raw_key)
                    if self._parse_iso(raw_value) is None:
                        raise ValueError("legacy 상태 시간이 올바르지 않습니다.")
                return state, True
            if set(raw) != {"days"}:
                raise ValueError("상태 파일의 최상위 필드 구성이 올바르지 않습니다.")

        raw_days = raw["days"]
        if not isinstance(raw_days, dict):
            raise ValueError("상태 파일의 days 값이 객체가 아닙니다.")

        common_fields = {"manual_breaks", "active_break_started_at"}
        flat_fields = {"plan_explicit", "target_net_minutes", "clock_in"}
        saw_flat_row = False
        active_count = 0
        for raw_key, raw_day in raw_days.items():
            key = self._day_key(raw_key)
            if not isinstance(raw_day, dict):
                raise ValueError("상태 파일의 날짜별 값이 객체가 아닙니다.")
            if not common_fields.issubset(raw_day):
                raise ValueError("상태 파일의 휴게 필드가 누락되었습니다.")

            has_nested_plan = "plan" in raw_day
            present_flat_fields = flat_fields.intersection(raw_day)
            if has_nested_plan and present_flat_fields:
                raise ValueError("중첩 plan과 flat plan 필드가 함께 존재합니다.")
            if version == STATE_VERSION and present_flat_fields:
                raise ValueError("현재 상태 파일에 legacy flat plan 필드가 있습니다.")

            allowed_fields = set(common_fields)
            if has_nested_plan:
                allowed_fields.add("plan")
            elif version != STATE_VERSION:
                allowed_fields.update(flat_fields)
            if version == STATE_VERSION:
                allowed_fields.add("activity_prompt")
            if set(raw_day) - allowed_fields:
                raise ValueError("상태 파일의 날짜별 필드 구성이 올바르지 않습니다.")

            raw_breaks = raw_day["manual_breaks"]
            if not isinstance(raw_breaks, list):
                raise ValueError("상태 파일의 manual_breaks 값이 배열이 아닙니다.")
            raw_active = raw_day["active_break_started_at"]
            if raw_active is not None and not isinstance(raw_active, str):
                raise ValueError("상태 파일의 active break 값이 올바르지 않습니다.")
            active = self._parse_iso(raw_active) if raw_active is not None else None
            if raw_active is not None and active is None:
                raise ValueError("상태 파일의 active break 시간이 올바르지 않습니다.")

            day_start = datetime.strptime(key, "%Y-%m-%d")
            day_end = day_start + timedelta(days=1)
            entry = self._empty_day()
            for item in raw_breaks:
                if not isinstance(item, dict) or set(item) != {"start", "end"}:
                    raise ValueError("상태 파일의 수동 휴게 항목이 올바르지 않습니다.")
                start = self._parse_iso(item["start"])
                end = self._parse_iso(item["end"])
                if (
                    start is None
                    or end is None
                    or end <= start
                    or start < day_start
                    or start >= day_end
                    or end > day_end
                ):
                    raise ValueError("상태 파일의 수동 휴게 구간이 올바르지 않습니다.")
                entry["manual_breaks"].append({
                    "start": item["start"],
                    "end": item["end"],
                })

            if active is not None:
                if active < day_start or active >= day_end:
                    raise ValueError("active break가 해당 날짜에 속하지 않습니다.")
                active_count += 1
                if active_count > 1:
                    raise ValueError("active break가 둘 이상 존재합니다.")
                entry["active_break_started_at"] = raw_active

            has_break_state = bool(raw_breaks) or active is not None
            if has_nested_plan:
                entry["plan"] = self._decode_plan(raw_day["plan"])
            elif present_flat_fields:
                saw_flat_row = True
                plan = self._decode_flat_plan(raw_day, has_break_state)
                if plan is not None:
                    entry["plan"] = plan

            if "activity_prompt" in raw_day:
                entry["activity_prompt"] = self._decode_activity_prompt(
                    raw_day["activity_prompt"],
                    day_start,
                    day_end,
                )
            state["days"][key] = entry

        needs_migration = version != STATE_VERSION or saw_flat_row
        if version == STATE_VERSION:
            if raw != state:
                raise ValueError("현재 상태 파일이 정규 v3 구조가 아닙니다.")
        elif version == 2 and not saw_flat_row:
            canonical_v2 = copy.deepcopy(state)
            canonical_v2["state_version"] = 2
            if raw != canonical_v2:
                raise ValueError("기존 상태 파일이 정규 v2 구조가 아닙니다.")
        return state, needs_migration

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

    def _load(self) -> None:
        with self._lock:
            self._write_blocked_reason = None
            try:
                if not os.path.isfile(self._path):
                    return
                with open(self._path, "r", encoding="utf-8") as fp:
                    raw = json.load(
                        fp,
                        object_pairs_hook=self._reject_duplicate_json_keys,
                        parse_constant=self._reject_json_constant,
                    )
            except Exception:
                self._state = self._empty_state()
                self._write_blocked_reason = (
                    "기존 상태 파일을 읽지 못해 덮어쓰기를 차단했습니다. "
                    "파일을 확인하거나 백업 후 삭제해 주세요."
                )
                return
            try:
                decoded, needs_migration = self._decode_state(raw)
            except Exception:
                self._state = self._empty_state()
                self._write_blocked_reason = (
                    "기존 상태 파일 형식이나 버전을 지원하지 않아 덮어쓰기를 "
                    "차단했습니다. 파일을 확인하거나 백업 후 삭제해 주세요."
                )
                return
            self._state = decoded
            if needs_migration and not self._save_locked():
                self._write_blocked_reason = (
                    "기존 상태 파일 마이그레이션을 저장하지 못해 추가 쓰기를 "
                    "차단했습니다. 파일 권한과 디스크 상태를 확인해 주세요."
                )

    def _write_error_locked(self) -> str:
        return self._write_blocked_reason or "상태 파일을 저장하지 못했습니다."

    def _save_locked(self) -> bool:
        if self._write_blocked_reason:
            return False
        parent = os.path.dirname(self._path) or os.curdir
        temp_path = None
        try:
            os.makedirs(parent, exist_ok=True)
            prefix = f".{os.path.basename(self._path)}."
            fd, temp_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
                json.dump(
                    self._state,
                    fp,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temp_path, self._path)
            temp_path = None
            return True
        except Exception:
            return False
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    def _append_completed_span_locked(self, start: datetime, end: datetime) -> None:
        for segment_start, segment_end in self._split_span(start, end):
            key = segment_start.strftime("%Y-%m-%d")
            entry = self._ensure_day_locked(key)
            entry["manual_breaks"].append({
                "start": self._format_iso(segment_start),
                "end": self._format_iso(segment_end),
            })

    def _active_locked(self) -> tuple[str | None, datetime | None]:
        found: list[tuple[str, datetime]] = []
        for key, entry in self._state["days"].items():
            if not isinstance(entry, dict):
                continue
            parsed = self._parse_iso(entry.get("active_break_started_at"))
            if parsed is not None:
                found.append((key, parsed))
        if len(found) != 1:
            return None, None
        return found[0]

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def get_day_plan(self, day=None, default_target_minutes=None) -> dict:
        key = self._day_key(day)
        if default_target_minutes is None:
            fallback = self._default_target_minutes
        else:
            fallback = self._validate_target(default_target_minutes)
        with self._lock:
            entry = self._state["days"].get(key)
            plan = entry.get("plan") if isinstance(entry, dict) else None
            explicit = isinstance(plan, dict)
            if not explicit:
                return {
                    "date": key,
                    "target_net_minutes": int(fallback),
                    "clock_in": None,
                    "explicit": False,
                }
            try:
                target = self._validate_target(plan.get("target_net_minutes"))
            except ValueError:
                target = fallback
            try:
                clock_in = self._clock_value(plan.get("clock_in"))
            except ValueError:
                clock_in = None
            return {
                "date": key,
                "target_net_minutes": int(target),
                "clock_in": clock_in,
                "explicit": True,
            }

    def update_day_plan(self, day, target_minutes, clock_in) -> tuple[bool, str | None]:
        try:
            key = self._day_key(day)
            target = self._validate_target(target_minutes)
            clock_value = self._clock_value(clock_in)
        except ValueError as exc:
            return False, str(exc)
        with self._lock:
            if self._write_blocked_reason:
                return False, self._write_error_locked()
            previous = copy.deepcopy(self._state)
            entry = self._ensure_day_locked(key)
            entry["plan"] = {
                "target_net_minutes": target,
                "clock_in": clock_value,
            }
            if clock_value is not None:
                entry.pop("activity_prompt", None)
            if not self._save_locked():
                self._state = previous
                return False, self._write_error_locked()
        return True, None

    def clear_day_plan(self, day=None) -> tuple[bool, str | None]:
        try:
            key = self._day_key(day)
        except ValueError as exc:
            return False, str(exc)
        with self._lock:
            if self._write_blocked_reason:
                return False, self._write_error_locked()
            entry = self._state["days"].get(key)
            if not isinstance(entry, dict) or "plan" not in entry:
                return True, None
            previous = copy.deepcopy(self._state)
            entry.pop("plan", None)
            self._drop_day_if_empty_locked(key)
            if not self._save_locked():
                self._state = previous
                return False, self._write_error_locked()
        return True, None

    def get_activity_prompt(self, day=None) -> dict | None:
        key = self._day_key(day)
        with self._lock:
            entry = self._state["days"].get(key)
            prompt = entry.get("activity_prompt") if isinstance(entry, dict) else None
            return copy.deepcopy(prompt) if isinstance(prompt, dict) else None

    def record_activity_prompt_pending(
        self,
        day=None,
        detected_at=None,
    ) -> tuple[bool, str | None]:
        if detected_at is None and isinstance(day, datetime):
            detected_at, day = day, None
        try:
            detected, detected_text = self._prompt_time_value(
                detected_at,
                "activity prompt 감지 시간",
                use_now=True,
            )
            key = self._day_key(day, now=detected)
            if detected.strftime("%Y-%m-%d") != key:
                raise ValueError("activity prompt 감지 시간이 해당 날짜에 속하지 않습니다.")
        except ValueError as exc:
            return False, str(exc)

        with self._lock:
            if self._write_blocked_reason:
                return False, self._write_error_locked()
            previous = copy.deepcopy(self._state)
            self._ensure_day_locked(key)["activity_prompt"] = {
                "status": "pending",
                "detected_at": detected_text,
            }
            if not self._save_locked():
                self._state = previous
                return False, self._write_error_locked()
        return True, None

    def snooze_activity_prompt(
        self,
        day=None,
        snooze_until=None,
        *,
        until=None,
    ) -> tuple[bool, str | None]:
        if until is not None:
            if snooze_until is not None:
                return False, "다시 알림 시간은 하나만 지정해야 합니다."
            snooze_until = until
        if snooze_until is None and isinstance(day, datetime):
            snooze_until, day = day, None
        try:
            key = self._day_key(day)
            snooze_value, snooze_text = self._prompt_time_value(
                snooze_until,
                "activity prompt 다시 알림 시간",
            )
        except ValueError as exc:
            return False, str(exc)

        with self._lock:
            if self._write_blocked_reason:
                return False, self._write_error_locked()
            entry = self._state["days"].get(key)
            prompt = entry.get("activity_prompt") if isinstance(entry, dict) else None
            if not isinstance(prompt, dict):
                return False, "다시 알림을 설정할 activity prompt가 없습니다."
            detected_at = self._parse_iso_seconds(prompt.get("detected_at"))
            if detected_at is None or snooze_value <= detected_at:
                return False, "다시 알림 시간은 감지 시간보다 뒤여야 합니다."

            previous = copy.deepcopy(self._state)
            entry["activity_prompt"] = {
                "status": "snoozed",
                "detected_at": prompt["detected_at"],
                "snooze_until": snooze_text,
            }
            if not self._save_locked():
                self._state = previous
                return False, self._write_error_locked()
        return True, None

    def skip_activity_prompt(self, day=None) -> tuple[bool, str | None]:
        try:
            key = self._day_key(day)
        except ValueError as exc:
            return False, str(exc)
        with self._lock:
            if self._write_blocked_reason:
                return False, self._write_error_locked()
            entry = self._state["days"].get(key)
            prompt = entry.get("activity_prompt") if isinstance(entry, dict) else None
            if not isinstance(prompt, dict):
                return False, "건너뛸 activity prompt가 없습니다."

            previous = copy.deepcopy(self._state)
            entry["activity_prompt"] = {
                "status": "skipped",
                "detected_at": prompt["detected_at"],
            }
            if not self._save_locked():
                self._state = previous
                return False, self._write_error_locked()
        return True, None

    def clear_activity_prompt(self, day=None) -> tuple[bool, str | None]:
        try:
            key = self._day_key(day)
        except ValueError as exc:
            return False, str(exc)
        with self._lock:
            if self._write_blocked_reason:
                return False, self._write_error_locked()
            entry = self._state["days"].get(key)
            if not isinstance(entry, dict) or "activity_prompt" not in entry:
                return True, None

            previous = copy.deepcopy(self._state)
            entry.pop("activity_prompt", None)
            self._drop_day_if_empty_locked(key)
            if not self._save_locked():
                self._state = previous
                return False, self._write_error_locked()
        return True, None

    def break_intervals_for_day(self, day, now=None) -> list[BreakInterval]:
        now_value = self._now(now)
        key = self._day_key(day, now=now_value)
        target_day = datetime.strptime(key, "%Y-%m-%d").date()
        day_start = datetime.combine(target_day, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        intervals: list[BreakInterval] = []

        with self._lock:
            for entry in self._state["days"].values():
                if not isinstance(entry, dict):
                    continue
                for item in entry.get("manual_breaks") or []:
                    if not isinstance(item, dict):
                        continue
                    start = self._parse_iso(item.get("start"))
                    end = self._parse_iso(item.get("end"))
                    if start is None or end is None:
                        continue
                    clipped_start = max(start, day_start)
                    clipped_end = min(end, day_end)
                    if clipped_end > clipped_start:
                        intervals.append(BreakInterval(clipped_start, clipped_end, "수동"))

            _active_key, active = self._active_locked()
            if active is not None and now_value >= active:
                clipped_start = max(active, day_start)
                clipped_end = min(now_value, day_end)
                if clipped_end > clipped_start:
                    if target_day == now_value.date() and now_value < day_end:
                        intervals.append(BreakInterval(clipped_start, None, "수동"))
                    else:
                        intervals.append(BreakInterval(clipped_start, clipped_end, "수동"))
        intervals.sort(key=lambda item: item.start)
        return intervals

    def get_manual_break_state(self, now=None) -> dict:
        now_value = self._now(now)
        with self._lock:
            _active_key, active = self._active_locked()
            intervals = self.break_intervals_for_day(now_value.date(), now=now_value)

        completed_spans = [
            (item.start, item.end)
            for item in intervals
            if isinstance(item.end, datetime)
        ]
        completed_seconds = sum(
            (end - start).total_seconds()
            for start, end in union_datetime_intervals(completed_spans)
        )
        ongoing_minutes = 0
        if active is not None and active <= now_value:
            today_start = datetime.combine(now_value.date(), datetime.min.time())
            ongoing_start = max(active, today_start)
            ongoing_minutes = int(max(0.0, (now_value - ongoing_start).total_seconds()) // 60)
        return {
            "active": active is not None,
            "started_at": self._format_iso(active) if active is not None else "",
            "completed_minutes": int(completed_seconds // 60),
            "ongoing_minutes": int(ongoing_minutes),
            "session_count": len(completed_spans),
        }

    def toggle_manual_break(self, now=None) -> dict:
        try:
            now_value = self._now(now)
        except ValueError as exc:
            return {
                "active": False,
                "started_at": "",
                "completed_minutes": 0,
                "ongoing_minutes": 0,
                "session_count": 0,
                "ok": False,
                "message": str(exc),
            }

        with self._lock:
            if self._write_blocked_reason:
                state = self.get_manual_break_state(now=now_value)
                state.update({
                    "ok": False,
                    "message": self._write_error_locked(),
                })
                return state
            previous = copy.deepcopy(self._state)
            _active_key, active = self._active_locked()
            if active is None:
                key = now_value.strftime("%Y-%m-%d")
                self._ensure_day_locked(key)["active_break_started_at"] = self._format_iso(now_value)
                message = f"휴게 시작 {now_value.strftime('%H:%M')}"
            else:
                if now_value < active:
                    state = self.get_manual_break_state(now=now_value)
                    state.update({
                        "ok": False,
                        "message": "현재 시간이 휴게 시작 시간보다 빠릅니다.",
                    })
                    return state
                for entry in self._state["days"].values():
                    if isinstance(entry, dict):
                        entry["active_break_started_at"] = None
                self._append_completed_span_locked(active, now_value)
                duration = int(max(0.0, (now_value - active).total_seconds()) // 60)
                message = f"휴게 종료 ({duration}분)"

            if not self._save_locked():
                self._state = previous
                state = self.get_manual_break_state(now=now_value)
                state.update({
                    "ok": False,
                    "message": self._write_error_locked(),
                })
                return state

            state = self.get_manual_break_state(now=now_value)
            if active is not None:
                message += f" · 오늘 수동 누적 {format_minutes(state['completed_minutes'])}"
            state.update({"ok": True, "message": message})
            return state
