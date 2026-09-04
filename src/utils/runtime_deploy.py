from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from src.utils.runtime_lifecycle import PROBE_PATH_ENV, PROBE_TOKEN_ENV, SCHEMA_VERSION


DEFAULT_READY_TIMEOUT_SECONDS = 60.0
DEFAULT_HEARTBEAT_SAMPLES = 3
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
DEFAULT_STOP_TIMEOUT_SECONDS = 15.0
_COMMENTS_COMMIT_RE = re.compile(r"\(([0-9a-f]+)(?:-dirty)?\)\s*$", re.IGNORECASE)


class DeployExitCode(IntEnum):
    INVALID_INPUT = 10
    STOP_FAILED = 20
    REPLACE_FAILED = 30
    LAUNCH_FAILED = 40
    READINESS_FAILED = 50
    ROLLBACK_FAILED = 60


class RuntimeDeployError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        exit_code: DeployExitCode,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.receipt = dict(
            receipt
            or {
                "schema_version": 1,
                "operation": "deploy",
                "status": "failed",
                "timestamp": _utc_now(),
                "error": message,
            }
        )


class RuntimeLaunchError(RuntimeError):
    """The runtime process could not be launched at all."""


class _CliArgumentError(ValueError):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliArgumentError(message)


class RuntimeProcessController(Protocol):
    def find_exact(self, executable_path: Path) -> Sequence[int]: ...

    def terminate_tree(
        self,
        pids: Sequence[int],
        timeout_seconds: float,
    ) -> Sequence[int]: ...

    def launch(
        self,
        executable_path: Path,
        *,
        env: Mapping[str, str],
        cwd: Path,
    ) -> int: ...

    def validate_probe_process(
        self,
        payload: Mapping[str, Any],
        *,
        launcher_pid: int,
        executable_path: Path,
    ) -> None: ...

    def window_owner_pid(self, hwnd: int) -> int: ...

    def startup_executable(self) -> Path | None: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    return os.path.normcase(os.path.realpath(os.fspath(left))) == os.path.normcase(
        os.path.realpath(os.fspath(right))
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_windows_artifact_identity(path: Path) -> tuple[str, str]:
    if os.name != "nt":
        raise RuntimeError("Windows version metadata is unavailable on this platform")
    import win32api

    language = "040904B0"
    version = str(
        win32api.GetFileVersionInfo(str(path), f"\\StringFileInfo\\{language}\\FileVersion")
        or ""
    ).strip()
    comments = str(
        win32api.GetFileVersionInfo(str(path), f"\\StringFileInfo\\{language}\\Comments")
        or ""
    ).strip()
    commit_match = _COMMENTS_COMMIT_RE.search(comments)
    commit = commit_match.group(1) if commit_match else ""
    if not version or not commit:
        raise RuntimeError("candidate FileVersion or commit metadata is missing")
    return version, commit


def _claim_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


class WindowsRuntimeProcessController:
    def __init__(self) -> None:
        import psutil

        self._psutil = psutil

    def find_exact(self, executable_path: Path) -> list[int]:
        found: list[int] = []
        for process in self._psutil.process_iter(attrs=["pid", "exe"]):
            try:
                executable = process.info.get("exe") or process.exe()
                if executable and _same_path(executable, executable_path):
                    found.append(int(process.info["pid"]))
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied, OSError):
                continue
        return sorted(set(found))

    def terminate_tree(self, pids: Sequence[int], timeout_seconds: float) -> list[int]:
        selected: dict[int, Any] = {}
        for pid in pids:
            try:
                process = self._psutil.Process(int(pid))
                selected[process.pid] = process
                for child in process.children(recursive=True):
                    selected[child.pid] = child
            except self._psutil.NoSuchProcess:
                continue
            except self._psutil.AccessDenied as exc:
                raise RuntimeError(f"access denied while inspecting process {pid}") from exc
        current_pid = os.getpid()
        processes = [proc for pid, proc in selected.items() if pid != current_pid]
        for process in sorted(processes, key=lambda item: item.pid, reverse=True):
            try:
                process.terminate()
            except self._psutil.NoSuchProcess:
                continue
            except self._psutil.AccessDenied as exc:
                raise RuntimeError(
                    f"access denied while terminating process {process.pid}"
                ) from exc
        _, alive = self._psutil.wait_procs(processes, timeout=max(0.1, timeout_seconds / 2))
        for process in alive:
            try:
                process.kill()
            except self._psutil.NoSuchProcess:
                continue
            except self._psutil.AccessDenied as exc:
                raise RuntimeError(f"access denied while killing process {process.pid}") from exc
        _, alive = self._psutil.wait_procs(alive, timeout=max(0.1, timeout_seconds / 2))
        if alive:
            raise RuntimeError(f"process tree did not stop: {[proc.pid for proc in alive]}")
        return sorted(selected)

    def launch(
        self,
        executable_path: Path,
        *,
        env: Mapping[str, str],
        cwd: Path,
    ) -> int:
        creationflags = 0
        for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= int(getattr(subprocess, name, 0))
        process = subprocess.Popen(
            [str(executable_path)],
            cwd=str(cwd),
            env=dict(env),
            creationflags=creationflags,
            close_fds=True,
        )
        return int(process.pid)

    def validate_probe_process(
        self,
        payload: Mapping[str, Any],
        *,
        launcher_pid: int,
        executable_path: Path,
    ) -> None:
        pid = int(payload["pid"])
        process = self._psutil.Process(pid)
        if not process.is_running():
            raise RuntimeError("probe PID is not running")
        if not _same_path(process.exe(), executable_path):
            raise RuntimeError("probe PID executable path does not match the promoted runtime")
        if not _same_path(str(payload["executable_path"]), executable_path):
            raise RuntimeError("probe executable path does not match the promoted runtime")
        try:
            reported_start = datetime.fromisoformat(str(payload["process_start_time"]))
            if reported_start.tzinfo is None:
                reported_start = reported_start.replace(tzinfo=timezone.utc)
            actual_start = datetime.fromtimestamp(process.create_time(), tz=timezone.utc)
        except (TypeError, ValueError, OSError) as exc:
            raise RuntimeError(f"invalid process start time: {exc}") from exc
        if abs((reported_start - actual_start).total_seconds()) > 15.0:
            raise RuntimeError("probe PID start time does not match the live process")
        if pid == int(launcher_pid):
            return
        ancestor = process
        for _ in range(16):
            try:
                ancestor = ancestor.parent()
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                ancestor = None
            if ancestor is None:
                break
            if int(ancestor.pid) == int(launcher_pid):
                return
        raise RuntimeError("probe PID is not the launched runtime or its descendant")

    def window_owner_pid(self, hwnd: int) -> int:
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(int(hwnd))
        return int(pid)

    def startup_executable(self) -> Path | None:
        if os.name != "nt":
            return None
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Windows Supporter")
        except OSError:
            return None
        text = str(value or "").strip().strip('"')
        return Path(text) if text else None


