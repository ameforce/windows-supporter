"""API credit balance in the Codex snapshot and the OpenAI Platform sign-in flow."""

from __future__ import annotations

import os
import queue
import tempfile
import unittest
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable
from unittest.mock import patch

from src.apps.codex_api_credit import (
    API_CREDIT_BILLING_URL,
    API_CREDIT_PAGE_PROBE_SCRIPT,
    ApiCreditReading,
    ApiCreditStatus,
)
from src.apps.codex_local_usage import parse_codex_rate_limit_event
from src.apps.codex_usage_browser_types import (
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
)
from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    UsageSnapshot,
    compute_usage_changes,
    merge_snapshot_with_previous,
    reconcile_snapshot_with_local_codex_usage,
)


OK_CREDIT = {"status": "ok", "balance": "$12.34"}
LOGIN_CREDIT = {"status": "login_required", "reason": "login_page"}


def usage_probe(**extra: Any) -> dict[str, Any]:
    return {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
        "mainText": "Analytics usage 5-hour usage limit 99% weekly usage limit 96%",
        "profileName": "Kim Jong",
        "metricBlocks": [
            {
                "metric_key": "five_hour_limit",
                "label_text": "5-hour usage limit",
                "value_candidates": ["99%"],
                "block_text": "5-hour usage limit 99%",
            },
            {
                "metric_key": "weekly_limit",
                "label_text": "weekly usage limit",
                "value_candidates": ["96%"],
                "block_text": "weekly usage limit 96%",
            },
        ],
        **extra,
    }


def credit_probe(payload: dict[str, str]) -> BrowserOperationResult:
    return BrowserOperationResult(
        probe={"url": API_CREDIT_BILLING_URL, "metricBlocks": [], "apiCredit": payload}
    )


PENDING = BrowserOperationResult(error="login_required")


class ApiCreditSession:
    """Browser session fake driven by a list of results (the last one repeats)."""

    def __init__(self, results: list[BrowserOperationResult]) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.on_open: Callable[[], None] | None = None
        self.on_poll: Callable[[], None] | None = None
        self.status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def _next(self) -> BrowserOperationResult:
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def open_api_credit_login(self) -> BrowserOperationResult:
        self.calls.append("open_api_credit_login")
        if self.on_open is not None:
            self.on_open()
        return self._next()

    def poll_api_credit_login(self) -> BrowserOperationResult:
        self.calls.append("poll_api_credit_login")
        if self.on_poll is not None:
            self.on_poll()
        return self._next()

    def close_session(self) -> None:
        self.calls.append("close_session")

    def request_cancel(self) -> bool:
        self.calls.append("request_cancel")
        return True

    def shutdown(self) -> bool:
        self.calls.append("shutdown")
        return True

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return self.status


class InlineThread:
    """Runs the target on start() so the whole flow is deterministic."""

    def __init__(self, target: Callable[..., None], args: tuple[Any, ...] = (), **_kwargs: Any) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


class FailingThread:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def start(self) -> None:
        raise RuntimeError("no thread")


def new_monitor(tmp: str, session: Any | None = None) -> CodexUsageMonitor:
    return CodexUsageMonitor(
        config_dir=tmp,
        profile_dir=os.path.join(tmp, "profile"),
        browser_session_factory=(lambda _config: session) if session is not None else None,
    )


def build(monitor: CodexUsageMonitor, probe: dict[str, Any]) -> UsageSnapshot:
    snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(probe)
    assert snapshot is not None
    return snapshot


