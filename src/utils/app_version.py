from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any


@dataclass(frozen=True)
class AppVersion:
    display_version: str
    numeric_version: str
    source_tag: str
    commit: str
    dirty: bool


_DEFAULT_VERSION = AppVersion(
    display_version="dev",
    numeric_version="0.0.0.0",
    source_tag="",
    commit="",
    dirty=False,
)


def _read_str(module: Any, name: str, default: str = "") -> str:
    value = getattr(module, name, default)
    text = str(value or "").strip()
    return text if text else default


def _read_bool(module: Any, name: str, default: bool = False) -> bool:
    value = getattr(module, name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def get_app_version() -> AppVersion:
    try:
        build_info = import_module("windows_supporter_build_info")
    except Exception:
        return _DEFAULT_VERSION

    display_version = _read_str(build_info, "DISPLAY_VERSION", _DEFAULT_VERSION.display_version)
    numeric_version = _read_str(build_info, "NUMERIC_VERSION", _DEFAULT_VERSION.numeric_version)
    return AppVersion(
        display_version=display_version,
        numeric_version=numeric_version,
        source_tag=_read_str(build_info, "SOURCE_TAG"),
        commit=_read_str(build_info, "COMMIT"),
        dirty=_read_bool(build_info, "DIRTY"),
    )


def get_app_version_label() -> str:
    return f"Version {get_app_version().display_version}"
