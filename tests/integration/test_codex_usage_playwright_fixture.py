from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from typing import cast

from playwright.sync_api import sync_playwright

from src.apps.codex_usage_browser_types import JsonValue, parse_usage_probe
from src.apps.codex_usage_monitor import USAGE_PAGE_PROBE_SCRIPT


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "e2e"
    / "fixtures"
    / "codex-usage-page-current.html"
)


class CodexUsagePlaywrightFixtureIntegrationTest(unittest.TestCase):
    def test_installed_chrome_evaluates_usage_probe_without_bundled_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as profile_dir:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    profile_dir,
                    channel="chrome",
                    headless=True,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    _ = page.goto(FIXTURE_PATH.as_uri(), wait_until="domcontentloaded")

                    raw_payload = cast(
                        JsonValue,
                        page.evaluate(USAGE_PAGE_PROBE_SCRIPT),
                    )
                    payload = parse_usage_probe(raw_payload)

                    self.assertIsNotNone(payload)
                    if payload is None:
                        self.fail("usage probe payload was not an object")
                    metric_blocks = payload.get("metricBlocks", [])
                    metric_keys = {
                        block.get("metric_key")
                        for block in metric_blocks
                    }
                    self.assertEqual(
                        metric_keys,
                        {
                            "five_hour_limit",
                            "weekly_limit",
                            "gpt_5_3_codex_spark_five_hour_limit",
                            "gpt_5_3_codex_spark_weekly_limit",
                            "remaining_credit",
                        },
                    )
                finally:
                    context.close()


if __name__ == "__main__":
    _ = unittest.main()
