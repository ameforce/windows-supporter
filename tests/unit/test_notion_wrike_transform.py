import unittest
from pathlib import Path

from src.apps.Notion import DEFAULT_WRIKE_CLIPBOARD_VARIANT, Notion, WRIKE_CLIPBOARD_VARIANTS


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "notion-copy-raw.txt"
HIERARCHY_SOURCE = """- [[CAS] - Parent Task](https://www.wrike.com/open.htm?id=4378785464)
    - [02/19] 분석 및 개선 완료
    - [[CAS] - Child Task](https://www.wrike.com/open.htm?id=4378796500)
        - [03/01] child detail
    - [[CAS] - Sibling Task](https://www.wrike.com/open.htm?id=4381637503)
        - [03/02] sibling detail
"""


def load_fixture_text() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


class NotionWrikeTransformUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.notion = Notion()
        self.raw_text = load_fixture_text()

    def test_variant_catalog_is_stable(self) -> None:
        self.assertEqual(
            self.notion.get_wrike_clipboard_variants(),
            WRIKE_CLIPBOARD_VARIANTS,
        )
        self.assertEqual(
            self.notion.get_default_wrike_clipboard_variant(),
            DEFAULT_WRIKE_CLIPBOARD_VARIANT,
        )

    def test_groups_by_product_and_filters_status_and_meeting(self) -> None:
        nodes = self.notion._parse_bullet_tree(self.raw_text)
        groups = self.notion._build_wrike_product_groups(nodes)
        rendered = self.notion._render_wrike_plain_sections(groups)

        self.assertIn("CAS", rendered)
        self.assertIn("인프라", rendered)
        self.assertIn("pdfcmd", rendered)
        self.assertIn("WebRender", rendered)

        self.assertNotIn("진행 중", rendered)
        self.assertNotIn("진행 완료", rendered)
        self.assertNotIn("회의 내용", rendered)

        self.assertIn("https://www.wrike.com/open.htm?id=4367592792", rendered)
        self.assertIn("https://www.wrike.com/open.htm?id=4381636980", rendered)
        self.assertIn("https://www.wrike.com/open.htm?id=4399374818", rendered)
        self.assertNotIn("[[한국언론진흥재단]", rendered)

    def test_inline_wrike_link_uses_url_only(self) -> None:
        source = "[[CAS] - 구조적 취약한 부분 분석 및 개선](https://www.wrike.com/open.htm?id=4378785464)"
        converted = self.notion._inline_to_plain_wrike(source)

        self.assertEqual(converted, "https://www.wrike.com/open.htm?id=4378785464")

    def test_plain_only_variant_omits_html_and_removes_task_url_bullets(self) -> None:
        bundle = self.notion.build_wrike_clipboard_payload_bundle(
            self.raw_text,
            variant="plain_only_url",
        )

        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["variant"], "plain_only_url")
        self.assertIsNone(bundle["cf_html"])
        self.assertIsNone(bundle["html_bytes"])
        self.assertEqual(bundle["html_fragment"], "")
        self.assertIn("CAS\r\nhttps://www.wrike.com/open.htm?id=4367592792\r\n", bundle["plain_text"])
        self.assertNotIn("- https://www.wrike.com/open.htm?id=4367592792", bundle["plain_text"])

    def test_plain_bulleted_variant_keeps_task_url_bullet_prefix(self) -> None:
        bundle = self.notion.build_wrike_clipboard_payload_bundle(
            self.raw_text,
            variant="plain_bulleted_url",
        )

        self.assertIsNotNone(bundle)
        self.assertIn("- https://www.wrike.com/open.htm?id=4367592792", bundle["plain_text"])
        self.assertIsNone(bundle["cf_html"])
        self.assertIsNone(bundle["html_bytes"])

    def test_html_text_and_anchor_variants_expose_distinct_html_shapes(self) -> None:
        text_bundle = self.notion.build_wrike_clipboard_payload_bundle(
            self.raw_text,
            variant="plain+html_text_url",
        )
        anchor_bundle = self.notion.build_wrike_clipboard_payload_bundle(
            self.raw_text,
            variant="plain+html_anchor_url",
        )

        self.assertIsNotNone(text_bundle)
        self.assertIsNotNone(anchor_bundle)
        self.assertIn("<li>https://www.wrike.com/open.htm?id=4367592792", text_bundle["html_fragment"])
        self.assertNotIn("<a ", text_bundle["html_fragment"])
        self.assertIn('href="https://www.wrike.com/open.htm?id=4367592792"', anchor_bundle["html_fragment"])
        self.assertIn("<a ", anchor_bundle["html_fragment"])
        self.assertIsInstance(text_bundle["cf_html"], bytes)
        self.assertIsInstance(text_bundle["html_bytes"], bytes)
        self.assertIsInstance(anchor_bundle["cf_html"], bytes)
        self.assertIsInstance(anchor_bundle["html_bytes"], bytes)
        self.assertEqual(anchor_bundle["html_list_mode"], "nested_simple")

    def test_anchor_indent_variants_expose_distinct_list_shapes(self) -> None:
        nested_bundle = self.notion.build_wrike_clipboard_payload_bundle(
            HIERARCHY_SOURCE,
            variant="plain+html_anchor_url",
        )
        flat_bundle = self.notion.build_wrike_clipboard_payload_bundle(
            HIERARCHY_SOURCE,
            variant="plain+html_anchor_url_flat_ql_indent",
        )
        flat_stringify_bundle = self.notion.build_wrike_clipboard_payload_bundle(
            HIERARCHY_SOURCE,
            variant="plain+html_anchor_url_flat_stringify",
        )
        nested_stringify_bundle = self.notion.build_wrike_clipboard_payload_bundle(
            HIERARCHY_SOURCE,
            variant="plain+html_anchor_url_nested_stringify",
        )

        self.assertIsNotNone(nested_bundle)
        self.assertIsNotNone(flat_bundle)
        self.assertIsNotNone(flat_stringify_bundle)
        self.assertIsNotNone(nested_stringify_bundle)

        self.assertEqual(nested_bundle["html_list_mode"], "nested_simple")
        self.assertEqual(flat_bundle["html_list_mode"], "flat_ql_indent")
        self.assertEqual(flat_stringify_bundle["html_list_mode"], "flat_stringify")
        self.assertEqual(nested_stringify_bundle["html_list_mode"], "nested_stringify")

        nested_html = str(nested_bundle["html_fragment"])
        flat_html = str(flat_bundle["html_fragment"])
        flat_stringify_html = str(flat_stringify_bundle["html_fragment"])
        nested_stringify_html = str(nested_stringify_bundle["html_fragment"])

        self.assertGreater(nested_html.count("<ul>"), 1)
        self.assertEqual(flat_html.count("<ul>"), 1)
        self.assertIn('class="ql-indent-1"', flat_html)
        self.assertIn('class="ql-indent-2"', flat_html)
        self.assertIn('class="ql-indent-3"', flat_html)

        self.assertEqual(flat_stringify_html.count("<ul"), 1)
        self.assertIn('data-stringify-type="unordered-list"', flat_stringify_html)
        self.assertIn('data-stringify-indent="1"', flat_stringify_html)
        self.assertIn('data-stringify-indent="2"', flat_stringify_html)
        self.assertIn('data-stringify-indent="3"', flat_stringify_html)

        self.assertGreater(nested_stringify_html.count("<ul"), 1)
        self.assertIn('data-stringify-type="unordered-list"', nested_stringify_html)
        self.assertIn('class="ql-indent-2"', nested_stringify_html)
        self.assertIn('class="ql-indent-3"', nested_stringify_html)

    def test_control_variant_adds_trailing_space_for_task_url_lines(self) -> None:
        bundle = self.notion.build_wrike_clipboard_payload_bundle(
            self.raw_text,
            variant="control_with_trailing_space_or_newline",
        )

        self.assertIsNotNone(bundle)
        self.assertIn("https://www.wrike.com/open.htm?id=4367592792 \r\n", bundle["plain_text"])
        self.assertIsNone(bundle["cf_html"])
        self.assertIsNone(bundle["html_bytes"])


if __name__ == "__main__":
    unittest.main()
