from __future__ import annotations

import unittest
from typing import TypedDict, final

from src.apps.codex_usage_browser_types import (
    BrowserState,
    ChromiumProtocol,
    PlaywrightProtocol,
    PlaywrightSessionConfig,
    UsageProbePayload,
)
from src.apps.codex_usage_playwright_driver import CodexUsagePlaywrightDriver


USAGE_URL = "https://chatgpt.com/codex/settings/usage"
PROBE: UsageProbePayload = {
    "url": USAGE_URL,
    "mainText": "usage limit",
    "metricBlocks": [{"metric_key": "weekly_limit"}],
}


class LaunchCall(TypedDict):
    profile_dir: str
    channel: str
    headless: bool
    chromium_sandbox: bool
    args: list[str]
    user_agent: str | None
    timeout: float


@final
class FakePage:
    """Mutable Playwright page fake used to model navigation failures."""

    def __init__(
        self,
        *,
        url: str = USAGE_URL,
        user_agent: str = "Chrome/140",
        probe: UsageProbePayload | None = None,
        failures: int = 0,
    ) -> None:
        self.url: str = url
        self.user_agent: str = user_agent
        self.probe: UsageProbePayload = probe or PROBE
        self.failures: int = failures
        self.closed: bool = False
        self.calls: list[tuple[str, str]] = []

    def reload(self, *, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        self._navigate("reload", self.url)

    def goto(self, url: str, *, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        self._navigate("goto", url)

    def _navigate(self, method: str, url: str) -> None:
        self.calls.append((method, url))
        if self.failures:
            self.failures -= 1
            raise OSError("page navigation failed")
        self.url = url

    def evaluate(self, expression: str) -> str | UsageProbePayload:
        if expression == "() => navigator.userAgent":
            return self.user_agent
        return self.probe

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


@final
class FakeContext:
    """Mutable persistent-context fake with deterministic page allocation."""

    def __init__(self, pages: list[FakePage], event_log: list[str] | None = None) -> None:
        self.pages: list[FakePage] = pages[:1]
        self._remaining_pages: list[FakePage] = pages[1:]
        self.closed: bool = False
        self.event_log: list[str] | None = event_log

    def new_page(self) -> FakePage:
        page = self._remaining_pages.pop(0) if self._remaining_pages else FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True
        if self.event_log is not None:
            self.event_log.append("close")


@final
class FakeChromium:
    """Mutable Chromium fake that records launch arguments and outcomes."""

    def __init__(self, outcomes: list[FakeContext | OSError], event_log: list[str] | None = None) -> None:
        self.outcomes: list[FakeContext | OSError] = outcomes
        self.calls: list[LaunchCall] = []
        self.event_log: list[str] | None = event_log

    def launch_persistent_context(
        self,
        user_data_dir: str,
        *,
        channel: str,
        headless: bool,
        chromium_sandbox: bool,
        args: list[str],
        user_agent: str | None,
        timeout: float,
    ) -> FakeContext:
        self.calls.append(
            LaunchCall(
                profile_dir=user_data_dir,
                channel=channel,
                headless=headless,
                chromium_sandbox=chromium_sandbox,
                args=args,
                user_agent=user_agent,
                timeout=timeout,
            )
        )
        if self.event_log is not None:
            self.event_log.append(f"launch:{headless}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, OSError):
            raise outcome
        return outcome


@final
class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self._chromium: FakeChromium = chromium
        self.stop_count: int = 0

    @property
    def chromium(self) -> ChromiumProtocol:
        return self._chromium

    def stop(self) -> None:
        self.stop_count += 1


@final
class FakeStarter:
    def __init__(self, chromium: FakeChromium) -> None:
        self.playwright: FakePlaywright = FakePlaywright(chromium)
        self.start_count: int = 0

    def __call__(self) -> PlaywrightProtocol:
        self.start_count += 1
        return self.playwright


def make_driver(
    outcomes: list[FakeContext | OSError],
    *,
    event_log: list[str] | None = None,
) -> tuple[CodexUsagePlaywrightDriver, FakeChromium, FakeStarter]:
    chromium = FakeChromium(outcomes, event_log)
    starter = FakeStarter(chromium)
    config = PlaywrightSessionConfig("profile", USAGE_URL, "probe()")
    driver = CodexUsagePlaywrightDriver(config, playwright_starter=starter)
    _ = driver.start()
    return driver, chromium, starter


class CodexUsagePlaywrightDriverTest(unittest.TestCase):
    def test_start_classifies_missing_playwright_runtime(self) -> None:
        def unavailable_starter() -> PlaywrightProtocol:
            raise ImportError("No module named 'playwright'")

        config = PlaywrightSessionConfig("profile", USAGE_URL, "probe()")
        driver = CodexUsagePlaywrightDriver(config, playwright_starter=unavailable_starter)

        result = driver.start()

        self.assertEqual(result.error, "playwright_unavailable")
        self.assertEqual(driver.get_runtime_status().state, BrowserState.FAILED)

    def test_collect_normalizes_headless_user_agent_once_and_reuses_context(self) -> None:
        first = FakeContext([FakePage(user_agent="HeadlessChrome/140")])
        ready_page = FakePage(user_agent="Chrome/140")
        driver, chromium, _controller = make_driver([first, FakeContext([ready_page])])

        first_result = driver.collect()
        second_result = driver.collect()

        self.assertEqual(first_result.error, None)
        self.assertEqual(second_result.probe, PROBE)
        self.assertEqual(len(chromium.calls), 2)
        self.assertTrue(all(call["channel"] == "chrome" for call in chromium.calls))
        self.assertEqual(chromium.calls[1]["user_agent"], "Chrome/140")
        self.assertEqual([name for name, _url in ready_page.calls], ["reload", "reload"])

    def test_collect_retries_page_then_context_once(self) -> None:
        first_page = FakePage(url="https://example.com", failures=1)
        second_page = FakePage(failures=1)
        third_page = FakePage(failures=1)
        recovered_page = FakePage()
        first_context = FakeContext([first_page, second_page, third_page])
        driver, chromium, _controller = make_driver([first_context, FakeContext([recovered_page])])

        result = driver.collect()

        self.assertEqual(result.probe, PROBE)
        self.assertEqual(first_page.calls[0], ("goto", USAGE_URL))
        self.assertTrue(first_context.closed)
        self.assertEqual(len(chromium.calls), 2)

    def test_collect_classifies_profile_lock_and_missing_chrome_channel(self) -> None:
        cases = (
            ("ProcessSingleton: profile is already in use", "profile_in_use", BrowserState.PROFILE_IN_USE),
            ("Chromium distribution 'chrome' is not found", "browser_channel_unavailable", BrowserState.FAILED),
        )
        for message, error, state in cases:
            with self.subTest(error=error):
                driver, _chromium, _controller = make_driver([OSError(message)])

                result = driver.collect()

                self.assertEqual(result.error, error)
                self.assertEqual(driver.get_runtime_status().state, state)

    def test_login_mode_switch_closes_old_context_before_launch_and_uses_usage_url(self) -> None:
        events: list[str] = []
        headless = FakeContext([FakePage()], events)
        login_page = FakePage(url="about:blank", probe={"url": USAGE_URL, "mainText": "Log in", "metricBlocks": []})
        headed = FakeContext([login_page], events)
        driver, _chromium, _controller = make_driver([headless, headed], event_log=events)
        _ = driver.collect()

        result = driver.open_login()

        self.assertEqual(result.error, "login_required")
        self.assertEqual(events[-2:], ["close", "launch:False"])
        self.assertEqual(login_page.calls, [("goto", USAGE_URL)])
        self.assertEqual(driver.get_runtime_status().state, BrowserState.HEADED_LOGIN)

    def test_poll_login_reports_closed_window_and_success_returns_to_headless(self) -> None:
        login_probe: UsageProbePayload = {
            "url": USAGE_URL,
            "mainText": "Log in",
            "metricBlocks": [],
        }
        closed_page = FakePage(probe=login_probe)
        closed_context = FakeContext([closed_page])
        driver, _chromium, _controller = make_driver([closed_context])
        _ = driver.open_login()
        closed_page.closed = True

        closed_result = driver.poll_login()

        self.assertEqual(closed_result.error, "login_window_closed")
        self.assertTrue(closed_context.closed)

        login_page = FakePage(probe=login_probe)
        headed = FakeContext([login_page])
        headless = FakeContext([FakePage()])
        driver, _chromium, _controller = make_driver([headed, headless])
        _ = driver.open_login()
        login_page.probe = PROBE
        success = driver.poll_login()

        self.assertEqual(success.probe, PROBE)
        self.assertTrue(headed.closed)
        self.assertEqual(driver.get_runtime_status().state, BrowserState.HEADLESS_READY)

    def test_poll_login_keeps_cloudflare_window_open(self) -> None:
        challenge: UsageProbePayload = {
            "url": USAGE_URL,
            "title": "Just a moment...",
            "mainText": "Cloudflare Verify you are human",
            "metricBlocks": [],
        }
        page = FakePage(probe=challenge)
        driver, _chromium, _controller = make_driver([FakeContext([page])])

        open_result = driver.open_login()
        poll_result = driver.poll_login()

        self.assertEqual(open_result.error, "cloudflare_challenge")
        self.assertEqual(poll_result.error, "cloudflare_challenge")
        status = driver.get_runtime_status()
        self.assertEqual(status.state, BrowserState.HEADED_LOGIN)
        self.assertTrue(status.login_window_open)

    def test_shutdown_stops_playwright_exactly_once(self) -> None:
        driver, _chromium, controller = make_driver([FakeContext([FakePage()])])
        _ = driver.collect()

        driver.shutdown()
        driver.shutdown()

        self.assertEqual(controller.start_count, 1)
        self.assertEqual(controller.playwright.stop_count, 1)
        self.assertEqual(driver.get_runtime_status().state, BrowserState.STOPPED)


if __name__ == "__main__":
    _ = unittest.main()
