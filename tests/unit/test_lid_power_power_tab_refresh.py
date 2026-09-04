from __future__ import annotations

import os
import queue
import threading
import time
import unittest
from unittest.mock import patch

import main
from src.apps.lid_power_policy import LidPowerPolicyService, PowerCapabilities
from src.apps.lid_power_settings_ui import LidPowerSettingsView
from src.apps.main_ui import WindowsSupporterMainUI
from src.utils.tray_icon import TrayReady
from src.utils.ui_event_pump import SharedUiEventPump


class _Lease:
    def __init__(self, *, fail_begin: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_begin = bool(fail_begin)

    def start(self) -> None:
        self.calls.append("start")

    def begin_ac_clamshell_session(self) -> None:
        self.calls.append("begin")
        if self.fail_begin:
            raise RuntimeError("begin failed")

    def end_ac_clamshell_session(self) -> None:
        self.calls.append("end")

    def reconcile_active_scheme(self) -> None:
        self.calls.append("scheme")

    def shutdown(self) -> None:
        self.calls.append("shutdown")


def _service(*, lease: _Lease | None = None) -> LidPowerPolicyService:
    return LidPowerPolicyService(
        capabilities=PowerCapabilities(True, True, False, None),
        lease=lease or _Lease(),
        request_sleep=lambda _reason: None,
        settings_path=None,
    )


class LidPowerStatusNotificationUnitTest(unittest.TestCase):
    def test_disabled_observations_are_visible_and_only_actual_snapshot_changes_notify(self) -> None:
        service = _service()
        notifications: list[dict] = []
        service.set_status_changed_callback(
            lambda: notifications.append(service.get_settings_snapshot())
        )

        service.handle_power_setting("acdc", 0)
        service.handle_power_setting("lid", 1)
        service.handle_power_setting("battery", 67)
        service.handle_power_setting("acdc", 0)
        service.handle_power_setting("lid", 1)
        service.handle_power_setting("battery", 67)

        snapshot = service.get_settings_snapshot()
        self.assertFalse(snapshot["enabled"])
        self.assertFalse(snapshot["runtime_enabled"])
        self.assertEqual("ac", snapshot["power_source"])
        self.assertIs(snapshot["lid_open"], True)
        self.assertEqual(67, snapshot["battery_percent"])
        self.assertEqual(3, len(notifications))

    def test_enable_disable_and_resume_each_notify_once_when_public_snapshot_changes(self) -> None:
        service = _service()
        notifications: list[dict] = []
        service.set_status_changed_callback(
            lambda: notifications.append(service.get_settings_snapshot())
        )

        self.assertTrue(service.update_enabled(True)[0])
        self.assertTrue(service.update_enabled(True)[0])
        self.assertTrue(service.update_enabled(False)[0])
        self.assertTrue(service.update_enabled(False)[0])
        self.assertEqual(2, len(notifications))

        service.handle_power_setting("acdc", 1)
        service.handle_power_setting("lid", 0)
        service.handle_power_setting("battery", 12)
        before_resume = len(notifications)
        service.on_resume()
        service.on_resume()

        snapshot = service.get_settings_snapshot()
        self.assertIsNone(snapshot["power_source"])
        self.assertIsNone(snapshot["lid_open"])
        self.assertIsNone(snapshot["battery_percent"])
        self.assertEqual(before_resume + 1, len(notifications))

    def test_notification_and_controller_failures_notify_once_and_callback_errors_are_isolated(self) -> None:
        lease = _Lease(fail_begin=True)
        service = _service(lease=lease)
        notifications: list[dict] = []
        service.set_status_changed_callback(
            lambda: notifications.append(service.get_settings_snapshot())
        )
        self.assertTrue(service.update_enabled(True)[0])
        service.handle_power_setting("acdc", 0)
        service.handle_power_setting("lid", 1)
        notifications.clear()

        service.handle_power_setting("lid", 0)

        self.assertEqual(1, len(notifications))
        self.assertFalse(service.get_settings_snapshot()["runtime_enabled"])
        self.assertIn("shutdown", lease.calls)

        service.set_status_changed_callback(lambda: (_ for _ in ()).throw(RuntimeError("ui failed")))
        service.notification_failure("registration failed")
        self.assertFalse(service.get_settings_snapshot()["enabled"])
        self.assertIn("shutdown", lease.calls)

    def test_controller_failure_callback_runs_after_service_and_controller_locks_release(self) -> None:
        service = _service(lease=_Lease(fail_begin=True))
        self.assertTrue(service.update_enabled(True)[0])
        service.handle_power_setting("acdc", 0)
        service.handle_power_setting("lid", 1)
        reader_finished: list[bool] = []
        readers: list[threading.Thread] = []

        def callback() -> None:
            completed = threading.Event()

            def read_snapshot() -> None:
                service.get_settings_snapshot()
                completed.set()

            reader = threading.Thread(target=read_snapshot)
            readers.append(reader)
            reader.start()
            reader_finished.append(completed.wait(0.2))

        service.set_status_changed_callback(callback)
        service.handle_power_setting("lid", 0)

        for reader in readers:
            reader.join(0.5)
        self.assertEqual([True], reader_finished)
        self.assertTrue(all(not reader.is_alive() for reader in readers))

    def test_notification_failure_notifies_exactly_once_per_public_snapshot_change(self) -> None:
        service = _service()
        self.assertTrue(service.update_enabled(True)[0])
        notifications: list[dict] = []
        service.set_status_changed_callback(
            lambda: notifications.append(service.get_settings_snapshot())
        )

        service.notification_failure("registration failed")
        service.notification_failure("registration failed")

        self.assertEqual(1, len(notifications))
        self.assertFalse(notifications[-1]["enabled"])
        self.assertFalse(notifications[-1]["runtime_enabled"])


class _RefreshView:
    def __init__(self, service: LidPowerPolicyService) -> None:
        self._service = service
        self.snapshots: list[dict] = []
        self.thread_ids: list[int] = []

    def refresh(self) -> None:
        self.thread_ids.append(threading.get_ident())
        self.snapshots.append(self._service.get_settings_snapshot())


class _FailingQueue:
    def put(self, _callback) -> None:
        raise RuntimeError("queue unavailable")


class _Notebook:
    def __init__(self, selected: str = "tab-power") -> None:
        self.selected = selected

    def select(self, value=None):
        if value is not None:
            self.selected = str(value)
        return self.selected


def _bare_ui(service: LidPowerPolicyService, event_queue, view: _RefreshView):
    ui = WindowsSupporterMainUI.__new__(WindowsSupporterMainUI)
    ui._lid_power_policy = service
    ui._event_queue = event_queue
    ui._power_status_refresh_lock = threading.Lock()
    ui._power_status_refresh_pending = False
    ui._power_view = view
    return ui


class LidPowerUiRefreshUnitTest(unittest.TestCase):
    def test_worker_callback_only_queues_and_drain_refreshes_final_service_snapshot(self) -> None:
        service = _service()
        events: queue.Queue = queue.Queue()
        view = _RefreshView(service)
        ui = _bare_ui(service, events, view)
        ui._attach_lid_power_status_callback()
        main_thread = threading.get_ident()

        def worker() -> None:
            service.handle_power_setting("acdc", 1)
            service.handle_power_setting("lid", 0)
            service.handle_power_setting("battery", 41)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, events.qsize())
        self.assertEqual([], view.snapshots)
        events.get_nowait()()
        self.assertEqual([main_thread], view.thread_ids)
        self.assertEqual("dc", view.snapshots[-1]["power_source"])
        self.assertIs(view.snapshots[-1]["lid_open"], False)
        self.assertEqual(41, view.snapshots[-1]["battery_percent"])

    def test_refresh_drain_clears_pending_before_view_read_so_bursts_keep_follow_up(self) -> None:
        service = _service()
        events: queue.Queue = queue.Queue()

        class _ReentrantRefreshView(_RefreshView):
            def refresh(self) -> None:
                super().refresh()
                if len(self.snapshots) == 1:
                    service.handle_power_setting("battery", 88)

        view = _ReentrantRefreshView(service)
        ui = _bare_ui(service, events, view)
        ui._attach_lid_power_status_callback()

        service.handle_power_setting("acdc", 0)
        events.get_nowait()()
        self.assertEqual(1, events.qsize())
        events.get_nowait()()
        self.assertEqual(88, view.snapshots[-1]["battery_percent"])

    def test_queue_failure_restores_pending_and_power_tab_reselection_refreshes_synchronously(self) -> None:
        service = _service()
        view = _RefreshView(service)
        ui = _bare_ui(service, _FailingQueue(), view)
        ui._attach_lid_power_status_callback()

        service.handle_power_setting("acdc", 1)

        self.assertFalse(ui._power_status_refresh_pending)
        self.assertEqual([], view.snapshots)
        ui._notebook = _Notebook()
        ui._tab_dashboard = None
        ui._tab_startup = None
        ui._tab_kakao = None
        ui._tab_wrike = None
        ui._tab_ai_usage = None
        ui._tab_codex = None
        ui._tab_update = None
        ui._tab_power = "tab-power"
        ui._power_built = True
        ui._current_tab = ui._TAB_POWER
        ui._apply_tab_geometry = lambda _tab: None
        ui._save_last_tab = lambda _tab: None

        ui._ensure_selected_tab_built()

        self.assertEqual("dc", view.snapshots[-1]["power_source"])


