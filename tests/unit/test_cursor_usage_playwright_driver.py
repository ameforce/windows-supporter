from __future__ import annotations

import unittest

from src.apps.codex_usage_browser_types import PlaywrightSessionConfig
from src.apps.cursor_usage_playwright_driver import (
    CursorUsagePlaywrightDriver,
    classify_cursor_browser_error,
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


class CursorUsagePlaywrightDriverUnitTest(unittest.TestCase):
    def _config(self) -> PlaywrightSessionConfig:
        return PlaywrightSessionConfig(
            profile_dir="C:/app-owned/cursor-profile",
            usage_url="https://cursor.com/dashboard/usage",
            probe_script="probe",
            page_recycle_success_count=2,
        )

    def test_collect_uses_only_configured_persistent_profile_and_visible_probe(self) -> None:
        probe = {
            "url": "https://cursor.com/dashboard/usage",
            "mainText": "Included usage: 5 / 20",
            "metricBlocks": [
                {"metric_key": "cursor_account_summary", "block_text": "Included usage: 5 / 20"}
            ],
        }
        page = _Page([probe])
        chromium = _Chromium(_Context(page))
        playwright = _Playwright(chromium)
        driver = CursorUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: playwright,
            sleep=lambda _delay: None,
        )

        result = driver.collect()

        self.assertIsNone(result.error)
        self.assertEqual(result.probe, probe)
        self.assertEqual(chromium.calls[0]["user_data_dir"], "C:/app-owned/cursor-profile")
        self.assertEqual(chromium.calls[0]["channel"], "chrome")
        self.assertTrue(chromium.calls[0]["chromium_sandbox"])
        self.assertNotIn("user_agent", chromium.calls[0])

    def test_login_page_is_reported_without_bypass(self) -> None:
        probe = {
            "url": "https://cursor.com/login",
            "mainText": "Sign in",
            "metricBlocks": [],
        }
        page = _Page([probe])
        driver = CursorUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
            sleep=lambda _delay: None,
        )

        result = driver.collect()

        self.assertEqual(result.error, "login_required")

    def test_transient_poll_error_keeps_headed_login_window_state(self) -> None:
        page = _Page(
            [
                {
                    "url": "https://cursor.com/login",
                    "mainText": "Sign in",
                    "metricBlocks": [],
                }
            ]
        )
        driver = CursorUsagePlaywrightDriver(
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
        self.assertEqual(classify_cursor_browser_error("Navigation timeout"), "command_timeout")
        self.assertEqual(classify_cursor_browser_error("Page crashed"), "renderer_crashed")
        self.assertEqual(classify_cursor_browser_error("Target page has been closed"), "transport_closed")
        self.assertEqual(classify_cursor_browser_error("429 Too Many Requests"), "rate_limited")

    def test_cookie_export_and_import_are_deliberate_noops(self) -> None:
        page = _Page([])
        driver = CursorUsagePlaywrightDriver(
            self._config(),
            playwright_starter=lambda: _Playwright(_Chromium(_Context(page))),
        )

        driver.import_session_cookies([{"name": "must-not-import"}])

        self.assertEqual(driver.export_session_cookies(), [])


if __name__ == "__main__":
    unittest.main()
