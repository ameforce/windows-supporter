import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    UsageSnapshot,
    compute_usage_limit_resets,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _metrics() -> dict[str, str]:
    return {
        "five_hour_limit": "10 / 40",
        "weekly_limit": "100 / 300",
        "monthly_limit": "50 / 200",
        "remaining_credit": "259",
    }


def _snapshot(
    reset_info: dict[str, str] | None = None,
    captured_at: str = "2026-09-18T10:00:00+09:00",
) -> UsageSnapshot:
    return UsageSnapshot.from_metrics(
        _metrics(),
        captured_at=captured_at,
        reset_info=reset_info or {},
    )


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls = []

    def after(self, delay, fn):
        self.after_calls.append((delay, fn))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, _after_id):
        return None


class ComputeUsageLimitResetsTest(unittest.TestCase):
    def test_no_baselines_returns_empty(self) -> None:
        self.assertEqual(compute_usage_limit_resets(None, _snapshot()), [])
        self.assertEqual(compute_usage_limit_resets({}, _snapshot()), [])

    def test_first_observation_without_prior_reset_at_returns_empty(self) -> None:
        current = _snapshot(
            {"five_hour_limit_reset_at": "2026-09-18T15:00:00+09:00"}
        )
        self.assertEqual(compute_usage_limit_resets({}, current), [])

    def test_elapsed_reset_that_rolls_forward_is_detected(self) -> None:
        baselines = {"five_hour_limit": "2026-09-18T09:00:00+09:00"}
        current = _snapshot(
            {"five_hour_limit_reset_at": "2026-09-18T14:00:00+09:00"}
        )
        now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        resets = compute_usage_limit_resets(baselines, current, now=now)
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0].key, "five_hour_limit")
        self.assertEqual(resets[0].label, "5시간 사용 한도")
        self.assertEqual(resets[0].new_reset_at, "2026-09-18T14:00:00+09:00")

    def test_reset_at_unchanged_returns_empty(self) -> None:
        baselines = {"weekly_limit": "2026-09-20T09:00:00+09:00"}
        current = _snapshot(
            {"weekly_limit_reset_at": "2026-09-20T09:00:00+09:00"}
        )
        now = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_usage_limit_resets(baselines, current, now=now), [])

    def test_small_forward_move_is_not_a_reset(self) -> None:
        baselines = {"five_hour_limit": "2026-09-18T09:00:00+09:00"}
        current = _snapshot(
            {"five_hour_limit_reset_at": "2026-09-18T09:30:00+09:00"}
        )
        now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(compute_usage_limit_resets(baselines, current, now=now), [])

    def test_future_baseline_is_a_schedule_change_not_a_reset(self) -> None:
        baselines = {"five_hour_limit": "2026-09-18T12:00:00+09:00"}
        current = _snapshot(
            {"five_hour_limit_reset_at": "2026-09-18T18:00:00+09:00"}
        )
        now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(compute_usage_limit_resets(baselines, current, now=now), [])

    def test_unparseable_reset_values_are_ignored(self) -> None:
        baselines = {"five_hour_limit": "not-a-date"}
        current = _snapshot(
            {"five_hour_limit_reset_at": "2026-09-18T14:00:00+09:00"}
        )
        now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(compute_usage_limit_resets(baselines, current, now=now), [])

    def test_missing_new_reset_at_returns_empty(self) -> None:
        baselines = {"monthly_limit": "2026-09-01T00:00:00+09:00"}
        current = _snapshot({})
        now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_usage_limit_resets(baselines, current, now=now), [])

    def test_multiple_limits_reset_together(self) -> None:
        baselines = {
            "five_hour_limit": "2026-09-18T09:00:00+09:00",
            "weekly_limit": "2026-09-17T09:00:00+09:00",
            "monthly_limit": "2026-09-01T00:00:00+09:00",
        }
        current = _snapshot(
            {
                "five_hour_limit_reset_at": "2026-09-18T14:00:00+09:00",
                "weekly_limit_reset_at": "2026-09-24T09:00:00+09:00",
                "monthly_limit_reset_at": "2026-10-01T00:00:00+09:00",
            }
        )
        now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        resets = compute_usage_limit_resets(baselines, current, now=now)
        self.assertEqual(
            [item.key for item in resets],
            ["five_hour_limit", "weekly_limit", "monthly_limit"],
        )

    def test_reset_detected_even_when_usage_stays_zero(self) -> None:
        zero_metrics = {
            "five_hour_limit": "0 / 40",
            "weekly_limit": "0 / 300",
            "monthly_limit": "0 / 200",
        }
        baselines = {"five_hour_limit": "2026-09-18T09:00:00+09:00"}
        current = UsageSnapshot.from_metrics(
            zero_metrics,
            captured_at="2026-09-18T10:00:00+09:00",
            reset_info={"five_hour_limit_reset_at": "2026-09-18T14:00:00+09:00"},
        )
        now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        resets = compute_usage_limit_resets(baselines, current, now=now)
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0].key, "five_hour_limit")

    def test_backward_move_does_not_alert(self) -> None:
        baselines = {"five_hour_limit": "2026-09-18T14:00:00+09:00"}
        current = _snapshot(
            {"five_hour_limit_reset_at": "2026-09-18T09:00:00+09:00"}
        )
        now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(compute_usage_limit_resets(baselines, current, now=now), [])


