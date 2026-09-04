from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.utils.app_version import get_app_version


SCHEMA_VERSION = 1
PROBE_PATH_ENV = "WINDOWS_SUPPORTER_RUNTIME_PROBE_PATH"
PROBE_TOKEN_ENV = "WINDOWS_SUPPORTER_RUNTIME_PROBE_TOKEN"
HEARTBEAT_INTERVAL_MS = 5_000
DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 3

_SPECIAL_MODE_ARGS = frozenset(
    {
        "--google-calendar-resource-smoke",
        "--codex-usage-worker-smoke",
        "--windows-supporter-update-handoff",
        "--lid-power-watchdog",
        "--lid-power-runtime-canary",
        "--multiprocessing-fork",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def is_main_runtime_invocation(argv: Sequence[str] | None = None) -> bool:
    values = tuple(str(value) for value in (sys.argv if argv is None else argv))
    return not any(value in _SPECIAL_MODE_ARGS for value in values[1:])


class RuntimeProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeReadinessV1:
    schema_version: int
    token: str
    state: str
    pid: int
    process_start_time: str
    executable_path: str
    version: str
    commit: str
    tray_hwnd: int
    mainloop_tick: int
    timestamp: str
    error: str | None


class NullRuntimeLifecycle:
    active = False

    def install_exception_hooks(self) -> None:
        return

    def install_tk_exception_hook(self, _root: Any) -> None:
        return

    def mark_process_start(self) -> None:
        return

    def mark_mutex_acquired(self) -> None:
        return

    def mark_already_running(self) -> None:
        return

    def mark_tk_ready(self) -> None:
        return

    def mark_components_ready(self) -> None:
        return

    def mark_tray_ready(self, _tray_hwnd: int) -> None:
        return

    def bind_mainloop(self, _root: Any, *, tray_hwnd: int) -> None:
        return

    def mark_stopping(self, _reason: str = "normal") -> None:
        return

    def mark_normal_stop(self, _reason: str = "normal") -> None:
        return

    def record_exception(
        self,
        _origin: str,
        _exc_type: type[BaseException],
        _exc_value: BaseException,
        _exc_traceback: Any,
    ) -> None:
        return

    def emit(self, _phase: str, **_details: Any) -> bool:
        return False


class RuntimeLifecycle:
    active = True

    def __init__(
        self,
        *,
        base_dir: Path,
        environ: Mapping[str, str],
        pid: int,
        executable_path: str,
        process_start_time: str,
        file_version: str,
        commit: str,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
        log_backups: int = DEFAULT_LOG_BACKUPS,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.log_path = self.base_dir / "windows-supporter" / "runtime.jsonl"
        self.pid = int(pid)
        self.executable_path = os.path.abspath(str(executable_path))
        self.process_start_time = str(process_start_time)
        self.file_version = str(file_version or "dev")
        self.commit = str(commit or "")
        self.max_log_bytes = max(1, int(max_log_bytes))
        self.log_backups = max(0, int(log_backups))
        probe_path = str(environ.get(PROBE_PATH_ENV, "") or "").strip()
        probe_token = str(environ.get(PROBE_TOKEN_ENV, "") or "").strip()
        if bool(probe_path) != bool(probe_token):
            raise RuntimeProbeError(
                f"{PROBE_PATH_ENV} and {PROBE_TOKEN_ENV} must be supplied together"
            )
        self.probe_path = Path(probe_path) if probe_path else None
        self.probe_token = probe_token
        self._lock = threading.RLock()
        self._state = "starting"
        self._tray_hwnd = 0
        self._mainloop_tick = 0
        self._failed = False
        self._stopping = False
        self._hooks_installed = False
        return

    @classmethod
    def from_current_process(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> RuntimeLifecycle:
        env = dict(os.environ if environ is None else environ)
        base = env.get("LOCALAPPDATA") or env.get("APPDATA") or str(Path.home())
        version = get_app_version()
        return cls(
            base_dir=Path(base),
            environ=env,
            pid=os.getpid(),
            executable_path=sys.executable,
            process_start_time=_utc_now(),
            file_version=version.numeric_version,
            commit=version.commit,
        )

    def _base_record(self, phase: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "timestamp": _utc_now(),
            "phase": str(phase),
            "pid": self.pid,
            "executable_path": self.executable_path,
            "file_version": self.file_version,
            "commit": self.commit,
        }

    def _rotate_log_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_size = self.log_path.stat().st_size
        except FileNotFoundError:
            return
        if current_size + incoming_bytes <= self.max_log_bytes:
            return
        if self.log_backups <= 0:
            self.log_path.unlink(missing_ok=True)
            return
        for index in range(self.log_backups, 1, -1):
            older = Path(f"{self.log_path}.{index - 1}")
            newer = Path(f"{self.log_path}.{index}")
            if older.exists():
                os.replace(older, newer)
        os.replace(self.log_path, Path(f"{self.log_path}.1"))

    def emit(self, phase: str, **details: Any) -> bool:
        record = self._base_record(phase)
        record.update(details)
        encoded = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        try:
            with self._lock:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_log_if_needed(len(encoded))
                with self.log_path.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            return True
        except OSError:
            return False

    def _probe_payload(self, *, error: str | None = None) -> dict[str, Any]:
        return asdict(
            RuntimeReadinessV1(
                schema_version=SCHEMA_VERSION,
                token=self.probe_token,
                state=self._state,
                pid=self.pid,
                process_start_time=self.process_start_time,
                executable_path=self.executable_path,
                version=self.file_version,
                commit=self.commit,
                tray_hwnd=self._tray_hwnd,
                mainloop_tick=self._mainloop_tick,
                timestamp=_utc_now(),
                error=error,
            )
        )

    def _write_probe(self, *, error: str | None = None) -> None:
        if self.probe_path is None:
            return
        payload = self._probe_payload(error=error)
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        temp_path = self.probe_path.with_name(
            f".{self.probe_path.name}.{self.pid}.{threading.get_ident()}.tmp"
        )
        try:
            self.probe_path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.probe_path)
        except OSError as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeProbeError(f"runtime probe write failed: {exc}") from exc

    def mark_process_start(self) -> None:
        with self._lock:
            self._state = "starting"
            self.emit("process_start")
            self._write_probe()

    def mark_mutex_acquired(self) -> None:
        self.emit("mutex_acquired")

    def mark_already_running(self) -> None:
        self.emit("already_running")

    def mark_tk_ready(self) -> None:
        self.emit("tk_ready")

    def mark_components_ready(self) -> None:
        self.emit("components_ready")

    def mark_tray_ready(self, tray_hwnd: int) -> None:
        with self._lock:
            self._tray_hwnd = int(tray_hwnd)
            self.emit("tray_ready", tray_hwnd=self._tray_hwnd)

    def bind_mainloop(self, root: Any, *, tray_hwnd: int) -> None:
        self._tray_hwnd = int(tray_hwnd)

        def heartbeat() -> None:
            with self._lock:
                if self._stopping or self._failed:
                    return
                self._state = "ready"
                self._mainloop_tick += 1
                if self._mainloop_tick == 1:
                    self.emit(
                        "mainloop_ready",
                        tray_hwnd=self._tray_hwnd,
                        mainloop_tick=self._mainloop_tick,
                    )
                self._write_probe()
            root.after(HEARTBEAT_INTERVAL_MS, heartbeat)

        root.after(0, heartbeat)

    def mark_stopping(self, reason: str = "normal") -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._state = "stopping"
            self.emit("stopping", reason=str(reason))
            self._write_probe()

    def mark_normal_stop(self, reason: str = "normal") -> None:
        self.emit("normal_stop", reason=str(reason))

    def record_exception(
        self,
        origin: str,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: Any,
    ) -> None:
        formatted = "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback)
        )
        error_text = f"{getattr(exc_type, '__name__', str(exc_type))}: {exc_value}"
        with self._lock:
            if self._failed:
                return
            self._failed = True
            self._state = "failed"
            self.emit(
                "fatal_exception",
                origin=str(origin),
                error={
                    "type": getattr(exc_type, "__name__", str(exc_type)),
                    "message": str(exc_value),
                    "traceback": formatted,
                },
            )
            try:
                self._write_probe(error=error_text)
            except RuntimeProbeError:
                pass

    def install_exception_hooks(self) -> None:
        if self._hooks_installed:
            return
        self._hooks_installed = True
        previous_sys_hook = sys.excepthook
        previous_thread_hook = threading.excepthook

        def sys_hook(exc_type, exc_value, exc_traceback) -> None:
            self.record_exception("sys", exc_type, exc_value, exc_traceback)
            previous_sys_hook(exc_type, exc_value, exc_traceback)

        def thread_hook(args: Any) -> None:
            self.record_exception(
                f"thread:{getattr(getattr(args, 'thread', None), 'name', 'unknown')}",
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
            )
            previous_thread_hook(args)

        sys.excepthook = sys_hook
        threading.excepthook = thread_hook

    def install_tk_exception_hook(self, root: Any) -> None:
        previous = getattr(root, "report_callback_exception", None)

        def report_callback_exception(exc_type, exc_value, exc_traceback) -> None:
            self.record_exception("tk_callback", exc_type, exc_value, exc_traceback)
            if callable(previous):
                previous(exc_type, exc_value, exc_traceback)

        root.report_callback_exception = report_callback_exception
