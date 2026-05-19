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


class _FakeLocator:
    def __init__(self, page, name: str, count: int = 0) -> None:
        self.page = page
        self.name = name
        self._count = count
        self.filled_values = []

    @property
    def first(self):
        return self

    def count(self) -> int:
        return self._count

    def click(self, **_kwargs) -> None:
        self.page.clicked.append(self.name)

    def fill(self, value: str, **_kwargs) -> None:
        self.filled_values.append(value)
        self.page.filled.append((self.name, value))


class _DelayedFakeLocator(_FakeLocator):
    def wait_for(self, **_kwargs) -> None:
        self.page.waited.append(self.name)
        self._count = 1


class _BlockingStartNewLocator(_DelayedFakeLocator):
    def click(self, **_kwargs) -> None:
        super().click(**_kwargs)
        self.page.labels["제목"] = _FakeLocator(self.page, "label:제목", 1)


class _FakeWrikeFormPage:
    def __init__(self, *, has_draft_prompt: bool = False) -> None:
        self.clicked = []
        self.filled = []
        self.waited = []
        self.roles = {}
        self.labels = {}
        self.placeholders = {}
        self.selectors = {}
        if has_draft_prompt:
            self.roles[("button", "Start new")] = _FakeLocator(self, "button:Start new", 1)
            self.roles[("button", "Resume")] = _FakeLocator(self, "button:Resume", 1)
        self.labels["제목"] = _FakeLocator(self, "label:제목", 1)
        self.roles[("button", "Submit")] = _FakeLocator(self, "button:Submit", 1)
        self.roles[("button", "제출")] = _FakeLocator(self, "button:제출", 1)

    def get_by_role(self, role: str, **kwargs):
        return self.roles.get((role, kwargs.get("name")), _FakeLocator(self, f"{role}:{kwargs.get('name')}", 0))

    def get_by_label(self, label: str, **_kwargs):
        return self.labels.get(label, _FakeLocator(self, f"label:{label}", 0))

    def get_by_placeholder(self, placeholder: str, **_kwargs):
        return self.placeholders.get(placeholder, _FakeLocator(self, f"placeholder:{placeholder}", 0))

    def get_by_text(self, text, **_kwargs):
        return self.selectors.get(f"text:{text}", _FakeLocator(self, f"text:{text}", 0))

    def locator(self, selector: str):
        return self.selectors.get(selector, _FakeLocator(self, f"selector:{selector}", 0))

    def wait_for_load_state(self, **_kwargs) -> None:
        return None


class _FakeWrikeLoginPage:
    url = "https://www.wrike.com/workspace.htm?acc=469516#/forms?formid=2239448"

    def title(self) -> str:
        return "Sign in to your Wrike account"

    def locator(self, _selector: str):
        return _FakeLocator(self, "password", 0)

    def get_by_text(self, _label: str, **_kwargs):
        return _FakeLocator(self, "text", 0)


