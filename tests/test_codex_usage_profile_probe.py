"""Exercise the actual browser probe against isolated, synthetic provider responses."""

import json
import unittest

from playwright.sync_api import sync_playwright

from src.apps.codex_usage_monitor import USAGE_PAGE_PROBE_SCRIPT


class CodexProfileProbeTest(unittest.TestCase):
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
            "user": {"id": "user-current", "name": "Login Name"},
            "account": {"id": "account-current", "planType": "pro"},
            "accessToken": "synthetic-test-token",
        }
        self.profile = {"user_id": "user-current", "display_name": "Daeng - enmsoftware"}
        self.profile_status = 200
        self.session_status = 200
        self.context.route("**/*", self._route)

    def tearDown(self):
        self.context.close()

    def _route(self, route):
        request = route.request
        self.requests.append((request.url, request.headers))
        if request.url.endswith("/api/auth/session"):
            route.fulfill(status=self.session_status, json=self.session)
        elif "/backend-api/calpico/chatgpt/profile/" in request.url:
            route.fulfill(status=self.profile_status, json=self.profile)
        else:
            route.fulfill(content_type="text/html", body="""
                <main><article><h2>Weekly usage limit</h2><p>100% remaining</p></article></main>
                <button aria-label="Profile menu">Stale DOM Name</button>
                <script>window.profileClicks = 0;
                document.querySelector('button').onclick = () => window.profileClicks++;</script>
            """)

    def _probe(self):
        self.page.goto("https://chatgpt.com/codex/cloud/settings/analytics#usage")
        return self.page.evaluate(USAGE_PAGE_PROBE_SCRIPT)

    def test_fresh_custom_name_wins_without_opening_profile_menu(self):
        probe = self._probe()
        self.assertEqual(probe["profileName"], "Daeng - enmsoftware")
        self.assertEqual(probe["accountId"], "account-current")
        self.assertEqual(self.page.evaluate("window.profileClicks"), 0)
        profile_requests = [r for r in self.requests if "/calpico/" in r[0]]
        self.assertEqual(len(profile_requests), 1)
        self.assertEqual(profile_requests[0][1]["authorization"], "Bearer synthetic-test-token")
        self.assertNotIn("synthetic-test-token", json.dumps(probe))

    def test_two_accounts_resolve_their_own_names(self):
        self.session["user"] = {"id": "user-second", "name": "김종인"}
        self.session["account"]["id"] = "account-second"
        self.profile = {"user_id": "user-second", "display_name": "Daeng - ameforce"}
        probe = self._probe()
        self.assertEqual(probe["profileName"], "Daeng - ameforce")
        self.assertEqual(probe["accountId"], "account-second")
        self.assertTrue(any(url.endswith("/profile/user-second") for url, _ in self.requests))

    def test_same_percentage_still_refetches_renamed_profile(self):
        first = self._probe()
        self.profile["display_name"] = "Daeng - renamed"
        second = self.page.evaluate(USAGE_PAGE_PROBE_SCRIPT)
        self.assertEqual(first["profileName"], "Daeng - enmsoftware")
        self.assertEqual(second["profileName"], "Daeng - renamed")
        self.assertEqual(first["metricBlocks"], second["metricBlocks"])
        self.assertEqual(sum("/calpico/" in url for url, _ in self.requests), 2)

    def test_wrong_user_response_is_not_adopted(self):
        self.profile["user_id"] = "user-other"
        probe = self._probe()
        self.assertEqual(probe["profileName"], "")
        self.assertEqual(probe["accountId"], "account-current")
        self.assertTrue(probe["metricBlocks"])

    def test_transient_profile_failure_does_not_downgrade_to_login_or_cached_name(self):
        self.profile_status = 503
        probe = self._probe()
        self.assertEqual(probe["profileName"], "")
        self.assertTrue(probe["metricBlocks"])

    def test_nonexistent_custom_profile_uses_login_name(self):
        self.profile_status = 404
        self.assertEqual(self._probe()["profileName"], "Login Name")

    def test_missing_session_identity_cannot_select_cached_other_user(self):
        self.session["user"].pop("id")
        self.page.goto("https://chatgpt.com/codex/cloud/settings/analytics#usage")
        self.page.evaluate("""localStorage.setItem('cache/user-other', JSON.stringify({
            user_id: 'user-other', display_name: 'Wrong Cached User'
        }))""")
        probe = self.page.evaluate(USAGE_PAGE_PROBE_SCRIPT)
        self.assertEqual(probe["profileName"], "")
        self.assertFalse(any("/calpico/" in url for url, _ in self.requests))

    def test_profile_auth_failure_does_not_block_usage(self):
        self.profile_status = 401
        probe = self._probe()
        self.assertEqual(probe["profileName"], "")
        self.assertTrue(probe["metricBlocks"])

    def test_session_failure_does_not_block_usage_or_use_unbound_dom(self):
        self.session_status = 503
        probe = self._probe()
        self.assertEqual(probe["profileName"], "")
        self.assertEqual(probe["accountId"], "")
        self.assertTrue(probe["metricBlocks"])


if __name__ == "__main__":
    unittest.main()