def _read_probe(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validate_readiness_payload(
    payload: Mapping[str, Any],
    *,
    token: str,
    target: Path,
    launcher_pid: int,
    controller: RuntimeProcessController,
    expected_version: str | None,
    expected_commit: str | None,
) -> tuple[int, Path]:
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        raise RuntimeError("unsupported readiness schema")
    if str(payload.get("token") or "") != token:
        raise RuntimeError("readiness token mismatch")
    state = str(payload.get("state") or "")
    if state == "failed":
        raise RuntimeError(f"runtime reported failed readiness: {payload.get('error')}")
    if state != "ready":
        raise RuntimeError(f"runtime is not ready: {state or 'missing state'}")
    tick = int(payload.get("mainloop_tick", 0) or 0)
    hwnd = int(payload.get("tray_hwnd", 0) or 0)
    pid = int(payload.get("pid", 0) or 0)
    if tick <= 0 or hwnd <= 0 or pid <= 0:
        raise RuntimeError("readiness probe is missing tick, tray HWND, or PID")
    if expected_version and str(payload.get("version") or "") != expected_version:
        raise RuntimeError("runtime version does not match the expected candidate")
    if expected_commit and str(payload.get("commit") or "") != expected_commit:
        raise RuntimeError("runtime commit does not match the expected candidate")
    controller.validate_probe_process(
        payload,
        launcher_pid=launcher_pid,
        executable_path=target,
    )
    if controller.window_owner_pid(hwnd) != pid:
        raise RuntimeError("tray HWND is not owned by the readiness PID")
    startup = controller.startup_executable()
    if startup is None or not _same_path(startup, target):
        raise RuntimeError("startup registration does not point to the promoted runtime")
    return tick, startup


def _wait_for_readiness(
    target: Path,
    *,
    controller: RuntimeProcessController,
    probe_path: Path,
    token: str,
    launcher_pid: int,
    timeout_seconds: float,
    heartbeat_samples: int,
    poll_interval: float,
    expected_version: str | None,
    expected_commit: str | None,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    deadline = monotonic() + max(0.01, float(timeout_seconds))
    ticks: list[int] = []
    last_error = "probe was not created"
    while monotonic() < deadline:
        payload = _read_probe(probe_path)
        if payload is not None:
            try:
                tick, startup = _validate_readiness_payload(
                    payload,
                    token=token,
                    target=target,
                    launcher_pid=launcher_pid,
                    controller=controller,
                    expected_version=expected_version,
                    expected_commit=expected_commit,
                )
                if not ticks or tick > ticks[-1]:
                    ticks.append(tick)
                if len(ticks) >= max(1, int(heartbeat_samples)):
                    return {
                        "pid": int(payload["pid"]),
                        "launcher_pid": int(launcher_pid),
                        "tray_hwnd": int(payload["tray_hwnd"]),
                        "heartbeat_samples": len(ticks),
                        "mainloop_ticks": ticks,
                        "process_start_time": str(
                            payload.get("process_start_time") or ""
                        ),
                        "version": str(payload.get("version") or ""),
                        "commit": str(payload.get("commit") or ""),
                        "probe_path": str(probe_path),
                        "probe_token_sha256": hashlib.sha256(
                            token.encode("utf-8")
                        ).hexdigest().upper(),
                        "startup_path": str(startup),
                    }
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                last_error = str(exc)
        sleep(max(0.001, float(poll_interval)))
    raise RuntimeError(f"runtime readiness timed out: {last_error}")


def _launch_and_verify(
    target: Path,
    *,
    controller: RuntimeProcessController,
    probe_path: Path,
    token: str,
    timeout_seconds: float,
    heartbeat_samples: int,
    poll_interval: float,
    expected_version: str | None,
    expected_commit: str | None,
    base_environment: Mapping[str, str],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    probe_path.unlink(missing_ok=True)
    environment = dict(base_environment)
    environment[PROBE_PATH_ENV] = str(probe_path)
    environment[PROBE_TOKEN_ENV] = token
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        launcher_pid = controller.launch(target, env=environment, cwd=target.parent)
    except Exception as exc:
        raise RuntimeLaunchError(f"runtime launch failed: {exc}") from exc
    if int(launcher_pid) <= 0:
        raise RuntimeLaunchError("runtime launch returned no PID")
    return _wait_for_readiness(
        target,
        controller=controller,
        probe_path=probe_path,
        token=token,
        launcher_pid=int(launcher_pid),
        timeout_seconds=timeout_seconds,
        heartbeat_samples=heartbeat_samples,
        poll_interval=poll_interval,
        expected_version=expected_version,
        expected_commit=expected_commit,
        sleep=sleep,
        monotonic=monotonic,
    )


def restart_runtime(
    target_path: str | os.PathLike[str],
    *,
    controller: RuntimeProcessController | None = None,
    probe_path: str | os.PathLike[str] | None = None,
    token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
    heartbeat_samples: int = DEFAULT_HEARTBEAT_SAMPLES,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    expected_version: str | None = None,
    expected_commit: str | None = None,
    base_environment: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    target = Path(target_path).resolve()
    marker = target.with_name("windows-supporter.promotion-pending.json")
    backup = target.with_name("windows-supporter.previous.exe")
    if not target.is_file():
        raise RuntimeDeployError(
            f"runtime executable is missing: {target}",
            exit_code=DeployExitCode.INVALID_INPUT,
            receipt={
                "schema_version": 1,
                "operation": "restart",
                "status": "failed",
                "timestamp": _utc_now(),
                "target": str(target),
                "error": f"runtime executable is missing: {target}",
            },
        )
    if marker.exists() or backup.exists():
        raise RuntimeDeployError(
            "an unfinished deployment transaction prevents runtime restart",
            exit_code=DeployExitCode.INVALID_INPUT,
            receipt={
                "schema_version": 1,
                "operation": "restart",
                "status": "failed",
                "timestamp": _utc_now(),
                "target": str(target),
                "transaction_conflict": True,
                "preserved_transaction": {
                    "marker": str(marker) if marker.exists() else None,
                    "backup": str(backup) if backup.exists() else None,
                },
                "error": "an unfinished deployment transaction prevents runtime restart",
            },
        )
    try:
        _claim_json_exclusive(
            marker,
            {
                "schema_version": 1,
                "operation": "restart",
                "target": str(target),
                "started_at": _utc_now(),
            },
        )
    except FileExistsError as exc:
        raise RuntimeDeployError(
            "another deployment transaction acquired the target concurrently",
            exit_code=DeployExitCode.INVALID_INPUT,
            receipt={
                "schema_version": 1,
                "operation": "restart",
                "status": "failed",
                "timestamp": _utc_now(),
                "target": str(target),
                "transaction_conflict": True,
                "preserved_transaction": {
                    "marker": str(marker),
                    "backup": str(backup) if backup.exists() else None,
                },
                "error": "another deployment transaction acquired the target concurrently",
            },
        ) from exc
    exact_pids: list[int] = []
    try:
        process_controller = controller or WindowsRuntimeProcessController()
        selected_probe = Path(probe_path) if probe_path else target.parent / ".windows-supporter.runtime-probe.json"
        if backup.exists():
            raise RuntimeDeployError(
                "an unowned deployment backup appeared after restart claim",
                exit_code=DeployExitCode.INVALID_INPUT,
            )
        exact_pids = list(process_controller.find_exact(target))
        if exact_pids:
            try:
                process_controller.terminate_tree(exact_pids, DEFAULT_STOP_TIMEOUT_SECONDS)
            except Exception as exc:
                raise RuntimeDeployError(
                    f"runtime restart stop failed: {exc}",
                    exit_code=DeployExitCode.STOP_FAILED,
                ) from exc
        readiness = _launch_and_verify(
            target,
            controller=process_controller,
            probe_path=selected_probe,
            token=str(token_factory()),
            timeout_seconds=timeout_seconds,
            heartbeat_samples=heartbeat_samples,
            poll_interval=poll_interval,
            expected_version=expected_version,
            expected_commit=expected_commit,
            base_environment=os.environ if base_environment is None else base_environment,
            sleep=sleep,
            monotonic=monotonic,
        )
    except RuntimeDeployError:
        raise
    except RuntimeLaunchError as exc:
        raise RuntimeDeployError(
            f"runtime restart launch failed: {exc}",
            exit_code=DeployExitCode.LAUNCH_FAILED,
        ) from exc
    except Exception as exc:
        raise RuntimeDeployError(
            f"runtime restart readiness failed: {exc}",
            exit_code=DeployExitCode.READINESS_FAILED,
        ) from exc
    finally:
        marker.unlink(missing_ok=True)
    return {
        "schema_version": 1,
        "operation": "restart",
        "status": "success",
        "timestamp": _utc_now(),
        "target": str(target),
        "terminated_pids": exact_pids,
        "readiness": readiness,
    }


def _pretransition_failure_receipt(
    candidate: Path,
    target: Path,
    error: str,
) -> dict[str, Any]:
    try:
        restartable_target = target.is_file() and target.stat().st_size > 0
    except OSError:
        restartable_target = False
    marker = target.with_name("windows-supporter.promotion-pending.json")
    backup = target.with_name("windows-supporter.previous.exe")
    transaction_conflict = marker.exists() or backup.exists()
    receipt = {
        "schema_version": 1,
        "operation": "deploy",
        "status": "failed",
        "timestamp": _utc_now(),
        "candidate": str(candidate),
        "target": str(target),
        "target_unchanged": True,
        "transaction_conflict": transaction_conflict,
        "rollback": {"status": "target-unchanged"},
        "recovery_action": (
            "restart-unchanged-runtime"
            if restartable_target and not transaction_conflict
            else "none"
        ),
        "error": error,
    }
    if transaction_conflict:
        receipt["preserved_transaction"] = {
            "marker": str(marker) if marker.exists() else None,
            "backup": str(backup) if backup.exists() else None,
        }
    return receipt


def deploy_runtime(
    candidate_path: str | os.PathLike[str],
    target_path: str | os.PathLike[str],
    *,
    controller: RuntimeProcessController | None = None,
    probe_path: str | os.PathLike[str] | None = None,
    token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
    heartbeat_samples: int = DEFAULT_HEARTBEAT_SAMPLES,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    expected_version: str | None = None,
    expected_commit: str | None = None,
    base_environment: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    copy_file: Callable[[Path, Path], Any] = shutil.copy2,
    replace_file: Callable[[Path, Path], Any] = os.replace,
) -> dict[str, Any]:
    candidate = Path(candidate_path).resolve()
    target = Path(target_path).resolve()
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        error = f"candidate executable is missing or empty: {candidate}"
        raise RuntimeDeployError(
            error,
            exit_code=DeployExitCode.INVALID_INPUT,
            receipt=_pretransition_failure_receipt(candidate, target, error),
        )
    had_previous = target.exists()
    if had_previous and (not target.is_file() or target.stat().st_size <= 0):
        error = f"installed runtime is missing or empty: {target}"
        raise RuntimeDeployError(
            error,
            exit_code=DeployExitCode.INVALID_INPUT,
            receipt=_pretransition_failure_receipt(candidate, target, error),
        )
    if _same_path(candidate, target):
        error = "candidate and installed runtime must be different files"
        raise RuntimeDeployError(
            error,
            exit_code=DeployExitCode.INVALID_INPUT,
            receipt=_pretransition_failure_receipt(candidate, target, error),
        )

    if controller is None:
        try:
            candidate_version, candidate_commit = _read_windows_artifact_identity(candidate)
        except Exception as exc:
            error = f"candidate version metadata validation failed: {exc}"
            raise RuntimeDeployError(
                error,
                exit_code=DeployExitCode.INVALID_INPUT,
                receipt=_pretransition_failure_receipt(candidate, target, error),
            ) from exc
        if expected_version and expected_version != candidate_version:
            error = "candidate FileVersion does not match --expected-version"
            raise RuntimeDeployError(
                error,
                exit_code=DeployExitCode.INVALID_INPUT,
                receipt=_pretransition_failure_receipt(candidate, target, error),
            )
        if expected_commit and expected_commit.lower() != candidate_commit.lower():
            error = "candidate commit does not match --expected-commit"
            raise RuntimeDeployError(
                error,
                exit_code=DeployExitCode.INVALID_INPUT,
                receipt=_pretransition_failure_receipt(candidate, target, error),
            )
        expected_version = candidate_version
        expected_commit = candidate_commit
    else:
        candidate_version = expected_version or ""
        candidate_commit = expected_commit or ""

    process_controller = controller or WindowsRuntimeProcessController()
    selected_probe = Path(probe_path) if probe_path else target.parent / ".windows-supporter.runtime-probe.json"
    backup = target.with_name("windows-supporter.previous.exe")
    marker = target.with_name("windows-supporter.promotion-pending.json")
    backup_stage = target.with_name(
        f".{target.name}.backup-{os.getpid()}-{uuid.uuid4().hex}"
    )
    staged = target.with_name(f".{target.name}.candidate-{os.getpid()}-{uuid.uuid4().hex}")
    restore_stage = target.with_name(f".{target.name}.restore-{os.getpid()}-{uuid.uuid4().hex}")
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "operation": "deploy",
        "status": "failed",
        "timestamp": _utc_now(),
        "candidate": str(candidate),
        "candidate_sha256": _sha256(candidate),
        "candidate_version": candidate_version,
        "candidate_commit": candidate_commit,
        "target": str(target),
        "had_previous": had_previous,
        "previous_sha256": _sha256(target) if had_previous else None,
        "target_unchanged": True,
        "transaction_conflict": False,
        "recovery_action": (
            "restart-unchanged-runtime" if had_previous else "none"
        ),
        "terminated_pids": [],
        "readiness": None,
        "rollback": {"status": "not-required"},
    }
    if marker.exists() or backup.exists():
        receipt["error"] = "an unfinished deployment transaction already exists"
        receipt["transaction_conflict"] = True
        receipt["recovery_action"] = "none"
        receipt["preserved_transaction"] = {
            "marker": str(marker) if marker.exists() else None,
            "backup": str(backup) if backup.exists() else None,
        }
        raise RuntimeDeployError(
            receipt["error"],
            exit_code=DeployExitCode.INVALID_INPUT,
            receipt=receipt,
        )
    exact_pids: list[int] = []
    changed_target = False
    transition_started = False
    transaction_claimed = False
    backup_ready = False
    failure_code = DeployExitCode.REPLACE_FAILED
    failure: Exception | None = None
    try:
        try:
            _claim_json_exclusive(
                marker,
                {
                    "schema_version": 1,
                    "target": str(target),
                    "backup": str(backup),
                    "candidate_sha256": receipt["candidate_sha256"],
                    "started_at": _utc_now(),
                },
            )
        except FileExistsError as exc:
            failure_code = DeployExitCode.INVALID_INPUT
            receipt["transaction_conflict"] = True
            receipt["recovery_action"] = "none"
            receipt["preserved_transaction"] = {
                "marker": str(marker),
                "backup": str(backup) if backup.exists() else None,
            }
            raise RuntimeError(
                "another deployment transaction acquired the target concurrently"
            ) from exc
        transaction_claimed = True
        if backup.exists():
            raise RuntimeError("an unowned deployment backup appeared after transaction claim")
        if had_previous:
            copy_file(target, backup_stage)
            if _sha256(backup_stage) != receipt["previous_sha256"]:
                raise RuntimeError("backup runtime hash differs from the installed runtime")
            replace_file(backup_stage, backup)
            backup_ready = True
        copy_file(candidate, staged)
        if _sha256(staged) != receipt["candidate_sha256"]:
            raise RuntimeError("staged runtime hash differs from the candidate")
        exact_pids = list(process_controller.find_exact(target))
        try:
            if exact_pids:
                transition_started = True
                receipt["recovery_action"] = "none"
                process_controller.terminate_tree(exact_pids, DEFAULT_STOP_TIMEOUT_SECONDS)
        except Exception as exc:
            failure_code = DeployExitCode.STOP_FAILED
            raise RuntimeError(f"failed to stop exact installed runtime: {exc}") from exc
        receipt["terminated_pids"] = exact_pids
        transition_started = True
        receipt["recovery_action"] = "none"
        try:
            replace_file(staged, target)
            changed_target = True
            receipt["target_unchanged"] = False
        except Exception as exc:
            failure_code = DeployExitCode.REPLACE_FAILED
            raise RuntimeError(f"atomic candidate replacement failed: {exc}") from exc
        if _sha256(target) != receipt["candidate_sha256"]:
            failure_code = DeployExitCode.REPLACE_FAILED
            raise RuntimeError("promoted runtime hash differs from the candidate")
        try:
            readiness = _launch_and_verify(
                target,
                controller=process_controller,
                probe_path=selected_probe,
                token=str(token_factory()),
                timeout_seconds=timeout_seconds,
                heartbeat_samples=heartbeat_samples,
                poll_interval=poll_interval,
                expected_version=expected_version,
                expected_commit=expected_commit,
                base_environment=os.environ if base_environment is None else base_environment,
                sleep=sleep,
                monotonic=monotonic,
            )
        except RuntimeLaunchError as exc:
            failure_code = DeployExitCode.LAUNCH_FAILED
            raise RuntimeError(str(exc)) from exc
        except Exception as exc:
            failure_code = DeployExitCode.READINESS_FAILED
            raise RuntimeError(str(exc)) from exc
        receipt["status"] = "success"
        receipt["target_unchanged"] = False
        receipt["readiness"] = readiness
        receipt["target_sha256"] = _sha256(target)
        marker.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        return receipt
    except Exception as exc:
        failure = exc
    finally:
        backup_stage.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)

    if not transition_started and not changed_target:
        cleanup_errors: list[str] = []
        if backup_ready:
            try:
                backup.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"backup cleanup failed: {cleanup_exc}")
        if transaction_claimed:
            try:
                marker.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"marker cleanup failed: {cleanup_exc}")
        receipt["rollback"] = {
            "status": "target-unchanged",
            "cleanup_errors": cleanup_errors,
        }
        receipt["error"] = str(failure or "deployment preparation failed")
        if cleanup_errors:
            failure_code = DeployExitCode.ROLLBACK_FAILED
        raise RuntimeDeployError(
            receipt["error"],
            exit_code=failure_code,
            receipt=receipt,
        ) from failure

    rollback: dict[str, Any] = {"status": "failed", "error": ""}
    try:
        running = list(process_controller.find_exact(target))
        if running:
            process_controller.terminate_tree(running, DEFAULT_STOP_TIMEOUT_SECONDS)
        if had_previous:
            if not backup.is_file():
                raise FileNotFoundError(backup)
            copy_file(backup, restore_stage)
            replace_file(restore_stage, target)
            readiness = _launch_and_verify(
                target,
                controller=process_controller,
                probe_path=selected_probe,
                token=str(token_factory()),
                timeout_seconds=timeout_seconds,
                heartbeat_samples=heartbeat_samples,
                poll_interval=poll_interval,
                expected_version=None,
                expected_commit=None,
                base_environment=(
                    os.environ if base_environment is None else base_environment
                ),
                sleep=sleep,
                monotonic=monotonic,
            )
            rollback = {
                "status": "ready",
                "target_sha256": _sha256(target),
                "readiness": readiness,
            }
        else:
            target.unlink(missing_ok=True)
            rollback = {"status": "restored-absent"}
        marker.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
    except Exception as rollback_exc:
        rollback = {"status": "failed", "error": str(rollback_exc)}
        failure_code = DeployExitCode.ROLLBACK_FAILED
    finally:
        restore_stage.unlink(missing_ok=True)
    try:
        receipt["target_unchanged"] = (
            target.is_file()
            and receipt["previous_sha256"] is not None
            and _sha256(target) == receipt["previous_sha256"]
            if had_previous
            else not target.exists()
        )
    except OSError:
        receipt["target_unchanged"] = False
    receipt["rollback"] = rollback
    receipt["error"] = str(failure or "deployment failed")
    raise RuntimeDeployError(
        receipt["error"],
        exit_code=failure_code,
        receipt=receipt,
    ) from failure


