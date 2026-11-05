from __future__ import annotations

import threading
import unittest
from collections.abc import Callable
from multiprocessing import Pipe
from typing import Any
from unittest.mock import patch

from src.apps.codex_api_credit import API_CREDIT_BILLING_URL as BILLING
from src.apps.codex_usage_browser_types import (
    BrowserErrorCode,
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    PlaywrightSessionConfig,
)
from src.apps.codex_usage_playwright_driver import CodexUsagePlaywrightDriver
from src.apps.codex_usage_playwright_worker import run_playwright_worker
from tests.unit.test_codex_usage_playwright_driver import (
    LOGIN_URL,
    PROBE,
    USAGE_URL,
    FakeChromium,
    FakeContext,
    FakePage,
    FakeStarter,
)


PLATFORM_LOGIN = "https://platform.openai.com/login?next=%2Fsettings%2Forganization%2Fbilling%2Foverview"
PLATFORM_HOME = "https://platform.openai.com/"


def ok_probe(amount: str) -> dict[str, str]:
    return {"url": BILLING, "title": "Billing", "text": f"Billing\nCredit balance\n{amount}\nAdd to credit balance"}


def login_probe() -> dict[str, str]:
    return {"url": PLATFORM_LOGIN, "title": "OpenAI Platform", "text": "Sign up or login with an OpenAI account"}


def home_probe() -> dict[str, str]:
    return {"url": PLATFORM_HOME, "title": "OpenAI Platform", "text": "Home\nQuickstart"}


