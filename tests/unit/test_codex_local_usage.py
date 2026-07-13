import unittest
from datetime import datetime, timezone

from src.apps.codex_local_usage import parse_codex_rate_limit_event
from src.apps.codex_usage_monitor import UsageSnapshot, reconcile_snapshot_with_local_codex_usage


class CodexLocalUsageUnitTest(unittest.TestCase):
    def test_parser_preserves_zero_used_as_full_remaining(self) -> None:
        # Given: Windows Codex reports an untouched five-hour window.
        event = {
            "timestamp": "2026-07-13T00:52:19.258Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 0.0,
                        "window_minutes": 300,
                        "resets_at": 1783882872,
                    },
                },
            },
        }

        # When: the zero value crosses the local rollout parser boundary.
        snapshot = parse_codex_rate_limit_event(event)

        # Then: zero is not mistaken for a missing field.
        if snapshot is None:
            self.fail("zero-used window was discarded")
        self.assertEqual(snapshot.five_hour_limit, "100%")

    def test_parser_maps_window_minutes_and_used_percent_to_remaining(self) -> None:
        # Given: Windows Codex reports one active seven-day window and no secondary window.
        event = {
            "timestamp": "2026-07-13T00:52:19.258Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 5.0,
                        "window_minutes": 10080,
                        "resets_at": 1784487672,
                    },
                    "secondary": None,
                    "plan_type": "pro",
                },
            },
        }

        # When: the local rollout event crosses the adapter boundary.
        snapshot = parse_codex_rate_limit_event(event)

        # Then: window identity and percentage meaning are explicit.
        if snapshot is None:
            self.fail("valid Codex rate-limit event was discarded")
        self.assertEqual(snapshot.weekly_limit, "95%")
        self.assertEqual(snapshot.five_hour_limit, "")
        self.assertEqual(snapshot.reported_metric_keys, ("weekly_limit",))
        self.assertEqual(snapshot.weekly_limit_reset_at, "2026-07-20T04:01:12+09:00")

    def test_reconcile_uses_local_usage_only_when_reset_and_time_match(self) -> None:
        # Given: the web page and Windows Codex identify the same weekly reset window.
        web = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "0%",
                "weekly_limit": "97%",
                "remaining_credit": "0",
            },
            captured_at="2026-07-13T09:52:20+09:00",
            reset_info={
                "five_hour_limit_reset_at": "2026-07-14T02:39:00+09:00",
                "weekly_limit_reset_at": "2026-07-20T04:01:00+09:00",
            },
            reported_metric_keys=(
                "five_hour_limit",
                "weekly_limit",
                "remaining_credit",
            ),
        )
        local_event = {
            "timestamp": "2026-07-13T00:52:19.258Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 5.0,
                        "window_minutes": 10080,
                        "resets_at": 1784487672,
                    },
                    "secondary": None,
                },
            },
        }
        local = parse_codex_rate_limit_event(local_event)

        # When: the web snapshot is reconciled at the acquisition boundary.
        reconciled = reconcile_snapshot_with_local_codex_usage(web, local)

        # Then: Codex's remaining value replaces lagging web data and absent windows stay absent.
        self.assertEqual(reconciled.weekly_limit, "95%")
        self.assertEqual(reconciled.five_hour_limit, "")
        self.assertEqual(reconciled.five_hour_limit_reset_at, "")
        self.assertEqual(reconciled.remaining_credit, "0")
        self.assertEqual(reconciled.captured_at, "2026-07-13T00:52:19.258Z")

    def test_reconcile_rejects_other_account_reset_window(self) -> None:
        # Given: a web profile has a different weekly reset than local Windows Codex.
        web = UsageSnapshot.from_metrics(
            {"weekly_limit": "61%"},
            captured_at="2026-07-13T09:52:20+09:00",
            reset_info={"weekly_limit_reset_at": "2026-07-18T04:01:00+09:00"},
            reported_metric_keys=("weekly_limit",),
        )
        local_event = {
            "timestamp": "2026-07-13T00:52:19.258Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 5.0,
                        "window_minutes": 10080,
                        "resets_at": 1784487672,
                    },
                },
            },
        }
        local = parse_codex_rate_limit_event(local_event)

        # When: reconciliation evaluates the mismatched account.
        reconciled = reconcile_snapshot_with_local_codex_usage(
            web,
            local,
            now=datetime(2026, 7, 13, 0, 52, 20, tzinfo=timezone.utc),
        )

        # Then: the other account's web snapshot remains untouched.
        self.assertEqual(reconciled.to_dict(), web.to_dict())
