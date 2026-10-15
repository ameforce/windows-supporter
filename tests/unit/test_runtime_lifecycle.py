from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.runtime_lifecycle import (
    DEFAULT_LOG_BACKUPS,
    DEFAULT_MAX_LOG_BYTES,
    PROBE_PATH_ENV,
    PROBE_TOKEN_ENV,
    RuntimeLifecycle,
    RuntimeProbeError,
    is_main_runtime_invocation,
    rotate_log_file,
)


class _FakeRoot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []

    def after(self, delay_ms, callback):
        self.calls.append((int(delay_ms), callback))
        return f"after-{len(self.calls)}"


class RuntimeLifecycleTest(unittest.TestCase):
    def test_special_process_modes_do_not_claim_main_runtime(self) -> None:
        for args in (
            ["app.exe", "--google-calendar-resource-smoke"],
            ["app.exe", "--codex-usage-worker-smoke"],
            ["app.exe", "--windows-supporter-update-handoff", "state.json"],
            ["app.exe", "--lid-power-watchdog"],
            ["app.exe", "--lid-power-runtime-canary"],
            ["app.exe", "--multiprocessing-fork"],
        ):
            with self.subTest(args=args):
                self.assertFalse(is_main_runtime_invocation(args))
        self.assertTrue(is_main_runtime_invocation(["app.exe"]))

    def test_ready_probe_is_atomic_and_mainloop_gated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _FakeRoot()
            probe = Path(temp_dir) / "probe.json"
            lifecycle = RuntimeLifecycle(
                base_dir=Path(temp_dir),
                environ={PROBE_PATH_ENV: str(probe), PROBE_TOKEN_ENV: "token-1"},
                pid=123,
                executable_path=r"C:\\app\\windows-supporter.exe",
                process_start_time="2026-09-04T00:00:00+00:00",
                file_version="0.18.18.0",
                commit="abc1234",
            )

            lifecycle.mark_process_start()
            lifecycle.mark_tray_ready(456)
            lifecycle.bind_mainloop(root, tray_hwnd=456)

            self.assertEqual(json.loads(probe.read_text(encoding="utf-8"))["state"], "starting")
            self.assertEqual(root.calls[0][0], 0)
            root.calls[0][1]()

            payload = json.loads(probe.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["token"], "token-1")
            self.assertEqual(payload["state"], "ready")
            self.assertEqual(payload["pid"], 123)
            self.assertEqual(payload["version"], "0.18.18.0")
            self.assertEqual(payload["tray_hwnd"], 456)
            self.assertEqual(payload["mainloop_tick"], 1)
            self.assertTrue(payload["timestamp"])
            self.assertFalse(list(probe.parent.glob("*.tmp")))
            self.assertEqual(root.calls[1][0], 5000)

    def test_probe_path_and_token_must_be_supplied_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(RuntimeProbeError):
                RuntimeLifecycle(
                    base_dir=Path(temp_dir),
                    environ={PROBE_PATH_ENV: str(Path(temp_dir) / "probe.json")},
                    pid=123,
                    executable_path="windows-supporter.exe",
                    process_start_time="2026-09-04T00:00:00+00:00",
                    file_version="0.18.18.0",
                    commit="abc1234",
                )

    def test_unhandled_exception_records_failed_probe_and_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = Path(temp_dir) / "probe.json"
            lifecycle = RuntimeLifecycle(
                base_dir=Path(temp_dir),
                environ={PROBE_PATH_ENV: str(probe), PROBE_TOKEN_ENV: "token-2"},
                pid=321,
                executable_path=r"C:\\app\\windows-supporter.exe",
                process_start_time="2026-09-04T00:00:00+00:00",
                file_version="0.18.18.0",
                commit="def5678",
            )
            try:
                raise ValueError("fatal test")
            except ValueError as exc:
                lifecycle.record_exception("thread", type(exc), exc, exc.__traceback__)

            payload = json.loads(probe.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "failed")
            self.assertIn("fatal test", payload["error"])
            entries = [
                json.loads(line)
                for line in lifecycle.log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(entries[-1]["phase"], "fatal_exception")
            self.assertIn("ValueError", entries[-1]["error"]["traceback"])

    def test_lost_tray_readiness_invalidates_ready_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _FakeRoot()
            probe = Path(temp_dir) / "probe.json"
            tray_ready = True
            lifecycle = RuntimeLifecycle(
                base_dir=Path(temp_dir),
                environ={PROBE_PATH_ENV: str(probe), PROBE_TOKEN_ENV: "token-3"},
                pid=654,
                executable_path=r"C:\\app\\windows-supporter.exe",
                process_start_time="2026-09-04T00:00:00+00:00",
                file_version="0.18.18.0",
                commit="fed9876",
            )

            lifecycle.mark_process_start()
            lifecycle.mark_tray_ready(777)
            lifecycle.bind_mainloop(
                root,
                tray_hwnd=777,
                readiness_check=lambda: tray_ready,
            )
            root.calls[0][1]()
            tray_ready = False
            root.calls[1][1]()

            payload = json.loads(probe.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "failed")
            self.assertIn("tray readiness was lost", payload["error"])
            self.assertEqual(len(root.calls), 2)

    def test_stopping_probe_failure_does_not_interrupt_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lifecycle = RuntimeLifecycle(
                base_dir=Path(temp_dir),
                environ={},
                pid=987,
                executable_path="windows-supporter.exe",
                process_start_time="2026-09-04T00:00:00+00:00",
                file_version="0.18.18.0",
                commit="abc9876",
            )
            with patch.object(
                lifecycle,
                "_write_probe",
                side_effect=RuntimeProbeError("probe unavailable"),
            ):
                lifecycle.mark_stopping("test")

            entries = [
                json.loads(line)
                for line in lifecycle.log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(entries[-1]["phase"], "probe_write_error")
            self.assertEqual(entries[-1]["state"], "stopping")

    def test_intentional_stopping_suppresses_late_tray_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _FakeRoot()
            probe = Path(temp_dir) / "probe.json"
            lifecycle = RuntimeLifecycle(
                base_dir=Path(temp_dir),
                environ={PROBE_PATH_ENV: str(probe), PROBE_TOKEN_ENV: "token-4"},
                pid=111,
                executable_path="windows-supporter.exe",
                process_start_time="2026-09-04T00:00:00+00:00",
                file_version="0.18.18.0",
                commit="abc1111",
            )
            lifecycle.mark_process_start()
            lifecycle.mark_tray_ready(222)
            lifecycle.bind_mainloop(
                root,
                tray_hwnd=222,
                readiness_check=lambda: False,
            )
            lifecycle.mark_stopping("tray_exit_requested")
            root.calls[0][1]()

            payload = json.loads(probe.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "stopping")
            phases = [
                json.loads(line)["phase"]
                for line in lifecycle.log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertNotIn("fatal_exception", phases)

    def test_log_rotation_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lifecycle = RuntimeLifecycle(
                base_dir=Path(temp_dir),
                environ={},
                pid=os.getpid(),
                executable_path="python.exe",
                process_start_time="2026-09-04T00:00:00+00:00",
                file_version="dev",
                commit="",
                max_log_bytes=160,
                log_backups=2,
            )
            for index in range(12):
                lifecycle.emit("test", index=index, payload="x" * 80)
            self.assertTrue(lifecycle.log_path.exists())
            self.assertTrue(lifecycle.log_path.with_suffix(".jsonl.1").exists())
            self.assertFalse(lifecycle.log_path.with_suffix(".jsonl.3").exists())


class RotateLogFileTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "service.log"

    def backup(self, index: int) -> Path:
        return Path(f"{self.path}.{index}")

    def test_defaults_match_the_runtime_log_policy(self) -> None:
        self.assertEqual(DEFAULT_MAX_LOG_BYTES, 2 * 1024 * 1024)
        self.assertEqual(DEFAULT_LOG_BACKUPS, 3)

    def test_record_that_fits_leaves_the_log_in_place(self) -> None:
        self.path.write_bytes(b"a" * 90)

        rotate_log_file(self.path, 10, max_log_bytes=100, log_backups=2)

        self.assertEqual(self.path.read_bytes(), b"a" * 90)
        self.assertFalse(self.backup(1).exists())

    def test_overflowing_record_moves_the_log_and_keeps_bounded_backups(self) -> None:
        for generation in (b"a", b"b", b"c", b"d"):
            self.path.write_bytes(generation * 100)

            rotate_log_file(self.path, 1, max_log_bytes=100, log_backups=2)

            self.assertFalse(self.path.exists())
        self.assertEqual(self.backup(1).read_bytes(), b"d" * 100)
        self.assertEqual(self.backup(2).read_bytes(), b"c" * 100)
        self.assertFalse(self.backup(3).exists())

    def test_missing_log_is_not_an_error(self) -> None:
        rotate_log_file(self.path, 10, max_log_bytes=1, log_backups=2)

        self.assertFalse(self.path.exists())
        self.assertFalse(self.backup(1).exists())

    def test_zero_backups_drop_the_overflowing_log(self) -> None:
        self.path.write_bytes(b"a" * 100)

        rotate_log_file(self.path, 1, max_log_bytes=100, log_backups=0)

        self.assertFalse(self.path.exists())
        self.assertFalse(self.backup(1).exists())

    def _seed_backups(self) -> None:
        self.path.write_bytes(b"live" * 30)
        for index, content in ((1, b"one"), (2, b"two"), (3, b"three")):
            self.backup(index).write_bytes(content)

    def _assert_nothing_moved(self) -> None:
        self.assertEqual(self.path.read_bytes(), b"live" * 30)
        self.assertEqual(
            [self.backup(index).read_bytes() for index in (1, 2, 3)],
            [b"one", b"two", b"three"],
        )
        self.assertFalse(Path(f"{self.path}.rotating").exists())

    def test_log_that_cannot_be_moved_leaves_the_backups_alone(self) -> None:
        # A failed rotation is retried with every record; the backups must not
        # be shifted by each attempt while the live log stays where it is.
        self._seed_backups()
        real_replace = os.replace

        def refuse_live_log(source, target):
            if Path(source) == self.path:
                raise PermissionError("held open")
            return real_replace(source, target)

        with patch("src.utils.runtime_lifecycle.os.replace", side_effect=refuse_live_log):
            for _attempt in range(3):
                with self.assertRaises(PermissionError):
                    rotate_log_file(self.path, 1, max_log_bytes=100, log_backups=3)

        self._assert_nothing_moved()

    def test_failed_backup_shift_puts_the_live_log_back(self) -> None:
        self._seed_backups()
        real_replace = os.replace

        def refuse_first_backup(source, target):
            if Path(source) == self.backup(1):
                raise PermissionError("held open")
            return real_replace(source, target)

        with patch("src.utils.runtime_lifecycle.os.replace", side_effect=refuse_first_backup):
            with self.assertRaises(PermissionError):
                rotate_log_file(self.path, 1, max_log_bytes=100, log_backups=2)

        self.assertEqual(self.path.read_bytes(), b"live" * 30)
        self.assertEqual(self.backup(1).read_bytes(), b"one")
        self.assertFalse(Path(f"{self.path}.rotating").exists())

    @unittest.skipUnless(os.name == "nt", "an open file is only refused a rename on Windows")
    def test_log_held_open_by_another_handle_leaves_the_backups_alone(self) -> None:
        self._seed_backups()

        with self.path.open("rb"):
            for _attempt in range(2):
                with self.assertRaises(PermissionError):
                    rotate_log_file(self.path, 1, max_log_bytes=100, log_backups=3)

        self._assert_nothing_moved()

    def test_string_path_and_default_limits_are_accepted(self) -> None:
        self.path.write_bytes(b"a" * DEFAULT_MAX_LOG_BYTES)

        rotate_log_file(str(self.path), 1)

        self.assertFalse(self.path.exists())
        self.assertEqual(self.backup(1).stat().st_size, DEFAULT_MAX_LOG_BYTES)


if __name__ == "__main__":
    unittest.main()
