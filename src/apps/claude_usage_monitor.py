from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import re
import shutil
import stat
import threading
import time
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
from src.apps.claude_usage_playwright_driver import ClaudeUsagePlaywrightDriver
from src.apps.claude_usage_playwright_worker import run_claude_playwright_worker
from src.apps.usage_limit_reset_alerts import UsageLimitResetAlert


CLAUDE_USAGE_URL = "https://claude.ai/settings/usage"


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


CLAUDE_COLLECTION_MODE = "usage_page_api"
MIN_CLAUDE_REFRESH_INTERVAL_SEC = 300.0


# The authenticated claude.ai settings page exposes usage through the web
# session JSON API (/api/organizations/{org}/usage). The probe prefers that
# structured contract and keeps the visible page text as a fallback summary,
# so DOM layout changes degrade into text parsing instead of inventing data.
CLAUDE_USAGE_PAGE_PROBE_SCRIPT = r"""
async () => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const cleanLines = (value) => String(value || '').split(/\r?\n/)
    .map(clean).filter(Boolean).join('\n');
  const isVisible = (element) => {
    if (!element) return false;
    if (element.offsetParent === null) {
      const rect = element.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) return false;
    }
    const style = window.getComputedStyle(element);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const fetchJson = async (path) => {
    try {
      const response = await fetch(path, {
        credentials: 'include',
        headers: {Accept: 'application/json'},
      });
      const contentType = String(
        response.headers.get('content-type') || ''
      ).toLowerCase();
      const cfMitigated = String(
        response.headers.get('cf-mitigated') || ''
      ).toLowerCase();
      // Cloudflare challenge/interstitial responses are not API answers: they
      // carry cf-mitigated headers or HTML bodies. A real Claude auth failure
      // is a JSON 401/403 from the API itself.
      if (cfMitigated.indexOf('challenge') !== -1) {
        return {challenge: true, status: response.status};
      }
      if (contentType.indexOf('json') === -1) {
        const redirectedUrl = String(response.url || '').toLowerCase();
        if (response.redirected && /\/(login|signin|logout)/.test(redirectedUrl)) {
          return {authRequired: true};
        }
        return {challenge: true, status: response.status};
      }
      if (response.status === 401 || response.status === 403) {
        return {authRequired: true};
      }
      if (response.status === 429) {
        return {rateLimited: true};
      }
      if (!response.ok) {
        return {error: response.status};
      }
      try {
        return {data: await response.json()};
      } catch (_jsonError) {
        return {error: 'invalid_json'};
      }
    } catch (_error) {
      return {error: 'fetch_failed'};
    }
  };
  const collectProfileName = () => {
    const identityCue = /(profile|account|프로필|계정|avatar|user)/i;
    const genericLabel = /^(?:sign in|log in|settings|설정|로그아웃|로그인|help|en|account|profile|menu|avatar|user|프로필|계정|메뉴|open|close|(?:my|your|edit|switch|view|open)\s+(?:account|profile)(?:\s+menu)?|(?:account|profile|user)\s+menu|(?:내|나의)\s*(?:계정|프로필)|계정\s*메뉴|프로필\s*메뉴)$/i;
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
      const broadMenu = /aria-haspopup=["']menu["']/.test(selector);
      for (const node of Array.from(document.querySelectorAll(selector))) {
        if (!isVisible(node)) continue;
        const ariaLabel = node.getAttribute ? node.getAttribute('aria-label') : '';
        const title = node.getAttribute ? node.getAttribute('title') : '';
        const testId = node.getAttribute ? node.getAttribute('data-testid') : '';
        const nodeIdentity = clean([testId, ariaLabel, title].join(' ')).toLowerCase();
        const hasCue = identityCue.test(nodeIdentity);
        // Uncued header/nav menus (notifications, language, workspace, ...)
        // must not be harvested as profile identity.
        if (broadMenu && !hasCue) continue;
        if (!broadMenu && nodeIdentity && !hasCue) continue;
        const rawCandidates = [ariaLabel, title, node.innerText || node.textContent || ''];
        for (const raw of rawCandidates) {
          const candidate = clean(raw);
          if (!candidate || candidate.length > 40 || /@/.test(candidate)) continue;
          if (genericLabel.test(candidate)) continue;
          if (seen.has(candidate)) continue;
          seen.add(candidate);
          return candidate;
        }
      }
    }
    return '';
  };

  const loginText = Array.from(
    document.querySelectorAll('main a, main button, [role="main"] a, [role="main"] button, body > div a, body > div button')
  ).filter(isVisible).map((element) => clean(element.innerText)).filter((text) =>
    /^(sign in|log in|continue with google|continue with email|create account|로그인)$/i.test(text)
  ).slice(0, 4).join(' ');

  const blocks = [];
  let authRequired = false;
  let rateLimited = false;
  let challenge = false;
  let apiPayload = null;
  let organizationName = '';
  let organizationId = '';
  let accountName = '';
  let accountEmailLocal = '';

  const orgsResult = await fetchJson('/api/organizations');
  if (orgsResult.challenge) {
    challenge = true;
  } else if (orgsResult.authRequired) {
    authRequired = true;
  } else if (orgsResult.rateLimited) {
    rateLimited = true;
  } else if (Array.isArray(orgsResult.data)) {
    const organizations = orgsResult.data.filter(
      (entry) => entry && typeof entry === 'object' && clean(entry.uuid)
    );
    let activeOrg = null;
    try {
      const cookieMatch = /(?:^|;\s*)lastActiveOrg=([^;]+)/.exec(document.cookie || '');
      const wanted = cookieMatch ? clean(decodeURIComponent(cookieMatch[1])) : '';
      activeOrg = organizations.find(
        (entry) => wanted && clean(entry.uuid) === wanted
      ) || null;
    } catch (_cookieError) {}
    const org = activeOrg || organizations[0] || null;
    if (org) {
      organizationId = clean(org.uuid);
      organizationName = clean(org.name);
      // Identity enrichment only: the organizations response already proved
      // the session is authenticated, so an account failure must never feed
      // auth/challenge classification.
      const accountResult = await fetchJson('/api/account');
      if (accountResult.data && typeof accountResult.data === 'object') {
        const account = accountResult.data;
        accountName = clean(
          account.full_name || account.display_name || account.name ||
          account.given_name || account.preferred_name
        );
        const accountEmail = clean(account.email_address || account.email);
        if (accountEmail.indexOf('@') > 0) {
          accountEmailLocal = clean(accountEmail.split('@')[0]);
        }
      }
      const usageResult = await fetchJson(
        '/api/organizations/' + encodeURIComponent(organizationId) + '/usage'
      );
      if (usageResult.challenge) {
        challenge = true;
      } else if (usageResult.authRequired) {
        authRequired = true;
      } else if (usageResult.rateLimited) {
        rateLimited = true;
      } else if (usageResult.data && typeof usageResult.data === 'object') {
        apiPayload = {
          usage: usageResult.data,
          organization: {uuid: organizationId, name: organizationName},
        };
      }
    }
  }

  const mainElement = document.querySelector('main, [role="main"]');
  const mainRegionText = mainElement ? cleanLines(mainElement.innerText) : '';
  const summaryText = cleanLines(mainRegionText).slice(0, 2000);

  if (apiPayload) {
    blocks.push({
      metric_key: 'claude_usage_api',
      block_text: JSON.stringify(apiPayload).slice(0, 8000),
    });
  }
  const pageChallenge =
    /cf_chl/i.test(String(location.href || '')) ||
    /just a moment|잠시만\s*기다리|보안\s*확인/i.test(clean(document.title)) ||
    Boolean(document.querySelector(
      'script[src*="challenge-platform"], #challenge-stage, #cf-chl-widget, ' +
      '.cf-browser-verification, #challenge-error-text'
    ));

  if (summaryText && /usage|사용량|session|세션|weekly|주간/i.test(summaryText)) {
    blocks.push({
      metric_key: 'claude_usage_summary',
      block_text: summaryText,
    });
  }
  // A real API payload is decisive: residual edge markers or a challenged
  // sub-request must not veto collected data.
  if ((challenge || pageChallenge) && !apiPayload) {
    blocks.push({metric_key: 'claude_cf_challenge', block_text: 'cf_challenge'});
  }
  if (authRequired && !apiPayload) {
    blocks.push({metric_key: 'claude_auth_required', block_text: 'auth_required'});
  }
  if (rateLimited && !apiPayload) {
    blocks.push({metric_key: 'claude_rate_limited', block_text: 'rate_limited'});
  }

  let profileName = '';
  let profileNameSource = '';
  if (accountName) {
    profileName = accountName;
    profileNameSource = 'account';
  } else if (organizationName) {
    profileName = organizationName;
    profileNameSource = 'organization';
  } else {
    const domName = collectProfileName();
    if (domName) {
      profileName = domName;
      profileNameSource = 'dom';
    } else if (accountEmailLocal) {
      profileName = accountEmailLocal;
      profileNameSource = 'email';
    }
  }

  return {
    url: String(location.href || ''),
    title: clean(document.title),
    mainText: summaryText || loginText || cleanLines(document.body ? document.body.innerText : '').slice(0, 800),
    profileName: profileName,
    profileNameSource: profileNameSource,
    accountId: organizationId,
    metricBlocks: blocks,
  };
}
"""


