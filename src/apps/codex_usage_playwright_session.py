from __future__ import annotations

from collections.abc import Callable
from queue import Queue
import threading
import time
from typing import Any, Protocol, final

from src.apps.codex_usage_browser_types import (
    BrowserCommand,
    BrowserErrorCode,
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    CloseSessionCommand,
    CollectCommand,
    LogSink,
    OpenLoginCommand,
    PlaywrightSessionConfig,
    PlaywrightStarter,
    PollLoginCommand,
    ShutdownCommand,
)
from src.apps.codex_usage_playwright_driver import (
    CodexUsagePlaywrightDriver,
)
from src.apps.codex_usage_playwright_process import (
    CodexUsagePlaywrightProcessDriver,
)


_RECOVERABLE_WORKER_ERRORS = {
    BrowserErrorCode.COMMAND_TIMEOUT.value,
    BrowserErrorCode.RENDERER_CRASHED.value,
    BrowserErrorCode.TRANSPORT_CLOSED.value,
}


class DriverProtocol(Protocol):
    def start(self) -> BrowserOperationResult: ...
    def collect(self) -> BrowserOperationResult: ...
    def open_login(self) -> BrowserOperationResult: ...
    def poll_login(self) -> BrowserOperationResult: ...
    def close_session(self) -> None: ...
    def shutdown(self) -> None: ...
    def get_runtime_status(self) -> BrowserRuntimeStatus: ...
    def force_terminate(self, reason: str) -> bool: ...


DriverFactory = Callable[
    [PlaywrightSessionConfig, LogSink | None, PlaywrightStarter | None],
    DriverProtocol,
]


def _default_driver_factory(
    config: PlaywrightSessionConfig,
    log_sink: LogSink | None,
    playwright_starter: PlaywrightStarter | None,
) -> DriverProtocol:
    if playwright_starter is not None:
        return CodexUsagePlaywrightDriver(config, log_sink, playwright_starter)
    return CodexUsagePlaywrightProcessDriver(config, log_sink)


@final
class _CommandEnvelope:
    """Mutable hand-off cell completed by the browser owner thread."""

    def __init__(
        self,
        command: BrowserCommand,
        *,
        retry_attempt: int = 0,
        retry_max: int = 0,
    ) -> None:
        self.command: BrowserCommand = command
        self.retry_attempt = max(0, int(retry_attempt))
        self.retry_max = max(0, int(retry_max))
        self.completed: threading.Event = threading.Event()
        self.result: BrowserOperationResult = BrowserOperationResult()


