from __future__ import annotations

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


class FlexBrowserArchitectureTests(unittest.TestCase):
    def test_settings_ui_exposes_employee_number_and_browser_actions_only(self) -> None:
        source = (REPO_ROOT / "src" / "apps" / "wrike_ui.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("Flex 사번", source)
        self.assertIn("Flex 로그인 · 지금 동기화", source)
        self.assertIn("브라우저 세션", source)
        self.assertNotIn("_flex_refresh_token_var", source)
        self.assertNotIn("_flex_client_id_var", source)
        self.assertNotIn("_flex_client_secret_var", source)
        self.assertNotIn("인증정보 지우기", source)

    def test_runtime_has_no_administrator_flex_client(self) -> None:
        flex_source = (REPO_ROOT / "src" / "apps" / "flex_worktime.py").read_text(
            encoding="utf-8"
        )
        wrike_source = (REPO_ROOT / "src" / "apps" / "Wrike.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("class FlexBrowserClient", flex_source)
        self.assertNotIn("class FlexOpenApiClient", flex_source)
        self.assertNotIn("FlexOpenApiClient", wrike_source)
        self.assertNotIn("FlexCredentials", flex_source)
        self.assertIn("FLEX_BROWSER_PROFILE_DIR_NAME", wrike_source)

    def test_settings_migration_drops_legacy_flex_secrets_on_save(self) -> None:
        source = (REPO_ROOT / "src" / "apps" / "Wrike.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("legacy_flex_keys", source)
        save_start = source.index("def __save_settings")
        save_source = source[save_start:]
        self.assertNotIn('"flex_refresh_token"', save_source)
        self.assertNotIn('"flex_client_id"', save_source)
        self.assertNotIn('"flex_client_secret"', save_source)


if __name__ == "__main__":
    unittest.main()
