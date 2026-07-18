from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.subprocess_utils import build_no_window_subprocess_kwargs
from src.utils.worktree_runtime import (
    is_codex_temporary_worktree_path,
    is_primary_worktree,
    resolve_persistent_executable_path,
)


class WorktreeRuntimeUnitTest(unittest.TestCase):
    def test_primary_worktree_accepts_filesystem_alias_path(self) -> None:
        porcelain = "\n".join(
            [
                "worktree C:/Users/RUNNER~1/project",
                "HEAD 9cc7cc8",
                "branch refs/heads/main",
                "",
            ]
        )

        def runner(_argv, **_kwargs):
            return types.SimpleNamespace(returncode=0, stdout=porcelain, stderr="")

        with patch("src.utils.worktree_runtime.os.path.samefile", return_value=True):
            self.assertTrue(
                is_primary_worktree(
                    r"C:\Users\runneradmin\project",
                    runner=runner,
                )
            )

    def test_codex_temporary_worktree_path_is_detected_case_insensitively(self) -> None:
        self.assertTrue(
            is_codex_temporary_worktree_path(
                r"C:\Users\epapyrus\.codex\worktrees\9f9a\windows-supporter"
            )
        )
        self.assertTrue(
            is_codex_temporary_worktree_path(
                "C:/Users/epapyrus/.CoDeX/worktrees/9f9a/windows-supporter/windows-supporter.exe"
            )
        )
        self.assertFalse(
            is_codex_temporary_worktree_path(
                r"C:\workspace\daeng\git\tools\windows-supporter\windows-supporter.exe"
            )
        )

    def test_non_temporary_executable_is_already_persistent_target(self) -> None:
        calls = []

        def runner(*_args, **_kwargs):
            calls.append((_args, _kwargs))
            return types.SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

        current = r"C:\apps\windows-supporter\windows-supporter.exe"

        self.assertEqual(
            resolve_persistent_executable_path(current, runner=runner),
            current,
        )
        self.assertEqual(calls, [])

    def test_primary_worktree_executable_remains_persistent_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "main" / "windows-supporter"
            primary.mkdir(parents=True)
            primary_exe = primary / "windows-supporter.exe"
            primary_exe.write_text("primary", encoding="utf-8")
            (primary / ".git").mkdir()
            porcelain = "\n".join(
                [
                    f"worktree {primary.as_posix()}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/main",
                    "",
                ]
            )

            def runner(_argv, **_kwargs):
                return types.SimpleNamespace(returncode=0, stdout=porcelain, stderr="")

            resolved = resolve_persistent_executable_path(str(primary_exe), runner=runner)

        assert resolved is not None
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(primary_exe)))

    def test_temporary_executable_resolves_to_primary_worktree_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "main" / "windows-supporter"
            temporary = root / ".codex" / "worktrees" / "9f9a" / "windows-supporter"
            primary.mkdir(parents=True)
            temporary.mkdir(parents=True)
            primary_exe = primary / "windows-supporter.exe"
            temporary_exe = temporary / "windows-supporter.exe"
            primary_exe.write_text("primary", encoding="utf-8")
            temporary_exe.write_text("temporary", encoding="utf-8")
            porcelain = "\n".join(
                [
                    f"worktree {primary.as_posix()}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/main",
                    "",
                    f"worktree {temporary.as_posix()}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/hotfix/v0.6.3",
                    "",
                ]
            )
            commands = []

            def runner(argv, **kwargs):
                commands.append((list(argv), dict(kwargs)))
                return types.SimpleNamespace(returncode=0, stdout=porcelain, stderr="")

            resolved = resolve_persistent_executable_path(
                str(temporary_exe),
                runner=runner,
            )

        assert resolved is not None
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(primary_exe)))
        self.assertEqual(commands[0][0][:4], ["git", "-C", str(temporary), "worktree"])
        expected_no_window = build_no_window_subprocess_kwargs()
        if "creationflags" in expected_no_window:
            self.assertEqual(
                commands[0][1]["creationflags"],
                expected_no_window["creationflags"],
            )
        if "startupinfo" in expected_no_window:
            self.assertIn("startupinfo", commands[0][1])

    def test_non_codex_linked_worktree_resolves_to_primary_worktree_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "main" / "windows-supporter"
            linked = root / "scratch-worktrees" / "feature" / "windows-supporter"
            primary.mkdir(parents=True)
            linked.mkdir(parents=True)
            primary_exe = primary / "windows-supporter.exe"
            linked_exe = linked / "windows-supporter.exe"
            primary_exe.write_text("primary", encoding="utf-8")
            linked_exe.write_text("linked", encoding="utf-8")
            (linked / ".git").write_text(
                "gitdir: ../../main/.git/worktrees/feature\n",
                encoding="utf-8",
            )
            porcelain = "\n".join(
                [
                    f"worktree {primary.as_posix()}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/main",
                    "",
                    f"worktree {linked.as_posix()}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/codex/feature",
                    "",
                ]
            )

            def runner(_argv, **_kwargs):
                return types.SimpleNamespace(returncode=0, stdout=porcelain, stderr="")

            resolved = resolve_persistent_executable_path(str(linked_exe), runner=runner)

        assert resolved is not None
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(primary_exe)))

    def test_temporary_executable_fails_closed_when_primary_executable_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "main" / "windows-supporter"
            temporary = root / ".codex" / "worktrees" / "9f9a" / "windows-supporter"
            primary.mkdir(parents=True)
            temporary.mkdir(parents=True)
            temporary_exe = temporary / "windows-supporter.exe"
            temporary_exe.write_text("temporary", encoding="utf-8")
            porcelain = "\n".join(
                [
                    f"worktree {primary.as_posix()}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/main",
                    "",
                    f"worktree {temporary.as_posix()}",
                    "HEAD 9cc7cc8",
                    "branch refs/heads/hotfix/v0.6.3",
                    "",
                ]
            )

            def runner(_argv, **_kwargs):
                return types.SimpleNamespace(returncode=0, stdout=porcelain, stderr="")

            self.assertIsNone(
                resolve_persistent_executable_path(str(temporary_exe), runner=runner)
            )


if __name__ == "__main__":
    unittest.main()