@final
class CodexUsagePlaywrightSession:
    """Serializes one account's browser operations onto a lazy daemon thread."""

    def __init__(
        self,
        config: PlaywrightSessionConfig,
        log_sink: LogSink | None = None,
        driver_factory: DriverFactory | None = None,
        playwright_starter: PlaywrightStarter | None = None,
        sleep: Callable[[float], None] | None = None,
        unrecoverable_timeout_handler: Callable[[], bool] | None = None,
    ) -> None:
        self._config: PlaywrightSessionConfig = config
        self._log_sink: LogSink | None = log_sink
        self._driver_factory: DriverFactory = driver_factory or _default_driver_factory
        self._playwright_starter: PlaywrightStarter | None = playwright_starter
        self._queue: Queue[_CommandEnvelope] = Queue()
        self._lock: threading.Lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._driver: DriverProtocol | None = None
        self._worker_generation: int = 0
        self._worker_poisoned: bool = False
        self._worker_recovery_exhausted: bool = False
        self._recovery_request_sent: bool = False
        self._status: BrowserRuntimeStatus = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
        self._shutdown: bool = False
        self._cancel_requested: bool = False
        self._session_cookies: list[dict[str, Any]] = []
        self._timeout_retry_delays_sec = tuple(
            max(0.0, float(delay)) for delay in config.timeout_retry_delays_sec
        )
        self._timeout_recovery_grace_sec = max(
            0.0, float(config.timeout_recovery_grace_sec)
        )
        self._sleep = sleep or time.sleep
        self._unrecoverable_timeout_handler = unrecoverable_timeout_handler

    def collect(self) -> BrowserOperationResult:
        retry_max = len(self._timeout_retry_delays_sec)
        retry_attempt = 0
        while True:
            result = self._invoke(
                CollectCommand(),
                retry_attempt=retry_attempt,
                retry_max=retry_max,
            )
            with self._lock:
                if bool(self._cancel_requested):
                    return result
            if result.error not in _RECOVERABLE_WORKER_ERRORS:
                return result
            recovery_error = result.error or BrowserErrorCode.COMMAND_TIMEOUT.value
            with self._lock:
                recovery_exhausted = bool(self._worker_recovery_exhausted)
            if recovery_exhausted:
                self._update_status(
                    BrowserRuntimeStatus(
                        BrowserState.FAILED,
                        False,
                        recovery_error,
                        retry_max,
                        retry_max,
                    )
                )
                return result
            if retry_attempt >= retry_max:
                self._update_status(
                    BrowserRuntimeStatus(
                        BrowserState.FAILED,
                        False,
                        recovery_error,
                        retry_attempt,
                        retry_max,
                    )
                )
                self._log(
                    "browser timeout retry exhausted "
                    f"attempt={retry_attempt} max={retry_max}"
                )
                return result
            delay_sec = float(self._timeout_retry_delays_sec[retry_attempt])
            retry_attempt += 1
            self._update_status(
                BrowserRuntimeStatus(
                    BrowserState.RECOVERING,
                    False,
                    recovery_error,
                    retry_attempt,
                    retry_max,
                )
            )
            self._log(
                "browser worker recovery retry scheduled "
                f"error={recovery_error} "
                f"attempt={retry_attempt} max={retry_max} delay_sec={delay_sec:g}"
            )
            if delay_sec > 0.0:
                self._sleep(delay_sec)

    def open_login(self) -> BrowserOperationResult:
        return self._invoke(OpenLoginCommand())

    def poll_login(self) -> BrowserOperationResult:
        return self._invoke(PollLoginCommand())

    def close_session(self) -> None:
        with self._lock:
            if self._thread is None:
                self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
                return
        _ = self._invoke(CloseSessionCommand())

    def request_cancel(self) -> bool:
        with self._lock:
            thread = self._thread
            self._cancel_requested = True
            if thread is None or not thread.is_alive():
                return True
            queue = self._queue
            driver = self._driver
            self._worker_poisoned = True
        queue.put(_CommandEnvelope(ShutdownCommand()))
        if driver is None:
            return True
        hard_cancelled = self._force_terminate_driver(
            driver,
            reason=BrowserErrorCode.COMMAND_TIMEOUT.value,
        )
        if bool(hard_cancelled):
            self._shutdown_driver(driver)
        self._capture_session_cookies(driver)
        return bool(hard_cancelled)

    def shutdown(self) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None:
                self._shutdown = True
                self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
                return True
            if self._shutdown:
                return bool(not thread.is_alive())
            self._shutdown = True
            poisoned = bool(self._worker_poisoned)
        if poisoned:
            recovered = self._recover_poisoned_worker()
            if not recovered:
                self._trip_unrecoverable_timeout_circuit()
                with self._lock:
                    self._status = BrowserRuntimeStatus(
                        BrowserState.FAILED,
                        False,
                        BrowserErrorCode.COMMAND_TIMEOUT.value,
                    )
                return False
            with self._lock:
                self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
            return True
        envelope = _CommandEnvelope(ShutdownCommand())
        self._queue.put(envelope)
        completed = envelope.completed.wait(self._config.command_timeout_sec)
        if not completed:
            driver: DriverProtocol | None = None
            with self._lock:
                if thread is self._thread:
                    self._worker_poisoned = True
                    driver = self._driver
            hard_boundary_supported = self._driver_supports_hard_termination(driver)
            hard_cancelled = self._force_terminate_driver(
                driver,
                reason=BrowserErrorCode.COMMAND_TIMEOUT.value,
            )
            self._capture_session_cookies(driver)
            recovered = self._recover_poisoned_worker()
            if not recovered or (hard_boundary_supported and not hard_cancelled):
                self._trip_unrecoverable_timeout_circuit()
                with self._lock:
                    self._status = BrowserRuntimeStatus(
                        BrowserState.FAILED,
                        False,
                        BrowserErrorCode.COMMAND_TIMEOUT.value,
                    )
                return False
        else:
            thread.join(self._timeout_recovery_grace_sec)
        terminated = bool(not thread.is_alive())
        with self._lock:
            self._status = BrowserRuntimeStatus(
                BrowserState.STOPPED if terminated else BrowserState.FAILED,
                False,
                "" if terminated else BrowserErrorCode.COMMAND_TIMEOUT.value,
            )
        return terminated

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        with self._lock:
            return self._status

    def _invoke(
        self,
        command: BrowserCommand,
        *,
        retry_attempt: int = 0,
        retry_max: int = 0,
    ) -> BrowserOperationResult:
        worker = self._ensure_worker()
        if worker is None:
            if self._recover_poisoned_worker():
                worker = self._ensure_worker()
            else:
                self._trip_unrecoverable_timeout_circuit()
        if worker is None:
            with self._lock:
                timeout_pending = self._status.last_error == BrowserErrorCode.COMMAND_TIMEOUT.value
            return BrowserOperationResult(
                error=(
                    BrowserErrorCode.COMMAND_TIMEOUT.value
                    if timeout_pending
                    else BrowserErrorCode.COLLECT_FAILED.value
                )
            )
        queue, thread, generation = worker
        envelope = _CommandEnvelope(
            command,
            retry_attempt=retry_attempt,
            retry_max=retry_max,
        )
        with self._lock:
            cancelled = bool(self._cancel_requested)
            worker_changed = bool(
                generation != self._worker_generation or thread is not self._thread
            )
            if self._shutdown or cancelled or worker_changed:
                return BrowserOperationResult(
                    error=(
                        BrowserErrorCode.COMMAND_TIMEOUT.value
                        if cancelled
                        else BrowserErrorCode.COLLECT_FAILED.value
                    )
                )
            queue.put(envelope)
        timeout_sec = self._command_timeout_sec(command)
        if not envelope.completed.wait(timeout_sec):
            driver: DriverProtocol | None = None
            with self._lock:
                worker_alive = bool(thread.is_alive())
                queue_depth = queue.qsize()
                if generation == self._worker_generation and thread is self._thread:
                    self._worker_poisoned = True
                    driver = self._driver
            hard_cancelled = self._force_terminate_driver(
                driver,
                reason=BrowserErrorCode.COMMAND_TIMEOUT.value,
            )
            self._capture_session_cookies(driver)
            hard_boundary_supported = self._driver_supports_hard_termination(driver)
            recovery_error = BrowserErrorCode.COMMAND_TIMEOUT.value
            if hard_cancelled and driver is not None:
                try:
                    signaled_error = driver.get_runtime_status().last_error
                except Exception:
                    signaled_error = ""
                if signaled_error in _RECOVERABLE_WORKER_ERRORS:
                    recovery_error = signaled_error
            self._log(
                "browser owner command timed out "
                f"command={type(command).__name__} "
                f"worker_alive={worker_alive} queue_depth={queue_depth} "
                f"generation={generation} timeout_sec={timeout_sec:g} "
                f"hard_cancelled={str(hard_cancelled).lower()} "
                f"error={recovery_error}"
            )
            self._update_status(
                BrowserRuntimeStatus(
                    BrowserState.RECOVERING,
                    False,
                    recovery_error,
                    min(retry_attempt + 1, retry_max),
                    retry_max,
                )
            )
            recovered = self._recover_poisoned_worker()
            if not recovered or (hard_boundary_supported and not hard_cancelled):
                self._trip_unrecoverable_timeout_circuit()
            return BrowserOperationResult(error=recovery_error)
        return envelope.result

    def _command_timeout_sec(self, command: BrowserCommand) -> float:
        command_timeout = max(0.01, float(self._config.command_timeout_sec))
        if isinstance(command, CollectCommand):
            return max(
                0.01,
                min(command_timeout, float(self._config.collect_timeout_sec)),
            )
        return command_timeout

    def _ensure_worker(
        self,
    ) -> tuple[Queue[_CommandEnvelope], threading.Thread, int] | None:
        with self._lock:
            if self._shutdown or self._cancel_requested:
                return None
            if self._thread is not None and self._thread.is_alive():
                if self._worker_poisoned:
                    return None
                return self._queue, self._thread, self._worker_generation
            if self._thread is not None:
                self._queue = Queue()
                self._thread = None
                self._driver = None
                self._worker_poisoned = False
                self._worker_recovery_exhausted = False
                self._recovery_request_sent = False
            self._status = BrowserRuntimeStatus(BrowserState.STARTING, False, "")
            self._worker_generation += 1
            generation = self._worker_generation
            queue: Queue[_CommandEnvelope] = Queue()
            self._queue = queue
            self._thread = threading.Thread(
                target=self._run,
                args=(queue, generation),
                name="CodexUsagePlaywrightSession",
                daemon=True,
            )
            self._thread.start()
            return queue, self._thread, generation

    def _run(self, queue: Queue[_CommandEnvelope], generation: int) -> None:
        driver = self._driver_factory(self._config, self._log_sink, self._playwright_starter)
        self._restore_session_cookies(driver)
        with self._lock:
            if generation == self._worker_generation:
                self._driver = driver
            cancel_before_start = bool(
                generation == self._worker_generation and self._cancel_requested
            )
        try:
            if bool(cancel_before_start):
                started = BrowserOperationResult(
                    error=BrowserErrorCode.COMMAND_TIMEOUT.value
                )
            else:
                started = driver.start()
                self._update_status(driver.get_runtime_status())
            while True:
                envelope = queue.get()
                command = envelope.command
                if isinstance(command, ShutdownCommand):
                    driver.shutdown()
                    self._update_status(driver.get_runtime_status())
                    envelope.completed.set()
                    return
                if self._is_cancel_requested_for_generation(generation):
                    envelope.result = BrowserOperationResult(
                        error=BrowserErrorCode.COMMAND_TIMEOUT.value
                    )
                    envelope.completed.set()
                    self._shutdown_driver(driver)
                    return
                if envelope.retry_attempt > 0:
                    self._update_status(
                        BrowserRuntimeStatus(
                            BrowserState.RECOVERING,
                            False,
                            BrowserErrorCode.COMMAND_TIMEOUT.value,
                            envelope.retry_attempt,
                            envelope.retry_max,
                        )
                    )
                if started.error in _RECOVERABLE_WORKER_ERRORS:
                    started = driver.start()
                    self._update_status(driver.get_runtime_status())
                if started.error is not None:
                    envelope.result = started
                else:
                    try:
                        result = self._dispatch(driver, command)
                        self._capture_session_cookies(driver)
                        if self._is_worker_poisoned(generation):
                            envelope.result = BrowserOperationResult(
                                error=BrowserErrorCode.COMMAND_TIMEOUT.value
                            )
                            envelope.completed.set()
                            self._shutdown_driver(driver)
                            return
                        envelope.result = result
                        self._update_status(driver.get_runtime_status())
                    except Exception as exc:
                        self._log(
                            "browser owner command failed "
                            f"command={type(command).__name__} type={type(exc).__name__}"
                        )
                        if self._is_worker_poisoned(generation):
                            envelope.result = BrowserOperationResult(
                                error=BrowserErrorCode.COMMAND_TIMEOUT.value
                            )
                            envelope.completed.set()
                            self._shutdown_driver(driver)
                            return
                        try:
                            driver.close_session()
                        except Exception as close_exc:
                            self._log(
                                "browser owner recovery failed "
                                f"type={type(close_exc).__name__}"
                            )
                        envelope.result = BrowserOperationResult(
                            error=BrowserErrorCode.COLLECT_FAILED.value
                        )
                        self._update_status(
                            BrowserRuntimeStatus(
                                BrowserState.FAILED,
                                False,
                                BrowserErrorCode.COLLECT_FAILED.value,
                            )
                        )
                envelope.completed.set()
        finally:
            self._finish_worker(generation)

    def _is_worker_poisoned(self, generation: int) -> bool:
        with self._lock:
            return bool(
                generation == self._worker_generation and self._worker_poisoned
            )

    def _is_cancel_requested_for_generation(self, generation: int) -> bool:
        with self._lock:
            return bool(
                generation == self._worker_generation and self._cancel_requested
            )

    def _recover_poisoned_worker(self) -> bool:
        with self._lock:
            thread = self._thread
            poisoned = bool(self._worker_poisoned)
        if thread is None or not poisoned:
            return bool(thread is None or not thread.is_alive())
        self._log("browser timeout recovery waiting for owner cleanup")
        try:
            thread.join(self._timeout_recovery_grace_sec)
        except Exception:
            pass
        return not thread.is_alive()

    def _trip_unrecoverable_timeout_circuit(self) -> None:
        with self._lock:
            self._worker_recovery_exhausted = True
            if self._recovery_request_sent:
                return
            self._recovery_request_sent = True
            handler = self._unrecoverable_timeout_handler
        requested = False
        if handler is not None:
            try:
                requested = bool(handler())
            except Exception as exc:
                self._log(
                    "browser owner process-boundary recovery failed "
                    f"type={type(exc).__name__}"
                )
        outcome = "app_restart_requested" if requested else "circuit_open"
        self._log(f"browser owner unresponsive recovery={outcome}")

    def _shutdown_driver(self, driver: DriverProtocol) -> None:
        try:
            driver.shutdown()
        except Exception as exc:
            self._log(f"browser owner shutdown failed type={type(exc).__name__}")

    def _force_terminate_driver(
        self,
        driver: DriverProtocol | None,
        *,
        reason: str,
    ) -> bool:
        if driver is None:
            return False
        terminate = getattr(driver, "force_terminate", None)
        if not callable(terminate):
            return False
        try:
            terminated = bool(terminate(reason))
        except Exception as exc:
            self._log(
                "browser worker hard cancel failed "
                f"reason={reason} type={type(exc).__name__}"
            )
            return False
        self._log(
            "browser worker hard cancel "
            f"reason={reason} terminated={str(terminated).lower()}"
        )
        return terminated

    def _driver_supports_hard_termination(
        self,
        driver: DriverProtocol | None,
    ) -> bool:
        return bool(driver is not None and callable(getattr(driver, "force_terminate", None)))

    def _capture_session_cookies(self, driver: DriverProtocol | None) -> None:
        if driver is None:
            return
        exporter = getattr(driver, "export_session_cookies", None)
        if not callable(exporter):
            return
        try:
            cookies = exporter()
        except Exception:
            return
        if not isinstance(cookies, list):
            return
        with self._lock:
            self._session_cookies = [
                dict(cookie) for cookie in cookies if isinstance(cookie, dict)
            ]

    def _restore_session_cookies(self, driver: DriverProtocol) -> None:
        importer = getattr(driver, "import_session_cookies", None)
        if not callable(importer):
            return
        with self._lock:
            cookies = [dict(cookie) for cookie in self._session_cookies]
        try:
            importer(cookies)
        except Exception:
            return

    def _finish_worker(self, generation: int) -> None:
        with self._lock:
            if generation != self._worker_generation:
                return
            self._queue = Queue()
            self._thread = None
            self._driver = None
            self._worker_poisoned = False

    def _dispatch(self, driver: DriverProtocol, command: BrowserCommand) -> BrowserOperationResult:
        if isinstance(command, CollectCommand):
            return driver.collect()
        if isinstance(command, OpenLoginCommand):
            return driver.open_login()
        if isinstance(command, PollLoginCommand):
            return driver.poll_login()
        if isinstance(command, CloseSessionCommand):
            driver.close_session()
            return BrowserOperationResult()
        driver.shutdown()
        return BrowserOperationResult()

    def _update_status(self, status: BrowserRuntimeStatus) -> None:
        with self._lock:
            self._status = status

    def _log(self, message: str) -> None:
        sink = self._log_sink
        if sink is None:
            return
        try:
            sink(message)
        except Exception:
            return
