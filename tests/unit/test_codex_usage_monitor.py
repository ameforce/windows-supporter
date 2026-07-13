import json
import os
import tempfile
import unittest
from unittest.mock import patch

from src.apps.codex_local_usage import LocalCodexUsageSnapshot
from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    UsageSnapshot,
    are_equivalent_codex_usage_urls,
    build_codex_login_entry_url,
    canonicalize_codex_usage_url,
    compute_usage_changes,
    extract_usage_metrics_from_semantic_blocks,
    merge_snapshot_with_previous,
    normalize_usage_value,
    parse_usage_metrics_from_text,
    sanitize_profile_name,
)


class CodexUsageMonitorUnitTest(unittest.TestCase):
    def test_shutdown_terminates_owned_cdp_process(self) -> None:
        class _OwnedProc:
            pid = 43210

        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
            )
            proc = _OwnedProc()
            monitor._CodexUsageMonitor__hidden_cdp_proc = proc
            monitor._CodexUsageMonitor__hidden_cdp_port = 11119
            with patch.object(
                monitor,
                "_CodexUsageMonitor__terminate_spawned_process",
            ) as terminate:
                monitor.shutdown()

            terminate.assert_called_once_with(proc, cleanup_orphans=True)
            self.assertIsNone(monitor._CodexUsageMonitor__hidden_cdp_proc)
            self.assertEqual(monitor._CodexUsageMonitor__hidden_cdp_port, 0)

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

    def test_sanitize_profile_name_rejects_menu_button_labels(self) -> None:
        for value in (
            "메뉴 열기",
            "프로필 메뉴 열기",
            "Open menu",
            "profile",
            "설정",
            "사용자 지정",
            "그룹화 기준: 일별",
        ):
            with self.subTest(value=value):
                self.assertEqual(sanitize_profile_name(value), "")

    def test_sanitize_profile_name_keeps_real_profile_name(self) -> None:
        self.assertEqual(sanitize_profile_name("Profile: Daeng"), "Daeng")
        self.assertEqual(sanitize_profile_name("이니미니"), "이니미니")

    def test_sanitize_profile_name_strips_plan_badge_suffix(self) -> None:
        self.assertEqual(sanitize_profile_name("이 PRO"), "이")

    def _usage_probe(self, profile_name: str) -> dict:
        return {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            "mainText": "Analytics usage 5-hour usage limit 99% weekly usage limit 96%",
            "profileName": profile_name,
            "metricBlocks": [
                {
                    "metric_key": "five_hour_limit",
                    "label_text": "5-hour usage limit",
                    "value_candidates": ["99%"],
                    "block_text": "5-hour usage limit 99%",
                },
                {
                    "metric_key": "weekly_limit",
                    "label_text": "weekly usage limit",
                    "value_candidates": ["96%"],
                    "block_text": "weekly usage limit 96%",
                },
            ],
        }

    def test_build_snapshot_from_probe_binds_first_profile_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("Kim Jong")
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(
                monitor.get_runtime_status().get("profile_name"),
                "Kim Jong",
            )

    def test_build_snapshot_from_probe_rejects_conflicting_bound_profile_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            monitor._CodexUsageMonitor__profile_name = "Kim Jong"

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("Other Profile")
            )

            self.assertIsNone(snapshot)
            self.assertEqual(
                monitor.get_runtime_status().get("profile_name"),
                "Kim Jong",
            )

    def test_build_snapshot_from_probe_keeps_bound_profile_name_when_probe_name_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            monitor._CodexUsageMonitor__profile_name = "Kim Jong"

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("")
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(
                monitor.get_runtime_status().get("profile_name"),
                "Kim Jong",
            )

    def test_build_snapshot_from_probe_accepts_empty_profile_name_when_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))

            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("")
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(monitor.get_runtime_status().get("profile_name"), "")

    def test_build_snapshot_keeps_web_value_when_local_provider_fails(self) -> None:
        # Given: web collection succeeds while the optional local adapter raises.
        def broken_local_provider():
            raise OSError("rollout unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                local_usage_provider=broken_local_provider,
            )

            # When: a valid web probe crosses the acquisition boundary.
            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(
                self._usage_probe("Kim Jong")
            )

        # Then: the optional adapter failure cannot discard authoritative web data.
        if snapshot is None:
            self.fail("valid web snapshot was discarded")
        self.assertEqual(snapshot.weekly_limit, "96%")

    def test_build_snapshot_applies_local_usage_to_matching_web_account(self) -> None:
        # Given: the web session and Windows Codex auth expose the same stable account ID.
        local = LocalCodexUsageSnapshot(
            captured_at="2026-07-13T00:52:19.258Z",
            account_id="acct-local",
            plan_type="pro",
            weekly_limit="95%",
            weekly_limit_reset_at="2026-07-20T04:01:12+09:00",
            reported_metric_keys=("weekly_limit",),
        )
        probe = self._usage_probe("Kim Jong")
        probe["accountId"] = "acct-local"
        probe["planType"] = "pro"
        probe["metricBlocks"][1]["reset_at_candidates"] = [
            "2026-07-20T04:01:00+09:00"
        ]
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
                local_usage_provider=lambda: local,
            )
            monitor._CodexUsageMonitor__now_iso = lambda: "2026-07-13T09:52:20+09:00"

            # When: the same-account probe crosses the acquisition boundary.
            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(probe)

        # Then: the fresher local remaining value replaces lagging web analytics.
        if snapshot is None:
            self.fail("valid same-account snapshot was discarded")
        self.assertEqual(snapshot.weekly_limit, "95%")
        self.assertEqual(snapshot.five_hour_limit, "")

    def test_parse_usage_metrics_from_inline_lines(self) -> None:
        raw = """
        5시간 사용 한도: 12 / 40
        주간 사용 한도: 111 / 300
        gpt-5.3-codex-spark 5시간 사용 한도: 8 / 10
        gpt-5.3-codex-spark 주간 사용 한도: 80 / 100
        남은 크레딧: 320
        """
        parsed = parse_usage_metrics_from_text(raw)

        self.assertEqual(parsed.get("five_hour_limit"), "70%")
        self.assertEqual(parsed.get("weekly_limit"), "63%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "20%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "20%")
        self.assertEqual(parsed.get("remaining_credit"), "320")

    def test_parse_usage_percent_converts_explicit_used_value_to_remaining(self) -> None:
        # Given: Codex reports a weekly window as an explicit used percentage.
        raw = "weekly usage limit: 5% used"

        # When: the external value crosses the usage parser boundary.
        parsed = parse_usage_metrics_from_text(raw)

        # Then: the snapshot contract contains remaining percentage.
        self.assertEqual(parsed.get("weekly_limit"), "95%")

    def test_parse_usage_ratio_converts_used_over_limit_to_remaining(self) -> None:
        # Given: Codex reports a five-hour window as used tokens over its limit.
        raw = "5-hour usage limit: 17 / 40"

        # When: the external ratio crosses the usage parser boundary.
        parsed = parse_usage_metrics_from_text(raw)

        # Then: the snapshot contract contains remaining percentage.
        self.assertEqual(parsed.get("five_hour_limit"), "57.5%")

    def test_semantic_usage_block_converts_explicit_used_value_to_remaining(self) -> None:
        # Given: the live DOM candidate explicitly qualifies its percentage as used.
        blocks = [
            {
                "metric_key": "weekly_limit",
                "label_text": "weekly usage limit",
                "value_candidates": ["5% used"],
                "block_text": "weekly usage limit 5% used",
            }
        ]

        # When: the semantic DOM contract is parsed.
        parsed = extract_usage_metrics_from_semantic_blocks(blocks)

        # Then: all acquisition paths expose the same remaining-percentage contract.
        self.assertEqual(parsed.get("weekly_limit"), "95%")

    def test_snapshot_from_dict_migrates_legacy_used_ratio_to_remaining(self) -> None:
        # Given: persisted state contains the legacy used-over-limit representation.
        payload = {
            "five_hour_limit": "17 / 40",
            "captured_at": "2026-07-13T09:48:03+09:00",
        }

        # When: the cache payload crosses the snapshot boundary.
        snapshot = UsageSnapshot.from_dict(payload)

        # Then: loaded state follows the canonical remaining-percentage contract.
        self.assertEqual(snapshot.five_hour_limit, "57.5%")

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

        self.assertEqual(parsed.get("five_hour_limit"), "62.5%")
        self.assertEqual(parsed.get("weekly_limit"), "59%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_five_hour_limit"), "16.6667%")
        self.assertEqual(parsed.get("gpt_5_3_codex_spark_weekly_limit"), "16%")
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

        self.assertEqual(merged.five_hour_limit, "52.5%")
        self.assertEqual(merged.weekly_limit, "60%")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit, "16.6667%")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit, "16%")
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

    def test_merge_snapshot_drops_stale_limits_absent_from_current_usage_page(self) -> None:
        # Given: an older page reported 5-hour and Spark limits, while the current
        # authoritative page reports only the weekly limit and credits.
        previous = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "0%",
                "weekly_limit": "98%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "remaining_credit": "0",
            },
            captured_at="2026-07-13T09:47:33+09:00",
            reset_info={
                "five_hour_limit_reset_at": "2026-07-14T02:39:00+09:00",
                "weekly_limit_reset_at": "2026-07-20T04:01:00+09:00",
            },
        )
        current = UsageSnapshot.from_metrics(
            {
                "weekly_limit": "97%",
                "remaining_credit": "0",
            },
            captured_at="2026-07-13T09:48:03+09:00",
            reset_info={
                "weekly_limit_reset_at": "2026-07-20T04:01:00+09:00",
            },
        )
        current.reported_metric_keys = ("weekly_limit", "remaining_credit")

        # When: the current snapshot is merged with the previous successful one.
        merged = merge_snapshot_with_previous(current, previous)

        # Then: metrics absent from the current authoritative page are not
        # relabeled with the current capture time as if they were fresh.
        self.assertEqual(merged.five_hour_limit, "")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit, "")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit, "")
        self.assertEqual(merged.five_hour_limit_reset_at, "")
        self.assertEqual(merged.weekly_limit, "97%")
        self.assertEqual(merged.remaining_credit, "0")
        self.assertEqual(merged.captured_at, "2026-07-13T09:48:03+09:00")

    def test_snapshot_from_dict_drops_implausible_day_scale_five_hour_reset(self) -> None:
        snapshot = UsageSnapshot.from_dict(
            {
                "five_hour_limit": "100%",
                "weekly_limit": "0%",
                "captured_at": "2026-06-03T20:48:37+09:00",
                "five_hour_limit_reset_at": "2026-06-07T15:39:00+09:00",
                "weekly_limit_reset_at": "2026-06-07T15:39:00+09:00",
            }
        )

        self.assertEqual(snapshot.five_hour_limit_reset_at, "")
        self.assertEqual(snapshot.weekly_limit_reset_at, "2026-06-07T15:39:00+09:00")

    def test_merge_snapshot_does_not_restore_previous_implausible_five_hour_reset(self) -> None:
        previous = UsageSnapshot.from_dict(
            {
                "five_hour_limit": "100%",
                "weekly_limit": "0%",
                "captured_at": "2026-06-03T20:48:37+09:00",
                "five_hour_limit_reset_at": "2026-06-07T15:39:00+09:00",
                "weekly_limit_reset_at": "2026-06-07T15:39:00+09:00",
            }
        )
        current = UsageSnapshot.from_metrics(
            {"five_hour_limit": "100%", "weekly_limit": "0%"},
            captured_at="2026-06-03T21:48:37+09:00",
        )

        merged = merge_snapshot_with_previous(current, previous)

        self.assertEqual(merged.five_hour_limit_reset_at, "")
        self.assertEqual(merged.weekly_limit_reset_at, "2026-06-07T15:39:00+09:00")

    def test_snapshot_from_dict_drops_cross_metric_cloned_five_hour_resets(self) -> None:
        snapshot = UsageSnapshot.from_dict(
            {
                "five_hour_limit": "89%",
                "weekly_limit": "35%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "captured_at": "2026-06-03T22:13:42+09:00",
                "five_hour_limit_reset_at": "2026-06-03T22:24:00+09:00",
                "weekly_limit_reset_at": "2026-06-08T00:38:00+09:00",
                "gpt_5_3_codex_spark_five_hour_limit_reset_at": "2026-06-03T22:24:00+09:00",
                "gpt_5_3_codex_spark_weekly_limit_reset_at": "2026-06-03T22:24:00+09:00",
            }
        )

        self.assertEqual(snapshot.five_hour_limit_reset_at, "2026-06-03T22:24:00+09:00")
        self.assertEqual(snapshot.weekly_limit_reset_at, "2026-06-08T00:38:00+09:00")
        self.assertEqual(snapshot.gpt_5_3_codex_spark_five_hour_limit_reset_at, "")
        self.assertEqual(snapshot.gpt_5_3_codex_spark_weekly_limit_reset_at, "")

    def test_merge_snapshot_does_not_restore_cross_metric_cloned_reset_values(self) -> None:
        previous = UsageSnapshot.from_dict(
            {
                "five_hour_limit": "89%",
                "weekly_limit": "35%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
                "captured_at": "2026-06-03T22:13:42+09:00",
                "five_hour_limit_reset_at": "2026-06-03T22:24:00+09:00",
                "weekly_limit_reset_at": "2026-06-08T00:38:00+09:00",
                "gpt_5_3_codex_spark_five_hour_limit_reset_at": "2026-06-03T22:24:00+09:00",
                "gpt_5_3_codex_spark_weekly_limit_reset_at": "2026-06-03T22:24:00+09:00",
            }
        )
        current = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "88%",
                "weekly_limit": "35%",
                "gpt_5_3_codex_spark_five_hour_limit": "100%",
                "gpt_5_3_codex_spark_weekly_limit": "100%",
            },
            captured_at="2026-06-03T22:14:42+09:00",
        )

        merged = merge_snapshot_with_previous(current, previous)

        self.assertEqual(merged.five_hour_limit_reset_at, "2026-06-03T22:24:00+09:00")
        self.assertEqual(merged.weekly_limit_reset_at, "2026-06-08T00:38:00+09:00")
        self.assertEqual(merged.gpt_5_3_codex_spark_five_hour_limit_reset_at, "")
        self.assertEqual(merged.gpt_5_3_codex_spark_weekly_limit_reset_at, "")

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

    def test_handle_snapshot_persists_compact_usage_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            reset_info = {"five_hour_limit_reset_at": "2026-06-01T11:00:00+09:00"}

            monitor.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {"five_hour_limit": "80%", "weekly_limit": "70%"},
                    captured_at="2026-06-01T10:00:00+09:00",
                    reset_info=reset_info,
                )
            )
            monitor.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {"five_hour_limit": "78%", "weekly_limit": "69%"},
                    captured_at="2026-06-01T10:02:00+09:00",
                    reset_info=reset_info,
                )
            )

            state_path = os.path.join(tmp, "codex_usage_state.json")
            with open(state_path, encoding="utf-8") as fp:
                state = json.load(fp)
            history = state.get("usage_history")

            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["captured_at"], "2026-06-01T10:00:00+09:00")
            self.assertEqual(history[1]["five_hour_limit"], "78%")
            self.assertEqual(
                history[1]["five_hour_limit_reset_at"],
                "2026-06-01T11:00:00+09:00",
            )
            self.assertEqual(monitor.get_runtime_status()["usage_history"], history)
            self.assertEqual(state.get("snapshot_contract_version"), 2)

    def test_load_state_invalidates_ambiguous_legacy_percent_cache(self) -> None:
        # Given: v0.6.60 persisted bare percentages after erasing used/remaining meaning.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "codex_usage_state.json")
            with open(state_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "session_state": "logged_in",
                        "last_snapshot": {
                            "five_hour_limit": "5%",
                            "weekly_limit": "17 / 40",
                            "remaining_credit": "320",
                            "captured_at": "2026-07-13T09:48:03+09:00",
                        },
                        "usage_history": [
                            {
                                "captured_at": "2026-07-13T09:46:03+09:00",
                                "five_hour_limit": "5%",
                            },
                            {
                                "captured_at": "2026-07-13T09:48:03+09:00",
                                "weekly_limit": "17 / 40",
                            },
                        ],
                    },
                    fp,
                )

            # When: the unversioned cache crosses the v2 snapshot contract boundary.
            monitor = CodexUsageMonitor(
                config_dir=tmp,
                profile_dir=os.path.join(tmp, "profile"),
            )
            snapshot = monitor.get_last_snapshot()
            history = monitor.get_runtime_status()["usage_history"]
            with open(state_path, encoding="utf-8") as fp:
                migrated = json.load(fp)

        # Then: ambiguous bare percentages disappear; unambiguous ratios migrate.
        self.assertEqual(snapshot.five_hour_limit, "")
        self.assertEqual(snapshot.weekly_limit, "57.5%")
        self.assertEqual(snapshot.remaining_credit, "320")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["weekly_limit"], "57.5%")
        self.assertEqual(migrated.get("snapshot_contract_version"), 2)

    def test_load_state_normalizes_old_or_oversized_usage_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "codex_usage_state.json")
            raw_history = [
                {"captured_at": "bad", "five_hour_limit": "99%"},
                {"captured_at": "2026-06-01T09:00:00+09:00", "five_hour_limit": "90%"},
                {"captured_at": "2026-06-01T10:00:00+09:00", "five_hour_limit": "80%"},
                {"captured_at": "2026-06-01T10:02:00+09:00", "five_hour_limit": "79%"},
                {"captured_at": "2026-06-01T10:04:00+09:00", "five_hour_limit": "78%"},
                {"captured_at": "2026-06-01T10:06:00+09:00", "five_hour_limit": "77%"},
                {"captured_at": "2026-06-01T10:08:00+09:00", "five_hour_limit": "76%"},
                {"captured_at": "2026-06-01T10:10:00+09:00", "five_hour_limit": "75%"},
            ]
            with open(state_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "session_state": "logged_in",
                        "snapshot_contract_version": 2,
                        "last_snapshot": {"five_hour_limit": "75%"},
                        "usage_history": raw_history,
                    },
                    fp,
                )

            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            history = monitor.get_runtime_status()["usage_history"]

            self.assertEqual(len(history), 5)
            self.assertEqual(history[0]["captured_at"], "2026-06-01T10:02:00+09:00")
            self.assertEqual(history[-1]["captured_at"], "2026-06-01T10:10:00+09:00")
            self.assertNotIn("bad", [item["captured_at"] for item in history])


if __name__ == "__main__":
    unittest.main()
