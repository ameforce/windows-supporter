from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest

from src.apps.codex_usage_browser_types import (
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
)
from src.apps.claude_usage_monitor import ClaudeUsageMonitor
from src.apps.codex_usage_monitor import UsageLimitReset
from src.apps.usage_limit_reset_alerts import UsageLimitResetAlert


class _Root:
    def __init__(self) -> None:
        self.after_calls: list = []

    def after(self, delay, fn):
        self.after_calls.append((delay, fn))
        return f"after-{len(self.after_calls)}"


def _reset(key: str, label: str) -> UsageLimitReset:
    return UsageLimitReset(key=key, label=label, previous_reset_at="", new_reset_at="")


class UsageLimitResetAlertTest(unittest.TestCase):
    def _alert(
        self,
        *,
        tick=lambda: None,
        sound=True,
        while_away=True,
        root=None,
        label=None,
        played=lambda: True,
        no_root=False,
    ):
        self.sounds: list = []
        self.tooltips: list = []
        self.root = None if no_root else (root or _Root())
        return UsageLimitResetAlert(
            title="Claude",
            post_ui=lambda fn: (fn(), True)[1],
            get_root=lambda: self.root,
            get_duration_ms=lambda: 7000,
            sound_enabled=sound if callable(sound) else (lambda: sound),
            sound_while_away=while_away if callable(while_away) else (lambda: while_away),
            get_label=label,
            input_tick=tick,
            play_sound=lambda: self.sounds.append(1) or played(),
            show_tooltip=lambda root, lines, ms: self.tooltips.append((lines, ms)),
        )

    def test_flushes_sound_and_tooltip_once_without_input_gate(self) -> None:
        alert = self._alert()
        self.assertTrue(alert.submit([_reset("weekly_limit", "주간 사용 한도")]))

        self.assertEqual(len(self.sounds), 1)
        self.assertEqual(len(self.tooltips), 1)
        lines, duration = self.tooltips[0]
        self.assertEqual(lines[0][0], "Claude 사용 한도 초기화")
        self.assertEqual(lines[1], ("주간 사용 한도 초기화됨", "#16A34A"))
        self.assertEqual(duration, 7000)

    def test_header_names_the_profile_when_label_is_known(self) -> None:
        alert = self._alert(label=lambda: "  업무 계정 ")
        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        self.assertEqual(self.tooltips[0][0][0][0], "Claude 사용 한도 초기화 - 업무 계정")

    def test_header_falls_back_when_label_is_empty_or_fails(self) -> None:
        def broken() -> str:
            raise RuntimeError("manager closed")

        for label in (lambda: "", broken):
            with self.subTest(label=label):
                alert = self._alert(label=label)
                alert.submit([_reset("weekly_limit", "주간 사용 한도")])
                self.assertEqual(self.tooltips[0][0][0][0], "Claude 사용 한도 초기화")

    def test_waits_for_new_user_input_then_flushes_merged_resets(self) -> None:
        ticks = [100]
        alert = self._alert(tick=lambda: ticks[0], while_away=False)
        alert.submit([_reset("five_hour_limit", "5시간 사용 한도")])
        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        self.assertEqual(self.tooltips, [])
        self.assertEqual(len(self.root.after_calls), 1)

        # Still idle: keep waiting.
        _delay, poll = self.root.after_calls[-1]
        poll()
        self.assertEqual(self.tooltips, [])

        ticks[0] = 250
        _delay, poll = self.root.after_calls[-1]
        poll()
        self.assertEqual(len(self.tooltips), 1)
        self.assertEqual(len(self.sounds), 1)
        texts = [line[0] for line in self.tooltips[0][0]]
        self.assertIn("5시간 사용 한도 초기화됨", texts)
        self.assertIn("주간 사용 한도 초기화됨", texts)

    def test_sound_setting_off_still_shows_tooltip(self) -> None:
        alert = self._alert(sound=False)
        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        self.assertEqual(self.sounds, [])
        self.assertEqual(len(self.tooltips), 1)

    def test_away_playback_sounds_at_detection_and_tooltip_waits_silently(self) -> None:
        ticks = [100]
        alert = self._alert(tick=lambda: ticks[0], while_away=True)

        alert.submit([_reset("five_hour_limit", "5시간 사용 한도")])
        # Nobody touched the keyboard: the fanfare is already heard, the
        # tooltip is still held for the user's return.
        self.assertEqual(len(self.sounds), 1)
        self.assertEqual(self.tooltips, [])
        _delay, poll = self.root.after_calls[-1]
        poll()
        self.assertEqual(self.tooltips, [])

        # A second reset detected while still away sounds on its own.
        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        self.assertEqual(len(self.sounds), 2)
        self.assertEqual(self.tooltips, [])

        ticks[0] = 250
        _delay, poll = self.root.after_calls[-1]
        poll()
        self.assertEqual(len(self.tooltips), 1)
        self.assertEqual(len(self.sounds), 2)
        texts = [line[0] for line in self.tooltips[0][0]]
        self.assertIn("5시간 사용 한도 초기화됨", texts)
        self.assertIn("주간 사용 한도 초기화됨", texts)

    def test_away_playback_off_holds_the_fanfare_for_the_return(self) -> None:
        ticks = [100]
        alert = self._alert(tick=lambda: ticks[0], while_away=False)

        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        self.assertEqual(self.sounds, [])
        self.assertEqual(self.tooltips, [])

        ticks[0] = 101
        _delay, poll = self.root.after_calls[-1]
        poll()
        self.assertEqual(len(self.sounds), 1)
        self.assertEqual(len(self.tooltips), 1)

    def test_sound_off_stays_silent_even_with_away_playback_on(self) -> None:
        ticks = [100]
        alert = self._alert(tick=lambda: ticks[0], sound=False, while_away=True)

        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        ticks[0] = 101
        _delay, poll = self.root.after_calls[-1]
        poll()

        self.assertEqual(self.sounds, [])
        self.assertEqual(len(self.tooltips), 1)

    def test_reset_queued_after_away_playback_is_turned_off_sounds_on_return(self) -> None:
        ticks = [100]
        away = [True]
        alert = self._alert(tick=lambda: ticks[0], while_away=lambda: away[0])

        alert.submit([_reset("five_hour_limit", "5시간 사용 한도")])
        self.assertEqual(len(self.sounds), 1)
        away[0] = False
        # Same metric resets again and a new one arrives, neither sounded yet.
        alert.submit([_reset("five_hour_limit", "5시간 사용 한도")])
        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        self.assertEqual(len(self.sounds), 1)

        ticks[0] = 101
        _delay, poll = self.root.after_calls[-1]
        poll()
        self.assertEqual(len(self.sounds), 2)
        self.assertEqual(len(self.tooltips), 1)

    def test_all_resets_sounded_while_away_are_not_replayed_on_return(self) -> None:
        ticks = [100]
        away = [True]
        alert = self._alert(tick=lambda: ticks[0], while_away=lambda: away[0])

        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        away[0] = False
        ticks[0] = 101
        _delay, poll = self.root.after_calls[-1]
        poll()

        self.assertEqual(len(self.sounds), 1)
        self.assertEqual(len(self.tooltips), 1)

    def test_failed_away_playback_is_retried_on_return(self) -> None:
        ticks = [100]
        results = [False, True]
        alert = self._alert(tick=lambda: ticks[0], played=lambda: results.pop(0))

        alert.submit([_reset("weekly_limit", "주간 사용 한도")])
        self.assertEqual(len(self.sounds), 1)

        ticks[0] = 101
        _delay, poll = self.root.after_calls[-1]
        poll()
        # The fanfare was not heard while away (no audio device), so the
        # return tries once more with the tooltip.
        self.assertEqual(len(self.sounds), 2)
        self.assertEqual(len(self.tooltips), 1)

    def test_away_playback_off_without_input_gate_sounds_with_the_tooltip(self) -> None:
        alert = self._alert(while_away=False)
        alert.submit([_reset("weekly_limit", "주간 사용 한도")])

        self.assertEqual(len(self.sounds), 1)
        self.assertEqual(len(self.tooltips), 1)

    def test_missing_root_flushes_the_pending_fanfare_without_a_tooltip(self) -> None:
        for while_away in (True, False):
            with self.subTest(while_away=while_away):
                alert = self._alert(tick=lambda: 100, while_away=while_away, no_root=True)
                alert.submit([_reset("weekly_limit", "주간 사용 한도")])

                self.assertEqual(len(self.sounds), 1)
                self.assertEqual(self.tooltips, [])

    def test_policy_getter_failure_keeps_the_tooltip_and_stays_silent(self) -> None:
        def broken() -> bool:
            raise RuntimeError("manager closed")

        alert = self._alert(sound=broken, while_away=True)
        alert.submit([_reset("weekly_limit", "주간 사용 한도")])

        self.assertEqual(self.sounds, [])
        self.assertEqual(len(self.tooltips), 1)

    def test_submit_reports_failure_without_ui_queue(self) -> None:
        alert = UsageLimitResetAlert(
            title="Claude",
            post_ui=lambda fn: False,
            get_root=lambda: None,
            get_duration_ms=lambda: 7000,
        )
        self.assertFalse(alert.submit([_reset("weekly_limit", "주간 사용 한도")]))
        self.assertFalse(alert.submit([]))


