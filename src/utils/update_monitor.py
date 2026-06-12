from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.utils.app_version import get_app_version
from src.utils.subprocess_utils import popen_no_window
from src.utils.worktree_runtime import is_primary_worktree


SEMVER_TAG_RE = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
DESCRIBE_TAG_RE = re.compile(
    r"^(?P<tag>v?\d+\.\d+\.\d+)(?:-\d+-g[0-9a-f]+(?:-dirty)?)?$",
    re.IGNORECASE,
)
DEFAULT_CLEAN_ALLOWLIST = ("build/", "dist/", "*.spec", "*.egg-info/")
GIT_COMMAND_TIMEOUT_SECONDS = 20
DETACHED_HELPER_FILENAME = "update_windows_supporter.ps1"
GIT_CHECKOUT_UNAVAILABLE_MESSAGE = "Git checkout 안에서 실행되는 windows-supporter.exe만 업데이트를 지원합니다."
NON_PRIMARY_WORKTREE_UNAVAILABLE_MESSAGE = (
    "main worktree가 아닌 worktree에서 실행 중입니다. 업데이트와 시작프로그램 등록은 "
    "main worktree의 windows-supporter.exe에서만 수행합니다."
)
UPDATE_HELPER_SCRIPT = """param(
    [Parameter(Mandatory=$true)]
    [string]$RepoRoot
)

$ErrorActionPreference = "Stop"
$env:GIT_TERMINAL_PROMPT = "0"
$baseLogDir = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:TEMP }
$logDir = Join-Path -Path $baseLogDir -ChildPath "windows-supporter"
$logFile = Join-Path -Path $logDir -ChildPath "update.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-UpdateLog {
    param([string]$Message)
    Add-Content -LiteralPath $logFile -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function Invoke-NativeStep {
    param(
        [string]$Label,
        [scriptblock]$Step
    )
    Write-UpdateLog $Label
    & $Step *>> $logFile
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Write-UpdateLog "Starting Windows Supporter update"
Set-Location -LiteralPath $RepoRoot

Invoke-NativeStep "Checking Git checkout" { git rev-parse --show-toplevel }

$status = & git status --porcelain --untracked-files=all 2>> $logFile
if ($LASTEXITCODE -ne 0) {
    throw "Failed to inspect Git status"
}
if (($status | Out-String).Trim().Length -gt 0) {
    Invoke-NativeStep "Stashing tracked and untracked changes" {
        git stash push --include-untracked -m "windows-supporter auto update"
    }
}

Invoke-NativeStep "Cleaning allowlisted build byproducts" {
    git clean -fdX -- build/ dist/ "*.spec" "*.egg-info/"
}
Invoke-NativeStep "Fetching origin" { git fetch --tags origin }

& git show-ref --verify --quiet refs/heads/main *>> $logFile
if ($LASTEXITCODE -ne 0) {
    Invoke-NativeStep "Creating local main from origin/main" {
        git switch -c main --track origin/main
    }
} else {
    Invoke-NativeStep "Switching to main" { git switch main }
}

Invoke-NativeStep "Fast-forwarding main from origin/main" {
    git merge --ff-only origin/main
}
Invoke-NativeStep "Running build.bat" { cmd /c build.bat }

Write-UpdateLog "Update completed"
"""


@dataclass(frozen=True)
class UpdateCandidate:
    tag: str
    version: tuple[int, int, int]


def render_update_helper_script() -> str:
    return UPDATE_HELPER_SCRIPT.strip() + "\n"


def get_detached_helper_path(base_dir: str | os.PathLike[str] | None = None) -> Path:
    if base_dir is None:
        root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        base_dir = Path(root) / "windows-supporter"
    return Path(base_dir) / DETACHED_HELPER_FILENAME


def is_git_checkout_root(repo_root: str | os.PathLike[str]) -> bool:
    return (Path(repo_root) / ".git").exists()


def write_detached_helper_script(
    helper_path: str | os.PathLike[str] | None = None,
) -> Path:
    resolved_path = Path(helper_path) if helper_path is not None else get_detached_helper_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(render_update_helper_script(), encoding="utf-8", newline="\r\n")
    return resolved_path


class UpdatePromptSession:
    def __init__(self) -> None:
        self._dismissed_tag = ""
        return

    def dismiss(self, tag: str) -> None:
        self._dismissed_tag = str(tag or "").strip()
        return

    def should_prompt(self, tag: str) -> bool:
        normalized = str(tag or "").strip()
        return bool(normalized) and normalized != self._dismissed_tag


