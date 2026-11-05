from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast, final, override
from urllib.parse import urlsplit, urlunsplit

from src.apps.codex_api_credit import (
    API_CREDIT_BILLING_URL,
    API_CREDIT_HOST,
    API_CREDIT_UNREADABLE_REASON,
    ApiCreditReading,
    ApiCreditStatus,
    describe_api_credit_shape,
    parse_api_credit_probe,
)
from src.apps.codex_usage_urls import canonicalize_codex_usage_url, is_codex_usage_url, is_legacy_usage_dialog_url

from src.apps.codex_usage_browser_types import (
    ApiCreditPayload,
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
    is_credit_only_usage_probe,
)


_T = TypeVar("_T")

# Time kept free after the optional API credit step so that the usage result is
# never lost to the session's collect timeout. The session clock starts before this
# process sees the command: worker recycle, spawn, bootstrap (15 s) and profile-lock
# retries are invisible here, so the margin has to cover them as well.
_API_CREDIT_STEP_MARGIN_SEC = 30.0
_API_CREDIT_POLL_INTERVAL_SEC = 0.5
# How long the sign-in window waits for the page to render on open and on each poll.
_API_CREDIT_LOGIN_OPEN_WAIT_SEC = 8.0
_API_CREDIT_LOGIN_POLL_WAIT_SEC = 3.0
# Signed in, billing page rendered, still no balance after this many reads in a row:
# waiting cannot help, so the window closes instead of holding the profile for 10 min.
_API_CREDIT_LOGIN_UNREADABLE_READS = 4
# Per address: a page the user must complete is left alone after this, while a later
# landing elsewhere still returns to the billing page.
_API_CREDIT_LOGIN_LANDING_RECOVERIES = 2


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
    # Compare observed landings without turning the history tab into a live page.
    if is_codex_usage_url(url):
        return canonicalize_codex_usage_url(url)
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
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config: PlaywrightSessionConfig = config
        self._log_sink: LogSink | None = log_sink
        self._starter: PlaywrightStarter = playwright_starter or _default_playwright_starter
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._clock: Callable[[], float] = clock or time.monotonic
        self._api_credit_next_due: float | None = None
        self._api_credit_unreadable_reads: int = 0
        self._api_credit_landing_recoveries: dict[str, int] = {}
        self._api_credit_new_worker_collect_passed: bool = False
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
        collect_started_at = self._clock()
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
                        return BrowserOperationResult(
                            probe=self._attach_api_credit(probe, collect_started_at)
                        )
                    if probe is not None:
                        blocks = probe.get("metricBlocks", [])
                        route = "legacy_usage" if is_legacy_usage_dialog_url(str(probe.get("url", ""))) else "usage" if is_codex_usage_url(str(probe.get("url", ""))) else "other"
                        self._log(
                            f"usage probe not ready route={route} blocks={len(blocks)} "
                            f"credit_blocks={sum(block.get('metric_key') == 'remaining_credit' for block in blocks)} "
                            f"text_present={bool(probe.get('mainText'))}"
                        )
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
                    self._login_entry_url,
                    timeout=self._config.navigation_timeout_ms,
                    wait_until="domcontentloaded",
                ),
            )
            probe = self._run_stage(
                "evaluate_probe",
                lambda: self._evaluate_probe_until_ready(
                    page,
                    stop_on_non_usage_landing=True,
                ),
            )
            if probe is not None and self._probe_is_authenticated(probe):
                self._close_context()
                _ = self._ensure_context(headless=True)
                self._set_status(BrowserState.HEADLESS_READY)
                return BrowserOperationResult(probe=probe)
            if probe is not None and not self._probe_is_login_pending(page, probe):
                probe = self._recover_post_login_landing(page, probe)
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
                lambda: self._evaluate_probe_until_ready(
                    page,
                    stop_on_non_usage_landing=True,
                ),
            )
            if probe is not None and self._probe_is_cloudflare(probe):
                return self._fail(
                    BrowserErrorCode.CLOUDFLARE_CHALLENGE.value,
                    state=BrowserState.HEADED_LOGIN,
                    login_window_open=True,
                )
            if probe is None or not self._probe_is_authenticated(probe):
                if probe is None or self._probe_is_login_pending(page, probe):
                    return self._fail(
                        BrowserErrorCode.LOGIN_REQUIRED.value,
                        state=BrowserState.HEADED_LOGIN,
                        login_window_open=True,
                    )
                probe = self._recover_post_login_landing(page, probe)
            if probe is None or not self._probe_is_authenticated(probe):
                return self._fail(
                    BrowserErrorCode.LOGIN_REQUIRED.value,
                    state=BrowserState.HEADED_LOGIN,
                    login_window_open=True,
                )
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

    def open_api_credit_login(self) -> BrowserOperationResult:
        """Show the OpenAI Platform billing page in a window so the user can sign in."""
        script = self._config.api_credit_probe_script
        url = self._config.api_credit_url
        if not url or not script:
            return self._fail(
                BrowserErrorCode.COLLECT_FAILED.value,
                "api credit page is not configured",
            )
        self._api_credit_unreadable_reads = 0
        self._api_credit_landing_recoveries = {}
        try:
            page = self._ensure_context(headless=False)
        except (OSError, RuntimeError) as exc:
            return self._fail(_classify_error(str(exc)), str(exc))
        except _playwright_error_type() as exc:
            return self._fail(_classify_error(str(exc)), str(exc))
        try:
            _ = self._run_stage(
                "api_credit_login_navigation",
                lambda: page.goto(
                    url,
                    timeout=self._config.navigation_timeout_ms,
                    wait_until="domcontentloaded",
                ),
            )
            reading = self._wait_for_api_credit(page, script, _API_CREDIT_LOGIN_OPEN_WAIT_SEC)
            return self._conclude_api_credit_login(reading)
        except (OSError, RuntimeError) as exc:
            return self._fail_api_credit_login_poll(exc)
        except _playwright_error_type() as exc:
            return self._fail_api_credit_login_poll(exc)

    def poll_api_credit_login(self) -> BrowserOperationResult:
        """Check the open sign-in window once; it closes itself after a balance is read."""
        page = self._page
        script = self._config.api_credit_probe_script
        if self._headless is not False or page is None or page.is_closed() or not script:
            self._close_context()
            return self._fail(BrowserErrorCode.LOGIN_WINDOW_CLOSED.value)
        try:
            reading = self._wait_for_api_credit(page, script, _API_CREDIT_LOGIN_POLL_WAIT_SEC)
            if (
                reading.status is ApiCreditStatus.UNAVAILABLE
                and reading.reason == "unexpected_page"
            ):
                reading = self._recover_api_credit_landing(page, script, reading)
            return self._conclude_api_credit_login(reading)
        except (OSError, RuntimeError) as exc:
            return self._fail_api_credit_login_poll(exc)
        except _playwright_error_type() as exc:
            return self._fail_api_credit_login_poll(exc)

    def _fail_api_credit_login_poll(self, exc: BaseException) -> BrowserOperationResult:
        error = _classify_error(str(exc), login_poll=True)
        if error == BrowserErrorCode.LOGIN_WINDOW_CLOSED.value:
            self._close_context()
        return self._fail(error, str(exc))

    def _conclude_api_credit_login(self, reading: ApiCreditReading) -> BrowserOperationResult:
        if (
            reading.status is ApiCreditStatus.UNAVAILABLE
            and reading.reason == "balance_not_found"
        ):
            self._api_credit_unreadable_reads += 1
            if self._api_credit_unreadable_reads >= _API_CREDIT_LOGIN_UNREADABLE_READS:
                reading = ApiCreditReading(
                    ApiCreditStatus.UNAVAILABLE,
                    reason=API_CREDIT_UNREADABLE_REASON,
                )
        else:
            self._api_credit_unreadable_reads = 0
        unreadable = reading.reason == API_CREDIT_UNREADABLE_REASON
        if reading.status is not ApiCreditStatus.OK and not unreadable:
            return self._fail(
                BrowserErrorCode.LOGIN_REQUIRED.value,
                state=BrowserState.HEADED_LOGIN,
                login_window_open=True,
            )
        self._close_context()
        _ = self._ensure_context(headless=True)
        self._set_status(BrowserState.HEADLESS_READY)
        # Recorded only once the headless window is back, so a failed relaunch
        # leaves the next usage collect free to read the balance again.
        interval = (
            self._config.api_credit_retry_interval_sec
            if unreadable
            else self._config.api_credit_success_interval_sec
        )
        self._api_credit_next_due = self._clock() + max(1.0, float(interval))
        probe: UsageProbePayload = {
            "url": API_CREDIT_BILLING_URL,
            "metricBlocks": [],
            "apiCredit": cast(ApiCreditPayload, reading.to_payload()),
        }
        return BrowserOperationResult(probe=probe)

    def _recover_api_credit_landing(
        self,
        page: PageLike,
        script: str,
        reading: ApiCreditReading,
    ) -> ApiCreditReading:
        """After sign-in the site may land elsewhere on the platform; go back to billing."""
        url = self._config.api_credit_url
        try:
            current_url = str(page.url)
        except (OSError, RuntimeError, _playwright_error_type()):
            return reading
        host = urlsplit(current_url).netloc.lower().split(":", 1)[0]
        returns = self._api_credit_landing_recoveries.get(current_url, 0)
        # Bounded: a platform page the user must complete (creating an organization,
        # accepting terms) would otherwise be reloaded away on every poll.
        if (
            not url
            or host != API_CREDIT_HOST
            or returns >= _API_CREDIT_LOGIN_LANDING_RECOVERIES
        ):
            return reading
        self._api_credit_landing_recoveries[current_url] = returns + 1
        try:
            _ = self._run_stage(
                "api_credit_login_navigation",
                lambda: page.goto(
                    url,
                    timeout=self._config.navigation_timeout_ms,
                    wait_until="domcontentloaded",
                ),
            )
        except (OSError, RuntimeError, _playwright_error_type()) as exc:
            lowered = str(exc).lower()
            if "closed" in lowered or "crashed" in lowered:
                raise
            # The site may be navigating by itself right after sign-in: look again later.
            self._log("api credit landing return failed")
            return reading
        return self._wait_for_api_credit(page, script, _API_CREDIT_LOGIN_POLL_WAIT_SEC)

    def _attach_api_credit(
        self,
        probe: UsageProbePayload,
        collect_started_at: float,
    ) -> UsageProbePayload:
        """Add the optional API credit reading; a failure here never touches the usage result."""
        if not self._probe_is_usage_ready(probe):
            return probe
        try:
            payload = self._collect_api_credit(collect_started_at)
        except Exception as exc:
            self._log(f"api credit step failed type={type(exc).__name__}")
            return probe
        if payload is None:
            return probe
        return {**probe, "apiCredit": payload}

    def _collect_api_credit(self, collect_started_at: float) -> ApiCreditPayload | None:
        config = self._config
        if not config.api_credit_url or not config.api_credit_probe_script or self._context is None:
            return None
        if config.api_credit_skip_new_worker_collect and not self._api_credit_new_worker_collect_passed:
            self._api_credit_new_worker_collect_passed = True
            return None
        now = self._clock()
        if self._api_credit_next_due is not None and now < self._api_credit_next_due:
            return None
        limit = min(float(config.command_timeout_sec), float(config.collect_timeout_sec))
        budget = min(
            float(config.api_credit_step_max_sec),
            limit - (now - collect_started_at) - _API_CREDIT_STEP_MARGIN_SEC,
        )
        if budget < float(config.api_credit_step_min_sec):
            self._log("api credit step skipped reason=budget")
            return None
        reading = self._read_api_credit(budget)
        if reading.status is ApiCreditStatus.OK:
            interval = config.api_credit_success_interval_sec
        elif reading.status is ApiCreditStatus.LOGIN_REQUIRED:
            interval = config.api_credit_login_required_interval_sec
        else:
            interval = config.api_credit_retry_interval_sec
        self._api_credit_next_due = self._clock() + max(1.0, float(interval))
        self._log(
            f"api credit step end status={reading.status.value} reason={reading.reason or '-'}"
        )
        return cast(ApiCreditPayload, reading.to_payload())

    def _read_api_credit(self, budget_sec: float) -> ApiCreditReading:
        """Read the billing page in a throwaway tab so the usage page stays untouched."""
        context = self._context
        url = self._config.api_credit_url
        script = self._config.api_credit_probe_script
        if context is None or not url:
            return ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="no_context")
        started_at = self._clock()
        page: PageLike | None = None
        try:
            page = context.new_page()
            tab = page
            navigation_ms = int(
                max(1_000.0, min(float(self._config.navigation_timeout_ms), budget_sec * 600.0))
            )
            _ = self._run_stage(
                "api_credit_navigation",
                lambda: tab.goto(url, timeout=navigation_ms, wait_until="domcontentloaded"),
            )
            wait_sec = max(1.0, budget_sec - (self._clock() - started_at))
            return self._wait_for_api_credit(page, script, wait_sec)
        except (DriverOperationError, OSError, RuntimeError, _playwright_error_type()) as exc:
            return ApiCreditReading(
                ApiCreditStatus.UNAVAILABLE,
                reason=_classify_error(str(exc)),
            )
        finally:
            if page is not None:
                try:
                    page.close()
                except (OSError, RuntimeError, _playwright_error_type()):
                    self._log("api credit page close failed")

    def _wait_for_api_credit(
        self,
        page: PageLike,
        script: str,
        wait_sec: float,
    ) -> ApiCreditReading:
        deadline = self._clock() + max(0.0, float(wait_sec))
        attempts = int(max(0.0, float(wait_sec)) / _API_CREDIT_POLL_INTERVAL_SEC) + 2
        reading = ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="loading")
        raw: object = None
        for attempt in range(attempts):
            try:
                raw = page.evaluate(script)
            except (OSError, RuntimeError, _playwright_error_type()) as exc:
                lowered = str(exc).lower()
                if "closed" in lowered or "crashed" in lowered:
                    raise
                # A navigation in flight destroys the page context; look again.
                raw = None
            reading = parse_api_credit_probe(raw)
            if reading.is_final or self._clock() >= deadline or attempt + 1 >= attempts:
                break
            self._sleep(_API_CREDIT_POLL_INTERVAL_SEC)
        if reading.status is ApiCreditStatus.UNAVAILABLE:
            self._log(
                f"api credit page not parsed reason={reading.reason} "
                f"{describe_api_credit_shape(raw)}"
            )
        return reading

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
            "--test-type",
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

    @property
    def _login_entry_url(self) -> str:
        return str(self._config.login_url or self._config.usage_url)

    def _probe_is_authenticated(self, probe: UsageProbePayload) -> bool:
        return self._probe_is_usage_ready(probe)

    def _probe_is_usage_ready(self, probe: UsageProbePayload) -> bool:
        if _canonical_usage_url(str(probe.get("url", ""))) != _canonical_usage_url(
            self._config.usage_url
        ):
            return False
        return any(
            str(block.get("metric_key", "")) != "remaining_credit"
            for block in probe.get("metricBlocks", [])
        ) or is_credit_only_usage_probe(probe)

    def _probe_is_login_pending(
        self,
        page: PageLike,
        probe: UsageProbePayload,
    ) -> bool:
        urls: list[str] = [str(probe.get("url", ""))]
        try:
            urls.append(str(page.url))
        except (OSError, RuntimeError, _playwright_error_type()):
            pass
        for url in urls:
            lowered = url.lower()
            if any(token in lowered for token in ("/login", "/auth", "signin", "sign-in")):
                return True
            host = urlsplit(url).netloc.lower()
            if host.endswith(("accounts.google.com", "auth.openai.com")):
                return True
        combined = " ".join(
            str(probe.get(key, "")) for key in ("title", "mainText")
        ).lower()
        return any(
            marker in combined
            for marker in ("log in", "sign in", "continue with google", "로그인")
        )

    def _recover_post_login_landing(
        self,
        page: PageLike,
        probe: UsageProbePayload | None = None,
    ) -> UsageProbePayload | None:
        try:
            current_url = str(page.url)
        except (OSError, RuntimeError, _playwright_error_type()):
            return None
        lowered = current_url.lower()
        if any(token in lowered for token in ("/login", "/auth", "signin", "sign-in")):
            return None
        host = urlsplit(current_url).netloc.lower()
        if host.endswith(("accounts.google.com", "auth.openai.com")):
            return None
        if probe is not None and self._probe_is_login_pending(page, probe):
            return None
        if _canonical_usage_url(current_url) == _canonical_usage_url(self._config.usage_url):
            return None
        try:
            self._run_stage(
                "navigation",
                lambda: page.goto(
                    self._config.usage_url,
                    timeout=self._config.navigation_timeout_ms,
                    wait_until="domcontentloaded",
                ),
            )
            return self._run_stage(
                "evaluate_probe",
                lambda: self._evaluate_probe_until_ready(page),
            )
        except (OSError, RuntimeError, _playwright_error_type()):
            self._log("post-login landing recovery navigation failed")
            return None

    def _evaluate_probe_until_ready(
        self,
        page: PageLike,
        *,
        stop_on_non_usage_landing: bool = False,
    ) -> UsageProbePayload | None:
        last_probe: UsageProbePayload | None = None
        for attempt in range(21):
            probe = parse_usage_probe(page.evaluate(self._config.probe_script))
            if probe is None:
                raise DriverOperationError("usage probe did not return an object")
            last_probe = probe
            if self._probe_is_terminal(
                probe,
                stop_on_non_usage_landing=stop_on_non_usage_landing,
            ):
                return probe
            if attempt < 20:
                self._sleep(0.25)
        return last_probe

    def _probe_is_terminal(
        self,
        probe: UsageProbePayload,
        *,
        stop_on_non_usage_landing: bool = False,
    ) -> bool:
        # Return a completed credit view even if identity lookup failed. The
        # monitor rejects missing identity without 21 slow auth retries here.
        if is_credit_only_usage_probe(probe, require_identity=False):
            return True
        if any(
            str(block.get("metric_key", "")) != "remaining_credit"
            for block in probe.get("metricBlocks", [])
        ):
            return True
        if self._probe_is_cloudflare(probe):
            return True
        url = str(probe.get("url", "")).lower()
        if stop_on_non_usage_landing and url and not _canonical_usage_url(url) == _canonical_usage_url(
            self._config.usage_url
        ):
            return True
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
