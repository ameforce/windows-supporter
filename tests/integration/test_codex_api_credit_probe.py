"""Run the shipped API credit probe in Chrome against synthetic billing pages.

The layouts are synthetic: they model how a card title, an action button and an
amount can be arranged, never a real account. Requests are answered locally.
"""

from __future__ import annotations

import unittest
from typing import Any

from playwright.sync_api import sync_playwright

from src.apps.codex_api_credit import (
    API_CREDIT_BILLING_URL,
    API_CREDIT_PAGE_PROBE_SCRIPT,
    ApiCreditReading,
    ApiCreditStatus,
    parse_api_credit_probe,
)


LOGIN_URL = "https://platform.openai.com/login?next=%2Fsettings%2Forganization%2Fbilling%2Foverview"


def html(body: str) -> str:
    return f"<!doctype html><html><head><title>Billing</title></head><body>{body}</body></html>"


class ApiCreditProbeIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def _read(self, pages: dict[str, str]) -> tuple[ApiCreditReading, dict[str, Any]]:
        """Open the billing address and read it the way the driver does.

        ``pages`` maps a URL to its HTML. Like the real site, a signed-out visit
        is sent to the login page by the page's own script, so the probe is
        repeated until the answer is final.
        """
        with self.browser.new_context() as context:

            def answer(route: Any) -> None:
                body = pages.get(route.request.url)
                if body is None:
                    route.fulfill(status=404, body="{}")
                else:
                    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

            context.route("**/*", answer)
            page = context.new_page()
            page.goto(API_CREDIT_BILLING_URL, wait_until="domcontentloaded")
            reading = ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="loading")
            raw: dict[str, Any] = {}
            for _attempt in range(20):
                try:
                    raw = page.evaluate(API_CREDIT_PAGE_PROBE_SCRIPT)
                except Exception:
                    raw = {}
                reading = parse_api_credit_probe(raw)
                if reading.is_final:
                    break
                page.wait_for_timeout(250)
        return reading, raw

    def test_title_above_amount_in_block_layout(self) -> None:
        reading, raw = self._read(
            {
                API_CREDIT_BILLING_URL: html(
                    "<main><h1>Billing</h1><div><div>Credit balance</div>"
                    "<div class='amount'>$12.34</div></div></main>"
                )
            }
        )

        self.assertEqual(reading, ApiCreditReading(ApiCreditStatus.OK, balance="$12.34"))
        self.assertEqual(raw["url"], API_CREDIT_BILLING_URL)

    def test_action_button_between_the_title_and_the_amount(self) -> None:
        reading, _raw = self._read(
            {
                API_CREDIT_BILLING_URL: html(
                    "<main><div style='display:flex;justify-content:space-between'>"
                    "<h3>Credit balance</h3><button>Add to credit balance</button></div>"
                    "<p>$7.50</p><p>Auto recharge is off</p></main>"
                )
            }
        )

        self.assertEqual(reading.balance, "$7.50")

    def test_title_and_amount_in_one_flex_row(self) -> None:
        reading, _raw = self._read(
            {
                API_CREDIT_BILLING_URL: html(
                    "<div style='display:flex;gap:8px'><span>Credit balance</span>"
                    "<strong>$1,234.56</strong></div>"
                )
            }
        )

        self.assertEqual(reading.balance, "$1,234.56")

    def test_inline_spans_without_a_separator(self) -> None:
        reading, _raw = self._read(
            {API_CREDIT_BILLING_URL: html("<div><span>Credit balance</span><span>$3.00</span></div>")}
        )

        self.assertEqual(reading.balance, "$3.00")

    def test_hidden_duplicates_and_template_text_are_ignored(self) -> None:
        reading, raw = self._read(
            {
                API_CREDIT_BILLING_URL: html(
                    "<div style='display:none'>Credit balance<br>$99.00</div>"
                    "<div style='visibility:hidden'>Credit balance<br>$98.00</div>"
                    "<template><div>Credit balance<br>$97.00</div></template>"
                    "<div>Credit balance<br>$12.34</div>"
                )
            }
        )

        self.assertEqual(reading.balance, "$12.34")
        for hidden in ("99.00", "98.00", "97.00"):
            self.assertNotIn(hidden, raw["text"])

    def test_page_still_rendering_is_not_final(self) -> None:
        reading, _raw = self._read(
            {
                API_CREDIT_BILLING_URL: html(
                    "<main><h1>Billing</h1><div>Credit balance</div><div class='skeleton'></div></main>"
                )
            }
        )

        self.assertEqual(reading.reason, "balance_not_found")
        self.assertFalse(reading.is_final)

    def test_redirect_to_the_platform_login_page_is_login_required(self) -> None:
        reading, raw = self._read(
            {
                API_CREDIT_BILLING_URL: html(f"<script>location.replace('{LOGIN_URL}')</script>"),
                LOGIN_URL: html(
                    "<main><h1>Build on the OpenAI API Platform</h1>"
                    "<p>Sign up or login with an OpenAI account to build with the OpenAI API.</p>"
                    "<button>Continue with Google</button></main>"
                ),
            }
        )

        self.assertEqual(reading, ApiCreditReading(ApiCreditStatus.LOGIN_REQUIRED, reason="login_page"))
        self.assertEqual(raw["url"], LOGIN_URL)


if __name__ == "__main__":
    unittest.main()