class ApiCreditSnapshotTest(unittest.TestCase):
    def test_dict_round_trip_normalizes_the_three_fields(self) -> None:
        snapshot = UsageSnapshot.from_dict(
            {
                "api_credit_balance": "US$ 12.34",
                "api_credit_captured_at": "2026-11-05T01:00:00Z",
                "api_credit_status": "OK",
            }
        )

        self.assertEqual(
            (snapshot.api_credit_balance, snapshot.api_credit_status),
            ("$12.34", "ok"),
        )
        self.assertEqual(UsageSnapshot.from_dict(snapshot.to_dict()), snapshot)
        junk = UsageSnapshot.from_dict(
            {"api_credit_balance": "lots", "api_credit_status": "weird"}
        )
        self.assertEqual((junk.api_credit_balance, junk.api_credit_status), ("", ""))
        self.assertEqual(UsageSnapshot.from_dict(None).to_dict()["api_credit_status"], "")

    def test_the_balance_is_not_a_usage_metric(self) -> None:
        only_balance = UsageSnapshot(
            api_credit_balance="$12.34",
            api_credit_captured_at="2026-11-05T01:00:00Z",
            api_credit_status="ok",
        )

        self.assertFalse(only_balance.has_any_metric())
        for key in ("api_credit_balance", "api_credit_captured_at", "api_credit_status"):
            self.assertNotIn(key, only_balance.metrics())
            self.assertIn(key, only_balance.to_dict())

    def test_balance_changes_never_raise_usage_change_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            before = build(monitor, usage_probe(apiCredit=OK_CREDIT))
            after = build(monitor, usage_probe(apiCredit={"status": "ok", "balance": "$1.00"}))

            self.assertEqual(compute_usage_changes(before, after), [])

    def test_merge_without_a_reading_keeps_the_last_answer_as_it_was(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            previous = build(monitor, usage_probe()).with_api_credit(
                ApiCreditReading(ApiCreditStatus.OK, balance="$12.34"),
                captured_at="2026-11-05T01:00:00Z",
            )
            current = build(monitor, usage_probe())

            merged = merge_snapshot_with_previous(current, previous)

            self.assertEqual(
                (merged.api_credit_balance, merged.api_credit_captured_at, merged.api_credit_status),
                ("$12.34", "2026-11-05T01:00:00Z", "ok"),
            )

    def test_merge_with_a_failed_read_keeps_the_balance_beside_the_new_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            previous = build(monitor, usage_probe()).with_api_credit(
                ApiCreditReading(ApiCreditStatus.OK, balance="$12.34"),
                captured_at="2026-11-05T01:00:00Z",
            )
            current = build(monitor, usage_probe(apiCredit=LOGIN_CREDIT))

            merged = merge_snapshot_with_previous(current, previous)

            self.assertEqual(
                (merged.api_credit_balance, merged.api_credit_captured_at, merged.api_credit_status),
                ("$12.34", "2026-11-05T01:00:00Z", "login_required"),
            )

    def test_merge_with_a_new_reading_replaces_the_old_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            previous = build(monitor, usage_probe()).with_api_credit(
                ApiCreditReading(ApiCreditStatus.OK, balance="$12.34"),
                captured_at="2026-11-05T01:00:00Z",
            )
            current = build(monitor, usage_probe(apiCredit={"status": "ok", "balance": "$9.00"}))

            merged = merge_snapshot_with_previous(current, previous)

            self.assertEqual(merged.api_credit_balance, "$9.00")
            self.assertEqual(merged.api_credit_captured_at, current.api_credit_captured_at)

    def test_first_snapshot_keeps_its_own_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            current = build(monitor, usage_probe(apiCredit=OK_CREDIT))

            self.assertEqual(merge_snapshot_with_previous(current, None), current)

    def test_local_usage_reconcile_keeps_the_balance(self) -> None:
        web = UsageSnapshot.from_metrics(
            {"five_hour_limit": "0%", "weekly_limit": "97%", "remaining_credit": "0"},
            captured_at="2026-07-13T09:52:20+09:00",
            reset_info={
                "five_hour_limit_reset_at": "2026-07-14T02:39:00+09:00",
                "weekly_limit_reset_at": "2026-07-20T04:01:00+09:00",
            },
            reported_metric_keys=("five_hour_limit", "weekly_limit", "remaining_credit"),
        ).with_api_credit(
            ApiCreditReading(ApiCreditStatus.OK, balance="$12.34"),
            captured_at="2026-07-13T09:52:20+09:00",
        )
        local = parse_codex_rate_limit_event(
            {
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
        )
        assert local is not None
        local = replace(local, account_id="acct-local")

        reconciled = reconcile_snapshot_with_local_codex_usage(
            web,
            local,
            now=datetime(2026, 7, 13, 0, 52, 20, tzinfo=timezone.utc),
            web_account_id="acct-local",
        )

        self.assertEqual(reconciled.weekly_limit, "95%")
        self.assertEqual(
            (reconciled.api_credit_balance, reconciled.api_credit_status),
            ("$12.34", "ok"),
        )


class ApiCreditProbeTest(unittest.TestCase):
    def test_reading_in_the_probe_reaches_the_snapshot_with_the_read_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = build(new_monitor(tmp), usage_probe(apiCredit=OK_CREDIT))

            self.assertEqual(
                (snapshot.api_credit_balance, snapshot.api_credit_status),
                ("$12.34", "ok"),
            )
            self.assertEqual(snapshot.api_credit_captured_at, snapshot.captured_at)

    def test_probe_without_a_reading_leaves_the_fields_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = build(new_monitor(tmp), usage_probe())

            self.assertEqual(
                (snapshot.api_credit_balance, snapshot.api_credit_captured_at, snapshot.api_credit_status),
                ("", "", ""),
            )

    def test_ok_payload_without_a_valid_balance_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = build(
                new_monitor(tmp),
                usage_probe(apiCredit={"status": "ok", "balance": "lots"}),
            )

            self.assertEqual(
                (snapshot.api_credit_balance, snapshot.api_credit_status),
                ("", "unavailable"),
            )

    def test_unrecognized_payload_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for raw in ("ok", 5, {"status": "weird"}, None):
                with self.subTest(raw=raw):
                    snapshot = build(new_monitor(tmp), usage_probe(apiCredit=raw))
                    self.assertEqual(snapshot.api_credit_status, "")

    def test_failed_read_does_not_make_the_collect_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = build(new_monitor(tmp), usage_probe(apiCredit=LOGIN_CREDIT))

            self.assertTrue(snapshot.has_any_metric())
            self.assertEqual(snapshot.api_credit_status, "login_required")
            self.assertEqual(snapshot.api_credit_balance, "")

    def test_balance_survives_collects_without_a_reading_and_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            monitor.handle_snapshot(build(monitor, usage_probe(apiCredit=OK_CREDIT)))
            read_at = monitor.get_last_snapshot().api_credit_captured_at
            monitor.handle_snapshot(build(monitor, usage_probe()))

            kept = monitor.get_last_snapshot()
            self.assertEqual((kept.api_credit_balance, kept.api_credit_status), ("$12.34", "ok"))

            monitor.handle_snapshot(build(monitor, usage_probe(apiCredit=LOGIN_CREDIT)))
            qualified = monitor.get_last_snapshot()
            self.assertEqual(
                (qualified.api_credit_balance, qualified.api_credit_captured_at, qualified.api_credit_status),
                ("$12.34", read_at, "login_required"),
            )

            reloaded = new_monitor(tmp).get_last_snapshot()
            self.assertEqual(
                (reloaded.api_credit_balance, reloaded.api_credit_captured_at, reloaded.api_credit_status),
                ("$12.34", read_at, "login_required"),
            )

    def test_commits_without_usage_backfill_keep_the_balance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            monitor.handle_snapshot(build(monitor, usage_probe(apiCredit=OK_CREDIT)))
            read_at = monitor.get_last_snapshot().api_credit_captured_at

            monitor.handle_snapshot(build(monitor, usage_probe()), allow_previous_backfill=False)
            kept = monitor.get_last_snapshot()
            self.assertEqual(
                (kept.api_credit_balance, kept.api_credit_captured_at, kept.api_credit_status),
                ("$12.34", read_at, "ok"),
            )

            monitor.handle_snapshot(
                build(monitor, usage_probe(apiCredit=LOGIN_CREDIT)),
                allow_previous_backfill=False,
            )
            failed = monitor.get_last_snapshot()
            self.assertEqual(
                (failed.api_credit_balance, failed.api_credit_captured_at, failed.api_credit_status),
                ("$12.34", read_at, "login_required"),
            )

    def test_snapshot_without_usage_metrics_keeps_a_stored_balance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot(
                api_credit_balance="$12.34",
                api_credit_captured_at="2026-11-05T01:00:00Z",
                api_credit_status="ok",
            )

            monitor.handle_snapshot(build(monitor, usage_probe()))

            kept = monitor.get_last_snapshot()
            self.assertTrue(kept.has_any_metric())
            self.assertEqual(kept.api_credit_balance, "$12.34")

    def test_an_empty_last_snapshot_gives_nothing_to_keep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot()

            monitor.handle_snapshot(build(monitor, usage_probe()), allow_previous_backfill=False)

            self.assertEqual(monitor.get_last_snapshot().api_credit_balance, "")

    def test_tooltip_lines_show_the_balance_and_qualify_a_stale_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp)
            lines_of = monitor._CodexUsageMonitor__build_snapshot_lines
            fresh = build(monitor, usage_probe(apiCredit=OK_CREDIT))
            stale = merge_snapshot_with_previous(
                build(monitor, usage_probe(apiCredit=LOGIN_CREDIT)), fresh
            )
            never = build(monitor, usage_probe())
            failed_first = build(monitor, usage_probe(apiCredit=LOGIN_CREDIT))

            self.assertIn("API 크레딧 잔액: $12.34", [text for text, _ in lines_of(fresh)])
            self.assertIn(
                "API 크레딧 잔액: $12.34 (이전 값, 로그인 필요)",
                [text for text, _ in lines_of(stale)],
            )
            self.assertIn("API 크레딧 잔액: 로그인 필요", [text for text, _ in lines_of(failed_first)])
            self.assertFalse(any("API 크레딧" in text for text, _ in lines_of(never)))

    def test_session_is_configured_with_the_billing_page(self) -> None:
        configs: list[Any] = []

        def factory(config: Any) -> ApiCreditSession:
            configs.append(config)
            return ApiCreditSession([PENDING])

        with tempfile.TemporaryDirectory() as tmp:
            CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                browser_session_factory=factory,
            )

        self.assertEqual(configs[0].api_credit_url, API_CREDIT_BILLING_URL)
        self.assertEqual(configs[0].api_credit_probe_script, API_CREDIT_PAGE_PROBE_SCRIPT)


