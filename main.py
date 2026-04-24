import os
import queue
import signal
import sys
import threading

from src.utils.LibConnector import LibConnector
from src.apps.Monitor import Monitor
from src.apps.main_ui import WindowsSupporterMainUI
from src.utils.StartReg import StartReg
from src.apps.startup_apps import StartupAppManager
from src.utils.tray_icon import SystemTrayIcon
from src.utils.ui_event_pump import SharedUiEventPump


def main() -> None:
    lib = LibConnector()
    try:
        threading.Thread(target=StartReg().add_to_startup, daemon=True).start()
    except Exception:
        pass
    root = lib.tk.Tk()
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base_dir, "src", "utils", "windows_supporter.ico"),
        ]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(
                os.path.join(meipass, "src", "utils", "windows_supporter.ico")
            )
        for p in candidates:
            if os.path.isfile(p):
                try:
                    root.iconbitmap(p)
                except Exception:
                    pass
                break
    except Exception:
        pass
    root.withdraw()
    monitor = Monitor()
    event_queue: queue.SimpleQueue = queue.SimpleQueue()
    startup_manager = StartupAppManager()
    main_ui = WindowsSupporterMainUI(root, startup_manager, monitor)
    try:
        setattr(root, "_ws_main_ui", main_ui)
    except Exception:
        pass
    try:
        monitor.attach(root, event_queue)
    except Exception:
        pass
    SharedUiEventPump(root=root, event_queue=event_queue).start()

    def _run_bg(fn) -> None:
        try:
            threading.Thread(target=fn, daemon=True).start()
        except Exception:
            pass

    def _start_startup_apps_bg() -> None:
        _run_bg(lambda: startup_manager.start(root))

    def _rescan_and_start_bg() -> None:
        def task() -> None:
            startup_manager.rescan_defaults_merge()
            startup_manager.start(root)

        _run_bg(task)

    def _toggle_and_start_bg() -> None:
        def task() -> None:
            startup_manager.toggle_enabled()
            startup_manager.start(root)

        _run_bg(task)

    def _on_session_unlock() -> None:
        try:
            event_queue.put(monitor.on_session_unlock)
        except Exception:
            pass

    try:
        root.after(120, lambda: event_queue.put(_start_startup_apps_bg))
    except Exception:
        pass

    tray = SystemTrayIcon(
        tooltip="Windows Supporter",
        on_open_settings=lambda: event_queue.put(main_ui.show_startup_apps),
        on_apply=lambda: event_queue.put(_start_startup_apps_bg),
        on_rescan=lambda: event_queue.put(_rescan_and_start_bg),
        on_open_config=lambda: event_queue.put(startup_manager.open_config_file),
        on_open_config_dir=lambda: event_queue.put(startup_manager.open_config_dir),
        on_toggle_enabled=lambda: event_queue.put(_toggle_and_start_bg),
        is_enabled=startup_manager.get_enabled_state,
        on_open_kakao_monitor=lambda: event_queue.put(main_ui.show_kakao_monitor),
        on_open_log=lambda: event_queue.put(startup_manager.open_log_file),
        on_exit=lambda: event_queue.put(root.quit),
        on_session_unlock=_on_session_unlock,
    )
    try:
        tray.start()
    except Exception:
        tray = None

    def _on_sigint(signum, frame) -> None:
        try:
            root.quit()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:
        pass

    try:
        root.mainloop()
    finally:
        try:
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        try:
            startup_manager.shutdown()
        except Exception:
            pass
        try:
            lib.keyboard.unhook_all()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
    return


if __name__ == "__main__":
    main()
