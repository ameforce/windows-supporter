import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.apps.claude_usage_monitor import ClaudeUsageMonitor
from src.apps.codex_usage_browser_types import (
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
)
from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    UsageSnapshot,
    observe_usage_limit_resets,
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _snapshot(
    remaining: str,
    captured_at: datetime,
    reset_at: str | None = None,
) -> UsageSnapshot:
    reset_info = {"five_hour_limit_reset_at": reset_at} if reset_at else {}
    return UsageSnapshot.from_metrics(
        {"five_hour_limit": remaining},
        captured_at=_iso(captured_at),
        reset_info=reset_info,
        reported_metric_keys=("five_hour_limit",),
    )


def _claude_result(
    five_hour_used: float,
    five_hour_reset: datetime,
    weekly_used: float,
    weekly_reset: datetime,
) -> BrowserOperationResult:
    payload = {
        "usage": {
            "five_hour": {
                "utilization": five_hour_used,
                "resets_at": five_hour_reset.isoformat(),
            },
            "seven_day": {
                "utilization": weekly_used,
                "resets_at": weekly_reset.isoformat(),
            },
        }
    }
    return BrowserOperationResult(
        probe={
            "url": "https://claude.ai/settings/usage",
            "mainText": "Current session",
            "accountId": "org-irregular-reset",
            "metricBlocks": [
                {"metric_key": "claude_usage_api", "block_text": json.dumps(payload)}
            ],
        }
    )


class _ClaudeSession:
    def __init__(self, results: list[BrowserOperationResult]) -> None:
        self.results = results

    def collect(self) -> BrowserOperationResult:
        return self.results.pop(0)

    def shutdown(self) -> bool:
        return True

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return BrowserRuntimeStatus(BrowserState.HEADLESS_READY, False, "")


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls = []

    def after(self, delay, fn):
        self.after_calls.append((delay, fn))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, _after_id):
        return None


