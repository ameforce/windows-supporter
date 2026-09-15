from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.apps.ai_usage_contracts import AiUsageProvider, UsageState
from src.apps.codex_usage_browser_types import (
    BrowserErrorCode,
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    PlaywrightSessionConfig,
)
from src.apps.claude_usage_monitor import (
    CLAUDE_USAGE_PAGE_PROBE_SCRIPT,
    ClaudeUsageMonitor,
    parse_claude_usage_api_payload,
    parse_sanitized_claude_usage_text,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_SOURCE = REPO_ROOT / "src" / "apps" / "claude_usage_monitor.py"


API_USAGE_JSON = json.dumps(
    {
        "usage": {
            "five_hour": {
                "utilization": 35.0,
                "resets_at": "2026-09-15T05:00:00.000Z",
            },
            "seven_day": {
                "utilization": 14.0,
                "resets_at": "2026-09-19T09:00:00.000Z",
            },
            "seven_day_opus": {
                "utilization": 60.0,
                "resets_at": "2026-09-19T09:00:00.000Z",
            },
            "seven_day_sonnet": None,
            "extra_usage": {
                "is_enabled": True,
                "monthly_limit": 5000,
                "used_credits": 190,
                "utilization": 3.8,
                "currency": "USD",
            },
        },
        "organization": {"uuid": "org-uuid-1", "name": "Test Org"},
    }
)


class ClaudeUsageMonitorUnitTest(unittest.TestCase):
    class _Session:
        def __init__(self, results: list[BrowserOperationResult]) -> None:
            self.results = list(results)
            self.calls: list[str] = []
            self.status = BrowserRuntimeStatus(BrowserState.HEADLESS_READY, False, "")

        def collect(self) -> BrowserOperationResult:
            self.calls.append("collect")
            return self.results.pop(0)

        def open_login(self) -> BrowserOperationResult:
            self.calls.append("open_login")
            return self.results.pop(0)

        def poll_login(self) -> BrowserOperationResult:
            self.calls.append("poll_login")
            return self.results.pop(0)

        def close_session(self) -> None:
            self.calls.append("close_session")

        def shutdown(self) -> bool:
            self.calls.append("shutdown")
            return True

        def get_runtime_status(self) -> BrowserRuntimeStatus:
            return self.status

    @staticmethod
    def _probe(payload_json: str = API_USAGE_JSON, **extra: object) -> dict[str, object]:
        probe: dict[str, object] = {
            "url": "https://claude.ai/settings/usage",
            "mainText": "Current session 35% used",
            "accountId": "org-uuid-1",
            "metricBlocks": [
                {
                    "metric_key": "claude_usage_api",
                    "block_text": payload_json,
                }
            ],
        }
        probe.update(extra)
        return probe

    def test_api_payload_parser_reads_named_windows_and_extra_usage(self) -> None:
        parsed = parse_claude_usage_api_payload(API_USAGE_JSON)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.session_used_percent, 35.0)
        self.assertEqual(parsed.session_reset_at, "2026-09-15T05:00:00+00:00")
        self.assertEqual(parsed.weekly_used_percent, 14.0)
        self.assertEqual(parsed.weekly_reset_at, "2026-09-19T09:00:00+00:00")
        self.assertEqual(parsed.scoped_weekly_used_percent, 60.0)
        self.assertEqual(parsed.scoped_weekly_label, "Opus")
        self.assertTrue(parsed.extra_usage_enabled)
        self.assertEqual(parsed.extra_usage_text, "$1.9 / $50")

    def test_api_payload_parser_reads_limits_array_shape(self) -> None:
        payload = json.dumps(
            {
                "usage": {
                    "limits": [
                        {
                            "kind": "session",
                            "percent": 20.0,
                            "resets_at": "2026-09-15T05:00:00Z",
                        },
                        {
                            "kind": "weekly_all",
                            "percent": 30.0,
                            "resets_at": "2026-09-20T00:00:00Z",
                        },
                        {
                            "kind": "weekly_scoped",
                            "percent": 55.0,
                            "resets_at": "2026-09-20T00:00:00Z",
                            "scope": {"model": {"display_name": "Sonnet"}},
                        },
                    ],
                    "extra_usage": {"is_enabled": False},
                }
            }
        )

        parsed = parse_claude_usage_api_payload(payload)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.session_used_percent, 20.0)
        self.assertEqual(parsed.weekly_used_percent, 30.0)
        self.assertEqual(parsed.scoped_weekly_used_percent, 55.0)
        self.assertEqual(parsed.scoped_weekly_label, "Sonnet")
        self.assertFalse(parsed.extra_usage_enabled)

    def test_api_payload_parser_fails_closed_on_invalid_or_empty_payloads(self) -> None:
        self.assertIsNone(parse_claude_usage_api_payload(""))
        self.assertIsNone(parse_claude_usage_api_payload("not json"))
        self.assertIsNone(parse_claude_usage_api_payload("{}"))
        self.assertIsNone(parse_claude_usage_api_payload('{"usage": {}}'))
        self.assertIsNone(parse_claude_usage_api_payload('[1, 2]'))

    def test_api_payload_parser_clamps_over_limit_utilization(self) -> None:
        payload = json.dumps(
            {
                "usage": {
                    "five_hour": {
                        "utilization": 123.0,
                        "resets_at": "2026-09-15T05:00:00Z",
                    }
                }
            }
        )

        parsed = parse_claude_usage_api_payload(payload)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.session_used_percent, 100.0)
        self.assertEqual(parsed.session_reset_at, "2026-09-15T05:00:00+00:00")

    def test_api_payload_parser_preserves_zero_extra_usage_amounts(self) -> None:
        payload = json.dumps(
            {
                "usage": {
                    "five_hour": {"utilization": 10.0},
                    "extra_usage": {
                        "is_enabled": True,
                        "used_credits": 0,
                        "monthly_limit": 2000,
                        "currency": "USD",
                    },
                }
            }
        )

        parsed = parse_claude_usage_api_payload(payload)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.extra_usage_enabled)
        self.assertEqual(parsed.extra_usage_text, "$0 / $20")

    def test_dom_text_parser_reads_session_weekly_and_extra_usage(self) -> None:
        now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)  # Monday
        text = (
            "Usage\n"
            "Plan usage limits\n"
            "Current session\n"
            "35% used\n"
            "Resets Fri, 13:59\n"
            "Weekly limits\n"
            "All models\n"
            "14% used\n"
            "Resets Sep 19\n"
            "Opus\n"
            "60% used\n"
            "Resets Sep 19\n"
            "Extra usage\n"
            "$1.90 / $50.00"
        )

        parsed = parse_sanitized_claude_usage_text(text, now=now)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.session_used_percent, 35.0)
        # Friday 2026-09-18 13:59 UTC
        self.assertTrue(parsed.session_reset_at.startswith("2026-09-18T13:59"))
        self.assertEqual(parsed.weekly_used_percent, 14.0)
        self.assertEqual(parsed.weekly_reset_at, "2026-09-19")
        self.assertEqual(parsed.scoped_weekly_used_percent, 60.0)
        self.assertEqual(parsed.scoped_weekly_label, "Opus")
        self.assertTrue(parsed.extra_usage_enabled)
        self.assertEqual(parsed.extra_usage_text, "$1.90 / $50.00")

    def test_dom_text_parser_fails_closed_on_login_or_empty_text(self) -> None:
        self.assertIsNone(parse_sanitized_claude_usage_text(""))
        self.assertIsNone(parse_sanitized_claude_usage_text("Sign in to continue"))
        self.assertIsNone(
            parse_sanitized_claude_usage_text("Settings\nAppearance\nTheme")
        )

    def test_dom_text_parser_rejects_korean_login_gate(self) -> None:
        self.assertIsNone(
            parse_sanitized_claude_usage_text(
                "로그인이 필요합니다\nCurrent session\n40% used"
            )
        )

    def test_dom_reset_text_disambiguates_day_of_month_from_clock(self) -> None:
        now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)  # Monday
        text = "Current session\n35% used\nResets Fri, Nov 21 at 3pm"

        parsed = parse_sanitized_claude_usage_text(text, now=now)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        # Friday 2026-09-18 15:00, not 21:00 from the day-of-month token.
        self.assertTrue(parsed.session_reset_at.startswith("2026-09-18T15:00"))

    def test_dom_reset_text_reads_korean_meridiem_before_hour(self) -> None:
        now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)  # Monday
        text = "현재 세션\n35% 사용\n초기화 금요일 오후 3시"

        parsed = parse_sanitized_claude_usage_text(text, now=now)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        # 금요일 오후 3시 -> 15:00, not 03:00.
        self.assertTrue(parsed.session_reset_at.startswith("2026-09-18T15:00"))

    def test_dom_parser_ignores_unrelated_percent_after_weekly_section(self) -> None:
        now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
        text = (
            "Weekly limits\n"
            "All models\n"
            "Resets Fri\n"
            "Preferences\n"
            "50% discount"
        )

        self.assertIsNone(parse_sanitized_claude_usage_text(text, now=now))

    def test_collector_parses_api_payload_and_builds_snapshot(self) -> None:
        session = self._Session(
            [BrowserOperationResult(probe=self._probe())]
        )
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()

        self.assertEqual(reading.provider, AiUsageProvider.CLAUDE)
        self.assertEqual(reading.profile_id, "claude-personal")
        self.assertEqual(reading.state, UsageState.READY)
        self.assertEqual(reading.used_percent, 35.0)
        self.assertEqual(reading.remaining_percent, 65.0)
        self.assertTrue(reading.is_usable)
        self.assertTrue(reading.on_demand_enabled)
        self.assertEqual(reading.reset_at, "2026-09-15T05:00:00+00:00")

        snapshot = monitor.get_last_snapshot().to_dict()
        self.assertEqual(snapshot["provider"], "claude")
        self.assertEqual(snapshot["five_hour_limit"], "65%")
        self.assertEqual(snapshot["weekly_limit"], "86%")
        self.assertEqual(snapshot["weekly_scoped_limit"], "Opus 40%")
        self.assertEqual(snapshot["on_demand_status"], "ON · $1.9 / $50")
        self.assertEqual(
            [metric["key"] for metric in snapshot["metrics"]],
            ["five_hour_limit", "weekly_limit"],
        )
        five_hour_metric = snapshot["metrics"][0]
        self.assertEqual(five_hour_metric["percent"], 65.0)
        self.assertEqual(five_hour_metric["short_label"], "5H")
        self.assertEqual(five_hour_metric["state"], "ready")
        self.assertEqual(
            five_hour_metric["reset_at"], "2026-09-15T05:00:00+00:00"
        )

    def test_collector_falls_back_to_dom_summary_when_api_block_is_missing(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe={
                        "url": "https://claude.ai/settings/usage",
                        "mainText": "Current session\n35% used",
                        "metricBlocks": [
                            {
                                "metric_key": "claude_usage_summary",
                                "block_text": (
                                    "Current session\n35% used\nResets in 2 hr"
                                ),
                            }
                        ],
                    }
                )
            ]
        )
        now = [datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)]
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
            clock=lambda: now[0],
        )

        reading = monitor.collect()

        self.assertEqual(reading.state, UsageState.READY)
        self.assertEqual(reading.used_percent, 35.0)
        snapshot = monitor.get_last_snapshot().to_dict()
        self.assertEqual(snapshot["five_hour_limit"], "65%")
        self.assertTrue(snapshot["five_hour_limit_reset_at"].startswith("2026-09-14T12:00"))

    def test_logged_out_error_is_normalized_without_stale_value(self) -> None:
        session = self._Session([BrowserOperationResult(error="login_required")])
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()

        self.assertEqual(reading.state, UsageState.LOGGED_OUT)
        self.assertFalse(reading.is_usable)
        self.assertIn("로그인", reading.message)

    def test_cloudflare_challenge_is_not_reported_as_logged_out(self) -> None:
        session = self._Session(
            [BrowserOperationResult(error="cloudflare_challenge")]
        )
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()

        self.assertNotEqual(reading.state, UsageState.LOGGED_OUT)
        runtime = monitor.get_runtime_status()
        self.assertNotEqual(runtime["provider_status"], "login")
        self.assertEqual(runtime["provider_status"], "retrying")
        self.assertNotEqual(runtime["session_state"], "logged_out")
        self.assertNotEqual(runtime["monitor_state"], "paused_auth_required")

    def test_cloudflare_challenge_keeps_usable_cache_as_stale(self) -> None:
        now = [datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)]
        session = self._Session(
            [
                BrowserOperationResult(probe=self._probe()),
                BrowserOperationResult(error="cloudflare_challenge"),
            ]
        )
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
            clock=lambda: now[0],
        )
        first = monitor.collect()
        now[0] += timedelta(minutes=20)

        stale = monitor.collect(force=True)

        self.assertEqual(first.state, UsageState.READY)
        self.assertEqual(stale.state, UsageState.STALE)
        self.assertTrue(stale.is_usable)
        runtime = monitor.get_runtime_status()
        self.assertEqual(runtime["provider_status"], "stale")
        self.assertEqual(runtime["session_state"], "logged_in")
        self.assertEqual(runtime["monitor_state"], "idle")

    def test_profile_name_from_probe_is_reported_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            profile_dir = Path(tmp) / "profile"
            session = self._Session(
                [
                    BrowserOperationResult(
                        probe=self._probe(
                            profileName="테스트 사용자",
                            profileNameSource="account",
                        )
                    )
                ]
            )
            monitor = ClaudeUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: session,
            )

            monitor.collect()

            self.assertEqual(
                monitor.get_runtime_status()["profile_name"], "테스트 사용자"
            )
            state_payload = json.loads(
                (config_dir / "claude_usage_state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state_payload["profile_name"], "테스트 사용자")
            self.assertTrue(state_payload["profile_name_verified"])

            restored = ClaudeUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: self._Session([]),
            )
            self.assertEqual(
                restored.get_runtime_status()["profile_name"], "테스트 사용자"
            )

    def test_verified_profile_name_keeps_plan_suffix(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe=self._probe(
                        profileName="Devin Team",
                        profileNameSource="organization",
                    )
                )
            ]
        )
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
        )

        monitor.collect()

        self.assertEqual(
            monitor.get_runtime_status()["profile_name"], "Devin Team"
        )

    def test_verified_profile_name_survives_state_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            profile_dir = Path(tmp) / "profile"
            session = self._Session(
                [
                    BrowserOperationResult(
                        probe=self._probe(
                            profileName="Devin Team",
                            profileNameSource="organization",
                        )
                    )
                ]
            )
            monitor = ClaudeUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: session,
            )
            monitor.collect()

            restored = ClaudeUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: self._Session([]),
            )

            self.assertEqual(
                restored.get_runtime_status()["profile_name"], "Devin Team"
            )

    def test_dom_sourced_profile_name_is_still_label_filtered(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe=self._probe(
                        profileName="Account menu",
                        profileNameSource="dom",
                    )
                )
            ]
        )
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
        )

        monitor.collect()

        self.assertEqual(monitor.get_runtime_status()["profile_name"], "")

    def test_failure_preserves_last_success_as_stale_with_weekly_extras(self) -> None:
        now = [datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)]
        session = self._Session(
            [
                BrowserOperationResult(probe=self._probe()),
                BrowserOperationResult(error="command_timeout"),
            ]
        )
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
            clock=lambda: now[0],
        )
        first = monitor.collect()
        now[0] += timedelta(minutes=20)

        stale = monitor.collect(force=True)

        self.assertEqual(first.state, UsageState.READY)
        self.assertEqual(stale.state, UsageState.STALE)
        self.assertEqual(stale.used_percent, 35.0)
        self.assertEqual(stale.last_error_state, UsageState.TIMEOUT)
        snapshot = monitor.get_last_snapshot().to_dict()
        self.assertEqual(snapshot["weekly_limit"], "86%")
        self.assertEqual(snapshot["metrics"][0]["state"], "stale")

    def test_empty_payload_is_dom_drift(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe={
                        "url": "https://claude.ai/settings/usage",
                        "mainText": "Usage",
                        "metricBlocks": [
                            {
                                "metric_key": "claude_usage_api",
                                "block_text": '{"usage": {}}',
                            }
                        ],
                    }
                )
            ]
        )
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()

        self.assertEqual(reading.state, UsageState.DOM_DRIFT)
        self.assertFalse(reading.is_usable)

    def test_runtime_status_is_primitive_and_exposes_collection_mode(self) -> None:
        session = self._Session([])
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
        )
        runtime = monitor.get_runtime_status()

        self.assertEqual(runtime["provider"], "claude")
        self.assertEqual(runtime["profile_id"], "claude-personal")
        self.assertEqual(runtime["state"], "unknown")
        self.assertEqual(runtime["collection_mode"], "usage_page_api")
        self.assertIn("provider_status", runtime)
        self.assertIn("monitor_state", runtime)
        self.assertIn("session_state", runtime)
        self.assertIn("can_login", runtime)

    def test_multi_monitor_child_contract_is_available(self) -> None:
        session = self._Session([])
        monitor = ClaudeUsageMonitor(
            config_dir="C:/app/config",
            profile_dir="C:/app/profile/claude-2",
            browser_session_factory=lambda _config: session,
        )

        monitor.attach(object(), None, start_monitor=False)
        updated, error = monitor.update_settings(
            {"enabled": True, "interval_sec": 90, "tooltip_duration_ms": 100}
        )

        self.assertTrue(updated)
        self.assertIsNone(error)
        self.assertEqual(monitor.get_settings_snapshot()["interval_sec"], 300.0)
        self.assertEqual(monitor.get_settings_snapshot()["tooltip_duration_ms"], 1200)
        self.assertEqual(
            monitor.get_last_snapshot().reading.provider, AiUsageProvider.CLAUDE
        )
        self.assertTrue(callable(monitor.show_current_status))
        self.assertTrue(callable(monitor.release_profile_session))
        self.assertTrue(callable(monitor.format_captured_at_for_display))
        self.assertTrue(callable(monitor.format_reset_at_for_display))

    def test_manual_login_polls_until_usage_summary_is_ready(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(error="login_required"),
                BrowserOperationResult(probe=self._probe()),
            ]
        )
        session.status = BrowserRuntimeStatus(BrowserState.HEADED_LOGIN, True, "")
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
            login_poll_interval_sec=0.01,
            login_poll_max_attempts=3,
        )

        monitor.show_current_status(force_refresh=True, source="manual_login")
        deadline = time.monotonic() + 1.0
        while (
            monitor.get_last_snapshot().reading.state != UsageState.READY
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        self.assertEqual(monitor.get_last_snapshot().reading.state, UsageState.READY)
        self.assertEqual(session.calls, ["open_login", "poll_login"])
        monitor.shutdown()

    def test_manual_login_polls_when_open_login_is_rate_limited(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(error="rate_limited"),
                BrowserOperationResult(probe=self._probe()),
            ]
        )
        session.status = BrowserRuntimeStatus(BrowserState.HEADED_LOGIN, True, "")
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
            login_poll_interval_sec=0.01,
            login_poll_max_attempts=3,
        )

        monitor.show_current_status(force_refresh=True, source="manual_login")
        deadline = time.monotonic() + 1.0
        while (
            monitor.get_last_snapshot().reading.state != UsageState.READY
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        self.assertEqual(monitor.get_last_snapshot().reading.state, UsageState.READY)
        self.assertEqual(session.calls, ["open_login", "poll_login"])
        monitor.shutdown()

    def test_login_poll_backs_off_after_consecutive_rate_limited_results(self) -> None:
        session = self._Session(
            [BrowserOperationResult(error="rate_limited")] * 8
        )
        monitor = ClaudeUsageMonitor(
            profile_id="claude-personal",
            browser_session_factory=lambda _config: session,
            login_poll_interval_sec=10.0,
            login_poll_max_attempts=8,
        )
        waits: list[float] = []

        class _RecordingEvent(threading.Event):
            def wait(self, timeout: float | None = None) -> bool:
                waits.append(float(timeout or 0))
                return len(waits) >= 5

        monitor._login_poll_stop = _RecordingEvent()
        monitor._run_login_poll()

        self.assertEqual(waits, [10.0, 20.0, 30.0, 30.0, 30.0])
        self.assertEqual(session.calls, ["poll_login"] * 4)
        self.assertEqual(
            monitor.get_last_snapshot().reading.state, UsageState.RATE_LIMITED
        )

    def test_settings_and_last_success_cache_persist_provider_scoped_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            profile_dir = Path(tmp) / "profile"
            session = self._Session(
                [BrowserOperationResult(probe=self._probe())]
            )
            monitor = ClaudeUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: session,
            )

            monitor.update_settings({"interval_sec": 420, "tooltip_duration_ms": 2300})
            fresh = monitor.collect()

            state_path = config_dir / "claude_usage_state.json"
            settings_path = config_dir / "claude_usage_settings.json"
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state_payload["provider"], "claude")
            self.assertEqual(state_payload["session_used_percent"], 35.0)
            self.assertEqual(state_payload["weekly_used_percent"], 14.0)
            self.assertEqual(state_payload["scoped_weekly_label"], "Opus")
            self.assertEqual(state_payload["extra_usage_text"], "$1.9 / $50")

            restored = ClaudeUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: self._Session([]),
            )

            cached = restored.get_last_snapshot().to_dict()
            self.assertEqual(fresh.state, UsageState.READY)
            self.assertEqual(cached["state"], "stale")
            self.assertEqual(cached["five_hour_limit"], "65%")
            self.assertEqual(cached["weekly_limit"], "86%")
            self.assertEqual(cached["weekly_scoped_limit"], "Opus 40%")
            self.assertEqual(restored.get_settings_snapshot()["interval_sec"], 420.0)
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8"))["provider"],
                "claude",
            )

    def test_release_removes_only_managed_profile_and_cached_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            profile_dir = Path(tmp) / "windows-supporter" / "claude-profile-account-1"
            profile_dir.mkdir(parents=True)
            (profile_dir / "marker.txt").write_text("managed", encoding="utf-8")
            session = self._Session(
                [BrowserOperationResult(probe=self._probe())]
            )
            monitor = ClaudeUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: session,
            )
            monitor.collect()

            ok, _message = monitor.release_profile_session()

            self.assertTrue(ok)
            self.assertFalse(profile_dir.exists())
            self.assertFalse((config_dir / "claude_usage_state.json").exists())
            self.assertEqual(
                monitor.get_last_snapshot().reading.state, UsageState.LOGGED_OUT
            )
            self.assertEqual(monitor.get_runtime_status()["session_state"], "logged_out")

    def test_release_removes_dynamic_app_owned_profile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            profile_id = f"profile_{'b' * 32}"
            profile_dir = (
                Path(tmp)
                / "windows-supporter"
                / "ai-profiles"
                / profile_id
                / "claude"
            )
            profile_dir.mkdir(parents=True)
            (profile_dir / "marker.txt").write_text("managed", encoding="utf-8")
            monitor = ClaudeUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                profile_id=profile_id,
                browser_session_factory=lambda _config: self._Session([]),
            )

            ok, message = monitor.release_profile_session()

            self.assertTrue(ok, message)
            self.assertFalse(profile_dir.exists())

    def test_release_rejects_profile_outside_windows_supporter_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "external-browser-profile"
            profile_dir.mkdir()
            marker = profile_dir / "marker.txt"
            marker.write_text("preserve", encoding="utf-8")
            monitor = ClaudeUsageMonitor(
                config_dir=str(Path(tmp) / "config"),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: self._Session([]),
            )

            ok, message = monitor.release_profile_session()

            self.assertFalse(ok)
            self.assertIn("전용 프로필", message)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_default_profile_is_owned_by_windows_supporter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"APPDATA": tmp}):
                monitor = ClaudeUsageMonitor(profile_id="claude-personal")

        profile_dir = Path(monitor.profile_dir)
        self.assertEqual(profile_dir.parent.parent, Path(tmp) / "windows-supporter")
        self.assertEqual(profile_dir.parent.name, "claude-usage-profiles")
        self.assertEqual(profile_dir.name, "claude-personal")

    def test_module_uses_no_http_client_and_probe_stays_on_same_origin(self) -> None:
        tree = ast.parse(MONITOR_SOURCE.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        string_literals: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                string_literals.append(node.value.lower())

        self.assertTrue(
            imported_roots.isdisjoint(
                {
                    "http",
                    "requests",
                    "selenium",
                    "socket",
                    "urllib",
                }
            )
        )
        # The probe must only call same-origin, first-party claude.ai session
        # endpoints; no absolute URLs or credential material may be fetched.
        self.assertNotIn("https://api.anthropic.com", " ".join(string_literals))
        self.assertNotIn("sessionkey", " ".join(string_literals))
        self.assertNotIn("localstorage", " ".join(string_literals))

    def test_probe_contract_uses_session_api_with_dom_fallback(self) -> None:
        lowered = CLAUDE_USAGE_PAGE_PROBE_SCRIPT.lower()

        self.assertIn("/api/organizations", lowered)
        self.assertIn("credentials", lowered)
        self.assertIn("claude_usage_api", lowered)
        self.assertIn("claude_usage_summary", lowered)
        self.assertIn("claude_auth_required", lowered)
        self.assertIn("claude_rate_limited", lowered)
        self.assertIn("claude_cf_challenge", lowered)
        self.assertIn("cf-mitigated", lowered)
        self.assertIn("/api/account", lowered)
        self.assertIn("lastactiveorg", lowered)
        self.assertIn("collectprofilename", lowered)
        self.assertNotIn("localstorage", lowered)
        self.assertNotIn("sessionkey", lowered)


if __name__ == "__main__":
    unittest.main()
