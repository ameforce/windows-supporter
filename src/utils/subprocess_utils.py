from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from typing import Any, Callable


def popen_no_window(
    argv: list[str], log: Callable[[str], None] | None = None
) -> subprocess.Popen | None:
    try:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags |= subprocess.DETACHED_PROCESS
        return subprocess.Popen(argv, creationflags=creationflags)
    except Exception as exc:
        if log is not None:
            log(f"launch failed: {argv!r} ({exc!r})")
        return None


def is_frozen_runtime(sys_module: Any | None = None) -> bool:
    runtime = sys if sys_module is None else sys_module
    try:
        return bool(getattr(runtime, "frozen", False))
    except Exception:
        return False


def build_python_module_command(
    module: str,
    args: Sequence[str] | None = None,
    *,
    sys_module: Any | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str] | None:
    runtime = sys if sys_module is None else sys_module
    module_name = str(module or "").strip()
    if not module_name:
        if log is not None:
            log("python module launch skipped: missing module name")
        return None
    if is_frozen_runtime(runtime):
        if log is not None:
            log(f"python module launch skipped in frozen runtime: -m {module_name}")
        return None

    executable = str(getattr(runtime, "executable", "") or "").strip()
    if not executable:
        if log is not None:
            log(f"python module launch skipped: no Python executable for -m {module_name}")
        return None

    return [executable, "-m", module_name, *[str(arg) for arg in (args or [])]]
