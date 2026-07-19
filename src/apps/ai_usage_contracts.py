from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
import re
from typing import Any


@unique
class AiUsageProvider(StrEnum):
    CODEX = "codex"
    CURSOR = "cursor"


@unique
class UsageState(StrEnum):
    READY = "ready"
    UNKNOWN = "unknown"
    UNSUPPORTED_CONTRACT = "unsupported_contract"
    LOGGED_OUT = "logged_out"
    DOM_DRIFT = "dom_drift"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    STALE = "stale"
    CRASH = "crash"
    RECYCLE = "recycle"


_STATE_ALIASES: dict[str, UsageState] = {
    "ready": UsageState.READY,
    "unknown": UsageState.UNKNOWN,
    "unsupported": UsageState.UNSUPPORTED_CONTRACT,
    "unsupported_contract": UsageState.UNSUPPORTED_CONTRACT,
    "logged_out": UsageState.LOGGED_OUT,
    "login_required": UsageState.LOGGED_OUT,
    "not_authenticated": UsageState.LOGGED_OUT,
    "dom_drift": UsageState.DOM_DRIFT,
    "parse_failed": UsageState.DOM_DRIFT,
    "schema_incompatible": UsageState.DOM_DRIFT,
    "timeout": UsageState.TIMEOUT,
    "command_timeout": UsageState.TIMEOUT,
    "navigation_timeout": UsageState.TIMEOUT,
    "rate_limited": UsageState.RATE_LIMITED,
    "rate_limit": UsageState.RATE_LIMITED,
    "too_many_requests": UsageState.RATE_LIMITED,
    "429": UsageState.RATE_LIMITED,
    "stale": UsageState.STALE,
    "cache_stale": UsageState.STALE,
    "expired_cache": UsageState.STALE,
    "crash": UsageState.CRASH,
    "renderer_crashed": UsageState.CRASH,
    "transport_closed": UsageState.CRASH,
    "recycle": UsageState.RECYCLE,
    "worker_recycle": UsageState.RECYCLE,
    "page_recycling": UsageState.RECYCLE,
}


def normalize_usage_state(value: object) -> UsageState:
    if isinstance(value, UsageState):
        return value
    key = str(value or "").strip().lower().replace("-", " ")
    key = "_".join(key.split())
    return _STATE_ALIASES.get(key, UsageState.UNKNOWN)


def usage_state_message(state: UsageState | str | None) -> str:
    normalized = normalize_usage_state(state)
    return {
        UsageState.READY: "사용량을 확인했습니다.",
        UsageState.UNKNOWN: "조회 상태를 확인할 수 없습니다.",
        UsageState.UNSUPPORTED_CONTRACT: "현재 환경에서는 사용량 조회 불가 상태입니다.",
        UsageState.LOGGED_OUT: "사용량을 확인하려면 로그인해야 합니다.",
        UsageState.DOM_DRIFT: "화면 구조가 변경되어 사용량을 조회할 수 없습니다.",
        UsageState.TIMEOUT: "사용량 조회 시간이 초과되었습니다.",
        UsageState.RATE_LIMITED: "요청 한도에 도달하여 잠시 후 다시 조회해야 합니다.",
        UsageState.STALE: "최근 조회에 실패하여 마지막 성공 값을 표시합니다.",
        UsageState.CRASH: "사용량 조회 브라우저가 비정상 종료되었습니다.",
        UsageState.RECYCLE: "사용량 조회 브라우저를 안전하게 재시작하고 있습니다.",
    }[normalized]


