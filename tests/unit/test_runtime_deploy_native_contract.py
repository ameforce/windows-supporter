from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import psutil

from src.utils.runtime_deploy import WindowsRuntimeProcessController


REPO_ROOT = Path(__file__).resolve().parents[2]


class RuntimeDeployNativeContractTest(unittest.TestCase):
    def test_cli_preserves_utf8_streams_and_quoted_value_in_powershell_5_and_7(self):
        value = "경로 with spaces & symbols"
        script = (
            "$OutputEncoding=[Text.UTF8Encoding]::new($false);"
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
            "[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);"
            "& $env:WSU_TEST_PY $env:WSU_TEST_TOOL --contract-smoke $env:WSU_TEST_VALUE"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        environment = dict(os.environ)
        environment.update(
            {
                "WSU_TEST_PY": sys.executable,
                "WSU_TEST_TOOL": str(REPO_ROOT / "tools" / "deploy_runtime.py"),
                "WSU_TEST_VALUE": value,
            }
        )

        for shell in ("powershell.exe", "pwsh.exe"):
            with self.subTest(shell=shell):
                executable = shutil.which(shell)
                self.assertIsNotNone(executable, f"required native launcher is missing: {shell}")
                completed = subprocess.run(
                    [
                        str(executable),
                        "-NoProfile",
                        "-NonInteractive",
                        "-EncodedCommand",
                        encoded,
                    ],
                    cwd=REPO_ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                stdout = completed.stdout.decode("utf-8", errors="strict").strip()
                stderr = completed.stderr.decode("utf-8", errors="strict").strip()
                self.assertEqual(completed.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["value"], value)
                self.assertIn("배포 도우미 진단", stderr)

    def test_exact_parent_termination_cleans_its_child_process(self):
        child_script = "import time; time.sleep(60)"
        parent_script = (
            "import subprocess,sys,time;"
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]]);"
            "print(child.pid,flush=True);"
            "time.sleep(60)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_script, child_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
        child_pid = int(parent.stdout.readline().strip())
        try:
            controller = WindowsRuntimeProcessController()
            terminated = controller.terminate_tree([parent.pid], timeout_seconds=10)
            self.assertIn(parent.pid, terminated)
            self.assertIn(child_pid, terminated)
            self.assertFalse(psutil.pid_exists(parent.pid))
            self.assertFalse(psutil.pid_exists(child_pid))
        finally:
            for pid in (child_pid, parent.pid):
                try:
                    psutil.Process(pid).kill()
                except psutil.Error:
                    pass
            parent.wait(timeout=5)
            if parent.stdout is not None:
                parent.stdout.close()
            if parent.stderr is not None:
                parent.stderr.close()


if __name__ == "__main__":
    unittest.main()