class MonitorLimitResetNotificationTest(unittest.TestCase):
    def _make_monitor(self, config_dir: str, **kwargs) -> CodexUsageMonitor:
        class _Session:
            def shutdown(self) -> bool:
                return True

        kwargs.setdefault("browser_session_factory", lambda _config: _Session())
        return CodexUsageMonitor(
            config_dir=config_dir,
            profile_dir=os.path.join(config_dir, "profile"),
            **kwargs,
        )

    def _rolled_reset_pair(self) -> tuple[UsageSnapshot, UsageSnapshot]:
        now = datetime.now(timezone.utc)
        previous = UsageSnapshot.from_metrics(
            _metrics(),
            captured_at=_iso(now - timedelta(hours=2)),
            reset_info={"five_hour_limit_reset_at": _iso(now - timedelta(hours=1))},
        )
        current = UsageSnapshot.from_metrics(
            _metrics(),
            captured_at=_iso(now),
            reset_info={"five_hour_limit_reset_at": _iso(now + timedelta(hours=4))},
        )
        return previous, current

    def test_reset_alert_shows_tooltip_and_plays_sound_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            monitor._CodexUsageMonitor__root = _FakeRoot()
            previous, current = self._rolled_reset_pair()
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
                monitor.handle_snapshot(previous)
                self.assertEqual(shown, [])
                monitor.handle_snapshot(current)

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)
            lines = shown[0][1] or []
            joined = " | ".join(str(line[0]) for line in lines)
            self.assertIn("사용 한도 초기화", joined)
            self.assertIn("5시간 사용 한도 초기화됨", joined)

    def test_same_reset_timestamp_does_not_realert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            monitor._CodexUsageMonitor__root = _FakeRoot()
            previous, current = self._rolled_reset_pair()
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
                monitor.handle_snapshot(previous)
                monitor.handle_snapshot(current)
                monitor.handle_snapshot(
                    UsageSnapshot.from_dict(current.to_dict())
                )
                monitor.handle_snapshot(
                    UsageSnapshot.from_dict(current.to_dict())
                )

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)

    def test_oscillating_reset_at_does_not_realert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            monitor._CodexUsageMonitor__root = _FakeRoot()
            previous, current = self._rolled_reset_pair()
            glitch = UsageSnapshot.from_metrics(
                _metrics(),
                captured_at=_iso(datetime.now(timezone.utc)),
                reset_info={
                    "five_hour_limit_reset_at": previous.five_hour_limit_reset_at
                },
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
                monitor.handle_snapshot(previous)
                monitor.handle_snapshot(current)
                monitor.handle_snapshot(glitch)
                monitor.handle_snapshot(
                    UsageSnapshot.from_dict(current.to_dict())
                )

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)

    def test_missing_reset_at_cycle_does_not_lose_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            monitor._CodexUsageMonitor__root = _FakeRoot()
            previous, current = self._rolled_reset_pair()
            gap = UsageSnapshot.from_metrics(
                _metrics(),
                captured_at=_iso(datetime.now(timezone.utc)),
                reset_info={},
                reported_metric_keys=("five_hour_limit",),
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
                monitor.handle_snapshot(previous)
                monitor.handle_snapshot(gap)
                monitor.handle_snapshot(current)

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)

    def test_sound_disabled_setting_suppresses_playback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            monitor._CodexUsageMonitor__root = _FakeRoot()
            monitor._CodexUsageMonitor__limit_reset_sound_enabled = False
            previous, current = self._rolled_reset_pair()
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
                monitor.handle_snapshot(previous)
                monitor.handle_snapshot(current)

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 0)

    def test_managed_mode_emits_sink_event_and_shows_local_tooltip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink_events: list = []
            monitor = self._make_monitor(
                tmp,
                notification_sink=lambda event: sink_events.append(event),
                suppress_normal_tooltips=True,
            )
            monitor._CodexUsageMonitor__root = _FakeRoot()
            previous, current = self._rolled_reset_pair()
            presented: list = []
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
                "_CodexUsageMonitor__present_local_tooltip",
                side_effect=lambda text, lines=None, duration_ms=None: presented.append(
                    (text, lines, duration_ms)
                ),
            ), patch(
                "src.utils.reset_fanfare.play_reset_fanfare", return_value=True
            ):
                monitor.handle_snapshot(previous)
                monitor.handle_snapshot(current)

            self.assertEqual(len(sink_events), 1)
            self.assertEqual(len(presented), 1)
            lines = sink_events[0].get("lines") or []
            joined = " | ".join(str(line[0]) for line in lines)
            self.assertIn("5시간 사용 한도 초기화됨", joined)

    def test_persisted_state_detects_reset_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            previous, current = self._rolled_reset_pair()
            state_payload = {
                "snapshot_contract_version": 2,
                "session_state": "logged_in",
                "snapshot_backfill_allowed": True,
                "last_snapshot": previous.to_dict(),
                "usage_history": [],
            }
            with open(
                os.path.join(tmp, "codex_usage_state.json"), "w", encoding="utf-8"
            ) as fp:
                json.dump(state_payload, fp)

            monitor = self._make_monitor(tmp)
            monitor._CodexUsageMonitor__root = _FakeRoot()
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
            ):
                monitor.handle_snapshot(current)

            self.assertEqual(len(shown), 1)
            lines = shown[0][1] or []
            joined = " | ".join(str(line[0]) for line in lines)
            self.assertIn("5시간 사용 한도 초기화됨", joined)

    def test_sound_setting_roundtrip_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            self.assertTrue(
                monitor.get_settings_snapshot()["limit_reset_sound_enabled"]
            )
            monitor._CodexUsageMonitor__limit_reset_sound_enabled = False
            monitor._CodexUsageMonitor__save_settings()

            reloaded = self._make_monitor(tmp)
            self.assertFalse(
                reloaded.get_settings_snapshot()["limit_reset_sound_enabled"]
            )

    def test_limit_reset_baselines_persist_in_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            monitor._CodexUsageMonitor__limit_reset_baselines = {
                "five_hour_limit": "2026-09-18T14:00:00+09:00"
            }
            monitor._CodexUsageMonitor__save_state()
            with open(
                os.path.join(tmp, "codex_usage_state.json"), encoding="utf-8"
            ) as fp:
                payload = json.load(fp)
            self.assertEqual(
                payload["limit_reset_baselines"],
                {"five_hour_limit": "2026-09-18T14:00:00+09:00"},
            )
            reloaded = self._make_monitor(tmp)
            self.assertEqual(
                reloaded._CodexUsageMonitor__limit_reset_baselines,
                {"five_hour_limit": "2026-09-18T14:00:00+09:00"},
            )

    def test_reset_alert_waits_for_user_input_then_flushes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._make_monitor(tmp)
            root = _FakeRoot()
            monitor._CodexUsageMonitor__root = root
            previous, current = self._rolled_reset_pair()
            shown: list = []
            with patch.object(
                monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ), patch.object(
                monitor,
                "_CodexUsageMonitor__get_last_input_tick",
                side_effect=[100, 100, 101],
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
                monitor.handle_snapshot(previous)
                monitor.handle_snapshot(current)
                self.assertEqual(shown, [])
                self.assertEqual(len(root.after_calls), 1)
                root.after_calls[-1][1]()
                self.assertEqual(shown, [])
                self.assertEqual(len(root.after_calls), 2)
                root.after_calls[-1][1]()

            self.assertEqual(len(shown), 1)
            self.assertEqual(play_mock.call_count, 1)


class ResetFanfareWavTest(unittest.TestCase):
    def test_build_fanfare_wav_bytes_is_valid_non_silent_wav(self) -> None:
        import io
        import wave

        from src.utils.reset_fanfare import build_fanfare_wav_bytes

        data = build_fanfare_wav_bytes()
        self.assertGreater(len(data), 1000)
        with wave.open(io.BytesIO(data), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), 22050)
            frames = wf.readframes(wf.getnframes())
        self.assertTrue(any(b != 0 for b in frames[:2000]))
        duration = wf.getnframes() / float(wf.getframerate())
        self.assertGreater(duration, 0.4)
        self.assertLess(duration, 2.0)


if __name__ == "__main__":
    unittest.main()
