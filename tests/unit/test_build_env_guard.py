from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_SCRIPT = REPO_ROOT / "tools" / "ensure_venv_ready.bat"


class BuildEnvGuardUnitTest(unittest.TestCase):
    def _run_guard(self, project_root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["cmd", "/c", "call", str(GUARD_SCRIPT), str(project_root)],
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _create_venv(
        project_root: Path,
        *,
        create_scripts_python: bool = True,
        home_path: Path | None = None,
    ) -> Path:
        venv_dir = project_root / ".venv"
        scripts_dir = venv_dir / "Scripts"
        scripts_dir.mkdir(parents=True)

        if create_scripts_python:
            (scripts_dir / "python.exe").write_bytes(b"stub")

        if home_path is not None:
            (venv_dir / "pyvenv.cfg").write_text(
                f"home = {home_path}\n",
                encoding="utf-8",
            )

        return venv_dir

    def test_removes_stale_venv_when_home_python_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            venv_dir = self._create_venv(
                project_root,
                home_path=Path(r"C:\missing-python-home"),
            )

            result = self._run_guard(project_root)

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(venv_dir.exists())

    def test_keeps_valid_venv_when_home_python_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)

            fake_home = project_root / "python-home"
            fake_home.mkdir()
            (fake_home / "python.exe").write_bytes(b"stub")
            venv_dir = self._create_venv(
                project_root,
                home_path=fake_home,
            )

            result = self._run_guard(project_root)

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue(venv_dir.exists())

    def test_removes_venv_when_scripts_python_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)

            fake_home = project_root / "python-home"
            fake_home.mkdir()
            (fake_home / "python.exe").write_bytes(b"stub")
            venv_dir = self._create_venv(
                project_root,
                create_scripts_python=False,
                home_path=fake_home,
            )

            result = self._run_guard(project_root)

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(venv_dir.exists())

    def test_removes_venv_when_pyvenv_cfg_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            venv_dir = self._create_venv(project_root)

            result = self._run_guard(project_root)

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(venv_dir.exists())

    def test_removes_venv_when_pyvenv_cfg_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            venv_dir = self._create_venv(project_root)

            result = self._run_guard(project_root)

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(venv_dir.exists())


if __name__ == "__main__":
    unittest.main()
