from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.runtime_lifecycle import (
    PROBE_PATH_ENV,
    PROBE_TOKEN_ENV,
    RuntimeLifecycle,
    RuntimeProbeError,
    is_main_runtime_invocation,
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


if __name__ == "__main__":
    unittest.main()