@dataclass(frozen=True, slots=True)
class ClaudeUsageData:
    session_used_percent: float | None = None
    session_reset_at: str = ""
    weekly_used_percent: float | None = None
    weekly_reset_at: str = ""
    weekly_label: str = ""
    scoped_weekly_used_percent: float | None = None
    scoped_weekly_reset_at: str = ""
    scoped_weekly_label: str = ""
    extra_usage_enabled: bool | None = None
    extra_usage_text: str = ""


class _BrowserSession(Protocol):
    def collect(self) -> BrowserOperationResult: ...
    def open_login(self) -> BrowserOperationResult: ...
    def poll_login(self) -> BrowserOperationResult: ...
    def close_session(self) -> None: ...
    def request_cancel(self) -> bool: ...
    def shutdown(self) -> bool: ...
    def get_runtime_status(self) -> BrowserRuntimeStatus: ...


class _LazyClaudeBrowserSession:
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
                driver_factory=_claude_driver_factory,
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


def _claude_driver_factory(
    config: PlaywrightSessionConfig,
    log_sink: LogSink | None,
    playwright_starter: PlaywrightStarter | None,
) -> Any:
    if playwright_starter is not None:
        return ClaudeUsagePlaywrightDriver(
            config,
            log_sink=log_sink,
            playwright_starter=playwright_starter,
        )
    from src.apps.codex_usage_playwright_process import CodexUsagePlaywrightProcessDriver

    return CodexUsagePlaywrightProcessDriver(
        config,
        log_sink,
        worker_target=run_claude_playwright_worker,
    )


def _default_base_dir() -> str:
    return os.getenv("APPDATA") or os.getenv("LOCALAPPDATA") or os.path.expanduser("~")


