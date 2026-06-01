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
        self.assertEqual(reset_texts, ["4h 12m", "5h 59m", "45m", "6h 7m"])
        self.assertEqual(reset_short_texts, ["4h", "5h", "45m", "6h"])
        self.assertEqual(reset_colors, ["#ef4444", "#ef4444", "#f59e0b", "#22c55e"])
        self.assertEqual(reset_states, ["urgent", "urgent", "warning", "stable"])

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
        self.assertEqual([metric["reset_text"] for metric in first_metrics], ["3h 5m", "3h 5m"])
        self.assertEqual(
            [metric["reset_short_text"] for metric in first_metrics],
            ["3h", "3h"],
        )
        self.assertEqual(
            [metric["reset_color"] for metric in first_metrics],
            ["#22c55e", "#f59e0b"],
        )
        self.assertEqual(
            [metric["reset_state"] for metric in first_metrics],
            ["stable", "warning"],
        )

    def test_model_marks_elapsed_reset_time_as_overdue(self):
        runtime = self._runtime()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        runtime["accounts"][0]["last_snapshot"]["five_hour_limit_reset_at"] = (
            "2026-06-01T09:59:00+09:00"
        )

        model = build_codex_usage_taskbar_overlay_model(runtime, now=now)

        first_metric = model["bars"][0]["metrics"][0]
        self.assertEqual(first_metric["reset_text"], "now")
        self.assertEqual(first_metric["reset_color"], "#ef4444")

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
                "width": 348,
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
        self.assertTrue(status_texts)
        self.assertTrue(first_metric_labels)
        self.assertLessEqual(status_texts[0][1][0] - label_texts[0][1][0], 58)
        self.assertGreaterEqual(first_metric_labels[0][1][0] - status_texts[0][1][0], 32)

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
        for track, value in zip(track_rects[:4], value_texts[:4], strict=True):
            self.assertGreaterEqual(value[1][0] - track[1][2], 26)
            self.assertLessEqual(value[1][0] - track[1][2], 32)
        self.assertGreaterEqual(first_weekly_labels[0][1][0] - value_texts[0][1][0], 18)

    def test_draw_metric_segment_shows_reset_time_only_when_space_allows(self):
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), self._runtime)
        metric = {
            "key": "5h",
            "percent": 73,
            "value_text": "73%",
            "color": "#22c55e",
            "reset_text": "3h 12m",
            "reset_short_text": "3h",
            "reset_color": "#22c55e",
        }
        wide_canvas = _FakeCanvas()
        compact_canvas = _FakeCanvas()
        narrow_canvas = _FakeCanvas()

        overlay._draw_metric_segment(wide_canvas, metric, 10, 2, 124, 15)
        overlay._draw_metric_segment(compact_canvas, metric, 10, 2, 110, 15)
        overlay._draw_metric_segment(narrow_canvas, metric, 10, 2, 90, 15)

        wide_value = [
            op for op in wide_canvas.ops if op[0] == "text" and op[2].get("text") == "73%"
        ][0]
        wide_reset = [
            op for op in wide_canvas.ops if op[0] == "text" and op[2].get("text") == "3h 12m"
        ][0]
        compact_detail_reset = [
            op for op in compact_canvas.ops if op[0] == "text" and op[2].get("text") == "3h 12m"
        ]
        compact_short_reset = [
            op for op in compact_canvas.ops if op[0] == "text" and op[2].get("text") == "3h"
        ]
        narrow_reset = [
            op for op in narrow_canvas.ops if op[0] == "text" and op[2].get("text") in {"3h 12m", "3h"}
        ]
        self.assertGreater(wide_reset[1][0], wide_value[1][0])
        self.assertLessEqual(wide_reset[1][0] - wide_value[1][0], 6)
        self.assertEqual(wide_reset[2].get("anchor"), "w")
        self.assertEqual(wide_reset[2].get("fill"), "#22c55e")
        self.assertEqual(compact_detail_reset, [])
        self.assertTrue(compact_short_reset)
        self.assertEqual(narrow_reset, [])

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
        self.assertEqual(weekly_metric["reset_text"], "6d 6h 7m")
        self.assertEqual(weekly_metric["reset_short_text"], "6d")

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
            "reset_text": "now",
            "reset_short_text": "now",
            "reset_color": "#ef4444",
        }

        overlay._draw_metric_segment(canvas, first_metric, 10, 2, 124, 15)
        overlay._draw_metric_segment(canvas, second_metric, 10, 20, 124, 15)

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
            if op[0] == "text" and op[2].get("text") in {"5m", "now"}
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

    def test_refresh_updates_changed_metric_without_flash_timer(self):
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
        self.assertFalse(changed_metrics[1]["flash"])
        self.assertTrue(root.after_calls)
        self.assertTrue(
            all(call[1].__name__ == "_keepalive_tick" for call in root.after_calls)
        )

    def test_refresh_marks_changed_metric_without_static_highlight(self):
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
        self.assertFalse(changed_metrics[1]["flash"])
        self.assertFalse(changed_metrics[1]["flash_phase"])

    def test_uniform_taskbar_background_column_is_not_treated_as_occupied(self):
        colors = [(118, 84, 154), (118, 84, 154), (118, 84, 154)]

        self.assertFalse(_column_looks_occupied(colors, (24, 24, 24)))

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

    def test_bottom_taskbar_geometry_uses_widest_unoccupied_slot(self):
        geometry = calculate_taskbar_overlay_geometry(
            1000,
            600,
            (0, 0, 1000, 560),
            occupied_spans=[(0, 180), (430, 710), (900, 1000)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertGreaterEqual(geometry["x"], 180)
        self.assertLessEqual(geometry["x"] + geometry["width"], 430)
        self.assertLess(geometry["width"], 640)
        self.assertGreaterEqual(geometry["width"], 220)

    def test_bottom_taskbar_geometry_prefers_right_empty_slot_over_wider_left_slot(self):
        geometry = calculate_taskbar_overlay_geometry(
            1200,
            600,
            (0, 0, 1200, 560),
            occupied_spans=[(0, 120), (520, 720), (980, 1200)],
        )

        self.assertTrue(geometry["visible"])
        self.assertEqual(geometry["orientation"], "bottom")
        self.assertGreaterEqual(geometry["x"], 720)
        self.assertLessEqual(geometry["x"] + geometry["width"], 980)
        self.assertGreaterEqual(geometry["width"], 220)

    def test_bottom_taskbar_geometry_hides_when_no_empty_slot_can_fit(self):
        geometry = calculate_taskbar_overlay_geometry(
            1000,
            600,
            (0, 0, 1000, 560),
            occupied_spans=[(0, 260), (270, 520), (530, 790), (800, 1000)],
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
