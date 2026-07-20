from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import re
import shutil
import stat
import threading
from typing import Any, Protocol
import uuid

from src.apps.ai_usage_contracts import (
    AiUsageProvider,
    AiUsageReading,
    UsageErrorType,
    UsageState,
    normalize_usage_error_type,
    normalize_usage_state,
    project_usage_provider_status,
    usage_state_message,
)
from src.apps.codex_usage_browser_types import (
    BrowserErrorCode,
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    LogSink,
    PlaywrightSessionConfig,
    PlaywrightStarter,
)
from src.apps.cursor_usage_playwright_driver import CursorUsagePlaywrightDriver
from src.apps.cursor_usage_playwright_worker import run_cursor_playwright_worker


CURSOR_USAGE_URL = "https://cursor.com/dashboard/usage"


def _is_non_reparse_descendant(candidate: str, boundary: str) -> bool:
    target = os.path.abspath(candidate)
    root = os.path.abspath(boundary)
    try:
        if os.path.normcase(os.path.commonpath((target, root))) != os.path.normcase(root):
            return False
        if os.path.normcase(target) == os.path.normcase(root):
            return False
        real_target = os.path.realpath(target)
        real_root = os.path.realpath(root)
        if os.path.normcase(os.path.commonpath((real_target, real_root))) != os.path.normcase(
            real_root
        ):
            return False
        relative = os.path.relpath(target, root)
    except (OSError, ValueError):
        return False
    current = root
    for part in ("", *relative.split(os.sep)):
        if part:
            current = os.path.join(current, part)
        if not os.path.lexists(current):
            continue
        try:
            info = os.lstat(current)
        except OSError:
            return False
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or (reparse_flag and attributes & reparse_flag):
            return False
    return True
CURSOR_COLLECTION_MODE = "visible_dashboard_summary"
MIN_CURSOR_REFRESH_INTERVAL_SEC = 300.0


