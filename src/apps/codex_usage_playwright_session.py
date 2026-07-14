from __future__ import annotations

from collections.abc import Callable
from queue import Queue
import threading
from typing import Protocol, final

from src.apps.codex_usage_browser_types import (
    BrowserCommand,
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

    def __init__(self, command: BrowserCommand) -> None:
        self.command: BrowserCommand = command
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
    ) -> None:
        self._config: PlaywrightSessionConfig = config
        self._log_sink: LogSink | None = log_sink
        self._driver_factory: DriverFactory = driver_factory or _default_driver_factory
        self._playwright_starter: PlaywrightStarter | None = playwright_starter
        self._queue: Queue[_CommandEnvelope] = Queue()
        self._lock: threading.Lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._driver: DriverProtocol | None = None
        self._status: BrowserRuntimeStatus = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")
        self._shutdown: bool = False

    def collect(self) -> BrowserOperationResult:
        return self._invoke(CollectCommand())

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
        envelope = _CommandEnvelope(ShutdownCommand())
        self._queue.put(envelope)
        _ = envelope.completed.wait(self._config.command_timeout_sec)
        thread.join(self._config.command_timeout_sec)
        with self._lock:
            self._status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        with self._lock:
            return self._status

    def _invoke(self, command: BrowserCommand) -> BrowserOperationResult:
        if not self._ensure_worker():
            return BrowserOperationResult(error="collect_failed")
        envelope = _CommandEnvelope(command)
        self._queue.put(envelope)
        if not envelope.completed.wait(self._config.command_timeout_sec):
            with self._lock:
                worker = self._thread
                worker_alive = bool(worker is not None and worker.is_alive())
                queue_depth = self._queue.qsize()
            self._log(
                "browser owner command timed out "
                f"command={type(command).__name__} "
                f"worker_alive={worker_alive} queue_depth={queue_depth}"
            )
            self._update_status(BrowserRuntimeStatus(BrowserState.FAILED, False, "collect_failed"))
            return BrowserOperationResult(error="collect_failed")
        return envelope.result

    def _ensure_worker(self) -> bool:
        with self._lock:
            if self._shutdown:
                return False
            if self._thread is not None and self._thread.is_alive():
                return True
            if self._thread is not None:
                self._queue = Queue()
                self._thread = None
                self._driver = None
            self._status = BrowserRuntimeStatus(BrowserState.STARTING, False, "")
            self._thread = threading.Thread(
                target=self._run,
                name="CodexUsagePlaywrightSession",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self) -> None:
        driver = self._driver_factory(self._config, self._log_sink, self._playwright_starter)
        self._driver = driver
        started = driver.start()
        self._update_status(driver.get_runtime_status())
        while True:
            envelope = self._queue.get()
            command = envelope.command
            if isinstance(command, ShutdownCommand):
                driver.shutdown()
                self._update_status(driver.get_runtime_status())
                envelope.completed.set()
                return
            if started.error is not None:
                envelope.result = started
            else:
                try:
                    envelope.result = self._dispatch(driver, command)
                    self._update_status(driver.get_runtime_status())
                except Exception as exc:
                    self._log(
                        "browser owner command failed "
                        f"command={type(command).__name__} type={type(exc).__name__}"
                    )
                    try:
                        driver.close_session()
                    except Exception as close_exc:
                        self._log(
                            "browser owner recovery failed "
                            f"type={type(close_exc).__name__}"
                        )
                    envelope.result = BrowserOperationResult(error="collect_failed")
                    self._update_status(
                        BrowserRuntimeStatus(BrowserState.FAILED, False, "collect_failed")
                    )
            envelope.completed.set()

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
