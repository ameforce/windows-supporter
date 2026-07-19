from __future__ import annotations

import unittest

from src.apps.ai_usage_contracts import (
    AiUsageProvider,
    AiUsageReading,
    UsageState,
    normalize_usage_state,
    normalize_reset_boundary,
    usage_state_message,
)


class AiUsageContractsUnitTest(unittest.TestCase):
    def test_provider_contract_contains_only_supported_provider_ids(self) -> None:
        self.assertEqual(
            {provider.value for provider in AiUsageProvider},
            {"codex", "cursor"},
        )

    def test_runtime_failure_aliases_normalize_to_stable_states(self) -> None:
        cases = {
            "logged_out": UsageState.LOGGED_OUT,
            "login_required": UsageState.LOGGED_OUT,
            "not-authenticated": UsageState.LOGGED_OUT,
            "dom_drift": UsageState.DOM_DRIFT,
            "parse_failed": UsageState.DOM_DRIFT,
            "schema incompatible": UsageState.DOM_DRIFT,
            "timeout": UsageState.TIMEOUT,
            "command_timeout": UsageState.TIMEOUT,
            "navigation-timeout": UsageState.TIMEOUT,
            "rate_limit": UsageState.RATE_LIMITED,
            "too many requests": UsageState.RATE_LIMITED,
            "429": UsageState.RATE_LIMITED,
            "stale": UsageState.STALE,
            "cache_stale": UsageState.STALE,
            "expired cache": UsageState.STALE,
            "crash": UsageState.CRASH,
            "renderer_crashed": UsageState.CRASH,
            "transport closed": UsageState.CRASH,
            "recycle": UsageState.RECYCLE,
            "worker_recycle": UsageState.RECYCLE,
            "page-recycling": UsageState.RECYCLE,
        }

        for raw_state, expected in cases.items():
            with self.subTest(raw_state=raw_state):
                self.assertEqual(normalize_usage_state(raw_state), expected)

    def test_unknown_state_fails_closed(self) -> None:
        self.assertEqual(normalize_usage_state(""), UsageState.UNKNOWN)
        self.assertEqual(normalize_usage_state("new-upstream-error"), UsageState.UNKNOWN)
        self.assertEqual(normalize_usage_state(None), UsageState.UNKNOWN)

    def test_unavailable_reading_has_no_fabricated_usage_value(self) -> None:
        reading = AiUsageReading.unavailable(
            provider=AiUsageProvider.CURSOR,
            profile_id="cursor-1",
            state=UsageState.UNSUPPORTED_CONTRACT,
        )

        self.assertEqual(reading.provider, AiUsageProvider.CURSOR)
        self.assertEqual(reading.state, UsageState.UNSUPPORTED_CONTRACT)
        self.assertIsNone(reading.used_percent)
        self.assertIsNone(reading.remaining_percent)
        self.assertFalse(reading.is_usable)
        self.assertIn("조회 불가", reading.message)

    def test_stale_reading_can_preserve_last_success_without_becoming_ready(self) -> None:
        reading = AiUsageReading(
            provider=AiUsageProvider.CODEX,
            profile_id="codex-1",
            state=UsageState.STALE,
            used_percent=42.0,
            remaining_percent=58.0,
            captured_at="2026-07-18T10:00:00+09:00",
            last_success_at="2026-07-18T10:00:00+09:00",
            message=usage_state_message(UsageState.STALE),
        )

        self.assertTrue(reading.is_usable)
        self.assertTrue(reading.is_stale)
        self.assertNotEqual(reading.state, UsageState.READY)
        self.assertEqual(reading.to_dict()["state"], "stale")

    def test_absolute_included_usage_is_preserved_as_json_safe_strings(self) -> None:
        reading = AiUsageReading(
            provider=AiUsageProvider.CURSOR,
            profile_id="cursor-1",
            state=UsageState.READY,
            used_percent=0,
            remaining_percent=100,
            included_used="US$0",
            included_limit="US$20",
        )

        payload = reading.to_dict()

        self.assertEqual(payload["included_used"], "US$0")
        self.assertEqual(payload["included_limit"], "US$20")
        self.assertEqual(payload["included_usage"], "US$0 / US$20")

    def test_reset_boundary_normalizes_korean_and_iso_dates_without_inventing_time(self) -> None:
        self.assertEqual(normalize_reset_boundary("2026년 8월 13일"), ("2026-08-13", "date"))
        self.assertEqual(normalize_reset_boundary("2026-08-13"), ("2026-08-13", "date"))
        normalized, precision = normalize_reset_boundary("2026-08-13T09:30:00+09:00")
        self.assertEqual(normalized, "2026-08-13T09:30:00+09:00")
        self.assertEqual(precision, "datetime")

    def test_reading_round_trip_preserves_reset_precision_and_full_amount(self) -> None:
        reading = AiUsageReading(
            provider=AiUsageProvider.CURSOR,
            profile_id="cursor-1",
            state=UsageState.READY,
            used_percent=0,
            remaining_percent=100,
            included_used="US$0",
            included_limit="US$20",
            reset_at="2026년 8월 13일",
        )

        payload = reading.to_dict()

        self.assertEqual(reading.reset_at, "2026-08-13")
        self.assertEqual(reading.reset_precision, "date")
        self.assertEqual(payload["reset_precision"], "date")
        self.assertEqual(payload["included_usage"], "US$0 / US$20")

    def test_reading_positional_constructor_preserves_pre_precision_argument_order(self) -> None:
        reading = AiUsageReading(
            AiUsageProvider.CURSOR,
            "cursor-1",
            UsageState.READY,
            25,
            75,
            "US$5",
            "US$20",
            "2026-07-19T09:00:00+09:00",
            "2026-07-19T09:00:00+09:00",
            "2026-08-13",
            False,
            "legacy message",
            UsageState.TIMEOUT,
        )

        self.assertFalse(reading.on_demand_enabled)
        self.assertEqual(reading.message, "legacy message")
        self.assertEqual(reading.last_error_state, UsageState.TIMEOUT)
        self.assertEqual(reading.reset_precision, "date")

    def test_percentage_contract_rejects_out_of_range_or_inconsistent_values(self) -> None:
        invalid_values = (
            {"used_percent": -0.1, "remaining_percent": 100.1},
            {"used_percent": 101.0, "remaining_percent": -1.0},
            {"used_percent": 60.0, "remaining_percent": 60.0},
        )

        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(ValueError):
                AiUsageReading(
                    provider=AiUsageProvider.CODEX,
                    profile_id="codex-1",
                    state=UsageState.READY,
                    **values,
                )


if __name__ == "__main__":
    unittest.main()
