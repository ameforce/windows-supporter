from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import final, override
from urllib.parse import urlsplit, urlunsplit

from src.apps.codex_usage_browser_types import (
    BrowserErrorCode,
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    ContextLike,
    LogSink,
    PageLike,
    PlaywrightSessionConfig,
    PlaywrightLike,
    PlaywrightStarter,
    UsageProbePayload,
    parse_usage_probe,
)


@final
@dataclass(frozen=True, slots=True)
class DriverOperationError(RuntimeError):
    message: str

    @override
    def __str__(self) -> str:
        return self.message


def _default_playwright_starter() -> PlaywrightLike:
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def _playwright_error_type() -> type[Exception]:
    from playwright.sync_api import Error

    return Error


def _canonical_usage_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/").lower()
    fragment = parts.fragment.lower()
    if host in {"chatgpt.com", "www.chatgpt.com"} and (
        path in {"/codex/settings/usage", "/codex/cloud/settings/analytics"}
        or fragment == "usage"
    ):
        return "https://chatgpt.com/codex/settings/usage"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, fragment))


def _classify_error(message: str, *, login_poll: bool = False) -> str:
    lowered = message.lower()
    if any(
        token in lowered
        for token in (
            "processsingleton",
            "profile is already in use",
            "user data directory is already in use",
            "singletonlock",
            "exitcode=21",
            "exit code: 21",
        )
    ):
        return BrowserErrorCode.PROFILE_IN_USE.value
    if any(
        token in lowered
        for token in (
            "chromium distribution 'chrome' is not found",
            'chromium distribution "chrome" is not found',
            "chrome distribution is not found",
            "chrome is not installed",
            "executable doesn't exist",
        )
    ):
        return BrowserErrorCode.BROWSER_CHANNEL_UNAVAILABLE.value
    if any(token in lowered for token in ("playwright unavailable", "no module named 'playwright'", "playwright not installed")):
        return BrowserErrorCode.PLAYWRIGHT_UNAVAILABLE.value
    if login_poll and any(token in lowered for token in ("closed", "target page", "browser has been closed")):
        return BrowserErrorCode.LOGIN_WINDOW_CLOSED.value
    return BrowserErrorCode.COLLECT_FAILED.value


