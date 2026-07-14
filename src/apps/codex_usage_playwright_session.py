from __future__ import annotations

from collections.abc import Callable
from queue import Queue
import threading
import time
from typing import Protocol, final

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


class DriverProtocol(Protocol):
    def start(self) -> BrowserOperationResult: ...
    def collect(self) -> BrowserOperationResult: ...
    def open_login(self) -> BrowserOperationResult: ...
    def poll_login(self) -> BrowserOperationResult: ...
    def close_session(self) -> None: ...
    def shutdown(self) -> None: ...
    def get_runtime_status(self) -> BrowserRuntimeStatus: ...


DriverFactory = Callable[
    [PlaywrightSessionConfig, LogSink | None, PlaywrightStarter | None],
    DriverProtocol,
]


def _default_driver_factory(
    config: PlaywrightSessionConfig,
    log_sink: LogSink | None,
    playwright_starter: PlaywrightStarter | None,
) -> DriverProtocol:
    return CodexUsagePlaywrightDriver(config, log_sink, playwright_starter)


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
        self._status: BrowserRuntimeStatus = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
        self._shutdown: bool = False
        self._timeout_retry_delays_sec = tuple(
            max(0.0, float(delay)) for delay in config.timeout_retry_delays_sec
        )
        self._timeout_recovery_grace_sec = max(
            0.0, float(config.timeout_recovery_grace_sec)
        )
        self._sleep = sleep or time.sleep

    def collect(self) -> BrowserOperationResult:
        retry_max = len(self._timeout_retry_delays_sec)
        retry_attempt = 0
        while True:
            result = self._invoke(
                CollectCommand(),
                retry_attempt=retry_attempt,
                retry_max=retry_max,
            )
            if result.error != BrowserErrorCode.COMMAND_TIMEOUT.value:
                return result
            if retry_attempt >= retry_max:
                self._update_status(
                    BrowserRuntimeStatus(
                        BrowserState.FAILED,
                        False,
                        BrowserErrorCode.COMMAND_TIMEOUT.value,
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
                    BrowserErrorCode.COMMAND_TIMEOUT.value,
                    retry_attempt,
                    retry_max,
                )
            )
            self._log(
                "browser timeout retry scheduled "
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

    def shutdown(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                self._shutdown = True
                self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
                return
            if self._shutdown:
                return
            self._shutdown = True
            poisoned = bool(self._worker_poisoned)
        if poisoned:
            _ = self._recover_poisoned_worker()
            with self._lock:
                self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
            return
        envelope = _CommandEnvelope(ShutdownCommand())
        self._queue.put(envelope)
        completed = envelope.completed.wait(self._config.command_timeout_sec)
        if not completed:
            with self._lock:
                if thread is self._thread:
                    self._worker_poisoned = True
            _ = self._recover_poisoned_worker()
        else:
            thread.join(self._timeout_recovery_grace_sec)
        with self._lock:
            self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

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
        if worker is None and self._recover_poisoned_worker():
            worker = self._ensure_worker()
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
        queue.put(envelope)
        timeout_sec = self._command_timeout_sec(command)
        if not envelope.completed.wait(timeout_sec):
            with self._lock:
                worker_alive = bool(thread.is_alive())
                queue_depth = queue.qsize()
                if generation == self._worker_generation and thread is self._thread:
                    self._worker_poisoned = True
            self._log(
                "browser owner command timed out "
                f"command={type(command).__name__} "
                f"worker_alive={worker_alive} queue_depth={queue_depth} "
                f"generation={generation} timeout_sec={timeout_sec:g}"
            )
            self._update_status(
                BrowserRuntimeStatus(
                    BrowserState.RECOVERING,
                    False,
                    BrowserErrorCode.COMMAND_TIMEOUT.value,
                    min(retry_attempt + 1, retry_max),
                    retry_max,
                )
            )
            _ = self._recover_poisoned_worker()
            return BrowserOperationResult(error=BrowserErrorCode.COMMAND_TIMEOUT.value)
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
            if self._shutdown:
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
        with self._lock:
            if generation == self._worker_generation:
                self._driver = driver
        try:
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
                if started.error is not None:
                    envelope.result = started
                else:
                    try:
                        result = self._dispatch(driver, command)
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

    def _shutdown_driver(self, driver: DriverProtocol) -> None:
        try:
            driver.shutdown()
        except Exception as exc:
            self._log(f"browser owner shutdown failed type={type(exc).__name__}")

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
