"""The API credit balance has its own amount slot in the compact row (v0.37.1, v0.37.2)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

import src.apps.codex_usage_taskbar_overlay as taskbar_overlay
from src.apps.codex_usage_taskbar_overlay import (
    CodexUsageTaskbarOverlay,
    build_codex_usage_taskbar_overlay_model,
)
from tests.unit.test_codex_usage_taskbar_credit_fit import (
    _FakeFont,
    _FakeRoot,
    _TextCanvas,
    _weekly,
    _weekly_with_guidance,
)


def profile(provider: str = "codex", **snapshot: Any) -> dict[str, Any]:
    return {
        "id": f"{provider}-1",
        "provider": provider,
        "label": "김종인",
        "enabled": True,
        "taskbar_selected": True,
        "runtime": {"monitor_state": "idle", "session_state": "logged_in", "collect_inflight": False},
        "last_snapshot": {"five_hour_limit": "47%", "weekly_limit": "52%", **snapshot},
    }


def metrics_of(*profiles: dict[str, Any]) -> list[dict[str, Any]]:
    model = build_codex_usage_taskbar_overlay_model({"enabled": True, "profiles": list(profiles)})
    return model["bars"][0]["metrics"]


def api(value_text: str) -> dict[str, Any]:
    return {"key": "API", "metric_key": "api_credit", "percent": None, "value_text": value_text}


def cr(value_text: str) -> dict[str, Any]:
    return {"key": "CR", "metric_key": "credit", "percent": None, "value_text": value_text}


AMOUNT_KEYS = ("credit", "api_credit")


class ApiCreditSlotModelTest(unittest.TestCase):
    def test_a_read_balance_takes_the_third_slot(self) -> None:
        metrics = metrics_of(profile(api_credit_balance="$12.34", api_credit_status="ok"))

        self.assertEqual([m.get("key") for m in metrics], ["5h", "7d", "API"])
        slot = metrics[-1]
        self.assertEqual((slot["metric_key"], slot["value_text"], slot["percent"]), ("api_credit", "$12.34", None))
        self.assertNotIn("api_credit_text", slot)
        self.assertEqual({m.get("api_credit_text") for m in metrics[:2]}, {"$12.34"})

    def test_negative_and_zero_balances_are_shown_as_read(self) -> None:
        for balance in ("-$5.00", "$0.00"):
            with self.subTest(balance=balance):
                metrics = metrics_of(profile(api_credit_balance=balance, api_credit_status="ok"))
                self.assertEqual(metrics[-1]["value_text"], balance)

    def test_no_slot_without_a_current_read(self) -> None:
        for snapshot in (
            {"api_credit_balance": "$12.34", "api_credit_status": "login_required"},
            {"api_credit_balance": "$12.34", "api_credit_status": "unavailable"},
            {"api_credit_status": "login_required"},
            {"api_credit_balance": "lots", "api_credit_status": "ok"},
            {},
        ):
            with self.subTest(snapshot=snapshot):
                keys = [m.get("key") for m in metrics_of(profile(**snapshot))]
                self.assertEqual(keys, ["5h", "7d"])

    def test_no_room_when_the_three_drawn_slots_are_taken(self) -> None:
        metrics = metrics_of(
            profile(remaining_credit="227", api_credit_balance="$12.34", api_credit_status="ok")
        )

        self.assertEqual([m.get("key") for m in metrics], ["5h", "7d", "CR"])
        self.assertEqual({m.get("api_credit_text") for m in metrics}, {"$12.34"})

    def test_an_unreported_limit_still_takes_a_drawn_slot(self) -> None:
        metrics = metrics_of(
            profile(
                five_hour_limit="",
                remaining_credit="227",
                api_credit_balance="$12.34",
                api_credit_status="ok",
            )
        )

        self.assertEqual([m.get("key") for m in metrics], ["5h", "7d", "CR"])

    def test_api_slot_sits_next_to_the_chatgpt_credit(self) -> None:
        # The live runtime passes reported metrics as descriptors: a weekly-only
        # account with ChatGPT credit leaves the third drawn slot free.
        codex = profile(remaining_credit="66585", api_credit_balance="$140.00", api_credit_status="ok")
        codex["metrics"] = [
            {"key": "weekly_limit", "short_label": "7d", "percent": 68, "value_text": "68%", "state": "ready"}
        ]

        metrics = metrics_of(codex)

        self.assertEqual(
            [(m.get("key"), m.get("metric_key")) for m in metrics],
            [("7d", "weekly_limit"), ("CR", "credit"), ("API", "api_credit")],
        )
        self.assertEqual(metrics[-1]["value_text"], "$140.00")

    def test_three_reported_limits_leave_no_slot(self) -> None:
        metrics = metrics_of(
            profile(monthly_limit="80%", api_credit_balance="$12.34", api_credit_status="ok")
        )

        self.assertEqual([m.get("key") for m in metrics], ["5h", "7d", "1M"])

    def test_no_slot_beyond_the_three_drawn_segments(self) -> None:
        metrics = metrics_of(
            profile(
                five_hour_limit="",
                monthly_limit="80%",
                api_credit_balance="$12.34",
                api_credit_status="ok",
            )
        )

        self.assertEqual([m.get("key") for m in metrics], ["5h", "7d", "1M"])
        self.assertEqual({m.get("api_credit_text") for m in metrics}, {"$12.34"})

    def test_other_providers_never_get_the_slot(self) -> None:
        cursor = profile("cursor", api_credit_balance="$12.34", api_credit_status="ok")
        cursor["metrics"] = [
            {"key": "included_usage", "short_label": "INC", "percent": 40, "value_text": "40%", "state": "ready"}
        ]

        self.assertNotIn("API", [m.get("key") for m in metrics_of(cursor)])

    def test_a_new_balance_changes_the_render_signature(self) -> None:
        def signature(balance: str) -> Any:
            model = build_codex_usage_taskbar_overlay_model(
                {"enabled": True, "profiles": [profile(api_credit_balance=balance, api_credit_status="ok")]}
            )
            return taskbar_overlay._overlay_render_signature(model)

        self.assertNotEqual(signature("$12.34"), signature("$11.00"))
        self.assertEqual(signature("$12.34"), signature("$12.34"))

    def test_slot_tooltip_names_the_api_balance(self) -> None:
        slot = metrics_of(profile(api_credit_balance="$12.34", api_credit_status="ok"))[-1]

        self.assertEqual(
            taskbar_overlay._build_metric_tooltip_text(slot, "김종인"),
            "API 크레딧 $12.34\n프로필: 김종인",
        )


class ApiCreditSlotLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self._context = patch.dict(
            taskbar_overlay._TK_MEASURE_CONTEXT,
            {"root": object(), "fonts": {pt: _FakeFont() for pt in (6, 7, 8)}},
        )
        self._context.start()
        taskbar_overlay._set_overlay_text_width_scale(1.0)
        taskbar_overlay._clear_overlay_width_caches()

    def tearDown(self) -> None:
        self._context.stop()
        taskbar_overlay._clear_overlay_width_caches()

    @staticmethod
    def _need(text: str, label: str) -> int:
        return max(18, _FakeFont().measure(label) + 3) + _FakeFont().measure(text) + 2

    def test_dollar_spellings_keep_the_sign_and_symbol(self) -> None:
        cases = {
            "$1,234.56": ("$1,234.56", "$1.2K", "$1K"),
            "-$12,345.00": ("-$12,345.00", "-$12.3K", "-$12K"),
            "$0,000.00": ("$0,000.00", "$0.0K", "$0K"),
            "$12.34": ("$12.34",),
            "-$5.00": ("-$5.00",),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(taskbar_overlay._credit_value_spellings(value), expected)

    def test_the_amount_starts_after_the_wider_api_label(self) -> None:
        self.assertEqual(taskbar_overlay._credit_value_offset("$12.34"), _FakeFont().measure("API") + 3)
        self.assertEqual(taskbar_overlay._credit_value_offset("227"), 18)
        self.assertEqual(taskbar_overlay._credit_value_offset("$00.00"), taskbar_overlay._credit_value_offset("$12.34"))

    def test_draw_writes_the_label_then_the_amount_past_it(self) -> None:
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), lambda: {"enabled": True, "profiles": []})
        canvas = _TextCanvas()

        overlay._draw_metric_segment(canvas, api("$12.34"), 10, 2, 120, 15)

        texts = canvas.texts()
        self.assertEqual(texts[0][1], "API")
        self.assertEqual(texts[-1][1], "$12.34")
        self.assertEqual(texts[-1][0][0], 10 + _FakeFont().measure("API") + 3)
        self.assertFalse([op for op in canvas.ops if op[0] == "rectangle"], "no progress bar")

    def test_cramped_slot_draws_a_shorter_dollar_spelling(self) -> None:
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), lambda: {"enabled": True, "profiles": []})
        for width, expected in (
            (self._need("$1,234.56", "API"), "$1,234.56"),
            (self._need("$1.2K", "API"), "$1.2K"),
            (20, "$1K"),
        ):
            with self.subTest(width=width):
                canvas = _TextCanvas()
                overlay._draw_metric_segment(canvas, api("$1,234.56"), 0, 0, width, 15)
                self.assertEqual(canvas.texts()[-1][1], expected)

    def test_preferred_pane_width_holds_the_whole_balance(self) -> None:
        rows = [(_weekly(7, "3d 18h 53m"), api("$1,234.56")), (_weekly(0, "3d 11h 02m"),)]
        signature = tuple(
            tuple(taskbar_overlay._metric_width_signature(metric) for metric in row) for row in rows
        )
        labels = ("Codex 1", "Codex 2")
        preferred = taskbar_overlay._preferred_width_for_rows_cached(
            signature, labels, require_status_text=True, require_detail=True
        )

        segments = [
            int(layout.segment_geometry(index)[1])
            for layout in taskbar_overlay._metric_rows_layout_for_overlay_width(
                preferred, rows, profile_labels=labels
            )
            for index, metric in enumerate(layout.visible_metrics)
            if metric.get("metric_key") in AMOUNT_KEYS
        ]
        self.assertTrue(segments)
        self.assertTrue(all(segment >= self._need("$1,234.56", "API") for segment in segments))

    def _amount_segments(self, width: int, rows, labels) -> list[tuple[str, str, int, int]]:
        """(label, drawn amount, amount right edge, segment width) per amount segment."""
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), lambda: {"enabled": True, "profiles": []})
        drawn = []
        for layout in taskbar_overlay._metric_rows_layout_for_overlay_width(width, rows, profile_labels=labels):
            for index, metric in enumerate(layout.visible_metrics):
                if metric.get("metric_key") not in AMOUNT_KEYS:
                    continue
                _offset, segment, _progress = layout.segment_geometry(index)
                canvas = _TextCanvas()
                overlay._draw_metric_segment(canvas, metric, 0, 0, int(segment), 15)
                texts = canvas.texts()
                (x, _y), text = texts[-1]
                drawn.append((texts[0][1], text, x + _FakeFont().measure(text) + 2, int(segment)))
        return drawn

    def test_cr_and_api_columns_both_draw_their_whole_amounts(self) -> None:
        rows = [
            (_weekly(68, "5d 06h 00m"), cr("66,585"), api("$140.00")),
            (_weekly(12, "2d 11h 02m"), cr("775")),
        ]
        signature = tuple(
            tuple(taskbar_overlay._metric_width_signature(metric) for metric in row) for row in rows
        )
        labels = ("Daeng - enmsoftware", "Codex 2")
        preferred = taskbar_overlay._preferred_width_for_rows_cached(
            signature, labels, require_status_text=True, require_detail=True
        )

        drawn = self._amount_segments(preferred, rows, labels)

        self.assertEqual(
            sorted((label, text) for label, text, _right, _segment in drawn),
            [("API", "$140.00"), ("CR", "66,585"), ("CR", "775")],
        )
        for label, text, right, segment in drawn:
            with self.subTest(label=label, amount=text):
                self.assertLessEqual(right, segment)

    def test_cr_column_comes_before_api_whatever_the_row_order(self) -> None:
        rows = [(_weekly(12, "2d 11h 02m"), api("$140.00")), (_weekly(68, "5d 06h 00m"), cr("775"))]

        self.assertEqual(
            taskbar_overlay._metric_slot_keys([tuple(row) for row in rows]),
            ["weekly_limit", "credit", "api_credit"],
        )

    def test_from_the_compact_width_up_both_amounts_draw_whole(self) -> None:
        # The middle band grants each amount column its whole amount before any
        # guidance, so neither the CR nor the API amount is shortened there.
        rows = [
            (_weekly_with_guidance(7, "3d 18h 53m"), cr("62,493"), api("$1,234.56")),
            (_weekly_with_guidance(0, "3d 11h 02m"),),
        ]
        signature = tuple(
            tuple(taskbar_overlay._metric_width_signature(metric) for metric in row) for row in rows
        )
        labels = ("Codex 8", "Codex 1")
        compact = taskbar_overlay._compact_preferred_width_for_rows_cached(signature, labels)
        preferred = taskbar_overlay._preferred_width_for_rows_cached(
            signature, labels, require_status_text=True, require_detail=True
        )
        self.assertLess(compact, preferred)
        for width in range(compact, preferred + 40, 2):
            for label, text, right, segment in self._amount_segments(width, rows, labels):
                with self.subTest(width=width, label=label):
                    self.assertIn(text, ("62,493", "$1,234.56"))
                    self.assertLessEqual(right, segment)

    def test_two_amount_columns_keep_their_own_slot_keys(self) -> None:
        rows = [(_weekly(68, "5d 06h 00m"), cr("66,585"), api("$140.00"))]

        self.assertEqual(
            taskbar_overlay._metric_slot_keys([tuple(rows[0])]),
            ["weekly_limit", "credit", "api_credit"],
        )
        layout = taskbar_overlay._metric_rows_layout_for_overlay_width(520, rows)[0]
        offsets = [layout.segment_geometry(index)[0] for index in range(3)]
        self.assertEqual(offsets, sorted(set(offsets)), "three distinct columns")

    def test_cr_rows_keep_their_offset(self) -> None:
        self.assertEqual(
            taskbar_overlay._credit_segment_width_for_text("62,493"),
            18 + _FakeFont().measure("62,493") + 2,
        )


if __name__ == "__main__":
    unittest.main()
