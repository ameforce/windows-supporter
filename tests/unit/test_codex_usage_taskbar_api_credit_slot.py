"""The API credit balance takes the compact row's amount slot (hotfix v0.37.1)."""

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
    return {"key": "API", "metric_key": "credit", "percent": None, "value_text": value_text}


class ApiCreditSlotModelTest(unittest.TestCase):
    def test_a_read_balance_takes_the_third_slot(self) -> None:
        metrics = metrics_of(profile(api_credit_balance="$12.34", api_credit_status="ok"))

        self.assertEqual([m.get("key") for m in metrics], ["5h", "7d", "API"])
        slot = metrics[-1]
        self.assertEqual((slot["metric_key"], slot["value_text"], slot["percent"]), ("credit", "$12.34", None))
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

    def test_the_chatgpt_credit_keeps_its_slot(self) -> None:
        metrics = metrics_of(
            profile(remaining_credit="227", api_credit_balance="$12.34", api_credit_status="ok")
        )

        self.assertEqual([m.get("key") for m in metrics], ["5h", "7d", "CR"])
        self.assertEqual({m.get("api_credit_text") for m in metrics}, {"$12.34"})

    def test_one_amount_slot_per_row_even_with_a_free_slot(self) -> None:
        metrics = metrics_of(
            profile(
                five_hour_limit="",
                remaining_credit="227",
                api_credit_balance="$12.34",
                api_credit_status="ok",
            )
        )

        self.assertEqual([m.get("key") for m in metrics].count("API"), 0)
        self.assertEqual([m.get("key") for m in metrics if m.get("metric_key") == "credit"], ["CR"])

    def test_three_reported_limits_leave_no_slot(self) -> None:
        metrics = metrics_of(
            profile(monthly_limit="80%", api_credit_balance="$12.34", api_credit_status="ok")
        )

        self.assertEqual([m.get("key") for m in metrics], ["5h", "7d", "1M"])

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
            if metric.get("metric_key") == "credit"
        ]
        self.assertTrue(segments)
        self.assertTrue(all(segment >= self._need("$1,234.56", "API") for segment in segments))

    def test_cr_rows_keep_their_offset(self) -> None:
        self.assertEqual(
            taskbar_overlay._credit_segment_width_for_text("62,493"),
            18 + _FakeFont().measure("62,493") + 2,
        )


if __name__ == "__main__":
    unittest.main()