class ObserveUsageLimitResetsTest(unittest.TestCase):
    def _seed(
        self,
        baselines: dict[str, str],
        state: dict[str, dict],
        snapshot: UsageSnapshot,
    ) -> None:
        captured_at = datetime.fromisoformat(snapshot.captured_at)
        self._observe(baselines, state, None, snapshot, captured_at)

    def _observe(
        self,
        baselines: dict[str, str],
        state: dict[str, dict],
        previous: UsageSnapshot | None,
        current: UsageSnapshot,
        now: datetime,
        observed: UsageSnapshot | None = None,
    ):
        return observe_usage_limit_resets(
            baselines,
            current,
            reset_state=state,
            previous=previous,
            observed_snapshot=observed or current,
            now=now,
        )

    def _pending_candidate(
        self,
        reset_at: str | None,
    ) -> tuple[dict[str, str], dict[str, dict], UsageSnapshot, UsageSnapshot, datetime]:
        now = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
        deadline = _iso(now + timedelta(hours=2))
        previous = _snapshot("70%", now - timedelta(minutes=1), deadline)
        current = _snapshot("100%", now, reset_at)
        return {"five_hour_limit": deadline}, {}, previous, current, now

    def test_rapid_recovery_requires_a_second_fresh_full_observation(self) -> None:
        deadline_modes = ("unchanged", "advanced", "missing")
        for mode in deadline_modes:
            with self.subTest(deadline_mode=mode):
                now = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
                baseline = _iso(now + timedelta(hours=2))
                reset_at = {
                    "unchanged": baseline,
                    "advanced": _iso(now + timedelta(hours=3)),
                    "missing": None,
                }[mode]
                previous = _snapshot("70%", now - timedelta(minutes=1), baseline)
                candidate = _snapshot("100%", now, reset_at)
                baselines = {"five_hour_limit": baseline}
                state: dict[str, dict] = {}
                self._seed(baselines, state, previous)
                self.assertEqual(
                    self._observe(baselines, state, previous, candidate, now), []
                )
                confirmed_at = now + timedelta(seconds=30)
                confirmed = _snapshot("100%", confirmed_at, candidate.five_hour_limit_reset_at)
                resets = self._observe(
                    baselines, state, candidate, confirmed, confirmed_at
                )
                self.assertEqual([reset.key for reset in resets], ["five_hour_limit"])
                self.assertEqual(resets[0].detected_by, "usage_replenished")

    def test_rapid_recovery_boundary_thresholds(self) -> None:
        cases = (
            (95, 100, 900, True),
            (95, 100, 901, False),
            (97, 100, 30, False),
            (93, 98, 30, False),
            (94, 99, 30, True),
        )
        for previous_percent, current_percent, gap_seconds, expected in cases:
            with self.subTest(
                previous=previous_percent,
                current=current_percent,
                gap_seconds=gap_seconds,
            ):
                now = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
                deadline = _iso(now + timedelta(hours=2))
                previous_at = now - timedelta(seconds=gap_seconds)
                previous = _snapshot(f"{previous_percent}%", previous_at, deadline)
                candidate = _snapshot(f"{current_percent}%", now, deadline)
                baselines = {"five_hour_limit": deadline}
                state: dict[str, dict] = {}
                self._seed(baselines, state, previous)

                first = self._observe(baselines, state, previous, candidate, now)
                confirmed_at = now + timedelta(seconds=1)
                confirmed = _snapshot(f"{current_percent}%", confirmed_at, deadline)
                second = self._observe(
                    baselines, state, candidate, confirmed, confirmed_at
                )
                self.assertEqual(bool(first or second), expected)
                self.assertEqual(len(first) + len(second), 1 if expected else 0)

    def test_confirmed_reset_and_repeated_full_observations_alert_only_once(self) -> None:
        baselines, state, previous, candidate, now = self._pending_candidate(
            _iso(datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc))
        )
        self._seed(baselines, state, previous)
        self.assertEqual(self._observe(baselines, state, previous, candidate, now), [])
        confirmed_at = now + timedelta(seconds=30)
        confirmed = _snapshot("100%", confirmed_at, candidate.five_hour_limit_reset_at)
        self.assertEqual(
            len(self._observe(baselines, state, candidate, confirmed, confirmed_at)), 1
        )
        for offset in (60, 90):
            later_at = now + timedelta(seconds=offset)
            later = _snapshot("100%", later_at, confirmed.five_hour_limit_reset_at)
            self.assertEqual(
                self._observe(baselines, state, confirmed, later, later_at), []
            )

    def test_pending_confirmation_survives_json_state_round_trip(self) -> None:
        baselines, state, previous, candidate, now = self._pending_candidate(None)
        self._seed(baselines, state, previous)
        self.assertEqual(self._observe(baselines, state, previous, candidate, now), [])
        restored_state = json.loads(json.dumps(state))
        restored_baselines = json.loads(json.dumps(baselines))
        confirmed_at = now + timedelta(seconds=20)
        confirmed = _snapshot("100%", confirmed_at)
        resets = self._observe(
            restored_baselines, restored_state, candidate, confirmed, confirmed_at
        )
        self.assertEqual([reset.key for reset in resets], ["five_hour_limit"])
        next_at = confirmed_at + timedelta(seconds=20)
        self.assertEqual(
            self._observe(
                restored_baselines,
                restored_state,
                confirmed,
                _snapshot("100%", next_at),
                next_at,
            ),
            [],
        )

    def test_time_only_schedule_change_and_small_or_partial_rises_are_ignored(self) -> None:
        now = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
        baseline = _iso(now + timedelta(hours=2))
        schedule_only = _snapshot("70%", now, _iso(now + timedelta(hours=3)))
        self.assertEqual(
            self._observe(
                {"five_hour_limit": baseline}, {}, schedule_only, schedule_only, now
            ),
            [],
        )

        cases = (
            ("96%", now + timedelta(minutes=1)),
            ("98%", now + timedelta(minutes=1)),
        )
        for remaining, at in cases:
            with self.subTest(remaining=remaining):
                state: dict[str, dict] = {}
                previous = _snapshot("94%", now, baseline)
                partial = _snapshot(remaining, at, baseline)
                self._seed({"five_hour_limit": baseline}, state, previous)
                self.assertEqual(
                    self._observe(
                        {"five_hour_limit": baseline}, state, previous, partial, at
                    ),
                    [],
                )
                self.assertNotIn("pending", state.get("five_hour_limit", {}))

    def test_gap_deadline_and_invalid_or_backfilled_current_values_do_not_confirm(self) -> None:
        now = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
        deadline = _iso(now + timedelta(hours=1))
        previous = _snapshot("70%", now, deadline)
        base_map = {"five_hour_limit": deadline}
        state: dict[str, dict] = {}
        self._seed(base_map, state, previous)
        full = _snapshot("100%", now + timedelta(seconds=30), deadline)

        too_late = _snapshot("100%", now + timedelta(minutes=16), deadline)
        too_late_at = now + timedelta(minutes=16)
        self.assertEqual(
            self._observe(
                base_map, state, previous, too_late, too_late_at
            ),
            [],
        )

        malformed = _snapshot("100%", now + timedelta(seconds=30), "not-a-date")
        malformed_state: dict[str, dict] = {}
        self._seed({"five_hour_limit": "not-a-date"}, malformed_state, previous)
        self.assertEqual(
            self._observe(
                {"five_hour_limit": "not-a-date"},
                malformed_state,
                previous,
                malformed,
                now + timedelta(seconds=30),
            ),
            [],
        )

        backfilled = UsageSnapshot.from_metrics(
            {"weekly_limit": "80%"},
            captured_at=_iso(now + timedelta(seconds=30)),
            reported_metric_keys=("five_hour_limit", "weekly_limit"),
        )
        full_at = now + timedelta(seconds=30)
        backfilled_state: dict[str, dict] = {}
        self._seed({"five_hour_limit": deadline}, backfilled_state, previous)
        merged_full = UsageSnapshot.from_metrics(
            {"five_hour_limit": "100%", "weekly_limit": "80%"},
            captured_at=_iso(full_at),
            reported_metric_keys=("five_hour_limit", "weekly_limit"),
        )
        self.assertEqual(
            self._observe(
                {"five_hour_limit": deadline},
                backfilled_state,
                previous,
                merged_full,
                full_at,
                observed=backfilled,
            ),
            [],
        )

        unparseable = UsageSnapshot.from_metrics(
            {"five_hour_limit": "not-a-percentage"},
            captured_at=_iso(full_at),
            reported_metric_keys=("five_hour_limit",),
        )
        self.assertEqual(
            self._observe(
                {"five_hour_limit": deadline},
                {},
                previous,
                unparseable,
                full_at,
            ),
            [],
        )

    def test_repeated_or_out_of_order_observations_and_full_spike_do_not_alert(self) -> None:
        now = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
        deadline = _iso(now + timedelta(hours=2))
        low = _snapshot("70%", now, deadline)
        spike = _snapshot("100%", now + timedelta(seconds=20), deadline)
        state: dict[str, dict] = {}
        self._seed({"five_hour_limit": deadline}, state, low)
        self.assertEqual(
            self._observe({"five_hour_limit": deadline}, state, low, spike, now + timedelta(seconds=20)),
            [],
        )
        lower = _snapshot("70%", now + timedelta(seconds=40), deadline)
        self.assertEqual(
            self._observe({"five_hour_limit": deadline}, state, spike, lower, now + timedelta(seconds=40)),
            [],
        )
        self.assertEqual(
            self._observe({"five_hour_limit": deadline}, state, low, spike, now + timedelta(seconds=45)),
            [],
        )
        out_of_order = _snapshot("100%", now + timedelta(seconds=10), deadline)
        self.assertEqual(
            self._observe({"five_hour_limit": deadline}, state, lower, out_of_order, now + timedelta(seconds=50)),
            [],
        )

    def test_stale_pending_and_missing_measurement_break_confirmation(self) -> None:
        baselines, state, previous, candidate, now = self._pending_candidate(None)
        self._seed(baselines, state, previous)
        self.assertEqual(self._observe(baselines, state, previous, candidate, now), [])
        missing_at = now + timedelta(seconds=30)
        missing = UsageSnapshot.from_metrics(
            {"weekly_limit": "80%"},
            captured_at=_iso(missing_at),
            reported_metric_keys=("weekly_limit",),
        )
        self.assertEqual(self._observe(baselines, state, candidate, missing, missing_at), [])
        confirm_at = now + timedelta(minutes=2)
        self.assertEqual(
            self._observe(
                baselines,
                state,
                candidate,
                _snapshot("100%", confirm_at),
                confirm_at,
            ),
            [],
        )

        stale_state: dict[str, dict] = {}
        self._seed(baselines, stale_state, previous)
        self.assertEqual(
            self._observe(baselines, stale_state, previous, candidate, now), []
        )
        stale_at = now + timedelta(minutes=16)
        stale_confirmation = _snapshot("100%", stale_at)
        self.assertEqual(
            self._observe(
                baselines,
                stale_state,
                candidate,
                stale_confirmation,
                stale_at,
            ),
            [],
        )

    def test_missing_measurement_breaks_consecutive_low_rearming_samples(self) -> None:
        baselines, state, previous, candidate, now = self._pending_candidate(None)
        self._seed(baselines, state, previous)
        self._observe(baselines, state, previous, candidate, now)
        confirmed_at = now + timedelta(seconds=30)
        confirmed = _snapshot("100%", confirmed_at)
        self.assertEqual(
            len(self._observe(baselines, state, candidate, confirmed, confirmed_at)),
            1,
        )

        first_low_at = confirmed_at + timedelta(seconds=30)
        first_low = _snapshot("95%", first_low_at)
        self.assertEqual(
            self._observe(baselines, state, confirmed, first_low, first_low_at), []
        )
        missing_at = first_low_at + timedelta(seconds=30)
        missing = UsageSnapshot.from_metrics(
            {"weekly_limit": "80%"},
            captured_at=_iso(missing_at),
            reported_metric_keys=("weekly_limit",),
        )
        self.assertEqual(
            self._observe(baselines, state, first_low, missing, missing_at), []
        )
        second_low_at = missing_at + timedelta(seconds=30)
        second_low = _snapshot("94%", second_low_at)
        self.assertEqual(
            self._observe(baselines, state, missing, second_low, second_low_at), []
        )
        recovered_at = second_low_at + timedelta(seconds=30)
        recovered = _snapshot("100%", recovered_at)
        self.assertEqual(
            self._observe(baselines, state, second_low, recovered, recovered_at), []
        )
        confirmation_at = recovered_at + timedelta(seconds=30)
        confirmation = _snapshot("100%", confirmation_at)
        self.assertEqual(
            self._observe(
                baselines, state, recovered, confirmation, confirmation_at
            ),
            [],
        )

    def test_latch_rearms_only_after_two_low_observations_then_new_confirmed_refill(self) -> None:
        baselines, state, previous, candidate, now = self._pending_candidate(None)
        self._seed(baselines, state, previous)
        self.assertEqual(self._observe(baselines, state, previous, candidate, now), [])
        at = now + timedelta(seconds=30)
        full = _snapshot("100%", at)
        self.assertEqual(len(self._observe(baselines, state, candidate, full, at)), 1)

        low_one_at = at + timedelta(seconds=30)
        low_one = _snapshot("95%", low_one_at)
        self.assertEqual(self._observe(baselines, state, full, low_one, low_one_at), [])
        bounce_at = low_one_at + timedelta(seconds=30)
        bounce = _snapshot("100%", bounce_at)
        self.assertEqual(self._observe(baselines, state, low_one, bounce, bounce_at), [])
        low_again_at = bounce_at + timedelta(seconds=30)
        low_again = _snapshot("94%", low_again_at)
        self.assertEqual(self._observe(baselines, state, bounce, low_again, low_again_at), [])
        low_two_at = low_again_at + timedelta(seconds=30)
        low_two = _snapshot("93%", low_two_at)
        self.assertEqual(self._observe(baselines, state, low_again, low_two, low_two_at), [])
        recovered_at = low_two_at + timedelta(seconds=30)
        recovered = _snapshot("100%", recovered_at)
        self.assertEqual(self._observe(baselines, state, low_two, recovered, recovered_at), [])
        confirmed_at = recovered_at + timedelta(seconds=30)
        confirmed = _snapshot("100%", confirmed_at)
        self.assertEqual(
            [
                reset.key
                for reset in self._observe(
                    baselines, state, recovered, confirmed, confirmed_at
                )
            ],
            ["five_hour_limit"],
        )

    def test_expired_scheduled_deadline_remains_immediate(self) -> None:
        now = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
        due = _iso(now - timedelta(seconds=10))
        previous = _snapshot("100%", now - timedelta(minutes=1), due)
        baselines = {"five_hour_limit": due}
        self._seed(baselines, {}, previous)
        current = _snapshot("100%", now, _iso(now + timedelta(hours=4)))
        resets = self._observe(baselines, {}, previous, current, now)
        self.assertEqual([reset.key for reset in resets], ["five_hour_limit"])

        missing_deadline = _snapshot("100%", now, None)
        elapsed = self._observe(
            {"five_hour_limit": due},
            {},
            previous,
            missing_deadline,
            now,
        )
        self.assertEqual([reset.key for reset in elapsed], ["five_hour_limit"])

    def test_later_scheduled_rollover_alerts_even_while_irregular_reset_is_latched(self) -> None:
        baselines, state, previous, candidate, now = self._pending_candidate(None)
        self._seed(baselines, state, previous)
        self._observe(baselines, state, previous, candidate, now)
        confirmed_at = now + timedelta(seconds=30)
        confirmed = _snapshot("100%", confirmed_at)
        self.assertEqual(
            len(self._observe(baselines, state, candidate, confirmed, confirmed_at)),
            1,
        )

        reset_deadline = confirmed_at + timedelta(hours=5)
        published_at = confirmed_at + timedelta(minutes=1)
        published = _snapshot("100%", published_at, _iso(reset_deadline))
        self.assertEqual(
            self._observe(baselines, state, confirmed, published, published_at), []
        )
        self.assertEqual(baselines["five_hour_limit"], _iso(reset_deadline))

        expired_at = reset_deadline + timedelta(seconds=1)
        rolled = _snapshot("100%", expired_at, _iso(expired_at + timedelta(hours=5)))
        resets = self._observe(baselines, state, published, rolled, expired_at)
        self.assertEqual([reset.key for reset in resets], ["five_hour_limit"])
        self.assertEqual(resets[0].detected_by, "reset_at")


