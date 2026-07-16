# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/qa_update_ui_smoke.py --output .omo/ulw-loop/evidence/C003-update-ui-smoke.json
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
        import ctypes
        import struct
        import zlib
        from ctypes import wintypes

        hwnd = int(window.winfo_id())
        rect = wintypes.RECT()
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        top_level_hwnd = int(user32.GetAncestor(hwnd, 2) or hwnd)
        hwnd = top_level_hwnd
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return "screenshot failed: GetWindowRect failed"
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return "screenshot failed: empty window bounds"
        hdc_window = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        old_obj = gdi32.SelectObject(hdc_mem, hbmp)
        copied = gdi32.BitBlt(
            hdc_mem,
            0,
            0,
            width,
            height,
            hdc_window,
            int(rect.left),
            int(rect.top),
            0x40CC0020,
        )
        if not copied:
            return "screenshot failed: BitBlt failed"

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
        user32.ReleaseDC(0, hdc_window)
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


MATRIX_FIXTURES = (
    "initial",
    "middle-empty",
    "middle-activity",
    "failed",
    "complete",
    "long-text",
)
CAPTURE_FIXTURES = MATRIX_FIXTURES + ("shutdown",)
MATRIX_SCALINGS = (("100", 1.3333333333), ("125", 1.6666666667), ("150", 2.0))


def _walk_widgets(widget: Any) -> list[Any]:
    descendants = [widget]
    for child in widget.winfo_children():
        descendants.extend(_walk_widgets(child))
    return descendants


def _window_dpi(window: Any) -> int:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = int(user32.GetAncestor(int(window.winfo_id()), 2) or window.winfo_id())
        get_dpi = getattr(user32, "GetDpiForWindow", None)
        return int(get_dpi(hwnd)) if callable(get_dpi) else 0
    except Exception:
        return 0


def _apply_fixture(progress: UpdateHandoffProgressUi, fixture: str, log_path: str) -> None:
    progress.show(
        build_update_progress_snapshot(
            "handoff_start",
            state="running",
            detail="업데이트 전용 프로세스를 시작했습니다.",
            log_path=log_path,
        )
    )
    if fixture == "initial":
        return
    progress.set_snapshot(
        build_update_progress_snapshot(
            "build_prepare",
            state="running",
            detail="안전한 빌드 환경을 준비하고 있습니다.",
            log_path=log_path,
        )
    )
    if fixture == "shutdown":
        snapshot = build_update_build_output_progress_snapshot(
            "Shutting down the running windows-supporter.exe process...[ Not running ]",
            log_path=log_path,
        )
        if snapshot is not None:
            progress.set_snapshot(snapshot)
        return
    if fixture == "middle-empty":
        return
    for line in (
        "Syncing uv environment...[ Success !! ]",
        "Building main.py to windows-supporter.exe...[ Success !! ]",
        "Validating PyInstaller archive...[ Success !! ]",
    ):
        snapshot = build_update_build_output_progress_snapshot(line, log_path=log_path)
        if snapshot is not None:
            progress.set_snapshot(snapshot)
    if fixture == "middle-activity":
        return
    if fixture == "failed":
        progress.set_snapshot(
            build_update_progress_snapshot(
                "failed",
                state="failed",
                detail=(
                    "실행 파일 검증 단계에서 업데이트를 완료하지 못했습니다. "
                    "이전 버전으로 안전하게 복구했습니다. 로그에서 오류 코드 WSU-UPD-001을 확인해 주세요."
                ),
                percent=80,
                log_path=log_path,
                failed_step="실행 파일 검증",
                can_retry=True,
                can_manual_action=True,
            )
        )
        return
    if fixture == "complete":
        progress.set_snapshot(
            build_update_progress_snapshot(
                "complete",
                state="complete",
                log_path=log_path,
            )
        )
        return
    long_snapshot = build_update_progress_snapshot(
        "build",
        state="running",
        detail=(
            "긴 경로와 설명도 창 경계를 넘지 않아야 합니다: "
            + "C:\\Users\\epapyrus\\AppData\\Local\\WindowsSupporter\\"
            + ("매우긴업데이트상태문구" * 48)
        ),
        percent=74,
        log_path=log_path,
    )
    long_snapshot["activity"] = {
        "id": "long_text",
        "source": "build",
        "line": "긴 단계 설명 " + ("공백없는문자열" * 36),
    }
    progress.set_snapshot(long_snapshot)