class _ClosedWrikeFormContext:
    def __init__(self) -> None:
        self.new_page_calls = 0

    @property
    def pages(self):
        raise RuntimeError("Target page, context or browser has been closed")

    def new_page(self):
        self.new_page_calls += 1
        raise RuntimeError("Target page, context or browser has been closed")

    def close(self) -> None:
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

    def test_transform_text_removes_separator_before_bracketed_description(self) -> None:
        wrike = self._new_wrike()

        transformed = wrike.transform_text("[인프라] [MARS] - Windows 빌드 서버 마이그레이션")

        self.assertEqual(transformed, "[인프라] [MARS] [Windows 빌드 서버 마이그레이션] [] - ")

    def test_fill_wrike_form_title_clicks_start_new_before_title_fill(self) -> None:
        wrike = self._new_wrike()
        page = _FakeWrikeFormPage(has_draft_prompt=True)

        ok = wrike._Wrike__fill_wrike_form_title(page, "[인프라] [MARS] [Windows 빌드 서버] [] - ")

        self.assertTrue(ok)
        self.assertEqual(page.clicked, ["button:Start new"])
        self.assertEqual(page.filled, [("label:제목", "[인프라] [MARS] [Windows 빌드 서버] [] - ")])
        self.assertNotIn("button:Submit", page.clicked)
        self.assertNotIn("button:제출", page.clicked)

    def test_fill_wrike_form_title_clicks_selector_start_new_when_role_lookup_misses(self) -> None:
        wrike = self._new_wrike()
        page = _FakeWrikeFormPage()
        page.selectors['button:has-text("Start new")'] = _FakeLocator(
            page,
            "selector:button-start-new",
            1,
        )

        ok = wrike._Wrike__fill_wrike_form_title(page, "[인프라] [MARS] [Windows 빌드 서버] [] - ")

        self.assertTrue(ok)
        self.assertEqual(page.clicked, ["selector:button-start-new"])
        self.assertEqual(page.filled, [("label:제목", "[인프라] [MARS] [Windows 빌드 서버] [] - ")])

    def test_fill_wrike_form_title_waits_for_delayed_start_new_prompt(self) -> None:
        wrike = self._new_wrike()
        page = _FakeWrikeFormPage()
        page.labels["제목"] = _FakeLocator(page, "label:제목", 0)
        page.roles[("button", "Start new")] = _BlockingStartNewLocator(page, "button:Start new", 0)

        ok = wrike._Wrike__fill_wrike_form_title(page, "[인프라] [MARS] [Windows 빌드 서버] [] - ")

        self.assertTrue(ok)
        self.assertEqual(page.waited, ["button:Start new"])
        self.assertEqual(page.clicked, ["button:Start new"])

    def test_fill_wrike_form_title_skips_draft_prompt_wait_when_title_is_ready(self) -> None:
        wrike = self._new_wrike()
        page = _FakeWrikeFormPage()
        page.roles[("button", "Start new")] = _DelayedFakeLocator(page, "button:Start new", 0)

        ok = wrike._Wrike__fill_wrike_form_title(page, "[인프라] [MARS] [Windows 빌드 서버] [] - ")

        self.assertTrue(ok)
        self.assertEqual(page.waited, [])
        self.assertEqual(page.clicked, [])
        self.assertEqual(page.filled, [("label:제목", "[인프라] [MARS] [Windows 빌드 서버] [] - ")])

    def test_fill_wrike_form_title_waits_briefly_for_delayed_title_field(self) -> None:
        wrike = self._new_wrike()
        page = _FakeWrikeFormPage()
        page.labels["제목"] = _DelayedFakeLocator(page, "label:제목", 0)

        ok = wrike._Wrike__fill_wrike_form_title(page, "[인프라] [MARS] [Windows 빌드 서버] [] - ")

        self.assertTrue(ok)
        self.assertEqual(page.waited, ["label:제목"])
        self.assertEqual(page.filled, [("label:제목", "[인프라] [MARS] [Windows 빌드 서버] [] - ")])

    def test_requires_login_detects_wrike_sign_in_title(self) -> None:
        wrike = self._new_wrike()

        self.assertTrue(wrike._Wrike__requires_login(_FakeWrikeLoginPage()))

    def test_fill_wrike_form_rechecks_login_after_page_settles(self) -> None:
        wrike = self._new_wrike()
        page = Mock()
        page.url = wrike._Wrike__form_url
        page.goto.return_value = None
        page.wait_for_load_state.return_value = None
        with patch.object(wrike, "_Wrike__ensure_playwright_ready", return_value=True):
            with patch.object(wrike, "_Wrike__get_wrike_form_page", return_value=(page, None)):
                with patch.object(
                    wrike,
                    "_Wrike__ensure_wrike_logged_in",
                    side_effect=[None, "Wrike 로그인 시간 초과"],
                ) as ensure_login:
                    with patch.object(wrike, "_Wrike__fill_wrike_form_title", return_value=True):
                        result = wrike._Wrike__fill_wrike_form_with_playwright(
                            _RecordingRoot(),
                            "[인프라] [MARS] [Windows 빌드 서버] [] - ",
                        )

        self.assertEqual(result, "Wrike 로그인 시간 초과")
        self.assertEqual(ensure_login.call_count, 2)

    def test_fill_wrike_form_uses_short_page_settle_wait_before_title_lookup(self) -> None:
        wrike = self._new_wrike()
        page = Mock()
        page.url = wrike._Wrike__form_url
        page.goto.return_value = None
        page.wait_for_load_state.return_value = None

        with patch.object(wrike, "_Wrike__ensure_playwright_ready", return_value=True):
            with patch.object(wrike, "_Wrike__get_wrike_form_page", return_value=(page, None)):
                with patch.object(wrike, "_Wrike__ensure_wrike_logged_in", return_value=None):
                    with patch.object(wrike, "_Wrike__fill_wrike_form_title", return_value=True):
                        result = wrike._Wrike__fill_wrike_form_with_playwright(
                            _RecordingRoot(),
                            "[인프라] [MARS] [Windows 빌드 서버] [] - ",
                        )

        self.assertIsNone(result)
        page.wait_for_load_state.assert_not_called()

    def test_fill_wrike_form_reuses_existing_form_page_without_reloading(self) -> None:
        wrike = self._new_wrike()
        page = Mock()
        page.url = wrike._Wrike__form_url

        with patch.object(wrike, "_Wrike__ensure_playwright_ready", return_value=True):
            with patch.object(wrike, "_Wrike__get_wrike_form_page", return_value=(page, None)):
                with patch.object(wrike, "_Wrike__ensure_wrike_logged_in", return_value=None):
                    with patch.object(wrike, "_Wrike__fill_wrike_form_title", return_value=True):
                        result = wrike._Wrike__fill_wrike_form_with_playwright(
                            _RecordingRoot(),
                            "[인프라] [MARS] [Windows 빌드 서버] [] - ",
                        )

        self.assertIsNone(result)
        page.goto.assert_not_called()

    def test_fill_wrike_form_retries_after_stale_thread_bound_page(self) -> None:
        wrike = self._new_wrike()
        stale_page = Mock()
        stale_page.goto.side_effect = RuntimeError(
            "cannot switch to a different thread (which happens to have exited)"
        )
        fresh_page = Mock()
        fresh_page.url = wrike._Wrike__form_url
        fresh_page.goto.return_value = None
        fresh_page.wait_for_load_state.return_value = None

        with patch.object(wrike, "_Wrike__ensure_playwright_ready", return_value=True):
            with patch.object(
                wrike,
                "_Wrike__get_wrike_form_page",
                side_effect=[(stale_page, None), (fresh_page, None)],
            ) as get_page:
                with patch.object(wrike, "_Wrike__ensure_wrike_logged_in", return_value=None):
                    with patch.object(wrike, "_Wrike__fill_wrike_form_title", return_value=True):
                        result = wrike._Wrike__fill_wrike_form_with_playwright(
                            _RecordingRoot(),
                            "[인프라] [MARS] [Windows 빌드 서버] [] - ",
                        )

        self.assertIsNone(result)
        self.assertEqual(get_page.call_count, 2)
        fresh_page.goto.assert_called_once()

    def test_reset_wrike_form_browser_handles_stops_old_playwright(self) -> None:
        wrike = self._new_wrike()
        old_context = Mock()
        old_playwright = Mock()
        wrike._Wrike__form_page = Mock()
        wrike._Wrike__form_context = old_context
        wrike._Wrike__form_playwright = old_playwright

        wrike._Wrike__reset_wrike_form_browser_handles()

        self.assertIsNone(wrike._Wrike__form_page)
        self.assertIsNone(wrike._Wrike__form_context)
        self.assertIsNone(wrike._Wrike__form_playwright)
        old_context.close.assert_called_once()
        old_playwright.stop.assert_called_once()

    def test_get_wrike_form_page_recovers_from_closed_cached_context(self) -> None:
        wrike = self._new_wrike()
        closed_context = _ClosedWrikeFormContext()
        fresh_page = Mock()
        fresh_page.is_closed.return_value = False
        fresh_context = Mock()
        fresh_context.pages = []
        fresh_context.new_page.return_value = fresh_page
        wrike._Wrike__form_playwright = Mock()
        wrike._Wrike__form_context = closed_context

        with patch.object(wrike, "_Wrike__ensure_wrike_form_playwright_started", return_value=True) as ensure_started:
            with patch.object(wrike, "_Wrike__launch_playwright_context", return_value=fresh_context):
                page, error = wrike._Wrike__get_wrike_form_page()

        self.assertIs(page, fresh_page)
        self.assertIsNone(error)
        self.assertGreaterEqual(ensure_started.call_count, 1)
        fresh_context.new_page.assert_called_once()

    def test_get_wrike_form_page_recovers_when_new_page_reports_closed_browser(self) -> None:
        wrike = self._new_wrike()
        stale_context = Mock()
        stale_context.pages = []
        stale_context.new_page.side_effect = RuntimeError("Browser has been closed")
        fresh_page = Mock()
        fresh_page.is_closed.return_value = False
        fresh_context = Mock()
        fresh_context.pages = []
        fresh_context.new_page.return_value = fresh_page
        wrike._Wrike__form_playwright = Mock()
        wrike._Wrike__form_context = stale_context

        with patch.object(wrike, "_Wrike__ensure_wrike_form_playwright_started", return_value=True):
            with patch.object(wrike, "_Wrike__launch_playwright_context", return_value=fresh_context):
                page, error = wrike._Wrike__get_wrike_form_page()

        self.assertIs(page, fresh_page)
        self.assertIsNone(error)
        stale_context.new_page.assert_called_once()
        fresh_context.new_page.assert_called_once()

    def test_attach_prewarms_form_browser_without_inline_playwright(self) -> None:
        wrike = self._new_wrike()
        root = _RecordingRoot()
        _FakeThread.created = []

        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            with patch.object(
                wrike,
                "_Wrike__prewarm_wrike_form_browser",
                side_effect=AssertionError("Playwright prewarm must not run on attach caller thread"),
                create=True,
            ):
                wrike.attach(root)

        self.assertEqual(len(_FakeThread.created), 1)
        self.assertTrue(_FakeThread.created[0].daemon)

    def test_form_browser_prewarm_failure_does_not_poison_later_retry(self) -> None:
        wrike = self._new_wrike()
        wrike._Wrike__playwright_checked = True
        wrike._Wrike__playwright_ready = False

        with patch.object(wrike, "_Wrike__ensure_playwright_ready", return_value=False):
            wrike._Wrike__prewarm_wrike_form_browser()

        self.assertFalse(wrike._Wrike__playwright_checked)

    def test_action_worker_dispatches_form_fill_to_browser_worker(self) -> None:
        wrike = self._new_wrike()
        root = _RecordingRoot()
        source_title = "[인프라] [MARS] - Windows 빌드 서버 마이그레이션"
        normalized = "[인프라] [MARS] [Windows 빌드 서버 마이그레이션] [] - "

        with patch.object(wrike._Wrike__lib.pyautogui, "click", return_value=None):
            with patch.object(wrike._Wrike__lib.pyautogui, "hotkey", return_value=None):
                with patch.object(wrike._Wrike__lib.time, "sleep", return_value=None):
                    with patch.object(wrike, "_Wrike__safe_clipboard_paste", return_value="before"):
                        with patch.object(wrike, "_Wrike__wait_for_clipboard_update", return_value=source_title):
                            with patch.object(
                                wrike,
                                "_Wrike__fill_wrike_form_with_playwright",
                                side_effect=AssertionError("Playwright must stay on the browser worker thread"),
                            ):
                                with patch.object(
                                    wrike,
                                    "_Wrike__fill_wrike_form_on_browser_worker",
                                    return_value=None,
                                    create=True,
                                ) as fill_on_worker:
                                    wrike._Wrike__action_worker(root)

        fill_on_worker.assert_called_once_with(root, normalized)

    def test_monitor_worker_posts_finish_to_ui_thread_before_rescheduling(self) -> None:
        wrike = self._new_wrike()
        root = _RecordingRoot()
        wrike._Wrike__monitor_enabled = True
        wrike._Wrike__wrike_api_token_session = "token"
        with patch.object(wrike, "_Wrike__prewarm_wrike_form_browser_async", return_value=None):
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
