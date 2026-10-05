"""The authenticated legacy credit balance is current data, not a loading limit."""

import tempfile
import unittest
from unittest.mock import Mock

from src.apps.codex_usage_browser_types import PlaywrightSessionConfig, parse_usage_probe
from src.apps.codex_usage_monitor import CodexUsageMonitor, UsageSnapshot
from src.apps.codex_usage_playwright_driver import CodexUsagePlaywrightDriver
from src.apps.codex_usage_urls import CURRENT_CODEX_USAGE_URL, is_codex_usage_url


LEGACY_USAGE = "https://chatgpt.com/?tab=overview#settings/Usage"


def credit_probe(**overrides):
    return {
        "url": LEGACY_USAGE,
        "mainText": "사용량 크레딧 1,234 크레딧 남음",
        "accountId": "account-current",
        "profileName": "Current user",
        "creditsOnly": True,
        "metricBlocks": [{"metric_key": "remaining_credit", "block_text": "1,234 크레딧 남음"}],
        **overrides,
    }


class CreditOnlyUsageTest(unittest.TestCase):
    def test_exact_legacy_usage_route_only(self):
        for url in (LEGACY_USAGE, "https://chatgpt.com/#settings/Usage"):
            self.assertTrue(is_codex_usage_url(url), url)
        for url in (
            "https://chatgpt.com/#settings/Analytics",
            "https://chatgpt.com/?tab=analytics#settings/Usage",
            "https://chatgpt.com/?tab=overview&tab=overview#settings/Usage",
            "https://chatgpt.com/c/example#settings/Usage",
            "https://example.com/#settings/Usage",
        ):
            self.assertFalse(is_codex_usage_url(url), url)

    def test_transport_keeps_only_a_boolean_credit_mode(self):
        self.assertIs(parse_usage_probe(credit_probe())["creditsOnly"], True)
        self.assertNotIn("creditsOnly", parse_usage_probe(credit_probe(creditsOnly="true")))

    def test_driver_accepts_credit_view_without_waiting_for_a_limit(self):
        driver = CodexUsagePlaywrightDriver(PlaywrightSessionConfig("unused", CURRENT_CODEX_USAGE_URL, "unused"))
        self.assertTrue(driver._probe_is_terminal(credit_probe()))
        self.assertTrue(driver._probe_is_authenticated(credit_probe()))
        missing_identity = credit_probe(accountId="")
        self.assertTrue(driver._probe_is_terminal(missing_identity))
        self.assertFalse(driver._probe_is_authenticated(missing_identity))
        page = Mock()
        page.evaluate.return_value = missing_identity
        self.assertEqual(driver._evaluate_probe_until_ready(page), missing_identity)
        page.evaluate.assert_called_once()

    def test_driver_rejects_overview_loading_and_non_boolean_flag(self):
        driver = CodexUsagePlaywrightDriver(PlaywrightSessionConfig("unused", CURRENT_CODEX_USAGE_URL, "unused"))
        for probe in (credit_probe(url=CURRENT_CODEX_USAGE_URL), credit_probe(creditsOnly="true"), credit_probe(creditsOnly=False)):
            self.assertFalse(driver._probe_is_terminal(probe))
            self.assertFalse(driver._probe_is_authenticated(probe))

    def test_snapshot_requires_identity_and_valid_current_credit(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = CodexUsageMonitor(config_dir=folder, profile_dir=folder + "/profile")
            build = monitor._CodexUsageMonitor__build_snapshot_from_probe
            for probe in (credit_probe(accountId=""), credit_probe(url=CURRENT_CODEX_USAGE_URL), credit_probe(metricBlocks=[]), credit_probe(creditsOnly="true")):
                self.assertIsNone(build(probe))
            snapshot = build(credit_probe())
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.remaining_credit, "1234")
            self.assertEqual(snapshot.reported_metric_keys, ("remaining_credit",))
            self.assertIsNone(build(credit_probe(accountId="account-other")))

    def test_current_credit_removes_old_limits_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = CodexUsageMonitor(config_dir=folder, profile_dir=folder + "/profile")
            monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot(
                monthly_limit="100%", monthly_limit_reset_at="2026-11-04T03:51:40Z", remaining_credit="99"
            )
            local_provider = Mock(side_effect=AssertionError("A credit-only view must not restore local limits"))
            monitor._CodexUsageMonitor__local_usage_provider = local_provider
            snapshot = monitor._CodexUsageMonitor__build_snapshot_from_probe(credit_probe())
            self.assertIsNotNone(snapshot)
            monitor.handle_snapshot(snapshot)
            current = monitor.get_last_snapshot()
            self.assertEqual(current.remaining_credit, "1234")
            self.assertEqual(current.monthly_limit, "")
            self.assertEqual(current.monthly_limit_reset_at, "")
            local_provider.assert_not_called()
            reloaded = CodexUsageMonitor(config_dir=folder, profile_dir=folder + "/profile").get_last_snapshot()
            self.assertEqual(reloaded.remaining_credit, "1234")
            self.assertEqual(reloaded.monthly_limit, "")
            self.assertEqual(reloaded.monthly_limit_reset_at, "")