class MonitorIrregularLimitResetNotificationTest(unittest.TestCase):
    def _make_monitor(self, config_dir: str) -> CodexUsageMonitor:
        class _Session:
            def shutdown(self) -> bool:
                return True

        return CodexUsageMonitor(
            config_dir=config_dir,
            profile_dir=os.path.join(config_dir, "profile"),
            browser_session_factory=lambda _config: _Session(),
        )

    def test_confirmed_irregular_reset_shows_tooltip_and_plays_fanfare_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            monitor._CodexUsageMonitor__root = _FakeRoot()
            now = datetime.now(timezone.utc)
            deadline = _iso(now + timedelta(hours=2))
            snapshots = (
                _snapshot("70%", now - timedelta(minutes=1), deadline),
                _snapshot("100%", now, deadline),
                _snapshot("100%", now + timedelta(seconds=30), deadline),
                _snapshot("100%", now + timedelta(minutes=1), deadline),
            )
            shown: list = []
            with patch.object(
                monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ), patch.object(
                monitor,
                "_CodexUsageMonitor__get_last_input_tick",
                return_value=None,
                create=True,
            ), patch.object(
                monitor,
                "_CodexUsageMonitor__show_alert_tooltip",
                side_effect=lambda text, lines=None, duration_ms=None: shown.append(
                    (text, lines, duration_ms)
                ),
            ), patch(
                "src.utils.reset_fanfare.play_reset_fanfare", return_value=True
            ) as play_mock:
                for snapshot in snapshots:
                    monitor.handle_snapshot(snapshot)

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)
            lines = shown[0][1] or []
            joined = " | ".join(str(line[0]) for line in lines)
            self.assertIn("사용 한도 초기화", joined)
            self.assertIn("5시간 사용 한도 초기화됨", joined)

    def test_pending_reset_confirmation_survives_monitor_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            deadline = _iso(now + timedelta(hours=2))
            low = _snapshot("70%", now - timedelta(minutes=1), deadline)
            candidate = _snapshot("100%", now, deadline)
            first = self._make_monitor(tmp)
            first.handle_snapshot(low)
            first.handle_snapshot(candidate)

            with open(first._CodexUsageMonitor__state_path, encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertIn("five_hour_limit", persisted.get("limit_reset_state", {}))
            self.assertIn(
                "pending",
                persisted["limit_reset_state"]["five_hour_limit"],
            )

            restarted = self._make_monitor(tmp)
            restarted._CodexUsageMonitor__root = _FakeRoot()
            confirmed_at = now + timedelta(seconds=30)
            confirmed = _snapshot("100%", confirmed_at, deadline)
            repeated_at = confirmed_at + timedelta(seconds=30)
            repeated = _snapshot("100%", repeated_at, deadline)
            shown: list = []
            with patch.object(
                restarted,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ), patch.object(
                restarted,
                "_CodexUsageMonitor__get_last_input_tick",
                return_value=None,
                create=True,
            ), patch.object(
                restarted,
                "_CodexUsageMonitor__show_alert_tooltip",
                side_effect=lambda text, lines=None, duration_ms=None: shown.append(
                    (text, lines, duration_ms)
                ),
            ), patch(
                "src.utils.reset_fanfare.play_reset_fanfare", return_value=True
            ) as play_mock:
                restarted.handle_snapshot(confirmed)
                restarted.handle_snapshot(repeated)

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)

    def test_legacy_state_seeds_low_sample_and_confirms_irregular_reset_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            deadline = _iso(now + timedelta(hours=2))
            low = _snapshot("70%", now - timedelta(minutes=1), deadline)
            first = self._make_monitor(tmp)
            first.handle_snapshot(low)

            with open(first._CodexUsageMonitor__state_path, encoding="utf-8") as stream:
                legacy_state = json.load(stream)
            legacy_state.pop("limit_reset_state", None)
            with open(first._CodexUsageMonitor__state_path, "w", encoding="utf-8") as stream:
                json.dump(legacy_state, stream)

            restarted = self._make_monitor(tmp)
            restarted._CodexUsageMonitor__root = _FakeRoot()
            full_one = _snapshot("100%", now, deadline)
            full_two = _snapshot("100%", now + timedelta(seconds=30), deadline)
            full_three = _snapshot("100%", now + timedelta(seconds=60), deadline)
            shown: list = []
            with patch.object(
                restarted,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ), patch.object(
                restarted,
                "_CodexUsageMonitor__get_last_input_tick",
                return_value=None,
                create=True,
            ), patch.object(
                restarted,
                "_CodexUsageMonitor__show_alert_tooltip",
                side_effect=lambda text, lines=None, duration_ms=None: shown.append(
                    (text, lines, duration_ms)
                ),
            ), patch(
                "src.utils.reset_fanfare.play_reset_fanfare", return_value=True
            ) as play_mock:
                restarted.handle_snapshot(full_one)
                restarted.handle_snapshot(full_two)
                restarted.handle_snapshot(full_three)

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)

    def test_confirmed_latch_survives_restart_and_future_deadline_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            initial_deadline = _iso(now + timedelta(hours=2))
            low = _snapshot("70%", now - timedelta(minutes=1), initial_deadline)
            candidate = _snapshot("100%", now, initial_deadline)
            confirmed_at = now + timedelta(seconds=30)
            confirmed = _snapshot("100%", confirmed_at, initial_deadline)
            first = self._make_monitor(tmp)
            first._CodexUsageMonitor__root = _FakeRoot()
            shown: list = []

            with patch.object(
                first,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ), patch.object(
                first,
                "_CodexUsageMonitor__get_last_input_tick",
                return_value=None,
                create=True,
            ), patch.object(
                first,
                "_CodexUsageMonitor__show_alert_tooltip",
                side_effect=lambda text, lines=None, duration_ms=None: shown.append(
                    (text, lines, duration_ms)
                ),
            ), patch(
                "src.utils.reset_fanfare.play_reset_fanfare", return_value=True
            ) as play_mock:
                first.handle_snapshot(low)
                first.handle_snapshot(candidate)
                first.handle_snapshot(confirmed)

                restarted = self._make_monitor(tmp)
                restarted._CodexUsageMonitor__root = _FakeRoot()
                self.assertTrue(
                    restarted._CodexUsageMonitor__limit_reset_state[
                        "five_hour_limit"
                    ]["latched"]
                )
                published_at = now + timedelta(minutes=2)
                published_deadline = _iso(now + timedelta(hours=5))
                published = _snapshot("100%", published_at, published_deadline)
                with patch.object(
                    restarted,
                    "_CodexUsageMonitor__ui_post",
                    side_effect=lambda fn: fn(),
                ), patch.object(
                    restarted,
                    "_CodexUsageMonitor__get_last_input_tick",
                    return_value=None,
                    create=True,
                ), patch.object(
                    restarted,
                    "_CodexUsageMonitor__show_alert_tooltip",
                    side_effect=lambda text, lines=None, duration_ms=None: shown.append(
                        (text, lines, duration_ms)
                    ),
                ):
                    restarted.handle_snapshot(published)

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)


