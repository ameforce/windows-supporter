from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import src.apps.codex_usage_taskbar_overlay as taskbar_overlay
from src.apps.codex_usage_taskbar_overlay import (
    CodexUsageTaskbarOverlay,
    _column_looks_occupied,
    _get_window_handle,
    build_codex_usage_taskbar_overlay_model,
    calculate_taskbar_overlay_geometry,
)


class _FakeRoot:
    def __init__(self):
        self.after_calls = []
        self.after_cancel_calls = []

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def after(self, delay_ms, callback):
        self.after_calls.append((int(delay_ms), callback))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, after_id):
        self.after_cancel_calls.append(after_id)

    def update_idletasks(self):
        return None


class _FakeWindow:
    def __init__(self):
        self.geometry_calls = []
        self.draw_calls = []
        self.withdraw_calls = 0
        self.deiconify_calls = 0
        self.lift_calls = 0

    def geometry(self, value):
        self.geometry_calls.append(str(value))

    def draw_model(self, model):
        self.draw_calls.append(dict(model))

    def withdraw(self):
        self.withdraw_calls += 1

    def deiconify(self):
        self.deiconify_calls += 1

    def lift(self):
        self.lift_calls += 1


class _FakeCanvas:
    def __init__(self):
        self.ops = []
        self.configure_calls = []

    def configure(self, **kwargs):
        self.configure_calls.append(dict(kwargs))

    def delete(self, *args):
        self.ops.append(("delete", args, {}))

    def create_rectangle(self, *args, **kwargs):
        self.ops.append(("rectangle", args, dict(kwargs)))
        return len(self.ops)

    def create_text(self, *args, **kwargs):
        self.ops.append(("text", args, dict(kwargs)))
        return len(self.ops)

    def create_oval(self, *args, **kwargs):
        self.ops.append(("oval", args, dict(kwargs)))
        return len(self.ops)