@unittest.skipUnless(os.name == "nt", "requires Windows Tk integration")
class LidPowerTkIntegrationTest(unittest.TestCase):
    def test_event_to_visible_label_refresh_is_within_300ms_when_tk_is_available(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"Tk is unavailable in this environment: {exc}")
        try:
            root.withdraw()
            service = _service()
            events: queue.SimpleQueue = queue.SimpleQueue()
            frame = tk.Frame(root)
            frame.pack()
            view = LidPowerSettingsView(root, service)
            view.mount(frame)
            ui = _bare_ui(service, events, view)
            ui._attach_lid_power_status_callback()
            SharedUiEventPump(root=root, event_queue=events).start()

            started = time.monotonic()
            thread = threading.Thread(
                target=lambda: service.handle_power_setting("acdc", 1)
            )
            thread.start()
            thread.join(0.1)
            deadline = started + 0.3
            while time.monotonic() < deadline:
                root.update()
                if "전원: dc" in str(view._status_label.cget("text")):
                    break
                time.sleep(0.005)
            elapsed = time.monotonic() - started
            self.assertIn("전원: dc", str(view._status_label.cget("text")))
            self.assertLessEqual(elapsed, 0.3)
        finally:
            root.destroy()


class _MainRoot:
    def iconbitmap(self, _path) -> None:
        return None

    def withdraw(self) -> None:
        return None

    def mainloop(self) -> None:
        return None

    def quit(self) -> None:
        return None

    def destroy(self) -> None:
        return None

    def after(self, _delay, _callback):
        return "after"


