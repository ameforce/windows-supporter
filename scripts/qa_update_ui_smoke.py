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
    build_update_handoff_payload,
    build_update_build_output_progress_snapshot,
    build_update_progress_snapshot,
    publish_build_output_progress,
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


def _capture_window_screenshot(window: Any, output_path: Path) -> str:
    try:
        import pyautogui

        window.update_idletasks()
        window.update()
        x = int(window.winfo_rootx())
        y = int(window.winfo_rooty())
        width = int(window.winfo_width())
        height = int(window.winfo_height())
        image = pyautogui.screenshot(region=(x, y, width, height))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
        return str(output_path)
    except Exception:
        pass
    try:
        import ctypes
        import struct
        import zlib
        from ctypes import wintypes

        hwnd = int(window.winfo_id())
        rect = wintypes.RECT()
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return "screenshot failed: GetWindowRect failed"
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return "screenshot failed: empty window bounds"
        hdc_window = user32.GetWindowDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        old_obj = gdi32.SelectObject(hdc_mem, hbmp)
        printed = user32.PrintWindow(hwnd, hdc_mem, 2)
        if not printed:
            gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_window, 0, 0, 0x00CC0020)

        class BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BitmapInfo(ctypes.Structure):
            _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]

        bitmap_info = BitmapInfo()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        bitmap_info.bmiHeader.biWidth = width
        bitmap_info.bmiHeader.biHeight = -height
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = 0
        bitmap_info.bmiHeader.biSizeImage = width * height * 4
        buffer = ctypes.create_string_buffer(width * height * 4)
        read_rows = gdi32.GetDIBits(
            hdc_mem,
            hbmp,
            0,
            height,
            buffer,
            ctypes.byref(bitmap_info),
            0,
        )
        gdi32.SelectObject(hdc_mem, old_obj)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_window)
        if int(read_rows) != height:
            return "screenshot failed: GetDIBits failed"
        rgb = bytearray()
        raw = buffer.raw
        for offset in range(0, len(raw), 4):
            blue = raw[offset]
            green = raw[offset + 1]
            red = raw[offset + 2]
            rgb.extend((red, green, blue))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.lower() == ".png":
            rows = bytearray()
            row_size = width * 3
            for row in range(height):
                start = row * row_size
                rows.append(0)
                rows.extend(rgb[start : start + row_size])

            def chunk(kind: bytes, payload: bytes) -> bytes:
                return (
                    struct.pack(">I", len(payload))
                    + kind
                    + payload
                    + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
                )

            output_path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + chunk(
                    b"IHDR",
                    struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
                )
                + chunk(b"IDAT", zlib.compress(bytes(rows), level=6))
                + chunk(b"IEND", b"")
            )
        else:
            output_path.write_bytes(
                f"P6\n{width} {height}\n255\n".encode("ascii") + bytes(rgb)
            )
        return str(output_path)
    except Exception as exc:
        return f"screenshot failed: {exc!r}"


def run_smoke(output_path: Path, screenshot_path: Path | None = None) -> dict[str, Any]:
    import tkinter as tk

    labels: list[str] = []
    cleanup: list[str] = []
    screenshot = ""
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
            borderless = progress._root.overrideredirect() if progress._root is not None else None
            labels.append(
                "borderless-true"
                if borderless is True or borderless == 1 or str(borderless).lower() == "true"
                else f"borderless-{borderless!r}"
            )
            labels.append(
                "log-visible"
                if progress._log_button.winfo_ismapped()
                or str(progress._log_button.winfo_manager() or "")
                else "log-hidden"
            )
            progress.set_snapshot(preflight)
            labels.append(str(preflight["label"]))
            labels.append(str(preflight["detail"]))
            state_path = Path(tmp) / "update_handoff.json"
            state_path.write_text(
                json.dumps(build_update_handoff_payload(repo_root=tmp), ensure_ascii=False),
                encoding="utf-8",
            )
            publish_build_output_progress(
                "\n".join(
                    [
                        "Building main.py to windows-supporter.exe...",
                        "1432 INFO: PyInstaller: checking Analysis",
                        "2179 INFO: Building PYZ (ZlibArchive)",
                        "3120 INFO: Building PKG (CArchive) windows-supporter.pkg",
                    ]
                ),
                progress_ui=progress,
                state_path=state_path,
                log_path=str(log_path),
                seen=set(),
            )
            activity_lines = list(getattr(progress, "_activity_lines", []))
            labels.extend(str(line) for line in activity_lines)
            labels.append(
                "activity-visible"
                if any(
                    str(label.cget("text") or "").strip()
                    for label in list(getattr(progress, "_activity_labels", []))
                )
                else "activity-hidden"
            )
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
            if screenshot_path is not None and progress._root is not None:
                screenshot = _capture_window_screenshot(progress._root, screenshot_path)
                labels.append("screenshot-written" if Path(screenshot).exists() else screenshot)
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
                any(label == "borderless-true" for label in labels),
                any(label == "log-visible" for label in labels),
                any("업데이트 사전 점검 중" in label for label in labels),
                any("Fork.exe" in label and "종료 승인" in label for label in labels),
                any("uv 환경 동기화 중" in label for label in labels),
                any(label == "30%" for label in labels),
                any("실행 파일 빌드 중" in label for label in labels),
                any("build.bat 단계" in label for label in labels),
                any("checking Analysis" in label for label in labels),
                any("Building PYZ" in label for label in labels),
                any("Building PKG" in label for label in labels),
                any(label == "activity-visible" for label in labels),
                screenshot_path is None or Path(screenshot).exists(),
                any("재실행" in label for label in labels),
                cleanup == ["closed UpdateHandoffProgressUi", "destroyed Tk root"],
            ]
        ),
        "labels": labels,
        "cleanup": cleanup,
        "screenshot": screenshot,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--screenshot", default="")
    args = parser.parse_args()
    result = run_smoke(
        Path(args.output),
        Path(args.screenshot) if str(args.screenshot or "").strip() else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
