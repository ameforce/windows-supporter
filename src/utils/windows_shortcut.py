from __future__ import annotations

import os
import re
import shlex
from typing import Callable

_PROFILE_RE = re.compile(r'--profile-directory=(?:"([^"]+)"|(\S+))')
_APP_ID_RE = re.compile(r'--app-id=(?:"([^"]+)"|(\S+))')


def read_shortcut_target_args(
    lnk_path: str, log: Callable[[str], None] | None = None
) -> dict[str, str] | None:
    path = str(lnk_path).strip()
    if not path or not os.path.isfile(path):
        return None
    if not path.lower().endswith(".lnk"):
        return None

    try:
        import pythoncom
        import win32com.client
    except Exception as exc:
        if log is not None:
            log(f"shortcut read unavailable: {exc!r}")
        return None

    did_init = False
    try:
        pythoncom.CoInitialize()
        did_init = True
    except Exception:
        pass

    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(path)
        target = str(getattr(shortcut, "Targetpath", "") or "").strip()
        args = str(getattr(shortcut, "Arguments", "") or "").strip()
        if not target:
            return None
        return {"target": target, "args": args}
    except Exception as exc:
        if log is not None:
            log(f"shortcut read failed: {path} ({exc!r})")
        return None
    finally:
        try:
            shortcut = None
            shell = None
        except Exception:
            pass
        try:
            if did_init:
                pythoncom.CoUninitialize()
        except Exception:
            pass


def split_args(raw_args: str) -> list[str]:
    value = str(raw_args).strip()
    if not value:
        return []
    try:
        return shlex.split(value, posix=False)
    except Exception:
        return [p for p in value.split() if p]


def parse_chrome_pwa_args(raw_args: str) -> tuple[str | None, str | None, list[str]]:
    raw = str(raw_args or "")
    profile_dir = None
    app_id = None

    m = _PROFILE_RE.search(raw)
    if m:
        profile_dir = (m.group(1) or m.group(2) or "").strip()

    m = _APP_ID_RE.search(raw)
    if m:
        app_id = (m.group(1) or m.group(2) or "").strip()

    return profile_dir, app_id, []
