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

    def test_login_required_error_persists_logged_out_session_state(self) -> None:
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
        self.monitor.handle_snapshot(snapshot)

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=False,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
            ):
                self.monitor._CodexUsageMonitor__handle_collect_error(
                    "login_required",
                    source="startup_warmup",
                )

        state_path = Path(self.monitor._CodexUsageMonitor__state_path)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("session_state"), "logged_out")

        reloaded = CodexUsageMonitor(
            config_dir=str(self._config_dir),
            profile_dir=str(self._profile_dir),
        )
        self.assertEqual(reloaded.get_runtime_status().get("session_state"), "logged_out")
        self.assertFalse(bool(reloaded.get_runtime_status().get("auto_monitoring_active")))

    def test_cloudflare_error_preserves_logged_in_session_state(self) -> None:
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
        self.monitor.handle_snapshot(snapshot)

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=False,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
            ):
                self.monitor._CodexUsageMonitor__handle_collect_error(
                    "cloudflare_challenge",
                    source="startup_warmup",
                )

        state_path = Path(self.monitor._CodexUsageMonitor__state_path)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        status = self.monitor.get_runtime_status()
        self.assertEqual(payload.get("session_state"), "logged_in")
        self.assertEqual(status.get("session_state"), "logged_in")
        self.assertTrue(bool(status.get("auth_attention_required")))
        self.assertEqual(status.get("auth_attention_reason"), "cloudflare_challenge")
        self.assertEqual(status.get("monitor_state"), "paused_auth_required")
        self.assertFalse(bool(status.get("auto_monitoring_active")))
        self.assertTrue(bool(status.get("can_login")))

    def test_background_auth_error_after_snapshot_keeps_session_retryable(self) -> None:
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
                self.assertFalse(bool(status.get("auth_attention_required")))
                self.assertNotEqual(status.get("monitor_state"), "paused_auth_required")
                self.assertEqual(
                    self.monitor._CodexUsageMonitor__monitor_after_id,
                    "tick-1",
                )
                self.assertEqual(root.cancelled, [])

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

    def test_collect_with_playwright_obj_does_not_open_visible_on_parse_failed(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "parse_failed"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=None,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                    return_value=None,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip") as show_tip:
                            snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                                object(),
                                source="manual_query",
                            )

        self.assertEqual(err, "parse_failed")
        self.assertIsNone(snap)
        self.assertEqual(collect_once.call_count, 1)
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertTrue(first_call.kwargs.get("prefer_system_channel"))
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))
        self.assertFalse(show_tip.called)

    def test_collect_with_playwright_obj_prefers_profile_raw_cdp_before_launch(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "profile_in_use"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=recovered,
            ) as raw_collect:
                snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                    object(),
                    source="manual_query",
                )

        self.assertIsNone(err)
        self.assertIs(snap, recovered)
        self.assertEqual(collect_once.call_count, 0)
        self.assertEqual(raw_collect.call_count, 1)

    def test_collect_snapshot_uses_raw_preflight_before_playwright_startup(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__try_no_focus_raw_preflight",
            return_value=(recovered, None, True),
        ) as raw_preflight:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ensure_playwright_available",
                side_effect=AssertionError("playwright should not be initialized"),
            ):
                snap, err = self.monitor._CodexUsageMonitor__collect_snapshot(
                    source="startup_warmup",
                )

        self.assertIsNone(err)
        self.assertIs(snap, recovered)
        raw_preflight.assert_called_once()

    def test_collect_snapshot_pending_login_poll_uses_short_raw_preflight(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__try_no_focus_raw_preflight",
            return_value=(recovered, None, True),
        ) as raw_preflight:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ensure_playwright_available",
                side_effect=AssertionError("pending login poll should not initialize Playwright"),
            ):
                snap, err = self.monitor._CodexUsageMonitor__collect_snapshot(
                    source="pending_login_poll",
                )

        self.assertIsNone(err)
        self.assertIs(snap, recovered)
        raw_preflight.assert_called_once_with(wait_timeout_sec=4.0, source="pending_login_poll")

    def test_collect_with_playwright_obj_prefers_profile_raw_cdp_before_cloudflare_probe(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "cloudflare_challenge"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=recovered,
            ) as raw_collect:
                snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                    object(),
                    source="manual_query",
                )

        self.assertIsNone(err)
        self.assertIs(snap, recovered)
        self.assertEqual(collect_once.call_count, 0)
        self.assertEqual(raw_collect.call_count, 1)

    def test_collect_with_playwright_obj_prefers_system_chrome_raw_cdp_when_available(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "18 / 40",
                "weekly_limit": "110 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "9 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "9 / 50",
                "remaining_credit": "250",
            },
            captured_at="2026-03-30T11:15:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            side_effect=AssertionError("browser launch should not run"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=None,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                    return_value=recovered,
                ) as raw_system:
                    snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                        object(),
                        source="manual_query",
                    )

        self.assertIsNone(err)
        self.assertIs(snap, recovered)
        self.assertEqual(collect_once.call_count, 0)
        self.assertEqual(raw_system.call_count, 1)

    def test_collect_with_playwright_obj_skips_headless_when_profile_cdp_is_active(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            side_effect=AssertionError("headless browser launch should not run"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                    return_value=None,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                        return_value=None,
                    ):
                        snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                            object(),
                            source="startup_warmup",
                        )

        self.assertIsNone(snap)
        self.assertEqual(err, "login_required")
        self.assertEqual(collect_once.call_count, 0)

    def test_collect_with_playwright_obj_background_raw_miss_with_profile_cdp_is_parse_failed(
        self,
    ) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            side_effect=AssertionError("headless browser launch should not run"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp_result",
                    return_value=(None, "parse_failed"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp_result",
                        return_value=(None, "parse_failed"),
                    ):
                        snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                            object(),
                            source="monitor_tick",
                        )

        self.assertIsNone(snap)
        self.assertEqual(err, "parse_failed")
        self.assertEqual(collect_once.call_count, 0)

    def test_background_raw_miss_parse_failed_does_not_mark_logged_out_or_show_login_tooltip(
        self,
    ) -> None:
        class _InlineQueue:
            def put(self, fn):
                fn()

        self.monitor._CodexUsageMonitor__event_queue = _InlineQueue()
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            side_effect=AssertionError("headless browser launch should not run"),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp_result",
                    return_value=(None, "parse_failed"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp_result",
                        return_value=(None, "parse_failed"),
                    ):
                        _snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                            object(),
                            source="monitor_tick",
                        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__show_tooltip",
        ) as show_tip:
            self.monitor._CodexUsageMonitor__handle_collect_error(
                str(err or ""),
                source="monitor_tick",
            )

        self.assertEqual(err, "parse_failed")
        self.assertEqual(self.monitor.get_runtime_status().get("session_state"), "logged_in")
        shown_texts = [str(call.args[0]) for call in show_tip.call_args_list if call.args]
        self.assertFalse(any("Codex 로그인이 필요합니다" in text for text in shown_texts))

    def test_collect_with_playwright_obj_background_raw_cancel_propagates_cancelled(
        self,
    ) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            side_effect=AssertionError("headless browser launch should not run"),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp_result",
                    return_value=(None, "collect_cancelled"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp_result",
                        return_value=(None, "parse_failed"),
                    ):
                        snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                            object(),
                            source="monitor_tick",
                        )

        self.assertIsNone(snap)
        self.assertEqual(err, "collect_cancelled")

    def test_classify_raw_usage_probe_error_detects_login_url(self) -> None:
        err = self.monitor._CodexUsageMonitor__classify_raw_usage_probe_error(
            {"url": "https://auth.openai.com/log-in", "mainText": ""}
        )

        self.assertEqual(err, "login_required")

    def test_classify_raw_usage_probe_error_detects_explicit_login_text(self) -> None:
        err = self.monitor._CodexUsageMonitor__classify_raw_usage_probe_error(
            {
                "url": "https://chatgpt.com/codex/settings/usage",
                "mainText": "Codex 사용량을 보려면 로그인이 필요합니다.",
            }
        )

        self.assertEqual(err, "login_required")

    def test_classify_raw_usage_probe_error_rejects_broad_account_markers(self) -> None:
        for text in ("Sign up", "Continue with Google"):
            with self.subTest(text=text):
                err = self.monitor._CodexUsageMonitor__classify_raw_usage_probe_error(
                    {
                        "url": "https://chatgpt.com/codex/settings/usage",
                        "mainText": text,
                    }
                )

                self.assertEqual(err, "parse_failed")

    def test_classify_raw_usage_probe_error_keeps_usage_page_without_metrics_parse_failed(
        self,
    ) -> None:
        err = self.monitor._CodexUsageMonitor__classify_raw_usage_probe_error(
            {
                "url": "https://chatgpt.com/codex/settings/usage",
                "mainText": "5-hour usage limit",
                "metricBlocks": [],
            }
        )

        self.assertEqual(err, "parse_failed")

    def test_collect_with_playwright_obj_uses_hidden_no_focus_probe_for_background_login_required(
        self,
    ) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "login_required"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=None,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                    return_value=None,
                ):
                    self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                        object(),
                        source="startup_warmup",
                    )

        self.assertEqual(collect_once.call_count, 1)
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))

    def test_collect_with_playwright_obj_uses_hidden_cdp_before_headless_fallback(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(recovered, None),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=None,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                    return_value=None,
                ):
                    snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                        object(),
                        source="startup_warmup",
                    )

        self.assertIsNone(err)
        self.assertIs(snap, recovered)
        collect_once.assert_called_once()
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))

    def test_raw_cdp_probe_target_executes_probe_function_expression(self) -> None:
        payload = {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            "mainText": "Usage",
            "metricBlocks": [],
        }
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__raw_cdp_send",
            return_value={"result": {"result": {"value": payload}}},
        ) as raw_send:
            probe = self.monitor._CodexUsageMonitor__raw_cdp_probe_target(
                object(),
                "session-1",
            )

        self.assertEqual(probe["url"], payload["url"])
        params = raw_send.call_args.args[2]
        self.assertTrue(str(params.get("expression", "")).startswith("("))
        self.assertTrue(str(params.get("expression", "")).endswith(")()"))

    def test_collect_with_playwright_obj_manual_login_opens_interactive_without_hidden_probe(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(recovered, None),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ) as ui_post:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__prepare_interactive_recovery_launch",
                ) as prepare_interactive:
                    with patch.object(
                        self.monitor._CodexUsageMonitor__lib.time,
                        "monotonic",
                        return_value=2000.0,
                    ):
                        snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                            object(),
                            source="manual_login",
                        )

        self.assertIsNone(err)
        self.assertIs(snap, recovered)
        self.assertEqual(collect_once.call_count, 1)
        self.assertTrue(prepare_interactive.called)
        ui_post.assert_not_called()
        only_call = collect_once.call_args_list[0]
        self.assertTrue(only_call.kwargs.get("allow_interactive_recovery"))
        self.assertFalse(only_call.kwargs.get("force_hidden"))
        self.assertEqual(
            only_call.kwargs.get("initial_url"),
            "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
        )

    def test_collect_snapshot_once_manual_login_reuses_existing_profile_cdp(self) -> None:
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

        class _DummyProc:
            pid = 23004
            _ws_listener_pid = 23004
            _ws_cdp_port = 12236
            _ws_external_cdp = True
            _ws_monitor_managed = False

        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyBrowser:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                return None

        context = _DummyContext()
        browser = _DummyBrowser()
        proc = _DummyProc()

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__iter_external_profile_remote_debugging_endpoints",
            return_value=[(12236, 23004, False)],
        ) as iter_endpoints:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__connect_existing_profile_remote_debug_context",
                return_value=(context, browser, proc, True),
            ) as connect_existing:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__launch_interactive_context_via_cdp",
                    side_effect=AssertionError("new Chrome should not be launched"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__set_cdp_window_visibility",
                        return_value=True,
                    ) as set_visibility:
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__terminate_spawned_process",
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__is_cloudflare_challenge",
                                return_value=False,
                            ):
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__is_login_required",
                                    return_value=False,
                                ):
                                    with patch.object(
                                        self.monitor,
                                        "_CodexUsageMonitor__build_snapshot_from_page",
                                        return_value=snapshot,
                                    ):
                                        got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                            object(),
                                            headless=False,
                                            allow_interactive_recovery=True,
                                            force_hidden=False,
                                            prefer_system_channel=True,
                                            initial_url=(
                                                "https://chatgpt.com/auth/login?"
                                                "next=/codex/cloud/settings/analytics%23usage"
                                            ),
                                        )

        self.assertIsNone(err)
        self.assertIs(got, snapshot)
        iter_endpoints.assert_called_once_with(include_owned=True)
        connect_existing.assert_called_once()
        set_visibility.assert_called_once_with(
            proc,
            visible=True,
            bring_to_front=True,
        )
        self.assertFalse(context.closed)
        self.assertFalse(browser.closed)

    def test_collect_snapshot_once_manual_login_relaunches_when_existing_profile_cdp_is_managed(
        self,
    ) -> None:
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

        class _DummyProc:
            pid = 24000
            _ws_cdp_port = 24001

            def poll(self):
                return None

        class _DummyPage:
            url = "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyBrowser:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                return None

        context = _DummyContext()
        browser = _DummyBrowser()
        proc = _DummyProc()

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__iter_external_profile_remote_debugging_endpoints",
            return_value=[(12236, 23004, True)],
        ) as iter_endpoints:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_profile_remote_debugging_processes",
            ) as terminate_managed:
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.time,
                    "sleep",
                ) as sleep:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__connect_existing_profile_remote_debug_context",
                        side_effect=AssertionError("managed hidden CDP should not be reused"),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
                            return_value=(context, browser, proc),
                        ) as launch_interactive:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__set_cdp_window_visibility",
                                return_value=True,
                            ) as set_visibility:
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__terminate_spawned_process",
                                ):
                                    with patch.object(
                                        self.monitor,
                                        "_CodexUsageMonitor__is_cloudflare_challenge",
                                        return_value=False,
                                    ):
                                        with patch.object(
                                            self.monitor,
                                            "_CodexUsageMonitor__is_login_required",
                                            return_value=False,
                                        ):
                                            with patch.object(
                                                self.monitor,
                                                "_CodexUsageMonitor__build_snapshot_from_page",
                                                return_value=snapshot,
                                            ):
                                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                                    object(),
                                                    headless=False,
                                                    allow_interactive_recovery=True,
                                                    force_hidden=False,
                                                    prefer_system_channel=True,
                                                    initial_url=(
                                                        "https://chatgpt.com/auth/login?"
                                                        "next=/codex/cloud/settings/analytics%23usage"
                                                    ),
                                                )

        self.assertIsNone(err)
        self.assertIs(got, snapshot)
        iter_endpoints.assert_called_once_with(include_owned=True)
        terminate_managed.assert_called_once_with(managed_only=True)
        sleep.assert_called_once()
        launch_interactive.assert_called_once()
        self.assertEqual(
            set_visibility.call_args_list[0].kwargs,
            {"visible": True, "bring_to_front": True},
        )
        self.assertIs(set_visibility.call_args_list[0].args[0], proc)
        self.assertEqual(set_visibility.call_count, 1)
        self.assertIsNone(self.monitor._CodexUsageMonitor__hidden_cdp_proc)
        self.assertEqual(int(self.monitor._CodexUsageMonitor__hidden_cdp_port), 0)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    def test_manual_login_button_bypasses_interactive_reopen_cooldown(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "8 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )
        self.monitor._CodexUsageMonitor__last_interactive_login_ts = 1999.0
        self.monitor._CodexUsageMonitor__manual_interactive_reopen_cooldown_sec = 120.0

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(recovered, None),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.time,
                    "monotonic",
                    return_value=2000.0,
                ):
                    snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                        object(),
                        source="manual_login",
                    )

        self.assertIsNone(err)
        self.assertIs(snap, recovered)
        self.assertEqual(collect_once.call_count, 1)

    def test_collect_with_playwright_obj_manual_query_does_not_open_interactive_for_login_required(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "login_required"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=None,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                    return_value=None,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__prepare_interactive_recovery_launch",
                        ) as prepare_interactive:
                            with patch.object(
                                self.monitor._CodexUsageMonitor__lib.time,
                                "monotonic",
                                return_value=2000.0,
                            ):
                                snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                                    object(),
                                    source="manual_query",
                                )

        self.assertEqual(err, "login_required")
        self.assertIsNone(snap)
        self.assertEqual(collect_once.call_count, 1)
        prepare_interactive.assert_not_called()
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))

    def test_collect_with_playwright_obj_manual_query_does_not_open_interactive_for_cloudflare(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "cloudflare_challenge"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=None,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                    return_value=None,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__prepare_interactive_recovery_launch",
                        ) as prepare_interactive:
                            with patch.object(
                                self.monitor._CodexUsageMonitor__lib.time,
                                "monotonic",
                                return_value=2000.0,
                            ):
                                snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                                    object(),
                                    source="manual_query",
                                )

        self.assertEqual(err, "cloudflare_challenge")
        self.assertIsNone(snap)
        self.assertEqual(collect_once.call_count, 1)
        prepare_interactive.assert_not_called()
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))

    def test_collect_with_playwright_obj_manual_query_reports_hidden_retry_failure_without_visible_window(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "cloudflare_challenge"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=None,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                    return_value=None,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__prepare_interactive_recovery_launch",
                        ) as prepare_interactive:
                            with patch.object(
                                self.monitor._CodexUsageMonitor__lib.time,
                                "monotonic",
                                return_value=2000.0,
                            ):
                                snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                                    object(),
                                    source="manual_query",
                                )

        prepare_interactive.assert_not_called()
        self.assertEqual(err, "cloudflare_challenge")
        self.assertIsNone(snap)
        self.assertEqual(collect_once.call_count, 1)
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))

    def test_collect_with_playwright_obj_skips_interactive_for_background_source(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "cloudflare_challenge"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_collect_snapshot_via_raw_external_cdp",
                return_value=None,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp",
                    return_value=None,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip") as show_tip:
                            snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                                object(),
                                source="startup_warmup",
                            )

        self.assertEqual(err, "cloudflare_challenge")
        self.assertIsNone(snap)
        self.assertEqual(collect_once.call_count, 1)
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))
        self.assertFalse(show_tip.called)

    def test_should_open_interactive_recovery_only_allows_manual_login(self) -> None:
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=100.0,
        ):
            self.assertFalse(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="startup_warmup"
                )
            )
            self.assertFalse(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="manual_query"
                )
            )
            self.assertTrue(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="manual_login"
                )
            )

    def test_should_open_interactive_recovery_manual_login_bypasses_short_cooldown(self) -> None:
        self.monitor._CodexUsageMonitor__last_interactive_login_ts = 99.0
        self.monitor._CodexUsageMonitor__manual_interactive_reopen_cooldown_sec = 120.0
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=100.0,
        ):
            self.assertTrue(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="manual_login"
                )
            )

    def test_is_cloudflare_challenge_detects_html_marker_with_empty_body_text(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def evaluate(self, _expr):
                return ""

            def content(self):
                return (
                    "<html><head>"
                    "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
                    "</head><body></body></html>"
                )

        self.assertTrue(
            self.monitor._CodexUsageMonitor__is_cloudflare_challenge(_DummyPage())
        )

    def test_is_cloudflare_challenge_detects_cf_query_token(self) -> None:
        class _DummyPage:
            url = (
                "https://chatgpt.com/codex/cloud/settings/analytics?"
                "__cf_chl_rt_tk=token-123#usage"
            )

            def evaluate(self, _expr):
                return ""

            def content(self):
                return "<html><body></body></html>"

        self.assertTrue(
            self.monitor._CodexUsageMonitor__is_cloudflare_challenge(_DummyPage())
        )

    def test_is_cloudflare_challenge_ignores_cf_token_when_usage_content_visible(self) -> None:
        class _DummyPage:
            url = (
                "https://chatgpt.com/codex/cloud/settings/analytics?"
                "__cf_chl_rt_tk=token-123#usage"
            )

            def evaluate(self, _expr):
                return (
                    "5-hour usage limit\\n"
                    "12 / 40\\n"
                    "weekly usage limit\\n"
                    "111 / 300\\n"
                    "gpt-5.3-codex-spark 5-hour usage limit\\n"
                    "8 / 10\\n"
                    "gpt-5.3-codex-spark weekly usage limit\\n"
                    "80 / 100\\n"
                    "remaining credit\\n"
                    "320"
                )

            def content(self):
                return "<html><body><main>usage metrics</main></body></html>"

        self.assertFalse(
            self.monitor._CodexUsageMonitor__is_cloudflare_challenge(_DummyPage())
        )

    def test_is_cloudflare_challenge_ignores_html_marker_when_usage_content_visible(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def evaluate(self, _expr):
                return (
                    "5-hour usage limit\\n"
                    "12 / 40\\n"
                    "weekly usage limit\\n"
                    "111 / 300\\n"
                    "gpt-5.3-codex-spark 5-hour usage limit\\n"
                    "8 / 10\\n"
                    "gpt-5.3-codex-spark weekly usage limit\\n"
                    "80 / 100\\n"
                    "remaining credit\\n"
                    "320"
                )

            def content(self):
                return (
                    "<html><head>"
                    "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
                    "</head><body>usage metrics rendered</body></html>"
                )

        self.assertFalse(
            self.monitor._CodexUsageMonitor__is_cloudflare_challenge(_DummyPage())
        )

    def test_launch_browser_context_uses_chrome_only_for_interactive_recovery(self) -> None:
        calls: list[dict] = []

        class _FakeChromium:
            def launch_persistent_context(self, profile_dir, **kwargs):
                calls.append({"profile_dir": profile_dir, "kwargs": dict(kwargs)})
                return object()

        class _FakePlaywright:
            chromium = _FakeChromium()

        ctx = self.monitor._CodexUsageMonitor__launch_browser_context(
            _FakePlaywright(),
            headless=False,
            prefer_system_channel=True,
        )

        self.assertIsNotNone(ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["kwargs"].get("channel"), "chrome")
        self.assertTrue(calls[0]["kwargs"].get("chromium_sandbox"))
        self.assertNotIn("ignore_default_args", calls[0]["kwargs"])
        self.assertIn("--disable-extensions", calls[0]["kwargs"].get("args", []))
        self.assertIn("--disable-notifications", calls[0]["kwargs"].get("args", []))

    def test_wait_until_logged_in_performs_active_login_entry(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__is_cloudflare_challenge",
            return_value=False,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__is_login_required",
                side_effect=[True, True, False],
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_open_login_entry",
                    return_value=True,
                ) as open_login:
                    with patch.object(
                        self.monitor._CodexUsageMonitor__lib.time,
                        "monotonic",
                        side_effect=[0.0, 0.1, 0.2, 0.3, 0.4],
                    ):
                        ok = self.monitor._CodexUsageMonitor__wait_until_logged_in(page, timeout_sec=5.0)

        self.assertTrue(ok)
        self.assertTrue(open_login.called)

    def test_wait_until_logged_in_does_not_reclick_login_entry_immediately(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__is_cloudflare_challenge",
            return_value=False,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__is_login_required",
                side_effect=[True, True, True, False],
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_open_login_entry",
                    return_value=True,
                ) as open_login:
                    with patch.object(
                        self.monitor._CodexUsageMonitor__lib.time,
                        "monotonic",
                        side_effect=[0.0, 1.0, 2.0, 3.0],
                    ):
                        ok = self.monitor._CodexUsageMonitor__wait_until_logged_in(page, timeout_sec=60.0)

        self.assertTrue(ok)
        self.assertEqual(open_login.call_count, 1)
        self.assertTrue(open_login.call_args.kwargs.get("force"))

    def test_try_open_login_entry_does_not_reload_auth_login_page(self) -> None:
        class _DummyLocator:
            def count(self):
                return 0

        class _DummyPage:
            url = "https://auth.openai.com/log-in-or-create-account"

            def __init__(self):
                self.goto_calls = []

            def locator(self, _selector):
                return _DummyLocator()

            def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))
                return None

        page = _DummyPage()
        opened = self.monitor._CodexUsageMonitor__try_open_login_entry(page, force=True)

        self.assertFalse(opened)
        self.assertEqual(page.goto_calls, [])

    def test_try_open_login_entry_recovers_invalid_state_with_try_again(self) -> None:
        class _DummyLocator:
            def __init__(self, should_exist: bool, page):
                self._should_exist = bool(should_exist)
                self._page = page

            @property
            def first(self):
                return self

            def count(self):
                return 1 if self._should_exist else 0

            def click(self, timeout=None):
                _ = timeout
                self._page.clicked = True
                return None

        class _DummyPage:
            url = "https://auth.openai.com/log-in-or-create-account"

            def __init__(self):
                self.goto_calls = []
                self.clicked = False

            def evaluate(self, _script):
                return "An error occurred during authentication (invalid_state). Please try again."

            def locator(self, selector):
                if "Try again" in str(selector):
                    return _DummyLocator(True, self)
                return _DummyLocator(False, self)

            def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))
                return None

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        opened = self.monitor._CodexUsageMonitor__try_open_login_entry(page, force=True)

        self.assertTrue(opened)
        self.assertTrue(page.clicked)
        self.assertEqual(page.goto_calls, [])

    def test_try_open_login_entry_recovers_route_error_with_try_again(self) -> None:
        class _DummyLocator:
            def __init__(self, should_exist: bool, page):
                self._should_exist = bool(should_exist)
                self._page = page

            @property
            def first(self):
                return self

            def count(self):
                return 1 if self._should_exist else 0

            def click(self, timeout=None):
                _ = timeout
                self._page.clicked = True
                return None

        class _DummyPage:
            url = "https://auth.openai.com/log-in-or-create-account"

            def __init__(self):
                self.goto_calls = []
                self.clicked = False

            def evaluate(self, _script):
                return "Route Error (400 Invalid content type: text/html; charset=UTF-8)"

            def locator(self, selector):
                if "Try again" in str(selector):
                    return _DummyLocator(True, self)
                return _DummyLocator(False, self)

            def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))
                return None

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        opened = self.monitor._CodexUsageMonitor__try_open_login_entry(page, force=True)

        self.assertTrue(opened)
        self.assertTrue(page.clicked)
        self.assertEqual(page.goto_calls, [])

    def test_wait_for_snapshot_ready_retries_usage_from_chatgpt_home(self) -> None:
        class _DummyPage:
            def __init__(self):
                self.url = "https://chatgpt.com/"
                self.goto_calls = []

            def goto(self, url, **_kwargs):
                self.goto_calls.append(url)
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "18 / 40",
                "weekly_limit": "110 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "9 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "9 / 50",
                "remaining_credit": "250",
            },
            captured_at="2026-03-30T12:00:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__build_snapshot_from_page",
            side_effect=[None, snapshot],
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.time,
                "monotonic",
                side_effect=[0.0, 0.2, 0.4, 0.6],
            ):
                got, err = self.monitor._CodexUsageMonitor__wait_for_snapshot_ready(page, timeout_sec=5.0)

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertIn("https://chatgpt.com/codex/cloud/settings/analytics#usage", page.goto_calls)

    def test_build_snapshot_accepts_semantic_limit_metric_snapshot(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__probe_usage_page",
            return_value={
                "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                "mainText": "Usage 5-hour usage limit 25% weekly usage limit 28%",
                "metricBlocks": [
                    {
                        "metric_key": "five_hour_limit",
                        "label_text": "5-hour usage limit",
                        "block_text": "5-hour usage limit 25% Resets at 2026-03-30T15:00:00+09:00",
                        "value_candidates": ["25%"],
                        "reset_at_candidates": ["2026-03-30T15:00:00+09:00"],
                    },
                    {
                        "metric_key": "weekly_limit",
                        "label_text": "weekly usage limit",
                        "block_text": "weekly usage limit 28% Resets at 2026-04-02T12:00:00+09:00",
                        "value_candidates": ["28%"],
                        "reset_at_candidates": ["2026-04-02T12:00:00+09:00"],
                    },
                    {
                        "metric_key": "gpt_5_3_codex_spark_five_hour_limit",
                        "label_text": "gpt-5.3-codex-spark 5-hour usage limit",
                        "block_text": "gpt-5.3-codex-spark 5-hour usage limit 79% 오후 12:08 초기화",
                        "value_candidates": ["79%"],
                        "reset_candidates": ["gpt-5.3-codex-spark 5-hour usage limit 79% 오후 12:08 초기화"],
                    },
                    {
                        "metric_key": "gpt_5_3_codex_spark_weekly_limit",
                        "label_text": "gpt-5.3-codex-spark weekly usage limit",
                        "block_text": "gpt-5.3-codex-spark weekly usage limit 74% 2026. 4. 1. 오후 12:14 초기화",
                        "value_candidates": ["74%"],
                        "reset_candidates": ["2026. 4. 1. 오후 12:14 초기화"],
                    },
                ],
            },
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__now_iso",
                return_value="2026-03-30T12:05:00",
            ):
                snap = self.monitor._CodexUsageMonitor__build_snapshot_from_page(_DummyPage())

        self.assertIsNotNone(snap)
        self.assertEqual(snap.five_hour_limit, "25%")
        self.assertEqual(snap.weekly_limit, "28%")
        self.assertEqual(snap.five_hour_limit_reset_at, "2026-03-30T15:00:00+09:00")
        self.assertEqual(snap.weekly_limit_reset_at, "2026-04-02T12:00:00+09:00")
        self.assertEqual(
            snap.gpt_5_3_codex_spark_five_hour_limit_reset_at,
            "2026-03-30T12:08:00+09:00",
        )
        self.assertEqual(
            snap.gpt_5_3_codex_spark_weekly_limit_reset_at,
            "2026-04-01T12:14:00+09:00",
        )

    def test_build_snapshot_accepts_fragmentless_analytics_probe_origin_when_metrics_exist(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__probe_usage_page",
            return_value={
                "url": "https://chatgpt.com/codex/cloud/settings/analytics",
                "mainText": "Analytics Usage 5-hour usage limit 25% weekly usage limit 28%",
                "metricBlocks": [
                    {
                        "metric_key": "five_hour_limit",
                        "label_text": "5-hour usage limit",
                        "block_text": "5-hour usage limit 25%",
                        "value_candidates": ["25%"],
                    },
                    {
                        "metric_key": "weekly_limit",
                        "label_text": "weekly usage limit",
                        "block_text": "weekly usage limit 28%",
                        "value_candidates": ["28%"],
                    },
                ],
            },
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__now_iso",
                return_value="2026-03-30T12:05:30",
            ):
                snap = self.monitor._CodexUsageMonitor__build_snapshot_from_page(_DummyPage())

        self.assertIsNotNone(snap)
        self.assertEqual(snap.five_hour_limit, "25%")
        self.assertEqual(snap.weekly_limit, "28%")

    def test_build_snapshot_rejects_metric_block_when_value_normalization_fails(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__probe_usage_page",
            return_value={
                "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                "mainText": "Usage 5-hour usage limit Connectors",
                "metricBlocks": [
                    {
                        "metric_key": "five_hour_limit",
                        "label_text": "5-hour usage limit",
                        "block_text": "5-hour usage limit Connectors",
                        "value_candidates": ["Connectors"],
                    }
                ],
            },
        ):
            snap = self.monitor._CodexUsageMonitor__build_snapshot_from_page(_DummyPage())

        self.assertIsNone(snap)

    def test_wait_for_snapshot_ready_returns_parse_failed_when_usage_page_reached_but_no_metric_blocks(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def wait_for_timeout(self, _ms):
                return None

        probes = [
            {
                "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                "mainText": "Usage loading",
                "metricBlocks": [],
            },
            {
                "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                "mainText": "Usage loading",
                "metricBlocks": [],
            },
        ]

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__probe_usage_page",
            side_effect=probes,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__is_cloudflare_challenge",
                return_value=False,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__is_login_required",
                    return_value=False,
                ):
                    with patch.object(
                        self.monitor._CodexUsageMonitor__lib.time,
                        "monotonic",
                        side_effect=[0.0, 0.1, 5.1],
                    ):
                        got, err = self.monitor._CodexUsageMonitor__wait_for_snapshot_ready(
                            _DummyPage(),
                            timeout_sec=5.0,
                        )

        self.assertIsNone(got)
        self.assertEqual(err, "parse_failed")

    def test_wait_for_snapshot_ready_retries_until_dom_ready_then_accepts_snapshot(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def wait_for_timeout(self, _ms):
                return None

        probe_loading = {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            "mainText": "Usage loading",
            "metricBlocks": [],
        }
        probe_ready = {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            "mainText": "Usage 5-hour usage limit 24%",
            "metricBlocks": [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "block_text": "5-hour usage limit 24%",
                    "value_candidates": ["24%"],
                }
            ],
        }

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__probe_usage_page",
            side_effect=[probe_loading, probe_ready],
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__now_iso",
                return_value="2026-03-30T12:07:00",
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.time,
                    "monotonic",
                    side_effect=[0.0, 0.2, 0.4],
                ):
                    got, err = self.monitor._CodexUsageMonitor__wait_for_snapshot_ready(
                        _DummyPage(),
                        timeout_sec=5.0,
                    )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertEqual(got.five_hour_limit, "24%")

    def test_collect_snapshot_once_prefers_cdp_context_for_interactive(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyPage:
            url = "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, None),
        ) as launch_cdp:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__launch_browser_context",
                side_effect=AssertionError("fallback launch should not be used"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__is_cloudflare_challenge",
                    return_value=False,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_login_required",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__build_snapshot_from_page",
                            return_value=snapshot,
                        ):
                            got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                object(),
                                headless=False,
                                allow_interactive_recovery=True,
                                prefer_system_channel=True,
                                initial_url="https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
                            )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(launch_cdp.called)
        self.assertFalse(
            bool(launch_cdp.call_args.kwargs.get("start_hidden", True))
        )

    def test_collect_snapshot_once_interactive_recovery_closes_app_cdp_after_success(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 55555
            _ws_cdp_port = 48123

            def poll(self):
                return None

        class _DummyPage:
            url = "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyBrowser:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                return None

        context = _DummyContext()
        browser = _DummyBrowser()
        proc = _DummyProc()

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(context, browser, proc),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__set_cdp_window_visibility",
            ) as set_visibility:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__terminate_spawned_process",
                ) as terminate_proc:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__ui_post",
                                    side_effect=lambda fn: fn(),
                                ):
                                    with patch.object(
                                        self.monitor,
                                        "_CodexUsageMonitor__hide_active_tooltip",
                                    ) as hide_tooltip:
                                        got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                            object(),
                                            headless=False,
                                            allow_interactive_recovery=True,
                                            force_hidden=False,
                                            prefer_system_channel=True,
                                            initial_url="https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
                                        )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        hide_tooltip.assert_called_once()
        self.assertIsNone(self.monitor._CodexUsageMonitor__hidden_cdp_proc)
        self.assertEqual(int(self.monitor._CodexUsageMonitor__hidden_cdp_port), 0)
        self.assertEqual(
            set_visibility.call_args_list[0].kwargs,
            {"visible": True, "bring_to_front": True},
        )
        self.assertIs(set_visibility.call_args_list[0].args[0], proc)
        self.assertEqual(set_visibility.call_count, 1)
        terminate_proc.assert_called_once_with(proc, cleanup_orphans=False)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    def test_collect_snapshot_once_closes_interactive_cdp_when_hide_fails_after_snapshot(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 55559
            _ws_cdp_port = 48126

            def poll(self):
                return None

        class _DummyPage:
            url = "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyBrowser:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                return None

        context = _DummyContext()
        browser = _DummyBrowser()
        proc = _DummyProc()

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(context, browser, proc),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__set_cdp_window_visibility",
                return_value=False,
            ) as set_visibility:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__terminate_spawned_process",
                ) as terminate_proc:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    object(),
                                    headless=False,
                                    allow_interactive_recovery=True,
                                    force_hidden=False,
                                    prefer_system_channel=True,
                                    initial_url="https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
                                )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertIsNone(self.monitor._CodexUsageMonitor__hidden_cdp_proc)
        self.assertEqual(int(self.monitor._CodexUsageMonitor__hidden_cdp_port), 0)
        self.assertEqual(
            set_visibility.call_args_list[0].kwargs,
            {"visible": True, "bring_to_front": True},
        )
        self.assertIs(set_visibility.call_args_list[0].args[0], proc)
        self.assertEqual(set_visibility.call_count, 1)
        terminate_proc.assert_called_once_with(proc, cleanup_orphans=False)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    def test_collect_snapshot_once_interactive_recovery_closes_app_cdp_after_wait(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 55556
            _ws_cdp_port = 48124

            def poll(self):
                return None

        class _DummyPage:
            url = "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyBrowser:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                return None

        context = _DummyContext()
        browser = _DummyBrowser()
        proc = _DummyProc()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(context, browser, proc),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__set_cdp_window_visibility",
            ) as set_visibility:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__terminate_spawned_process",
                ) as terminate_proc:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=None,
                            ):
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__wait_for_snapshot_ready",
                                    return_value=(snapshot, None),
                                ):
                                    got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                        object(),
                                        headless=False,
                                        allow_interactive_recovery=True,
                                        force_hidden=False,
                                        prefer_system_channel=True,
                                        initial_url="https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
                                    )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertIsNone(self.monitor._CodexUsageMonitor__hidden_cdp_proc)
        self.assertEqual(int(self.monitor._CodexUsageMonitor__hidden_cdp_port), 0)
        self.assertEqual(
            set_visibility.call_args_list[0].kwargs,
            {"visible": True, "bring_to_front": True},
        )
        self.assertIs(set_visibility.call_args_list[0].args[0], proc)
        self.assertEqual(set_visibility.call_count, 1)
        terminate_proc.assert_called_once_with(proc, cleanup_orphans=False)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    def test_collect_snapshot_once_keeps_interactive_cdp_open_when_login_still_pending(self) -> None:
        class _DummyProc:
            pid = 55557
            _ws_cdp_port = 48125

            def poll(self):
                return None

        class _DummyPage:
            url = "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyBrowser:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                return None

        context = _DummyContext()
        browser = _DummyBrowser()
        proc = _DummyProc()

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(context, browser, proc),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__set_cdp_window_visibility",
            ) as set_visibility:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__terminate_spawned_process",
                ) as terminate_proc:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=True,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__wait_until_logged_in",
                                return_value=False,
                            ):
                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    object(),
                                    headless=False,
                                    allow_interactive_recovery=True,
                                    force_hidden=False,
                                    prefer_system_channel=True,
                                    initial_url="https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
                                )

        self.assertIsNone(got)
        self.assertEqual(err, "login_required")
        self.assertEqual(int(self.monitor._CodexUsageMonitor__last_successful_cdp_port), 48125)
        self.assertTrue(bool(getattr(proc, "_ws_monitor_managed", False)))
        self.assertIs(self.monitor._CodexUsageMonitor__hidden_cdp_proc, proc)
        self.assertEqual(int(self.monitor._CodexUsageMonitor__hidden_cdp_port), 48125)
        set_visibility.assert_called_once_with(proc, visible=True, bring_to_front=True)
        terminate_proc.assert_not_called()
        self.assertFalse(context.closed)
        self.assertFalse(browser.closed)

    def test_collect_snapshot_once_hides_cdp_window_when_force_hidden(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 12345

        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, _DummyProc()),
        ) as launch_cdp:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_spawned_process",
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__set_cdp_window_visibility",
                ) as set_visibility:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    object(),
                                    headless=False,
                                    allow_interactive_recovery=False,
                                    force_hidden=True,
                                    prefer_system_channel=True,
                                )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(
            bool(launch_cdp.call_args.kwargs.get("start_hidden", False))
        )
        self.assertTrue(set_visibility.called)
        args, kwargs = set_visibility.call_args
        self.assertFalse(kwargs.get("visible", args[1] if len(args) > 1 else True))

    def test_connect_hidden_cdp_context_attaches_existing_remote_debug_process(self) -> None:
        profile = "c:/tmp/chatgpt-profile"
        self.monitor._CodexUsageMonitor__profile_dir = profile
        self.monitor._CodexUsageMonitor__hidden_cdp_proc = None
        self.monitor._CodexUsageMonitor__hidden_cdp_port = 0

        class _DummyProcInfo:
            def __init__(self, pid: int, name: str, cmdline: list[str]):
                self.info = {"pid": int(pid), "name": str(name), "cmdline": list(cmdline)}

        class _DummyContext:
            pass

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def __init__(self):
                self.endpoints: list[str] = []
                self.timeouts: list[int | None] = []

            def connect_over_cdp(self, endpoint, timeout=None, **_kwargs):
                self.endpoints.append(str(endpoint))
                self.timeouts.append(timeout)
                return _DummyBrowser()

        class _DummyPlaywright:
            def __init__(self):
                self.chromium = _DummyChromium()

        process_items = [
            _DummyProcInfo(
                24652,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-port=9333",
                    f"--user-data-dir={profile}",
                ],
            ),
        ]
        playwright_obj = _DummyPlaywright()
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.psutil,
            "process_iter",
            return_value=process_items,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__is_profile_locked_without_remote_debugging",
                side_effect=AssertionError("lock check should not run after successful external attach"),
            ):
                context, browser, proc, keep = self.monitor._CodexUsageMonitor__connect_hidden_cdp_context(
                    playwright_obj,
                    launch_url="https://chatgpt.com/codex/cloud/settings/analytics#usage",
                )

        self.assertIsNotNone(context)
        self.assertIsNotNone(browser)
        self.assertTrue(bool(keep))
        self.assertTrue(bool(getattr(proc, "_ws_external_cdp", False)))
        self.assertEqual(int(getattr(proc, "_ws_cdp_port", 0)), 9333)
        self.assertEqual(playwright_obj.chromium.endpoints, ["http://127.0.0.1:9333"])
        self.assertEqual(
            playwright_obj.chromium.timeouts,
            [self.monitor._CodexUsageMonitor__cdp_connect_timeout_ms],
        )

    def test_collect_snapshot_once_external_cdp_avoids_window_hide_and_closes_temp_page(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _ExternalProc:
            _ws_external_cdp = True
            pid = 24652
            _ws_listener_pid = 24652

        class _DummyPage:
            def __init__(self):
                self.url = "about:blank"
                self.closed = False
                self.goto_calls: list[str] = []

            def goto(self, url, **_kwargs):
                self.url = str(url)
                self.goto_calls.append(str(url))
                return None

            def wait_for_timeout(self, _ms):
                return None

            def close(self):
                self.closed = True
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]
                self.temp_page = _DummyPage()

            def new_page(self):
                return self.temp_page

            def close(self):
                return None

        class _DummyBrowser:
            def close(self):
                return None

        context = _DummyContext()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__connect_hidden_cdp_context",
            return_value=(context, _DummyBrowser(), _ExternalProc(), True),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__set_cdp_window_visibility",
            ) as set_visibility:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__select_collect_page",
                    side_effect=AssertionError("external attach should use temp page"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    object(),
                                    headless=False,
                                    allow_interactive_recovery=False,
                                    force_hidden=True,
                                    prefer_system_channel=True,
                                )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertFalse(set_visibility.called)
        self.assertTrue(context.temp_page.closed)
        self.assertTrue(context.temp_page.goto_calls)

    def test_collect_snapshot_once_external_managed_cdp_hides_window(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _ManagedExternalProc:
            _ws_external_cdp = True
            _ws_monitor_managed = True
            pid = 24652
            _ws_listener_pid = 24652

        class _DummyPage:
            def __init__(self):
                self.url = "about:blank"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

            def close(self):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]
                self.temp_page = _DummyPage()

            def new_page(self):
                return self.temp_page

            def close(self):
                return None

        class _DummyBrowser:
            def close(self):
                return None

        context = _DummyContext()
        selected_page = _DummyPage()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__connect_hidden_cdp_context",
            return_value=(context, _DummyBrowser(), _ManagedExternalProc(), True),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__set_cdp_window_visibility",
            ) as set_visibility:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__select_collect_page",
                    return_value=selected_page,
                ) as select_page:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    object(),
                                    headless=False,
                                    allow_interactive_recovery=False,
                                    force_hidden=True,
                                    prefer_system_channel=True,
                                )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(select_page.called)
        self.assertTrue(set_visibility.called)

    def test_iter_external_profile_remote_debugging_endpoints_marks_managed_launch_signature(
        self,
    ) -> None:
        class _DummyProcInfo:
            def __init__(self, pid: int, name: str, cmdline: list[str]):
                self.info = {"pid": int(pid), "name": str(name), "cmdline": list(cmdline)}

        profile = "c:/tmp/chatgpt-profile"
        self.monitor._CodexUsageMonitor__profile_dir = profile
        self.monitor._CodexUsageMonitor__hidden_cdp_proc = None
        self.monitor._CodexUsageMonitor__hidden_cdp_port = 0

        process_items = [
            _DummyProcInfo(
                24652,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-port=9333",
                    f'--user-data-dir="{profile}"',
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-notifications",
                ],
            ),
            _DummyProcInfo(
                24653,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-port",
                    "9334",
                    "--user-data-dir",
                    profile,
                ],
            ),
        ]
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.psutil,
            "process_iter",
            return_value=process_items,
        ):
            rows = self.monitor._CodexUsageMonitor__iter_external_profile_remote_debugging_endpoints()

        self.assertEqual(rows[0], (9333, 24652, True))
        self.assertEqual(rows[1], (9334, 24653, False))

    def test_iter_external_profile_remote_debugging_endpoints_can_include_owned_process(
        self,
    ) -> None:
        class _DummyProcInfo:
            def __init__(self, pid: int, name: str, cmdline: list[str]):
                self.info = {"pid": int(pid), "name": str(name), "cmdline": list(cmdline)}

        class _OwnedProc:
            pid = 24652
            _ws_listener_pid = 24652

        profile = "c:/tmp/chatgpt-profile"
        self.monitor._CodexUsageMonitor__profile_dir = profile
        self.monitor._CodexUsageMonitor__hidden_cdp_proc = _OwnedProc()
        self.monitor._CodexUsageMonitor__hidden_cdp_port = 9333

        process_items = [
            _DummyProcInfo(
                24652,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-port=9333",
                    f"--user-data-dir={profile}",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            ),
        ]
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.psutil,
            "process_iter",
            return_value=process_items,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__find_profile_remote_debugging_pid",
                return_value=24652,
            ):
                default_rows = (
                    self.monitor._CodexUsageMonitor__iter_external_profile_remote_debugging_endpoints()
                )
                owned_rows = self.monitor._CodexUsageMonitor__iter_external_profile_remote_debugging_endpoints(
                    include_owned=True,
                )

        self.assertEqual(default_rows, [])
        self.assertEqual(owned_rows, [(9333, 24652, True)])

    def test_iter_system_chrome_remote_debugging_endpoints_excludes_app_profile(self) -> None:
        class _DummyProcInfo:
            def __init__(self, pid: int, name: str, cmdline: list[str]):
                self.info = {"pid": int(pid), "name": str(name), "cmdline": list(cmdline)}

        app_profile = "c:/tmp/chatgpt-profile"
        self.monitor._CodexUsageMonitor__profile_dir = app_profile
        process_items = [
            _DummyProcInfo(
                3001,
                "chrome.exe",
                ["chrome.exe", "--remote-debugging-port=9222"],
            ),
            _DummyProcInfo(
                3002,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-port=9333",
                    f'--user-data-dir="{app_profile}"',
                ],
            ),
            _DummyProcInfo(
                3003,
                "chrome.exe",
                ["chrome.exe", "--type=renderer", "--remote-debugging-port=9444"],
            ),
            _DummyProcInfo(
                3004,
                "chrome.exe",
                ["chrome.exe", "--remote-debugging-port", "9555"],
            ),
        ]

        with patch.object(
            self.monitor._CodexUsageMonitor__lib.psutil,
            "process_iter",
            return_value=process_items,
        ):
            rows = self.monitor._CodexUsageMonitor__iter_system_chrome_remote_debugging_endpoints()

        self.assertEqual(rows, [(9222, 3001), (9555, 3004)])

    def test_try_collect_snapshot_via_raw_system_chrome_cdp_reads_existing_debug_port(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "18 / 40",
                "weekly_limit": "110 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "9 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "9 / 50",
                "remaining_credit": "250",
            },
            captured_at="2026-03-30T11:15:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__iter_system_chrome_remote_debugging_endpoints",
            return_value=[(9222, 3001)],
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_via_raw_cdp_port",
                return_value=snapshot,
            ) as raw_collect:
                got = self.monitor._CodexUsageMonitor__try_collect_snapshot_via_raw_system_chrome_cdp()

        self.assertIs(got, snapshot)
        raw_collect.assert_called_once_with(9222, wait_timeout_sec=None)

    def test_launch_interactive_context_via_cdp_hidden_start_disables_extensions_and_notifications(
        self,
    ) -> None:
        class _DummyProc:
            pid = 43210

            def poll(self):
                return None

        class _DummyContext:
            pass

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def __init__(self):
                self.timeouts: list[int | None] = []

            def connect_over_cdp(self, _endpoint, timeout=None, **_kwargs):
                self.timeouts.append(timeout)
                return _DummyBrowser()

        class _DummyPlaywright:
            def __init__(self):
                self.chromium = _DummyChromium()

        popen_calls: list[tuple[list[str], dict]] = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append((list(cmd), dict(kwargs)))
            return _DummyProc()

        class _DummyStartupInfo:
            def __init__(self):
                self.dwFlags = 0
                self.wShowWindow = 0

        playwright_obj = _DummyPlaywright()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__resolve_chrome_executable_path",
            return_value="C:/Program Files/Google/Chrome/Application/chrome.exe",
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.os,
                "makedirs",
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.subprocess,
                    "Popen",
                    side_effect=fake_popen,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__find_profile_remote_debugging_pid",
                        side_effect=[0, 43210],
                    ):
                        with patch.object(
                            self.monitor._CodexUsageMonitor__lib.subprocess,
                            "STARTUPINFO",
                            side_effect=_DummyStartupInfo,
                        ):
                            context, browser, proc = (
                                self.monitor._CodexUsageMonitor__launch_interactive_context_via_cdp(
                                    playwright_obj,
                                    start_hidden=True,
                                )
                            )

        self.assertIsNotNone(context)
        self.assertIsNotNone(browser)
        self.assertIsNotNone(proc)
        self.assertTrue(popen_calls)
        cmd, kwargs = popen_calls[0]
        self.assertIn("--disable-extensions", cmd)
        self.assertIn("--disable-notifications", cmd)
        self.assertIn("--start-minimized", cmd)
        self.assertIn("--disable-session-crashed-bubble", cmd)
        self.assertIn("--hide-crash-restore-bubble", cmd)
        self.assertIn("--headless=new", cmd)
        self.assertNotIn("--new-window", cmd)
        self.assertIn("--window-position=-32000,-32000", cmd)
        self.assertNotIn("about:blank", cmd)
        self.assertIn("https://chatgpt.com/codex/cloud/settings/analytics#usage", cmd)
        self.assertIn("startupinfo", kwargs)
        self.assertIn("creationflags", kwargs)
        self.assertEqual(
            playwright_obj.chromium.timeouts,
            [self.monitor._CodexUsageMonitor__cdp_connect_timeout_ms],
        )

    def test_launch_interactive_context_via_cdp_uses_ephemeral_loopback_debug_port(
        self,
    ) -> None:
        class _DummyProc:
            pid = 43211

            def poll(self):
                return None

        class _DummyContext:
            pass

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def __init__(self):
                self.endpoints: list[str] = []

            def connect_over_cdp(self, endpoint, **_kwargs):
                self.endpoints.append(str(endpoint))
                return _DummyBrowser()

        class _DummyPlaywright:
            def __init__(self):
                self.chromium = _DummyChromium()

        popen_calls: list[list[str]] = []

        def fake_popen(cmd, **_kwargs):
            popen_calls.append(list(cmd))
            return _DummyProc()

        playwright_obj = _DummyPlaywright()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__resolve_chrome_executable_path",
            return_value="C:/Program Files/Google/Chrome/Application/chrome.exe",
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.os,
                "makedirs",
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.subprocess,
                    "Popen",
                    side_effect=fake_popen,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__find_profile_remote_debugging_pid",
                        side_effect=[0, 43211],
                    ):
                        context, browser, proc = (
                            self.monitor._CodexUsageMonitor__launch_interactive_context_via_cdp(
                                playwright_obj,
                                start_hidden=True,
                            )
                        )

        self.assertIsNotNone(context)
        self.assertIsNotNone(browser)
        self.assertIsNotNone(proc)
        self.assertTrue(popen_calls)
        cmd = popen_calls[0]
        debug_port_flags = [
            item for item in cmd if str(item).startswith("--remote-debugging-port=")
        ]
        self.assertEqual(len(debug_port_flags), 1)
        debug_port = int(debug_port_flags[0].split("=", 1)[1])
        self.assertNotIn(debug_port, range(9333, 9345))
        self.assertGreater(debug_port, 0)
        self.assertIn("--remote-debugging-address=127.0.0.1", cmd)
        self.assertEqual(playwright_obj.chromium.endpoints, [f"http://127.0.0.1:{debug_port}"])

    def test_launch_interactive_context_via_cdp_accepts_listener_pid_remap(self) -> None:
        class _DummyProc:
            def __init__(self, pid):
                self.pid = int(pid)

            def poll(self):
                return None

        class _DummyContext:
            pass

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def __init__(self):
                self.timeouts: list[int | None] = []

            def connect_over_cdp(self, _endpoint, timeout=None, **_kwargs):
                self.timeouts.append(timeout)
                return _DummyBrowser()

        class _DummyPlaywright:
            def __init__(self):
                self.chromium = _DummyChromium()

        popen_calls: list[list[str]] = []
        popen_pids = [11111]

        def fake_popen(cmd, **_kwargs):
            popen_calls.append(list(cmd))
            return _DummyProc(popen_pids[len(popen_calls) - 1])

        playwright_obj = _DummyPlaywright()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__resolve_chrome_executable_path",
            return_value="C:/Program Files/Google/Chrome/Application/chrome.exe",
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.os,
                "makedirs",
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.subprocess,
                    "Popen",
                    side_effect=fake_popen,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__terminate_spawned_process",
                    ) as terminate_proc:
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__find_profile_remote_debugging_pid",
                            side_effect=[0, 99999],
                        ):
                            context, browser, proc = (
                                self.monitor._CodexUsageMonitor__launch_interactive_context_via_cdp(
                                    playwright_obj,
                                    start_hidden=False,
                                )
                            )

        self.assertIsNotNone(context)
        self.assertIsNotNone(browser)
        self.assertIsNotNone(proc)
        self.assertEqual(int(getattr(proc, "pid", 0)), 11111)
        self.assertEqual(len(popen_calls), 1)
        self.assertFalse(terminate_proc.called)
        debug_port_flags = [
            item for item in popen_calls[0] if str(item).startswith("--remote-debugging-port=")
        ]
        self.assertEqual(len(debug_port_flags), 1)
        debug_port = int(debug_port_flags[0].split("=", 1)[1])
        self.assertNotIn(debug_port, range(9333, 9345))
        self.assertIn("--remote-debugging-address=127.0.0.1", popen_calls[0])
        self.assertIn("--disable-session-crashed-bubble", popen_calls[0])
        self.assertIn("--hide-crash-restore-bubble", popen_calls[0])
        self.assertIn("--no-first-run", popen_calls[0])
        self.assertIn("--window-size=960,720", popen_calls[0])
        self.assertIn("--window-position=32,32", popen_calls[0])
        self.assertEqual(
            playwright_obj.chromium.timeouts,
            [self.monitor._CodexUsageMonitor__cdp_connect_timeout_ms],
        )

    def test_launch_interactive_context_via_cdp_visible_start_disables_extensions(
        self,
    ) -> None:
        class _DummyProc:
            pid = 43212

            def poll(self):
                return None

        class _DummyContext:
            pass

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def connect_over_cdp(self, _endpoint, **_kwargs):
                return _DummyBrowser()

        class _DummyPlaywright:
            def __init__(self):
                self.chromium = _DummyChromium()

        popen_calls: list[tuple[list[str], dict]] = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append((list(cmd), dict(kwargs)))
            return _DummyProc()

        class _DummyStartupInfo:
            def __init__(self):
                self.dwFlags = 0
                self.wShowWindow = 0

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__resolve_chrome_executable_path",
            return_value="C:/Program Files/Google/Chrome/Application/chrome.exe",
        ):
            with patch.object(self.monitor._CodexUsageMonitor__lib.os, "makedirs"):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.subprocess,
                    "Popen",
                    side_effect=fake_popen,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__find_profile_remote_debugging_pid",
                        side_effect=[0, 43212],
                    ):
                        with patch.object(
                            self.monitor._CodexUsageMonitor__lib.subprocess,
                            "STARTUPINFO",
                            side_effect=_DummyStartupInfo,
                        ):
                            context, browser, proc = (
                                self.monitor._CodexUsageMonitor__launch_interactive_context_via_cdp(
                                    _DummyPlaywright(),
                                    start_hidden=False,
                                )
                            )

        self.assertIsNotNone(context)
        self.assertIsNotNone(browser)
        self.assertIsNotNone(proc)
        self.assertTrue(popen_calls)
        cmd, kwargs = popen_calls[0]
        self.assertIn("--disable-extensions", cmd)
        self.assertIn("--disable-notifications", cmd)
        self.assertIn("--new-window", cmd)
        self.assertNotIn("--headless=new", cmd)
        self.assertIn("startupinfo", kwargs)
        self.assertIn("creationflags", kwargs)

    def test_collect_snapshot_once_reuses_hidden_cdp_process_between_calls(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 54321
            _ws_cdp_port = 48125

            def poll(self):
                return None

        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def __init__(self):
                self.connect_calls = 0
                self.timeouts: list[int | None] = []

            def connect_over_cdp(self, _endpoint, timeout=None, **_kwargs):
                self.connect_calls += 1
                self.timeouts.append(timeout)
                return _DummyBrowser()

        class _DummyPlaywright:
            def __init__(self):
                self.chromium = _DummyChromium()

        pw = _DummyPlaywright()

        self.monitor._CodexUsageMonitor__hidden_cdp_proc = None
        self.monitor._CodexUsageMonitor__hidden_cdp_port = 0

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), _DummyBrowser(), _DummyProc()),
        ) as launch_cdp:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__connect_existing_profile_remote_debug_context",
                return_value=(None, None, None, False),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__set_cdp_window_visibility",
                    return_value=True,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                got1, err1 = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    pw,
                                    headless=False,
                                    allow_interactive_recovery=False,
                                    force_hidden=True,
                                    prefer_system_channel=True,
                                )
                                got2, err2 = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    pw,
                                    headless=False,
                                    allow_interactive_recovery=False,
                                    force_hidden=True,
                                    prefer_system_channel=True,
                                )

        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertIsNotNone(got1)
        self.assertIsNotNone(got2)
        self.assertEqual(launch_cdp.call_count, 1)
        self.assertEqual(pw.chromium.connect_calls, 1)
        self.assertIs(self.monitor._CodexUsageMonitor__hidden_cdp_proc, launch_cdp.return_value[2])
        self.assertEqual(int(self.monitor._CodexUsageMonitor__hidden_cdp_port), 48125)

    def test_refresh_collect_page_reloads_existing_usage_page(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def __init__(self):
                self.reload_calls: list[dict] = []
                self.goto_calls: list[str] = []

            def reload(self, **kwargs):
                self.reload_calls.append(dict(kwargs))
                return None

            def goto(self, url, **_kwargs):
                self.goto_calls.append(str(url))
                return None

        page = _DummyPage()

        self.monitor._CodexUsageMonitor__refresh_collect_page(
            page,
            "https://chatgpt.com/codex/cloud/settings/analytics#usage",
        )

        self.assertEqual(len(page.reload_calls), 1)
        self.assertEqual(page.goto_calls, [])

    def test_select_collect_page_prefers_non_blank_and_closes_extra_blank_tabs(self) -> None:
        class _DummyPage:
            def __init__(self, url):
                self.url = url
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyContext:
            def __init__(self, pages):
                self.pages = pages

            def new_page(self):
                p = _DummyPage("about:blank")
                self.pages.append(p)
                return p

        blank1 = _DummyPage("about:blank")
        usage = _DummyPage("https://chatgpt.com/codex/cloud/settings/analytics#usage")
        blank2 = _DummyPage("chrome://newtab/")
        ctx = _DummyContext([blank1, usage, blank2])

        selected = self.monitor._CodexUsageMonitor__select_collect_page(
            ctx,
            preferred_url="https://chatgpt.com/codex/cloud/settings/analytics#usage",
            close_extra_blank_tabs=True,
        )

        self.assertIs(selected, usage)
        self.assertTrue(blank1.closed)
        self.assertTrue(blank2.closed)

    def test_select_collect_page_closes_duplicate_usage_tabs_for_managed_hidden_context(self) -> None:
        class _DummyPage:
            def __init__(self, url):
                self.url = url
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyContext:
            def __init__(self, pages):
                self.pages = pages

            def new_page(self):
                p = _DummyPage("about:blank")
                self.pages.append(p)
                return p

        selected_usage = _DummyPage("https://chatgpt.com/codex/cloud/settings/analytics#usage")
        duplicate_usage = _DummyPage("https://chatgpt.com/codex/cloud/settings/analytics")
        login = _DummyPage("https://chatgpt.com/auth/login")
        ctx = _DummyContext([selected_usage, duplicate_usage, login])

        selected = self.monitor._CodexUsageMonitor__select_collect_page(
            ctx,
            preferred_url="https://chatgpt.com/codex/cloud/settings/analytics#usage",
            close_extra_blank_tabs=True,
        )

        self.assertIs(selected, selected_usage)
        self.assertTrue(duplicate_usage.closed)
        self.assertFalse(login.closed)

    def test_collect_snapshot_once_background_waits_briefly_for_cloudflare_clear(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 12345

        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, _DummyProc()),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_spawned_process",
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__set_cdp_window_visibility",
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        side_effect=[True, False],
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__wait_until_cloudflare_cleared",
                            return_value=True,
                        ) as wait_cf:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__is_login_required",
                                return_value=False,
                            ):
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__build_snapshot_from_page",
                                    return_value=snapshot,
                                ):
                                    got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                        object(),
                                        headless=False,
                                        allow_interactive_recovery=False,
                                        force_hidden=True,
                                        prefer_system_channel=True,
                                    )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(wait_cf.called)

    def test_collect_snapshot_once_background_returns_cloudflare_when_challenge_persists(self) -> None:
        class _DummyProc:
            pid = 12345

        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, _DummyProc()),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_spawned_process",
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__set_cdp_window_visibility",
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=True,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__wait_until_cloudflare_cleared",
                            return_value=False,
                        ) as wait_cf:
                            got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                object(),
                                headless=False,
                                allow_interactive_recovery=False,
                                force_hidden=True,
                                prefer_system_channel=True,
                            )

        self.assertIsNone(got)
        self.assertEqual(err, "cloudflare_challenge")
        self.assertTrue(wait_cf.called)

    def test_collect_snapshot_once_force_hidden_uses_headless_fallback_when_cdp_unavailable(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(None, None, None),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__launch_browser_context",
                return_value=_DummyContext(),
            ) as launch_context:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__apply_headless_fast_routes",
                ) as fast_routes:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    object(),
                                    headless=False,
                                    allow_interactive_recovery=False,
                                    force_hidden=True,
                                    prefer_system_channel=True,
                                )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(launch_context.called)
        self.assertTrue(fast_routes.called)
        self.assertTrue(launch_context.call_args.kwargs.get("headless"))

    def test_set_cdp_window_visibility_falls_back_to_profile_pids(self) -> None:
        class _DummyProc:
            pid = 111

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__set_windows_visibility_for_pid",
            side_effect=[False, True],
        ) as set_by_pid:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__list_profile_chrome_pids",
                return_value=[111, 222],
            ):
                ok = self.monitor._CodexUsageMonitor__set_cdp_window_visibility(
                    _DummyProc(),
                    visible=False,
                    bring_to_front=False,
                    timeout_sec=1.0,
                )

        self.assertTrue(ok)
        called_pids: list[int] = []
        for call in set_by_pid.call_args_list:
            if call.args:
                called_pids.append(int(call.args[0]))
            else:
                called_pids.append(int(call.kwargs.get("pid")))
        self.assertIn(111, called_pids)
        self.assertIn(222, called_pids)

    def test_set_cdp_window_visibility_only_uses_managed_profile_pids_when_hiding(self) -> None:
        class _DummyProc:
            pid = 111

        class _DummyProcInfo:
            def __init__(self, pid: int, name: str, cmdline: list[str]):
                self.info = {"pid": int(pid), "name": str(name), "cmdline": list(cmdline)}

        profile = "c:/tmp/chatgpt-profile"
        self.monitor._CodexUsageMonitor__profile_dir = profile
        process_items = [
            _DummyProcInfo(
                222,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-port=9333",
                    f"--user-data-dir={profile}",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-notifications",
                ],
            ),
            _DummyProcInfo(
                333,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-port=9444",
                    f"--user-data-dir={profile}",
                ],
            ),
        ]

        with patch.object(
            self.monitor._CodexUsageMonitor__lib.psutil,
            "process_iter",
            return_value=process_items,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__set_windows_visibility_for_pid",
                return_value=False,
            ) as set_by_pid:
                self.monitor._CodexUsageMonitor__set_cdp_window_visibility(
                    _DummyProc(),
                    visible=False,
                    bring_to_front=False,
                    timeout_sec=1.0,
                )

        called_pids: list[int] = []
        for call in set_by_pid.call_args_list:
            if call.args:
                called_pids.append(int(call.args[0]))
            else:
                called_pids.append(int(call.kwargs.get("pid")))
        self.assertIn(111, called_pids)
        self.assertIn(222, called_pids)
        self.assertNotIn(333, called_pids)

    def test_set_windows_visibility_for_pid_hides_window_from_taskbar(self) -> None:
        class _DummyWin32Gui:
            def __init__(self):
                self.style = 0x00040000
                self.set_styles: list[tuple[int, int, int]] = []
                self.position_calls: list[tuple[int, int, int, int, int, int, int]] = []
                self.show_calls: list[tuple[int, int]] = []

            def GetWindowLong(self, hwnd, index):
                self.set_styles.append((int(hwnd), int(index), self.style))
                return self.style

            def SetWindowLong(self, hwnd, index, value):
                self.style = int(value)
                self.set_styles.append((int(hwnd), int(index), int(value)))
                return int(value)

            def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
                self.position_calls.append(
                    (
                        int(hwnd),
                        int(insert_after),
                        int(x),
                        int(y),
                        int(cx),
                        int(cy),
                        int(flags),
                    )
                )
                return True

            def ShowWindow(self, hwnd, command):
                self.show_calls.append((int(hwnd), int(command)))
                return True

        fake_win32gui = _DummyWin32Gui()

        with patch.object(self.monitor._CodexUsageMonitor__lib.os, "name", "nt"):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib,
                "win32gui",
                fake_win32gui,
                create=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__list_top_windows_for_pid",
                    return_value=[789],
                ):
                    ok = self.monitor._CodexUsageMonitor__set_windows_visibility_for_pid(
                        pid=123,
                        visible=False,
                        bring_to_front=False,
                        timeout_sec=0.2,
                    )

        self.assertTrue(ok)
        self.assertEqual(fake_win32gui.style & 0x00040000, 0)
        self.assertTrue(fake_win32gui.style & 0x00000080)
        self.assertTrue(fake_win32gui.position_calls)
        self.assertEqual(fake_win32gui.position_calls[-1][2:4], (-32000, -32000))
        self.assertEqual(fake_win32gui.show_calls, [(789, 0)])

    def test_set_windows_visibility_for_pid_restores_without_activating_by_default(self) -> None:
        class _DummyWin32Gui:
            def __init__(self):
                self.show_calls: list[tuple[int, int]] = []
                self.foreground_calls: list[int] = []

            def ShowWindow(self, hwnd, command):
                self.show_calls.append((int(hwnd), int(command)))
                return True

            def SetForegroundWindow(self, hwnd):
                self.foreground_calls.append(int(hwnd))
                return True

        fake_win32gui = _DummyWin32Gui()

        with patch.object(self.monitor._CodexUsageMonitor__lib.os, "name", "nt"):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib,
                "win32gui",
                fake_win32gui,
                create=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__list_top_windows_for_pid",
                    return_value=[789],
                ):
                    ok = self.monitor._CodexUsageMonitor__set_windows_visibility_for_pid(
                        pid=123,
                        visible=True,
                        bring_to_front=False,
                        timeout_sec=0.2,
                    )

        self.assertTrue(ok)
        self.assertEqual(fake_win32gui.show_calls, [(789, 4)])
        self.assertEqual(fake_win32gui.foreground_calls, [])

    def test_set_windows_visibility_for_pid_moves_offscreen_window_on_manual_restore(self) -> None:
        class _DummyWin32Gui:
            def __init__(self):
                self.show_calls: list[tuple[int, int]] = []
                self.foreground_calls: list[int] = []
                self.position_calls: list[tuple[int, int, int, int, int, int, int]] = []

            def ShowWindow(self, hwnd, command):
                self.show_calls.append((int(hwnd), int(command)))
                return True

            def SetForegroundWindow(self, hwnd):
                self.foreground_calls.append(int(hwnd))
                return True

            def GetWindowRect(self, _hwnd):
                return (-32000, -32000, -30720, -31100)

            def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
                self.position_calls.append(
                    (
                        int(hwnd),
                        int(insert_after),
                        int(x),
                        int(y),
                        int(cx),
                        int(cy),
                        int(flags),
                    )
                )
                return True

        class _DummyUser32:
            def GetSystemMetrics(self, index):
                values = {
                    0: 1920,
                    1: 1080,
                    76: 0,
                    77: 0,
                    78: 1920,
                    79: 1080,
                }
                return values.get(int(index), 0)

        class _DummyCtypes:
            class _Windll:
                user32 = _DummyUser32()

            windll = _Windll()

        fake_win32gui = _DummyWin32Gui()

        with patch.object(self.monitor._CodexUsageMonitor__lib.os, "name", "nt"):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib,
                "win32gui",
                fake_win32gui,
                create=True,
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib,
                    "ctypes",
                    _DummyCtypes(),
                    create=True,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__list_top_windows_for_pid",
                        return_value=[789],
                    ):
                        ok = self.monitor._CodexUsageMonitor__set_windows_visibility_for_pid(
                            pid=123,
                            visible=True,
                            bring_to_front=True,
                            timeout_sec=0.2,
                        )

        self.assertTrue(ok)
        self.assertEqual(fake_win32gui.show_calls, [(789, 9)])
        self.assertEqual(fake_win32gui.foreground_calls, [789])
        self.assertTrue(fake_win32gui.position_calls)
        self.assertEqual(fake_win32gui.position_calls[-1][2:4], (80, 80))

    def test_set_windows_visibility_for_pid_moves_barely_visible_window_on_manual_restore(
        self,
    ) -> None:
        class _DummyWin32Gui:
            def __init__(self):
                self.show_calls: list[tuple[int, int]] = []
                self.foreground_calls: list[int] = []
                self.position_calls: list[tuple[int, int, int, int, int, int, int]] = []

            def ShowWindow(self, hwnd, command):
                self.show_calls.append((int(hwnd), int(command)))
                return True

            def SetForegroundWindow(self, hwnd):
                self.foreground_calls.append(int(hwnd))
                return True

            def GetWindowRect(self, _hwnd):
                return (-990, 40, 12, 740)

            def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
                self.position_calls.append(
                    (
                        int(hwnd),
                        int(insert_after),
                        int(x),
                        int(y),
                        int(cx),
                        int(cy),
                        int(flags),
                    )
                )
                return True

        class _DummyUser32:
            def GetSystemMetrics(self, index):
                values = {
                    0: 1024,
                    1: 768,
                    76: 0,
                    77: 0,
                    78: 1024,
                    79: 768,
                }
                return values.get(int(index), 0)

        class _DummyCtypes:
            class _Windll:
                user32 = _DummyUser32()

            windll = _Windll()

        fake_win32gui = _DummyWin32Gui()

        with patch.object(self.monitor._CodexUsageMonitor__lib.os, "name", "nt"):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib,
                "win32gui",
                fake_win32gui,
                create=True,
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib,
                    "ctypes",
                    _DummyCtypes(),
                    create=True,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__list_top_windows_for_pid",
                        return_value=[789],
                    ):
                        ok = self.monitor._CodexUsageMonitor__set_windows_visibility_for_pid(
                            pid=123,
                            visible=True,
                            bring_to_front=True,
                            timeout_sec=0.2,
                        )

        self.assertTrue(ok)
        self.assertEqual(fake_win32gui.show_calls, [(789, 9)])
        self.assertEqual(fake_win32gui.foreground_calls, [789])
        self.assertTrue(fake_win32gui.position_calls)
        _, _, x, y, width, height, _ = fake_win32gui.position_calls[-1]
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertGreaterEqual(width, 640)
        self.assertGreaterEqual(height, 480)

    def test_configure_playwright_env_adds_no_deprecation_node_option_once(self) -> None:
        with patch.dict(self.monitor._CodexUsageMonitor__lib.os.environ, {}, clear=True):
            self.monitor._CodexUsageMonitor__configure_playwright_env()
            first = str(
                self.monitor._CodexUsageMonitor__lib.os.environ.get("NODE_OPTIONS", "")
            )
            self.assertIn("--no-deprecation", first)
            self.monitor._CodexUsageMonitor__configure_playwright_env()
            second = str(
                self.monitor._CodexUsageMonitor__lib.os.environ.get("NODE_OPTIONS", "")
            )
            self.assertEqual(first, second)

    def test_collect_snapshot_once_applies_headless_fast_routes(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyPage:
            url = "https://chatgpt.com/codex/cloud/settings/analytics#usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_browser_context",
            return_value=_DummyContext(),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__apply_headless_fast_routes",
            ) as fast_routes:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__is_cloudflare_challenge",
                    return_value=False,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_login_required",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__build_snapshot_from_page",
                            return_value=snapshot,
                        ):
                            got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                object(),
                                headless=True,
                                prefer_system_channel=True,
                            )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(fast_routes.called)

    def test_build_snapshot_rejects_remaining_credit_only_noise(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__probe_usage_page",
            return_value={
                "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
                "mainText": "Usage remaining credit 0",
                "metricBlocks": [
                    {
                        "metric_key": "remaining_credit",
                        "label_text": "remaining credit",
                        "block_text": "remaining credit 0",
                        "value_candidates": ["0"],
                    }
                ],
            },
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__now_iso",
                return_value="2026-03-30T12:20:00",
            ):
                snap = self.monitor._CodexUsageMonitor__build_snapshot_from_page(object())

        self.assertIsNone(snap)

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

    def test_restart_monitor_defers_hidden_cdp_clear_while_collect_inflight(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")
        self.monitor._CodexUsageMonitor__collect_inflight = True
        self.monitor._CodexUsageMonitor__pending_hidden_cdp_clear = False

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__start_startup_warmup",
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__clear_hidden_cdp_process",
            ) as clear_hidden:
                self.monitor._CodexUsageMonitor__restart_monitor()

        self.assertFalse(clear_hidden.called)
        self.assertTrue(self.monitor._CodexUsageMonitor__pending_hidden_cdp_clear)

    def test_collect_snapshot_guarded_clears_deferred_hidden_cdp_after_inflight_done(self) -> None:
        self.monitor._CodexUsageMonitor__pending_hidden_cdp_clear = True

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot",
            return_value=(None, "collect_failed"),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__clear_hidden_cdp_process",
            ) as clear_hidden:
                _snap, err = self.monitor._CodexUsageMonitor__collect_snapshot_guarded(
                    source="manual_query"
                )

        self.assertEqual(err, "collect_failed")
        self.assertTrue(clear_hidden.called)
        self.assertFalse(self.monitor._CodexUsageMonitor__pending_hidden_cdp_clear)

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

    def test_update_settings_forces_playwright_collection_mode(self) -> None:
        self.monitor._CodexUsageMonitor__collection_mode = "api"

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
        self.assertEqual(self.monitor._CodexUsageMonitor__collection_mode, "playwright")

    def test_get_runtime_status_reports_playwright_even_if_mutated(self) -> None:
        self.monitor._CodexUsageMonitor__collection_mode = "api"
        status = self.monitor.get_runtime_status()
        self.assertEqual(status.get("collection_mode"), "playwright")

    def test_load_settings_reenforces_playwright_mode(self) -> None:
        self.monitor._CodexUsageMonitor__collection_mode = "api"
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

        self.assertEqual(self.monitor._CodexUsageMonitor__collection_mode, "playwright")

    def test_collect_snapshot_retries_once_when_playwright_bootstrap_fails(self) -> None:
        self.monitor._CodexUsageMonitor__playwright_launch_retry_count = 2
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        call_count = {"value": 0}

        class _PlaywrightCtx:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, tb):
                _ = (exc_type, exc, tb)
                return False

        def fake_sync_playwright():
            call_count["value"] += 1
            if call_count["value"] == 1:
                raise RuntimeError("transient playwright bootstrap error")
            return _PlaywrightCtx()

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__ensure_playwright_available",
            return_value=True,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_no_focus_raw_preflight",
                return_value=(None, None, False),
            ):
                with patch("playwright.sync_api.sync_playwright", side_effect=fake_sync_playwright):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__collect_with_playwright_obj",
                        return_value=(snapshot, None),
                    ) as collect_obj:
                        got, err = self.monitor._CodexUsageMonitor__collect_snapshot(
                            source="monitor_tick"
                        )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertEqual(call_count["value"], 2)
        self.assertTrue(collect_obj.called)

    def test_collect_snapshot_returns_failed_after_retry_exhausted(self) -> None:
        self.monitor._CodexUsageMonitor__playwright_launch_retry_count = 2

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__ensure_playwright_available",
            return_value=True,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__try_no_focus_raw_preflight",
                return_value=(None, None, False),
            ):
                with patch(
                    "playwright.sync_api.sync_playwright",
                    side_effect=RuntimeError("persistent playwright bootstrap error"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__collect_with_playwright_obj",
                    ) as collect_obj:
                        got, err = self.monitor._CodexUsageMonitor__collect_snapshot(
                            source="monitor_tick"
                        )

        self.assertIsNone(got)
        self.assertEqual(err, "collect_failed")
        self.assertFalse(collect_obj.called)

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
        self.assertEqual(line_map.get("5시간 사용 한도: 19 / 40"), "#16A34A")
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
        self.assertIn("5시간 사용 한도: 18 / 40", joined)
        self.assertIn("주간 사용 한도: 119 / 300", joined)
        self.assertIn("남은 크레딧: 259", joined)
        self.assertIn("5시간 사용 한도: 20 / 40 -> 18 / 40", joined)
        self.assertIn("주간 사용 한도: 120 / 300 -> 119 / 300", joined)
        self.assertIn("남은 크레딧: 260 -> 259", joined)

    def test_release_profile_session_success_resets_runtime_state(self) -> None:
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "18 / 40",
                "weekly_limit": "118 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "9 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "9 / 50",
                "remaining_credit": "258",
            },
            captured_at="2026-03-30T10:20:00",
        )
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__clear_hidden_cdp_process",
        ) as clear_hidden:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_profile_remote_debugging_processes",
            ) as terminate_remote:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__terminate_profile_chrome_processes",
                ) as terminate_profile:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__clear_profile_directory",
                        return_value=(True, "로그아웃되었습니다."),
                    ):
                        with patch.object(self.monitor, "_CodexUsageMonitor__save_state") as save_state:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__restart_monitor",
                            ) as restart_monitor:
                                ok, msg = self.monitor.release_profile_session()

        self.assertTrue(ok)
        self.assertIn("로그아웃", msg)
        self.assertFalse(self.monitor.get_last_snapshot().has_any_metric())
        self.assertEqual(
            self.monitor.get_runtime_status().get("session_state"),
            "logged_out",
        )
        self.assertTrue(clear_hidden.called)
        self.assertTrue(terminate_remote.called)
        self.assertTrue(terminate_profile.called)
        self.assertTrue(save_state.called)
        self.assertFalse(restart_monitor.called)

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

    def test_handle_collect_error_cloudflare_pauses_background_monitor(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.cancelled = []

            def after_cancel(self, token):
                self.cancelled.append(token)
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__monitor_after_id = "tick-1"
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=False,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
            ):
                self.monitor._CodexUsageMonitor__handle_collect_error(
                    "cloudflare_challenge",
                    source="monitor_tick",
                )

        status = self.monitor.get_runtime_status()
        self.assertEqual(status.get("session_state"), "logged_in")
        self.assertTrue(bool(status.get("auth_attention_required")))
        self.assertEqual(status.get("auth_attention_reason"), "cloudflare_challenge")
        self.assertEqual(status.get("monitor_state"), "paused_auth_required")
        self.assertFalse(bool(status.get("auto_monitoring_active")))
        self.assertEqual(self.monitor._CodexUsageMonitor__monitor_after_id, None)
        self.assertTrue(root.cancelled)

    def test_handle_collect_error_login_required_schedules_pending_login_poll(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__monitor_after_id = None
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=True,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.time,
                    "monotonic",
                    return_value=100.0,
                ):
                    self.monitor._CodexUsageMonitor__handle_collect_error(
                        "login_required",
                        source="manual_login",
                    )

        status = self.monitor.get_runtime_status()
        self.assertTrue(bool(status.get("pending_login_poll_active")))
        self.assertTrue(bool(status.get("can_login")))
        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, "poll-1")
        self.assertEqual(root.after_calls[0][0], 5000)

    def test_handle_collect_error_background_with_profile_cdp_does_not_schedule_poll(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=True,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__clear_hidden_cdp_process",
            ) as clear_hidden:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_tooltip",
                ):
                    self.monitor._CodexUsageMonitor__handle_collect_error(
                        "login_required",
                        source="auto_monitor",
                    )

        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, None)
        self.assertFalse(root.after_calls)
        self.assertFalse(bool(self.monitor.get_runtime_status().get("pending_login_poll_active")))
        clear_hidden.assert_called_once_with(terminate=True)

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

    def test_handle_collect_error_manual_login_schedules_poll_without_initial_cdp(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=False,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.time,
                    "monotonic",
                    return_value=100.0,
                ):
                    self.monitor._CodexUsageMonitor__handle_collect_error(
                        "login_required",
                        source="manual_login",
                    )

        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, "poll-1")
        self.assertEqual(root.after_calls[0][0], 5000)
        self.assertTrue(bool(self.monitor.get_runtime_status().get("pending_login_poll_active")))

    def test_handle_collect_error_background_does_not_poll_without_initial_cdp(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__set_session_state("logged_in")

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=False,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__show_tooltip",
            ):
                self.monitor._CodexUsageMonitor__handle_collect_error(
                    "login_required",
                    source="startup_warmup",
                )

        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, None)
        self.assertFalse(root.after_calls)
        self.assertFalse(bool(self.monitor.get_runtime_status().get("pending_login_poll_active")))

    def test_pending_login_poll_success_handles_snapshot_and_resumes_monitor(self) -> None:
        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__pending_login_poll_until_ts = 1000.0
        self.monitor._CodexUsageMonitor__set_session_state("logged_out")

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(snapshot, None),
                ) as collect_guarded:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_snapshot_tooltip",
                        ) as show_snapshot:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__schedule_monitor_tick",
                            ) as schedule_monitor:
                                with patch.object(
                                    self.monitor._CodexUsageMonitor__lib.time,
                                    "monotonic",
                                    return_value=100.0,
                                ):
                                    self.monitor._CodexUsageMonitor__pending_login_poll_tick()

        collect_guarded.assert_called_once_with(source="pending_login_poll")
        self.assertEqual(self.monitor.get_runtime_status().get("session_state"), "logged_in")
        self.assertFalse(bool(self.monitor.get_runtime_status().get("pending_login_poll_active")))
        state_path = Path(self.monitor._CodexUsageMonitor__state_path)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("session_state"), "logged_in")
        self.assertTrue(show_snapshot.called)
        self.assertTrue(schedule_monitor.called)

    def test_pending_login_poll_reschedules_when_login_is_still_required(self) -> None:
        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__pending_login_poll_until_ts = 1000.0

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "login_required"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor._CodexUsageMonitor__lib.time,
                            "monotonic",
                            return_value=100.0,
                        ):
                            self.monitor._CodexUsageMonitor__pending_login_poll_tick()

        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, "poll-1")
        self.assertEqual(root.after_calls[0][0], 8000)

    def test_pending_login_poll_stops_after_repeated_login_required_with_profile_cdp(self) -> None:
        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__pending_login_poll_until_ts = 1000.0
        self.monitor._CodexUsageMonitor__pending_login_error_max_retries = 2
        self.monitor._CodexUsageMonitor__pending_login_error_count = 2

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "login_required"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor._CodexUsageMonitor__lib.time,
                            "monotonic",
                            return_value=100.0,
                        ):
                            self.monitor._CodexUsageMonitor__pending_login_poll_tick()

        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, None)
        self.assertFalse(root.after_calls)
        self.assertFalse(bool(self.monitor.get_runtime_status().get("pending_login_poll_active")))
        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_error_count, 0)

    def test_pending_login_poll_uses_shell_safe_delay_when_collect_is_busy(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__collect_inflight = True
        self.monitor._CodexUsageMonitor__pending_login_poll_until_ts = 1000.0

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=True,
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.time,
                "monotonic",
                return_value=100.0,
            ):
                self.monitor._CodexUsageMonitor__pending_login_poll_tick()

        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, "poll-1")
        self.assertGreaterEqual(root.after_calls[0][0], 15000)

    def test_pending_login_poll_retries_transient_missing_profile_cdp(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__pending_login_poll_until_ts = 1000.0
        self.monitor._CodexUsageMonitor__pending_login_no_cdp_miss_count = 0

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=False,
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.time,
                "monotonic",
                return_value=100.0,
            ):
                self.monitor._CodexUsageMonitor__pending_login_poll_tick()

        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, "poll-1")
        self.assertEqual(root.after_calls[0][0], 5000)
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=100.0,
        ):
            status = self.monitor.get_runtime_status()
        self.assertTrue(bool(status.get("pending_login_poll_active")))
        self.assertEqual(status.get("pending_login_poll_reason"), "no_profile_cdp")
        self.assertEqual(status.get("pending_login_no_cdp_miss_count"), 1)
        self.assertEqual(status.get("pending_login_no_cdp_max_misses"), 6)
        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_no_cdp_miss_count, 1)

    def test_pending_login_poll_stops_after_repeated_missing_profile_cdp(self) -> None:
        class _DummyRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object]] = []

            def after(self, delay, fn):
                self.after_calls.append((int(delay), fn))
                return f"poll-{len(self.after_calls)}"

            def after_cancel(self, _token):
                return None

        root = _DummyRoot()
        self.monitor._CodexUsageMonitor__root = root
        self.monitor._CodexUsageMonitor__pending_login_poll_until_ts = 1000.0
        self.monitor._CodexUsageMonitor__pending_login_no_cdp_max_misses = 2
        self.monitor._CodexUsageMonitor__pending_login_no_cdp_miss_count = 2

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__has_profile_remote_debugging_endpoint",
            return_value=False,
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.time,
                "monotonic",
                return_value=100.0,
            ):
                self.monitor._CodexUsageMonitor__pending_login_poll_tick()

        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_after_id, None)
        self.assertFalse(root.after_calls)
        self.assertFalse(bool(self.monitor.get_runtime_status().get("pending_login_poll_active")))
        self.assertEqual(self.monitor._CodexUsageMonitor__pending_login_no_cdp_miss_count, 0)

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

    def test_get_runtime_status_reports_safe_existing_chrome_cdp_availability(self) -> None:
        self.monitor._CodexUsageMonitor__cdp_status_cache_ts = 0.0
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__iter_external_profile_remote_debugging_endpoints",
            return_value=[(9333, 3002, True)],
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__iter_system_chrome_remote_debugging_endpoints",
                return_value=[(9222, 3001), (9555, 3004)],
            ):
                self.monitor._CodexUsageMonitor__refresh_cdp_status_counts_sync(
                    now=100.0,
                )
                status = self.monitor.get_runtime_status()

        self.assertTrue(bool(status.get("profile_cdp_available")))
        self.assertEqual(status.get("profile_cdp_count"), 1)
        self.assertTrue(bool(status.get("system_chrome_cdp_available")))
        self.assertEqual(status.get("system_chrome_cdp_count"), 2)

    def test_get_runtime_status_uses_cached_cdp_counts_without_rescanning(self) -> None:
        self.monitor._CodexUsageMonitor__cdp_status_cache = {"profile": 1, "system": 1}
        self.monitor._CodexUsageMonitor__cdp_status_cache_ts = 99.0
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__iter_external_profile_remote_debugging_endpoints",
            side_effect=AssertionError("runtime status should not block on profile scan"),
        ) as profile_iter:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__iter_system_chrome_remote_debugging_endpoints",
                side_effect=AssertionError("runtime status should not block on system scan"),
            ) as system_iter:
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.time,
                    "monotonic",
                    return_value=100.0,
                ):
                    status = self.monitor.get_runtime_status()

        self.assertEqual(status.get("profile_cdp_count"), 1)
        self.assertEqual(status.get("system_chrome_cdp_count"), 1)
        self.assertEqual(profile_iter.call_count, 0)
        self.assertEqual(system_iter.call_count, 0)

    def test_get_runtime_status_starts_single_async_cdp_refresh_when_stale(self) -> None:
        class _DeferredThread:
            started: list[object] = []

            def __init__(self, target=None, daemon=None, name=None):
                _ = daemon, name
                self._target = target

            def start(self):
                self.started.append(self._target)
                return None

        self.monitor._CodexUsageMonitor__cdp_status_cache = {"profile": 0, "system": 0}
        self.monitor._CodexUsageMonitor__cdp_status_cache_ts = 0.0
        with patch("src.apps.codex_usage_monitor.threading.Thread", _DeferredThread):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.time,
                "monotonic",
                return_value=100.0,
            ):
                first = self.monitor.get_runtime_status()
                second = self.monitor.get_runtime_status()

        self.assertEqual(first.get("profile_cdp_count"), 0)
        self.assertEqual(second.get("profile_cdp_count"), 0)
        self.assertEqual(len(_DeferredThread.started), 1)

    def test_collect_snapshot_once_interactive_closes_extra_blank_tabs(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "7 / 50",
                "gpt_5_3_codex_spark_weekly_limit": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyPage:
            url = "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            pages = []

            def close(self):
                return None

        class _DummySelectPage:
            def __init__(self):
                self.kwargs = None
                self.page = _DummyPage()

            def __call__(self, *_args, **kwargs):
                self.kwargs = dict(kwargs)
                return self.page

        select_page = _DummySelectPage()

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, None),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__select_collect_page",
                side_effect=select_page,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__is_cloudflare_challenge",
                    return_value=False,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_login_required",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__build_snapshot_from_page",
                            return_value=snapshot,
                        ):
                            got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                object(),
                                headless=False,
                                allow_interactive_recovery=True,
                                force_hidden=False,
                                prefer_system_channel=True,
                                initial_url="https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
                            )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertIsNotNone(select_page.kwargs)
        self.assertTrue(bool(select_page.kwargs.get("close_extra_blank_tabs")))

    def test_terminate_spawned_process_terminates_listener_pid_when_remapped(self) -> None:
        class _DummyProc:
            pid = 1001
            _ws_listener_pid = 2002

            def poll(self):
                return 0

        proc = _DummyProc()

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__terminate_pid_tree",
        ) as terminate_pid_tree:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_profile_remote_debugging_processes",
            ) as terminate_orphans:
                self.monitor._CodexUsageMonitor__terminate_spawned_process(
                    proc,
                    cleanup_orphans=False,
                )

        self.assertTrue(terminate_pid_tree.called)
        self.assertEqual(
            int(terminate_pid_tree.call_args.args[0]),
            2002,
        )
        self.assertFalse(terminate_orphans.called)

    def test_collect_snapshot_once_background_skips_when_profile_locked_without_remote_debug(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__connect_hidden_cdp_context",
            return_value=(None, None, None, False),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__is_profile_locked_without_remote_debugging",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__launch_browser_context",
                    side_effect=AssertionError("fallback browser launch should be skipped"),
                ):
                    snapshot, error = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                        object(),
                        headless=False,
                        allow_interactive_recovery=False,
                        force_hidden=True,
                        prefer_system_channel=True,
                    )

        self.assertIsNone(snapshot)
        self.assertEqual(error, "profile_in_use")

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

    def test_is_profile_locked_without_remote_debugging_ignores_child_type_process(self) -> None:
        class _DummyProcInfo:
            def __init__(self, pid: int, name: str, cmdline: list[str]):
                self.info = {"pid": int(pid), "name": name, "cmdline": cmdline}

        profile = "c:/tmp/chatgpt-profile"
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__profile_dir = profile

        process_items = [
            _DummyProcInfo(
                1,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--type=renderer",
                    f"--user-data-dir={profile}",
                ],
            ),
            _DummyProcInfo(
                2,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-pipe",
                    f"--user-data-dir={profile}",
                ],
            ),
        ]
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.psutil,
            "process_iter",
            return_value=process_items,
        ):
            locked = self.monitor._CodexUsageMonitor__is_profile_locked_without_remote_debugging()

        self.assertFalse(locked)

    def test_is_profile_locked_without_remote_debugging_detects_external_remote_debug_port(self) -> None:
        class _DummyProcInfo:
            def __init__(self, pid: int, name: str, cmdline: list[str]):
                self.info = {"pid": int(pid), "name": name, "cmdline": cmdline}

        profile = "c:/tmp/chatgpt-profile"
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__profile_dir = profile
        self.monitor._CodexUsageMonitor__hidden_cdp_proc = None
        self.monitor._CodexUsageMonitor__hidden_cdp_port = 0

        process_items = [
            _DummyProcInfo(
                101,
                "chrome.exe",
                [
                    "chrome.exe",
                    "--remote-debugging-port=9333",
                    f"--user-data-dir={profile}",
                ],
            ),
        ]
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.psutil,
            "process_iter",
            return_value=process_items,
        ):
            locked = self.monitor._CodexUsageMonitor__is_profile_locked_without_remote_debugging()

        self.assertTrue(locked)


if __name__ == "__main__":
    unittest.main()
