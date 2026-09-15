from __future__ import annotations

import unittest

from src.apps.codex_usage_browser_types import PlaywrightSessionConfig
from src.apps.claude_usage_playwright_driver import (
    ClaudeUsagePlaywrightDriver,
    classify_claude_browser_error,
)


class _Page:
    def __init__(self, probes: list[dict[str, object]]) -> None:
        self.url = "about:blank"
        self.probes = list(probes)
        self.calls: list[str] = []
        self.closed = False
        self.handlers: dict[str, object] = {}

    def goto(self, url: str, *, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        self.url = url
        self.calls.append("goto")

    def reload(self, *, timeout: int, wait_until: str) -> None:
        _ = timeout, wait_until
        self.calls.append("reload")

    def evaluate(self, _script: str) -> dict[str, object]:
        self.calls.append("evaluate")
        return self.probes.pop(0)

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]
        self.page = page
        self.closed = False

    def new_page(self) -> _Page:
        return self.page

    def close(self) -> None:
        self.closed = True


class _Chromium:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.calls: list[dict[str, object]] = []

    def launch_persistent_context(self, user_data_dir: str, **kwargs: object) -> _Context:
        self.calls.append({"user_data_dir": user_data_dir, **kwargs})
        return self.context


class _Playwright:
    def __init__(self, chromium: _Chromium) -> None:
        self.chromium = chromium
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class ClaudeUsagePlaywrightDriverUnitTest(unittest.TestCase):
    def _config(self) -> PlaywrightSessionConfig:
        return PlaywrightSessionConfig(
            profile_dir="C:/app-owned/claude-profile",
            usage_url="https://claude.ai/settings/usage",
            probe_script="probe",
            page_recycle_success_count=2,
        )

    def test_collect_uses_only_configured_persistent_profile_and_usage_api_block(self) -> None:
        probe = {
            "url": "https://claude.ai/settings/usage",
            "mainText": "Current session 12% used",
            "metricBlocks": [
                {
                    "metric_key": "claude_usage_api",
                    "block_text": '{"usage": {"five_hour": {"utilization": 12.0}}}',
                }
            ],
        }
        page = _Page([probe])
        chromium = _Chromium(_Context(page))
        playwright = _Playwright(chromium)
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: playwright,
            sleep=lambda _delay: None,
        )

        result = driver.collect()

        self.assertIsNone(result.error)
        self.assertEqual(result.probe, probe)
        self.assertEqual(chromium.calls[0]["user_data_dir"], "C:/app-owned/claude-profile")
        self.assertEqual(chromium.calls[0]["channel"], "chrome")
        self.assertTrue(chromium.calls[0]["chromium_sandbox"])
        self.assertNotIn("user_agent", chromium.calls[0])

    def test_launch_suppresses_automation_fingerprints_for_oauth(self) -> None:
        probe = {
            "url": "https://claude.ai/settings/usage",
            "mainText": "Current session 12% used",
            "metricBlocks": [
                {
                    "metric_key": "claude_usage_api",
                    "block_text": '{"usage": {"five_hour": {"utilization": 12.0}}}',
                }
            ],
        }
        chromium = _Chromium(_Context(_Page([probe])))
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(chromium),
            sleep=lambda _delay: None,
        )

        result = driver.collect()

        self.assertIsNone(result.error)
        call = chromium.calls[0]
        self.assertIn("--enable-automation", call["ignore_default_args"])
        self.assertNotIn("--enable-automation", call["args"])
        self.assertIn(
            "--disable-blink-features=AutomationControlled",
            call["args"],
            "navigator.webdriver must stay false for google oauth sign-in",
        )
        self.assertIn(
            "--test-type",
            call["args"],
            "chrome bad-flags prompt must be suppressed while the flag is in use",
        )

    def test_headed_login_launch_hides_automation_signals(self) -> None:
        page = _Page(
            [
                {
                    "url": "https://claude.ai/login",
                    "mainText": "Log in",
                    "metricBlocks": [],
                }
            ]
        )
        chromium = _Chromium(_Context(page))
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(chromium),
            sleep=lambda _delay: None,
        )

        result = driver.open_login()

        self.assertEqual(result.error, "login_required")
        self.assertTrue(driver.get_runtime_status().login_window_open)
        call = chromium.calls[-1]
        self.assertFalse(call["headless"])
        self.assertIn("--enable-automation", call["ignore_default_args"])
        self.assertNotIn("--enable-automation", call["args"])
        self.assertIn("--disable-blink-features=AutomationControlled", call["args"])
        self.assertIn("--test-type", call["args"])

    def test_summary_block_alone_counts_as_summary(self) -> None:
        probe = {
            "url": "https://claude.ai/settings/usage",
            "mainText": "Current session 12% used",
            "metricBlocks": [
                {
                    "metric_key": "claude_usage_summary",
                    "block_text": "Current session\n12% used",
                }
            ],
        }
        page = _Page([probe])
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )

        result = driver.collect()

        self.assertIsNone(result.error)
        self.assertEqual(result.probe, probe)

    def test_auth_required_marker_block_is_reported_as_login_required(self) -> None:
        probe = {
            "url": "https://claude.ai/settings/usage",
            "mainText": "Usage",
            "metricBlocks": [
                {"metric_key": "claude_auth_required", "block_text": "auth_required"}
            ],
        }
        page = _Page([probe])
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )

        result = driver.collect()

        self.assertEqual(result.error, "login_required")

    def test_login_page_is_reported_without_bypass(self) -> None:
        probe = {
            "url": "https://claude.ai/login",
            "mainText": "Log in to Claude",
            "metricBlocks": [],
        }
        page = _Page([probe])
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )

        result = driver.collect()

        self.assertEqual(result.error, "login_required")

    def test_rate_limited_marker_block_is_reported(self) -> None:
        probe = {
            "url": "https://claude.ai/settings/usage",
            "mainText": "Usage",
            "metricBlocks": [
                {"metric_key": "claude_rate_limited", "block_text": "rate_limited"}
            ],
        }
        page = _Page([probe])
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )

        result = driver.collect()

        self.assertEqual(result.error, "rate_limited")

    def test_open_login_prefers_auth_marker_over_summary_text(self) -> None:
        probe = {
            "url": "https://claude.ai/login",
            "mainText": "Log in to Claude. Manage session limits after sign in.",
            "metricBlocks": [
                {
                    "metric_key": "claude_usage_summary",
                    "block_text": "Session and weekly usage",
                },
                {"metric_key": "claude_auth_required", "block_text": "auth_required"},
            ],
        }
        page = _Page([probe])
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )

        result = driver.open_login()
        status = driver.get_runtime_status()

        self.assertEqual(result.error, "login_required")
        self.assertIsNone(result.probe)
        self.assertEqual(status.state.value, "headed_login")
        self.assertTrue(status.login_window_open)

    def test_open_login_reports_rate_limited_instead_of_login_prompt(self) -> None:
        probe = {
            "url": "https://claude.ai/settings/usage",
            "mainText": "Usage",
            "metricBlocks": [
                {"metric_key": "claude_rate_limited", "block_text": "rate_limited"}
            ],
        }
        page = _Page([probe])
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )

        result = driver.open_login()

        self.assertEqual(result.error, "rate_limited")
        self.assertTrue(driver.get_runtime_status().login_window_open)

    def test_poll_login_prefers_auth_marker_over_summary_text(self) -> None:
        login_probe = {
            "url": "https://claude.ai/login",
            "mainText": "Log in",
            "metricBlocks": [],
        }
        masked_probe = {
            "url": "https://claude.ai/login",
            "mainText": "Log in to see weekly session usage",
            "metricBlocks": [
                {
                    "metric_key": "claude_usage_summary",
                    "block_text": "Session and weekly usage",
                },
                {"metric_key": "claude_auth_required", "block_text": "auth_required"},
            ],
        }
        page = _Page([login_probe, masked_probe])
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )
        driver.open_login()

        result = driver.poll_login()

        self.assertEqual(result.error, "login_required")
        self.assertIsNone(result.probe)
        self.assertTrue(driver.get_runtime_status().login_window_open)

    def test_transient_poll_error_keeps_headed_login_window_state(self) -> None:
        page = _Page(
            [
                {
                    "url": "https://claude.ai/login",
                    "mainText": "Log in",
                    "metricBlocks": [],
                }
            ]
        )
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )
        driver.open_login()

        result = driver.poll_login()
        status = driver.get_runtime_status()

        self.assertEqual(result.error, "collect_failed")
        self.assertEqual(status.state.value, "headed_login")
        self.assertTrue(status.login_window_open)

    def test_error_classifier_normalizes_timeout_crash_and_closed_transport(self) -> None:
        self.assertEqual(classify_claude_browser_error("Navigation timeout"), "command_timeout")
        self.assertEqual(classify_claude_browser_error("Page crashed"), "renderer_crashed")
        self.assertEqual(classify_claude_browser_error("Target page has been closed"), "transport_closed")
        self.assertEqual(classify_claude_browser_error("429 Too Many Requests"), "rate_limited")

    def test_cookie_export_and_import_are_deliberate_noops(self) -> None:
        page = _Page([])
        driver = ClaudeUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
        )

        driver.import_session_cookies([{"name": "must-not-import"}])

        self.assertEqual(driver.export_session_cookies(), [])


if __name__ == "__main__":
    unittest.main()
