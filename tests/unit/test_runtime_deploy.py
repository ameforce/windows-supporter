from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path

from src.utils.runtime_deploy import (
    DeployExitCode,
    RuntimeDeployError,
    _claim_json_exclusive,
    cli,
    deploy_runtime,
    restart_runtime,
)


class FakeController:
    def __init__(
        self,
        *,
        exact_pids=(101, 102),
        fail_new=False,
        fail_all=False,
        fail_new_launch=False,
        reject_new_probe=False,
    ):
        self.exact_pids = list(exact_pids)
        self.other_same_name_pid = 999
        self.fail_new = bool(fail_new)
        self.fail_all = bool(fail_all)
        self.fail_new_launch = bool(fail_new_launch)
        self.reject_new_probe = bool(reject_new_probe)
        self.terminated = []
        self.launches = []
        self.probe_path: Path | None = None
        self.token = ""
        self.probe_pid = 0
        self.tick = 0
        self.launcher_pid = 200

    def find_exact(self, executable_path: Path):
        self.target = Path(executable_path)
        return list(self.exact_pids)

    def terminate_tree(self, pids, timeout_seconds):
        self.terminated.extend(int(pid) for pid in pids)
        return list(pids)

    def launch(self, executable_path: Path, *, env, cwd: Path):
        if self.fail_new_launch and not self.launches:
            self.launches.append((Path(executable_path), dict(env), Path(cwd)))
            raise OSError("launch denied")
        self.launcher_pid += 100
        self.launches.append((Path(executable_path), dict(env), Path(cwd)))
        self.probe_path = Path(env["WINDOWS_SUPPORTER_RUNTIME_PROBE_PATH"])
        self.token = env["WINDOWS_SUPPORTER_RUNTIME_PROBE_TOKEN"]
        self.probe_pid = self.launcher_pid + 1
        self.tick = 1
        is_new = len(self.launches) == 1
        if not self.fail_all and not (is_new and self.fail_new):
            self._write_probe()
        return self.launcher_pid

    def validate_probe_process(self, payload, *, launcher_pid, executable_path):
        if self.reject_new_probe and len(self.launches) == 1:
            raise RuntimeError("stale pid reuse")
        if int(payload["pid"]) != self.probe_pid:
            raise RuntimeError("unexpected pid")
        if int(launcher_pid) != self.launcher_pid:
            raise RuntimeError("unexpected launcher")
        if os.path.normcase(payload["executable_path"]) != os.path.normcase(str(executable_path)):
            raise RuntimeError("wrong executable")

    def window_owner_pid(self, hwnd):
        return self.probe_pid

    def startup_executable(self):
        return self.target

    def sleep(self, _seconds):
        if self.probe_path is not None and self.probe_path.exists():
            self.tick += 1
            self._write_probe()

    def _write_probe(self):
        assert self.probe_path is not None
        self.probe_path.parent.mkdir(parents=True, exist_ok=True)
        self.probe_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "token": self.token,
                    "state": "ready",
                    "pid": self.probe_pid,
                    "process_start_time": "2026-09-04T00:00:00+00:00",
                    "executable_path": str(self.target),
                    "version": "0.18.18.0",
                    "commit": "abc1234",
                    "tray_hwnd": 777,
                    "mainloop_tick": self.tick,
                    "timestamp": "2026-09-04T00:00:00+00:00",
                    "error": None,
                }
            ),
            encoding="utf-8",
        )