class CodexUsageTaskbarOverlayUnitTest(unittest.TestCase):
    def _runtime(self):
        return {
            "enabled": True,
            "collect_inflight": False,
            "accounts": [
                {
                    "id": "account_1",
                    "label": "Codex 1",
                    "enabled": True,
                    "runtime": {
                        "monitor_state": "idle",
                        "session_state": "logged_in",
                        "collect_inflight": False,
                    },
                    "last_snapshot": {"five_hour_limit": "47%", "weekly_limit": "52%"},
                },
                {
                    "id": "account_2",
                    "label": "Codex 2",
                    "enabled": True,
                    "runtime": {
                        "monitor_state": "idle",
                        "session_state": "logged_in",
                        "collect_inflight": False,
                    },
                    "last_snapshot": {"five_hour_limit": "82%", "weekly_limit": "76%"},
                },
            ],
        }

    def test_model_keeps_two_account_rows_with_threshold_colors(self):
        model = build_codex_usage_taskbar_overlay_model(self._runtime())

        self.assertTrue(model["visible"])
        self.assertEqual([bar["label"] for bar in model["bars"]], ["Codex 1", "Codex 2"])
        self.assertEqual([bar["percent"] for bar in model["bars"]], [47, 82])
        self.assertEqual([bar["state"] for bar in model["bars"]], ["warning", "normal"])
        self.assertEqual(model["bars"][0]["color"], "#f59e0b")
        self.assertEqual(model["bars"][1]["color"], "#22c55e")

    def test_model_colors_remaining_percent_with_wide_safe_boundary(self):
        runtime = self._runtime()
        runtime["accounts"][0]["last_snapshot"]["five_hour_limit"] = "60%"
        runtime["accounts"][0]["last_snapshot"]["weekly_limit"] = "40%"
        runtime["accounts"][1]["last_snapshot"]["five_hour_limit"] = "59%"
        runtime["accounts"][1]["last_snapshot"]["weekly_limit"] = "39%"

        model = build_codex_usage_taskbar_overlay_model(runtime)

        colors = [
            metric["color"]
            for bar in model["bars"]
            for metric in bar["metrics"]
        ]
        states = [
            metric["state"]
            for bar in model["bars"]
            for metric in bar["metrics"]
        ]
        self.assertEqual(
            colors,
            ["#22c55e", "#f59e0b", "#f59e0b", "#ef4444"],
        )
        self.assertEqual(states, ["normal", "warning", "warning", "high"])

    def test_model_includes_five_hour_and_weekly_metrics_per_account(self):
        model = build_codex_usage_taskbar_overlay_model(self._runtime())

        first_metrics = model["bars"][0]["metrics"]
        self.assertEqual([metric["key"] for metric in first_metrics], ["5h", "7d"])
        self.assertEqual([metric["percent"] for metric in first_metrics], [47, 52])
        self.assertEqual([metric["value_text"] for metric in first_metrics], ["47%", "52%"])
        self.assertEqual(model["bars"][0]["status_text"], "OK")
        self.assertEqual(model["bars"][0]["status_color"], "#22c55e")

    def test_model_keeps_taskbar_status_stable_during_background_collection(self):
        runtime = self._runtime()
        runtime["accounts"][0]["runtime"]["collect_inflight"] = True
        runtime["accounts"][0]["runtime"]["monitor_state"] = "running"

        model = build_codex_usage_taskbar_overlay_model(runtime)

        self.assertEqual(model["state"], "collecting")
        self.assertEqual(model["bars"][0]["status_text"], "OK")
        self.assertEqual(model["bars"][0]["status_color"], "#22c55e")

    def test_model_shows_sync_when_collecting_without_snapshot_data(self):
        runtime = self._runtime()
        runtime["accounts"][0]["runtime"]["collect_inflight"] = True
        runtime["accounts"][0]["last_snapshot"]["five_hour_limit"] = ""
        runtime["accounts"][0]["last_snapshot"]["weekly_limit"] = ""

        model = build_codex_usage_taskbar_overlay_model(runtime)

        self.assertEqual(model["state"], "collecting")
        self.assertEqual(model["bars"][0]["status_text"], "SYNC")
        self.assertEqual(model["bars"][0]["status_color"], "#38bdf8")

    def test_model_keeps_account_metric_values_and_reset_times_independent(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "five_hour_limit": "76%",
                "weekly_limit": "33%",
                "five_hour_limit_reset_at": "2026-06-01T11:35:00+09:00",
                "weekly_limit_reset_at": "2026-06-05T13:49:00+09:00",
            }
        )
        runtime["accounts"][1]["last_snapshot"].update(
            {
                "five_hour_limit": "100%",
                "weekly_limit": "0%",
                "five_hour_limit_reset_at": "2026-06-01T14:10:00+09:00",
                "weekly_limit_reset_at": "2026-06-05T13:50:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metrics = model["bars"][0]["metrics"]
        second_metrics = model["bars"][1]["metrics"]
        self.assertEqual([metric["value_text"] for metric in first_metrics], ["76%", "33%"])
        self.assertEqual([metric["value_text"] for metric in second_metrics], ["100%", "0%"])
        self.assertEqual(first_metrics[0]["reset_text"], "01h 35m")
        self.assertEqual(first_metrics[1]["reset_text"], "4d 03h 49m")
        self.assertEqual(second_metrics[0]["reset_text"], "04h 10m")
        self.assertEqual(second_metrics[1]["reset_text"], "4d 03h 50m")

    def test_model_colors_reset_time_from_remaining_usage_and_metric_window(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"]["five_hour_limit"] = "20%"
        runtime["accounts"][0]["last_snapshot"]["weekly_limit"] = "90%"
        runtime["accounts"][1]["last_snapshot"]["five_hour_limit"] = "68%"
        runtime["accounts"][1]["last_snapshot"]["weekly_limit"] = "22%"
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "five_hour_limit_reset_at": "2026-06-01T14:12:00+09:00",
                "weekly_limit_reset_at": "2026-06-01T15:59:00+09:00",
            }
        )
        runtime["accounts"][1]["last_snapshot"].update(
            {
                "five_hour_limit_reset_at": "2026-06-01T10:45:00+09:00",
                "weekly_limit_reset_at": "2026-06-01T16:07:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        reset_texts = [
            metric["reset_text"]
            for bar in model["bars"]
            for metric in bar["metrics"]
        ]
        reset_short_texts = [
            metric["reset_short_text"]
            for bar in model["bars"]
            for metric in bar["metrics"]
        ]
        reset_colors = [
            metric["reset_color"]
            for bar in model["bars"]
            for metric in bar["metrics"]
        ]
        reset_states = [
            metric["reset_state"]
            for bar in model["bars"]
            for metric in bar["metrics"]
        ]
        self.assertEqual(
            reset_texts,
            ["04h 12m", "0d 05h 59m", "00h 45m", "0d 06h 07m"],
        )
        self.assertEqual(reset_short_texts, reset_texts)
        self.assertEqual(reset_colors, ["#ef4444", "#f59e0b", "#f59e0b", "#22c55e"])
        self.assertEqual(reset_states, ["urgent", "warning", "warning", "stable"])
        self.assertEqual(
            [
                metric["reset_direction"]
                for bar in model["bars"]
                for metric in bar["metrics"]
            ],
            ["shortage", "surplus", "surplus", "on_track"],
        )
        self.assertEqual(
            [
                metric["reset_marker"]
                for bar in model["bars"]
                for metric in bar["metrics"]
            ],
            ["↓", "↑", "↑", "="],
        )
        self.assertEqual(
            [
                metric["reset_badge_label"]
                for bar in model["bars"]
                for metric in bar["metrics"]
            ],
            ["부족", "남음", "남음", "정상"],
        )
        self.assertEqual(
            [
                metric["reset_badge_short_label"]
                for bar in model["bars"]
                for metric in bar["metrics"]
            ],
            ["부", "남", "남", "정"],
        )
        self.assertEqual(
            [
                metric["reset_badge_fill"]
                for bar in model["bars"]
                for metric in bar["metrics"]
            ],
            ["#7f1d1d", "#78350f", "#78350f", "#064e3b"],
        )
        self.assertEqual(
            [
                metric["reset_badge_outline"]
                for bar in model["bars"]
                for metric in bar["metrics"]
            ],
            ["#ef4444", "#f59e0b", "#f59e0b", "#22c55e"],
        )
        self.assertEqual(
            [
                metric["reset_badge_text_color"]
                for bar in model["bars"]
                for metric in bar["metrics"]
            ],
            ["#fee2e2", "#fef3c7", "#fef3c7", "#dcfce7"],
        )

    def test_model_uses_snapshot_only_tags_independent_of_history_and_now(self):
        runtime = self._runtime()
        snapshot = {
            "captured_at": "2026-06-01T13:00:00+09:00",
            "five_hour_limit": "46%",
            "weekly_limit": "80%",
            "five_hour_limit_reset_at": "2026-06-01T15:00:00+09:00",
            "weekly_limit_reset_at": "2026-06-02T13:00:00+09:00",
        }
        runtime["accounts"][0]["last_snapshot"].update(
            snapshot
        )
        runtime["accounts"][0]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "five_hour_limit": "1%",
                "weekly_limit": "1%",
            }
        ]
        runtime["accounts"][1]["last_snapshot"].update(
            snapshot
        )
        runtime["accounts"][1]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "five_hour_limit": "99%",
                "weekly_limit": "99%",
            }
        ]

        first_model = build_codex_usage_taskbar_overlay_model(
            runtime,
            now=datetime(2026, 6, 1, 13, 10, tzinfo=timezone(timedelta(hours=9))),
        )
        second_model = build_codex_usage_taskbar_overlay_model(
            runtime,
            now=datetime(2026, 6, 1, 14, 10, tzinfo=timezone(timedelta(hours=9))),
        )

        for model in (first_model, second_model):
            first_metrics = model["bars"][0]["metrics"]
            second_metrics = model["bars"][1]["metrics"]
            self.assertEqual(
                [metric["reset_direction"] for metric in first_metrics],
                ["on_track", "surplus"],
            )
            self.assertEqual(
                [metric["reset_badge_label"] for metric in first_metrics],
                ["정상", "남음"],
            )
            self.assertEqual(
                [metric["reset_badge_short_label"] for metric in first_metrics],
                ["정", "남"],
            )
            self.assertEqual(
                [metric["reset_direction"] for metric in second_metrics],
                ["on_track", "surplus"],
            )
            self.assertEqual(
                [metric["reset_badge_label"] for metric in second_metrics],
                ["정상", "남음"],
            )
            self.assertEqual(
                [metric["reset_badge_short_label"] for metric in second_metrics],
                ["정", "남"],
            )

    def test_model_marks_fast_early_weekly_burn_as_shortage(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 10, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T10:10:00+09:00",
                "weekly_limit": "97%",
                "weekly_limit_reset_at": "2026-06-08T06:10:00+09:00",
            }
        )
        runtime["accounts"][0]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "weekly_limit": "97%",
                "weekly_limit_reset_at": "2026-06-08T06:10:00+09:00",
            }
        ]

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        weekly_metric = model["bars"][0]["metrics"][1]
        self.assertEqual(weekly_metric["percent"], 97)
        self.assertEqual(weekly_metric["state"], "normal")
        self.assertEqual(weekly_metric["color"], "#22c55e")
        self.assertEqual(weekly_metric["reset_text"], "6d 20h 00m")
        self.assertEqual(weekly_metric["reset_direction"], "shortage")
        self.assertEqual(weekly_metric["reset_marker"], "↓")
        self.assertEqual(weekly_metric["reset_state"], "urgent")
        self.assertEqual(weekly_metric["reset_color"], "#ef4444")

    def test_model_marks_snapshot_projection_below_zero_as_shortage(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 10, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T11:00:00+09:00",
                "five_hour_limit": "20%",
                "five_hour_limit_reset_at": "2026-06-01T15:00:00+09:00",
            }
        )
        runtime["accounts"][0]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "five_hour_limit": "90%",
                "five_hour_limit_reset_at": "2026-06-01T10:30:00+09:00",
            }
        ]

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        five_hour_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(five_hour_metric["reset_direction"], "shortage")
        self.assertEqual(five_hour_metric["reset_marker"], "↓")
        self.assertEqual(five_hour_metric["reset_state"], "urgent")
        self.assertEqual(five_hour_metric["reset_color"], "#ef4444")

    def test_model_maps_snapshot_projection_thresholds(self):
        cases = [
            ("39%", "shortage", "부족"),
            ("40%", "on_track", "정상"),
            ("46%", "on_track", "정상"),
            ("47%", "surplus", "남음"),
        ]
        for percent, expected_direction, expected_badge in cases:
            with self.subTest(percent=percent):
                runtime = self._runtime()
                runtime["accounts"][0]["last_snapshot"].update(
                    {
                        "captured_at": "2026-06-01T13:00:00+09:00",
                        "five_hour_limit": percent,
                        "five_hour_limit_reset_at": "2026-06-01T15:00:00+09:00",
                    }
                )

                model = build_codex_usage_taskbar_overlay_model(runtime)

                five_hour_metric = model["bars"][0]["metrics"][0]
                self.assertEqual(five_hour_metric["reset_direction"], expected_direction)
                self.assertEqual(five_hour_metric["reset_badge_label"], expected_badge)

    def test_model_keeps_snapshot_surplus_despite_noisy_history(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 10, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T10:10:00+09:00",
                "weekly_limit": "80%",
                "weekly_limit_reset_at": "2026-06-01T11:10:00+09:00",
            }
        )
        runtime["accounts"][0]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "weekly_limit": "100%",
                "weekly_limit_reset_at": "2026-06-01T11:10:00+09:00",
            }
        ]

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        weekly_metric = model["bars"][0]["metrics"][1]
        self.assertEqual(weekly_metric["reset_direction"], "surplus")
        self.assertEqual(weekly_metric["reset_marker"], "↑")
        self.assertEqual(weekly_metric["reset_state"], "warning")
        self.assertEqual(weekly_metric["reset_color"], "#f59e0b")

    def test_model_marks_static_low_remaining_far_reset_as_shortage(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "weekly_limit": "18%",
                "weekly_limit_reset_at": "2026-06-07T10:00:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        weekly_metric = model["bars"][0]["metrics"][1]
        self.assertEqual(weekly_metric["reset_direction"], "shortage")
        self.assertEqual(weekly_metric["reset_marker"], "↓")
        self.assertEqual(weekly_metric["reset_state"], "urgent")
        self.assertEqual(weekly_metric["reset_color"], "#ef4444")

    def test_model_exposes_unknown_direction_without_reset_time(self):
        runtime = self._runtime()
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T10:10:00+09:00",
                "five_hour_limit": "80%",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime)

        five_hour_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(five_hour_metric["reset_direction"], "unknown")
        self.assertEqual(five_hour_metric["reset_marker"], "")
        self.assertEqual(five_hour_metric["reset_state"], "unknown")
        self.assertEqual(five_hour_metric["reset_badge_label"], "")
        self.assertEqual(five_hour_metric["reset_badge_short_label"], "")
        self.assertEqual(five_hour_metric["reset_badge_fill"], "")
        self.assertEqual(five_hour_metric["reset_badge_outline"], "")
        self.assertEqual(five_hour_metric["reset_badge_text_color"], "")

    def test_model_keeps_snapshot_projection_when_history_is_missing_or_conflicting(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 10, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T10:10:00+09:00",
                "five_hour_limit": "50%",
                "five_hour_limit_reset_at": "2026-06-01T10:50:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(first_metric["state"], "warning")
        self.assertEqual(first_metric["color"], "#f59e0b")
        self.assertEqual(first_metric["reset_direction"], "surplus")

        runtime["accounts"][0]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "five_hour_limit": "40%",
                "five_hour_limit_reset_at": "2026-06-01T10:50:00+09:00",
            }
        ]

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(first_metric["state"], "warning")
        self.assertEqual(first_metric["color"], "#f59e0b")
        self.assertEqual(first_metric["reset_direction"], "surplus")

    def test_model_keeps_unknown_reset_direction_without_reset_time_even_with_history(self):
        runtime = self._runtime()
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T10:10:00+09:00",
                "five_hour_limit": "80%",
            }
        )
        runtime["accounts"][0]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "five_hour_limit": "82%",
            }
        ]

        model = build_codex_usage_taskbar_overlay_model(runtime)

        first_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(first_metric["state"], "normal")
        self.assertEqual(first_metric["color"], "#22c55e")
        self.assertEqual(first_metric["reset_state"], "unknown")
        self.assertEqual(first_metric["reset_direction"], "unknown")
        self.assertEqual(first_metric["reset_badge_label"], "")

    def test_model_ignores_history_when_projecting_from_snapshot(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 10, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T10:10:00+09:00",
                "five_hour_limit": "50%",
                "five_hour_limit_reset_at": "2026-06-01T10:50:00+09:00",
            }
        )
        runtime["accounts"][0]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "five_hour_limit": "60%",
            }
        ]

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(first_metric["state"], "warning")
        self.assertEqual(first_metric["color"], "#f59e0b")
        self.assertEqual(first_metric["reset_direction"], "surplus")

    def test_model_keeps_zero_remaining_urgent_before_reset(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 10, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-01T10:10:00+09:00",
                "five_hour_limit": "0%",
                "five_hour_limit_reset_at": "2026-06-01T10:50:00+09:00",
            }
        )
        runtime["accounts"][0]["usage_history"] = [
            {
                "captured_at": "2026-06-01T10:00:00+09:00",
                "five_hour_limit": "0%",
                "five_hour_limit_reset_at": "2026-06-01T10:50:00+09:00",
            }
        ]

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(first_metric["state"], "high")
        self.assertEqual(first_metric["color"], "#ef4444")
        self.assertEqual(first_metric["reset_state"], "urgent")

    def test_model_uses_separate_reset_windows_for_five_hour_and_weekly_limits(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "five_hour_limit": "64%",
                "weekly_limit": "64%",
                "five_hour_limit_reset_at": "2026-06-01T13:05:00+09:00",
                "weekly_limit_reset_at": "2026-06-01T13:05:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metrics = model["bars"][0]["metrics"]
        self.assertEqual(
            [metric["reset_text"] for metric in first_metrics],
            ["03h 05m", "0d 03h 05m"],
        )
        self.assertEqual(
            [metric["reset_short_text"] for metric in first_metrics],
            ["03h 05m", "0d 03h 05m"],
        )
        self.assertEqual(
            [metric["reset_color"] for metric in first_metrics],
            ["#22c55e", "#f59e0b"],
        )
        self.assertEqual(
            [metric["reset_state"] for metric in first_metrics],
            ["stable", "warning"],
        )

    def test_model_derives_elapsed_reset_badge_when_captured_at_missing(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"]["five_hour_limit_reset_at"] = (
            "2026-06-01T09:59:00+09:00"
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(first_metric["reset_text"], "00h 00m")
        self.assertEqual(first_metric["reset_color"], "#ef4444")
        self.assertEqual(first_metric["reset_direction"], "shortage")
        self.assertEqual(first_metric["reset_badge_label"], "부족")
        self.assertEqual(first_metric["reset_badge_short_label"], "부")

    def test_model_derives_elapsed_high_remaining_badge_when_captured_at_missing(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "five_hour_limit": "99%",
                "five_hour_limit_reset_at": "2026-06-01T09:59:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(first_metric["reset_text"], "00h 00m")
        self.assertEqual(first_metric["reset_direction"], "surplus")
        self.assertEqual(first_metric["reset_badge_label"], "남음")
        self.assertEqual(first_metric["reset_badge_short_label"], "남")

    def test_model_keeps_utc_reset_remaining_time_aligned_with_dashboard(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 12, 14, 10, 50, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-12 14:10:03",
                "five_hour_limit": "99%",
                "weekly_limit": "95%",
                "five_hour_limit_reset_at": "2026-06-12T09:49:00.000Z",
                "weekly_limit_reset_at": "2026-06-18T14:46:00.000Z",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metrics = model["bars"][0]["metrics"]
        self.assertEqual(
            [metric["reset_text"] for metric in first_metrics],
            ["04h 39m", "6d 09h 36m"],
        )
        self.assertEqual(
            [metric["reset_direction"] for metric in first_metrics],
            ["surplus", "surplus"],
        )

    def test_model_treats_naive_overlay_now_as_local_for_utc_reset_time(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 12, 14, 10, 50)
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "captured_at": "2026-06-12 14:10:03",
                "five_hour_limit": "99%",
                "weekly_limit": "95%",
                "five_hour_limit_reset_at": "2026-06-12T09:49:00.000Z",
                "weekly_limit_reset_at": "2026-06-18T14:46:00.000Z",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metrics = model["bars"][0]["metrics"]
        self.assertEqual(
            [metric["reset_text"] for metric in first_metrics],
            ["04h 39m", "6d 09h 36m"],
        )
        self.assertEqual(
            [metric["reset_direction"] for metric in first_metrics],
            ["surplus", "surplus"],
        )

    def test_model_derives_elapsed_weekly_reset_badge(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "weekly_limit": "99%",
                "weekly_limit_reset_at": "2026-06-01T09:59:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        weekly_metric = model["bars"][0]["metrics"][1]
        self.assertEqual(weekly_metric["reset_text"], "0d 00h 00m")
        self.assertEqual(weekly_metric["reset_direction"], "surplus")
        self.assertEqual(weekly_metric["reset_badge_label"], "남음")
        self.assertEqual(weekly_metric["reset_badge_short_label"], "남")

    def test_draw_keeps_status_close_and_metric_groups_separated(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        canvas = _FakeCanvas()
        overlay._canvas = canvas
        runtime = self._runtime()
        runtime["accounts"][0]["runtime"]["collect_inflight"] = True
        model = build_codex_usage_taskbar_overlay_model(
            runtime,
            geometry={
                "x": 1960,
                "y": 1397,
                "width": 376,
                "height": 38,
                "orientation": "bottom",
                "visible": True,
            },
        )

        overlay._draw(model)

        label_texts = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "Codex 1"
        ]
        status_texts = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "SYNC"
        ]
        first_metric_labels = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "5h"
        ]
        self.assertTrue(label_texts)
        self.assertEqual(status_texts, [])
        self.assertTrue(first_metric_labels)
        self.assertGreaterEqual(first_metric_labels[0][1][0] - label_texts[0][1][0], 56)

        track_rects = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#2a2f38"
        ]
        value_texts = [
            op
            for op in canvas.ops
            if op[0] == "text" and op[2].get("text") in {"47%", "52%", "82%", "76%"}
        ]
        first_weekly_labels = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "7d"
        ]
        self.assertGreaterEqual(len(track_rects), 4)
        self.assertGreaterEqual(len(value_texts), 4)
        self.assertGreaterEqual(len(first_weekly_labels), 2)
        track_widths = [int(track[1][2]) - int(track[1][0]) for track in track_rects[:4]]
        self.assertEqual(track_widths, [track_widths[0]] * 4)
        for track, value in zip(track_rects[:4], value_texts[:4], strict=True):
            self.assertGreaterEqual(value[1][0] - track[1][2], 8)
            self.assertLessEqual(value[1][0] - track[1][2], 38)
        self.assertGreaterEqual(first_weekly_labels[0][1][0] - value_texts[0][1][0], 18)

        weekly_reset_texts = [
            op
            for op in canvas.ops
            if op[0] == "text" and str(op[2].get("text") or "").startswith("0d ")
        ]
        if weekly_reset_texts:
            right_edge_estimate = weekly_reset_texts[-1][1][0] + 52
            self.assertLessEqual(abs((376 - right_edge_estimate) - 6), 8)

    def test_draw_keeps_profile_name_status_dot_and_metric_label_separated(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        canvas = _FakeCanvas()
        overlay._canvas = canvas
        runtime = self._runtime()
        runtime["accounts"][0]["label"] = "Kim Jong"
        runtime["accounts"][1]["label"] = "이니미니"
        model = build_codex_usage_taskbar_overlay_model(
            runtime,
            geometry={
                "x": 1832,
                "y": 1397,
                "width": 376,
                "height": 38,
                "orientation": "bottom",
                "visible": True,
            },
        )

        overlay._draw(model)

        profile_label = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "Kim Jong"
        ][0]
        status_dot = [
            op for op in canvas.ops if op[0] == "oval" and op[2].get("fill") == "#22c55e"
        ][0]
        metric_label = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "5h"
        ][0]

        self.assertGreaterEqual(status_dot[1][0] - profile_label[1][0], 64)
        self.assertGreaterEqual(metric_label[1][0] - status_dot[1][2], 10)

    def test_draw_keeps_metric_columns_clear_when_preferred_cap_shows_status_text(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        canvas = _FakeCanvas()
        overlay._canvas = canvas
        model = build_codex_usage_taskbar_overlay_model(
            self._runtime(),
            geometry={
                "x": 1800,
                "y": 1397,
                "width": 420,
                "height": 38,
                "orientation": "bottom",
                "visible": True,
            },
        )

        overlay._draw(model)

        status_texts = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "OK"
        ]
        metric_labels = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "5h"
        ]
        track_rects = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#2a2f38"
        ]
        self.assertGreaterEqual(len(status_texts), 2)
        self.assertGreaterEqual(len(metric_labels), 2)
        self.assertGreaterEqual(len(track_rects), 4)
        self.assertLess(status_texts[0][1][0], metric_labels[0][1][0] - 8)
        track_widths = [int(track[1][2]) - int(track[1][0]) for track in track_rects[:4]]
        self.assertEqual(track_widths, [track_widths[0]] * 4)

    def test_draw_compact_slot_keeps_progress_and_percent_inside_overlay(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        canvas = _FakeCanvas()
        overlay._canvas = canvas
        runtime = self._runtime()
        runtime["accounts"][0]["label"] = "Kim Jong"
        runtime["accounts"][1]["label"] = "이니미니"
        runtime["accounts"][0]["last_snapshot"]["five_hour_limit_reset_at"] = (
            "2026-06-01T11:35:00+09:00"
        )
        model = build_codex_usage_taskbar_overlay_model(
            runtime,
            geometry={
                "x": 780,
                "y": 1040,
                "width": 176,
                "height": 38,
                "orientation": "bottom",
                "visible": True,
            },
        )

        overlay._draw(model)

        status_texts = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") in {"OK", "OUT"}
        ]
        track_rects = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#2a2f38"
        ]
        value_texts = [
            op
            for op in canvas.ops
            if op[0] == "text" and op[2].get("text") in {"47%", "52%", "82%", "76%"}
        ]

        self.assertEqual(status_texts, [])
        self.assertGreaterEqual(len(track_rects), 4)
        self.assertGreaterEqual(len(value_texts), 4)
        for rect in track_rects[:4]:
            self.assertLessEqual(int(rect[1][2]), 176)
            self.assertGreaterEqual(int(rect[1][2]) - int(rect[1][0]), 6)
        for text in value_texts[:4]:
            self.assertLessEqual(int(text[1][0]), 176)

    def test_reset_badge_fit_prefers_reset_time_over_badge(self):
        badge_width = taskbar_overlay._reset_badge_width_for_label("부족")
        short_badge_width = taskbar_overlay._reset_badge_width_for_label("부")
        detail_width = taskbar_overlay._reset_column_width_for_text(
            "3h 12m detail",
            metric_key="five_hour_limit",
        )
        short_width = taskbar_overlay._reset_column_width_for_text(
            "3h",
            metric_key="five_hour_limit",
        )
        gap = taskbar_overlay._RESET_BADGE_TIME_GAP_PX

        detail_fit = taskbar_overlay._fit_reset_badge_for_space(
            "3h 12m detail",
            "3h",
            badge_label="부족",
            badge_short_label="부",
            metric_key="five_hour_limit",
            available_px=badge_width + gap + detail_width,
        )
        short_fit = taskbar_overlay._fit_reset_badge_for_space(
            "3h 12m detail",
            "3h",
            badge_label="부족",
            badge_short_label="부",
            metric_key="five_hour_limit",
            available_px=badge_width + gap + short_width,
        )
        time_only_fit = taskbar_overlay._fit_reset_badge_for_space(
            "3h 12m detail",
            "3h",
            badge_label="부족",
            badge_short_label="부",
            metric_key="five_hour_limit",
            available_px=short_width,
        )
        short_badge_fit = taskbar_overlay._fit_reset_badge_for_space(
            "3h 12m detail",
            "3h",
            badge_label="부족",
            badge_short_label="부",
            metric_key="five_hour_limit",
            available_px=short_badge_width,
        )
        hidden_fit = taskbar_overlay._fit_reset_badge_for_space(
            "3h 12m detail",
            "3h",
            badge_label="부족",
            badge_short_label="부",
            metric_key="five_hour_limit",
            available_px=short_badge_width - 1,
        )

        self.assertEqual(detail_fit["variant"], "badge_detail")
        self.assertEqual(detail_fit["badge_label"], "부족")
        self.assertEqual(detail_fit["time_text"], "3h 12m detail")
        self.assertEqual(short_fit["variant"], "badge_short")
        self.assertEqual(short_fit["time_text"], "3h")
        self.assertEqual(time_only_fit["variant"], "time_short")
        self.assertFalse(time_only_fit["badge_visible"])
        self.assertEqual(time_only_fit["time_text"], "3h")
        self.assertEqual(short_badge_fit["variant"], "badge_short_only")
        self.assertEqual(short_badge_fit["badge_label"], "부")
        self.assertEqual(hidden_fit["variant"], "hidden")
        self.assertFalse(hidden_fit["badge_visible"])

    def test_metric_segment_layout_shrinks_progress_before_dropping_badge_time(self):
        layout = taskbar_overlay._fit_metric_segment_layout(
            158,
            "6d 20h 00m",
            "6d 20h",
            badge_label="남음",
            badge_short_label="남",
            metric_key="weekly_limit",
            reset_marker="↑",
            has_reset_badge=True,
            progress_width=taskbar_overlay._METRIC_PROGRESS_PREFERRED_WIDTH_PX,
        )

        self.assertLess(
            layout["progress_width"],
            taskbar_overlay._METRIC_PROGRESS_PREFERRED_WIDTH_PX,
        )
        self.assertGreaterEqual(
            layout["progress_width"],
            taskbar_overlay._METRIC_PROGRESS_MIN_WIDTH_PX,
        )
        self.assertTrue(layout["badge_fit"]["badge_visible"])
        self.assertEqual(layout["badge_fit"]["badge_label"], "남")
        self.assertIn(layout["badge_fit"]["time_text"], {"6d 20h 00m", "6d 20h"})

    def test_metric_segment_layout_fits_zero_time_risk_badge_in_compact_five_hour_segment(self):
        layout = taskbar_overlay._fit_metric_segment_layout(
            145,
            "00h 00m",
            "00h 00m",
            badge_label="부족",
            badge_short_label="부",
            metric_key="five_hour_limit",
            reset_marker="↓",
            has_reset_badge=True,
            progress_width=taskbar_overlay._METRIC_PROGRESS_PREFERRED_WIDTH_PX,
        )

        self.assertLess(
            layout["progress_width"],
            taskbar_overlay._METRIC_PROGRESS_PREFERRED_WIDTH_PX,
        )
        self.assertGreaterEqual(
            layout["progress_width"],
            taskbar_overlay._METRIC_PROGRESS_MIN_WIDTH_PX,
        )
        self.assertTrue(layout["badge_fit"]["badge_visible"])
        self.assertEqual(layout["badge_fit"]["badge_label"], "부")
        self.assertEqual(layout["badge_fit"]["time_text"], "00h 00m")
        self.assertIn(
            layout["badge_fit"]["variant"],
            {"badge_short_detail", "badge_short_time"},
        )

    def test_draw_metric_segment_draws_reset_badge_and_time_without_overlap(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "5h",
            "percent": 73,
            "value_text": "73%",
            "color": "#22c55e",
            "reset_text": "3h 12m",
            "reset_short_text": "3h",
            "reset_color": "#22c55e",
            "reset_marker": "↓",
            "reset_badge_label": "부족",
            "reset_badge_short_label": "부",
            "reset_badge_fill": "#7f1d1d",
            "reset_badge_outline": "#ef4444",
            "reset_badge_text_color": "#fee2e2",
        }
        wide_canvas = _FakeCanvas()
        overlay._draw_metric_segment(wide_canvas, metric, 10, 2, 220, 15)

        wide_value = [
            op for op in wide_canvas.ops if op[0] == "text" and op[2].get("text") == "73%"
        ][0]
        track_rect = [
            op
            for op in wide_canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#2a2f38"
        ][0]
        badge_rect = [
            op
            for op in wide_canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#7f1d1d"
        ][0]
        badge_label = [
            op for op in wide_canvas.ops if op[0] == "text" and op[2].get("text") == "부족"
        ][0]
        reset_time = [
            op for op in wide_canvas.ops if op[0] == "text" and op[2].get("text") == "3h 12m"
        ][0]
        arrow_only = [
            op for op in wide_canvas.ops if op[0] == "text" and op[2].get("text") == "↓"
        ]

        self.assertGreater(badge_rect[1][0], wide_value[1][0])
        self.assertGreater(badge_rect[1][0], track_rect[1][2])
        self.assertGreater(reset_time[1][0], badge_rect[1][2])
        self.assertGreaterEqual(badge_label[1][0], badge_rect[1][0])
        self.assertLessEqual(badge_label[1][0], badge_rect[1][2])
        self.assertEqual(badge_label[2].get("fill"), "#fee2e2")
        self.assertEqual(badge_rect[2].get("outline"), "#ef4444")
        self.assertEqual(arrow_only, [])

    def test_draw_metric_segment_draws_zero_time_badge_with_small_gap_in_compact_width(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "5h",
            "metric_key": "five_hour_limit",
            "percent": 47,
            "value_text": "47%",
            "color": "#22c55e",
            "reset_text": "00h 00m",
            "reset_short_text": "00h 00m",
            "reset_color": "#ef4444",
            "reset_marker": "↓",
            "reset_badge_label": "부족",
            "reset_badge_short_label": "부",
            "reset_badge_fill": "#7f1d1d",
            "reset_badge_outline": "#ef4444",
            "reset_badge_text_color": "#fee2e2",
        }
        canvas = _FakeCanvas()

        overlay._draw_metric_segment(canvas, metric, 10, 2, 145, 15)

        badge_rects = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#7f1d1d"
        ]
        badge_labels = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "부"
        ]
        reset_times = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "00h 00m"
        ]
        arrow_only = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "↓"
        ]

        self.assertEqual(len(badge_rects), 1)
        self.assertEqual(len(badge_labels), 1)
        self.assertEqual(len(reset_times), 1)
        gap = int(reset_times[0][1][0]) - int(badge_rects[0][1][2])
        self.assertEqual(gap, 5)
        self.assertEqual(arrow_only, [])

    def test_fit_reset_badge_uses_five_pixel_gap_in_total_width(self):
        fit = taskbar_overlay._fit_reset_badge_for_space(
            "00h 00m",
            "00h 00m",
            badge_label="부족",
            badge_short_label="부",
            metric_key="five_hour_limit",
            available_px=200,
        )
        expected_total_width = (
            taskbar_overlay._reset_badge_width_for_label("부족")
            + 5
            + taskbar_overlay._reset_column_width_for_text(
                "00h 00m",
                metric_key="five_hour_limit",
            )
        )

        self.assertEqual(taskbar_overlay._RESET_BADGE_TIME_GAP_PX, 5)
        self.assertEqual(fit["variant"], "badge_detail")
        self.assertEqual(fit["total_width"], expected_total_width)

    def test_draw_metric_segment_keeps_reset_time_before_badge_when_space_is_narrow(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "7d",
            "percent": 97,
            "value_text": "97%",
            "color": "#22c55e",
            "reset_text": "6d 20h 00m",
            "reset_short_text": "6d 20h",
            "reset_color": "#f59e0b",
            "reset_marker": "↑",
            "reset_badge_label": "남음",
            "reset_badge_short_label": "남",
            "reset_badge_fill": "#78350f",
            "reset_badge_outline": "#f59e0b",
            "reset_badge_text_color": "#fef3c7",
        }
        narrow_canvas = _FakeCanvas()

        overlay._draw_metric_segment(narrow_canvas, metric, 10, 2, 134, 15)

        badge_rects = [
            op
            for op in narrow_canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#78350f"
        ]
        badge_labels = [
            op
            for op in narrow_canvas.ops
            if op[0] == "text" and op[2].get("text") in {"남음", "남"}
        ]
        reset_times = [
            op
            for op in narrow_canvas.ops
            if op[0] == "text" and op[2].get("text") in {"6d 20h 00m", "6d 20h"}
        ]
        arrow_only = [
            op for op in narrow_canvas.ops if op[0] == "text" and op[2].get("text") == "↑"
        ]
        track_rect = [
            op
            for op in narrow_canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#2a2f38"
        ][0]

        self.assertEqual(badge_rects, [])
        self.assertEqual(badge_labels, [])
        self.assertEqual(len(reset_times), 1)
        self.assertEqual(arrow_only, [])
        track_width = int(track_rect[1][2]) - int(track_rect[1][0])
        self.assertLess(track_width, taskbar_overlay._METRIC_PROGRESS_PREFERRED_WIDTH_PX)
        self.assertGreaterEqual(track_width, taskbar_overlay._METRIC_PROGRESS_MIN_WIDTH_PX)

    def test_draw_metric_segment_shrinks_progress_to_keep_badge_with_reset_time(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "7d",
            "percent": 97,
            "value_text": "97%",
            "color": "#22c55e",
            "reset_text": "6d 20h 00m",
            "reset_short_text": "6d 20h",
            "reset_color": "#f59e0b",
            "reset_marker": "↑",
            "reset_badge_label": "남음",
            "reset_badge_short_label": "남",
            "reset_badge_fill": "#78350f",
            "reset_badge_outline": "#f59e0b",
            "reset_badge_text_color": "#fef3c7",
        }
        canvas = _FakeCanvas()

        overlay._draw_metric_segment(canvas, metric, 10, 2, 158, 15)

        track_rect = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#2a2f38"
        ][0]
        badge_labels = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "남"
        ]
        reset_times = [
            op
            for op in canvas.ops
            if op[0] == "text" and op[2].get("text") in {"6d 20h 00m", "6d 20h"}
        ]
        arrow_only = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "↑"
        ]

        self.assertEqual(len(badge_labels), 1)
        self.assertEqual(len(reset_times), 1)
        track_width = int(track_rect[1][2]) - int(track_rect[1][0])
        self.assertLess(track_width, taskbar_overlay._METRIC_PROGRESS_PREFERRED_WIDTH_PX)
        self.assertGreaterEqual(track_width, taskbar_overlay._METRIC_PROGRESS_MIN_WIDTH_PX)
        self.assertGreater(reset_times[0][1][0], badge_labels[0][1][0])
        self.assertEqual(arrow_only, [])

    def test_draw_metric_segment_hides_known_direction_marker_when_badge_metadata_is_absent(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "7d",
            "percent": 97,
            "value_text": "97%",
            "color": "#22c55e",
            "reset_text": "6d 20h 00m",
            "reset_short_text": "6d 20h",
            "reset_color": "#f59e0b",
            "reset_marker": "↑",
        }
        tiny_canvas = _FakeCanvas()

        overlay._draw_metric_segment(tiny_canvas, metric, 10, 2, 91, 15)

        arrow_only = [
            op for op in tiny_canvas.ops if op[0] == "text" and op[2].get("text") == "↑"
        ]

        self.assertEqual(arrow_only, [])

    def test_draw_metric_segment_keeps_reset_time_with_short_badge_label_only(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "7d",
            "percent": 97,
            "value_text": "97%",
            "color": "#22c55e",
            "reset_text": "6d 20h 00m",
            "reset_short_text": "6d 20h",
            "reset_color": "#f59e0b",
            "reset_marker": "↑",
            "reset_badge_short_label": "남",
            "reset_badge_fill": "#78350f",
            "reset_badge_outline": "#f59e0b",
            "reset_badge_text_color": "#fef3c7",
        }
        canvas = _FakeCanvas()

        overlay._draw_metric_segment(canvas, metric, 10, 2, 220, 15)

        reset_times = [
            op
            for op in canvas.ops
            if op[0] == "text" and op[2].get("text") in {"6d 20h 00m", "6d 20h"}
        ]
        badge_labels = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "남"
        ]
        arrow_only = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "↑"
        ]

        self.assertEqual(len(reset_times), 1)
        self.assertEqual(len(badge_labels), 1)
        self.assertEqual(arrow_only, [])

    def test_draw_metric_segment_keeps_reset_time_with_badge_style_only(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "7d",
            "percent": 97,
            "value_text": "97%",
            "color": "#22c55e",
            "reset_text": "6d 20h 00m",
            "reset_short_text": "6d 20h",
            "reset_color": "#f59e0b",
            "reset_marker": "↑",
            "reset_badge_fill": "#78350f",
            "reset_badge_outline": "#f59e0b",
            "reset_badge_text_color": "#fef3c7",
        }
        canvas = _FakeCanvas()

        overlay._draw_metric_segment(canvas, metric, 10, 2, 220, 15)

        reset_times = [
            op
            for op in canvas.ops
            if op[0] == "text" and op[2].get("text") in {"6d 20h 00m", "6d 20h"}
        ]
        badge_rects = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#78350f"
        ]
        arrow_only = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "↑"
        ]

        self.assertEqual(len(reset_times), 1)
        self.assertEqual(badge_rects, [])
        self.assertEqual(arrow_only, [])

    def test_draw_metric_segment_hides_known_direction_instead_of_arrow_only(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "7d",
            "percent": 97,
            "value_text": "97%",
            "color": "#22c55e",
            "reset_text": "6d 20h 00m",
            "reset_short_text": "6d 20h",
            "reset_color": "#f59e0b",
            "reset_marker": "↑",
            "reset_badge_label": "남음",
            "reset_badge_short_label": "남",
            "reset_badge_fill": "#78350f",
            "reset_badge_outline": "#f59e0b",
            "reset_badge_text_color": "#fef3c7",
        }
        tiny_canvas = _FakeCanvas()

        overlay._draw_metric_segment(tiny_canvas, metric, 10, 2, 91, 15)

        badge_labels = [
            op
            for op in tiny_canvas.ops
            if op[0] == "text" and op[2].get("text") in {"남음", "남"}
        ]
        arrow_only = [
            op for op in tiny_canvas.ops if op[0] == "text" and op[2].get("text") == "↑"
        ]
        reset_times = [
            op
            for op in tiny_canvas.ops
            if op[0] == "text" and op[2].get("text") in {"6d 20h 00m", "6d 20h"}
        ]

        self.assertEqual(badge_labels, [])
        self.assertEqual(arrow_only, [])
        self.assertEqual(reset_times, [])

    def test_draw_metric_segment_uses_default_badge_style_when_label_is_visible(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "7d",
            "percent": 50,
            "value_text": "50%",
            "color": "#f59e0b",
            "reset_text": "1d 00h 00m",
            "reset_short_text": "1d",
            "reset_color": "#f59e0b",
            "reset_badge_label": "남음",
            "reset_badge_short_label": "남",
        }
        canvas = _FakeCanvas()

        overlay._draw_metric_segment(canvas, metric, 10, 2, 220, 15)

        badge_rects = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#374151"
        ]
        badge_labels = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "남음"
        ]
        self.assertEqual(len(badge_rects), 1)
        self.assertEqual(badge_rects[0][2].get("outline"), "#f59e0b")
        self.assertEqual(len(badge_labels), 1)

    def test_draw_metric_segment_keeps_percent_and_reset_text_outside_track(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        canvas = _FakeCanvas()
        metric = {
            "key": "7d",
            "percent": 100,
            "value_text": "100%",
            "color": "#22c55e",
            "reset_text": "4d 03h 49m",
            "reset_short_text": "4d 03h 49m",
            "reset_color": "#94a3b8",
        }

        overlay._draw_metric_segment(canvas, metric, 20, 2, 186, 15)

        track_rect = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#2a2f38"
        ][0]
        value_text = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "100%"
        ][0]
        reset_text = [
            op for op in canvas.ops if op[0] == "text" and op[2].get("text") == "4d 03h 49m"
        ][0]

        self.assertGreaterEqual(value_text[1][0] - track_rect[1][2], 30)
        self.assertEqual(value_text[2].get("anchor"), "e")
        self.assertGreaterEqual(reset_text[1][0] - value_text[1][0], 4)
        self.assertLessEqual(reset_text[1][0] + 66, 20 + 186)

    def test_model_preserves_minutes_in_day_scale_reset_text(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "weekly_limit": "92%",
                "weekly_limit_reset_at": "2026-06-07T16:07:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        weekly_metric = model["bars"][0]["metrics"][1]
        self.assertEqual(weekly_metric["reset_text"], "6d 06h 07m")
        self.assertEqual(weekly_metric["reset_short_text"], "6d 06h 07m")

    def test_model_hides_implausible_day_scale_five_hour_reset_time(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"].update(
            {
                "five_hour_limit": "100%",
                "weekly_limit": "0%",
                "five_hour_limit_reset_at": "2026-06-05T13:50:00+09:00",
                "weekly_limit_reset_at": "2026-06-05T13:50:00+09:00",
            }
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        five_hour_metric = model["bars"][0]["metrics"][0]
        weekly_metric = model["bars"][0]["metrics"][1]
        self.assertEqual(five_hour_metric["reset_text"], "")
        self.assertEqual(five_hour_metric["reset_short_text"], "")
        self.assertEqual(weekly_metric["reset_text"], "4d 03h 50m")
        self.assertEqual(weekly_metric["reset_short_text"], "4d 03h 50m")

    def test_draw_metric_segment_keeps_value_and_track_columns_stable_for_reset_widths(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        canvas = _FakeCanvas()
        first_metric = {
            "key": "5h",
            "percent": 6,
            "value_text": "6%",
            "color": "#ef4444",
            "reset_text": "5m",
            "reset_short_text": "5m",
            "reset_color": "#22c55e",
        }
        second_metric = {
            "key": "5h",
            "percent": 100,
            "value_text": "100%",
            "color": "#22c55e",
            "reset_text": "00h 00m",
            "reset_short_text": "00h 00m",
            "reset_color": "#ef4444",
        }

        overlay._draw_metric_segment(canvas, first_metric, 10, 2, 174, 15)
        overlay._draw_metric_segment(canvas, second_metric, 10, 20, 174, 15)

        track_rects = [
            op
            for op in canvas.ops
            if op[0] == "rectangle" and op[2].get("fill") == "#2a2f38"
        ]
        value_texts = [
            op
            for op in canvas.ops
            if op[0] == "text" and op[2].get("text") in {"6%", "100%"}
        ]
        reset_texts = [
            op
            for op in canvas.ops
            if op[0] == "text" and op[2].get("text") in {"5m", "00h 00m"}
        ]
        self.assertEqual(track_rects[0][1][2], track_rects[1][1][2])
        self.assertEqual(value_texts[0][1][0], value_texts[1][1][0])
        self.assertEqual(reset_texts[0][1][0], reset_texts[1][1][0])

    def test_model_displays_unknown_usage_without_hiding_the_overlay(self):
        runtime = self._runtime()
        runtime["accounts"][0]["last_snapshot"]["five_hour_limit"] = ""
        runtime["accounts"][0]["last_snapshot"]["weekly_limit"] = ""

        model = build_codex_usage_taskbar_overlay_model(runtime)

        self.assertTrue(model["visible"])
        self.assertEqual(model["bars"][0]["percent"], 0)
        self.assertEqual(model["bars"][0]["value_text"], "--")
        self.assertEqual(model["bars"][0]["state"], "nodata")
        self.assertEqual(model["bars"][0]["status_text"], "DATA")
        self.assertEqual(
            [metric["value_text"] for metric in model["bars"][0]["metrics"]],
            ["--", "--"],
        )

    def test_model_marks_logged_out_account_with_login_status(self):
        runtime = self._runtime()
        runtime["accounts"][1]["runtime"]["session_state"] = "logged_out"

        model = build_codex_usage_taskbar_overlay_model(runtime)

        self.assertEqual(model["bars"][1]["state"], "login")
        self.assertEqual(model["bars"][1]["status_text"], "OUT")
        self.assertEqual(model["bars"][1]["status_color"], "#f59e0b")
        self.assertEqual(
            [metric["color"] for metric in model["bars"][1]["metrics"]],
            ["#6b7280", "#6b7280"],
        )

    def test_model_parses_ratio_usage_values_for_taskbar_progress(self):
        runtime = self._runtime()
        runtime["accounts"][0]["last_snapshot"]["five_hour_limit"] = "17 / 40"
        runtime["accounts"][0]["last_snapshot"]["weekly_limit"] = "109 / 300"

        model = build_codex_usage_taskbar_overlay_model(runtime)

        self.assertEqual(
            [metric["percent"] for metric in model["bars"][0]["metrics"]],
            [42, 36],
        )

    def test_window_handle_normalizes_tk_child_to_top_level_root(self):
        class FakeWindow:
            def winfo_id(self):
                return 222

        class FakeUser32:
            def GetAncestor(self, hwnd, flag):
                self.last_call = (int(hwnd), int(flag))
                return 111

        fake_user32 = FakeUser32()

        class FakeWindll:
            user32 = fake_user32

        with patch.object(taskbar_overlay.ctypes, "windll", FakeWindll()):
            self.assertEqual(_get_window_handle(FakeWindow()), 111)
            self.assertEqual(fake_user32.last_call, (222, 2))

    def test_prepare_native_window_binds_overlay_owner_to_shell_taskbar(self):
        class FakeWindow:
            def winfo_id(self):
                return 222

        class FakeUser32:
            def __init__(self):
                self.owner_calls = []

            def GetAncestor(self, hwnd, flag):
                self.last_ancestor_call = (int(hwnd), int(flag))
                return 111

            def SetWindowLongPtrW(self, hwnd, index, value):
                self.owner_calls.append((int(hwnd), int(index), int(value)))
                return 0

        fake_user32 = FakeUser32()

        class FakeWindll:
            user32 = fake_user32

        class FakeWin32Gui:
            def __init__(self):
                self.style_calls = []
                self.find_calls = []

            def GetWindowLong(self, hwnd, index):
                self.style_calls.append(("get", int(hwnd), int(index)))
                return 0

            def SetWindowLong(self, hwnd, index, value):
                self.style_calls.append(("set", int(hwnd), int(index), int(value)))
                return 0

            def FindWindow(self, class_name, title):
                self.find_calls.append((str(class_name), title))
                return 555

        fake_win32gui = FakeWin32Gui()
        overlay = CodexUsageTaskbarOverlay(
            _FakeRoot(),
            self._runtime,
            window_factory=lambda _root: FakeWindow(),
        )

        with patch.object(taskbar_overlay, "win32gui", fake_win32gui):
            with patch.object(taskbar_overlay.ctypes, "windll", FakeWindll()):
                overlay._prepare_native_window(FakeWindow())

        self.assertEqual(fake_win32gui.find_calls, [("Shell_TrayWnd", None)])
        self.assertEqual(fake_user32.owner_calls, [(111, -8, 555)])

    def test_refresh_updates_changed_metric_with_flash_timer(self):
        root = _FakeRoot()
        window = _FakeWindow()
        runtime = self._runtime()
        overlay = CodexUsageTaskbarOverlay(
            root,
            lambda: runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: None,
        )

        overlay.refresh()
        runtime["accounts"][0]["last_snapshot"]["weekly_limit"] = "63%"
        overlay.refresh()

        changed_metrics = window.draw_calls[-1]["bars"][0]["metrics"]
        self.assertFalse(changed_metrics[0]["flash"])
        self.assertTrue(changed_metrics[1]["flash"])
        self.assertTrue(root.after_calls)
        self.assertTrue(
            any(call[1].__name__ == "_flash_tick" for call in root.after_calls)
        )

    def test_refresh_marks_changed_metric_with_static_highlight_phase(self):
        root = _FakeRoot()
        window = _FakeWindow()
        runtime = self._runtime()
        overlay = CodexUsageTaskbarOverlay(
            root,
            lambda: runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: None,
        )

        with patch(
            "src.apps.codex_usage_taskbar_overlay.time.monotonic",
            return_value=10.0,
        ):
            overlay.refresh()

        with patch(
            "src.apps.codex_usage_taskbar_overlay.time.monotonic",
            return_value=10.24,
        ):
            runtime["accounts"][0]["last_snapshot"]["weekly_limit"] = "63%"
            overlay.refresh()

        changed_metrics = window.draw_calls[-1]["bars"][0]["metrics"]
        self.assertTrue(changed_metrics[1]["flash"])
        self.assertFalse(changed_metrics[1]["flash_phase"])

    def test_uniform_taskbar_background_column_is_not_treated_as_occupied(self):
        colors = [(118, 84, 154), (118, 84, 154), (118, 84, 154)]

        self.assertFalse(_column_looks_occupied(colors, (118, 84, 154)))

    def test_flat_high_contrast_taskbar_control_column_is_treated_as_occupied(self):
        colors = [(242, 242, 242), (242, 242, 242), (242, 242, 242)]

        self.assertTrue(_column_looks_occupied(colors, (118, 84, 154)))

    def test_detect_horizontal_taskbar_occupied_spans_includes_flat_high_contrast_columns(self):
        background = [(118, 84, 154)] * 5
        flat_control = [(242, 242, 242)] * 5
        columns = [(x, flat_control if x == 320 else background) for x in range(0, 800, 40)]

        with patch.object(taskbar_overlay.ctypes, "windll", object(), create=True), patch.object(
            taskbar_overlay,
            "_sample_taskbar_columns",
            return_value=columns,
        ):
            spans = taskbar_overlay._detect_horizontal_taskbar_occupied_spans(
                800,
                600,
                (0, 0, 800, 560),
                {"orientation": "bottom"},
            )

        self.assertIsNotNone(spans)
        self.assertTrue(any(start <= 320 < end for start, end in spans))
        self.assertFalse(any(start <= 400 < end for start, end in spans))

    def test_detect_horizontal_taskbar_occupied_spans_includes_taskbar_child_windows(self):
        class _FakeWin32Gui:
            windows = {
                10: ("ReBarWindow32", (599, 1392, 1440, 1440), True),
                11: ("TrayNotifyWnd", (1976, 1392, 2304, 1440), True),
                12: ("Windows.UI.Composition.DesktopWindowContentBridge", (0, 1392, 2304, 1440), True),
                13: ("Start", (599, 1392, 644, 1440), False),
            }

            def FindWindow(self, class_name, _title):
                return 1 if class_name == "Shell_TrayWnd" else 0

            def EnumChildWindows(self, _hwnd, callback, extra):
                for hwnd in self.windows:
                    callback(hwnd, extra)

            def GetClassName(self, hwnd):
                return self.windows[int(hwnd)][0]

            def IsWindowVisible(self, hwnd):
                return self.windows[int(hwnd)][2]

            def GetWindowRect(self, hwnd):
                return self.windows[int(hwnd)][1]

        background = [(118, 84, 154)] * 5
        columns = [(x, background) for x in range(0, 2304, 40)]

        with patch.object(taskbar_overlay.ctypes, "windll", object(), create=True), patch.object(
            taskbar_overlay,
            "win32gui",
            _FakeWin32Gui(),
        ), patch.object(
            taskbar_overlay,
            "_sample_taskbar_columns",
            return_value=columns,
        ):
            spans = taskbar_overlay._detect_horizontal_taskbar_occupied_spans(
                2304,
                1440,
                (0, 0, 2304, 1392),
                {"orientation": "bottom"},
            )

        self.assertIsNotNone(spans)
        self.assertTrue(any(start <= 599 and end >= 1440 for start, end in spans))
        self.assertTrue(any(start <= 1976 and end >= 2304 for start, end in spans))

        geometry = calculate_taskbar_overlay_geometry(
            2304,
            1440,
            (0, 0, 2304, 1392),
            occupied_spans=spans,
        )

        self.assertTrue(geometry["visible"])
        self.assertGreaterEqual(geometry["x"], 1440)
        self.assertLessEqual(geometry["x"] + geometry["width"], 1976)

    def test_subtract_spans_preserves_all_non_overlapping_fragments(self):
        spans = [(100, 200), (300, 400), (500, 650)]

        result = taskbar_overlay._subtract_spans(spans, [(150, 160), (540, 600)])

        self.assertEqual(
            result,
            [(100, 150), (160, 200), (300, 400), (500, 540), (600, 650)],
        )

    def test_detect_horizontal_taskbar_occupied_spans_preserves_children_when_excluding_overlay(self):
        class _FakeWin32Gui:
            windows = {
                10: ("ReBarWindow32", (100, 560, 220, 600), True),
                11: ("MSTaskSwWClass", (300, 560, 480, 600), True),
                12: ("TrayNotifyWnd", (820, 560, 960, 600), True),
            }

            def FindWindow(self, class_name, _title):
                return 1 if class_name == "Shell_TrayWnd" else 0

            def EnumChildWindows(self, _hwnd, callback, extra):
                for hwnd in self.windows:
                    callback(hwnd, extra)

            def GetClassName(self, hwnd):
                return self.windows[int(hwnd)][0]

            def IsWindowVisible(self, hwnd):
                return self.windows[int(hwnd)][2]

            def GetWindowRect(self, hwnd):
                return self.windows[int(hwnd)][1]

        background = [(118, 84, 154)] * 5
        columns = [(x, background) for x in range(0, 1000, 40)]

        with patch.object(taskbar_overlay.ctypes, "windll", object(), create=True), patch.object(
            taskbar_overlay,
            "win32gui",
            _FakeWin32Gui(),
        ), patch.object(
            taskbar_overlay,
            "_sample_taskbar_columns",
            return_value=columns,
        ):
            spans = taskbar_overlay._detect_horizontal_taskbar_occupied_spans(
                1000,
                600,
                (0, 0, 1000, 560),
                {"orientation": "bottom", "_exclude_spans": [(340, 380)]},
            )

        self.assertIsNotNone(spans)
        self.assertTrue(any(start <= 100 and end >= 220 for start, end in spans))
        self.assertTrue(any(start <= 300 and end >= 340 for start, end in spans))
        self.assertTrue(any(start <= 380 and end >= 480 for start, end in spans))
        self.assertTrue(any(start <= 820 and end >= 960 for start, end in spans))

    def test_foreground_fullscreen_detector_uses_foreground_window_monitor_bounds(self):
        class _FakeWin32Gui:
            def __init__(self):
                self.foreground = 100
                self.rect = (0, 0, 1920, 1080)

            def GetForegroundWindow(self):
                return self.foreground

            def GetAncestor(self, hwnd, _flag):
                return hwnd

            def IsWindowVisible(self, _hwnd):
                return True

            def IsIconic(self, _hwnd):
                return False

            def GetWindowRect(self, _hwnd):
                return self.rect

        class _FakeWin32Api:
            def MonitorFromWindow(self, hwnd, default):
                self.last_monitor_request = (hwnd, default)
                return "monitor-1"

            def MonitorFromRect(self, rect, default):
                self.last_rect_request = (rect, default)
                if rect[0] >= 1920:
                    return "monitor-2"
                return "monitor-1"

            def GetMonitorInfo(self, _monitor):
                if _monitor == "monitor-2":
                    return {"Monitor": (1920, 0, 3840, 1080)}
                return {"Monitor": (0, 0, 1920, 1080)}

        class _FakeWin32Con:
            MONITOR_DEFAULTTONEAREST = 2

        fake_gui = _FakeWin32Gui()
        fake_api = _FakeWin32Api()
        with patch.object(taskbar_overlay, "win32gui", fake_gui), patch.object(
            taskbar_overlay,
            "win32api",
            fake_api,
        ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con):
            self.assertEqual(
                taskbar_overlay._foreground_monitor_rect(100, _FakeRoot()),
                (0, 0, 1920, 1080),
            )
            self.assertTrue(taskbar_overlay._is_foreground_fullscreen(overlay_hwnd=200, root=_FakeRoot()))
            self.assertTrue(
                taskbar_overlay._is_foreground_fullscreen(
                    overlay_hwnd=200,
                    root=_FakeRoot(),
                    target_geometry={"x": 100, "y": 1040, "width": 400, "height": 38},
                )
            )
            self.assertFalse(
                taskbar_overlay._is_foreground_fullscreen(
                    overlay_hwnd=200,
                    root=_FakeRoot(),
                    target_geometry={"x": 2100, "y": 1040, "width": 400, "height": 38},
                )
            )

            fake_gui.rect = (0, 0, 1910, 1080)
            self.assertFalse(taskbar_overlay._is_foreground_fullscreen(overlay_hwnd=200, root=_FakeRoot()))

            fake_gui.rect = (0, 0, 1920, 1080)
            fake_gui.foreground = 200
            self.assertFalse(taskbar_overlay._is_foreground_fullscreen(overlay_hwnd=200, root=_FakeRoot()))

    def test_fullscreen_detector_scans_visible_windows_when_foreground_is_unavailable(self):
        class _FakeWin32Gui:
            windows = {
                100: ("TkTopLevel", (0, 0, 1920, 1080), True, False),
                101: ("Shell_TrayWnd", (0, 1040, 1920, 1080), True, False),
                200: ("TkTopLevel", (1500, 1040, 1860, 1078), True, False),
            }

            def GetForegroundWindow(self):
                return 0

            def EnumWindows(self, callback, extra):
                for hwnd in self.windows:
                    if not callback(hwnd, extra):
                        break

            def GetAncestor(self, hwnd, _flag):
                return hwnd

            def GetClassName(self, hwnd):
                return self.windows[int(hwnd)][0]

            def IsWindowVisible(self, hwnd):
                return self.windows[int(hwnd)][2]

            def IsIconic(self, hwnd):
                return self.windows[int(hwnd)][3]

            def GetWindowRect(self, hwnd):
                return self.windows[int(hwnd)][1]

        class _FakeWin32Api:
            def MonitorFromRect(self, rect, default):
                self.last_rect_request = (rect, default)
                return "monitor-1"

            def MonitorFromWindow(self, hwnd, default):
                self.last_window_request = (hwnd, default)
                return "monitor-1"

            def GetMonitorInfo(self, _monitor):
                return {"Monitor": (0, 0, 1920, 1080)}

        class _FakeWin32Con:
            MONITOR_DEFAULTTONEAREST = 2

        with patch.object(taskbar_overlay, "win32gui", _FakeWin32Gui()), patch.object(
            taskbar_overlay,
            "win32api",
            _FakeWin32Api(),
        ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con):
            self.assertTrue(
                taskbar_overlay._is_foreground_fullscreen(
                    overlay_hwnd=200,
                    root=_FakeRoot(),
                    target_geometry={"x": 1500, "y": 1040, "width": 360, "height": 38},
                )
            )

    def test_fullscreen_detector_scan_accepts_logical_root_screen_coordinates(self):
        class _ScaledRoot(_FakeRoot):
            def winfo_screenwidth(self):
                return 2560

            def winfo_screenheight(self):
                return 1440

        class _FakeWin32Gui:
            windows = {
                100: ("TkTopLevel", "codex-overlay-fullscreen-qa", (0, 0, 2560, 1440), True, False),
                200: ("TkTopLevel", "Windows Supporter", (1864, 1397, 2224, 1435), True, False),
            }

            def GetForegroundWindow(self):
                return 0

            def EnumWindows(self, callback, extra):
                for hwnd in self.windows:
                    if not callback(hwnd, extra):
                        break

            def GetAncestor(self, hwnd, _flag):
                return hwnd

            def GetClassName(self, hwnd):
                return self.windows[int(hwnd)][0]

            def GetWindowText(self, hwnd):
                return self.windows[int(hwnd)][1]

            def IsWindowVisible(self, hwnd):
                return self.windows[int(hwnd)][3]

            def IsIconic(self, hwnd):
                return self.windows[int(hwnd)][4]

            def GetWindowRect(self, hwnd):
                return self.windows[int(hwnd)][2]

        class _FakeWin32Api:
            def MonitorFromRect(self, _rect, _default):
                return "monitor-1"

            def MonitorFromWindow(self, _hwnd, _default):
                return "monitor-1"

            def GetMonitorInfo(self, _monitor):
                return {"Monitor": (0, 0, 2880, 1800)}

        class _FakeWin32Con:
            MONITOR_DEFAULTTONEAREST = 2

        with patch.object(taskbar_overlay, "win32gui", _FakeWin32Gui()), patch.object(
            taskbar_overlay,
            "win32api",
            _FakeWin32Api(),
        ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con):
            self.assertTrue(
                taskbar_overlay._is_foreground_fullscreen(
                    overlay_hwnd=200,
                    root=_ScaledRoot(),
                    target_geometry={"x": 1864, "y": 1397, "width": 360, "height": 38},
                )
            )

    def test_fullscreen_detector_scan_ignores_windows_input_experience(self):
        class _FakeWin32Gui:
            windows = {
                100: ("Windows.UI.Core.CoreWindow", "Windows Input Experience", (0, 0, 2560, 1440), True, False),
                200: ("TkTopLevel", "Windows Supporter", (1864, 1397, 2224, 1435), True, False),
            }

            def GetForegroundWindow(self):
                return 0

            def EnumWindows(self, callback, extra):
                for hwnd in self.windows:
                    if not callback(hwnd, extra):
                        break

            def GetAncestor(self, hwnd, _flag):
                return hwnd

            def GetClassName(self, hwnd):
                return self.windows[int(hwnd)][0]

            def GetWindowText(self, hwnd):
                return self.windows[int(hwnd)][1]

            def IsWindowVisible(self, hwnd):
                return self.windows[int(hwnd)][3]

            def IsIconic(self, hwnd):
                return self.windows[int(hwnd)][4]

            def GetWindowRect(self, hwnd):
                return self.windows[int(hwnd)][2]

        class _FakeWin32Api:
            def MonitorFromRect(self, _rect, _default):
                return "monitor-1"

            def MonitorFromWindow(self, _hwnd, _default):
                return "monitor-1"

            def GetMonitorInfo(self, _monitor):
                return {"Monitor": (0, 0, 2560, 1440)}

        class _FakeWin32Con:
            MONITOR_DEFAULTTONEAREST = 2

        with patch.object(taskbar_overlay, "win32gui", _FakeWin32Gui()), patch.object(
            taskbar_overlay,
            "win32api",
            _FakeWin32Api(),
        ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con):
            self.assertFalse(
                taskbar_overlay._is_foreground_fullscreen(
                    overlay_hwnd=200,
                    root=_FakeRoot(),
                    target_geometry={"x": 1864, "y": 1397, "width": 360, "height": 38},
                )
            )

    def test_fullscreen_detector_scan_ignores_background_fullscreen_sized_windows(self):
        class _FakeWin32Gui:
            windows = {
                100: ("TkTopLevel", "background-fullscreen", (0, 0, 2560, 1440), True, False),
                200: ("TkTopLevel", "Windows Supporter", (1864, 1397, 2224, 1435), True, False),
                300: ("Chrome_WidgetWin_1", "foreground", (20, 20, 1200, 900), True, False),
            }

            def GetForegroundWindow(self):
                return 0

            def EnumWindows(self, callback, extra):
                for hwnd in self.windows:
                    callback(hwnd, extra)

            def GetAncestor(self, hwnd, _flag):
                return hwnd

            def GetClassName(self, hwnd):
                return self.windows[int(hwnd)][0]

            def GetWindowText(self, hwnd):
                return self.windows[int(hwnd)][1]

            def WindowFromPoint(self, _point):
                return 300

            def IsWindowVisible(self, hwnd):
                return self.windows[int(hwnd)][3]

            def IsIconic(self, hwnd):
                return self.windows[int(hwnd)][4]

            def GetWindowRect(self, hwnd):
                return self.windows[int(hwnd)][2]

        class _FakeWin32Api:
            def MonitorFromRect(self, _rect, _default):
                return "monitor-1"

            def MonitorFromWindow(self, _hwnd, _default):
                return "monitor-1"

            def GetMonitorInfo(self, _monitor):
                return {"Monitor": (0, 0, 2560, 1440)}

        class _FakeWin32Con:
            MONITOR_DEFAULTTONEAREST = 2

        with patch.object(taskbar_overlay, "win32gui", _FakeWin32Gui()), patch.object(
            taskbar_overlay,
            "win32api",
            _FakeWin32Api(),
        ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con):
            self.assertFalse(
                taskbar_overlay._is_foreground_fullscreen(
                    overlay_hwnd=200,
                    root=_FakeRoot(),
                    target_geometry={"x": 1864, "y": 1397, "width": 360, "height": 38},
                )
            )

    def test_fullscreen_detector_scan_ignores_other_monitors(self):
        class _FakeWin32Gui:
            windows = {
                100: ("TkTopLevel", (0, 0, 1920, 1080), True, False),
                200: ("TkTopLevel", (2100, 1040, 2460, 1078), True, False),
            }

            def GetForegroundWindow(self):
                return 0

            def EnumWindows(self, callback, extra):
                for hwnd in self.windows:
                    if not callback(hwnd, extra):
                        break

            def GetAncestor(self, hwnd, _flag):
                return hwnd

            def GetClassName(self, hwnd):
                return self.windows[int(hwnd)][0]

            def IsWindowVisible(self, hwnd):
                return self.windows[int(hwnd)][2]

            def IsIconic(self, hwnd):
                return self.windows[int(hwnd)][3]

            def GetWindowRect(self, hwnd):
                return self.windows[int(hwnd)][1]

        class _FakeWin32Api:
            def MonitorFromRect(self, rect, _default):
                return "monitor-2" if int(rect[0]) >= 1920 else "monitor-1"

            def MonitorFromWindow(self, hwnd, _default):
                rect = _FakeWin32Gui.windows[int(hwnd)][1]
                return "monitor-2" if int(rect[0]) >= 1920 else "monitor-1"

            def GetMonitorInfo(self, monitor):
                if monitor == "monitor-2":
                    return {"Monitor": (1920, 0, 3840, 1080)}
                return {"Monitor": (0, 0, 1920, 1080)}

        class _FakeWin32Con:
            MONITOR_DEFAULTTONEAREST = 2

        with patch.object(taskbar_overlay, "win32gui", _FakeWin32Gui()), patch.object(
            taskbar_overlay,
            "win32api",
            _FakeWin32Api(),
        ), patch.object(taskbar_overlay, "win32con", _FakeWin32Con):
            self.assertFalse(
                taskbar_overlay._is_foreground_fullscreen(
                    overlay_hwnd=200,
                    root=_FakeRoot(),
                    target_geometry={"x": 2100, "y": 1040, "width": 360, "height": 38},
                )
            )

    def test_bottom_taskbar_geometry_is_long_and_inside_taskbar_band(self):
        geometry = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1920, 1040),
        )

        self.assertEqual(geometry["orientation"], "bottom")
        self.assertGreaterEqual(geometry["width"], 640)
        self.assertGreater(geometry["width"], geometry["height"] * 12)
        self.assertGreaterEqual(geometry["y"], 1040)
        self.assertLessEqual(geometry["y"] + geometry["height"], 1080)

    def test_bottom_taskbar_geometry_uses_right_slot_when_it_can_fit(self):
        geometry = calculate_taskbar_overlay_geometry(
            1200,
            600,
            (0, 0, 1200, 560),
            occupied_spans=[(0, 120), (520, 720), (1040, 1200)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertGreaterEqual(geometry["x"], 720)
        self.assertLessEqual(geometry["x"] + geometry["width"], 1040)
        self.assertLess(geometry["width"], 640)
        self.assertGreaterEqual(geometry["width"], 300)

    def test_bottom_taskbar_geometry_prefers_right_slot_even_when_left_slot_is_wider(self):
        geometry = calculate_taskbar_overlay_geometry(
            1400,
            600,
            (0, 0, 1400, 560),
            occupied_spans=[(0, 120), (620, 820), (1280, 1400)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertGreaterEqual(geometry["x"], 820)
        self.assertLessEqual(geometry["x"] + geometry["width"], 1280)
        self.assertGreaterEqual(geometry["width"], 344)

    def test_bottom_taskbar_geometry_uses_measured_mid_sized_right_slot(self):
        geometry = calculate_taskbar_overlay_geometry(
            2560,
            1440,
            (0, 0, 2560, 1392),
            occupied_spans=[(0, 112), (740, 1824), (2220, 2560)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertEqual(geometry["x"], 1832)
        self.assertEqual(geometry["width"], 380)

    def test_bottom_taskbar_geometry_can_use_wider_safe_right_slot_than_old_preferred_cap(self):
        geometry = calculate_taskbar_overlay_geometry(
            2560,
            1440,
            (0, 0, 2560, 1392),
            occupied_spans=[(0, 112), (740, 1700), (2400, 2560)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertGreater(
            geometry["width"],
            taskbar_overlay._TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX,
        )
        self.assertGreaterEqual(geometry["x"], 1708)
        self.assertLessEqual(geometry["x"] + geometry["width"], 2392)

    def test_bottom_taskbar_geometry_uses_preferred_width_when_slot_is_wide(self):
        geometry = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1920, 1040),
            occupied_spans=[(0, 900), (1500, 1920)],
            preferred_width=420,
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertEqual(geometry["width"], 420)
        self.assertEqual(geometry["x"], 1072)

    def test_bottom_taskbar_geometry_caps_large_preferred_width_to_slot(self):
        geometry = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1920, 1040),
            occupied_spans=[(0, 900), (1600, 1920)],
            preferred_width=720,
        )

        self.assertTrue(geometry["visible"])
        self.assertGreater(
            geometry["width"],
            taskbar_overlay._TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX,
        )
        self.assertEqual(geometry["width"], 684)
        self.assertGreaterEqual(geometry["x"], 908)
        self.assertLessEqual(geometry["x"] + geometry["width"], 1592)

    def test_bottom_taskbar_geometry_uses_available_slot_below_preferred_width(self):
        geometry = calculate_taskbar_overlay_geometry(
            1000,
            600,
            (0, 0, 1000, 560),
            occupied_spans=[(0, 100), (460, 1000)],
            preferred_width=420,
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["width"], 344)

    def test_bottom_taskbar_geometry_uses_compact_safe_slot_instead_of_hiding(self):
        geometry = calculate_taskbar_overlay_geometry(
            1000,
            1000,
            (0, 0, 1000, 960),
            occupied_spans=[(0, 780), (972, 1000)],
            preferred_width=420,
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["width"], 176)
        self.assertEqual(geometry["x"], 788)

    def test_bottom_taskbar_geometry_ignores_preferred_width_without_occupied_spans(self):
        baseline = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1920, 1040),
        )
        preferred = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1920, 1040),
            preferred_width=420,
        )

        self.assertEqual(preferred, baseline)

    def test_preferred_taskbar_overlay_width_returns_none_without_visible_rows(self):
        self.assertIsNone(
            taskbar_overlay._preferred_taskbar_overlay_width_for_model(
                {"visible": False, "bars": []}
            )
        )
        self.assertIsNone(
            taskbar_overlay._preferred_taskbar_overlay_width_for_model(
                {"visible": True, "bars": []}
            )
        )

    def test_preferred_taskbar_overlay_width_below_status_text_switch_uses_dot_only(self):
        model = {
            "visible": True,
            "bars": [
                {
                    "enabled": True,
                    "metrics": [
                        {
                            "key": "5h",
                            "metric_key": "five_hour_limit",
                            "percent": 47,
                            "value_text": "47%",
                            "color": "#22c55e",
                            "reset_text": "00h 00m",
                            "reset_short_text": "00h 00m",
                            "reset_badge_label": "부",
                            "reset_badge_short_label": "부",
                        }
                    ],
                }
            ],
        }

        width = taskbar_overlay._preferred_taskbar_overlay_width_for_model(model)

        self.assertIsNotNone(width)
        self.assertLess(width, 420)
        self.assertEqual(
            taskbar_overlay._status_width_for_overlay_width(width),
            taskbar_overlay._STATUS_DOT_ONLY_WIDTH_PX,
        )

    def _row_badge_metrics(self):
        return (
            {
                "key": "5h",
                "metric_key": "five_hour_limit",
                "percent": 47,
                "value_text": "47%",
                "color": "#22c55e",
                "reset_text": "00h 00m",
                "reset_short_text": "00h 00m",
                "reset_color": "#ef4444",
                "reset_marker": "↓",
                "reset_badge_label": "부족",
                "reset_badge_short_label": "부",
                "reset_badge_fill": "#7f1d1d",
                "reset_badge_outline": "#ef4444",
                "reset_badge_text_color": "#fee2e2",
            },
            {
                "key": "7d",
                "metric_key": "weekly_limit",
                "percent": 52,
                "value_text": "52%",
                "color": "#22c55e",
                "reset_text": "6d 20h 00m",
                "reset_short_text": "6d 20h",
                "reset_color": "#f59e0b",
                "reset_marker": "↑",
                "reset_badge_label": "남음",
                "reset_badge_short_label": "남",
                "reset_badge_fill": "#78350f",
                "reset_badge_outline": "#f59e0b",
                "reset_badge_text_color": "#fef3c7",
            },
        )

    def _row_badge_model(self, width, metrics=None):
        visible_metrics = tuple(metrics or self._row_badge_metrics())
        return {
            "visible": True,
            "state": "ready",
            "geometry": {
                "x": 0,
                "y": 0,
                "width": int(width),
                "height": 38,
                "orientation": "bottom",
                "visible": True,
            },
            "bars": [
                {
                    "enabled": True,
                    "label": "Codex 1",
                    "status_text": "정상",
                    "status_color": "#22c55e",
                    "metrics": [dict(metric) for metric in visible_metrics],
                }
            ],
        }

    def _two_row_badge_model(self, width, first_metrics, second_metrics):
        model = self._row_badge_model(width, metrics=first_metrics)
        model["bars"].append(
            {
                "enabled": True,
                "label": "Codex 2",
                "status_text": "정상",
                "status_color": "#22c55e",
                "metrics": [dict(metric) for metric in second_metrics],
            }
        )
        return model

    def _draw_row_badge_texts(self, width, metrics=None):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        canvas = _FakeCanvas()
        overlay._canvas = canvas
        overlay._draw(self._row_badge_model(width, metrics=metrics))
        return [
            op[2].get("text")
            for op in canvas.ops
            if op[0] == "text"
        ]

    def test_preferred_taskbar_overlay_width_accounts_for_status_text_switch(self):
        metric = {
            "key": "5h",
            "metric_key": "five_hour_limit",
            "percent": 47,
            "value_text": "47%",
            "color": "#22c55e",
            "reset_text": "00h 00m",
            "reset_short_text": "00h 00m",
            "reset_badge_label": "부족한도",
            "reset_badge_short_label": "부족한도",
        }
        model = {
            "visible": True,
            "bars": [
                {
                    "enabled": True,
                    "metrics": [dict(metric), dict(metric, key="7d", metric_key="weekly_limit")],
                }
            ],
        }

        width = taskbar_overlay._preferred_taskbar_overlay_width_for_model(model)

        self.assertIsNotNone(width)
        self.assertGreaterEqual(width, 420)
        self.assertEqual(
            taskbar_overlay._status_width_for_overlay_width(width),
            taskbar_overlay._STATUS_WITH_TEXT_WIDTH_PX,
        )

    def test_preferred_taskbar_overlay_width_fits_widest_equal_segment(self):
        narrow_metric = {
            "key": "5h",
            "metric_key": "five_hour_limit",
            "percent": 47,
            "value_text": "47%",
            "color": "#22c55e",
            "reset_text": "00h 00m",
            "reset_short_text": "00h 00m",
            "reset_badge_label": "부",
            "reset_badge_short_label": "부",
        }
        wide_metric = dict(
            narrow_metric,
            key="7d",
            metric_key="weekly_limit",
            reset_badge_label="부족한도",
            reset_badge_short_label="부족한도",
        )
        model = {
            "visible": True,
            "bars": [
                {
                    "enabled": True,
                    "metrics": [narrow_metric, wide_metric],
                }
            ],
        }

        width = taskbar_overlay._preferred_taskbar_overlay_width_for_model(model)

        self.assertIsNotNone(width)
        self.assertEqual(width, 484)
        row_layout = taskbar_overlay._metric_row_layout_for_overlay_width(
            width,
            (narrow_metric, wide_metric),
        )
        narrower_row_layout = taskbar_overlay._metric_row_layout_for_overlay_width(
            width - 1,
            (narrow_metric, wide_metric),
        )
        required_wide_segment = taskbar_overlay._required_metric_segment_width(wide_metric)
        self.assertGreaterEqual(row_layout.segment_width, required_wide_segment)
        self.assertLess(narrower_row_layout.segment_width, required_wide_segment)
        layout = taskbar_overlay._fit_metric_segment_layout(
            row_layout.segment_width,
            wide_metric["reset_text"],
            wide_metric["reset_short_text"],
            badge_label=wide_metric["reset_badge_label"],
            badge_short_label=wide_metric["reset_badge_short_label"],
            metric_key=wide_metric["metric_key"],
            reset_marker="",
            has_reset_badge=True,
            progress_width=row_layout.progress_width,
        )
        badge_fit = dict(layout["badge_fit"])

        self.assertTrue(badge_fit["badge_visible"])
        self.assertEqual(badge_fit["time_text"], "00h 00m")
        self.assertGreaterEqual(
            int(layout["progress_width"]),
            taskbar_overlay._METRIC_PROGRESS_MIN_WIDTH_PX,
        )

    def test_overlay_badge_mode_resolves_full_or_short_from_row_layouts(self):
        metrics = self._row_badge_metrics()

        full_layout = taskbar_overlay._metric_row_layout_for_overlay_width(460, metrics)
        short_layout = taskbar_overlay._metric_row_layout_for_overlay_width(414, metrics)

        self.assertEqual(
            taskbar_overlay._resolve_overlay_badge_mode((full_layout,)),
            "full",
        )
        self.assertEqual(
            taskbar_overlay._resolve_overlay_badge_mode((short_layout,)),
            "short",
        )

    def test_preferred_width_uses_full_badges_before_compacting_row(self):
        metrics = self._row_badge_metrics()
        model = {
            "visible": True,
            "bars": [
                {
                    "enabled": True,
                    "metrics": [dict(metric) for metric in metrics],
                }
            ],
        }

        width = taskbar_overlay._preferred_taskbar_overlay_width_for_model(model)
        row_layout = taskbar_overlay._metric_row_layout_for_overlay_width(width, metrics)
        narrower_layout = taskbar_overlay._metric_row_layout_for_overlay_width(width - 1, metrics)

        self.assertGreater(width, 414)
        self.assertEqual(
            taskbar_overlay._resolve_overlay_badge_mode((row_layout,)),
            "full",
        )
        self.assertNotEqual(
            taskbar_overlay._resolve_overlay_badge_mode((narrower_layout,)),
            "full",
        )

    def test_draw_compacts_all_row_badges_when_full_mode_does_not_fit(self):
        texts = self._draw_row_badge_texts(414)

        self.assertIn("부", texts)
        self.assertIn("남", texts)
        self.assertNotIn("부족", texts)
        self.assertNotIn("남음", texts)

    def test_draw_uses_full_row_badges_when_full_mode_fits(self):
        texts = self._draw_row_badge_texts(460)

        self.assertIn("부족", texts)
        self.assertIn("남음", texts)
        self.assertNotIn("부", texts)
        self.assertNotIn("남", texts)

    def test_draw_forces_compact_badge_mode_across_all_rows(self):
        first_metrics = tuple(
            dict(
                metric,
                reset_text="00h",
                reset_short_text="00h",
                reset_badge_label=metric["reset_badge_short_label"],
            )
            for metric in self._row_badge_metrics()
        )
        second_metrics = self._row_badge_metrics()
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        canvas = _FakeCanvas()
        calls = []

        def capture_segment(_canvas, metric, _x, _y, _width, _row_height, **kwargs):
            calls.append(
                {
                    "metric": dict(metric),
                    "badge_mode": kwargs.get("badge_mode"),
                    "progress_width": kwargs.get("progress_width"),
                }
            )

        overlay._canvas = canvas
        overlay._draw_metric_segment = capture_segment
        overlay._draw(
            self._two_row_badge_model(
                414,
                first_metrics=first_metrics,
                second_metrics=second_metrics,
            )
        )

        self.assertEqual(len(calls), 4)
        self.assertEqual({call["badge_mode"] for call in calls}, {"short"})
        self.assertEqual(
            len({call["progress_width"] for call in calls}),
            1,
        )

    def test_fit_reset_badge_can_be_forced_to_short_or_full_mode(self):
        short_available = (
            taskbar_overlay._reset_badge_width_for_label("남")
            + taskbar_overlay._RESET_BADGE_TIME_GAP_PX
            + taskbar_overlay._reset_column_width_for_text(
                "6d 20h 00m",
                metric_key="weekly_limit",
            )
        )

        full_fit = taskbar_overlay._fit_reset_badge_for_space(
            "6d 20h 00m",
            "6d 20h",
            badge_label="남음",
            badge_short_label="남",
            metric_key="weekly_limit",
            available_px=short_available,
            badge_mode="full",
        )
        short_fit = taskbar_overlay._fit_reset_badge_for_space(
            "6d 20h 00m",
            "6d 20h",
            badge_label="남음",
            badge_short_label="남",
            metric_key="weekly_limit",
            available_px=short_available,
            badge_mode="short",
        )

        self.assertNotEqual(full_fit["badge_label"], "남")
        self.assertEqual(short_fit["badge_label"], "남")
        self.assertEqual(short_fit["time_text"], "6d 20h 00m")

    def test_forced_short_badge_mode_prefers_visible_badge_over_time_only(self):
        short_badge_width = taskbar_overlay._reset_badge_width_for_label("부")
        detail_width = taskbar_overlay._reset_column_width_for_text(
            "4d 11h 26m",
            metric_key="weekly_limit",
        )
        available = max(short_badge_width, detail_width)

        self.assertLess(
            available,
            short_badge_width
            + taskbar_overlay._RESET_BADGE_TIME_GAP_PX
            + detail_width,
        )

        fit = taskbar_overlay._fit_reset_badge_for_space(
            "4d 11h 26m",
            "4d 11h",
            badge_label="부족",
            badge_short_label="부",
            metric_key="weekly_limit",
            available_px=available,
            badge_mode="short",
        )

        self.assertTrue(fit["badge_visible"])
        self.assertEqual(fit["badge_label"], "부")
        self.assertEqual(fit["time_text"], "")
        self.assertEqual(fit["variant"], "badge_short_only")

    def test_refresh_clamp_path_compacts_all_row_badges_from_final_geometry(self):
        metrics = self._row_badge_metrics()
        window = _FakeWindow()
        canvas = _FakeCanvas()
        preferred_widths = []
        original_preferred_width = taskbar_overlay._preferred_taskbar_overlay_width_for_model

        def build_model(runtime_status, geometry=None, *, now=None):
            return self._row_badge_model(
                int(dict(geometry or taskbar_overlay._DEFAULT_GEOMETRY)["width"]),
                metrics=metrics,
            )

        def preferred_width(model):
            width = original_preferred_width(model)
            preferred_widths.append(width)
            return width

        overlay = CodexUsageTaskbarOverlay(
            _FakeRoot(),
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: [
                (0, 900),
                (1330, 1920),
            ],
        )
        overlay._canvas = canvas

        with patch.object(
            taskbar_overlay,
            "build_codex_usage_taskbar_overlay_model",
            side_effect=build_model,
        ), patch.object(
            taskbar_overlay,
            "_preferred_taskbar_overlay_width_for_model",
            side_effect=preferred_width,
        ):
            overlay.refresh()

        texts = [
            op[2].get("text")
            for op in canvas.ops
            if op[0] == "text"
        ]

        self.assertGreater(preferred_widths[0], 414)
        self.assertIn("414x", window.geometry_calls[-1])
        self.assertIn("부", texts)
        self.assertIn("남", texts)
        self.assertNotIn("부족", texts)
        self.assertNotIn("남음", texts)

    def test_refresh_uses_real_preferred_width_above_old_text_cap_when_slot_is_wide(self):
        metrics = self._row_badge_metrics()
        window = _FakeWindow()
        preferred_widths = []
        original_preferred_width = taskbar_overlay._preferred_taskbar_overlay_width_for_model

        def build_model(runtime_status, geometry=None, *, now=None):
            return self._row_badge_model(
                int(dict(geometry or taskbar_overlay._DEFAULT_GEOMETRY)["width"]),
                metrics=metrics,
            )

        def preferred_width(model):
            width = original_preferred_width(model)
            preferred_widths.append(width)
            return width

        overlay = CodexUsageTaskbarOverlay(
            _FakeRoot(),
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: [
                (0, 900),
                (1700, 1920),
            ],
        )

        with patch.object(
            taskbar_overlay,
            "build_codex_usage_taskbar_overlay_model",
            side_effect=build_model,
        ), patch.object(
            taskbar_overlay,
            "_preferred_taskbar_overlay_width_for_model",
            side_effect=preferred_width,
        ):
            overlay.refresh()

        self.assertTrue(preferred_widths)
        self.assertGreater(
            preferred_widths[0],
            taskbar_overlay._TEXT_FRIENDLY_EMPTY_SLOT_WIDTH_PX,
        )
        self.assertIn(
            f"{taskbar_overlay._WIDE_EMPTY_SLOT_TARGET_WIDTH_PX}x",
            window.geometry_calls[-1],
        )

    def test_bottom_taskbar_geometry_uses_right_slot_as_equal_width_tie_breaker(self):
        geometry = calculate_taskbar_overlay_geometry(
            1400,
            600,
            (0, 0, 1400, 560),
            occupied_spans=[(0, 120), (620, 820), (1320, 1400)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertGreaterEqual(geometry["x"], 820)
        self.assertLessEqual(geometry["x"] + geometry["width"], 1320)
        self.assertGreaterEqual(geometry["width"], 344)

    def test_bottom_taskbar_geometry_accepts_compact_right_slot(self):
        geometry = calculate_taskbar_overlay_geometry(
            1000,
            600,
            (0, 0, 1000, 560),
            occupied_spans=[(0, 100), (460, 1000)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertEqual(geometry["width"], 344)
        self.assertGreaterEqual(geometry["x"], 108)
        self.assertLessEqual(geometry["x"] + geometry["width"], 452)

    def test_bottom_taskbar_geometry_uses_small_right_slot_without_left_fallback(self):
        geometry = calculate_taskbar_overlay_geometry(
            1000,
            600,
            (0, 0, 1000, 560),
            occupied_spans=[(0, 100), (520, 720), (990, 1000)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["width"], 254)
        self.assertEqual(geometry["x"], 728)

    def test_bottom_taskbar_geometry_hides_when_no_empty_slot_can_fit(self):
        geometry = calculate_taskbar_overlay_geometry(
            1000,
            600,
            (0, 0, 1000, 560),
            occupied_spans=[(0, 300), (310, 620), (630, 1000)],
        )

        self.assertFalse(geometry["visible"])
        self.assertEqual(geometry["width"], 0)

    def test_model_hides_when_geometry_reports_no_empty_taskbar_space(self):
        model = build_codex_usage_taskbar_overlay_model(
            self._runtime(),
            geometry={
                "x": 0,
                "y": 560,
                "width": 0,
                "height": 0,
                "orientation": "bottom",
                "visible": False,
            },
        )

        self.assertFalse(model["visible"])

    def test_top_taskbar_geometry_stays_inside_top_band(self):
        geometry = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 40, 1920, 1080),
        )

        self.assertEqual(geometry["orientation"], "top")
        self.assertGreaterEqual(geometry["y"], 0)
        self.assertLessEqual(geometry["y"] + geometry["height"], 40)
        self.assertGreaterEqual(geometry["width"], 640)

    def test_side_taskbar_geometries_stay_inside_side_bands(self):
        left = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (80, 0, 1920, 1080),
        )
        right = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1840, 1080),
        )

        self.assertEqual(left["orientation"], "left")
        self.assertLessEqual(left["x"] + left["width"], 80)
        self.assertEqual(right["orientation"], "right")
        self.assertGreaterEqual(right["x"], 1840)
        self.assertLessEqual(right["x"] + right["width"], 1920)

    def test_thin_bottom_taskbar_geometry_keeps_overlay_inside_screen(self):
        geometry = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1920, 1056),
        )

        self.assertEqual(geometry["orientation"], "bottom")
        self.assertGreaterEqual(geometry["y"], 1056)
        self.assertLessEqual(geometry["y"] + geometry["height"], 1080)
        self.assertGreaterEqual(geometry["height"], 18)

    def test_ultra_thin_taskbar_geometries_never_overflow_the_band(self):
        bottom = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1920, 1068),
        )
        left = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (12, 0, 1920, 1080),
        )
        right = calculate_taskbar_overlay_geometry(
            1920,
            1080,
            (0, 0, 1908, 1080),
        )

        self.assertLessEqual(bottom["y"] + bottom["height"], 1080)
        self.assertLessEqual(left["x"] + left["width"], 12)
        self.assertGreaterEqual(right["x"], 1908)
        self.assertLessEqual(right["x"] + right["width"], 1920)

    def test_refresh_draws_and_shows_overlay_without_querying_window_tooltips(self):
        window = _FakeWindow()
        overlay = CodexUsageTaskbarOverlay(
            _FakeRoot(),
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: None,
        )

        refreshed = overlay.refresh()

        self.assertTrue(refreshed)
        self.assertEqual(window.deiconify_calls, 1)
        self.assertEqual(window.withdraw_calls, 0)
        self.assertEqual(len(window.geometry_calls), 1)
        self.assertIn("x", window.draw_calls[0]["geometry"])
        self.assertEqual(len(window.draw_calls[0]["bars"]), 2)

    def test_refresh_suppresses_overlay_while_fullscreen_is_active(self):
        root = _FakeRoot()
        window = _FakeWindow()
        fullscreen_active = {"value": True}
        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: None,
            fullscreen_detector=lambda _window: bool(fullscreen_active["value"]),
        )
        z_order_calls = []
        overlay._reassert_native_z_order = lambda target: z_order_calls.append(target)

        self.assertTrue(overlay.refresh())

        self.assertEqual(window.deiconify_calls, 0)
        self.assertEqual(window.draw_calls, [])
        self.assertTrue(root.after_calls)

        root.after_calls[-1][1]()
        self.assertEqual(z_order_calls, [])

        fullscreen_active["value"] = False
        root.after_calls[-1][1]()

        self.assertEqual(window.deiconify_calls, 1)
        self.assertEqual(len(window.draw_calls), 1)

    def test_refresh_reuses_geometry_without_withdrawing_visible_overlay(self):
        window = _FakeWindow()
        occupied_calls = []

        def occupied_span_getter(width, height, work_area, geometry):
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return [(0, 700), (1100, 1920)]

        overlay = CodexUsageTaskbarOverlay(
            _FakeRoot(),
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay.refresh()

        self.assertEqual(window.withdraw_calls, 0)
        self.assertEqual(len(occupied_calls), 1)

    def test_refresh_builds_preferred_and_final_model_from_one_runtime_snapshot_and_now(self):
        window = _FakeWindow()
        runtime_calls = []
        build_calls = []

        def runtime_getter():
            runtime_calls.append("runtime")
            return self._runtime()

        def build_model(runtime_status, geometry=None, *, now=None):
            build_calls.append((runtime_status, dict(geometry or {}), now))
            return {
                "visible": True,
                "state": "ready",
                "geometry": dict(geometry or taskbar_overlay._DEFAULT_GEOMETRY),
                "bars": [
                    {
                        "enabled": True,
                        "label": "Codex 1",
                        "status_text": "정상",
                        "status_color": "#22c55e",
                        "metrics": [],
                    }
                ],
            }

        overlay = CodexUsageTaskbarOverlay(
            _FakeRoot(),
            runtime_getter,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: [
                (0, 900),
                (1500, 1920),
            ],
        )

        with patch.object(
            taskbar_overlay,
            "build_codex_usage_taskbar_overlay_model",
            side_effect=build_model,
        ), patch.object(
            taskbar_overlay,
            "_preferred_taskbar_overlay_width_for_model",
            return_value=420,
            create=True,
        ):
            overlay.refresh()

        self.assertEqual(len(runtime_calls), 1)
        self.assertEqual(len(build_calls), 2)
        self.assertIs(build_calls[0][0], build_calls[1][0])
        self.assertIsNotNone(build_calls[0][2])
        self.assertIs(build_calls[0][2], build_calls[1][2])
        self.assertEqual(build_calls[0][1]["width"], taskbar_overlay._DEFAULT_GEOMETRY["width"])
        self.assertEqual(build_calls[1][1]["width"], 420)

    def test_refresh_resamples_geometry_when_preferred_width_changes(self):
        window = _FakeWindow()
        occupied_calls = []

        def occupied_span_getter(width, height, work_area, geometry):
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return [(0, 900), (1500, 1920)]

        overlay = CodexUsageTaskbarOverlay(
            _FakeRoot(),
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        with patch.object(
            taskbar_overlay,
            "_preferred_taskbar_overlay_width_for_model",
            side_effect=[420, 360],
            create=True,
        ):
            overlay.refresh()
            overlay.refresh()

        self.assertEqual(len(occupied_calls), 2)
        self.assertIn("420x", window.geometry_calls[0])
        self.assertIn("360x", window.geometry_calls[1])

    def test_geometry_changed_detects_status_text_threshold_crossing(self):
        previous = {
            "visible": True,
            "orientation": "bottom",
            "x": 100,
            "y": 200,
            "width": 419,
            "height": 38,
        }
        current = dict(previous, width=420)

        self.assertTrue(taskbar_overlay._geometry_changed(previous, current))

    def test_geometry_changed_ignores_width_jitter_within_tolerance(self):
        previous = {
            "visible": True,
            "orientation": "bottom",
            "x": 100,
            "y": 200,
            "width": 376,
            "height": 38,
        }
        current = dict(previous, x=101, width=375)

        self.assertFalse(taskbar_overlay._geometry_changed(previous, current))

    def test_geometry_invalidation_allows_explicit_resample(self):
        window = _FakeWindow()
        occupied_calls = []

        def occupied_span_getter(width, height, work_area, geometry):
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return [(0, 700), (1100, 1920)]

        overlay = CodexUsageTaskbarOverlay(
            _FakeRoot(),
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay.invalidate_geometry()
        overlay.refresh()

        self.assertEqual(window.withdraw_calls, 1)
        self.assertEqual(len(occupied_calls), 2)

    def test_geometry_monitor_tick_resamples_slot_without_explicit_invalidation(self):
        root = _FakeRoot()
        window = _FakeWindow()
        occupied_calls = []
        spans_by_call = [
            [(0, 900), (1300, 1920)],
            [(0, 900), (1500, 1920)],
        ]

        def occupied_span_getter(width, height, work_area, geometry):
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return spans_by_call[min(len(occupied_calls) - 1, len(spans_by_call) - 1)]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay._last_geometry_hard_resample_at = taskbar_overlay.time.monotonic()
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][0]
        geometry_tick()

        self.assertEqual(len(occupied_calls), 2)
        self.assertGreaterEqual(len(window.geometry_calls), 2)
        expected_width = 1500 - 900 - taskbar_overlay._EMPTY_SLOT_PADDING_PX * 2
        self.assertIn(
            f"{expected_width}x",
            window.geometry_calls[-1],
        )

    def test_geometry_monitor_tick_reuses_runtime_snapshot_and_now_for_width_change(self):
        root = _FakeRoot()
        window = _FakeWindow()
        runtime_calls = []
        build_calls = []

        def runtime_getter():
            runtime_calls.append("runtime")
            return self._runtime()

        def build_model(runtime_status, geometry=None, *, now=None):
            build_calls.append((runtime_status, dict(geometry or {}), now))
            return {
                "visible": True,
                "state": "ready",
                "geometry": dict(geometry or taskbar_overlay._DEFAULT_GEOMETRY),
                "bars": [
                    {
                        "enabled": True,
                        "label": "Codex 1",
                        "status_text": "정상",
                        "status_color": "#22c55e",
                        "metrics": [],
                    }
                ],
            }

        overlay = CodexUsageTaskbarOverlay(
            root,
            runtime_getter,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: [
                (0, 900),
                (1500, 1920),
            ],
        )
        overlay._window = window
        overlay._last_model = {
            "visible": True,
            "geometry": {
                "x": 908,
                "y": 1041,
                "width": 300,
                "height": 38,
                "orientation": "bottom",
                "visible": True,
            },
            "bars": [],
        }

        with patch.object(
            taskbar_overlay,
            "build_codex_usage_taskbar_overlay_model",
            side_effect=build_model,
        ), patch.object(
            taskbar_overlay,
            "_preferred_taskbar_overlay_width_for_model",
            return_value=420,
            create=True,
        ):
            overlay._geometry_monitor_tick()

        self.assertEqual(len(runtime_calls), 1)
        self.assertEqual(len(build_calls), 2)
        self.assertIs(build_calls[0][0], build_calls[1][0])
        self.assertIsNotNone(build_calls[0][2])
        self.assertIs(build_calls[0][2], build_calls[1][2])
        self.assertEqual(build_calls[0][1]["width"], taskbar_overlay._DEFAULT_GEOMETRY["width"])
        self.assertEqual(build_calls[1][1]["width"], 420)

    def test_geometry_monitor_tick_redraws_when_time_changes_without_geometry_change(self):
        root = _FakeRoot()
        window = _FakeWindow()
        geometry = {
            "x": 908,
            "y": 1041,
            "width": 420,
            "height": 38,
            "orientation": "bottom",
            "visible": True,
        }
        build_calls = []

        def build_model(runtime_status, geometry=None, *, now=None):
            build_calls.append((runtime_status, dict(geometry or {}), now))
            return {
                "visible": True,
                "state": "ready",
                "geometry": dict(geometry or taskbar_overlay._DEFAULT_GEOMETRY),
                "bars": [
                    {
                        "id": "account_1",
                        "enabled": True,
                        "label": "Codex 1",
                        "status_text": "정상",
                        "status_color": "#22c55e",
                        "metrics": [
                            {
                                "key": "5h",
                                "metric_key": "five_hour_limit",
                                "percent": 99,
                                "value_text": "99%",
                                "color": "#22c55e",
                                "reset_text": "04h 59m",
                                "reset_short_text": "04h 59m",
                                "reset_color": "#22c55e",
                            }
                        ],
                    }
                ],
            }

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
        )
        overlay._window = window
        overlay._last_metric_values = {"account_1:five_hour_limit": "98%"}
        overlay._last_model = {
            "visible": True,
            "geometry": dict(geometry),
            "bars": [
                {
                    "id": "account_1",
                    "enabled": True,
                    "label": "Codex 1",
                    "status_text": "정상",
                    "status_color": "#22c55e",
                    "metrics": [
                        {
                            "key": "5h",
                            "metric_key": "five_hour_limit",
                            "percent": 99,
                            "value_text": "99%",
                            "color": "#22c55e",
                            "reset_text": "00h 00m",
                            "reset_short_text": "00h 00m",
                            "reset_color": "#ef4444",
                        }
                    ],
                }
            ],
        }

        with patch.object(
            taskbar_overlay,
            "build_codex_usage_taskbar_overlay_model",
            side_effect=build_model,
        ), patch.object(
            taskbar_overlay,
            "_preferred_taskbar_overlay_width_for_model",
            return_value=420,
        ), patch.object(
            overlay,
            "_calculate_geometry",
            return_value=dict(geometry),
        ):
            overlay._geometry_monitor_tick()

        self.assertEqual(len(build_calls), 2)
        self.assertEqual(window.geometry_calls, [])
        self.assertEqual(len(window.draw_calls), 1)
        self.assertEqual(
            overlay._last_model["bars"][0]["metrics"][0]["reset_text"],
            "04h 59m",
        )
        self.assertEqual(
            overlay._last_metric_values.get("account_1:five_hour_limit"),
            "99%",
        )
        self.assertIn("account_1:five_hour_limit", overlay._flash_until)
        self.assertIsNotNone(overlay._geometry_after_id)

    def test_geometry_monitor_hard_resample_restores_window_when_slot_is_unchanged(self):
        root = _FakeRoot()
        window = _FakeWindow()
        occupied_calls = []

        def occupied_span_getter(width, height, work_area, geometry):
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return [(0, 900), (1500, 1920)]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )
        repaint_calls = []
        overlay._force_native_repaint = lambda target: repaint_calls.append(target)

        overlay.refresh()
        deiconify_calls_before_tick = window.deiconify_calls
        lift_calls_before_tick = window.lift_calls
        overlay._last_geometry_hard_resample_at = 0.0
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][0]
        with patch(
            "src.apps.codex_usage_taskbar_overlay.time.monotonic",
            return_value=10.0,
        ):
            geometry_tick()

        self.assertEqual(len(occupied_calls), 2)
        self.assertEqual(window.withdraw_calls, 0)
        self.assertEqual(len(window.geometry_calls), 1)
        self.assertEqual(window.deiconify_calls, deiconify_calls_before_tick + 1)
        self.assertEqual(window.lift_calls, lift_calls_before_tick + 1)
        self.assertEqual(repaint_calls[-1], window)
        self.assertIsNotNone(overlay._geometry_after_id)

    def test_geometry_monitor_keeps_polling_when_slot_temporarily_disappears(self):
        root = _FakeRoot()
        window = _FakeWindow()
        occupied_calls = []
        spans_by_call = [
            [(0, 900), (1300, 1920)],
            [(0, 1920)],
            [(0, 900), (1500, 1920)],
        ]

        def occupied_span_getter(width, height, work_area, geometry):
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return spans_by_call[min(len(occupied_calls) - 1, len(spans_by_call) - 1)]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay._last_geometry_hard_resample_at = taskbar_overlay.time.monotonic()
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][0]
        geometry_tick()

        self.assertEqual(window.withdraw_calls, 0)
        self.assertEqual(len(window.geometry_calls), 1)
        self.assertIsNotNone(overlay._geometry_after_id)

        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][-1]
        geometry_tick()

        self.assertGreaterEqual(len(occupied_calls), 3)
        self.assertGreaterEqual(window.deiconify_calls, 2)
        expected_width = 1500 - 900 - taskbar_overlay._EMPTY_SLOT_PADDING_PX * 2
        self.assertIn(
            f"{expected_width}x",
            window.geometry_calls[-1],
        )

    def test_geometry_monitor_defers_transient_width_shrink_without_jitter(self):
        root = _FakeRoot()
        window = _FakeWindow()
        occupied_calls = []
        spans_by_call = [
            [(0, 900), (1700, 1920)],
            [(0, 900), (1400, 1920)],
            [(0, 900), (1700, 1920)],
        ]

        def occupied_span_getter(width, height, work_area, geometry):
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return spans_by_call[min(len(occupied_calls) - 1, len(spans_by_call) - 1)]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay._last_geometry_hard_resample_at = taskbar_overlay.time.monotonic()
        initial_geometry = window.geometry_calls[-1]
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][0]
        geometry_tick()

        self.assertEqual(window.geometry_calls, [initial_geometry])
        self.assertIsNotNone(overlay._geometry_after_id)

        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][-1]
        geometry_tick()

        self.assertGreaterEqual(len(occupied_calls), 3)
        self.assertEqual(window.geometry_calls[-1], initial_geometry)

    def test_geometry_monitor_deferral_keeps_refresh_from_using_transient_cache(self):
        root = _FakeRoot()
        window = _FakeWindow()
        spans_by_call = [
            [(0, 900), (1700, 1920)],
            [(0, 900), (1400, 1920)],
            [(0, 900), (1700, 1920)],
        ]
        occupied_calls = []

        def occupied_span_getter(width, height, work_area, geometry):
            index = min(len(occupied_calls), len(spans_by_call) - 1)
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return spans_by_call[index]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay._last_geometry_hard_resample_at = taskbar_overlay.time.monotonic()
        initial_geometry = window.geometry_calls[-1]
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][0]
        geometry_tick()
        overlay.refresh()

        self.assertEqual(window.geometry_calls[-1], initial_geometry)
        self.assertNotIn("484x", window.geometry_calls[-1])

    def test_geometry_monitor_accepts_second_consecutive_width_regression_with_drift(self):
        root = _FakeRoot()
        window = _FakeWindow()
        spans_by_call = [
            [(0, 900), (1700, 1920)],
            [(0, 900), (1400, 1920)],
            [(0, 900), (1420, 1920)],
        ]
        occupied_calls = []

        def occupied_span_getter(width, height, work_area, geometry):
            index = min(len(occupied_calls), len(spans_by_call) - 1)
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return spans_by_call[index]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay._last_geometry_hard_resample_at = taskbar_overlay.time.monotonic()
        initial_geometry = window.geometry_calls[-1]
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][0]
        geometry_tick()
        self.assertEqual(window.geometry_calls, [initial_geometry])

        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][-1]
        geometry_tick()

        self.assertGreaterEqual(len(occupied_calls), 3)
        self.assertIn("504x", window.geometry_calls[-1])

    def test_geometry_monitor_accepts_no_slot_when_work_area_context_changes(self):
        root = _FakeRoot()
        window = _FakeWindow()
        occupied_calls = []
        work_areas = [
            (0, 0, 1920, 1040),
            (0, 0, 1920, 1000),
        ]
        spans_by_call = [
            [(0, 900), (1700, 1920)],
            [(0, 1920)],
        ]

        def work_area_getter():
            return work_areas[min(len(occupied_calls), len(work_areas) - 1)]

        def occupied_span_getter(width, height, work_area, geometry):
            index = min(len(occupied_calls), len(spans_by_call) - 1)
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return spans_by_call[index]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=work_area_getter,
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay._last_geometry_hard_resample_at = taskbar_overlay.time.monotonic()
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][0]
        geometry_tick()

        self.assertGreaterEqual(window.withdraw_calls, 1)
        self.assertIsNotNone(overlay._geometry_after_id)

    def test_geometry_monitor_pending_regression_does_not_bridge_context_refresh(self):
        root = _FakeRoot()
        window = _FakeWindow()
        work_area_calls = []
        occupied_calls = []
        work_areas = [
            (0, 0, 1920, 1040),
            (0, 0, 1920, 1040),
            (0, 0, 1920, 1000),
            (0, 0, 1920, 1000),
        ]
        spans_by_call = [
            [(0, 900), (1700, 1920)],
            [(0, 900), (1400, 1920)],
            [(0, 900), (1700, 1920)],
            [(0, 900), (1400, 1920)],
        ]

        def work_area_getter():
            index = min(len(work_area_calls), len(work_areas) - 1)
            work_area_calls.append(work_areas[index])
            return work_areas[index]

        def occupied_span_getter(width, height, work_area, geometry):
            index = min(len(occupied_calls), len(spans_by_call) - 1)
            occupied_calls.append((width, height, work_area, dict(geometry)))
            return spans_by_call[index]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=work_area_getter,
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay._last_geometry_hard_resample_at = taskbar_overlay.time.monotonic()
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][0]
        geometry_tick()
        self.assertNotIn("484x", window.geometry_calls[-1])

        overlay.refresh()
        refreshed_geometry = window.geometry_calls[-1]
        geometry_tick = [
            callback
            for _delay, callback in root.after_calls
            if callback.__name__ == "_geometry_monitor_tick"
        ][-1]
        geometry_tick()

        self.assertEqual(window.geometry_calls[-1], refreshed_geometry)
        self.assertNotIn("484x", window.geometry_calls[-1])

    def test_geometry_monitor_tick_excludes_current_overlay_span_from_live_sampling(self):
        root = _FakeRoot()
        window = _FakeWindow()
        sampled_geometries = []

        def occupied_span_getter(_width, _height, _work_area, geometry):
            sampled_geometries.append(dict(geometry))
            return [(0, 900), (1500, 1920)]

        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=occupied_span_getter,
        )

        overlay.refresh()
        overlay._last_geometry_hard_resample_at = taskbar_overlay.time.monotonic()
        with patch.object(
            taskbar_overlay,
            "_current_horizontal_window_span",
            return_value=(910, 1290),
        ):
            geometry_tick = [
                callback
                for _delay, callback in root.after_calls
                if callback.__name__ == "_geometry_monitor_tick"
            ][0]
            geometry_tick()

        self.assertGreaterEqual(len(sampled_geometries), 2)
        self.assertEqual(sampled_geometries[-1].get("_exclude_spans"), [(910, 1290)])
        self.assertEqual(window.withdraw_calls, 0)

    def test_keepalive_tick_reasserts_z_order_and_repaints_when_overlay_is_covered(self):
        root = _FakeRoot()
        window = _FakeWindow()
        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: None,
        )
        z_order_calls = []
        repaint_calls = []
        overlay._is_native_z_order_visible = lambda _window: False
        overlay._reassert_native_z_order = lambda target: z_order_calls.append(target)
        overlay._force_native_repaint = lambda target: repaint_calls.append(target)

        overlay.refresh()
        self.assertTrue(root.after_calls)
        z_order_calls.clear()
        repaint_calls.clear()
        root.after_calls[0][1]()

        self.assertEqual(len(window.draw_calls), 1)
        self.assertEqual(window.deiconify_calls, 1)
        self.assertEqual(window.lift_calls, 1)
        self.assertEqual(z_order_calls, [window])
        self.assertEqual(repaint_calls, [window])
        self.assertGreaterEqual(len(root.after_calls), 2)

    def test_keepalive_tick_reasserts_z_order_without_repaint_when_overlay_is_already_topmost(self):
        root = _FakeRoot()
        window = _FakeWindow()
        overlay = CodexUsageTaskbarOverlay(
            root,
            self._runtime,
            window_factory=lambda _root: window,
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda _width, _height, _work_area, _geometry: None,
        )
        z_order_calls = []
        repaint_calls = []
        overlay._is_native_z_order_visible = lambda _window: True
        overlay._reassert_native_z_order = lambda target: z_order_calls.append(target)
        overlay._force_native_repaint = lambda target: repaint_calls.append(target)

        overlay.refresh()
        z_order_calls.clear()
        repaint_calls.clear()
        root.after_calls[0][1]()

        self.assertEqual(z_order_calls, [window])
        self.assertEqual(repaint_calls, [])


if __name__ == "__main__":
    unittest.main()
