import unittest
from unittest.mock import patch

from src.apps.main_ui import WindowsSupporterMainUI


class _FakeRoot:
    def __init__(self, *, after_result_prefix="after", raise_after=False, falsey_after=False):
        self.after_calls = []
        self.after_result_prefix = after_result_prefix
        self.raise_after = raise_after
        self.falsey_after = falsey_after

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))
        if self.raise_after:
            raise RuntimeError("after failed")
        if self.falsey_after:
            return None
        return f"{self.after_result_prefix}-{len(self.after_calls)}"


class _FakeWidget:
    def __init__(self, master=None, **kwargs):
        self.master = master
        self.kwargs = dict(kwargs)
        self.pack_calls = []

    def pack(self, **kwargs):
        self.pack_calls.append(dict(kwargs))


class _FakeNotebook(_FakeWidget):
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self.tabs = []
        self.bind_calls = []

    def add(self, child, text=""):
        self.tabs.append((child, text))

    def bind(self, event, callback):
        self.bind_calls.append((event, callback))


class _FakeTtk:
    def __init__(self):
        self.frames = []
        self.labels = []
        self.notebooks = []

    def Frame(self, master=None, **kwargs):
        frame = _FakeWidget(master, **kwargs)
        self.frames.append(frame)
        return frame

    def Label(self, master=None, **kwargs):
        label = _FakeWidget(master, **kwargs)
        self.labels.append(label)
        return label

    def Notebook(self, master=None, **kwargs):
        notebook = _FakeNotebook(master, **kwargs)
        self.notebooks.append(notebook)
        return notebook


class _FakeKakao:
    def __init__(self, results):
        self.results = list(results)
        self.open_calls = []

    def open_monitor_selector(self, root, embedded_parent=None):
        self.open_calls.append((root, embedded_parent))
        if self.results:
            return self.results.pop(0)
        return False


class _FakeMonitor:
    def __init__(self, kakao):
        self.kakao = kakao

    def get_kakao_manager(self):
        return self.kakao


class MainUiCodexLayoutUnitTest(unittest.TestCase):
    def _build_ui(self, *, root=None, kakao=None):
        if root is None:
            root = _FakeRoot()
        if kakao is None:
            kakao = _FakeKakao([True])
        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", return_value=None):
            with patch.object(WindowsSupporterMainUI, "_build_shell", return_value=None):
                ui = WindowsSupporterMainUI(
                    root=root,
                    startup_manager=object(),
                    monitor=_FakeMonitor(kakao),
                )
        ui._tab_kakao = object()
        return ui, root, kakao

    def test_codex_tab_default_size_fits_realtime_status_on_first_open(self) -> None:
        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", return_value=None):
            with patch.object(WindowsSupporterMainUI, "_build_shell", return_value=None):
                ui = WindowsSupporterMainUI(root=object(), startup_manager=object(), monitor=object())

        self.assertEqual(ui._tab_sizes.get(ui._TAB_CODEX), (900, 760))
        self.assertEqual(ui._tab_minsizes.get(ui._TAB_CODEX), (860, 720))

    def test_main_shell_places_version_label_at_bottom_right(self) -> None:
        fake_ttk = _FakeTtk()

        def install_fake_tk(ui):
            ui._tk = object()
            ui._ttk = fake_ttk

        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", install_fake_tk):
            with patch("src.apps.main_ui.get_app_version_label", return_value="Version v0.3.5 (64d97c3)"):
                ui = WindowsSupporterMainUI(
                    root=_FakeRoot(),
                    startup_manager=object(),
                    monitor=object(),
                )

        self.assertIsNotNone(ui._footer_frame)
        self.assertIsNotNone(ui._version_label)
        self.assertEqual(ui._version_label.kwargs.get("text"), "Version v0.3.5 (64d97c3)")
        self.assertEqual(ui._version_label.kwargs.get("anchor"), "e")
        self.assertEqual(ui._footer_frame.pack_calls[-1].get("side"), "bottom")
        self.assertEqual(ui._version_label.pack_calls[-1].get("side"), "right")

    def test_kakao_build_false_keeps_tab_unbuilt_and_schedules_single_retry(self) -> None:
        ui, root, kakao = self._build_ui(kakao=_FakeKakao([False, False]))

        ui._ensure_kakao_built()
        ui._ensure_kakao_built()

        self.assertFalse(ui._kakao_built)
        self.assertEqual(len(kakao.open_calls), 2)
        self.assertEqual(len(root.after_calls), 1)
        delay, retry = root.after_calls[0]
        self.assertGreaterEqual(delay, 500)
        self.assertIsNotNone(ui._kakao_retry_after_id)

        retry()

        self.assertEqual(len(root.after_calls), 2)
        self.assertEqual(ui._kakao_retry_after_id, "after-2")

    def test_kakao_build_success_marks_built_without_retry(self) -> None:
        ui, root, kakao = self._build_ui(kakao=_FakeKakao([True]))

        ui._ensure_kakao_built()

        self.assertTrue(ui._kakao_built)
        self.assertEqual(len(kakao.open_calls), 1)
        self.assertEqual(root.after_calls, [])

    def test_kakao_retry_after_failure_clears_guard_for_future_attempts(self) -> None:
        for root in (_FakeRoot(raise_after=True), _FakeRoot(falsey_after=True)):
            ui, _, _ = self._build_ui(root=root, kakao=_FakeKakao([False]))

            ui._ensure_kakao_built()

            self.assertFalse(ui._kakao_built)
            self.assertIsNone(ui._kakao_retry_after_id)


if __name__ == "__main__":
    unittest.main()
