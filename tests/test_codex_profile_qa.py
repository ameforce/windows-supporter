"""Independent regression checks; all browser traffic is synthetic and intercepted."""

import json
import os
import tempfile
import time
import unittest

from playwright.sync_api import sync_playwright

from src.apps.codex_usage_browser_types import PlaywrightSessionConfig, parse_usage_probe
from src.apps.codex_usage_playwright_driver import CodexUsagePlaywrightDriver
from src.apps.codex_usage_monitor import (
    CodexUsageMonitor,
    USAGE_LIMIT_RESET_AT_KEY_BY_METRIC,
    USAGE_PAGE_PROBE_SCRIPT,
    UsageSnapshot,
    merge_snapshot_with_previous,
)


class ProfileFailureBoundaryQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.requests = []
        self.session = {
            "user": {"id": "user-a", "name": "Login Name"},
            "account": {"id": "account-a"},
            "accessToken": "synthetic-qa-token",
        }
        self.profile = {"user_id": "user-a", "display_name": "Daeng - enmsoftware"}
        self.mode = "normal"
        self.context.route("**/*", self.route)
        self.page.goto("https://chatgpt.com/codex/cloud/settings/analytics#usage")
        self.page.evaluate("""() => {
            localStorage.setItem('cache/user-stale', JSON.stringify({
                user_id: 'user-stale', display_name: 'Stale Account Name'
            }));
        }""")

    def tearDown(self):
        self.context.close()

    def route(self, route):
        url = route.request.url
        self.requests.append(url)
        if url.endswith("/api/auth/session"):
            route.fulfill(json=self.session)
        elif "/backend-api/calpico/chatgpt/profile/" in url:
            if self.mode == "redirect":
                route.fulfill(status=302, headers={"Location": "https://qa.invalid/leak"})
            elif self.mode == "invalid-json":
                route.fulfill(content_type="application/json", body="{broken")
            else:
                route.fulfill(json=self.profile)
        elif url.startswith("https://chatgpt.com/codex/cloud/settings/analytics"):
            route.fulfill(content_type="text/html", body=(
                "<main><article><h2>Weekly usage limit</h2>"
                "<p>100% remaining</p></article></main>"
                "<button aria-label='Profile menu'>Stale DOM Name</button>"
            ))
        else:
            route.abort()

    def probe(self):
        return self.page.evaluate(USAGE_PAGE_PROBE_SCRIPT)

    def test_redirect_is_rejected_before_followup_request(self):
        self.mode = "redirect"
        probe = self.probe()
        self.assertEqual(probe["profileName"], "")
        self.assertEqual(probe["accountId"], "account-a")
        self.assertTrue(probe["metricBlocks"])
        self.assertFalse(any("qa.invalid" in url for url in self.requests))

    def test_invalid_json_preserves_verified_name_through_monitor(self):
        good = self.probe()
        self.mode = "invalid-json"
        failed = self.probe()
        self.assertEqual(failed["profileName"], "")
        with tempfile.TemporaryDirectory() as tmp:
            monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
            build = monitor._CodexUsageMonitor__build_snapshot_from_probe
            self.assertIsNotNone(build(good))
            self.assertIsNotNone(build(failed))
            self.assertEqual(monitor.get_runtime_status()["profile_name"], "Daeng - enmsoftware")
            failed["accountId"] = "account-other"
            failed["profileName"] = "Wrong Account Name"
            self.assertIsNone(build(failed))
            self.assertEqual(monitor.get_runtime_status()["profile_name"], "Daeng - enmsoftware")

    def test_response_body_timeout_is_bounded_and_usage_survives(self):
        self.page.evaluate("""() => {
            const originalFetch = window.fetch.bind(window);
            window.fetch = async (path, options) => {
                if (!String(path).includes('/calpico/')) return originalFetch(path, options);
                return {status: 200, ok: true, json: () => new Promise((resolve, reject) => {
                    options.signal.addEventListener('abort', () => {
                        window.qaBodyAborted = true;
                        reject(new DOMException('Synthetic stalled body', 'AbortError'));
                    }, {once: true});
                })};
            };
        }""")
        started = time.monotonic()
        probe = self.probe()
        self.assertLess(time.monotonic() - started, 8)
        self.assertTrue(self.page.evaluate("window.qaBodyAborted"))
        self.assertEqual(probe["profileName"], "")
        self.assertEqual(probe["accountId"], "account-a")
        self.assertTrue(probe["metricBlocks"])

    def test_missing_access_token_cannot_use_stale_cache_or_login_name(self):
        self.session.pop("accessToken")
        probe = self.probe()
        self.assertEqual(probe["profileName"], "")
        self.assertTrue(probe["metricBlocks"])
        self.assertFalse(any("/calpico/" in url for url in self.requests))

    def test_empty_custom_name_requires_matching_user_before_fallback(self):
        self.profile["display_name"] = ""
        self.assertEqual(self.probe()["profileName"], "Login Name")
        self.profile["user_id"] = "user-stale"
        self.assertEqual(self.probe()["profileName"], "")

    def test_cache_does_not_override_fresh_name_and_token_is_not_serialized(self):
        probe = self.probe()
        self.assertEqual(probe["profileName"], "Daeng - enmsoftware")
        self.assertNotIn("synthetic-qa-token", json.dumps(probe))
        self.profile["display_name"] = "Daeng - ameforce"
        self.assertEqual(self.probe()["profileName"], "Daeng - ameforce")

    def test_verified_structured_name_keeps_literal_words_in_browser(self):
        # GitHub review 3955829757: API names are data, not decorated menu labels.
        for name in ("Alice Pro", "Profile"):
            with self.subTest(name=name):
                self.profile["display_name"] = name
                self.assertEqual(self.probe()["profileName"], name)

    def test_verified_structured_name_survives_binding_and_persistence(self):
        # Keep browser provenance, but supply the literal expected field to isolate
        # Python binding/reload from the independent JavaScript stripping failure.
        for name in ("Alice Pro", "Profile"):
            with tempfile.TemporaryDirectory() as tmp:
                self.profile["display_name"] = name
                probe = self.probe()
                probe["profileName"] = name
                probe = parse_usage_probe(probe)
                monitor = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
                self.assertIsNotNone(monitor._CodexUsageMonitor__build_snapshot_from_probe(probe))
                with self.subTest(name=name, stage="bind"):
                    self.assertEqual(monitor.get_runtime_status()["profile_name"], name)
                monitor._CodexUsageMonitor__save_state()
                reloaded = CodexUsageMonitor(config_dir=tmp, profile_dir=os.path.join(tmp, "profile"))
                with self.subTest(name=name, stage="reload"):
                    self.assertEqual(reloaded.get_runtime_status()["profile_name"], name)

    def test_incomplete_dom_does_not_multiply_identity_timeouts(self):
        # GitHub review 3955829763. Exercise the real readiness loop and script;
        # accelerate only browser timers, retaining requested timeout durations.
        self.page.evaluate("""() => {
            document.querySelector('main').innerHTML = '<article><h2>Credits remaining</h2><p>100</p></article>';
            const originalTimer = window.setTimeout.bind(window);
            const originalFetch = window.fetch.bind(window);
            window.qaTimeouts = [];
            window.qaProfileCalls = 0;
            window.qaSessionCalls = 0;
            window.qaStallProfile = true;
            window.setTimeout = (callback, delay, ...args) => {
                window.qaTimeouts.push(delay);
                return originalTimer(callback, Math.min(delay, 25), ...args);
            };
            window.fetch = async (path, options) => {
                if (String(path).includes('/api/auth/session')) window.qaSessionCalls++;
                if (!String(path).includes('/calpico/')) return originalFetch(path, options);
                window.qaProfileCalls++;
                if (!window.qaStallProfile) return originalFetch(path, options);
                return new Promise((resolve, reject) => {
                    options.signal.addEventListener('abort', () => reject(
                        new DOMException('Synthetic unavailable profile', 'AbortError')
                    ), {once: true});
                });
            };
        }""")
        driver = CodexUsagePlaywrightDriver(
            PlaywrightSessionConfig("synthetic-profile", self.page.url, USAGE_PAGE_PROBE_SCRIPT),
            sleep=lambda _: None,
        )
        probe = driver._evaluate_probe_until_ready(self.page)
        self.assertIsNotNone(probe)
        self.assertEqual({block["metric_key"] for block in probe["metricBlocks"]}, {"remaining_credit"})
        counts = self.page.evaluate("({profileCalls: window.qaProfileCalls, sessionCalls: window.qaSessionCalls, requestedTimeouts: window.qaTimeouts})")
        self.assertEqual(counts["profileCalls"], 0, counts)
        self.assertEqual(counts["sessionCalls"], 0, counts)
        self.page.evaluate("""() => {
            window.qaStallProfile = false;
            document.querySelector('main').innerHTML = '<article><h2>Weekly usage limit</h2><p>100% remaining</p></article>';
        }""")
        ready = driver._evaluate_probe_until_ready(self.page)
        self.assertEqual(ready["profileName"], "Daeng - enmsoftware")
        self.assertEqual(self.page.evaluate("window.qaProfileCalls"), 1)
        self.profile["display_name"] = "Daeng - ameforce"
        refreshed = driver._evaluate_probe_until_ready(self.page)
        self.assertEqual(refreshed["profileName"], "Daeng - ameforce")
        self.assertEqual(self.page.evaluate("window.qaProfileCalls"), 2)


