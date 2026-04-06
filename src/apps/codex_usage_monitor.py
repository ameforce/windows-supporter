from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re
import threading
import traceback
from datetime import timedelta, timezone
from typing import Any

from src.utils.LibConnector import LibConnector
from src.utils.ToolTip import ToolTip


USAGE_METRIC_KEYS = (
    "five_hour_limit",
    "weekly_limit",
    "code_review",
    "remaining_credit",
)

USAGE_METRIC_LABELS: dict[str, str] = {
    "five_hour_limit": "5시간 사용 한도",
    "weekly_limit": "주간 사용 한도",
    "code_review": "코드 검토",
    "remaining_credit": "남은 크레딧",
}

USAGE_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "five_hour_limit": (
        "5시간 사용 한도",
        "5시간한도",
        "5-hour usage limit",
        "5 hour usage limit",
        "5h usage limit",
    ),
    "weekly_limit": (
        "주간 사용 한도",
        "주간한도",
        "weekly usage limit",
        "weekly limit",
    ),
    "code_review": (
        "코드 검토",
        "코드리뷰",
        "code review",
        "reviews",
    ),
    "remaining_credit": (
        "남은 크레딧",
        "잔여 크레딧",
        "remaining credit",
        "credits remaining",
    ),
}


def normalize_usage_value(value: str) -> str:
    text = str(value or "").replace("\r", "\n")
    parts: list[str] = []
    for line in text.split("\n"):
        cleaned = " ".join(str(line).strip().split())
        if cleaned:
            parts.append(cleaned)
    return " ".join(parts).strip()


def _normalize_match_token(text: str) -> str:
    raw = normalize_usage_value(text).lower()
    for token in (" ", ":", "：", "-", "_", "|", "\t"):
        raw = raw.replace(token, "")
    return raw


def _find_alias_in_line(line: str, aliases: tuple[str, ...]) -> tuple[str | None, int]:
    line_text = str(line or "")
    line_match = _normalize_match_token(line_text)
    if not line_match:
        return None, -1

    for alias in sorted(aliases, key=len, reverse=True):
        alias_text = str(alias or "").strip()
        if not alias_text:
            continue
        alias_match = _normalize_match_token(alias_text)
        if not alias_match:
            continue
        if alias_match in line_match:
            try:
                idx = line_text.lower().find(alias_text.lower())
            except Exception:
                idx = line_text.find(alias_text)
            return alias_text, idx
    return None, -1


def _line_contains_any_usage_label(line: str) -> bool:
    normalized = _normalize_match_token(line)
    if not normalized:
        return False
    for aliases in USAGE_METRIC_ALIASES.values():
        for alias in aliases:
            alias_token = _normalize_match_token(alias)
            if alias_token and alias_token in normalized:
                return True
    return False


def _normalize_metric_candidate(key: str, value: str) -> str:
    text = normalize_usage_value(value)
    if not text:
        return ""
    try:
        import re
    except Exception:
        return ""

    if key in {"five_hour_limit", "weekly_limit", "code_review"}:
        ratio = re.search(r"(\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?)", text)
        if ratio:
            return normalize_usage_value(ratio.group(1))
        percent = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if percent:
            return f"{percent.group(1)}%"
        return ""

    if key == "remaining_credit":
        if "%" in text or "/" in text:
            return ""
        number = re.search(r"\d[\d,]*", text)
        if not number:
            return ""
        return number.group(0).replace(",", "")

    return text


