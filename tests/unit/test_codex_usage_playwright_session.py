from __future__ import annotations

from collections.abc import Callable
import threading
import unittest
from typing import final

from src.apps.codex_usage_browser_types import (
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    LogSink,
    PlaywrightSessionConfig,
    PlaywrightStarter,
)
from src.apps.codex_usage_playwright_session import CodexUsagePlaywrightSession


@final
class FakeDriver:
    """Mutable driver fake that records owner-thread command routing."""

    def __init__(self, start_error: str | None = None) -> None:
        self.calls: list[str] = []
        self.thread_ids: list[int] = []
        self.daemon_flags: list[bool] = []
        self.start_error: str | None = start_error
        self.status: BrowserRuntimeStatus = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def _record(self, name: str) -> None:
        self.calls.append(name)
        self.thread_ids.append(threading.get_ident())
        self.daemon_flags.append(threading.current_thread().daemon)

    def start(self) -> BrowserOperationResult:
        self._record("start")
        if self.start_error:
            self.status = BrowserRuntimeStatus(BrowserState.FAILED, False, self.start_error)
            return BrowserOperationResult(error=self.start_error)
        return BrowserOperationResult()

    def collect(self) -> BrowserOperationResult:
        self._record("collect")
        self.status = BrowserRuntimeStatus(BrowserState.HEADLESS_READY, False, "")
        return BrowserOperationResult(probe={"url": "usage", "metricBlocks": []})

    def open_login(self) -> BrowserOperationResult:
        self._record("open_login")
        self.status = BrowserRuntimeStatus(BrowserState.HEADED_LOGIN, True, "")
        return BrowserOperationResult()

    def poll_login(self) -> BrowserOperationResult:
        self._record("poll_login")
        return BrowserOperationResult(error="login_required")

    def close_session(self) -> None:
        self._record("close_session")
        self.status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def shutdown(self) -> None:
        self._record("shutdown")
        self.status = BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return self.status


@final
class DriverFactory:
    """Mutable factory fake proving lazy construction."""

    def __init__(self, start_error: str | None = None) -> None:
        self.driver: FakeDriver = FakeDriver(start_error)
        self.calls: int = 0

    def __call__(
        self,
        _config: PlaywrightSessionConfig,
        _log_sink: LogSink | None,
        _playwright_starter: PlaywrightStarter | None,
    ) -> FakeDriver:
        self.calls += 1
        return self.driver


def make_session(factory: DriverFactory) -> CodexUsagePlaywrightSession:
    config = PlaywrightSessionConfig(
        profile_dir="profile",
        usage_url="https://chatgpt.com/codex/settings/usage",
        probe_script="probe()",
        command_timeout_sec=2.0,
    )
    return CodexUsagePlaywrightSession(config, driver_factory=factory)


class CodexUsagePlaywrightSessionTest(unittest.TestCase):
    def test_runtime_status_and_unstarted_shutdown_do_not_create_worker(self) -> None:
        factory = DriverFactory()
        session = make_session(factory)

        status = session.get_runtime_status()
        session.shutdown()
        session.shutdown()

        self.assertEqual(status, BrowserRuntimeStatus(BrowserState.STOPPED, False, ""))
        self.assertEqual(factory.calls, 0)
        self.assertEqual(factory.driver.calls, [])

    def test_each_browser_operation_can_be_the_lazy_start_trigger(self) -> None:
        operations: tuple[
            tuple[str, Callable[[CodexUsagePlaywrightSession], BrowserOperationResult | None]],
            ...,
        ] = (
            ("collect", lambda session: session.collect()),
            ("open_login", lambda session: session.open_login()),
            ("poll_login", lambda session: session.poll_login()),
        )
        for expected_call, operation in operations:
            with self.subTest(operation=expected_call):
                factory = DriverFactory()
                session = make_session(factory)

                _ = operation(session)

                self.assertEqual(factory.calls, 1)
                self.assertEqual(factory.driver.calls[:2], ["start", expected_call])
                session.shutdown()

    def test_close_session_before_first_use_does_not_start_worker(self) -> None:
        factory = DriverFactory()
        session = make_session(factory)

        session.close_session()

        self.assertEqual(factory.calls, 0)
        self.assertEqual(factory.driver.calls, [])
        self.assertEqual(session.get_runtime_status().state, BrowserState.STOPPED)

    def test_first_operation_lazy_starts_daemon_owner_and_routes_all_commands(self) -> None:
        factory = DriverFactory()
        session = make_session(factory)
        caller_thread = threading.get_ident()

        collect_result = session.collect()
        login_result = session.open_login()
        poll_result = session.poll_login()
        session.close_session()

        self.assertIsNotNone(collect_result.probe)
        self.assertEqual(login_result.error, None)
        self.assertEqual(poll_result.error, "login_required")
        self.assertEqual(factory.calls, 1)
        self.assertEqual(
            factory.driver.calls,
            ["start", "collect", "open_login", "poll_login", "close_session"],
        )
        self.assertEqual(len(set(factory.driver.thread_ids)), 1)
        self.assertNotEqual(factory.driver.thread_ids[0], caller_thread)
        self.assertTrue(all(factory.driver.daemon_flags))
        session.shutdown()

    def test_shutdown_routes_once_and_is_idempotent_after_start(self) -> None:
        factory = DriverFactory()
        session = make_session(factory)
        _ = session.collect()

        session.shutdown()
        session.shutdown()

        self.assertEqual(factory.driver.calls.count("start"), 1)
        self.assertEqual(factory.driver.calls.count("shutdown"), 1)
        self.assertEqual(session.get_runtime_status().state, BrowserState.STOPPED)

    def test_playwright_start_failure_is_returned_without_dispatching_operation(self) -> None:
        factory = DriverFactory(start_error="playwright_unavailable")
        session = make_session(factory)

        result = session.collect()

        self.assertEqual(result.error, "playwright_unavailable")
        self.assertEqual(factory.driver.calls, ["start"])
        self.assertEqual(session.get_runtime_status().state, BrowserState.FAILED)
        session.shutdown()


if __name__ == "__main__":
    _ = unittest.main()
