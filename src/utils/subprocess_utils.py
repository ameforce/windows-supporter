from __future__ import annotations

import subprocess
from typing import Callable


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
