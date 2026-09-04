from __future__ import annotations

from collections.abc import Mapping


class UpdateHandoffError(RuntimeError):
    __slots__ = ("message",)

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def build_relaunch_environment(base_environment: Mapping[str, str]) -> dict[str, str]:
    environment = dict(base_environment)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment
