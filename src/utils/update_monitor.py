from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.utils.app_version import get_app_version
from src.utils.subprocess_utils import (
    build_no_window_subprocess_kwargs,
    popen_no_window,
    run_no_window,
)
from src.utils.update_settings import (
    UpdateSettings,
    get_update_settings_path,
    load_update_settings,
    normalize_update_settings,
    save_update_settings,
)
from src.utils.worktree_runtime import is_primary_worktree


SEMVER_TAG_RE = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
DESCRIBE_TAG_RE = re.compile(
    r"^(?P<tag>v?\d+\.\d+\.\d+)(?:-\d+-g[0-9a-f]+(?:-dirty)?)?$",
    re.IGNORECASE,
)
DEFAULT_CLEAN_ALLOWLIST = ("build/", "dist/", "*.spec", "*.egg-info/")
GIT_COMMAND_TIMEOUT_SECONDS = 20
UPDATE_HANDOFF_ARG = "--windows-supporter-update-handoff"
UPDATE_HANDOFF_FILENAME = "update_handoff.json"
UPDATE_HANDOFF_EXECUTABLE_NAME = "windows-supporter-updater.exe"
UPDATE_HANDOFF_ACK_TIMEOUT_SECONDS = 15.0
UPDATE_HANDOFF_COMMAND_TIMEOUT_SECONDS = 1800
UPDATE_PROGRESS_TITLE = "Windows Supporter 업데이트"
UPDATE_PROGRESS_FAILURE_TITLE = "Windows Supporter 업데이트 실패"
UPDATE_PROGRESS_LOG_BUTTON_TEXT = "로그 열기"
UPDATE_PROGRESS_RETRY_BUTTON_TEXT = "재시도"
UPDATE_PROGRESS_CLOSE_BUTTON_TEXT = "닫기"
UPDATE_PROGRESS_MANUAL_ACTION_TEXT = "수동 조치"
UPDATE_CLEANUP_ONLY_NOTICE = "무시된 빌드 산출물만 정리한 뒤 업데이트를 계속합니다."
UPDATE_SOURCE_CHANGE_NOTICE = "커밋되지 않은 변경이 있어 stash 후 업데이트를 계속합니다."
UPDATE_FORCE_CLEAN_APPROVAL_TEXT = (
    "로컬 변경 또는 로컬 전용 커밋 때문에 자동 업데이트를 바로 진행할 수 없습니다.\n"
    "강제정리를 진행하면 uncommitted/untracked 변경은 stash 하고, "
    "로컬 전용 커밋은 백업 브랜치로 보존한 뒤 main을 origin/main 기준으로 reset/동기화합니다.\n"
    "강제정리를 진행할까요?"
)
UPDATE_FORCE_CLEAN_REJECTED_NOTICE = (
    "강제정리가 취소되어 업데이트를 중단했습니다. Git 상태를 직접 정리한 뒤 다시 업데이트를 실행해 주세요."
)
GIT_CHECKOUT_UNAVAILABLE_MESSAGE = "Git checkout 안에서 실행되는 windows-supporter.exe만 업데이트를 지원합니다."
NON_PRIMARY_WORKTREE_UNAVAILABLE_MESSAGE = (
    "main worktree가 아닌 worktree에서 실행 중입니다. 업데이트와 시작프로그램 등록은 "
    "main worktree의 windows-supporter.exe에서만 수행합니다."
)

@dataclass(frozen=True)
class UpdateCandidate:
    tag: str
    version: tuple[int, int, int]


@dataclass(frozen=True)
class UpdateProgressStep:
    key: str
    label: str
    detail: str
    percent: int


@dataclass(frozen=True)
class BuildOutputProgressRule:
    marker: str
    label: str
    percent: int


UPDATE_PROGRESS_STEPS: tuple[UpdateProgressStep, ...] = (
    UpdateProgressStep("idle", "업데이트 대기", "업데이트 확인을 기다리는 중입니다.", 0),
    UpdateProgressStep("checking", "업데이트 확인 중", "현재 버전과 원격 릴리스를 확인합니다.", 5),
    UpdateProgressStep("available", "업데이트 준비 완료", "새 버전을 설치할 수 있습니다.", 15),
    UpdateProgressStep("preflight", "업데이트 사전 점검 중", "Git 상태와 로컬 변경 여부를 확인합니다.", 25),
    UpdateProgressStep("stash", "변경 사항 스태시 중", "커밋되지 않은 변경을 stash로 보존합니다.", 35),
    UpdateProgressStep("cleanup", "빌드 산출물 정리 중", "무시된 빌드 산출물을 allowlist 범위에서 정리합니다.", 45),
    UpdateProgressStep("fetch", "원격 변경 확인 중", "origin 태그와 main 브랜치 정보를 가져옵니다.", 55),
    UpdateProgressStep("sync", "main 동기화 중", "main 브랜치를 업데이트 기준으로 맞춥니다.", 65),
    UpdateProgressStep("handoff", "업데이트 실행 준비 중", "빌드와 재실행을 맡을 업데이트 프로세스를 준비합니다.", 75),
    UpdateProgressStep("build", "빌드 실행 중", "build.bat를 실행합니다.", 85),
    UpdateProgressStep("relaunch", "Windows Supporter 재실행 중", "새 실행 파일을 시작합니다.", 95),
    UpdateProgressStep("complete", "업데이트 완료", "업데이트가 완료되었습니다.", 100),
    UpdateProgressStep("failed", "업데이트 실패", "실패 단계와 로그를 확인해 주세요.", 100),
)
UPDATE_PROGRESS_STEP_BY_KEY = {step.key: step for step in UPDATE_PROGRESS_STEPS}
BUILD_OUTPUT_PROGRESS_RULES: tuple[BuildOutputProgressRule, ...] = (
    BuildOutputProgressRule("Shutting down the running", "실행 중인 앱 종료 중", 86),
    BuildOutputProgressRule("Stopping stale PyInstaller workers", "빌드 작업자 정리 중", 87),
    BuildOutputProgressRule("Syncing uv environment", "uv 환경 동기화 중", 88),
    BuildOutputProgressRule("Preparing bundled Playwright", "브라우저 런타임 준비 중", 89),
    BuildOutputProgressRule("Cleaning prior PyInstaller", "이전 빌드 산출물 정리 중", 90),
    BuildOutputProgressRule("Generating version metadata", "버전 메타데이터 생성 중", 91),
    BuildOutputProgressRule("Building main.py", "실행 파일 빌드 중", 93),
    BuildOutputProgressRule("Moving windows-supporter.exe", "실행 파일 배치 중", 96),
    BuildOutputProgressRule("Remove build byproducts", "빌드 임시 파일 정리 중", 97),
    BuildOutputProgressRule("Skipping post-build launch", "빌드 후 직접 재실행 준비 중", 98),
)


