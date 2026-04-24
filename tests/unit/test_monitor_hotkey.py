import os
import queue
import unittest
from unittest.mock import MagicMock, patch

from src.apps.Monitor import Monitor


class _FakeKeyboard:
    def __init__(self) -> None:
        self.hotkeys: list[str] = []
        self.press_keys: list[str] = []

    def add_hotkey(self, combo, _callback, suppress=False):
        _ = suppress
        self.hotkeys.append(str(combo))
        return None

    def on_press_key(self, key, _callback, suppress=False):
        _ = suppress
        self.press_keys.append(str(key))
        return None

    def unhook_all(self):
        return None

    def stash_state(self):
        return None

    def is_pressed(self, _key):
        return False


class MonitorHotkeyUnitTest(unittest.TestCase):
    def test_register_hotkeys_includes_ctrl_alt_c(self) -> None:
        monitor = Monitor()
        fake_kb = _FakeKeyboard()
        monitor._Monitor__lib.keyboard = fake_kb

        with patch.dict(os.environ, {}, clear=True):
            monitor._Monitor__register_hotkeys()

        self.assertIn("ctrl+alt+c", fake_kb.hotkeys)
        self.assertIn("ctrl+alt+k", fake_kb.hotkeys)
        self.assertIn("ctrl+alt+w", fake_kb.hotkeys)
        self.assertIn("alt+q", fake_kb.hotkeys)
        self.assertIn("ctrl+q", fake_kb.hotkeys)
        self.assertIn("ctrl+s", fake_kb.hotkeys)
        self.assertNotIn("ctrl+c", fake_kb.hotkeys)
        self.assertNotIn("enter", fake_kb.hotkeys)
        self.assertEqual([], fake_kb.press_keys)

    def test_ctrl_alt_c_dispatches_codex_current_status(self) -> None:
        monitor = Monitor()
        monitor._Monitor__root = object()
        monitor._Monitor__event_queue = queue.SimpleQueue()
        codex = MagicMock()
        monitor._Monitor__codex_usage = codex
        monitor._Monitor__codex_attached = True

        monitor._Monitor__on_ctrl_alt_c()
        fn = monitor._Monitor__event_queue.get_nowait()
        fn()

        codex.show_current_status.assert_called_once_with(force_refresh=True)

    def test_alt_q_dispatches_wrike_action_when_wrike_is_active(self) -> None:
        monitor = Monitor()
        root = object()
        monitor._Monitor__root = root
        monitor._Monitor__event_queue = queue.SimpleQueue()
        wrike = MagicMock()
        wrike.is_wrike_active.return_value = True
        monitor._Monitor__wrike = wrike
        monitor._Monitor__wrike_attached = True

        monitor._Monitor__on_alt_q()
        fn = monitor._Monitor__event_queue.get_nowait()
        fn()

        wrike.action.assert_called_once_with(root)

    def test_ctrl_q_dispatches_wrike_open_when_wrike_is_active(self) -> None:
        monitor = Monitor()
        root = object()
        monitor._Monitor__root = root
        monitor._Monitor__event_queue = queue.SimpleQueue()
        wrike = MagicMock()
        wrike.is_wrike_active.return_value = True
        monitor._Monitor__wrike = wrike
        monitor._Monitor__wrike_attached = True

        monitor._Monitor__on_ctrl_q()
        fn = monitor._Monitor__event_queue.get_nowait()
        fn()

        wrike.open_in_separate_tab.assert_called_once_with(root)

    def test_ctrl_s_dispatches_notion_action_when_notion_is_active(self) -> None:
        monitor = Monitor()
        root = object()
        monitor._Monitor__root = root
        monitor._Monitor__event_queue = queue.SimpleQueue()
        notion = MagicMock()
        notion.is_notion_active.return_value = True
        monitor._Monitor__notion = notion

        monitor._Monitor__on_ctrl_s()
        fn = monitor._Monitor__event_queue.get_nowait()
        fn()

        notion.action.assert_called_once_with(root)

    def test_ctrl_s_ignores_when_notion_is_not_active(self) -> None:
        monitor = Monitor()
        root = object()
        monitor._Monitor__root = root
        monitor._Monitor__event_queue = queue.SimpleQueue()
        notion = MagicMock()
        notion.is_notion_active.return_value = False
        monitor._Monitor__notion = notion

        monitor._Monitor__on_ctrl_s()
        fn = monitor._Monitor__event_queue.get_nowait()
        fn()

        notion.action.assert_not_called()

    def test_attach_starts_feature_warmup_async(self) -> None:
        monitor = Monitor()
        monitor._Monitor__lib.keyboard = _FakeKeyboard()
        root = object()
        event_queue = queue.SimpleQueue()

        with patch.object(
            monitor,
            "_Monitor__start_feature_warmup_async",
        ) as warmup:
            monitor.attach(root, event_queue)

        self.assertTrue(warmup.called)


if __name__ == "__main__":
    unittest.main()
