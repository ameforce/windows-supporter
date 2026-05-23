from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.generate_build_metadata import (
    derive_build_version,
    write_build_info_module,
    write_pyinstaller_version_file,
)


class GenerateBuildMetadataUnitTest(unittest.TestCase):
    def test_derives_patch_increment_from_commits_since_tag(self) -> None:
        with patch(
            "tools.generate_build_metadata._run_git",
            return_value="v0.3.1-4-g64d97c3",
        ):
            version = derive_build_version(Path("."))

        self.assertEqual(version.source_tag, "v0.3.1")
        self.assertEqual(version.commits_since_tag, 4)
        self.assertEqual(version.display_version, "v0.3.5 (64d97c3)")
        self.assertEqual(version.numeric_version, "0.3.5.0")
        self.assertEqual(version.numeric_tuple, (0, 3, 5, 0))

    def test_marks_dirty_builds_in_display_only(self) -> None:
        with patch(
            "tools.generate_build_metadata._run_git",
            return_value="v1.2.3-0-gabcdef1-dirty",
        ):
            version = derive_build_version(Path("."))

        self.assertEqual(version.display_version, "v1.2.3 (abcdef1-dirty)")
        self.assertEqual(version.numeric_version, "1.2.3.0")
        self.assertTrue(version.dirty)

    def test_rejects_non_tag_based_describe_output(self) -> None:
        with patch("tools.generate_build_metadata._run_git", return_value="abcdef1"):
            with self.assertRaisesRegex(RuntimeError, "not tag-based"):
                derive_build_version(Path("."))

    def test_writes_runtime_module_and_pyinstaller_version_file(self) -> None:
        with patch(
            "tools.generate_build_metadata._run_git",
            return_value="v0.3.1-4-g64d97c3",
        ):
            version = derive_build_version(Path("."))

        with tempfile.TemporaryDirectory() as temp_dir:
            module_path = Path(temp_dir) / "windows_supporter_build_info.py"
            version_path = Path(temp_dir) / "version-info.txt"

            write_build_info_module(module_path, version)
            write_pyinstaller_version_file(version_path, version, "windows-supporter.exe")

            module_text = module_path.read_text(encoding="utf-8")
            version_text = version_path.read_text(encoding="utf-8")

        self.assertIn("DISPLAY_VERSION = 'v0.3.5 (64d97c3)'", module_text)
        self.assertIn("NUMERIC_VERSION = '0.3.5.0'", module_text)
        self.assertIn("filevers=(0, 3, 5, 0)", version_text)
        self.assertIn("StringStruct('ProductVersion', '0.3.5.0')", version_text)
        self.assertIn("StringStruct('Comments', 'v0.3.5 (64d97c3)')", version_text)


if __name__ == "__main__":
    unittest.main()