@dataclass(frozen=True)
class UpdateWorkingTreeState:
    source_status: tuple[str, ...] = ()
    cleanup_targets: tuple[str, ...] = ()
    local_only_count: int = 0
    remote_only_count: int = 0
    local_only_commits: tuple[str, ...] = ()
    remote_only_commits: tuple[str, ...] = ()

    @property
    def has_source_changes(self) -> bool:
        return bool(self.source_status)

    @property
    def has_cleanup_targets(self) -> bool:
        return bool(self.cleanup_targets)

    @property
    def has_local_only_commits(self) -> bool:
        return self.local_only_count > 0 or bool(self.local_only_commits)

    @property
    def has_remote_only_commits(self) -> bool:
        return self.remote_only_count > 0 or bool(self.remote_only_commits)

    @property
    def is_diverged(self) -> bool:
        return self.has_local_only_commits and self.has_remote_only_commits

    @property
    def needs_source_stash(self) -> bool:
        return self.has_source_changes

    @property
    def needs_pre_update_notice(self) -> bool:
        return self.has_source_changes or self.has_cleanup_targets or self.is_diverged



def get_update_progress_step(step_key: str) -> UpdateProgressStep:
    key = str(step_key or "").strip() or "idle"
    return UPDATE_PROGRESS_STEP_BY_KEY.get(key, UPDATE_PROGRESS_STEP_BY_KEY["idle"])


def build_update_progress_snapshot(
    step_key: str = "idle",
    *,
    state: str | None = None,
    detail: str | None = None,
    log_path: str = "",
    failed_step: str = "",
    can_retry: bool = False,
    can_manual_action: bool = False,
) -> dict[str, Any]:
    step = get_update_progress_step(step_key)
    resolved_state = str(state or ("failed" if step.key == "failed" else "idle")).strip()
    percent = max(0, min(100, int(step.percent)))
    return {
        "title": UPDATE_PROGRESS_FAILURE_TITLE if resolved_state == "failed" else UPDATE_PROGRESS_TITLE,
        "state": resolved_state,
        "step_key": step.key,
        "label": step.label,
        "detail": str(detail if detail is not None else step.detail),
        "percent": percent,
        "progressbar": {
            "visible": True,
            "mode": "determinate",
            "value": percent,
            "maximum": 100,
        },
        "log_path": str(log_path or ""),
        "failed_step": str(failed_step or ""),
        "can_open_log": bool(log_path) or resolved_state == "failed",
        "can_retry": bool(can_retry),
        "can_manual_action": bool(can_manual_action),
        "labels": {
            "log": UPDATE_PROGRESS_LOG_BUTTON_TEXT,
            "retry": UPDATE_PROGRESS_RETRY_BUTTON_TEXT,
            "close": UPDATE_PROGRESS_CLOSE_BUTTON_TEXT,
            "manual_action": UPDATE_PROGRESS_MANUAL_ACTION_TEXT,
        },
    }


def build_update_build_output_progress_snapshot(line: str) -> dict[str, Any] | None:
    text = str(line or "").strip()
    if not text:
        return None
    for rule in BUILD_OUTPUT_PROGRESS_RULES:
        if rule.marker not in text:
            continue
        snapshot = build_update_progress_snapshot(
            "build",
            state="running",
            detail=f"build.bat 단계: {text}",
        )
        percent = max(0, min(100, int(rule.percent)))
        snapshot["label"] = rule.label
        snapshot["percent"] = percent
        progressbar = snapshot.get("progressbar", {})
        if isinstance(progressbar, dict):
            progressbar["value"] = percent
        return snapshot
    return None


def publish_build_output_progress(
    output: str,
    *,
    progress_ui: Any,
    state_path: str | os.PathLike[str],
    seen: set[str] | None = None,
) -> None:
    published = seen if seen is not None else set()
    for line in str(output or "").splitlines():
        snapshot = build_update_build_output_progress_snapshot(line)
        if snapshot is None:
            continue
        label = str(snapshot.get("label") or "")
        if label in published:
            continue
        published.add(label)
        if progress_ui is not None:
            progress_ui.set_snapshot(snapshot)
        update_handoff_state(state_path, status="running", progress=snapshot)
    return


def format_update_status_parts(data: Any) -> tuple[bool, list[tuple[str, str]]]:
    if not isinstance(data, dict):
        return False, [("확인 불가", "disabled")]
    state = str(data.get("state", "") or "").strip()
    current = str(data.get("current_tag", "") or "").strip()
    latest = str(data.get("latest_tag", "") or "").strip()
    progress = data.get("progress", {})
    if not isinstance(progress, dict):
        progress = {}
    progress_label = str(progress.get("label", "") or "").strip()
    progress_detail = str(progress.get("detail", "") or "").strip()
    progress_percent = progress.get("percent", None)
    if state == "update_available" and latest:
        parts = [("업데이트 가능", "enabled")]
        if current:
            parts.append((f"{current} -> {latest}", "normal"))
        else:
            parts.append((latest, "normal"))
        if progress_label:
            parts.append((progress_label, "normal"))
        return True, parts
    if state == "checking":
        parts = [(progress_label or "업데이트 확인 중", "normal")]
        if isinstance(progress_percent, int):
            parts.append((f"{progress_percent}%", "normal"))
        return False, parts
    if state == "updating":
        parts = [("업데이트 중", "enabled")]
        if progress_label:
            parts.append((progress_label, "normal"))
        if isinstance(progress_percent, int):
            parts.append((f"{progress_percent}%", "normal"))
        return True, parts
    if state == "unavailable":
        return False, [("지원 안 됨", "disabled"), ("Git checkout 필요", "normal")]
    if state == "cancelled":
        detail = progress_detail or str(data.get("last_error", "") or "").strip()
        parts = [("취소됨", "disabled")]
        if detail:
            parts.append((detail, "normal"))
        return False, parts
    if state == "error":
        parts = [("확인 실패", "disabled")]
        failed_step = str(progress.get("failed_step", "") or "").strip()
        if failed_step:
            parts.append((f"실패 단계: {failed_step}", "normal"))
        elif progress_label:
            parts.append((progress_label, "normal"))
        if progress_detail:
            parts.append((progress_detail, "normal"))
        return False, parts
    if current:
        return False, [("최신", "normal"), (current, "normal")]
    return False, [("확인 대기", "normal")]


