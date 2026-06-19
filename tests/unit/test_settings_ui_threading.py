import unittest
from unittest.mock import patch

from src.apps.wrike_ui import WrikeSettingsView
from src.apps.startup_apps_ui import StartupAppsWindow


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


class _FakeVar:
    def __init__(self, value=None):
        self.value = value

    def set(self, value):
        self.value = value
        return None

    def get(self):
        return self.value

    def trace_add(self, *_args, **_kwargs):
        return "trace"


class _FakeWidget:
    def __init__(self, owner=None, *args, **kwargs):
        _ = args
        self._owner = owner
        self.kwargs = dict(kwargs)
        self.children = []
        self.pack_calls = []
        self.grid_calls = []
        self.configure_calls = []
        self.bind_calls = []
        self.after_calls = []

    def pack(self, **kwargs):
        self.pack_calls.append(dict(kwargs))
        return None

    def grid(self, **kwargs):
        self.grid_calls.append(dict(kwargs))
        return None

    def configure(self, **kwargs):
        self.configure_calls.append(dict(kwargs))
        return None

    def bind(self, event, callback):
        self.bind_calls.append((event, callback))
        return None

    def columnconfigure(self, *_args, **_kwargs):
        return None

    def rowconfigure(self, *_args, **_kwargs):
        return None

    def winfo_children(self):
        return list(self.children)

    def destroy(self):
        return None

    def after(self, delay_ms, callback):
        self.after_calls.append((int(delay_ms), callback))
        return f"after-{len(self.after_calls)}"


class _FakeButton(_FakeWidget):
    def __init__(self, owner=None, *args, **kwargs):
        super().__init__(owner, *args, **kwargs)
        if owner is not None:
            owner.buttons.append(self)


class _FakeTk:
    def Frame(self, *args, **kwargs):
        return _FakeWidget(self, *args, **kwargs)

    def Label(self, *args, **kwargs):
        return _FakeWidget(self, *args, **kwargs)

    def Checkbutton(self, *args, **kwargs):
        return _FakeWidget(self, *args, **kwargs)

    def StringVar(self, value=""):
        return _FakeVar(value=value)

    def BooleanVar(self, value=False):
        return _FakeVar(value=value)


class _FakeTtk:
    def __init__(self):
        self.buttons = []

    def Entry(self, *args, **kwargs):
        return _FakeWidget(self, *args, **kwargs)

    def Button(self, *args, **kwargs):
        return _FakeButton(self, *args, **kwargs)

    def Combobox(self, *args, **kwargs):
        return _FakeWidget(self, *args, **kwargs)


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

    def test_wrike_mount_removes_manual_save_and_reload_buttons(self) -> None:
        class _FakeWrike:
            def get_settings_snapshot(self):
                return {"settings_path": ""}

        fake_ttk = _FakeTtk()
        view = WrikeSettingsView(root=None, wrike=_FakeWrike())
        view._tk = _FakeTk()
        view._ttk = fake_ttk
        view._lazy_import_tk = lambda: None
        view._load_settings = lambda: None

        view.mount(_FakeWidget())

        button_texts = [button.kwargs.get("text") for button in fake_ttk.buttons]
        self.assertNotIn("저장", button_texts)
        self.assertNotIn("로드하기", button_texts)
        self.assertIn("토큰 지우기", button_texts)
        self.assertIn("토큰 검증", button_texts)

    def test_wrike_save_status_does_not_hide_main_ui(self) -> None:
        class _FakeWrike:
            def __init__(self):
                self.payloads = []

            def update_settings(self, payload):
                self.payloads.append(dict(payload))
                return True, None

        wrike = _FakeWrike()
        view = WrikeSettingsView(root=object(), wrike=wrike)
        view._token_var = _FakeVar(value="token")
        view._daily_var = _FakeVar(value="8")
        view._tooltip_var = _FakeVar(value="6")
        view._monitor_enabled_var = _FakeVar(value=True)
        view._monitor_interval_var = _FakeVar(value="5")
        statuses = []
        view._set_status = lambda text, level="info": statuses.append((str(text), str(level)))
        view._hide_main_ui = lambda: self.fail("Wrike settings save must not hide the dashboard")

        view._on_save()

        self.assertTrue(wrike.payloads)
        self.assertEqual(statuses[-1], ("저장됨", "ok"))

    def test_startup_toggle_schedules_autosave(self) -> None:
        view = StartupAppsWindow(root=object(), manager=object())
        view._instances = [{"enabled": True}]
        view._selected_index = lambda: 0
        view._refresh_tree = lambda: None
        scheduled = []
        view._schedule_autosave = lambda: scheduled.append(True)

        view._on_toggle_enabled()

        self.assertEqual(view._instances[0]["enabled"], False)
        self.assertEqual(scheduled, [True])


if __name__ == "__main__":
    unittest.main()