def _iso_now(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        if match is None:
            return None
        try:
            number = float(match.group(0))
        except (TypeError, ValueError):
            return None
    if number < 0.0:
        return None
    # The API reports utilization above 100 for an exceeded window; clamp so
    # the metric still renders (0% remaining) instead of disappearing.
    return round(min(number, 100.0), 4)


def _reset_at_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat()


def _window_parts(entry: Any) -> tuple[float | None, str]:
    """Read one usage window object ({utilization|percent, resets_at})."""
    if not isinstance(entry, dict):
        return None, ""
    percent = _percent(entry.get("utilization"))
    if percent is None:
        percent = _percent(entry.get("percent"))
    reset_at = _reset_at_text(
        entry.get("resets_at") or entry.get("reset_at") or entry.get("reset")
    )
    return percent, reset_at


def _format_extra_usage_text(entry: dict[str, Any]) -> str:
    def _money(value: Any, currency: str) -> str:
        if value is None or isinstance(value, bool):
            return ""
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
            if match is None:
                return ""
            try:
                number = float(match.group(0))
            except (TypeError, ValueError):
                return ""
        if number < 0:
            return ""
        # API reports subunit amounts (cents); whole units still render sanely.
        amount = number / 100.0 if number > 0 else 0.0
        text = f"{amount:.2f}".rstrip("0").rstrip(".")
        return f"{currency}{text}" if currency else text

    currency = str(entry.get("currency") or "").strip()
    symbol = {"USD": "$", "EUR": "€", "KRW": "₩", "GBP": "£"}.get(
        currency.upper(), f"{currency} " if currency else ""
    )
    used_raw = entry.get("used_credits")
    if used_raw is None:
        used_raw = entry.get("used")
    limit_raw = entry.get("monthly_limit")
    if limit_raw is None:
        limit_raw = entry.get("limit")
    used = _money(used_raw, symbol)
    limit = _money(limit_raw, symbol)
    if used and limit:
        return f"{used} / {limit}"
    return used or limit


def parse_claude_usage_api_payload(text: object) -> ClaudeUsageData | None:
    """Parse the sanitized claude.ai organizations usage JSON payload."""

    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = payload
    if not isinstance(usage, dict):
        return None

    session_used, session_reset = _window_parts(usage.get("five_hour"))
    weekly_used, weekly_reset = _window_parts(usage.get("seven_day"))
    scoped_used: float | None = None
    scoped_reset = ""
    scoped_label = ""
    for key, label in (
        ("seven_day_opus", "Opus"),
        ("seven_day_sonnet", "Sonnet"),
        ("seven_day_oauth_apps", "OAuth apps"),
        ("seven_day_cowork", "Cowork"),
    ):
        entry_used, entry_reset = _window_parts(usage.get(key))
        if entry_used is not None or entry_reset:
            scoped_used = entry_used
            scoped_reset = entry_reset
            scoped_label = label
            break

    # Newer responses may expose a limits[] array instead of named buckets.
    limits = usage.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or entry.get("type") or "").strip().lower()
            entry_used, entry_reset = _window_parts(entry)
            if kind in {"session", "five_hour", "5h"}:
                if session_used is None:
                    session_used, session_reset = entry_used, entry_reset
            elif kind in {"weekly_all", "weekly", "seven_day", "7d"}:
                if weekly_used is None:
                    weekly_used, weekly_reset = entry_used, entry_reset
            elif kind.startswith("weekly") and scoped_used is None:
                scope = entry.get("scope")
                label = ""
                if isinstance(scope, dict):
                    model = scope.get("model")
                    if isinstance(model, dict):
                        label = str(model.get("display_name") or model.get("name") or "")
                    elif isinstance(model, str):
                        label = model
                scoped_used = entry_used
                scoped_reset = entry_reset
                scoped_label = label.strip() or "Model"

    extra_enabled: bool | None = None
    extra_text = ""
    extra = usage.get("extra_usage") or usage.get("credits")
    if isinstance(extra, dict):
        if isinstance(extra.get("is_enabled"), bool):
            extra_enabled = bool(extra.get("is_enabled"))
        elif isinstance(extra.get("enabled"), bool):
            extra_enabled = bool(extra.get("enabled"))
        else:
            extra_text_probe = _format_extra_usage_text(extra)
            if extra_text_probe:
                extra_enabled = True
        extra_text = _format_extra_usage_text(extra)
        if extra_enabled is None and isinstance(extra.get("utilization"), (int, float)):
            extra_enabled = True

    if (
        session_used is None
        and not session_reset
        and weekly_used is None
        and not weekly_reset
        and scoped_used is None
        and extra_enabled is None
    ):
        return None
    return ClaudeUsageData(
        session_used_percent=session_used,
        session_reset_at=session_reset,
        weekly_used_percent=weekly_used,
        weekly_reset_at=weekly_reset,
        scoped_weekly_used_percent=scoped_used,
        scoped_weekly_reset_at=scoped_reset,
        scoped_weekly_label=scoped_label,
        extra_usage_enabled=extra_enabled,
        extra_usage_text=extra_text,
    )


_SESSION_LABEL_PATTERN = re.compile(
    r"^\s*(?:current\s+session|session\s+(?:limit|usage)|5[\s-]*hour(?:\s+limit)?|"
    r"현재\s*세션|세션\s*한도)\s*$",
    re.IGNORECASE,
)
_WEEKLY_LABEL_PATTERN = re.compile(
    r"^\s*(?:weekly\s+limits?|weekly\s+usage|all\s+(?:other\s+)?models|"
    r"주간\s*한도|모든\s*모델)\s*$",
    re.IGNORECASE,
)
_SCOPED_WEEKLY_LABEL_PATTERN = re.compile(
    r"^\s*(?:opus|sonnet|haiku)(?:\s+only)?(?:\s+weekly)?\s*$",
    re.IGNORECASE,
)
_EXTRA_USAGE_LABEL_PATTERN = re.compile(
    r"^\s*(?:extra\s+usage|usage\s+credits?|additional\s+usage|"
    r"추가\s*사용량|사용량\s*크레딧)\s*:?.*$",
    re.IGNORECASE,
)
_PERCENT_USED_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:used|사용)", re.IGNORECASE)
_RESET_LINE_PATTERN = re.compile(
    r"^\s*(?:resets?|reset|초기화(?:\s*시각)?|다시\s*설정)\b[\s:：\-]*(.*)$",
    re.IGNORECASE,
)
_WEEKDAY_INDEX = {
    "mon": 0, "monday": 0, "월": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "화": 1,
    "wed": 2, "wednesday": 2, "수": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "목": 3,
    "fri": 4, "friday": 4, "금": 4,
    "sat": 5, "saturday": 5, "토": 5,
    "sun": 6, "sunday": 6, "일": 6,
}
_MONTH_INDEX = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _clock_parts(value: str) -> tuple[int, int] | None:
    """Extract a time-of-day (hour, minute), skipping bare day-of-month digits."""
    korean = re.search(
        r"(오전|오후)\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?",
        value,
        flags=re.IGNORECASE,
    )
    if korean is not None:
        hour = int(korean.group(2))
        minute = int(korean.group(3) or 0)
        if korean.group(1) == "오후" and hour < 12:
            hour += 12
        elif korean.group(1) == "오전" and hour == 12:
            hour = 0
        return hour, minute
    english = re.search(
        r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        value,
        flags=re.IGNORECASE,
    )
    if english is not None:
        hour = int(english.group(1))
        minute = int(english.group(2) or 0)
        meridiem = english.group(3).lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return hour, minute
    colon = re.search(r"\b(\d{1,2}):(\d{2})\b", value)
    if colon is not None:
        return int(colon.group(1)), int(colon.group(2))
    at_clock = re.search(
        r"\bat\s+(\d{1,2})(?::(\d{2}))?\b",
        value,
        flags=re.IGNORECASE,
    )
    if at_clock is not None:
        return int(at_clock.group(1)), int(at_clock.group(2) or 0)
    korean_hour = re.search(r"(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", value)
    if korean_hour is not None:
        return int(korean_hour.group(1)), int(korean_hour.group(2) or 0)
    return None