class _Session:
    def __init__(self) -> None:
        self.results: list[BrowserOperationResult] = []
        self.status = BrowserRuntimeStatus(BrowserState.HEADLESS_READY, False, "")

    def collect(self) -> BrowserOperationResult:
        return self.results.pop(0)

    def open_login(self) -> BrowserOperationResult:
        return self.results.pop(0)

    def shutdown(self) -> bool:
        return True

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return self.status


def _probe(
    five_hour_used: float,
    five_hour_reset: datetime,
    weekly_used: float,
    weekly_reset: datetime,
) -> BrowserOperationResult:
    payload = {
        "usage": {
            "five_hour": {"utilization": five_hour_used, "resets_at": five_hour_reset.isoformat()},
            "seven_day": {"utilization": weekly_used, "resets_at": weekly_reset.isoformat()},
        }
    }
    return BrowserOperationResult(
        probe={
            "url": "https://claude.ai/settings/usage",
            "mainText": "Current session",
            "accountId": "org-uuid-1",
            "metricBlocks": [
                {"metric_key": "claude_usage_api", "block_text": json.dumps(payload)}
            ],
        }
    )


class ClaudeLimitResetDetectionTest(unittest.TestCase):
    def _monitor(self, session: _Session, config_dir: str | None = None) -> ClaudeUsageMonitor:
        monitor = ClaudeUsageMonitor(
            profile_id="claude-1",
            config_dir=config_dir,
            browser_session_factory=lambda _config: session,
        )
        self.submitted: list = getattr(self, "submitted", [])
        monitor._limit_reset_alert.submit = lambda resets: self.submitted.append(
            [item.key for item in resets]
        ) or True
        return monitor

    def setUp(self) -> None:
        self.submitted = []

    def test_weekly_window_reset_alerts_once(self) -> None:
        now = datetime.now(timezone.utc)
        session = _Session()
        session.results = [
            _probe(30.0, now + timedelta(hours=3), 95.0, now - timedelta(hours=1)),
            _probe(30.0, now + timedelta(hours=3), 0.0, now + timedelta(days=7)),
            _probe(31.0, now + timedelta(hours=3), 1.0, now + timedelta(days=7)),
        ]
        monitor = self._monitor(session)

        monitor.collect(force=True)
        self.assertEqual(self.submitted, [])
        monitor.collect(force=True)
        self.assertEqual(self.submitted, [["weekly_limit"]])
        monitor.collect(force=True)
        self.assertEqual(self.submitted, [["weekly_limit"]])

    def test_session_window_reset_alerts(self) -> None:
        now = datetime.now(timezone.utc)
        session = _Session()
        session.results = [
            _probe(88.0, now - timedelta(minutes=5), 20.0, now + timedelta(days=3)),
            _probe(0.0, now + timedelta(hours=5), 20.0, now + timedelta(days=3)),
        ]
        monitor = self._monitor(session)
        monitor.collect(force=True)
        monitor.collect(force=True)
        self.assertEqual(self.submitted, [["five_hour_limit"]])

    def test_baselines_persist_across_restart(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            first = _Session()
            first.results = [_probe(30.0, now + timedelta(hours=3), 95.0, now - timedelta(hours=1))]
            monitor = self._monitor(first, config_dir=tmp)
            monitor.collect(force=True)
            with open(monitor._state_path, encoding="utf-8") as stream:
                state = json.load(stream)
            self.assertIn("weekly_limit", state.get("limit_reset_baselines", {}))

            second = _Session()
            second.results = [_probe(30.0, now + timedelta(hours=3), 0.0, now + timedelta(days=7))]
            restarted = self._monitor(second, config_dir=tmp)
            restarted.collect(force=True)
            self.assertEqual(self.submitted, [["weekly_limit"]])

    def test_manual_login_commit_detects_reset(self) -> None:
        now = datetime.now(timezone.utc)
        session = _Session()
        session.results = [
            _probe(30.0, now + timedelta(hours=3), 95.0, now - timedelta(hours=1)),
            _probe(30.0, now + timedelta(hours=3), 0.0, now + timedelta(days=7)),
        ]
        monitor = self._monitor(session)
        monitor.collect(force=True)
        monitor.show_current_status(source="manual_login")
        self.assertEqual(self.submitted, [["weekly_limit"]])

    def test_restart_without_saved_baselines_seeds_them_from_saved_deadlines(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            first = _Session()
            first.results = [_probe(30.0, now + timedelta(hours=3), 95.0, now - timedelta(hours=1))]
            monitor = self._monitor(first, config_dir=tmp)
            monitor.collect(force=True)
            # A state written before baselines existed (pre-upgrade file).
            with open(monitor._state_path, encoding="utf-8") as stream:
                state = json.load(stream)
            state.pop("limit_reset_baselines", None)
            with open(monitor._state_path, "w", encoding="utf-8") as stream:
                json.dump(state, stream)

            second = _Session()
            second.results = [_probe(30.0, now + timedelta(hours=3), 0.0, now + timedelta(days=7))]
            restarted = self._monitor(second, config_dir=tmp)
            self.assertIn("weekly_limit", restarted._limit_reset_baselines)
            restarted.collect(force=True)
            self.assertEqual(self.submitted, [["weekly_limit"]])

    def test_alert_header_uses_label_provider_from_profile_manager(self) -> None:
        monitor = ClaudeUsageMonitor(
            profile_id="claude-1",
            browser_session_factory=lambda _config: _Session(),
        )
        self.assertEqual(monitor._limit_reset_alert._header(), "Claude 사용 한도 초기화")
        monitor.set_alert_label_provider(lambda: "Claude 2")
        self.assertEqual(
            monitor._limit_reset_alert._header(),
            "Claude 사용 한도 초기화 - Claude 2",
        )
        monitor.set_alert_label_provider(None)
        self.assertEqual(monitor._limit_reset_alert._header(), "Claude 사용 한도 초기화")

    def test_sound_setting_roundtrips_in_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._monitor(_Session(), config_dir=tmp)
            monitor._limit_reset_sound_enabled = False
            monitor._save_settings_file()
            reloaded = self._monitor(_Session(), config_dir=tmp)
            self.assertFalse(reloaded._limit_reset_sound_enabled)

    def test_sound_settings_update_snapshot_and_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self._monitor(_Session(), config_dir=tmp)
            snapshot = monitor.get_settings_snapshot()
            self.assertTrue(snapshot["limit_reset_sound_enabled"])
            self.assertTrue(snapshot["limit_reset_sound_while_away"])

            ok, error = monitor.update_settings(
                {
                    "limit_reset_sound_enabled": False,
                    "limit_reset_sound_while_away": False,
                }
            )

            self.assertTrue(ok, error)
            reloaded = self._monitor(_Session(), config_dir=tmp).get_settings_snapshot()
            self.assertFalse(reloaded["limit_reset_sound_enabled"])
            self.assertFalse(reloaded["limit_reset_sound_while_away"])

    def test_manager_sound_policy_overrides_profile_settings(self) -> None:
        monitor = ClaudeUsageMonitor(
            profile_id="claude-1",
            browser_session_factory=lambda _config: _Session(),
        )
        alert = monitor._limit_reset_alert
        self.assertTrue(alert._sound_enabled())
        self.assertTrue(alert._sound_while_away())

        monitor.set_limit_reset_sound_policy_provider(lambda: (False, False))
        self.assertFalse(alert._sound_enabled())
        self.assertFalse(alert._sound_while_away())

        def broken() -> tuple[bool, bool]:
            raise RuntimeError("manager closed")

        # A failing manager falls back to the profile's own switches.
        monitor._limit_reset_sound_while_away = False
        monitor.set_limit_reset_sound_policy_provider(broken)
        self.assertTrue(alert._sound_enabled())
        self.assertFalse(alert._sound_while_away())

        monitor.set_limit_reset_sound_policy_provider(None)
        self.assertTrue(alert._sound_enabled())
        self.assertFalse(alert._sound_while_away())


if __name__ == "__main__":
    unittest.main()
