from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
from src.apps.cursor_usage_monitor import (
    CursorUsageMonitor,
    CURSOR_USAGE_PAGE_PROBE_SCRIPT,
    _LazyCursorBrowserSession,
    parse_sanitized_cursor_usage_text,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_SOURCE = REPO_ROOT / "src" / "apps" / "cursor_usage_monitor.py"


class CursorUsageMonitorUnitTest(unittest.TestCase):
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

    def test_lazy_session_cancels_inner_created_after_terminal_request(self) -> None:
        constructor_started = threading.Event()
        release_constructor = threading.Event()

        class _InnerSession:
            def __init__(self) -> None:
                self.calls = []

            def collect(self) -> BrowserOperationResult:
                self.calls.append("collect")
                return BrowserOperationResult(probe={"url": "usage"})

            def request_cancel(self) -> bool:
                self.calls.append("request_cancel")
                return True

            def shutdown(self) -> bool:
                self.calls.append("shutdown")
                return True

            def get_runtime_status(self) -> BrowserRuntimeStatus:
                return BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

        inner = _InnerSession()

        def blocking_constructor(*_args, **_kwargs):
            constructor_started.set()
            release_constructor.wait(2.0)
            return inner

        lazy = _LazyCursorBrowserSession(
            PlaywrightSessionConfig(
                profile_dir="profile",
                usage_url="https://cursor.com/dashboard/usage",
                probe_script="probe()",
            ),
            None,
        )
        results = []
        collect_thread = threading.Thread(
            target=lambda: results.append(lazy.collect()),
            daemon=True,
        )

        with patch(
            "src.apps.codex_usage_playwright_session.CodexUsagePlaywrightSession",
            side_effect=blocking_constructor,
        ):
            collect_thread.start()
            self.assertTrue(constructor_started.wait(1.0))

            self.assertFalse(lazy.request_cancel())
            self.assertFalse(lazy.shutdown())
            release_constructor.set()
            collect_thread.join(1.0)

        self.assertFalse(collect_thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].error, BrowserErrorCode.COLLECT_FAILED.value)
        self.assertEqual(inner.calls, ["request_cancel", "shutdown"])
        self.assertTrue(lazy.shutdown())

    def test_lazy_session_retains_failed_terminal_cleanup_for_shutdown_retry(self) -> None:
        constructor_started = threading.Event()
        release_constructor = threading.Event()

        class _RetryableInnerSession:
            def __init__(self) -> None:
                self.calls = []
                self.shutdown_results = [False, True]

            def collect(self) -> BrowserOperationResult:
                self.calls.append("collect")
                return BrowserOperationResult()

            def request_cancel(self) -> bool:
                self.calls.append("request_cancel")
                return False

            def shutdown(self) -> bool:
                self.calls.append("shutdown")
                return self.shutdown_results.pop(0)

        inner = _RetryableInnerSession()

        def blocking_constructor(*_args, **_kwargs):
            constructor_started.set()
            release_constructor.wait(2.0)
            return inner

        lazy = _LazyCursorBrowserSession(
            PlaywrightSessionConfig(
                profile_dir="profile",
                usage_url="https://cursor.com/dashboard/usage",
                probe_script="probe()",
            ),
            None,
        )
        result = []
        collect_thread = threading.Thread(
            target=lambda: result.append(lazy.collect()),
            daemon=True,
        )

        with patch(
            "src.apps.codex_usage_playwright_session.CodexUsagePlaywrightSession",
            side_effect=blocking_constructor,
        ):
            collect_thread.start()
            self.assertTrue(constructor_started.wait(1.0))
            self.assertFalse(lazy.shutdown())
            release_constructor.set()
            collect_thread.join(1.0)

        self.assertFalse(collect_thread.is_alive())
        self.assertEqual(result[0].error, BrowserErrorCode.COLLECT_FAILED.value)
        self.assertTrue(lazy.shutdown())
        self.assertEqual(inner.calls, ["request_cancel", "shutdown", "shutdown"])

    @staticmethod
    def _probe(text: str) -> dict[str, object]:
        return {
            "url": "https://cursor.com/dashboard/usage",
            "mainText": text,
            "metricBlocks": [
                {
                    "metric_key": "cursor_account_summary",
                    "block_text": text,
                }
            ],
        }

    def test_collector_parses_visible_account_summary(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe=self._probe(
                        "Included usage: $12.50 / $20.00\n"
                        "Reset: 2026-08-01T00:00:00Z\n"
                        "On-demand usage: ON"
                    )
                )
            ]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()

        self.assertEqual(reading.provider, AiUsageProvider.CURSOR)
        self.assertEqual(reading.profile_id, "cursor-personal")
        self.assertEqual(reading.state, UsageState.READY)
        self.assertEqual(reading.used_percent, 62.5)
        self.assertEqual(reading.remaining_percent, 37.5)
        self.assertTrue(reading.is_usable)
        self.assertTrue(reading.on_demand_enabled)
        self.assertEqual(reading.reset_at, "2026-08-01T00:00:00Z")
        self.assertEqual(reading.to_dict()["included_usage"], "$12.50 / $20.00")

    def test_collector_normalizes_korean_date_reset_at_provider_boundary(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe=self._probe(
                        "Included usage: US$0 / US$20\n"
                        "Reset: 2026년 8월 13일\n"
                        "On-demand usage: OFF"
                    )
                )
            ]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()

        self.assertEqual(reading.reset_at, "2026-08-13")
        self.assertEqual(reading.reset_precision, "date")
        self.assertFalse(reading.on_demand_enabled)
        self.assertEqual(
            monitor.format_reset_at_for_display(reading.reset_at, "billing_reset_at"),
            "2026-08-13",
        )

    def test_low_frequency_collection_reuses_fresh_reading(self) -> None:
        now = [datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)]
        session = self._Session(
            [
                BrowserOperationResult(
                    probe=self._probe("Included usage: 5 / 20\nOn-demand usage: OFF")
                )
            ]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
            refresh_interval_sec=600,
            clock=lambda: now[0],
        )

        first = monitor.collect()
        now[0] += timedelta(minutes=5)
        second = monitor.collect()

        self.assertIs(first, second)
        self.assertEqual(session.calls, ["collect"])

    def test_auto_monitor_force_flag_still_respects_cursor_throttle(self) -> None:
        now = [datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)]
        session = self._Session(
            [BrowserOperationResult(probe=self._probe("Included usage: 5 / 20"))]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
            refresh_interval_sec=300,
            clock=lambda: now[0],
        )

        monitor.show_current_status(force_refresh=True, source="auto_monitor")
        now[0] += timedelta(seconds=90)
        monitor.show_current_status(force_refresh=True, source="auto_monitor")

        self.assertEqual(session.calls, ["collect"])

    def test_manual_login_polls_until_visible_summary_is_ready(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(error="login_required"),
                BrowserOperationResult(
                    probe=self._probe("Your included usage\nUS$0\n/ US$20")
                ),
            ]
        )
        session.status = BrowserRuntimeStatus(BrowserState.HEADED_LOGIN, True, "")
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
            login_poll_interval_sec=0.01,
            login_poll_max_attempts=3,
        )

        monitor.show_current_status(force_refresh=True, source="manual_login")
        deadline = time.monotonic() + 1.0
        while monitor.get_last_snapshot().state != UsageState.READY and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(monitor.get_last_snapshot().state, UsageState.READY)
        self.assertEqual(session.calls, ["open_login", "poll_login"])
        monitor.shutdown()

    def test_module_uses_no_http_client_or_internal_cursor_endpoint(self) -> None:
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
        self.assertFalse(any("api.cursor" in value for value in string_literals))
        self.assertFalse(any("filtered-usage-events" in value for value in string_literals))
        self.assertFalse(any("events table" in value for value in string_literals))

    def test_probe_contract_reads_visible_summary_and_excludes_event_rows(self) -> None:
        lowered = CURSOR_USAGE_PAGE_PROBE_SCRIPT.lower()

        self.assertIn("offsetparent", lowered)
        self.assertIn("cursor_account_summary", lowered)
        self.assertIn("your included usage", lowered)
        self.assertIn("on-demand usage", lowered)
        self.assertIn("main div", lowered)
        self.assertIn("matches.length === 1", lowered)
        self.assertIn("table", lowered)
        self.assertIn("role=\"row\"", lowered)
        self.assertIn("current.queryselector", lowered)
        self.assertIn("usage events", lowered)
        self.assertIn("hasreset", lowered)
        self.assertIn("fallback", lowered)
        self.assertNotIn("document.cookie", lowered)
        self.assertNotIn("localstorage", lowered)
        self.assertNotIn("fetch(", lowered)

    def test_default_profile_is_owned_by_windows_supporter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"APPDATA": tmp}):
                monitor = CursorUsageMonitor(profile_id="cursor-personal")

        profile_dir = Path(monitor.profile_dir)
        self.assertEqual(profile_dir.parent.parent, Path(tmp) / "windows-supporter")
        self.assertEqual(profile_dir.parent.name, "cursor-usage-profiles")
        self.assertEqual(profile_dir.name, "cursor-personal")

    def test_sanitized_text_fixture_parser_extracts_included_usage_only(self) -> None:
        fixture = """
        Account: user@example.invalid
        Included usage: $12.50 / $20.00
        Usage: 62.5% used
        On-demand usage: $3.00 / $15.00
        Billing cycle: 2026-07-01T00:00:00Z -> 2026-08-01T00:00:00Z
        """

        parsed = parse_sanitized_cursor_usage_text(fixture)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.included_used, Decimal("12.50"))
        self.assertEqual(parsed.included_limit, Decimal("20.00"))
        self.assertEqual(parsed.used_percent, 62.5)
        self.assertEqual(parsed.remaining_percent, 37.5)
        self.assertEqual(parsed.reset_at, "2026-08-01T00:00:00Z")
        self.assertTrue(parsed.on_demand_enabled)

    def test_observed_cursor_summary_cards_parse_zero_and_korean_reset_date(self) -> None:
        fixture = """
        Your included usage
        US$0
        / US$20
        Resets 2026년 8월 13일
        On-Demand Usage
        Off
        Pay for extra usage beyond your plan limits.
        Off
        """

        parsed = parse_sanitized_cursor_usage_text(fixture)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.included_used, Decimal("0"))
        self.assertEqual(parsed.included_limit, Decimal("20"))
        self.assertEqual(parsed.included_used_display, "US$0")
        self.assertEqual(parsed.included_limit_display, "US$20")
        self.assertEqual(parsed.used_percent, 0.0)
        self.assertEqual(parsed.remaining_percent, 100.0)
        self.assertEqual(parsed.reset_at, "2026년 8월 13일")
        self.assertFalse(parsed.on_demand_enabled)

    def test_sanitized_text_fixture_parser_understands_remaining_percent(self) -> None:
        fixture = """
        포함 사용량: 5 / 20
        사용량: 75% remaining
        """

        parsed = parse_sanitized_cursor_usage_text(fixture)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.used_percent, 25.0)
        self.assertEqual(parsed.remaining_percent, 75.0)
        self.assertIsNone(parsed.reset_at)
        self.assertIsNone(parsed.on_demand_enabled)

    def test_sanitized_text_fixture_parser_returns_none_fields_when_values_are_absent(self) -> None:
        parsed = parse_sanitized_cursor_usage_text(
            "Included usage: unavailable\nReset: -\nOn-demand usage: unknown"
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNone(parsed.included_used)
        self.assertIsNone(parsed.included_limit)
        self.assertIsNone(parsed.used_percent)
        self.assertIsNone(parsed.remaining_percent)
        self.assertIsNone(parsed.reset_at)
        self.assertIsNone(parsed.on_demand_enabled)

    def test_sanitized_text_fixture_parser_fails_closed_on_inconsistent_or_non_summary_text(self) -> None:
        self.assertIsNone(parse_sanitized_cursor_usage_text("Sign in to continue"))
        self.assertIsNone(
            parse_sanitized_cursor_usage_text(
                "Included usage: 5 / 20\nUsage: 80% used"
            )
        )
        self.assertIsNone(
            parse_sanitized_cursor_usage_text(
                "On-demand usage: 5 / 20\nUsage: 25% used"
            )
        )

    def test_logged_out_error_is_normalized_without_stale_value(self) -> None:
        session = self._Session([BrowserOperationResult(error="login_required")])
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()

        self.assertEqual(reading.state, UsageState.LOGGED_OUT)
        self.assertFalse(reading.is_usable)
        self.assertIn("로그인", reading.message)

    def test_failure_preserves_last_success_as_stale(self) -> None:
        now = [datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)]
        session = self._Session(
            [
                BrowserOperationResult(
                    probe=self._probe("Included usage: 5 / 20\nOn-demand usage: OFF")
                ),
                BrowserOperationResult(error="command_timeout"),
            ]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
            clock=lambda: now[0],
        )
        first = monitor.collect()
        now[0] += timedelta(minutes=20)

        stale = monitor.collect(force=True)

        self.assertEqual(first.state, UsageState.READY)
        self.assertEqual(stale.state, UsageState.STALE)
        self.assertEqual(stale.used_percent, 25.0)
        self.assertEqual(stale.last_error_state, UsageState.TIMEOUT)
        self.assertTrue(stale.is_usable)

    def test_logged_out_failure_keeps_stale_value_but_requires_reconnection(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe=self._probe("Included usage: 5 / 20\nOn-demand usage: OFF")
                ),
                BrowserOperationResult(error="login_required"),
            ]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )
        monitor.collect()

        stale = monitor.collect(force=True)
        runtime = monitor.get_runtime_status()

        self.assertEqual(stale.state, UsageState.STALE)
        self.assertEqual(stale.last_error_state, UsageState.LOGGED_OUT)
        self.assertEqual(runtime["provider_status"], "login")
        self.assertEqual(runtime["freshness"], "stale")
        self.assertTrue(runtime["last_snapshot_is_stale"])
        self.assertEqual(runtime["session_state"], "logged_out")
        self.assertEqual(runtime["monitor_state"], "paused_auth_required")
        self.assertTrue(runtime["can_login"])

    def test_rate_limit_is_explicit_and_preserves_prior_success_as_stale(self) -> None:
        session = self._Session([BrowserOperationResult(error="rate_limited")])
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )

        limited = monitor.collect()

        self.assertEqual(limited.state, UsageState.RATE_LIMITED)
        self.assertIsNone(limited.used_percent)
        limited_runtime = monitor.get_runtime_status()
        self.assertEqual(limited_runtime["provider_status"], "rate_limited")
        self.assertEqual(limited_runtime["freshness"], "unavailable")

        success_then_limit = self._Session(
            [
                BrowserOperationResult(probe=self._probe("Included usage: 5 / 20")),
                BrowserOperationResult(error="rate_limited"),
            ]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: success_then_limit,
        )
        monitor.collect()

        stale = monitor.collect(force=True)

        self.assertEqual(stale.state, UsageState.STALE)
        self.assertEqual(stale.last_error_state, UsageState.RATE_LIMITED)
        self.assertEqual(stale.used_percent, 25.0)
        stale_runtime = monitor.get_runtime_status()
        self.assertEqual(stale_runtime["provider_status"], "rate_limited")
        self.assertEqual(stale_runtime["freshness"], "stale")

    def test_inflight_refresh_keeps_cached_cursor_session_logged_in(self) -> None:
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: self._Session(
                [BrowserOperationResult(probe=self._probe("Included usage: 5 / 20"))]
            ),
        )
        monitor.collect(force=True)
        monitor._collect_inflight = True

        runtime = monitor.get_runtime_status()

        self.assertEqual(runtime["provider_status"], "ready")
        self.assertEqual(runtime["freshness"], "fresh")
        self.assertEqual(runtime["session_state"], "logged_in")
        self.assertFalse(runtime["can_login"])

    def test_empty_summary_is_dom_drift(self) -> None:
        session = self._Session(
            [BrowserOperationResult(probe=self._probe("Usage dashboard"))]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()

        self.assertEqual(reading.state, UsageState.DOM_DRIFT)
        self.assertFalse(reading.is_usable)

    def test_runtime_status_is_primitive_and_exposes_collection_mode(self) -> None:
        session = self._Session([])
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )
        runtime = monitor.get_runtime_status()

        self.assertEqual(runtime["provider"], "cursor")
        self.assertEqual(runtime["profile_id"], "cursor-personal")
        self.assertEqual(runtime["state"], "unknown")
        self.assertEqual(runtime["message"], "조회 상태를 확인할 수 없습니다.")
        self.assertEqual(runtime["collection_mode"], "visible_dashboard_summary")
        self.assertEqual(runtime["browser_state"], "headless_ready")
        self.assertEqual(runtime["browser_last_error"], "")
        self.assertFalse(runtime["login_window_open"])
        self.assertIn("provider_status", runtime)
        self.assertIn("monitor_state", runtime)
        self.assertIn("session_state", runtime)
        self.assertIn("freshness", runtime)

    def test_multi_monitor_child_contract_is_available(self) -> None:
        session = self._Session([])
        monitor = CursorUsageMonitor(
            config_dir="C:/app/config",
            profile_dir="C:/app/profile/cursor-2",
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
        self.assertEqual(monitor.get_last_snapshot().provider, AiUsageProvider.CURSOR)
        self.assertTrue(callable(monitor.show_current_status))
        self.assertTrue(callable(monitor.release_profile_session))
        self.assertTrue(callable(monitor.format_captured_at_for_display))
        self.assertTrue(callable(monitor.format_reset_at_for_display))

    def test_settings_and_last_success_cache_persist_without_probe_text_or_pii(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            profile_dir = Path(tmp) / "profile"
            session = self._Session(
                [
                    BrowserOperationResult(
                        probe=self._probe(
                            "Account: private@example.invalid\n"
                            "Your included usage\nUS$0\n/ US$20\n"
                            "Resets 2026년 8월 13일\n"
                            "On-Demand Usage\nOff"
                        )
                    )
                ]
            )
            monitor = CursorUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: session,
            )

            monitor.update_settings({"interval_sec": 420, "tooltip_duration_ms": 2300})
            fresh = monitor.collect()

            state_path = config_dir / "cursor_usage_state.json"
            settings_path = config_dir / "cursor_usage_settings.json"
            state_text = state_path.read_text(encoding="utf-8")
            self.assertNotIn("private@example.invalid", state_text)
            self.assertNotIn("probe", state_text.lower())
            self.assertNotIn("cookie", state_text.lower())
            state_payload = json.loads(state_text)
            self.assertEqual(state_payload["included_used"], "US$0")
            self.assertEqual(state_payload["reset_at"], "2026-08-13")
            self.assertEqual(state_payload["reset_precision"], "date")

            restored = CursorUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: self._Session([]),
            )

            cached = restored.get_last_snapshot()
            self.assertEqual(fresh.state, UsageState.READY)
            self.assertEqual(cached.state, UsageState.STALE)
            self.assertEqual(cached.to_dict()["included_usage"], "US$0 / US$20")
            self.assertEqual(cached.reset_precision, "date")
            self.assertEqual(restored.get_settings_snapshot()["interval_sec"], 420.0)
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["provider"], "cursor")

    def test_release_removes_only_managed_profile_and_cached_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            profile_dir = Path(tmp) / "windows-supporter" / "cursor-profile-account-1"
            profile_dir.mkdir(parents=True)
            (profile_dir / "marker.txt").write_text("managed", encoding="utf-8")
            session = self._Session(
                [
                    BrowserOperationResult(
                        probe=self._probe("Included usage: 5 / 20\nOn-demand usage: OFF")
                    )
                ]
            )
            monitor = CursorUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: session,
            )
            monitor.collect()

            ok, _message = monitor.release_profile_session()

            self.assertTrue(ok)
            self.assertFalse(profile_dir.exists())
            self.assertFalse((config_dir / "cursor_usage_state.json").exists())
            self.assertEqual(monitor.get_last_snapshot().state, UsageState.LOGGED_OUT)
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
                / "cursor"
            )
            profile_dir.mkdir(parents=True)
            (profile_dir / "marker.txt").write_text("managed", encoding="utf-8")
            monitor = CursorUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                profile_id=profile_id,
                browser_session_factory=lambda _config: self._Session([]),
            )

            ok, message = monitor.release_profile_session()

            self.assertTrue(ok, message)
            self.assertFalse(profile_dir.exists())

    def test_release_rejects_dynamic_profile_resolving_outside_app_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            profile_id = f"profile_{'d' * 32}"
            profile_dir = (
                Path(tmp)
                / "windows-supporter"
                / "ai-profiles"
                / profile_id
                / "cursor"
            )
            profile_dir.mkdir(parents=True)
            marker = profile_dir / "preserve.txt"
            marker.write_text("outside-backed", encoding="utf-8")
            outside = Path(tmp) / "outside" / "cursor"
            real_realpath = os.path.realpath

            def junction_realpath(path):
                if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
                    os.path.abspath(profile_dir)
                ):
                    return str(outside)
                return real_realpath(path)

            monitor = CursorUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(profile_dir),
                profile_id=profile_id,
                browser_session_factory=lambda _config: self._Session([]),
            )

            with patch(
                "src.apps.cursor_usage_monitor.os.path.realpath",
                side_effect=junction_realpath,
            ):
                ok, _message = monitor.release_profile_session()

            self.assertFalse(ok)
            self.assertTrue(marker.is_file())

    def test_release_rejects_profile_outside_windows_supporter_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "external-browser-profile"
            profile_dir.mkdir()
            marker = profile_dir / "marker.txt"
            marker.write_text("preserve", encoding="utf-8")
            monitor = CursorUsageMonitor(
                config_dir=str(Path(tmp) / "config"),
                profile_dir=str(profile_dir),
                browser_session_factory=lambda _config: self._Session([]),
            )

            ok, message = monitor.release_profile_session()

            self.assertFalse(ok)
            self.assertIn("전용 프로필", message)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")


    def test_runtime_projects_transient_auth_paused_and_terminal_failures_by_cache_boundary(self) -> None:
        transient = CursorUsageMonitor(
            profile_id="cursor-transient",
            browser_session_factory=lambda _config: self._Session(
                [BrowserOperationResult(error="command_timeout") for _ in range(3)]
            ),
        )

        transient.collect(force=True)
        retrying = transient.get_runtime_status()
        self.assertEqual(retrying["provider_status"], "retrying")
        self.assertEqual(retrying["failure_count"], 1)
        self.assertEqual(retrying["last_error_type"], "timeout")
        self.assertFalse(retrying["retry_exhausted"])

        transient.collect(force=True)
        transient.collect(force=True)
        exhausted = transient.get_runtime_status()
        self.assertEqual(exhausted["provider_status"], "error")
        self.assertTrue(exhausted["retry_exhausted"])

        cached = CursorUsageMonitor(
            profile_id="cursor-cached",
            browser_session_factory=lambda _config: self._Session(
                [
                    BrowserOperationResult(probe=self._probe("Included usage: 5 / 20")),
                    BrowserOperationResult(error="command_timeout"),
                ]
            ),
        )
        cached.collect(force=True)
        cached.collect(force=True)
        self.assertEqual(cached.get_runtime_status()["provider_status"], "stale")

        auth = CursorUsageMonitor(
            profile_id="cursor-auth",
            browser_session_factory=lambda _config: self._Session(
                [BrowserOperationResult(error="cloudflare_challenge")]
            ),
        )
        auth.collect(force=True)
        self.assertEqual(auth.get_runtime_status()["provider_status"], "login")

        paused = CursorUsageMonitor(
            profile_id="cursor-paused",
            browser_session_factory=lambda _config: self._Session(
                [BrowserOperationResult(error="profile_in_use")]
            ),
        )
        paused.collect(force=True)
        self.assertEqual(paused.get_runtime_status()["provider_status"], "paused")

        terminal = CursorUsageMonitor(
            profile_id="cursor-terminal",
            browser_session_factory=lambda _config: self._Session(
                [BrowserOperationResult(probe=self._probe("Usage dashboard"))]
            ),
        )
        terminal.collect(force=True)
        self.assertEqual(terminal.get_runtime_status()["provider_status"], "error")

    def test_cursor_failure_writes_structured_jsonl_without_probe_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            monitor = CursorUsageMonitor(
                config_dir=str(config_dir),
                profile_dir=str(Path(tmp) / "profile"),
                profile_id="cursor-structured",
                browser_session_factory=lambda _config: self._Session(
                    [BrowserOperationResult(error="command_timeout")]
                ),
            )

            monitor.collect(force=True)

            log_path = config_dir / "cursor_usage_events.jsonl"
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["provider"], "cursor")
            self.assertEqual(records[0]["profile_id"], "cursor-structured")
            self.assertEqual(records[0]["error_type"], "timeout")
            self.assertEqual(records[0]["failure_count"], 1)
            self.assertNotIn("probe", records[0])

    def test_probe_contract_collects_visible_profile_name(self) -> None:
        lowered = CURSOR_USAGE_PAGE_PROBE_SCRIPT.lower()

        self.assertIn("profilename", lowered)
        self.assertIn("collectprofilename", lowered)
        self.assertIn("aria-label", lowered)
        self.assertIn("genericlabel", lowered)
        self.assertIn("broadmenu", lowered)
        self.assertIn("nearbynames", lowered)
        self.assertIn("imagenames", lowered)
        self.assertIn("resolvelabelledby", lowered)
        self.assertIn('[data-testid*="user" i]'.lower(), lowered)
        self.assertIn(r"(?:account|profile|user)\s+menu", lowered)
        self.assertIn(r"(?:my|your|edit|switch|view|open)\s+(?:account|profile)", lowered)
        # Uncued header menus must be skipped; only identity-cued nodes harvest names.
        self.assertRegex(
            CURSOR_USAGE_PAGE_PROBE_SCRIPT,
            r"if\s*\(\s*broadMenu\s*&&\s*!hasCue\s*\)\s*continue\s*;",
        )
        self.assertNotIn("document.cookie", lowered)
        self.assertNotIn("localstorage", lowered)
        self.assertNotIn("fetch(", lowered)

    def _evaluate_probe_on_html(self, html: str) -> dict[str, object]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = None
            last_error: Exception | None = None
            for kwargs in (
                {"channel": "chrome", "headless": True},
                {"headless": True},
            ):
                try:
                    browser = playwright.chromium.launch(**kwargs)
                    break
                except Exception as exc:  # pragma: no cover - environment dependent
                    last_error = exc
            if browser is None:
                self.skipTest(f"playwright browser unavailable: {last_error}")
            try:
                page = browser.new_page()
                page.set_content(html)
                probe = page.evaluate(CURSOR_USAGE_PAGE_PROBE_SCRIPT)
            finally:
                browser.close()
        self.assertIsInstance(probe, dict)
        return probe

    @staticmethod
    def _usage_summary_html(*, identity_html: str) -> str:
        # Cursor mounts the dashboard sidebar inside main; fixtures must too.
        return f"""<!doctype html>
<html><body>
<main role="main">
  <aside>{identity_html}</aside>
  <div><span>Your included usage</span>
    <div>US$4.00
/ US$20.00
Billing cycle
Resets Aug 13, 2026
</div>
  </div>
  <div><span>On-Demand Usage</span>
    <div>OFF</div>
  </div>
  <button type="button" aria-label="Usage events for all users">Showing token usage</button>
</main>
</body></html>"""

    def test_probe_harvests_nearby_name_from_user_menu_chrome(self) -> None:
        # Red: Cursor dashboard exposes "User menu" chrome without the display
        # name in aria-label; the visible name sits next to the identity control.
        probe = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<div class="account-chip">'
                    '<div><img src="about:blank" alt="종인 김" width="28" height="28" /></div>'
                    '<div><div><span>종인 김</span></div>'
                    '<div><span>Team Plan</span></div></div>'
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu">'
                    "<span></span>"
                    "</button>"
                    "</div>"
                )
            )
        )

        self.assertEqual(probe.get("profileName"), "종인 김")
        blocks = probe.get("metricBlocks") or []
        self.assertTrue(
            any(
                isinstance(block, dict)
                and block.get("metric_key") == "cursor_account_summary"
                for block in blocks
            )
        )

    def test_probe_harvests_img_alt_from_user_menu_anchor(self) -> None:
        probe = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu" data-testid="user-menu">'
                    '<img src="about:blank" alt="Bob Builder" width="24" height="24" />'
                    "</button>"
                )
            )
        )

        self.assertEqual(probe.get("profileName"), "Bob Builder")

    def test_probe_harvests_aria_labelledby_and_data_display_name(self) -> None:
        labelled = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<span id="cursor-user-label">Carol Danvers</span>'
                    '<button type="button" data-testid="account-trigger" '
                    'aria-labelledby="cursor-user-label"></button>'
                )
            )
        )
        self.assertEqual(labelled.get("profileName"), "Carol Danvers")

        data_named = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<button type="button" data-testid="accountMenu" '
                    'aria-haspopup="menu" '
                    'data-display-name="Dana Scully"></button>'
                )
            )
        )
        self.assertEqual(data_named.get("profileName"), "Dana Scully")

    def test_probe_ignores_uncued_menu_and_email_only_identity(self) -> None:
        probe = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<button type="button" aria-label="Notifications" '
                    'aria-haspopup="menu"><span>Secret Name</span></button>'
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu" data-testid="user-menu">'
                    '<span>alice@example.com</span>'
                    "</button>"
                )
            )
        )

        self.assertEqual(probe.get("profileName"), "")

    def test_probe_prefers_user_menu_over_earlier_invite_user_chrome(self) -> None:
        probe = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<button type="button" data-testid="invite-user-row" '
                    'aria-haspopup="menu"><span>Invite Teammates</span></button>'
                    '<div class="account-chip">'
                    "<div><span>Real Person</span></div>"
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu"><span></span></button>'
                    "</div>"
                )
            )
        )

        self.assertEqual(probe.get("profileName"), "Real Person")

    def test_probe_ignores_hidden_img_alt_and_component_data_name(self) -> None:
        probe = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<div class="account-chip">'
                    '<div><span>Visible Name</span></div>'
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu" data-name="user-menu-trigger">'
                    '<img src="about:blank" alt="Hidden Alt" width="0" height="0" '
                    'style="display:none" />'
                    "<span></span>"
                    "</button>"
                    "</div>"
                )
            )
        )

        self.assertEqual(probe.get("profileName"), "Visible Name")

    def test_probe_keeps_hyphenated_display_name_and_prefers_full_name(self) -> None:
        hyphen = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<div class="account-chip">'
                    "<div><span>Anne-Marie</span></div>"
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu"><span></span></button>'
                    "</div>"
                )
            )
        )
        self.assertEqual(hyphen.get("profileName"), "Anne-Marie")

        full = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<div class="account-chip">'
                    "<div><span>JD</span></div>"
                    "<div><span>John Doe</span></div>"
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu"><span></span></button>'
                    "</div>"
                )
            )
        )
        self.assertEqual(full.get("profileName"), "John Doe")

        initials_only = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<div class="account-chip">'
                    "<div><span>JD</span></div>"
                    "<div><span>Pro</span></div>"
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu"><span></span></button>'
                    "</div>"
                )
            )
        )
        self.assertEqual(initials_only.get("profileName"), "JD")

        camel = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<div class="account-chip">'
                    "<div><span>Camel Case</span></div>"
                    '<button type="button" data-testid="userMenu" '
                    'aria-haspopup="menu"><span></span></button>'
                    "</div>"
                )
            )
        )
        self.assertEqual(camel.get("profileName"), "Camel Case")

        korean = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<div class="account-chip">'
                    "<div><span>김종인</span></div>"
                    '<button type="button" aria-label="프로필 메뉴" '
                    'aria-haspopup="menu"><span></span></button>'
                    "</div>"
                )
            )
        )
        self.assertEqual(korean.get("profileName"), "김종인")

        korean_user = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<div class="account-chip">'
                    "<div><span>김종인</span></div>"
                    '<button type="button" aria-label="사용자 메뉴" '
                    'aria-haspopup="menu"><span></span></button>'
                    "</div>"
                )
            )
        )
        self.assertEqual(korean_user.get("profileName"), "김종인")

        attr_hyphen = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<button type="button" data-testid="account-trigger" '
                    'aria-haspopup="menu" '
                    'data-display-name="Anne-Marie"></button>'
                )
            )
        )
        self.assertEqual(attr_hyphen.get("profileName"), "Anne-Marie")

        broad_invite = self._evaluate_probe_on_html(
            self._usage_summary_html(
                identity_html=(
                    '<button type="button" data-testid="invite-user-row" '
                    'aria-haspopup="menu"><span>Invite Teammates</span></button>'
                    '<div class="account-chip">'
                    "<div><span>Real Person</span></div>"
                    '<button type="button" aria-label="User menu" '
                    'aria-haspopup="menu"><span></span></button>'
                    "</div>"
                )
            )
        )
        self.assertEqual(broad_invite.get("profileName"), "Real Person")

    def test_collector_rejects_chrome_aria_profile_name_candidates(self) -> None:
        for chrome_name in (
            "Account menu",
            "Profile menu",
            "My Account",
            "Edit profile",
            "Switch account",
        ):
            with self.subTest(chrome_name=chrome_name):
                session = self._Session(
                    [
                        BrowserOperationResult(
                            probe={
                                **self._probe(
                                    "Included usage: $12.50 / $20.00\n"
                                    "Reset: 2026-08-01T00:00:00Z\n"
                                    "On-demand usage: ON"
                                ),
                                "profileName": chrome_name,
                            }
                        )
                    ]
                )
                monitor = CursorUsageMonitor(
                    profile_id="cursor-personal",
                    browser_session_factory=lambda _config: session,
                )
                reading = monitor.collect()
                self.assertEqual(reading.state, UsageState.READY)
                self.assertEqual(monitor.get_runtime_status()["profile_name"], "")

    def test_collector_binds_probe_profile_name_into_runtime(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe={
                        **self._probe(
                            "Included usage: $12.50 / $20.00\n"
                            "Reset: 2026-08-01T00:00:00Z\n"
                            "On-demand usage: ON"
                        ),
                        "profileName": "Kim Jong Account",
                    }
                )
            ]
        )
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )

        reading = monitor.collect()
        runtime = monitor.get_runtime_status()

        self.assertEqual(reading.state, UsageState.READY)
        self.assertEqual(runtime["profile_name"], "Kim Jong")

    def test_successful_scrape_without_profile_name_clears_stale_identity(self) -> None:
        first = BrowserOperationResult(
            probe={
                **self._probe(
                    "Included usage: $12.50 / $20.00\n"
                    "Reset: 2026-08-01T00:00:00Z\n"
                    "On-demand usage: ON"
                ),
                "profileName": "Alice",
            }
        )
        second = BrowserOperationResult(
            probe={
                **self._probe(
                    "Included usage: $10.00 / $20.00\n"
                    "Reset: 2026-08-01T00:00:00Z\n"
                    "On-demand usage: ON"
                ),
                "profileName": "",
            }
        )
        session = self._Session([first, second])
        monitor = CursorUsageMonitor(
            profile_id="cursor-personal",
            browser_session_factory=lambda _config: session,
        )

        monitor.collect()
        self.assertEqual(monitor.get_runtime_status()["profile_name"], "Alice")
        monitor.collect(force=True)
        self.assertEqual(monitor.get_runtime_status()["profile_name"], "")

    def test_release_profile_session_clears_runtime_profile_name(self) -> None:
        session = self._Session(
            [
                BrowserOperationResult(
                    probe={
                        **self._probe(
                            "Included usage: $12.50 / $20.00\n"
                            "Reset: 2026-08-01T00:00:00Z\n"
                            "On-demand usage: ON"
                        ),
                        "profileName": "Alice",
                    }
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(
                tmp,
                "windows-supporter",
                "cursor-usage-profiles",
                "cursor-personal",
            )
            os.makedirs(profile_dir, exist_ok=True)
            monitor = CursorUsageMonitor(
                config_dir=os.path.join(tmp, "config"),
                profile_dir=profile_dir,
                profile_id="cursor-personal",
                browser_session_factory=lambda _config: session,
            )
            monitor.collect()
            self.assertEqual(monitor.get_runtime_status()["profile_name"], "Alice")
            ok, _message = monitor.release_profile_session()
            self.assertTrue(ok)
            self.assertEqual(monitor.get_runtime_status()["profile_name"], "")


if __name__ == "__main__":
    unittest.main()