def _parse_reset_text(text: str, now: datetime) -> str:
    """Normalize a 'Resets ...' fragment to ISO when the shape is known."""

    value = str(text or "").strip().rstrip(".")
    if not value:
        return ""
    iso = re.search(
        r"\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
        r"(?:Z|[+-]\d{2}:?\d{2})?",
        value,
    )
    if iso is not None:
        return _reset_at_text(iso.group(0))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    relative = re.fullmatch(
        r"(?:in\s+)?(\d+)\s*(?:hr|hrs|hour|hours|시간)"
        r"(?:\s*(\d+)\s*(?:min|mins|minute|minutes|분))?",
        value,
        flags=re.IGNORECASE,
    ) or re.fullmatch(
        r"(?:in\s+)?(\d+)\s*(?:min|mins|minute|minutes|분)",
        value,
        flags=re.IGNORECASE,
    )
    if relative is not None:
        hours = int(relative.group(1))
        minutes = (
            int(relative.group(2))
            if relative.lastindex and relative.lastindex >= 2 and relative.group(2)
            else 0
        )
        if "min" in value.lower() or "분" in value:
            if "hr" not in value.lower() and "hour" not in value.lower() and "시간" not in value:
                hours, minutes = 0, int(relative.group(1))
        try:
            return (now + timedelta(hours=hours, minutes=minutes)).isoformat()
        except (OverflowError, ValueError):
            return ""
    clock = _clock_parts(value)
    weekday_match = re.search(
        r"\b(" + "|".join(sorted(_WEEKDAY_INDEX, key=len, reverse=True)) + r")"
        r"(?:요일)?\b",
        value,
        flags=re.IGNORECASE,
    )
    if weekday_match is not None:
        target_weekday = _WEEKDAY_INDEX[weekday_match.group(1).lower().replace("요일", "")]
        base = now
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        days_ahead = (target_weekday - base.weekday()) % 7
        if clock is not None:
            hour, minute = clock
            candidate = (base + timedelta(days=days_ahead)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= base:
                candidate += timedelta(days=7)
            return candidate.isoformat()
        return (base + timedelta(days=days_ahead or 7)).date().isoformat()
    month_match = re.search(
        r"\b(" + "|".join(sorted(_MONTH_INDEX, key=len, reverse=True)) + r")\b\.?"
        r"\s*(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?",
        value,
        flags=re.IGNORECASE,
    )
    if month_match is not None:
        month = _MONTH_INDEX[month_match.group(1).lower()]
        day = int(month_match.group(2))
        year = int(month_match.group(3) or now.year)
        try:
            candidate = datetime(year, month, day)
            if month_match.group(3) is None and candidate.date() < now.date():
                candidate = datetime(year + 1, month, day)
            return candidate.date().isoformat()
        except ValueError:
            return ""
    if clock is not None and re.fullmatch(
        r"(?:(?:at|오전|오후)\s*)?\d{1,2}(?::\d{2})?\s*(?:am|pm|오전|오후)?"
        r"\s*시?(?:\s*\d{1,2}\s*분)?",
        value,
        flags=re.IGNORECASE,
    ):
        hour, minute = clock
        base = now
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate.isoformat()
    return value


def _lines(text: object) -> list[str]:
    return [
        line
        for line in (part.strip() for part in str(text or "").replace("\r", "\n").split("\n"))
        if line
    ]


def _window_from_lines(lines: list[str], start: int, now: datetime) -> tuple[float | None, str, int]:
    """Read 'percent used' and 'Resets ...' within a short window after a label."""
    used: float | None = None
    reset_at = ""
    index = start
    limit = min(len(lines), start + 6)
    while index < limit:
        line = lines[index]
        if index > start and (
            _SESSION_LABEL_PATTERN.match(line)
            or _WEEKLY_LABEL_PATTERN.match(line)
            or _SCOPED_WEEKLY_LABEL_PATTERN.match(line)
            or _EXTRA_USAGE_LABEL_PATTERN.match(line)
        ):
            break
        percent_match = _PERCENT_USED_PATTERN.search(line)
        if used is None and percent_match is not None:
            used = _percent(percent_match.group(1))
        reset_match = _RESET_LINE_PATTERN.match(line)
        if not reset_at and reset_match is not None:
            reset_at = _parse_reset_text(reset_match.group(1), now)
        index += 1
    return used, reset_at, index


def parse_sanitized_claude_usage_text(
    text: object,
    *,
    now: datetime | None = None,
) -> ClaudeUsageData | None:
    """Parse the visible, pre-sanitized Claude usage summary text."""

    normalized = str(text or "").strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if any(
        marker in lowered
        for marker in (
            "sign in to continue",
            "log in to continue",
            "verify you are human",
            "로그인이 필요",
            "로그인하세요",
            "로그인 해주세요",
            "계속하려면 로그인",
        )
    ):
        return None
    reference = now or datetime.now(timezone.utc)
    lines = _lines(normalized)

    session_used: float | None = None
    session_reset = ""
    weekly_used: float | None = None
    weekly_reset = ""
    scoped_used: float | None = None
    scoped_reset = ""
    scoped_label = ""
    extra_enabled: bool | None = None
    extra_text = ""

    index = 0
    while index < len(lines):
        line = lines[index]
        if _SESSION_LABEL_PATTERN.match(line):
            session_used, session_reset, index = _window_from_lines(
                lines, index + 1, reference
            )
            continue
        if _WEEKLY_LABEL_PATTERN.match(line):
            # A weekly section heading may group several model rows; walk each,
            # but bound the scan so unrelated page sections cannot leak in.
            index += 1
            section_limit = min(len(lines), index + 12)
            while index < section_limit:
                sub = lines[index]
                if _SESSION_LABEL_PATTERN.match(sub) or _EXTRA_USAGE_LABEL_PATTERN.match(sub):
                    break
                if _SCOPED_WEEKLY_LABEL_PATTERN.match(sub):
                    used, reset, index = _window_from_lines(lines, index + 1, reference)
                    if scoped_used is None:
                        scoped_used, scoped_reset = used, reset
                        scoped_label = sub.split()[0].title()
                    continue
                percent_match = _PERCENT_USED_PATTERN.search(sub)
                reset_match = _RESET_LINE_PATTERN.match(sub)
                if weekly_used is None and percent_match is not None:
                    weekly_used = _percent(percent_match.group(1))
                if not weekly_reset and reset_match is not None:
                    weekly_reset = _parse_reset_text(reset_match.group(1), reference)
                index += 1
            continue
        if _EXTRA_USAGE_LABEL_PATTERN.match(line):
            following = _lines("\n".join(lines[index + 1 : index + 5]))
            ratio = re.search(
                r"((?:[A-Z]{2,3}\s*)?[$€£₩]?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
                r"/\s*((?:[A-Z]{2,3}\s*)?[$€£₩]?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                line + "\n" + "\n".join(following),
            )
            if ratio is not None:
                extra_enabled = True
                extra_text = (
                    f"{ratio.group(1).replace(' ', '')}{ratio.group(2)} / "
                    f"{ratio.group(3).replace(' ', '')}{ratio.group(4)}"
                )
            elif re.search(
                r"\b(?:off|disabled|inactive)\b|비활성",
                line + "\n" + "\n".join(following),
                flags=re.IGNORECASE,
            ):
                extra_enabled = False
            elif re.search(
                r"\b(?:on|enabled|active)\b|활성",
                line + "\n" + "\n".join(following),
                flags=re.IGNORECASE,
            ):
                extra_enabled = True
            index += 1
            continue
        index += 1

    if session_used is None and weekly_used is None and scoped_used is None:
        return None
    return ClaudeUsageData(
        session_used_percent=session_used,
        session_reset_at=session_reset,
        weekly_used_percent=weekly_used,
        weekly_reset_at=weekly_reset,
        scoped_weekly_used_percent=scoped_used,
        scoped_weekly_reset_at=scoped_reset,
        scoped_weekly_label=scoped_label,
        extra_usage_enabled=extra_enabled,
        extra_usage_text=extra_text,
    )


def _browser_error_state(error: object) -> UsageState:
    key = str(error or "").strip().lower()
    if key in {
        BrowserErrorCode.LOGIN_REQUIRED.value,
        BrowserErrorCode.LOGIN_WINDOW_CLOSED.value,
    }:
        return UsageState.LOGGED_OUT
    if key == BrowserErrorCode.CLOUDFLARE_CHALLENGE.value:
        return UsageState.UNKNOWN
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


@dataclass(frozen=True, slots=True)
class ClaudeUsageSnapshot:
    """Provider snapshot consumed by the multi-monitor and UI layers."""

    reading: AiUsageReading
    session_used_percent: float | None = None
    session_reset_at: str = ""
    weekly_used_percent: float | None = None
    weekly_reset_at: str = ""
    weekly_label: str = ""
    scoped_weekly_used_percent: float | None = None
    scoped_weekly_reset_at: str = ""
    scoped_weekly_label: str = ""
    extra_usage_text: str = ""

    @staticmethod
    def _remaining_text(used_percent: float | None) -> str:
        if used_percent is None:
            return ""
        remaining = round(100.0 - float(used_percent), 4)
        return f"{remaining:g}%"

    @staticmethod
    def _remaining_percent(used_percent: float | None) -> float | None:
        if used_percent is None:
            return None
        return round(100.0 - float(used_percent), 4)

    def to_dict(self) -> dict[str, Any]:
        data = self.reading.to_dict()
        data["five_hour_limit"] = self._remaining_text(self.session_used_percent)
        data["five_hour_limit_reset_at"] = str(self.session_reset_at or "")
        data["weekly_limit"] = self._remaining_text(self.weekly_used_percent)
        data["weekly_limit_reset_at"] = str(self.weekly_reset_at or "")
        scoped_value = ""
        if self.scoped_weekly_used_percent is not None:
            label = str(self.scoped_weekly_label or "").strip()
            scoped_value = (
                f"{label} {self._remaining_text(self.scoped_weekly_used_percent)}"
                if label
                else self._remaining_text(self.scoped_weekly_used_percent)
            )
        data["weekly_scoped_limit"] = scoped_value
        data["weekly_scoped_limit_reset_at"] = str(self.scoped_weekly_reset_at or "")
        data["extra_usage_text"] = str(self.extra_usage_text or "")
        if self.reading.on_demand_enabled is True:
            suffix = f" · {self.extra_usage_text}" if self.extra_usage_text else ""
            data["on_demand_status"] = f"ON{suffix}"
        elif self.reading.on_demand_enabled is False:
            data["on_demand_status"] = "OFF"
        else:
            data["on_demand_status"] = "조회 불가"
        metrics: list[dict[str, Any]] = []
        for key, short_label, used, reset_key in (
            ("five_hour_limit", "5H", self.session_used_percent, "session_reset_at"),
            ("weekly_limit", "7D", self.weekly_used_percent, "weekly_reset_at"),
        ):
            remaining = self._remaining_percent(used)
            if remaining is None:
                continue
            reset_at = str(
                getattr(self, reset_key) or ""
            )
            metrics.append(
                {
                    "key": key,
                    "short_label": short_label,
                    "percent": remaining,
                    "value_text": f"{remaining:g}%",
                    "short_value_text": f"{int(round(remaining))}%",
                    "reset_at": reset_at,
                    "reset_precision": "datetime" if "T" in reset_at else "",
                    "state": self.reading.state.value,
                }
            )
        data["metrics"] = metrics
        return data


class ClaudeUsageMonitor:
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
        self.profile_id = resolved_profile_id or "claude-personal"
        self.config_dir = str(
            config_dir
            or os.path.join(base_dir, "windows-supporter", f"claude-account-{self.profile_id}")
        )
        self.profile_dir = str(
            profile_dir
            or os.path.join(
                base_dir,
                "windows-supporter",
                "claude-usage-profiles",
                self.profile_id,
            )
        )
        self._settings_path = os.path.join(self.config_dir, "claude_usage_settings.json")
        self._state_path = os.path.join(self.config_dir, "claude_usage_state.json")
        self._event_log_path = os.path.join(self.config_dir, "claude_usage_events.jsonl")
        self._persistence_enabled = bool(
            config_dir is not None or browser_session_factory is None
        )
        self._notification_sink = notification_sink
        self._suppress_normal_tooltips = bool(suppress_normal_tooltips)
        self._enabled = True
        self._refresh_interval_sec = max(
            MIN_CLAUDE_REFRESH_INTERVAL_SEC, float(refresh_interval_sec)
        )
        self._stale_after_sec = max(self._refresh_interval_sec, float(stale_after_sec))
        self._tooltip_duration_ms = 7000
        self._limit_reset_sound_enabled = True
        self._alert_label_provider: Callable[[], str] | None = None
        self._limit_reset_lock = threading.Lock()
        self._limit_reset_baselines: dict[str, str] = {}
        self._last_committed_limit_usage: dict[str, str] | None = None
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
            provider=AiUsageProvider.CLAUDE,
            profile_id=self.profile_id,
            state=UsageState.UNKNOWN,
        )
        self._profile_name = ""
        self._profile_name_verified = False
        self._session_used_percent: float | None = None
        self._session_reset_at = ""
        self._weekly_used_percent: float | None = None
        self._weekly_reset_at = ""
        self._weekly_label = ""
        self._scoped_weekly_used_percent: float | None = None
        self._scoped_weekly_reset_at = ""
        self._scoped_weekly_label = ""
        self._extra_usage_text = ""
        self._restore_last_success()
        if self._last_reading.state == UsageState.STALE:
            self._last_error_type = UsageErrorType.TRANSIENT
        self._limit_reset_alert = UsageLimitResetAlert(
            title="Claude",
            post_ui=self._post_ui,
            get_root=lambda: self._root,
            get_duration_ms=lambda: int(self._tooltip_duration_ms),
            sound_enabled=lambda: bool(self._limit_reset_sound_enabled),
            get_label=self._resolve_alert_label,
        )
        config = PlaywrightSessionConfig(
            profile_dir=self.profile_dir,
            usage_url=CLAUDE_USAGE_URL,
            probe_script=CLAUDE_USAGE_PAGE_PROBE_SCRIPT,
            page_recycle_success_count=12,
            worker_recycle_success_count=48,
            worker_recycle_max_age_sec=3600.0,
        )
        if browser_session_factory is not None:
            self._session = browser_session_factory(config)
        else:
            self._session = _LazyClaudeBrowserSession(
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
                    self._commit_ready_reading(reading)
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
            "provider": AiUsageProvider.CLAUDE.value,
            "interval_sec": float(self._refresh_interval_sec),
            "tooltip_duration_ms": int(self._tooltip_duration_ms),
            "usage_url": CLAUDE_USAGE_URL,
            "collection_mode": CLAUDE_COLLECTION_MODE,
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
            self._refresh_interval_sec = max(MIN_CLAUDE_REFRESH_INTERVAL_SEC, interval)
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
            "provider": AiUsageProvider.CLAUDE.value,
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
            "collection_mode": CLAUDE_COLLECTION_MODE,
            "browser_state": browser.state.value,
            "browser_last_error": browser.last_error,
            "login_window_open": bool(browser.login_window_open),
            "can_login": session_state != "logged_in" and not browser.login_window_open,
            "can_logout": browser.state != BrowserState.STOPPED or session_state == "logged_in",
            "profile_name": str(self._profile_name or ""),
            "usage_history": [],
        }

    def get_last_snapshot(self) -> ClaudeUsageSnapshot:
        return ClaudeUsageSnapshot(
            reading=self._last_reading,
            session_used_percent=self._session_used_percent,
            session_reset_at=self._session_reset_at,
            weekly_used_percent=self._weekly_used_percent,
            weekly_reset_at=self._weekly_reset_at,
            weekly_label=self._weekly_label,
            scoped_weekly_used_percent=self._scoped_weekly_used_percent,
            scoped_weekly_reset_at=self._scoped_weekly_reset_at,
            scoped_weekly_label=self._scoped_weekly_label,
            extra_usage_text=self._extra_usage_text,
        )

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
                self._commit_ready_reading(self._last_reading)
            elif result.error in {
                BrowserErrorCode.LOGIN_REQUIRED.value,
                BrowserErrorCode.CLOUDFLARE_CHALLENGE.value,
                "rate_limited",
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
            return False, "진행 중인 Claude 조회를 중단하지 못했습니다. 잠시 후 다시 시도해 주세요."
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
            self._profile_name = ""
            self._profile_name_verified = False
            self._session_used_percent = None
            self._session_reset_at = ""
            self._weekly_used_percent = None
            self._weekly_reset_at = ""
            self._weekly_label = ""
            self._scoped_weekly_used_percent = None
            self._scoped_weekly_reset_at = ""
            self._scoped_weekly_label = ""
            self._extra_usage_text = ""
            self._last_reading = AiUsageReading.unavailable(
                provider=AiUsageProvider.CLAUDE,
                profile_id=self.profile_id,
                state=UsageState.LOGGED_OUT,
                captured_at=captured_at,
            )
            self._last_attempt_at = None
            self._failure_count = 0
            self._last_error_type = UsageErrorType.AUTH
        except Exception as exc:
            return False, f"Claude 전용 브라우저 세션 종료 실패: {type(exc).__name__}"
        finally:
            self._collect_lock.release()
        return True, message or "Claude 연결을 해제했습니다."

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
            basename == "claude"
            and re.fullmatch(r"profile_[0-9a-f]{32}", parent_basename) is not None
            and os.path.basename(grandparent_dir).lower() == "ai-profiles"
            and os.path.basename(great_grandparent_dir).lower() == "windows-supporter"
        )
        managed_name = (
            basename.startswith("claude-profile-")
            or (parent_basename == "claude-usage-profiles" and bool(basename))
            or dynamic_managed
        )
        if "windows-supporter" not in components or not managed_name:
            return False, "Windows Supporter가 관리하는 Claude 전용 프로필만 연결 해제할 수 있습니다."
        if dynamic_managed:
            app_root = great_grandparent_dir
        elif parent_basename == "claude-usage-profiles":
            app_root = os.path.dirname(os.path.dirname(profile_dir))
        else:
            app_root = os.path.dirname(profile_dir)
        if (
            os.path.basename(app_root).lower() != "windows-supporter"
            or not _is_non_reparse_descendant(profile_dir, app_root)
        ):
            return False, "Windows Supporter가 관리하는 Claude 전용 프로필만 연결 해제할 수 있습니다."
        if not os.path.isdir(profile_dir):
            return True, "이미 연결 해제된 상태입니다."
        try:
            shutil.rmtree(profile_dir)
        except OSError as exc:
            return False, f"Claude 전용 프로필 정리 실패: {type(exc).__name__}"
        return True, "Claude 연결을 해제했습니다."

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

        name_source = str(probe.get("profileNameSource", "") or "").strip().lower()
        verified = name_source in {"account", "organization", "email"}
        profile_name = sanitize_profile_name(
            probe.get("profileName", ""),
            verified=verified,
        )
        api_text = ""
        summary_text = ""
        for block in probe.get("metricBlocks", []):
            if not isinstance(block, dict):
                continue
            key = str(block.get("metric_key", ""))
            if key == "claude_usage_api" and not api_text:
                api_text = str(block.get("block_text", "") or "")
            elif key == "claude_usage_summary" and not summary_text:
                summary_text = str(block.get("block_text", "") or "")
        parsed = parse_claude_usage_api_payload(api_text) if api_text else None
        if parsed is None:
            if not summary_text:
                summary_text = str(probe.get("mainText", "") or "")
            parsed = parse_sanitized_claude_usage_text(summary_text, now=now)
        if parsed is None or (
            parsed.session_used_percent is None
            and parsed.weekly_used_percent is None
            and parsed.scoped_weekly_used_percent is None
        ):
            return self._failure_reading(
                state=UsageState.DOM_DRIFT,
                captured_at=captured_at,
            )
        # Successful usage scrape owns identity: empty profileName clears stale names.
        self._profile_name = profile_name
        self._profile_name_verified = bool(verified and profile_name)
        self._session_used_percent = parsed.session_used_percent
        self._session_reset_at = parsed.session_reset_at
        self._weekly_used_percent = parsed.weekly_used_percent
        self._weekly_reset_at = parsed.weekly_reset_at
        self._weekly_label = parsed.weekly_label
        self._scoped_weekly_used_percent = parsed.scoped_weekly_used_percent
        self._scoped_weekly_reset_at = parsed.scoped_weekly_reset_at
        self._scoped_weekly_label = parsed.scoped_weekly_label
        self._extra_usage_text = parsed.extra_usage_text
        primary_used = (
            parsed.session_used_percent
            if parsed.session_used_percent is not None
            else parsed.weekly_used_percent
            if parsed.weekly_used_percent is not None
            else parsed.scoped_weekly_used_percent
        )
        primary_reset = parsed.session_reset_at or parsed.weekly_reset_at
        return AiUsageReading(
            provider=AiUsageProvider.CLAUDE,
            profile_id=self.profile_id,
            state=UsageState.READY,
            used_percent=primary_used,
            remaining_percent=(
                None if primary_used is None else round(100.0 - primary_used, 4)
            ),
            captured_at=captured_at,
            last_success_at=captured_at,
            reset_at=primary_reset,
            on_demand_enabled=parsed.extra_usage_enabled,
        )

    def _failure_reading(self, *, state: UsageState, captured_at: str) -> AiUsageReading:
        prior = self._last_reading
        if prior.is_usable:
            return AiUsageReading(
                provider=AiUsageProvider.CLAUDE,
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
            provider=AiUsageProvider.CLAUDE,
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
            error_key = str(result.error).strip().lower()
            if error_key == BrowserErrorCode.CLOUDFLARE_CHALLENGE.value:
                # An edge challenge is environmental and retryable; it must
                # not pin the provider to the auth-required "OUT" projection.
                return UsageErrorType.TRANSIENT
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
            "provider": AiUsageProvider.CLAUDE.value,
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
                    "provider": AiUsageProvider.CLAUDE.value,
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
            name=f"ClaudeUsageLoginPoll-{self.profile_id}",
            daemon=True,
        )
        self._login_poll_thread.start()

    def _run_login_poll(self) -> None:
        rate_limit_streak = 0
        # Preserve the original lifetime contract: attempts * interval seconds.
        deadline = time.monotonic() + (
            self._login_poll_max_attempts * self._login_poll_interval_sec
        )
        for _attempt in range(self._login_poll_max_attempts):
            if time.monotonic() >= deadline:
                break
            wait = self._login_poll_interval_sec
            if rate_limit_streak:
                # Back off probe fetches while the API is throttling us.
                wait = min(wait * (2 ** min(rate_limit_streak, 5)), 30.0)
            remaining = deadline - time.monotonic()
            if self._login_poll_stop.wait(min(wait, max(remaining, 0.0))):
                return
            try:
                result = self._session.poll_login()
            except Exception:
                result = BrowserOperationResult(
                    error=BrowserErrorCode.COLLECT_FAILED.value
                )
            if result.error == "rate_limited":
                rate_limit_streak += 1
            else:
                rate_limit_streak = 0
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            reading = self._reading_from_result(result, now)
            self._last_reading = reading
            if reading.state == UsageState.READY:
                self._failure_count = 0
                self._last_error_type = UsageErrorType.NONE
                self._commit_ready_reading(reading)
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
        if not isinstance(data, dict) or data.get("provider") not in {None, "claude"}:
            return
        if "enabled" in data:
            self._enabled = bool(data.get("enabled"))
        try:
            interval = float(data.get("interval_sec", self._refresh_interval_sec))
        except (TypeError, ValueError):
            interval = self._refresh_interval_sec
        self._refresh_interval_sec = max(MIN_CLAUDE_REFRESH_INTERVAL_SEC, interval)
        try:
            duration = int(data.get("tooltip_duration_ms", self._tooltip_duration_ms))
        except (TypeError, ValueError):
            duration = self._tooltip_duration_ms
        self._tooltip_duration_ms = max(1200, duration)
        self._limit_reset_sound_enabled = bool(
            data.get("limit_reset_sound_enabled", self._limit_reset_sound_enabled)
        )

    def _save_settings_file(self) -> None:
        if not self._persistence_enabled:
            return
        try:
            self._write_json_atomic(
                self._settings_path,
                {
                    "settings_version": 1,
                    "provider": AiUsageProvider.CLAUDE.value,
                    "enabled": bool(self._enabled),
                    "interval_sec": float(self._refresh_interval_sec),
                    "tooltip_duration_ms": int(self._tooltip_duration_ms),
                    "limit_reset_sound_enabled": bool(self._limit_reset_sound_enabled),
                    "collection_mode": CLAUDE_COLLECTION_MODE,
                },
            )
        except OSError:
            pass

    def _restore_last_success(self) -> None:
        if not self._persistence_enabled:
            return
        data = self._read_json(self._state_path)
        if not isinstance(data, dict) or data.get("provider") != "claude":
            return
        try:
            reading = AiUsageReading(
                provider=AiUsageProvider.CLAUDE,
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
        self._session_used_percent = _percent(data.get("session_used_percent"))
        self._session_reset_at = _reset_at_text(data.get("session_reset_at"))
        self._weekly_used_percent = _percent(data.get("weekly_used_percent"))
        self._weekly_reset_at = _reset_at_text(data.get("weekly_reset_at"))
        self._weekly_label = str(data.get("weekly_label") or "").strip()
        self._scoped_weekly_used_percent = _percent(
            data.get("scoped_weekly_used_percent")
        )
        self._scoped_weekly_reset_at = _reset_at_text(
            data.get("scoped_weekly_reset_at")
        )
        self._scoped_weekly_label = str(data.get("scoped_weekly_label") or "").strip()
        self._extra_usage_text = str(data.get("extra_usage_text") or "").strip()
        from src.apps.codex_usage_monitor import sanitize_profile_name

        profile_name_verified = data.get("profile_name_verified") is True
        profile_name = sanitize_profile_name(
            data.get("profile_name", ""),
            verified=profile_name_verified,
        )
        if profile_name:
            self._profile_name = profile_name
            self._profile_name_verified = profile_name_verified
        raw_baselines = data.get("limit_reset_baselines")
        if isinstance(raw_baselines, dict):
            self._limit_reset_baselines = {
                str(key): str(value)
                for key, value in raw_baselines.items()
                if str(key or "").strip() and str(value or "").strip()
            }
        if reading.is_usable:
            self._last_committed_limit_usage = self._limit_usage_payload()
            if not self._limit_reset_baselines:
                # Same as the Codex state loader: seed baselines from the
                # restored deadlines so a reset that happened while the app
                # was not running is still detected on the first reading.
                from src.apps.codex_usage_monitor import (
                    UsageSnapshot,
                    advance_limit_reset_baselines,
                )

                advance_limit_reset_baselines(
                    self._limit_reset_baselines,
                    UsageSnapshot.from_dict(self._last_committed_limit_usage),
                )

    def set_alert_label_provider(self, provider: Callable[[], str] | None) -> None:
        # The profile manager owns the label shown on the profile card; alerts
        # use the same label so the user can tell which profile was reset.
        self._alert_label_provider = provider if callable(provider) else None

    def _resolve_alert_label(self) -> str:
        provider = self._alert_label_provider
        if provider is None:
            return ""
        try:
            return str(provider() or "").strip()
        except Exception:
            return ""

    def _post_ui(self, fn: Callable[[], None]) -> bool:
        queue_obj = self._event_queue
        if queue_obj is None:
            return False
        try:
            queue_obj.put(fn)
            return True
        except Exception:
            return False

    def _limit_usage_payload(self) -> dict[str, str]:
        snapshot = self.get_last_snapshot().to_dict()
        reading = self._last_reading
        return {
            "captured_at": str(reading.last_success_at or reading.captured_at or ""),
            "five_hour_limit": str(snapshot.get("five_hour_limit") or ""),
            "five_hour_limit_reset_at": str(snapshot.get("five_hour_limit_reset_at") or ""),
            "weekly_limit": str(snapshot.get("weekly_limit") or ""),
            "weekly_limit_reset_at": str(snapshot.get("weekly_limit_reset_at") or ""),
        }

    def _observe_limit_resets(self) -> list[Any]:
        # Same detection and baseline contract as the Codex monitor, so a
        # Claude session/weekly window reset alerts exactly once as well.
        from src.apps.codex_usage_monitor import (
            UsageSnapshot,
            advance_limit_reset_baselines,
            compute_usage_limit_resets,
        )

        current_payload = self._limit_usage_payload()
        current = UsageSnapshot.from_dict(current_payload)
        if not current.has_any_metric():
            return []
        previous_payload = self._last_committed_limit_usage
        previous = (
            UsageSnapshot.from_dict(previous_payload)
            if isinstance(previous_payload, dict)
            else None
        )
        resets = compute_usage_limit_resets(
            self._limit_reset_baselines,
            current,
            previous=previous,
            observed_snapshot=current,
        )
        advance_limit_reset_baselines(self._limit_reset_baselines, current, resets=resets)
        self._last_committed_limit_usage = current_payload
        if resets:
            self._limit_reset_alert.submit(list(resets))
        return resets

    def _commit_ready_reading(self, reading: AiUsageReading) -> None:
        # collect(), manual login and the login poll can commit from
        # different threads; observe and persist atomically so a reset is
        # neither alerted twice nor saved with half-updated baselines.
        with self._limit_reset_lock:
            self._observe_limit_resets()
            self._save_last_success(reading)

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
                    "provider": AiUsageProvider.CLAUDE.value,
                    "used_percent": reading.used_percent,
                    "remaining_percent": reading.remaining_percent,
                    "included_used": reading.included_used,
                    "included_limit": reading.included_limit,
                    "captured_at": reading.last_success_at or reading.captured_at,
                    "reset_at": reading.reset_at,
                    "reset_precision": reading.reset_precision,
                    "on_demand_enabled": reading.on_demand_enabled,
                    "profile_name": str(self._profile_name or ""),
                    "profile_name_verified": bool(self._profile_name_verified),
                    "session_used_percent": self._session_used_percent,
                    "session_reset_at": self._session_reset_at,
                    "weekly_used_percent": self._weekly_used_percent,
                    "weekly_reset_at": self._weekly_reset_at,
                    "weekly_label": self._weekly_label,
                    "scoped_weekly_used_percent": self._scoped_weekly_used_percent,
                    "scoped_weekly_reset_at": self._scoped_weekly_reset_at,
                    "scoped_weekly_label": self._scoped_weekly_label,
                    "extra_usage_text": self._extra_usage_text,
                    "limit_reset_baselines": dict(self._limit_reset_baselines),
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
