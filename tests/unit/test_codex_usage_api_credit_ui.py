"""API credit rows, status wording and the profile action in the AI usage settings view."""

from __future__ import annotations

import unittest
from typing import Any

from src.apps.codex_usage_ui import CodexUsageSettingsView
from src.apps.profile_controls import ProfileActionBinding, ProfileCardMenu
from tests.unit.test_codex_usage_ui import (
    _FakeDetailTable,
    _FakeTk,
    _FakeTtk,
    _FakeVar,
    _FakeWidget,
)


def codex_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "account_1",
        "provider": "codex",
        "enabled": True,
        "runtime": {"can_login_api_credit": True},
        "last_snapshot": {},
        "metrics": [],
    }
    entry.update(overrides)
    return entry


class ApiCreditRowsTest(unittest.TestCase):
    def test_codex_card_ends_with_the_api_credit_pair(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._tk = _FakeTk()
        table = _FakeDetailTable()

        metric_vars, display_vars = view._build_account_metric_rows(table, account_id="codex-1")

        self.assertEqual(
            [(entry["key"], entry["label"]) for entry in table.added[-2:]],
            [("api_credit_balance", "API 크레딧 잔액"), ("api_credit_captured_at", "API 잔액 확인")],
        )
        self.assertEqual(display_vars["api_credit_balance"].get(), "-")
        self.assertIn("api_credit_captured_at", metric_vars)

    def test_other_providers_do_not_get_api_credit_rows(self) -> None:
        for provider in ("cursor", "claude"):
            with self.subTest(provider=provider):
                view = CodexUsageSettingsView(root=None, codex_monitor=None)
                view._tk = _FakeTk()
                metric_vars, _display_vars = view._build_account_metric_rows(
                    _FakeDetailTable(), provider=provider
                )
                self.assertFalse(any(key.startswith("api_credit") for key in metric_vars))

    def test_balance_wording_follows_the_latest_read(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        format_value = view._format_account_metric_value
        for snapshot, expected in (
            ({"api_credit_balance": "$12.34", "api_credit_status": "ok"}, "$12.34"),
            ({"api_credit_balance": "$12.34"}, "$12.34"),
            (
                {"api_credit_balance": "$12.34", "api_credit_status": "login_required"},
                "$12.34 (이전 값, 로그인 필요)",
            ),
            ({"api_credit_status": "login_required"}, "로그인 필요"),
            ({"api_credit_status": "unavailable"}, "조회 불가"),
            ({}, "-"),
            ({"api_credit_balance": "lots"}, "-"),
        ):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(format_value("api_credit_balance", snapshot), expected)

    def test_read_time_uses_the_same_wording_as_the_usage_time(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        self.assertEqual(
            view._format_account_metric_value(
                "api_credit_captured_at", {"api_credit_captured_at": "2026-11-05T10:00:00"}
            ),
            "2026-11-05 10:00:00",
        )
        self.assertEqual(view._format_account_metric_value("api_credit_captured_at", {}), "-")

    def test_state_text_names_the_sign_in_wait(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)

        self.assertEqual(
            view._runtime_state_text(
                {
                    "session_state": "logged_in",
                    "monitor_state": "running",
                    "collect_inflight": True,
                    "collect_source": "api_credit_login",
                }
            ),
            "API 크레딧 로그인 대기 중",
        )

    def test_runtime_refresh_fills_the_api_rows_of_a_card(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._account_rendered_providers = {"account_1": "codex"}
        balance, read_at = _FakeVar("-"), _FakeVar("-")
        view._account_metric_vars = {
            "account_1": {"api_credit_balance": balance, "api_credit_captured_at": read_at}
        }

        view._refresh_account_runtime_summaries(
            {
                "profiles": [
                    codex_entry(
                        last_snapshot={
                            "api_credit_balance": "$12.34",
                            "api_credit_captured_at": "2026-11-05T10:00:00",
                            "api_credit_status": "ok",
                        }
                    )
                ]
            }
        )

        self.assertEqual(balance.get(), "$12.34")
        self.assertEqual(read_at.get(), "2026-11-05 10:00:00")

    def test_live_panel_shows_the_api_pair_and_hides_only_monthly_and_spark_rows(self) -> None:
        fake_tk = _FakeTk()
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._tk = fake_tk
        view._ttk = _FakeTtk()
        view._lazy_import_tk = lambda: None
        view._safe_get_settings = lambda: {"settings_path": "", "state_path": "", "profile_dir": ""}
        view._load_settings = lambda: None
        view._start_runtime_refresh = lambda: None

        view.mount(_FakeWidget())

        labels = {
            label.kwargs.get("text"): label.grid_kwargs
            for label in fake_tk.labels
            if label.kwargs.get("text")
        }
        self.assertEqual(labels["API 크레딧 잔액"].get("column"), 0)
        self.assertEqual(labels["API 잔액 확인"].get("column"), 2)
        hideable = {
            widget.kwargs.get("text")
            for widget in view._live_spark_cells
            if widget.kwargs.get("text")
        }
        self.assertEqual(
            hideable,
            {
                "월간 사용 한도",
                "월간 한도 초기화",
                "Spark 5시간 한도",
                "Spark 5시간 초기화",
                "Spark 주간 한도",
                "Spark 주간 초기화",
            },
        )
        self.assertEqual(view._live_api_credit_var.get(), "-")
        self.assertEqual(view._live_api_credit_time_var.get(), "-")


class ApiCreditActionTest(unittest.TestCase):
    def test_permission_needs_an_enabled_idle_codex_profile(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        allowed = view._account_api_credit_login_permission

        self.assertTrue(allowed(codex_entry()))
        self.assertFalse(allowed(None))
        self.assertFalse(allowed(codex_entry(enabled=False)))
        self.assertFalse(allowed(codex_entry(provider="cursor")))
        self.assertFalse(allowed(codex_entry(provider="claude")))
        self.assertFalse(allowed(codex_entry(runtime={"can_login_api_credit": False})))
        self.assertFalse(allowed(codex_entry(runtime={})))
        self.assertFalse(allowed(codex_entry(runtime="busy")))

    def test_buttons_follow_the_runtime_of_their_own_profile(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        ready, busy, other = ProfileActionBinding(), ProfileActionBinding(), ProfileActionBinding()
        view._account_api_credit_login_buttons = {"a": ready, "b": busy, "c": other}

        view._refresh_action_buttons(
            {
                "profiles": [
                    codex_entry(id="a"),
                    codex_entry(id="b", runtime={"can_login_api_credit": False}),
                    codex_entry(id="c", provider="cursor"),
                ]
            }
        )

        self.assertTrue(ready.enabled)
        self.assertFalse(busy.enabled)
        self.assertFalse(other.enabled)

    def test_buttons_stay_disabled_while_a_profile_action_is_running(self) -> None:
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        button = ProfileActionBinding()
        view._account_api_credit_login_buttons = {"a": button}
        view._profile_actions_inflight.add("a")

        view._refresh_action_buttons({"profiles": [codex_entry(id="a")]})

        self.assertFalse(button.enabled)

    def test_click_starts_the_sign_in_for_that_profile_only(self) -> None:
        class _Monitor:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.result = True

            def login_api_credit_account(self, account_id: str) -> bool:
                self.calls.append(account_id)
                return self.result

            def get_runtime_status(self) -> dict[str, Any]:
                return {"profiles": [codex_entry(id="account_2")]}

        monitor = _Monitor()
        statuses: list[tuple[str, str]] = []
        view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))

        view._on_account_api_credit_login("account_2")
        monitor.result = False
        view._on_account_api_credit_login("account_2")

        self.assertEqual(monitor.calls, ["account_2", "account_2"])
        self.assertIn("API 크레딧 로그인 창을 여는 중", statuses[0][0])
        self.assertIn("시작할 수 없습니다", statuses[1][0])

    def test_click_is_refused_without_calling_the_monitor_when_the_profile_is_busy(self) -> None:
        class _Monitor:
            calls = 0

            def login_api_credit_account(self, _account_id: str) -> bool:
                type(self).calls += 1
                return True

            def get_runtime_status(self) -> dict[str, Any]:
                return {
                    "profiles": [codex_entry(id="account_1", runtime={"can_login_api_credit": False})]
                }

        statuses: list[str] = []
        view = CodexUsageSettingsView(root=None, codex_monitor=_Monitor())
        view._set_status = lambda text, level="info": statuses.append(str(text))

        view._on_account_api_credit_login("account_1")

        self.assertEqual(_Monitor.calls, 0)
        self.assertIn("시작할 수 없습니다", statuses[0])

    def test_click_reports_an_unsupported_monitor_and_a_failing_one(self) -> None:
        class _Failing:
            def login_api_credit_account(self, _account_id: str) -> bool:
                raise RuntimeError("boom")

        statuses: list[tuple[str, str]] = []
        for monitor, expected in ((object(), "사용할 수 없습니다"), (_Failing(), "오류가 발생했습니다")):
            with self.subTest(monitor=type(monitor).__name__):
                view = CodexUsageSettingsView(root=None, codex_monitor=monitor)
                view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))
                view._on_account_api_credit_login("account_1")
                self.assertIn(expected, statuses[-1][0])
                self.assertEqual(statuses[-1][1], "error")

    def test_menu_entry_exists_and_routes_to_the_view(self) -> None:
        self.assertIn(
            ("api_credit_login", "command", "API 크레딧 연결"),
            ProfileCardMenu._ENTRIES,
        )
        routed: list[str] = []

        class _View:
            _account_order = ["account_1"]

            def _on_account_api_credit_login(self, profile_id: str) -> None:
                routed.append(profile_id)

        menu = ProfileCardMenu(_View(), None)
        menu.profile_id = "account_1"

        menu.invoke("api_credit_login")

        self.assertEqual(routed, ["account_1"])


if __name__ == "__main__":
    unittest.main()
