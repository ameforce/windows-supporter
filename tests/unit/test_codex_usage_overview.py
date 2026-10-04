"""Regression for ChatGPT's October 2026 shared usage settings migration."""

import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    are_equivalent_codex_usage_urls,
    build_codex_login_entry_url,
    canonicalize_codex_usage_url,
    is_codex_usage_url,
)
from src.apps.codex_usage_playwright_driver import _canonical_usage_url


OVERVIEW = "https://chatgpt.com/settings/usage?tab=overview"


class UsageOverviewUrlTest(unittest.TestCase):
    def test_saved_legacy_and_analytics_targets_migrate_to_overview(self):
        for url in (
            "", "https://chatgpt.com/codex/settings/usage",
            "https://chatgpt.com/codex/cloud/settings/usage",
            "https://chatgpt.com/codex/settings/analytics",
            "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            "https://chatgpt.com/settings/usage?tab=analytics#usage",
            "https://chatgpt.com/settings/usage",
        ):
            with self.subTest(url=url):
                self.assertEqual(canonicalize_codex_usage_url(url), OVERVIEW)

    def test_overview_is_accepted_but_history_and_foreign_pages_are_not(self):
        self.assertTrue(is_codex_usage_url(OVERVIEW))
        self.assertTrue(is_codex_usage_url("https://chatgpt.com/settings/usage"))
        for url in (
            "https://chatgpt.com/settings/usage?tab=analytics",
            "https://chatgpt.com/settings/usage?tab=overview&tab=analytics",
            "https://chatgpt.com/settings/billing#usage",
            "https://example.com/settings/usage?tab=overview",
            "https://chatgpt.com.evil.example/settings/usage?tab=overview",
            "http://chatgpt.com/settings/usage?tab=overview",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_codex_usage_url(url))
                self.assertNotEqual(_canonical_usage_url(url), _canonical_usage_url(OVERVIEW))

    def test_foreign_and_fixture_navigation_is_not_rewritten(self):
        for url in ("https://example.com/codex/settings/usage", "http://127.0.0.1:1234/usage"):
            self.assertEqual(canonicalize_codex_usage_url(url), url)

    def test_login_returns_to_overview(self):
        self.assertEqual(build_codex_login_entry_url(OVERVIEW),
                         "https://chatgpt.com/auth/login?next=/settings/usage%3Ftab%3Doverview")

    def test_login_keeps_the_complete_usage_query_inside_next(self):
        login = build_codex_login_entry_url("https://chatgpt.com/settings/usage?source=app&tab=analytics")
        self.assertEqual(parse_qs(urlsplit(login).query),
                         {"next": ["/settings/usage?source=app&tab=overview"]})

    def test_history_is_not_equivalent_to_a_live_usage_page(self):
        history = "https://chatgpt.com/settings/usage?tab=analytics#usage"
        self.assertFalse(are_equivalent_codex_usage_urls(history, OVERVIEW))
        self.assertNotEqual(_canonical_usage_url(history), _canonical_usage_url(OVERVIEW))
        self.assertEqual(_canonical_usage_url("https://chatgpt.com/codex/settings/usage"),
                         _canonical_usage_url(OVERVIEW))

    def test_monitor_accepts_current_overview_and_rejects_history_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp)
            probe = {"url": OVERVIEW, "mainText": "주간 사용 한도 71% 남음",
                     "metricBlocks": [{"metric_key": "weekly_limit", "label_text": "주간 사용 한도",
                                       "block_text": "주간 사용 한도 71% 남음", "value_candidates": ["71% 남음"]}]}
            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(probe)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.weekly_limit, "71%")
            probe["url"] = "https://chatgpt.com/settings/usage?tab=analytics"
            self.assertIsNone(monitor._CodexUsageMonitor__build_snapshot_from_probe(probe))


if __name__ == "__main__":
    unittest.main()