def cli(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")
    parser = _JsonArgumentParser(description="Transactional windows-supporter runtime deployment")
    parser.add_argument("--candidate")
    parser.add_argument("--target")
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-commit")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_READY_TIMEOUT_SECONDS)
    parser.add_argument("--heartbeat-samples", type=int, default=DEFAULT_HEARTBEAT_SAMPLES)
    parser.add_argument("--contract-smoke")
    try:
        args = parser.parse_args(argv)
    except _CliArgumentError as exc:
        receipt = {
            "schema_version": 1,
            "operation": "deploy",
            "status": "failed",
            "timestamp": _utc_now(),
            "error": str(exc),
        }
        print(f"runtime deployment argument error: {exc}", file=sys.stderr, flush=True)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True), flush=True)
        return int(DeployExitCode.INVALID_INPUT)
    if args.contract_smoke is not None:
        print("배포 도우미 진단: UTF-8 stderr", file=sys.stderr, flush=True)
        print(
            json.dumps(
                {"schema_version": 1, "status": "ok", "value": args.contract_smoke},
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    if not args.candidate or not args.target:
        receipt = {
            "schema_version": 1,
            "operation": "deploy",
            "status": "failed",
            "timestamp": _utc_now(),
            "error": "--candidate and --target are required",
        }
        print(
            "runtime deployment argument error: --candidate and --target are required",
            file=sys.stderr,
            flush=True,
        )
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True), flush=True)
        return int(DeployExitCode.INVALID_INPUT)
    try:
        receipt = deploy_runtime(
            args.candidate,
            args.target,
            expected_version=args.expected_version,
            expected_commit=args.expected_commit,
            timeout_seconds=args.timeout_seconds,
            heartbeat_samples=args.heartbeat_samples,
        )
    except RuntimeDeployError as exc:
        print(f"runtime deployment failed: {exc}", file=sys.stderr, flush=True)
        print(json.dumps(exc.receipt, ensure_ascii=False, sort_keys=True), flush=True)
        return int(exc.exit_code)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
