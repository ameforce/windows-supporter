from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, final, override
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


_T = TypeVar("_T")


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
            "page crashed",
            "renderer process crashed",
            "renderer crashed",
        )
    ):
        return BrowserErrorCode.RENDERER_CRASHED.value
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
    if any(
        token in lowered
        for token in (
            "timeout",
            "timed out",
            "deadline exceeded",
        )
    ):
        return BrowserErrorCode.COMMAND_TIMEOUT.value
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
        initial_session_cookies: list[dict[str, Any]] | None = None,
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
        self._context_generation: int = 0
        self._page_generation: int = 0
        self._page_success_count: int = 0
        self._page_crashed: bool = False
        self._session_cookies: list[dict[str, Any]] = [
            dict(cookie) for cookie in (initial_session_cookies or [])
        ]

    def start(self) -> BrowserOperationResult:
        if self._playwright is not None:
            return BrowserOperationResult()
        self._set_status(BrowserState.STARTING)
        try:
            self._playwright = self._run_stage("playwright_start", self._starter)
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
                try:
                    self._navigate_for_collect(page)
                    self._raise_if_page_crashed()
                    probe = self._run_stage(
                        "evaluate_probe",
                        lambda: self._evaluate_probe_until_ready(page),
                    )
                    self._raise_if_page_crashed()
                    if probe is not None and self._probe_is_terminal(probe):
                        self._page_success_count += 1
                        self._set_status(BrowserState.HEADLESS_READY)
                        return BrowserOperationResult(probe=probe)
                    raise DriverOperationError("usage probe did not become ready")
                except (DriverOperationError, OSError, RuntimeError) as exc:
                    last_error = str(exc)
                except _playwright_error_type() as exc:
                    last_error = str(exc)
                last_error = self._probe_crash_after_navigation_abort(
                    page,
                    last_error,
                )
                error = self._classify_driver_error(last_error)
            except (DriverOperationError, OSError, RuntimeError) as exc:
                last_error = str(exc)
                error = self._classify_driver_error(last_error)
            except _playwright_error_type() as exc:
                last_error = str(exc)
                error = self._classify_driver_error(last_error)
            if error == BrowserErrorCode.RENDERER_CRASHED.value:
                return self._fail(error, last_error)
            if error not in {
                BrowserErrorCode.COLLECT_FAILED.value,
                BrowserErrorCode.COMMAND_TIMEOUT.value,
            }:
                return self._fail(error, last_error)
            if context_attempt == 0:
                self._set_status(BrowserState.RECOVERING)
                self._close_context()
                continue
            self._close_context()
            return self._fail(error, last_error)
        return self._fail(BrowserErrorCode.COLLECT_FAILED.value)

    def open_login(self) -> BrowserOperationResult:
        probe: UsageProbePayload | None = None
        try:
            page = self._ensure_context(headless=False)
            _ = self._run_stage(
                "navigation",
                lambda: page.goto(
                    self._config.usage_url,
                    timeout=self._config.navigation_timeout_ms,
                    wait_until="domcontentloaded",
                ),
            )
            probe = self._run_stage(
                "evaluate_probe",
                lambda: self._evaluate_probe_until_ready(page),
            )
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
            probe = self._run_stage(
                "evaluate_probe",
                lambda: self._evaluate_probe_until_ready(page),
            )
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
                playwright = self._playwright
                self._run_stage("playwright_stop", playwright.stop)
            except (OSError, RuntimeError):
                self._log("playwright stop failed")
            except _playwright_error_type():
                self._log("playwright stop failed")
        self._playwright = None
        self._set_status(BrowserState.STOPPED)

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return self._status

    def export_session_cookies(self) -> list[dict[str, Any]]:
        if self._context is not None:
            self._snapshot_session_cookies(self._context)
        return [dict(cookie) for cookie in self._session_cookies]

    def _ensure_context(self, *, headless: bool) -> PageLike:
        if self._playwright is None:
            started = self.start()
            if started.error:
                raise DriverOperationError(started.error)
        if self._context is not None and self._headless == headless and self._page is not None and not self._page.is_closed():
            if headless and self._page_success_count >= max(
                1, int(self._config.page_recycle_success_count)
            ):
                self._log(
                    "browser recycle requested "
                    f"reason=page_success_count count={self._page_success_count}"
                )
                return self._replace_page()
            return self._page
        self._close_context()
        self._set_status(BrowserState.STARTING)
        page = self._launch_context(headless=headless)
        if headless and self._cached_user_agent is None:
            user_agent = self._run_stage(
                "evaluate_user_agent",
                lambda: page.evaluate("() => navigator.userAgent"),
            )
            if isinstance(user_agent, str) and user_agent:
                self._cached_user_agent = user_agent.replace("HeadlessChrome", "Chrome")
                if user_agent != self._cached_user_agent:
                    self._close_context()
                    page = self._launch_context(headless=True)
        return page

    def _launch_context(self, *, headless: bool) -> PageLike:
        if self._playwright is None:
            raise DriverOperationError("playwright unavailable")
        args = [
            "--disable-extensions",
            "--disable-notifications",
            "--disable-blink-features=AutomationControlled",
        ]
        ignore_default_args = ["--enable-automation"]
        user_agent = self._cached_user_agent if headless else None
        context = self._run_stage(
            "context_launch",
            lambda: self._playwright.chromium.launch_persistent_context(
                self._config.profile_dir,
                channel="chrome",
                headless=headless,
                chromium_sandbox=True,
                args=args,
                ignore_default_args=ignore_default_args,
                user_agent=user_agent,
                timeout=float(self._config.navigation_timeout_ms),
            ),
        )
        self._context = context
        self._headless = headless
        self._context_generation += 1
        add_cookies = getattr(context, "add_cookies", None)
        if self._session_cookies and callable(add_cookies):
            self._run_stage(
                "session_cookie_restore",
                lambda: add_cookies(
                    [dict(cookie) for cookie in self._session_cookies]
                ),
            )
            self._log(
                "browser session cookies restored "
                f"count={len(self._session_cookies)} "
                f"context_generation={self._context_generation}"
            )
        self._page = context.pages[0] if context.pages else context.new_page()
        self._register_page(self._page)
        return self._page

    def _replace_page(self) -> PageLike:
        if self._context is None:
            raise DriverOperationError("browser context is unavailable")
        if self._page is not None and not self._page.is_closed():
            page = self._page
            self._run_stage("page_close", page.close)
        self._page = self._context.new_page()
        self._register_page(self._page)
        return self._page

    def _navigate_for_collect(self, page: PageLike) -> None:
        if _canonical_usage_url(page.url) == _canonical_usage_url(self._config.usage_url):
            _ = self._run_stage(
                "navigation",
                lambda: page.reload(
                    timeout=self._config.navigation_timeout_ms,
                    wait_until="domcontentloaded",
                ),
            )
        else:
            _ = self._run_stage(
                "navigation",
                lambda: page.goto(
                    self._config.usage_url,
                    timeout=self._config.navigation_timeout_ms,
                    wait_until="domcontentloaded",
                ),
            )

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
        if context is not None:
            self._snapshot_session_cookies(context)
        self._context = None
        self._page = None
        self._headless = None
        self._page_success_count = 0
        self._page_crashed = False
        if context is None:
            return
        try:
            self._run_stage("context_close", context.close)
        except (OSError, RuntimeError):
            self._log("browser context close failed")
        except _playwright_error_type():
            self._log("browser context close failed")

    def _snapshot_session_cookies(self, context: ContextLike) -> None:
        cookies = getattr(context, "cookies", None)
        if not callable(cookies):
            return
        try:
            raw_cookies = self._run_stage("session_cookie_snapshot", cookies)
        except BaseException:
            self._log("browser session cookie snapshot failed")
            return
        if not isinstance(raw_cookies, list):
            return
        session_cookies: list[dict[str, Any]] = []
        for cookie in raw_cookies:
            if not isinstance(cookie, dict):
                continue
            try:
                expires = float(cookie.get("expires", -1))
            except (TypeError, ValueError):
                expires = -1
            if expires <= 0:
                session_cookies.append(dict(cookie))
        self._session_cookies = session_cookies
        self._log(
            "browser session cookies captured "
            f"count={len(session_cookies)} "
            f"context_generation={self._context_generation}"
        )
    def _fail(self, error: str, detail: str = "", *, state: BrowserState | None = None, login_window_open: bool = False) -> BrowserOperationResult:
        target_state = state or (BrowserState.PROFILE_IN_USE if error == BrowserErrorCode.PROFILE_IN_USE.value else BrowserState.FAILED)
        self._set_status(target_state, login_window_open=login_window_open, error=error)
        self._log(f"browser operation failed error={error} detail={detail}")
        return BrowserOperationResult(error=error)

    def _set_status(self, state: BrowserState, *, login_window_open: bool = False, error: str = "") -> None:
        self._status = BrowserRuntimeStatus(state, login_window_open, error)

    def _register_page(self, page: PageLike) -> None:
        self._page_generation += 1
        self._page_success_count = 0
        self._page_crashed = False
        try:
            page.on("crash", self._on_page_crash)
        except (AttributeError, TypeError):
            return

    def _on_page_crash(self) -> None:
        self._page_crashed = True
        self._log(
            "browser page crashed "
            f"context_generation={self._context_generation} "
            f"page_generation={self._page_generation}"
        )

    def _raise_if_page_crashed(self) -> None:
        if self._page_crashed:
            raise DriverOperationError("Page crashed")

    def _classify_driver_error(self, message: str) -> str:
        if self._page_crashed:
            return BrowserErrorCode.RENDERER_CRASHED.value
        return _classify_error(message)

    def _probe_crash_after_navigation_abort(
        self,
        page: PageLike,
        message: str,
    ) -> str:
        if "net::err_aborted" not in message.lower():
            return message
        try:
            self._run_stage("crash_probe", lambda: page.evaluate("() => true"))
        except BaseException as exc:
            detail = str(exc)
            if _classify_error(detail) == BrowserErrorCode.RENDERER_CRASHED.value:
                self._page_crashed = True
            return f"{message}; crash_probe={detail}"
        return message

    def _run_stage(self, stage: str, operation: Callable[[], _T]) -> _T:
        started_at = time.monotonic()
        self._log(
            "browser stage start "
            f"stage={stage} context_generation={self._context_generation} "
            f"page_generation={self._page_generation}"
        )
        try:
            result = operation()
        except BaseException as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1_000)
            self._log(
                "browser stage end "
                f"stage={stage} elapsed_ms={elapsed_ms} outcome=error "
                f"type={type(exc).__name__} context_generation={self._context_generation} "
                f"page_generation={self._page_generation}"
            )
            raise
        elapsed_ms = int((time.monotonic() - started_at) * 1_000)
        self._log(
            "browser stage end "
            f"stage={stage} elapsed_ms={elapsed_ms} outcome=success "
            f"context_generation={self._context_generation} "
            f"page_generation={self._page_generation}"
        )
        return result

    def _log(self, message: str) -> None:
        if self._log_sink is not None:
            self._log_sink(message)
