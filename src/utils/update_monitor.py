from __future__ import annotations

# noqa: SIZE_OK — legacy updater/UI integration module; recovery logic stays extracted.

import json
import os
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
from src.utils.progress_subprocess import run_no_window_with_progress
from src.utils.runtime_deploy import RuntimeDeployError, deploy_runtime, restart_runtime
from src.utils.subprocess_utils import popen_no_window, run_no_window
from src.utils.update_handoff_recovery import (
    UpdateHandoffError,
    build_relaunch_environment,
    restore_previous_executable,
)
from src.utils.update_settings import (
    get_update_settings_path,
    load_update_settings,
    save_update_settings,
    validate_update_settings_update,
)
from src.utils.worktree_runtime import is_primary_worktree


SEMVER_TAG_RE = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
DESCRIBE_TAG_RE = re.compile(
    r"^(?P<tag>v?\d+\.\d+\.\d+)(?:-\d+-g[0-9a-f]+(?:-dirty)?)?$",
    re.IGNORECASE,
)
DEFAULT_CLEAN_ALLOWLIST = ("build/", "dist/", "*.spec", "*.egg-info/")
KNOWN_GIT_GUI_PROCESS_NAMES = (
    "Fork.exe",
    "GitHubDesktop.exe",
    "SourceTree.exe",
    "GitKraken.exe",
    "TortoiseGitProc.exe",
)
GIT_COMMAND_TIMEOUT_SECONDS = 20
GIT_GUI_CHECK_TIMEOUT_SECONDS = 3
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
GIT_GUI_UPDATE_BLOCKED_MESSAGE = (
    "Git GUI가 이 checkout을 감시 중일 수 있어 자동 업데이트를 중단했습니다. "
    "Fork/GitHub Desktop/SourceTree 같은 Git GUI를 닫은 뒤 다시 업데이트해 주세요."
)
GIT_GUI_UPDATE_CANCELLED_MESSAGE = (
    "Git GUI 종료가 취소되어 업데이트를 중단했습니다. 업데이트 설정에서 다시 확인할 수 있습니다."
)


def find_running_git_gui_processes(
    repo_root: str | os.PathLike[str],
    *,
    subprocess_module=subprocess,
) -> tuple[str, ...]:
    _ = repo_root
    if os.name != "nt":
        return ()
    found: list[str] = []
    for process_name in KNOWN_GIT_GUI_PROCESS_NAMES:
        result = run_no_window(
            [
                "tasklist",
                "/FI",
                f"IMAGENAME eq {process_name}",
                "/NH",
            ],
            subprocess_module=subprocess_module,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_GUI_CHECK_TIMEOUT_SECONDS,
        )
        if int(getattr(result, "returncode", 1) or 0) != 0:
            continue
        output = str(getattr(result, "stdout", "") or "")
        if process_name.lower() in output.lower():
            found.append(process_name)
    return tuple(found)


