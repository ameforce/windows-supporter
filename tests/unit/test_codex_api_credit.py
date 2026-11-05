from __future__ import annotations

import unittest

from src.apps.codex_api_credit import (
    API_CREDIT_BILLING_URL,
    API_CREDIT_PAGE_PROBE_SCRIPT,
    ApiCreditReading,
    ApiCreditStatus,
    describe_api_credit_shape,
    extract_credit_balance,
    normalize_api_credit_balance,
    normalize_api_credit_status,
    parse_api_credit_probe,
    reading_from_payload,
)


LOGIN_URL = "https://platform.openai.com/login?next=%2Fsettings%2Forganization%2Fbilling%2Foverview"


def page(text: str, url: str = API_CREDIT_BILLING_URL, title: str = "Billing") -> dict[str, str]:
    return {"url": url, "title": title, "text": text}


class ApiCreditBalanceParsingTest(unittest.TestCase):
    def test_amount_on_the_line_after_the_label(self) -> None:
        reading = parse_api_credit_probe(
            page("Billing\nOverview\nCredit balance\n$12.34\nAdd to credit balance\nAuto recharge is off")
        )

        self.assertEqual(reading, ApiCreditReading(ApiCreditStatus.OK, balance="$12.34"))

    def test_amount_on_the_label_line(self) -> None:
        for text, expected in (
            ("Credit balance: $1,234.56", "$1,234.56"),
            ("Credit balance $0.00", "$0.00"),
            ("  Credit   balance   $7.5 Add to credit balance", "$7.5"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_api_credit_probe(page(text)),
                    ApiCreditReading(ApiCreditStatus.OK, balance=expected),
                )

    def test_korean_label_is_read(self) -> None:
        reading = parse_api_credit_probe(page("결제\n크레딧 잔액\n$7.50"))

        self.assertEqual(reading.balance, "$7.50")

    def test_spelling_variants_normalize_to_one_dollar_form(self) -> None:
        for raw, expected in (
            ("$12.34", "$12.34"),
            ("US$ 5", "$5"),
            ("$ 12.34 USD", "$12.34"),
            ("12.34 USD", "$12.34"),
            ("-$1.25", "-$1.25"),
            ("−$1.25", "-$1.25"),
            ("$1,000", "$1,000"),
            (" $3.00 ", "$3.00"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_api_credit_balance(raw), expected)

    def test_text_that_is_not_one_amount_is_rejected(self) -> None:
        for raw in ("", "lots", "$12,34", "$12.34 expires 2027", "12.34", "€5", None, 5):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_api_credit_balance(raw), "")

    def test_only_the_labelled_block_counts(self) -> None:
        text = "\n".join(
            [
                "Monthly budget",
                "$100.00",
                "Credit balance",
                "$12.34",
                "Auto recharge threshold",
                "$5.00",
            ]
        )

        self.assertEqual(extract_credit_balance(text.split("\n")), "$12.34")

    def test_button_text_containing_the_label_is_not_a_heading(self) -> None:
        reading = parse_api_credit_probe(page("Billing\nAdd to credit balance\n$5.00"))

        self.assertEqual(reading.status, ApiCreditStatus.UNAVAILABLE)
        self.assertEqual(reading.reason, "balance_not_found")

    def test_label_that_starts_a_sentence_is_not_a_heading(self) -> None:
        reading = parse_api_credit_probe(
            page("Credit balance is applied to API usage first.\n$10.00")
        )

        self.assertEqual(reading.reason, "balance_not_found")

    def test_amount_must_be_the_whole_next_line(self) -> None:
        for text in (
            "Credit balance\nAuto recharge is on\n$10.00",
            "Credit balance\n$12.34 expires 2027",
            "Credit balance\n\n\nPay as you go",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_api_credit_probe(page(text)).reason,
                    "balance_not_found",
                )

    def test_amount_after_the_cards_action_buttons_is_read(self) -> None:
        for text in (
            "Credit balance\nAdd to credit balance\n$12.34",
            "Credit balance\nAdd to credit balance\nModify auto recharge\n$12.34",
            "크레딧 잔액\n크레딧 추가\n$12.34",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_api_credit_probe(page(text)),
                    ApiCreditReading(ApiCreditStatus.OK, balance="$12.34"),
                )

    def test_amount_after_other_text_or_too_many_buttons_is_not_guessed(self) -> None:
        for text in (
            "Credit balance\nAdd to credit balance\nMonthly budget\n$100.00",
            "Credit balance\nSome heading\n$12.34",
            "Credit balance\nAdd credit\nModify recharge\nManage billing\n$12.34",
            "$12.34\nCredit balance",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_api_credit_probe(page(text)).reason,
                    "balance_not_found",
                )

    def test_the_page_title_outranks_every_synonym(self) -> None:
        for text, expected in (
            ("Remaining credits\n$5.00\nCredit balance\n$20.00", "$20.00"),
            ("Available credit\n$100.00\n크레딧 잔액\n$7.00", "$7.00"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_api_credit_probe(page(text)).balance, expected)

    def test_a_title_still_loading_never_falls_back_to_a_synonym(self) -> None:
        reading = parse_api_credit_probe(
            page("Credit balance\nAvailable credit\n$100.00\nPay as you go")
        )

        self.assertEqual(reading.reason, "balance_not_found")

    def test_synonym_counts_on_a_page_without_the_title(self) -> None:
        self.assertEqual(parse_api_credit_probe(page("Available credits\n$9.99")).balance, "$9.99")

    def test_negative_amount_on_the_label_line_keeps_its_sign(self) -> None:
        for text, expected in (
            ("Credit balance -$5.00", "-$5.00"),
            ("Credit balance –$5.00", "-$5.00"),
            ("Credit balance: -$5.00", "-$5.00"),
            ("Credit balance - $5.00", "$5.00"),
            ("Credit balance\n-$5.00", "-$5.00"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_api_credit_probe(page(text)).balance, expected)

    def test_amount_followed_by_more_digits_is_not_cut_short(self) -> None:
        for text in ("Credit balance $1,23", "Credit balance $12.34.56", "Credit balance $1.234,56"):
            with self.subTest(text=text):
                self.assertEqual(parse_api_credit_probe(page(text)).balance, "")

    def test_sentence_punctuation_after_the_amount_is_not_part_of_it(self) -> None:
        for text, expected in (
            ("Credit balance $12.34.", "$12.34"),
            ("Credit balance $3.00, renews", "$3.00"),
            ("Credit balance $5,000.00.", "$5,000.00"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_api_credit_probe(page(text)).balance, expected)

    def test_only_known_button_wording_is_stepped_over(self) -> None:
        for line in (
            "Manage budgets",
            "Edit limits",
            "Add thresholds",
            "Set up auto recharge at",
            "Top up to",
            "Recharge to",
            "Add credits under",
            "Add budget설정",
            "자동 충전 기준 변경",
            "월 사용 상한 변경",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    parse_api_credit_probe(page(f"Credit balance\n{line}\n$5.00")).balance, ""
                )
        for line in (
            "+ Add to credit balance",
            "Add credits →",
            "Buy credits",
            "Top up",
            "Set up auto-recharge",
            "자동 충전 설정",
            "크레딧 충전하기",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    parse_api_credit_probe(page(f"Credit balance\n{line}\n$5.00")).balance, "$5.00"
                )

    def test_inline_button_right_after_the_amount_still_reads(self) -> None:
        self.assertEqual(
            parse_api_credit_probe(page("Credit balance$3.00Add to credit balance")).balance,
            "$3.00",
        )

    def test_sentences_about_other_amounts_are_not_buttons(self) -> None:
        for text in (
            "Credit balance\nEnable auto recharge when balance falls below\n$5.00",
            "Credit balance\nManage budget limit\n$500.00",
            "크레딧 잔액\n월 예산 설정\n$500.00",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_api_credit_probe(page(text)).reason, "balance_not_found")

    def test_synonyms_count_only_on_the_billing_page(self) -> None:
        usage = "https://platform.openai.com/usage"

        self.assertNotEqual(
            parse_api_credit_probe(page("Remaining credits\n$5.00", url=usage)).status,
            ApiCreditStatus.OK,
        )
        self.assertEqual(parse_api_credit_probe(page("Credit balance\n$5.00", url=usage)).balance, "$5.00")

    def test_longer_label_wins_over_its_prefix(self) -> None:
        self.assertEqual(
            extract_credit_balance(["Available credits $9.99"]),
            "$9.99",
        )

    def test_balance_on_another_host_is_ignored(self) -> None:
        reading = parse_api_credit_probe(
            page("Credit balance\n$5.00", url="https://example.com/settings")
        )

        self.assertNotEqual(reading.status, ApiCreditStatus.OK)

    def test_balance_is_never_read_from_a_login_page(self) -> None:
        reading = parse_api_credit_probe(page("Credit balance\n$5.00", url=LOGIN_URL))

        self.assertEqual(reading.status, ApiCreditStatus.LOGIN_REQUIRED)
        self.assertEqual(reading.balance, "")


class ApiCreditPageStateTest(unittest.TestCase):
    def test_redirect_to_the_platform_login_page_means_login_required(self) -> None:
        reading = parse_api_credit_probe(
            page("Sign up or login with an OpenAI account\nContinue", url=LOGIN_URL)
        )

        self.assertEqual(reading, ApiCreditReading(ApiCreditStatus.LOGIN_REQUIRED, reason="login_page"))
        self.assertTrue(reading.is_final)

    def test_identity_provider_pages_mean_a_sign_in_is_in_progress(self) -> None:
        for url in (
            "https://auth.openai.com/log-in",
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=x",
            "https://login.microsoftonline.com/common/oauth2",
        ):
            with self.subTest(url=url):
                reading = parse_api_credit_probe(page("Sign in", url=url))
                self.assertEqual(reading.status, ApiCreditStatus.LOGIN_REQUIRED)

    def test_login_text_without_a_login_path_is_login_required(self) -> None:
        reading = parse_api_credit_probe(
            page("Sign up or login with an OpenAI account\nContinue with Google")
        )

        self.assertEqual(reading, ApiCreditReading(ApiCreditStatus.LOGIN_REQUIRED, reason="login_text"))

    def test_challenge_page_may_still_clear(self) -> None:
        reading = parse_api_credit_probe(page("Verify you are human", title="Just a moment..."))

        self.assertEqual(reading.reason, "challenge")
        self.assertFalse(reading.is_final)

    def test_billing_page_that_is_still_rendering_is_not_final(self) -> None:
        empty = parse_api_credit_probe(page(""))
        shell = parse_api_credit_probe(page("Billing\nOverview"))

        self.assertEqual((empty.reason, shell.reason), ("loading", "balance_not_found"))
        self.assertFalse(empty.is_final)
        self.assertFalse(shell.is_final)

    def test_other_pages_are_unexpected_but_not_final(self) -> None:
        for url in (
            "https://platform.openai.com/settings/organization/general",
            "https://platform.openai.com/",
            "about:blank",
        ):
            with self.subTest(url=url):
                reading = parse_api_credit_probe(page("Home\nQuickstart", url=url))
                self.assertEqual(reading.reason, "unexpected_page")
                self.assertFalse(reading.is_final)

    def test_probe_that_is_not_an_object_is_invalid_and_not_final(self) -> None:
        for raw in (None, "text", 5, []):
            with self.subTest(raw=raw):
                reading = parse_api_credit_probe(raw)
                self.assertEqual(reading.reason, "probe_invalid")
                self.assertFalse(reading.is_final)

    def test_failure_reasons_outside_the_wait_list_are_final(self) -> None:
        reading = ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="command_timeout")

        self.assertTrue(reading.is_final)


class ApiCreditPayloadTest(unittest.TestCase):
    def test_payload_round_trip_keeps_only_what_the_status_allows(self) -> None:
        ok = ApiCreditReading(ApiCreditStatus.OK, balance="$12.34")
        self.assertEqual(ok.to_payload(), {"status": "ok", "balance": "$12.34"})
        self.assertEqual(reading_from_payload(ok.to_payload()), ok)
        login = reading_from_payload({"status": "login_required", "balance": "$5", "reason": "login_page"})
        self.assertEqual(login, ApiCreditReading(ApiCreditStatus.LOGIN_REQUIRED, reason="login_page"))

    def test_payload_without_a_valid_balance_is_not_trusted_as_ok(self) -> None:
        self.assertEqual(
            reading_from_payload({"status": "ok", "balance": "lots"}),
            ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="balance_invalid"),
        )
        self.assertEqual(
            reading_from_payload({"status": "ok"}),
            ApiCreditReading(ApiCreditStatus.UNAVAILABLE, reason="balance_invalid"),
        )

    def test_unknown_or_malformed_payloads_are_dropped(self) -> None:
        for raw in (None, "ok", {"status": "weird"}, {"balance": "$1"}, []):
            with self.subTest(raw=raw):
                self.assertIsNone(reading_from_payload(raw))

    def test_status_normalizer_accepts_only_known_values(self) -> None:
        self.assertEqual(normalize_api_credit_status(" OK "), "ok")
        self.assertEqual(normalize_api_credit_status("Login_Required"), "login_required")
        self.assertEqual(normalize_api_credit_status("nope"), "")
        self.assertEqual(normalize_api_credit_status(None), "")


class ApiCreditDiagnosticsTest(unittest.TestCase):
    def test_shape_description_masks_digits_and_keeps_the_layout(self) -> None:
        shape = describe_api_credit_shape(
            page("Billing\nCredit balance\n$123.45\nAdd to credit balance")
        )

        self.assertIn("label_line=1", shape)
        self.assertIn("$###.##", shape)
        self.assertNotIn("123", shape)

    def test_shape_description_never_names_other_hosts_or_identifiers(self) -> None:
        tenant = describe_api_credit_shape(
            page(
                "Sign in",
                url="https://acme-corp.okta.com/app/acmecorp_openai_1/exk1abc234xyz/sso/saml",
            )
        )
        project = describe_api_credit_shape(
            page("Limits", url="https://platform.openai.com/settings/proj_ab12cd34ef56/limits")
        )

        self.assertEqual(tenant, "host=other")
        self.assertIn("path=/settings/#/limits", project)
        self.assertNotIn("ab12", project)

    def test_shape_description_leaves_out_neighbours_and_addresses(self) -> None:
        shape = describe_api_credit_shape(
            page("Acme Corp\nCredit balance\nowner admin@acme.com\nCard ending 4242")
        )

        self.assertNotIn("Acme", shape)
        self.assertNotIn("admin@acme.com", shape)
        self.assertIn("<email>", shape)

    def test_shape_description_survives_malformed_input(self) -> None:
        self.assertEqual(describe_api_credit_shape(None), "probe=invalid")
        self.assertIn("label_line=-1", describe_api_credit_shape(page("Billing")))

    def test_probe_script_reads_only_rendered_body_text(self) -> None:
        self.assertIn("innerText", API_CREDIT_PAGE_PROBE_SCRIPT)
        self.assertIn("getClientRects", API_CREDIT_PAGE_PROBE_SCRIPT)
        self.assertIn("document.body", API_CREDIT_PAGE_PROBE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
