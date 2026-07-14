import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    UsageChange,
    UsageSnapshot,
    merge_snapshot_with_previous,
)


class CodexUsageMonitorFlowE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        root = Path(self._tempdir.name)
        self._root = root
        self._config_dir = root / "config"
        profile_dir = root / "profile"
        self._profile_dir = profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "session.txt").write_text("test", encoding="utf-8")
        self.monitor = CodexUsageMonitor(
            config_dir=str(self._config_dir),
            profile_dir=str(profile_dir),
        )
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot()

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_profile_files_without_persisted_login_do_not_enable_monitoring(self) -> None:
        status = self.monitor.get_runtime_status()

        self.assertEqual(status.get("session_state"), "logged_out")
        self.assertFalse(bool(status.get("auto_monitoring_active")))
        self.assertTrue(bool(status.get("can_login")))

    def test_handle_snapshot_persists_logged_in_session_state(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )
        self.monitor._CodexUsageMonitor__set_auth_attention(
            "cloudflare_challenge",
            source="monitor_tick",
        )

        self.monitor.handle_snapshot(snapshot)

        state_path = Path(self.monitor._CodexUsageMonitor__state_path)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("session_state"), "logged_in")
        self.assertFalse(bool(self.monitor.get_runtime_status().get("auth_attention_required")))


    def test_background_auth_error_after_snapshot_marks_auth_attention(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.cancelled = []

            def after_cancel(self, token):
                self.cancelled.append(token)
                return None

        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        for source, error in (
            ("auto_monitor", "login_required"),
            ("monitor_tick", "cloudflare_challenge"),
        ):
            with self.subTest(source=source, error=error):
                root = _DummyRoot()
                self.monitor._CodexUsageMonitor__root = root
                self.monitor.handle_snapshot(snapshot)
                self.monitor._CodexUsageMonitor__monitor_after_id = "tick-1"
                self.monitor._CodexUsageMonitor__set_auth_attention(
                    "cloudflare_challenge",
                    source="auto_monitor",
                )

                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_tooltip",
                ):
                    self.monitor._CodexUsageMonitor__handle_collect_error(
                        error,
                        source=source,
                    )

                status = self.monitor.get_runtime_status()
                state_path = Path(self.monitor._CodexUsageMonitor__state_path)
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(status.get("session_state"), "logged_in")
                self.assertEqual(payload.get("session_state"), "logged_in")
                self.assertTrue(bool(status.get("auth_attention_required")))
                self.assertEqual(status.get("auth_attention_reason"), error)
                self.assertEqual(status.get("auth_attention_source"), source)
                self.assertEqual(status.get("monitor_state"), "paused_auth_required")
                self.assertFalse(bool(status.get("auto_monitoring_active")))
                self.assertTrue(bool(status.get("can_login")))
                self.assertTrue(bool(payload.get("auth_attention_required")))
                self.assertEqual(payload.get("auth_attention_reason"), error)
                self.assertEqual(payload.get("auth_attention_source"), source)
                self.assertEqual(
                    self.monitor._CodexUsageMonitor__monitor_after_id,
                    None,
                )
                self.assertEqual(root.cancelled, ["tick-1"])

                reloaded = CodexUsageMonitor(
                    config_dir=str(self._config_dir),
                    profile_dir=str(self._profile_dir),
                )
                reloaded_status = reloaded.get_runtime_status()
                self.assertTrue(bool(reloaded_status.get("auth_attention_required")))
                self.assertEqual(reloaded_status.get("auth_attention_reason"), error)
                self.assertEqual(reloaded_status.get("monitor_state"), "paused_auth_required")

    def test_background_auth_retry_success_replaces_stale_snapshot_without_backfill(self) -> None:
        previous = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T12:00:00",
        )
        self.monitor.handle_snapshot(previous)

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__show_tooltip",
        ):
            self.monitor._CodexUsageMonitor__handle_collect_error(
                "login_required",
                source="auto_monitor",
            )

        fresh_after_login = UsageSnapshot.from_metrics(
            {"five_hour_limit": "16 / 40"},
            captured_at="2026-03-30T12:10:00",
        )
        self.monitor.handle_snapshot(fresh_after_login)

        latest = self.monitor.get_last_snapshot()
        state_path = Path(self.monitor._CodexUsageMonitor__state_path)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        persisted = payload.get("last_snapshot") or {}

        self.assertEqual(latest.five_hour_limit, "60%")
        self.assertEqual(latest.weekly_limit, "")
        self.assertEqual(latest.gpt_5_3_codex_spark_five_hour_limit, "")
        self.assertEqual(latest.gpt_5_3_codex_spark_weekly_limit, "")
        self.assertEqual(latest.remaining_credit, "")
        self.assertEqual(persisted.get("weekly_limit"), "")
        self.assertEqual(payload.get("session_state"), "logged_in")
        history = self.monitor.get_runtime_status().get("usage_history") or []
        saved_history = payload.get("usage_history") or []
        self.assertEqual(len(history), 1)
        self.assertEqual(len(saved_history), 1)
        self.assertEqual(history[0].get("captured_at"), "2026-03-30T12:10:00")
        self.assertEqual(saved_history[0].get("weekly_limit"), "")

    def test_load_state_logged_in_with_profile_enables_monitoring(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )
        self._config_dir.mkdir(parents=True, exist_ok=True)
        state_path = self._config_dir / "codex_usage_state.json"
        state_path.write_text(
            json.dumps(
                {
                    "session_state": "logged_in",
                    "last_snapshot": snapshot.to_dict(),
                }
            ),
            encoding="utf-8",
        )

        reloaded = CodexUsageMonitor(
            config_dir=str(self._config_dir),
            profile_dir=str(self._profile_dir),
        )

        status = reloaded.get_runtime_status()
        self.assertEqual(status.get("session_state"), "logged_in")
        self.assertTrue(bool(status.get("auto_monitoring_active")))

    def test_load_state_without_session_state_migrates_logged_out(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )
        self._config_dir.mkdir(parents=True, exist_ok=True)
        state_path = self._config_dir / "codex_usage_state.json"
        state_path.write_text(
            json.dumps({"last_snapshot": snapshot.to_dict()}),
            encoding="utf-8",
        )

        reloaded = CodexUsageMonitor(
            config_dir=str(self._config_dir),
            profile_dir=str(self._profile_dir),
        )

        status = reloaded.get_runtime_status()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(status.get("session_state"), "logged_out")
        self.assertEqual(payload.get("session_state"), "logged_out")
        self.assertFalse(bool(status.get("auto_monitoring_active")))

    def test_load_state_logged_in_without_profile_becomes_logged_out(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )
        missing_profile = self._root / "missing-profile"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        state_path = self._config_dir / "codex_usage_state.json"
        state_path.write_text(
            json.dumps(
                {
                    "session_state": "logged_in",
                    "last_snapshot": snapshot.to_dict(),
                }
            ),
            encoding="utf-8",
        )

        reloaded = CodexUsageMonitor(
            config_dir=str(self._config_dir),
            profile_dir=str(missing_profile),
        )

        status = reloaded.get_runtime_status()
        self.assertEqual(status.get("session_state"), "logged_out")
        self.assertFalse(bool(status.get("auto_monitoring_active")))

    def test_handle_snapshot_baseline_and_change_flow(self) -> None:
        baseline = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        same_again = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:10:00",
        )
        changed = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "259",
            },
            captured_at="2026-03-30T10:20:00",
        )

        with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
            first_changes = self.monitor.handle_snapshot(baseline)
            second_changes = self.monitor.handle_snapshot(same_again)
            third_changes = self.monitor.handle_snapshot(changed)

        self.assertEqual(first_changes, [])
        self.assertEqual(second_changes, [])
        self.assertEqual(len(third_changes), 2)
        labels = [c.label for c in third_changes]
        self.assertIn("5시간 사용 한도", labels)
        self.assertIn("남은 크레딧", labels)

    def test_partial_snapshot_uses_previous_values_conservatively(self) -> None:
        baseline = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        partial = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "",
                "gpt_5_3_codex_spark_five_hour_limit": "",
                "gpt_5_3_codex_spark_weekly_limit": "",
                "remaining_credit": "",
            },
            captured_at="2026-03-30T10:10:00",
        )

        with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
            self.monitor.handle_snapshot(baseline)
            changes = self.monitor.handle_snapshot(partial)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].label, "5시간 사용 한도")


    def test_classify_usage_probe_error_detects_login_url(self) -> None:
        err = self.monitor._CodexUsageMonitor__classify_usage_probe_error(
            {"url": "https://auth.openai.com/log-in", "mainText": ""}
        )

        self.assertEqual(err, "login_required")

    def test_classify_usage_probe_error_detects_explicit_login_text(self) -> None:
        err = self.monitor._CodexUsageMonitor__classify_usage_probe_error(
            {
                "url": "https://chatgpt.com/codex/settings/usage",
                "mainText": "Codex 사용량을 보려면 로그인이 필요합니다.",
            }
        )

        self.assertEqual(err, "login_required")

    def test_classify_usage_probe_error_rejects_broad_account_markers(self) -> None:
        for text in ("Sign up", "Continue with Google"):
            with self.subTest(text=text):
                err = self.monitor._CodexUsageMonitor__classify_usage_probe_error(
                    {
                        "url": "https://chatgpt.com/codex/settings/usage",
                        "mainText": text,
                    }
                )

                self.assertEqual(err, "parse_failed")

    def test_classify_usage_probe_error_keeps_usage_page_without_metrics_parse_failed(
        self,
    ) -> None:
        err = self.monitor._CodexUsageMonitor__classify_usage_probe_error(
            {
                "url": "https://chatgpt.com/codex/settings/usage",
                "mainText": "5-hour usage limit",
                "metricBlocks": [],
            }
        )

        self.assertEqual(err, "parse_failed")


    def test_show_change_tooltip_also_shows_current_credit(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        current = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "26%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "959",
            },
            captured_at="2026-03-30T12:30:00",
        )
        changes = self.monitor.handle_snapshot(current)
        self.assertEqual(changes, [])
        only_change = [
            self.monitor.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {
                        "five_hour_limit": "25%",
                        "weekly_limit": "28%",
                        "gpt_5_3_codex_spark_five_hour_limit": "100%",
                        "gpt_5_3_codex_spark_weekly_limit": "100%",
                        "remaining_credit": "959",
                    },
                    captured_at="2026-03-30T12:35:00",
                )
            )[0]
        ]

        captured = {}

        def fake_show(text, lines=None, duration_ms=None):
            captured["text"] = text
            captured["lines"] = lines or []
            captured["duration_ms"] = duration_ms

        with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip", side_effect=fake_show):
            self.monitor._CodexUsageMonitor__show_change_tooltip(
                only_change,
                self.monitor.get_last_snapshot(),
            )

        lines = captured.get("lines", [])
        line_texts = [str(line[0]) for line in lines]
        joined = " | ".join(str(line[0]) for line in lines)
        self.assertTrue(lines)
        self.assertEqual(lines[0][0], "Codex 현재 사용량")
        self.assertIn("--------------------------------", line_texts)
        self.assertLess(line_texts.index("--------------------------------"), line_texts.index("변경"))
        self.assertIn("변경", joined)
        self.assertIn("남은 크레딧: 959", joined)

    def test_show_change_tooltip_uses_red_and_green_colors(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:40:00",
        )
        changes = [
            UsageChange(
                key="five_hour_limit",
                label="5시간 사용 한도",
                before="26%",
                after="25%",
            ),
            UsageChange(
                key="remaining_credit",
                label="남은 크레딧",
                before="959",
                after="960",
            ),
        ]
        captured = {}

        def fake_show(text, lines=None, duration_ms=None):
            captured["text"] = text
            captured["lines"] = lines or []
            captured["duration_ms"] = duration_ms

        with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip", side_effect=fake_show):
            self.monitor._CodexUsageMonitor__show_change_tooltip(changes, snapshot)

        color_map = {str(line[0]): line[1] for line in captured.get("lines", [])}
        self.assertEqual(
            color_map.get("5시간 사용 한도: 26% -> 25%"),
            "#DC2626",
        )
        self.assertEqual(
            color_map.get("남은 크레딧: 959 -> 960"),
            "#16A34A",
        )

    def test_build_snapshot_lines_formats_timestamp_without_t(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:45:00",
        )
        lines = self.monitor._CodexUsageMonitor__build_snapshot_lines(snapshot)
        joined = " | ".join(str(line[0]) for line in lines)
        self.assertIn("확인: 2026-03-30 12:45:00", joined)

    def test_build_snapshot_lines_converts_utc_timestamp_to_kst(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T00:00:00+00:00",
        )
        lines = self.monitor._CodexUsageMonitor__build_snapshot_lines(snapshot)
        joined = " | ".join(str(line[0]) for line in lines)
        self.assertIn("확인: 2026-03-30 09:00:00", joined)

    def test_build_snapshot_lines_shows_reset_countdowns(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:45:00",
            reset_info={
                "five_hour_limit_reset_at": "2026-03-30T15:01:02+09:00",
                "weekly_limit_reset_at": "2026-04-02T12:00:04+09:00",
                "gpt_5_3_codex_spark_five_hour_limit_reset_at": "2026-03-30T12:08:03+09:00",
                "gpt_5_3_codex_spark_weekly_limit_reset_at": "2026-04-01T12:14:05+09:00",
            },
        )
        now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone(timedelta(hours=9)))

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__now_local_datetime",
            return_value=now,
        ):
            lines = self.monitor._CodexUsageMonitor__build_snapshot_lines(snapshot)

        line_texts = [str(line[0]) for line in lines]
        expected = [
            "5시간 사용 한도: 25%",
            "      초기화: 15:01:02 (03h 01m 02s)",
            "주간 사용 한도: 28%",
            "      초기화: 04/02 12:00:04 (3d 00h 00m 04s)",
            "gpt-5.3-codex-spark 5시간 사용 한도: 100%",
            "      초기화: 12:08:03 (00h 08m 03s)",
            "gpt-5.3-codex-spark 주간 사용 한도: 100%",
            "      초기화: 04/01 12:14:05 (2d 00h 14m 05s)",
        ]
        self.assertEqual(line_texts[: len(expected)], expected)

    def test_snapshot_tooltip_lines_refresh_reset_countdown(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:45:00",
            reset_info={"five_hour_limit_reset_at": "2026-03-30T15:00:00+09:00"},
        )
        now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone(timedelta(hours=9)))
        later = datetime(2026, 3, 30, 12, 0, 10, tzinfo=timezone(timedelta(hours=9)))
        captured = {}

        def fake_show(_text, lines=None, duration_ms=None):
            captured["lines"] = lines
            captured["duration_ms"] = duration_ms

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__now_local_datetime",
            side_effect=[now, now, later, later],
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
                side_effect=fake_show,
            ):
                self.monitor._CodexUsageMonitor__show_snapshot_tooltip(
                    snapshot,
                    title="Codex 현재 사용량",
                )
            lines = captured["lines"]
            refreshed = lines.refresh()

        initial_text = " | ".join(str(line[0]) for line in captured["lines"])
        refreshed_text = " | ".join(str(line[0]) for line in refreshed)
        self.assertIn("      초기화: 15:00:00 (03h 00m 00s)", initial_text)
        self.assertIn("      초기화: 15:00:00 (02h 59m 50s)", refreshed_text)

    def test_reset_display_omits_days_only_for_five_hour_limits(self) -> None:
        now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone(timedelta(hours=9)))

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__now_local_datetime",
            return_value=now,
        ):
            five_hour = self.monitor.format_reset_at_for_display(
                "2026-03-30T15:00:00+09:00",
                "five_hour_limit_reset_at",
            )
            spark_five_hour = self.monitor.format_reset_at_for_display(
                "2026-03-30T12:08:03+09:00",
                "gpt_5_3_codex_spark_five_hour_limit_reset_at",
            )
            weekly = self.monitor.format_reset_at_for_display(
                "2026-04-02T12:00:04+09:00",
                "weekly_limit_reset_at",
            )

        self.assertEqual(five_hour, "2026-03-30 15:00:00 (03h 00m 00s)")
        self.assertEqual(spark_five_hour, "2026-03-30 12:08:03 (00h 08m 03s)")
        self.assertEqual(weekly, "2026-04-02 12:00:04 (3d 00h 00m 04s)")

    def test_merge_snapshot_preserves_previous_reset_times_when_missing(self) -> None:
        previous = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:45:00",
            reset_info={
                "five_hour_limit_reset_at": "2026-03-30T15:00:00+09:00",
                "weekly_limit_reset_at": "2026-04-02T12:00:00+09:00",
                "gpt_5_3_codex_spark_five_hour_limit_reset_at": "2026-03-30T12:08:00+09:00",
                "gpt_5_3_codex_spark_weekly_limit_reset_at": "2026-04-01T12:14:00+09:00",
            },
        )
        current = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "959",
            },
            captured_at="2026-03-30T13:00:00",
        )

        merged = merge_snapshot_with_previous(current, previous)

        self.assertEqual(merged.five_hour_limit_reset_at, "2026-03-30T15:00:00+09:00")
        self.assertEqual(merged.weekly_limit_reset_at, "2026-04-02T12:00:00+09:00")
        self.assertEqual(
            merged.gpt_5_3_codex_spark_five_hour_limit_reset_at,
            "2026-03-30T12:08:00+09:00",
        )
        self.assertEqual(
            merged.gpt_5_3_codex_spark_weekly_limit_reset_at,
            "2026-04-01T12:14:00+09:00",
        )

    def test_show_current_status_shows_loading_tooltip_for_manual_query(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:50:00",
        )
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                def fake_collect_guarded(source, on_acquired=None):
                    _ = source
                    if callable(on_acquired):
                        on_acquired()
                    return snapshot, None

                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    side_effect=fake_collect_guarded,
                ):
                    with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_tooltip",
                            side_effect=fake_show,
                        ):
                            self.monitor.show_current_status(force_refresh=True)

        self.assertTrue(shown)
        self.assertEqual(shown[0], ("Codex 사용량 조회 중...", None, 0))
        titles = [entry[1][0][0] for entry in shown if entry[1]]
        self.assertIn("Codex 현재 사용량", titles)

    def test_show_current_status_shows_login_tooltip_for_manual_login(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:50:00",
        )
        self.monitor._CodexUsageMonitor__last_snapshot = snapshot
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []
        seen_sources: list[str] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        def fake_collect_guarded(source, on_acquired=None):
            seen_sources.append(str(source))
            if callable(on_acquired):
                on_acquired()
            return snapshot, None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    side_effect=fake_collect_guarded,
                ):
                    with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_tooltip",
                            side_effect=fake_show,
                        ):
                            self.monitor.show_current_status(
                                force_refresh=True,
                                source="manual_login",
                            )

        self.assertEqual(seen_sources, ["manual_login"])
        self.assertTrue(shown)
        self.assertEqual(
            shown[0],
            (
                "Codex 로그인 창을 여는 중...",
                None,
                0,
            ),
        )
        self.assertNotIn("사용량 조회 중", shown[0][0])

    def test_show_current_status_shows_cached_snapshot_while_manual_query_runs(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "gpt_5_3_codex_spark_five_hour_limit": "98%",
                "gpt_5_3_codex_spark_weekly_limit": "99%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30T12:45:00",
        )
        refreshed = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:50:00",
        )
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                def fake_collect_guarded(source, on_acquired=None):
                    _ = source
                    if callable(on_acquired):
                        on_acquired()
                    return refreshed, None

                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    side_effect=fake_collect_guarded,
                ):
                    with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_tooltip",
                            side_effect=fake_show,
                        ):
                            self.monitor.show_current_status(force_refresh=True)

        first_lines = shown[0][1] or []
        first_joined = " | ".join(str(line[0]) for line in first_lines)
        self.assertEqual(first_lines[0][0], "Codex 최근 사용량 (조회 중...)")
        self.assertIn("5시간 사용 한도: 24%", first_joined)
        self.assertEqual(shown[0][2], 0)

    def test_show_current_status_ignores_when_collect_already_busy(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "collect_busy"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                        side_effect=fake_show,
                    ):
                        self.monitor.show_current_status(force_refresh=True)

        self.assertEqual(len(shown), 1)
        self.assertIn("이미 Codex 사용량 조회가 진행 중입니다.", shown[0][0])
        self.assertIn("완료되면 결과를 자동으로 표시합니다.", shown[0][0])
        self.assertEqual(shown[0][2], 0)

    def test_show_current_status_auto_monitor_skips_when_auth_attention_required(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__set_monitor_state("running")
        self.monitor._CodexUsageMonitor__set_auth_attention(
            "cloudflare_challenge",
            source="auto_monitor",
        )

        with patch("src.apps.codex_usage_monitor.threading.Thread") as thread_ctor:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
            ) as collect_guarded:
                with patch.object(self.monitor, "_CodexUsageMonitor__log") as log:
                    self.monitor.show_current_status(
                        force_refresh=True,
                        source="auto_monitor",
                    )

        thread_ctor.assert_not_called()
        collect_guarded.assert_not_called()
        log.assert_called_once_with(
            "collect skip source=auto_monitor reason=cloudflare_challenge"
        )
        status = self.monitor.get_runtime_status()
        self.assertEqual(status.get("monitor_state"), "paused_auth_required")
        self.assertTrue(bool(status.get("auth_attention_required")))

    def test_show_current_status_manual_login_runs_when_auth_attention_required(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__set_auth_attention(
            "cloudflare_challenge",
            source="auto_monitor",
        )

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "collect_cancelled"),
                ) as collect_guarded:
                    with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip"):
                        self.monitor.show_current_status(
                            force_refresh=True,
                            source="manual_login",
                        )

        collect_guarded.assert_called_once()
        self.assertEqual(collect_guarded.call_args.kwargs.get("source"), "manual_login")

    def test_show_current_status_manual_login_busy_avoids_usage_query_wording(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "collect_busy"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                        side_effect=fake_show,
                    ):
                        self.monitor.show_current_status(
                            force_refresh=True,
                            source="manual_login",
                        )

        self.assertEqual(len(shown), 1)
        self.assertIn("현재 Codex 작업이 진행 중입니다.", shown[0][0])
        self.assertIn("로그인 창", shown[0][0])
        self.assertNotIn("사용량 조회", shown[0][0])
        self.assertFalse(self.monitor._CodexUsageMonitor__consume_manual_query_pending_result())
        self.assertEqual(shown[0][2], 0)

    def test_show_current_status_busy_shows_cached_snapshot_lines(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30 12:58:00",
        )
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "collect_busy"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                        side_effect=fake_show,
                    ):
                        self.monitor.show_current_status(force_refresh=True)

        self.assertEqual(len(shown), 1)
        text, lines, duration = shown[0]
        self.assertEqual(text, "")
        self.assertEqual(duration, 0)
        line_text = " | ".join(str(line[0]) for line in (lines or []))
        self.assertIn("Codex 최근 사용량 (이미 조회 중)", line_text)
        self.assertIn("5시간 사용 한도: 24%", line_text)
        self.assertIn("완료되면 결과를 자동으로 표시합니다.", line_text)

    def test_monitor_tick_shows_pending_manual_snapshot_after_busy(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__set_manual_query_pending_result()

        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30T12:55:00",
        )

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(snapshot, None),
            ):
                with patch.object(
                    self.monitor,
                    "handle_snapshot",
                    return_value=[],
                ):
                    with patch.object(
                        self.monitor,
                        "get_last_snapshot",
                        return_value=snapshot,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_snapshot_tooltip",
                        ) as show_snapshot:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__ui_post",
                                side_effect=lambda fn: fn(),
                            ):
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__schedule_monitor_tick",
                                ):
                                    self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertTrue(show_snapshot.called)

    def test_monitor_tick_shows_pending_manual_error_after_busy(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__set_manual_query_pending_result()

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        captured: list[str] = []

        def fake_tooltip(text, lines=None, duration_ms=None):
            _ = lines
            _ = duration_ms
            captured.append(str(text or ""))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "collect_failed"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_tooltip",
                    side_effect=fake_tooltip,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                        ):
                            self.monitor._CodexUsageMonitor__monitor_tick()

        joined = " | ".join(captured)
        self.assertIn("진행 중이던 조회가 실패했습니다.", joined)
        self.assertIn("조회 작업 중 오류가 발생했습니다.", joined)

    def test_show_current_status_force_refresh_parse_failed_does_not_show_old_snapshot(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30 12:58:00",
        )

        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "parse_failed"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                        side_effect=fake_show,
                    ):
                        self.monitor.show_current_status(force_refresh=True)

        self.assertTrue(shown)
        self.assertIn("사용량 조회 실패:", shown[-1][0])
        self.assertIn("페이지에서 사용량을 읽지 못했습니다.", shown[-1][0])
        self.assertIsNone(shown[-1][1])

    def test_show_current_status_profile_in_use_uses_info_message_without_failure_prefix(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "profile_in_use"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                        side_effect=fake_show,
                    ):
                        self.monitor.show_current_status(force_refresh=True)

        self.assertTrue(shown)
        self.assertNotIn("사용량 조회 실패:", shown[-1][0])
        self.assertIn("다른 Chrome 세션에서 프로필을 사용 중", shown[-1][0])

    def test_show_current_status_profile_in_use_shows_latest_snapshot_when_available(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30 12:58:00",
        )

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "profile_in_use"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_snapshot_tooltip",
                    ) as show_snapshot:
                        self.monitor.show_current_status(force_refresh=True)

        self.assertTrue(show_snapshot.called)
        self.assertEqual(
            show_snapshot.call_args.kwargs.get("title"),
            "Codex 최근 사용량 (자동 조회 일시중지)",
        )

    def test_monitor_tick_retries_once_for_pending_manual_parse_failed(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__set_manual_query_pending_result()

        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30 13:05:00",
        )

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                side_effect=[(None, "parse_failed"), (snapshot, None)],
            ):
                with patch.object(
                    self.monitor,
                    "handle_snapshot",
                    return_value=[],
                ):
                    with patch.object(
                        self.monitor,
                        "get_last_snapshot",
                        return_value=snapshot,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_snapshot_tooltip",
                        ) as show_snapshot:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__show_tooltip",
                            ) as show_text_tip:
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__ui_post",
                                    side_effect=lambda fn: fn(),
                                ):
                                    with patch.object(
                                        self.monitor,
                                        "_CodexUsageMonitor__schedule_monitor_tick",
                                    ):
                                        self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertTrue(show_snapshot.called)
        self.assertFalse(show_text_tip.called)

    def test_monitor_tick_profile_in_use_finishes_pending_manual_result(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__set_manual_query_pending_result()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30 13:05:00",
        )
        self.monitor._CodexUsageMonitor__last_snapshot = snapshot

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "profile_in_use"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_snapshot_tooltip",
                ) as show_snapshot:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                    ) as show_text_tip:
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__ui_post",
                            side_effect=lambda fn: fn(),
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__schedule_monitor_tick",
                            ):
                                self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertTrue(show_snapshot.called)
        self.assertEqual(
            show_snapshot.call_args.kwargs.get("title"),
            "Codex 최근 사용량 (자동 조회 일시중지)",
        )
        self.assertFalse(show_text_tip.called)
        self.assertFalse(self.monitor._CodexUsageMonitor__has_manual_query_pending_result())

    def test_get_runtime_status_hides_countdown_while_collecting(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__collect_inflight = True
        self.monitor._CodexUsageMonitor__collect_inflight_source = "manual_query"
        self.monitor._CodexUsageMonitor__monitor_running = True
        self.monitor._CodexUsageMonitor__startup_warmup_running = False
        self.monitor._CodexUsageMonitor__failure_count = 2
        self.monitor._CodexUsageMonitor__next_collect_due_ts = 101.25

        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=100.0,
        ):
            status = self.monitor.get_runtime_status()

        self.assertTrue(status.get("collect_inflight"))
        self.assertEqual(status.get("collect_source"), "manual_query")
        self.assertIsNone(status.get("next_collect_in_sec"))
        self.assertFalse(bool(status.get("next_collect_estimated")))

    def test_get_runtime_status_hides_countdown_without_due_while_running(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__monitor_running = True
        self.monitor._CodexUsageMonitor__startup_warmup_running = False
        self.monitor._CodexUsageMonitor__collect_inflight = True
        self.monitor._CodexUsageMonitor__collect_started_ts = 90.0
        self.monitor._CodexUsageMonitor__interval_sec = 30.0
        self.monitor._CodexUsageMonitor__next_collect_due_ts = 0.0

        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=100.0,
        ):
            status = self.monitor.get_runtime_status()

        self.assertFalse(bool(status.get("next_collect_estimated")))
        self.assertIsNone(status.get("next_collect_in_sec"))

    def test_get_runtime_status_reports_profile_in_use_pause(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__profile_in_use_detected = True
        self.monitor._CodexUsageMonitor__next_collect_due_ts = 101.25
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=100.0,
        ):
            status = self.monitor.get_runtime_status()

        self.assertEqual(status.get("monitor_state"), "paused_profile_in_use")
        self.assertTrue(bool(status.get("profile_in_use")))
        self.assertIsNone(status.get("next_collect_in_sec"))

    def test_update_settings_allows_ten_second_interval(self) -> None:
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        with patch.object(self.monitor, "_CodexUsageMonitor__save_settings"):
            with patch.object(self.monitor, "_CodexUsageMonitor__restart_monitor"):
                ok, err = self.monitor.update_settings(
                    {
                        "enabled": True,
                        "interval_sec": 10,
                        "tooltip_duration_ms": 7000,
                        "usage_url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                    }
                )

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(self.monitor._CodexUsageMonitor__interval_sec, 10.0)

    def test_load_settings_clamps_interval_to_ten_seconds_minimum(self) -> None:
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__read_json_file",
            return_value={"enabled": True, "interval_sec": 3},
        ):
            with patch.object(self.monitor, "_CodexUsageMonitor__save_settings"):
                self.monitor._CodexUsageMonitor__load_settings()

        self.assertEqual(self.monitor._CodexUsageMonitor__interval_sec, 10.0)

    def test_handle_collect_error_cloudflare_background_guides_manual_query(self) -> None:
        captured: list[str] = []

        def fake_tooltip(text, lines=None, duration_ms=None):
            _ = lines
            _ = duration_ms
            captured.append(str(text or ""))

        self.monitor._CodexUsageMonitor__last_login_notice_ts = 0.0
        self.monitor._CodexUsageMonitor__login_notice_cooldown_sec = 0.0
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=123.0,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_tooltip",
                    side_effect=fake_tooltip,
                ):
                    self.monitor._CodexUsageMonitor__handle_collect_error(
                        "cloudflare_challenge",
                        source="monitor_tick",
                    )

        self.assertTrue(captured)
        self.assertIn("로그인 버튼", captured[-1])
        self.assertNotIn("열린 브라우저 창", captured[-1])

    def test_handle_collect_error_cloudflare_manual_avoids_open_window_assumption(self) -> None:
        captured: list[str] = []

        def fake_tooltip(text, lines=None, duration_ms=None):
            _ = lines
            _ = duration_ms
            captured.append(str(text or ""))

        self.monitor._CodexUsageMonitor__last_login_notice_ts = 0.0
        self.monitor._CodexUsageMonitor__login_notice_cooldown_sec = 0.0
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=123.0,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_tooltip",
                    side_effect=fake_tooltip,
                ):
                    self.monitor._CodexUsageMonitor__handle_collect_error(
                        "cloudflare_challenge",
                        source="manual_query",
                    )

        self.assertTrue(captured)
        self.assertIn("로그인 버튼", captured[-1])
        self.assertNotIn("열린 브라우저 창", captured[-1])

    def test_now_iso_is_korea_time(self) -> None:
        class _FakeDatetime:
            @staticmethod
            def now(_tz=None):
                return datetime(2026, 3, 30, 0, 0, 0, tzinfo=timezone.utc)

        self.monitor._CodexUsageMonitor__lib.datetime = _FakeDatetime
        got = self.monitor._CodexUsageMonitor__now_iso()
        self.assertEqual(got, "2026-03-30 09:00:00")

    def test_format_captured_at_for_display_converts_utc_to_kst(self) -> None:
        got = self.monitor.format_captured_at_for_display("2026-03-30T00:00:00+00:00")
        self.assertEqual(got, "2026-03-30 09:00:00")

    def test_collect_snapshot_guarded_uses_non_blocking_busy_skip(self) -> None:
        class _BusyLock:
            def __init__(self):
                self.acquire_calls: list[tuple[tuple, dict]] = []

            def acquire(self, *args, **kwargs):
                self.acquire_calls.append((args, kwargs))
                return False

            def release(self):
                raise AssertionError("release should not be called when acquire fails")

        busy_lock = _BusyLock()
        self.monitor._CodexUsageMonitor__collect_lock = busy_lock

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot",
            side_effect=AssertionError("collect should be skipped when busy"),
        ):
            snapshot, error = self.monitor._CodexUsageMonitor__collect_snapshot_guarded(
                source="manual_query"
            )

        self.assertIsNone(snapshot)
        self.assertEqual(error, "collect_busy")
        self.assertTrue(busy_lock.acquire_calls)
        args, kwargs = busy_lock.acquire_calls[0]
        is_non_blocking = bool(kwargs.get("blocking") is False or (len(args) >= 1 and args[0] is False))
        self.assertTrue(is_non_blocking)

    def test_collect_snapshot_guarded_manual_query_resets_monitor_countdown_after_done(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.cancelled: list[object] = []

            def after_cancel(self, token):
                self.cancelled.append(token)
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__monitor_after_id = "tick-1"
        self.monitor._CodexUsageMonitor__next_collect_due_ts = 55.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__startup_warmup_running = False
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__ui_post",
            side_effect=lambda fn: fn(),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot",
                return_value=(None, "collect_failed"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__schedule_monitor_tick",
                ) as schedule_tick:
                    _snap, err = self.monitor._CodexUsageMonitor__collect_snapshot_guarded(
                        source="manual_query"
                    )

        self.assertEqual(err, "collect_failed")
        self.assertTrue(root.cancelled)
        self.assertEqual(root.cancelled[0], "tick-1")
        self.assertTrue(schedule_tick.called)
        self.assertEqual(
            schedule_tick.call_args.kwargs.get("initial_delay_sec"),
            float(self.monitor._CodexUsageMonitor__interval_sec),
        )

    def test_monitor_tick_busy_collect_is_ignored_without_error_handler(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "collect_busy"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__handle_collect_error",
                ) as handle_error:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                        ) as schedule_tick:
                            self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertFalse(handle_error.called)
        self.assertTrue(schedule_tick.called)
        self.assertEqual(
            schedule_tick.call_args.kwargs.get("initial_delay_sec"),
            5.0,
        )

    def test_startup_warmup_busy_collect_is_ignored_without_error_handler(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__interval_sec = 90.0

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "collect_busy"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__handle_collect_error",
                ) as handle_error:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                        ) as schedule_tick:
                            self.monitor._CodexUsageMonitor__start_startup_warmup()

        self.assertFalse(handle_error.called)
        self.assertTrue(schedule_tick.called)
        self.assertEqual(
            schedule_tick.call_args.kwargs.get("initial_delay_sec"),
            5.0,
        )

    def test_on_worker_done_ignores_stale_worker_epoch(self) -> None:
        self.monitor._CodexUsageMonitor__worker_epoch = 3
        self.monitor._CodexUsageMonitor__monitor_running = True
        self.monitor._CodexUsageMonitor__startup_warmup_running = True

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__schedule_monitor_tick",
        ) as schedule_tick:
            self.monitor._CodexUsageMonitor__on_worker_done(
                5.0,
                worker_epoch=2,
                from_startup=True,
            )

        self.assertTrue(self.monitor._CodexUsageMonitor__monitor_running)
        self.assertTrue(self.monitor._CodexUsageMonitor__startup_warmup_running)
        self.assertFalse(schedule_tick.called)

    def test_monitor_worker_stale_epoch_skips_snapshot_apply(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__worker_epoch = 1

        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T13:10:00",
        )

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_collect_guarded(source, on_acquired=None):
            _ = source
            _ = on_acquired
            self.monitor._CodexUsageMonitor__worker_epoch = 2
            return snapshot, None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                side_effect=fake_collect_guarded,
            ):
                with patch.object(
                    self.monitor,
                    "handle_snapshot",
                ) as handle_snapshot:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                        ) as schedule_tick:
                            self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertFalse(handle_snapshot.called)
        self.assertFalse(schedule_tick.called)

    def test_restart_monitor_uses_startup_warmup(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__start_startup_warmup",
        ) as warmup:
            self.monitor._CodexUsageMonitor__restart_monitor()

        self.assertTrue(warmup.called)


    def test_startup_warmup_runs_headless_first_collect_path(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "parse_failed"),
            ) as collect:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__handle_collect_error",
                ) as handle_error:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                            return_value=None,
                        ):
                            self.monitor._CodexUsageMonitor__start_startup_warmup()

        self.assertTrue(collect.called)
        self.assertEqual(collect.call_args.kwargs.get("source"), "startup_warmup")
        self.assertTrue(handle_error.called)

    def test_update_settings_ignores_legacy_collection_mode(self) -> None:
        with patch.object(self.monitor, "_CodexUsageMonitor__save_settings"):
            with patch.object(self.monitor, "_CodexUsageMonitor__restart_monitor"):
                ok, err = self.monitor.update_settings(
                    {
                        "enabled": True,
                        "interval_sec": 30,
                        "tooltip_duration_ms": 7000,
                        "usage_url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                        "collection_mode": "api",
                    }
                )

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(
            self.monitor.get_settings_snapshot()["collection_mode"],
            "playwright",
        )

    def test_get_runtime_status_reports_fixed_playwright_mode(self) -> None:
        status = self.monitor.get_runtime_status()
        self.assertEqual(status.get("collection_mode"), "playwright")

    def test_load_settings_ignores_legacy_collection_mode(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__read_json_file",
            return_value={
                "enabled": True,
                "interval_sec": 15,
                "tooltip_duration_ms": 7000,
                "usage_url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                "collection_mode": "api",
            },
        ):
            with patch.object(self.monitor, "_CodexUsageMonitor__save_settings"):
                self.monitor._CodexUsageMonitor__load_settings()

        self.assertEqual(
            self.monitor.get_settings_snapshot()["collection_mode"],
            "playwright",
        )


    def test_show_change_tooltip_colors_changed_metrics_in_usage_section(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "259",
            },
            captured_at="2026-03-30T10:20:00",
        )
        changes = [
            UsageChange("five_hour_limit", "5시간 사용 한도", "20 / 40", "19 / 40"),
            UsageChange("remaining_credit", "남은 크레딧", "260", "259"),
        ]
        captured = {"lines": None}

        def fake_show_tooltip(_text, lines=None, duration_ms=None):
            _ = duration_ms
            captured["lines"] = lines

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__show_tooltip",
            side_effect=fake_show_tooltip,
        ):
            self.monitor._CodexUsageMonitor__show_change_tooltip(changes, snapshot)

        lines = captured.get("lines") or []
        line_map = {str(text): color for text, color in lines}
        self.assertEqual(line_map.get("5시간 사용 한도: 52.5%"), "#16A34A")
        self.assertEqual(line_map.get("남은 크레딧: 259"), "#DC2626")

    def test_monitor_tick_defers_change_tooltip_until_input_changes(self) -> None:
        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        class _FakeRoot:
            def __init__(self):
                self.after_calls = []

            def after(self, delay, fn):
                self.after_calls.append((delay, fn))
                return f"after-{len(self.after_calls)}"

            def after_cancel(self, _after_id):
                return None

        root = _FakeRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__session_state = "logged_in"
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        changed = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:20:00",
        )
        shown = []

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(changed, None),
            ):
                with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__get_last_input_tick",
                        return_value=100,
                        create=True,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_tooltip",
                            side_effect=lambda text, lines=None, duration_ms=None: shown.append(
                                (text, lines, duration_ms)
                            ),
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__ui_post",
                                side_effect=lambda fn: fn(),
                            ):
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__schedule_monitor_tick",
                                ):
                                    self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertEqual(shown, [])
        self.assertTrue(root.after_calls)

    def test_pending_change_tooltip_flushes_after_input_tick_changes(self) -> None:
        class _FakeRoot:
            def __init__(self):
                self.after_calls = []

            def after(self, delay, fn):
                self.after_calls.append((delay, fn))
                return f"after-{len(self.after_calls)}"

            def after_cancel(self, _after_id):
                return None

        root = _FakeRoot()
        self.monitor._CodexUsageMonitor__root = root
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "259",
            },
            captured_at="2026-03-30T10:20:00",
        )
        changes = [
            UsageChange("five_hour_limit", "5시간 사용 한도", "20 / 40", "19 / 40"),
            UsageChange("remaining_credit", "남은 크레딧", "260", "259"),
        ]
        shown = []

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__get_last_input_tick",
            side_effect=[100, 100, 101],
            create=True,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
                side_effect=lambda text, lines=None, duration_ms=None: shown.append(
                    (text, lines, duration_ms)
                ),
            ):
                self.monitor._CodexUsageMonitor__queue_change_tooltip_until_input(
                    changes,
                    snapshot,
                )
                self.assertEqual(shown, [])
                self.assertEqual(len(root.after_calls), 1)

                root.after_calls[-1][1]()
                self.assertEqual(shown, [])
                self.assertEqual(len(root.after_calls), 2)

                root.after_calls[-1][1]()

        self.assertEqual(len(shown), 1)
        lines = shown[0][1] or []
        joined = " | ".join(str(line[0]) for line in lines)
        self.assertIn("Codex 현재 사용량", joined)
        self.assertIn("5시간 사용 한도: 20 / 40 -> 19 / 40", joined)
        self.assertIn("남은 크레딧: 260 -> 259", joined)

    def test_pending_change_tooltip_merges_changes_until_user_input(self) -> None:
        class _FakeRoot:
            def __init__(self):
                self.after_calls = []

            def after(self, delay, fn):
                self.after_calls.append((delay, fn))
                return f"after-{len(self.after_calls)}"

            def after_cancel(self, _after_id):
                return None

        root = _FakeRoot()
        self.monitor._CodexUsageMonitor__root = root
        first = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "259",
            },
            captured_at="2026-03-30T10:20:00",
        )
        second = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "18 / 40",
                "weekly_limit": "119 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "259",
            },
            captured_at="2026-03-30T10:30:00",
        )
        first_changes = [
            UsageChange("five_hour_limit", "5시간 사용 한도", "20 / 40", "19 / 40"),
            UsageChange("remaining_credit", "남은 크레딧", "260", "259"),
        ]
        second_changes = [
            UsageChange("five_hour_limit", "5시간 사용 한도", "19 / 40", "18 / 40"),
            UsageChange("weekly_limit", "주간 사용 한도", "120 / 300", "119 / 300"),
        ]
        shown = []

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__get_last_input_tick",
            side_effect=[100, 100, 101],
            create=True,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
                side_effect=lambda text, lines=None, duration_ms=None: shown.append(
                    (text, lines, duration_ms)
                ),
            ):
                self.monitor._CodexUsageMonitor__queue_change_tooltip_until_input(
                    first_changes,
                    first,
                )
                self.monitor._CodexUsageMonitor__queue_change_tooltip_until_input(
                    second_changes,
                    second,
                )
                self.assertEqual(len(root.after_calls), 1)
                root.after_calls[-1][1]()

        self.assertEqual(len(shown), 1)
        lines = shown[0][1] or []
        joined = " | ".join(str(line[0]) for line in lines)
        self.assertIn("5시간 사용 한도: 55%", joined)
        self.assertIn("주간 사용 한도: 60.3333%", joined)
        self.assertIn("남은 크레딧: 259", joined)
        self.assertIn("5시간 사용 한도: 20 / 40 -> 18 / 40", joined)
        self.assertIn("주간 사용 한도: 120 / 300 -> 119 / 300", joined)
        self.assertIn("남은 크레딧: 260 -> 259", joined)


    def test_clear_profile_directory_deletes_only_managed_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            appdata = root / "Roaming"
            localappdata = root / "Local"
            appdata.mkdir()
            localappdata.mkdir()
            with patch.dict(
                "os.environ",
                {"APPDATA": str(appdata), "LOCALAPPDATA": str(localappdata)},
            ):
                monitor = CodexUsageMonitor()

            profile = localappdata / "windows-supporter" / "chatgpt-profile"
            profile.mkdir(parents=True)
            (profile / "session.txt").write_text("login", encoding="utf-8")

            ok, msg = monitor._CodexUsageMonitor__clear_profile_directory()

            self.assertTrue(ok)
            self.assertIn("로그아웃", msg)
            self.assertFalse(profile.exists())

    def test_clear_profile_directory_accepts_account_specific_managed_profile_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            appdata = root / "Roaming"
            localappdata = root / "Local"
            appdata.mkdir()
            localappdata.mkdir()
            account_1_profile = localappdata / "windows-supporter" / "chatgpt-profile-account-1"
            account_2_profile = localappdata / "windows-supporter" / "chatgpt-profile-account-2"
            account_1_profile.mkdir(parents=True)
            account_2_profile.mkdir(parents=True)
            (account_1_profile / "session.txt").write_text("login-1", encoding="utf-8")
            (account_2_profile / "session.txt").write_text("login-2", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {"APPDATA": str(appdata), "LOCALAPPDATA": str(localappdata)},
            ):
                monitor = CodexUsageMonitor(profile_dir=str(account_1_profile))

            ok, msg = monitor._CodexUsageMonitor__clear_profile_directory()

            self.assertTrue(ok)
            self.assertIn("로그아웃", msg)
            self.assertFalse(account_1_profile.exists())
            self.assertTrue((account_2_profile / "session.txt").exists())

    def test_clear_profile_directory_rejects_custom_profile_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            appdata = root / "Roaming"
            localappdata = root / "Local"
            appdata.mkdir()
            localappdata.mkdir()
            custom_profile = root / "important-profile"
            custom_profile.mkdir()
            (custom_profile / "keep.txt").write_text("do not delete", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {"APPDATA": str(appdata), "LOCALAPPDATA": str(localappdata)},
            ):
                monitor = CodexUsageMonitor(profile_dir=str(custom_profile))

            ok, msg = monitor._CodexUsageMonitor__clear_profile_directory()

            self.assertFalse(ok)
            self.assertIn("관리하는", msg)
            self.assertTrue((custom_profile / "keep.txt").exists())

    def test_notification_sink_suppresses_direct_tooltip_for_managed_normal_events(self) -> None:
        events = []
        monitor = CodexUsageMonitor(
            config_dir=str(self._config_dir),
            profile_dir=str(self._profile_dir),
            notification_sink=events.append,
            suppress_normal_tooltips=True,
        )

        monitor._CodexUsageMonitor__show_tooltip("Codex 사용량 조회 중...", duration_ms=0)

        self.assertEqual(
            events,
            [
                {
                    "text": "Codex 사용량 조회 중...",
                    "lines": None,
                    "duration_ms": 0,
                }
            ],
        )
        self.assertIsNone(monitor._CodexUsageMonitor__active_tooltip)

    def test_release_profile_session_rejects_while_collect_busy(self) -> None:
        class _BusyLock:
            def acquire(self, *args, **kwargs):
                _ = (args, kwargs)
                return False

            def release(self):
                raise AssertionError("release should not be called")

        self.monitor._CodexUsageMonitor__collect_lock = _BusyLock()
        self.monitor._CodexUsageMonitor__release_wait_timeout_sec = 0.2
        self.monitor._CodexUsageMonitor__release_poll_interval_sec = 0.01
        ok, msg = self.monitor.release_profile_session()
        self.assertFalse(ok)
        self.assertIn("중단하지 못했습니다", msg)

    def test_ui_post_does_not_schedule_tk_directly_when_queue_fails(self) -> None:
        class _FailingQueue:
            def put(self, _fn):
                raise RuntimeError("queue unavailable")

        class _Root:
            def after(self, *_args, **_kwargs):
                raise AssertionError("worker must not call root.after directly")

        self.monitor._CodexUsageMonitor__event_queue = _FailingQueue()
        self.monitor._CodexUsageMonitor__root = _Root()

        self.monitor._CodexUsageMonitor__ui_post(lambda: None)

    def test_restart_monitor_skips_warmup_when_logged_out(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_out")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__start_startup_warmup",
        ) as warmup:
            self.monitor._CodexUsageMonitor__restart_monitor()

        self.assertFalse(warmup.called)
        status = self.monitor.get_runtime_status()
        self.assertEqual(status.get("session_state"), "logged_out")
        self.assertFalse(bool(status.get("auto_monitoring_active")))
        self.assertIsNone(status.get("next_collect_in_sec"))

    def test_handle_collect_error_login_required_pauses_background_monitor(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.cancelled = []

            def after_cancel(self, token):
                self.cancelled.append(token)
                return None

            def after(self, _delay, fn):
                fn()
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__monitor_after_id = "tick-1"
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__show_tooltip",
        ):
            self.monitor._CodexUsageMonitor__handle_collect_error(
                "login_required",
                source="monitor_tick",
            )

        status = self.monitor.get_runtime_status()
        self.assertEqual(status.get("session_state"), "logged_out")
        self.assertEqual(self.monitor._CodexUsageMonitor__monitor_after_id, None)
        self.assertTrue(root.cancelled)


    def test_show_current_status_preserves_auto_monitor_source_for_background_refresh(self) -> None:
        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "collect_busy"),
            ) as collect_guarded:
                with patch.object(self.monitor, "_CodexUsageMonitor__ui_post_coalesced"):
                    with patch.object(self.monitor, "_CodexUsageMonitor__ui_post"):
                        self.monitor.show_current_status(
                            force_refresh=True,
                            source="auto_monitor",
                        )

        collect_guarded.assert_called_once()
        self.assertEqual(collect_guarded.call_args.kwargs.get("source"), "auto_monitor")

    def test_show_current_status_manual_login_cancels_scheduled_pending_poll(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.cancelled: list[str] = []

            def after_cancel(self, token):
                self.cancelled.append(str(token))
                return None

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__pending_login_after_id = "poll-1"
        self.monitor._CodexUsageMonitor__pending_login_poll_until_ts = 1000.0
        self.monitor._CodexUsageMonitor__pending_login_poll_reason = "login_required"

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "collect_busy"),
            ) as collect_guarded:
                with patch.object(self.monitor, "_CodexUsageMonitor__ui_post_coalesced"):
                    with patch.object(self.monitor, "_CodexUsageMonitor__ui_post"):
                        self.monitor.show_current_status(
                            force_refresh=True,
                            source="manual_login",
                        )

        self.assertEqual(root.cancelled, ["poll-1"])
        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, None)
        collect_guarded.assert_called_once()
        self.assertEqual(collect_guarded.call_args.kwargs.get("source"), "manual_login")

    def test_show_current_status_manual_login_replaces_pre_login_snapshot_without_backfill(self) -> None:
        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        previous = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T12:00:00",
        )
        fresh_after_login = UsageSnapshot.from_metrics(
            {"five_hour_limit": "16 / 40"},
            captured_at="2026-03-30T12:10:00",
        )
        self.monitor.handle_snapshot(previous)
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__set_session_state("logged_out")

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(fresh_after_login, None),
            ) as collect_guarded:
                with patch.object(self.monitor, "_CodexUsageMonitor__ui_post_coalesced"):
                    with patch.object(self.monitor, "_CodexUsageMonitor__ui_post"):
                        self.monitor.show_current_status(
                            force_refresh=True,
                            source="manual_login",
                        )

        latest = self.monitor.get_last_snapshot()
        state_path = Path(self.monitor._CodexUsageMonitor__state_path)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        persisted = payload.get("last_snapshot") or {}
        history = self.monitor.get_runtime_status().get("usage_history") or []
        saved_history = payload.get("usage_history") or []

        collect_guarded.assert_called_once()
        self.assertEqual(collect_guarded.call_args.kwargs.get("source"), "manual_login")
        self.assertEqual(latest.five_hour_limit, "60%")
        self.assertEqual(latest.weekly_limit, "")
        self.assertEqual(latest.gpt_5_3_codex_spark_five_hour_limit, "")
        self.assertEqual(latest.gpt_5_3_codex_spark_weekly_limit, "")
        self.assertEqual(latest.remaining_credit, "")
        self.assertEqual(persisted.get("weekly_limit"), "")
        self.assertEqual(payload.get("session_state"), "logged_in")
        self.assertEqual(len(history), 1)
        self.assertEqual(len(saved_history), 1)
        self.assertEqual(history[0].get("captured_at"), "2026-03-30T12:10:00")
        self.assertEqual(saved_history[0].get("weekly_limit"), "")


    def test_get_runtime_status_exposes_login_logout_controls(self) -> None:
        self.monitor._CodexUsageMonitor__collect_inflight = False
        self.monitor._CodexUsageMonitor__logout_in_progress = False
        self.monitor._CodexUsageMonitor__set_session_state("logged_out")
        logged_out_status = self.monitor.get_runtime_status()

        self.assertTrue(bool(logged_out_status.get("can_login")))
        self.assertFalse(bool(logged_out_status.get("can_logout")))

        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        logged_in_status = self.monitor.get_runtime_status()
        self.assertFalse(bool(logged_in_status.get("can_login")))
        self.assertTrue(bool(logged_in_status.get("can_logout")))


    def test_handle_collect_error_profile_in_use_background_sets_pause_without_tooltip(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__ui_post",
            side_effect=lambda fn: fn(),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
            ) as show_tip:
                self.monitor._CodexUsageMonitor__handle_collect_error(
                    "profile_in_use",
                    source="startup_warmup",
                )

        self.assertFalse(show_tip.called)
        self.assertTrue(bool(self.monitor._CodexUsageMonitor__profile_in_use_detected))


if __name__ == "__main__":
    unittest.main()
