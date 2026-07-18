from __future__ import annotations

from collections.abc import Callable
import os
import time

from src.apps.codex_usage_browser_types import (
    BrowserErrorCode,
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    ContextLike,
    LogSink,
    PageLike,
    PlaywrightLike,
    PlaywrightSessionConfig,
    PlaywrightStarter,
    UsageProbePayload,
    parse_usage_probe,
)


def _default_playwright_starter() -> PlaywrightLike:
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def classify_cursor_browser_error(message: object) -> str:
    lowered = str(message or "").strip().lower()
    if any(token in lowered for token in ("429", "too many requests", "rate limit")):
        return "rate_limited"
    if any(token in lowered for token in ("page crashed", "renderer crash", "page crash")):
        return BrowserErrorCode.RENDERER_CRASHED.value
    if any(
        token in lowered
        for token in (
            "target page has been closed",
            "target closed",
            "browser has been closed",
            "connection closed",
            "transport closed",
        )
    ):
        return BrowserErrorCode.TRANSPORT_CLOSED.value
    if any(token in lowered for token in ("timeout", "timed out", "deadline exceeded")):
        return BrowserErrorCode.COMMAND_TIMEOUT.value
    if any(
        token in lowered
        for token in (
            "user data directory is already in use",
            "profile appears to be in use",
            "processsingleton",
        )
    ):
        return BrowserErrorCode.PROFILE_IN_USE.value
    return BrowserErrorCode.COLLECT_FAILED.value