def build_force_clean_approval_message(working_tree: UpdateWorkingTreeState) -> str:
    local_count = int(working_tree.local_only_count or len(working_tree.local_only_commits))
    remote_count = int(working_tree.remote_only_count or len(working_tree.remote_only_commits))
    return (
        f"{UPDATE_FORCE_CLEAN_APPROVAL_TEXT}\n\n"
        f"로컬 전용 커밋: {local_count}개\n"
        f"원격 전용 커밋: {remote_count}개\n"
        "거부하면 stash, 백업 브랜치 생성, reset/동기화, clean을 수행하지 않습니다."
    )



def get_update_state_dir(base_dir: str | os.PathLike[str] | None = None) -> Path:
    if base_dir is None:
        root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        base_dir = Path(root) / "windows-supporter"
    return Path(base_dir)


def get_update_log_path(base_dir: str | os.PathLike[str] | None = None) -> Path:
    return get_update_state_dir(base_dir) / "update.log"


def get_update_handoff_path(base_dir: str | os.PathLike[str] | None = None) -> Path:
    return get_update_state_dir(base_dir) / UPDATE_HANDOFF_FILENAME


def get_update_handoff_executable_path(
    base_dir: str | os.PathLike[str] | None = None,
) -> Path:
    return get_update_state_dir(base_dir) / UPDATE_HANDOFF_EXECUTABLE_NAME


def is_git_checkout_root(repo_root: str | os.PathLike[str]) -> bool:
    return (Path(repo_root) / ".git").exists()



def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def read_update_handoff_state(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_update_handoff_state(
    path: str | os.PathLike[str],
    payload: dict[str, Any],
) -> Path:
    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2, default=_json_default)
        fp.write("\n")
    return resolved_path


def update_handoff_state(
    path: str | os.PathLike[str],
    **updates: Any,
) -> dict[str, Any]:
    state = read_update_handoff_state(path)
    state.update(updates)
    write_update_handoff_state(path, state)
    return state


