from __future__ import annotations

import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any, final

from src.apps.codex_usage_browser_types import (
    BrowserErrorCode,
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    LogSink,
    PlaywrightSessionConfig,
)
from src.apps.codex_usage_playwright_worker import run_playwright_worker
from src.apps.codex_usage_process_boundary import (
    OwnedProcessMemorySample,
    WindowsJobBoundary,
    owned_process_memory_sample,
)


WorkerTarget = Callable[[Connection], None]
RssSampler = Callable[[int], int | OwnedProcessMemorySample]


def _run_boundary_smoke_worker(connection: Connection) -> None:
    try:
        if connection.recv() != "contained":
            return
        connection.send(("ready", os.getpid()))
        connection.recv()
    except (EOFError, OSError):
        return


def run_process_boundary_smoke(timeout_sec: float = 15.0) -> int:
    """Exercise frozen/source spawn plus Job containment without launching Chrome."""

    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=True)
    job = WindowsJobBoundary()
    process = context.Process(
        target=_run_boundary_smoke_worker,
        args=(child_connection,),
        name="CodexUsageBoundarySmokeWorker",
        daemon=False,
    )
    try:
        process.start()
        child_connection.close()
        job.assign_process(process.sentinel)
        parent_connection.send("contained")
        if not parent_connection.poll(max(0.1, float(timeout_sec))):
            return 2
        message = parent_connection.recv()
        if not isinstance(message, tuple) or message[0] != "ready":
            return 3
        job.terminate()
        process.join(max(0.1, float(timeout_sec)))
        empty = job.wait_empty(1.0)
        return 0 if not process.is_alive() and empty else 4
    except BaseException:
        return 5
    finally:
        try:
            if process.is_alive():
                job.terminate()
                process.join(1.0)
        except BaseException:
            pass
        try:
            parent_connection.close()
        except BaseException:
            pass
        try:
            child_connection.close()
        except BaseException:
            pass
        try:
            job.close()
        except BaseException:
            pass


def select_worker_recycle_reason(
    config: PlaywrightSessionConfig,
    *,
    successful_collects: int,
    age_sec: float,
    max_process_rss_bytes: int,
    status: BrowserRuntimeStatus,
) -> str | None:
    """Return the first planned recycle guard reached by a headless worker."""

    if status.login_window_open or status.state == BrowserState.HEADED_LOGIN:
        if age_sec >= max(
            1.0, float(config.headed_login_recycle_max_age_sec)
        ):
            return "headed_login_max_age"
        if max_process_rss_bytes >= max(
            1, int(config.headed_login_emergency_max_process_rss_bytes)
        ):
            return "headed_login_emergency_rss"
        return None
    if successful_collects >= max(1, int(config.worker_recycle_success_count)):
        return "success_count"
    if age_sec >= max(1.0, float(config.worker_recycle_max_age_sec)):
        return "max_age"
    if max_process_rss_bytes >= max(
        1, int(config.worker_recycle_max_process_rss_bytes)
    ):
        return "max_process_rss"
    return None


