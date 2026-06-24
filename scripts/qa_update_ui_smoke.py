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
            "progress": build_update_progress_snapshot(
                "accepted",
                state="running",
                detail="선택한 버전 업데이트 요청을 접수했습니다.",
            ),
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

            log_path = Path(tmp) / "update.log"
            progress = UpdateHandoffProgressUi(log_path=str(log_path))
            handoff_start = build_update_progress_snapshot(
                "handoff_start",
                state="running",
                detail="업데이트 전용 프로세스가 시작되었습니다.",
                log_path=str(log_path),
            )
            preflight = build_update_progress_snapshot(
                "preflight",
                state="await_git_gui_close",
                detail="Fork.exe가 실행 중입니다. 종료 승인 대기 중입니다.",
            )
            progress.show(handoff_start)
            labels.append(str(handoff_start["label"]))
            labels.append(str(progress._percent_label.cget("text")))
            labels.append("custom-canvas" if progress._progress_canvas is not None else "missing-canvas")
            labels.append("log-visible" if progress._log_button.winfo_ismapped() else "log-hidden")
            progress.set_snapshot(preflight)
            labels.append(str(preflight["label"]))
            labels.append(str(preflight["detail"]))
            uv_snapshot = build_update_build_output_progress_snapshot(
                "Syncing uv environment...[ Success !! ]"
            )
            if uv_snapshot is not None:
                progress.set_snapshot(uv_snapshot)
                labels.append(str(uv_snapshot["label"]))
                labels.append(str(progress._percent_label.cget("text")))
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
                any("업데이트 프로세스 시작" in label for label in labels),
                any(label == "0%" for label in labels),
                any(label == "custom-canvas" for label in labels),
                any(label == "log-visible" for label in labels),
                any("업데이트 사전 점검 중" in label for label in labels),
                any("Fork.exe" in label and "종료 승인" in label for label in labels),
                any("uv 환경 동기화 중" in label for label in labels),
                any(label == "30%" for label in labels),
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