@final
class CodexUsagePlaywrightDriver:
    """Owns one synchronous Playwright runtime and persistent Chrome context."""

    def __init__(
        self,
        config: PlaywrightSessionConfig,
        log_sink: LogSink | None = None,
        playwright_starter: PlaywrightStarter | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config: PlaywrightSessionConfig = config
        self._log_sink: LogSink | None = log_sink
        self._starter: PlaywrightStarter = playwright_starter or _default_playwright_starter
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._playwright: PlaywrightLike | None = None
        self._context: ContextLike | None = None
        self._page: PageLike | None = None
        self._headless: bool | None = None
        self._cached_user_agent: str | None = None
        self._status: BrowserRuntimeStatus = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
        self._shutdown: bool = False

    def start(self) -> BrowserOperationResult:
        if self._playwright is not None:
            return BrowserOperationResult()
        self._set_status(BrowserState.STARTING)
        try:
            self._playwright = self._starter()
        except (ImportError, OSError, RuntimeError) as exc:
            return self._fail(BrowserErrorCode.PLAYWRIGHT_UNAVAILABLE.value, str(exc))
        except _playwright_error_type() as exc:
            return self._fail(BrowserErrorCode.PLAYWRIGHT_UNAVAILABLE.value, str(exc))
        self._set_status(BrowserState.STOPPED)
        return BrowserOperationResult()

    def collect(self) -> BrowserOperationResult:
        for context_attempt in range(2):
            last_error = "usage collection failed"
            try:
                page = self._ensure_context(headless=True)
                for page_attempt in range(2):
                    try:
                        self._navigate_for_collect(page)
                        probe = self._evaluate_probe_until_ready(page)
                        if probe is not None and self._probe_is_terminal(probe):
                            self._set_status(BrowserState.HEADLESS_READY)
                            return BrowserOperationResult(probe=probe)
                        raise DriverOperationError("usage probe did not become ready")
                    except (DriverOperationError, OSError, RuntimeError) as exc:
                        last_error = str(exc)
                    except _playwright_error_type() as exc:
                        last_error = str(exc)
                    if page_attempt == 0:
                        page = self._replace_page()
                error = _classify_error(last_error)
            except (DriverOperationError, OSError, RuntimeError) as exc:
                last_error = str(exc)
                error = _classify_error(last_error)
            except _playwright_error_type() as exc:
                last_error = str(exc)
                error = _classify_error(last_error)
            if error != BrowserErrorCode.COLLECT_FAILED.value:
                return self._fail(error, last_error)
            if context_attempt == 0:
                self._set_status(BrowserState.RECOVERING)
                self._close_context()
        return self._fail(BrowserErrorCode.COLLECT_FAILED.value)

    def open_login(self) -> BrowserOperationResult:
        try:
            page = self._ensure_context(headless=False)
            _ = page.goto(self._config.usage_url, timeout=self._config.navigation_timeout_ms, wait_until="domcontentloaded")
            probe = self._evaluate_probe_until_ready(page)
            if probe is not None and self._probe_is_authenticated(probe):
                self._close_context()
                _ = self._ensure_context(headless=True)
                self._set_status(BrowserState.HEADLESS_READY)
                return BrowserOperationResult(probe=probe)
        except (OSError, RuntimeError) as exc:
            return self._fail(_classify_error(str(exc)), str(exc))
        except _playwright_error_type() as exc:
            return self._fail(_classify_error(str(exc)), str(exc))
        if probe is not None and self._probe_is_cloudflare(probe):
            return self._fail(
                BrowserErrorCode.CLOUDFLARE_CHALLENGE.value,
                state=BrowserState.HEADED_LOGIN,
                login_window_open=True,
            )
        return self._fail(
            BrowserErrorCode.LOGIN_REQUIRED.value,
            state=BrowserState.HEADED_LOGIN,
            login_window_open=True,
        )

    def poll_login(self) -> BrowserOperationResult:
        page = self._page
        if self._headless is not False or page is None or page.is_closed():
            self._close_context()
            return self._fail(BrowserErrorCode.LOGIN_WINDOW_CLOSED.value)
        try:
            probe = self._evaluate_probe_until_ready(page)
            if probe is not None and self._probe_is_cloudflare(probe):
                return self._fail(
                    BrowserErrorCode.CLOUDFLARE_CHALLENGE.value,
                    state=BrowserState.HEADED_LOGIN,
                    login_window_open=True,
                )
            if probe is None or not self._probe_is_authenticated(probe):
                return self._fail(BrowserErrorCode.LOGIN_REQUIRED.value, state=BrowserState.HEADED_LOGIN, login_window_open=True)
            self._close_context()
            _ = self._ensure_context(headless=True)
        except (OSError, RuntimeError) as exc:
            error = _classify_error(str(exc), login_poll=True)
            if error == BrowserErrorCode.LOGIN_WINDOW_CLOSED.value:
                self._close_context()
            return self._fail(error, str(exc))
        except _playwright_error_type() as exc:
            error = _classify_error(str(exc), login_poll=True)
            if error == BrowserErrorCode.LOGIN_WINDOW_CLOSED.value:
                self._close_context()
            return self._fail(error, str(exc))
        self._set_status(BrowserState.HEADLESS_READY)
        return BrowserOperationResult(probe=probe)

    def close_session(self) -> None:
        self._close_context()
        self._set_status(BrowserState.STOPPED)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._close_context()
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except (OSError, RuntimeError):
                self._log("playwright stop failed")
            except _playwright_error_type():
                self._log("playwright stop failed")
        self._playwright = None
        self._set_status(BrowserState.STOPPED)

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return self._status

    def _ensure_context(self, *, headless: bool) -> PageLike:
        if self._playwright is None:
            started = self.start()
            if started.error:
                raise DriverOperationError(started.error)
        if self._context is not None and self._headless == headless and self._page is not None and not self._page.is_closed():
            return self._page
        self._close_context()
        self._set_status(BrowserState.STARTING)
        page = self._launch_context(headless=headless)
        if headless and self._cached_user_agent is None:
            user_agent = page.evaluate("() => navigator.userAgent")
            if isinstance(user_agent, str) and user_agent:
                self._cached_user_agent = user_agent.replace("HeadlessChrome", "Chrome")
                if user_agent != self._cached_user_agent:
                    self._close_context()
                    page = self._launch_context(headless=True)
        return page

    def _launch_context(self, *, headless: bool) -> PageLike:
        if self._playwright is None:
            raise DriverOperationError("playwright unavailable")
        context = self._playwright.chromium.launch_persistent_context(
            self._config.profile_dir,
            channel="chrome",
            headless=headless,
            chromium_sandbox=True,
            args=["--disable-extensions", "--disable-notifications"],
            user_agent=self._cached_user_agent,
            timeout=float(self._config.navigation_timeout_ms),
        )
        self._context = context
        self._headless = headless
        self._page = context.pages[0] if context.pages else context.new_page()
        return self._page

    def _replace_page(self) -> PageLike:
        if self._context is None:
            raise DriverOperationError("browser context is unavailable")
        if self._page is not None and not self._page.is_closed():
            self._page.close()
        self._page = self._context.new_page()
        return self._page

    def _navigate_for_collect(self, page: PageLike) -> None:
        if _canonical_usage_url(page.url) == _canonical_usage_url(self._config.usage_url):
            _ = page.reload(timeout=self._config.navigation_timeout_ms, wait_until="domcontentloaded")
        else:
            _ = page.goto(self._config.usage_url, timeout=self._config.navigation_timeout_ms, wait_until="domcontentloaded")

    def _probe_is_authenticated(self, probe: UsageProbePayload) -> bool:
        main_text = str(probe.get("mainText", "")).lower()
        if any(token in main_text for token in ("log in", "sign in", "로그인")):
            return False
        return bool(probe.get("metricBlocks")) or any(token in main_text for token in ("usage", "limit", "사용", "한도"))

    def _evaluate_probe_until_ready(self, page: PageLike) -> UsageProbePayload | None:
        last_probe: UsageProbePayload | None = None
        for attempt in range(21):
            probe = parse_usage_probe(page.evaluate(self._config.probe_script))
            if probe is None:
                raise DriverOperationError("usage probe did not return an object")
            last_probe = probe
            if self._probe_is_terminal(probe):
                return probe
            if attempt < 20:
                self._sleep(0.25)
        return last_probe

    def _probe_is_terminal(self, probe: UsageProbePayload) -> bool:
        if any(
            str(block.get("metric_key", "")) != "remaining_credit"
            for block in probe.get("metricBlocks", [])
        ):
            return True
        if self._probe_is_cloudflare(probe):
            return True
        url = str(probe.get("url", "")).lower()
        combined = " ".join(str(probe.get(key, "")) for key in ("title", "mainText")).lower()
        if any(token in url for token in ("/login", "/auth", "signin", "sign-in")):
            return True
        return any(
            marker in combined
            for marker in (
                "log in",
                "sign in",
                "continue with google",
                "로그인",
            )
        )

    def _probe_is_cloudflare(self, probe: UsageProbePayload) -> bool:
        combined = " ".join(
            str(probe.get(key, ""))
            for key in ("title", "mainText")
        ).lower()
        return any(
            marker in combined
            for marker in (
                "cloudflare",
                "verify you are human",
                "checking your browser",
                "challenge-platform",
                "challenge-error-text",
                "enable javascript and cookies to continue",
            )
        )

    def _close_context(self) -> None:
        context = self._context
        self._context = None
        self._page = None
        self._headless = None
        if context is None:
            return
        try:
            context.close()
        except (OSError, RuntimeError):
            self._log("browser context close failed")
        except _playwright_error_type():
            self._log("browser context close failed")

    def _fail(self, error: str, detail: str = "", *, state: BrowserState | None = None, login_window_open: bool = False) -> BrowserOperationResult:
        target_state = state or (BrowserState.PROFILE_IN_USE if error == BrowserErrorCode.PROFILE_IN_USE.value else BrowserState.FAILED)
        self._set_status(target_state, login_window_open=login_window_open, error=error)
        self._log(f"browser operation failed error={error} detail={detail}")
        return BrowserOperationResult(error=error)

    def _set_status(self, state: BrowserState, *, login_window_open: bool = False, error: str = "") -> None:
        self._status = BrowserRuntimeStatus(state, login_window_open, error)

    def _log(self, message: str) -> None:
        if self._log_sink is not None:
            self._log_sink(message)
