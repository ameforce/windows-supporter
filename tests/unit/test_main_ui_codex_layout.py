import ctypes
import unittest
from unittest.mock import patch

from src.apps.ai_usage_ui import AIUsageSettingsView
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

    def winfo_children(self):
        return []


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

    def test_ai_usage_tab_default_size_is_wider_and_content_fit(self) -> None:
        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", return_value=None):
            with patch.object(WindowsSupporterMainUI, "_build_shell", return_value=None):
                ui = WindowsSupporterMainUI(root=object(), startup_manager=object(), monitor=object())

        width, height = ui._tab_sizes.get(ui._TAB_AI_USAGE)
        min_width, min_height = ui._tab_minsizes.get(ui._TAB_AI_USAGE)
        # 프로필 2개(2열 카드) 콘텐츠 요구 높이가 ~740px이므로 기본 창은
        # 스크롤 없이 주요 항목이 보이는 크기여야 한다.
        self.assertGreaterEqual(width, 1100)
        self.assertGreaterEqual(height, 740)
        self.assertGreaterEqual(min_width, 940)
        self.assertLessEqual(min_height, 600)

    def test_ui_scale_clamps_tk_scaling_ratio(self) -> None:
        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", return_value=None):
            with patch.object(WindowsSupporterMainUI, "_build_shell", return_value=None):
                ui = WindowsSupporterMainUI(root=object(), startup_manager=object(), monitor=object())

        self.assertEqual(ui._ui_scale(), 1.0)

        class _TkBridge:
            def call(self, *_args):
                return 2.0

        ui._root = type("Root", (), {"tk": _TkBridge()})()
        self.assertAlmostEqual(ui._ui_scale(), 1.5)

        class _HugeBridge:
            def call(self, *_args):
                return 99.0

        ui._root = type("Root", (), {"tk": _HugeBridge()})()
        self.assertEqual(ui._ui_scale(), 3.0)

    def test_ai_usage_geometry_is_capped_to_current_monitor_work_area(self) -> None:
        class _GeometryRoot(_FakeRoot):
            def __init__(self):
                super().__init__()
                self.geometry_calls = []
                self.minsize_calls = []
                self.resize_events = []

            def winfo_width(self):
                return 1200

            def winfo_height(self):
                return 900

            def geometry(self, value):
                self.geometry_calls.append(value)
                self.resize_events.append(("geometry", value))

            def minsize(self, width, height):
                self.minsize_calls.append((int(width), int(height)))
                self.resize_events.append(("minsize", (int(width), int(height))))

        root = _GeometryRoot()
        ui, _, _ = self._build_ui(root=root)
        ui._work_area_size = lambda: (800, 500)

        ui._apply_tab_geometry(ui._TAB_AI_USAGE)

        self.assertEqual(root.geometry_calls[-1], "768x452")
        self.assertEqual(root.minsize_calls[-1], (768, 452))
        self.assertEqual(
            root.resize_events,
            [("minsize", (768, 452)), ("geometry", "768x452")],
        )

    def test_work_area_winapi_uses_pointer_sized_monitor_handles(self) -> None:
        class _Callable:
            def __init__(self, result):
                self.result = result
                self.argtypes = None
                self.restype = None
                self.calls = []

            def __call__(self, *args):
                self.calls.append(args)
                return self.result

        class _User32:
            def __init__(self):
                self.MonitorFromWindow = _Callable(0x123456789ABC)
                self.GetMonitorInfoW = _Callable(0)

        class _Root(_FakeRoot):
            def winfo_id(self):
                return 0x23456789ABCD

            def winfo_screenwidth(self):
                return 1920

            def winfo_screenheight(self):
                return 1080

        user32 = _User32()
        ui, _, _ = self._build_ui(root=_Root())

        with patch.object(ctypes, "windll", type("Windll", (), {"user32": user32})(), create=True):
            self.assertEqual(ui._work_area_size(), (1920, 1080))

        self.assertIs(user32.MonitorFromWindow.argtypes[0], ctypes.c_void_p)
        self.assertIs(user32.MonitorFromWindow.restype, ctypes.c_void_p)
        self.assertIs(user32.GetMonitorInfoW.argtypes[0], ctypes.c_void_p)
        self.assertEqual(user32.MonitorFromWindow.calls[0][0], 0x23456789ABCD)
        self.assertEqual(user32.GetMonitorInfoW.calls[0][0], 0x123456789ABC)

    def test_main_shell_uses_ai_usage_tab_title_and_loading_text(self) -> None:
        fake_ttk = _FakeTtk()

        def install_fake_tk(ui):
            ui._tk = object()
            ui._ttk = fake_ttk

        with patch.object(WindowsSupporterMainUI, "_lazy_import_tk", install_fake_tk):
            WindowsSupporterMainUI(
                root=_FakeRoot(),
                startup_manager=object(),
                monitor=object(),
            )

        tab_titles = [title for _tab, title in fake_ttk.notebooks[0].tabs]
        label_texts = [label.kwargs.get("text") for label in fake_ttk.labels]
        self.assertIn("AI 사용량", tab_titles)
        self.assertNotIn("Codex", tab_titles)
        self.assertIn("AI 사용량 설정을 여는 중...", label_texts)

    def test_ai_usage_facade_rewrites_only_common_codex_titles(self) -> None:
        class _TextWidget:
            def __init__(self, text="", children=None):
                self.text = text
                self.children = list(children or [])

            def winfo_children(self):
                return list(self.children)

            def cget(self, key):
                if key != "text":
                    raise KeyError(key)
                return self.text

            def configure(self, **kwargs):
                if "text" in kwargs:
                    self.text = kwargs["text"]

        title = _TextWidget("Codex Usage Monitoring 설정")
        description = _TextWidget("Codex 사용량 자동 모니터링 동작을 설정합니다.")
        provider_specific = _TextWidget("Codex 1")
        parent = _TextWidget(children=[title, description, provider_specific])
        view = AIUsageSettingsView(root=None, usage_monitor=None)

        with patch(
            "src.apps.ai_usage_ui.CodexUsageSettingsView.mount",
            return_value=None,
        ):
            view.mount(parent)

        self.assertEqual(title.text, "AI 사용량 설정")
        self.assertEqual(
            description.text,
            "AI 사용량 프로필과 자동 모니터링 동작을 설정합니다.",
        )
        self.assertEqual(provider_specific.text, "Codex 1")

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