class CursorUsagePlaywrightDriver:
    """Playwright driver limited to an app-owned Cursor dashboard profile."""

    def __init__(
        self,
        config: PlaywrightSessionConfig,
        log_sink: LogSink | None = None,
        playwright_starter: PlaywrightStarter | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._log_sink = log_sink
        self._starter = playwright_starter or _default_playwright_starter
        self._sleep = sleep or time.sleep
        self._playwright: PlaywrightLike | None = None
        self._context: ContextLike | None = None
        self._page: PageLike | None = None
        self._headless: bool | None = None
        self._page_success_count = 0
        self._page_crashed = False
        self._shutdown = False
        self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def start(self) -> BrowserOperationResult:
        if self._playwright is not None:
            return BrowserOperationResult()
        if self._shutdown:
            return BrowserOperationResult(error=BrowserErrorCode.COLLECT_FAILED.value)
        self._set_status(BrowserState.STARTING)
        try:
            self._playwright = self._starter()
        except Exception as exc:
            return self._fail(BrowserErrorCode.PLAYWRIGHT_UNAVAILABLE.value, str(exc))
        self._set_status(BrowserState.STOPPED)
        return BrowserOperationResult()

    def collect(self) -> BrowserOperationResult:
        try:
            page = self._ensure_context(headless=True)
            self._navigate(page)
            probe = self._evaluate_probe_until_terminal(page)
            if self._page_crashed:
                raise RuntimeError("Page crashed")
            if probe is None:
                raise RuntimeError("usage probe did not return an object")
            if self._is_cloudflare(probe):
                return self._fail(BrowserErrorCode.CLOUDFLARE_CHALLENGE.value)
            if self._is_login_required(probe):
                return self._fail(BrowserErrorCode.LOGIN_REQUIRED.value)
            if self._is_rate_limited(probe):
                return self._fail("rate_limited")
            if not self._has_summary(probe):
                return self._fail(BrowserErrorCode.COLLECT_FAILED.value)
            self._page_success_count += 1
            self._set_status(BrowserState.HEADLESS_READY)
            return BrowserOperationResult(probe=probe)
        except Exception as exc:
            error = classify_cursor_browser_error(exc)
            if error in {
                BrowserErrorCode.RENDERER_CRASHED.value,
                BrowserErrorCode.TRANSPORT_CLOSED.value,
            }:
                self._close_context()
            return self._fail(error, str(exc))

    def open_login(self) -> BrowserOperationResult:
        try:
            page = self._ensure_context(headless=False)
            page.goto(
                self._config.usage_url,
                timeout=self._config.navigation_timeout_ms,
                wait_until="domcontentloaded",
            )
            probe = self._evaluate_probe_until_terminal(page)
            if probe is not None and self._has_summary(probe):
                self._close_context()
                self._set_status(BrowserState.STOPPED)
                return BrowserOperationResult(probe=probe)
            if probe is not None and self._is_cloudflare(probe):
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
        except Exception as exc:
            return self._fail(classify_cursor_browser_error(exc), str(exc))

    def poll_login(self) -> BrowserOperationResult:
        page = self._page
        if self._headless is not False or page is None or page.is_closed():
            self._close_context()
            return self._fail(BrowserErrorCode.LOGIN_WINDOW_CLOSED.value)
        try:
            probe = self._evaluate_probe_until_terminal(page)
            if probe is not None and self._is_cloudflare(probe):
                return self._fail(
                    BrowserErrorCode.CLOUDFLARE_CHALLENGE.value,
                    state=BrowserState.HEADED_LOGIN,
                    login_window_open=True,
                )
            if probe is not None and self._is_rate_limited(probe):
                return self._fail(
                    "rate_limited",
                    state=BrowserState.HEADED_LOGIN,
                    login_window_open=True,
                )
            if probe is None or not self._has_summary(probe):
                return self._fail(
                    BrowserErrorCode.LOGIN_REQUIRED.value,
                    state=BrowserState.HEADED_LOGIN,
                    login_window_open=True,
                )
            self._close_context()
            self._set_status(BrowserState.STOPPED)
            return BrowserOperationResult(probe=probe)
        except Exception as exc:
            error = classify_cursor_browser_error(exc)
            if error in {
                BrowserErrorCode.RENDERER_CRASHED.value,
                BrowserErrorCode.TRANSPORT_CLOSED.value,
            }:
                return self._fail(error, str(exc))
            return self._fail(
                error,
                str(exc),
                state=BrowserState.HEADED_LOGIN,
                login_window_open=True,
            )

    def close_session(self) -> None:
        self._close_context()
        self._set_status(BrowserState.STOPPED)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._close_context()
        playwright = self._playwright
        self._playwright = None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                self._log("cursor playwright stop failed")
        self._set_status(BrowserState.STOPPED)

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return self._status

    def import_session_cookies(self, _cookies: list[dict[str, object]]) -> None:
        """Deliberately ignore external cookie material."""

    def export_session_cookies(self) -> list[dict[str, object]]:
        """Never extract cookies from the app-owned persistent profile."""
        return []

    def _ensure_context(self, *, headless: bool) -> PageLike:
        if self._playwright is None:
            result = self.start()
            if result.error:
                raise RuntimeError(result.error)
        if (
            self._context is not None
            and self._headless is headless
            and self._page is not None
            and not self._page.is_closed()
        ):
            if headless and self._page_success_count >= max(
                1, int(self._config.page_recycle_success_count)
            ):
                return self._replace_page()
            return self._page
        self._close_context()
        if self._playwright is None:
            raise RuntimeError("playwright unavailable")
        os.makedirs(self._config.profile_dir, exist_ok=True)
        self._set_status(BrowserState.STARTING)
        self._context = self._playwright.chromium.launch_persistent_context(
            self._config.profile_dir,
            channel="chrome",
            headless=headless,
            chromium_sandbox=True,
            args=["--disable-extensions", "--disable-notifications"],
            timeout=float(self._config.navigation_timeout_ms),
        )
        self._headless = headless
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else self._context.new_page()
        )
        self._register_page(self._page)
        return self._page

    def _replace_page(self) -> PageLike:
        if self._context is None:
            raise RuntimeError("browser context is unavailable")
        if self._page is not None and not self._page.is_closed():
            self._page.close()
        self._page = self._context.new_page()
        self._register_page(self._page)
        return self._page

    def _register_page(self, page: PageLike) -> None:
        self._page_success_count = 0
        self._page_crashed = False
        try:
            page.on("crash", self._mark_page_crashed)
        except Exception:
            pass

    def _mark_page_crashed(self, *_args: object) -> None:
        self._page_crashed = True

    def _navigate(self, page: PageLike) -> None:
        current = str(getattr(page, "url", "")).rstrip("/").lower()
        target = self._config.usage_url.rstrip("/").lower()
        if current == target:
            page.reload(
                timeout=self._config.navigation_timeout_ms,
                wait_until="domcontentloaded",
            )
        else:
            page.goto(
                self._config.usage_url,
                timeout=self._config.navigation_timeout_ms,
                wait_until="domcontentloaded",
            )

    def _evaluate_probe_until_terminal(self, page: PageLike) -> UsageProbePayload | None:
        last_probe: UsageProbePayload | None = None
        for attempt in range(21):
            probe = parse_usage_probe(page.evaluate(self._config.probe_script))
            if probe is None:
                return None
            last_probe = probe
            if (
                self._has_summary(probe)
                or self._is_login_required(probe)
                or self._is_cloudflare(probe)
                or self._is_rate_limited(probe)
            ):
                return probe
            if attempt < 20:
                self._sleep(0.25)
        return last_probe

    @staticmethod
    def _has_summary(probe: UsageProbePayload) -> bool:
        return any(
            str(block.get("metric_key", "")) == "cursor_account_summary"
            for block in probe.get("metricBlocks", [])
        )

    @staticmethod
    def _is_login_required(probe: UsageProbePayload) -> bool:
        url = str(probe.get("url", "")).lower()
        combined = " ".join(
            str(probe.get(key, "")) for key in ("title", "mainText")
        ).lower()
        return any(token in url for token in ("/login", "/signin", "/auth")) or any(
            marker in combined
            for marker in ("sign in", "log in", "continue with google", "로그인")
        )

    @staticmethod
    def _is_cloudflare(probe: UsageProbePayload) -> bool:
        combined = " ".join(
            str(probe.get(key, "")) for key in ("title", "mainText")
        ).lower()
        return any(
            marker in combined
            for marker in (
                "cloudflare",
                "verify you are human",
                "checking your browser",
                "challenge-platform",
            )
        )

    @staticmethod
    def _is_rate_limited(probe: UsageProbePayload) -> bool:
        combined = " ".join(
            str(probe.get(key, "")) for key in ("title", "mainText")
        ).lower()
        return any(
            marker in combined
            for marker in ("429", "too many requests", "rate limit")
        )

    def _close_context(self) -> None:
        context = self._context
        self._context = None
        self._page = None
        self._headless = None
        self._page_success_count = 0
        self._page_crashed = False
        if context is None:
            return
        try:
            context.close()
        except Exception:
            self._log("cursor browser context close failed")

    def _set_status(
        self,
        state: BrowserState,
        *,
        login_window_open: bool = False,
        last_error: str = "",
    ) -> None:
        self._status = BrowserRuntimeStatus(
            state,
            bool(login_window_open),
            str(last_error or ""),
        )

    def _fail(
        self,
        error: str,
        detail: str = "",
        *,
        state: BrowserState = BrowserState.FAILED,
        login_window_open: bool = False,
    ) -> BrowserOperationResult:
        self._set_status(
            state,
            login_window_open=login_window_open,
            last_error=error,
        )
        if detail:
            self._log(f"cursor browser operation failed error={error} detail={detail}")
        return BrowserOperationResult(error=error)

    def _log(self, message: str) -> None:
        if self._log_sink is not None:
            self._log_sink(str(message))
