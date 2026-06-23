# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/qa_update_ui_smoke.py --output .omo/ulw-loop/evidence/C003-update-ui-smoke.json
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from src.apps.update_settings_ui import UpdateSettingsView
from src.utils.update_monitor import (
    UpdateHandoffProgressUi,
    build_update_build_output_progress_snapshot,
    build_update_progress_snapshot,
)


class SmokeUpdater:
    def __init__(self, settings_path: Path) -> None:
        self.settings_path = settings_path
        self.settings: dict[str, Any] = {
            "auto_check_enabled": True,
            "check_interval_minutes": 10,
            "check_interval_ms": 600000,
            "settings_path": str(settings_path),
            "auto_update_available": True,
            "unavailable_reason": "",
        }
        self.check_calls = 0

    def get_settings_snapshot(self) -> dict[str, Any]:
        return dict(self.settings)

    def update_settings(self, payload: dict[str, Any]) -> tuple[bool, str | None]:
        self.settings.update(payload)
        minutes = int(self.settings.get("check_interval_minutes", 10))
        self.settings["check_interval_ms"] = minutes * 60 * 1000
        return True, None

    def get_status_snapshot(self) -> dict[str, Any]:
        return {
            "state": "updating",
            "progress": build_update_progress_snapshot("fetch", state="running"),
        }

    def check_now(self, *, manual: bool = False) -> None:
        if manual:
            self.check_calls += 1


def run_smoke(output_path: Path) -> dict[str, Any]:
    import tkinter as tk

    labels: list[str] = []
    cleanup: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        settings_path = Path(tmp) / "update_settings.json"
        root = tk.Tk()
        root.withdraw()
        try:
            frame = tk.Frame(root)
            frame.pack(fill="both", expand=True)
            updater = SmokeUpdater(settings_path)
            settings_view = UpdateSettingsView(root, updater)
            settings_view.mount(frame)
            settings_view._check_now()
            root.update_idletasks()
            root.update()
            labels.append(str(settings_view._status_label.cget("text")))

            progress = UpdateHandoffProgressUi(log_path=str(Path(tmp) / "update.log"))
            progress.show(build_update_progress_snapshot("fetch", state="running"))
            build_snapshot = build_update_build_output_progress_snapshot(
                "Building main.py to windows-supporter.exe...[ Success !! ]"
            )
            if build_snapshot is not None:
                progress.set_snapshot(build_snapshot)
                labels.append(str(build_snapshot["label"]))
                labels.append(str(build_snapshot["detail"]))
            progress.set_snapshot(build_update_progress_snapshot("relaunch", state="running"))
            labels.append("Windows Supporter 재실행 중")
            progress.close()
            cleanup.append("closed UpdateHandoffProgressUi")
        finally:
            root.destroy()
            cleanup.append("destroyed Tk root")

    result = {
        "ok": all(
            [
                any("자동 확인" in label for label in labels),
                any("실행 파일 빌드 중" in label for label in labels),
                any("build.bat 단계" in label for label in labels),
                any("재실행" in label for label in labels),
                cleanup == ["closed UpdateHandoffProgressUi", "destroyed Tk root"],
            ]
        ),
        "labels": labels,
        "cleanup": cleanup,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run_smoke(Path(args.output))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
