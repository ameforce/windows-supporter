import unittest
from unittest.mock import patch

import main


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls = []
        self.withdraw_calls = 0
        self.destroy_calls = 0
        self.quit_calls = 0

    def iconbitmap(self, _path):
        return None

    def withdraw(self):
        self.withdraw_calls += 1

    def after(self, delay_ms, callback):
        self.after_calls.append((int(delay_ms), callback))
        return f"after-{len(self.after_calls)}"

    def mainloop(self):
        return None

    def quit(self):
        self.quit_calls += 1

    def destroy(self):
        self.destroy_calls += 1


class _FakeKeyboard:
    def __init__(self) -> None:
        self.unhook_calls = 0

    def unhook_all(self):
        self.unhook_calls += 1


class _FakeLib:
    def __init__(self, root) -> None:
        self.tk = type("_TkModule", (), {"Tk": lambda _self: root})()
        self.keyboard = _FakeKeyboard()


class _FakeStartupManager:
    def __init__(self) -> None:
        self.start_calls = []
        self.shutdown_calls = 0

    def start(self, root):
        self.start_calls.append(root)

    def shutdown(self):
        self.shutdown_calls += 1

    def rescan_defaults_merge(self):
        return None

    def toggle_enabled(self):
        return True

    def get_enabled_state(self):
        return True

    def open_config_file(self):
        return None

    def open_config_dir(self):
        return None

    def open_log_file(self):
        return None


class _FakeMonitor:
    def attach(self, _root, _event_queue):
        return None

    def on_session_unlock(self):
        return None

    def on_display_topology_changed(self, _reason):
        return None


class _FakeUi:
    def show(self):
        return None


class _FakePump:
    def __init__(self, **_kwargs) -> None:
        return

    def start(self):
        return None


class _FakeTray:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1


class _FakeThread:
    def __init__(self, target=None, daemon=False, **_kwargs) -> None:
        self.target = target
        self.daemon = bool(daemon)

    def start(self):
        return None


class MainStartupBehaviourUnitTest(unittest.TestCase):
    def test_main_does_not_schedule_startup_apps_on_launch(self) -> None:
        root = _FakeRoot()
        lib = _FakeLib(root)
        startup = _FakeStartupManager()

        with patch("main.LibConnector", return_value=lib):
            with patch("main.StartReg"):
                with patch("main.threading.Thread", _FakeThread):
                    with patch("main.Monitor", return_value=_FakeMonitor()):
                        with patch("main.StartupAppManager", return_value=startup):
                            with patch("main.WindowsSupporterMainUI", return_value=_FakeUi()):
                                with patch("main.SharedUiEventPump", _FakePump):
                                    with patch("main.SystemTrayIcon", _FakeTray):
                                        with patch("main.signal.signal"):
                                            main.main()

        self.assertNotIn(120, [delay for delay, _callback in root.after_calls])
        self.assertEqual(startup.start_calls, [])


if __name__ == "__main__":
    unittest.main()
