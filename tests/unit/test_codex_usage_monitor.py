import unittest

from src.apps.codex_usage_monitor import (
    UsageSnapshot,
    are_equivalent_codex_usage_urls,
    build_codex_login_entry_url,
    canonicalize_codex_usage_url,
    compute_usage_changes,
    extract_usage_metrics_from_semantic_blocks,
    merge_snapshot_with_previous,
    normalize_usage_value,
    parse_usage_metrics_from_text,
)


class CodexUsageMonitorUnitTest(unittest.TestCase):
    def test_canonicalize_codex_usage_url_promotes_legacy_usage_path_to_analytics_hash(self) -> None:
        self.assertEqual(
            canonicalize_codex_usage_url("https://chatgpt.com/codex/settings/usage"),
            "https://chatgpt.com/codex/cloud/settings/analytics#usage",
        )

    def test_build_codex_login_entry_url_targets_analytics_hash_path(self) -> None:
        self.assertEqual(
            build_codex_login_entry_url("https://chatgpt.com/codex/settings/usage"),
            "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
        )

    def test_build_codex_login_entry_url_preserves_analytics_fragment_for_direct_input(self) -> None:
        self.assertEqual(
            build_codex_login_entry_url(
                "https://chatgpt.com/codex/cloud/settings/analytics#usage"
            ),
            "https://chatgpt.com/auth/login?next=/codex/cloud/settings/analytics%23usage",
        )

    def test_are_equivalent_codex_usage_urls_treats_fragmentless_analytics_variant_as_same_target(self) -> None:
        self.assertTrue(
            are_equivalent_codex_usage_urls(
                "https://chatgpt.com/codex/cloud/settings/analytics",
                "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            )
        )

    def test_normalize_usage_value_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_usage_value("""  12 / 40 

  left """),
            "12 / 40 left",
        )

    def test_parse_usage_metrics_from_inline_lines(self) -> None:
        raw = """
        5시간 사용 한도: 12 / 40
        주간 사용 한도: 111 / 300
        gpt-5.3-codex-spark 5시간 사용 한도: 8 / 10
        gpt-5.3-codex-spark 주간 사용 한도: 80 / 100
        남은 크레딧: 320
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "12 / 40")
        self.assertEqual(parsed.get("weekly_limit"), "111 / 300")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "8 / 10")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "80 / 100")
        self.assertEqual(parsed.get("remaining_credit"), "320")

    def test_parse_usage_metrics_from_multiline_blocks(self) -> None:
        raw = """
        5시간 사용 한도
        15 / 40
        주간 사용 한도
        123 / 300
        gpt-5.3-codex-spark 5시간 사용 한도
        10 / 12
        gpt-5.3-codex-spark 주간 사용 한도
        84 / 100
        남은 크레딧
        287
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "15 / 40")
        self.assertEqual(parsed.get("weekly_limit"), "123 / 300")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "10 / 12")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "84 / 100")
        self.assertEqual(parsed.get("remaining_credit"), "287")

    def test_parse_usage_metrics_prefers_spark_specific_labels_over_generic_suffix_matches(self) -> None:
        raw = """
        5시간 사용 한도
        80%
        주간 사용 한도
        68%
        gpt-5.3-codex-spark 5시간 사용 한도
        83%
        gpt-5.3-codex-spark 주간 사용 한도
        95%
        남은 크레딧
        903
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "80%")
        self.assertEqual(parsed.get("weekly_limit"), "68%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "83%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "95%")
        self.assertEqual(parsed.get("remaining_credit"), "903")

    def test_extract_usage_metrics_from_semantic_blocks_ignores_unknown_block(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "value_candidates": ["26%"],
                    "block_text": "5-hour usage limit 26%",
                },
                {
                    "metric_key": "experimental_metric",
                    "label_text": "Experimental",
                    "value_candidates": ["999"],
                    "block_text": "Experimental 999",
                },
            ]
        )

        self.assertEqual(parsed.get("five_hour_limit"), "26%")
        self.assertNotIn("experimental_metric", parsed)

    def test_extract_usage_metrics_from_semantic_blocks_prefers_specific_metric_label(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "gpt-5.3-codex-spark 5-hour usage limit",
                    "value_candidates": ["83%"],
                    "block_text": "gpt-5.3-codex-spark 5-hour usage limit 83%",
                },
                {
                    "metric_key": "weekly_limit",
                    "label_text": "gpt-5.3-codex-spark weekly usage limit",
                    "value_candidates": ["95%"],
                    "block_text": "gpt-5.3-codex-spark weekly usage limit 95%",
                },
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "value_candidates": ["80%"],
                    "block_text": "5-hour usage limit 80%",
                },
                {
                    "metric_key": "weekly_limit",
                    "label_text": "weekly usage limit",
                    "value_candidates": ["68%"],
                    "block_text": "weekly usage limit 68%",
                },
                {
                    "metric_key": "remaining_credit",
                    "label_text": "remaining credit",
                    "value_candidates": ["903"],
                    "block_text": "remaining credit 903",
                },
            ]
        )

        self.assertEqual(parsed.get("five_hour_limit"), "80%")
        self.assertEqual(parsed.get("weekly_limit"), "68%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "83%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "95%")
        self.assertEqual(parsed.get("remaining_credit"), "903")

    def test_extract_usage_metrics_from_semantic_blocks_requires_recognized_label_or_key(self) -> None:
        parsed = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "label_text": "Random number",
                    "value_candidates": ["123"],
                    "block_text": "Random number 123",
                }
            ]
        )

        self.assertEqual(parsed, {})

    def test_merge_snapshot_with_previous_preserves_missing_values(self) -> None:
        prev = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 12",
                "gpt_5_3_codex_spark_weekly_limit": "84 / 100",
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
        merged = merge_snapshot_with_previous(partial, prev)

        self.assertEqual(merged.five_hour_limit, "19 / 40")
        self.assertEqual(merged.weekly_limit, "120 / 300")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit, "10 / 12")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit, "84 / 100")
        self.assertEqual(merged.remaining_credit, "260")

    def test_merge_snapshot_with_previous_preserves_missing_values_after_semantic_partial_snapshot(self) -> None:
        prev = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "26%",
                "weekly_limit": "28%",
                "gpt_5_3_codex_spark_five_hour_limit": "83%",
                "gpt_5_3_codex_spark_weekly_limit": "95%",
                "remaining_credit": "959",
            },
            captured_at="2026-03-30T10:00:00",
        )
        partial_metrics = extract_usage_metrics_from_semantic_blocks(
            [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "value_candidates": ["25%"],
                    "block_text": "5-hour usage limit 25%",
                }
            ]
        )

        merged = merge_snapshot_with_previous(
            UsageSnapshot.from_metrics(partial_metrics, captured_at="2026-03-30T10:10:00"),
            prev,
        )

        self.assertEqual(merged.five_hour_limit, "25%")
        self.assertEqual(merged.weekly_limit, "28%")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit, "83%")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit, "95%")
        self.assertEqual(merged.remaining_credit, "959")

    def test_compute_usage_changes_detects_only_changed_fields(self) -> None:
        prev = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "10 / 12",
                "gpt_5_3_codex_spark_weekly_limit": "84 / 100",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        curr = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "gpt_5_3_codex_spark_five_hour_limit": "9 / 12",
                "gpt_5_3_codex_spark_weekly_limit": "84 / 100",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:10:00",
        )

        changes = compute_usage_changes(prev, curr)
        labels = [c.label for c in changes]

        self.assertEqual(len(changes), 2)
        self.assertIn("5시간 사용 한도", labels)
        self.assertIn("gpt-5.3-codex-spark 5시간 사용 한도", labels)
        self.assertNotIn("주간 사용 한도", labels)
        self.assertNotIn("gpt-5.3-codex-spark 주간 사용 한도", labels)
        self.assertNotIn("남은 크레딧", labels)


if __name__ == "__main__":
    unittest.main()