def run_fixture_capture(
    fixture: str,
    scaling: float,
    output_path: Path,
    screenshot_path: Path,
) -> dict[str, Any]:
    import tkinter as tk

    original_tk = tk.Tk

    def scaled_tk(*args: Any, **kwargs: Any):
        root = original_tk(*args, **kwargs)
        root.tk.call("tk", "scaling", float(scaling))
        return root

    tk.Tk = scaled_tk
    progress = UpdateHandoffProgressUi(log_path=str(output_path.with_suffix(".log")))
    try:
        _apply_fixture(progress, fixture, str(output_path.with_suffix(".log")))
        root = progress._root
        if root is None:
            raise RuntimeError("progress root was not created")
        root.update_idletasks()
        root.update()
        drag_start = [int(root.winfo_x()), int(root.winfo_y())]
        progress._start_drag(
            types.SimpleNamespace(x_root=drag_start[0] + 20, y_root=drag_start[1] + 18)
        )
        progress._drag_window(
            types.SimpleNamespace(x_root=drag_start[0] + 44, y_root=drag_start[1] + 34)
        )
        progress._end_drag()
        root.update_idletasks()
        root.update()
        drag_end = [int(root.winfo_x()), int(root.winfo_y())]
        drag_delta = [drag_end[0] - drag_start[0], drag_end[1] - drag_start[1]]
        pointer_x = drag_end[0] + 20
        pointer_y = drag_end[1] + 18
        progress._start_drag(types.SimpleNamespace(x_root=pointer_x, y_root=pointer_y))
        progress._drag_window(
            types.SimpleNamespace(
                x_root=pointer_x + (-10 - drag_end[0]),
                y_root=pointer_y + (-20 - drag_end[1]),
            )
        )
        progress._end_drag()
        root.update_idletasks()
        negative_drag_position = [int(root.winfo_x()), int(root.winfo_y())]
        root.geometry(f"+{drag_end[0]}+{drag_end[1]}")
        root.update_idletasks()
        root.update()
        time.sleep(0.6)
        root.update_idletasks()
        root.update()
        screenshot = _capture_window_screenshot(root, screenshot_path)
        root_left = int(root.winfo_rootx())
        root_top = int(root.winfo_rooty())
        root_right = root_left + int(root.winfo_width())
        root_bottom = root_top + int(root.winfo_height())
        clipped: list[str] = []
        widget_metrics: list[dict[str, Any]] = []
        for widget in _walk_widgets(root):
            if widget is root or not widget.winfo_manager() or not widget.winfo_ismapped():
                continue
            left = int(widget.winfo_rootx())
            top = int(widget.winfo_rooty())
            width = int(widget.winfo_width())
            height = int(widget.winfo_height())
            right = left + width
            bottom = top + height
            path = str(widget)
            widget_metrics.append(
                {
                    "path": path,
                    "class": widget.winfo_class(),
                    "rect": [left, top, width, height],
                    "requested": [int(widget.winfo_reqwidth()), int(widget.winfo_reqheight())],
                }
            )
            if left < root_left or top < root_top or right > root_right + 1 or bottom > root_bottom + 1:
                clipped.append(path)
        visible_activity = [
            str(label.cget("text") or "")
            for label in progress._activity_labels
            if label.winfo_manager() and label.winfo_ismapped()
        ]
        visible_buttons = [
            str(button.cget("text") or "")
            for button in (
                progress._retry_button,
                progress._manual_button,
                progress._log_button,
                progress._close_button,
            )
            if button is not None and button.winfo_manager() and button.winfo_ismapped()
        ]
        activity_manager = (
            progress._activity_shell.winfo_manager()
            if progress._activity_shell is not None
            else "missing"
        )
        detail_text = str(progress._detail_label.cget("text") or "")
        empty_expected = fixture in {"initial", "middle-empty"}
        result = {
            "ok": all(
                [
                    Path(screenshot).exists(),
                    not clipped,
                    activity_manager == "" if empty_expected else activity_manager == "pack",
                    bool(root.overrideredirect()),
                    drag_delta == [24, 16],
                    negative_drag_position == [-10, -20],
                    bool(root.bind("<Alt-F4>")),
                    not any(token in detail_text for token in ("build.bat 단계", "[ Success !! ]")),
                    len(detail_text) <= 320,
                ]
            ),
            "fixture": fixture,
            "scaling_requested": scaling,
            "tk_scaling": float(root.tk.call("tk", "scaling")),
            "tk_fpixels_1i": float(root.winfo_fpixels("1i")),
            "window_dpi": _window_dpi(root),
            "geometry": root.geometry(),
            "root_size": [int(root.winfo_width()), int(root.winfo_height())],
            "root_requested": [int(root.winfo_reqwidth()), int(root.winfo_reqheight())],
            "root_bg": str(root.cget("bg")),
            "borderless_shell": bool(root.overrideredirect()),
            "drag_start": drag_start,
            "drag_end": drag_end,
            "drag_delta": drag_delta,
            "negative_drag_position": negative_drag_position,
            "alt_f4_binding": bool(root.bind("<Alt-F4>")),
            "activity_manager": activity_manager,
            "activity_lines": visible_activity,
            "visible_buttons": visible_buttons,
            "detail": detail_text,
            "clipped_widgets": clipped,
            "widgets": widget_metrics,
            "screenshot": screenshot,
        }
    finally:
        progress.close()
        tk.Tk = original_tk
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def run_matrix(output_path: Path, matrix_dir: Path) -> dict[str, Any]:
    matrix_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for scale_name, scaling in MATRIX_SCALINGS:
        for fixture in MATRIX_FIXTURES:
            stem = f"{scale_name}-{fixture}"
            metrics_path = matrix_dir / f"{stem}.json"
            screenshot_path = matrix_dir / f"{stem}.png"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--fixture",
                    fixture,
                    "--scaling",
                    str(scaling),
                    "--output",
                    str(metrics_path),
                    "--screenshot",
                    str(screenshot_path),
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
            )
            if completed.returncode != 0:
                results.append(
                    {
                        "ok": False,
                        "fixture": fixture,
                        "scale": scale_name,
                        "stderr": completed.stderr,
                    }
                )
                continue
            results.append(json.loads(metrics_path.read_text(encoding="utf-8")))
    result = {
        "ok": len(results) == len(MATRIX_FIXTURES) * len(MATRIX_SCALINGS)
        and all(item.get("ok") for item in results),
        "capture_count": len(results),
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


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
                "borderless-shell"
                if bool(borderless)
                else f"unexpected-native-chrome-{borderless!r}"
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
                    label.winfo_manager() and label.winfo_ismapped()
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
                any(label == "borderless-shell" for label in labels),
                any(label == "log-visible" for label in labels),
                any("업데이트 사전 점검 중" in label for label in labels),
                any("Fork.exe" in label and "종료 승인" in label for label in labels),
                any("빌드 환경 동기화 중" in label for label in labels),
                any(label == "74%" for label in labels),
                any("실행 파일 빌드 중" in label for label in labels),
                not any("build.bat 단계" in label for label in labels),
                not any("checking Analysis" in label for label in labels),
                not any("Building PYZ" in label for label in labels),
                not any("Building PKG" in label for label in labels),
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
    parser.add_argument("--fixture", choices=CAPTURE_FIXTURES, default="")
    parser.add_argument("--scaling", type=float, default=1.3333333333)
    parser.add_argument("--matrix-dir", default="")
    args = parser.parse_args()
    if str(args.matrix_dir or "").strip():
        result = run_matrix(Path(args.output), Path(args.matrix_dir))
    elif str(args.fixture or "").strip():
        if not str(args.screenshot or "").strip():
            parser.error("--fixture requires --screenshot")
        result = run_fixture_capture(
            str(args.fixture),
            float(args.scaling),
            Path(args.output),
            Path(args.screenshot),
        )
    else:
        result = run_smoke(
            Path(args.output),
            Path(args.screenshot) if str(args.screenshot or "").strip() else None,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
