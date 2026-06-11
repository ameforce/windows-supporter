from __future__ import annotations

import subprocess
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
    def test_derives_revision_from_commits_since_tag_without_patch_increment(self) -> None:
        def fake_run_git(repo_root: Path, args: list[str]) -> str:
            if args[:3] == ["describe", "--tags", "--long"]:
                return "v0.3.1-4-g64d97c3"
            if args == ["rev-parse", "v0.3.1^{}"]:
                return "tagcommit"
            if args == ["rev-list", "--first-parent", "--parents", "HEAD"]:
                return "headcommit parentcommit"
            self.fail(f"unexpected git args: {args!r}")

        with patch("tools.generate_build_metadata._run_git", side_effect=fake_run_git):
            version = derive_build_version(Path("."))

        self.assertEqual(version.source_tag, "v0.3.1")
        self.assertEqual(version.commits_since_tag, 4)
        self.assertEqual(version.revision, 4)
        self.assertEqual(version.display_version, "v0.3.1.4 (64d97c3)")
        self.assertEqual(version.numeric_version, "0.3.1.4")
        self.assertEqual(version.numeric_tuple, (0, 3, 1, 4))

    def test_git_failure_while_detecting_integration_merge_fails_metadata_generation(self) -> None:
        def fake_run_git(repo_root: Path, args: list[str]) -> str:
            if args[:3] == ["describe", "--tags", "--long"]:
                return "v0.3.1-4-g64d97c3"
            if args == ["rev-parse", "v0.3.1^{}"]:
                raise RuntimeError("rev-parse failed")
            self.fail(f"unexpected git args: {args!r}")

        with patch("tools.generate_build_metadata._run_git", side_effect=fake_run_git):
            with self.assertRaisesRegex(RuntimeError, "rev-parse failed"):
                derive_build_version(Path("."))

    def test_invalid_integration_merge_commit_count_fails_metadata_generation(self) -> None:
        def fake_run_git(repo_root: Path, args: list[str]) -> str:
            if args[:3] == ["describe", "--tags", "--long"]:
                return "v0.3.1-4-g64d97c3"
            if args == ["rev-parse", "v0.3.1^{}"]:
                return "tagcommit"
            if args == ["rev-list", "--first-parent", "--parents", "HEAD"]:
                return "merge first-parent tagcommit"
            if args == ["rev-list", "--first-parent", "--count", "merge..HEAD"]:
                return "not-a-number"
            self.fail(f"unexpected git args: {args!r}")

        with patch("tools.generate_build_metadata._run_git", side_effect=fake_run_git):
            with self.assertRaisesRegex(RuntimeError, "invalid first-parent commit count"):
                derive_build_version(Path("."))

    def test_marks_dirty_builds_in_display_only(self) -> None:
        with patch(
            "tools.generate_build_metadata._run_git",
            return_value="v1.2.3-0-gabcdef1-dirty",
        ):
            version = derive_build_version(Path("."))

        self.assertEqual(version.display_version, "v1.2.3 (abcdef1-dirty)")
        self.assertEqual(version.numeric_version, "1.2.3.0")
        self.assertEqual(version.revision, 0)
        self.assertTrue(version.dirty)

    def test_derives_revision_one_for_release_tag_back_merge_on_develop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self._create_release_back_merge_repo(Path(temp_dir))

            version = derive_build_version(repo)

        self.assertEqual(version.source_tag, "v0.5.5")
        self.assertGreater(version.commits_since_tag, 1)
        self.assertEqual(version.revision, 1)
        self.assertEqual(version.display_version, f"v0.5.5.1 ({version.commit})")
        self.assertEqual(version.numeric_version, "0.5.5.1")
        self.assertEqual(version.numeric_tuple, (0, 5, 5, 1))

    def test_increments_revision_after_release_tag_back_merge_on_develop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self._create_release_back_merge_repo(Path(temp_dir))
            self._write_and_commit(repo, "develop-after.txt", "after merge\n", "develop after merge")

            version = derive_build_version(repo)

        self.assertEqual(version.source_tag, "v0.5.5")
        self.assertEqual(version.revision, 2)
        self.assertEqual(version.display_version, f"v0.5.5.2 ({version.commit})")
        self.assertEqual(version.numeric_version, "0.5.5.2")

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

        self.assertIn("DISPLAY_VERSION = 'v0.3.1.4 (64d97c3)'", module_text)
        self.assertIn("NUMERIC_VERSION = '0.3.1.4'", module_text)
        self.assertIn("REVISION = 4", module_text)
        self.assertIn("filevers=(0, 3, 1, 4)", version_text)
        self.assertIn("StringStruct('ProductVersion', '0.3.1.4')", version_text)
        self.assertIn("StringStruct('Comments', 'v0.3.1.4 (64d97c3)')", version_text)

    def _create_release_back_merge_repo(self, repo: Path) -> Path:
        self._git(repo, "init", "-b", "develop")
        self._git(repo, "config", "user.email", "codex@example.invalid")
        self._git(repo, "config", "user.name", "Codex Test")
        self._write_and_commit(repo, "base.txt", "base\n", "initial")
        self._write_and_commit(repo, "develop-before.txt", "develop before\n", "develop before")
        self._git(repo, "checkout", "-b", "main", "HEAD~1")
        self._write_and_commit(repo, "release.txt", "release\n", "release")
        self._git(repo, "tag", "v0.5.5")
        self._git(repo, "checkout", "develop")
        self._git(repo, "merge", "--no-ff", "v0.5.5", "-m", "merge v0.5.5 into develop")
        return repo

    def _write_and_commit(self, repo: Path, relative_path: str, content: str, message: str) -> None:
        path = repo / relative_path
        path.write_text(content, encoding="utf-8")
        self._git(repo, "add", relative_path)
        self._git(repo, "commit", "-m", message)

    def _git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