class RuntimeDeployTest(unittest.TestCase):
    def _paths(self, root: str):
        base = Path(root)
        target = base / "installed" / "windows-supporter.exe"
        candidate = base / "dist" / "windows-supporter.exe"
        target.parent.mkdir()
        candidate.parent.mkdir()
        target.write_bytes(b"old-runtime")
        candidate.write_bytes(b"new-runtime")
        return candidate, target

    def test_success_promotes_exact_candidate_and_observes_three_heartbeats(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            controller = FakeController()

            receipt = deploy_runtime(
                candidate,
                target,
                controller=controller,
                probe_path=Path(root) / "probe.json",
                token_factory=lambda: "new-token",
                timeout_seconds=1,
                heartbeat_samples=3,
                poll_interval=0.01,
                sleep=controller.sleep,
            )

            self.assertEqual(receipt["status"], "success")
            self.assertEqual(receipt["readiness"]["heartbeat_samples"], 3)
            self.assertEqual(receipt["readiness"]["startup_path"], str(target))
            self.assertEqual(len(receipt["readiness"]["probe_token_sha256"]), 64)
            self.assertEqual(target.read_bytes(), b"new-runtime")
            self.assertEqual(controller.terminated, [101, 102])
            self.assertNotIn(controller.other_same_name_pid, controller.terminated)
            self.assertFalse(target.with_name("windows-supporter.previous.exe").exists())

    def test_stale_probe_and_pid_reuse_are_rejected_then_old_runtime_is_restored(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            probe = Path(root) / "probe.json"
            probe.write_text('{"token":"stale","state":"ready","pid":101}', encoding="utf-8")
            controller = FakeController(reject_new_probe=True)

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    controller=controller,
                    probe_path=probe,
                    token_factory=iter(("new-token", "rollback-token")).__next__,
                    timeout_seconds=0.05,
                    heartbeat_samples=1,
                    poll_interval=0.01,
                    sleep=controller.sleep,
                )

            self.assertEqual(raised.exception.receipt["rollback"]["status"], "ready")
            self.assertEqual(target.read_bytes(), b"old-runtime")
            self.assertEqual(len(controller.launches), 2)

    def test_readiness_timeout_rolls_back_and_returns_nonzero_failure_class(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            controller = FakeController(fail_new=True)

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    controller=controller,
                    probe_path=Path(root) / "probe.json",
                    token_factory=iter(("new-token", "rollback-token")).__next__,
                    timeout_seconds=0.02,
                    heartbeat_samples=1,
                    poll_interval=0.01,
                    sleep=controller.sleep,
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.READINESS_FAILED)
            self.assertEqual(raised.exception.receipt["rollback"]["status"], "ready")
            self.assertEqual(target.read_bytes(), b"old-runtime")

    def test_replace_failure_restarts_preserved_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            controller = FakeController()
            calls = []

            def replace_file(source, destination):
                calls.append((Path(source), Path(destination)))
                if Path(destination) == target and ".candidate-" in Path(source).name:
                    raise OSError("replace denied")
                os.replace(source, destination)

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    controller=controller,
                    probe_path=Path(root) / "probe.json",
                    token_factory=lambda: "rollback-token",
                    timeout_seconds=0.05,
                    heartbeat_samples=1,
                    poll_interval=0.01,
                    sleep=controller.sleep,
                    replace_file=replace_file,
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.REPLACE_FAILED)
            self.assertEqual(raised.exception.receipt["rollback"]["status"], "ready")
            self.assertEqual(target.read_bytes(), b"old-runtime")

    def test_backup_preparation_failure_leaves_running_runtime_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            controller = FakeController()

            def fail_backup_copy(source, destination):
                if ".backup-" in Path(destination).name:
                    raise OSError("disk full")
                return shutil.copy2(source, destination)

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    controller=controller,
                    copy_file=fail_backup_copy,
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.REPLACE_FAILED)
            self.assertEqual(
                raised.exception.receipt["rollback"]["status"], "target-unchanged"
            )
            self.assertEqual(target.read_bytes(), b"old-runtime")
            self.assertEqual(controller.terminated, [])
            self.assertEqual(controller.launches, [])
            self.assertFalse(target.with_name("windows-supporter.previous.exe").exists())
            self.assertFalse(
                target.with_name("windows-supporter.promotion-pending.json").exists()
            )

    def test_transaction_claim_is_exclusive_and_preserves_first_owner(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "promotion-pending.json"
            first = {"owner": "first"}
            _claim_json_exclusive(marker, first)

            with self.assertRaises(FileExistsError):
                _claim_json_exclusive(marker, {"owner": "second"})

            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), first)

    def test_restart_preserves_existing_deployment_transaction(self):
        with tempfile.TemporaryDirectory() as root:
            _candidate, target = self._paths(root)
            marker = target.with_name("windows-supporter.promotion-pending.json")
            marker.write_text('{"owner":"deploy"}', encoding="utf-8")
            controller = FakeController()

            with self.assertRaises(RuntimeDeployError) as raised:
                restart_runtime(
                    target,
                    controller=controller,
                    timeout_seconds=0.05,
                    heartbeat_samples=1,
                    poll_interval=0.01,
                    sleep=controller.sleep,
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.INVALID_INPUT)
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"owner": "deploy"})
            self.assertEqual(controller.terminated, [])
            self.assertEqual(controller.launches, [])

    def test_transaction_claim_race_is_reported_as_preserved_conflict(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            marker = target.with_name("windows-supporter.promotion-pending.json")
            controller = FakeController()

            def lose_claim(path, _payload):
                path.write_text('{"owner":"first"}', encoding="utf-8")
                raise FileExistsError(path)

            with patch(
                "src.utils.runtime_deploy._claim_json_exclusive",
                side_effect=lose_claim,
            ), self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(candidate, target, controller=controller)

            receipt = raised.exception.receipt
            self.assertEqual(raised.exception.exit_code, DeployExitCode.INVALID_INPUT)
            self.assertTrue(receipt["target_unchanged"])
            self.assertTrue(receipt["transaction_conflict"])
            self.assertEqual(receipt["rollback"]["status"], "target-unchanged")
            self.assertEqual(receipt["preserved_transaction"]["marker"], str(marker))
            self.assertEqual(target.read_bytes(), b"old-runtime")
            self.assertEqual(controller.terminated, [])
            self.assertEqual(controller.launches, [])

    def test_fresh_install_promotes_candidate_without_previous_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            candidate = base / "dist" / "windows-supporter.exe"
            target = base / "installed" / "windows-supporter.exe"
            candidate.parent.mkdir()
            target.parent.mkdir()
            candidate.write_bytes(b"new-runtime")
            controller = FakeController(exact_pids=())

            receipt = deploy_runtime(
                candidate,
                target,
                controller=controller,
                probe_path=base / "probe.json",
                token_factory=lambda: "fresh-token",
                timeout_seconds=0.05,
                heartbeat_samples=1,
                poll_interval=0.01,
                sleep=controller.sleep,
            )

            self.assertFalse(receipt["had_previous"])
            self.assertIsNone(receipt["previous_sha256"])
            self.assertEqual(target.read_bytes(), b"new-runtime")

    def test_failed_fresh_install_restores_absent_target(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            candidate = base / "dist" / "windows-supporter.exe"
            target = base / "installed" / "windows-supporter.exe"
            candidate.parent.mkdir()
            target.parent.mkdir()
            candidate.write_bytes(b"new-runtime")
            controller = FakeController(exact_pids=(), fail_new=True)

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    controller=controller,
                    probe_path=base / "probe.json",
                    token_factory=lambda: "fresh-token",
                    timeout_seconds=0.02,
                    heartbeat_samples=1,
                    poll_interval=0.01,
                    sleep=controller.sleep,
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.READINESS_FAILED)
            self.assertEqual(
                raised.exception.receipt["rollback"]["status"], "restored-absent"
            )
            self.assertFalse(target.exists())

    def test_launch_failure_has_distinct_code_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            controller = FakeController(fail_new_launch=True)

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    controller=controller,
                    probe_path=Path(root) / "probe.json",
                    token_factory=iter(("new-token", "rollback-token")).__next__,
                    timeout_seconds=0.05,
                    heartbeat_samples=1,
                    poll_interval=0.01,
                    sleep=controller.sleep,
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.LAUNCH_FAILED)
            self.assertEqual(raised.exception.receipt["rollback"]["status"], "ready")
            self.assertEqual(target.read_bytes(), b"old-runtime")

    def test_existing_transaction_artifacts_are_preserved_without_runtime_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            marker = target.with_name("windows-supporter.promotion-pending.json")
            backup = target.with_name("windows-supporter.previous.exe")
            marker.write_text('{"owner":"unknown"}', encoding="utf-8")
            backup.write_bytes(b"unknown-backup")
            controller = FakeController()

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(candidate, target, controller=controller)

            self.assertEqual(raised.exception.exit_code, DeployExitCode.INVALID_INPUT)
            self.assertTrue(marker.exists())
            self.assertEqual(backup.read_bytes(), b"unknown-backup")
            self.assertEqual(target.read_bytes(), b"old-runtime")
            self.assertEqual(controller.terminated, [])
            self.assertEqual(controller.launches, [])

    def test_failed_rollback_returns_dedicated_failure_code_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            controller = FakeController(fail_all=True)

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    controller=controller,
                    probe_path=Path(root) / "probe.json",
                    token_factory=iter(("new-token", "rollback-token")).__next__,
                    timeout_seconds=0.02,
                    heartbeat_samples=1,
                    poll_interval=0.01,
                    sleep=controller.sleep,
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.ROLLBACK_FAILED)
            self.assertEqual(raised.exception.receipt["rollback"]["status"], "failed")
            self.assertEqual(target.read_bytes(), b"old-runtime")
            self.assertTrue(target.with_name("windows-supporter.previous.exe").exists())
            self.assertTrue(
                target.with_name("windows-supporter.promotion-pending.json").exists()
            )

    def test_failed_restore_copy_reports_changed_target(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)
            controller = FakeController(fail_new=True)

            def fail_restore_copy(source, destination):
                if ".restore-" in Path(destination).name:
                    raise OSError("restore copy denied")
                return shutil.copy2(source, destination)

            with self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    controller=controller,
                    probe_path=Path(root) / "probe.json",
                    token_factory=lambda: "new-token",
                    timeout_seconds=0.02,
                    heartbeat_samples=1,
                    poll_interval=0.01,
                    sleep=controller.sleep,
                    copy_file=fail_restore_copy,
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.ROLLBACK_FAILED)
            self.assertEqual(raised.exception.receipt["rollback"]["status"], "failed")
            self.assertFalse(raised.exception.receipt["target_unchanged"])
            self.assertEqual(target.read_bytes(), b"new-runtime")

    def test_cli_invalid_arguments_emit_json_failure_receipt(self):
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli([])

        self.assertEqual(result, DeployExitCode.INVALID_INPUT)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertIn("required", payload["error"])
        self.assertIn("argument error", stderr.getvalue())

    def test_candidate_metadata_mismatch_is_rejected_before_process_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            candidate, target = self._paths(root)

            with patch(
                "src.utils.runtime_deploy._read_windows_artifact_identity",
                return_value=("0.18.18.0", "abc1234"),
            ), self.assertRaises(RuntimeDeployError) as raised:
                deploy_runtime(
                    candidate,
                    target,
                    expected_version="0.18.17.0",
                )

            self.assertEqual(raised.exception.exit_code, DeployExitCode.INVALID_INPUT)
            self.assertTrue(raised.exception.receipt["target_unchanged"])


if __name__ == "__main__":
    unittest.main()