def _default_git_gui_relaunch_entries(process_names: Sequence[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw_name in process_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        entries.append({"name": name, "command": [name]})
    return entries


def close_running_git_gui_processes(
    process_names: Sequence[str],
    *,
    subprocess_module=subprocess,
) -> dict[str, Any]:
    names = [str(name or "").strip() for name in process_names if str(name or "").strip()]
    result_payload: dict[str, Any] = {
        "closed": [],
        "still_running": [],
        "relaunch": _default_git_gui_relaunch_entries(names),
    }
    if not names or os.name != "nt":
        return result_payload

    names_json = json.dumps(names, ensure_ascii=False)
    script = "\n".join(
        [
            "$ErrorActionPreference = 'SilentlyContinue'",
            "$names = ConvertFrom-Json @'",
            names_json,
            "'@",
            "$records = @()",
            "foreach ($name in $names) {",
            "  $base = [System.IO.Path]::GetFileNameWithoutExtension([string]$name)",
            "  foreach ($proc in Get-Process -Name $base -ErrorAction SilentlyContinue) {",
            "    $exeName = if ([string]::IsNullOrWhiteSpace($proc.Path)) { \"$($proc.ProcessName).exe\" } else { [System.IO.Path]::GetFileName($proc.Path) }",
            "    $records += [pscustomobject]@{ name = $exeName; pid = $proc.Id; path = $proc.Path }",
            "  }",
            "}",
            "foreach ($record in $records) {",
            "  try {",
            "    $proc = Get-Process -Id $record.pid -ErrorAction SilentlyContinue",
            "    if ($proc) { [void]$proc.CloseMainWindow() }",
            "  } catch { }",
            "}",
            "$deadline = (Get-Date).AddSeconds(8)",
            "do {",
            "  $remaining = @()",
            "  foreach ($record in $records) {",
            "    if (Get-Process -Id $record.pid -ErrorAction SilentlyContinue) { $remaining += $record }",
            "  }",
            "  if ($remaining.Count -eq 0) { break }",
            "  Start-Sleep -Milliseconds 250",
            "} while ((Get-Date) -lt $deadline)",
            "$still = @()",
            "foreach ($record in $records) {",
            "  if (Get-Process -Id $record.pid -ErrorAction SilentlyContinue) { $still += $record.name }",
            "}",
            "$relaunch = @()",
            "foreach ($record in $records) {",
            "  if ([string]::IsNullOrWhiteSpace($record.path)) {",
            "    $relaunch += [pscustomobject]@{ name = $record.name; command = @($record.name) }",
            "  } else {",
            "    $relaunch += [pscustomobject]@{ name = $record.name; command = @($record.path) }",
            "  }",
            "}",
            "[pscustomobject]@{",
            "  closed = @($records | ForEach-Object { $_.name })",
            "  still_running = @($still)",
            "  relaunch = @($relaunch)",
            "} | ConvertTo-Json -Depth 6 -Compress",
        ]
    )
    try:
        completed = run_no_window(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            subprocess_module=subprocess_module,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except Exception as exc:
        result_payload["error"] = repr(exc)
        return result_payload

    stdout = str(getattr(completed, "stdout", "") or "").strip()
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        result_payload["error"] = str(getattr(completed, "stderr", "") or stdout).strip()
        return result_payload
    if not stdout:
        return result_payload
    try:
        parsed = json.loads(stdout)
    except Exception as exc:
        result_payload["error"] = f"failed to parse Git GUI close output: {exc}"
        return result_payload
    if not isinstance(parsed, dict):
        return result_payload

    closed = parsed.get("closed", [])
    still_running = parsed.get("still_running", [])
    relaunch = parsed.get("relaunch", [])
    result_payload["closed"] = [str(item) for item in closed if str(item).strip()]
    result_payload["still_running"] = [
        str(item) for item in still_running if str(item).strip()
    ]
    if isinstance(relaunch, dict):
        relaunch = [relaunch]
    if isinstance(relaunch, list):
        relaunch_entries: list[dict[str, Any]] = []
        for item in relaunch:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            command_value = item.get("command")
            if isinstance(command_value, str):
                command = [command_value]
            elif isinstance(command_value, list):
                command = [str(part) for part in command_value if str(part).strip()]
            else:
                command = []
            if name and command:
                relaunch_entries.append({"name": name, "command": command})
        if relaunch_entries:
            result_payload["relaunch"] = relaunch_entries
    return result_payload

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
    stage_id: str
    label: str
    detail: str
    activity: str
    percent: int


BUILD_OUTPUT_LAST_PERCENT_PREFIX = "__last_percent__:"
BUILD_OUTPUT_STEP_LOG_PREFIX = "WINDOWS_SUPPORTER_STEP_LOG="
BUILD_LOG_TAIL_POLL_SECONDS = 0.05
BUILD_LOG_TAIL_STOP_WAIT_SECONDS = 1.0
UPDATE_PROGRESS_VISIBLE_TEXT_LIMIT = 320
UPDATE_PROGRESS_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\\\))")
UPDATE_PROGRESS_UNSAFE_CHAR_RE = re.compile(
    r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)


UPDATE_PROGRESS_STEPS: tuple[UpdateProgressStep, ...] = (
    UpdateProgressStep("idle", "업데이트 대기", "업데이트 확인을 기다리는 중입니다.", 0),
    UpdateProgressStep("checking", "업데이트 확인 중", "현재 버전과 원격 릴리스를 확인합니다.", 6),
    UpdateProgressStep("available", "업데이트 준비 완료", "새 버전을 설치할 수 있습니다.", 14),
    UpdateProgressStep("accepted", "업데이트 요청 접수", "선택한 버전을 설치할 준비를 시작합니다.", 20),
    UpdateProgressStep("preflight", "업데이트 사전 점검 중", "Git 상태와 로컬 변경 여부를 확인합니다.", 28),
    UpdateProgressStep("stash", "변경 사항 스태시 중", "커밋되지 않은 변경을 stash로 보존합니다.", 38),
    UpdateProgressStep("cleanup", "빌드 산출물 정리 중", "무시된 빌드 산출물을 allowlist 범위에서 정리합니다.", 46),
    UpdateProgressStep("fetch", "원격 변경 확인 중", "origin 태그와 main 브랜치 정보를 가져옵니다.", 54),
    UpdateProgressStep("sync", "main 동기화 중", "main 브랜치를 업데이트 기준으로 맞춥니다.", 62),
    UpdateProgressStep("handoff", "업데이트 실행 준비 중", "빌드와 재실행을 맡을 업데이트 프로세스를 준비합니다.", 68),
    UpdateProgressStep("handoff_start", "업데이트 프로세스 시작", "업데이트 전용 프로세스를 시작했습니다.", 0),
    UpdateProgressStep("shutdown", "기존 앱 정리 중", "기존 Windows Supporter와 하위 프로세스를 정리합니다.", 8),
    UpdateProgressStep("build_prepare", "빌드 준비 중", "build.bat 실행 환경을 준비합니다.", 14),
    UpdateProgressStep("build", "빌드 실행 중", "build.bat를 실행합니다.", 74),
    UpdateProgressStep("relaunch", "Windows Supporter 재실행 중", "새 실행 파일과 닫았던 Git 앱을 시작합니다.", 94),
    UpdateProgressStep("complete", "업데이트 완료", "업데이트가 완료되었습니다.", 100),
    UpdateProgressStep("failed", "업데이트 실패", "실패 단계와 로그를 확인해 주세요.", 100),
)
UPDATE_PROGRESS_STEP_BY_KEY = {step.key: step for step in UPDATE_PROGRESS_STEPS}
BUILD_OUTPUT_PROGRESS_RULES: tuple[BuildOutputProgressRule, ...] = (
    BuildOutputProgressRule(
        "Stopping stale PyInstaller workers",
        "stale_workers",
        "빌드 작업자 정리 중",
        "이전 빌드에서 남은 작업을 안전하게 정리합니다.",
        "남아 있던 빌드 작업을 정리했습니다.",
        20,
    ),
    BuildOutputProgressRule(
        "Preparing pinned uv",
        "uv_prepare",
        "빌드 도구 준비 중",
        "프로젝트에 고정된 빌드 도구를 준비합니다.",
        "빌드 도구를 준비했습니다.",
        24,
    ),
    BuildOutputProgressRule(
        "Syncing uv environment",
        "uv_sync",
        "빌드 환경 동기화 중",
        "검증된 의존성으로 빌드 환경을 맞춥니다.",
        "빌드 환경을 동기화했습니다.",
        32,
    ),
    BuildOutputProgressRule(
        "Preparing bundled Playwright",
        "browser_runtime",
        "브라우저 구성 요소 준비 중",
        "앱에 포함할 브라우저 구성 요소를 확인합니다.",
        "브라우저 구성 요소를 준비했습니다.",
        40,
    ),
    BuildOutputProgressRule(
        "Cleaning prior PyInstaller",
        "clean_previous",
        "이전 빌드 정리 중",
        "새 빌드를 위해 이전 임시 산출물을 정리합니다.",
        "이전 빌드 산출물을 정리했습니다.",
        48,
    ),
    BuildOutputProgressRule(
        "Generating version metadata",
        "version_metadata",
        "버전 정보 생성 중",
        "새 실행 파일에 버전 정보를 반영합니다.",
        "버전 정보를 생성했습니다.",
        58,
    ),
    BuildOutputProgressRule(
        "Building main.py",
        "build_executable",
        "실행 파일 빌드 중",
        "새 Windows Supporter 실행 파일을 만들고 있습니다.",
        "새 실행 파일을 빌드했습니다.",
        74,
    ),
    BuildOutputProgressRule(
        "Validating PyInstaller archive",
        "validate_archive",
        "실행 파일 검증 중",
        "필수 구성 요소가 실행 파일에 포함됐는지 확인합니다.",
        "실행 파일 구성을 검증했습니다.",
        80,
    ),
    BuildOutputProgressRule(
        "Artifact-only build complete",
        "candidate_ready",
        "실행 파일 준비 완료",
        "검증된 후보 실행 파일을 배포 도우미에 넘길 준비를 합니다.",
        "후보 실행 파일 검증을 마쳤습니다.",
        88,
    ),
    BuildOutputProgressRule(
        "Remove build byproducts",
        "cleanup_build",
        "임시 파일 정리 중",
        "업데이트에 사용한 임시 빌드 파일을 정리합니다.",
        "임시 빌드 파일을 정리했습니다.",
        90,
    ),
    BuildOutputProgressRule(
        "Deploying verified",
        "transactional_deploy",
        "실행 파일 안전 배포 중",
        "기존 앱을 백업하고 새 앱의 준비 상태까지 확인합니다.",
        "새 앱의 실행 준비 상태를 확인했습니다.",
        92,
    ),
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
    percent: int | None = None,
    log_path: str = "",
    failed_step: str = "",
    can_retry: bool = False,
    can_manual_action: bool = False,
) -> dict[str, Any]:
    step = get_update_progress_step(step_key)
    resolved_state = str(state or ("failed" if step.key == "failed" else "idle")).strip()
    resolved_percent = max(
        0,
        min(100, int(step.percent if percent is None else percent)),
    )
    return {
        "title": UPDATE_PROGRESS_FAILURE_TITLE if resolved_state == "failed" else UPDATE_PROGRESS_TITLE,
        "state": resolved_state,
        "step_key": step.key,
        "label": step.label,
        "detail": str(detail if detail is not None else step.detail),
        "percent": resolved_percent,
        "progressbar": {
            "visible": True,
            "mode": "determinate",
            "value": resolved_percent,
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


def _normalize_update_progress_text(text: Any, *, limit: int = UPDATE_PROGRESS_VISIBLE_TEXT_LIMIT) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = UPDATE_PROGRESS_ANSI_ESCAPE_RE.sub("", value)
    value = UPDATE_PROGRESS_UNSAFE_CHAR_RE.sub(" ", value)
    value = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n"))
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    maximum = max(1, int(limit or UPDATE_PROGRESS_VISIBLE_TEXT_LIMIT))
    if len(value) > maximum:
        value = value[: maximum - 1].rstrip() + "…"
    return value


def _build_output_activity(rule: BuildOutputProgressRule) -> dict[str, str]:
    return {
        "source": "build",
        "id": rule.stage_id,
        "line": rule.activity,
    }


def _attach_build_output_activity(
    snapshot: dict[str, Any],
    rule: BuildOutputProgressRule,
) -> dict[str, Any]:
    snapshot["activity"] = _build_output_activity(rule)
    return snapshot


def _get_seen_last_percent(published: set[str]) -> int:
    for item in tuple(published):
        if not item.startswith(BUILD_OUTPUT_LAST_PERCENT_PREFIX):
            continue
        try:
            return int(item.removeprefix(BUILD_OUTPUT_LAST_PERCENT_PREFIX))
        except Exception:
            return 0
    return 0


def _set_seen_last_percent(published: set[str], percent: int) -> None:
    for item in tuple(published):
        if item.startswith(BUILD_OUTPUT_LAST_PERCENT_PREFIX):
            published.discard(item)
    published.add(f"{BUILD_OUTPUT_LAST_PERCENT_PREFIX}{max(0, min(100, int(percent)))}")
    return


def _is_build_output_control_line(line: str) -> bool:
    return str(line or "").strip().startswith(BUILD_OUTPUT_STEP_LOG_PREFIX)


def _extract_build_step_log_path(line: str) -> str:
    text = str(line or "").strip()
    if not text.startswith(BUILD_OUTPUT_STEP_LOG_PREFIX):
        return ""
    return text.removeprefix(BUILD_OUTPUT_STEP_LOG_PREFIX).strip().strip('"')


def _filter_build_output_control_lines(output: str) -> str:
    lines = [
        line
        for line in str(output or "").splitlines()
        if not _is_build_output_control_line(line)
    ]
    return "\n".join(lines)


def build_update_build_output_progress_snapshot(
    line: str,
    *,
    log_path: str = "",
) -> dict[str, Any] | None:
    text = str(line or "").strip()
    if not text:
        return None
    for rule in BUILD_OUTPUT_PROGRESS_RULES:
        if rule.marker not in text:
            continue
        snapshot = build_update_progress_snapshot(
            "build",
            state="running",
            detail=rule.detail,
            log_path=log_path,
        )
        percent = max(0, min(100, int(rule.percent)))
        snapshot["label"] = rule.label
        snapshot["percent"] = percent
        progressbar = snapshot.get("progressbar", {})
        if isinstance(progressbar, dict):
            progressbar["value"] = percent
        return _attach_build_output_activity(snapshot, rule)
    return None


def publish_build_output_progress(
    output: str,
    *,
    progress_ui: Any,
    state_path: str | os.PathLike[str],
    log_path: str = "",
    seen: set[str] | None = None,
) -> None:
    published = seen if seen is not None else set()
    last_percent = _get_seen_last_percent(published)
    for line in str(output or "").splitlines():
        if _is_build_output_control_line(line):
            continue
        snapshot = build_update_build_output_progress_snapshot(line, log_path=log_path)
        if snapshot is None:
            continue
        percent = max(0, min(100, int(snapshot.get("percent") or 0)))
        if percent < last_percent:
            percent = last_percent
            snapshot["percent"] = percent
            progressbar = snapshot.get("progressbar", {})
            if isinstance(progressbar, dict):
                progressbar["value"] = percent
        label = str(snapshot.get("label") or "")
        detail = str(snapshot.get("detail") or "")
        activity = snapshot.get("activity", {})
        activity_line = ""
        activity_id = ""
        if isinstance(activity, dict):
            activity_line = str(activity.get("line") or "")
            activity_id = str(activity.get("id") or "")
        dedupe_key = f"{activity_id}\n{label}\n{detail}\n{activity_line}"
        if dedupe_key in published:
            continue
        published.add(dedupe_key)
        last_percent = max(last_percent, percent)
        _set_seen_last_percent(published, last_percent)
        if progress_ui is not None:
            progress_ui.set_snapshot(snapshot)
        update_handoff_state(state_path, status="running", progress=snapshot)
    return


class _BuildStepLogTailer:
    def __init__(
        self,
        publish_line: Callable[[str], None],
        *,
        poll_seconds: float = BUILD_LOG_TAIL_POLL_SECONDS,
        stop_wait_seconds: float = BUILD_LOG_TAIL_STOP_WAIT_SECONDS,
    ) -> None:
        self._publish_line = publish_line
        self._poll_seconds = max(0.01, float(poll_seconds))
        self._stop_wait_seconds = max(0.1, float(stop_wait_seconds))
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self, path: str | os.PathLike[str]) -> None:
        resolved_path = str(path or "").strip().strip('"')
        if not resolved_path:
            return
        self.stop()
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._tail_loop,
            args=(resolved_path, stop_event),
            daemon=True,
        )
        self._stop_event = stop_event
        self._thread = thread
        thread.start()
        return

    def stop(self) -> None:
        stop_event = self._stop_event
        thread = self._thread
        if stop_event is not None:
            stop_event.set()
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=self._stop_wait_seconds)
        self._stop_event = None
        self._thread = None
        return

    def _tail_loop(self, path: str, stop_event: threading.Event) -> None:
        log_path = Path(path)
        signature: tuple[int, int] | None = None
        position = 0
        partial = ""
        empty_reads_after_stop = 0
        while True:
            data = ""
            try:
                stat = log_path.stat()
                current_signature = (
                    int(getattr(stat, "st_ctime_ns", 0) or 0),
                    int(getattr(stat, "st_ino", 0) or 0),
                )
                if signature is not None and current_signature != signature:
                    position = 0
                    partial = ""
                signature = current_signature
                if int(stat.st_size) < int(position):
                    position = 0
                    partial = ""
                with log_path.open("r", encoding="utf-8", errors="replace") as fp:
                    fp.seek(position)
                    data = fp.read()
                    position = fp.tell()
            except FileNotFoundError:
                signature = None
                position = 0
                partial = ""
            except Exception:
                data = ""

            if data:
                empty_reads_after_stop = 0
                partial = self._publish_complete_lines(partial + data)
            elif stop_event.is_set():
                empty_reads_after_stop += 1
                if empty_reads_after_stop >= 2:
                    break

            time.sleep(self._poll_seconds)

        if partial.strip():
            self._safe_publish(partial.strip())
        return

    def _publish_complete_lines(self, text: str) -> str:
        parts = text.splitlines(keepends=True)
        if parts and not parts[-1].endswith(("\n", "\r")):
            partial = parts.pop()
        else:
            partial = ""
        for item in parts:
            line = item.strip("\r\n")
            if line.strip():
                self._safe_publish(line)
        return partial

    def _safe_publish(self, line: str) -> None:
        try:
            self._publish_line(line)
        except Exception:
            pass
        return


