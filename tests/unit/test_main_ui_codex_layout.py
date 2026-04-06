import unittest
from unittest.mock import patch

from src.apps.main_ui import WindowsSupporterMainUI


class MainUiCodexLayoutUnitTest(unittest.TestCase):
    def test_codex_tab_default_size_is_tall_enough_for_runtime_section(self) -> None:
        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", return_value=None):
            with patch.object(WindowsSupporterMainUI, "_build_shell", return_value=None):
                ui = WindowsSupporterMainUI(root=object(), startup_manager=object(), monitor=object())

        self.assertEqual(ui._tab_sizes.get(ui._TAB_CODEX), (840, 630))
        self.assertEqual(ui._tab_minsizes.get(ui._TAB_CODEX), (800, 590))


if __name__ == "__main__":
    unittest.main()
