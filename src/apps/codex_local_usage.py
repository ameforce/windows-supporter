from __future__ import annotations

import base64
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import heapq
import json
import os
from pathlib import Path
from typing import Any


_FIVE_HOUR_WINDOW_MINUTES = 300
_WEEKLY_WINDOW_MINUTES = 10080
_KOREA_TZ = timezone(timedelta(hours=9), name="KST")
_TAIL_BYTES = 512 * 1024
_SESSION_META_BYTES = 64 * 1024
_MAX_ROLLOUT_FILES = 16


@dataclass(frozen=True, slots=True)
class LocalCodexUsageSnapshot:
    captured_at: str
    account_id: str = ""
    plan_type: str = ""
    five_hour_limit: str = ""
    weekly_limit: str = ""
    five_hour_limit_reset_at: str = ""
    weekly_limit_reset_at: str = ""
    reported_metric_keys: tuple[str, ...] = ()


def _read_codex_identity(root: Path) -> tuple[str, str, datetime | None]:
    auth_path = root / "auth.json"
    try:
        before = auth_path.stat()
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        after = auth_path.stat()
    except (OSError, json.JSONDecodeError):
        return "", "", None
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        return "", "", None
    if not isinstance(auth, dict):
        return "", "", None
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        return "", "", None
    account_id = str(tokens.get("account_id") or "").strip()
    plan_type = ""
    token_parts = str(tokens.get("id_token") or "").split(".")
    if len(token_parts) >= 2:
        try:
            encoded = token_parts[1] + "=" * (-len(token_parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(encoded))
            auth_claims = claims.get("https://api.openai.com/auth", {})
            if isinstance(auth_claims, dict):
                account_id = account_id or str(
                    auth_claims.get("chatgpt_account_id") or ""
                ).strip()
                plan_type = str(auth_claims.get("chatgpt_plan_type") or "").strip()
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    changed_at = datetime.fromtimestamp(after.st_mtime, tz=timezone.utc)
    return account_id, plan_type, changed_at


def _format_remaining_percent(used_percent: float) -> str:
    remaining = max(0.0, min(100.0, 100.0 - float(used_percent)))
    rendered = f"{remaining:.4f}".rstrip("0").rstrip(".")
    return f"{rendered}%"


def _format_reset_at(value: Any) -> str:
    try:
        seconds = int(value)
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(_KOREA_TZ)
    except (OSError, OverflowError, TypeError, ValueError):
        return ""
    return parsed.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _parse_captured_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def parse_codex_rate_limit_event(event: dict[str, Any]) -> LocalCodexUsageSnapshot | None:
    if str(event.get("type") or "") != "event_msg":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict) or str(payload.get("type") or "") != "token_count":
        return None
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict) or str(rate_limits.get("limit_id") or "") != "codex":
        return None
    captured_at = str(event.get("timestamp") or "").strip()
    if not captured_at or _parse_captured_at(captured_at) is None:
        return None

    values: dict[str, str] = {}
    reported: set[str] = set()
    for window_name in ("primary", "secondary"):
        window = rate_limits.get(window_name)
        if not isinstance(window, dict):
            continue
        raw_window_minutes = window.get("window_minutes")
        raw_used_percent = window.get("used_percent")
        if raw_window_minutes is None or raw_used_percent is None:
            continue
        try:
            window_minutes = int(str(raw_window_minutes))
            used_percent = float(str(raw_used_percent))
        except (TypeError, ValueError):
            continue
        if not 0.0 <= used_percent <= 100.0:
            continue
        if window_minutes == _FIVE_HOUR_WINDOW_MINUTES:
            metric_key = "five_hour_limit"
            reset_key = "five_hour_limit_reset_at"
        elif window_minutes == _WEEKLY_WINDOW_MINUTES:
            metric_key = "weekly_limit"
            reset_key = "weekly_limit_reset_at"
        else:
            continue
        values[metric_key] = _format_remaining_percent(used_percent)
        values[reset_key] = _format_reset_at(window.get("resets_at"))
        reported.add(metric_key)

    if not reported:
        return None
    return LocalCodexUsageSnapshot(
        captured_at=captured_at,
        plan_type=str(rate_limits.get("plan_type") or "").strip().lower(),
        five_hour_limit=values.get("five_hour_limit", ""),
        weekly_limit=values.get("weekly_limit", ""),
        five_hour_limit_reset_at=values.get("five_hour_limit_reset_at", ""),
        weekly_limit_reset_at=values.get("weekly_limit_reset_at", ""),
        reported_metric_keys=tuple(
            key for key in ("five_hour_limit", "weekly_limit") if key in reported
        ),
    )


def _latest_snapshot_in_rollout(path: Path) -> LocalCodexUsageSnapshot | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            offset = max(0, size - _TAIL_BYTES)
            stream.seek(offset)
            payload = stream.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    lines = payload.splitlines()
    if offset > 0 and lines:
        lines = lines[1:]
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        snapshot = parse_codex_rate_limit_event(event)
        if snapshot is not None:
            return snapshot
    return None


def _rollout_started_at(path: Path) -> datetime | None:
    try:
        with path.open("rb") as stream:
            payload = stream.read(_SESSION_META_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in payload.splitlines():
        if '"session_meta"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or str(event.get("type") or "") != "session_meta":
            continue
        return _parse_captured_at(str(event.get("timestamp") or ""))
    return None


def find_latest_windows_codex_usage(
    codex_home: str | None = None,
) -> LocalCodexUsageSnapshot | None:
    if os.name != "nt":
        return None
    root = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    candidates: list[tuple[float, str, Path]] = []
    sessions_root = root / "sessions"
    for path in sessions_root.glob("*/*/*/rollout-*.jsonl"):
        try:
            candidate = (path.stat().st_mtime, str(path), path)
        except OSError:
            continue
        if len(candidates) < _MAX_ROLLOUT_FILES:
            heapq.heappush(candidates, candidate)
        else:
            heapq.heappushpop(candidates, candidate)

    latest: LocalCodexUsageSnapshot | None = None
    latest_at: datetime | None = None
    latest_path: Path | None = None
    for _, _, path in sorted(candidates, reverse=True):
        snapshot = _latest_snapshot_in_rollout(path)
        if snapshot is None:
            continue
        captured_at = _parse_captured_at(snapshot.captured_at)
        if captured_at is not None and (latest_at is None or captured_at > latest_at):
            latest = snapshot
            latest_at = captured_at
            latest_path = path
    if latest is None:
        return None
    account_id, plan_type, auth_changed_at = _read_codex_identity(root)
    session_started_at = _rollout_started_at(latest_path) if latest_path is not None else None
    if (
        latest_at is None
        or auth_changed_at is None
        or latest_at < auth_changed_at
        or session_started_at is None
        or session_started_at < auth_changed_at
    ):
        return latest
    event_plan_type = str(latest.plan_type or "").strip().lower()
    auth_plan_type = str(plan_type or "").strip().lower()
    if event_plan_type and auth_plan_type and event_plan_type != auth_plan_type:
        return latest
    return replace(
        latest,
        account_id=account_id,
        plan_type=event_plan_type or auth_plan_type,
    )