CURSOR_USAGE_PAGE_PROBE_SCRIPT = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const cleanLines = (value) => String(value || '').split(/\r?\n/)
    .map(clean).filter(Boolean).join('\n');
  const isVisible = (element) => {
    if (!element || element.offsetParent === null) {
      const rect = element ? element.getBoundingClientRect() : null;
      if (!rect || rect.width <= 0 || rect.height <= 0) return false;
    }
    const style = window.getComputedStyle(element);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const isExcluded = (element) => Boolean(
    element.closest('table, [role="row"], [role="grid"], [role="treegrid"]')
  );
  const labelElements = Array.from(document.querySelectorAll(
    'main div, main span, [role="main"] div, [role="main"] span'
  )).filter((element) => isVisible(element) && !isExcluded(element));
  const findUniqueExactLabel = (label) => {
    const matches = labelElements.filter(
    (element) => clean(element.innerText).toLowerCase() === label
    );
    return matches.length === 1 ? matches[0] : null;
  };
  const nearestSummaryCard = (heading, kind) => {
    if (!heading) return null;
    let current = heading;
    let fallback = null;
    for (let depth = 0; depth < 7 && current; depth += 1, current = current.parentElement) {
      if (!isVisible(current) || isExcluded(current)) continue;
      if (current.querySelector('table, [role="row"], [role="grid"], [role="treegrid"]')) continue;
      const text = cleanLines(current.innerText);
      if (!text || text.length > 600 || text.split('\n').length > 16) continue;
      if (/usage events/i.test(text)) continue;
      if (/\b[^\s@]+@[^\s@]+\.[^\s@]+\b/.test(text)) continue;
      const hasValue = kind === 'included'
        ? /(?:US\$|[$€£₩]|\b[A-Z]{3}\b)?\s*\d[\d,.]*\s*\n?\s*\/\s*(?:US\$|[$€£₩]|\b[A-Z]{3}\b)?\s*\d/i.test(text)
        : /(?:^|\n)\s*(?:on|off|enabled|disabled|활성|비활성)\s*(?:\n|$)/i.test(text);
      if (!hasValue) continue;
      if (kind !== 'included') return {element: current, text};
      if (!fallback) fallback = {element: current, text};
      const hasReset = /(?:^|\n)\s*(?:billing\s+cycle|resets?|결제\s*주기|초기화)\b/i.test(text);
      if (hasReset) return {element: current, text};
    }
    return fallback;
  };
  const includedCard = nearestSummaryCard(findUniqueExactLabel('your included usage'), 'included');
  const onDemandCard = nearestSummaryCard(findUniqueExactLabel('on-demand usage'), 'on_demand');
  const summaryText = [includedCard, onDemandCard]
    .filter(Boolean).map((item) => item.text).join('\n');
  const loginText = Array.from(
    document.querySelectorAll('main a, main button, [role="main"] a, [role="main"] button')
  ).filter(isVisible).map((element) => clean(element.innerText)).filter((text) =>
    /^(sign in|log in|continue with google|로그인)$/i.test(text)
  ).slice(0, 4).join(' ');
  const collectProfileName = () => {
    const selectors = [
      '[data-testid*="profile" i]',
      '[data-testid*="account" i]',
      '[aria-label*="profile" i]',
      '[aria-label*="account" i]',
      '[aria-label*="프로필" i]',
      '[aria-label*="계정" i]',
      'header button[aria-haspopup="menu"]',
      'nav button[aria-haspopup="menu"]',
      'button[aria-haspopup="menu"]',
    ];
    const seen = new Set();
    for (const selector of selectors) {
      for (const node of Array.from(document.querySelectorAll(selector))) {
        if (!isVisible(node) || isExcluded(node)) continue;
        const nodeIdentity = clean([
          node.getAttribute ? node.getAttribute('data-testid') : '',
          node.getAttribute ? node.getAttribute('aria-label') : '',
          node.getAttribute ? node.getAttribute('title') : '',
        ].join(' ')).toLowerCase();
        if (nodeIdentity && !/(profile|account|프로필|계정|avatar|user)/i.test(nodeIdentity)) {
          continue;
        }
        for (const raw of [
          node.getAttribute ? node.getAttribute('aria-label') : '',
          node.getAttribute ? node.getAttribute('title') : '',
          node.innerText || node.textContent || '',
        ]) {
          const candidate = clean(raw);
          if (!candidate || candidate.length > 40 || /@/.test(candidate)) continue;
          if (/^(sign in|log in|settings|설정|로그아웃|로그인)$/i.test(candidate)) continue;
          if (seen.has(candidate)) continue;
          seen.add(candidate);
          return candidate;
        }
      }
    }
    return '';
  };
  return {
    url: String(location.href || ''),
    title: clean(document.title),
    mainText: summaryText || loginText,
    profileName: collectProfileName(),
    metricBlocks: summaryText ? [{
      metric_key: 'cursor_account_summary',
      block_text: summaryText,
    }] : [],
  };
}
"""


@dataclass(frozen=True, slots=True)
class CursorUsageTextFixture:
    included_used: Decimal | None
    included_limit: Decimal | None
    used_percent: float | None
    remaining_percent: float | None
    reset_at: str | None
    on_demand_enabled: bool | None
    included_used_display: str | None
    included_limit_display: str | None


class _BrowserSession(Protocol):
    def collect(self) -> BrowserOperationResult: ...
    def open_login(self) -> BrowserOperationResult: ...
    def poll_login(self) -> BrowserOperationResult: ...
    def close_session(self) -> None: ...
    def request_cancel(self) -> bool: ...
    def shutdown(self) -> bool: ...
    def get_runtime_status(self) -> BrowserRuntimeStatus: ...


class _LazyCursorBrowserSession:
    """Avoid loading Playwright/process dependencies until collection is requested."""

    def __init__(
        self,
        config: PlaywrightSessionConfig,
        unrecoverable_timeout_handler: Callable[[], bool] | None,
    ) -> None:
        self._config = config
        self._unrecoverable_timeout_handler = unrecoverable_timeout_handler
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._session: _BrowserSession | None = None
        self._creating = False
        self._terminal = False
        self._terminal_cleanup_complete = False
        self._terminal_cleanup_succeeded = False

    def collect(self) -> BrowserOperationResult:
        session = self._ensure()
        if session is None:
            return BrowserOperationResult(error=BrowserErrorCode.COLLECT_FAILED.value)
        return session.collect()

    def open_login(self) -> BrowserOperationResult:
        session = self._ensure()
        if session is None:
            return BrowserOperationResult(error=BrowserErrorCode.COLLECT_FAILED.value)
        return session.open_login()

    def poll_login(self) -> BrowserOperationResult:
        session = self._ensure()
        if session is None:
            return BrowserOperationResult(error=BrowserErrorCode.COLLECT_FAILED.value)
        return session.poll_login()

    def close_session(self) -> None:
        with self._lock:
            session = self._session
        if session is not None:
            session.close_session()

    def shutdown(self) -> bool:
        with self._lock:
            self._terminal = True
            session = self._session
            if session is None:
                if self._creating:
                    return False
                if self._terminal_cleanup_complete:
                    return bool(self._terminal_cleanup_succeeded)
                self._terminal_cleanup_complete = True
                self._terminal_cleanup_succeeded = True
                return True
        try:
            succeeded = session.shutdown() is True
        except Exception:
            succeeded = False
        with self._lock:
            if session is self._session:
                self._terminal_cleanup_complete = True
                self._terminal_cleanup_succeeded = bool(succeeded)
                if succeeded:
                    self._session = None
        return bool(succeeded)

    def request_cancel(self) -> bool:
        with self._lock:
            self._terminal = True
            session = self._session
            if session is None:
                if self._creating:
                    return False
                if self._terminal_cleanup_complete:
                    return bool(self._terminal_cleanup_succeeded)
                self._terminal_cleanup_complete = True
                self._terminal_cleanup_succeeded = True
                return True
        request_cancel = getattr(session, "request_cancel", None)
        if callable(request_cancel):
            try:
                return bool(request_cancel())
            except Exception:
                return False
        try:
            return session.shutdown() is True
        except Exception:
            return False

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        with self._lock:
            session = self._session
            terminal = bool(self._terminal)
            cleanup_complete = bool(self._terminal_cleanup_complete)
            cleanup_succeeded = bool(self._terminal_cleanup_succeeded)
        if session is None:
            if terminal and cleanup_complete and not cleanup_succeeded:
                return BrowserRuntimeStatus(
                    BrowserState.FAILED,
                    False,
                    BrowserErrorCode.COLLECT_FAILED.value,
                )
            return BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
        return session.get_runtime_status()

    def _ensure(self) -> _BrowserSession | None:
        with self._condition:
            while self._creating:
                self._condition.wait(timeout=0.25)
            if self._terminal:
                return None
            if self._session is not None:
                return self._session
            self._creating = True
        created: _BrowserSession | None = None
        try:
            from src.apps.codex_usage_playwright_session import CodexUsagePlaywrightSession

            created = CodexUsagePlaywrightSession(
                self._config,
                driver_factory=_cursor_driver_factory,
                unrecoverable_timeout_handler=self._unrecoverable_timeout_handler,
            )
        except Exception:
            with self._condition:
                self._creating = False
                if self._terminal:
                    self._terminal_cleanup_complete = True
                    self._terminal_cleanup_succeeded = True
                self._condition.notify_all()
            raise
        with self._condition:
            terminal = bool(self._terminal)
            if not terminal:
                self._session = created
                self._creating = False
                self._condition.notify_all()
                return created
        cancel = getattr(created, "request_cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        try:
            cleanup_succeeded = created.shutdown() is True
        except Exception:
            cleanup_succeeded = False
        with self._condition:
            self._creating = False
            self._terminal_cleanup_complete = True
            self._terminal_cleanup_succeeded = bool(cleanup_succeeded)
            if not cleanup_succeeded:
                self._session = created
            self._condition.notify_all()
        return None


def _decimal(value: str) -> Decimal | None:
    normalized = str(value or "").replace(",", "").strip()
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def _percentage(value: Decimal, limit: Decimal) -> float | None:
    if limit <= 0 or value < 0:
        return None
    result = float(value * Decimal("100") / limit)
    if not 0.0 <= result <= 100.0:
        return None
    return round(result, 4)


def parse_sanitized_cursor_usage_text(text: object) -> CursorUsageTextFixture | None:
    """Parse only the visible, pre-sanitized account summary text."""

    normalized = str(text or "").replace("\r", "\n").strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if any(marker in lowered for marker in ("sign in to continue", "log in to continue")):
        return None
    included_label_pattern = (
        r"(?im)^\s*(?:your\s+included\s+usage|included\s+usage|포함\s*사용량)\s*:?\s*"
    )
    included_label = re.search(included_label_pattern, normalized)
    if included_label is None:
        return None

    ratio_pattern = (
        included_label_pattern
        + r"((?:[A-Z]{2,3}\s*)?[$€£₩]?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
        + r"/\s*((?:[A-Z]{2,3}\s*)?[$€£₩]?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    )
    ratio = re.search(
        ratio_pattern,
        normalized,
    )
    included_used: Decimal | None = None
    included_limit: Decimal | None = None
    included_used_display: str | None = None
    included_limit_display: str | None = None
    ratio_used_percent: float | None = None
    if ratio is not None:
        included_used = _decimal(ratio.group(2))
        included_limit = _decimal(ratio.group(4))
        if included_used is None or included_limit is None:
            return None
        included_used_display = f"{ratio.group(1).replace(' ', '')}{ratio.group(2)}"
        included_limit_display = f"{ratio.group(3).replace(' ', '')}{ratio.group(4)}"
        ratio_used_percent = _percentage(included_used, included_limit)
        if ratio_used_percent is None:
            return None

    explicit_percent: float | None = None
    explicit_remaining = False
    for line in normalized.splitlines():
        line_lower = line.lower()
        if "on-demand" in line_lower or "on demand" in line_lower:
            continue
        match = re.search(
            r"(?i)(?:^|\s)(?:usage|사용량)\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*"
            r"(used|remaining|사용|남음|잔여)?",
            line,
        )
        if match is None:
            continue
        explicit_percent = float(match.group(1))
        if not 0.0 <= explicit_percent <= 100.0:
            return None
        qualifier = str(match.group(2) or "used").lower()
        explicit_remaining = qualifier in {"remaining", "남음", "잔여"}
        break

    if explicit_percent is not None:
        explicit_used = 100.0 - explicit_percent if explicit_remaining else explicit_percent
        if ratio_used_percent is not None and abs(explicit_used - ratio_used_percent) > 0.2:
            return None
        used_percent = round(explicit_used, 4)
    else:
        used_percent = ratio_used_percent
    remaining_percent = (
        None if used_percent is None else round(100.0 - used_percent, 4)
    )

    reset_at: str | None = None
    reset_line = re.search(
        r"(?im)^\s*(?:billing\s+cycle|resets?|결제\s*주기|초기화)\s*:?\s*([^\n]+)",
        normalized,
    )
    if reset_line is not None:
        timestamps = re.findall(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})",
            reset_line.group(1),
        )
        if timestamps:
            reset_at = timestamps[-1]
        else:
            korean_date = re.search(
                r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일",
                reset_line.group(1),
            )
            if korean_date is not None:
                reset_at = korean_date.group(0)

    on_demand_enabled: bool | None = None
    on_demand_line = re.search(
        r"(?im)^\s*(?:on[- ]demand\s+usage|온디맨드\s*사용량)\s*:?\s*([^\n]+)",
        normalized,
    )
    if on_demand_line is not None:
        value = on_demand_line.group(1).strip().lower()
        if re.search(r"\b(?:off|disabled|false|no)\b|비활성", value):
            on_demand_enabled = False
        elif re.search(r"\b(?:on|enabled|true|yes)\b|활성", value):
            on_demand_enabled = True
        elif re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?\s*/\s*[$€£₩]?[0-9]", value):
            on_demand_enabled = True

    return CursorUsageTextFixture(
        included_used=included_used,
        included_limit=included_limit,
        used_percent=used_percent,
        remaining_percent=remaining_percent,
        reset_at=reset_at,
        on_demand_enabled=on_demand_enabled,
        included_used_display=included_used_display,
        included_limit_display=included_limit_display,
    )


def _cursor_driver_factory(
    config: PlaywrightSessionConfig,
    log_sink: LogSink | None,
    playwright_starter: PlaywrightStarter | None,
) -> Any:
    if playwright_starter is not None:
        return CursorUsagePlaywrightDriver(
            config,
            log_sink=log_sink,
            playwright_starter=playwright_starter,
        )
    from src.apps.codex_usage_playwright_process import CodexUsagePlaywrightProcessDriver

    return CodexUsagePlaywrightProcessDriver(
        config,
        log_sink,
        worker_target=run_cursor_playwright_worker,
    )


def _default_base_dir() -> str:
    return os.getenv("APPDATA") or os.getenv("LOCALAPPDATA") or os.path.expanduser("~")


def _iso_now(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _browser_error_state(error: object) -> UsageState:
    key = str(error or "").strip().lower()
    if key in {
        BrowserErrorCode.LOGIN_REQUIRED.value,
        BrowserErrorCode.LOGIN_WINDOW_CLOSED.value,
        BrowserErrorCode.CLOUDFLARE_CHALLENGE.value,
    }:
        return UsageState.LOGGED_OUT
    if key in {
        BrowserErrorCode.COMMAND_TIMEOUT.value,
        "navigation_timeout",
    }:
        return UsageState.TIMEOUT
    if key in {"rate_limited", "rate_limit", "too_many_requests", "429"}:
        return UsageState.RATE_LIMITED
    if key in {
        BrowserErrorCode.RENDERER_CRASHED.value,
        BrowserErrorCode.TRANSPORT_CLOSED.value,
    }:
        return UsageState.CRASH
    if key in {"worker_recycle", "page_recycling"}:
        return UsageState.RECYCLE
    if key == BrowserErrorCode.PROFILE_IN_USE.value:
        return UsageState.UNKNOWN
    if key == BrowserErrorCode.COLLECT_FAILED.value:
        return UsageState.DOM_DRIFT
    return normalize_usage_state(key)


class CursorUsageMonitor:
    def __init__(
        self,
        config_dir: str | None = None,
        profile_dir: str | None = None,
        notification_sink: Callable[[dict[str, Any]], None] | None = None,
        suppress_normal_tooltips: bool = True,
        unrecoverable_timeout_handler: Callable[[], bool] | None = None,
        *,
        profile_id: str | None = None,
        browser_session_factory: Callable[[PlaywrightSessionConfig], _BrowserSession] | None = None,
        refresh_interval_sec: float = 600.0,
        stale_after_sec: float = 1800.0,
        clock: Callable[[], datetime] | None = None,
        login_poll_interval_sec: float = 1.0,
        login_poll_max_attempts: int = 180,
    ) -> None:
        base_dir = _default_base_dir()
        resolved_profile_id = str(profile_id or "").strip()
        if not resolved_profile_id and profile_dir:
            resolved_profile_id = os.path.basename(os.path.normpath(profile_dir))
        self.profile_id = resolved_profile_id or "cursor-personal"
        self.config_dir = str(
            config_dir
            or os.path.join(base_dir, "windows-supporter", f"cursor-account-{self.profile_id}")
        )
        self.profile_dir = str(
            profile_dir
            or os.path.join(
                base_dir,
                "windows-supporter",
                "cursor-usage-profiles",
                self.profile_id,
            )
        )
        self._settings_path = os.path.join(self.config_dir, "cursor_usage_settings.json")
        self._state_path = os.path.join(self.config_dir, "cursor_usage_state.json")
        self._event_log_path = os.path.join(self.config_dir, "cursor_usage_events.jsonl")
        self._persistence_enabled = bool(
            config_dir is not None or browser_session_factory is None
        )
        self._notification_sink = notification_sink
        self._suppress_normal_tooltips = bool(suppress_normal_tooltips)
        self._enabled = True
        self._refresh_interval_sec = max(
            MIN_CURSOR_REFRESH_INTERVAL_SEC, float(refresh_interval_sec)
        )
        self._stale_after_sec = max(self._refresh_interval_sec, float(stale_after_sec))
        self._tooltip_duration_ms = 7000
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._load_settings_file()
        self._root: Any = None
        self._event_queue: Any = None
        self._external_scheduler = False
        self._collect_lock = threading.Lock()
        self._collect_inflight = False
        self._last_attempt_at: datetime | None = None
        self._failure_count = 0
        self._retry_failure_limit = 3
        self._last_error_type = UsageErrorType.NONE
        self._login_poll_interval_sec = max(0.01, float(login_poll_interval_sec))
        self._login_poll_max_attempts = max(1, int(login_poll_max_attempts))
        self._login_poll_stop = threading.Event()
        self._login_poll_thread: threading.Thread | None = None
        self._last_reading = AiUsageReading.unavailable(
            provider=AiUsageProvider.CURSOR,
            profile_id=self.profile_id,
            state=UsageState.UNKNOWN,
        )
        self._profile_name = ""
        self._restore_last_success()
        if self._last_reading.state == UsageState.STALE:
            self._last_error_type = UsageErrorType.TRANSIENT
        config = PlaywrightSessionConfig(
            profile_dir=self.profile_dir,
            usage_url=CURSOR_USAGE_URL,
            probe_script=CURSOR_USAGE_PAGE_PROBE_SCRIPT,
            page_recycle_success_count=12,
            worker_recycle_success_count=48,
            worker_recycle_max_age_sec=3600.0,
        )
        if browser_session_factory is not None:
            self._session = browser_session_factory(config)
        else:
            self._session = _LazyCursorBrowserSession(
                config,
                unrecoverable_timeout_handler,
            )

    def attach(self, root: Any, event_queue: Any = None, *, start_monitor: bool = True) -> None:
        self._root = root
        self._event_queue = event_queue
        self._external_scheduler = not bool(start_monitor)

    def shutdown(self) -> bool:
        supports_cancel = callable(getattr(self._session, "request_cancel", None))
        cancelled = self.request_collect_cancel()
        shutdown_succeeded = bool(cancelled)
        if bool(supports_cancel or not cancelled):
            shutdown_succeeded = self._session.shutdown() is True
        self._root = None
        self._event_queue = None
        return bool(cancelled and shutdown_succeeded)

    def request_collect_cancel(self) -> bool:
        self._stop_login_poll()
        request_cancel = getattr(self._session, "request_cancel", None)
        if callable(request_cancel):
            return bool(request_cancel())
        return self._session.shutdown() is True

    def collect(self, *, force: bool = False) -> AiUsageReading:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if (
            not force
            and self._last_attempt_at is not None
            and (now - self._last_attempt_at).total_seconds() < self._refresh_interval_sec
        ):
            return self._last_reading
        with self._collect_lock:
            self._collect_inflight = True
            self._last_attempt_at = now
            try:
                result = self._session.collect()
                reading = self._reading_from_result(result, now)
                self._last_reading = reading
                if reading.state == UsageState.READY:
                    self._failure_count = 0
                    self._last_error_type = UsageErrorType.NONE
                    self._save_last_success(reading)
                else:
                    self._failure_count = min(self._failure_count + 1, 999)
                    self._last_error_type = self._error_type_from_result(result, reading)
                self._append_collection_event(source="collect")
                return reading
            finally:
                self._collect_inflight = False

    def get_settings_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(self._enabled),
            "provider": AiUsageProvider.CURSOR.value,
            "interval_sec": float(self._refresh_interval_sec),
            "tooltip_duration_ms": int(self._tooltip_duration_ms),
            "usage_url": CURSOR_USAGE_URL,
            "collection_mode": CURSOR_COLLECTION_MODE,
            "collection_supported": True,
            "settings_path": self._settings_path,
            "state_path": self._state_path,
            "profile_dir": self.profile_dir,
        }

    def update_settings(self, data: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"
        if "enabled" in data:
            self._enabled = bool(data.get("enabled"))
        if "interval_sec" in data:
            try:
                interval = float(data.get("interval_sec"))
            except (TypeError, ValueError):
                return False, "interval"
            self._refresh_interval_sec = max(MIN_CURSOR_REFRESH_INTERVAL_SEC, interval)
        if "tooltip_duration_ms" in data:
            try:
                duration = int(data.get("tooltip_duration_ms"))
            except (TypeError, ValueError):
                return False, "tooltip_duration"
            self._tooltip_duration_ms = max(1200, duration)
        self._save_settings_file()
        return True, None

    def get_runtime_status(self) -> dict[str, Any]:
        browser = self._session.get_runtime_status()
        reading = self._last_reading
        provider_status = project_usage_provider_status(
            has_usable_cache=reading.is_usable,
            error_type=self._last_error_type,
            failure_count=self._failure_count,
            retry_limit=self._retry_failure_limit,
            collect_inflight=self._collect_inflight,
        )
        if provider_status == "ready":
            freshness = "fresh"
            monitor_state = "idle"
            session_state = "logged_in"
        elif provider_status == "stale":
            freshness = "stale"
            monitor_state = "idle"
            session_state = "logged_in"
        elif provider_status == "login":
            freshness = "stale" if reading.is_usable else "unavailable"
            monitor_state = "paused_auth_required"
            session_state = "logged_out"
        elif provider_status == "paused":
            freshness = "stale" if reading.is_usable else "unavailable"
            monitor_state = "paused_profile_in_use"
            session_state = "logged_in" if reading.is_usable else "unknown"
        elif provider_status == "rate_limited":
            freshness = "stale" if reading.is_usable else "unavailable"
            monitor_state = "idle"
            session_state = "logged_in" if reading.is_usable else "unknown"
        else:
            freshness = "unavailable"
            monitor_state = "running" if self._collect_inflight else "idle"
            session_state = "unknown"
        return {
            "enabled": bool(self._enabled),
            "provider": AiUsageProvider.CURSOR.value,
            "profile_id": self.profile_id,
            "state": reading.state.value,
            "message": reading.message,
            "provider_status": provider_status,
            "last_error_state": (
                reading.last_error_state.value
                if reading.last_error_state is not None
                else ""
            ),
            "freshness": freshness,
            "last_snapshot_is_stale": reading.is_stale,
            "last_error_type": self._last_error_type.value,
            "failure_count": int(self._failure_count),
            "retry_failure_limit": int(self._retry_failure_limit),
            "retry_exhausted": bool(
                self._last_error_type
                not in {
                    UsageErrorType.NONE,
                    UsageErrorType.AUTH,
                    UsageErrorType.PROFILE_IN_USE,
                    UsageErrorType.DOM_DRIFT,
                    UsageErrorType.UNSUPPORTED_CONTRACT,
                }
                and self._failure_count >= self._retry_failure_limit
                and not reading.is_usable
            ),
            "retry_after_sec": self._retry_after_sec(provider_status),
            "monitor_state": monitor_state,
            "session_state": session_state,
            "collect_inflight": bool(self._collect_inflight),
            "auto_monitoring_active": bool(self._enabled and not self._external_scheduler),
            "collection_mode": CURSOR_COLLECTION_MODE,
            "browser_state": browser.state.value,
            "browser_last_error": browser.last_error,
            "login_window_open": bool(browser.login_window_open),
            "can_login": session_state != "logged_in" and not browser.login_window_open,
            "can_logout": browser.state != BrowserState.STOPPED or session_state == "logged_in",
            "profile_name": str(self._profile_name or ""),
            "usage_history": [],
        }

    def get_last_snapshot(self) -> AiUsageReading:
        return self._last_reading

    def show_current_status(self, force_refresh: bool = True, source: str = "manual_query") -> None:
        source_key = str(source or "manual_query").strip().lower()
        if source_key == "manual_login":
            self._stop_login_poll()
            result = self._session.open_login()
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            self._last_reading = self._reading_from_result(result, now)
            if self._last_reading.state == UsageState.READY:
                self._failure_count = 0
                self._last_error_type = UsageErrorType.NONE
                self._save_last_success(self._last_reading)
            elif result.error in {
                BrowserErrorCode.LOGIN_REQUIRED.value,
                BrowserErrorCode.CLOUDFLARE_CHALLENGE.value,
            }:
                self._start_login_poll()
            if self._last_reading.state != UsageState.READY:
                self._failure_count = min(self._failure_count + 1, 999)
                self._last_error_type = self._error_type_from_result(
                    result,
                    self._last_reading,
                )
            self._append_collection_event(source="manual_login")
        elif source_key == "auto_monitor" and force_refresh:
            self.collect(force=False)
        elif force_refresh:
            self.collect(force=True)
        self._emit_update(source_key)

    def release_profile_session(self) -> tuple[bool, str]:
        self._stop_login_poll()
        acquired = self._collect_lock.acquire(timeout=15.0)
        if not acquired:
            return False, "진행 중인 Cursor 조회를 중단하지 못했습니다. 잠시 후 다시 시도해 주세요."
        try:
            self._session.close_session()
            ok, message = self._clear_managed_profile_directory()
            if not ok:
                return False, message
            try:
                if os.path.isfile(self._state_path):
                    os.remove(self._state_path)
            except OSError:
                pass
            captured_at = _iso_now(self._clock)
            self._last_reading = AiUsageReading.unavailable(
                provider=AiUsageProvider.CURSOR,
                profile_id=self.profile_id,
                state=UsageState.LOGGED_OUT,
                captured_at=captured_at,
            )
            self._last_attempt_at = None
            self._failure_count = 0
            self._last_error_type = UsageErrorType.AUTH
        except Exception as exc:
            return False, f"Cursor 전용 브라우저 세션 종료 실패: {type(exc).__name__}"
        finally:
            self._collect_lock.release()
        return True, message or "Cursor 연결을 해제했습니다."

    def _clear_managed_profile_directory(self) -> tuple[bool, str]:
        profile_dir = os.path.abspath(os.path.normpath(str(self.profile_dir or "")))
        components: list[str] = []
        current = profile_dir
        while True:
            head, tail = os.path.split(current)
            if tail:
                components.append(tail.lower())
            if not head or head == current:
                break
            current = head
        basename = os.path.basename(profile_dir).lower()
        parent_basename = os.path.basename(os.path.dirname(profile_dir)).lower()
        grandparent_dir = os.path.dirname(os.path.dirname(profile_dir))
        great_grandparent_dir = os.path.dirname(grandparent_dir)
        dynamic_managed = (
            basename == "cursor"
            and re.fullmatch(r"profile_[0-9a-f]{32}", parent_basename) is not None
            and os.path.basename(grandparent_dir).lower() == "ai-profiles"
            and os.path.basename(great_grandparent_dir).lower() == "windows-supporter"
        )
        managed_name = (
            basename.startswith("cursor-profile-")
            or (parent_basename == "cursor-usage-profiles" and bool(basename))
            or dynamic_managed
        )
        if "windows-supporter" not in components or not managed_name:
            return False, "Windows Supporter가 관리하는 Cursor 전용 프로필만 연결 해제할 수 있습니다."
        if dynamic_managed:
            app_root = great_grandparent_dir
        elif parent_basename == "cursor-usage-profiles":
            app_root = os.path.dirname(os.path.dirname(profile_dir))
        else:
            app_root = os.path.dirname(profile_dir)
        if (
            os.path.basename(app_root).lower() != "windows-supporter"
            or not _is_non_reparse_descendant(profile_dir, app_root)
        ):
            return False, "Windows Supporter가 관리하는 Cursor 전용 프로필만 연결 해제할 수 있습니다."
        if not os.path.isdir(profile_dir):
            return True, "이미 연결 해제된 상태입니다."
        try:
            shutil.rmtree(profile_dir)
        except OSError as exc:
            return False, f"Cursor 전용 프로필 정리 실패: {type(exc).__name__}"
        return True, "Cursor 연결을 해제했습니다."

    def format_captured_at_for_display(self, value: str) -> str:
        return self._format_timestamp(value)

    def format_reset_at_for_display(self, value: str, key: str = "") -> str:
        _ = key
        normalized = str(value or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            return normalized
        return self._format_timestamp(value)

    def _reading_from_result(
        self,
        result: BrowserOperationResult,
        now: datetime,
    ) -> AiUsageReading:
        captured_at = now.isoformat()
        if result.error:
            return self._failure_reading(
                state=_browser_error_state(result.error),
                captured_at=captured_at,
            )
        probe = result.probe
        if not isinstance(probe, dict):
            return self._failure_reading(
                state=UsageState.DOM_DRIFT,
                captured_at=captured_at,
            )
        from src.apps.codex_usage_monitor import sanitize_profile_name

        profile_name = sanitize_profile_name(probe.get("profileName", ""))
        if profile_name:
            self._profile_name = profile_name
        summary_text = ""
        for block in probe.get("metricBlocks", []):
            if not isinstance(block, dict):
                continue
            if str(block.get("metric_key", "")) != "cursor_account_summary":
                continue
            summary_text = str(block.get("block_text", "") or "")
            break
        if not summary_text:
            summary_text = str(probe.get("mainText", "") or "")
        parsed = parse_sanitized_cursor_usage_text(summary_text)
        if parsed is None or parsed.used_percent is None:
            return self._failure_reading(
                state=UsageState.DOM_DRIFT,
                captured_at=captured_at,
            )
        return AiUsageReading(
            provider=AiUsageProvider.CURSOR,
            profile_id=self.profile_id,
            state=UsageState.READY,
            used_percent=parsed.used_percent,
            remaining_percent=parsed.remaining_percent,
            included_used=str(parsed.included_used_display or ""),
            included_limit=str(parsed.included_limit_display or ""),
            captured_at=captured_at,
            last_success_at=captured_at,
            reset_at=str(parsed.reset_at or ""),
            on_demand_enabled=parsed.on_demand_enabled,
        )

    def _failure_reading(self, *, state: UsageState, captured_at: str) -> AiUsageReading:
        prior = self._last_reading
        if prior.is_usable:
            return AiUsageReading(
                provider=AiUsageProvider.CURSOR,
                profile_id=self.profile_id,
                state=UsageState.STALE,
                used_percent=prior.used_percent,
                remaining_percent=prior.remaining_percent,
                included_used=prior.included_used,
                included_limit=prior.included_limit,
                captured_at=captured_at,
                last_success_at=prior.last_success_at,
                reset_at=prior.reset_at,
                reset_precision=prior.reset_precision,
                on_demand_enabled=prior.on_demand_enabled,
                last_error_state=state,
            )
        return AiUsageReading.unavailable(
            provider=AiUsageProvider.CURSOR,
            profile_id=self.profile_id,
            state=state,
            captured_at=captured_at,
        )

    def _error_type_from_result(
        self,
        result: BrowserOperationResult,
        reading: AiUsageReading,
    ) -> UsageErrorType:
        if result.error:
            return normalize_usage_error_type(result.error)
        if reading.last_error_state is not None:
            return normalize_usage_error_type(reading.last_error_state.value)
        return normalize_usage_error_type(reading.state.value)

    def _retry_after_sec(self, provider_status: str) -> float | None:
        if str(provider_status or "") != "retrying":
            return None
        exponent = max(0, min(int(self._failure_count) - 1, 4))
        return float(min(self._refresh_interval_sec * (2**exponent), 15 * 60))

    def _append_collection_event(self, *, source: str) -> None:
        if not self._persistence_enabled:
            return
        provider_status = project_usage_provider_status(
            has_usable_cache=self._last_reading.is_usable,
            error_type=self._last_error_type,
            failure_count=self._failure_count,
            retry_limit=self._retry_failure_limit,
            collect_inflight=False,
        )
        payload = {
            "timestamp": _iso_now(self._clock),
            "event": "collection_result",
            "source": str(source or "collect"),
            "provider": AiUsageProvider.CURSOR.value,
            "profile_id": self.profile_id,
            "reading_state": self._last_reading.state.value,
            "provider_status": provider_status,
            "error_type": self._last_error_type.value,
            "failure_count": int(self._failure_count),
            "has_usable_cache": bool(self._last_reading.is_usable),
        }
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            with open(self._event_log_path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                stream.write("\n")
        except OSError:
            pass

    def _emit_update(self, source: str) -> None:
        if self._notification_sink is None:
            return
        if self._suppress_normal_tooltips and self._last_reading.state == UsageState.READY:
            return
        try:
            self._notification_sink(
                {
                    "provider": AiUsageProvider.CURSOR.value,
                    "profile_id": self.profile_id,
                    "source": source,
                    "state": self._last_reading.state.value,
                    "message": self._last_reading.message,
                }
            )
        except Exception:
            pass

    def _start_login_poll(self) -> None:
        thread = self._login_poll_thread
        if thread is not None and thread.is_alive():
            return
        self._login_poll_stop.clear()
        self._login_poll_thread = threading.Thread(
            target=self._run_login_poll,
            name=f"CursorUsageLoginPoll-{self.profile_id}",
            daemon=True,
        )
        self._login_poll_thread.start()

    def _run_login_poll(self) -> None:
        for _attempt in range(self._login_poll_max_attempts):
            if self._login_poll_stop.wait(self._login_poll_interval_sec):
                return
            try:
                result = self._session.poll_login()
            except Exception:
                result = BrowserOperationResult(
                    error=BrowserErrorCode.COLLECT_FAILED.value
                )
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            reading = self._reading_from_result(result, now)
            self._last_reading = reading
            if reading.state == UsageState.READY:
                self._failure_count = 0
                self._last_error_type = UsageErrorType.NONE
                self._save_last_success(reading)
                self._append_collection_event(source="manual_login_poll")
                self._emit_update("manual_login")
                return
            self._failure_count = min(self._failure_count + 1, 999)
            self._last_error_type = self._error_type_from_result(result, reading)
            self._append_collection_event(source="manual_login_poll")
            if result.error in {
                BrowserErrorCode.LOGIN_WINDOW_CLOSED.value,
                BrowserErrorCode.TRANSPORT_CLOSED.value,
                BrowserErrorCode.RENDERER_CRASHED.value,
            }:
                self._emit_update("manual_login")
                return
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self._last_reading = self._failure_reading(
            state=UsageState.TIMEOUT,
            captured_at=now.isoformat(),
        )
        self._failure_count = min(self._failure_count + 1, 999)
        self._last_error_type = UsageErrorType.TIMEOUT
        self._append_collection_event(source="manual_login_poll_exhausted")
        self._emit_update("manual_login")

    def _stop_login_poll(self) -> None:
        self._login_poll_stop.set()
        thread = self._login_poll_thread
        self._login_poll_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=min(1.0, self._login_poll_interval_sec + 0.1))

    def _load_settings_file(self) -> None:
        if not self._persistence_enabled:
            return
        data = self._read_json(self._settings_path)
        if not isinstance(data, dict) or data.get("provider") not in {None, "cursor"}:
            return
        if "enabled" in data:
            self._enabled = bool(data.get("enabled"))
        try:
            interval = float(data.get("interval_sec", self._refresh_interval_sec))
        except (TypeError, ValueError):
            interval = self._refresh_interval_sec
        self._refresh_interval_sec = max(MIN_CURSOR_REFRESH_INTERVAL_SEC, interval)
        try:
            duration = int(data.get("tooltip_duration_ms", self._tooltip_duration_ms))
        except (TypeError, ValueError):
            duration = self._tooltip_duration_ms
        self._tooltip_duration_ms = max(1200, duration)

    def _save_settings_file(self) -> None:
        if not self._persistence_enabled:
            return
        try:
            self._write_json_atomic(
                self._settings_path,
                {
                    "settings_version": 1,
                    "provider": AiUsageProvider.CURSOR.value,
                    "enabled": bool(self._enabled),
                    "interval_sec": float(self._refresh_interval_sec),
                    "tooltip_duration_ms": int(self._tooltip_duration_ms),
                    "collection_mode": CURSOR_COLLECTION_MODE,
                },
            )
        except OSError:
            pass

    def _restore_last_success(self) -> None:
        if not self._persistence_enabled:
            return
        data = self._read_json(self._state_path)
        if not isinstance(data, dict) or data.get("provider") != "cursor":
            return
        try:
            reading = AiUsageReading(
                provider=AiUsageProvider.CURSOR,
                profile_id=self.profile_id,
                state=UsageState.STALE,
                used_percent=data.get("used_percent"),
                remaining_percent=data.get("remaining_percent"),
                included_used=str(data.get("included_used") or ""),
                included_limit=str(data.get("included_limit") or ""),
                captured_at=_iso_now(self._clock),
                last_success_at=str(data.get("captured_at") or ""),
                reset_at=str(data.get("reset_at") or ""),
                reset_precision=str(data.get("reset_precision") or ""),
                on_demand_enabled=(
                    data.get("on_demand_enabled")
                    if isinstance(data.get("on_demand_enabled"), bool)
                    else None
                ),
            )
        except (TypeError, ValueError):
            return
        if reading.is_usable:
            self._last_reading = reading
        from src.apps.codex_usage_monitor import sanitize_profile_name

        profile_name = sanitize_profile_name(data.get("profile_name", ""))
        if profile_name:
            self._profile_name = profile_name

    def _save_last_success(self, reading: AiUsageReading) -> None:
        if (
            not self._persistence_enabled
            or reading.state != UsageState.READY
            or not reading.is_usable
        ):
            return
        try:
            self._write_json_atomic(
                self._state_path,
                {
                    "state_version": 1,
                    "provider": AiUsageProvider.CURSOR.value,
                    "used_percent": reading.used_percent,
                    "remaining_percent": reading.remaining_percent,
                    "included_used": reading.included_used,
                    "included_limit": reading.included_limit,
                    "captured_at": reading.last_success_at or reading.captured_at,
                    "reset_at": reading.reset_at,
                    "reset_precision": reading.reset_precision,
                    "on_demand_enabled": reading.on_demand_enabled,
                    "profile_name": str(self._profile_name or ""),
                },
            )
        except OSError:
            pass

    @staticmethod
    def _read_json(path: str) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

    @staticmethod
    def _format_timestamp(value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return ""
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return normalized
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
