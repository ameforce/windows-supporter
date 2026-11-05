"""The profile manager hands the API credit sign-in to the right Codex profile."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

from src.apps.codex_usage_multi_monitor import CodexUsageMultiMonitor
from tests.unit.test_codex_usage_multi_monitor import _FakeChildMonitor


class _ApiCreditChild(_FakeChildMonitor):
    def __init__(self, config_dir: str, profile_dir: str) -> None:
        super().__init__(config_dir, profile_dir)
        self.api_credit_login_calls = 0
        self.api_credit_login_result = True

    def login_api_credit(self) -> bool:
        self.api_credit_login_calls += 1
        return self.api_credit_login_result


def make_manager(tmp: str, child_type: type[_FakeChildMonitor]) -> tuple[CodexUsageMultiMonitor, list[Any]]:
    children: list[Any] = []

    def factory(config_dir: str, profile_dir: str) -> _FakeChildMonitor:
        child = child_type(config_dir, profile_dir)
        children.append(child)
        return child

    manager = CodexUsageMultiMonitor(
        config_dir=os.path.join(tmp, "config"),
        local_base_dir=os.path.join(tmp, "local"),
        monitor_factory=factory,
    )
    return manager, children


class ApiCreditProfileLoginTest(unittest.TestCase):
    def test_request_reaches_the_profile_that_was_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = make_manager(tmp, _ApiCreditChild)

            self.assertTrue(manager.login_api_credit_account("account_1"))

            self.assertEqual(children[0].api_credit_login_calls, 1)

    def test_a_refused_start_is_reported_to_the_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = make_manager(tmp, _ApiCreditChild)
            children[0].api_credit_login_result = False

            self.assertFalse(manager.login_api_credit_account("account_1"))

    def test_providers_without_the_feature_answer_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _children = make_manager(tmp, _FakeChildMonitor)

            self.assertFalse(manager.login_api_credit_account("account_1"))

    def test_unknown_blocked_or_closing_profiles_never_reach_a_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = make_manager(tmp, _ApiCreditChild)

            self.assertFalse(manager.login_api_credit_account("missing"))
            manager._CodexUsageMultiMonitor__set_profile_refresh_blocked("account_1", True)
            self.assertFalse(manager.login_api_credit_account("account_1"))
            manager._CodexUsageMultiMonitor__set_profile_refresh_blocked("account_1", False)
            manager._CodexUsageMultiMonitor__closing = True
            self.assertFalse(manager.login_api_credit_account("account_1"))

            self.assertEqual(children[0].api_credit_login_calls, 0)

    def test_the_request_does_not_leave_the_profile_marked_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _children = make_manager(tmp, _ApiCreditChild)

            _ = manager.login_api_credit_account("account_1")

            self.assertEqual(manager._CodexUsageMultiMonitor__active_refresh_counts, {})

    def test_runtime_entry_exposes_the_balance_and_the_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = make_manager(tmp, _ApiCreditChild)
            children[0].last_snapshot = {
                **children[0].last_snapshot,
                "api_credit_balance": "$12.34",
                "api_credit_status": "ok",
            }
            children[0].runtime = {**children[0].runtime, "can_login_api_credit": True}

            entry = manager.get_runtime_status()["profiles"][0]

            self.assertEqual(entry["last_snapshot"]["api_credit_balance"], "$12.34")
            self.assertTrue(entry["runtime"]["can_login_api_credit"])


if __name__ == "__main__":
    unittest.main()
