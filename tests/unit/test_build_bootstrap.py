from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
UV_VERSION = "0.10.2"
UV_WIN_AMD64_SHA256 = "7b3685aa1da15acbe080b4cba8684afbb6baf11c9b04d4d4b347cc18b7b9cfa0"


class BuildBootstrapUnitTest(unittest.TestCase):
    def test_build_bootstraps_pinned_uv_before_using_it(self) -> None:
        # Given
        build_script = (REPO_ROOT / "build.bat").read_text(encoding="utf-8")
        helper_path = REPO_ROOT / "tools" / "ensure_uv_ready.bat"

        # When / Then
        self.assertTrue(helper_path.is_file(), "build.bat must ship an uv bootstrap helper")
        self.assertIn('call "tools\\ensure_uv_ready.bat" "%STEP_LOG%"', build_script)
        self.assertIn('"%WINDOWS_SUPPORTER_UV_EXE%" sync --locked --extra build', build_script)
        self.assertIn('"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python', build_script)
        self.assertIsNone(
            re.search(r"(?im)^\s*uv(?:\.exe)?\s", build_script),
            "build.bat must not depend on a bare uv command from PATH",
        )

    def test_uv_bootstrap_is_user_local_version_and_hash_pinned(self) -> None:
        # Given
        helper_path = REPO_ROOT / "tools" / "ensure_uv_ready.bat"
        requirements_path = REPO_ROOT / "tools" / "uv-bootstrap-requirements.txt"

        # When / Then
        self.assertTrue(helper_path.is_file(), "uv bootstrap helper is missing")
        self.assertTrue(requirements_path.is_file(), "hashed uv bootstrap requirement is missing")
        helper = helper_path.read_text(encoding="utf-8")
        requirements = requirements_path.read_text(encoding="utf-8")
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'set "UV_VERSION={UV_VERSION}"', helper)
        self.assertIn("WINDOWS_SUPPORTER_UV_CACHE_ROOT", helper)
        self.assertIn("-m venv", helper)
        self.assertIn("--require-hashes", helper)
        self.assertIn("--only-binary=:all:", helper)
        self.assertIn(f"uv=={UV_VERSION}", requirements)
        self.assertIn(f"sha256:{UV_WIN_AMD64_SHA256}", requirements)
        self.assertIn(f'required-version = "=={UV_VERSION}"', pyproject)


if __name__ == "__main__":
    unittest.main()