class ApiCreditLoginFlowTest(unittest.TestCase):
    def _run(
        self,
        results: list[BrowserOperationResult],
        setup: Callable[[CodexUsageMonitor, ApiCreditSession], None] | None = None,
        *,
        window_sec: float | None = None,
    ) -> tuple[CodexUsageMonitor, ApiCreditSession, list[str], bool]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = ApiCreditSession(results)
        monitor = new_monitor(tmp.name, session)
        events: queue.Queue[Callable[[], None]] = queue.Queue()
        monitor.attach(object(), events, start_monitor=False)
        texts: list[str] = []
        monitor.set_notification_sink(
            lambda payload: texts.append(str(payload["text"])),
            suppress_normal_tooltips=True,
        )
        if window_sec is not None:
            monitor._CodexUsageMonitor__api_credit_login_window_sec = window_sec
        if setup is not None:
            setup(monitor, session)
        with patch("src.apps.codex_usage_monitor.threading.Thread", InlineThread):
            started = monitor.login_api_credit()
        while not events.empty():
            events.get()()
        return monitor, session, texts, started

    def test_sign_in_waits_then_stores_the_balance_and_reports_it(self) -> None:
        monitor, session, texts, started = self._run([PENDING, credit_probe(OK_CREDIT)])

        self.assertTrue(started)
        self.assertEqual(session.calls, ["open_api_credit_login", "poll_api_credit_login"])
        snapshot = monitor.get_last_snapshot()
        self.assertEqual((snapshot.api_credit_balance, snapshot.api_credit_status), ("$12.34", "ok"))
        self.assertTrue(snapshot.api_credit_captured_at)
        self.assertEqual(texts[-1], "API 크레딧 잔액을 확인했습니다: $12.34")
        runtime = monitor.get_runtime_status()
        self.assertFalse(runtime["collect_inflight"])
        self.assertFalse(runtime["api_credit_login_active"])
        self.assertTrue(runtime["can_login_api_credit"])

    def test_stored_balance_is_persisted_for_the_next_launch(self) -> None:
        monitor, _session, _texts, _started = self._run([credit_probe(OK_CREDIT)])

        config_dir = monitor.get_settings_snapshot()["state_path"]
        reloaded = new_monitor(os.path.dirname(config_dir)).get_last_snapshot()
        self.assertEqual(reloaded.api_credit_balance, "$12.34")

    def test_already_signed_in_profile_needs_no_polling(self) -> None:
        monitor, session, _texts, _started = self._run([credit_probe(OK_CREDIT)])

        self.assertEqual(session.calls, ["open_api_credit_login"])
        self.assertEqual(monitor.get_last_snapshot().api_credit_balance, "$12.34")

    def test_flow_holds_the_collect_lock_and_reports_itself_while_waiting(self) -> None:
        seen: list[dict[str, Any]] = []

        def setup(monitor: CodexUsageMonitor, session: ApiCreditSession) -> None:
            session.on_open = lambda: seen.append(monitor.get_runtime_status())

        _monitor, _session, _texts, _started = self._run([credit_probe(OK_CREDIT)], setup)

        runtime = seen[0]
        self.assertTrue(runtime["api_credit_login_active"])
        self.assertTrue(runtime["collect_inflight"])
        self.assertEqual(runtime["collect_source"], "api_credit_login")
        self.assertEqual(runtime["monitor_state"], "running")
        self.assertFalse(runtime["can_login_api_credit"])

    def test_closing_the_window_ends_the_flow_without_a_balance(self) -> None:
        monitor, session, texts, _started = self._run(
            [PENDING, BrowserOperationResult(error="login_window_closed")]
        )

        self.assertEqual(session.calls, ["open_api_credit_login", "poll_api_credit_login"])
        self.assertEqual(monitor.get_last_snapshot().api_credit_balance, "")
        self.assertIn("창이 닫혀", texts[-1])

    def test_waiting_has_a_time_limit_and_closes_the_window(self) -> None:
        monitor, session, texts, _started = self._run([PENDING], window_sec=0.2)

        self.assertEqual(session.calls[-1], "close_session")
        self.assertEqual(monitor.get_last_snapshot().api_credit_balance, "")
        self.assertIn("대기 시간이 지났습니다", texts[-1])
        self.assertFalse(monitor.get_runtime_status()["collect_inflight"])

    def test_profile_lock_is_reported_without_closing_anything(self) -> None:
        _monitor, session, texts, _started = self._run(
            [BrowserOperationResult(error="profile_in_use")]
        )

        self.assertEqual(session.calls, ["open_api_credit_login"])
        self.assertIn("프로필을 사용 중", texts[-1])

    def test_unexpected_driver_error_closes_the_window_and_reports_failure(self) -> None:
        _monitor, session, texts, _started = self._run(
            [BrowserOperationResult(error="collect_failed")]
        )

        self.assertEqual(session.calls, ["open_api_credit_login", "close_session"])
        self.assertIn("오류가 발생했습니다", texts[-1])

    def test_a_running_collect_blocks_the_flow(self) -> None:
        def setup(monitor: CodexUsageMonitor, _session: ApiCreditSession) -> None:
            self.assertTrue(monitor._CodexUsageMonitor__collect_lock.acquire(blocking=False))

        monitor, session, texts, started = self._run([credit_probe(OK_CREDIT)], setup)

        self.assertTrue(started)
        self.assertEqual(session.calls, [])
        self.assertIn("조회가 진행 중입니다", texts[-1])
        monitor._CodexUsageMonitor__collect_lock.release()

    def test_cancel_stops_the_wait_quietly(self) -> None:
        def setup(monitor: CodexUsageMonitor, session: ApiCreditSession) -> None:
            session.on_open = monitor._CodexUsageMonitor__request_collect_cancel

        monitor, session, texts, _started = self._run([PENDING], setup)

        self.assertEqual(session.calls, ["open_api_credit_login"])
        self.assertEqual(len(texts), 1, "only the opening notice, no outcome notice")
        self.assertEqual(monitor.get_last_snapshot().api_credit_balance, "")

    def test_a_result_that_arrives_after_a_restart_is_not_stored(self) -> None:
        def setup(monitor: CodexUsageMonitor, session: ApiCreditSession) -> None:
            def restart() -> None:
                monitor._CodexUsageMonitor__worker_epoch += 1

            session.on_open = restart

        monitor, _session, texts, _started = self._run([credit_probe(OK_CREDIT)], setup)

        self.assertEqual(monitor.get_last_snapshot().api_credit_balance, "")
        self.assertEqual(len(texts), 1, "only the opening notice")

    def test_a_result_that_arrives_during_logout_is_not_stored(self) -> None:
        def setup(monitor: CodexUsageMonitor, session: ApiCreditSession) -> None:
            def log_out() -> None:
                monitor._CodexUsageMonitor__logout_in_progress = True

            session.on_open = log_out

        monitor, _session, _texts, _started = self._run([credit_probe(OK_CREDIT)], setup)

        self.assertEqual(monitor.get_last_snapshot().api_credit_balance, "")

    def test_unreadable_page_ends_the_flow_with_its_own_notice_and_status(self) -> None:
        unreadable = credit_probe({"status": "unavailable", "reason": "balance_unreadable"})

        def setup(monitor: CodexUsageMonitor, _session: ApiCreditSession) -> None:
            monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot(
                api_credit_balance="$12.34",
                api_credit_captured_at="2026-11-05T01:00:00Z",
                api_credit_status="ok",
            )

        monitor, session, texts, _started = self._run([unreadable], setup)

        self.assertEqual(session.calls, ["open_api_credit_login"])
        self.assertIn("찾지 못했습니다", texts[-1])
        snapshot = monitor.get_last_snapshot()
        self.assertEqual(
            (snapshot.api_credit_balance, snapshot.api_credit_status),
            ("$12.34", "unavailable"),
        )

    def test_cancel_outranks_an_error_from_the_cancelled_command(self) -> None:
        def setup(monitor: CodexUsageMonitor, session: ApiCreditSession) -> None:
            session.on_open = monitor._CodexUsageMonitor__request_collect_cancel

        _monitor, session, texts, _started = self._run(
            [BrowserOperationResult(error="command_timeout")], setup
        )

        self.assertEqual(session.calls, ["open_api_credit_login"])
        self.assertEqual(len(texts), 1, "only the opening notice")

    def test_flow_refuses_while_the_chatgpt_sign_in_is_being_polled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = ApiCreditSession([PENDING])
            monitor = new_monitor(tmp, session)
            monitor.attach(object(), queue.Queue(), start_monitor=False)
            monitor._CodexUsageMonitor__pending_login_after_id = "poll-timer"

            self.assertFalse(monitor.login_api_credit())
            self.assertEqual(session.calls, [])
            self.assertFalse(monitor.get_runtime_status()["can_login_api_credit"])
            monitor._CodexUsageMonitor__pending_login_after_id = None
            monitor._CodexUsageMonitor__pending_login_poll_until_ts = float("inf")
            self.assertFalse(monitor.get_runtime_status()["can_login_api_credit"])

    def test_shutdown_waits_for_the_cancelled_flow_to_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = ApiCreditSession([PENDING])
            opened = threading.Event()
            session.on_open = opened.set
            monitor = new_monitor(tmp, session)
            monitor.attach(object(), queue.Queue(), start_monitor=False)
            self.assertTrue(monitor.login_api_credit())
            self.assertTrue(opened.wait(5.0))
            thread = monitor._CodexUsageMonitor__api_credit_login_thread

            monitor.shutdown()

            self.assertFalse(thread.is_alive())

    def test_flow_does_not_start_without_a_ui_root_or_after_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            detached = new_monitor(tmp, ApiCreditSession([PENDING]))
            self.assertFalse(detached.login_api_credit())
            attached = new_monitor(tmp, ApiCreditSession([PENDING]))
            attached.attach(object(), queue.Queue(), start_monitor=False)
            attached._CodexUsageMonitor__request_collect_cancel()
            self.assertFalse(attached.login_api_credit())

    def test_thread_start_failure_is_reported_to_the_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp, ApiCreditSession([PENDING]))
            monitor.attach(object(), queue.Queue(), start_monitor=False)
            with patch("src.apps.codex_usage_monitor.threading.Thread", FailingThread):
                self.assertFalse(monitor.login_api_credit())

    def test_runtime_flags_block_a_second_flow_while_busy_or_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = new_monitor(tmp, ApiCreditSession([PENDING]))
            self.assertTrue(monitor.get_runtime_status()["can_login_api_credit"])
            monitor._CodexUsageMonitor__collect_inflight = True
            self.assertFalse(monitor.get_runtime_status()["can_login_api_credit"])
            monitor._CodexUsageMonitor__collect_inflight = False
            monitor._CodexUsageMonitor__profile_in_use_detected = True
            self.assertFalse(monitor.get_runtime_status()["can_login_api_credit"])


if __name__ == "__main__":
    unittest.main()