def build_update_handoff_payload(
    *,
    repo_root: str | os.PathLike[str],
    target_tag: str = "",
    working_tree: UpdateWorkingTreeState | None = None,
    log_path: str | os.PathLike[str] | None = None,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tree = working_tree or UpdateWorkingTreeState()
    return {
        "version": 1,
        "status": "pending",
        "repo_root": str(Path(repo_root).resolve()),
        "target_tag": str(target_tag or ""),
        "requested_at": time.time(),
        "acknowledged_at": None,
        "log_path": str(log_path or get_update_log_path()),
        "working_tree": {
            "source_status": list(tree.source_status),
            "cleanup_targets": list(tree.cleanup_targets),
            "local_only_count": tree.local_only_count,
            "remote_only_count": tree.remote_only_count,
            "local_only_commits": list(tree.local_only_commits),
            "remote_only_commits": list(tree.remote_only_commits),
        },
        "preflight": dict(preflight or {}),
        "progress": build_update_progress_snapshot("handoff", state="pending"),
    }


def _resolve_script_path(
    *,
    argv: Sequence[str] | None = None,
    main_file: str | os.PathLike[str] | None = None,
) -> str:
    resolved_argv = list(sys.argv if argv is None else argv)
    script = resolved_argv[0] if resolved_argv else ""
    if not script or str(script).startswith("-"):
        script = str(main_file or Path(__file__).resolve().parents[2] / "main.py")
    return os.path.abspath(str(script))


def build_update_handoff_command(
    state_path: str | os.PathLike[str],
    *,
    executable: str | os.PathLike[str] | None = None,
    argv: Sequence[str] | None = None,
    frozen: bool | None = None,
    main_file: str | os.PathLike[str] | None = None,
    handoff_executable_path: str | os.PathLike[str] | None = None,
    copy_function=shutil.copy2,
) -> list[str]:
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    resolved_state = str(state_path)
    if is_frozen:
        source = Path(executable or sys.executable)
        target = Path(handoff_executable_path) if handoff_executable_path is not None else get_update_handoff_executable_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.normcase(os.path.abspath(source)) != os.path.normcase(os.path.abspath(target)):
            copy_function(source, target)
        return [str(target), UPDATE_HANDOFF_ARG, resolved_state]

    script = _resolve_script_path(argv=argv, main_file=main_file)
    return [str(executable or sys.executable), script, UPDATE_HANDOFF_ARG, resolved_state]


def cleanup_update_handoff_executable(
    *,
    base_dir: str | os.PathLike[str] | None = None,
    executable_path: str | os.PathLike[str] | None = None,
    current_executable: str | os.PathLike[str] | None = None,
    unlink_function: Callable[[Path], Any] | None = None,
    log: Callable[[str], None] | None = None,
) -> bool:
    target = (
        Path(executable_path)
        if executable_path is not None
        else get_update_handoff_executable_path(base_dir)
    )
    try:
        if not target.exists():
            return True
        current = Path(current_executable or sys.executable)
        if os.path.normcase(os.path.abspath(target)) == os.path.normcase(
            os.path.abspath(current)
        ):
            return False
        if unlink_function is None:
            target.unlink()
        else:
            unlink_function(target)
        return True
    except Exception as exc:
        if callable(log):
            log(f"handoff helper cleanup skipped: {target} ({exc!r})")
        return False


def cleanup_update_handoff_executable_with_retries(
    *,
    base_dir: str | os.PathLike[str] | None = None,
    executable_path: str | os.PathLike[str] | None = None,
    current_executable: str | os.PathLike[str] | None = None,
    attempts: int = 20,
    delay_seconds: float = 0.5,
    log: Callable[[str], None] | None = None,
) -> bool:
    total_attempts = max(1, int(attempts or 1))
    for attempt in range(total_attempts):
        if cleanup_update_handoff_executable(
            base_dir=base_dir,
            executable_path=executable_path,
            current_executable=current_executable,
            log=log,
        ):
            return True
        if attempt < total_attempts - 1:
            time.sleep(max(0.0, float(delay_seconds)))
    return False


def start_update_handoff_cleanup_thread(
    *,
    base_dir: str | os.PathLike[str] | None = None,
    executable_path: str | os.PathLike[str] | None = None,
    current_executable: str | os.PathLike[str] | None = None,
    attempts: int = 20,
    delay_seconds: float = 0.5,
    thread_factory=threading.Thread,
    log: Callable[[str], None] | None = None,
) -> Any:
    def worker() -> None:
        cleanup_update_handoff_executable_with_retries(
            base_dir=base_dir,
            executable_path=executable_path,
            current_executable=current_executable,
            attempts=attempts,
            delay_seconds=delay_seconds,
            log=log,
        )

    thread = thread_factory(target=worker, daemon=True)
    try:
        thread.start()
    except Exception as exc:
        if callable(log):
            log(f"handoff helper cleanup thread failed: {exc!r}")
    return thread


def is_update_handoff_argv(argv: Sequence[str] | None = None) -> bool:
    resolved_argv = list(sys.argv if argv is None else argv)
    return UPDATE_HANDOFF_ARG in resolved_argv


def get_update_handoff_state_arg(argv: Sequence[str] | None = None) -> str:
    resolved_argv = list(sys.argv if argv is None else argv)
    try:
        idx = resolved_argv.index(UPDATE_HANDOFF_ARG)
        return str(resolved_argv[idx + 1])
    except Exception:
        return ""


def wait_for_update_handoff_ack(
    state_path: str | os.PathLike[str],
    *,
    timeout_seconds: float = UPDATE_HANDOFF_ACK_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 0.05,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() <= deadline:
        state = read_update_handoff_state(state_path)
        if state.get("acknowledged_at") or state.get("status") in {"running", "complete", "failed"}:
            return True
        time.sleep(max(0.01, float(poll_interval_seconds)))
    return False


def append_update_log(log_path: str | os.PathLike[str], message: str) -> None:
    try:
        resolved_path = Path(log_path)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        with resolved_path.open("a", encoding="utf-8") as fp:
            fp.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass
    return


class UpdateHandoffProgressUi:
    def __init__(self, *, log_path: str | os.PathLike[str] = "") -> None:
        self._log_path = str(log_path or "")
        self._root = None
        self._stage_label = None
        self._detail_label = None
        self._progressbar = None
        self._retry_button = None
        self._manual_button = None
        self._close_button = None
        self.retry_requested = False
        self._closed = False
        return

    def show(self, snapshot: dict[str, Any]) -> None:
        if self._root is not None:
            self.set_snapshot(snapshot)
            return
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            return
        try:
            root = tk.Tk()
            self._closed = False
            root.title(str(snapshot.get("title") or UPDATE_PROGRESS_TITLE))
            root.geometry("460x220")
            root.resizable(False, False)
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass

            frame = tk.Frame(root, padx=16, pady=14)
            root.protocol("WM_DELETE_WINDOW", self.close)
            frame.pack(fill="both", expand=True)
            title = tk.Label(
                frame,
                text="Windows Supporter 업데이트 중",
                font=("Segoe UI", 11, "bold"),
                anchor="w",
            )
            title.pack(fill="x")
            stage = tk.Label(frame, text="", font=("Segoe UI", 9), anchor="w")
            stage.pack(fill="x", pady=(12, 4))
            progress = ttk.Progressbar(
                frame,
                orient="horizontal",
                mode="determinate",
                maximum=100,
            )
            progress.pack(fill="x")
            detail = tk.Label(
                frame,
                text="",
                font=("Segoe UI", 9),
                anchor="w",
                justify="left",
                wraplength=410,
            )
            detail.pack(fill="x", pady=(10, 8))

            buttons = tk.Frame(frame)
            buttons.pack(fill="x")
            log_button = ttk.Button(
                buttons,
                text=UPDATE_PROGRESS_LOG_BUTTON_TEXT,
                command=self._open_log,
                width=11,
            )
            log_button.pack(side="right")
            close_button = ttk.Button(
                buttons,
                text=UPDATE_PROGRESS_CLOSE_BUTTON_TEXT,
                command=self.close,
                width=9,
            )
            close_button.pack(side="right", padx=(0, 6))
            manual_button = ttk.Button(
                buttons,
                text=UPDATE_PROGRESS_MANUAL_ACTION_TEXT,
                command=self._show_manual_action,
                width=11,
            )
            manual_button.pack(side="right", padx=(0, 6))
            retry_button = ttk.Button(
                buttons,
                text=UPDATE_PROGRESS_RETRY_BUTTON_TEXT,
                command=self._request_retry,
                width=9,
            )
            retry_button.pack(side="right", padx=(0, 6))

            self._root = root
            self._stage_label = stage
            self._detail_label = detail
            self._progressbar = progress
            self._retry_button = retry_button
            self._manual_button = manual_button
            self._close_button = close_button
            self.set_snapshot(snapshot)
            self.pump()
        except Exception:
            self._root = None
        return

    def set_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self._root is None:
            self.show(snapshot)
            return
        try:
            state = str(snapshot.get("state") or "")
            self._root.title(str(snapshot.get("title") or UPDATE_PROGRESS_TITLE))
            if self._stage_label is not None:
                self._stage_label.configure(text=str(snapshot.get("label") or ""))
            if self._detail_label is not None:
                self._detail_label.configure(text=str(snapshot.get("detail") or ""))
            if self._progressbar is not None:
                self._progressbar.configure(value=int(snapshot.get("percent") or 0))
            failure = state == "failed"
            if self._retry_button is not None:
                self._retry_button.configure(
                    state="normal" if failure and bool(snapshot.get("can_retry")) else "disabled"
                )
            for button in (self._manual_button, self._close_button):
                if button is not None:
                    button.configure(state="normal" if failure else "disabled")
            self.pump()
        except Exception:
            pass
        return

    def pump(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            root.update_idletasks()
            root.update()
        except Exception:
            pass
        return

    def close(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            root.destroy()
        except Exception:
            pass
        self._root = None
        self._closed = True
        return

    def _open_log(self) -> None:
        if not self._log_path:
            return
        try:
            os.startfile(self._log_path)
        except Exception:
            pass
        return

    def _request_retry(self) -> None:
        self.retry_requested = True
        return

    def wait_for_retry_or_close(self) -> bool:
        if self._root is None:
            return False
        while not self._closed and not self.retry_requested:
            self.pump()
            time.sleep(0.1)
        should_retry = bool(self.retry_requested)
        self.retry_requested = False
        return should_retry

    def _show_manual_action(self) -> None:
        try:
            from tkinter import messagebox

            messagebox.showinfo(
                UPDATE_PROGRESS_TITLE,
                f"수동 조치가 필요하면 로그를 확인한 뒤 Git 상태와 build.bat 실행 결과를 점검해 주세요.\n로그: {self._log_path}",
            )
        except Exception:
            pass
        return


def run_no_window_with_progress(
    argv: list[str],
    *,
    subprocess_module=subprocess,
    progress_ui: UpdateHandoffProgressUi | None = None,
    progress_line_callback: Callable[[str], None] | None = None,
    pump_interval_seconds: float = 0.1,
    **kwargs: Any,
) -> Any:
    popen_factory = getattr(subprocess_module, "Popen", None)
    if callable(popen_factory) and bool(kwargs.get("capture_output")) and bool(kwargs.get("text")):
        run_kwargs = dict(kwargs)
        timeout = run_kwargs.pop("timeout", None)
        check = bool(run_kwargs.pop("check", False))
        run_kwargs.pop("capture_output", None)
        run_kwargs.setdefault("stdout", getattr(subprocess_module, "PIPE", subprocess.PIPE))
        run_kwargs.setdefault("stderr", getattr(subprocess_module, "STDOUT", subprocess.STDOUT))
        for key, value in build_no_window_subprocess_kwargs(subprocess_module).items():
            run_kwargs.setdefault(key, value)
        process = popen_factory(argv, **run_kwargs)
        output_parts: list[str] = []
        deadline = (
            time.monotonic() + float(timeout)
            if timeout is not None and float(timeout) > 0
            else None
        )
        stdout = getattr(process, "stdout", None)
        line_queue: queue.Queue[Any] = queue.Queue()
        reader_done = object()

        def read_output() -> None:
            if stdout is None:
                line_queue.put(reader_done)
                return
            try:
                while True:
                    line = stdout.readline()
                    if not line:
                        break
                    if isinstance(line, bytes):
                        line = line.decode(errors="replace")
                    line_queue.put(str(line))
            except Exception:
                pass
            finally:
                line_queue.put(reader_done)
            return

        threading.Thread(target=read_output, daemon=True).start()
        output_complete = False
        while True:
            if deadline is not None and time.monotonic() > deadline:
                killer = getattr(process, "kill", None)
                if callable(killer):
                    killer()
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            while True:
                try:
                    item = line_queue.get_nowait()
                except queue.Empty:
                    break
                if item is reader_done:
                    output_complete = True
                    continue
                output_parts.append(str(item))
                if callable(progress_line_callback):
                    progress_line_callback(str(item))
                if progress_ui is not None:
                    progress_ui.pump()
            poll = getattr(process, "poll", None)
            returncode = poll() if callable(poll) else getattr(process, "returncode", None)
            if returncode is not None and output_complete:
                break
            if progress_ui is not None:
                progress_ui.pump()
            time.sleep(max(0.01, float(pump_interval_seconds)))
        wait = getattr(process, "wait", None)
        returncode = getattr(process, "returncode", None)
        if callable(wait) and returncode is None:
            returncode = wait(timeout=0)
        returncode = int(returncode if returncode is not None else 0)
        stdout_text = "".join(output_parts)
        if check and returncode != 0:
            raise subprocess.CalledProcessError(returncode, argv, output=stdout_text)
        return subprocess.CompletedProcess(argv, returncode, stdout_text, "")

    holder: dict[str, Any] = {}

    def worker() -> None:
        try:
            holder["result"] = run_no_window(
                argv,
                subprocess_module=subprocess_module,
                **kwargs,
            )
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while thread.is_alive():
        if progress_ui is not None:
            progress_ui.pump()
        thread.join(max(0.01, float(pump_interval_seconds)))
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


def run_update_handoff(
    state_path: str | os.PathLike[str],
    *,
    subprocess_module=subprocess,
    launch=popen_no_window,
    progress_ui_factory=UpdateHandoffProgressUi,
    max_attempts: int = 2,
) -> int:
    state_path = str(state_path)
    state = read_update_handoff_state(state_path)
    repo_root = str(state.get("repo_root") or "").strip()
    log_path = str(state.get("log_path") or get_update_log_path())
    if not repo_root:
        update_handoff_state(
            state_path,
            status="failed",
            failed_step="handoff state",
            error="repo_root is missing",
        )
        return 1

    progress_ui = progress_ui_factory(log_path=log_path) if callable(progress_ui_factory) else None
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        published_build_labels: set[str] = set()

        def publish_build_line(line: str) -> None:
            publish_build_output_progress(
                line,
                progress_ui=progress_ui,
                state_path=state_path,
                seen=published_build_labels,
            )

        build_progress = build_update_progress_snapshot(
            "build",
            state="running",
            detail=f"build.bat를 실행합니다. (시도 {attempt}/{attempts})",
        )
        if progress_ui is not None:
            progress_ui.show(build_progress)
        update_handoff_state(
            state_path,
            status="running",
            acknowledged_at=time.time(),
            attempt=attempt,
            progress=build_progress,
        )
        append_update_log(log_path, f"handoff attempt {attempt} acknowledged")
        try:
            env = dict(os.environ)
            env["WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN"] = "1"
            env["GIT_TERMINAL_PROMPT"] = "0"
            result = run_no_window_with_progress(
                ["cmd", "/c", "build.bat"],
                subprocess_module=subprocess_module,
                progress_ui=progress_ui,
                progress_line_callback=publish_build_line,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                env=env,
                timeout=UPDATE_HANDOFF_COMMAND_TIMEOUT_SECONDS,
            )
            output = str(getattr(result, "stdout", "") or "")
            error_output = str(getattr(result, "stderr", "") or "")
            if output:
                append_update_log(log_path, output.strip())
            if error_output:
                append_update_log(log_path, error_output.strip())
            publish_build_output_progress(
                output,
                progress_ui=progress_ui,
                state_path=state_path,
                seen=published_build_labels,
            )
            if int(getattr(result, "returncode", 1) or 0) != 0:
                raise RuntimeError(f"build.bat failed with exit code {getattr(result, 'returncode', 1)}")

            relaunch_progress = build_update_progress_snapshot("relaunch", state="running")
            if progress_ui is not None:
                progress_ui.set_snapshot(relaunch_progress)
            update_handoff_state(
                state_path,
                status="running",
                progress=relaunch_progress,
            )
            exe_path = str(Path(repo_root) / "windows-supporter.exe")
            proc = launch([exe_path], cwd=repo_root)
            if proc is None:
                raise RuntimeError("failed to relaunch windows-supporter.exe")

            complete_progress = build_update_progress_snapshot("complete", state="complete")
            if progress_ui is not None:
                progress_ui.set_snapshot(complete_progress)
            update_handoff_state(
                state_path,
                status="complete",
                completed_at=time.time(),
                progress=complete_progress,
            )
            append_update_log(log_path, "handoff completed")
            if progress_ui is not None:
                progress_ui.close()
            return 0
        except Exception as exc:
            can_retry = attempt < attempts
            failed_progress = build_update_progress_snapshot(
                "failed",
                state="failed",
                detail=str(exc),
                failed_step="build/relaunch handoff",
                can_retry=can_retry,
                can_manual_action=True,
            )
            if progress_ui is not None:
                progress_ui.set_snapshot(failed_progress)
            update_handoff_state(
                state_path,
                status="failed",
                failed_at=time.time(),
                failed_step="build/relaunch handoff",
                error=str(exc),
                attempt=attempt,
                progress=failed_progress,
            )
            append_update_log(log_path, f"handoff attempt {attempt} failed: {exc}")
            if can_retry and progress_ui is not None and progress_ui.wait_for_retry_or_close():
                continue
            return 1
    return 1


def run_update_handoff_from_argv(argv: Sequence[str] | None = None) -> bool:
    if not is_update_handoff_argv(argv):
        return False
    state_path = get_update_handoff_state_arg(argv)
    raise SystemExit(run_update_handoff(state_path))


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
    CHECK_INTERVAL_MS = 10 * 60 * 1000

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
        handoff_path_provider=get_update_handoff_path,
        handoff_writer=write_update_handoff_state,
        handoff_command_builder=build_update_handoff_command,
        handoff_ack_waiter=wait_for_update_handoff_ack,
        timestamp_provider=lambda: time.strftime("%Y%m%d-%H%M%S"),
        worktree_runner=subprocess.run,
        settings_path_provider=get_update_settings_path,
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
        self._handoff_path_provider = handoff_path_provider
        self._handoff_writer = handoff_writer
        self._handoff_command_builder = handoff_command_builder
        self._handoff_ack_waiter = handoff_ack_waiter
        self._timestamp_provider = timestamp_provider
        self._worktree_runner = worktree_runner
        self._settings_path_provider = settings_path_provider
        self._settings_path = Path(self._settings_path_provider())
        self._settings = load_update_settings(self._settings_path)
        self._session = UpdatePromptSession()
        self._worker_active = False
        self._state = "idle"
        self._current_tag = ""
        self._latest_tag = ""
        self._last_error = ""
        self._working_tree_state = UpdateWorkingTreeState()
        self._progress_snapshot = build_update_progress_snapshot("idle", state="idle")
        self._preflight_result: dict[str, Any] = {}
        self._scheduled_after_id = None
        return

    def start(self) -> None:
        if self._mark_unavailable_if_needed():
            return
        if not self._settings.auto_check_enabled:
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
        self._progress_snapshot = build_update_progress_snapshot("checking", state="checking")
        self._notify_status_changed()

        def worker() -> None:
            candidate = None
            working_tree = UpdateWorkingTreeState()
            error = ""
            try:
                candidate, working_tree, error = self._collect_update_candidate()
            except Exception as exc:
                error = repr(exc)
            self._post_ui(
                lambda: self._handle_check_result(candidate, working_tree, error, manual)
            )

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
            "auto_update": self.get_settings_snapshot(),
            "working_tree": {
                "has_source_changes": self._working_tree_state.has_source_changes,
                "has_cleanup_targets": self._working_tree_state.has_cleanup_targets,
                "is_diverged": self._working_tree_state.is_diverged,
                "local_only_count": self._working_tree_state.local_only_count,
                "remote_only_count": self._working_tree_state.remote_only_count,
            },
            "progress": dict(self._progress_snapshot),
            "preflight": dict(self._preflight_result),
        }

    def set_status_changed_callback(self, callback) -> None:
        self._status_changed_callback = callback
        return

    def get_settings_snapshot(self) -> dict[str, Any]:
        snapshot = self._settings.as_snapshot()
        snapshot["auto_update_available"] = self._state != "unavailable"
        snapshot["unavailable_reason"] = self._last_error if self._state == "unavailable" else ""
        return snapshot

    def update_settings(self, data: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"
        try:
            next_settings = normalize_update_settings(
                data,
                settings_path=self._settings_path,
                current=self._settings,
            )
            save_update_settings(self._settings_path, next_settings)
        except Exception as exc:
            return False, str(exc)
        self._settings = next_settings
        if self._settings.auto_check_enabled:
            if self._mark_unavailable_if_needed():
                self._cancel_scheduled_check()
            else:
                self._schedule_check(self._settings.check_interval_ms)
        else:
            self._cancel_scheduled_check()
        self._notify_status_changed()
        return True, None

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
        self._progress_snapshot = build_update_progress_snapshot(
            "failed",
            state="unavailable",
            detail=message,
            failed_step="업데이트 지원 여부 확인",
            can_manual_action=True,
        )
        self._notify_status_changed()
        return True

    def _schedule_check(self, delay_ms: int) -> None:
        self._cancel_scheduled_check()
        try:
            self._scheduled_after_id = self._root.after(int(delay_ms), self._scheduled_check)
        except Exception:
            self._scheduled_after_id = None
            pass
        return

    def _cancel_scheduled_check(self) -> None:
        after_id = self._scheduled_after_id
        self._scheduled_after_id = None
        if after_id is None:
            return
        cancel = getattr(self._root, "after_cancel", None)
        if not callable(cancel):
            return
        try:
            cancel(after_id)
        except Exception:
            pass
        return

    def _scheduled_check(self) -> None:
        self._scheduled_after_id = None
        if not self._settings.auto_check_enabled:
            return
        self.check_now(manual=False)
        if self._settings.auto_check_enabled:
            self._schedule_check(self._settings.check_interval_ms)
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

    def _collect_update_candidate(
        self,
    ) -> tuple[UpdateCandidate | None, UpdateWorkingTreeState, str]:
        describe = self._git_output(["git", "describe", "--tags", "--long", "--match", "v[0-9]*"])
        current_tag = resolve_current_tag(
            app_version=self._app_version_provider(),
            git_describe=describe,
        )
        self._current_tag = current_tag
        if not current_tag:
            return None, UpdateWorkingTreeState(), "current tag could not be resolved"

        remote_output = self._git_output(build_remote_tag_check_command())
        remote_tags = parse_remote_tag_refs(remote_output)
        candidate = select_update_candidate(current_tag=current_tag, remote_tags=remote_tags)
        working_tree = self._inspect_working_tree_state() if candidate is not None else UpdateWorkingTreeState()
        return candidate, working_tree, ""

    def _git_output(self, argv: list[str]) -> str:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = run_no_window(
                argv,
                subprocess_module=self._subprocess,
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
        working_tree: UpdateWorkingTreeState | bool,
        error: str,
        manual: bool,
    ) -> None:
        self._worker_active = False
        self._last_error = str(error or "")
        if isinstance(working_tree, UpdateWorkingTreeState):
            self._working_tree_state = working_tree
        else:
            self._working_tree_state = (
                UpdateWorkingTreeState(source_status=("legacy-dirty",))
                if bool(working_tree)
                else UpdateWorkingTreeState()
            )
        if candidate is None:
            self._latest_tag = ""
            self._state = "error" if error else "current"
            self._progress_snapshot = (
                build_update_progress_snapshot(
                    "failed",
                    state="failed",
                    detail=self._last_error,
                    failed_step="업데이트 확인",
                    can_retry=True,
                    can_manual_action=True,
                )
                if error
                else build_update_progress_snapshot("complete", state="current")
            )
            self._notify_status_changed()
            if manual:
                self._show_info("Windows Supporter 업데이트", self._manual_no_update_message())
            return

        self._latest_tag = candidate.tag
        self._state = "update_available"
        self._progress_snapshot = build_update_progress_snapshot(
            "available",
            state="update_available",
            detail=f"새 버전 {candidate.tag}을 설치할 수 있습니다.",
        )
        self._notify_status_changed()
        if not manual and not self._session.should_prompt(candidate.tag):
            return

        if self._ask_update(candidate):
            try:
                working_tree = self._inspect_working_tree_state()
            except Exception as exc:
                self._state = "error"
                self._last_error = f"Git 상태를 확인할 수 없습니다: {exc}"
                self._progress_snapshot = build_update_progress_snapshot(
                    "failed",
                    state="failed",
                    detail=self._last_error,
                    failed_step="Git 상태 확인",
                    can_retry=True,
                    can_manual_action=True,
                )
                self._notify_status_changed()
                return
            self._working_tree_state = working_tree
            if working_tree.has_source_changes:
                self._show_warning(
                    "Windows Supporter 업데이트",
                    UPDATE_SOURCE_CHANGE_NOTICE,
                )
            if not self._prepare_repository_for_update(working_tree):
                return
            self.launch_update()
        else:
            self._session.dismiss(candidate.tag)
            self._progress_snapshot = build_update_progress_snapshot("idle", state="idle")
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
        return self._inspect_working_tree_state().has_source_changes

    def _has_update_cleanup_targets(self) -> bool:
        return self._inspect_working_tree_state().has_cleanup_targets

    def _inspect_working_tree_state(self) -> UpdateWorkingTreeState:
        source_status = parse_git_status_porcelain(
            self._git_output(["git", "status", "--porcelain", "--untracked-files=all"])
        )
        cleanup_targets = parse_clean_probe_output(
            self._git_output(build_allowed_clean_probe_command())
        )
        local_count, remote_count = self._read_divergence_counts()
        local_commits, remote_commits = self._read_divergence_commits()
        return UpdateWorkingTreeState(
            source_status=source_status,
            cleanup_targets=cleanup_targets,
            local_only_count=local_count,
            remote_only_count=remote_count,
            local_only_commits=local_commits,
            remote_only_commits=remote_commits,
        )

    def _read_divergence_counts(self) -> tuple[int, int]:
        output = self._git_output(build_divergence_count_command())
        return parse_divergence_counts(output)

    def _read_divergence_commits(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        output = self._git_output(build_divergence_log_command())
        return parse_left_right_log(output)

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

    def _ask_force_clean(self, working_tree: UpdateWorkingTreeState) -> bool:
        try:
            from tkinter import messagebox

            return bool(
                messagebox.askyesno(
                    "Windows Supporter 업데이트",
                    build_force_clean_approval_message(working_tree),
                )
            )
        except Exception:
            return False

    def _next_update_timestamp(self) -> str:
        try:
            return str(self._timestamp_provider()).strip()
        except Exception:
            return time.strftime("%Y%m%d-%H%M%S")

    def _create_backup_branch(self, timestamp: str) -> str:
        try:
            short_sha = self._git_output(build_short_head_command("main"))
        except Exception:
            short_sha = "head"
        last_error: Exception | None = None
        for suffix in range(0, 10):
            branch_name = build_backup_branch_name(timestamp, short_sha, suffix=suffix)
            try:
                self._git_output(build_backup_branch_command(branch_name, "main"))
                return branch_name
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("backup branch could not be created")

    def _prepare_repository_for_update(self, working_tree: UpdateWorkingTreeState) -> bool:
        timestamp = self._next_update_timestamp()
        preflight: dict[str, Any] = {
            "timestamp": timestamp,
            "force_clean_approved": False,
            "stash_output": "",
            "backup_branch": "",
            "cleaned_targets": list(working_tree.cleanup_targets),
        }
        try:
            self._progress_snapshot = build_update_progress_snapshot("fetch", state="running")
            self._notify_status_changed()
            self._git_output(build_fetch_origin_command())

            fresh_tree = self._inspect_working_tree_state()
            self._working_tree_state = fresh_tree
            preflight["cleaned_targets"] = list(fresh_tree.cleanup_targets)

            requires_force_clean = fresh_tree.is_diverged or fresh_tree.has_local_only_commits
            if requires_force_clean:
                self._progress_snapshot = build_update_progress_snapshot(
                    "preflight",
                    state="await_force_clean_approval",
                    detail="로컬 전용 커밋을 보존한 뒤 main을 origin/main 기준으로 동기화해야 합니다.",
                )
                self._notify_status_changed()
                if not self._ask_force_clean(fresh_tree):
                    self._state = "cancelled"
                    self._last_error = UPDATE_FORCE_CLEAN_REJECTED_NOTICE
                    self._progress_snapshot = build_update_progress_snapshot(
                        "failed",
                        state="cancelled",
                        detail=UPDATE_FORCE_CLEAN_REJECTED_NOTICE,
                        failed_step="강제정리 승인",
                        can_manual_action=True,
                    )
                    self._notify_status_changed()
                    return False
                preflight["force_clean_approved"] = True

            if fresh_tree.has_source_changes:
                self._progress_snapshot = build_update_progress_snapshot("stash", state="running")
                self._notify_status_changed()
                stash_message = f"windows-supporter auto update {timestamp}"
                preflight["stash_output"] = self._git_output(build_stash_command(stash_message))

            if fresh_tree.has_local_only_commits:
                preflight["backup_branch"] = self._create_backup_branch(timestamp)

            self._progress_snapshot = build_update_progress_snapshot("sync", state="running")
            self._notify_status_changed()
            self._git_output(build_switch_main_command())
            if fresh_tree.has_local_only_commits or fresh_tree.is_diverged:
                self._git_output(build_reset_main_command())
            elif fresh_tree.has_remote_only_commits:
                self._git_output(build_fast_forward_main_command())

            if fresh_tree.has_cleanup_targets:
                self._progress_snapshot = build_update_progress_snapshot("cleanup", state="running")
                self._notify_status_changed()
                self._git_output(build_allowed_clean_command())
        except Exception as exc:
            self._state = "error"
            self._last_error = f"update preflight failed: {exc}"
            self._progress_snapshot = build_update_progress_snapshot(
                "failed",
                state="failed",
                detail=self._last_error,
                failed_step="업데이트 사전 정리",
                can_retry=True,
                can_manual_action=True,
            )
            self._preflight_result = preflight
            self._notify_status_changed()
            return False

        self._preflight_result = preflight
        return True

    def launch_update(self) -> bool:
        if self._mark_unavailable_if_needed():
            return False
        try:
            handoff_path = Path(self._handoff_path_provider())
            payload = build_update_handoff_payload(
                repo_root=self._repo_root,
                target_tag=self._latest_tag,
                working_tree=self._working_tree_state,
                log_path=get_update_log_path(handoff_path.parent),
                preflight=self._preflight_result,
            )
            self._handoff_writer(handoff_path, payload)
            command = self._handoff_command_builder(handoff_path)
        except Exception as exc:
            self._state = "error"
            self._last_error = f"failed to prepare update handoff: {exc}"
            self._progress_snapshot = build_update_progress_snapshot(
                "failed",
                state="failed",
                detail=self._last_error,
                failed_step="업데이트 실행 준비",
                can_retry=True,
                can_manual_action=True,
            )
            self._notify_status_changed()
            return False

        proc = self._popen(command, cwd=self._repo_root)
        self._state = "updating"
        self._progress_snapshot = build_update_progress_snapshot("handoff", state="running")
        self._notify_status_changed()
        if proc is None:
            self._state = "error"
            self._last_error = "failed to launch update handoff"
            self._progress_snapshot = build_update_progress_snapshot(
                "failed",
                state="failed",
                detail=self._last_error,
                failed_step="업데이트 프로세스 시작",
                can_retry=True,
                can_manual_action=True,
            )
            self._notify_status_changed()
            return False

        if not self._handoff_ack_waiter(handoff_path):
            self._state = "error"
            self._last_error = "update handoff did not acknowledge startup"
            self._progress_snapshot = build_update_progress_snapshot(
                "failed",
                state="failed",
                detail=self._last_error,
                failed_step="업데이트 프로세스 확인",
                can_retry=True,
                can_manual_action=True,
            )
            self._notify_status_changed()
            return False

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


def build_fetch_origin_command(remote: str = "origin") -> list[str]:
    resolved_remote = str(remote or "").strip() or "origin"
    return ["git", "fetch", "--tags", resolved_remote]


def build_switch_main_command(branch: str = "main") -> list[str]:
    return ["git", "switch", str(branch or "main")]


def build_reset_main_command(remote_ref: str = "origin/main") -> list[str]:
    return ["git", "reset", "--hard", str(remote_ref or "origin/main")]


def build_fast_forward_main_command(remote_ref: str = "origin/main") -> list[str]:
    return ["git", "merge", "--ff-only", str(remote_ref or "origin/main")]


def build_short_head_command(ref: str = "main") -> list[str]:
    return ["git", "rev-parse", "--short", str(ref or "main")]


def build_backup_branch_name(timestamp: str, short_sha: str, *, suffix: int = 0) -> str:
    safe_timestamp = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(timestamp or "").strip())
    safe_sha = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(short_sha or "").strip())
    name = f"backup/windows-supporter-auto-update/{safe_timestamp or 'unknown'}-{safe_sha or 'head'}"
    if int(suffix or 0) > 0:
        name = f"{name}-{int(suffix)}"
    return name


def build_backup_branch_command(branch_name: str, ref: str = "main") -> list[str]:
    return ["git", "branch", str(branch_name), str(ref or "main")]


def build_divergence_count_command(
    *,
    local_ref: str = "main",
    remote_ref: str = "origin/main",
) -> list[str]:
    return ["git", "rev-list", "--left-right", "--count", f"{remote_ref}...{local_ref}"]


def build_divergence_log_command(
    *,
    local_ref: str = "main",
    remote_ref: str = "origin/main",
) -> list[str]:
    return [
        "git",
        "log",
        "--oneline",
        "--left-right",
        "--cherry-pick",
        f"{remote_ref}...{local_ref}",
    ]


def parse_git_status_porcelain(output: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in str(output or "").splitlines() if line.strip())


def parse_clean_probe_output(output: str) -> tuple[str, ...]:
    targets: list[str] = []
    prefix = "Would remove "
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(prefix):
            line = line[len(prefix) :].strip()
        targets.append(line)
    return tuple(targets)


def parse_divergence_counts(output: str) -> tuple[int, int]:
    parts = str(output or "").strip().split()
    if len(parts) < 2:
        return 0, 0
    try:
        remote_only = max(0, int(parts[0]))
        local_only = max(0, int(parts[1]))
    except Exception:
        return 0, 0
    return local_only, remote_only


def parse_left_right_log(output: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    local: list[str] = []
    remote: list[str] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker = line[:1]
        commit = line[1:].strip()
        if marker == ">":
            local.append(commit)
        elif marker == "<":
            remote.append(commit)
    return tuple(local), tuple(remote)


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
