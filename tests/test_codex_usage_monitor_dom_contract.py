import re
import unittest
from pathlib import Path

from src.apps.codex_usage_monitor import (
    extract_usage_metrics_from_semantic_blocks,
    extract_usage_reset_info_from_semantic_blocks,
)


FIXTURE_PATH = Path(__file__).resolve().parent / "e2e" / "fixtures" / "codex-usage-page-current.html"


def _build_metric_blocks_from_fixture(html_text: str) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    pattern = re.compile(
        r"<article[^>]*>\s*<h2>(?P<label>[^<]+)</h2>\s*<p>(?P<value>[^<]+)</p>\s*</article>",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in pattern.finditer(html_text):
        label = str(match.group("label") or "").strip()
        value = str(match.group("value") or "").strip()
        blocks.append(
            {
                "label_text": label,
                "block_text": f"{label} {value}",
                "value_candidates": [value],
            }
        )
    return blocks


class CodexUsageMonitorDomContractTest(unittest.TestCase):
    def test_semantic_dom_contract_extracts_all_five_metrics_from_current_usage_fixture(self) -> None:
        html_text = FIXTURE_PATH.read_text(encoding="utf-8")
        blocks = _build_metric_blocks_from_fixture(html_text)

        parsed = extract_usage_metrics_from_semantic_blocks(blocks)

        self.assertEqual(parsed.get("five_hour_limit"), "80%")
        self.assertEqual(parsed.get("weekly_limit"), "68%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "83%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "95%")
        self.assertEqual(parsed.get("remaining_credit"), "903")

    def test_semantic_dom_contract_pairs_label_and_value_within_same_metric_block(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "label_text": "5-hour usage limit",
                    "block_text": "5-hour usage limit 80%",
                    "value_candidates": ["80%"],
                },
                {
                    "label_text": "weekly usage limit",
                    "block_text": "weekly usage limit 68%",
                    "value_candidates": ["68%"],
                },
                {
                    "label_text": "gpt-5.3-codex-spark 5-hour usage limit",
                    "block_text": "gpt-5.3-codex-spark 5-hour usage limit 83%",
                    "value_candidates": ["83%"],
                },
                {
                    "label_text": "gpt-5.3-codex-spark weekly usage limit",
                    "block_text": "gpt-5.3-codex-spark weekly usage limit 95%",
                    "value_candidates": ["95%"],
                },
            ]
        )

        self.assertEqual(parsed.get("five_hour_limit"), "80%")
        self.assertEqual(parsed.get("weekly_limit"), "68%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "83%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "95%")

    def test_semantic_dom_contract_ignores_orphan_value_outside_metric_block(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "label_text": "Random number",
                    "block_text": "999",
                    "value_candidates": ["999"],
                }
            ]
        )

        self.assertEqual(parsed, {})

    def test_semantic_dom_contract_preserves_existing_metric_keys_only(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "label_text": "remaining credit",
                    "block_text": "remaining credit 903",
                    "value_candidates": ["903"],
                },
                {
                    "metric_key": "bonus_credit",
                    "label_text": "bonus credit",
                    "block_text": "bonus credit 50",
                    "value_candidates": ["50"],
                },
            ]
        )

        self.assertEqual(parsed.get("remaining_credit"), "903")
        self.assertNotIn("bonus_credit", parsed)

    def test_semantic_dom_contract_extracts_limit_reset_times(self) -> None:
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [
                {
                    "label_text": "5-hour usage limit",
                    "block_text": "5-hour usage limit 80% Resets at 2026-05-06T18:30:00+09:00",
                    "reset_at_candidates": ["2026-05-06T18:30:00+09:00"],
                },
                {
                    "label_text": "weekly usage limit",
                    "block_text": "weekly usage limit 68% Resets at 2026-05-11T00:00:00+09:00",
                    "reset_at_candidates": ["2026-05-11T00:00:00+09:00"],
                },
                {
                    "label_text": "gpt-5.3-codex-spark 5-hour usage limit",
                    "block_text": "gpt-5.3-codex-spark 5-hour usage limit 79% 오후 12:08 초기화",
                    "reset_candidates": ["gpt-5.3-codex-spark 5-hour usage limit 79% 오후 12:08 초기화"],
                },
                {
                    "label_text": "gpt-5.3-codex-spark weekly usage limit",
                    "block_text": "gpt-5.3-codex-spark weekly usage limit 74% 2026. 5. 11. 오후 12:14 초기화",
                    "reset_candidates": ["2026. 5. 11. 오후 12:14 초기화"],
                },
            ],
            captured_at="2026-05-06T11:30:10+09:00",
        )

        self.assertEqual(parsed.get("five_hour_limit_reset_at"), "2026-05-06T18:30:00+09:00")
        self.assertEqual(parsed.get("weekly_limit_reset_at"), "2026-05-11T00:00:00+09:00")
        self.assertEqual(
            parsed.get("gpt_5_3_codex_spark_five_hour_limit_reset_at"),
            "2026-05-06T12:08:00+09:00",
        )
        self.assertEqual(
            parsed.get("gpt_5_3_codex_spark_weekly_limit_reset_at"),
            "2026-05-11T12:14:00+09:00",
        )

    def test_semantic_dom_contract_does_not_treat_metric_label_as_relative_reset(self) -> None:
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [
                {
                    "label_text": "5-hour usage limit",
                    "block_text": "5-hour usage limit 24% 남음 오후 12:05 초기화",
                    "reset_candidates": ["5시간 사용 한도 24% 남음 오후 12:05 초기화"],
                }
            ],
            captured_at="2026-05-06T11:30:10+09:00",
        )

        self.assertEqual(parsed.get("five_hour_limit_reset_at"), "2026-05-06T12:05:00+09:00")

    def test_semantic_dom_contract_uses_block_text_when_reset_candidates_are_missing(self) -> None:
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [
                {
                    "label_text": "5-hour usage limit",
                    "block_text": "5-hour usage limit 17% 오후 12:05 초기화",
                }
            ],
            captured_at="2026-05-06T11:48:23+09:00",
        )

        self.assertEqual(parsed.get("five_hour_limit_reset_at"), "2026-05-06T12:05:00+09:00")

    def test_semantic_dom_contract_does_not_copy_weekly_reset_from_broad_block_to_five_hour(
        self,
    ) -> None:
        broad_block = (
            "5 hour usage limit 100% remaining "
            "weekly usage limit 0% remaining Resets Jun 7, 2026 11:39 PM"
        )
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [
                {
                    "label_text": "5 hour usage limit",
                    "block_text": broad_block,
                    "reset_candidates": ["Resets Jun 7, 2026 11:39 PM"],
                },
                {
                    "label_text": "weekly usage limit",
                    "block_text": broad_block,
                    "reset_candidates": ["Resets Jun 7, 2026 11:39 PM"],
                },
            ],
            captured_at="2026-06-03T20:48:37+09:00",
        )

        self.assertNotIn("five_hour_limit_reset_at", parsed)
        self.assertEqual(parsed.get("weekly_limit_reset_at"), "2026-06-07T23:39:00+09:00")

    def test_semantic_dom_contract_rejects_day_scale_five_hour_reset_candidate(self) -> None:
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [
                {
                    "label_text": "5 hour usage limit",
                    "block_text": "5 hour usage limit 100% remaining Resets Jun 7, 2026 11:39 PM",
                    "reset_candidates": ["Resets Jun 7, 2026 11:39 PM"],
                },
                {
                    "label_text": "weekly usage limit",
                    "block_text": "weekly usage limit 0% remaining Resets Jun 7, 2026 11:39 PM",
                    "reset_candidates": ["Resets Jun 7, 2026 11:39 PM"],
                },
            ],
            captured_at="2026-06-03T20:48:37+09:00",
        )

        self.assertNotIn("five_hour_limit_reset_at", parsed)
        self.assertEqual(parsed.get("weekly_limit_reset_at"), "2026-06-07T23:39:00+09:00")

    def test_semantic_dom_contract_uses_nearby_reset_candidate_when_boundary_has_no_reset(self) -> None:
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [
                {
                    "label_text": "5-hour usage limit",
                    "block_text": "5-hour usage limit 8%",
                    "reset_candidates": ["오후 12:05 초기화"],
                }
            ],
            captured_at="2026-05-06T11:48:23+09:00",
        )

        self.assertEqual(parsed.get("five_hour_limit_reset_at"), "2026-05-06T12:05:00+09:00")

    def test_semantic_dom_contract_extracts_english_time_only_reset_times(self) -> None:
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [
                {
                    "label_text": "5 hour usage limit 96% remaining Resets 5:05 PM",
                    "block_text": "5 hour usage limit 96% remaining Resets 5:05 PM",
                    "reset_candidates": [
                        "5 hour usage limit 96% remaining Resets 5:05 PM",
                        "Resets 5:05 PM",
                    ],
                },
                {
                    "label_text": "GPT-5.3-Codex-Spark 5 hour usage limit 98% remaining Resets 5:11 PM",
                    "block_text": (
                        "GPT-5.3-Codex-Spark 5 hour usage limit "
                        "98% remaining Resets 5:11 PM"
                    ),
                    "reset_candidates": [
                        "GPT-5.3-Codex-Spark 5 hour usage limit 98% remaining Resets 5:11 PM",
                        "Resets 5:11 PM",
                    ],
                },
            ],
            captured_at="2026-05-06T12:16:43+09:00",
        )

        self.assertEqual(parsed.get("five_hour_limit_reset_at"), "2026-05-06T17:05:00+09:00")
        self.assertEqual(
            parsed.get("gpt_5_3_codex_spark_five_hour_limit_reset_at"),
            "2026-05-06T17:11:00+09:00",
        )

    def test_semantic_dom_contract_rolls_english_time_only_reset_to_next_day(self) -> None:
        parsed = extract_usage_reset_info_from_semantic_blocks(
            [
                {
                    "label_text": "5 hour usage limit",
                    "block_text": "5 hour usage limit 96% remaining Resets 11:59 AM",
                    "reset_candidates": ["Resets 11:59 AM"],
                }
            ],
            captured_at="2026-05-06T12:16:43+09:00",
        )

        self.assertEqual(parsed.get("five_hour_limit_reset_at"), "2026-05-07T11:59:00+09:00")


if __name__ == "__main__":
    unittest.main()
