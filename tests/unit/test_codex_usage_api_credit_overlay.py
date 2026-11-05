"""The taskbar overlay shows the API credit balance in the hover tooltip only."""

from __future__ import annotations

import unittest
from copy import deepcopy
from typing import Any

import src.apps.codex_usage_taskbar_overlay as taskbar_overlay
from src.apps.codex_usage_taskbar_overlay import build_codex_usage_taskbar_overlay_model


def profile(provider: str = "codex", **snapshot: Any) -> dict[str, Any]:
    return {
        "id": f"{provider}-1",
        "provider": provider,
        "label": "김종인",
        "enabled": True,
        "taskbar_selected": True,
        "runtime": {"monitor_state": "idle", "session_state": "logged_in", "collect_inflight": False},
        "last_snapshot": {
            "five_hour_limit": "47%",
            "weekly_limit": "52%",
            "remaining_credit": "227",
            **snapshot,
        },
    }


def model_for(*profiles: dict[str, Any]) -> dict[str, Any]:
    return build_codex_usage_taskbar_overlay_model({"enabled": True, "profiles": list(profiles)})


class ApiCreditOverlayTest(unittest.TestCase):
    def test_every_segment_of_a_codex_row_carries_the_balance(self) -> None:
        model = model_for(profile(api_credit_balance="$12.34", api_credit_status="ok"))

        metrics = model["bars"][0]["metrics"]
        self.assertGreaterEqual(len(metrics), 3, "5H, 7D and the credit segment")
        self.assertEqual({metric.get("api_credit_text") for metric in metrics}, {"$12.34"})

    def test_a_failed_read_qualifies_the_last_balance(self) -> None:
        model = model_for(
            profile(api_credit_balance="$12.34", api_credit_status="login_required")
        )

        texts = {metric.get("api_credit_text") for metric in model["bars"][0]["metrics"]}
        self.assertEqual(texts, {"$12.34 (이전 값, 로그인 필요)"})

    def test_a_login_problem_without_any_balance_is_still_explained(self) -> None:
        model = model_for(profile(api_credit_status="login_required"))

        texts = {metric.get("api_credit_text") for metric in model["bars"][0]["metrics"]}
        self.assertEqual(texts, {"로그인 필요"})

    def test_rows_without_a_reading_are_unchanged(self) -> None:
        plain = model_for(profile())
        again = model_for(profile(api_credit_balance="", api_credit_status=""))

        self.assertEqual(plain, again)
        for metric in plain["bars"][0]["metrics"]:
            self.assertNotIn("api_credit_text", metric)

    def test_other_providers_never_show_a_codex_balance(self) -> None:
        cursor = profile("cursor", api_credit_balance="$12.34", api_credit_status="ok")
        cursor["metrics"] = [
            {
                "key": "included_usage",
                "short_label": "INC",
                "percent": 40,
                "value_text": "40%",
                "state": "ready",
            }
        ]

        model = model_for(cursor)

        for metric in model["bars"][0]["metrics"]:
            self.assertNotIn("api_credit_text", metric)

    def test_the_balance_does_not_change_any_layout_input(self) -> None:
        with_balance = model_for(profile(api_credit_balance="$12.34", api_credit_status="ok"))
        without = model_for(profile())

        stripped = deepcopy(with_balance)
        for metric in stripped["bars"][0]["metrics"]:
            metric.pop("api_credit_text")
        self.assertEqual(stripped, without)
        for with_metric, plain_metric in zip(
            with_balance["bars"][0]["metrics"], without["bars"][0]["metrics"], strict=True
        ):
            self.assertEqual(
                taskbar_overlay._metric_width_signature(with_metric),
                taskbar_overlay._metric_width_signature(plain_metric),
            )

    def test_a_new_balance_changes_the_render_signature_so_the_tooltip_refreshes(self) -> None:
        before = model_for(profile(api_credit_balance="$12.34", api_credit_status="ok"))
        after = model_for(profile(api_credit_balance="$11.00", api_credit_status="ok"))
        unchanged = model_for(profile(api_credit_balance="$12.34", api_credit_status="ok"))

        signature = taskbar_overlay._overlay_render_signature
        self.assertNotEqual(signature(before), signature(after))
        self.assertEqual(signature(before), signature(unchanged))

    def test_tooltip_lists_the_balance_before_the_profile(self) -> None:
        metric = {
            "key": "7d",
            "metric_key": "weekly_limit",
            "value_text": "46%",
            "reset_text": "00d 06h 51m 00s",
            "normal_guidance_text": "N 90% / 2d 20h",
            "reset_badge_label": "부족",
            "api_credit_text": "$12.34",
        }

        text = taskbar_overlay._build_metric_tooltip_text(metric, "김종인")

        self.assertEqual(
            text,
            "주간 한도 46% (부족)\n"
            "리셋까지 00d 06h 51m 00s\n"
            "정상 범위 N 90% / 2d 20h\n"
            "API 크레딧 $12.34\n"
            "프로필: 김종인",
        )
        credit = {"key": "CR", "metric_key": "credit", "value_text": "227", "api_credit_text": "$1.00"}
        self.assertEqual(
            taskbar_overlay._build_metric_tooltip_text(credit, "김종인"),
            "크레딧 227\nAPI 크레딧 $1.00\n프로필: 김종인",
        )

    def test_tooltip_without_a_balance_keeps_the_existing_text(self) -> None:
        credit = {"key": "CR", "metric_key": "credit", "value_text": "227"}

        self.assertEqual(
            taskbar_overlay._build_metric_tooltip_text(credit, "김종인"),
            "크레딧 227\n프로필: 김종인",
        )


if __name__ == "__main__":
    unittest.main()