def parse_usage_metrics_from_text(raw_text: str) -> dict[str, str]:
    text = str(raw_text or "")
    if not text.strip():
        return {}

    lines: list[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        norm = normalize_usage_value(line)
        if norm:
            lines.append(norm)

    if not lines:
        return {}

    parsed: dict[str, str] = {}

    for idx, line in enumerate(lines):
        for key in USAGE_METRIC_KEYS:
            if key in parsed:
                continue
            aliases = USAGE_METRIC_ALIASES.get(key, ())
            alias, start_idx = _find_alias_in_line(line, aliases)
            if alias is None:
                continue

            value = ""
            if start_idx >= 0:
                cut = start_idx + len(alias)
                inline_candidate = line[cut:].strip(" :：-|")
                value = _normalize_metric_candidate(key, inline_candidate)
            if not value:
                j = idx + 1
                while j < len(lines):
                    candidate = normalize_usage_value(lines[j])
                    if not candidate:
                        j += 1
                        continue
                    if _line_contains_any_usage_label(candidate):
                        break
                    candidate_value = _normalize_metric_candidate(key, candidate)
                    if candidate_value:
                        value = candidate_value
                        break
                    j += 1
            value = _normalize_metric_candidate(key, value)
            if value:
                parsed[key] = value

    # Fallback: robust colon parsing over the full flattened text.
    if len(parsed) < len(USAGE_METRIC_KEYS):
        merged = "\n".join(lines)
        try:
            import re

            for key in USAGE_METRIC_KEYS:
                if key in parsed:
                    continue
                aliases = USAGE_METRIC_ALIASES.get(key, ())
                for alias in aliases:
                    pat = re.compile(
                        rf"{re.escape(str(alias))}\s*[:：-]\s*([^\n]+)",
                        re.IGNORECASE,
                    )
                    m = pat.search(merged)
                    if not m:
                        continue
                    value = _normalize_metric_candidate(key, m.group(1))
                    if value:
                        parsed[key] = value
                        break
        except Exception:
            pass

    return parsed


@dataclass
class UsageSnapshot:
    five_hour_limit: str = ""
    weekly_limit: str = ""
    code_review: str = ""
    remaining_credit: str = ""
    captured_at: str = ""

    @classmethod
    def from_metrics(
        cls,
        metrics: dict[str, str] | None,
        captured_at: str = "",
    ) -> "UsageSnapshot":
        data = metrics or {}
        return cls(
            five_hour_limit=normalize_usage_value(data.get("five_hour_limit", "")),
            weekly_limit=normalize_usage_value(data.get("weekly_limit", "")),
            code_review=normalize_usage_value(data.get("code_review", "")),
            remaining_credit=normalize_usage_value(data.get("remaining_credit", "")),
            captured_at=normalize_usage_value(captured_at),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UsageSnapshot":
        if not isinstance(data, dict):
            return cls()
        return cls(
            five_hour_limit=normalize_usage_value(data.get("five_hour_limit", "")),
            weekly_limit=normalize_usage_value(data.get("weekly_limit", "")),
            code_review=normalize_usage_value(data.get("code_review", "")),
            remaining_credit=normalize_usage_value(data.get("remaining_credit", "")),
            captured_at=normalize_usage_value(data.get("captured_at", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "five_hour_limit": normalize_usage_value(self.five_hour_limit),
            "weekly_limit": normalize_usage_value(self.weekly_limit),
            "code_review": normalize_usage_value(self.code_review),
            "remaining_credit": normalize_usage_value(self.remaining_credit),
            "captured_at": normalize_usage_value(self.captured_at),
        }

    def metrics(self) -> dict[str, str]:
        payload = self.to_dict()
        payload.pop("captured_at", None)
        return payload

    def has_any_metric(self) -> bool:
        return any(bool(v) for v in self.metrics().values())


@dataclass
class UsageChange:
    key: str
    label: str
    before: str
    after: str


def merge_snapshot_with_previous(
    current: UsageSnapshot,
    previous: UsageSnapshot | None,
) -> UsageSnapshot:
    prev = previous if isinstance(previous, UsageSnapshot) else None
    if prev is None:
        return current
    merged = current.to_dict()
    prev_payload = prev.to_dict()
    for key in USAGE_METRIC_KEYS:
        if not merged.get(key):
            merged[key] = prev_payload.get(key, "")
    if not merged.get("captured_at"):
        merged["captured_at"] = prev_payload.get("captured_at", "")
    return UsageSnapshot.from_dict(merged)


def compute_usage_changes(
    previous: UsageSnapshot | None,
    current: UsageSnapshot,
) -> list[UsageChange]:
    if previous is None or not previous.has_any_metric():
        return []
    changes: list[UsageChange] = []
    prev_payload = previous.to_dict()
    curr_payload = current.to_dict()
    for key in USAGE_METRIC_KEYS:
        before = normalize_usage_value(prev_payload.get(key, ""))
        after = normalize_usage_value(curr_payload.get(key, ""))
        if before == after:
            continue
        if not after:
            # Missing parse is treated conservatively as no change.
            continue
        changes.append(
            UsageChange(
                key=key,
                label=USAGE_METRIC_LABELS.get(key, key),
                before=before,
                after=after,
            )
        )
    return changes


class CodexUsageMonitor:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        self.__root = None
        self.__event_queue = None

        self.__monitor_after_id = None
        self.__monitor_running = False
        self.__startup_warmup_running = False
        self.__worker_epoch = 0
        self.__active_tooltip = None
        self.__failure_count = 0
        self.__collect_inflight = False
        self.__collect_inflight_source = ""
        self.__collect_started_ts = 0.0
        self.__next_collect_due_ts = 0.0
        self.__manual_query_waiting_result = False
        self.__manual_query_state_lock = threading.Lock()
        self.__playwright_checked = False
        self.__playwright_available = False
        self.__last_login_notice_ts = 0.0
        self.__login_notice_cooldown_sec = 600.0
        self.__last_playwright_notice_ts = 0.0
        self.__playwright_notice_cooldown_sec = 1800.0
        self.__last_interactive_login_ts = 0.0
        self.__interactive_login_cooldown_sec = 600.0
        self.__manual_interactive_reopen_cooldown_sec = 3.0
        self.__collect_lock = threading.Lock()
        self.__hidden_cdp_proc = None
        self.__hidden_cdp_port = 0
        self.__pending_hidden_cdp_clear = False

        self.__settings_version = 1
        self.__enabled = True
        self.__interval_sec = 90.0
        self.__min_interval_sec = 10.0
        self.__tooltip_duration_ms = 7000
        self.__usage_url = "https://chatgpt.com/codex/settings/usage"
        self.__login_entry_url = "https://chatgpt.com/auth/login?next=/codex/settings/usage"
        self.__navigation_timeout_ms = 30000
        self.__login_timeout_sec = 180.0
        self.__headless_wait_timeout_sec = 10.0
        self.__background_cloudflare_grace_sec = 6.0
        self.__korea_tz = timezone(timedelta(hours=9), name="KST")

        self.__last_snapshot = UsageSnapshot()

        base_dir = self.__lib.os.getenv("APPDATA")
        if not base_dir:
            base_dir = self.__lib.os.getenv("LOCALAPPDATA")
        if not base_dir:
            base_dir = self.__lib.os.path.expanduser("~")
        local_base = self.__lib.os.getenv("LOCALAPPDATA") or base_dir

        self.__config_dir = self.__lib.os.path.join(base_dir, "windows-supporter")
        self.__settings_path = self.__lib.os.path.join(
            self.__config_dir,
            "codex_usage_settings.json",
        )
        self.__state_path = self.__lib.os.path.join(
            self.__config_dir,
            "codex_usage_state.json",
        )
        self.__log_path = self.__lib.os.path.join(self.__config_dir, "codex_usage.log")
        self.__profile_dir = self.__lib.os.path.join(
            local_base,
            "windows-supporter",
            "chatgpt-profile",
        )

        self.__load_settings()
        self.__load_state()
        return

    def attach(self, root, event_queue=None) -> None:
        self.__root = root
        self.__event_queue = event_queue
        self.__restart_monitor()
        return

    def get_settings_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.__enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
            "settings_path": str(self.__settings_path),
            "state_path": str(self.__state_path),
            "profile_dir": str(self.__profile_dir),
        }

    def update_settings(self, data: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"
        enabled = bool(data.get("enabled", self.__enabled))
        usage_url = normalize_usage_value(data.get("usage_url", self.__usage_url))
        if not usage_url:
            usage_url = self.__usage_url
        try:
            interval_sec = float(data.get("interval_sec", self.__interval_sec))
        except Exception:
            return False, "interval"
        try:
            tooltip_ms = int(data.get("tooltip_duration_ms", self.__tooltip_duration_ms))
        except Exception:
            return False, "tooltip"
        min_interval = float(getattr(self, "_CodexUsageMonitor__min_interval_sec", 10.0) or 10.0)
        if interval_sec < min_interval:
            interval_sec = min_interval
        if tooltip_ms < 1200:
            tooltip_ms = 1200
        self.__enabled = enabled
        self.__usage_url = usage_url
        self.__interval_sec = float(interval_sec)
        self.__tooltip_duration_ms = int(tooltip_ms)
        self.__save_settings()
        self.__restart_monitor()
        return True, None

    def get_last_snapshot(self) -> UsageSnapshot:
        return UsageSnapshot.from_dict(self.__last_snapshot.to_dict())

    def get_runtime_status(self) -> dict[str, Any]:
        now = 0.0
        try:
            now = float(self.__lib.time.monotonic())
        except Exception:
            now = 0.0
        remain: float | None = None
        estimated = False
        due = float(self.__next_collect_due_ts or 0.0)
        if due > 0.0:
            remain = due - now
            if remain < 0.0:
                remain = 0.0
        elif bool(self.__monitor_running or self.__startup_warmup_running):
            started = float(self.__collect_started_ts or 0.0)
            if started > 0.0:
                remain = float(self.__interval_sec) - (now - started)
                if remain < 0.0:
                    remain = 0.0
            else:
                remain = float(self.__interval_sec)
            estimated = True
        return {
            "enabled": bool(self.__enabled),
            "collect_inflight": bool(self.__collect_inflight),
            "collect_source": str(self.__collect_inflight_source or ""),
            "monitor_running": bool(self.__monitor_running),
            "startup_warmup_running": bool(self.__startup_warmup_running),
            "next_collect_in_sec": remain,
            "next_collect_estimated": bool(estimated),
            "failure_count": int(self.__failure_count),
        }

    def format_captured_at_for_display(self, value: str) -> str:
        return self.__format_timestamp_display(str(value or ""))

    def show_current_status(self, force_refresh: bool = True) -> None:
        root = self.__root
        if root is None:
            return
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            snapshot = None if bool(force_refresh) else self.get_last_snapshot()
            error = None
            try:
                if bool(force_refresh):
                    refreshed, error = self.__collect_snapshot_guarded(
                        source="manual_query",
                        on_acquired=lambda: self.__ui_post(
                            lambda: self.__show_tooltip(
                                "Codex 사용량 조회 중...",
                                duration_ms=0,
                            )
                        ),
                    )
                    if error == "collect_busy":
                        self.__set_manual_query_pending_result()
                        self.__ui_post(self.__show_busy_collect_tooltip)
                        return
                    self.__consume_manual_query_pending_result()
                    if error is not None:
                        self.__handle_collect_error(error, source="manual_query")
                    if refreshed is not None:
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        merged = merge_snapshot_with_previous(
                            refreshed,
                            self.__last_snapshot if self.__last_snapshot.has_any_metric() else None,
                        )
                        self.__last_snapshot = merged
                        self.__save_state()
                        snapshot = merged
                if snapshot is not None and snapshot.has_any_metric():
                    self.__ui_post(
                        lambda: self.__show_snapshot_tooltip(
                            snapshot,
                            title="Codex 현재 사용량",
                        )
                    )
                    return
                msg = (
                    "사용량 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요."
                    if error is None
                    else f"사용량 조회 실패: {self.__describe_collect_error_for_user(error)}"
                )
                self.__ui_post(lambda: self.__show_tooltip(msg))
            except Exception as exc:
                self.__log_exception("manual status query failed", exc)
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        "사용량 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
                    )
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self.__log_exception("manual status worker start failed", exc)
            self.__ui_post(
                lambda: self.__show_tooltip(
                    "사용량 조회 작업을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요."
                )
            )
        return

    def handle_snapshot(self, snapshot: UsageSnapshot) -> list[UsageChange]:
        prev = self.__last_snapshot if self.__last_snapshot.has_any_metric() else None
        merged = merge_snapshot_with_previous(snapshot, prev)
        if not merged.has_any_metric():
            return []
        changes = compute_usage_changes(prev, merged)
        self.__last_snapshot = merged
        self.__save_state()
        return changes

    def __restart_monitor(self) -> None:
        root = self.__root
        if root is None:
            return
        try:
            if self.__monitor_after_id is not None:
                root.after_cancel(self.__monitor_after_id)
        except Exception:
            pass
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        try:
            self.__worker_epoch = int(self.__worker_epoch) + 1
        except Exception:
            self.__worker_epoch = 1
        self.__monitor_running = False
        self.__startup_warmup_running = False
        if bool(self.__collect_inflight):
            self.__pending_hidden_cdp_clear = True
        else:
            self.__clear_hidden_cdp_process(terminate=True)
        self.__start_startup_warmup()
        return

    def __start_startup_warmup(self) -> None:
        root = self.__root
        if root is None:
            return
        if not self.__enabled:
            self.__schedule_monitor_tick(initial_delay_sec=self.__interval_sec)
            return
        if self.__startup_warmup_running:
            return
        self.__startup_warmup_running = True
        self.__monitor_running = True
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            next_delay = float(self.__interval_sec)
            try:
                self.__log("startup warmup begin mode=headful-hidden-first")
                snapshot, error = self.__collect_snapshot_guarded(source="startup_warmup")
                if not self.__is_worker_epoch_current(worker_epoch):
                    self.__log("startup warmup stale result ignored")
                    return
                if error is not None:
                    if error == "collect_busy":
                        self.__log("startup warmup skipped reason=busy")
                        next_delay = min(self.__interval_sec, 5.0)
                        return
                    if error in {"parse_failed", "collect_failed"} and self.__has_manual_query_pending_result():
                        retry_snapshot, retry_error = self.__collect_snapshot_guarded(
                            source="startup_warmup_pending_retry"
                        )
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        if retry_error is None and retry_snapshot is not None:
                            error = None
                            snapshot = retry_snapshot
                        elif retry_error:
                            error = str(retry_error)
                    if error is None and snapshot is not None:
                        self.__failure_count = 0
                        changes = self.handle_snapshot(snapshot)
                        latest_snapshot = self.get_last_snapshot()
                        if changes:
                            self.__ui_post(lambda: self.__show_change_tooltip(changes, latest_snapshot))
                        self.__ui_post(
                            lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                                snap,
                                error=None,
                            )
                        )
                        self.__log("startup warmup end ok (pending retry)")
                        return
                    self.__ui_post(
                        lambda err=error: self.__show_pending_manual_result_if_needed(None, error=err)
                    )
                    self.__failure_count = min(self.__failure_count + 1, 8)
                    next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                    self.__handle_collect_error(error, source="startup_warmup")
                    self.__log(f"startup warmup end error={error}")
                    return
                self.__failure_count = 0
                if snapshot is not None:
                    changes = self.handle_snapshot(snapshot)
                    latest_snapshot = self.get_last_snapshot()
                    if changes:
                        self.__ui_post(lambda: self.__show_change_tooltip(changes, latest_snapshot))
                    self.__ui_post(
                        lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                            snap,
                            error=None,
                        )
                    )
                self.__log("startup warmup end ok")
            except Exception as exc:
                if not self.__is_worker_epoch_current(worker_epoch):
                    return
                self.__failure_count = min(self.__failure_count + 1, 8)
                next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                self.__log_exception("startup warmup failed", exc)
            finally:
                self.__ui_post(
                    lambda: self.__on_worker_done(
                        next_delay,
                        worker_epoch=worker_epoch,
                        from_startup=True,
                    )
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            if worker_epoch == int(self.__worker_epoch):
                self.__startup_warmup_running = False
                self.__monitor_running = False
            self.__log_exception("startup warmup thread start failed", exc)
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 10.0))
        return

    def __schedule_monitor_tick(self, initial_delay_sec: float | None = None) -> None:
        root = self.__root
        if root is None:
            return
        delay_sec = self.__interval_sec if initial_delay_sec is None else float(initial_delay_sec)
        if delay_sec < 1.0:
            delay_sec = 1.0
        delay_ms = int(delay_sec * 1000)
        try:
            self.__next_collect_due_ts = float(self.__lib.time.monotonic()) + float(delay_sec)
        except Exception:
            self.__next_collect_due_ts = 0.0
        try:
            self.__monitor_after_id = root.after(delay_ms, self.__monitor_tick)
        except Exception:
            self.__monitor_after_id = None
            self.__next_collect_due_ts = 0.0
        return

    def __monitor_tick(self) -> None:
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        if not self.__enabled:
            self.__schedule_monitor_tick(initial_delay_sec=self.__interval_sec)
            return
        if self.__monitor_running:
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 5.0))
            return
        self.__monitor_running = True
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            next_delay = float(self.__interval_sec)
            try:
                snapshot, error = self.__collect_snapshot_guarded(source="monitor_tick")
                if not self.__is_worker_epoch_current(worker_epoch):
                    self.__log("monitor worker stale result ignored")
                    return
                if error is not None:
                    if error == "collect_busy":
                        self.__log("monitor tick skipped reason=busy")
                        next_delay = min(self.__interval_sec, 5.0)
                        return
                    if error in {"parse_failed", "collect_failed"} and self.__has_manual_query_pending_result():
                        retry_snapshot, retry_error = self.__collect_snapshot_guarded(
                            source="monitor_tick_pending_retry"
                        )
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        if retry_error is None and retry_snapshot is not None:
                            error = None
                            snapshot = retry_snapshot
                        elif retry_error:
                            error = str(retry_error)
                    if error is None and snapshot is not None:
                        self.__failure_count = 0
                        changes = self.handle_snapshot(snapshot)
                        latest_snapshot = self.get_last_snapshot()
                        if changes:
                            self.__ui_post(lambda: self.__show_change_tooltip(changes, latest_snapshot))
                        self.__ui_post(
                            lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                                snap,
                                error=None,
                            )
                        )
                        return
                    self.__ui_post(
                        lambda err=error: self.__show_pending_manual_result_if_needed(None, error=err)
                    )
                    self.__failure_count = min(self.__failure_count + 1, 8)
                    next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                    self.__handle_collect_error(error, source="monitor_tick")
                    return
                self.__failure_count = 0
                if snapshot is None:
                    return
                changes = self.handle_snapshot(snapshot)
                latest_snapshot = self.get_last_snapshot()
                if changes:
                    self.__ui_post(lambda: self.__show_change_tooltip(changes, latest_snapshot))
                self.__ui_post(
                    lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                        snap,
                        error=None,
                    )
                )
            except Exception as exc:
                if not self.__is_worker_epoch_current(worker_epoch):
                    return
                self.__failure_count = min(self.__failure_count + 1, 8)
                next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                self.__log_exception("monitor worker failed", exc)
            finally:
                self.__ui_post(
                    lambda: self.__on_worker_done(
                        next_delay,
                        worker_epoch=worker_epoch,
                        from_startup=False,
                    )
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self.__monitor_running = False
            self.__log_exception("monitor thread start failed", exc)
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 15.0))
        return

    def __on_worker_done(
        self,
        next_delay: float,
        worker_epoch: int | None = None,
        from_startup: bool = False,
    ) -> None:
        if not self.__is_worker_epoch_current(worker_epoch):
            return
        if from_startup:
            self.__startup_warmup_running = False
        self.__monitor_running = False
        self.__schedule_monitor_tick(initial_delay_sec=next_delay)
        return

    def __is_worker_epoch_current(self, worker_epoch: int | None) -> bool:
        if worker_epoch is None:
            return True
        try:
            return int(worker_epoch) == int(self.__worker_epoch)
        except Exception:
            return False

    def __collect_snapshot_guarded(
        self,
        source: str,
        on_acquired=None,
    ) -> tuple[UsageSnapshot | None, str | None]:
        acquired = False
        try:
            acquired = bool(self.__collect_lock.acquire(blocking=False))
        except TypeError:
            try:
                acquired = bool(self.__collect_lock.acquire(False))
            except Exception as exc:
                self.__log_exception("collect lock acquire failed", exc)
                return None, "collect_failed"
        except Exception as exc:
            self.__log_exception("collect lock acquire failed", exc)
            return None, "collect_failed"
        if not acquired:
            self.__log(f"collect skip source={source} reason=busy")
            return None, "collect_busy"
        try:
            self.__collect_inflight = True
            self.__collect_inflight_source = str(source or "")
            try:
                self.__collect_started_ts = float(self.__lib.time.monotonic())
            except Exception:
                self.__collect_started_ts = 0.0
            self.__log(f"collect start source={source}")
            if callable(on_acquired):
                try:
                    on_acquired()
                except Exception:
                    pass
            snapshot, error = self.__collect_snapshot(source=str(source or ""))
            self.__log(f"collect end source={source} error={error or 'none'}")
            return snapshot, error
        finally:
            self.__collect_inflight = False
            self.__collect_inflight_source = ""
            self.__collect_started_ts = 0.0
            if bool(self.__pending_hidden_cdp_clear):
                self.__pending_hidden_cdp_clear = False
                self.__clear_hidden_cdp_process(terminate=True)
            try:
                self.__collect_lock.release()
            except Exception:
                pass

    def __ui_post(self, fn) -> None:
        queue_obj = self.__event_queue
        if queue_obj is not None:
            try:
                queue_obj.put(fn)
                return
            except Exception:
                pass
        root = self.__root
        if root is None:
            return
        try:
            root.after(0, fn)
        except Exception:
            return
        return

    def __show_change_tooltip(
        self,
        changes: list[UsageChange],
        snapshot: UsageSnapshot | None = None,
    ) -> None:
        root = self.__root
        if root is None or not changes:
            return
        current = snapshot if isinstance(snapshot, UsageSnapshot) else self.get_last_snapshot()
        lines: list[tuple[str, str | None]] = [("Codex 현재 사용량", None)]
        lines.extend(self.__build_snapshot_lines(current))
        lines.append(("", None))
        lines.append(("변경 항목", None))
        for item in changes:
            before = item.before if item.before else "-"
            after = item.after if item.after else "-"
            lines.append(
                (
                    f"- {item.label}: {before} -> {after}",
                    self.__resolve_change_color(item),
                )
            )
        self.__show_tooltip("", lines=lines)
        return

    def __show_snapshot_tooltip(self, snapshot: UsageSnapshot, title: str) -> None:
        lines: list[tuple[str, str | None]] = [(str(title or "Codex 현재 사용량"), None)]
        lines.extend(self.__build_snapshot_lines(snapshot))
        self.__show_tooltip("", lines=lines)
        return

    def __show_busy_collect_tooltip(self) -> None:
        self.__show_tooltip(
            "이미 Codex 사용량 조회가 진행 중입니다. 완료되면 결과를 자동으로 표시합니다.",
            duration_ms=0,
        )
        return

    def __set_manual_query_pending_result(self) -> None:
        try:
            with self.__manual_query_state_lock:
                self.__manual_query_waiting_result = True
        except Exception:
            self.__manual_query_waiting_result = True
        return

    def __consume_manual_query_pending_result(self) -> bool:
        try:
            with self.__manual_query_state_lock:
                if not bool(self.__manual_query_waiting_result):
                    return False
                self.__manual_query_waiting_result = False
                return True
        except Exception:
            pending = bool(self.__manual_query_waiting_result)
            self.__manual_query_waiting_result = False
            return pending

    def __has_manual_query_pending_result(self) -> bool:
        try:
            with self.__manual_query_state_lock:
                return bool(self.__manual_query_waiting_result)
        except Exception:
            return bool(self.__manual_query_waiting_result)

    def __show_pending_manual_result_if_needed(
        self,
        snapshot: UsageSnapshot | None,
        error: str | None = None,
    ) -> None:
        if not self.__consume_manual_query_pending_result():
            return
        err = normalize_usage_value(str(error or ""))
        if err:
            self.__show_tooltip(
                f"진행 중이던 조회가 실패했습니다. {self.__describe_collect_error_for_user(err)}"
            )
            return
        if snapshot is None or not snapshot.has_any_metric():
            self.__show_tooltip("조회가 완료되었지만 사용량을 확인하지 못했습니다.")
            return
        self.__show_snapshot_tooltip(snapshot, title="Codex 현재 사용량")
        return

    def __describe_collect_error_for_user(self, error: str) -> str:
        key = normalize_usage_value(str(error or "")).lower()
        if not key:
            return "잠시 후 다시 시도해 주세요."
        mapping = {
            "parse_failed": "페이지에서 사용량을 읽지 못했습니다.",
            "collect_failed": "조회 작업 중 오류가 발생했습니다.",
            "playwright_unavailable": "브라우저 런타임을 확인해 주세요.",
            "login_required": "로그인이 필요합니다.",
            "cloudflare_challenge": "Cloudflare 인증이 필요합니다.",
            "collect_busy": "이미 조회가 진행 중입니다.",
        }
        return mapping.get(key, "잠시 후 다시 시도해 주세요.")

    def __build_snapshot_lines(
        self,
        snapshot: UsageSnapshot | None,
        section_title: str | None = None,
    ) -> list[tuple[str, str | None]]:
        payload = snapshot.to_dict() if isinstance(snapshot, UsageSnapshot) else {}
        lines: list[tuple[str, str | None]] = []
        if section_title:
            lines.append((str(section_title), None))
        for key in USAGE_METRIC_KEYS:
            label = USAGE_METRIC_LABELS.get(key, key)
            value = normalize_usage_value(payload.get(key, ""))
            if not value:
                value = "-"
            lines.append((f"{label}: {value}", None))
        captured_at = normalize_usage_value(payload.get("captured_at", ""))
        if captured_at:
            lines.append((f"확인 시각: {self.__format_timestamp_display(captured_at)}", None))
        return lines

    def __format_timestamp_display(self, value: str) -> str:
        text = normalize_usage_value(value)
        if not text:
            return ""
        candidate = text
        try:
            normalized = candidate.replace("Z", "+00:00")
            parsed = self.__lib.datetime.fromisoformat(normalized)
            if parsed.tzinfo is not None:
                localized = parsed.astimezone(self.__korea_tz)
            else:
                localized = parsed
            return str(localized.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            return candidate.replace("T", " ")

    def __resolve_change_color(self, item: UsageChange) -> str | None:
        before_score = self.__metric_score_for_compare(item.key, item.before)
        after_score = self.__metric_score_for_compare(item.key, item.after)
        if before_score is None or after_score is None:
            return None
        if after_score > before_score:
            return "#16A34A"
        if after_score < before_score:
            return "#DC2626"
        return None

    def __metric_score_for_compare(self, key: str, value: str) -> float | None:
        text = normalize_usage_value(value)
        if not text or text == "-":
            return None

        ratio = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
        if ratio is not None:
            try:
                left = float(ratio.group(1))
            except Exception:
                return None
            if key in {"five_hour_limit", "weekly_limit", "code_review"}:
                # Usage ratios are treated as "used/limit", so lower is better.
                return -left
            return left

        percent = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
        if percent is not None:
            try:
                return float(percent.group(1))
            except Exception:
                return None

        raw = text.replace(",", "")
        number = re.search(r"(-?\d+(?:\.\d+)?)", raw)
        if number is None:
            return None
        try:
            return float(number.group(1))
        except Exception:
            return None

    def __show_tooltip(
        self,
        text: str,
        lines: list[tuple[str, str | None]] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        root = self.__root
        if root is None:
            return
        auto_hide_ms: int | None
        if duration_ms is None:
            duration = int(self.__tooltip_duration_ms)
            if duration < 1200:
                duration = 1200
            auto_hide_ms = duration
        else:
            try:
                duration = int(duration_ms)
            except Exception:
                duration = int(self.__tooltip_duration_ms)
            if duration <= 0:
                auto_hide_ms = None
            else:
                if duration < 1200:
                    duration = 1200
                auto_hide_ms = duration
        current = self.__active_tooltip
        if current is not None:
            try:
                current.hide_tooltip()
            except Exception:
                pass
        tooltip = ToolTip(
            root,
            str(text or ""),
            bind_events=False,
            auto_hide_ms=auto_hide_ms,
            keep_on_hover=True,
            lines=lines,
        )
        self.__active_tooltip = tooltip
        try:
            tooltip.show_tooltip()
        except Exception:
            return
        return

    def __handle_collect_error(self, error: str, source: str = "") -> None:
        msg = str(error or "unknown_error")
        if msg == "collect_busy":
            return
        self.__log(f"collect error: {msg}")
        normalized_source = normalize_usage_value(source).lower()
        is_manual_query = normalized_source == "manual_query"

        if msg in {"login_required", "cloudflare_challenge"}:
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = 0.0
            if (now - float(self.__last_login_notice_ts)) >= float(self.__login_notice_cooldown_sec):
                self.__last_login_notice_ts = now
                if msg == "cloudflare_challenge":
                    if is_manual_query:
                        message = (
                            "Cloudflare 인증이 필요합니다. 인증 창이 자동으로 열리지 않으면 "
                            "잠시 후 Ctrl+Alt+C로 다시 조회해 주세요."
                        )
                    else:
                        message = (
                            "Cloudflare 인증이 필요합니다. Ctrl+Alt+C로 수동 조회를 실행하면 "
                            "인증 창을 열어 확인할 수 있습니다."
                        )
                else:
                    if is_manual_query:
                        message = (
                            "Codex 로그인이 필요합니다. 로그인 창이 자동으로 열리지 않으면 "
                            "잠시 후 Ctrl+Alt+C로 다시 조회해 주세요."
                        )
                    else:
                        message = (
                            "Codex 로그인이 필요합니다. Ctrl+Alt+C로 수동 조회를 실행하면 "
                            "로그인 창을 열 수 있습니다."
                        )
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        message,
                    )
                )
        elif msg == "playwright_unavailable":
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = 0.0
            if (now - float(self.__last_playwright_notice_ts)) >= float(self.__playwright_notice_cooldown_sec):
                self.__last_playwright_notice_ts = now
                is_frozen = False
                try:
                    is_frozen = bool(getattr(self.__lib.sys, "frozen", False))
                except Exception:
                    is_frozen = False
                message = (
                    "Playwright 런타임 로드 실패: 빌드 포함 상태를 확인하세요."
                    if is_frozen
                    else "Playwright 런타임 로드 실패: 개발 환경 동기화 상태를 확인하세요."
                )
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        message,
                    )
                )
        return

    def __collect_snapshot(self, source: str = "") -> tuple[UsageSnapshot | None, str | None]:
        self.__configure_playwright_env()
        if not self.__ensure_playwright_available():
            return None, "playwright_unavailable"
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return None, "playwright_unavailable"

        try:
            with sync_playwright() as playwright_obj:
                return self.__collect_with_playwright_obj(playwright_obj, source=str(source or ""))
        except Exception as exc:
            self.__log_exception("collect snapshot failed", exc)
            return None, "collect_failed"

    def __collect_with_playwright_obj(
        self,
        playwright_obj,
        source: str = "",
    ) -> tuple[UsageSnapshot | None, str | None]:
        normalized_source = normalize_usage_value(source).lower()
        is_manual_query = normalized_source == "manual_query"
        self.__log("collect strategy=headful-hidden-first step=hidden")
        snapshot, error = self.__collect_snapshot_once(
            playwright_obj,
            headless=False,
            allow_interactive_recovery=False,
            force_hidden=True,
            prefer_system_channel=True,
        )
        if is_manual_query and error in {"login_required", "cloudflare_challenge"}:
            self.__log(
                f"collect strategy=headful-hidden-first pre-interactive-retry reason={error} "
                f"source={normalized_source}"
            )
            retry_snapshot, retry_error = self.__collect_snapshot_once(
                playwright_obj,
                headless=False,
                allow_interactive_recovery=False,
                force_hidden=True,
                prefer_system_channel=True,
            )
            if retry_error is None and retry_snapshot is not None:
                return retry_snapshot, None
            if retry_error is not None:
                error = str(retry_error)
                snapshot = retry_snapshot
        if error not in {"login_required", "cloudflare_challenge"}:
            return snapshot, error
        if not self.__should_open_interactive_recovery(source=source):
            self.__log(
                f"collect strategy=headful-hidden-first interactive=skip reason={error} "
                f"source={normalize_usage_value(source)}"
            )
            return None, error
        self.__log(
            f"collect strategy=headful-hidden-first interactive=open reason={error} "
            f"source={normalize_usage_value(source)}"
        )
        self.__prepare_interactive_recovery_launch(
            source=normalized_source,
            reason=str(error or ""),
        )
        if error == "login_required":
            notice = "Codex 로그인 창을 여는 중... 로그인 완료 후 자동으로 수집합니다."
        else:
            notice = "Cloudflare 인증 창을 여는 중... 인증 완료 후 자동으로 수집합니다."
        self.__ui_post(lambda: self.__show_tooltip(notice))
        return self.__collect_snapshot_once(
            playwright_obj,
            headless=False,
            allow_interactive_recovery=True,
            force_hidden=False,
            prefer_system_channel=True,
            initial_url=str(self.__login_entry_url),
        )

    def __should_open_interactive_recovery(self, source: str = "") -> bool:
        normalized_source = normalize_usage_value(source).lower()
        # Background collectors should never open visible auth windows.
        if normalized_source != "manual_query":
            return False
        now = 0.0
        try:
            now = float(self.__lib.time.monotonic())
        except Exception:
            now = 0.0
        cooldown_sec = float(self.__manual_interactive_reopen_cooldown_sec)
        if cooldown_sec < 0.0:
            cooldown_sec = 0.0
        if (now - float(self.__last_interactive_login_ts)) < cooldown_sec:
            return False
        self.__last_interactive_login_ts = now
        return True

    def __prepare_interactive_recovery_launch(self, source: str = "", reason: str = "") -> None:
        normalized_source = normalize_usage_value(source)
        normalized_reason = normalize_usage_value(reason)
        self.__log(
            "interactive recovery prep "
            f"source={normalized_source or 'unknown'} "
            f"reason={normalized_reason or 'unknown'}"
        )
        # Interactive recovery must not attach to stale hidden CDP sessions.
        self.__pending_hidden_cdp_clear = False
        had_hidden_proc = self.__hidden_cdp_proc is not None
        self.__clear_hidden_cdp_process(terminate=True)
        if not bool(had_hidden_proc):
            self.__terminate_profile_remote_debugging_processes()
        return

    def __collect_snapshot_once(
        self,
        playwright_obj,
        headless: bool,
        allow_interactive_recovery: bool = False,
        force_hidden: bool = False,
        prefer_system_channel: bool = False,
        initial_url: str | None = None,
    ) -> tuple[UsageSnapshot | None, str | None]:
        context = None
        cdp_browser = None
        cdp_proc = None
        keep_hidden_cdp_process = False
        usage_url = str(self.__usage_url)
        start_url = normalize_usage_value(initial_url)
        if not start_url:
            start_url = usage_url
        needs_usage_navigation = str(start_url) != usage_url
        effective_headless = bool(headless)
        try:
            if (not bool(effective_headless)) and bool(prefer_system_channel):
                if bool(force_hidden) and not bool(allow_interactive_recovery):
                    (
                        context,
                        cdp_browser,
                        cdp_proc,
                        keep_hidden_cdp_process,
                    ) = self.__connect_hidden_cdp_context(
                        playwright_obj,
                        launch_url=start_url,
                    )
                else:
                    context, cdp_browser, cdp_proc = self.__launch_interactive_context_via_cdp(
                        playwright_obj,
                        start_hidden=False,
                        initial_url=start_url,
                    )
            if context is None:
                launch_headless = bool(effective_headless)
                if (not launch_headless) and bool(force_hidden):
                    launch_headless = True
                context = self.__launch_browser_context(
                    playwright_obj,
                    headless=bool(launch_headless),
                    prefer_system_channel=bool(prefer_system_channel),
                )
                effective_headless = bool(launch_headless)
            if context is None:
                return None, "collect_failed"
            if bool(effective_headless):
                self.__apply_headless_fast_routes(context)
            if cdp_proc is not None:
                if bool(force_hidden):
                    self.__set_cdp_window_visibility(cdp_proc, visible=False, bring_to_front=False)
                elif bool(allow_interactive_recovery):
                    self.__set_cdp_window_visibility(cdp_proc, visible=True, bring_to_front=True)
            page = self.__select_collect_page(
                context,
                preferred_url=start_url,
                close_extra_blank_tabs=bool(force_hidden) and not bool(allow_interactive_recovery),
            )
            page.goto(
                str(start_url),
                wait_until="domcontentloaded",
                timeout=int(self.__navigation_timeout_ms),
            )
            if cdp_proc is not None and bool(force_hidden) and not bool(allow_interactive_recovery):
                # Navigation can trigger profile popups; re-hide the window defensively.
                self.__set_cdp_window_visibility(cdp_proc, visible=False, bring_to_front=False)

            if self.__is_cloudflare_challenge(page):
                if bool(effective_headless):
                    return None, "cloudflare_challenge"
                if not bool(allow_interactive_recovery):
                    grace_sec = 0.0
                    try:
                        grace_sec = float(self.__background_cloudflare_grace_sec)
                    except Exception:
                        grace_sec = 0.0
                    if grace_sec <= 0.0:
                        return None, "cloudflare_challenge"
                    ok_cf = self.__wait_until_cloudflare_cleared(
                        page,
                        timeout_sec=grace_sec,
                    )
                    if not ok_cf:
                        return None, "cloudflare_challenge"
                else:
                    if cdp_proc is not None:
                        self.__set_cdp_window_visibility(cdp_proc, visible=True, bring_to_front=True)
                    ok_cf = self.__wait_until_cloudflare_cleared(
                        page,
                        timeout_sec=max(float(self.__login_timeout_sec), 420.0),
                    )
                    if not ok_cf:
                        return None, "cloudflare_challenge"

            if self.__is_login_required(page):
                if bool(effective_headless) or not bool(allow_interactive_recovery):
                    return None, "login_required"
                if cdp_proc is not None:
                    self.__set_cdp_window_visibility(cdp_proc, visible=True, bring_to_front=True)
                ok = self.__wait_until_logged_in(page, timeout_sec=self.__login_timeout_sec)
                if not ok:
                    return None, "login_required"
            if needs_usage_navigation:
                try:
                    page.goto(
                        usage_url,
                        wait_until="domcontentloaded",
                        timeout=int(self.__navigation_timeout_ms),
                    )
                except Exception as exc:
                    self.__log_exception("navigate usage after login failed", exc)
                    return None, "collect_failed"

            snapshot = self.__build_snapshot_from_page(page)
            if snapshot is not None:
                return snapshot, None
            if bool(effective_headless):
                return self.__wait_for_snapshot_ready(
                    page,
                    timeout_sec=min(float(self.__login_timeout_sec), float(self.__headless_wait_timeout_sec)),
                )
            if not bool(effective_headless) and bool(allow_interactive_recovery):
                return self.__wait_for_snapshot_ready(page, timeout_sec=self.__login_timeout_sec)
            if not bool(effective_headless):
                return self.__wait_for_snapshot_ready(
                    page,
                    timeout_sec=float(self.__headless_wait_timeout_sec),
                )
            try:
                self.__log(
                    f"parse_failed url={str(page.url or '')} "
                    f"login={self.__is_login_required(page)} "
                    f"cloudflare={self.__is_cloudflare_challenge(page)}"
                )
            except Exception:
                pass
            return None, "parse_failed"
        except Exception as exc:
            if bool(keep_hidden_cdp_process):
                self.__clear_hidden_cdp_process(terminate=True)
            self.__log_exception("collect snapshot once failed", exc)
            return None, "collect_failed"
        finally:
            if context is not None and not bool(keep_hidden_cdp_process):
                try:
                    context.close()
                except Exception:
                    pass
            if cdp_browser is not None:
                try:
                    cdp_browser.close()
                except Exception:
                    pass
            if cdp_proc is not None and not bool(keep_hidden_cdp_process):
                self.__terminate_spawned_process(cdp_proc)

    def __build_snapshot_from_page(self, page) -> UsageSnapshot | None:
        captured_at = self.__now_iso()
        metrics = self.__extract_metrics(page)
        if not metrics:
            return None
        limit_keys = ("five_hour_limit", "weekly_limit", "code_review")
        has_limit_metric = any(normalize_usage_value(metrics.get(k, "")) for k in limit_keys)
        if not has_limit_metric:
            # Guard against noisy partial parses like standalone "0" values.
            return None
        snapshot = UsageSnapshot.from_metrics(metrics, captured_at=captured_at)
        if not snapshot.has_any_metric():
            return None
        return snapshot

    def __wait_for_snapshot_ready(self, page, timeout_sec: float) -> tuple[UsageSnapshot | None, str | None]:
        deadline = 0.0
        try:
            deadline = float(self.__lib.time.monotonic()) + float(timeout_sec)
        except Exception:
            deadline = 0.0
        next_home_recovery_ts = 0.0

        while True:
            snapshot = self.__build_snapshot_from_page(page)
            if snapshot is not None:
                return snapshot, None

            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = deadline + 1.0
            if now >= float(next_home_recovery_ts):
                current_url = self.__get_page_url(page)
                if self.__is_chatgpt_home_url(current_url):
                    if self.__is_login_required(page):
                        try:
                            if self.__try_open_login_entry(page, force=False):
                                next_home_recovery_ts = now + 4.0
                                continue
                        except Exception:
                            pass
                    else:
                        try:
                            page.goto(
                                str(self.__usage_url),
                                wait_until="domcontentloaded",
                                timeout=int(self.__navigation_timeout_ms),
                            )
                            try:
                                page.wait_for_timeout(900)
                            except Exception:
                                pass
                            self.__log("usage retry navigation from chatgpt home")
                            next_home_recovery_ts = now + 4.0
                            continue
                        except Exception as exc:
                            self.__log_exception("usage retry from home failed", exc)
                    next_home_recovery_ts = now + 4.0
            if now > deadline:
                if self.__is_cloudflare_challenge(page):
                    return None, "cloudflare_challenge"
                if self.__is_login_required(page):
                    return None, "login_required"
                return None, "parse_failed"
            try:
                page.wait_for_timeout(1500)
            except Exception:
                if self.__is_cloudflare_challenge(page):
                    return None, "cloudflare_challenge"
                if self.__is_login_required(page):
                    return None, "login_required"
                return None, "parse_failed"

    def __get_page_url(self, page) -> str:
        try:
            return str(page.url or "")
        except Exception:
            return ""

    def __is_chatgpt_home_url(self, url: str) -> bool:
        lowered = str(url or "").strip().lower()
        if not lowered.startswith("https://chatgpt.com"):
            return False
        tail = lowered[len("https://chatgpt.com") :]
        if not tail:
            return True
        if tail == "/":
            return True
        if tail.startswith("/?") or tail.startswith("/#"):
            return True
        return False

    def __is_blank_page_url(self, url: str) -> bool:
        lowered = str(url or "").strip().lower()
        if not lowered:
            return True
        return lowered in {
            "about:blank",
            "chrome://newtab/",
            "chrome://newtab",
            "chrome://new-tab-page/",
            "chrome://new-tab-page",
            "edge://newtab/",
            "edge://newtab",
        }

    def __select_collect_page(self, context, preferred_url: str, close_extra_blank_tabs: bool = False):
        pages = []
        try:
            pages = list(context.pages or [])
        except Exception:
            pages = []

        preferred = normalize_usage_value(preferred_url)
        selected = None
        for candidate in pages:
            url = normalize_usage_value(self.__get_page_url(candidate))
            if preferred and url == preferred:
                selected = candidate
                break

        if selected is None:
            for candidate in pages:
                if not self.__is_blank_page_url(self.__get_page_url(candidate)):
                    selected = candidate
                    break

        if selected is None:
            selected = pages[0] if pages else context.new_page()

        if bool(close_extra_blank_tabs):
            for candidate in pages:
                if candidate is selected:
                    continue
                if not self.__is_blank_page_url(self.__get_page_url(candidate)):
                    continue
                try:
                    candidate.close()
                except Exception:
                    continue
        return selected

    def __launch_interactive_context_via_cdp(
        self,
        playwright_obj,
        start_hidden: bool = False,
        initial_url: str | None = None,
    ):
        chrome_path = self.__resolve_chrome_executable_path()
        if not chrome_path:
            return None, None, None
        try:
            self.__lib.os.makedirs(self.__profile_dir, exist_ok=True)
        except Exception:
            pass

        last_error = None
        for port in range(9333, 9345):
            proc = None
            browser = None
            try:
                existing_pid = self.__find_profile_remote_debugging_pid(int(port))
                if existing_pid > 0:
                    self.__log(
                        f"interactive cdp skip occupied profile port={int(port)} pid={int(existing_pid)}"
                    )
                    continue
                launch_url = normalize_usage_value(initial_url)
                if not launch_url:
                    launch_url = str(self.__usage_url)
                cmd = [
                    str(chrome_path),
                    f"--remote-debugging-port={int(port)}",
                    f"--user-data-dir={self.__profile_dir}",
                ]
                if bool(start_hidden):
                    cmd.extend(
                        [
                            "--window-size=1280,720",
                            "--disable-extensions",
                            "--disable-notifications",
                            "--disable-session-crashed-bubble",
                            "--no-first-run",
                            "--no-default-browser-check",
                        ]
                    )
                cmd.extend(["--new-window", str(launch_url)])
                popen_kwargs: dict[str, Any] = {}
                if bool(start_hidden):
                    try:
                        startupinfo = self.__lib.subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= int(
                            getattr(self.__lib.subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
                        )
                        startupinfo.wShowWindow = int(
                            getattr(self.__lib.subprocess, "SW_HIDE", 0)
                        )
                        popen_kwargs["startupinfo"] = startupinfo
                    except Exception:
                        pass
                proc = self.__lib.subprocess.Popen(cmd, **popen_kwargs)
                endpoint = f"http://127.0.0.1:{int(port)}"
                deadline = 0.0
                try:
                    deadline = float(self.__lib.time.monotonic()) + 15.0
                except Exception:
                    deadline = 0.0
                while True:
                    try:
                        browser = playwright_obj.chromium.connect_over_cdp(endpoint)
                        break
                    except Exception as exc:
                        last_error = exc
                        now = 0.0
                        try:
                            now = float(self.__lib.time.monotonic())
                        except Exception:
                            now = deadline + 1.0
                        if now > deadline:
                            break
                        try:
                            self.__lib.time.sleep(0.35)
                        except Exception:
                            pass

                if browser is None:
                    self.__terminate_spawned_process(proc)
                    continue
                contexts = []
                try:
                    contexts = list(browser.contexts or [])
                except Exception:
                    contexts = []
                if not contexts:
                    try:
                        browser.close()
                    except Exception:
                        pass
                    self.__terminate_spawned_process(proc)
                    last_error = RuntimeError("cdp browser has no context")
                    continue
                spawned_pid = 0
                try:
                    spawned_pid = int(getattr(proc, "pid", 0) or 0)
                except Exception:
                    spawned_pid = 0
                listener_pid = self.__find_profile_remote_debugging_pid(int(port))
                if spawned_pid > 0 and listener_pid > 0 and listener_pid != spawned_pid:
                    self.__log(
                        "interactive cdp endpoint mismatch "
                        f"port={int(port)} spawned={int(spawned_pid)} listener={int(listener_pid)}"
                    )
                    try:
                        browser.close()
                    except Exception:
                        pass
                    self.__terminate_spawned_process(proc)
                    last_error = RuntimeError("cdp endpoint pid mismatch")
                    continue
                if spawned_pid > 0 and (not self.__is_subprocess_running(proc)):
                    self.__log(
                        "interactive cdp process exited early "
                        f"port={int(port)} pid={int(spawned_pid)}"
                    )
                    try:
                        browser.close()
                    except Exception:
                        pass
                    self.__terminate_spawned_process(proc)
                    last_error = RuntimeError("cdp spawned process exited")
                    continue
                self.__log(
                    f"interactive cdp connected port={int(port)} pid={int(spawned_pid)}"
                )
                try:
                    setattr(proc, "_ws_cdp_port", int(port))
                except Exception:
                    pass
                return contexts[0], browser, proc
            except Exception as exc:
                last_error = exc
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                if proc is not None:
                    self.__terminate_spawned_process(proc)
                continue
        if last_error is not None:
            self.__log_exception("interactive cdp launch failed", last_error)
        return None, None, None

    def __is_subprocess_running(self, proc) -> bool:
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            return False

    def __clear_hidden_cdp_process(self, terminate: bool = False) -> None:
        proc = self.__hidden_cdp_proc
        self.__hidden_cdp_proc = None
        self.__hidden_cdp_port = 0
        if bool(terminate) and proc is not None:
            self.__terminate_spawned_process(proc)
        return

    def __connect_hidden_cdp_context(self, playwright_obj, launch_url: str | None = None):
        proc = self.__hidden_cdp_proc
        port = 0
        try:
            port = int(self.__hidden_cdp_port or 0)
        except Exception:
            port = 0

        if proc is not None:
            if (not self.__is_subprocess_running(proc)) or (port <= 0):
                self.__clear_hidden_cdp_process(terminate=True)
                proc = None
                port = 0

        if proc is not None and port > 0:
            endpoint = f"http://127.0.0.1:{int(port)}"
            reconnect_browser = None
            try:
                reconnect_browser = playwright_obj.chromium.connect_over_cdp(endpoint)
                contexts = []
                try:
                    contexts = list(reconnect_browser.contexts or [])
                except Exception:
                    contexts = []
                if contexts:
                    return contexts[0], reconnect_browser, proc, True
            except Exception as exc:
                self.__log_exception("hidden cdp reconnect failed", exc)
            if reconnect_browser is not None:
                try:
                    reconnect_browser.close()
                except Exception:
                    pass
            self.__clear_hidden_cdp_process(terminate=True)

        context, browser, proc = self.__launch_interactive_context_via_cdp(
            playwright_obj,
            start_hidden=True,
            initial_url=launch_url,
        )
        if proc is None:
            return context, browser, proc, False
        try:
            port = int(getattr(proc, "_ws_cdp_port", 0) or 0)
        except Exception:
            port = 0
        if port > 0:
            self.__hidden_cdp_proc = proc
            self.__hidden_cdp_port = int(port)
            return context, browser, proc, True
        return context, browser, proc, False

    def __set_cdp_window_visibility(
        self,
        proc,
        visible: bool,
        bring_to_front: bool = False,
        timeout_sec: float = 3.0,
    ) -> bool:
        if proc is None:
            return False
        try:
            pid = int(getattr(proc, "pid", 0) or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            return False
        primary = self.__set_windows_visibility_for_pid(
            pid=pid,
            visible=bool(visible),
            bring_to_front=bool(bring_to_front),
            timeout_sec=float(timeout_sec),
        )
        if primary:
            return True
        fallback_timeout = float(timeout_sec)
        if fallback_timeout > 1.0:
            fallback_timeout = 1.0
        for alt_pid in self.__list_profile_chrome_pids():
            try:
                candidate = int(alt_pid)
            except Exception:
                continue
            if candidate <= 0 or candidate == pid:
                continue
            if self.__set_windows_visibility_for_pid(
                pid=candidate,
                visible=bool(visible),
                bring_to_front=bool(bring_to_front),
                timeout_sec=fallback_timeout,
            ):
                return True
        return False

    def __set_windows_visibility_for_pid(
        self,
        pid: int,
        visible: bool,
        bring_to_front: bool = False,
        timeout_sec: float = 3.0,
    ) -> bool:
        if int(pid) <= 0:
            return False
        try:
            if str(self.__lib.os.name).lower() != "nt":
                return False
        except Exception:
            return False

        now = 0.0
        deadline = 0.0
        try:
            now = float(self.__lib.time.monotonic())
            deadline = now + max(float(timeout_sec), 0.2)
        except Exception:
            deadline = 0.0

        while True:
            handles = self.__list_top_windows_for_pid(int(pid))
            if handles:
                changed = False
                for hwnd in handles:
                    try:
                        if bool(visible):
                            self.__lib.win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
                            if bool(bring_to_front):
                                try:
                                    self.__lib.win32gui.SetForegroundWindow(hwnd)
                                except Exception:
                                    pass
                        else:
                            self.__lib.win32gui.ShowWindow(hwnd, 0)  # SW_HIDE
                        changed = True
                    except Exception:
                        continue
                if changed:
                    return True
            if deadline <= 0.0:
                break
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = deadline + 1.0
            if now >= deadline:
                break
            try:
                self.__lib.time.sleep(0.15)
            except Exception:
                break
        return False

    def __list_top_windows_for_pid(self, pid: int) -> list[int]:
        handles: list[int] = []
        try:
            target_pid = int(pid)
        except Exception:
            target_pid = 0
        if target_pid <= 0:
            return handles

        def _collect(hwnd, _lparam):
            try:
                _, wnd_pid = self.__lib.win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return True
            if int(wnd_pid) != target_pid:
                return True
            try:
                parent = self.__lib.win32gui.GetParent(hwnd)
                if parent:
                    return True
            except Exception:
                pass
            handles.append(int(hwnd))
            return True

        try:
            self.__lib.win32gui.EnumWindows(_collect, 0)
        except Exception:
            return handles
        return handles

    def __list_profile_chrome_pids(self) -> list[int]:
        target_profile = str(self.__profile_dir or "").strip().lower()
        if not target_profile:
            return []
        pids: list[int] = []
        try:
            proc_iter = self.__lib.psutil.process_iter(attrs=["pid", "name", "cmdline"])
        except Exception:
            return pids
        for item in proc_iter:
            try:
                info = item.info if hasattr(item, "info") else {}
                name = str((info or {}).get("name") or "").lower()
                if "chrome" not in name:
                    continue
                cmdline = (info or {}).get("cmdline") or []
                cmd_text = " ".join(str(x) for x in cmdline).lower()
                if not cmd_text:
                    continue
                if f"--user-data-dir={target_profile}" not in cmd_text:
                    continue
                pid = int((info or {}).get("pid") or 0)
                if pid > 0:
                    pids.append(pid)
            except Exception:
                continue
        seen: set[int] = set()
        ordered: list[int] = []
        for pid in pids:
            if pid in seen:
                continue
            seen.add(pid)
            ordered.append(pid)
        return ordered

    def __find_profile_remote_debugging_pid(self, port: int) -> int:
        target_profile = str(self.__profile_dir or "").strip().lower()
        if not target_profile:
            return 0
        try:
            target_port = int(port)
        except Exception:
            return 0
        if target_port <= 0:
            return 0

        port_token = f"--remote-debugging-port={target_port}"
        profile_token = f"--user-data-dir={target_profile}"
        try:
            proc_iter = self.__lib.psutil.process_iter(attrs=["pid", "name", "cmdline"])
        except Exception:
            return 0
        for item in proc_iter:
            try:
                info = item.info if hasattr(item, "info") else {}
                name = str((info or {}).get("name") or "").lower()
                if "chrome" not in name:
                    continue
                cmdline = (info or {}).get("cmdline") or []
                cmd_text = " ".join(str(x) for x in cmdline).lower()
                if not cmd_text:
                    continue
                if port_token not in cmd_text:
                    continue
                if profile_token not in cmd_text:
                    continue
                pid = int((info or {}).get("pid") or 0)
                if pid > 0:
                    return pid
            except Exception:
                continue
        return 0

    def __resolve_chrome_executable_path(self) -> str:
        candidates = [
            self.__lib.os.path.join(
                self.__lib.os.getenv("PROGRAMFILES", ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            self.__lib.os.path.join(
                self.__lib.os.getenv("PROGRAMFILES(X86)", ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            self.__lib.os.path.join(
                self.__lib.os.getenv("LOCALAPPDATA", ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
        ]
        for candidate in candidates:
            path = str(candidate or "").strip()
            if not path:
                continue
            try:
                if self.__lib.os.path.isfile(path):
                    return path
            except Exception:
                continue
        return ""

    def __terminate_spawned_process(self, proc) -> None:
        # 1) Stop the direct spawned process (if still alive).
        if proc is not None:
            running = False
            try:
                running = proc.poll() is None
            except Exception:
                running = False
            if running:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=6.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass

        # 2) Cleanup orphaned Chrome instances attached to this monitor profile.
        # Some Chrome launches can detach from the original parent process,
        # leaving visible windows even after the collected snapshot returns.
        self.__terminate_profile_remote_debugging_processes()
        return

    def __terminate_profile_remote_debugging_processes(self) -> None:
        target_profile = str(self.__profile_dir or "").strip().lower()
        if not target_profile:
            return

        to_kill: list[Any] = []
        try:
            proc_iter = self.__lib.psutil.process_iter(attrs=["name", "cmdline"])
        except Exception:
            return

        for item in proc_iter:
            try:
                info = item.info if hasattr(item, "info") else {}
                name = str((info or {}).get("name") or "").lower()
                if "chrome" not in name:
                    continue
                cmdline = (info or {}).get("cmdline") or []
                cmd_text = " ".join(str(x) for x in cmdline).lower()
                if not cmd_text:
                    continue
                if f"--user-data-dir={target_profile}" not in cmd_text:
                    continue
                if "--remote-debugging-port=" not in cmd_text:
                    continue
                to_kill.append(item)
            except Exception:
                continue

        for item in to_kill:
            try:
                children = item.children(recursive=True)
            except Exception:
                children = []
            for child in children:
                try:
                    child.terminate()
                except Exception:
                    continue
            try:
                item.terminate()
            except Exception:
                continue

        for item in to_kill:
            try:
                item.wait(timeout=2.0)
            except Exception:
                try:
                    item.kill()
                except Exception:
                    continue

    def __apply_headless_fast_routes(self, context) -> None:
        try:
            blockers = (
                "google-analytics",
                "googletagmanager",
                "doubleclick",
                "hotjar",
                "segment.io",
                "sentry.io",
                "intercom",
            )

            def _route_handler(route, request):
                try:
                    rtype = str(getattr(request, "resource_type", "") or "").lower()
                except Exception:
                    rtype = ""
                if rtype in {"image", "font", "media"}:
                    try:
                        route.abort()
                    except Exception:
                        try:
                            route.continue_()
                        except Exception:
                            pass
                    return
                url = ""
                try:
                    url = str(getattr(request, "url", "") or "").lower()
                except Exception:
                    url = ""
                if url and any(token in url for token in blockers):
                    try:
                        route.abort()
                    except Exception:
                        try:
                            route.continue_()
                        except Exception:
                            pass
                    return
                try:
                    route.continue_()
                except Exception:
                    return

            context.route("**/*", _route_handler)
        except Exception:
            return
        return

    def __launch_browser_context(
        self,
        playwright_obj,
        headless: bool,
        prefer_system_channel: bool = False,
    ):
        channels: list[str | None] = [None]
        if bool(prefer_system_channel):
            if bool(headless):
                channels = ["chrome", None]
            else:
                channels = ["chrome"]
        last_error = None
        for channel in channels:
            # Keep browser sandbox enabled to avoid auth instability
            # from unsupported --no-sandbox launches.
            kwargs = {
                "headless": bool(headless),
                "chromium_sandbox": True,
            }
            if channel:
                kwargs["channel"] = channel
            if not bool(headless):
                kwargs["no_viewport"] = True
            try:
                return playwright_obj.chromium.launch_persistent_context(
                    self.__profile_dir,
                    **kwargs,
                )
            except Exception as exc:
                last_error = exc
                try:
                    self.__log(
                        f"launch context failed channel={channel or 'bundled'} err={exc!r}"
                    )
                except Exception:
                    pass
                continue
        if last_error is not None:
            self.__log_exception("all browser launch attempts failed", last_error)
        return None

    def __wait_until_logged_in(self, page, timeout_sec: float) -> bool:
        deadline = 0.0
        try:
            deadline = float(self.__lib.time.monotonic()) + float(timeout_sec)
        except Exception:
            deadline = 0.0
        attempted_open = False
        stagnant_count = 0
        last_url = ""
        while True:
            if self.__is_cloudflare_challenge(page):
                now_cf = 0.0
                try:
                    now_cf = float(self.__lib.time.monotonic())
                except Exception:
                    now_cf = deadline + 1.0
                remain = max(5.0, float(deadline) - float(now_cf))
                if not self.__wait_until_cloudflare_cleared(page, timeout_sec=min(60.0, remain)):
                    return False

            if not self.__is_login_required(page):
                return True

            did_open = self.__try_open_login_entry(
                page,
                force=(not attempted_open or stagnant_count >= 2),
            )
            if did_open:
                attempted_open = True

            current_url = ""
            try:
                current_url = str(page.url or "")
            except Exception:
                current_url = ""
            if current_url and current_url == last_url:
                stagnant_count += 1
            else:
                stagnant_count = 0
                last_url = current_url

            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = deadline + 1.0
            if now > deadline:
                return False
            try:
                page.wait_for_timeout(1000)
            except Exception:
                return False

    def __try_open_login_entry(self, page, force: bool = False) -> bool:
        url = ""
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        lowered = url.lower()
        if self.__is_auth_invalid_state(page):
            for selector in (
                "button:has-text('Try again')",
                "button:has-text('다시 시도')",
                "button[type='submit']",
            ):
                try:
                    locator = page.locator(selector)
                except Exception:
                    continue
                try:
                    if locator.count() <= 0:
                        continue
                except Exception:
                    continue
                try:
                    locator.first.click(timeout=1500)
                    self.__log(f"auth error recovery clicked selector={selector}")
                    return True
                except Exception:
                    continue
            if force:
                try:
                    page.goto(
                        str(self.__login_entry_url),
                        wait_until="domcontentloaded",
                        timeout=int(self.__navigation_timeout_ms),
                    )
                    self.__log("auth error recovery navigated login entry")
                    return True
                except Exception:
                    pass

        # Do not force-refresh login pages while the user is interacting
        # (e.g., Google OAuth), otherwise the auth flow keeps restarting.
        if (
            "/auth/login" in lowered
            or "/log-in" in lowered
            or "auth.openai.com" in lowered
        ):
            return False

        selectors = [
            "button:has-text('Log in')",
            "button:has-text('로그인')",
            "a:has-text('Log in')",
            "a:has-text('로그인')",
            "[data-testid*='login']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
            except Exception:
                continue
            try:
                if locator.count() <= 0:
                    continue
            except Exception:
                continue
            try:
                locator.first.click(timeout=1500)
                self.__log(f"login entry clicked selector={selector}")
                return True
            except Exception:
                continue

        if not force:
            return False

        for candidate in (
            str(self.__login_entry_url),
            "https://chatgpt.com/auth/login",
            "https://auth.openai.com/log-in-or-create-account",
        ):
            try:
                page.goto(
                    candidate,
                    wait_until="domcontentloaded",
                    timeout=int(self.__navigation_timeout_ms),
                )
                self.__log(f"login entry navigated url={candidate}")
                return True
            except Exception:
                continue
        return False

    def __is_auth_invalid_state(self, page) -> bool:
        body_text = ""
        try:
            body_text = str(
                page.evaluate("() => document && document.body ? (document.body.innerText || '') : ''")
                or ""
            ).lower()
        except Exception:
            body_text = ""
        if not body_text:
            return False
        if "invalid_state" in body_text:
            return True
        if "route error" in body_text:
            return True
        if "invalid content type" in body_text:
            return True
        if "error occurred during authentication" in body_text:
            return True
        if "인증 중 오류" in body_text:
            return True
        return False

    def __is_cloudflare_challenge(self, page) -> bool:
        url = ""
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        lowered_url = url.lower()
        has_cloudflare_query_token = False
        if "challenges.cloudflare.com" in lowered_url:
            return True
        if "cdn-cgi/challenge" in lowered_url:
            return True
        if "__cf_chl_rt_tk=" in lowered_url:
            has_cloudflare_query_token = True
        elif "__cf_chl_" in lowered_url:
            has_cloudflare_query_token = True

        body_text = ""
        try:
            body_text = str(
                page.evaluate("() => document && document.body ? (document.body.innerText || '') : ''")
                or ""
            ).lower()
        except Exception:
            body_text = ""
        has_usage_limit_metric = False
        if body_text:
            try:
                parsed = parse_usage_metrics_from_text(body_text)
                has_usage_limit_metric = any(
                    normalize_usage_value(parsed.get(k, ""))
                    for k in ("five_hour_limit", "weekly_limit", "code_review")
                )
            except Exception:
                has_usage_limit_metric = False
        if has_usage_limit_metric:
            return False
        if body_text:
            if "verify you are human" in body_text and "cloudflare" in body_text:
                return True
            if "checking your browser" in body_text and "cloudflare" in body_text:
                return True
        html_text = ""
        try:
            html_text = str(page.content() or "").lower()
        except Exception:
            html_text = ""
        if not html_text:
            return bool(has_cloudflare_query_token and not body_text)
        if "challenges.cloudflare.com" in html_text:
            return True
        if "cdn-cgi/challenge-platform" in html_text:
            return True
        if "cf-challenge" in html_text:
            return True
        if has_cloudflare_query_token and not body_text:
            return True
        return False

    def __wait_until_cloudflare_cleared(self, page, timeout_sec: float) -> bool:
        deadline = 0.0
        try:
            deadline = float(self.__lib.time.monotonic()) + float(timeout_sec)
        except Exception:
            deadline = 0.0
        while True:
            if not self.__is_cloudflare_challenge(page):
                return True
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = deadline + 1.0
            if now > deadline:
                return False
            try:
                page.wait_for_timeout(1500)
            except Exception:
                return False

    def __extract_metrics(self, page) -> dict[str, str]:
        body_text = ""
        try:
            body_text = str(
                page.evaluate("() => document && document.body ? (document.body.innerText || '') : ''")
                or ""
            )
        except Exception:
            body_text = ""

        metrics = parse_usage_metrics_from_text(body_text)
        if len(metrics) >= 2:
            return metrics

        # Fallback: parse html source as plain text.
        content = ""
        try:
            content = str(page.content() or "")
        except Exception:
            content = ""
        if content:
            plain = self.__html_to_text(content)
            fallback_metrics = parse_usage_metrics_from_text(plain)
            if len(fallback_metrics) > len(metrics):
                metrics = fallback_metrics
        return metrics

    def __html_to_text(self, html_text: str) -> str:
        text = str(html_text or "")
        if not text:
            return ""
        try:
            import re

            text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
            text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", "\n", text)
        except Exception:
            pass
        text = html.unescape(text)
        return text

    def __is_login_required(self, page) -> bool:
        url = ""
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        lowered = url.lower()
        if any(token in lowered for token in ("login", "signin", "auth")):
            return True
        try:
            if page.locator("input[type='password']").count() > 0:
                return True
        except Exception:
            pass
        try:
            body_text = str(
                page.evaluate("() => document && document.body ? (document.body.innerText || '') : ''")
                or ""
            ).lower()
        except Exception:
            body_text = ""
        if not body_text:
            return False
        markers = (
            "log in",
            "sign in",
            "sign up",
            "로그인",
            "회원가입",
            "continue with google",
            "continue with email",
        )
        return any(marker in body_text for marker in markers)

    def __configure_playwright_env(self) -> None:
        try:
            is_frozen = bool(getattr(self.__lib.sys, "frozen", False))
        except Exception:
            is_frozen = False
        try:
            if is_frozen:
                self.__lib.os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
            else:
                self.__lib.os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        except Exception:
            return
        try:
            raw = str(self.__lib.os.environ.get("NODE_OPTIONS", "") or "").strip()
            tokens = [token for token in raw.split(" ") if token]
            if "--no-deprecation" not in tokens:
                tokens.append("--no-deprecation")
                self.__lib.os.environ["NODE_OPTIONS"] = " ".join(tokens).strip()
        except Exception:
            pass
        return

    def __ensure_playwright_available(self) -> bool:
        if self.__playwright_checked:
            return bool(self.__playwright_available)
        self.__playwright_checked = True
        self.__configure_playwright_env()
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401

            self.__playwright_available = True
            return True
        except Exception as exc:
            self.__playwright_available = False
            self.__log_exception("playwright import failed", exc)
            return False

    def __load_settings(self) -> None:
        data = self.__read_json_file(self.__settings_path)
        if not isinstance(data, dict):
            data = {}
        try:
            self.__enabled = bool(data.get("enabled", self.__enabled))
        except Exception:
            self.__enabled = True
        try:
            interval = float(data.get("interval_sec", self.__interval_sec))
        except Exception:
            interval = self.__interval_sec
        min_interval = float(getattr(self, "_CodexUsageMonitor__min_interval_sec", 10.0) or 10.0)
        if interval < min_interval:
            interval = min_interval
        self.__interval_sec = float(interval)
        try:
            tooltip = int(data.get("tooltip_duration_ms", self.__tooltip_duration_ms))
        except Exception:
            tooltip = self.__tooltip_duration_ms
        if tooltip < 1200:
            tooltip = 1200
        self.__tooltip_duration_ms = int(tooltip)
        usage_url = normalize_usage_value(data.get("usage_url", self.__usage_url))
        if usage_url:
            self.__usage_url = usage_url
        self.__save_settings()
        return

    def __save_settings(self) -> None:
        payload = {
            "settings_version": int(self.__settings_version),
            "enabled": bool(self.__enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
        }
        self.__write_json_file(self.__settings_path, payload)
        return

    def __load_state(self) -> None:
        data = self.__read_json_file(self.__state_path)
        if not isinstance(data, dict):
            self.__last_snapshot = UsageSnapshot()
            return
        snap = UsageSnapshot.from_dict(data.get("last_snapshot"))
        self.__last_snapshot = snap
        return

    def __save_state(self) -> None:
        payload = {
            "last_snapshot": self.__last_snapshot.to_dict(),
        }
        self.__write_json_file(self.__state_path, payload)
        return

    def __read_json_file(self, path: str) -> dict | None:
        if not path:
            return None
        try:
            if not self.__lib.os.path.isfile(path):
                return None
        except Exception:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fp:
                raw = fp.read()
        except Exception:
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def __write_json_file(self, path: str, payload: dict) -> None:
        if not path:
            return
        try:
            self.__lib.os.makedirs(self.__config_dir, exist_ok=True)
        except Exception:
            pass
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.__log_exception("json write failed", exc)
        return

    def __now_iso(self) -> str:
        try:
            utc_now = self.__lib.datetime.now(timezone.utc)
            local_now = utc_now.astimezone(self.__korea_tz)
            return str(local_now.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            return ""

    def __log(self, message: str) -> None:
        try:
            self.__lib.os.makedirs(self.__config_dir, exist_ok=True)
        except Exception:
            return
        ts = self.__now_iso() or "time"
        line = f"[{ts}] {str(message)}\n"
        try:
            with open(self.__log_path, "a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            return

    def __log_exception(self, title: str, exc: Exception) -> None:
        try:
            self.__log(f"{title}: {exc!r}")
            tb = traceback.format_exc()
            if tb:
                self.__log(tb.strip())
        except Exception:
            return