def _extract_git_gui_relaunch_commands(state: dict[str, Any]) -> list[tuple[str, list[str]]]:
    preflight = state.get("preflight", {})
    if not isinstance(preflight, dict):
        return []
    relaunch = preflight.get("git_gui_relaunch", [])
    if isinstance(relaunch, dict):
        relaunch = [relaunch]
    commands: list[tuple[str, list[str]]] = []
    if not isinstance(relaunch, list):
        return commands
    for item in relaunch:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        raw_command = item.get("command")
        if isinstance(raw_command, str):
            command = [raw_command]
        elif isinstance(raw_command, list):
            command = [str(part) for part in raw_command if str(part).strip()]
        else:
            command = []
        if command:
            commands.append((name or command[0], command))
    return commands


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
        if progress_detail and progress_detail != progress_label:
            parts.append((progress_detail, "normal"))
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


def _executable_file_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def cleanup_update_build_artifacts(repo_root: str | os.PathLike[str]) -> list[str]:
    root = Path(repo_root).resolve()
    targets = (
        root / "build",
        root / "dist",
        root / "windows-supporter.spec",
    )
    removed: list[str] = []
    for target in targets:
        is_link = target.is_symlink() or getattr(
            os.path, "isjunction", lambda _path: False
        )(target)
        if not target.exists() and not is_link:
            continue
        if is_link:
            raise UpdateHandoffError(f"refusing to remove linked build artifact: {target}")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        if target.exists():
            raise UpdateHandoffError(f"build artifact cleanup did not remove: {target}")
        removed.append(str(target))
    return removed


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


def _coerce_positive_pid(value: Any) -> int:
    try:
        pid = int(value)
    except Exception:
        return 0
    return pid if pid > 0 else 0


def terminate_process_descendants(
    parent_pid: int,
    *,
    exclude_pids: Sequence[int] | None = None,
    subprocess_module=subprocess,
    timeout_seconds: float = 5.0,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    resolved_parent_pid = _coerce_positive_pid(parent_pid)
    resolved_excludes = tuple(
        pid
        for pid in (
            _coerce_positive_pid(item)
            for item in (exclude_pids or ())
        )
        if pid > 0
    )
    result_payload: dict[str, Any] = {
        "parent_pid": resolved_parent_pid,
        "exclude_pids": list(resolved_excludes),
        "terminated_pids": [],
        "failed_pids": [],
        "skipped": "",
        "error": "",
    }
    if resolved_parent_pid <= 0:
        result_payload["skipped"] = "missing parent pid"
        return result_payload
    if os.name != "nt":
        result_payload["skipped"] = "unsupported platform"
        return result_payload

    exclude_literal = ", ".join(str(pid) for pid in resolved_excludes)
    script = "\n".join(
        [
            "$ErrorActionPreference = 'SilentlyContinue'",
            f"$parentPid = {resolved_parent_pid}",
            f"$exclude = @({exclude_literal})",
            "$childrenByParent = @{}",
            "foreach ($proc in Get-CimInstance Win32_Process) {",
            "  $ppid = [int]$proc.ParentProcessId",
            "  if (-not $childrenByParent.ContainsKey($ppid)) { $childrenByParent[$ppid] = @() }",
            "  $childrenByParent[$ppid] += $proc",
            "}",
            "$targets = New-Object System.Collections.Generic.List[int]",
            "function Add-Descendants([int]$treePid) {",
            "  if (-not $childrenByParent.ContainsKey($treePid)) { return }",
            "  foreach ($child in @($childrenByParent[$treePid])) {",
            "    $childPid = [int]$child.ProcessId",
            "    if ($childPid -eq $PID) { continue }",
            "    if ($exclude -contains $childPid) { continue }",
            "    Add-Descendants $childPid",
            "    if ($childPid -ne $parentPid) { [void]$targets.Add($childPid) }",
            "  }",
            "}",
            "Add-Descendants $parentPid",
            "$terminated = @()",
            "$failed = @()",
            "foreach ($targetPid in @($targets | Select-Object -Unique)) {",
            "  try {",
            "    Stop-Process -Id $targetPid -Force -ErrorAction Stop",
            "    $terminated += $targetPid",
            "  } catch {",
            "    $failed += $targetPid",
            "  }",
            "}",
            "[pscustomobject]@{",
            "  parent_pid = $parentPid",
            "  exclude_pids = @($exclude)",
            "  terminated_pids = @($terminated)",
            "  failed_pids = @($failed)",
            "} | ConvertTo-Json -Depth 4 -Compress",
        ]
    )
    try:
        completed = run_no_window(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            subprocess_module=subprocess_module,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1.0, float(timeout_seconds)),
        )
    except Exception as exc:
        result_payload["error"] = repr(exc)
        if callable(log):
            log(f"process descendant cleanup failed: {exc!r}")
        return result_payload

    stdout = str(getattr(completed, "stdout", "") or "").strip()
    stderr = str(getattr(completed, "stderr", "") or "").strip()
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        result_payload["error"] = stderr or stdout or "cleanup command failed"
        if callable(log):
            log(f"process descendant cleanup failed: {result_payload['error']}")
        return result_payload
    if not stdout:
        return result_payload
    try:
        parsed = json.loads(stdout)
    except Exception as exc:
        result_payload["error"] = f"failed to parse cleanup output: {exc}"
        return result_payload
    if not isinstance(parsed, dict):
        return result_payload
    for key in ("terminated_pids", "failed_pids", "exclude_pids"):
        values = parsed.get(key, [])
        if not isinstance(values, list):
            values = [values]
        result_payload[key] = [
            pid
            for pid in (_coerce_positive_pid(item) for item in values)
            if pid > 0
        ]
    result_payload["parent_pid"] = _coerce_positive_pid(parsed.get("parent_pid", resolved_parent_pid))
    return result_payload


