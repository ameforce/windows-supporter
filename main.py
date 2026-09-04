import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

import ctypes
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence

from src.utils.runtime_lifecycle import (
    NullRuntimeLifecycle,
    RuntimeLifecycle,
    is_main_runtime_invocation,
)


_RUNTIME_LIFECYCLE = (
    RuntimeLifecycle.from_current_process()
    if __name__ == "__main__" and is_main_runtime_invocation(sys.argv)
    else NullRuntimeLifecycle()
)
if _RUNTIME_LIFECYCLE.active:
    _RUNTIME_LIFECYCLE.install_exception_hooks()
    _RUNTIME_LIFECYCLE.mark_process_start()

from src.utils.LibConnector import LibConnector
from src.apps.Monitor import Monitor
from src.apps.main_ui import WindowsSupporterMainUI
from src.apps.lid_power_policy import (
    LidPowerPolicyService,
    run_lid_power_special_mode,
)
from src.utils.StartReg import StartReg
from src.apps.startup_apps import StartupAppManager
from src.apps.codex_usage_playwright_process import run_process_boundary_smoke
from src.apps.google_calendar_oauth import (
    GoogleCalendarSuccess,
    load_bundled_desktop_client_config,
)
from src.utils.tray_icon import SystemTrayIcon
from src.utils.ui_event_pump import SharedUiEventPump
from src.utils.update_monitor import (
    WindowsSupporterUpdater,
    run_update_handoff_from_argv,
    start_update_handoff_cleanup_thread,
)


_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_SINGLE_INSTANCE_MUTEX_NAME = "Local\\windows-supporter-main-instance"
_CODEX_TIMEOUT_RECOVERY_RESTART_ENV = "WINDOWS_SUPPORTER_CODEX_TIMEOUT_RECOVERY_RESTART_AT"
_CODEX_TIMEOUT_RECOVERY_RESTART_COOLDOWN_SEC = 900.0


class _NoopSingleInstanceLock:
    def close(self) -> None:
        return


class _SingleInstanceLock:
    def __init__(self, kernel32, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle = int(handle)

    def close(self) -> None:
        handle = self._handle
        if handle <= 0:
            return
        self._handle = 0
        try:
            self._kernel32.ReleaseMutex(handle)
        except Exception:
            pass
        try:
            self._kernel32.CloseHandle(handle)
        except Exception:
            pass


class _Pywin32SingleInstanceLock:
    def __init__(self, win32api_module, win32event_module, handle) -> None:
        self._win32api = win32api_module
        self._win32event = win32event_module
        self._handle = handle

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            self._win32event.ReleaseMutex(handle)
        except Exception:
            pass
        try:
            self._win32api.CloseHandle(handle)
        except Exception:
            pass


def _acquire_single_instance_lock():
    if os.name != "nt":
        return _NoopSingleInstanceLock()
    try:
        import win32api
        import win32event

        handle = win32event.CreateMutex(
            None,
            True,
            _SINGLE_INSTANCE_MUTEX_NAME,
        )
        last_error = int(win32api.GetLastError())
        if last_error in {_ERROR_ALREADY_EXISTS, _ERROR_ACCESS_DENIED}:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass
            return None
        return _Pywin32SingleInstanceLock(win32api, win32event, handle)
    except Exception:
        pass

    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.GetLastError.restype = ctypes.c_ulong
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, True, _SINGLE_INSTANCE_MUTEX_NAME)
        last_error = int(kernel32.GetLastError())
    except Exception:
        return _NoopSingleInstanceLock()

    if last_error in {_ERROR_ALREADY_EXISTS, _ERROR_ACCESS_DENIED}:
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        return None
    if not handle:
        return _NoopSingleInstanceLock()
    return _SingleInstanceLock(kernel32, int(handle))


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


def _claim_codex_timeout_recovery_restart(
    *,
    environ: dict[str, str] | None = None,
    now: float | None = None,
    cooldown_sec: float = _CODEX_TIMEOUT_RECOVERY_RESTART_COOLDOWN_SEC,
) -> bool:
    env = os.environ if environ is None else environ
    current = float(time.time() if now is None else now)
    try:
        previous = float(env.get(_CODEX_TIMEOUT_RECOVERY_RESTART_ENV, "") or 0.0)
    except (TypeError, ValueError):
        previous = 0.0
    if previous > 0.0 and current - previous < max(0.0, float(cooldown_sec)):
        return False
    env[_CODEX_TIMEOUT_RECOVERY_RESTART_ENV] = f"{current:.6f}"
    return True


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


def _build_update_repo_root(
    *,
    executable: str | None = None,
    main_file: str | None = None,
    frozen: bool | None = None,
) -> str:
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    if is_frozen:
        exe_dir = os.path.dirname(os.path.abspath(executable or sys.executable))
        if exe_dir:
            return exe_dir
    return os.path.dirname(os.path.abspath(main_file or __file__))


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


def _run_google_calendar_resource_smoke() -> int:
    result = load_bundled_desktop_client_config()
    return 0 if isinstance(result, GoogleCalendarSuccess) else 1


