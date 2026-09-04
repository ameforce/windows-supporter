from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from src.utils.runtime_deploy import DeployExitCode, RuntimeDeployError
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
        candidate = self._root_executable.parent / "dist" / "windows-supporter.exe"
        candidate.parent.mkdir(exist_ok=True)
        candidate.write_bytes(b"new-executable")
        self.last_env = dict(kwargs.get("env") or {})
        return types.SimpleNamespace(
            returncode=self._returncode,
            stdout="built" if self._returncode == 0 else "",
            stderr="" if self._returncode == 0 else "failed",
        )


class PreservingFailedBuildSubprocess:
    def run(self, argv, **kwargs):
        _ = argv, kwargs
        return types.SimpleNamespace(returncode=1, stdout="", stderr="failed")


class UnexpectedlyMutatingFailedBuildSubprocess:
    def __init__(self, root_executable: Path) -> None:
        self._root_executable = root_executable

    def run(self, argv, **kwargs):
        _ = argv, kwargs
        self._root_executable.write_bytes(b"unexpected-root-change")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="failed")


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
            restarts = []

            rc = run_update_handoff(
                state_path,
                subprocess_module=PreservingFailedBuildSubprocess(),
                runtime_restarter=lambda target, **kwargs: restarts.append(
                    (Path(target), dict(kwargs))
                ) or {"status": "success"},
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(root_executable.read_bytes(), b"trusted-root")
            self.assertEqual(len(restarts), 1)
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
            restarts = []

            # When
            rc = run_update_handoff(
                state_path,
                subprocess_module=UnexpectedlyMutatingFailedBuildSubprocess(root_executable),
                runtime_restarter=lambda target, **kwargs: restarts.append(
                    (Path(target), dict(kwargs))
                ),
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            # Then
            self.assertEqual(rc, 1)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["recovery_status"], "failed")
            self.assertIn("missing-updater.exe", state["recovery_error"])
            self.assertEqual(restarts, [])

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
            restarts = []

            # When
            rc = run_update_handoff(
                state_path,
                subprocess_module=UnexpectedlyMutatingFailedBuildSubprocess(root_executable),
                runtime_restarter=lambda target, **kwargs: restarts.append(
                    (Path(target), dict(kwargs))
                ) or {"status": "success"},
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            # Then
            self.assertEqual(rc, 1)
            self.assertEqual(root_executable.read_bytes(), b"old-executable")
            self.assertEqual(len(restarts), 1)
            self.assertTrue(restarts[0][0].samefile(root_executable))
            self.assertEqual(restarts[0][1]["base_environment"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertEqual(state["recovery_status"], "complete")

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
            deploy_calls = []

            def failed_deploy(candidate, target, **kwargs):
                deploy_calls.append((Path(candidate), Path(target), dict(kwargs)))
                raise RuntimeDeployError(
                    "exited during startup",
                    exit_code=DeployExitCode.READINESS_FAILED,
                    receipt={"rollback": {"status": "ready", "readiness": {"pid": 500}}},
                )

            # When
            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=0),
                runtime_deployer=failed_deploy,
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            # Then
            self.assertEqual(rc, 1)
            self.assertEqual(state["status"], "failed")
            self.assertIn("exited during startup", state["error"])
            self.assertEqual(state["recovery_status"], "complete")
            self.assertEqual(root_executable.read_bytes(), b"old-root")
            self.assertEqual(len(deploy_calls), 1)

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
            deploy_calls = []

            def successful_deploy(candidate, target, **kwargs):
                deploy_calls.append((Path(candidate), Path(target), dict(kwargs)))
                shutil.copy2(candidate, target)
                return {"schema_version": 1, "status": "success", "readiness": {"pid": 600}}

            # When
            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=0),
                runtime_deployer=successful_deploy,
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            # Then
            self.assertEqual(rc, 0)
            self.assertEqual(state["status"], "complete")
            self.assertEqual(root_executable.read_bytes(), b"new-executable")
            self.assertEqual(len(deploy_calls), 1)
            self.assertTrue(deploy_calls[0][0].samefile(repo / "dist" / "windows-supporter.exe"))
            self.assertEqual(
                deploy_calls[0][2]["base_environment"]["PYINSTALLER_RESET_ENVIRONMENT"],
                "1",
            )


if __name__ == "__main__":
    unittest.main()