class FakeClock:
    """Deterministic time: sleeping is the only thing that moves it."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class BillingPage:
    """Page fake whose probe follows the address it was sent to or redirected to."""

    def __init__(
        self,
        routes: dict[str, dict[str, str]],
        *,
        redirects: dict[str, str] | None = None,
        goto_error: Exception | None = None,
        default: object = None,
    ) -> None:
        self.url = "about:blank"
        self.routes = routes
        self.redirects = dict(redirects or {})
        self.goto_error = goto_error
        self.default = default
        self.closed = False
        self.calls: list[tuple[str, str]] = []
        self.evaluations = 0

    def reload(self, *, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        self.calls.append(("reload", self.url))

    def goto(self, url: str, *, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        self.calls.append(("goto", url))
        if self.goto_error is not None:
            raise self.goto_error
        self.url = self.redirects.get(url, url)

    def evaluate(self, expression: str) -> Any:
        if expression == "() => navigator.userAgent":
            return "Chrome/140"
        self.evaluations += 1
        return self.routes.get(self.url, self.default)

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def on(self, event: str, handler: Callable[[], None]) -> None:
        _ = event, handler


def api_config(**overrides: Any) -> PlaywrightSessionConfig:
    return PlaywrightSessionConfig(
        "profile",
        USAGE_URL,
        "probe()",
        login_url=LOGIN_URL,
        api_credit_url=BILLING,
        api_credit_probe_script="api_probe()",
        **overrides,
    )


def make_driver(
    outcomes: list[FakeContext | OSError],
    *,
    config: PlaywrightSessionConfig | None = None,
    clock: FakeClock | None = None,
) -> tuple[CodexUsagePlaywrightDriver, FakeChromium, FakeClock, list[str]]:
    clock = clock or FakeClock()
    logs: list[str] = []
    chromium = FakeChromium(outcomes)
    driver = CodexUsagePlaywrightDriver(
        config or api_config(),
        log_sink=logs.append,
        playwright_starter=FakeStarter(chromium),
        sleep=clock.sleep,
        clock=clock,
    )
    _ = driver.start()
    return driver, chromium, clock, logs


class ApiCreditCollectTest(unittest.TestCase):
    def test_collect_reads_the_balance_in_a_separate_tab(self) -> None:
        usage = FakePage()
        billing = BillingPage({BILLING: ok_probe("$12.34")})
        driver, _chromium, _clock, _logs = make_driver([FakeContext([usage, billing])])

        result = driver.collect()

        self.assertIsNone(result.error)
        assert result.probe is not None
        self.assertEqual(result.probe["apiCredit"], {"status": "ok", "balance": "$12.34"})
        self.assertEqual(result.probe["metricBlocks"], PROBE["metricBlocks"])
        self.assertEqual(usage.calls, [("reload", USAGE_URL)])
        self.assertEqual(billing.calls, [("goto", BILLING)])
        self.assertTrue(billing.closed)
        self.assertFalse(usage.closed)

    def test_login_redirect_is_reported_without_touching_the_usage_result(self) -> None:
        billing = BillingPage(
            {PLATFORM_LOGIN: login_probe()}, redirects={BILLING: PLATFORM_LOGIN}
        )
        driver, _chromium, _clock, _logs = make_driver([FakeContext([FakePage(), billing])])

        result = driver.collect()

        assert result.probe is not None
        self.assertEqual(
            result.probe["apiCredit"],
            {"status": "login_required", "reason": "login_page"},
        )
        self.assertEqual(driver.get_runtime_status().state, BrowserState.HEADLESS_READY)

    def test_billing_failure_never_fails_the_usage_collect(self) -> None:
        billing = BillingPage({}, goto_error=OSError("net::ERR_CONNECTION_RESET"))
        driver, chromium, _clock, _logs = make_driver([FakeContext([FakePage(), billing])])

        result = driver.collect()

        self.assertIsNone(result.error)
        assert result.probe is not None
        self.assertEqual(result.probe["metricBlocks"], PROBE["metricBlocks"])
        self.assertEqual(result.probe["apiCredit"]["status"], "unavailable")
        self.assertTrue(billing.closed)
        self.assertEqual(len(chromium.calls), 1, "no context recovery for an optional step")
        self.assertEqual(driver.get_runtime_status().state, BrowserState.HEADLESS_READY)

    def test_unexpected_exception_in_the_step_is_contained_and_logged(self) -> None:
        context = FakeContext([FakePage()])

        def explode() -> Any:
            raise ValueError("boom")

        context.new_page = explode  # type: ignore[method-assign]
        driver, _chromium, _clock, logs = make_driver([context])

        result = driver.collect()

        self.assertIsNone(result.error)
        assert result.probe is not None
        self.assertNotIn("apiCredit", result.probe)
        self.assertIn("api credit step failed type=ValueError", logs)

    def test_balance_is_refreshed_on_its_own_slower_cadence(self) -> None:
        first = BillingPage({BILLING: ok_probe("$12.34")})
        second = BillingPage({BILLING: ok_probe("$11.00")})
        driver, _chromium, clock, _logs = make_driver(
            [FakeContext([FakePage(), first, second])]
        )

        one = driver.collect().probe
        clock.now += 599
        two = driver.collect().probe
        clock.now += 2
        three = driver.collect().probe

        assert one is not None and two is not None and three is not None
        self.assertEqual(one["apiCredit"]["balance"], "$12.34")
        self.assertNotIn("apiCredit", two)
        self.assertEqual(three["apiCredit"]["balance"], "$11.00")
        self.assertEqual(second.calls, [("goto", BILLING)])

    def test_failed_reads_retry_sooner_and_login_waits_longest(self) -> None:
        for page, wait_before_retry in (
            (BillingPage({PLATFORM_HOME: home_probe()}, redirects={BILLING: PLATFORM_HOME}), 300),
            (BillingPage({PLATFORM_LOGIN: login_probe()}, redirects={BILLING: PLATFORM_LOGIN}), 1_800),
        ):
            with self.subTest(wait_before_retry=wait_before_retry):
                retry = BillingPage({BILLING: ok_probe("$1.00")})
                driver, _chromium, clock, _logs = make_driver(
                    [FakeContext([FakePage(), page, retry])]
                )

                _ = driver.collect()
                finished_at = clock.now
                clock.now = finished_at + wait_before_retry - 1
                early = driver.collect().probe
                clock.now = finished_at + wait_before_retry + 1
                late = driver.collect().probe

                assert early is not None and late is not None
                self.assertNotIn("apiCredit", early)
                self.assertEqual(late["apiCredit"]["balance"], "$1.00")

    def test_step_is_skipped_when_the_collect_budget_is_nearly_spent(self) -> None:
        billing = BillingPage({BILLING: ok_probe("$12.34")})
        config = api_config(command_timeout_sec=12.0, collect_timeout_sec=12.0)
        driver, _chromium, _clock, logs = make_driver(
            [FakeContext([FakePage(), billing])], config=config
        )

        result = driver.collect()

        assert result.probe is not None
        self.assertNotIn("apiCredit", result.probe)
        self.assertEqual(billing.calls, [])
        self.assertIn("api credit step skipped reason=budget", logs)

    def _slow_usage_driver(
        self, usage_seconds: float, billing: BillingPage
    ) -> tuple[CodexUsagePlaywrightDriver, FakeClock, list[str]]:
        clock = FakeClock()
        usage = FakePage()
        original_evaluate = usage.evaluate

        def slow_evaluate(expression: str) -> Any:
            if expression != "() => navigator.userAgent":
                clock.sleep(usage_seconds)
            return original_evaluate(expression)

        usage.evaluate = slow_evaluate  # type: ignore[method-assign]
        driver, _chromium, _clock, logs = make_driver(
            [FakeContext([usage, billing])],
            config=api_config(command_timeout_sec=195.0),
            clock=clock,
        )
        return driver, clock, logs

    def test_time_already_spent_on_usage_skips_the_step(self) -> None:
        billing = BillingPage({BILLING: ok_probe("$12.34")})
        driver, _clock, logs = self._slow_usage_driver(55.0, billing)

        result = driver.collect()

        assert result.probe is not None
        self.assertNotIn("apiCredit", result.probe)
        self.assertEqual(billing.calls, [])
        self.assertIn("api credit step skipped reason=budget", logs)

    def test_time_already_spent_on_usage_shortens_the_step(self) -> None:
        billing = BillingPage(
            {PLATFORM_HOME: home_probe()}, redirects={BILLING: PLATFORM_HOME}
        )
        driver, clock, _logs = self._slow_usage_driver(50.0, billing)

        started_at = clock.now
        result = driver.collect()

        assert result.probe is not None
        self.assertEqual(result.probe["apiCredit"]["reason"], "unexpected_page")
        # Limit 90 s, margin 30 s, 50 s spent: only 10 s are left of the 15 s step.
        self.assertLessEqual(clock.now - started_at, 60.5)

    def test_step_never_exceeds_its_maximum_when_time_is_plentiful(self) -> None:
        billing = BillingPage(
            {PLATFORM_HOME: home_probe()}, redirects={BILLING: PLATFORM_HOME}
        )
        driver, _chromium, clock, _logs = make_driver(
            [FakeContext([FakePage(), billing])],
            config=api_config(command_timeout_sec=195.0),
        )

        started_at = clock.now
        _ = driver.collect()

        self.assertGreaterEqual(clock.now - started_at, 14.0)
        self.assertLessEqual(clock.now - started_at, 15.5)

    def test_new_worker_reads_the_balance_only_from_its_second_collect(self) -> None:
        billing = BillingPage({BILLING: ok_probe("$12.34")})
        driver, _chromium, _clock, _logs = make_driver(
            [FakeContext([FakePage(), billing])],
            config=api_config(api_credit_skip_new_worker_collect=True),
        )

        first = driver.collect().probe
        second = driver.collect().probe

        assert first is not None and second is not None
        self.assertNotIn("apiCredit", first)
        self.assertEqual(second["apiCredit"]["balance"], "$12.34")

    def test_step_is_off_unless_the_page_is_configured(self) -> None:
        billing = BillingPage({BILLING: ok_probe("$12.34")})
        config = PlaywrightSessionConfig("profile", USAGE_URL, "probe()", login_url=LOGIN_URL)
        driver, _chromium, _clock, _logs = make_driver(
            [FakeContext([FakePage(), billing])], config=config
        )

        result = driver.collect()

        assert result.probe is not None
        self.assertNotIn("apiCredit", result.probe)
        self.assertEqual(billing.calls, [])

    def test_step_never_runs_after_a_usage_probe_that_is_not_signed_in(self) -> None:
        billing = BillingPage({BILLING: ok_probe("$12.34")})
        signed_out = FakePage(
            probe={"url": "https://chatgpt.com/auth/login", "mainText": "Log in", "metricBlocks": []}
        )
        driver, _chromium, _clock, _logs = make_driver([FakeContext([signed_out, billing])])

        result = driver.collect()

        assert result.probe is not None
        self.assertNotIn("apiCredit", result.probe)
        self.assertEqual(billing.calls, [])

    def test_wait_is_bounded_even_when_the_clock_stands_still(self) -> None:
        page = BillingPage({BILLING: home_probe()}, redirects={})
        page.url = PLATFORM_HOME
        driver = CodexUsagePlaywrightDriver(
            api_config(),
            playwright_starter=FakeStarter(FakeChromium([])),
            sleep=lambda _seconds: None,
            clock=lambda: 0.0,
        )
        page.routes = {PLATFORM_HOME: home_probe()}

        reading = driver._wait_for_api_credit(page, "api_probe()", 3.0)  # noqa: SLF001

        self.assertEqual(reading.reason, "unexpected_page")
        self.assertLessEqual(page.evaluations, 8)


class ApiCreditLoginTest(unittest.TestCase):
    def _pending_login(self) -> tuple[
        CodexUsagePlaywrightDriver, FakeChromium, FakeContext, BillingPage, FakeContext
    ]:
        headed_page = BillingPage(
            {PLATFORM_LOGIN: login_probe(), PLATFORM_HOME: home_probe()},
            redirects={BILLING: PLATFORM_LOGIN},
        )
        headed = FakeContext([headed_page])
        headless = FakeContext([FakePage()])
        driver, chromium, _clock, _logs = make_driver([headed, headless])
        return driver, chromium, headed, headed_page, headless

    def test_window_stays_open_until_the_balance_is_visible(self) -> None:
        driver, chromium, headed, page, _headless = self._pending_login()

        opened = driver.open_api_credit_login()

        self.assertEqual(opened.error, BrowserErrorCode.LOGIN_REQUIRED.value)
        status = driver.get_runtime_status()
        self.assertEqual(status.state, BrowserState.HEADED_LOGIN)
        self.assertTrue(status.login_window_open)
        self.assertEqual(page.calls, [("goto", BILLING)])
        self.assertFalse(headed.closed)
        # The user signs in and the site returns to the billing overview.
        page.redirects.clear()
        page.url = BILLING
        page.routes[BILLING] = ok_probe("$3.21")

        polled = driver.poll_api_credit_login()

        self.assertIsNone(polled.error)
        assert polled.probe is not None
        self.assertEqual(polled.probe["apiCredit"], {"status": "ok", "balance": "$3.21"})
        self.assertEqual([call["headless"] for call in chromium.calls], [False, True])
        self.assertTrue(headed.closed)
        self.assertEqual(driver.get_runtime_status().state, BrowserState.HEADLESS_READY)

    def test_already_signed_in_profile_finishes_on_open(self) -> None:
        page = BillingPage({BILLING: ok_probe("$8.00")})
        driver, chromium, _clock, _logs = make_driver(
            [FakeContext([page]), FakeContext([FakePage()])]
        )

        opened = driver.open_api_credit_login()

        self.assertIsNone(opened.error)
        assert opened.probe is not None
        self.assertEqual(opened.probe["apiCredit"], {"status": "ok", "balance": "$8.00"})
        self.assertEqual([call["headless"] for call in chromium.calls], [False, True])

    def test_poll_returns_to_billing_when_sign_in_lands_elsewhere(self) -> None:
        driver, _chromium, _headed, page, _headless = self._pending_login()
        _ = driver.open_api_credit_login()
        page.redirects.clear()
        page.url = PLATFORM_HOME
        page.routes[BILLING] = ok_probe("$5.00")

        polled = driver.poll_api_credit_login()

        assert polled.probe is not None
        self.assertEqual(polled.probe["apiCredit"]["balance"], "$5.00")
        self.assertEqual(page.calls[-1], ("goto", BILLING))

    def test_poll_never_pulls_the_user_away_from_the_identity_provider(self) -> None:
        driver, _chromium, headed, page, _headless = self._pending_login()
        _ = driver.open_api_credit_login()
        page.url = "https://accounts.google.com/o/oauth2/v2/auth"
        page.routes[page.url] = {
            "url": page.url,
            "title": "Sign in",
            "text": "Sign in with Google",
        }

        polled = driver.poll_api_credit_login()

        self.assertEqual(polled.error, BrowserErrorCode.LOGIN_REQUIRED.value)
        self.assertEqual(page.calls, [("goto", BILLING)])
        self.assertFalse(headed.closed)

    def _signed_in_without_a_balance(
        self,
    ) -> tuple[CodexUsagePlaywrightDriver, FakeChromium, FakeContext, BillingPage]:
        page = BillingPage(
            {BILLING: {"url": BILLING, "title": "Billing", "text": "Billing\nCurrent balance\n$1.00"}}
        )
        headed = FakeContext([page])
        driver, chromium, _clock, _logs = make_driver([headed, FakeContext([FakePage()])])
        return driver, chromium, headed, page

    def test_unreadable_billing_page_ends_the_wait_after_a_few_reads(self) -> None:
        driver, chromium, headed, _page = self._signed_in_without_a_balance()

        results = [driver.open_api_credit_login()]
        results += [driver.poll_api_credit_login() for _ in range(3)]

        self.assertEqual(
            [result.error for result in results[:3]],
            [BrowserErrorCode.LOGIN_REQUIRED.value] * 3,
        )
        self.assertIsNone(results[3].error)
        assert results[3].probe is not None
        self.assertEqual(
            results[3].probe["apiCredit"],
            {"status": "unavailable", "reason": "balance_unreadable"},
        )
        self.assertTrue(headed.closed)
        self.assertEqual([call["headless"] for call in chromium.calls], [False, True])
        self.assertEqual(driver.get_runtime_status().state, BrowserState.HEADLESS_READY)

    def test_login_screen_between_reads_restarts_the_unreadable_count(self) -> None:
        driver, _chromium, headed, page = self._signed_in_without_a_balance()
        billing = page.routes[BILLING]

        _ = driver.open_api_credit_login()
        _ = driver.poll_api_credit_login()
        _ = driver.poll_api_credit_login()
        page.routes[BILLING] = login_probe()
        _ = driver.poll_api_credit_login()
        page.routes[BILLING] = billing
        result = driver.poll_api_credit_login()

        self.assertEqual(result.error, BrowserErrorCode.LOGIN_REQUIRED.value)
        self.assertFalse(headed.closed)

    def test_landing_recovery_gives_up_after_two_returns(self) -> None:
        driver, _chromium, _headed, page, _headless = self._pending_login()
        onboarding = "https://platform.openai.com/onboarding"
        onboarding_probe = {"url": onboarding, "title": "Setup", "text": "Create organization"}
        page.routes[onboarding] = onboarding_probe
        _ = driver.open_api_credit_login()
        page.redirects = {BILLING: onboarding}
        page.url = onboarding

        for _ in range(4):
            _ = driver.poll_api_credit_login()

        self.assertEqual(page.calls.count(("goto", BILLING)), 3, "open + two returns")

    def test_window_closed_during_the_first_wait_is_classified_as_closed(self) -> None:
        driver, _chromium, headed, page, _headless = self._pending_login()

        def closed(_expression: str) -> Any:
            raise RuntimeError("Target page, context or browser has been closed")

        page.evaluate = closed  # type: ignore[method-assign]

        opened = driver.open_api_credit_login()

        self.assertEqual(opened.error, BrowserErrorCode.LOGIN_WINDOW_CLOSED.value)
        self.assertTrue(headed.closed)

    def test_landing_elsewhere_after_a_completed_page_still_returns_to_billing(self) -> None:
        driver, _chromium, _headed, page, _headless = self._pending_login()
        onboarding = "https://platform.openai.com/onboarding"
        page.routes[onboarding] = {"url": onboarding, "title": "Setup", "text": "Create organization"}
        _ = driver.open_api_credit_login()
        page.redirects = {BILLING: onboarding}
        page.url = onboarding
        for _ in range(3):
            _ = driver.poll_api_credit_login()
        page.redirects = {}
        page.routes[BILLING] = ok_probe("$4.00")
        page.url = PLATFORM_HOME

        polled = driver.poll_api_credit_login()

        assert polled.probe is not None
        self.assertEqual(polled.probe["apiCredit"]["balance"], "$4.00")

    def test_changing_query_strings_do_not_lift_the_return_limit(self) -> None:
        driver, _chromium, _headed, page, _headless = self._pending_login()
        _ = driver.open_api_credit_login()
        page.redirects = {}
        page.routes[BILLING] = home_probe()
        page.default = home_probe()
        for step in range(8):
            page.url = f"https://platform.openai.com/onboarding?state={step}"
            _ = driver.poll_api_credit_login()

        self.assertEqual(page.calls.count(("goto", BILLING)), 1 + 2, "the open and two returns")

    def test_returns_across_many_paths_stop_at_the_total(self) -> None:
        driver, _chromium, _headed, page, _headless = self._pending_login()
        _ = driver.open_api_credit_login()
        page.redirects = {}
        page.routes[BILLING] = home_probe()
        page.default = home_probe()
        for step in range(12):
            page.url = f"https://platform.openai.com/step{step}"
            _ = driver.poll_api_credit_login()

        self.assertEqual(page.calls.count(("goto", BILLING)), 1 + 6)

    def test_network_error_during_a_landing_return_is_not_a_closed_window(self) -> None:
        driver, _chromium, headed, page, _headless = self._pending_login()
        _ = driver.open_api_credit_login()
        page.url = PLATFORM_HOME
        page.goto_error = RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED at https://platform")

        polled = driver.poll_api_credit_login()

        self.assertEqual(polled.error, BrowserErrorCode.LOGIN_REQUIRED.value)
        self.assertFalse(headed.closed)

    def test_failed_headless_restart_after_sign_in_is_not_a_closed_window(self) -> None:
        billing = BillingPage({BILLING: ok_probe("$4.00")})
        driver, _chromium, _clock, _logs = make_driver(
            [FakeContext([billing]), OSError("Target page, context or browser has been closed")]
        )

        result = driver.open_api_credit_login()

        self.assertEqual(result.error, BrowserErrorCode.COLLECT_FAILED.value)

    def test_failed_landing_return_keeps_the_window_and_tries_again(self) -> None:
        driver, _chromium, headed, page, _headless = self._pending_login()
        _ = driver.open_api_credit_login()
        page.redirects = {}
        page.url = PLATFORM_HOME
        page.goto_error = RuntimeError("Navigation is interrupted by another navigation")

        failed = driver.poll_api_credit_login()

        self.assertEqual(failed.error, BrowserErrorCode.LOGIN_REQUIRED.value)
        self.assertFalse(headed.closed)
        page.goto_error = None
        page.routes[BILLING] = ok_probe("$4.00")
        recovered = driver.poll_api_credit_login()
        assert recovered.probe is not None
        self.assertEqual(recovered.probe["apiCredit"]["balance"], "$4.00")

    def test_closed_window_during_a_landing_return_ends_the_wait(self) -> None:
        driver, _chromium, headed, page, _headless = self._pending_login()
        _ = driver.open_api_credit_login()
        page.url = PLATFORM_HOME
        page.goto_error = RuntimeError("Target page, context or browser has been closed")

        polled = driver.poll_api_credit_login()

        self.assertEqual(polled.error, BrowserErrorCode.LOGIN_WINDOW_CLOSED.value)
        self.assertTrue(headed.closed)

    def test_browser_start_failure_is_not_reported_as_a_closed_window(self) -> None:
        driver, _chromium, _clock, _logs = make_driver(
            [OSError("BrowserType.launch_persistent_context: browser has been closed")]
        )

        opened = driver.open_api_credit_login()

        self.assertEqual(opened.error, BrowserErrorCode.COLLECT_FAILED.value)

    def test_closing_the_window_ends_the_wait(self) -> None:
        driver, _chromium, headed, page, _headless = self._pending_login()
        _ = driver.open_api_credit_login()
        page.closed = True

        polled = driver.poll_api_credit_login()

        self.assertEqual(polled.error, BrowserErrorCode.LOGIN_WINDOW_CLOSED.value)
        self.assertTrue(headed.closed)

    def test_poll_without_an_open_window_reports_it_closed(self) -> None:
        driver, chromium, _clock, _logs = make_driver([])

        polled = driver.poll_api_credit_login()

        self.assertEqual(polled.error, BrowserErrorCode.LOGIN_WINDOW_CLOSED.value)
        self.assertEqual(chromium.calls, [])

    def test_open_is_refused_without_a_configured_page(self) -> None:
        config = PlaywrightSessionConfig("profile", USAGE_URL, "probe()", login_url=LOGIN_URL)
        driver, chromium, _clock, _logs = make_driver([], config=config)

        opened = driver.open_api_credit_login()

        self.assertEqual(opened.error, BrowserErrorCode.COLLECT_FAILED.value)
        self.assertEqual(chromium.calls, [])

    def test_profile_lock_failure_is_classified_like_other_logins(self) -> None:
        driver, _chromium, _clock, _logs = make_driver(
            [OSError("Chrome user data directory is already in use")]
        )

        opened = driver.open_api_credit_login()

        self.assertEqual(opened.error, BrowserErrorCode.PROFILE_IN_USE.value)

    def test_failed_relaunch_does_not_postpone_the_next_balance_read(self) -> None:
        page = BillingPage({BILLING: ok_probe("$8.00")})
        driver, _chromium, clock, _logs = make_driver(
            [FakeContext([page]), OSError("Chrome user data directory is already in use")]
        )

        opened = driver.open_api_credit_login()

        self.assertEqual(opened.error, BrowserErrorCode.PROFILE_IN_USE.value)
        self.assertIsNone(driver._api_credit_next_due)  # noqa: SLF001
        _ = clock


class _WorkerDriver:
    calls: list[str] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def open_api_credit_login(self) -> BrowserOperationResult:
        self.calls.append("open_api_credit_login")
        return BrowserOperationResult(error="login_required")

    def poll_api_credit_login(self) -> BrowserOperationResult:
        self.calls.append("poll_api_credit_login")
        return BrowserOperationResult(error="login_window_closed")

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def export_session_cookies(self) -> list[dict[str, Any]]:
        return []


class WorkerApiCreditCommandTest(unittest.TestCase):
    def test_sign_in_commands_reach_the_driver_and_answer(self) -> None:
        _WorkerDriver.calls = []
        parent, child = Pipe()
        with patch(
            "src.apps.codex_usage_playwright_worker.CodexUsagePlaywrightDriver", _WorkerDriver
        ):
            worker = threading.Thread(target=run_playwright_worker, args=(child,), daemon=True)
            worker.start()
            parent.send(("bootstrap", PlaywrightSessionConfig("profile", "url", "probe"), 1, []))
            self.assertEqual(parent.recv()[0], "ready")
            answers = []
            for request_id, command in enumerate(
                ("open_api_credit_login", "poll_api_credit_login", "shutdown")
            ):
                parent.send(("command", request_id, command))
                answers.append(parent.recv())
            worker.join(5.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(
            _WorkerDriver.calls,
            ["open_api_credit_login", "poll_api_credit_login", "shutdown"],
        )
        self.assertEqual(answers[0][0:2], ("result", 0))
        self.assertEqual(answers[0][2].error, "login_required")
        self.assertEqual(answers[1][2].error, "login_window_closed")


if __name__ == "__main__":
    unittest.main()
