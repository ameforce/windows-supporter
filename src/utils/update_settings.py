from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UPDATE_SETTINGS_VERSION = 1
DEFAULT_AUTO_CHECK_ENABLED = True
DEFAULT_CHECK_INTERVAL_MINUTES = 10
MIN_CHECK_INTERVAL_MINUTES = 3
MAX_CHECK_INTERVAL_MINUTES = 240


@dataclass(frozen=True, slots=True)
class UpdateSettings:
    auto_check_enabled: bool = DEFAULT_AUTO_CHECK_ENABLED
    check_interval_minutes: int = DEFAULT_CHECK_INTERVAL_MINUTES
    settings_path: str = ""

    @property
    def check_interval_ms(self) -> int:
        return int(self.check_interval_minutes) * 60 * 1000

    def as_payload(self) -> dict[str, Any]:
        return {
            "settings_version": UPDATE_SETTINGS_VERSION,
            "auto_check_enabled": bool(self.auto_check_enabled),
            "check_interval_minutes": int(self.check_interval_minutes),
        }

    def as_snapshot(self) -> dict[str, Any]:
        snapshot = self.as_payload()
        snapshot["check_interval_ms"] = self.check_interval_ms
        snapshot["settings_path"] = str(self.settings_path)
        return snapshot


def get_update_settings_path(base_dir: str | os.PathLike[str] | None = None) -> Path:
    if base_dir is None:
        root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        base_dir = Path(root) / "windows-supporter"
    return Path(base_dir) / "update_settings.json"


def normalize_update_settings(
    data: dict[str, Any] | None,
    *,
    settings_path: str | os.PathLike[str] = "",
    current: UpdateSettings | None = None,
) -> UpdateSettings:
    source = dict(data or {})
    fallback = current or UpdateSettings(settings_path=str(settings_path or ""))
    enabled = source.get("auto_check_enabled", fallback.auto_check_enabled)
    minutes = source.get("check_interval_minutes", fallback.check_interval_minutes)
    try:
        interval = int(round(float(minutes)))
    except Exception:
        interval = int(fallback.check_interval_minutes)
    interval = max(MIN_CHECK_INTERVAL_MINUTES, min(MAX_CHECK_INTERVAL_MINUTES, interval))
    return UpdateSettings(
        auto_check_enabled=bool(enabled),
        check_interval_minutes=interval,
        settings_path=str(settings_path or fallback.settings_path),
    )


def load_update_settings(path: str | os.PathLike[str]) -> UpdateSettings:
    resolved = Path(path)
    try:
        with resolved.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        data = {}
    payload = data if isinstance(data, dict) else {}
    return normalize_update_settings(payload, settings_path=resolved)


def save_update_settings(path: str | os.PathLike[str], settings: UpdateSettings) -> None:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("w", encoding="utf-8") as fp:
        json.dump(settings.as_payload(), fp, ensure_ascii=False, indent=2)
        fp.write("\n")
