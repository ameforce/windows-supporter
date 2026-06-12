from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


CODEX_TEMP_WORKTREE_RE = re.compile(r"(^|[/\\])\.codex[/\\]worktrees([/\\]|$)", re.IGNORECASE)
GIT_WORKTREE_COMMAND_TIMEOUT_SECONDS = 5


def is_codex_temporary_worktree_path(path: str | os.PathLike[str] | None) -> bool:
    return bool(CODEX_TEMP_WORKTREE_RE.search(str(path or "")))


def _has_git_metadata(repo_root: Path) -> bool:
    return (repo_root / ".git").exists()


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(left)))) == os.path.normcase(
        os.path.abspath(os.path.normpath(str(right)))
    )


def _parse_git_worktree_porcelain(output: str) -> list[dict[str, str]]:
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line:
            current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"worktree": value.strip()}
            worktrees.append(current)
            continue
        if current is not None and key:
            current[key] = value.strip()
    return worktrees


def _run_git_worktree_list(
    repo_root: Path,
    *,
    runner: Any = subprocess.run,
) -> str | None:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = runner(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=GIT_WORKTREE_COMMAND_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if int(getattr(result, "returncode", 1) or 0) != 0:
        return None
    return str(getattr(result, "stdout", "") or "")


def resolve_primary_worktree_path(
    repo_root: str | os.PathLike[str],
    *,
    runner: Any = subprocess.run,
) -> str | None:
    output = _run_git_worktree_list(Path(repo_root), runner=runner)
    if output is None:
        return None
    worktrees = _parse_git_worktree_porcelain(output)
    if not worktrees:
        return None
    primary_root = worktrees[0].get("worktree", "").strip()
    if not primary_root or is_codex_temporary_worktree_path(primary_root):
        return None
    return primary_root


def is_primary_worktree(
    repo_root: str | os.PathLike[str],
    *,
    runner: Any = subprocess.run,
) -> bool:
    primary_root = resolve_primary_worktree_path(repo_root, runner=runner)
    if primary_root is None:
        return False
    return _same_path(repo_root, primary_root)


def resolve_persistent_executable_path(
    current_executable: str | os.PathLike[str],
    *,
    runner: Any = subprocess.run,
) -> str | None:
    current = str(current_executable or "").strip()
    if not current:
        return None
    current_path = Path(current)
    repo_root = current_path.parent
    if not is_codex_temporary_worktree_path(current) and not _has_git_metadata(repo_root):
        return current

    primary_root = resolve_primary_worktree_path(repo_root, runner=runner)
    if primary_root is None:
        return None
    if _same_path(repo_root, primary_root):
        return current

    executable_name = current_path.name or "windows-supporter.exe"
    candidate = Path(primary_root) / executable_name
    if not candidate.is_file():
        return None
    return str(candidate)


def log_startup_registration_event(message: str) -> None:
    try:
        base_dir = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        log_dir = Path(base_dir) / "windows-supporter"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "startup.log").open("a", encoding="utf-8") as fp:
            fp.write(str(message).strip() + "\n")
    except Exception:
        pass
