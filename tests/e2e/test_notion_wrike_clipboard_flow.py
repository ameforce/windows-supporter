import unittest
from pathlib import Path
from unittest.mock import patch

from src.apps.Notion import DEFAULT_WRIKE_CLIPBOARD_VARIANT, Notion


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "notion-copy-raw.txt"


def load_fixture_text() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


class DummyRoot:
    def after(self, _ms: int, callback=None):
        if callback is None:
            return None
        try:
            callback()
        except Exception:
            return None
        return None


class NotionWrikeClipboardE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.notion = Notion()
        self.raw_text = load_fixture_text()

    def test_rewrite_clipboard_for_wrike_uses_default_variant(self) -> None:
        with patch.object(self.notion._Notion__lib.pyperclip, "paste", return_value=self.raw_text):
            with patch.object(self.notion, "_set_clipboard_text_and_html") as writer:
                with patch("src.apps.Notion.ToolTip"):
                    self.notion.rewrite_clipboard_for_wrike(DummyRoot())

        self.assertTrue(writer.called)
        written_text, written_cf_html, written_html_bytes = writer.call_args[0]

        self.assertEqual(self.notion.get_default_wrike_clipboard_variant(), DEFAULT_WRIKE_CLIPBOARD_VARIANT)
        self.assertIn("CAS", written_text)
        self.assertIn("https://www.wrike.com/open.htm?id=4367592792", written_text)
        self.assertNotIn("- https://www.wrike.com/open.htm?id=4367592792", written_text)
        self.assertNotIn("회의 내용", written_text)
        self.assertIsInstance(written_cf_html, bytes)
        self.assertIsInstance(written_html_bytes, bytes)
        self.assertIn(
            b'href="https://www.wrike.com/open.htm?id=4367592792"',
            written_html_bytes,
        )

    def test_single_line_anchor_variant_generates_html_formats(self) -> None:
        source = "[제목](https://www.wrike.com/open.htm?id=4389136245)"
        bundle = self.notion.build_wrike_clipboard_payload_bundle(
            source,
            variant="plain+html_anchor_url",
        )

        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["plain_text"], "https://www.wrike.com/open.htm?id=4389136245\r\n")
        self.assertIn(
            'href="https://www.wrike.com/open.htm?id=4389136245"',
            bundle["html_fragment"],
        )
        self.assertIsInstance(bundle["cf_html"], bytes)
        self.assertIsInstance(bundle["html_bytes"], bytes)


if __name__ == "__main__":
    unittest.main()
