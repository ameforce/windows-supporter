from __future__ import annotations

import os
from multiprocessing.connection import Connection

from src.apps.codex_usage_browser_types import (
    BrowserErrorCode,
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    PlaywrightSessionConfig,
)
from src.apps.codex_usage_playwright_driver import CodexUsagePlaywrightDriver


def _safe_send(connection: Connection, message: object) -> bool:
    try:
        connection.send(message)
    except (BrokenPipeError, EOFError, OSError):
        return False
    return True


def run_playwright_worker(connection: Connection) -> None:
    """Run synchronous Playwright only after the parent confirms Job containment."""

    try:
        bootstrap = connection.recv()
    except (EOFError, OSError):
        return
    if (
        not isinstance(bootstrap, tuple)
        or len(bootstrap) != 4
        or bootstrap[0] != "bootstrap"
        or not isinstance(bootstrap[1], PlaywrightSessionConfig)
    ):
        _safe_send(connection, ("bootstrap_error", "invalid_bootstrap"))
        return
    config = bootstrap[1]
    generation = int(bootstrap[2])
    initial_session_cookies = bootstrap[3]
    if not isinstance(initial_session_cookies, list):
        initial_session_cookies = []
    worker_pid = os.getpid()

    def emit_log(message: str) -> None:
        _safe_send(connection, ("log", str(message)))

    driver = CodexUsagePlaywrightDriver(
        config,
        log_sink=emit_log,
        initial_session_cookies=initial_session_cookies,
    )
    if not _safe_send(connection, ("ready", worker_pid, generation)):
        return

    while True:
        try:
            request = connection.recv()
        except (EOFError, OSError):
            return
        if (
            not isinstance(request, tuple)
            or len(request) != 3
            or request[0] != "command"
        ):
            continue
        request_id = int(request[1])
        command = str(request[2])
        should_exit = command == "shutdown"
        try:
            if command == "start":
                result = driver.start()
            elif command == "collect":
                result = driver.collect()
            elif command == "open_login":
                result = driver.open_login()
            elif command == "poll_login":
                result = driver.poll_login()
            elif command == "close_session":
                driver.close_session()
                result = BrowserOperationResult()
            elif command == "shutdown":
                driver.shutdown()
                result = BrowserOperationResult()
            else:
                result = BrowserOperationResult(
                    error=BrowserErrorCode.COLLECT_FAILED.value
                )
        except BaseException as exc:
            emit_log(
                "browser worker command failed "
                f"command={command} type={type(exc).__name__}"
            )
            result = BrowserOperationResult(
                error=BrowserErrorCode.COLLECT_FAILED.value
            )
        try:
            status = driver.get_runtime_status()
        except BaseException:
            status = BrowserRuntimeStatus(
                BrowserState.FAILED,
                False,
                result.error or BrowserErrorCode.COLLECT_FAILED.value,
            )
        try:
            session_cookies = driver.export_session_cookies()
        except BaseException:
            session_cookies = []
        if not _safe_send(
            connection,
            ("result", request_id, result, status, session_cookies),
        ):
            return
        if should_exit:
            return
