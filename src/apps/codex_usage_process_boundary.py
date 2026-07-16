from __future__ import annotations

import time
from dataclasses import dataclass
from typing import final

import psutil
import win32api
import win32job


@dataclass(frozen=True, slots=True)
class OwnedProcessMemorySample:
    max_rss_bytes: int = 0
    total_rss_bytes: int = 0
    max_private_bytes: int = 0
    total_private_bytes: int = 0


@final
class WindowsJobBoundary:
    """Owns a kill-on-close Windows Job for one browser worker tree."""

    def __init__(self) -> None:
        job = win32job.CreateJobObject(None, "")
        try:
            info = win32job.QueryInformationJobObject(
                job,
                win32job.JobObjectExtendedLimitInformation,
            )
            basic = info["BasicLimitInformation"]
            basic["LimitFlags"] = int(basic["LimitFlags"]) | int(
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                job,
                win32job.JobObjectExtendedLimitInformation,
                info,
            )
        except BaseException:
            win32api.CloseHandle(job)
            raise
        self._job = job

    def assign_process(self, process_handle: int) -> None:
        job = self._job
        if job is None:
            raise RuntimeError("job boundary is closed")
        win32job.AssignProcessToJobObject(job, process_handle)

    def terminate(self, exit_code: int = 0xC0DE) -> None:
        job = self._job
        if job is None:
            return
        win32job.TerminateJobObject(job, int(exit_code))

    def active_processes(self) -> int:
        job = self._job
        if job is None:
            return 0
        info = win32job.QueryInformationJobObject(
            job,
            win32job.JobObjectBasicAccountingInformation,
        )
        return max(0, int(info.get("ActiveProcesses", 0)))

    def wait_empty(self, timeout_sec: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while True:
            try:
                if self.active_processes() == 0:
                    return True
            except BaseException:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def close(self) -> None:
        job = self._job
        if job is None:
            return
        self._job = None
        win32api.CloseHandle(job)


def max_owned_process_rss_bytes(worker_pid: int) -> int:
    """Return the largest RSS among the owned worker and its descendants."""

    return owned_process_memory_sample(worker_pid).max_rss_bytes


def owned_process_memory_sample(worker_pid: int) -> OwnedProcessMemorySample:
    """Sample resident and private bytes for the worker and descendants."""

    try:
        root = psutil.Process(int(worker_pid))
        processes = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError, ValueError):
        return OwnedProcessMemorySample()
    max_rss = 0
    total_rss = 0
    max_private = 0
    total_private = 0
    for process in processes:
        try:
            rss = max(0, int(process.memory_info().rss))
            max_rss = max(max_rss, rss)
            total_rss += rss
            full_info = process.memory_full_info()
            private = max(0, int(getattr(full_info, "private", 0)))
            max_private = max(max_private, private)
            total_private += private
        except (psutil.Error, OSError, ValueError):
            continue
    return OwnedProcessMemorySample(
        max_rss_bytes=max_rss,
        total_rss_bytes=total_rss,
        max_private_bytes=max_private,
        total_private_bytes=total_private,
    )