def _optional_percent(value: float | int | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not 0.0 <= number <= 100.0:
        raise ValueError("percentage must be between 0 and 100")
    return round(number, 4)


def normalize_reset_boundary(value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    korean = re.fullmatch(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if korean is not None:
        year, month, day = (int(part) for part in korean.groups())
        try:
            return datetime(year, month, day).date().isoformat(), "date"
        except ValueError:
            return text, ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.fromisoformat(text).date().isoformat(), "date"
        except ValueError:
            return text, ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text, ""
    return text, "datetime"


@dataclass(frozen=True, slots=True)
class AiUsageReading:
    provider: AiUsageProvider
    profile_id: str
    state: UsageState
    used_percent: float | None = None
    remaining_percent: float | None = None
    included_used: str = ""
    included_limit: str = ""
    captured_at: str = ""
    last_success_at: str = ""
    reset_at: str = ""
    on_demand_enabled: bool | None = None
    message: str = ""
    last_error_state: UsageState | None = None
    reset_precision: str = ""

    def __post_init__(self) -> None:
        provider = (
            self.provider
            if isinstance(self.provider, AiUsageProvider)
            else AiUsageProvider(str(self.provider))
        )
        state = normalize_usage_state(self.state)
        last_error_state = (
            None
            if self.last_error_state is None
            else normalize_usage_state(self.last_error_state)
        )
        used = _optional_percent(self.used_percent)
        remaining = _optional_percent(self.remaining_percent)
        if used is None and remaining is not None:
            used = round(100.0 - remaining, 4)
        elif remaining is None and used is not None:
            remaining = round(100.0 - used, 4)
        if used is not None and remaining is not None and abs(used + remaining - 100.0) > 0.05:
            raise ValueError("used and remaining percentages must total 100")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "last_error_state", last_error_state)
        object.__setattr__(self, "used_percent", used)
        object.__setattr__(self, "remaining_percent", remaining)
        object.__setattr__(self, "profile_id", str(self.profile_id or "").strip())
        object.__setattr__(self, "included_used", str(self.included_used or "").strip())
        object.__setattr__(self, "included_limit", str(self.included_limit or "").strip())
        normalized_reset, inferred_precision = normalize_reset_boundary(self.reset_at)
        requested_precision = str(self.reset_precision or "").strip().lower()
        if requested_precision not in {"date", "datetime"}:
            requested_precision = inferred_precision
        object.__setattr__(self, "reset_at", normalized_reset)
        object.__setattr__(self, "reset_precision", requested_precision)
        if not self.message:
            object.__setattr__(self, "message", usage_state_message(state))

    @classmethod
    def unavailable(
        cls,
        *,
        provider: AiUsageProvider,
        profile_id: str,
        state: UsageState = UsageState.UNKNOWN,
        captured_at: str = "",
        message: str = "",
        last_error_state: UsageState | None = None,
    ) -> "AiUsageReading":
        return cls(
            provider=provider,
            profile_id=profile_id,
            state=state,
            captured_at=captured_at,
            message=message,
            last_error_state=last_error_state,
        )

    @property
    def is_usable(self) -> bool:
        return self.state in {UsageState.READY, UsageState.STALE} and (
            self.used_percent is not None or self.remaining_percent is not None
        )

    @property
    def is_stale(self) -> bool:
        return self.state == UsageState.STALE

    def to_dict(self) -> dict[str, Any]:
        included_usage = ""
        if self.included_used and self.included_limit:
            included_usage = f"{self.included_used} / {self.included_limit}"
        elif self.used_percent is not None:
            included_usage = f"{self.used_percent:g}% used"
        return {
            "provider": self.provider.value,
            "profile_id": self.profile_id,
            "state": self.state.value,
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
            "included_used_percent": self.used_percent,
            "included_remaining_percent": self.remaining_percent,
            "included_used": self.included_used,
            "included_limit": self.included_limit,
            "included_usage": included_usage,
            "captured_at": self.captured_at,
            "last_success_at": self.last_success_at,
            "reset_at": self.reset_at,
            "billing_reset_at": self.reset_at,
            "reset_precision": self.reset_precision,
            "on_demand_enabled": self.on_demand_enabled,
            "on_demand_status": (
                "ON" if self.on_demand_enabled is True else "OFF" if self.on_demand_enabled is False else "조회 불가"
            ),
            "on_demand_state": self.state.value,
            "message": self.message,
            "last_error_state": (
                self.last_error_state.value if self.last_error_state is not None else ""
            ),
        }
