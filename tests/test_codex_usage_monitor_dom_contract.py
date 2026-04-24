import re
import unittest
from pathlib import Path

from src.apps.codex_usage_monitor import extract_usage_metrics_from_semantic_blocks


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


if __name__ == "__main__":
    unittest.main()