class _MainLib:
    def __init__(self, root) -> None:
        self.tk = type("TkModule", (), {"Tk": lambda _self: root})()
        self.keyboard = type("Keyboard", (), {"unhook_all": lambda _self: None})()


class _MainPolicy:
    is_supported = True

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self) -> None:
        self.events.append("policy.start")

    def set_status_changed_callback(self, callback) -> None:
        self.events.append("policy.detach" if callback is None else "policy.attach")

    def handle_power_setting(self, *_args) -> None:
        return None

    def on_resume(self) -> None:
        return None

    def notification_failure(self, _message) -> None:
        return None

    def shutdown(self) -> None:
        self.events.append("policy.shutdown")


class _MainTray:
    def __init__(self, events: list[str], **_kwargs) -> None:
        self.events = events

    def start(self, timeout=0) -> TrayReady:
        self.events.append("tray.start")
        return TrayReady(hwnd=707, class_name="WindowsSupporterTray_707")

    def stop(self) -> None:
        self.events.append("tray.stop")

    def is_ready(self) -> bool:
        return True


class _MainUi:
    def __init__(self, *_args, **kwargs) -> None:
        policy = kwargs.get("lid_power_policy")
        if policy is not None:
            policy.set_status_changed_callback(lambda: None)

    def show(self) -> None:
        return None


class LidPowerTeardownUnitTest(unittest.TestCase):
    def test_main_detaches_listener_before_tray_stop_and_policy_shutdown(self) -> None:
        events: list[str] = []
        root = _MainRoot()
        policy = _MainPolicy(events)
        tray = _MainTray
        monitor = type(
            "Monitor",
            (),
            {
                "attach": lambda _self, *_args: None,
                "shutdown": lambda _self: None,
                "on_session_unlock": lambda _self: None,
                "on_display_topology_changed": lambda _self, _reason: None,
            },
        )()
        startup = type(
            "Startup",
            (),
            {
                "get_enabled_state": lambda _self: True,
                "shutdown": lambda _self: None,
                "start": lambda _self, _root: None,
                "rescan_defaults_merge": lambda _self: None,
                "toggle_enabled": lambda _self: None,
                "open_config_file": lambda _self: None,
                "open_config_dir": lambda _self: None,
                "open_log_file": lambda _self: None,
            },
        )()
        updater = type("Updater", (), {"__init__": lambda _self, **_kwargs: None, "start": lambda _self: None})
        pump = type("Pump", (), {"__init__": lambda _self, **_kwargs: None, "start": lambda _self: None})
        thread = type("Thread", (), {"__init__": lambda _self, **_kwargs: None, "start": lambda _self: None})
        lock = type("Lock", (), {"close": lambda _self: None})()

        with (
            patch("main.run_update_handoff_from_argv", return_value=False),
            patch("main._acquire_single_instance_lock", return_value=lock),
            patch("main.start_update_handoff_cleanup_thread"),
            patch("main.LibConnector", return_value=_MainLib(root)),
            patch("main.StartReg"),
            patch("main.threading.Thread", thread),
            patch("main.Monitor", return_value=monitor),
            patch("main.StartupAppManager", return_value=startup),
            patch("main.LidPowerPolicyService.create_default", return_value=policy),
            patch("main.WindowsSupporterUpdater", updater),
            patch("main.WindowsSupporterMainUI", _MainUi),
            patch("main.SharedUiEventPump", pump),
            patch("main.SystemTrayIcon", lambda **kwargs: tray(events, **kwargs)),
            patch("main.signal.signal"),
        ):
            main.main()

        self.assertLess(events.index("policy.attach"), events.index("policy.start"))
        self.assertLess(events.index("policy.detach"), events.index("tray.stop"))
        self.assertLess(events.index("tray.stop"), events.index("policy.shutdown"))


if __name__ == "__main__":
    unittest.main()
