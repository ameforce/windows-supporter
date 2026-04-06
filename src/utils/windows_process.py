from __future__ import annotations

from typing import Iterable

import psutil


def snapshot_running_processes() -> tuple[set[str], set[tuple[str, str]]]:
    names: set[str] = set()
    pwas: set[tuple[str, str]] = set()

    for proc in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            name = proc.info.get("name") or ""
            if name:
                names.add(str(name).casefold())
            cmdline = proc.info.get("cmdline") or []
        except Exception:
            continue

        if not isinstance(cmdline, list) or not cmdline:
            continue

        app_id = None
        profile_dir = None
        for a in cmdline:
            s = str(a)
            if s.startswith("--app-id="):
                app_id = s.split("=", 1)[1].strip().strip('"')
            elif s.startswith("--profile-directory="):
                profile_dir = s.split("=", 1)[1].strip().strip('"')
        if app_id:
            pwas.add((str(app_id), str(profile_dir or "")))

    return names, pwas


def snapshot_running_pids() -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    name_to_pid: dict[str, int] = {}
    pwa_to_pid: dict[tuple[str, str], int] = {}

    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
        except Exception:
            pid = 0

        try:
            name = str(proc.info.get("name") or "").casefold()
        except Exception:
            name = ""

        if pid > 0 and name:
            prev = name_to_pid.get(name)
            if prev is None or pid < int(prev):
                name_to_pid[name] = int(pid)

        try:
            cmdline = proc.info.get("cmdline") or []
        except Exception:
            cmdline = []

        if pid <= 0 or not isinstance(cmdline, list) or not cmdline:
            continue

        app_id = None
        profile_dir = None
        for a in cmdline:
            s = str(a)
            if s.startswith("--app-id="):
                app_id = s.split("=", 1)[1].strip().strip('"')
            elif s.startswith("--profile-directory="):
                profile_dir = s.split("=", 1)[1].strip().strip('"')

        if app_id:
            key = (str(app_id), str(profile_dir or ""))
            prev = pwa_to_pid.get(key)
            if prev is None or pid < int(prev):
                pwa_to_pid[key] = int(pid)

    return name_to_pid, pwa_to_pid


def snapshot_running_name_pids() -> dict[str, int]:
    name_to_pid: dict[str, int] = {}
    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            pid = int(proc.info.get("pid") or 0)
        except Exception:
            continue

        try:
            name = str(proc.info.get("name") or "").casefold()
        except Exception:
            name = ""

        if pid > 0 and name:
            prev = name_to_pid.get(name)
            if prev is None or pid < int(prev):
                name_to_pid[name] = int(pid)
    return name_to_pid


def get_process_info(
    pid: int, cache: dict[int, tuple[str, list[str]]] | None = None
) -> tuple[str, list[str]]:
    if cache is not None:
        cached = cache.get(int(pid))
        if cached is not None:
            return cached

    name = ""
    cmdline: list[str] = []
    try:
        proc = psutil.Process(int(pid))
        try:
            name = str(proc.name() or "").casefold()
        except Exception:
            name = ""
        try:
            raw_cmd = proc.cmdline() or []
        except Exception:
            raw_cmd = []
        if isinstance(raw_cmd, list):
            cmdline = [str(x) for x in raw_cmd]
    except Exception:
        name = ""
        cmdline = []

    if cache is not None:
        cache[int(pid)] = (name, cmdline)
    return name, cmdline


def cmdline_matches_pwa(
    cmdline: Iterable[str],
    app_id: str,
    profile_directory: str | None,
) -> bool:
    target_app = str(app_id).strip()
    if not target_app:
        return False

    want_profile = (str(profile_directory).strip() if profile_directory else "")
    got_app = False
    got_profile = (not bool(want_profile))

    for a in cmdline:
        s = str(a)
        if s.startswith("--app-id="):
            got_app = (s.split("=", 1)[1].strip().strip('"') == target_app)
        elif want_profile and s.startswith("--profile-directory="):
            got_profile = (s.split("=", 1)[1].strip().strip('"') == want_profile)
        if got_app and got_profile:
            return True
    return False