class ClaudeIrregularLimitResetPersistenceTest(unittest.TestCase):
    def _monitor(
        self,
        config_dir: str,
        session: _ClaudeSession,
        clock: dict[str, datetime],
    ) -> ClaudeUsageMonitor:
        monitor = ClaudeUsageMonitor(
            profile_id="irregular-reset-test",
            config_dir=config_dir,
            browser_session_factory=lambda _config: session,
            clock=lambda: clock["now"],
        )
        self.submitted: list[list[str]] = []
        monitor._limit_reset_alert.submit = lambda resets: self.submitted.append(
            [reset.key for reset in resets]
        ) or True
        return monitor

    def test_pending_irregular_reset_survives_claude_monitor_restart_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            future = now + timedelta(hours=2)
            weekly = now + timedelta(days=5)
            clock = {"now": now - timedelta(minutes=1)}
            first_session = _ClaudeSession(
                [
                    _claude_result(30.0, future, 20.0, weekly),
                    _claude_result(0.0, future, 20.0, weekly),
                ]
            )
            first = self._monitor(tmp, first_session, clock)
            first.collect(force=True)
            clock["now"] = now
            first.collect(force=True)

            with open(first._state_path, encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertIn("pending", persisted["limit_reset_state"]["five_hour_limit"])

            second_session = _ClaudeSession(
                [
                    _claude_result(0.0, future, 20.0, weekly),
                    _claude_result(0.0, future, 20.0, weekly),
                    _claude_result(0.0, future, 20.0, weekly),
                ]
            )
            clock["now"] = now + timedelta(seconds=30)
            restarted = self._monitor(tmp, second_session, clock)
            restarted.collect(force=True)
            self.assertEqual(self.submitted, [["five_hour_limit"]])

    def test_legacy_claude_state_seeds_low_sample_and_confirms_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            future = now + timedelta(hours=2)
            weekly = now + timedelta(days=5)
            clock = {"now": now - timedelta(minutes=1)}
            first = self._monitor(
                tmp,
                _ClaudeSession([_claude_result(30.0, future, 20.0, weekly)]),
                clock,
            )
            first.collect(force=True)

            with open(first._state_path, encoding="utf-8") as stream:
                legacy_state = json.load(stream)
            legacy_state.pop("limit_reset_state", None)
            with open(first._state_path, "w", encoding="utf-8") as stream:
                json.dump(legacy_state, stream)

            results = [
                _claude_result(0.0, future, 20.0, weekly),
                _claude_result(0.0, future, 20.0, weekly),
                _claude_result(0.0, future, 20.0, weekly),
            ]
            clock["now"] = now
            restarted = self._monitor(tmp, _ClaudeSession(results), clock)
            restarted.collect(force=True)
            self.assertEqual(self.submitted, [])
            clock["now"] = now + timedelta(seconds=30)
            restarted.collect(force=True)
            self.assertEqual(self.submitted, [["five_hour_limit"]])
            clock["now"] = now + timedelta(seconds=60)
            restarted.collect(force=True)
            self.assertEqual(self.submitted, [["five_hour_limit"]])
