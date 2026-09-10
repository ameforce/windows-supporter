from __future__ import annotations

from pathlib import Path
import unittest

from src.apps.wrike_ui import WrikeSettingsView


REPO_ROOT = Path(__file__).resolve().parents[2]


class FlexBrowserArchitectureTests(unittest.TestCase):
    def test_settings_ui_exposes_employee_number_and_browser_actions_only(self) -> None:
        source = (REPO_ROOT / "src" / "apps" / "wrike_ui.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("Flex 사번 (자동 감지)", source)
        self.assertIn("Flex 로그인 · 지금 동기화", source)
        self.assertIn("브라우저 세션", source)
        self.assertIn("confirm_flex_employee_number", source)
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
        self.assertIn("class FlexBrowserSyncResult", flex_source)
        self.assertIn("extract_flex_employee_number", flex_source)
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


class FlexEmployeeNumberUiTests(unittest.TestCase):
    class _Var:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class _MessageBox:
        def askyesno(self, *_args, **_kwargs):
            return True

    class _Backend:
        def __init__(self):
            self.confirmed = ""

        def get_settings_snapshot(self):
            return {
                "flex_employee_number": "",
                "flex_detected_employee_number": "E-42",
                "flex_status": {
                    "state": "fresh",
                    "detected_employee_number": "E-42",
                },
            }

        def confirm_flex_employee_number(self, employee_number):
            self.confirmed = employee_number
            return True, None

    def test_detected_employee_number_is_shown_and_confirmed_before_field_update(self):
        backend = self._Backend()
        view = WrikeSettingsView(None, backend)
        view._flex_employee_number_var = self._Var()
        view._flex_status_var = self._Var()
        view._messagebox = self._MessageBox()

        view._refresh_flex_status_from_backend()

        self.assertEqual(backend.confirmed, "E-42")
        self.assertEqual(view._flex_employee_number_var.get(), "E-42")
        self.assertIn("감지된 사번 E-42", view._flex_status_var.get())


if __name__ == "__main__":
    unittest.main()
