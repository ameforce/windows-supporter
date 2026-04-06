import re
import unittest
from unittest.mock import patch

from src.apps.startup_apps import HideRule, StartupAppManager


class StartupAppsShowOnceUnitTest(unittest.TestCase):
    @staticmethod
    def _enum_windows_factory(windows_by_tick: list[list[int]]):
        state = {"index": 0}

        def _enum_windows(callback, _param) -> None:
            idx = min(state["index"], len(windows_by_tick) - 1)
            for hwnd in windows_by_tick[idx]:
                callback(hwnd, None)
            state["index"] += 1

        return _enum_windows

    def test_show_action_runs_only_once_for_same_window(self) -> None:
        manager = StartupAppManager()
        rules = [HideRule(action="show", title_re=re.compile(r"(?i)slack"))]
        show_once_hwnds: set[int] = set()

        with patch(
            "src.apps.startup_apps.win32gui.EnumWindows",
            side_effect=self._enum_windows_factory([[1001], [1001]]),
        ):
            with patch("src.apps.startup_apps.is_tool_window", return_value=False):
                with patch("src.apps.startup_apps.get_window_text", return_value="Slack"):
                    with patch(
                        "src.apps.startup_apps.apply_window_action",
                        return_value=True,
                    ) as mock_apply:
                        manager._hide_matching_windows(
                            rules,
                            show_once_hwnds=show_once_hwnds,
                        )
                        manager._hide_matching_windows(
                            rules,
                            show_once_hwnds=show_once_hwnds,
                        )

        self.assertEqual(mock_apply.call_count, 1)
        self.assertEqual(show_once_hwnds, {1001})

    def test_show_action_still_runs_for_new_window_handle(self) -> None:
        manager = StartupAppManager()
        rules = [HideRule(action="show", title_re=re.compile(r"(?i)slack"))]
        show_once_hwnds: set[int] = set()

        with patch(
            "src.apps.startup_apps.win32gui.EnumWindows",
            side_effect=self._enum_windows_factory([[1001], [1001, 2002]]),
        ):
            with patch("src.apps.startup_apps.is_tool_window", return_value=False):
                with patch("src.apps.startup_apps.get_window_text", return_value="Slack"):
                    with patch(
                        "src.apps.startup_apps.apply_window_action",
                        return_value=True,
                    ) as mock_apply:
                        manager._hide_matching_windows(
                            rules,
                            show_once_hwnds=show_once_hwnds,
                        )
                        manager._hide_matching_windows(
                            rules,
                            show_once_hwnds=show_once_hwnds,
                        )

        self.assertEqual(mock_apply.call_count, 2)
        self.assertEqual(show_once_hwnds, {1001, 2002})


if __name__ == "__main__":
    unittest.main()
