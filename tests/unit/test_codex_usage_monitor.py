import unittest

from src.apps.codex_usage_monitor import (
    UsageSnapshot,
    build_codex_login_entry_url,
    canonicalize_codex_usage_url,
    compute_usage_changes,
    extract_usage_metrics_from_semantic_blocks,
    merge_snapshot_with_previous,
    normalize_usage_value,
    parse_usage_metrics_from_text,
)


class CodexUsageMonitorUnitTest(unittest.TestCase):
    def test_canonicalize_codex_usage_url_promotes_cloud_usage_path(self) -> None:
        self.assertEqual(
            canonicalize_codex_usage_url("https://chatgpt.com/codex/settings/usage"),
            "https://chatgpt.com/codex/cloud/settings/usage",
        )

    def test_build_codex_login_entry_url_targets_canonical_usage_path(self) -> None:
        self.assertEqual(
            build_codex_login_entry_url("https://chatgpt.com/codex/settings/usage"),
            "https://chatgpt.com/auth/login?next=/codex/cloud/settings/usage",
        )

    def test_normalize_usage_value_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_usage_value("  12 / 40 \n\n  left "),
            "12 / 40 left",
        )

    def test_parse_usage_metrics_from_inline_lines(self) -> None:
        raw = """
        5시간 사용 한도: 12 / 40
        주간 사용 한도: 111 / 300
        코드 검토: 8 / 50
        남은 크레딧: 320
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "12 / 40")
        self.assertEqual(parsed.get("weekly_limit"), "111 / 300")
        self.assertEqual(parsed.get("code_review"), "8 / 50")
        self.assertEqual(parsed.get("remaining_credit"), "320")

    def test_parse_usage_metrics_from_multiline_blocks(self) -> None:
        raw = """
        5시간 사용 한도
        15 / 40
        주간 사용 한도
        123 / 300
        코드 검토
        10 / 50
        남은 크레딧
        287
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "15 / 40")
        self.assertEqual(parsed.get("weekly_limit"), "123 / 300")
        self.assertEqual(parsed.get("code_review"), "10 / 50")
        self.assertEqual(parsed.get("remaining_credit"), "287")

    def test_parse_usage_metrics_skips_non_numeric_code_review_candidate(self) -> None:
        raw = """
        5시간 사용 한도
        26% 남음
        주간 사용 한도
        28% 남음
        코드 검토
        Connectors
        100% 남음
        남은 크레딧
        959
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "26%")
        self.assertEqual(parsed.get("weekly_limit"), "28%")
        self.assertEqual(parsed.get("code_review"), "100%")
        self.assertEqual(parsed.get("remaining_credit"), "959")

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
                "code_review": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        partial = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "",
                "code_review": "",
                "remaining_credit": "",
            },
            captured_at="2026-03-30T10:10:00",
        )
        merged = merge_snapshot_with_previous(partial, prev)

        self.assertEqual(merged.five_hour_limit, "19 / 40")
        self.assertEqual(merged.weekly_limit, "120 / 300")
        self.assertEqual(merged.code_review, "10 / 50")
        self.assertEqual(merged.remaining_credit, "260")

    def test_merge_snapshot_with_previous_preserves_missing_values_after_semantic_partial_snapshot(self) -> None:
        prev = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "26%",
                "weekly_limit": "28%",
                "code_review": "100%",
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
        self.assertEqual(merged.code_review, "100%")
        self.assertEqual(merged.remaining_credit, "959")

    def test_compute_usage_changes_detects_only_changed_fields(self) -> None:
        prev = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "code_review": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        curr = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "code_review": "9 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:10:00",
        )

        changes = compute_usage_changes(prev, curr)
        labels = [c.label for c in changes]

        self.assertEqual(len(changes), 2)
        self.assertIn("5시간 사용 한도", labels)
        self.assertIn("코드 검토", labels)
        self.assertNotIn("주간 사용 한도", labels)
        self.assertNotIn("남은 크레딧", labels)


if __name__ == "__main__":
    unittest.main()
