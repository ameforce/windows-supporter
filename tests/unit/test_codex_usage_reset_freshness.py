import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from src.apps.codex_usage_browser_types import BrowserOperationResult
from src.apps.codex_usage_monitor import CodexUsageMonitor, UsageSnapshot, merge_snapshot_with_previous


class CodexResetFreshnessTest(unittest.TestCase):
    def test_successful_scrape_without_reset_does_not_republish_old_deadline(self):
        old_reset = (datetime.now().astimezone() + timedelta(days=2)).isoformat()

        class Session:
            def __init__(self):
                self.calls = 0
                self.reset = old_reset
                self.remaining = "98%"

            def collect(self):
                self.calls += 1
                return BrowserOperationResult(probe={
                    "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                    "mainText": "Weekly usage limit",
                    "accountId": "account-current",
                    "profileName": "Daeng - ameforce",
                    "metricBlocks": [{
                        "metric_key": "weekly_limit",
                        "label_text": "Weekly usage limit",
                        "block_text": "Weekly usage limit " + self.remaining,
                        "value_candidates": [self.remaining],
                        "reset_at_candidates": [self.reset] if self.reset else [],
                    }],
                })

        session = Session()
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=lambda _: session,
            )
            def collect_and_commit():
                snapshot, error = monitor._CodexUsageMonitor__collect_snapshot()
                self.assertIsNone(error)
                self.assertIsNotNone(snapshot)
                monitor.handle_snapshot(snapshot)
                return monitor.get_last_snapshot()

            first = collect_and_commit()
            self.assertTrue(first.weekly_limit_reset_at)
            session.remaining, session.reset = "100%", ""
            second = collect_and_commit()
            self.assertEqual(second.weekly_limit, "100%")
            self.assertEqual(second.weekly_limit_reset_at, "")

            # The percentage remains 100%; collection must still accept a new window.
            session.reset = (datetime.now().astimezone() + timedelta(days=6)).isoformat()
            third = collect_and_commit()
            self.assertEqual(third.weekly_limit, "100%")
            self.assertTrue(third.weekly_limit_reset_at)
            self.assertNotEqual(third.weekly_limit_reset_at, first.weekly_limit_reset_at)
            self.assertEqual(session.calls, 3)
            with open(os.path.join(tmp, "codex_usage_state.json"), encoding="utf-8") as stream:
                stored = json.load(stream)
            self.assertEqual(stored["last_snapshot"]["weekly_limit_reset_at"], third.weekly_limit_reset_at)

    def test_unchanged_percentage_with_missing_reset_clears_previous_deadline(self):
        previous = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"},
            reset_info={"weekly_limit_reset_at": "2026-09-10T15:47:00+09:00"},
        )
        current = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"}, reported_metric_keys=("weekly_limit",)
        )
        self.assertEqual(merge_snapshot_with_previous(current, previous).weekly_limit_reset_at, "")

    def test_value_parse_failure_can_still_keep_previous_value_and_deadline(self):
        previous = UsageSnapshot.from_metrics(
            {"weekly_limit": "68%"},
            reset_info={"weekly_limit_reset_at": "2026-09-15T10:24:00+09:00"},
        )
        current = UsageSnapshot.from_metrics({}, reported_metric_keys=("weekly_limit",))
        merged = merge_snapshot_with_previous(current, previous)
        self.assertEqual(merged.weekly_limit, "68%")
        self.assertEqual(merged.weekly_limit_reset_at, previous.weekly_limit_reset_at)


if __name__ == "__main__":
    unittest.main()
