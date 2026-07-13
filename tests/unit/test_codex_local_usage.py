import base64
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.apps.codex_local_usage import (
    find_latest_windows_codex_usage,
    parse_codex_rate_limit_event,
)
from src.apps.codex_usage_monitor import UsageSnapshot, reconcile_snapshot_with_local_codex_usage


class CodexLocalUsageUnitTest(unittest.TestCase):
    def test_parser_rejects_timezone_less_timestamp(self) -> None:
        # Given: a malformed rollout event has no timezone on its timestamp.
        event = {
            "timestamp": "2026-07-13T00:52:19",
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

        # When: the malformed event crosses the parser boundary.
        snapshot = parse_codex_rate_limit_event(event)

        # Then: it cannot mix naive and aware timestamps in latest-event ordering.
        self.assertIsNone(snapshot)

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
        if local is None:
            self.fail("valid local event was discarded")
        local = replace(local, account_id="acct-local")

        # When: the web snapshot is reconciled at the acquisition boundary.
        reconciled = reconcile_snapshot_with_local_codex_usage(
            web,
            local,
            web_account_id="acct-local",
        )

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
        if local is None:
            self.fail("valid local event was discarded")
        local = replace(local, account_id="acct-local")

        # When: reconciliation evaluates the mismatched account.
        reconciled = reconcile_snapshot_with_local_codex_usage(
            web,
            local,
            now=datetime(2026, 7, 13, 0, 52, 20, tzinfo=timezone.utc),
            web_account_id="acct-local",
        )

        # Then: the other account's web snapshot remains untouched.
        self.assertEqual(reconciled.to_dict(), web.to_dict())

    def test_reconcile_rejects_when_only_one_local_window_reset_matches(self) -> None:
        # Given: weekly reset matches but the local five-hour reset belongs to another window.
        web = UsageSnapshot.from_metrics(
            {"five_hour_limit": "70%", "weekly_limit": "61%"},
            captured_at="2026-07-13T09:52:20+09:00",
            reset_info={
                "five_hour_limit_reset_at": "2026-07-13T12:00:00+09:00",
                "weekly_limit_reset_at": "2026-07-20T04:01:00+09:00",
            },
            reported_metric_keys=("five_hour_limit", "weekly_limit"),
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
                    "secondary": {
                        "used_percent": 30.0,
                        "window_minutes": 300,
                        "resets_at": 1783908000,
                    },
                },
            },
        }
        local = parse_codex_rate_limit_event(local_event)
        if local is None:
            self.fail("valid local event was discarded")
        local = replace(local, account_id="acct-local")

        # When: reconciliation checks the mixed-reset payload.
        reconciled = reconcile_snapshot_with_local_codex_usage(
            web,
            local,
            web_account_id="acct-local",
        )

        # Then: one matching reset cannot authorize cross-window replacement.
        self.assertEqual(reconciled.to_dict(), web.to_dict())

    def test_reconcile_rejects_same_reset_from_different_account(self) -> None:
        # Given: two accounts share a weekly reset but have different stable IDs.
        web = UsageSnapshot.from_metrics(
            {"weekly_limit": "61%"},
            captured_at="2026-07-13T09:52:20+09:00",
            reset_info={"weekly_limit_reset_at": "2026-07-20T04:01:00+09:00"},
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
        if local is None:
            self.fail("valid local event was discarded")
        local = replace(local, account_id="acct-local")

        # When: reconciliation sees the other web account's stable ID.
        reconciled = reconcile_snapshot_with_local_codex_usage(
            web,
            local,
            web_account_id="acct-other",
        )

        # Then: reset coincidence alone cannot cross the account boundary.
        self.assertEqual(reconciled.to_dict(), web.to_dict())

    def test_reconcile_rejects_matching_account_with_different_plan(self) -> None:
        # Given: stable account ID matches but rollout/auth plan conflicts with web plan.
        web = UsageSnapshot.from_metrics(
            {"weekly_limit": "61%"},
            captured_at="2026-07-13T09:52:20+09:00",
            reset_info={"weekly_limit_reset_at": "2026-07-20T04:01:00+09:00"},
            reported_metric_keys=("weekly_limit",),
        )
        local_event = {
            "timestamp": "2026-07-13T00:52:19.258Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "plan_type": "free",
                    "primary": {
                        "used_percent": 5.0,
                        "window_minutes": 10080,
                        "resets_at": 1784487672,
                    },
                },
            },
        }
        local = parse_codex_rate_limit_event(local_event)
        if local is None:
            self.fail("valid local event was discarded")
        local = replace(local, account_id="acct-local")

        # When: reconciliation evaluates the web plan boundary.
        reconciled = reconcile_snapshot_with_local_codex_usage(
            web,
            local,
            web_account_id="acct-local",
            web_plan_type="pro",
        )

        # Then: the conflicting plan keeps the web snapshot authoritative.
        self.assertEqual(reconciled.to_dict(), web.to_dict())

    def test_finder_includes_recently_updated_rollout_from_older_session_date(self) -> None:
        # Given: an active long-running session is stored under an older start date.
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
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            rollout_dir = Path(tmp) / "sessions" / "2026" / "01" / "01"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / "rollout-old-start.jsonl"
            rollout.write_text(json.dumps(event) + "\n", encoding="utf-8")
            claims = {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-local",
                    "chatgpt_plan_type": "pro",
                }
            }
            encoded_claims = base64.urlsafe_b64encode(
                json.dumps(claims).encode("utf-8")
            ).decode("ascii").rstrip("=")
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "account_id": "acct-local",
                            "id_token": f"header.{encoded_claims}.signature",
                        }
                    }
                ),
                encoding="utf-8",
            )
            event_time = datetime.fromisoformat(
                str(event["timestamp"]).replace("Z", "+00:00")
            )
            os.utime(auth_path, (event_time.timestamp() - 60, event_time.timestamp() - 60))

            # When: the Windows finder scans the Codex home.
            with patch("src.apps.codex_local_usage.os.name", "nt"):
                snapshot = find_latest_windows_codex_usage(tmp)

        # Then: session start date does not hide the latest runtime event.
        if snapshot is None:
            self.fail("active older-date rollout was not discovered")
        self.assertEqual(snapshot.weekly_limit, "95%")
        self.assertEqual(snapshot.account_id, "acct-local")
        self.assertEqual(snapshot.plan_type, "pro")

    def test_finder_does_not_attach_new_auth_identity_to_older_rollout(self) -> None:
        # Given: account B auth was written after account A's latest usage event.
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
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            rollout_dir = Path(tmp) / "sessions" / "2026" / "07" / "13"
            rollout_dir.mkdir(parents=True)
            (rollout_dir / "rollout-account-a.jsonl").write_text(
                json.dumps(event) + "\n",
                encoding="utf-8",
            )
            (Path(tmp) / "auth.json").write_text(
                json.dumps({"tokens": {"account_id": "acct-B"}}),
                encoding="utf-8",
            )

            # When: the finder observes the account switch before a new usage event.
            with patch("src.apps.codex_local_usage.os.name", "nt"):
                snapshot = find_latest_windows_codex_usage(tmp)

        # Then: old usage remains unbound and cannot overwrite account B's web value.
        if snapshot is None:
            self.fail("valid rollout usage was discarded")
        self.assertEqual(snapshot.weekly_limit, "95%")
        self.assertEqual(snapshot.account_id, "")

    def test_finder_rejects_auth_identity_when_event_plan_differs(self) -> None:
        # Given: a free-plan rollout conflicts with current pro-plan auth.
        event = {
            "timestamp": "2026-07-13T00:52:19.258Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "plan_type": "free",
                    "primary": {
                        "used_percent": 5.0,
                        "window_minutes": 10080,
                        "resets_at": 1784487672,
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            rollout_dir = Path(tmp) / "sessions" / "2026" / "07" / "13"
            rollout_dir.mkdir(parents=True)
            (rollout_dir / "rollout-free.jsonl").write_text(
                json.dumps(event) + "\n",
                encoding="utf-8",
            )
            claims = {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-local",
                    "chatgpt_plan_type": "pro",
                }
            }
            encoded_claims = base64.urlsafe_b64encode(
                json.dumps(claims).encode("utf-8")
            ).decode("ascii").rstrip("=")
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "account_id": "acct-local",
                            "id_token": f"header.{encoded_claims}.signature",
                        }
                    }
                ),
                encoding="utf-8",
            )
            event_time = datetime.fromisoformat(
                str(event["timestamp"]).replace("Z", "+00:00")
            )
            os.utime(auth_path, (event_time.timestamp() - 60, event_time.timestamp() - 60))

            # When: the finder validates event and auth identity as one contract.
            with patch("src.apps.codex_local_usage.os.name", "nt"):
                snapshot = find_latest_windows_codex_usage(tmp)

        # Then: plan mismatch leaves usage unbound despite matching account ID.
        if snapshot is None:
            self.fail("valid rollout usage was discarded")
        self.assertEqual(snapshot.plan_type, "free")
        self.assertEqual(snapshot.account_id, "")

    def test_finder_skips_rollout_that_disappears_during_scan(self) -> None:
        # Given: one candidate disappears while another valid rollout remains readable.
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
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            rollout_dir = Path(tmp) / "sessions" / "2026" / "07" / "13"
            rollout_dir.mkdir(parents=True)
            missing = rollout_dir / "rollout-missing.jsonl"
            missing.write_text("{}\n", encoding="utf-8")
            valid = rollout_dir / "rollout-valid.jsonl"
            valid.write_text(json.dumps(event) + "\n", encoding="utf-8")
            original_stat = Path.stat

            def flaky_stat(path: Path, *args: object, **kwargs: object) -> object:
                if path == missing:
                    raise FileNotFoundError(path)
                return original_stat(path, *args, **kwargs)

            # When: the bounded latest-rollout scan encounters the vanished file.
            with (
                patch("src.apps.codex_local_usage.os.name", "nt"),
                patch.object(Path, "stat", autospec=True, side_effect=flaky_stat),
            ):
                snapshot = find_latest_windows_codex_usage(tmp)

        # Then: the transient filesystem race does not discard other candidates.
        if snapshot is None:
            self.fail("valid rollout was discarded after another candidate vanished")
        self.assertEqual(snapshot.weekly_limit, "95%")
