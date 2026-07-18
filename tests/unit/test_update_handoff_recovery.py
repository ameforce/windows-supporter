from __future__ import annotations

import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

from src.utils.update_monitor import (
    build_update_handoff_payload,
    read_update_handoff_state,
    run_update_handoff,
)


class BuildSubprocess:
    def __init__(self, root_executable: Path, returncode: int) -> None:
        self._root_executable = root_executable
        self._returncode = returncode

    def run(self, argv, **kwargs):
        self._root_executable.write_bytes(b"new-executable")
        return types.SimpleNamespace(
            returncode=self._returncode,
            stdout="built" if self._returncode == 0 else "",
            stderr="" if self._returncode == 0 else "failed",
        )


class PreservingFailedBuildSubprocess:
    def run(self, argv, **kwargs):
        _ = argv, kwargs
        return types.SimpleNamespace(returncode=1, stdout="", stderr="failed")


class StableProcess:
    """Fake process whose mutable wait history proves the health check ran."""

    def __init__(self) -> None:
        self.wait_timeouts: list[float] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(float(timeout or 0.0))
        raise subprocess.TimeoutExpired(cmd=["windows-supporter.exe"], timeout=timeout)


class ExitedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return self.returncode


def write_handoff_state(state_path: Path, repo: Path, previous_executable: Path) -> None:
    payload = build_update_handoff_payload(repo_root=repo, log_path=state_path.with_suffix(".log"))
    payload["recovery_executable_path"] = str(previous_executable)
    state_path.write_text(json.dumps(payload), encoding="utf-8")


class UpdateHandoffRecoveryUnitTest(unittest.TestCase):
    def test_build_failure_relaunches_same_file_without_copy_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"trusted-root")
            previous_executable = Path(tmp) / "windows-supporter-updater.exe"
            previous_executable.write_bytes(b"copy-fallback")
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, previous_executable)
            stable_process = StableProcess()
            launches = []

            rc = run_update_handoff(
                state_path,
                subprocess_module=PreservingFailedBuildSubprocess(),
                launch=lambda command, **kwargs: launches.append((list(command), dict(kwargs)))
                or stable_process,
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(root_executable.read_bytes(), b"trusted-root")
            self.assertEqual(len(launches), 1)
            self.assertEqual(state["recovery_status"], "complete")

    def test_missing_recovery_executable_reports_failure_without_launching(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"old-root")
            missing_previous_executable = Path(tmp) / "missing-updater.exe"
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, missing_previous_executable)
            launches = []

            # When
            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=1),
                launch=lambda command, **kwargs: launches.append((list(command), dict(kwargs))),
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            # Then
            self.assertEqual(rc, 1)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["recovery_status"], "failed")
            self.assertIn("missing-updater.exe", state["recovery_error"])
            self.assertEqual(launches, [])

    def test_build_failure_restores_and_relaunches_previous_executable(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"old-root")
            previous_executable = Path(tmp) / "windows-supporter-updater.exe"
            previous_executable.write_bytes(b"old-executable")
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, previous_executable)
            launches = []
            stable_process = StableProcess()

            # When
            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=1),
                launch=lambda command, **kwargs: launches.append((list(command), dict(kwargs)))
                or stable_process,
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            # Then
            self.assertEqual(rc, 1)
            self.assertEqual(root_executable.read_bytes(), b"old-executable")
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0][0], [str(root_executable)])
            self.assertEqual(launches[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertEqual(state["recovery_status"], "complete")
            self.assertTrue(stable_process.wait_timeouts)

    def test_new_executable_early_exit_is_failed_and_rolled_back(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"old-root")
            previous_executable = Path(tmp) / "windows-supporter-updater.exe"
            previous_executable.write_bytes(b"old-executable")
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, previous_executable)
            processes = [ExitedProcess(7), StableProcess()]
            launches = []

            def launch(command, **kwargs):
                launches.append((list(command), dict(kwargs)))
                return processes.pop(0)

            # When
            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=0),
                launch=launch,
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            # Then
            self.assertEqual(rc, 1)
            self.assertEqual(state["status"], "failed")
            self.assertIn("exited during startup", state["error"])
            self.assertEqual(state["recovery_status"], "complete")
            self.assertEqual(root_executable.read_bytes(), b"old-executable")
            self.assertEqual(len(launches), 2)

    def test_success_is_recorded_only_after_new_executable_stays_alive(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"old-root")
            previous_executable = Path(tmp) / "windows-supporter-updater.exe"
            previous_executable.write_bytes(b"old-executable")
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, previous_executable)
            stable_process = StableProcess()

            # When
            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=0),
                launch=lambda command, **kwargs: stable_process,
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            # Then
            self.assertEqual(rc, 0)
            self.assertEqual(state["status"], "complete")
            self.assertTrue(stable_process.wait_timeouts)
            self.assertEqual(root_executable.read_bytes(), b"new-executable")


if __name__ == "__main__":
    unittest.main()