class WindowsSupporterUpdater:
    INITIAL_CHECK_DELAY_MS = 1000
    CHECK_INTERVAL_MS = 60 * 60 * 1000

    def __init__(
        self,
        *,
        root: Any,
        event_queue: Any,
        repo_root: str | os.PathLike[str] | None = None,
        app_version_provider=get_app_version,
        subprocess_module=subprocess,
        thread_factory=threading.Thread,
        popen=popen_no_window,
        quit_callback=None,
        status_changed_callback=None,
        helper_writer=write_detached_helper_script,
        worktree_runner=subprocess.run,
    ) -> None:
        self._root = root
        self._event_queue = event_queue
        self._repo_root = str(Path(repo_root or os.getcwd()).resolve())
        self._app_version_provider = app_version_provider
        self._subprocess = subprocess_module
        self._thread_factory = thread_factory
        self._popen = popen
        self._quit_callback = quit_callback
        self._status_changed_callback = status_changed_callback
        self._helper_writer = helper_writer
        self._worktree_runner = worktree_runner
        self._session = UpdatePromptSession()
        self._worker_active = False
        self._state = "idle"
        self._current_tag = ""
        self._latest_tag = ""
        self._last_error = ""
        return

    def start(self) -> None:
        if self._mark_unavailable_if_needed():
            return
        self._schedule_check(self.INITIAL_CHECK_DELAY_MS)
        return

    def check_now(self, *, manual: bool = False) -> None:
        if self._worker_active:
            return
        if self._mark_unavailable_if_needed():
            if manual:
                self._show_info("Windows Supporter 업데이트", self._manual_no_update_message())
            return
        self._worker_active = True
        self._state = "checking"
        self._notify_status_changed()

        def worker() -> None:
            candidate = None
            dirty = False
            error = ""
            try:
                candidate, dirty, error = self._collect_update_candidate()
            except Exception as exc:
                error = repr(exc)
            self._post_ui(lambda: self._handle_check_result(candidate, dirty, error, manual))

        try:
            thread = self._thread_factory(target=worker, daemon=True)
            thread.start()
        except Exception as exc:
            self._worker_active = False
            self._state = "error"
            self._last_error = repr(exc)
            self._notify_status_changed()
        return

    def get_status_snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "current_tag": self._current_tag,
            "latest_tag": self._latest_tag,
            "last_error": self._last_error,
        }

    def set_status_changed_callback(self, callback) -> None:
        self._status_changed_callback = callback
        return

    def _mark_unavailable_if_needed(self) -> bool:
        message = ""
        if not is_git_checkout_root(self._repo_root):
            message = GIT_CHECKOUT_UNAVAILABLE_MESSAGE
        elif not is_primary_worktree(self._repo_root, runner=self._worktree_runner):
            message = NON_PRIMARY_WORKTREE_UNAVAILABLE_MESSAGE

        if not message:
            return False
        self._state = "unavailable"
        self._last_error = message
        self._notify_status_changed()
        return True

    def _schedule_check(self, delay_ms: int) -> None:
        try:
            self._root.after(int(delay_ms), self._scheduled_check)
        except Exception:
            pass
        return

    def _scheduled_check(self) -> None:
        self.check_now(manual=False)
        self._schedule_check(self.CHECK_INTERVAL_MS)
        return

    def _post_ui(self, callback) -> None:
        try:
            self._event_queue.put(callback)
            return
        except Exception:
            pass
        try:
            callback()
        except Exception:
            pass
        return

    def _collect_update_candidate(self) -> tuple[UpdateCandidate | None, bool, str]:
        describe = self._git_output(["git", "describe", "--tags", "--long", "--match", "v[0-9]*"])
        current_tag = resolve_current_tag(
            app_version=self._app_version_provider(),
            git_describe=describe,
        )
        self._current_tag = current_tag
        if not current_tag:
            return None, False, "current tag could not be resolved"

        remote_output = self._git_output(build_remote_tag_check_command())
        remote_tags = parse_remote_tag_refs(remote_output)
        candidate = select_update_candidate(current_tag=current_tag, remote_tags=remote_tags)
        dirty = bool(candidate is not None and self._has_update_cleanup_targets())
        return candidate, dirty, ""

    def _git_output(self, argv: list[str]) -> str:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = self._subprocess.run(
                argv,
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                check=False,
                env=env,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{' '.join(argv)} timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s"
            ) from exc
        if getattr(result, "returncode", 1) != 0:
            message = str(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
            raise RuntimeError(message or f"{' '.join(argv)} failed")
        return str(getattr(result, "stdout", "") or "").strip()

    def _handle_check_result(
        self,
        candidate: UpdateCandidate | None,
        dirty: bool,
        error: str,
        manual: bool,
    ) -> None:
        self._worker_active = False
        self._last_error = str(error or "")
        if candidate is None:
            self._latest_tag = ""
            self._state = "error" if error else "current"
            self._notify_status_changed()
            if manual:
                self._show_info("Windows Supporter 업데이트", self._manual_no_update_message())
            return

        self._latest_tag = candidate.tag
        self._state = "update_available"
        self._notify_status_changed()
        if not manual and not self._session.should_prompt(candidate.tag):
            return

        if self._ask_update(candidate):
            dirty = bool(dirty or self._is_worktree_dirty())
            if dirty:
                self._show_warning(
                    "Windows Supporter 업데이트",
                    "커밋되지 않은 변경이 있어 자동 stash 및 빌드 산출물 정리 후 업데이트를 계속합니다.",
                )
            self.launch_update()
        else:
            self._session.dismiss(candidate.tag)
        return

    def _manual_no_update_message(self) -> str:
        if self._last_error:
            return f"업데이트 상태를 확인할 수 없습니다.\n{self._last_error}"
        if not self._current_tag:
            return "현재 버전 태그를 확인할 수 없습니다."
        return "현재 최신 버전입니다."

    def _notify_status_changed(self) -> None:
        callback = self._status_changed_callback
        if not callable(callback):
            return
        try:
            callback()
        except Exception:
            pass
        return

    def _is_worktree_dirty(self) -> bool:
        return self._has_update_cleanup_targets()

    def _has_update_cleanup_targets(self) -> bool:
        try:
            if self._git_output(["git", "status", "--porcelain"]):
                return True
            return bool(self._git_output(build_allowed_clean_probe_command()))
        except Exception:
            return False

    def _ask_update(self, candidate: UpdateCandidate) -> bool:
        try:
            from tkinter import messagebox

            return bool(
                messagebox.askyesno(
                    "Windows Supporter 업데이트",
                    f"새 버전 {candidate.tag}이 있습니다.\n지금 업데이트할까요?",
                )
            )
        except Exception:
            return False

    def _show_info(self, title: str, message: str) -> None:
        try:
            from tkinter import messagebox

            messagebox.showinfo(title, message)
        except Exception:
            pass
        return

    def _show_warning(self, title: str, message: str) -> None:
        try:
            from tkinter import messagebox

            messagebox.showwarning(title, message)
        except Exception:
            pass
        return

    def launch_update(self) -> bool:
        if self._mark_unavailable_if_needed():
            return False
        try:
            helper_path = Path(self._helper_writer())
        except Exception as exc:
            self._state = "error"
            self._last_error = f"failed to prepare update helper: {exc}"
            self._notify_status_changed()
            return False
        command = build_detached_helper_command(self._repo_root, helper_path=helper_path)
        proc = self._popen(command)
        if proc is None:
            self._state = "error"
            self._last_error = "failed to launch update helper"
            self._notify_status_changed()
            return False
        self._state = "updating"
        self._notify_status_changed()
        try:
            if callable(self._quit_callback):
                self._quit_callback()
        except Exception:
            pass
        return True


def parse_semver_tag(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG_RE.match(str(tag or "").strip())
    if not match:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def parse_remote_tag_refs(output: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line or line.endswith("^{}"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        prefix = "refs/tags/"
        if not ref.startswith(prefix):
            continue
        tag = ref[len(prefix) :].strip()
        if parse_semver_tag(tag) is None or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def select_update_candidate(
    *, current_tag: str, remote_tags: list[str] | tuple[str, ...]
) -> UpdateCandidate | None:
    current_version = parse_semver_tag(current_tag)
    if current_version is None:
        return None

    candidates: list[UpdateCandidate] = []
    for tag in remote_tags:
        version = parse_semver_tag(tag)
        if version is None or version <= current_version:
            continue
        candidates.append(UpdateCandidate(tag=str(tag).strip(), version=version))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.version)


def resolve_current_tag(*, app_version: Any, git_describe: str | None = None) -> str:
    source_tag = str(getattr(app_version, "source_tag", "") or "").strip()
    if parse_semver_tag(source_tag) is not None:
        return source_tag

    describe = str(git_describe or "").strip()
    match = DESCRIBE_TAG_RE.match(describe)
    if not match:
        return ""
    tag = str(match.group("tag") or "").strip()
    return tag if parse_semver_tag(tag) is not None else ""


def build_remote_tag_check_command(remote: str = "origin") -> list[str]:
    resolved_remote = str(remote or "").strip() or "origin"
    return ["git", "ls-remote", "--tags", "--refs", resolved_remote]


def build_stash_command(message: str) -> list[str]:
    return [
        "git",
        "stash",
        "push",
        "--include-untracked",
        "-m",
        str(message or "").strip(),
    ]


def build_allowed_clean_command(
    allowlist: tuple[str, ...] = DEFAULT_CLEAN_ALLOWLIST,
) -> list[str]:
    return ["git", "clean", "-fdX", "--", *[str(item) for item in allowlist]]


def build_allowed_clean_probe_command(
    allowlist: tuple[str, ...] = DEFAULT_CLEAN_ALLOWLIST,
) -> list[str]:
    return ["git", "clean", "-ndX", "--", *[str(item) for item in allowlist]]


def classify_switch_main_error(stderr: str) -> str:
    text = str(stderr or "").lower()
    if "already checked out at" in text:
        return "main_checked_out_in_other_worktree"
    return "unknown"


def build_detached_helper_command(
    repo_root: str | os.PathLike[str],
    *,
    helper_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    resolved_helper = Path(helper_path) if helper_path is not None else get_detached_helper_path()
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(resolved_helper),
        "-RepoRoot",
        str(repo_root),
    ]
