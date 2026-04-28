import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.apps.Wrike import Wrike
from src.utils.secret_store import SecretStore


class _FakeThread:
    created = []

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon
        _FakeThread.created.append(self)

    def start(self):
        return None


class _RecordingRoot:
    def __init__(self):
        self.after_calls = []

    def after(self, delay_ms, callback):
        self.after_calls.append((delay_ms, callback))
        return f"after#{len(self.after_calls)}"

    def after_cancel(self, _after_id):
        return None


class WrikeResponsivenessAndTokenUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.appdata = Path(self.tmp.name)

    def _new_wrike(self) -> Wrike:
        with patch.dict("os.environ", {"APPDATA": str(self.appdata)}, clear=False):
            return Wrike()

    def test_action_dispatches_background_worker_without_inline_pyautogui(self) -> None:
        wrike = self._new_wrike()
        root = object()
        _FakeThread.created = []

        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            with patch.object(
                wrike._Wrike__lib.pyautogui,
                "click",
                side_effect=AssertionError("pyautogui must not run on caller thread"),
            ):
                wrike.action(root)

        self.assertEqual(len(_FakeThread.created), 1)
        self.assertTrue(wrike._Wrike__is_running)

    def test_open_in_separate_tab_dispatches_background_worker_without_inline_pyautogui(self) -> None:
        wrike = self._new_wrike()
        root = object()
        _FakeThread.created = []

        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            with patch.object(
                wrike._Wrike__lib.pyautogui,
                "rightClick",
                side_effect=AssertionError("pyautogui must not run on caller thread"),
            ):
                wrike.open_in_separate_tab(root)

        self.assertEqual(len(_FakeThread.created), 1)

    def test_open_in_separate_tab_is_single_flight_until_worker_finishes(self) -> None:
        wrike = self._new_wrike()
        root = object()
        _FakeThread.created = []

        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            with patch.object(wrike._Wrike__lib.pyautogui, "rightClick", return_value=None):
                with patch.object(wrike._Wrike__lib.pyautogui, "moveRel", return_value=None):
                    with patch.object(wrike._Wrike__lib.pyautogui, "hotkey", return_value=None):
                        wrike.open_in_separate_tab(root)
                        wrike.open_in_separate_tab(root)
                        self.assertEqual(len(_FakeThread.created), 1)
                        self.assertTrue(wrike._Wrike__open_tab_running)

                        _FakeThread.created[0].target()

        self.assertFalse(wrike._Wrike__open_tab_running)

    def test_monitor_worker_posts_finish_to_ui_thread_before_rescheduling(self) -> None:
        wrike = self._new_wrike()
        root = _RecordingRoot()
        wrike._Wrike__monitor_enabled = True
        wrike._Wrike__wrike_api_token_session = "token"
        wrike.attach(root)
        root.after_calls.clear()
        _FakeThread.created = []

        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            with patch.object(
                wrike,
                "_Wrike__resolve_contact_identity",
                return_value=(None, None, "auth_failed"),
            ):
                wrike._Wrike__monitor_tick()
                root.after_calls.clear()
                _FakeThread.created[0].target()

        self.assertTrue(wrike._Wrike__monitor_running)
        self.assertEqual(root.after_calls, [])

        wrike._Wrike__drain_ui_queue()

        self.assertFalse(wrike._Wrike__monitor_running)
        self.assertTrue(any(delay >= 5000 for delay, _callback in root.after_calls))

    def test_ui_safe_does_not_schedule_tk_directly_when_queue_fails(self) -> None:
        wrike = self._new_wrike()
        root = _RecordingRoot()
        failing_queue = Mock()
        failing_queue.put.side_effect = RuntimeError("queue unavailable")
        wrike._Wrike__ui_queue = failing_queue

        posted = wrike._Wrike__ui_safe(root, lambda: None)

        self.assertFalse(posted)
        self.assertEqual(root.after_calls, [])

    def test_saving_token_omits_plaintext_and_snapshot_masks_token(self) -> None:
        wrike = self._new_wrike()

        ok, err = wrike.update_settings({"api_token": "secret-token-1234567890"})

        self.assertTrue(ok, err)
        settings_path = Path(wrike.get_settings_snapshot()["settings_path"])
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertNotIn("api_token", saved)
        self.assertNotIn("secret-token-1234567890", settings_path.read_text(encoding="utf-8"))

        snapshot = wrike.get_settings_snapshot()
        self.assertNotIn("api_token", snapshot)
        self.assertTrue(snapshot["api_token_configured"])
        self.assertEqual(snapshot["api_token_masked"], "sec*****************890")

    def test_plaintext_token_migrates_on_load_and_empty_save_preserves_secret(self) -> None:
        config_dir = self.appdata / "windows-supporter"
        config_dir.mkdir(parents=True)
        settings_path = config_dir / "wrike_settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "settings_version": 3,
                    "api_token": "legacy-token-1234567890",
                    "daily_target_minutes": 480,
                    "tooltip_duration_ms": 6000,
                    "monitor_enabled": False,
                    "monitor_interval_sec": 5.0,
                    "monitor_folder_path": [],
                }
            ),
            encoding="utf-8",
        )

        wrike = self._new_wrike()
        snapshot = wrike.get_settings_snapshot()

        self.assertTrue(snapshot["api_token_configured"])
        self.assertNotIn("api_token", snapshot)
        saved_after_load = settings_path.read_text(encoding="utf-8")
        self.assertNotIn('"api_token"', saved_after_load)
        self.assertNotIn("legacy-token-1234567890", saved_after_load)

        ok, err = wrike.update_settings({"api_token": ""})

        self.assertTrue(ok, err)
        self.assertTrue(wrike.get_settings_snapshot()["api_token_configured"])

    def test_prompt_masks_api_token_entry(self) -> None:
        wrike = self._new_wrike()
        askstring = Mock(return_value="prompt-token-1234567890")

        with patch.dict("sys.modules", {"tkinter.simpledialog": Mock(askstring=askstring)}):
            token = wrike._Wrike__prompt_api_token(root=object())

        self.assertEqual(token, "prompt-token-1234567890")
        self.assertEqual(askstring.call_args.kwargs.get("show"), "*")

    def test_secret_store_fails_closed_when_dpapi_is_unavailable(self) -> None:
        store = SecretStore("test")

        with patch.object(store, "_SecretStore__protect_with_win32crypt", return_value=b""):
            with patch.object(store, "_SecretStore__protect_with_ctypes", return_value=b""):
                protected = store.protect("secret-token")

        self.assertEqual(protected, "")
        self.assertEqual(store.unprotect("local-v1:c2VjcmV0LXRva2Vu"), "")

    def test_legacy_token_file_migrates_to_protected_settings_and_is_removed(self) -> None:
        config_dir = self.appdata / "windows-supporter"
        config_dir.mkdir(parents=True)
        token_path = config_dir / "wrike_token.txt"
        token_path.write_text("legacy-file-token-1234567890\n", encoding="utf-8")
        wrike = self._new_wrike()
        wrike._Wrike__secret_store.protect = Mock(return_value="dpapi:protected-token")

        token = wrike._Wrike__get_wrike_api_token(root=None, prompt_if_missing=False)

        self.assertEqual(token, "legacy-file-token-1234567890")
        self.assertFalse(token_path.exists())
        settings_path = Path(wrike.get_settings_snapshot()["settings_path"])
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_token_protected"], "dpapi:protected-token")
        self.assertNotIn("api_token", saved)
        self.assertNotIn("legacy-file-token-1234567890", settings_path.read_text(encoding="utf-8"))

    def test_legacy_token_file_is_kept_when_protected_save_fails(self) -> None:
        config_dir = self.appdata / "windows-supporter"
        config_dir.mkdir(parents=True)
        token_path = config_dir / "wrike_token.txt"
        token_path.write_text("legacy-file-token-1234567890\n", encoding="utf-8")
        wrike = self._new_wrike()
        wrike._Wrike__secret_store.protect = Mock(return_value="")

        token = wrike._Wrike__get_wrike_api_token(root=None, prompt_if_missing=False)

        self.assertEqual(token, "legacy-file-token-1234567890")
        self.assertTrue(token_path.exists())
        settings_path = Path(wrike.get_settings_snapshot()["settings_path"])
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertNotIn("api_token", saved)
        self.assertNotIn("api_token_protected", saved)


if __name__ == "__main__":
    unittest.main()
