"""Harmless native argv and Python-selection checks on installed PowerShell hosts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "tools" / "installer_workspace_process.ps1"


def ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt", "Installer native invocation requires Windows")
class InstallerWorkspaceProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hosts = [path for name in ("powershell.exe", "pwsh.exe") if (path := shutil.which(name))]
        if not cls.hosts:
            raise unittest.SkipTest("No PowerShell host available")

    def invoke_host(self, host: str, body: str, fixture: Path, stderr_contains: str | None = None) -> dict:
        script = fixture / "native probe with spaces.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "$OutputEncoding = [Console]::OutputEncoding\n"
            f". {ps_literal(HELPER)}\n{body}\n",
            encoding="utf-8-sig",  # Windows PowerShell 5.1 script decoding.
        )
        result = subprocess.run(
            [host, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=REPO_ROOT, env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
        stdout = result.stdout.decode("utf-8", errors="strict")
        stderr = result.stderr.decode("utf-8", errors="strict")
        self.assertEqual(result.returncode, 0, f"{host}\nstdout={stdout}\nstderr={stderr}")
        if stderr_contains is None:
            self.assertEqual(stderr, "", host)
        else:
            self.assertIn(stderr_contains, stderr, host)
        return json.loads(stdout)

    def test_native_argv_roundtrip_and_separate_streams(self):
        arguments = ["-OutputDirectory", "C:\\output directory\\", "-CompilerPath", "next argument",
                     "", 'embedded"quote', 'slash\\"quote', "한글 😀", "amp&percent%", "two\\\\"]
        child = (
            "import json,sys; print(json.dumps(sys.argv[1:],ensure_ascii=False)); "
            "print('distinct stderr',file=sys.stderr); raise SystemExit(29)"
        )
        native_args = ["-X", "utf8", "-c", child, *arguments]
        body = (
            f"$result = Invoke-InstallerWorkspaceProcess -Executable {ps_literal(sys.executable)} "
            "-CaptureOutput -Arguments @("
            + ", ".join(ps_literal(value) for value in native_args)
            + ")\n$result | ConvertTo-Json -Compress"
        )
        for host in self.hosts:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                result = self.invoke_host(host, body, Path(temporary))
                self.assertEqual(result["ExitCode"], 29)
                self.assertEqual(json.loads(result["Stdout"]), arguments)
                self.assertEqual(result["Stderr"].strip(), "distinct stderr")

    def test_python_owner_to_powershell_child_keeps_trailing_slash(self):
        output = "C:\\installer output space\\"
        compiler = "C:\\Program Files\\compiler.exe"
        for host in self.hosts:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary)
                child_script = fixture / "child parameter probe.ps1"
                child_script.write_text(
                    "param([string]$OutputDirectory,[string]$CompilerPath)\n"
                    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
                    "@{ OutputDirectory=$OutputDirectory; CompilerPath=$CompilerPath } | ConvertTo-Json -Compress\n",
                    encoding="utf-8-sig",
                )
                bootstrap = (
                    "from tools import build_workspace as b; from types import SimpleNamespace; "
                    "b.allocate_workspace=lambda kind:(SimpleNamespace(path='fresh fake scratch',retain=lambda:None),True); "
                    "raise SystemExit(b.main())"
                )
                owner_args = ["-X", "utf8", "-c", bootstrap, "--kind", "installer", "--keep", "--",
                              host, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(child_script),
                              "-OutputDirectory", output, "-CompilerPath", compiler]
                body = (
                    f"$result = Invoke-InstallerWorkspaceProcess -Executable {ps_literal(sys.executable)} "
                    "-CaptureOutput -Arguments @(" + ", ".join(ps_literal(v) for v in owner_args)
                    + ")\n$result | ConvertTo-Json -Compress"
                )
                result = self.invoke_host(host, body, fixture)
                self.assertEqual(result["ExitCode"], 0, result)
                child_result = json.loads(result["Stdout"].splitlines()[-1])
                self.assertEqual(child_result, {"OutputDirectory": output, "CompilerPath": compiler})
                self.assertIn("Scratch retained", result["Stderr"])

    def test_non_captured_owner_returns_numeric_exit_code(self):
        body = (
            f"$status = Invoke-InstallerWorkspaceProcess -Executable {ps_literal(sys.executable)} "
            "-Arguments @('-X', 'utf8', '-c', 'raise SystemExit(23)')\n"
            "@{ ExitCode=$status } | ConvertTo-Json -Compress"
        )
        for host in self.hosts:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                self.assertEqual(self.invoke_host(host, body, Path(temporary))["ExitCode"], 23)

    def test_owner_child_metadata_is_available_in_assignment_and_tee(self):
        artifact = "INSTALLER_ARTIFACT=C:\\export 한글 😀 space\\Setup.exe"
        checksum = "INSTALLER_SHA256=" + "A" * 64
        for host in self.hosts:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary)
                child = fixture / "metadata child.ps1"
                child.write_text(
                    f". {ps_literal(HELPER)}\nInitialize-InstallerWorkspaceChildStreams\n"
                    "[Console]::Error.WriteLine('harmless child stderr 한글')\n"
                    f"Write-Output {ps_literal(artifact)}\nWrite-Output {ps_literal(checksum)}\n",
                    encoding="utf-8-sig",
                )
                bootstrap = (
                    "from tools import build_workspace as b; from types import SimpleNamespace; "
                    "b.allocate_workspace=lambda kind:(SimpleNamespace(path='fresh fake scratch',retain=lambda:None),True); "
                    "raise SystemExit(b.main())"
                )
                args = ["-X", "utf8", "-c", bootstrap, "--kind", "installer", "--keep", "--",
                        host, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(child)]
                producer = fixture / "owner producer.ps1"
                producer.write_text(
                    "$ErrorActionPreference='Stop'\n"
                    f". {ps_literal(HELPER)}\n"
                    f"$ownerResult = Invoke-InstallerWorkspaceProcess -Executable {ps_literal(sys.executable)} "
                    "-CaptureOutput -Arguments @(" + ", ".join(ps_literal(v) for v in args) + ")\n"
                    "Write-InstallerWorkspaceOutput -Result $ownerResult\n"
                    "if ($ownerResult.ExitCode -ne 0) { throw 'unexpected owner failure' }\n",
                    encoding="utf-8-sig",
                )
                body = (
                    "$beforeOut=[Console]::Out; $beforeEncoding=[Console]::OutputEncoding.CodePage; "
                    "$beforePythonUtf8=$env:PYTHONUTF8\n"
                    f"$assigned = @(& {ps_literal(producer)})\n"
                    f"$teed = @(& {ps_literal(producer)} | Tee-Object -Variable teeLines)\n"
                    "@{ Assigned=$assigned; Teed=$teed; TeeLines=$teeLines; "
                    "HostUnchanged=([object]::ReferenceEquals($beforeOut,[Console]::Out) -and "
                    "$beforeEncoding -eq [Console]::OutputEncoding.CodePage -and "
                    "$beforePythonUtf8 -eq $env:PYTHONUTF8) } | ConvertTo-Json -Compress"
                )
                result = self.invoke_host(host, body, fixture, stderr_contains="harmless child stderr 한글")
                for key in ("Assigned", "Teed", "TeeLines"):
                    self.assertIn(artifact, result[key])
                    self.assertIn(checksum, result[key])
                    self.assertFalse(any("harmless child stderr" in line for line in result[key]))
                    self.assertTrue(all(isinstance(line, str) for line in result[key]))
                self.assertTrue(result["HostUnchanged"])

    def test_python_selection_skips_missing_corrupt_and_nonzero_candidates(self):
        for host in self.hosts:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary)
                broken = fixture / ".venv" / "Scripts" / "python.exe"
                broken.parent.mkdir(parents=True)
                broken.write_bytes(b"not an executable; task-owned fixture")
                failing = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
                body = (
                    f"$python = Resolve-InstallerWorkspacePython -RepoRoot {ps_literal(fixture)} -Candidates @("
                    + ", ".join(ps_literal(v) for v in ["", fixture / "missing.exe", broken, failing, sys.executable])
                    + ")\n@{ Python=$python } | ConvertTo-Json -Compress"
                )
                result = self.invoke_host(host, body, fixture)
                self.assertEqual(result["Python"], sys.executable)
                self.assertEqual(broken.read_bytes(), b"not an executable; task-owned fixture")

    def test_no_executable_python_fails_closed(self):
        for host in self.hosts:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary)
                broken = fixture / "broken.exe"
                broken.write_bytes(b"invalid executable")
                body = (
                    "$failed = $false\ntry { $null = Resolve-InstallerWorkspacePython "
                    f"-RepoRoot {ps_literal(fixture)} -Candidates @({ps_literal(broken)}) }}\n"
                    "catch { $failed = $_.Exception.Message -like 'No executable Python 3.14*' }\n"
                    "@{ Failed=$failed } | ConvertTo-Json -Compress"
                )
                self.assertTrue(self.invoke_host(host, body, fixture)["Failed"])
                self.assertEqual(broken.read_bytes(), b"invalid executable")

    def test_default_selection_uses_independent_python_before_corrupt_venv(self):
        for host in self.hosts:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary)
                broken = fixture / ".venv" / "Scripts" / "python.exe"
                broken.parent.mkdir(parents=True)
                broken.write_bytes(b"untouched corrupt venv fixture")
                body = (
                    f"$env:WINDOWS_SUPPORTER_BUILD_PYTHON = {ps_literal(sys.executable)}\n"
                    f"$python = Resolve-InstallerWorkspacePython -RepoRoot {ps_literal(fixture)}\n"
                    "@{ Python=$python } | ConvertTo-Json -Compress"
                )
                self.assertEqual(self.invoke_host(host, body, fixture)["Python"], sys.executable)
                self.assertEqual(broken.read_bytes(), b"untouched corrupt venv fixture")

    def test_installer_uses_checked_python_and_native_owner_invocation(self):
        source = (REPO_ROOT / "tools" / "build_installer.ps1").read_text(encoding="utf-8")
        self.assertIn('"installer_workspace_process.ps1"', source)
        self.assertIn("Resolve-InstallerWorkspacePython -RepoRoot $repoRoot", source)
        self.assertIn("Invoke-InstallerWorkspaceProcess -Executable $python -Arguments $ownerArguments", source)
        self.assertNotIn("& $python @ownerArguments", source)
        self.assertIn("-Arguments $ownerArguments -CaptureOutput", source)
        self.assertIn("Write-InstallerWorkspaceOutput -Result $ownerResult", source)
        self.assertIn("$ownerExitCode = $ownerResult.ExitCode", source)
        self.assertIn("if ($WorkspaceChild) { Initialize-InstallerWorkspaceChildStreams }", source)
        self.assertIn("if ($ownerExitCode -ne 0)", source)

    def test_installer_creates_fresh_subdirectory_and_rejects_existing_one(self):
        source = (REPO_ROOT / "tools" / "build_installer.ps1").read_text(encoding="utf-8")
        start = source.index("$scratchOutputDirectory = ")
        end = source.index("$coreInstallerPath = ", start)
        setup = source[start:end]
        for host in self.hosts:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary)
                root = fixture / "fresh scratch root"
                root.mkdir()
                body = (
                    f"$env:WINDOWS_SUPPORTER_INSTALLER_WORKSPACE = {ps_literal(root)}\n"
                    + setup
                    + "$created = $scratchOutputDirectory\n"
                    + "$refused = $false\ntry {\n" + setup
                    + "}\ncatch { $refused = $true }\n"
                    + "@{ OutputDirectory=$created; RefusedExisting=$refused } | ConvertTo-Json -Compress"
                )
                result = self.invoke_host(host, body, fixture)
                self.assertEqual(result["OutputDirectory"], str(root / "installer"))
                self.assertTrue((root / "installer").is_dir())
                self.assertTrue(result["RefusedExisting"])


if __name__ == "__main__":
    unittest.main()