class ResetMergeIsolationQA(unittest.TestCase):
    def test_all_limit_windows_clear_absent_reset_including_zero_remaining(self):
        for metric, reset in USAGE_LIMIT_RESET_AT_KEY_BY_METRIC.items():
            for value in ("0%", "100%"):
                with self.subTest(metric=metric, value=value):
                    previous = UsageSnapshot.from_metrics(
                        {metric: value}, reset_info={reset: "2026-09-10T12:00:00+09:00"}
                    )
                    current = UsageSnapshot.from_metrics({metric: value}, reported_metric_keys=(metric,))
                    self.assertEqual(getattr(merge_snapshot_with_previous(current, previous), reset), "")

    def test_mixed_parse_failure_and_success_preserve_only_failed_window(self):
        previous = UsageSnapshot.from_metrics(
            {"five_hour_limit": "43%", "weekly_limit": "100%"},
            reset_info={
                "five_hour_limit_reset_at": "2026-09-09T10:00:00+09:00",
                "weekly_limit_reset_at": "2026-09-15T10:00:00+09:00",
            },
        )
        current = UsageSnapshot.from_metrics(
            {"weekly_limit": "100%"}, reported_metric_keys=("five_hour_limit", "weekly_limit")
        )
        merged = merge_snapshot_with_previous(current, previous)
        self.assertEqual(merged.five_hour_limit, "43%")
        self.assertEqual(merged.five_hour_limit_reset_at, previous.five_hour_limit_reset_at)
        self.assertEqual(merged.weekly_limit_reset_at, "")

    def test_legacy_partial_payload_retains_existing_reset_contract(self):
        previous = UsageSnapshot.from_metrics(
            {"weekly_limit": "55%"},
            reset_info={"weekly_limit_reset_at": "2026-09-15T10:00:00+09:00"},
        )
        current = UsageSnapshot.from_metrics({"weekly_limit": "55%"})
        self.assertEqual(
            merge_snapshot_with_previous(current, previous).weekly_limit_reset_at,
            previous.weekly_limit_reset_at,
        )


if __name__ == "__main__":
    unittest.main()
