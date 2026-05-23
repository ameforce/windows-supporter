import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence

from src.utils.LibConnector import LibConnector
from src.apps.Monitor import Monitor
from src.apps.main_ui import WindowsSupporterMainUI
from src.utils.StartReg import StartReg
from src.apps.startup_apps import StartupAppManager
from src.utils.tray_icon import SystemTrayIcon
from src.utils.ui_event_pump import SharedUiEventPump


def _build_restart_command(
    *,
    executable: str | None = None,
    argv: Sequence[str] | None = None,
    frozen: bool | None = None,
    main_file: str | None = None,
) -> list[str]:
    resolved_executable = executable or sys.executable
    resolved_argv = list(sys.argv if argv is None else argv)
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    extra_args = resolved_argv[1:]
    if is_frozen:
        return [resolved_executable, *extra_args]

    script = resolved_argv[0] if resolved_argv else None
    if not script or str(script).startswith("-"):
        script = main_file or __file__
    return [resolved_executable, os.path.abspath(str(script)), *extra_args]


def _build_restart_environment(
    *,
    environ: Mapping[str, str] | None = None,
    frozen: bool | None = None,
) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    if is_frozen:
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _build_restart_cwd(
    *,
    executable: str | None = None,
    current_cwd: str | None = None,
    frozen: bool | None = None,
) -> str:
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    if is_frozen:
        exe_dir = os.path.dirname(os.path.abspath(executable or sys.executable))
        if exe_dir:
            return exe_dir
    return os.path.abspath(current_cwd or os.getcwd())


def _restart_current_process() -> None:
    command = _build_restart_command()
    env = _build_restart_environment()
    cwd = _build_restart_cwd()
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)

    subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        close_fds=True,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    return


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
    main_ui = WindowsSupporterMainUI(root, startup_manager, monitor, event_queue=event_queue)
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

    def _on_display_topology_change(reason: str) -> None:
        try:
            event_queue.put(lambda: monitor.on_display_topology_changed(reason))
        except Exception:
            pass

    restart_requested = False

    def _request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        try:
            root.quit()
        except Exception:
            pass
        return

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
        on_restart=lambda: event_queue.put(_request_restart),
        on_exit=lambda: event_queue.put(root.quit),
        on_session_unlock=_on_session_unlock,
        on_display_topology_change=_on_display_topology_change,
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
    if restart_requested:
        _restart_current_process()
    return


if __name__ == "__main__":
    main()
