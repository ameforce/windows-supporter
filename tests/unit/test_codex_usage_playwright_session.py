from __future__ import annotations

from collections.abc import Callable
from queue import Queue
import threading
import time
import unittest
from typing import final
from unittest.mock import patch

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

    def __init__(
        self,
        start_error: str | None = None,
        collect_failures: list[Exception] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.thread_ids: list[int] = []
        self.daemon_flags: list[bool] = []
        self.start_error: str | None = start_error
        self.collect_failures: list[Exception] = list(collect_failures or [])
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
        if self.collect_failures:
            raise self.collect_failures.pop(0)
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

    def __init__(
        self,
        start_error: str | None = None,
        collect_failures: list[Exception] | None = None,
    ) -> None:
        self.driver: FakeDriver = FakeDriver(start_error, collect_failures)
        self.calls: int = 0

    def __call__(
        self,
        _config: PlaywrightSessionConfig,
        _log_sink: LogSink | None,
        _playwright_starter: PlaywrightStarter | None,
    ) -> FakeDriver:
        self.calls += 1
        return self.driver


@final
class BlockingDriver(FakeDriver):
    def __init__(
        self,
        release: threading.Event,
        *,
        release_after_sec: float | None = None,
        wait_timeout_sec: float | None = 2.0,
    ) -> None:
        super().__init__()
        self.release = release
        self.release_after_sec = release_after_sec
        self.wait_timeout_sec = wait_timeout_sec

    def collect(self) -> BrowserOperationResult:
        self._record("collect")
        if self.release_after_sec is not None:
            timer = threading.Timer(self.release_after_sec, self.release.set)
            timer.daemon = True
            timer.start()
        _ = self.release.wait(self.wait_timeout_sec)
        self.status = BrowserRuntimeStatus(BrowserState.FAILED, False, "collect_failed")
        return BrowserOperationResult(error="collect_failed")


@final
class TimeoutThenSuccessDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.collect_count = 0

    def collect(self) -> BrowserOperationResult:
        self._record("collect")
        self.collect_count += 1
        if self.collect_count == 1:
            self.status = BrowserRuntimeStatus(
                BrowserState.FAILED,
                False,
                "command_timeout",
            )
            return BrowserOperationResult(error="command_timeout")
        self.status = BrowserRuntimeStatus(BrowserState.HEADLESS_READY, False, "")
        return BrowserOperationResult(probe={"url": "usage", "metricBlocks": []})


@final
class TerminableBlockingDriver(BlockingDriver):
    def force_terminate(self, reason: str) -> bool:
        self.calls.append(f"force_terminate:{reason}")
        self.release.set()
        return True


@final
class TerminableShutdownBlockingDriver(FakeDriver):
    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self.release = release

    def shutdown(self) -> None:
        self._record("shutdown")
        self.release.wait()

    def force_terminate(self, reason: str) -> bool:
        self.calls.append(f"force_terminate:{reason}")
        self.release.set()
        return True


@final
class GatedTerminateBlockingDriver(BlockingDriver):
    def __init__(
        self,
        collect_release: threading.Event,
        terminate_started: threading.Event,
        terminate_release: threading.Event,
    ) -> None:
        super().__init__(collect_release, wait_timeout_sec=None)
        self.terminate_started = terminate_started
        self.terminate_release = terminate_release

    def force_terminate(self, reason: str) -> bool:
        self.calls.append(f"force_terminate:{reason}")
        self.terminate_started.set()
        self.terminate_release.wait(2.0)
        self.release.set()
        return True


@final
class SequenceDriverFactory:
    def __init__(self, drivers: list[FakeDriver]) -> None:
        self.drivers = list(drivers)
        self.calls = 0

    def __call__(
        self,
        _config: PlaywrightSessionConfig,
        _log_sink: LogSink | None,
        _playwright_starter: PlaywrightStarter | None,
    ) -> FakeDriver:
        driver = self.drivers[self.calls]
        self.calls += 1
        return driver


def make_session(
    factory: DriverFactory,
    *,
    command_timeout_sec: float = 2.0,
    unrecoverable_timeout_handler: Callable[[], bool] | None = None,
) -> CodexUsagePlaywrightSession:
    config = PlaywrightSessionConfig(
        profile_dir="profile",
        usage_url="https://chatgpt.com/codex/settings/usage",
        probe_script="probe()",
        command_timeout_sec=command_timeout_sec,
    )
    return CodexUsagePlaywrightSession(
        config,
        driver_factory=factory,
        unrecoverable_timeout_handler=unrecoverable_timeout_handler,
    )


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

    def test_unexpected_collect_exception_does_not_kill_owner_or_stall_next_refresh(self) -> None:
        factory = DriverFactory(
            collect_failures=[RuntimeError("unexpected browser owner failure")]
        )
        session = make_session(factory, command_timeout_sec=0.1)

        failed = session.collect()
        recovered = session.collect()

        self.assertEqual(failed.error, "collect_failed")
        self.assertIsNotNone(
            recovered.probe,
            "the next refresh must run instead of timing out behind a dead owner thread",
        )
        self.assertEqual(
            factory.driver.calls[:4],
            ["start", "collect", "close_session", "collect"],
        )
        session.shutdown()

    def test_command_timeout_recovers_connection_and_retries_on_a_fresh_owner(self) -> None:
        release = threading.Event()
        first = TerminableBlockingDriver(release)
        second = FakeDriver()
        factory = SequenceDriverFactory([first, second])
        session = make_session(factory, command_timeout_sec=0.05)
        session._timeout_retry_delays_sec = (0.0,)
        session._timeout_recovery_grace_sec = 0.2
        session._sleep = lambda _delay: None

        try:
            result = session.collect()

            self.assertIsNotNone(
                result.probe,
                "a timed-out owner must be discarded before the automatic retry",
            )
            self.assertEqual(factory.calls, 2)
            self.assertIn("shutdown", first.calls)
            self.assertEqual(second.calls[:2], ["start", "collect"])
        finally:
            release.set()
            session.shutdown()

    def test_driver_timeout_result_is_automatically_retried(self) -> None:
        driver = TimeoutThenSuccessDriver()
        factory = SequenceDriverFactory([driver])
        session = make_session(factory)
        session._timeout_retry_delays_sec = (0.0,)
        session._sleep = lambda _delay: None

        result = session.collect()

        self.assertIsNotNone(result.probe)
        self.assertEqual(factory.calls, 1)
        self.assertEqual(driver.calls[:3], ["start", "collect", "collect"])
        status = session.get_runtime_status()
        self.assertEqual(status.state, BrowserState.HEADLESS_READY)
        self.assertEqual(status.retry_attempt, 0)
        session.shutdown()

    def test_command_timeout_retries_are_bounded_and_remain_visible(self) -> None:
        releases = [threading.Event() for _ in range(3)]
        drivers = [
            TerminableBlockingDriver(release)
            for release in releases
        ]
        factory = SequenceDriverFactory(drivers)
        session = make_session(factory, command_timeout_sec=0.03)
        session._timeout_retry_delays_sec = (0.0, 0.0)
        session._timeout_recovery_grace_sec = 0.2
        session._sleep = lambda _delay: None

        try:
            result = session.collect()
            status = session.get_runtime_status()

            self.assertEqual(result.error, "command_timeout")
            self.assertEqual(factory.calls, 3)
            self.assertEqual(status.state, BrowserState.FAILED)
            self.assertEqual(status.last_error, "command_timeout")
            self.assertEqual(status.retry_attempt, 2)
            self.assertEqual(status.retry_max, 2)
        finally:
            for release in releases:
                release.set()
            session.shutdown()

    def test_command_timeout_does_not_queue_more_work_behind_unresponsive_owner(self) -> None:
        release = threading.Event()
        driver = BlockingDriver(release)
        factory = SequenceDriverFactory([driver])
        session = make_session(factory, command_timeout_sec=0.03)
        session._timeout_retry_delays_sec = (0.0, 0.0)
        session._timeout_recovery_grace_sec = 0.0
        session._sleep = lambda _delay: None

        try:
            result = session.collect()

            self.assertEqual(result.error, "command_timeout")
            self.assertEqual(factory.calls, 1)
            self.assertEqual(driver.calls, ["start", "collect"])
            self.assertEqual(session._queue.qsize(), 0)
        finally:
            release.set()
            session.shutdown()

    def test_permanently_stuck_owner_requests_process_boundary_recovery_once(self) -> None:
        release = threading.Event()
        driver = BlockingDriver(release, wait_timeout_sec=None)
        factory = SequenceDriverFactory([driver])
        recovery_requests: list[str] = []
        retry_sleeps: list[float] = []
        session = make_session(
            factory,
            command_timeout_sec=0.03,
            unrecoverable_timeout_handler=(
                lambda: recovery_requests.append("restart") or True
            ),
        )
        session._timeout_retry_delays_sec = (5.0, 15.0, 30.0)
        session._timeout_recovery_grace_sec = 0.0
        session._sleep = retry_sleeps.append

        try:
            first = session.collect()
            second = session.collect()

            self.assertEqual(first.error, "command_timeout")
            self.assertEqual(second.error, "command_timeout")
            self.assertEqual(
                recovery_requests,
                ["restart"],
                "an immortal Playwright owner must request one process-boundary restart",
            )
            self.assertEqual(
                retry_sleeps,
                [],
                "once owner cleanup is impossible, fake retries must not sustain a timeout storm",
            )
            self.assertEqual(factory.calls, 1)
            status = session.get_runtime_status()
            self.assertEqual(status.retry_attempt, status.retry_max)
        finally:
            release.set()
            session.shutdown()

    def test_stuck_owner_is_hard_cancelled_and_retried_without_app_restart(self) -> None:
        release = threading.Event()
        first = TerminableBlockingDriver(release, wait_timeout_sec=None)
        second = FakeDriver()
        factory = SequenceDriverFactory([first, second])
        recovery_requests: list[str] = []
        session = make_session(
            factory,
            command_timeout_sec=0.03,
            unrecoverable_timeout_handler=(
                lambda: recovery_requests.append("restart") or True
            ),
        )
        session._timeout_retry_delays_sec = (0.0,)
        session._timeout_recovery_grace_sec = 0.2
        session._sleep = lambda _delay: None

        try:
            result = session.collect()

            self.assertIsNotNone(
                result.probe,
                "the killed browser worker must be replaced in the same app process",
            )
            self.assertIn("force_terminate:command_timeout", first.calls)
            self.assertEqual(factory.calls, 2)
            self.assertEqual(recovery_requests, [])
        finally:
            release.set()
            session.shutdown()

    def test_shutdown_timeout_hard_cancels_the_owner_before_returning(self) -> None:
        release = threading.Event()
        driver = TerminableShutdownBlockingDriver(release)
        factory = SequenceDriverFactory([driver])
        session = make_session(factory, command_timeout_sec=0.03)
        session._timeout_recovery_grace_sec = 0.2
        self.assertIsNotNone(session.collect().probe)

        session.shutdown()

        self.assertIn("force_terminate:command_timeout", driver.calls)
        self.assertTrue(session._thread is None or not session._thread.is_alive())
        self.assertEqual(session.get_runtime_status().state, BrowserState.STOPPED)

    def test_request_cancel_hard_cancels_active_owner_without_command_timeout(self) -> None:
        release = threading.Event()
        driver = TerminableBlockingDriver(release, wait_timeout_sec=None)
        factory = SequenceDriverFactory([driver])
        session = make_session(factory, command_timeout_sec=2.0)
        collect_thread = threading.Thread(target=session.collect, daemon=True)
        collect_thread.start()
        deadline = time.monotonic() + 1.0
        while "collect" not in driver.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("collect", driver.calls)

        started = time.monotonic()
        session.request_cancel()
        elapsed = time.monotonic() - started
        collect_thread.join(1.0)

        self.assertLess(elapsed, 0.5)
        self.assertFalse(collect_thread.is_alive())
        self.assertIn("force_terminate:command_timeout", driver.calls)
        session.shutdown()

    def test_request_cancel_hard_cancels_active_login_command(self) -> None:
        release = threading.Event()

        class _TerminableBlockingLoginDriver(FakeDriver):
            def open_login(self) -> BrowserOperationResult:
                self._record("open_login")
                release.wait()
                return BrowserOperationResult(error="login_required")

            def force_terminate(self, reason: str) -> bool:
                self.calls.append(f"force_terminate:{reason}")
                release.set()
                return True

        driver = _TerminableBlockingLoginDriver()
        factory = SequenceDriverFactory([driver])
        session = make_session(factory, command_timeout_sec=2.0)
        login_thread = threading.Thread(target=session.open_login, daemon=True)
        login_thread.start()
        deadline = time.monotonic() + 1.0
        while "open_login" not in driver.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("open_login", driver.calls)

        started = time.monotonic()
        cancel_result = session.request_cancel()
        elapsed = time.monotonic() - started
        login_thread.join(1.0)

        self.assertTrue(cancel_result)
        self.assertLess(elapsed, 0.5)
        self.assertFalse(login_thread.is_alive())
        self.assertIn("force_terminate:command_timeout", driver.calls)
        session.shutdown()

    def test_request_cancel_hard_cancels_active_login_poll(self) -> None:
        release = threading.Event()

        class _TerminableBlockingLoginPollDriver(FakeDriver):
            def poll_login(self) -> BrowserOperationResult:
                self._record("poll_login")
                release.wait()
                return BrowserOperationResult(error="login_required")

            def force_terminate(self, reason: str) -> bool:
                self.calls.append(f"force_terminate:{reason}")
                release.set()
                return True

        driver = _TerminableBlockingLoginPollDriver()
        factory = SequenceDriverFactory([driver])
        session = make_session(factory, command_timeout_sec=2.0)
        poll_thread = threading.Thread(target=session.poll_login, daemon=True)
        poll_thread.start()
        deadline = time.monotonic() + 1.0
        while "poll_login" not in driver.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("poll_login", driver.calls)

        cancel_result = session.request_cancel()
        poll_thread.join(1.0)

        self.assertTrue(cancel_result)
        self.assertFalse(poll_thread.is_alive())
        self.assertIn("force_terminate:command_timeout", driver.calls)
        session.shutdown()

    def test_request_cancel_leaves_alive_idle_owner_for_normal_shutdown(self) -> None:
        class _TerminableIdleDriver(FakeDriver):
            def force_terminate(self, reason: str) -> bool:
                self.calls.append(f"force_terminate:{reason}")
                return True

        driver = _TerminableIdleDriver()
        factory = SequenceDriverFactory([driver])
        session = make_session(factory)
        self.assertIsNotNone(session.collect().probe)
        self.assertIsNotNone(session._thread)
        self.assertTrue(session._thread.is_alive())

        cancel_result = session.request_cancel()

        self.assertTrue(cancel_result)
        self.assertFalse(session._worker_poisoned)
        self.assertNotIn("force_terminate:command_timeout", driver.calls)
        self.assertTrue(session.shutdown())
        self.assertIn("shutdown", driver.calls)
        self.assertTrue(session._thread is None or not session._thread.is_alive())

    def test_request_cancel_during_factory_start_never_dispatches_queued_collect(self) -> None:
        factory_started = threading.Event()
        release_factory = threading.Event()
        driver = FakeDriver()

        def gated_factory(
            _config: PlaywrightSessionConfig,
            _log_sink: LogSink | None,
            _playwright_starter: PlaywrightStarter | None,
        ) -> FakeDriver:
            factory_started.set()
            release_factory.wait(2.0)
            return driver

        session = make_session(gated_factory)
        collect_thread = threading.Thread(target=session.collect, daemon=True)
        collect_thread.start()
        self.assertTrue(factory_started.wait(1.0))

        cancel_result = session.request_cancel()
        release_factory.set()
        collect_thread.join(1.0)

        self.assertFalse(cancel_result)
        self.assertFalse(collect_thread.is_alive())
        self.assertNotIn("collect", driver.calls)
        session.shutdown()

    def test_request_cancel_interrupts_retry_wait_without_reporting_completion(self) -> None:
        driver = TimeoutThenSuccessDriver()
        factory = SequenceDriverFactory([driver])
        session = make_session(factory)
        session._timeout_retry_delays_sec = (0.3,)
        result = []
        collect_thread = threading.Thread(
            target=lambda: result.append(session.collect()),
            daemon=True,
        )
        collect_thread.start()
        deadline = time.monotonic() + 1.0
        while (
            session.get_runtime_status().state != BrowserState.RECOVERING
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        self.assertEqual(session.get_runtime_status().state, BrowserState.RECOVERING)

        cancel_result = session.request_cancel()
        collect_thread.join(0.1)

        self.assertFalse(cancel_result)
        self.assertFalse(collect_thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertEqual(driver.collect_count, 1)
        self.assertTrue(session.shutdown())

    def test_request_cancel_makes_driver_terminal_before_racing_dispatch(self) -> None:
        class _RespawnGuardDriver(FakeDriver):
            def __init__(self) -> None:
                super().__init__()
                self.hard_terminated = False
                self.terminal = False
                self.spawned_after_cancel = False

            def collect(self) -> BrowserOperationResult:
                self._record("collect")
                if self.hard_terminated and not self.terminal:
                    self.spawned_after_cancel = True
                return BrowserOperationResult(error="collect_failed")

            def force_terminate(self, reason: str) -> bool:
                self.calls.append(f"force_terminate:{reason}")
                self.hard_terminated = True
                return True

            def shutdown(self) -> None:
                self.terminal = True
                super().shutdown()

        check_started = threading.Event()
        release_check = threading.Event()
        driver = _RespawnGuardDriver()
        factory = SequenceDriverFactory([driver])
        session = make_session(factory)

        def gated_cancel_check(_generation: int) -> bool:
            check_started.set()
            release_check.wait(2.0)
            return False

        session._is_cancel_requested_for_generation = gated_cancel_check
        collect_thread = threading.Thread(target=session.collect, daemon=True)
        collect_thread.start()
        self.assertTrue(check_started.wait(1.0))

        self.assertTrue(session.request_cancel())
        release_check.set()
        collect_thread.join(1.0)

        self.assertFalse(collect_thread.is_alive())
        self.assertFalse(driver.spawned_after_cancel)
        session.shutdown()

    def test_collect_enqueue_is_atomic_with_cancel_shutdown_enqueue(self) -> None:
        collect_put_started = threading.Event()
        release_collect_put = threading.Event()

        class _GateQueue(Queue):
            def put(self, item, block=True, timeout=None):
                command_name = type(getattr(item, "command", None)).__name__
                if command_name == "CollectCommand":
                    collect_put_started.set()
                    release_collect_put.wait(2.0)
                return super().put(item, block=block, timeout=timeout)

        gate_queue = _GateQueue()
        factory = DriverFactory()
        with patch(
            "src.apps.codex_usage_playwright_session.Queue",
            return_value=gate_queue,
        ):
            session = make_session(factory, command_timeout_sec=0.3)
            collect_finished = threading.Event()
            cancel_finished = threading.Event()
            collect_thread = threading.Thread(
                target=lambda: (session.collect(), collect_finished.set()),
                daemon=True,
            )
            collect_thread.start()
            self.assertTrue(collect_put_started.wait(1.0))
            cancel_thread = threading.Thread(
                target=lambda: (session.request_cancel(), cancel_finished.set()),
                daemon=True,
            )
            cancel_thread.start()

            cancel_completed_before_release = cancel_finished.wait(0.1)
            release_collect_put.set()
            collect_completed_after_release = collect_finished.wait(0.15)
            collect_thread.join(1.0)
            cancel_thread.join(1.0)

            self.assertFalse(cancel_completed_before_release)
            self.assertTrue(collect_completed_after_release)
            self.assertFalse(collect_thread.is_alive())
            self.assertFalse(cancel_thread.is_alive())
            session.shutdown()

    def test_concurrent_shutdown_never_reports_stopped_with_poisoned_owner_alive(self) -> None:
        collect_release = threading.Event()
        terminate_started = threading.Event()
        terminate_release = threading.Event()
        driver = GatedTerminateBlockingDriver(
            collect_release,
            terminate_started,
            terminate_release,
        )
        factory = SequenceDriverFactory([driver])
        session = make_session(factory, command_timeout_sec=0.03)
        session._timeout_retry_delays_sec = ()
        session._timeout_recovery_grace_sec = 0.02
        collect_thread = threading.Thread(target=session.collect)
        collect_thread.start()
        self.assertTrue(terminate_started.wait(1.0))

        shutdown_result = session.shutdown()

        self.assertIs(shutdown_result, False)
        self.assertEqual(session.get_runtime_status().state, BrowserState.FAILED)
        self.assertTrue(collect_thread.is_alive())
        terminate_release.set()
        collect_thread.join(1.0)
        collect_release.set()


if __name__ == "__main__":
    _ = unittest.main()
