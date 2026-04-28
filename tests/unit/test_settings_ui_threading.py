import unittest
from unittest.mock import patch

from src.apps.wrike_ui import WrikeSettingsView


class _InlineThread:
    def __init__(self, target=None, daemon=None):
        _ = daemon
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()
        return None


class _NoTkAfterWindow:
    def after(self, *_args, **_kwargs):
        raise AssertionError("worker must not call Tk after directly")


class SettingsUiThreadingUnitTest(unittest.TestCase):
    def test_wrike_background_result_uses_injected_ui_post(self) -> None:
        posted = []
        results = []
        view = WrikeSettingsView(
            root=None,
            wrike=object(),
            ui_post=lambda fn: posted.append(fn),
        )
        view._win = _NoTkAfterWindow()

        with patch("src.apps.wrike_ui.threading.Thread", _InlineThread):
            view._run_bg(lambda: "ok", lambda result: results.append(result))

        self.assertEqual(results, [])
        self.assertEqual(len(posted), 1)

        posted[0]()

        self.assertEqual(results, ["ok"])


if __name__ == "__main__":
    unittest.main()