@final
class CodexUsagePlaywrightProcessDriver:
    """Runs the synchronous Playwright driver inside a killable Windows worker."""

    def __init__(
        self,
        config: PlaywrightSessionConfig,
        log_sink: LogSink | None = None,
        *,
        worker_target: WorkerTarget = run_playwright_worker,
        process_context: Any | None = None,
        clock: Callable[[], float] | None = None,
        rss_sampler: RssSampler | None = None,
    ) -> None:
        self._config = config
        self._log_sink = log_sink
        self._worker_target = worker_target
        self._context = process_context or multiprocessing.get_context("spawn")
        self._clock = clock or time.monotonic
        self._rss_sampler = rss_sampler or owned_process_memory_sample
        self._invoke_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._termination_lock = threading.RLock()
        self._process: Any | None = None
        self._connection: Connection | None = None
        self._job: WindowsJobBoundary | None = None
        self._worker_pid: int = 0
        self._worker_started_at: float = 0.0
        self._process_generation: int = 0
        self._request_id: int = 0
        self._successful_collects: int = 0
        self._last_failure_signal: str = ""
        self._session_cookies: list[dict[str, Any]] = []
        self._boundary_failed: bool = False
        self._shutdown: bool = False
        self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    @property
    def process_generation(self) -> int:
        return self._process_generation

    @property
    def worker_pid(self) -> int:
        return self._worker_pid

    def start(self) -> BrowserOperationResult:
        if self._shutdown:
            return BrowserOperationResult(error=BrowserErrorCode.COLLECT_FAILED.value)
        return self._invoke("start")

    def collect(self) -> BrowserOperationResult:
        if self._shutdown:
            return BrowserOperationResult(error=BrowserErrorCode.COLLECT_FAILED.value)
        with self._invoke_lock:
            self._recycle_worker_if_needed()
            result = self._invoke_locked("collect")
            if result.error is None and result.probe is not None:
                self._successful_collects += 1
            if result.error == BrowserErrorCode.RENDERER_CRASHED.value:
                self.force_terminate(BrowserErrorCode.RENDERER_CRASHED.value)
            return result

    def open_login(self) -> BrowserOperationResult:
        if self._shutdown:
            return BrowserOperationResult(error=BrowserErrorCode.COLLECT_FAILED.value)
        return self._invoke("open_login")

    def poll_login(self) -> BrowserOperationResult:
        if self._shutdown:
            return BrowserOperationResult(error=BrowserErrorCode.COLLECT_FAILED.value)
        return self._invoke("poll_login")

    def close_session(self) -> None:
        with self._invoke_lock:
            if not self._has_live_worker():
                self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
                return
            result = self._invoke_locked(
                "close_session",
                timeout_sec=max(0.01, float(self._config.worker_cleanup_timeout_sec)),
                allow_spawn=False,
            )
            if result.error == BrowserErrorCode.COMMAND_TIMEOUT.value:
                self.force_terminate("context_close_timeout")

    def shutdown(self) -> None:
        self._shutdown = True
        with self._invoke_lock:
            self._graceful_stop("shutdown")
        self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return self._status

    def export_session_cookies(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [dict(cookie) for cookie in self._session_cookies]

    def import_session_cookies(self, cookies: list[dict[str, Any]]) -> None:
        with self._state_lock:
            self._session_cookies = [
                dict(cookie) for cookie in cookies if isinstance(cookie, dict)
            ]

    def force_terminate(self, reason: str) -> bool:
        effective_reason = (
            self._last_failure_signal
            if reason == BrowserErrorCode.COMMAND_TIMEOUT.value
            and self._last_failure_signal in {item.value for item in BrowserErrorCode}
            else reason
        )
        terminated = self._terminate_current(effective_reason)
        if terminated:
            error = (
                effective_reason
                if effective_reason in {item.value for item in BrowserErrorCode}
                else BrowserErrorCode.COMMAND_TIMEOUT.value
            )
            self._status = BrowserRuntimeStatus(BrowserState.RECOVERING, False, error)
        return terminated

    def _invoke(self, command: str) -> BrowserOperationResult:
        with self._invoke_lock:
            return self._invoke_locked(command)

    def _invoke_locked(
        self,
        command: str,
        *,
        timeout_sec: float | None = None,
        allow_spawn: bool = True,
    ) -> BrowserOperationResult:
        error = self._ensure_worker(allow_spawn=allow_spawn)
        if error is not None:
            if error in {"driver_shutdown", "profile_handoff_blocked"}:
                self._status = BrowserRuntimeStatus(
                    BrowserState.FAILED,
                    False,
                    BrowserErrorCode.COLLECT_FAILED.value,
                )
                return BrowserOperationResult(
                    error=BrowserErrorCode.COLLECT_FAILED.value
                )
            return self._transport_failure(error)
        with self._state_lock:
            connection = self._connection
            process = self._process
            generation = self._process_generation
            worker_pid = self._worker_pid
            self._request_id += 1
            request_id = self._request_id
        if connection is None or process is None:
            return self._transport_failure("worker_missing")
        try:
            connection.send(("command", request_id, command))
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._terminate_current("transport_send_failed")
            return self._transport_failure(type(exc).__name__)
        deadline = (
            None
            if timeout_sec is None
            else self._clock() + max(0.01, float(timeout_sec))
        )
        while True:
            try:
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0.0 or not connection.poll(remaining):
                        self._log(
                            "browser worker command timeout "
                            f"command={command} generation={generation} pid={worker_pid}"
                        )
                        return BrowserOperationResult(
                            error=BrowserErrorCode.COMMAND_TIMEOUT.value
                        )
                message = connection.recv()
            except (EOFError, OSError) as exc:
                self._terminate_current("transport_closed")
                return self._transport_failure(type(exc).__name__)
            if not isinstance(message, tuple) or not message:
                continue
            if message[0] == "log" and len(message) >= 2:
                worker_message = str(message[1])
                if "browser page crashed" in worker_message:
                    self._last_failure_signal = BrowserErrorCode.RENDERER_CRASHED.value
                self._log(
                    "browser worker event "
                    f"generation={generation} pid={worker_pid} {worker_message}"
                )
                continue
            if (
                message[0] == "result"
                and len(message) in {4, 5}
                and int(message[1]) == request_id
                and isinstance(message[2], BrowserOperationResult)
                and isinstance(message[3], BrowserRuntimeStatus)
            ):
                if len(message) == 5 and isinstance(message[4], list):
                    with self._state_lock:
                        self._session_cookies = [
                            dict(cookie)
                            for cookie in message[4]
                            if isinstance(cookie, dict)
                        ]
                self._status = message[3]
                return message[2]

    def _ensure_worker(self, *, allow_spawn: bool = True) -> str | None:
        with self._termination_lock:
            if self._has_live_worker():
                return None
            if self._shutdown or not allow_spawn:
                return "driver_shutdown"
            if self._boundary_failed:
                return "profile_handoff_blocked"
            with self._state_lock:
                stale_process = self._process
                stale_job = self._job
            if stale_process is not None or stale_job is not None:
                if not self._terminate_current("stale_before_spawn"):
                    return "profile_handoff_blocked"
            parent_connection, child_connection = self._context.Pipe(duplex=True)
            job = WindowsJobBoundary()
            process = self._context.Process(
                target=self._worker_target,
                args=(child_connection,),
                name="CodexUsagePlaywrightWorker",
                daemon=False,
            )
            try:
                process.start()
                child_connection.close()
                job.assign_process(process.sentinel)
            except BaseException as exc:
                try:
                    child_connection.close()
                except BaseException:
                    pass
                try:
                    if process.is_alive():
                        process.kill()
                        process.join(1.0)
                except BaseException:
                    pass
                parent_connection.close()
                job.close()
                self._log(
                    "browser worker containment failed "
                    f"type={type(exc).__name__}"
                )
                return "containment_failed"
            with self._state_lock:
                self._process_generation += 1
                generation = self._process_generation
                self._process = process
                self._connection = parent_connection
                self._job = job
                self._worker_pid = int(process.pid or 0)
                self._worker_started_at = self._clock()
                self._successful_collects = 0
                self._last_failure_signal = ""
        self._log(
            "browser worker spawned "
            f"generation={generation} pid={self._worker_pid}"
        )
        try:
            parent_connection.send(
                (
                    "bootstrap",
                    self._config,
                    generation,
                    [dict(cookie) for cookie in self._session_cookies],
                )
            )
            timeout = max(0.01, float(self._config.worker_bootstrap_timeout_sec))
            if not parent_connection.poll(timeout):
                self._terminate_current("bootstrap_timeout")
                return "bootstrap_timeout"
            message = parent_connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._terminate_current("bootstrap_transport_closed")
            return type(exc).__name__
        if (
            not isinstance(message, tuple)
            or len(message) < 3
            or message[0] != "ready"
            or int(message[2]) != generation
        ):
            self._terminate_current("bootstrap_invalid")
            return "bootstrap_invalid"
        self._worker_pid = int(message[1])
        self._log(
            "browser worker ready "
            f"generation={generation} pid={self._worker_pid}"
        )
        return None

    def _recycle_worker_if_needed(self) -> None:
        with self._state_lock:
            process = self._process
            worker_pid = self._worker_pid
            started_at = self._worker_started_at
            status = self._status
            successful_collects = self._successful_collects
        if process is None or not process.is_alive() or worker_pid <= 0:
            return
        age_sec = max(0.0, self._clock() - started_at)
        try:
            raw_memory = self._rss_sampler(worker_pid)
            if isinstance(raw_memory, OwnedProcessMemorySample):
                memory = raw_memory
            else:
                memory = OwnedProcessMemorySample(
                    max_rss_bytes=max(0, int(raw_memory))
                )
        except BaseException:
            memory = OwnedProcessMemorySample()
        rss_bytes = memory.max_rss_bytes
        reason = select_worker_recycle_reason(
            self._config,
            successful_collects=successful_collects,
            age_sec=age_sec,
            max_process_rss_bytes=rss_bytes,
            status=status,
        )
        if reason is None:
            return
        self._log(
            "browser worker recycle requested "
            f"reason={reason} generation={self._process_generation} pid={worker_pid} "
            f"successful_collects={successful_collects} age_sec={age_sec:.1f} "
            f"max_process_rss_bytes={rss_bytes} "
            f"total_process_rss_bytes={memory.total_rss_bytes} "
            f"max_process_private_bytes={memory.max_private_bytes} "
            f"total_process_private_bytes={memory.total_private_bytes}"
        )
        self._graceful_stop(f"planned_{reason}")

    def _graceful_stop(self, reason: str) -> None:
        if not self._has_live_worker():
            with self._state_lock:
                stale_process = self._process
                stale_job = self._job
            if stale_process is not None or stale_job is not None:
                self._terminate_current(reason)
            else:
                self._release_current_handles()
            return
        result = self._invoke_locked(
            "shutdown",
            timeout_sec=max(0.01, float(self._config.worker_cleanup_timeout_sec)),
            allow_spawn=False,
        )
        with self._state_lock:
            process = self._process
        if result.error is None and process is not None:
            process.join(max(0.0, float(self._config.worker_cleanup_timeout_sec)))
        cleanup_reason = reason
        if process is not None and process.is_alive():
            cleanup_reason = f"{reason}_cleanup_timeout"
        terminated = self._terminate_current(cleanup_reason)
        if terminated:
            self._log(
                "browser worker stopped "
                f"reason={reason} generation={self._process_generation}"
            )

    def _terminate_current(self, reason: str) -> bool:
        with self._termination_lock:
            with self._state_lock:
                process = self._process
                connection = self._connection
                job = self._job
                generation = self._process_generation
                worker_pid = self._worker_pid
                self._process = None
                self._connection = None
                self._job = None
                self._worker_pid = 0
                self._worker_started_at = 0.0
                self._successful_collects = 0
            if process is None:
                return False
            return self._terminate_detached(
                process,
                connection,
                job,
                generation=generation,
                worker_pid=worker_pid,
                reason=reason,
            )

    def _terminate_detached(
        self,
        process: Any,
        connection: Connection | None,
        job: WindowsJobBoundary | None,
        *,
        generation: int,
        worker_pid: int,
        reason: str,
    ) -> bool:
        self._log(
            "browser worker terminate start "
            f"reason={reason} generation={generation} pid={worker_pid}"
        )
        try:
            if job is not None:
                job.terminate()
        except BaseException as exc:
            self._log(
                "browser worker job terminate failed "
                f"reason={reason} type={type(exc).__name__}"
            )
        grace = max(0.0, float(self._config.timeout_recovery_grace_sec))
        try:
            process.join(grace)
            if process.is_alive():
                process.kill()
                process.join(grace)
        except BaseException:
            pass
        job_empty = False
        if job is not None:
            try:
                job_empty = job.wait_empty(grace)
            except BaseException:
                job_empty = False
        try:
            if connection is not None:
                connection.close()
        except BaseException:
            pass
        if job is not None:
            try:
                job.close()
            except BaseException:
                pass
        alive = False
        try:
            alive = bool(process.is_alive())
        except BaseException:
            pass
        self._log(
            "browser worker terminate end "
            f"reason={reason} generation={generation} pid={worker_pid} "
            f"worker_alive={str(alive).lower()} job_empty={str(job_empty).lower()}"
        )
        terminated = not alive and (job is None or job_empty)
        if not terminated:
            self._boundary_failed = True
            self._status = BrowserRuntimeStatus(
                BrowserState.FAILED,
                False,
                BrowserErrorCode.COLLECT_FAILED.value,
            )
            self._log(
                "browser profile handoff blocked "
                f"reason={reason} generation={generation} pid={worker_pid}"
            )
        return terminated

    def _has_live_worker(self) -> bool:
        with self._state_lock:
            process = self._process
            connection = self._connection
        return bool(process is not None and process.is_alive() and connection is not None)

    def _release_current_handles(self) -> None:
        with self._state_lock:
            process = self._process
            connection = self._connection
            job = self._job
            self._process = None
            self._connection = None
            self._job = None
            self._worker_pid = 0
            self._worker_started_at = 0.0
            self._successful_collects = 0
        try:
            if process is not None:
                process.join(0.1)
                process.close()
        except BaseException:
            pass
        try:
            if connection is not None:
                connection.close()
        except BaseException:
            pass
        try:
            if job is not None:
                job.close()
        except BaseException:
            pass

    def _transport_failure(self, detail: str) -> BrowserOperationResult:
        error = (
            self._last_failure_signal
            if self._last_failure_signal == BrowserErrorCode.RENDERER_CRASHED.value
            else BrowserErrorCode.TRANSPORT_CLOSED.value
        )
        self._status = BrowserRuntimeStatus(
            BrowserState.RECOVERING,
            False,
            error,
        )
        self._log(
            "browser worker transport closed "
            f"detail={detail} error={error}"
        )
        return BrowserOperationResult(error=error)

    def _log(self, message: str) -> None:
        sink = self._log_sink
        if sink is None:
            return
        try:
            sink(message)
        except BaseException:
            return
