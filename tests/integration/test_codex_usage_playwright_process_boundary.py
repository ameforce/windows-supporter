from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

from src.apps.codex_usage_browser_types import (
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    LogSink,
    PlaywrightSessionConfig,
    PlaywrightStarter,
)
from src.apps.codex_usage_playwright_process import (
    CodexUsagePlaywrightProcessDriver,
)
from src.apps.codex_usage_playwright_session import CodexUsagePlaywrightSession


def _send_result(connection, request_id: int, result: BrowserOperationResult) -> None:
    status = BrowserRuntimeStatus(
        BrowserState.HEADLESS_READY if result.error is None else BrowserState.FAILED,
        False,
        result.error or "",
    )
    connection.send(("result", request_id, result, status))


def _scenario_worker(connection) -> None:
    bootstrap = connection.recv()
    config = bootstrap[1]
    generation = int(bootstrap[2])
    profile_dir = Path(config.profile_dir)
    scenario = (profile_dir / "scenario.txt").read_text(encoding="utf-8").strip()
    connection.send(("ready", os.getpid(), generation))
    while True:
        request = connection.recv()
        request_id = int(request[1])
        command = str(request[2])
        if command == "start":
            _send_result(connection, request_id, BrowserOperationResult())
            continue
        if scenario == "hang_then_success" and command == "collect":
            marker = profile_dir / "first-hang-seen"
            if not marker.exists():
                marker.write_text("1", encoding="utf-8")
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                (profile_dir / "grandchild.pid").write_text(
                    str(child.pid), encoding="utf-8"
                )
                while True:
                    time.sleep(60)
            _send_result(
                connection,
                request_id,
                BrowserOperationResult(
                    probe={"url": "usage", "metricBlocks": []}
                ),
            )
            continue
        if scenario == "close_hang" and command == "close_session":
            while True:
                time.sleep(60)
        if scenario == "stop_hang" and command == "shutdown":
            while True:
                time.sleep(60)
        if scenario == "transport_hang" and command == "collect":
            while True:
                time.sleep(60)
        if scenario == "always_success" and command == "collect":
            _send_result(
                connection,
                request_id,
                BrowserOperationResult(
                    probe={"url": "usage", "metricBlocks": []}
                ),
            )
            continue
        _send_result(connection, request_id, BrowserOperationResult())
        if command == "shutdown":
            return


class _ProcessDriverFactory:
    def __init__(self, logs: list[str]) -> None:
        self.logs = logs

    def __call__(
        self,
        config: PlaywrightSessionConfig,
        log_sink: LogSink | None,
        _playwright_starter: PlaywrightStarter | None,
    ) -> CodexUsagePlaywrightProcessDriver:
        sink = log_sink or self.logs.append
        return CodexUsagePlaywrightProcessDriver(
            config,
            sink,
            worker_target=_scenario_worker,
        )


def _wait_pid_gone(pid: int, timeout_sec: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return not psutil.pid_exists(pid)


class CodexUsagePlaywrightProcessBoundaryIntegrationTest(unittest.TestCase):
    def _config(self, profile_dir: str) -> PlaywrightSessionConfig:
        return PlaywrightSessionConfig(
            profile_dir=profile_dir,
            usage_url="https://example.invalid/usage",
            probe_script="probe()",
            command_timeout_sec=0.5,
            collect_timeout_sec=0.5,
            timeout_retry_delays_sec=(0.0,),
            timeout_recovery_grace_sec=1.0,
            worker_cleanup_timeout_sec=0.2,
            worker_bootstrap_timeout_sec=5.0,
        )

    def test_hung_transport_kills_worker_tree_and_retry_recovers_in_same_app(self) -> None:
        with tempfile.TemporaryDirectory() as profile_dir:
            root = Path(profile_dir)
            (root / "scenario.txt").write_text(
                "hang_then_success", encoding="utf-8"
            )
            logs: list[str] = []
            session = CodexUsagePlaywrightSession(
                self._config(profile_dir),
                logs.append,
                driver_factory=_ProcessDriverFactory(logs),
            )
            try:
                result = session.collect()
                grandchild_pid = int(
                    (root / "grandchild.pid").read_text(encoding="utf-8")
                )

                self.assertIsNotNone(result.probe)
                self.assertTrue(
                    _wait_pid_gone(grandchild_pid),
                    "Job termination must remove the worker's descendant too",
                )
                self.assertTrue(
                    any("hard cancel" in line for line in logs),
                    logs,
                )
                termination_end = next(
                    index
                    for index, line in enumerate(logs)
                    if "browser worker terminate end" in line
                    and "job_empty=true" in line
                )
                second_spawn = [
                    index
                    for index, line in enumerate(logs)
                    if "browser worker spawned" in line
                ][1]
                self.assertLess(
                    termination_end,
                    second_spawn,
                    "same-profile retry must start only after old Job is empty",
                )
            finally:
                session.shutdown()

    def test_success_count_proactively_recycles_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as profile_dir:
            Path(profile_dir, "scenario.txt").write_text(
                "always_success", encoding="utf-8"
            )
            logs: list[str] = []
            config = self._config(profile_dir)
            config = PlaywrightSessionConfig(
                profile_dir=config.profile_dir,
                usage_url=config.usage_url,
                probe_script=config.probe_script,
                command_timeout_sec=config.command_timeout_sec,
                collect_timeout_sec=config.collect_timeout_sec,
                timeout_retry_delays_sec=config.timeout_retry_delays_sec,
                timeout_recovery_grace_sec=config.timeout_recovery_grace_sec,
                worker_cleanup_timeout_sec=config.worker_cleanup_timeout_sec,
                worker_bootstrap_timeout_sec=config.worker_bootstrap_timeout_sec,
                worker_recycle_success_count=1,
                worker_recycle_max_age_sec=60.0,
                worker_recycle_max_process_rss_bytes=10_000_000_000,
            )
            driver = CodexUsagePlaywrightProcessDriver(
                config,
                logs.append,
                worker_target=_scenario_worker,
            )
            try:
                self.assertIsNone(driver.start().error)
                self.assertIsNotNone(driver.collect().probe)
                self.assertIsNotNone(driver.collect().probe)

                self.assertEqual(driver.process_generation, 2)
                self.assertTrue(
                    any("reason=success_count" in line for line in logs),
                    logs,
                )
            finally:
                driver.shutdown()

    def test_context_close_and_playwright_stop_hangs_are_bounded(self) -> None:
        for scenario, operation in (
            ("close_hang", "close_session"),
            ("stop_hang", "shutdown"),
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as profile_dir:
                Path(profile_dir, "scenario.txt").write_text(
                    scenario, encoding="utf-8"
                )
                driver = CodexUsagePlaywrightProcessDriver(
                    self._config(profile_dir),
                    worker_target=_scenario_worker,
                )
                self.assertIsNone(driver.start().error)
                started_at = time.monotonic()

                getattr(driver, operation)()

                self.assertLess(time.monotonic() - started_at, 2.0)
                self.assertEqual(driver.worker_pid, 0)


if __name__ == "__main__":
    unittest.main()
