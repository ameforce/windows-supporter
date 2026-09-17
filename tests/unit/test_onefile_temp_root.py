from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_TMPDIR = r"%LOCALAPPDATA%\windows-supporter\runtime"


class OnefileTempRootTest(unittest.TestCase):
    def test_build_preserves_runtime_expansion_not_build_account(self) -> None:
        script = (ROOT / "build.bat").read_text(encoding="utf-8")
        self.assertIn('--runtime-tmpdir "%%LOCALAPPDATA%%\\windows-supporter\\runtime"', script)
        self.assertNotIn('--runtime-tmpdir "."', script)
        self.assertIn('--add-data "%BUILD_GENERATED_DIR%\\tcltk\\_tcl_data;_tcl_data"', script)
        self.assertIn('--add-data "%BUILD_GENERATED_DIR%\\tcltk\\_tk_data;_tk_data"', script)

    @unittest.skipUnless(os.name == "nt", "Windows batch expansion")
    def test_cmd_passes_literal_variable_to_bootloader_option(self) -> None:
        script = (ROOT / "build.bat").read_text(encoding="utf-8")
        line = next(line for line in script.splitlines() if "python -m PyInstaller" in line)
        argument = line.split("--runtime-tmpdir ", 1)[1].split(" --icon", 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            batch = Path(temp) / "option.bat"
            batch.write_text("@echo off\nsetlocal DisableDelayedExpansion\necho " + argument + "\n", encoding="ascii")
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(batch)],
                env=dict(os.environ, LOCALAPPDATA=r"C:\build-account-must-not-be-embedded"),
                capture_output=True,
                timeout=10,
                check=True,
            )
        self.assertEqual(result.stdout.decode("ascii").strip(), '"' + RUNTIME_TMPDIR + '"')
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
