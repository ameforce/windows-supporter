from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock

from src.apps.codex_usage_browser_types import (
    BrowserErrorCode,
    BrowserRuntimeStatus,
    BrowserState,
    PlaywrightSessionConfig,
)
from src.apps.codex_usage_playwright_process import (
    CodexUsagePlaywrightProcessDriver,
    select_worker_recycle_reason,
)


class _StoppedProcess:
    pid = 1234

    def is_alive(self) -> bool:
        return False

    def join(self, _timeout: float) -> None:
        return


class _NonEmptyJob:
    def __init__(self) -> None:
        self.closed = False

    def terminate(self) -> None:
        return

    def wait_empty(self, _timeout: float) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class _DiesDuringShutdownProcess:
    pid = 4321

    def __init__(self) -> None:
        self.checks = 0

    def is_alive(self) -> bool:
        self.checks += 1
        return self.checks == 1

    def join(self, _timeout: float) -> None:
        return

    def close(self) -> None:
        return


class _BlockingEmptyJob(_NonEmptyJob):
    def __init__(self) -> None:
        super().__init__()
        self.wait_started = threading.Event()
        self.release = threading.Event()

    def wait_empty(self, _timeout: float) -> bool:
        self.wait_started.set()
        self.release.wait(2.0)
        return True


class CodexUsagePlaywrightProcessPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PlaywrightSessionConfig(
            profile_dir="profile",
            usage_url="https://chatgpt.com/codex/settings/usage",
            probe_script="probe()",
            worker_recycle_success_count=100,
            worker_recycle_max_age_sec=3_600.0,
            worker_recycle_max_process_rss_bytes=1_610_612_736,
        )
        self.ready = BrowserRuntimeStatus(BrowserState.HEADLESS_READY, False, "")

    def test_recycle_policy_uses_independent_count_age_and_rss_guards(self) -> None:
        cases = (
            (100, 10.0, 1, "success_count"),
            (1, 3_600.0, 1, "max_age"),
            (1, 10.0, 1_610_612_736, "max_process_rss"),
            (99, 3_599.0, 1_610_612_735, None),
        )
        for count, age_sec, rss_bytes, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    select_worker_recycle_reason(
                        self.config,
                        successful_collects=count,
                        age_sec=age_sec,
                        max_process_rss_bytes=rss_bytes,
                        status=self.ready,
                    ),
                    expected,
                )

    def test_headed_login_defers_only_planned_recycle(self) -> None:
        headed = BrowserRuntimeStatus(BrowserState.HEADED_LOGIN, True, "login_required")

        reason = select_worker_recycle_reason(
            self.config,
            successful_collects=100,
            age_sec=3_600.0,
            max_process_rss_bytes=2_000_000_000,
            status=headed,
        )

        self.assertIsNone(reason)

    def test_headed_login_deferral_has_age_and_emergency_memory_caps(self) -> None:
        headed = BrowserRuntimeStatus(BrowserState.HEADED_LOGIN, True, "login_required")

        self.assertEqual(
            select_worker_recycle_reason(
                self.config,
                successful_collects=1,
                age_sec=7_200.0,
                max_process_rss_bytes=1,
                status=headed,
            ),
            "headed_login_max_age",
        )
        self.assertEqual(
            select_worker_recycle_reason(
                self.config,
                successful_collects=1,
                age_sec=1.0,
                max_process_rss_bytes=2_147_483_648,
                status=headed,
            ),
            "headed_login_emergency_rss",
        )

    def test_hard_cancel_requires_both_worker_exit_and_empty_job(self) -> None:
        driver = CodexUsagePlaywrightProcessDriver(self.config)
        driver._process = _StoppedProcess()
        driver._connection = Mock()
        driver._job = _NonEmptyJob()
        driver._worker_pid = 1234

        terminated = driver.force_terminate(BrowserErrorCode.COMMAND_TIMEOUT.value)

        self.assertFalse(terminated)
        self.assertEqual(
            driver.get_runtime_status().last_error,
            BrowserErrorCode.COLLECT_FAILED.value,
        )

    def test_renderer_crash_signal_is_not_overwritten_by_transport_eof(self) -> None:
        driver = CodexUsagePlaywrightProcessDriver(self.config)
        driver._last_failure_signal = BrowserErrorCode.RENDERER_CRASHED.value

        result = driver._transport_failure("EOFError")

        self.assertEqual(result.error, BrowserErrorCode.RENDERER_CRASHED.value)
        self.assertEqual(
            driver.get_runtime_status().last_error,
            BrowserErrorCode.RENDERER_CRASHED.value,
        )

    def test_shutdown_never_spawns_a_replacement_if_worker_dies_mid_stop(self) -> None:
        driver = CodexUsagePlaywrightProcessDriver(self.config)
        driver._process = _DiesDuringShutdownProcess()
        driver._connection = Mock()
        driver._job = _NonEmptyJob()
        driver._worker_pid = 4321
        driver._process_generation = 7

        driver.shutdown()

        self.assertEqual(driver.process_generation, 7)
        self.assertEqual(driver.worker_pid, 0)
        self.assertEqual(
            driver.open_login().error,
            BrowserErrorCode.COLLECT_FAILED.value,
        )
        self.assertEqual(
            driver.poll_login().error,
            BrowserErrorCode.COLLECT_FAILED.value,
        )

    def test_spawn_waits_until_concurrent_job_termination_is_complete(self) -> None:
        process_context = Mock()
        driver = CodexUsagePlaywrightProcessDriver(
            self.config,
            process_context=process_context,
        )
        job = _BlockingEmptyJob()
        driver._process = _StoppedProcess()
        driver._connection = Mock()
        driver._job = job
        driver._worker_pid = 1234
        terminate_thread = threading.Thread(
            target=lambda: driver.force_terminate(
                BrowserErrorCode.COMMAND_TIMEOUT.value
            )
        )
        terminate_thread.start()
        self.assertTrue(job.wait_started.wait(1.0))
        result: list[object] = []
        start_thread = threading.Thread(target=lambda: result.append(driver.start()))
        start_thread.start()

        time.sleep(0.05)
        self.assertTrue(start_thread.is_alive())
        process_context.Pipe.assert_not_called()
        driver._shutdown = True
        job.release.set()
        terminate_thread.join(1.0)
        start_thread.join(1.0)

        self.assertFalse(terminate_thread.is_alive())
        self.assertFalse(start_thread.is_alive())
        process_context.Pipe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