def main() -> None:
    if "--google-calendar-resource-smoke" in sys.argv:
        raise SystemExit(_run_google_calendar_resource_smoke())
    if "--codex-usage-worker-smoke" in sys.argv:
        raise SystemExit(run_process_boundary_smoke())
    if run_update_handoff_from_argv(sys.argv):
        return
    single_instance_lock = _acquire_single_instance_lock()
    if single_instance_lock is None:
        _RUNTIME_LIFECYCLE.mark_already_running()
        _RUNTIME_LIFECYCLE.mark_stopping("already_running")
        return
    _RUNTIME_LIFECYCLE.mark_mutex_acquired()
    try:
        _run_main_app(_RUNTIME_LIFECYCLE)
        _RUNTIME_LIFECYCLE.mark_normal_stop()
    except BaseException as exc:
        _RUNTIME_LIFECYCLE.record_exception(
            "main",
            type(exc),
            exc,
            exc.__traceback__,
        )
        raise
    finally:
        single_instance_lock.close()


def _run_main_app(lifecycle=None) -> None:
    lifecycle = lifecycle or _RUNTIME_LIFECYCLE
    try:
        start_update_handoff_cleanup_thread(current_executable=sys.executable)
    except Exception:
        pass

    lib = LibConnector()
    try:
        threading.Thread(target=StartReg().add_to_startup, daemon=True).start()
    except Exception:
        pass
    root = lib.tk.Tk()
    lifecycle.install_tk_exception_hook(root)
    lifecycle.mark_tk_ready()
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
    try:
        root.withdraw()
    except Exception:
        pass
    event_queue: queue.SimpleQueue = queue.SimpleQueue()
    restart_requested = False

    def _request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        try:
            root.quit()
        except Exception:
            pass
        return

    def _request_codex_timeout_recovery_restart() -> bool:
        if not _claim_codex_timeout_recovery_restart():
            return False
        try:
            event_queue.put(_request_restart)
        except Exception:
            return False
        return True

    monitor = Monitor(
        codex_timeout_recovery_handler=_request_codex_timeout_recovery_restart,
    )
    startup_manager = StartupAppManager()
    try:
        lid_power_policy = LidPowerPolicyService.create_default(
            exclusive_instance=True,
        )
    except Exception:
        lid_power_policy = None
    updater = WindowsSupporterUpdater(
        root=root,
        event_queue=event_queue,
        repo_root=_build_update_repo_root(),
        quit_callback=root.quit,
        exit_callback=lambda: os._exit(0),
    )
    main_ui = WindowsSupporterMainUI(
        root,
        startup_manager,
        monitor,
        event_queue=event_queue,
        updater=updater,
        lid_power_policy=lid_power_policy,
    )
    try:
        setattr(root, "_ws_main_ui", main_ui)
    except Exception:
        pass
    try:
        if lid_power_policy is not None:
            lid_power_policy.start()
    except Exception:
        pass
    try:
        monitor.attach(root, event_queue)
    except Exception:
        pass
    SharedUiEventPump(root=root, event_queue=event_queue).start()
    try:
        updater.start()
    except Exception:
        pass
    lifecycle.mark_components_ready()

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

    tray = SystemTrayIcon(
        tooltip="Windows Supporter",
        on_open_settings=lambda: event_queue.put(main_ui.show),
        on_apply=lambda: event_queue.put(_start_startup_apps_bg),
        on_rescan=lambda: event_queue.put(_rescan_and_start_bg),
        on_open_config=lambda: event_queue.put(startup_manager.open_config_file),
        on_open_config_dir=lambda: event_queue.put(startup_manager.open_config_dir),
        on_toggle_enabled=lambda: event_queue.put(_toggle_and_start_bg),
        is_enabled=startup_manager.get_enabled_state,
        on_open_worktime=lambda: getattr(
            monitor,
            "show_worktime_quick_panel",
            lambda: None,
        )(),
        on_open_kakao_monitor=lambda: event_queue.put(main_ui.show),
        on_open_log=lambda: event_queue.put(startup_manager.open_log_file),
        on_restart=lambda: event_queue.put(_request_restart),
        on_exit=lambda: event_queue.put(root.quit),
        on_session_unlock=_on_session_unlock,
        on_display_topology_change=_on_display_topology_change,
        on_power_setting_change=(
            lid_power_policy.handle_power_setting
            if lid_power_policy is not None
            else None
        ),
        on_power_resume=(
            lid_power_policy.on_resume if lid_power_policy is not None else None
        ),
        on_power_notification_failure=(
            lid_power_policy.notification_failure
            if lid_power_policy is not None
            else None
        ),
        power_notifications_enabled=bool(
            lid_power_policy is not None and lid_power_policy.is_supported
        ),
    )
    tray_ready = tray.start(timeout=15.0)
    lifecycle.mark_tray_ready(tray_ready.hwnd)

    def _on_sigint(signum, frame) -> None:
        try:
            root.quit()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:
        pass

    lifecycle.bind_mainloop(
        root,
        tray_hwnd=tray_ready.hwnd,
        readiness_check=tray.is_ready,
    )

    try:
        root.mainloop()
    finally:
        lifecycle.mark_stopping("mainloop_exit")
        try:
            monitor.shutdown()
        except Exception:
            pass
        try:
            setter = getattr(lid_power_policy, "set_status_changed_callback", None)
            if callable(setter):
                setter(None)
        except Exception:
            pass
        try:
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        try:
            if lid_power_policy is not None:
                lid_power_policy.shutdown()
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
    special_exit = run_lid_power_special_mode(sys.argv[1:])
    if special_exit is None:
        main()
    else:
        raise SystemExit(int(special_exit))
