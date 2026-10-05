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

    def _probe(self, html, url="https://chatgpt.com/settings/usage?tab=overview", session=None):
        # UTC makes an accidental dependency on the machine's Korean timezone visible.
        with self.browser.new_context(timezone_id="UTC") as context:
            def route_request(route):
                if route.request.url.endswith("/api/auth/session") and session is not None:
                    route.fulfill(json=session)
                else:
                    route.fulfill(status=404, body="{}")
            context.route("**/*", route_request)
            page = context.new_page()
            page.goto(url)
            page.set_content(html)
            return page.evaluate(USAGE_PAGE_PROBE_SCRIPT)

    def test_legacy_credit_dialog_excludes_background_and_hidden_history(self):
        html = FIXTURE.with_name("codex-usage-credits-dialog.html").read_text(encoding="utf-8")
        session = {"user": {"id": "user-current", "name": "Current user"}, "account": {"id": "account-current", "planType": "free"}}
        probe = self._probe(html, "https://chatgpt.com/?tab=overview#settings/Usage", session)
        self.assertIs(probe.get("creditsOnly"), True)
        self.assertEqual(probe["accountId"], "account-current")
        self.assertEqual(extract_usage_metrics_from_semantic_blocks(probe["metricBlocks"]), {"remaining_credit": "1234"})
        self.assertNotIn("99%", probe["mainText"])
        self.assertNotIn("55%", probe["mainText"])

    def test_legacy_credit_view_with_no_session_cannot_supply_identity(self):
        html = FIXTURE.with_name("codex-usage-credits-dialog.html").read_text(encoding="utf-8")
        # A dangling account object without an authenticated user is not identity proof.
        probe = self._probe(html, "https://chatgpt.com/#settings/Usage", {"account": {"id": "account-current"}})
        self.assertIs(probe.get("creditsOnly"), True)
        self.assertEqual(probe.get("accountId"), "")

    def test_legacy_credit_view_rejects_inactive_loading_and_other_panels(self):
        html = FIXTURE.with_name("codex-usage-credits-dialog.html").read_text(encoding="utf-8")
        for modified in (
            html.replace('data-state="active"', 'data-state="inactive" hidden'),
            html.replace('data-state="active"', 'data-state="active" aria-busy="true"'),
            html.replace('<h3>사용량</h3>', '<h3>사용량</h3><div role="progressbar"></div>'),
            html.replace('<h3>사용량</h3>', '<h3>사용량</h3><div aria-busy="true"></div>'),
            html.replace('id="settings-content-Usage"', 'id="settings-content-Billing"'),
        ):
            probe = self._probe(modified, "https://chatgpt.com/#settings/Usage")
            self.assertFalse(probe.get("creditsOnly", False))
            self.assertFalse(any(block["metric_key"] != "remaining_credit" for block in probe["metricBlocks"]))

    def test_legacy_zero_credit_balance_is_current_data(self):
        html = FIXTURE.with_name("codex-usage-credits-dialog.html").read_text(encoding="utf-8").replace("1,234", "0")
        probe = self._probe(html, "https://chatgpt.com/#settings/Usage", {"user": {"id": "user-current"}, "account": {"id": "account-current"}})
        self.assertIs(probe.get("creditsOnly"), True)
        self.assertEqual(extract_usage_metrics_from_semantic_blocks(probe["metricBlocks"]), {"remaining_credit": "0"})

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