class UpdateHandoffProgressUi:
    def __init__(self, *, log_path: str | os.PathLike[str] = "") -> None:
        self._log_path = str(log_path or "")
        self._root = None
        self._stage_label = None
        self._detail_label = None
        self._percent_label = None
        self._progress_canvas = None
        self._drag_region = None
        self._drag_offset = None
        self._drag_press = None
        self._drag_window_origin = None
        self._dragging = False
        self._drag_threshold = 4
        self._activity_shell = None
        self._activity_title_label = None
        self._activity_timeline = None
        self._activity_rows_frame = None
        self._activity_labels = []
        self._activity_lines = []
        self._activity_ids = []
        self._buttons_frame = None
        self._log_button = None
        self._retry_button = None
        self._manual_button = None
        self._close_button = None
        self._last_percent = 0
        self._last_state = ""
        self._layout_key = None
        self._positioned = False
        self._progress_color = "#2563EB"
        self._preferred_focus_button = None
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
            root.withdraw()
            self._closed = False
            root.title(str(snapshot.get("title") or UPDATE_PROGRESS_TITLE))
            root.overrideredirect(True)
            root.resizable(False, False)
            root.configure(bg="#FFFFFF")
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass

            frame = tk.Frame(root, bg="#FFFFFF")
            root.protocol("WM_DELETE_WINDOW", self._request_close)
            root.bind("<Escape>", self._handle_escape)
            root.bind("<Alt-F4>", self._handle_alt_f4)
            root.bind("<FocusOut>", self._end_drag)
            root.bind("<Unmap>", self._end_drag)
            frame.pack(fill="both", expand=True)
            shell = tk.Frame(
                frame,
                bg="#FFFFFF",
                highlightthickness=1,
                highlightbackground="#D8DEE9",
            )
            shell.pack(fill="both", expand=True)
            accent = tk.Frame(shell, height=3, bg="#2563EB")
            accent.pack(fill="x", side="top")
            body = tk.Frame(shell, padx=24, pady=20, bg="#FFFFFF")
            body.pack(fill="both", expand=True)
            header = tk.Frame(body, bg="#FFFFFF", cursor="fleur")
            header.pack(fill="x")
            title = tk.Label(
                header,
                text="Windows Supporter 업데이트",
                font=("Segoe UI", 15, "bold"),
                anchor="w",
                bg="#FFFFFF",
                fg="#0F172A",
                cursor="fleur",
            )
            title.pack(fill="x")
            subtitle = tk.Label(
                header,
                text="Git 동기화, 빌드, 재실행 상태를 실시간으로 반영합니다.",
                font=("Segoe UI", 9),
                anchor="w",
                bg="#FFFFFF",
                fg="#64748B",
                cursor="fleur",
            )
            subtitle.pack(fill="x", pady=(3, 18))
            for drag_target in (header, title, subtitle):
                drag_target.bind("<ButtonPress-1>", self._start_drag)
                drag_target.bind("<B1-Motion>", self._drag_window)
                drag_target.bind("<ButtonRelease-1>", self._end_drag)

            status_row = tk.Frame(body, bg="#FFFFFF")
            status_row.pack(fill="x")
            stage = tk.Label(
                status_row,
                text="",
                font=("Segoe UI", 11, "bold"),
                anchor="w",
                bg="#FFFFFF",
                fg="#111827",
            )
            stage.pack(side="left", fill="x", expand=True)
            percent = tk.Label(
                status_row,
                text="0%",
                font=("Segoe UI", 12, "bold"),
                anchor="e",
                bg="#FFFFFF",
                fg="#2563EB",
            )
            percent.pack(side="right")

            progress = tk.Canvas(
                body,
                width=550,
                height=14,
                bg="#FFFFFF",
                bd=0,
                highlightthickness=0,
                relief="flat",
            )
            progress.pack(fill="x", pady=(8, 0))
            detail = tk.Label(
                body,
                text="",
                font=("Segoe UI", 9),
                anchor="w",
                justify="left",
                wraplength=550,
                bg="#FFFFFF",
                fg="#334155",
            )
            detail.pack(fill="x", pady=(12, 12))

            activity_shell = tk.Frame(
                body,
                bg="#FFFFFF",
            )
            activity_title = tk.Label(
                activity_shell,
                text="최근 진행 단계",
                font=("Segoe UI", 9, "bold"),
                anchor="w",
                bg="#FFFFFF",
                fg="#475569",
            )
            activity_title.pack(fill="x", pady=(0, 6))
            activity_content = tk.Frame(activity_shell, bg="#FFFFFF")
            activity_content.pack(fill="x")
            activity_timeline = tk.Canvas(
                activity_content,
                width=18,
                height=18,
                bg="#FFFFFF",
                bd=0,
                highlightthickness=0,
                relief="flat",
            )
            activity_timeline.pack(side="left", fill="y", padx=(0, 6))
            activity_rows = tk.Frame(activity_content, bg="#FFFFFF")
            activity_rows.pack(side="left", fill="x", expand=True)
            activity_labels = []
            for _idx in range(3):
                row = tk.Label(
                    activity_rows,
                    text="",
                    font=("Segoe UI", 9),
                    anchor="w",
                    justify="left",
                    bg="#FFFFFF",
                    fg="#64748B",
                    wraplength=506,
                )
                activity_labels.append(row)

            buttons = tk.Frame(body, bg="#FFFFFF")
            buttons.pack(fill="x", side="bottom")
            log_button = ttk.Button(
                buttons,
                text=UPDATE_PROGRESS_LOG_BUTTON_TEXT,
                command=self._open_log,
                width=11,
            )
            close_button = ttk.Button(
                buttons,
                text=UPDATE_PROGRESS_CLOSE_BUTTON_TEXT,
                command=self.close,
                width=9,
            )
            manual_button = ttk.Button(
                buttons,
                text=UPDATE_PROGRESS_MANUAL_ACTION_TEXT,
                command=self._show_manual_action,
                width=11,
            )
            retry_button = ttk.Button(
                buttons,
                text=UPDATE_PROGRESS_RETRY_BUTTON_TEXT,
                command=self._request_retry,
                width=9,
            )

            self._root = root
            self._stage_label = stage
            self._detail_label = detail
            self._percent_label = percent
            self._progress_canvas = progress
            self._drag_region = header
            self._activity_shell = activity_shell
            self._activity_title_label = activity_title
            self._activity_timeline = activity_timeline
            self._activity_rows_frame = activity_rows
            self._activity_labels = activity_labels
            self._buttons_frame = buttons
            self._log_button = log_button
            self._retry_button = retry_button
            self._manual_button = manual_button
            self._close_button = close_button
            self.set_snapshot(snapshot)
            root.deiconify()
            try:
                root.after_idle(root.focus_force)
            except Exception:
                pass
            self.pump()
            self._draw_progress(self._last_percent)
        except Exception:
            self._root = None
        return

    def set_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self._root is None:
            self.show(snapshot)
            return
        try:
            state = str(snapshot.get("state") or "")
            step_key = str(snapshot.get("step_key") or "")
            percent = max(0, min(100, int(snapshot.get("percent") or 0)))
            if step_key == "handoff_start":
                self._activity_lines = []
                self._activity_ids = []
                self._last_percent = 0
            elif state not in {"complete"}:
                percent = max(self._last_percent, percent)
            if state in {"failed", "cancelled"} and percent >= 100 and self._last_percent < 100:
                percent = self._last_percent
            self._root.title(str(snapshot.get("title") or UPDATE_PROGRESS_TITLE))
            if self._stage_label is not None:
                self._stage_label.configure(
                    text=_normalize_update_progress_text(snapshot.get("label"), limit=96)
                )
            if self._detail_label is not None:
                self._detail_label.configure(
                    text=_normalize_update_progress_text(snapshot.get("detail"))
                )
            if self._percent_label is not None:
                self._percent_label.configure(text=f"{percent}%")
            self._record_activity(snapshot)
            failure = state == "failed"
            cancelled = state == "cancelled"
            complete = state == "complete"
            terminal = failure or cancelled or complete
            self._set_button_visible(
                self._retry_button,
                failure and bool(snapshot.get("can_retry")),
            )
            self._set_button_visible(
                self._manual_button,
                (failure or cancelled) and bool(snapshot.get("can_manual_action")),
            )
            self._set_button_visible(self._close_button, terminal)
            self._set_button_visible(
                self._log_button,
                bool(snapshot.get("can_open_log")) or failure or cancelled,
                side="left",
            )
            self._progress_color = (
                "#16A34A"
                if complete
                else "#DC2626"
                if failure or cancelled
                else "#2563EB"
            )
            self._fit_window_to_content(state)
            self._draw_progress(percent)
            self._last_state = state
            if terminal:
                self._focus_first_visible_action()
            self.pump()
            self._draw_progress(percent)
        except Exception:
            pass
        return

    def _record_activity(self, snapshot: dict[str, Any]) -> None:
        activity = snapshot.get("activity", {})
        line = ""
        activity_id = ""
        if isinstance(activity, dict):
            line = _normalize_update_progress_text(activity.get("line"), limit=180)
            activity_id = str(activity.get("id") or line).strip()
        if line:
            if activity_id and activity_id not in self._activity_ids:
                self._activity_ids.append(activity_id)
                self._activity_lines.append(line)
            elif not activity_id and (not self._activity_lines or self._activity_lines[-1] != line):
                self._activity_lines.append(line)
            self._activity_ids = self._activity_ids[-3:]
            self._activity_lines = self._activity_lines[-3:]
        self._render_activity_lines()
        return

    def _render_activity_lines(self) -> None:
        labels = list(self._activity_labels or [])
        shell = self._activity_shell
        if not labels or shell is None:
            return
        visible_lines = list(self._activity_lines[-len(labels):])
        if not visible_lines:
            for label in labels:
                label.pack_forget()
                label.configure(text="")
            self._draw_activity_timeline(0)
            shell.pack_forget()
            return
        if not shell.winfo_manager():
            options: dict[str, Any] = {"fill": "x", "pady": (0, 14)}
            if self._buttons_frame is not None:
                options["before"] = self._buttons_frame
            shell.pack(**options)
        for index, label in enumerate(labels):
            try:
                if index < len(visible_lines):
                    label.configure(text=visible_lines[index])
                    if not label.winfo_manager():
                        label.pack(fill="x", pady=(0, 6))
                else:
                    label.configure(text="")
                    label.pack_forget()
            except Exception:
                continue
        self._draw_activity_timeline(len(visible_lines))
        return

    def _draw_activity_timeline(self, visible_count: int) -> None:
        canvas = self._activity_timeline
        if canvas is None:
            return
        try:
            count = max(0, min(3, int(visible_count)))
            canvas.delete("all")
            if count <= 0:
                return
            visible_labels = list(self._activity_labels[:count])
            row_heights = [max(1, int(label.winfo_reqheight())) for label in visible_labels]
            first_line_height = min(row_heights)
            row_gap = 6
            positions: list[float] = []
            cursor_y = 0
            for row_height in row_heights:
                positions.append(cursor_y + (first_line_height / 2))
                cursor_y += row_height + row_gap
            first_y = positions[0]
            last_y = positions[-1]
            canvas.configure(height=max(18, cursor_y - row_gap))
            if count > 1:
                canvas.create_line(
                    7,
                    first_y,
                    7,
                    last_y,
                    fill="#BFDBFE",
                    width=2,
                )
            for index in range(count):
                y = positions[index]
                latest = index == count - 1
                canvas.create_oval(
                    3,
                    y - 4,
                    11,
                    y + 4,
                    fill="#2563EB" if latest else "#FFFFFF",
                    outline="#2563EB" if latest else "#93C5FD",
                    width=2,
                )
        except Exception:
            return
        return

    def _fit_window_to_content(self, state: str) -> None:
        root = self._root
        if root is None:
            return
        try:
            scaling = float(root.tk.call("tk", "scaling") or 1.3333333333)
            scale = max(1.0, scaling / 1.3333333333)
            width = max(600, int(round(600 * scale)))
            wraplength = max(420, width - int(round(50 * scale)))
            if self._detail_label is not None:
                self._detail_label.configure(wraplength=wraplength)
            for label in self._activity_labels:
                label.configure(wraplength=max(380, wraplength - int(round(22 * scale))))
            root.update_idletasks()
            activity_visible = bool(self._activity_lines)
            layout_state = (
                "terminal"
                if state in {"failed", "cancelled"}
                else "complete"
                if state == "complete"
                else "running"
            )
            layout_key = (layout_state, activity_visible)
            requested_height = int(root.winfo_reqheight())
            height = max(220, requested_height)
            height = min(height, max(240, int(root.winfo_screenheight()) - 96))
            current_height = int(root.winfo_height() or 1)
            if self._layout_key == layout_key and requested_height <= current_height:
                return
            if self._positioned:
                x = int(root.winfo_x())
                y = int(root.winfo_y())
            else:
                x = max(0, int((root.winfo_screenwidth() - width) / 2))
                y = max(0, int((root.winfo_screenheight() - height) / 2))
                self._positioned = True
            root.geometry(f"{width}x{height}+{x}+{y}")
            root.update_idletasks()
            self._layout_key = layout_key
        except Exception:
            return

    def _focus_first_visible_action(self) -> None:
        root = self._root
        if root is None:
            return
        if self._last_state == "complete":
            candidates = (self._close_button, self._log_button)
        elif self._last_state == "cancelled":
            candidates = (self._manual_button, self._log_button, self._close_button)
        else:
            candidates = (
                self._retry_button,
                self._manual_button,
                self._log_button,
                self._close_button,
            )
        for button in candidates:
            if button is None or not button.winfo_manager():
                continue
            self._preferred_focus_button = button
            try:
                root.after_idle(button.focus_set)
            except Exception:
                pass
            return

    def _request_close(self) -> None:
        if self._last_state in {"failed", "cancelled", "complete"}:
            self.close()
            return
        root = self._root
        if root is not None:
            try:
                root.bell()
            except Exception:
                pass
        return

    def _handle_escape(self, _event: Any = None) -> str:
        self._request_close()
        return "break"

    def _handle_alt_f4(self, _event: Any = None) -> str:
        self._request_close()
        return "break"

    def _start_drag(self, event: Any) -> str:
        root = self._root
        if root is None:
            return "break"
        try:
            pointer = (int(event.x_root), int(event.y_root))
            origin = (int(root.winfo_x()), int(root.winfo_y()))
            self._drag_press = pointer
            self._drag_window_origin = origin
            self._drag_offset = (
                pointer[0] - origin[0],
                pointer[1] - origin[1],
            )
            self._dragging = False
        except Exception:
            self._drag_offset = None
            self._drag_press = None
            self._drag_window_origin = None
            self._dragging = False
        return "break"

    def _drag_window(self, event: Any) -> str:
        root = self._root
        if (
            root is None
            or self._drag_offset is None
            or self._drag_press is None
            or self._drag_window_origin is None
        ):
            return "break"
        try:
            delta_x = int(event.x_root) - int(self._drag_press[0])
            delta_y = int(event.y_root) - int(self._drag_press[1])
            if not self._dragging:
                if abs(delta_x) < self._drag_threshold and abs(delta_y) < self._drag_threshold:
                    return "break"
                self._dragging = True
            x = int(self._drag_window_origin[0]) + delta_x
            y = int(self._drag_window_origin[1]) + delta_y
            root.geometry(f"+{x}+{y}")
            self._positioned = True
        except Exception:
            pass
        return "break"

    def _end_drag(self, _event: Any = None) -> str:
        self._drag_offset = None
        self._drag_press = None
        self._drag_window_origin = None
        self._dragging = False
        return "break"

    def _set_button_visible(self, button: Any, visible: bool, *, side: str = "right") -> None:
        if button is None:
            return
        try:
            if visible:
                if not button.winfo_ismapped():
                    padding = (0, 8) if side == "left" else (8, 0)
                    button.pack(side=side, padx=padding)
                button.configure(state="normal")
            else:
                button.pack_forget()
        except Exception:
            pass
        return

    def _draw_progress(self, percent: int) -> None:
        canvas = self._progress_canvas
        if canvas is None:
            return
        self._last_percent = max(0, min(100, int(percent)))
        try:
            canvas.update_idletasks()
            width = int(canvas.winfo_width() or 476)
            height = int(canvas.winfo_height() or 14)
            inset = max(4, int(height / 2))
            fill_width = max(inset, int((width - inset * 2) * (self._last_percent / 100.0)) + inset)
            center = int(height / 2)
            canvas.delete("all")
            canvas.create_line(
                inset,
                center,
                width - inset,
                center,
                fill="#E2E8F0",
                width=height,
                capstyle="round",
            )
            if self._last_percent > 0:
                canvas.create_line(
                    inset,
                    center,
                    min(width - inset, fill_width),
                    center,
                    fill=self._progress_color,
                    width=height,
                    capstyle="round",
                )
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
                (
                    "업데이트를 자동으로 완료하지 못했습니다.\n"
                    "1. '로그 열기'에서 상세 내용을 확인합니다.\n"
                    "2. Windows Supporter를 다시 실행한 뒤 업데이트를 다시 시도합니다.\n"
                    "문제가 계속되면 로그 파일을 지원 담당자에게 전달해 주세요.\n\n"
                    f"로그 위치: {self._log_path or '업데이트 로그를 만들지 못했습니다.'}"
                ),
            )
        except Exception:
            pass
        return


