from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
REMOVE_SCRIPT = REPO_ROOT / "tools" / "remove_path_with_retry.bat"


class RemovePathWithRetryUnitTest(unittest.TestCase):
    @staticmethod
    def _lock_file(
        target_file: Path,
        sleep_seconds: int,
        ready_file: Path,
    ) -> subprocess.Popen[str]:
        code = textwrap.dedent(
            """
            import pathlib
            import sys
            import time

            target = pathlib.Path(sys.argv[1])
            sleep_seconds = int(sys.argv[2])
            ready_file = pathlib.Path(sys.argv[3])
            with open(target, "rb"):
                ready_file.write_text("ready", encoding="utf-8")
                time.sleep(sleep_seconds)
            """
        )
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(target_file),
                str(sleep_seconds),
                str(ready_file),
            ],
            text=True,
        )

    @staticmethod
    def _wait_for_ready(ready_file: Path) -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            if ready_file.exists():
                return
            time.sleep(0.05)
        raise AssertionError(f"locker did not create ready file: {ready_file}")

    def _run_remove(
        self,
        target_path: Path,
        *,
        attempts: int,
        wait_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "cmd",
                "/c",
                "call",
                str(REMOVE_SCRIPT),
                str(target_path),
                str(attempts),
                str(wait_seconds),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_removes_directory_after_transient_lock_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "build"
            target_dir.mkdir()
            locked_file = target_dir / "locked.pkg"
            locked_file.write_bytes(b"payload")
            ready_file = Path(temp_dir) / "ready.txt"

            locker = self._lock_file(
                locked_file,
                sleep_seconds=2,
                ready_file=ready_file,
            )
            try:
                self._wait_for_ready(ready_file)
                result = self._run_remove(
                    target_dir,
                    attempts=5,
                    wait_seconds=1,
                )
            finally:
                locker.wait(timeout=10)

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(target_dir.exists())

    def test_fails_when_directory_stays_locked_past_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "build"
            target_dir.mkdir()
            locked_file = target_dir / "locked.pkg"
            locked_file.write_bytes(b"payload")
            ready_file = Path(temp_dir) / "ready.txt"

            locker = self._lock_file(
                locked_file,
                sleep_seconds=5,
                ready_file=ready_file,
            )
            try:
                self._wait_for_ready(ready_file)
                result = self._run_remove(
                    target_dir,
                    attempts=2,
                    wait_seconds=1,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(target_dir.exists())
            finally:
                locker.wait(timeout=10)
                time.sleep(1)
                if target_dir.exists():
                    subprocess.run(
                        ["cmd", "/c", "rmdir", "/s", "/q", str(target_dir)],
                        check=False,
                    )


if __name__ == "__main__":
    unittest.main()
