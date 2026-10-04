"""Run the shipped probe in Chrome against the observed, sanitized overview DOM."""

from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

from src.apps.codex_usage_monitor import (
    USAGE_PAGE_PROBE_SCRIPT, extract_usage_metrics_from_semantic_blocks,
    extract_usage_reset_info_from_semantic_blocks,
)


FIXTURE = Path(__file__).parents[1] / "e2e/fixtures/codex-usage-overview.html"


class UsageOverviewProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def _probe(self, html):
        # UTC makes an accidental dependency on the machine's Korean timezone visible.
        with self.browser.new_context(timezone_id="UTC") as context:
            context.route("**/*", lambda route: route.fulfill(status=404, body="{}"))
            page = context.new_page()
            page.goto("https://chatgpt.com/settings/usage?tab=overview")
            page.set_content(html)
            return page.evaluate(USAGE_PAGE_PROBE_SCRIPT)

    def test_nested_overview_cards_yield_current_values_and_exact_reset(self):
        probe = self._probe(FIXTURE.read_text(encoding="utf-8"))
        metrics = extract_usage_metrics_from_semantic_blocks(probe["metricBlocks"])
        self.assertEqual(metrics, {"weekly_limit": "71%", "remaining_credit": "62462"})
        resets = extract_usage_reset_info_from_semantic_blocks(
            probe["metricBlocks"], captured_at="2026-10-04T10:00:00+00:00")
        self.assertEqual(resets.get("weekly_limit_reset_at"), "2026-10-09T22:40:04.000Z")
        self.assertFalse(resets.get("five_hour_limit_reset_at"))

    def test_loading_limit_never_borrows_history_percentage(self):
        html = FIXTURE.read_text(encoding="utf-8").replace("71% 남음", "불러오는 중")
        probe = self._probe(html)
        metrics = extract_usage_metrics_from_semantic_blocks(probe["metricBlocks"])
        self.assertNotIn("weekly_limit", metrics)
        self.assertNotIn("five_hour_limit", metrics)

    def test_monthly_plan_with_zero_remaining_is_a_valid_current_limit(self):
        html = FIXTURE.read_text(encoding="utf-8").replace("주간 사용 한도", "월 사용 한도").replace("71% 남음", "0% 남음")
        probe = self._probe(html)
        metrics = extract_usage_metrics_from_semantic_blocks(probe["metricBlocks"])
        self.assertEqual(metrics, {"monthly_limit": "0%", "remaining_credit": "62462"})
        resets = extract_usage_reset_info_from_semantic_blocks(
            probe["metricBlocks"], captured_at="2026-10-04T10:00:00+00:00")
        self.assertEqual(resets.get("monthly_limit_reset_at"), "2026-10-09T22:40:04.000Z")

    def test_hidden_analytics_never_creates_limits_on_the_overview(self):
        html = FIXTURE.read_text(encoding="utf-8")
        history = '<section style="display:none"><h2>5-hour usage limit</h2><p>66% left</p></section>'
        probe = self._probe(html.replace("</body>", history + "</body>"))
        metrics = extract_usage_metrics_from_semantic_blocks(probe["metricBlocks"])
        self.assertEqual(metrics, {"weekly_limit": "71%", "remaining_credit": "62462"})

    def test_loading_current_card_never_uses_hidden_history_of_same_limit(self):
        html = FIXTURE.read_text(encoding="utf-8").replace("71% 남음", "불러오는 중")
        history = '<section hidden><h2>주간 사용 한도</h2><p>42% 남음</p></section>'
        probe = self._probe(html.replace("</body>", history + "</body>"))
        metrics = extract_usage_metrics_from_semantic_blocks(probe["metricBlocks"])
        self.assertNotIn("weekly_limit", metrics)


if __name__ == "__main__":
    unittest.main()
