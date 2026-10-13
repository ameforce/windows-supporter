from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.utils.runtime_deploy import DeployExitCode, RuntimeDeployError, deploy_runtime
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


class UnexpectedlyMutatingSuccessfulBuildSubprocess:
    def __init__(self, root_executable: Path) -> None:
        self._root_executable = root_executable

    def run(self, argv, **kwargs):
        _ = argv, kwargs
        self._root_executable.write_bytes(b"unverified-root")
        candidate = self._root_executable.parent / "dist" / "windows-supporter.exe"
        candidate.parent.mkdir(exist_ok=True)
        candidate.write_bytes(b"new-executable")
        return types.SimpleNamespace(returncode=0, stdout="built", stderr="")


class ConcurrentDeploymentBuildSubprocess:
    def __init__(self, root_executable: Path) -> None:
        self._root_executable = root_executable

    def run(self, argv, **kwargs):
        _ = argv, kwargs
        self._root_executable.write_bytes(b"owner-new-runtime")
        marker = self._root_executable.with_name(
            "windows-supporter.promotion-pending.json"
        )
        marker.write_text('{"owner":"deploy"}', encoding="utf-8")
        candidate = self._root_executable.parent / "dist" / "windows-supporter.exe"
        candidate.parent.mkdir(exist_ok=True)
        candidate.write_bytes(b"new-executable")
        return types.SimpleNamespace(returncode=0, stdout="built", stderr="")


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
                runtime_restarter=lambda target, **kwargs: (
                    shutil.copy2(kwargs["restore_source"], target),
                    restarts.append((Path(target), dict(kwargs))),
                    {"status": "success"},
                )[-1],
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
            build_sentinel = repo / "build" / "existing-intermediate.bin"
            dist_sentinel = repo / "dist" / "existing-candidate.bin"
            spec_sentinel = repo / "windows-supporter.spec"
            for sentinel in (build_sentinel, dist_sentinel, spec_sentinel):
                sentinel.parent.mkdir(exist_ok=True)
                sentinel.write_bytes(b"existing-owned-elsewhere")
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
            self.assertEqual(
                deploy_calls[0][0],
                (repo / "dist" / "windows-supporter.exe").resolve(),
            )
            self.assertEqual(
                deploy_calls[0][2]["base_environment"]["PYINSTALLER_RESET_ENVIRONMENT"],
                "1",
            )
            self.assertEqual(
                (repo / "dist" / "windows-supporter.exe").read_bytes(),
                b"new-executable",
            )
            for sentinel in (build_sentinel, dist_sentinel, spec_sentinel):
                self.assertEqual(sentinel.read_bytes(), b"existing-owned-elsewhere")

    def test_success_does_not_delete_build_outputs_or_request_cleanup_retry(self) -> None:
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
            deploy_calls = []
            progress_ui = Mock()
            progress_ui.wait_for_retry_or_close.return_value = True
            build_subprocess = BuildSubprocess(root_executable, returncode=0)

            def successful_deploy(candidate, target, **_kwargs):
                deploy_calls.append((Path(candidate), Path(target)))
                shutil.copy2(candidate, target)
                return {"status": "success", "readiness": {"pid": 600}}

            with (
                patch.object(
                    build_subprocess, "run", wraps=build_subprocess.run
                ) as build_run,
                patch(
                    "src.utils.update_monitor.shutil.rmtree",
                    side_effect=OSError("cleanup denied"),
                ) as remove_tree,
                patch(
                    "src.utils.update_monitor.Path.unlink",
                    side_effect=OSError("cleanup denied"),
                ) as unlink,
            ):
                rc = run_update_handoff(
                    state_path,
                    subprocess_module=build_subprocess,
                    runtime_deployer=successful_deploy,
                    runtime_restarter=lambda *_args, **_kwargs: restarts.append(True),
                    progress_ui_factory=lambda **_kwargs: progress_ui,
                    max_attempts=2,
                )
                build_run.assert_called_once()
                remove_tree.assert_not_called()
                unlink.assert_not_called()
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 0)
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["attempt"], 1)
            self.assertEqual(root_executable.read_bytes(), b"new-executable")
            self.assertEqual(len(deploy_calls), 1)
            self.assertEqual(restarts, [])
            progress_ui.wait_for_retry_or_close.assert_not_called()
            progress_ui.close.assert_called_once()
            self.assertNotIn(
                "cleanup_build",
                [call.args[0]["step_key"] for call in progress_ui.set_snapshot.call_args_list],
            )

    def test_completion_progress_failure_keeps_verified_new_runtime_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"old-root")
            previous_executable = Path(tmp) / "windows-supporter-updater.exe"
            previous_executable.write_bytes(b"old-executable")
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, previous_executable)
            progress_ui = Mock()
            completion_errors = []
            runtime_restarter = Mock()

            def publish_progress(snapshot):
                if snapshot["step_key"] == "complete" and not completion_errors:
                    completion_errors.append("completion progress failed")
                    raise OSError(completion_errors[0])

            def successful_deploy(candidate, target, **_kwargs):
                shutil.copy2(candidate, target)
                return {"status": "success", "readiness": {"pid": 600}}

            progress_ui.set_snapshot.side_effect = publish_progress
            runtime_deployer = Mock(side_effect=successful_deploy)
            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=0),
                runtime_deployer=runtime_deployer,
                runtime_restarter=runtime_restarter,
                progress_ui_factory=lambda **_kwargs: progress_ui,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            self.assertEqual(completion_errors, ["completion progress failed"])
            self.assertEqual(rc, 1)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["recovery_status"], "complete")
            self.assertEqual(state["recovery_receipt"]["status"], "new-runtime-ready")
            self.assertEqual(state["recovery_receipt"]["deployment"]["status"], "success")
            self.assertEqual(root_executable.read_bytes(), b"new-executable")
            self.assertEqual(
                (repo / "dist" / "windows-supporter.exe").read_bytes(),
                b"new-executable",
            )
            runtime_deployer.assert_called_once()
            runtime_restarter.assert_not_called()

    def test_unchanged_target_deploy_failure_restarts_previous_runtime(self) -> None:
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

            def failed_deploy(*_args, **_kwargs):
                raise RuntimeDeployError(
                    "backup preparation failed",
                    exit_code=DeployExitCode.REPLACE_FAILED,
                    receipt={
                        "target_unchanged": True,
                        "recovery_action": "restart-unchanged-runtime",
                        "rollback": {"status": "target-unchanged"},
                    },
                )

            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=0),
                runtime_deployer=failed_deploy,
                runtime_restarter=lambda target, **kwargs: restarts.append(
                    (Path(target), dict(kwargs))
                )
                or {"status": "success"},
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(len(restarts), 1)
            self.assertEqual(state["recovery_status"], "complete")

    def test_candidate_metadata_preflight_failure_restarts_unchanged_runtime(self) -> None:
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

            with patch(
                "src.utils.runtime_deploy._read_windows_artifact_identity",
                side_effect=RuntimeError("invalid version resource"),
            ):
                rc = run_update_handoff(
                    state_path,
                    subprocess_module=BuildSubprocess(root_executable, returncode=0),
                    runtime_deployer=deploy_runtime,
                    runtime_restarter=lambda target, **kwargs: restarts.append(
                        (Path(target), dict(kwargs))
                    )
                    or {"status": "success"},
                    progress_ui_factory=None,
                    max_attempts=1,
                )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(root_executable.read_bytes(), b"old-root")
            self.assertEqual(len(restarts), 1)
            self.assertEqual(state["recovery_status"], "complete")

    def test_candidate_metadata_failure_preserves_existing_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"old-root")
            marker = repo / "windows-supporter.promotion-pending.json"
            marker.write_text('{"owner":"deploy"}', encoding="utf-8")
            previous_executable = Path(tmp) / "windows-supporter-updater.exe"
            previous_executable.write_bytes(b"old-executable")
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, previous_executable)
            restarts = []

            with patch(
                "src.utils.runtime_deploy._read_windows_artifact_identity",
                side_effect=RuntimeError("invalid version resource"),
            ):
                rc = run_update_handoff(
                    state_path,
                    subprocess_module=BuildSubprocess(root_executable, returncode=0),
                    runtime_deployer=deploy_runtime,
                    runtime_restarter=lambda *_args, **_kwargs: restarts.append(True),
                    progress_ui_factory=None,
                    max_attempts=1,
                )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(restarts, [])
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"owner": "deploy"})
            self.assertEqual(state["recovery_status"], "failed")

    def test_successful_build_cannot_change_installed_runtime_before_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"verified-old-root")
            previous_executable = Path(tmp) / "windows-supporter-updater.exe"
            previous_executable.write_bytes(b"verified-old-root")
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, previous_executable)
            deploy_calls = []
            restarts = []

            rc = run_update_handoff(
                state_path,
                subprocess_module=UnexpectedlyMutatingSuccessfulBuildSubprocess(
                    root_executable
                ),
                runtime_deployer=lambda *_args, **_kwargs: deploy_calls.append(True),
                runtime_restarter=lambda target, **kwargs: (
                    shutil.copy2(kwargs["restore_source"], target),
                    restarts.append((Path(target), dict(kwargs))),
                    {"status": "success"},
                )[-1],
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(deploy_calls, [])
            self.assertEqual(root_executable.read_bytes(), b"verified-old-root")
            self.assertEqual(len(restarts), 1)
            self.assertEqual(state["recovery_status"], "complete")

    def test_concurrent_deployment_marker_prevents_restore_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            root_executable = repo / "windows-supporter.exe"
            root_executable.write_bytes(b"verified-old-root")
            marker = repo / "windows-supporter.promotion-pending.json"
            previous_executable = Path(tmp) / "windows-supporter-updater.exe"
            previous_executable.write_bytes(b"verified-old-root")
            state_path = Path(tmp) / "update_handoff.json"
            write_handoff_state(state_path, repo, previous_executable)
            deploy_calls = []
            restart_calls = []

            def refuse_restart(target, **kwargs):
                restart_calls.append((Path(target), dict(kwargs)))
                raise RuntimeDeployError(
                    "another deployment owns the transaction",
                    exit_code=DeployExitCode.INVALID_INPUT,
                    receipt={"transaction_conflict": True},
                )

            rc = run_update_handoff(
                state_path,
                subprocess_module=ConcurrentDeploymentBuildSubprocess(root_executable),
                runtime_deployer=lambda *_args, **_kwargs: deploy_calls.append(True),
                runtime_restarter=refuse_restart,
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(deploy_calls, [])
            self.assertEqual(len(restart_calls), 1)
            self.assertEqual(root_executable.read_bytes(), b"owner-new-runtime")
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"owner": "deploy"})
            self.assertEqual(state["recovery_status"], "failed")

    def test_transaction_claim_loser_does_not_restart_runtime(self) -> None:
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

            def failed_deploy(*_args, **_kwargs):
                raise RuntimeDeployError(
                    "another deployment owns the transaction",
                    exit_code=DeployExitCode.INVALID_INPUT,
                    receipt={
                        "target_unchanged": True,
                        "transaction_conflict": True,
                        "preserved_transaction": {
                            "marker": str(repo / "windows-supporter.promotion-pending.json"),
                            "backup": None,
                        },
                        "rollback": {"status": "target-unchanged"},
                    },
                )

            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=0),
                runtime_deployer=failed_deploy,
                runtime_restarter=lambda *_args, **_kwargs: restarts.append(True),
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(restarts, [])
            self.assertEqual(state["recovery_status"], "failed")

    def test_failed_rollback_never_restarts_unverified_target(self) -> None:
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

            def failed_deploy(*_args, **_kwargs):
                root_executable.write_bytes(b"new-unverified")
                raise RuntimeDeployError(
                    "rollback failed",
                    exit_code=DeployExitCode.ROLLBACK_FAILED,
                    receipt={
                        "target_unchanged": True,
                        "rollback": {"status": "failed", "error": "restore copy denied"},
                    },
                )

            rc = run_update_handoff(
                state_path,
                subprocess_module=BuildSubprocess(root_executable, returncode=0),
                runtime_deployer=failed_deploy,
                runtime_restarter=lambda *_args, **_kwargs: restarts.append(True),
                progress_ui_factory=None,
                max_attempts=1,
            )
            state = read_update_handoff_state(state_path)

            self.assertEqual(rc, 1)
            self.assertEqual(restarts, [])
            self.assertEqual(root_executable.read_bytes(), b"new-unverified")
            self.assertEqual(state["recovery_status"], "failed")


if __name__ == "__main__":
    unittest.main()
