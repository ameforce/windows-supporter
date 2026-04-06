import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.apps.codex_usage_monitor import CodexUsageMonitor, UsageChange, UsageSnapshot


class CodexUsageMonitorFlowE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = CodexUsageMonitor()
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot()

    def test_handle_snapshot_baseline_and_change_flow(self) -> None:
        baseline = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "code_review": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:00:00",
        )
        same_again = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "20 / 40",
                "weekly_limit": "120 / 300",
                "code_review": "10 / 50",
                "remaining_credit": "260",
            },
            captured_at="2026-03-30T10:10:00",
        )
        changed = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "19 / 40",
                "weekly_limit": "120 / 300",
                "code_review": "10 / 50",
                "remaining_credit": "259",
            },
            captured_at="2026-03-30T10:20:00",
        )

        with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
            first_changes = self.monitor.handle_snapshot(baseline)
            second_changes = self.monitor.handle_snapshot(same_again)
            third_changes = self.monitor.handle_snapshot(changed)

        self.assertEqual(first_changes, [])
        self.assertEqual(second_changes, [])
        self.assertEqual(len(third_changes), 2)
        labels = [c.label for c in third_changes]
        self.assertIn("5시간 사용 한도", labels)
        self.assertIn("남은 크레딧", labels)

    def test_partial_snapshot_uses_previous_values_conservatively(self) -> None:
        baseline = UsageSnapshot.from_metrics(
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

        with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
            self.monitor.handle_snapshot(baseline)
            changes = self.monitor.handle_snapshot(partial)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].label, "5시간 사용 한도")

    def test_collect_with_playwright_obj_does_not_open_visible_on_parse_failed(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "parse_failed"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip") as show_tip:
                    snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                        object(),
                        source="manual_query",
                    )

        self.assertEqual(err, "parse_failed")
        self.assertIsNone(snap)
        self.assertEqual(collect_once.call_count, 1)
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertTrue(first_call.kwargs.get("prefer_system_channel"))
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))
        self.assertFalse(show_tip.called)

    def test_collect_with_playwright_obj_retries_hidden_before_opening_interactive(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "code_review": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            side_effect=[
                (None, "cloudflare_challenge"),
                (recovered, None),
            ],
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(self.monitor._CodexUsageMonitor__lib.time, "monotonic", return_value=2000.0):
                    snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                        object(),
                        source="manual_query",
                    )

        self.assertIsNone(err)
        self.assertIsNotNone(snap)
        self.assertEqual(collect_once.call_count, 2)
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))
        second_call = collect_once.call_args_list[1]
        self.assertFalse(second_call.kwargs.get("headless"))
        self.assertFalse(second_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(second_call.kwargs.get("prefer_system_channel"))
        self.assertTrue(second_call.kwargs.get("force_hidden"))

    def test_collect_with_playwright_obj_opens_interactive_when_hidden_retry_still_fails(self) -> None:
        recovered = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "17 / 40",
                "weekly_limit": "109 / 300",
                "code_review": "8 / 50",
                "remaining_credit": "245",
            },
            captured_at="2026-03-30T11:05:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            side_effect=[
                (None, "cloudflare_challenge"),
                (None, "cloudflare_challenge"),
                (recovered, None),
            ],
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__prepare_interactive_recovery_launch",
                ) as prepare_interactive:
                    with patch.object(
                        self.monitor._CodexUsageMonitor__lib.time,
                        "monotonic",
                        return_value=2000.0,
                    ):
                        snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                            object(),
                            source="manual_query",
                        )

        self.assertTrue(prepare_interactive.called)
        self.assertEqual(
            prepare_interactive.call_args.kwargs.get("source"),
            "manual_query",
        )
        self.assertEqual(
            prepare_interactive.call_args.kwargs.get("reason"),
            "cloudflare_challenge",
        )
        self.assertIsNone(err)
        self.assertIsNotNone(snap)
        self.assertEqual(collect_once.call_count, 3)
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))
        second_call = collect_once.call_args_list[1]
        self.assertFalse(second_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(second_call.kwargs.get("force_hidden"))
        third_call = collect_once.call_args_list[2]
        self.assertTrue(third_call.kwargs.get("allow_interactive_recovery"))
        self.assertFalse(third_call.kwargs.get("force_hidden"))
        self.assertEqual(
            third_call.kwargs.get("initial_url"),
            "https://chatgpt.com/auth/login?next=/codex/settings/usage",
        )

    def test_collect_with_playwright_obj_skips_interactive_for_background_source(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot_once",
            return_value=(None, "cloudflare_challenge"),
        ) as collect_once:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip") as show_tip:
                    snap, err = self.monitor._CodexUsageMonitor__collect_with_playwright_obj(
                        object(),
                        source="startup_warmup",
                    )

        self.assertEqual(err, "cloudflare_challenge")
        self.assertIsNone(snap)
        self.assertEqual(collect_once.call_count, 1)
        first_call = collect_once.call_args_list[0]
        self.assertFalse(first_call.kwargs.get("headless"))
        self.assertFalse(first_call.kwargs.get("allow_interactive_recovery"))
        self.assertTrue(first_call.kwargs.get("force_hidden"))
        self.assertFalse(show_tip.called)

    def test_should_open_interactive_recovery_manual_not_blocked_by_background_source(self) -> None:
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            side_effect=[100.0, 100.0],
        ):
            self.assertFalse(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="startup_warmup"
                )
            )
            self.assertTrue(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="manual_query"
                )
            )

    def test_should_open_interactive_recovery_manual_uses_short_cooldown(self) -> None:
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            side_effect=[100.0, 101.0, 104.2],
        ):
            self.assertTrue(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="manual_query"
                )
            )
            self.assertFalse(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="manual_query"
                )
            )
            self.assertTrue(
                self.monitor._CodexUsageMonitor__should_open_interactive_recovery(
                    source="manual_query"
                )
            )

    def test_is_cloudflare_challenge_detects_html_marker_with_empty_body_text(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def evaluate(self, _expr):
                return ""

            def content(self):
                return (
                    "<html><head>"
                    "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
                    "</head><body></body></html>"
                )

        self.assertTrue(
            self.monitor._CodexUsageMonitor__is_cloudflare_challenge(_DummyPage())
        )

    def test_is_cloudflare_challenge_detects_cf_query_token(self) -> None:
        class _DummyPage:
            url = (
                "https://chatgpt.com/codex/settings/usage?"
                "__cf_chl_rt_tk=token-123"
            )

            def evaluate(self, _expr):
                return ""

            def content(self):
                return "<html><body></body></html>"

        self.assertTrue(
            self.monitor._CodexUsageMonitor__is_cloudflare_challenge(_DummyPage())
        )

    def test_is_cloudflare_challenge_ignores_cf_token_when_usage_content_visible(self) -> None:
        class _DummyPage:
            url = (
                "https://chatgpt.com/codex/settings/usage?"
                "__cf_chl_rt_tk=token-123"
            )

            def evaluate(self, _expr):
                return (
                    "5-hour usage limit\\n"
                    "12 / 40\\n"
                    "weekly usage limit\\n"
                    "111 / 300\\n"
                    "code review\\n"
                    "8 / 50\\n"
                    "remaining credit\\n"
                    "320"
                )

            def content(self):
                return "<html><body><main>usage metrics</main></body></html>"

        self.assertFalse(
            self.monitor._CodexUsageMonitor__is_cloudflare_challenge(_DummyPage())
        )

    def test_is_cloudflare_challenge_ignores_html_marker_when_usage_content_visible(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def evaluate(self, _expr):
                return (
                    "5-hour usage limit\\n"
                    "12 / 40\\n"
                    "weekly usage limit\\n"
                    "111 / 300\\n"
                    "code review\\n"
                    "8 / 50\\n"
                    "remaining credit\\n"
                    "320"
                )

            def content(self):
                return (
                    "<html><head>"
                    "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
                    "</head><body>usage metrics rendered</body></html>"
                )

        self.assertFalse(
            self.monitor._CodexUsageMonitor__is_cloudflare_challenge(_DummyPage())
        )

    def test_launch_browser_context_uses_chrome_only_for_interactive_recovery(self) -> None:
        calls: list[dict] = []

        class _FakeChromium:
            def launch_persistent_context(self, profile_dir, **kwargs):
                calls.append({"profile_dir": profile_dir, "kwargs": dict(kwargs)})
                return object()

        class _FakePlaywright:
            chromium = _FakeChromium()

        ctx = self.monitor._CodexUsageMonitor__launch_browser_context(
            _FakePlaywright(),
            headless=False,
            prefer_system_channel=True,
        )

        self.assertIsNotNone(ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["kwargs"].get("channel"), "chrome")
        self.assertTrue(calls[0]["kwargs"].get("chromium_sandbox"))
        self.assertNotIn("ignore_default_args", calls[0]["kwargs"])
        self.assertNotIn("args", calls[0]["kwargs"])

    def test_wait_until_logged_in_performs_active_login_entry(self) -> None:
        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__is_cloudflare_challenge",
            return_value=False,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__is_login_required",
                side_effect=[True, True, False],
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__try_open_login_entry",
                    return_value=True,
                ) as open_login:
                    with patch.object(
                        self.monitor._CodexUsageMonitor__lib.time,
                        "monotonic",
                        side_effect=[0.0, 0.1, 0.2, 0.3, 0.4],
                    ):
                        ok = self.monitor._CodexUsageMonitor__wait_until_logged_in(page, timeout_sec=5.0)

        self.assertTrue(ok)
        self.assertTrue(open_login.called)

    def test_try_open_login_entry_does_not_reload_auth_login_page(self) -> None:
        class _DummyLocator:
            def count(self):
                return 0

        class _DummyPage:
            url = "https://auth.openai.com/log-in-or-create-account"

            def __init__(self):
                self.goto_calls = []

            def locator(self, _selector):
                return _DummyLocator()

            def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))
                return None

        page = _DummyPage()
        opened = self.monitor._CodexUsageMonitor__try_open_login_entry(page, force=True)

        self.assertFalse(opened)
        self.assertEqual(page.goto_calls, [])

    def test_try_open_login_entry_recovers_invalid_state_with_try_again(self) -> None:
        class _DummyLocator:
            def __init__(self, should_exist: bool, page):
                self._should_exist = bool(should_exist)
                self._page = page

            @property
            def first(self):
                return self

            def count(self):
                return 1 if self._should_exist else 0

            def click(self, timeout=None):
                _ = timeout
                self._page.clicked = True
                return None

        class _DummyPage:
            url = "https://auth.openai.com/log-in-or-create-account"

            def __init__(self):
                self.goto_calls = []
                self.clicked = False

            def evaluate(self, _script):
                return "An error occurred during authentication (invalid_state). Please try again."

            def locator(self, selector):
                if "Try again" in str(selector):
                    return _DummyLocator(True, self)
                return _DummyLocator(False, self)

            def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))
                return None

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        opened = self.monitor._CodexUsageMonitor__try_open_login_entry(page, force=True)

        self.assertTrue(opened)
        self.assertTrue(page.clicked)
        self.assertEqual(page.goto_calls, [])

    def test_try_open_login_entry_recovers_route_error_with_try_again(self) -> None:
        class _DummyLocator:
            def __init__(self, should_exist: bool, page):
                self._should_exist = bool(should_exist)
                self._page = page

            @property
            def first(self):
                return self

            def count(self):
                return 1 if self._should_exist else 0

            def click(self, timeout=None):
                _ = timeout
                self._page.clicked = True
                return None

        class _DummyPage:
            url = "https://auth.openai.com/log-in-or-create-account"

            def __init__(self):
                self.goto_calls = []
                self.clicked = False

            def evaluate(self, _script):
                return "Route Error (400 Invalid content type: text/html; charset=UTF-8)"

            def locator(self, selector):
                if "Try again" in str(selector):
                    return _DummyLocator(True, self)
                return _DummyLocator(False, self)

            def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))
                return None

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        opened = self.monitor._CodexUsageMonitor__try_open_login_entry(page, force=True)

        self.assertTrue(opened)
        self.assertTrue(page.clicked)
        self.assertEqual(page.goto_calls, [])

    def test_wait_for_snapshot_ready_retries_usage_from_chatgpt_home(self) -> None:
        class _DummyPage:
            def __init__(self):
                self.url = "https://chatgpt.com/"
                self.goto_calls = []

            def goto(self, url, **_kwargs):
                self.goto_calls.append(url)
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        page = _DummyPage()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "18 / 40",
                "weekly_limit": "110 / 300",
                "code_review": "9 / 50",
                "remaining_credit": "250",
            },
            captured_at="2026-03-30T12:00:00",
        )

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__build_snapshot_from_page",
            side_effect=[None, snapshot],
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.time,
                "monotonic",
                side_effect=[0.0, 0.2, 0.4, 0.6],
            ):
                got, err = self.monitor._CodexUsageMonitor__wait_for_snapshot_ready(page, timeout_sec=5.0)

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertIn("https://chatgpt.com/codex/settings/usage", page.goto_calls)

    def test_collect_snapshot_once_prefers_cdp_context_for_interactive(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "code_review": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyPage:
            url = "https://chatgpt.com/auth/login?next=/codex/settings/usage"

            def goto(self, url, **_kwargs):
                self.url = str(url)
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, None),
        ) as launch_cdp:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__launch_browser_context",
                side_effect=AssertionError("fallback launch should not be used"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__is_cloudflare_challenge",
                    return_value=False,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_login_required",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__build_snapshot_from_page",
                            return_value=snapshot,
                        ):
                            got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                object(),
                                headless=False,
                                allow_interactive_recovery=True,
                                prefer_system_channel=True,
                                initial_url="https://chatgpt.com/auth/login?next=/codex/settings/usage",
                            )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(launch_cdp.called)
        self.assertFalse(
            bool(launch_cdp.call_args.kwargs.get("start_hidden", True))
        )

    def test_collect_snapshot_once_hides_cdp_window_when_force_hidden(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "code_review": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 12345

        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, _DummyProc()),
        ) as launch_cdp:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_spawned_process",
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__set_cdp_window_visibility",
                ) as set_visibility:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    object(),
                                    headless=False,
                                    allow_interactive_recovery=False,
                                    force_hidden=True,
                                    prefer_system_channel=True,
                                )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(
            bool(launch_cdp.call_args.kwargs.get("start_hidden", False))
        )
        self.assertTrue(set_visibility.called)
        args, kwargs = set_visibility.call_args
        self.assertFalse(kwargs.get("visible", args[1] if len(args) > 1 else True))

    def test_launch_interactive_context_via_cdp_hidden_start_disables_extensions_and_notifications(
        self,
    ) -> None:
        class _DummyProc:
            pid = 43210

            def poll(self):
                return None

        class _DummyContext:
            pass

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def connect_over_cdp(self, _endpoint):
                return _DummyBrowser()

        class _DummyPlaywright:
            chromium = _DummyChromium()

        popen_calls: list[tuple[list[str], dict]] = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append((list(cmd), dict(kwargs)))
            return _DummyProc()

        class _DummyStartupInfo:
            def __init__(self):
                self.dwFlags = 0
                self.wShowWindow = 0

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__resolve_chrome_executable_path",
            return_value="C:/Program Files/Google/Chrome/Application/chrome.exe",
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.os,
                "makedirs",
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.subprocess,
                    "Popen",
                    side_effect=fake_popen,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__find_profile_remote_debugging_pid",
                        side_effect=[0, 43210],
                    ):
                        with patch.object(
                            self.monitor._CodexUsageMonitor__lib.subprocess,
                            "STARTUPINFO",
                            side_effect=_DummyStartupInfo,
                        ):
                            context, browser, proc = (
                                self.monitor._CodexUsageMonitor__launch_interactive_context_via_cdp(
                                    _DummyPlaywright(),
                                    start_hidden=True,
                                )
                            )

        self.assertIsNotNone(context)
        self.assertIsNotNone(browser)
        self.assertIsNotNone(proc)
        self.assertTrue(popen_calls)
        cmd, kwargs = popen_calls[0]
        self.assertIn("--disable-extensions", cmd)
        self.assertIn("--disable-notifications", cmd)
        self.assertNotIn("--window-position=-32000,-32000", cmd)
        self.assertNotIn("about:blank", cmd)
        self.assertIn("https://chatgpt.com/codex/settings/usage", cmd)
        self.assertIn("startupinfo", kwargs)

    def test_launch_interactive_context_via_cdp_retries_when_listener_pid_mismatch(self) -> None:
        class _DummyProc:
            def __init__(self, pid):
                self.pid = int(pid)

            def poll(self):
                return None

        class _DummyContext:
            pass

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def connect_over_cdp(self, _endpoint):
                return _DummyBrowser()

        class _DummyPlaywright:
            chromium = _DummyChromium()

        popen_calls: list[list[str]] = []
        popen_pids = [11111, 22222]

        def fake_popen(cmd, **_kwargs):
            popen_calls.append(list(cmd))
            return _DummyProc(popen_pids[len(popen_calls) - 1])

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__resolve_chrome_executable_path",
            return_value="C:/Program Files/Google/Chrome/Application/chrome.exe",
        ):
            with patch.object(
                self.monitor._CodexUsageMonitor__lib.os,
                "makedirs",
            ):
                with patch.object(
                    self.monitor._CodexUsageMonitor__lib.subprocess,
                    "Popen",
                    side_effect=fake_popen,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__terminate_spawned_process",
                    ) as terminate_proc:
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__find_profile_remote_debugging_pid",
                            side_effect=[0, 99999, 0, 22222],
                        ):
                            context, browser, proc = (
                                self.monitor._CodexUsageMonitor__launch_interactive_context_via_cdp(
                                    _DummyPlaywright(),
                                    start_hidden=False,
                                )
                            )

        self.assertIsNotNone(context)
        self.assertIsNotNone(browser)
        self.assertIsNotNone(proc)
        self.assertEqual(int(getattr(proc, "pid", 0)), 22222)
        self.assertEqual(len(popen_calls), 2)
        self.assertTrue(terminate_proc.called)
        self.assertIn("--remote-debugging-port=9333", popen_calls[0])
        self.assertIn("--remote-debugging-port=9334", popen_calls[1])

    def test_collect_snapshot_once_reuses_hidden_cdp_process_between_calls(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "code_review": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 54321
            _ws_cdp_port = 9333

            def poll(self):
                return None

        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        class _DummyBrowser:
            def __init__(self):
                self.contexts = [_DummyContext()]

            def close(self):
                return None

        class _DummyChromium:
            def __init__(self):
                self.connect_calls = 0

            def connect_over_cdp(self, _endpoint):
                self.connect_calls += 1
                return _DummyBrowser()

        class _DummyPlaywright:
            def __init__(self):
                self.chromium = _DummyChromium()

        pw = _DummyPlaywright()

        self.monitor._CodexUsageMonitor__hidden_cdp_proc = None
        self.monitor._CodexUsageMonitor__hidden_cdp_port = 0

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), _DummyBrowser(), _DummyProc()),
        ) as launch_cdp:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__set_cdp_window_visibility",
                return_value=True,
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__is_cloudflare_challenge",
                    return_value=False,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_login_required",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__build_snapshot_from_page",
                            return_value=snapshot,
                        ):
                            got1, err1 = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                pw,
                                headless=False,
                                allow_interactive_recovery=False,
                                force_hidden=True,
                                prefer_system_channel=True,
                            )
                            got2, err2 = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                pw,
                                headless=False,
                                allow_interactive_recovery=False,
                                force_hidden=True,
                                prefer_system_channel=True,
                            )

        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertIsNotNone(got1)
        self.assertIsNotNone(got2)
        self.assertEqual(launch_cdp.call_count, 1)

    def test_select_collect_page_prefers_non_blank_and_closes_extra_blank_tabs(self) -> None:
        class _DummyPage:
            def __init__(self, url):
                self.url = url
                self.closed = False

            def close(self):
                self.closed = True
                return None

        class _DummyContext:
            def __init__(self, pages):
                self.pages = pages

            def new_page(self):
                p = _DummyPage("about:blank")
                self.pages.append(p)
                return p

        blank1 = _DummyPage("about:blank")
        usage = _DummyPage("https://chatgpt.com/codex/settings/usage")
        blank2 = _DummyPage("chrome://newtab/")
        ctx = _DummyContext([blank1, usage, blank2])

        selected = self.monitor._CodexUsageMonitor__select_collect_page(
            ctx,
            preferred_url="https://chatgpt.com/codex/settings/usage",
            close_extra_blank_tabs=True,
        )

        self.assertIs(selected, usage)
        self.assertTrue(blank1.closed)
        self.assertTrue(blank2.closed)

    def test_collect_snapshot_once_background_waits_briefly_for_cloudflare_clear(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "code_review": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyProc:
            pid = 12345

        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, _DummyProc()),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_spawned_process",
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__set_cdp_window_visibility",
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        side_effect=[True, False],
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__wait_until_cloudflare_cleared",
                            return_value=True,
                        ) as wait_cf:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__is_login_required",
                                return_value=False,
                            ):
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__build_snapshot_from_page",
                                    return_value=snapshot,
                                ):
                                    got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                        object(),
                                        headless=False,
                                        allow_interactive_recovery=False,
                                        force_hidden=True,
                                        prefer_system_channel=True,
                                    )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(wait_cf.called)

    def test_collect_snapshot_once_background_returns_cloudflare_when_challenge_persists(self) -> None:
        class _DummyProc:
            pid = 12345

        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(_DummyContext(), None, _DummyProc()),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__terminate_spawned_process",
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__set_cdp_window_visibility",
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=True,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__wait_until_cloudflare_cleared",
                            return_value=False,
                        ) as wait_cf:
                            got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                object(),
                                headless=False,
                                allow_interactive_recovery=False,
                                force_hidden=True,
                                prefer_system_channel=True,
                            )

        self.assertIsNone(got)
        self.assertEqual(err, "cloudflare_challenge")
        self.assertTrue(wait_cf.called)

    def test_collect_snapshot_once_force_hidden_uses_headless_fallback_when_cdp_unavailable(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "code_review": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_interactive_context_via_cdp",
            return_value=(None, None, None),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__launch_browser_context",
                return_value=_DummyContext(),
            ) as launch_context:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__apply_headless_fast_routes",
                ) as fast_routes:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_cloudflare_challenge",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__is_login_required",
                            return_value=False,
                        ):
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__build_snapshot_from_page",
                                return_value=snapshot,
                            ):
                                got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                    object(),
                                    headless=False,
                                    allow_interactive_recovery=False,
                                    force_hidden=True,
                                    prefer_system_channel=True,
                                )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(launch_context.called)
        self.assertTrue(fast_routes.called)
        self.assertTrue(launch_context.call_args.kwargs.get("headless"))

    def test_set_cdp_window_visibility_falls_back_to_profile_pids(self) -> None:
        class _DummyProc:
            pid = 111

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__set_windows_visibility_for_pid",
            side_effect=[False, True],
        ) as set_by_pid:
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__list_profile_chrome_pids",
                return_value=[111, 222],
            ):
                ok = self.monitor._CodexUsageMonitor__set_cdp_window_visibility(
                    _DummyProc(),
                    visible=False,
                    bring_to_front=False,
                    timeout_sec=1.0,
                )

        self.assertTrue(ok)
        called_pids: list[int] = []
        for call in set_by_pid.call_args_list:
            if call.args:
                called_pids.append(int(call.args[0]))
            else:
                called_pids.append(int(call.kwargs.get("pid")))
        self.assertIn(111, called_pids)
        self.assertIn(222, called_pids)

    def test_configure_playwright_env_adds_no_deprecation_node_option_once(self) -> None:
        with patch.dict(self.monitor._CodexUsageMonitor__lib.os.environ, {}, clear=True):
            self.monitor._CodexUsageMonitor__configure_playwright_env()
            first = str(
                self.monitor._CodexUsageMonitor__lib.os.environ.get("NODE_OPTIONS", "")
            )
            self.assertIn("--no-deprecation", first)
            self.monitor._CodexUsageMonitor__configure_playwright_env()
            second = str(
                self.monitor._CodexUsageMonitor__lib.os.environ.get("NODE_OPTIONS", "")
            )
            self.assertEqual(first, second)

    def test_collect_snapshot_once_applies_headless_fast_routes(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "16 / 40",
                "weekly_limit": "108 / 300",
                "code_review": "7 / 50",
                "remaining_credit": "240",
            },
            captured_at="2026-03-30T12:10:00",
        )

        class _DummyPage:
            url = "https://chatgpt.com/codex/settings/usage"

            def goto(self, _url, **_kwargs):
                return None

            def wait_for_timeout(self, _ms):
                return None

        class _DummyContext:
            def __init__(self):
                self.pages = [_DummyPage()]

            def close(self):
                return None

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__launch_browser_context",
            return_value=_DummyContext(),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__apply_headless_fast_routes",
            ) as fast_routes:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__is_cloudflare_challenge",
                    return_value=False,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__is_login_required",
                        return_value=False,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__build_snapshot_from_page",
                            return_value=snapshot,
                        ):
                            got, err = self.monitor._CodexUsageMonitor__collect_snapshot_once(
                                object(),
                                headless=True,
                                prefer_system_channel=True,
                            )

        self.assertIsNone(err)
        self.assertIsNotNone(got)
        self.assertTrue(fast_routes.called)

    def test_build_snapshot_rejects_remaining_credit_only_noise(self) -> None:
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__extract_metrics",
            return_value={"remaining_credit": "0"},
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__now_iso",
                return_value="2026-03-30T12:20:00",
            ):
                snap = self.monitor._CodexUsageMonitor__build_snapshot_from_page(object())

        self.assertIsNone(snap)

    def test_show_change_tooltip_also_shows_current_credit(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        current = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "26%",
                "weekly_limit": "28%",
                "code_review": "100%",
                "remaining_credit": "959",
            },
            captured_at="2026-03-30T12:30:00",
        )
        changes = self.monitor.handle_snapshot(current)
        self.assertEqual(changes, [])
        only_change = [
            self.monitor.handle_snapshot(
                UsageSnapshot.from_metrics(
                    {
                        "five_hour_limit": "25%",
                        "weekly_limit": "28%",
                        "code_review": "100%",
                        "remaining_credit": "959",
                    },
                    captured_at="2026-03-30T12:35:00",
                )
            )[0]
        ]

        captured = {}

        def fake_show(text, lines=None, duration_ms=None):
            captured["text"] = text
            captured["lines"] = lines or []
            captured["duration_ms"] = duration_ms

        with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip", side_effect=fake_show):
            self.monitor._CodexUsageMonitor__show_change_tooltip(
                only_change,
                self.monitor.get_last_snapshot(),
            )

        lines = captured.get("lines", [])
        joined = " | ".join(str(line[0]) for line in lines)
        self.assertTrue(lines)
        self.assertEqual(lines[0][0], "Codex 현재 사용량")
        self.assertIn("변경 항목", joined)
        self.assertIn("남은 크레딧: 959", joined)

    def test_show_change_tooltip_uses_red_and_green_colors(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "code_review": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:40:00",
        )
        changes = [
            UsageChange(
                key="five_hour_limit",
                label="5시간 사용 한도",
                before="26%",
                after="25%",
            ),
            UsageChange(
                key="remaining_credit",
                label="남은 크레딧",
                before="959",
                after="960",
            ),
        ]
        captured = {}

        def fake_show(text, lines=None, duration_ms=None):
            captured["text"] = text
            captured["lines"] = lines or []
            captured["duration_ms"] = duration_ms

        with patch.object(self.monitor, "_CodexUsageMonitor__show_tooltip", side_effect=fake_show):
            self.monitor._CodexUsageMonitor__show_change_tooltip(changes, snapshot)

        color_map = {str(line[0]): line[1] for line in captured.get("lines", [])}
        self.assertEqual(
            color_map.get("- 5시간 사용 한도: 26% -> 25%"),
            "#DC2626",
        )
        self.assertEqual(
            color_map.get("- 남은 크레딧: 959 -> 960"),
            "#16A34A",
        )

    def test_build_snapshot_lines_formats_timestamp_without_t(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "code_review": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:45:00",
        )
        lines = self.monitor._CodexUsageMonitor__build_snapshot_lines(snapshot)
        joined = " | ".join(str(line[0]) for line in lines)
        self.assertIn("확인 시각: 2026-03-30 12:45:00", joined)

    def test_build_snapshot_lines_converts_utc_timestamp_to_kst(self) -> None:
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "code_review": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T00:00:00+00:00",
        )
        lines = self.monitor._CodexUsageMonitor__build_snapshot_lines(snapshot)
        joined = " | ".join(str(line[0]) for line in lines)
        self.assertIn("확인 시각: 2026-03-30 09:00:00", joined)

    def test_show_current_status_shows_loading_tooltip_for_manual_query(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "code_review": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T12:50:00",
        )
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                def fake_collect_guarded(source, on_acquired=None):
                    _ = source
                    if callable(on_acquired):
                        on_acquired()
                    return snapshot, None

                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    side_effect=fake_collect_guarded,
                ):
                    with patch.object(self.monitor, "_CodexUsageMonitor__save_state"):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_tooltip",
                            side_effect=fake_show,
                        ):
                            self.monitor.show_current_status(force_refresh=True)

        self.assertTrue(shown)
        self.assertEqual(shown[0], ("Codex 사용량 조회 중...", None, 0))
        titles = [entry[1][0][0] for entry in shown if entry[1]]
        self.assertIn("Codex 현재 사용량", titles)

    def test_show_current_status_ignores_when_collect_already_busy(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "collect_busy"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                        side_effect=fake_show,
                    ):
                        self.monitor.show_current_status(force_refresh=True)

        self.assertEqual(len(shown), 1)
        self.assertIn("이미 Codex 사용량 조회가 진행 중입니다.", shown[0][0])
        self.assertIn("완료되면 결과를 자동으로 표시합니다.", shown[0][0])
        self.assertEqual(shown[0][2], 0)

    def test_show_current_status_busy_does_not_show_old_snapshot_lines(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "code_review": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30 12:58:00",
        )
        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "collect_busy"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                        side_effect=fake_show,
                    ):
                        self.monitor.show_current_status(force_refresh=True)

        self.assertEqual(len(shown), 1)
        text, lines, _duration = shown[0]
        self.assertIn("이미 Codex 사용량 조회가 진행 중입니다.", text)
        self.assertIsNone(lines)

    def test_monitor_tick_shows_pending_manual_snapshot_after_busy(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__set_manual_query_pending_result()

        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "code_review": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30T12:55:00",
        )

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(snapshot, None),
            ):
                with patch.object(
                    self.monitor,
                    "handle_snapshot",
                    return_value=[],
                ):
                    with patch.object(
                        self.monitor,
                        "get_last_snapshot",
                        return_value=snapshot,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_snapshot_tooltip",
                        ) as show_snapshot:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__ui_post",
                                side_effect=lambda fn: fn(),
                            ):
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__schedule_monitor_tick",
                                ):
                                    self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertTrue(show_snapshot.called)

    def test_monitor_tick_shows_pending_manual_error_after_busy(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__set_manual_query_pending_result()

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        captured: list[str] = []

        def fake_tooltip(text, lines=None, duration_ms=None):
            _ = lines
            _ = duration_ms
            captured.append(str(text or ""))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "collect_failed"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_tooltip",
                    side_effect=fake_tooltip,
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                        ):
                            self.monitor._CodexUsageMonitor__monitor_tick()

        joined = " | ".join(captured)
        self.assertIn("진행 중이던 조회가 실패했습니다.", joined)
        self.assertIn("조회 작업 중 오류가 발생했습니다.", joined)

    def test_show_current_status_force_refresh_parse_failed_does_not_show_old_snapshot(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__last_snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "code_review": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30 12:58:00",
        )

        shown: list[tuple[str, list[tuple[str, str | None]] | None, int | None]] = []

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_show(text, lines=None, duration_ms=None):
            shown.append((str(text or ""), lines, duration_ms))

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__collect_snapshot_guarded",
                    return_value=(None, "parse_failed"),
                ):
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__show_tooltip",
                        side_effect=fake_show,
                    ):
                        self.monitor.show_current_status(force_refresh=True)

        self.assertTrue(shown)
        self.assertIn("사용량 조회 실패:", shown[-1][0])
        self.assertIn("페이지에서 사용량을 읽지 못했습니다.", shown[-1][0])
        self.assertIsNone(shown[-1][1])

    def test_monitor_tick_retries_once_for_pending_manual_parse_failed(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__set_manual_query_pending_result()

        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "24%",
                "weekly_limit": "27%",
                "code_review": "100%",
                "remaining_credit": "958",
            },
            captured_at="2026-03-30 13:05:00",
        )

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                side_effect=[(None, "parse_failed"), (snapshot, None)],
            ):
                with patch.object(
                    self.monitor,
                    "handle_snapshot",
                    return_value=[],
                ):
                    with patch.object(
                        self.monitor,
                        "get_last_snapshot",
                        return_value=snapshot,
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__show_snapshot_tooltip",
                        ) as show_snapshot:
                            with patch.object(
                                self.monitor,
                                "_CodexUsageMonitor__show_tooltip",
                            ) as show_text_tip:
                                with patch.object(
                                    self.monitor,
                                    "_CodexUsageMonitor__ui_post",
                                    side_effect=lambda fn: fn(),
                                ):
                                    with patch.object(
                                        self.monitor,
                                        "_CodexUsageMonitor__schedule_monitor_tick",
                                    ):
                                        self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertTrue(show_snapshot.called)
        self.assertFalse(show_text_tip.called)

    def test_get_runtime_status_exposes_collecting_and_countdown(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__collect_inflight = True
        self.monitor._CodexUsageMonitor__collect_inflight_source = "manual_query"
        self.monitor._CodexUsageMonitor__monitor_running = True
        self.monitor._CodexUsageMonitor__startup_warmup_running = False
        self.monitor._CodexUsageMonitor__failure_count = 2
        self.monitor._CodexUsageMonitor__next_collect_due_ts = 101.25

        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=100.0,
        ):
            status = self.monitor.get_runtime_status()

        self.assertTrue(status.get("collect_inflight"))
        self.assertEqual(status.get("collect_source"), "manual_query")
        self.assertAlmostEqual(float(status.get("next_collect_in_sec")), 1.25, places=2)
        self.assertFalse(bool(status.get("next_collect_estimated")))

    def test_get_runtime_status_estimates_countdown_while_running(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__monitor_running = True
        self.monitor._CodexUsageMonitor__startup_warmup_running = False
        self.monitor._CodexUsageMonitor__collect_inflight = True
        self.monitor._CodexUsageMonitor__collect_started_ts = 90.0
        self.monitor._CodexUsageMonitor__interval_sec = 30.0
        self.monitor._CodexUsageMonitor__next_collect_due_ts = 0.0

        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=100.0,
        ):
            status = self.monitor.get_runtime_status()

        self.assertTrue(bool(status.get("next_collect_estimated")))
        self.assertAlmostEqual(float(status.get("next_collect_in_sec")), 20.0, places=2)

    def test_update_settings_allows_ten_second_interval(self) -> None:
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        with patch.object(self.monitor, "_CodexUsageMonitor__save_settings"):
            with patch.object(self.monitor, "_CodexUsageMonitor__restart_monitor"):
                ok, err = self.monitor.update_settings(
                    {
                        "enabled": True,
                        "interval_sec": 10,
                        "tooltip_duration_ms": 7000,
                        "usage_url": "https://chatgpt.com/codex/settings/usage",
                    }
                )

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(self.monitor._CodexUsageMonitor__interval_sec, 10.0)

    def test_load_settings_clamps_interval_to_ten_seconds_minimum(self) -> None:
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__read_json_file",
            return_value={"enabled": True, "interval_sec": 3},
        ):
            with patch.object(self.monitor, "_CodexUsageMonitor__save_settings"):
                self.monitor._CodexUsageMonitor__load_settings()

        self.assertEqual(self.monitor._CodexUsageMonitor__interval_sec, 10.0)

    def test_handle_collect_error_cloudflare_background_guides_manual_query(self) -> None:
        captured: list[str] = []

        def fake_tooltip(text, lines=None, duration_ms=None):
            _ = lines
            _ = duration_ms
            captured.append(str(text or ""))

        self.monitor._CodexUsageMonitor__last_login_notice_ts = 0.0
        self.monitor._CodexUsageMonitor__login_notice_cooldown_sec = 0.0
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=123.0,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_tooltip",
                    side_effect=fake_tooltip,
                ):
                    self.monitor._CodexUsageMonitor__handle_collect_error(
                        "cloudflare_challenge",
                        source="monitor_tick",
                    )

        self.assertTrue(captured)
        self.assertIn("Ctrl+Alt+C", captured[-1])
        self.assertNotIn("열린 브라우저 창", captured[-1])

    def test_handle_collect_error_cloudflare_manual_avoids_open_window_assumption(self) -> None:
        captured: list[str] = []

        def fake_tooltip(text, lines=None, duration_ms=None):
            _ = lines
            _ = duration_ms
            captured.append(str(text or ""))

        self.monitor._CodexUsageMonitor__last_login_notice_ts = 0.0
        self.monitor._CodexUsageMonitor__login_notice_cooldown_sec = 0.0
        with patch.object(
            self.monitor._CodexUsageMonitor__lib.time,
            "monotonic",
            return_value=123.0,
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__ui_post",
                side_effect=lambda fn: fn(),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__show_tooltip",
                    side_effect=fake_tooltip,
                ):
                    self.monitor._CodexUsageMonitor__handle_collect_error(
                        "cloudflare_challenge",
                        source="manual_query",
                    )

        self.assertTrue(captured)
        self.assertIn("열리지 않으면", captured[-1])
        self.assertNotIn("열린 브라우저 창", captured[-1])

    def test_now_iso_is_korea_time(self) -> None:
        class _FakeDatetime:
            @staticmethod
            def now(_tz=None):
                return datetime(2026, 3, 30, 0, 0, 0, tzinfo=timezone.utc)

        self.monitor._CodexUsageMonitor__lib.datetime = _FakeDatetime
        got = self.monitor._CodexUsageMonitor__now_iso()
        self.assertEqual(got, "2026-03-30 09:00:00")

    def test_format_captured_at_for_display_converts_utc_to_kst(self) -> None:
        got = self.monitor.format_captured_at_for_display("2026-03-30T00:00:00+00:00")
        self.assertEqual(got, "2026-03-30 09:00:00")

    def test_collect_snapshot_guarded_uses_non_blocking_busy_skip(self) -> None:
        class _BusyLock:
            def __init__(self):
                self.acquire_calls: list[tuple[tuple, dict]] = []

            def acquire(self, *args, **kwargs):
                self.acquire_calls.append((args, kwargs))
                return False

            def release(self):
                raise AssertionError("release should not be called when acquire fails")

        busy_lock = _BusyLock()
        self.monitor._CodexUsageMonitor__collect_lock = busy_lock

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot",
            side_effect=AssertionError("collect should be skipped when busy"),
        ):
            snapshot, error = self.monitor._CodexUsageMonitor__collect_snapshot_guarded(
                source="manual_query"
            )

        self.assertIsNone(snapshot)
        self.assertEqual(error, "collect_busy")
        self.assertTrue(busy_lock.acquire_calls)
        args, kwargs = busy_lock.acquire_calls[0]
        is_non_blocking = bool(kwargs.get("blocking") is False or (len(args) >= 1 and args[0] is False))
        self.assertTrue(is_non_blocking)

    def test_monitor_tick_busy_collect_is_ignored_without_error_handler(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "collect_busy"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__handle_collect_error",
                ) as handle_error:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                        ) as schedule_tick:
                            self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertFalse(handle_error.called)
        self.assertTrue(schedule_tick.called)
        self.assertEqual(
            schedule_tick.call_args.kwargs.get("initial_delay_sec"),
            5.0,
        )

    def test_startup_warmup_busy_collect_is_ignored_without_error_handler(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__interval_sec = 90.0

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "collect_busy"),
            ):
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__handle_collect_error",
                ) as handle_error:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                        ) as schedule_tick:
                            self.monitor._CodexUsageMonitor__start_startup_warmup()

        self.assertFalse(handle_error.called)
        self.assertTrue(schedule_tick.called)
        self.assertEqual(
            schedule_tick.call_args.kwargs.get("initial_delay_sec"),
            5.0,
        )

    def test_on_worker_done_ignores_stale_worker_epoch(self) -> None:
        self.monitor._CodexUsageMonitor__worker_epoch = 3
        self.monitor._CodexUsageMonitor__monitor_running = True
        self.monitor._CodexUsageMonitor__startup_warmup_running = True

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__schedule_monitor_tick",
        ) as schedule_tick:
            self.monitor._CodexUsageMonitor__on_worker_done(
                5.0,
                worker_epoch=2,
                from_startup=True,
            )

        self.assertTrue(self.monitor._CodexUsageMonitor__monitor_running)
        self.assertTrue(self.monitor._CodexUsageMonitor__startup_warmup_running)
        self.assertFalse(schedule_tick.called)

    def test_monitor_worker_stale_epoch_skips_snapshot_apply(self) -> None:
        self.monitor._CodexUsageMonitor__enabled = True
        self.monitor._CodexUsageMonitor__interval_sec = 90.0
        self.monitor._CodexUsageMonitor__monitor_running = False
        self.monitor._CodexUsageMonitor__worker_epoch = 1

        snapshot = UsageSnapshot.from_metrics(
            {
                "five_hour_limit": "25%",
                "weekly_limit": "28%",
                "code_review": "100%",
                "remaining_credit": "960",
            },
            captured_at="2026-03-30T13:10:00",
        )

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        def fake_collect_guarded(source, on_acquired=None):
            _ = source
            _ = on_acquired
            self.monitor._CodexUsageMonitor__worker_epoch = 2
            return snapshot, None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                side_effect=fake_collect_guarded,
            ):
                with patch.object(
                    self.monitor,
                    "handle_snapshot",
                ) as handle_snapshot:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                        ) as schedule_tick:
                            self.monitor._CodexUsageMonitor__monitor_tick()

        self.assertFalse(handle_snapshot.called)
        self.assertFalse(schedule_tick.called)

    def test_restart_monitor_uses_startup_warmup(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__start_startup_warmup",
        ) as warmup:
            self.monitor._CodexUsageMonitor__restart_monitor()

        self.assertTrue(warmup.called)

    def test_restart_monitor_defers_hidden_cdp_clear_while_collect_inflight(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__collect_inflight = True
        self.monitor._CodexUsageMonitor__pending_hidden_cdp_clear = False

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__start_startup_warmup",
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__clear_hidden_cdp_process",
            ) as clear_hidden:
                self.monitor._CodexUsageMonitor__restart_monitor()

        self.assertFalse(clear_hidden.called)
        self.assertTrue(self.monitor._CodexUsageMonitor__pending_hidden_cdp_clear)

    def test_collect_snapshot_guarded_clears_deferred_hidden_cdp_after_inflight_done(self) -> None:
        self.monitor._CodexUsageMonitor__pending_hidden_cdp_clear = True

        with patch.object(
            self.monitor,
            "_CodexUsageMonitor__collect_snapshot",
            return_value=(None, "collect_failed"),
        ):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__clear_hidden_cdp_process",
            ) as clear_hidden:
                _snap, err = self.monitor._CodexUsageMonitor__collect_snapshot_guarded(
                    source="manual_query"
                )

        self.assertEqual(err, "collect_failed")
        self.assertTrue(clear_hidden.called)
        self.assertFalse(self.monitor._CodexUsageMonitor__pending_hidden_cdp_clear)

    def test_startup_warmup_runs_headless_first_collect_path(self) -> None:
        self.monitor._CodexUsageMonitor__root = object()
        self.monitor._CodexUsageMonitor__enabled = True

        class _InlineThread:
            def __init__(self, target=None, daemon=None):
                _ = daemon
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()
                return None

        with patch("src.apps.codex_usage_monitor.threading.Thread", _InlineThread):
            with patch.object(
                self.monitor,
                "_CodexUsageMonitor__collect_snapshot_guarded",
                return_value=(None, "parse_failed"),
            ) as collect:
                with patch.object(
                    self.monitor,
                    "_CodexUsageMonitor__handle_collect_error",
                ) as handle_error:
                    with patch.object(
                        self.monitor,
                        "_CodexUsageMonitor__ui_post",
                        side_effect=lambda fn: fn(),
                    ):
                        with patch.object(
                            self.monitor,
                            "_CodexUsageMonitor__schedule_monitor_tick",
                            return_value=None,
                        ):
                            self.monitor._CodexUsageMonitor__start_startup_warmup()

        self.assertTrue(collect.called)
        self.assertEqual(collect.call_args.kwargs.get("source"), "startup_warmup")
        self.assertTrue(handle_error.called)


if __name__ == "__main__":
    unittest.main()