def run_update_handoff(
    state_path: str | os.PathLike[str],
    *,
    subprocess_module=subprocess,
    launch=popen_no_window,
    runtime_deployer=deploy_runtime,
    runtime_restarter=restart_runtime,
    artifact_cleaner=cleanup_update_build_artifacts,
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

    root_executable = Path(repo_root) / "windows-supporter.exe"
    recovery_executable = Path(str(state.get("recovery_executable_path") or ""))
    relaunch_environment = build_relaunch_environment(os.environ)
    progress_ui = progress_ui_factory(log_path=log_path) if callable(progress_ui_factory) else None
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        root_identity_before_build = _executable_file_identity(root_executable)
        published_build_labels: set[str] = set()
        publish_build_lock = threading.Lock()
        step_log_tailer: _BuildStepLogTailer | None = None
        deployment_completed = False
        deployment_receipt: dict[str, Any] | None = None

        def publish_handoff_progress(snapshot: dict[str, Any], *, first: bool = False) -> None:
            if progress_ui is not None:
                if first:
                    progress_ui.show(snapshot)
                else:
                    progress_ui.set_snapshot(snapshot)
            update_handoff_state(state_path, status="running", progress=snapshot)
            return

        def publish_build_line(line: str) -> None:
            step_log_path = _extract_build_step_log_path(line)
            if step_log_path:
                if step_log_tailer is not None:
                    step_log_tailer.start(step_log_path)
                return
            normalized_line = str(line or "").strip()
            if normalized_line:
                append_update_log(log_path, normalized_line)
            with publish_build_lock:
                publish_build_output_progress(
                    line,
                    progress_ui=progress_ui,
                    state_path=state_path,
                    log_path=log_path,
                    seen=published_build_labels,
                )
            return

        step_log_tailer = _BuildStepLogTailer(publish_build_line)
        start_progress = build_update_progress_snapshot(
            "handoff_start",
            state="running",
            detail=f"업데이트 전용 프로세스가 시작되었습니다. (시도 {attempt}/{attempts})",
            log_path=log_path,
        )
        publish_handoff_progress(start_progress, first=True)
        update_handoff_state(
            state_path,
            status="running",
            acknowledged_at=time.time(),
            attempt=attempt,
            progress=start_progress,
        )
        append_update_log(log_path, f"handoff attempt {attempt} acknowledged")
        failed_step = "build.bat 실행"
        try:
            shutdown_progress = build_update_progress_snapshot(
                "shutdown",
                state="running",
                detail="기존 앱과 업데이트에 물린 하위 프로세스를 정리합니다.",
                log_path=log_path,
            )
            publish_handoff_progress(shutdown_progress)
            build_progress = build_update_progress_snapshot(
                "build_prepare",
                state="running",
                detail="build.bat를 실행할 준비를 마쳤습니다.",
                log_path=log_path,
            )
            publish_handoff_progress(build_progress)
            env = dict(os.environ)
            env["WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY"] = "1"
            env["WINDOWS_SUPPORTER_EMIT_STEP_LOG"] = "1"
            env["GIT_TERMINAL_PROMPT"] = "0"
            try:
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
            finally:
                if step_log_tailer is not None:
                    step_log_tailer.stop()
            output = str(getattr(result, "stdout", "") or "")
            error_output = str(getattr(result, "stderr", "") or "")
            visible_output = _filter_build_output_control_lines(output)
            if error_output:
                append_update_log(log_path, error_output.strip())
            publish_build_output_progress(
                visible_output,
                progress_ui=progress_ui,
                state_path=state_path,
                log_path=log_path,
                seen=published_build_labels,
            )
            if int(getattr(result, "returncode", 1) or 0) != 0:
                raise UpdateHandoffError(
                    f"build.bat failed with exit code {getattr(result, 'returncode', 1)}"
                )

            failed_step = "새 버전 배포 및 준비 확인"
            relaunch_progress = build_update_progress_snapshot(
                "relaunch",
                state="running",
                log_path=log_path,
            )
            if progress_ui is not None:
                progress_ui.set_snapshot(relaunch_progress)
            update_handoff_state(
                state_path,
                status="running",
                progress=relaunch_progress,
            )
            deployment_receipt = runtime_deployer(
                Path(repo_root) / "dist" / "windows-supporter.exe",
                root_executable,
                base_environment=relaunch_environment,
            )
            if not isinstance(deployment_receipt, dict) or deployment_receipt.get(
                "status"
            ) != "success":
                raise UpdateHandoffError("deployment helper returned no success receipt")
            deployment_completed = True
            failed_step = "빌드 산출물 정리"
            removed_artifacts = artifact_cleaner(repo_root)
            append_update_log(
                log_path,
                f"removed build artifacts: {', '.join(removed_artifacts) or 'none'}",
            )
            for gui_name, gui_command in _extract_git_gui_relaunch_commands(state):
                try:
                    launch(gui_command, cwd=repo_root)
                    append_update_log(log_path, f"relaunch requested for {gui_name}")
                except Exception as exc:
                    append_update_log(log_path, f"Git GUI relaunch skipped for {gui_name}: {exc!r}")

            complete_progress = build_update_progress_snapshot(
                "complete",
                state="complete",
                log_path=log_path,
            )
            if progress_ui is not None:
                progress_ui.set_snapshot(complete_progress)
            update_handoff_state(
                state_path,
                status="complete",
                completed_at=time.time(),
                deployment_receipt=deployment_receipt,
                progress=complete_progress,
            )
            append_update_log(log_path, "handoff completed")
            if progress_ui is not None:
                progress_ui.close()
            return 0
        except Exception as exc:
            can_retry = attempt < attempts
            recovery_status = "failed"
            recovery_error = ""
            recovered_at = None
            recovery_receipt: dict[str, Any] | None = None
            if deployment_completed:
                recovery_status = "complete"
                recovered_at = time.time()
                recovery_receipt = {
                    "status": "new-runtime-ready",
                    "deployment": deployment_receipt,
                }
                recovery_error = "new runtime remains ready; build artifact cleanup failed"
                append_update_log(log_path, recovery_error)
            elif isinstance(exc, RuntimeDeployError):
                rollback = exc.receipt.get("rollback", {})
                if isinstance(rollback, dict) and rollback.get("status") == "ready":
                    recovery_status = "complete"
                    recovered_at = time.time()
                    recovery_receipt = dict(rollback)
                    append_update_log(log_path, "deployment helper restored and verified the previous runtime")
                elif (
                    exc.receipt.get("target_unchanged") is True
                    and exc.receipt.get("recovery_action")
                    == "restart-unchanged-runtime"
                    and isinstance(rollback, dict)
                    and rollback.get("status") == "target-unchanged"
                    and exc.receipt.get("transaction_conflict") is not True
                    and "preserved_transaction" not in exc.receipt
                ):
                    try:
                        recovery_receipt = runtime_restarter(
                            root_executable,
                            base_environment=relaunch_environment,
                        )
                        recovery_status = "complete"
                        recovered_at = time.time()
                        append_update_log(
                            log_path,
                            "unchanged previous runtime restarted with readiness verification",
                        )
                    except (OSError, RuntimeDeployError) as recovery_exc:
                        recovery_error = str(recovery_exc)
                else:
                    rollback_error = (
                        rollback.get("error") if isinstance(rollback, dict) else None
                    )
                    recovery_error = str(rollback_error or exc)
            else:
                root_identity_after_build = _executable_file_identity(root_executable)
                try:
                    if (
                        root_identity_before_build is None
                        or root_identity_after_build != root_identity_before_build
                    ):
                        restore_previous_executable(recovery_executable, root_executable)
                    recovery_receipt = runtime_restarter(
                        root_executable,
                        base_environment=relaunch_environment,
                    )
                    recovery_status = "complete"
                    recovered_at = time.time()
                    append_update_log(log_path, "previous runtime restarted with readiness verification")
                except (OSError, RuntimeDeployError) as recovery_exc:
                    recovery_error = str(recovery_exc)
                    append_update_log(
                        log_path,
                        f"previous executable recovery failed: {recovery_error}",
                    )

            if deployment_completed:
                recovery_detail = "새 버전은 정상 실행 중이지만 빌드 산출물 정리가 필요합니다."
            else:
                recovery_detail = (
                    "이전 버전으로 안전하게 복구했습니다."
                    if recovery_status == "complete"
                    else "이전 버전 자동 복구에도 실패했습니다."
                )
            diagnostic_error = f"{exc} (recovery={recovery_status}: {recovery_error})"
            error_detail = (
                f"{failed_step} 단계에서 업데이트를 완료하지 못했습니다. "
                f"{recovery_detail} 로그에서 오류 코드 WSU-UPD-001을 확인해 주세요."
            )
            current_progress = read_update_handoff_state(state_path).get("progress", {})
            last_percent = (
                int(current_progress.get("percent") or 0)
                if isinstance(current_progress, dict)
                else 0
            )
            failed_progress = build_update_progress_snapshot(
                "failed",
                state="failed",
                detail=error_detail,
                percent=last_percent,
                log_path=log_path,
                failed_step=failed_step,
                can_retry=can_retry,
                can_manual_action=True,
            )
            if progress_ui is not None:
                progress_ui.set_snapshot(failed_progress)
            update_handoff_state(
                state_path,
                status="failed",
                failed_at=time.time(),
                failed_step=failed_step,
                error=diagnostic_error,
                attempt=attempt,
                recovery_status=recovery_status,
                recovery_error=recovery_error,
                recovery_receipt=recovery_receipt,
                recovered_at=recovered_at,
                progress=failed_progress,
            )
            append_update_log(log_path, f"handoff attempt {attempt} failed: {diagnostic_error}")
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
        exit_callback=None,
        status_changed_callback=None,
        handoff_path_provider=get_update_handoff_path,
        handoff_writer=write_update_handoff_state,
        handoff_command_builder=build_update_handoff_command,
        handoff_ack_waiter=wait_for_update_handoff_ack,
        timestamp_provider=lambda: time.strftime("%Y%m%d-%H%M%S"),
        worktree_runner=subprocess.run,
        settings_path_provider=get_update_settings_path,
        git_gui_process_detector=None,
        progress_ui_factory=UpdateHandoffProgressUi,
    ) -> None:
        self._root = root
        self._event_queue = event_queue
        self._repo_root = str(Path(repo_root or os.getcwd()).resolve())
        self._app_version_provider = app_version_provider
        self._subprocess = subprocess_module
        self._thread_factory = thread_factory
        self._popen = popen
        self._quit_callback = quit_callback
        self._exit_callback = exit_callback
        self._status_changed_callback = status_changed_callback
        self._handoff_path_provider = handoff_path_provider
        self._handoff_writer = handoff_writer
        self._handoff_command_builder = handoff_command_builder
        self._handoff_ack_waiter = handoff_ack_waiter
        self._timestamp_provider = timestamp_provider
        self._worktree_runner = worktree_runner
        self._settings_path_provider = settings_path_provider
        self._progress_ui_factory = progress_ui_factory
        self._preflight_progress_ui = None
        self._show_preflight_progress_ui = False
        if callable(git_gui_process_detector):
            self._git_gui_process_detector = git_gui_process_detector
        elif subprocess_module is subprocess:
            self._git_gui_process_detector = lambda repo_root: find_running_git_gui_processes(
                repo_root,
                subprocess_module=self._subprocess,
            )
        else:
            self._git_gui_process_detector = lambda _repo_root: ()
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
            next_settings, error = validate_update_settings_update(
                data,
                settings_path=self._settings_path,
                current=self._settings,
            )
            if next_settings is None:
                return False, str(error or "invalid settings")
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
        candidate = select_update_candidate(
            current_tag=current_tag,
            remote_tags=remote_tags,
            is_contained=self._is_tag_contained_in_history,
        )
        working_tree = self._inspect_working_tree_state() if candidate is not None else UpdateWorkingTreeState()
        return candidate, working_tree, ""

    def _is_tag_contained_in_history(self, tag: str) -> bool:
        """True when the tag's commit is already an ancestor of HEAD.

        Version numbers alone mislead whenever a patch line advances past a
        released minor (hotfix 0.18.x after minor 0.19.0): the higher number
        is content-older, and offering it would downgrade the install and nag
        forever. Evidence failures stay fail-open (offer) to preserve current
        behavior exactly where ancestry cannot be proven.
        """
        name = str(tag or "").strip()
        if not name:
            return False
        try:
            self._git_output(["git", "fetch", "origin", "tag", name])
        except Exception:
            pass
        try:
            sha = self._git_output(["git", "rev-parse", f"{name}^{{commit}}"]).strip()
        except Exception:
            return False
        if not sha:
            return False
        try:
            self._git_output(["git", "merge-base", "--is-ancestor", sha, "HEAD"])
        except Exception:
            return False
        return True

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
            self._state = "updating"
            self._publish_update_progress(
                "accepted",
                state="running",
                detail=f"{candidate.tag} 업데이트 요청을 접수했습니다.",
                show_ui=True,
            )
            try:
                working_tree = self._inspect_working_tree_state()
            except Exception as exc:
                self._state = "error"
                self._last_error = f"Git 상태를 확인할 수 없습니다: {exc}"
                self._publish_update_progress(
                    "failed",
                    state="failed",
                    detail=self._last_error,
                    failed_step="Git 상태 확인",
                    can_retry=True,
                    can_manual_action=True,
                )
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

    def _publish_update_progress(
        self,
        step_key: str,
        *,
        state: str,
        detail: str | None = None,
        failed_step: str = "",
        can_retry: bool = False,
        can_manual_action: bool = False,
        show_ui: bool = False,
    ) -> None:
        previous_percent = int(self._progress_snapshot.get("percent") or 0)
        visible_percent = previous_percent if step_key == "failed" else None
        self._progress_snapshot = build_update_progress_snapshot(
            step_key,
            state=state,
            detail=detail,
            percent=visible_percent,
            log_path=str(get_update_log_path()),
            failed_step=failed_step,
            can_retry=False,
            can_manual_action=can_manual_action,
        )
        self._notify_status_changed()
        if show_ui:
            self._show_or_update_preflight_progress_ui(self._progress_snapshot)
        elif self._preflight_progress_ui is not None:
            self._update_preflight_progress_ui(self._progress_snapshot)
        return

    def _show_or_update_preflight_progress_ui(self, snapshot: dict[str, Any]) -> None:
        self._show_preflight_progress_ui = True
        progress_ui = self._preflight_progress_ui
        if progress_ui is None:
            factory = self._progress_ui_factory
            if not callable(factory):
                return
            try:
                progress_ui = factory(log_path=str(get_update_log_path()))
            except Exception:
                return
            self._preflight_progress_ui = progress_ui
            try:
                progress_ui.show(snapshot)
                return
            except Exception:
                self._preflight_progress_ui = None
                return
        self._update_preflight_progress_ui(snapshot)
        return

    def _update_preflight_progress_ui(self, snapshot: dict[str, Any]) -> None:
        progress_ui = self._preflight_progress_ui
        if progress_ui is None:
            return
        try:
            progress_ui.set_snapshot(snapshot)
        except Exception:
            pass
        return

    def _close_preflight_progress_ui(self) -> None:
        progress_ui = self._preflight_progress_ui
        self._preflight_progress_ui = None
        self._show_preflight_progress_ui = False
        if progress_ui is None:
            return
        try:
            progress_ui.close()
        except Exception:
            pass
        return

    def _is_worktree_dirty(self) -> bool:
        return self._inspect_working_tree_state().has_source_changes

    def _has_update_cleanup_targets(self) -> bool:
        return self._inspect_working_tree_state().has_cleanup_targets

    def _find_running_git_gui_processes(self) -> tuple[str, ...]:
        detector = self._git_gui_process_detector
        if not callable(detector):
            return ()
        try:
            return tuple(str(item) for item in detector(self._repo_root) if str(item).strip())
        except subprocess.TimeoutExpired:
            return ()

    def _ask_close_git_gui_processes(self, process_names: Sequence[str]) -> bool:
        names = ", ".join(str(name) for name in process_names if str(name).strip())
        try:
            from tkinter import messagebox

            return bool(
                messagebox.askyesno(
                    "업데이트를 계속하려면 Git 앱을 닫아야 합니다",
                    (
                        f"{names}가 windows-supporter checkout을 사용 중일 수 있어 "
                        "업데이트를 바로 진행할 수 없습니다.\n\n"
                        "Windows Supporter가 해당 Git 앱을 닫고, 업데이트를 계속한 뒤 "
                        "다시 실행해도 될까요?\n\n"
                        "아니요를 선택하면 이번 업데이트 시도를 취소하고 같은 버전 팝업을 반복하지 않습니다."
                    ),
                )
            )
        except Exception:
            return False

    def _close_git_gui_processes_for_update(
        self,
        process_names: Sequence[str],
    ) -> dict[str, Any]:
        return close_running_git_gui_processes(
            process_names,
            subprocess_module=self._subprocess,
        )

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

    def _request_current_process_exit_for_update(self) -> bool:
        try:
            if callable(self._quit_callback):
                self._quit_callback()
        except Exception:
            pass
        exit_callback = self._exit_callback
        if not callable(exit_callback):
            return False
        try:
            exit_callback()
            return True
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
            "git_gui_processes": [],
            "git_gui_close_approved": False,
            "git_gui_close_result": {},
            "git_gui_relaunch": [],
        }
        self._publish_update_progress(
            "preflight",
            state="running",
            detail="Git GUI와 checkout 상태를 확인합니다.",
            show_ui=self._show_preflight_progress_ui,
        )
        git_gui_processes = self._find_running_git_gui_processes()
        if git_gui_processes:
            process_names = ", ".join(git_gui_processes)
            preflight["git_gui_processes"] = list(git_gui_processes)
            self._publish_update_progress(
                "preflight",
                state="await_git_gui_close",
                detail=f"{process_names}가 실행 중입니다. 종료 승인 대기 중입니다.",
                show_ui=self._show_preflight_progress_ui,
            )
            if not self._ask_close_git_gui_processes(git_gui_processes):
                self._state = "cancelled"
                self._last_error = f"{GIT_GUI_UPDATE_CANCELLED_MESSAGE}\n실행 중: {process_names}"
                self._publish_update_progress(
                    "failed",
                    state="cancelled",
                    detail=self._last_error,
                    failed_step="Git GUI 확인",
                    can_manual_action=True,
                    show_ui=self._show_preflight_progress_ui,
                )
                if self._latest_tag:
                    self._session.dismiss(self._latest_tag)
                self._preflight_result = preflight
                return False
            preflight["git_gui_close_approved"] = True
            try:
                close_result = self._close_git_gui_processes_for_update(git_gui_processes)
            except Exception as exc:
                close_result = {"error": repr(exc)}
            if not isinstance(close_result, dict):
                close_result = {"error": "Git GUI 종료 결과를 해석할 수 없습니다."}
            relaunch_entries = close_result.get("relaunch", [])
            if not relaunch_entries:
                relaunch_entries = _default_git_gui_relaunch_entries(git_gui_processes)
            preflight["git_gui_close_result"] = close_result
            preflight["git_gui_relaunch"] = relaunch_entries
            still_running = close_result.get("still_running", [])
            if still_running:
                still_names = ", ".join(str(item) for item in still_running)
                self._state = "error"
                self._last_error = f"Git GUI를 닫을 수 없어 업데이트를 중단했습니다.\n실행 중: {still_names}"
                self._publish_update_progress(
                    "failed",
                    state="failed",
                    detail=self._last_error,
                    failed_step="Git GUI 종료",
                    can_retry=True,
                    can_manual_action=True,
                    show_ui=self._show_preflight_progress_ui,
                )
                self._preflight_result = preflight
                return False
            self._publish_update_progress(
                "preflight",
                state="running",
                detail=f"{process_names} 종료를 확인했습니다. 업데이트를 계속합니다.",
                show_ui=self._show_preflight_progress_ui,
            )
        try:
            self._publish_update_progress(
                "fetch",
                state="running",
                show_ui=self._show_preflight_progress_ui,
            )
            self._git_output(build_fetch_origin_command())

            fresh_tree = self._inspect_working_tree_state()
            self._working_tree_state = fresh_tree
            preflight["cleaned_targets"] = list(fresh_tree.cleanup_targets)

            requires_force_clean = fresh_tree.is_diverged or fresh_tree.has_local_only_commits
            if requires_force_clean:
                self._publish_update_progress(
                    "preflight",
                    state="await_force_clean_approval",
                    detail="로컬 전용 커밋을 보존한 뒤 main을 origin/main 기준으로 동기화해야 합니다.",
                    show_ui=self._show_preflight_progress_ui,
                )
                if not self._ask_force_clean(fresh_tree):
                    self._state = "cancelled"
                    self._last_error = UPDATE_FORCE_CLEAN_REJECTED_NOTICE
                    self._publish_update_progress(
                        "failed",
                        state="cancelled",
                        detail=UPDATE_FORCE_CLEAN_REJECTED_NOTICE,
                        failed_step="강제정리 승인",
                        can_manual_action=True,
                        show_ui=self._show_preflight_progress_ui,
                    )
                    return False
                preflight["force_clean_approved"] = True

            if fresh_tree.has_source_changes:
                self._publish_update_progress(
                    "stash",
                    state="running",
                    show_ui=self._show_preflight_progress_ui,
                )
                stash_message = f"windows-supporter auto update {timestamp}"
                preflight["stash_output"] = self._git_output(build_stash_command(stash_message))

            if fresh_tree.has_local_only_commits:
                preflight["backup_branch"] = self._create_backup_branch(timestamp)

            self._publish_update_progress(
                "sync",
                state="running",
                show_ui=self._show_preflight_progress_ui,
            )
            self._git_output(build_switch_main_command())
            if fresh_tree.has_local_only_commits or fresh_tree.is_diverged:
                self._git_output(build_reset_main_command())
            elif fresh_tree.has_remote_only_commits:
                self._git_output(build_fast_forward_main_command())

            if fresh_tree.has_cleanup_targets:
                self._publish_update_progress(
                    "cleanup",
                    state="running",
                    show_ui=self._show_preflight_progress_ui,
                )
                self._git_output(build_allowed_clean_command())
        except Exception as exc:
            self._state = "error"
            self._last_error = f"update preflight failed: {exc}"
            self._publish_update_progress(
                "failed",
                state="failed",
                detail=self._last_error,
                failed_step="업데이트 사전 정리",
                can_retry=True,
                can_manual_action=True,
                show_ui=self._show_preflight_progress_ui,
            )
            self._preflight_result = preflight
            return False

        self._preflight_result = preflight
        return True

    def launch_update(self) -> bool:
        if self._mark_unavailable_if_needed():
            return False
        try:
            handoff_path = Path(self._handoff_path_provider())
            log_path = get_update_log_path(handoff_path.parent)
            payload = build_update_handoff_payload(
                repo_root=self._repo_root,
                target_tag=self._latest_tag,
                working_tree=self._working_tree_state,
                log_path=log_path,
                preflight=self._preflight_result,
            )
            payload["recovery_executable_path"] = str(
                get_update_handoff_executable_path(handoff_path.parent)
            )
            self._handoff_writer(handoff_path, payload)
            command = self._handoff_command_builder(handoff_path)
        except Exception as exc:
            self._state = "error"
            self._last_error = f"failed to prepare update handoff: {exc}"
            self._publish_update_progress(
                "failed",
                state="failed",
                detail=self._last_error,
                failed_step="업데이트 실행 준비",
                can_retry=True,
                can_manual_action=True,
                show_ui=self._show_preflight_progress_ui,
            )
            return False

        proc = self._popen(command, cwd=self._repo_root)
        self._state = "updating"
        self._publish_update_progress(
            "handoff",
            state="running",
            show_ui=self._show_preflight_progress_ui,
        )
        if proc is None:
            self._state = "error"
            self._last_error = "failed to launch update handoff"
            self._publish_update_progress(
                "failed",
                state="failed",
                detail=self._last_error,
                failed_step="업데이트 프로세스 시작",
                can_retry=True,
                can_manual_action=True,
                show_ui=self._show_preflight_progress_ui,
            )
            return False

        helper_pid = _coerce_positive_pid(getattr(proc, "pid", 0))
        if helper_pid > 0:
            cleanup_result = terminate_process_descendants(
                os.getpid(),
                exclude_pids=(helper_pid,),
                subprocess_module=self._subprocess,
                timeout_seconds=3.0,
                log=lambda message: append_update_log(log_path, message),
            )
            terminated_pids = cleanup_result.get("terminated_pids", [])
            failed_pids = cleanup_result.get("failed_pids", [])
            if terminated_pids or failed_pids:
                append_update_log(
                    log_path,
                    (
                        "pre-exit child cleanup "
                        f"terminated={terminated_pids} failed={failed_pids} "
                        f"excluded_helper={helper_pid}"
                    ),
                )

        if self._request_current_process_exit_for_update():
            return True

        if not self._handoff_ack_waiter(handoff_path):
            self._state = "error"
            self._last_error = "update handoff did not acknowledge startup"
            self._publish_update_progress(
                "failed",
                state="failed",
                detail=self._last_error,
                failed_step="업데이트 프로세스 확인",
                can_retry=True,
                can_manual_action=True,
                show_ui=self._show_preflight_progress_ui,
            )
            return False

        self._close_preflight_progress_ui()
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
    *,
    current_tag: str,
    remote_tags: list[str] | tuple[str, ...],
    is_contained: Callable[[str], bool] | None = None,
) -> UpdateCandidate | None:
    current_version = parse_semver_tag(current_tag)
    if current_version is None:
        return None

    candidates: list[UpdateCandidate] = []
    for tag in remote_tags:
        version = parse_semver_tag(tag)
        if version is None or version <= current_version:
            continue
        if is_contained is not None:
            try:
                if bool(is_contained(str(tag).strip())):
                    continue
            except Exception:
                pass
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
    return ["git", "fetch", "--force", "--tags", resolved_remote]


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
