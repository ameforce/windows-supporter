from __future__ import annotations

import io
import tempfile
import types
import unittest
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools import verify_codex_usage_worker_smoke


class VerifyCodexUsageWorkerSmokeUnitTest(unittest.TestCase):
    def test_success_requires_zero_exit_from_frozen_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "windows-supporter.exe"
            executable.write_bytes(b"candidate")
            with (
                patch.object(
                    verify_codex_usage_worker_smoke.subprocess,
                    "run",
                    return_value=types.SimpleNamespace(returncode=0),
                ) as run,
                patch("sys.argv", ["verify", str(executable)]),
                redirect_stdout(io.StringIO()),
            ):
                result = verify_codex_usage_worker_smoke.main()

        self.assertEqual(result, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][1], "--codex-usage-worker-smoke")

    def test_smart_app_control_launch_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "windows-supporter.exe"
            executable.write_bytes(b"candidate")
            blocked = OSError("[WinError 4551] Smart App Control blocked the executable")
            stderr = io.StringIO()
            with (
                patch.object(
                    verify_codex_usage_worker_smoke.subprocess,
                    "run",
                    side_effect=blocked,
                ),
                patch("sys.argv", ["verify", str(executable)]),
                redirect_stderr(stderr),
            ):
                result = verify_codex_usage_worker_smoke.main()

        self.assertEqual(result, 1)
        self.assertIn("WinError 4551", stderr.getvalue())

    def test_nonzero_worker_exit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "windows-supporter.exe"
            executable.write_bytes(b"candidate")
            stderr = io.StringIO()
            with (
                patch.object(
                    verify_codex_usage_worker_smoke.subprocess,
                    "run",
                    return_value=types.SimpleNamespace(returncode=7),
                ),
                patch("sys.argv", ["verify", str(executable)]),
                redirect_stderr(stderr),
            ):
                result = verify_codex_usage_worker_smoke.main()

        self.assertEqual(result, 1)
        self.assertIn("code 7", stderr.getvalue())

    def test_worker_timeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "windows-supporter.exe"
            executable.write_bytes(b"candidate")
            stderr = io.StringIO()
            with (
                patch.object(
                    verify_codex_usage_worker_smoke.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(
                        cmd=[str(executable)], timeout=30.0
                    ),
                ),
                patch("sys.argv", ["verify", str(executable)]),
                redirect_stderr(stderr),
            ):
                result = verify_codex_usage_worker_smoke.main()

        self.assertEqual(result, 1)
        self.assertIn("timed out", stderr.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
