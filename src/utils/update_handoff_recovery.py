from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path


class UpdateHandoffError(RuntimeError):
    __slots__ = ("message",)

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def build_relaunch_environment(base_environment: Mapping[str, str]) -> dict[str, str]:
    environment = dict(base_environment)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def restore_previous_executable(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    staged = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
    try:
        shutil.copy2(source, staged)
        os.replace(staged, destination)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
