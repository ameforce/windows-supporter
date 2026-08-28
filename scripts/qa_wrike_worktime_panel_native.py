# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Native 800x540 synthetic renderer harness for the Wrike worktime panel."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import time
from typing import Any
import zlib


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

@dataclass(frozen=True)
class _RendererApi:
    WorktimeActivityPrompt: Any
    WorktimePanelDayRow: Any
    WorktimePanelLine: Any
    WorktimePanelModel: Any
    WorktimeQuickPanel: Any


RUNNER_VERSION = "2.3"
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
VIEWPORT = (800, 540)
CAPTURE_IDLE_TIMEOUT_MS = 60_000
SHORT_IDLE_TIMEOUT_MS = 1_200
SHORT_IDLE_EVENT_PUMP_GRACE_MS = 350
POINTER_DELIVERY_TIMEOUT_MS = 1_500
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CONVERTER_RELATIVE_PATH = Path("scripts") / "convert_bmp_to_png.ps1"
CAPTURE_BACKEND = "repository-owned-win32-client-getdc-bitblt"
CAPTURE_PROVENANCE = {
    "backend": CAPTURE_BACKEND,
    "api_chain": [
        "GetDC(target-window-client)",
        "BitBlt(target-window-client-compatible-dc)",
        CONVERTER_RELATIVE_PATH.as_posix(),
    ],
    "client_only": True,
    "unrelated_desktop_pixels_read": False,
    "converter": CONVERTER_RELATIVE_PATH.as_posix(),
}
CHECKPOINT_FILENAMES = {
    "initial": "initial.png",
    "vacation-provisional": "vacation-provisional.png",
    "break-active": "break-active.png",
    "activity-prompt-focus": "activity-prompt-focus.png",
    "error-last-good": "error-last-good.png",
}
REVIEW_RECEIPT_FILENAME = "review-receipt.json"
CAPTURE_COMPLETE_FILENAMES = frozenset(
    {
        *CHECKPOINT_FILENAMES.values(),
        "run.json",
        "manifest.pending.json",
    }
)
FINALIZE_INPUT_FILENAMES = frozenset(
    {*CAPTURE_COMPLETE_FILENAMES, REVIEW_RECEIPT_FILENAME}
)
FINALIZED_FILENAMES = frozenset(
    {
        *CHECKPOINT_FILENAMES.values(),
        "run.json",
        REVIEW_RECEIPT_FILENAME,
        "manifest.json",
    }
)
MAX_PNG_FILE_BYTES = 64 * 1024 * 1024
MAX_PNG_PIXELS = 16_000_000
MAX_PNG_DECODED_BYTES = 128 * 1024 * 1024
NONACTIVATING_SHOW_REPETITIONS = 3
FOCUS_SCOPE = "same-process synthetic Tk sentinel only"
SENSITIVITY_VALUES = frozenset({"none", "redacted", "restricted-local"})
REQUIRED_ASSERTIONS = frozenset(
    {
        "seven_weekday_rows",
        "today_badge_visible",
        "normal_capture_timeout_is_60_seconds",
        "exact_equal_refresh_preserves_widgets",
        "exact_equal_refresh_preserves_geometry",
        "exact_equal_refresh_uses_distinct_instance",
        "exact_equal_refresh_skips_render_structure",
        "exact_equal_refresh_skips_model_update",
        "exact_equal_refresh_skips_reconcile",
        "same_structure_refresh_preserves_widgets",
        "same_structure_refresh_preserves_geometry",
        "same_structure_refresh_skips_render_structure",
        "same_structure_refresh_updates_model_once",
        "same_structure_refresh_skips_reconcile",
        "provisional_vacation_wording_visible",
        "break_callback_invoked",
        "break_button_transition",
        "keyboard_focus_visible",
        "prompt_actions_in_focus_order",
        "nonactivating_show_preserves_focus",
        "nonactivating_show_preserves_foreground_hwnd",
        "nonactivating_show_preserves_geometry",
        "repeated_nonactivating_show_preserves_contract",
        "prompt_focus_checkpoint",
        "snooze_callback_invoked",
        "prompt_hidden_after_snooze",
        "error_state_visible",
        "native_pointer_leave_delivered",
        "native_pointer_enter_delivered",
        "interaction_native_leave_delivered",
        "rearmed_native_leave_delivered",
        "short_idle_timeout_withdraws",
        "idle_withdraw_preserves_window",
        "hover_defers_idle_withdraw",
        "interaction_defers_idle_withdraw",
        "rearmed_idle_withdraws",
        "reopen_reuses_window",
        "normal_idle_timeout_restored",
        "capture_revision_stable",
        "checkpoint_png_set_exact",
        "client_only_capture_provenance",
        "all_states_unclipped",
        "no_runtime_errors",
    }
)
SCOPE_CLAIMS = (
    "Synthetic WorktimeQuickPanel renderer content at 800x540",
    "Synthetic renderer layout and required control visibility",
    "Synthetic Tk mouse callbacks and visible state transitions",
    "Exact-equal refresh dispatches no render, update, or geometry work; same-structure refresh dispatches one in-place update",
    "Win32 pointer moves delivered through additive Tk <Enter>/<Leave> bindings",
    "Synthetic provisional vacation wording",
    "Synthetic idle-dismiss, hover/interaction defer, and reusable reopen lifecycle",
    "Same-process synthetic Tk sentinel focus preservation",
)
SCOPE_EXCLUSIONS = (
    {
        "id": "production-snapshot",
        "description": "Production Wrike snapshot integration is not exercised.",
    },
    {
        "id": "production-cache",
        "description": "Production snapshot cache behavior is not exercised.",
    },
    {
        "id": "vacation",
        "description": (
            "Production vacation fetch and calculation are not exercised; only "
            "synthetic provisional wording is inspected."
        ),
    },
    {
        "id": "state-persistence",
        "description": "Production state persistence or migration is not exercised.",
    },
    {
        "id": "tray",
        "description": "Tray integration is not exercised.",
    },
    {
        "id": "hotkey",
        "description": "Global hotkey integration is not exercised.",
    },
    {
        "id": "packaged-exe",
        "description": "Packaged windows-supporter.exe behavior is not exercised.",
    },
    {
        "id": "cross-process-focus",
        "description": "Cross-process foreground and focus preservation is not exercised.",
    },
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--finalize-review", action="store_true")
    mode.add_argument("--validate-finalized", action="store_true")
    parser.add_argument(
        "--review-receipt",
        help=(
            "Declared manual-review receipt. With --finalize-review this must "
            "be the review-receipt.json sibling of run.json in --output-dir."
        ),
    )
    args = parser.parse_args(argv)
    if args.finalize_review and not args.review_receipt:
        parser.error("--review-receipt is required with --finalize-review")
    if args.review_receipt and not args.finalize_review:
        parser.error("--review-receipt is only valid with --finalize-review")
    return args


def _external_output_dir(raw: str) -> Path:
    output = Path(raw).expanduser().resolve(strict=False)
    repo = REPO_ROOT.resolve()
    try:
        common = Path(os.path.commonpath([str(repo), str(output)]))
    except ValueError:
        common = Path()
    if os.path.normcase(str(common)) == os.path.normcase(str(repo)):
        raise ValueError("--output-dir must be outside the repository")
    return output


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _decode_process_output(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "cp949", "cp1252"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _git_output(*args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout


def _target_revision() -> str:
    head = _decode_process_output(_git_output("rev-parse", "HEAD"))
    status = _git_output(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    diff = _git_output("diff", "--binary", "HEAD", "--", ".")
    digest = hashlib.sha256()
    digest.update(diff)
    for item in status.split(b"\0"):
        if not item.startswith(b"?? "):
            continue
        relative_bytes = item[3:]
        relative = relative_bytes.decode("utf-8", errors="strict")
        path = REPO_ROOT / relative
        if path.is_file():
            contents = path.read_bytes()
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(len(contents).to_bytes(8, "big"))
            digest.update(contents)
    return f"git:{head}+worktree:{digest.hexdigest()}"


def _require_target_revision(expected: str, phase: str) -> str:
    observed = _target_revision()
    if observed != expected:
        raise RuntimeError(
            f"target revision changed {phase}: expected {expected}, got {observed}"
        )
    return observed


def _load_renderer(expected_revision: str) -> _RendererApi:
    """Import renderer code only inside capture mode under a revision seal."""

    _require_target_revision(expected_revision, "before renderer import")
    module = importlib.import_module("src.apps.wrike_worktime_panel")
    renderer = _RendererApi(
        WorktimeActivityPrompt=module.WorktimeActivityPrompt,
        WorktimePanelDayRow=module.WorktimePanelDayRow,
        WorktimePanelLine=module.WorktimePanelLine,
        WorktimePanelModel=module.WorktimePanelModel,
        WorktimeQuickPanel=module.WorktimeQuickPanel,
    )
    _require_target_revision(expected_revision, "after renderer import")
    return renderer


def _rows(renderer: _RendererApi) -> tuple[Any, ...]:
    summaries = (
        "Wrike 8시간 · 목표 8시간 · 딱 맞음",
        "Wrike 7시간 30분 · 목표 8시간 · 부족 30분",
        "Wrike 8시간 20분 · 목표 8시간 · 초과 20분",
        "Wrike 5시간 30분 · 현재 기대 5시간 · 초과 30분",
        "목표 8시간 · 휴가 2시간 · 적용 6시간",
        "목표 2시간",
        "휴무",
    )
    return tuple(
        renderer.WorktimePanelDayRow(
            weekday=weekday,
            date=f"08/{24 + index:02d}",
            summary=summaries[index],
            today=index == 3,
            color="#059669" if index in {0, 2, 3} else "#6B7280",
        )
        for index, weekday in enumerate(WEEKDAYS)
    )


def _today_lines(
    renderer: _RendererApi,
    *,
    break_active: bool = False,
    error: bool = False,
    vacation_provisional: bool = False,
):
    if vacation_provisional and error:
        raise ValueError("vacation provisional and error fixtures are mutually exclusive")
    return (
        renderer.WorktimePanelLine(
            "Wrike 기록 5시간 30분 · 현재 기대 5시간"
            + (" (임시)" if vacation_provisional else ""),
            "#6B7280" if vacation_provisional else "#2563EB",
        ),
        renderer.WorktimePanelLine(
            "현재 기준 초과 30분"
            + (" (임시)" if vacation_provisional else ""),
            "#059669",
        ),
        renderer.WorktimePanelLine(
            "출근 08:00 · 예상 퇴근 17:00"
            + (" (임시)" if vacation_provisional else ""),
            "#111827",
        ),
        renderer.WorktimePanelLine(
            "병합 휴게 1시간" + (" · 진행 중" if break_active else ""),
            "#6B7280",
        ),
        renderer.WorktimePanelLine(
            (
                "휴가 미확정 (loading) · "
                "휴가 미반영 임시 목표 8시간 (임시)"
                if vacation_provisional
                else "휴가 차감 0분 · 적용 목표 8시간"
            ),
            "#6B7280",
        ),
        renderer.WorktimePanelLine(
            "동기화 error · 마지막 성공값 유지 · request_failed"
            if error
            else "동기화 fresh · 방금",
            "#DC2626" if error else "#6B7280",
        ),
    )


def _model(
    renderer: _RendererApi,
    *,
    break_active: bool = False,
    prompt: Any | None = None,
    error: bool = False,
    vacation_provisional: bool = False,
) -> Any:
    return renderer.WorktimePanelModel(
        week_range="2026-08-24 - 2026-08-30",
        sync_text=(
            "2026-08-27 14:00:00 · error · request_failed"
            if error
            else "2026-08-27 14:00:00 · 방금 · fresh"
        ),
        sync_state="error" if error else "fresh",
        today_lines=_today_lines(
            renderer,
            break_active=break_active,
            error=error,
            vacation_provisional=vacation_provisional,
        ),
        has_clock_in=True,
        break_active=break_active,
        rows=_rows(renderer),
        prompt=prompt,
    )


def _walk(widget: Any) -> list[Any]:
    result = [widget]
    for child in widget.winfo_children():
        result.extend(_walk(child))
    return result


def _live_buttons(window: Any) -> list[Any]:
    return [
        widget
        for widget in _walk(window)
        if widget.winfo_class() == "Button" and widget.winfo_ismapped()
    ]


def _button(window: Any, text: str) -> Any:
    for candidate in _live_buttons(window):
        if str(candidate.cget("text")) == text:
            return candidate
    raise AssertionError(f"visible button not found: {text}")


def _click_button(window: Any, text: str) -> None:
    button = _button(window, text)
    button.update_idletasks()
    x = max(1, int(button.winfo_width()) // 2)
    y = max(1, int(button.winfo_height()) // 2)
    button.event_generate("<Enter>", x=x, y=y)
    window.update()
    button.event_generate("<Motion>", x=x, y=y, warp=True)
    window.update()
    button.event_generate("<ButtonPress-1>", x=x, y=y)
    window.update()
    button.event_generate("<ButtonRelease-1>", x=x, y=y)
    window.update_idletasks()
    window.update()


def _widget_identity(window: Any) -> tuple[str, ...]:
    return tuple(str(widget) for widget in _walk(window))


def _pump_events_for(window: Any, duration_ms: int) -> int:
    if type(duration_ms) is not int or duration_ms < 0:
        raise ValueError("duration_ms must be a non-negative int")
    started = time.monotonic()
    deadline = started + duration_ms / 1000.0
    while True:
        window.update_idletasks()
        window.update()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.02, remaining))
    return int((time.monotonic() - started) * 1000)


def _move_pointer(window: Any, *, inside: bool) -> list[int]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL

    geometry = _window_geometry(window)
    left = geometry["x"]
    top = geometry["y"]
    right = left + geometry["width"]
    bottom = top + geometry["height"]
    if inside:
        target = (left + geometry["width"] // 2, top + geometry["height"] // 2)
    else:
        virtual_left = user32.GetSystemMetrics(76)
        virtual_top = user32.GetSystemMetrics(77)
        virtual_right = virtual_left + user32.GetSystemMetrics(78)
        virtual_bottom = virtual_top + user32.GetSystemMetrics(79)
        candidates = (
            (virtual_left, virtual_top),
            (virtual_right - 1, virtual_top),
            (virtual_left, virtual_bottom - 1),
            (virtual_right - 1, virtual_bottom - 1),
        )
        target = next(
            (
                (x, y)
                for x, y in candidates
                if not (left <= x < right and top <= y < bottom)
            ),
            None,
        )
        if target is None:
            raise RuntimeError("could not choose a cursor position outside the panel")

    if not user32.SetCursorPos(*target):
        raise OSError(ctypes.get_last_error(), "Win32 SetCursorPos failed")
    _pump_events_for(window, 100)
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise OSError(ctypes.get_last_error(), "Win32 GetCursorPos failed")
    actual = [int(point.x), int(point.y)]
    actual_inside = left <= actual[0] < right and top <= actual[1] < bottom
    if actual_inside is not inside:
        raise RuntimeError(
            f"cursor placement precondition failed: expected inside={inside}, got {actual}"
        )
    return actual


def _wait_for_pointer_delivery(
    window: Any,
    events: list[dict[str, Any]],
    *,
    expected: str,
    start_index: int,
    timeout_ms: int = POINTER_DELIVERY_TIMEOUT_MS,
) -> int:
    if expected not in {"enter", "leave"}:
        raise ValueError("expected pointer transition must be enter or leave")
    if type(start_index) is not int or start_index < 0:
        raise ValueError("start_index must be a non-negative int")
    started = time.monotonic()
    deadline = started + max(1, int(timeout_ms)) / 1000.0
    while True:
        if any(
            item.get("sequence") == expected
            for item in events[start_index:]
            if isinstance(item, dict)
        ):
            return int((time.monotonic() - started) * 1000)
        window.update_idletasks()
        window.update()
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    raise RuntimeError(
        f"native Tk <{expected.title()}> delivery was not observed within {timeout_ms}ms"
    )


def _focus_order(start: Any, limit: int = 16) -> list[str]:
    order: list[str] = []
    current = start
    seen: set[str] = set()
    for _ in range(limit):
        key = str(current)
        if key in seen:
            break
        seen.add(key)
        if current.winfo_class() == "Button":
            order.append(str(current.cget("text")))
        current = current.tk_focusNext()
        if current is None:
            break
    return order


def _window_hwnd(window: Any) -> int:
    import ctypes
    from ctypes import wintypes

    get_ancestor = ctypes.windll.user32.GetAncestor
    get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
    get_ancestor.restype = wintypes.HWND
    hwnd = int(window.winfo_id())
    top = int(get_ancestor(wintypes.HWND(hwnd), 2) or hwnd)
    if top <= 0:
        raise RuntimeError(f"unsupported window handle: {top}")
    return top


def _foreground_hwnd() -> int:
    import ctypes
    from ctypes import wintypes

    get_foreground_window = ctypes.windll.user32.GetForegroundWindow
    get_foreground_window.argtypes = []
    get_foreground_window.restype = wintypes.HWND
    return int(get_foreground_window() or 0)


def _request_foreground_hwnd(window_handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    set_foreground_window = ctypes.windll.user32.SetForegroundWindow
    set_foreground_window.argtypes = [wintypes.HWND]
    set_foreground_window.restype = wintypes.BOOL
    return bool(set_foreground_window(wintypes.HWND(window_handle)))


def _window_geometry(window: Any) -> dict[str, int]:
    return {
        "x": int(window.winfo_rootx()),
        "y": int(window.winfo_rooty()),
        "width": int(window.winfo_width()),
        "height": int(window.winfo_height()),
    }


def _valid_viewport_geometry(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"x", "y", "width", "height"}
        and all(type(value[key]) is int for key in value)
        and value["width"] == VIEWPORT[0]
        and value["height"] == VIEWPORT[1]
    )


def _window_size(window: Any) -> list[int]:
    geometry = _window_geometry(window)
    return [geometry["width"], geometry["height"]]


def _capture_client_bitmap(window_handle: int, bitmap_path: Path) -> list[int]:
    import ctypes
    from ctypes import wintypes

    srccopy = 0x00CC0020
    dib_rgb_colors = 0
    bi_rgb = 0

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
        _fields_ = [
            ("bmiHeader", BitmapInfoHeader),
            ("bmiColors", wintypes.DWORD * 1),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    handle_type = wintypes.HANDLE

    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = handle_type
    user32.ReleaseDC.argtypes = [wintypes.HWND, handle_type]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [handle_type]
    gdi32.CreateCompatibleDC.restype = handle_type
    gdi32.CreateCompatibleBitmap.argtypes = [handle_type, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = handle_type
    gdi32.SelectObject.argtypes = [handle_type, handle_type]
    gdi32.SelectObject.restype = handle_type
    gdi32.BitBlt.argtypes = [
        handle_type,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        handle_type,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        handle_type,
        handle_type,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BitmapInfo),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [handle_type]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [handle_type]
    gdi32.DeleteDC.restype = wintypes.BOOL

    def fail(action: str) -> None:
        error = ctypes.get_last_error()
        raise OSError(error, f"Win32 {action} failed")

    hwnd = wintypes.HWND(window_handle)
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        fail("GetClientRect")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid target client bounds: {width}x{height}")

    source_dc = user32.GetDC(hwnd)
    if not source_dc:
        fail("GetDC(target client)")
    target_dc = None
    bitmap = None
    old_object = None
    bitmap_selected = False
    try:
        target_dc = gdi32.CreateCompatibleDC(source_dc)
        if not target_dc:
            fail("CreateCompatibleDC")
        bitmap = gdi32.CreateCompatibleBitmap(source_dc, width, height)
        if not bitmap:
            fail("CreateCompatibleBitmap")
        old_object = gdi32.SelectObject(target_dc, bitmap)
        invalid_object = ctypes.c_void_p(-1).value
        if not old_object or int(old_object) == invalid_object:
            fail("SelectObject(capture bitmap)")
        bitmap_selected = True
        if not gdi32.BitBlt(
            target_dc,
            0,
            0,
            width,
            height,
            source_dc,
            0,
            0,
            srccopy,
        ):
            fail("BitBlt(target client)")

        restored = gdi32.SelectObject(target_dc, old_object)
        if not restored or int(restored) == invalid_object:
            fail("SelectObject(original bitmap)")
        bitmap_selected = False

        stride = ((width * 32 + 31) // 32) * 4
        image_size = stride * height
        info = BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = bi_rgb
        info.bmiHeader.biSizeImage = image_size
        pixels = (ctypes.c_ubyte * image_size)()
        rows = gdi32.GetDIBits(
            target_dc,
            bitmap,
            0,
            height,
            ctypes.cast(pixels, ctypes.c_void_p),
            ctypes.byref(info),
            dib_rgb_colors,
        )
        if rows != height:
            fail("GetDIBits")

        file_header_size = 14
        bitmap_header_size = 40
        pixel_offset = file_header_size + bitmap_header_size
        file_size = pixel_offset + image_size
        with bitmap_path.open("wb") as stream:
            stream.write(
                struct.pack(
                    "<2sIHHI",
                    b"BM",
                    file_size,
                    0,
                    0,
                    pixel_offset,
                )
            )
            stream.write(
                struct.pack(
                    "<IiiHHIIiiII",
                    bitmap_header_size,
                    width,
                    height,
                    1,
                    32,
                    bi_rgb,
                    image_size,
                    0,
                    0,
                    0,
                    0,
                )
            )
            stream.write(bytes(pixels))
    finally:
        if bitmap_selected and target_dc and old_object:
            gdi32.SelectObject(target_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if target_dc:
            gdi32.DeleteDC(target_dc)
        user32.ReleaseDC(hwnd, source_dc)

    return [width, height]


def _capture(window: Any, output_path: Path) -> dict[str, Any]:
    converter = (REPO_ROOT / CONVERTER_RELATIVE_PATH).resolve(strict=False)
    if not converter.is_file():
        raise FileNotFoundError(f"BMP converter not found: {converter}")
    if REPO_ROOT.resolve() not in converter.parents:
        raise RuntimeError(f"BMP converter must be repository-owned: {converter}")

    bitmap_path = output_path.with_suffix(".capture.bmp")
    for candidate in (output_path, bitmap_path):
        if candidate.exists() or candidate.is_symlink():
            raise RuntimeError(
                f"capture path unexpectedly already exists; refusing to delete it: {candidate}"
            )
    window.update_idletasks()
    window.update()
    time.sleep(0.25)
    window_handle = _window_hwnd(window)
    try:
        client_dimensions = _capture_client_bitmap(window_handle, bitmap_path)
        converted = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(converter),
                "-InputPath",
                str(bitmap_path),
                "-OutputPath",
                str(output_path),
            ],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if converted.returncode != 0:
            raise RuntimeError(
                "BMP conversion failed: "
                f"{_decode_process_output(converted.stderr)}; stdout: "
                f"{_decode_process_output(converted.stdout)}"
            )
    finally:
        bitmap_path.unlink(missing_ok=True)

    if output_path.is_symlink() or not output_path.is_file():
        raise RuntimeError(f"client capture was not created as a regular file: {output_path}")
    if output_path.stat().st_size <= 0:
        raise RuntimeError(f"client capture is empty: {output_path}")
    return {
        **CAPTURE_PROVENANCE,
        "window_handle": window_handle,
        "client_dimensions": client_dimensions,
    }


def _decode_png(path: Path) -> dict[str, Any]:
    """Validate one bounded, complete, noninterlaced PNG and hash those bytes."""

    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"PNG must be a regular file: {path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_PNG_FILE_BYTES:
        raise RuntimeError(f"PNG file size is outside the allowed bound: {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != size or not raw.startswith(PNG_SIGNATURE):
        raise RuntimeError(f"invalid PNG signature or unstable file: {path}")

    offset = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int, int, int, int] | None = None
    palette_seen = False
    idat_started = False
    idat_ended = False
    iend_seen = False
    compressed = bytearray()

    while offset < len(raw):
        if iend_seen:
            raise RuntimeError(f"PNG contains bytes after IEND: {path}")
        if offset + 12 > len(raw):
            raise RuntimeError(f"PNG chunk header is truncated: {path}")
        chunk_length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_type = raw[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + chunk_length
        crc_end = data_end + 4
        if data_end < data_start or crc_end > len(raw):
            raise RuntimeError(f"PNG chunk exceeds file bounds: {path}")
        if not chunk_type.isalpha() or len(chunk_type) != 4:
            raise RuntimeError(f"PNG chunk type is invalid: {path}")
        chunk_data = raw[data_start:data_end]
        expected_crc = struct.unpack(">I", raw[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            label = chunk_type.decode("ascii", errors="replace")
            raise RuntimeError(f"PNG {label} CRC mismatch: {path}")
        offset = crc_end

        if header is None:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise RuntimeError(f"PNG must start with one 13-byte IHDR: {path}")
            header = struct.unpack(">IIBBBBB", chunk_data)
            continue
        if chunk_type == b"IHDR":
            raise RuntimeError(f"PNG contains duplicate IHDR: {path}")
        if chunk_type == b"IEND":
            if chunk_length != 0 or not idat_started:
                raise RuntimeError(f"PNG IEND ordering or length is invalid: {path}")
            iend_seen = True
            if offset != len(raw):
                raise RuntimeError(f"PNG contains trailing bytes after IEND: {path}")
            break
        if chunk_type == b"IDAT":
            if idat_ended:
                raise RuntimeError(f"PNG IDAT chunks are not consecutive: {path}")
            idat_started = True
            compressed.extend(chunk_data)
            if len(compressed) > MAX_PNG_FILE_BYTES:
                raise RuntimeError(f"PNG compressed stream is too large: {path}")
            continue
        if idat_started:
            idat_ended = True
        if chunk_type == b"PLTE":
            if palette_seen or idat_started:
                raise RuntimeError(f"PNG PLTE ordering is invalid: {path}")
            if chunk_length < 3 or chunk_length > 768 or chunk_length % 3:
                raise RuntimeError(f"PNG PLTE length is invalid: {path}")
            palette_seen = True
            continue
        if not (chunk_type[0] & 0x20):
            label = chunk_type.decode("ascii", errors="replace")
            raise RuntimeError(f"unsupported critical PNG chunk {label}: {path}")

    if header is None or not iend_seen or not compressed:
        raise RuntimeError(f"PNG required chunks are incomplete: {path}")
    width, height, bit_depth, color_type, compression, filter_method, interlace = header
    valid_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if width <= 0 or height <= 0 or width * height > MAX_PNG_PIXELS:
        raise RuntimeError(f"invalid or oversized PNG dimensions {width}x{height}: {path}")
    if bit_depth not in valid_depths.get(color_type, set()):
        raise RuntimeError(f"unsupported PNG color/depth: {path}")
    if compression != 0 or filter_method != 0 or interlace != 0:
        raise RuntimeError(f"PNG must use standard noninterlaced encoding: {path}")
    if color_type == 3 and not palette_seen:
        raise RuntimeError(f"indexed PNG is missing PLTE: {path}")
    if color_type in {0, 4} and palette_seen:
        raise RuntimeError(f"grayscale PNG must not contain PLTE: {path}")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    row_bytes = (width * channels * bit_depth + 7) // 8
    decoded_size = height * (row_bytes + 1)
    if decoded_size <= 0 or decoded_size > MAX_PNG_DECODED_BYTES:
        raise RuntimeError(f"PNG decoded stream is outside the allowed bound: {path}")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(compressed), decoded_size + 1)
        if len(decoded) > decoded_size or decompressor.unconsumed_tail:
            raise RuntimeError(f"PNG decoded stream exceeds expected scanlines: {path}")
        decoded += decompressor.flush(decoded_size + 1 - len(decoded))
    except zlib.error as exc:
        raise RuntimeError(f"PNG zlib stream is invalid: {path}") from exc
    if (
        not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or len(decoded) != decoded_size
    ):
        raise RuntimeError(f"PNG zlib stream does not match exact scanline size: {path}")
    stride = row_bytes + 1
    invalid_filters = [
        row
        for row in range(height)
        if decoded[row * stride] not in {0, 1, 2, 3, 4}
    ]
    if invalid_filters:
        raise RuntimeError(f"PNG contains invalid scanline filter bytes: {path}")
    return {
        "dimensions": [int(width), int(height)],
        "sha256": digest,
        "decoded_bytes": int(decoded_size),
        "fully_decoded": True,
    }


def _visual_metrics(window: Any) -> dict[str, Any]:
    window.update_idletasks()
    window.update()
    left = int(window.winfo_rootx())
    top = int(window.winfo_rooty())
    width = int(window.winfo_width())
    height = int(window.winfo_height())
    right = left + width
    bottom = top + height
    clipped: list[str] = []
    underallocated: list[str] = []
    labels: list[str] = []
    buttons: list[str] = []
    for widget in _walk(window):
        if widget is window or not widget.winfo_ismapped() or not widget.winfo_manager():
            continue
        widget_left = int(widget.winfo_rootx())
        widget_top = int(widget.winfo_rooty())
        widget_width = int(widget.winfo_width())
        widget_height = int(widget.winfo_height())
        widget_right = widget_left + widget_width
        widget_bottom = widget_top + widget_height
        if (
            widget_left < left
            or widget_top < top
            or widget_right > right + 1
            or widget_bottom > bottom + 1
        ):
            clipped.append(str(widget))
        if widget.winfo_class() in {"Label", "Button"}:
            if (
                int(widget.winfo_reqwidth()) > widget_width + 2
                or int(widget.winfo_reqheight()) > widget_height + 2
            ):
                underallocated.append(str(widget))
        if widget.winfo_class() == "Label":
            labels.append(str(widget.cget("text")))
        elif widget.winfo_class() == "Button":
            buttons.append(str(widget.cget("text")))
    focused = window.focus_get()
    focus_text = ""
    if focused is not None and focused.winfo_class() == "Button":
        focus_text = str(focused.cget("text"))
    return {
        "window_geometry": {
            "x": left,
            "y": top,
            "width": width,
            "height": height,
        },
        "window_size": [width, height],
        "window_requested": [int(window.winfo_reqwidth()), int(window.winfo_reqheight())],
        "clipped_widgets": clipped,
        "underallocated_text_widgets": underallocated,
        "labels": labels,
        "buttons": buttons,
        "focus_text": focus_text,
    }


def _capture_state(
    window: Any,
    output_dir: Path,
    state: str,
    *,
    required_labels: tuple[str, ...] = (),
    required_buttons: tuple[str, ...] = (),
) -> dict[str, Any]:
    try:
        filename = CHECKPOINT_FILENAMES[state]
    except KeyError as exc:
        raise ValueError(f"unknown checkpoint state: {state}") from exc
    metrics = _visual_metrics(window)
    screenshot = output_dir / filename
    capture_provenance = _capture(window, screenshot)
    png = _decode_png(screenshot)
    dimensions = png["dimensions"]
    missing_labels = [text for text in required_labels if text not in metrics["labels"]]
    missing_buttons = [text for text in required_buttons if text not in metrics["buttons"]]
    metrics.update(
        {
            "state": state,
            "screenshot": str(screenshot),
            "sha256": png["sha256"],
            "dimensions": dimensions,
            "png_signature": PNG_SIGNATURE.hex(),
            "png_fully_decoded": png["fully_decoded"],
            "png_decoded_bytes": png["decoded_bytes"],
            "capture_provenance": capture_provenance,
            "missing_labels": missing_labels,
            "missing_buttons": missing_buttons,
            "ok": bool(
                metrics["window_size"] == list(VIEWPORT)
                and dimensions == list(VIEWPORT)
                and capture_provenance["client_dimensions"] == list(VIEWPORT)
                and capture_provenance["client_only"] is True
                and capture_provenance["unrelated_desktop_pixels_read"] is False
                and not metrics["clipped_widgets"]
                and not metrics["underallocated_text_widgets"]
                and not missing_labels
                and not missing_buttons
            ),
        }
    )
    return metrics


def _dpi_snapshot(window: Any) -> dict[str, Any]:
    dpi = 0
    try:
        import ctypes
        from ctypes import wintypes

        get_dpi_for_window = ctypes.windll.user32.GetDpiForWindow
        get_dpi_for_window.argtypes = [wintypes.HWND]
        get_dpi_for_window.restype = wintypes.UINT
        dpi = int(get_dpi_for_window(wintypes.HWND(_window_hwnd(window))))
    except Exception:
        dpi = 0
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "tk_patchlevel": str(window.tk.call("info", "patchlevel")),
        "tk_scaling": float(window.tk.call("tk", "scaling")),
        "window_dpi": dpi,
    }


def _root_png_names(output_dir: Path) -> list[str]:
    return sorted(
        child.name
        for child in output_dir.iterdir()
        if child.suffix.lower() == ".png"
    )


def _root_entry_names(output_dir: Path) -> list[str]:
    return sorted(child.name for child in output_dir.iterdir())


def _require_exact_inventory(
    output_dir: Path,
    expected_names: frozenset[str],
    phase: str,
) -> None:
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise RuntimeError(f"evidence root must be a regular directory {phase}: {output_dir}")
    observed = _root_entry_names(output_dir)
    expected = sorted(expected_names)
    if observed != expected:
        raise RuntimeError(
            f"evidence artifact set mismatch {phase}: expected {expected}, got {observed}"
        )
    for name in expected:
        path = output_dir / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"evidence artifact must be a regular file {phase}: {path}")


def _prepare_empty_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise RuntimeError(f"--output-dir must be a regular directory: {output_dir}")
    else:
        output_dir.mkdir(parents=True, exist_ok=False)
    existing = _root_entry_names(output_dir)
    if existing:
        raise RuntimeError(
            "--output-dir must be new or empty; existing entries are never deleted: "
            + ", ".join(existing)
        )


def _run(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve(strict=False)
    _prepare_empty_output_dir(output_dir)
    sealed_revision = _target_revision()
    renderer = _load_renderer(sealed_revision)

    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    callback_errors: list[str] = []
    calls: list[str] = []
    holder = {"model": _model(renderer)}
    interaction_probe: dict[str, Any] = {"enabled": False}
    native_pointer_events: list[dict[str, Any]] = []
    pointer_clock_origin = time.monotonic()
    panel: Any = None

    def callback_error(exc_type, exc, _traceback) -> None:
        callback_errors.append(f"{exc_type.__name__}: {exc}")

    root.report_callback_exception = callback_error

    def toggle_break() -> None:
        calls.append("toggle_break")
        holder["model"] = replace(
            holder["model"],
            break_active=not holder["model"].break_active,
            today_lines=_today_lines(
                renderer,
                break_active=not holder["model"].break_active,
                error=False,
            ),
        )

    def refresh() -> None:
        calls.append("refresh")
        if interaction_probe.get("enabled") is not True:
            return
        interaction_probe["enabled"] = False
        active_window = panel._window
        if active_window is None:
            raise RuntimeError("interaction probe window is unavailable")
        interaction_probe["depth"] = panel._interaction_depth
        delivery_start = len(native_pointer_events)
        interaction_probe["cursor_position"] = _move_pointer(
            active_window,
            inside=False,
        )
        interaction_probe["delivery_elapsed_ms"] = _wait_for_pointer_delivery(
            active_window,
            native_pointer_events,
            expected="leave",
            start_index=delivery_start,
        )
        interaction_probe["delivered"] = True
        interaction_probe["elapsed_ms"] = _pump_events_for(
            active_window,
            SHORT_IDLE_TIMEOUT_MS + SHORT_IDLE_EVENT_PUMP_GRACE_MS,
        )
        interaction_probe["visible"] = panel.is_visible()

    def snooze() -> None:
        calls.append("prompt_snooze")
        holder["model"] = replace(holder["model"], prompt=None)

    panel = renderer.WorktimeQuickPanel(
        root,
        lambda: holder["model"],
        refresh=refresh,
        clock_in_now=lambda: calls.append("clock_in_now"),
        edit_clock_in=lambda: calls.append("edit_clock_in"),
        edit_plan=lambda: calls.append("edit_plan"),
        toggle_break=toggle_break,
        open_settings=lambda: calls.append("open_settings"),
        prompt_accept=lambda value: calls.append(f"prompt_accept:{value}"),
        prompt_edit=lambda value: calls.append(f"prompt_edit:{value}"),
        prompt_snooze=snooze,
        prompt_skip=lambda: calls.append("prompt_skip"),
        idle_timeout_ms=CAPTURE_IDLE_TIMEOUT_MS,
    )

    states: list[dict[str, Any]] = []
    assertions: dict[str, bool] = {}
    first_failure: str | None = None
    focus_order: list[str] = []
    focus_observation: dict[str, Any] = {}
    refresh_observation: dict[str, Any] = {}
    idle_observation: dict[str, Any] = {}
    pointer_delivery_observation: dict[str, Any] = {}
    capture_start_revision = ""
    capture_end_revision = ""
    try:
        capture_start_revision = _require_target_revision(
            sealed_revision,
            "before first renderer show/capture",
        )
        if panel.show(activate=True) is not True or not panel.is_visible():
            raise RuntimeError("Quick Panel did not become mapped for capture")
        window = panel._window
        if window is None:
            raise RuntimeError("Quick Panel window was not created")

        def observe_pointer(sequence: str, event: Any) -> None:
            native_pointer_events.append(
                {
                    "sequence": sequence,
                    "widget": str(getattr(event, "widget", "")),
                    "elapsed_ms": int(
                        (time.monotonic() - pointer_clock_origin) * 1000
                    ),
                }
            )

        window.bind(
            "<Enter>",
            lambda event: observe_pointer("enter", event),
            add="+",
        )
        window.bind(
            "<Leave>",
            lambda event: observe_pointer("leave", event),
            add="+",
        )
        window.minsize(*VIEWPORT)
        window.maxsize(*VIEWPORT)
        window.geometry(f"{VIEWPORT[0]}x{VIEWPORT[1]}+40+40")
        window.update_idletasks()
        window.update()
        if _window_size(window) != list(VIEWPORT):
            raise RuntimeError("could not establish the fixed 800x540 QA viewport")

        base_buttons = ("새로고침", "출근 수정", "휴게 시작", "계획 수정", "설정")
        states.append(
            _capture_state(
                window,
                output_dir,
                "initial",
                required_labels=(
                    "Wrike 기록 5시간 30분 · 현재 기대 5시간",
                    "현재 기준 초과 30분",
                    "오늘",
                    *WEEKDAYS,
                ),
                required_buttons=base_buttons,
            )
        )
        assertions["seven_weekday_rows"] = all(
            states[0]["labels"].count(day) == 1 for day in WEEKDAYS
        )
        assertions["today_badge_visible"] = states[0]["labels"].count("오늘") == 1
        assertions["normal_capture_timeout_is_60_seconds"] = (
            panel._idle_timeout_ms == CAPTURE_IDLE_TIMEOUT_MS
        )

        dispatch_calls = {
            "render_structure": 0,
            "update_rendered_model": 0,
            "reconcile_geometry": 0,
        }
        original_render_structure = panel._render_structure
        original_update_rendered_model = panel._update_rendered_model
        original_reconcile_geometry = panel._reconcile_geometry

        def counted_render_structure(model) -> None:
            dispatch_calls["render_structure"] += 1
            original_render_structure(model)

        def counted_update_rendered_model(model) -> None:
            dispatch_calls["update_rendered_model"] += 1
            original_update_rendered_model(model)

        def counted_reconcile_geometry() -> bool:
            dispatch_calls["reconcile_geometry"] += 1
            return original_reconcile_geometry()

        panel._render_structure = counted_render_structure
        panel._update_rendered_model = counted_update_rendered_model
        panel._reconcile_geometry = counted_reconcile_geometry

        exact_identity_before = _widget_identity(window)
        exact_geometry_before = _window_geometry(window)
        exact_dispatch_before = dict(dispatch_calls)
        exact_previous_model = holder["model"]
        holder["model"] = replace(exact_previous_model)
        exact_distinct_equal = bool(
            holder["model"] is not exact_previous_model
            and holder["model"] == exact_previous_model
        )
        panel.refresh_now()
        window.update_idletasks()
        exact_identity_after = _widget_identity(window)
        exact_geometry_after = _window_geometry(window)
        exact_dispatch_delta = {
            name: dispatch_calls[name] - exact_dispatch_before[name]
            for name in dispatch_calls
        }
        assertions["exact_equal_refresh_preserves_widgets"] = (
            exact_identity_before == exact_identity_after
        )
        assertions["exact_equal_refresh_preserves_geometry"] = (
            exact_geometry_before == exact_geometry_after
        )
        assertions["exact_equal_refresh_uses_distinct_instance"] = (
            exact_distinct_equal
        )
        assertions["exact_equal_refresh_skips_render_structure"] = (
            exact_dispatch_delta["render_structure"] == 0
        )
        assertions["exact_equal_refresh_skips_model_update"] = (
            exact_dispatch_delta["update_rendered_model"] == 0
        )
        assertions["exact_equal_refresh_skips_reconcile"] = (
            exact_dispatch_delta["reconcile_geometry"] == 0
        )

        same_structure_identity_before = _widget_identity(window)
        same_structure_geometry_before = _window_geometry(window)
        same_structure_signature_before = panel._structure_signature
        same_structure_dispatch_before = dict(dispatch_calls)
        holder["model"] = _model(renderer, vacation_provisional=True)
        panel.refresh_now()
        window.update_idletasks()
        same_structure_identity_after = _widget_identity(window)
        same_structure_geometry_after = _window_geometry(window)
        same_structure_signature_after = panel._structure_signature
        same_structure_dispatch_delta = {
            name: dispatch_calls[name] - same_structure_dispatch_before[name]
            for name in dispatch_calls
        }
        assertions["same_structure_refresh_preserves_widgets"] = (
            same_structure_identity_before == same_structure_identity_after
        )
        assertions["same_structure_refresh_preserves_geometry"] = (
            same_structure_geometry_before == same_structure_geometry_after
        )
        assertions["same_structure_refresh_skips_render_structure"] = (
            same_structure_dispatch_delta["render_structure"] == 0
        )
        assertions["same_structure_refresh_updates_model_once"] = (
            same_structure_dispatch_delta["update_rendered_model"] == 1
        )
        assertions["same_structure_refresh_skips_reconcile"] = (
            same_structure_dispatch_delta["reconcile_geometry"] == 0
        )
        provisional_expected_label = (
            "Wrike 기록 5시간 30분 · 현재 기대 5시간 (임시)"
        )
        provisional_vacation_label = (
            "휴가 미확정 (loading) · 휴가 미반영 임시 목표 8시간 (임시)"
        )
        states.append(
            _capture_state(
                window,
                output_dir,
                "vacation-provisional",
                required_labels=(
                    provisional_expected_label,
                    "현재 기준 초과 30분 (임시)",
                    provisional_vacation_label,
                ),
                required_buttons=base_buttons,
            )
        )
        assertions["provisional_vacation_wording_visible"] = bool(
            provisional_expected_label in states[-1]["labels"]
            and provisional_vacation_label in states[-1]["labels"]
        )
        refresh_observation = {
            "normal_capture_timeout_ms": CAPTURE_IDLE_TIMEOUT_MS,
            "exact_equal": {
                "provider_returned_distinct_equal_instance": exact_distinct_equal,
                "widget_identity_before": list(exact_identity_before),
                "widget_identity_after": list(exact_identity_after),
                "geometry_before": exact_geometry_before,
                "geometry_after": exact_geometry_after,
                "method_calls": dict(exact_dispatch_delta),
            },
            "same_structure": {
                "signature_before": list(same_structure_signature_before or ()),
                "signature_after": list(same_structure_signature_after or ()),
                "widget_identity_before": list(same_structure_identity_before),
                "widget_identity_after": list(same_structure_identity_after),
                "geometry_before": same_structure_geometry_before,
                "geometry_after": same_structure_geometry_after,
                "method_calls": dict(same_structure_dispatch_delta),
            },
        }

        _click_button(window, "휴게 시작")
        assertions["break_callback_invoked"] = calls.count("toggle_break") == 1
        assertions["break_button_transition"] = bool(_button(window, "휴게 종료"))
        states.append(
            _capture_state(
                window,
                output_dir,
                "break-active",
                required_labels=("병합 휴게 1시간 · 진행 중",),
                required_buttons=("휴게 종료",),
            )
        )

        holder["model"] = replace(
            holder["model"],
            prompt=renderer.WorktimeActivityPrompt("08:05"),
        )
        panel.refresh_now()
        window.update()
        prompt_button = _button(window, "08:05으로 출근")
        prompt_button.focus_force()
        window.update()
        focus_order = _focus_order(prompt_button)
        assertions["keyboard_focus_visible"] = window.focus_get() is prompt_button
        assertions["prompt_actions_in_focus_order"] = all(
            text in focus_order
            for text in ("08:05으로 출근", "시간 수정", "30분 후", "오늘 건너뛰기")
        )

        window.update_idletasks()
        window.update()
        geometry_before = _window_geometry(window)

        sentinel = tk.Toplevel(root)
        sentinel.title("Wrike native QA focus sentinel")
        sentinel.geometry("160x60+900+40")
        sentinel_entry = tk.Entry(sentinel)
        sentinel_entry.pack(fill="both", expand=True)
        sentinel.update_idletasks()
        sentinel.update()
        sentinel.lift()
        sentinel_hwnd = _window_hwnd(sentinel)

        focus_deadline = time.monotonic() + 2.0
        while True:
            _request_foreground_hwnd(sentinel_hwnd)
            sentinel_entry.focus_force()
            sentinel.update_idletasks()
            sentinel.update()
            focus_before = root.focus_get()
            foreground_before = _foreground_hwnd()
            if focus_before is sentinel_entry and foreground_before > 0:
                break
            if time.monotonic() >= focus_deadline:
                break
            time.sleep(0.02)

        repeated_observations: list[dict[str, Any]] = []
        for attempt in range(1, NONACTIVATING_SHOW_REPETITIONS + 1):
            if panel.show(activate=False) is not True or not panel.is_visible():
                raise RuntimeError(
                    f"nonactivating show did not remain mapped on attempt {attempt}"
                )
            window.update_idletasks()
            window.update()
            repeated_observations.append(
                {
                    "attempt": attempt,
                    "foreground_hwnd": _foreground_hwnd(),
                    "tk_focus_is_sentinel": root.focus_get() is sentinel_entry,
                    "window_geometry": _window_geometry(window),
                    "window_size": _window_size(window),
                }
            )
        tk_focus_preserved = bool(
            focus_before is sentinel_entry
            and all(item["tk_focus_is_sentinel"] for item in repeated_observations)
        )
        foreground_preserved = bool(
            foreground_before > 0
            and foreground_before == sentinel_hwnd
            and all(
                item["foreground_hwnd"] == foreground_before
                for item in repeated_observations
            )
        )
        geometry_preserved = bool(
            [geometry_before["width"], geometry_before["height"]]
            == list(VIEWPORT)
            and all(
                item["window_geometry"] == geometry_before
                for item in repeated_observations
            )
        )
        assertions["nonactivating_show_preserves_focus"] = tk_focus_preserved
        assertions["nonactivating_show_preserves_foreground_hwnd"] = (
            foreground_preserved
        )
        assertions["nonactivating_show_preserves_geometry"] = geometry_preserved
        assertions["repeated_nonactivating_show_preserves_contract"] = bool(
            tk_focus_preserved and foreground_preserved and geometry_preserved
        )
        focus_observation = {
            "scope": FOCUS_SCOPE,
            "cross_process_focus_excluded": True,
            "repetitions": NONACTIVATING_SHOW_REPETITIONS,
            "sentinel_hwnd": sentinel_hwnd,
            "foreground_hwnd_before": foreground_before,
            "tk_focus_before_is_sentinel": focus_before is sentinel_entry,
            "window_geometry_before": geometry_before,
            "window_size_before": [
                geometry_before["width"],
                geometry_before["height"],
            ],
            "observations": repeated_observations,
        }
        sentinel.destroy()
        prompt_button.focus_force()
        window.update()

        states.append(
            _capture_state(
                window,
                output_dir,
                "activity-prompt-focus",
                required_labels=("08:05 활동을 출근으로 반영할까요?",),
                required_buttons=(
                    "08:05으로 출근",
                    "시간 수정",
                    "30분 후",
                    "오늘 건너뛰기",
                ),
            )
        )
        assertions["prompt_focus_checkpoint"] = (
            states[-1]["focus_text"] == "08:05으로 출근"
        )

        _click_button(window, "30분 후")
        assertions["snooze_callback_invoked"] = calls.count("prompt_snooze") == 1
        assertions["prompt_hidden_after_snooze"] = all(
            button not in {"08:05으로 출근", "시간 수정", "30분 후", "오늘 건너뛰기"}
            for button in [str(item.cget("text")) for item in _live_buttons(window)]
        )

        holder["model"] = _model(renderer, error=True)
        panel.refresh_now()
        window.update()
        states.append(
            _capture_state(
                window,
                output_dir,
                "error-last-good",
                required_labels=(
                    "동기화 error · 마지막 성공값 유지 · request_failed",
                    "Wrike 기록 5시간 30분 · 현재 기대 5시간",
                ),
                required_buttons=base_buttons,
            )
        )
        assertions["error_state_visible"] = bool(
            "동기화 error · 마지막 성공값 유지 · request_failed"
            in states[-1]["labels"]
        )

        idle_window_identity = _widget_identity(window)
        idle_window_handle = _window_hwnd(window)
        panel.set_idle_timeout_ms(SHORT_IDLE_TIMEOUT_MS)
        idle_delivery_start = len(native_pointer_events)
        idle_cursor_position = _move_pointer(window, inside=False)
        idle_delivery_elapsed_ms = _wait_for_pointer_delivery(
            window,
            native_pointer_events,
            expected="leave",
            start_index=idle_delivery_start,
        )
        idle_pointer_outside = panel._pointer_inside is False
        idle_elapsed_ms = _pump_events_for(
            window,
            SHORT_IDLE_TIMEOUT_MS + SHORT_IDLE_EVENT_PUMP_GRACE_MS,
        )
        idle_withdrawn = not panel.is_visible()
        window_exists_after_idle = bool(window.winfo_exists())
        first_reopen_visible = bool(
            panel.show(activate=False) is True and panel.is_visible()
        )

        hover_delivery_start = len(native_pointer_events)
        hover_cursor_position = _move_pointer(window, inside=True)
        hover_delivery_elapsed_ms = _wait_for_pointer_delivery(
            window,
            native_pointer_events,
            expected="enter",
            start_index=hover_delivery_start,
        )
        hover_pointer_inside = panel._pointer_inside is True
        hover_elapsed_ms = _pump_events_for(
            window,
            SHORT_IDLE_TIMEOUT_MS + SHORT_IDLE_EVENT_PUMP_GRACE_MS,
        )
        hover_visible = panel.is_visible()

        interaction_probe["enabled"] = True
        _click_button(window, "새로고침")
        interaction_elapsed_ms = int(interaction_probe.get("elapsed_ms", -1))
        interaction_visible = interaction_probe.get("visible") is True
        interaction_depth = interaction_probe.get("depth")
        interaction_delivery_elapsed_ms = int(
            interaction_probe.get("delivery_elapsed_ms", -1)
        )
        interaction_native_leave = interaction_probe.get("delivered") is True

        rearmed_enter_start = len(native_pointer_events)
        rearmed_enter_cursor_position = _move_pointer(window, inside=True)
        rearmed_enter_delivery_elapsed_ms = _wait_for_pointer_delivery(
            window,
            native_pointer_events,
            expected="enter",
            start_index=rearmed_enter_start,
        )
        rearmed_leave_start = len(native_pointer_events)
        rearmed_cursor_position = _move_pointer(window, inside=False)
        rearmed_leave_delivery_elapsed_ms = _wait_for_pointer_delivery(
            window,
            native_pointer_events,
            expected="leave",
            start_index=rearmed_leave_start,
        )
        rearmed_pointer_outside = panel._pointer_inside is False
        rearmed_idle_elapsed_ms = _pump_events_for(
            window,
            SHORT_IDLE_TIMEOUT_MS + SHORT_IDLE_EVENT_PUMP_GRACE_MS,
        )
        rearmed_idle_withdrawn = not panel.is_visible()
        reopened_after_interaction = bool(
            panel.show(activate=False) is True and panel.is_visible()
        )
        panel.set_idle_timeout_ms(CAPTURE_IDLE_TIMEOUT_MS)
        window.update_idletasks()
        final_window_handle = _window_hwnd(window)
        final_window_identity = _widget_identity(window)
        same_window_reused = bool(
            panel._window is window
            and idle_window_handle == final_window_handle
            and idle_window_identity == final_window_identity
        )
        normal_timeout_restored = (
            panel._idle_timeout_ms == CAPTURE_IDLE_TIMEOUT_MS
        )
        assertions["native_pointer_leave_delivered"] = bool(
            idle_delivery_elapsed_ms <= POINTER_DELIVERY_TIMEOUT_MS
        )
        assertions["native_pointer_enter_delivered"] = bool(
            hover_delivery_elapsed_ms <= POINTER_DELIVERY_TIMEOUT_MS
        )
        assertions["interaction_native_leave_delivered"] = bool(
            interaction_native_leave
            and interaction_delivery_elapsed_ms <= POINTER_DELIVERY_TIMEOUT_MS
        )
        assertions["rearmed_native_leave_delivered"] = bool(
            rearmed_enter_delivery_elapsed_ms <= POINTER_DELIVERY_TIMEOUT_MS
            and rearmed_leave_delivery_elapsed_ms <= POINTER_DELIVERY_TIMEOUT_MS
        )
        assertions["short_idle_timeout_withdraws"] = bool(
            idle_pointer_outside
            and idle_elapsed_ms >= SHORT_IDLE_TIMEOUT_MS
            and idle_withdrawn
        )
        assertions["idle_withdraw_preserves_window"] = bool(
            window_exists_after_idle and first_reopen_visible
        )
        assertions["hover_defers_idle_withdraw"] = bool(
            hover_pointer_inside
            and hover_elapsed_ms >= SHORT_IDLE_TIMEOUT_MS
            and hover_visible
        )
        assertions["interaction_defers_idle_withdraw"] = bool(
            interaction_depth == 1
            and interaction_elapsed_ms >= SHORT_IDLE_TIMEOUT_MS
            and interaction_visible
        )
        assertions["rearmed_idle_withdraws"] = bool(
            rearmed_pointer_outside
            and rearmed_idle_elapsed_ms >= SHORT_IDLE_TIMEOUT_MS
            and rearmed_idle_withdrawn
        )
        assertions["reopen_reuses_window"] = bool(
            reopened_after_interaction and same_window_reused
        )
        assertions["normal_idle_timeout_restored"] = bool(
            normal_timeout_restored and panel.is_visible()
        )
        idle_observation = {
            "normal_timeout_ms": CAPTURE_IDLE_TIMEOUT_MS,
            "short_timeout_ms": SHORT_IDLE_TIMEOUT_MS,
            "window_handle_before": idle_window_handle,
            "widget_identity_before": list(idle_window_identity),
            "idle_cursor_position": idle_cursor_position,
            "idle_pointer_outside": idle_pointer_outside,
            "idle_elapsed_ms": idle_elapsed_ms,
            "idle_withdrawn": idle_withdrawn,
            "window_exists_after_idle": window_exists_after_idle,
            "first_reopen_visible": first_reopen_visible,
            "hover_cursor_position": hover_cursor_position,
            "hover_pointer_inside": hover_pointer_inside,
            "hover_elapsed_ms": hover_elapsed_ms,
            "hover_visible": hover_visible,
            "interaction_depth": interaction_depth,
            "interaction_elapsed_ms": interaction_elapsed_ms,
            "interaction_visible": interaction_visible,
            "rearmed_cursor_position": rearmed_cursor_position,
            "rearmed_pointer_outside": rearmed_pointer_outside,
            "rearmed_idle_elapsed_ms": rearmed_idle_elapsed_ms,
            "rearmed_idle_withdrawn": rearmed_idle_withdrawn,
            "reopened_after_interaction": reopened_after_interaction,
            "window_handle_after": final_window_handle,
            "widget_identity_after": list(final_window_identity),
            "same_window_reused": same_window_reused,
            "normal_timeout_restored": normal_timeout_restored,
            "final_visible": panel.is_visible(),
        }
        pointer_delivery_observation = {
            "cursor_backend": "Win32 SetCursorPos",
            "binding": "additive Tk <Enter>/<Leave>",
            "delivery_timeout_ms": POINTER_DELIVERY_TIMEOUT_MS,
            "transitions": [
                {
                    "phase": "idle",
                    "expected": "leave",
                    "cursor_position": idle_cursor_position,
                    "delivery_elapsed_ms": idle_delivery_elapsed_ms,
                },
                {
                    "phase": "hover",
                    "expected": "enter",
                    "cursor_position": hover_cursor_position,
                    "delivery_elapsed_ms": hover_delivery_elapsed_ms,
                },
                {
                    "phase": "interaction",
                    "expected": "leave",
                    "cursor_position": interaction_probe.get("cursor_position"),
                    "delivery_elapsed_ms": interaction_delivery_elapsed_ms,
                },
                {
                    "phase": "rearmed-enter",
                    "expected": "enter",
                    "cursor_position": rearmed_enter_cursor_position,
                    "delivery_elapsed_ms": rearmed_enter_delivery_elapsed_ms,
                },
                {
                    "phase": "rearmed-leave",
                    "expected": "leave",
                    "cursor_position": rearmed_cursor_position,
                    "delivery_elapsed_ms": rearmed_leave_delivery_elapsed_ms,
                },
            ],
            "events": [dict(item) for item in native_pointer_events],
        }

        capture_end_revision = _require_target_revision(
            sealed_revision,
            "after final native scenario",
        )
        assertions["capture_revision_stable"] = bool(
            capture_start_revision
            and capture_start_revision == sealed_revision
            and capture_start_revision == capture_end_revision
        )
        assertions["checkpoint_png_set_exact"] = bool(
            _root_png_names(output_dir)
            == sorted(CHECKPOINT_FILENAMES.values())
        )
        assertions["client_only_capture_provenance"] = all(
            state["capture_provenance"]["backend"] == CAPTURE_BACKEND
            and state["capture_provenance"]["client_only"] is True
            and state["capture_provenance"]["unrelated_desktop_pixels_read"] is False
            for state in states
        )
        assertions["all_states_unclipped"] = all(state["ok"] for state in states)
        assertions["no_runtime_errors"] = not callback_errors
        for name, passed in assertions.items():
            if not passed:
                first_failure = name
                break
        result = {
            "schema_version": 3,
            "runner_version": RUNNER_VERSION,
            "ok": bool(
                set(assertions) == REQUIRED_ASSERTIONS
                and all(assertions.values())
                and all(state["ok"] for state in states)
            ),
            "output_root": str(output_dir),
            "capture_start_revision": capture_start_revision,
            "capture_end_revision": capture_end_revision,
            "target_revision": capture_start_revision,
            "capture_provenance": dict(CAPTURE_PROVENANCE),
            "scope": {
                "claims": list(SCOPE_CLAIMS),
                "focus": FOCUS_SCOPE,
                "exclusions": [dict(item) for item in SCOPE_EXCLUSIONS],
            },
            "environment": _dpi_snapshot(window),
            "viewport": list(VIEWPORT),
            "states": states,
            "assertions": assertions,
            "refresh_observation": refresh_observation,
            "idle_observation": idle_observation,
            "pointer_delivery_observation": pointer_delivery_observation,
            "focus_order": focus_order,
            "nonactivating_focus_observation": focus_observation,
            "callbacks": calls,
            "runtime_errors": callback_errors,
            "first_failure": first_failure,
            "fixture_contains_real_identity": False,
            "attempts": 1,
        }
    finally:
        panel.destroy()
        try:
            root.destroy()
        except Exception:
            pass

    run_path = output_dir / "run.json"
    run_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_manifest(output_dir, result, review=None)
    _require_exact_inventory(
        output_dir,
        CAPTURE_COMPLETE_FILENAMES,
        "after capture",
    )
    return result


def _requirements() -> list[dict[str, Any]]:
    return [
        {
            "id": "REQ-SYNTHETIC-RENDERER",
            "claim": (
                "Synthetic fixture values for recorded work, current expectation, "
                "and variance render together."
            ),
            "required": True,
        },
        {
            "id": "REQ-SYNTHETIC-LAYOUT",
            "claim": (
                "The synthetic renderer keeps all seven weekday rows, required "
                "controls, and decoded PNG checkpoints within 800x540."
            ),
            "required": True,
        },
        {
            "id": "REQ-SYNTHETIC-REFRESH-STABILITY",
            "claim": (
                "Exact-equal refresh dispatches no render, update, or geometry "
                "method, while a same-structure text change dispatches exactly "
                "one in-place update without reconciliation."
            ),
            "required": True,
        },
        {
            "id": "REQ-SYNTHETIC-PROVISIONAL-VACATION",
            "claim": (
                "The synthetic provisional fixture visibly marks current expectation "
                "and the vacation-unconfirmed temporary target as provisional."
            ),
            "required": True,
        },
        {
            "id": "REQ-SYNTHETIC-CALLBACKS",
            "claim": (
                "Synthetic Tk mouse callbacks update the rendered break and prompt states."
            ),
            "required": True,
        },
        {
            "id": "REQ-SYNTHETIC-IDLE-LIFECYCLE",
            "claim": (
                "A short synthetic timeout withdraws without destruction; Win32 "
                "pointer moves reach additive Tk enter/leave bindings; hover and "
                "interaction defer dismissal; reopen reuses the same window."
            ),
            "required": True,
        },
        {
            "id": "REQ-SAME-PROCESS-FOCUS",
            "claim": (
                "Repeated nonactivating show calls preserve the OS foreground HWND "
                "and same-process synthetic Tk sentinel focus under the fixed "
                "800x540 QA viewport."
            ),
            "required": True,
        },
        {
            "id": "REQ-SYNTHETIC-ERROR",
            "claim": (
                "The synthetic renderer shows its explicit error fixture with retained data."
            ),
            "required": True,
        },
        {
            "id": "REQ-RENDERER-SCOPE",
            "claim": (
                "Evidence is limited to the synthetic renderer, layout, lifecycle, "
                "callbacks, and same-process focus; it does not claim production integration."
            ),
            "required": True,
        },
    ]


def _write_manifest(
    output_dir: Path,
    run: dict[str, Any],
    *,
    review: dict[str, Any] | None,
) -> Path:
    run_path = output_dir / "run.json"
    if run_path.is_symlink() or not run_path.is_file():
        raise RuntimeError(f"run evidence must be a regular file: {run_path}")
    run_digest = _sha256(run_path)
    finalized = review is not None
    expected_inventory = sorted(
        FINALIZED_FILENAMES if finalized else CAPTURE_COMPLETE_FILENAMES
    )
    review_binding = None
    if review is not None:
        review_binding = {
            "path": REVIEW_RECEIPT_FILENAME,
            "sha256": review["sha256"],
            "schema_version": review["schema_version"],
            "declared_review_provenance": dict(
                review["declared_review_provenance"]
            ),
        }
    review_by_state = {
        item["state"]: item
        for item in (review or {}).get("checkpoints", [])
    }
    checkpoints = []
    for state in run.get("states", []):
        receipt_checkpoint = review_by_state.get(state["state"])
        checkpoints.append(
            {
                "state": state["state"],
                "path": state["screenshot"],
                "sha256": state["sha256"],
                "reviewed": bool(
                    receipt_checkpoint is not None
                    and receipt_checkpoint["reviewed"] is True
                ),
                "sensitivity": (
                    receipt_checkpoint["sensitivity"]
                    if receipt_checkpoint is not None
                    else "restricted-local"
                ),
            }
        )
    reviewed = bool(review is not None and review.get("reviewed") is True)
    sensitive_reviewed = bool(
        review is not None and review.get("sensitive_reviewed") is True
    )
    passed = bool(run.get("ok") is True and reviewed and sensitive_reviewed)
    first_failure = run.get("first_failure")
    if not passed and not first_failure:
        first_failure = "external review receipt required"
    requirements = _requirements()
    manifest = {
        "schema_version": 1,
        "target": {
            "name": "windows-supporter Wrike Quick Panel synthetic renderer only",
            "revision": run.get("target_revision", "missing"),
            "surface": "desktop",
            "environment": (
                "local Windows synthetic Tk fixture; production integrations excluded"
            ),
            "workspace_root": str(REPO_ROOT),
        },
        "runner": {
            "name": "qa_wrike_worktime_panel_native.py",
            "version": RUNNER_VERSION,
            "command": (
                "uv run python scripts/qa_wrike_worktime_panel_native.py "
                f"--output-dir \"{output_dir}\""
            ),
        },
        "evidence_bindings": {
            "run_json": {
                "path": "run.json",
                "sha256": run_digest,
                "schema_version": run.get("schema_version"),
            },
            "review_receipt": review_binding,
            "exact_inventory": expected_inventory,
        },
        "requirements": requirements,
        "matrix": [
            {
                "id": "windows-native-tk-synthetic-800x540",
                "source": "repository synthetic renderer-only harness",
                "required": True,
                "browser": "Tk native desktop synthetic fixture",
                "viewport": {"width": VIEWPORT[0], "height": VIEWPORT[1]},
            }
        ],
        "scenarios": [
            {
                "id": "synthetic-wrike-quick-panel-renderer",
                "requirement_ids": [item["id"] for item in requirements],
                "required": True,
                "steps": [
                    "Open the synthetic Quick Panel renderer at 800x540 with a 60-second capture timeout",
                    "Inspect synthetic actual-versus-expected text and seven week rows",
                    "Verify a distinct exact-equal model dispatches zero render, update, or geometry methods",
                    "Apply a same-structure provisional-vacation text change through exactly one in-place update without geometry reconciliation",
                    "Inspect provisional current-expectation, vacation-unconfirmed, and temporary-target wording",
                    "Activate the synthetic break callback through a Tk mouse event",
                    "Exercise repeated nonactivating show calls with a same-process sentinel",
                    "Traverse synthetic prompt actions by keyboard focus",
                    "Snooze the synthetic prompt through a Tk mouse event",
                    "Render the explicit synthetic error fixture with retained data",
                    "Use Win32 SetCursorPos and additive Tk enter/leave observers with a bounded delivery wait",
                    "Use a 1.2-second timeout to verify idle withdraw without window destruction",
                    "Verify native hover delivery and an active callback defer dismissal, then reopen the same window",
                    "Keep production snapshot, cache, vacation fetch/calculation, state, tray, hotkey, packaged EXE, and cross-process focus outside this evidence scope",
                ],
                "executions": [
                    {
                        "matrix_id": "windows-native-tk-synthetic-800x540",
                        "status": "passed" if passed else "failed",
                        "attempts": int(run.get("attempts", 1)),
                        "assertions": [
                            name
                            for name, value in run.get("assertions", {}).items()
                            if value is True
                        ],
                        "unexpected_errors": list(run.get("runtime_errors", [])),
                        "visual_states": [
                            state["state"] for state in run.get("states", [])
                        ],
                        "checkpoints": checkpoints,
                        "first_failure": None if passed else first_failure,
                        "flake_classification": None,
                    }
                ],
            }
        ],
        "artifact_policy": {
            "root": str(output_dir),
            "kind": "external_temp",
            "preexisting_ignored_output": False,
            "repository_ignore_mutated": False,
            "sensitive_reviewed": sensitive_reviewed,
            "shared": False,
            "retention": "kept-for-handoff",
        },
        "result": {
            "status": "passed" if passed else "failed",
            "known_gaps": [],
        },
    }
    name = "manifest.json" if review is not None else "manifest.pending.json"
    path = output_dir / name
    if path.is_symlink():
        raise RuntimeError(f"manifest path must not be a symlink: {path}")
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _validate_manifest_evidence(path, run, review)
    return path


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular file: {path}")
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError(f"{label} is unexpectedly large: {path}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain a JSON object: {path}")
    return value


def _validate_manifest_evidence(
    path: Path,
    run: dict[str, Any],
    review: dict[str, Any] | None,
) -> None:
    manifest = _load_json_object(path, "manifest evidence")
    reviewed = bool(review is not None and review.get("reviewed") is True)
    sensitive_reviewed = bool(
        review is not None and review.get("sensitive_reviewed") is True
    )
    passed = bool(run.get("ok") is True and reviewed and sensitive_reviewed)
    expected_name = "manifest.json" if review is not None else "manifest.pending.json"
    if path.name != expected_name or manifest.get("schema_version") != 1:
        raise RuntimeError("manifest filename or schema is invalid")
    target = manifest.get("target")
    runner = manifest.get("runner")
    if (
        not isinstance(target, dict)
        or target.get("revision") != run.get("target_revision")
        or not isinstance(runner, dict)
        or runner.get("name") != "qa_wrike_worktime_panel_native.py"
        or runner.get("version") != RUNNER_VERSION
    ):
        raise RuntimeError("manifest target or runner binding is invalid")
    evidence_bindings = manifest.get("evidence_bindings")
    expected_inventory = sorted(
        FINALIZED_FILENAMES if review is not None else CAPTURE_COMPLETE_FILENAMES
    )
    expected_review_binding = None
    if review is not None:
        expected_review_binding = {
            "path": REVIEW_RECEIPT_FILENAME,
            "sha256": review["sha256"],
            "schema_version": review["schema_version"],
            "declared_review_provenance": dict(
                review["declared_review_provenance"]
            ),
        }
    expected_bindings = {
        "run_json": {
            "path": "run.json",
            "sha256": _sha256(path.parent / "run.json"),
            "schema_version": run.get("schema_version"),
        },
        "review_receipt": expected_review_binding,
        "exact_inventory": expected_inventory,
    }
    if evidence_bindings != expected_bindings:
        raise RuntimeError("manifest run/receipt digest bindings are invalid")
    requirements = _requirements()
    if manifest.get("requirements") != requirements:
        raise RuntimeError("manifest requirements are incomplete or stale")
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 1:
        raise RuntimeError("manifest scenario set is invalid")
    scenario = scenarios[0]
    expected_requirement_ids = [item["id"] for item in requirements]
    if (
        not isinstance(scenario, dict)
        or scenario.get("requirement_ids") != expected_requirement_ids
        or scenario.get("required") is not True
    ):
        raise RuntimeError("manifest requirement traceability is invalid")
    executions = scenario.get("executions")
    if not isinstance(executions, list) or len(executions) != 1:
        raise RuntimeError("manifest execution set is invalid")
    execution = executions[0]
    review_by_state = {
        item["state"]: item
        for item in (review or {}).get("checkpoints", [])
        if isinstance(item, dict) and isinstance(item.get("state"), str)
    }
    expected_checkpoints = []
    for state in run.get("states", []):
        receipt_checkpoint = review_by_state.get(state["state"])
        expected_checkpoints.append(
            {
                "state": state["state"],
                "path": state["screenshot"],
                "sha256": state["sha256"],
                "reviewed": bool(
                    receipt_checkpoint is not None
                    and receipt_checkpoint.get("reviewed") is True
                ),
                "sensitivity": (
                    receipt_checkpoint["sensitivity"]
                    if receipt_checkpoint is not None
                    else "restricted-local"
                ),
            }
        )
    expected_states = list(CHECKPOINT_FILENAMES)
    if (
        not isinstance(execution, dict)
        or execution.get("status") != ("passed" if passed else "failed")
        or execution.get("visual_states") != expected_states
        or execution.get("checkpoints") != expected_checkpoints
        or execution.get("unexpected_errors") != run.get("runtime_errors", [])
    ):
        raise RuntimeError("manifest execution/checkpoint evidence is inconsistent")
    artifact_policy = manifest.get("artifact_policy")
    result = manifest.get("result")
    if (
        not isinstance(artifact_policy, dict)
        or artifact_policy.get("root") != run.get("output_root")
        or artifact_policy.get("sensitive_reviewed") is not sensitive_reviewed
        or not isinstance(result, dict)
        or result.get("status") != ("passed" if passed else "failed")
    ):
        raise RuntimeError("manifest review gate or result is inconsistent")


def _validate_run_evidence(
    output_dir: Path,
) -> tuple[dict[str, Any], str]:
    run_path = output_dir / "run.json"
    run = _load_json_object(run_path, "run evidence")
    run_digest = _sha256(run_path)
    if run.get("schema_version") != 3:
        raise RuntimeError("unsupported run evidence schema")
    if run.get("runner_version") != RUNNER_VERSION:
        raise RuntimeError("run evidence runner version is missing or stale")
    if run.get("ok") is not True:
        raise RuntimeError("cannot finalize a failed native UI run")
    if run.get("output_root") != str(output_dir):
        raise RuntimeError("run evidence output root does not match --output-dir")
    if run.get("viewport") != list(VIEWPORT):
        raise RuntimeError("run evidence viewport is not exactly 800x540")

    capture_start = run.get("capture_start_revision")
    capture_end = run.get("capture_end_revision")
    target_revision = run.get("target_revision")
    if not isinstance(target_revision, str) or not target_revision:
        raise RuntimeError("run evidence target revision is missing")
    if capture_start != target_revision or capture_end != target_revision:
        raise RuntimeError("capture start/end revisions are not identical to target revision")
    if run.get("capture_provenance") != CAPTURE_PROVENANCE:
        raise RuntimeError("run evidence capture provenance is not client-only")
    expected_scope = {
        "claims": list(SCOPE_CLAIMS),
        "focus": FOCUS_SCOPE,
        "exclusions": [dict(item) for item in SCOPE_EXCLUSIONS],
    }
    if run.get("scope") != expected_scope:
        raise RuntimeError("run evidence scope is incomplete or unexpected")

    states = run.get("states")
    if not isinstance(states, list):
        raise RuntimeError("run evidence states must be a list")
    expected_states = list(CHECKPOINT_FILENAMES)
    actual_states = [
        state.get("state") if isinstance(state, dict) else None
        for state in states
    ]
    if actual_states != expected_states:
        raise RuntimeError(
            f"checkpoint state set/order mismatch: expected {expected_states}, "
            f"got {actual_states}"
        )
    observed_png_names = _root_png_names(output_dir)
    expected_png_names = sorted(CHECKPOINT_FILENAMES.values())
    if observed_png_names != expected_png_names:
        raise RuntimeError(
            f"output root PNG set mismatch: expected {expected_png_names}, "
            f"got {observed_png_names}"
        )

    for state, expected_state in zip(states, expected_states, strict=True):
        expected_path = output_dir / CHECKPOINT_FILENAMES[expected_state]
        if state.get("screenshot") != str(expected_path):
            raise RuntimeError(
                f"checkpoint path is not exact for {expected_state}: "
                f"{state.get('screenshot')!r}"
            )
        if expected_path.is_symlink() or not expected_path.is_file():
            raise RuntimeError(f"checkpoint must be a regular file: {expected_path}")
        decoded_png = _decode_png(expected_path)
        decoded_dimensions = decoded_png["dimensions"]
        if decoded_dimensions != list(VIEWPORT):
            raise RuntimeError(
                f"checkpoint is not exactly 800x540: {expected_path} "
                f"({decoded_dimensions[0]}x{decoded_dimensions[1]})"
            )
        if state.get("dimensions") != decoded_dimensions:
            raise RuntimeError(f"checkpoint dimensions are stale: {expected_path}")
        if state.get("png_signature") != PNG_SIGNATURE.hex():
            raise RuntimeError(f"checkpoint PNG signature record is invalid: {expected_path}")
        if state.get("png_fully_decoded") is not True:
            raise RuntimeError(f"checkpoint PNG was not fully decoded: {expected_path}")
        if state.get("png_decoded_bytes") != decoded_png["decoded_bytes"]:
            raise RuntimeError(f"checkpoint decoded byte count is stale: {expected_path}")
        expected_digest = state.get("sha256")
        if (
            not _is_sha256(expected_digest)
            or decoded_png["sha256"] != expected_digest
        ):
            raise RuntimeError(f"checkpoint digest mismatch: {expected_path}")
        if state.get("window_size") != list(VIEWPORT):
            raise RuntimeError(f"checkpoint window size is not 800x540: {expected_state}")
        if not _valid_viewport_geometry(state.get("window_geometry")):
            raise RuntimeError(
                f"checkpoint full window geometry is invalid: {expected_state}"
            )
        if state.get("ok") is not True:
            raise RuntimeError(f"checkpoint assertion failed: {expected_state}")
        if expected_state == "vacation-provisional":
            labels = state.get("labels")
            required_provisional_labels = {
                "Wrike 기록 5시간 30분 · 현재 기대 5시간 (임시)",
                "현재 기준 초과 30분 (임시)",
                "휴가 미확정 (loading) · 휴가 미반영 임시 목표 8시간 (임시)",
            }
            if not isinstance(labels, list) or not required_provisional_labels <= set(labels):
                raise RuntimeError("provisional vacation wording evidence is incomplete")
        provenance = state.get("capture_provenance")
        if not isinstance(provenance, dict):
            raise RuntimeError(f"checkpoint capture provenance missing: {expected_state}")
        for key, value in CAPTURE_PROVENANCE.items():
            if provenance.get(key) != value:
                raise RuntimeError(
                    f"checkpoint capture provenance mismatch for {expected_state}: {key}"
                )
        if provenance.get("client_dimensions") != list(VIEWPORT):
            raise RuntimeError(
                f"checkpoint client capture is not 800x540: {expected_state}"
            )
        if not isinstance(provenance.get("window_handle"), int):
            raise RuntimeError(f"checkpoint HWND missing: {expected_state}")

    assertions = run.get("assertions")
    if not isinstance(assertions, dict) or set(assertions) != REQUIRED_ASSERTIONS:
        raise RuntimeError("run evidence assertion set is incomplete or unexpected")
    if any(value is not True for value in assertions.values()):
        raise RuntimeError("run evidence contains a failed assertion")
    if run.get("runtime_errors") != [] or run.get("first_failure") is not None:
        raise RuntimeError("run evidence contains a runtime failure")
    if run.get("fixture_contains_real_identity") is not False:
        raise RuntimeError("synthetic fixture identity declaration is invalid")
    if run.get("attempts") != 1:
        raise RuntimeError("run evidence attempt count is invalid")

    def valid_widget_identity(value: object) -> bool:
        return bool(
            isinstance(value, list)
            and value
            and all(isinstance(item, str) and item for item in value)
            and len(value) == len(set(value))
        )

    refresh = run.get("refresh_observation")
    if not isinstance(refresh, dict) or set(refresh) != {
        "normal_capture_timeout_ms",
        "exact_equal",
        "same_structure",
    }:
        raise RuntimeError("refresh observation is missing or unexpected")
    if refresh.get("normal_capture_timeout_ms") != CAPTURE_IDLE_TIMEOUT_MS:
        raise RuntimeError("normal capture timeout observation is invalid")
    exact_equal = refresh.get("exact_equal")
    if not isinstance(exact_equal, dict) or set(exact_equal) != {
        "provider_returned_distinct_equal_instance",
        "widget_identity_before",
        "widget_identity_after",
        "geometry_before",
        "geometry_after",
        "method_calls",
    }:
        raise RuntimeError("exact-equal refresh observation is incomplete")
    if (
        exact_equal.get("provider_returned_distinct_equal_instance") is not True
        or not valid_widget_identity(exact_equal.get("widget_identity_before"))
        or exact_equal.get("widget_identity_after")
        != exact_equal.get("widget_identity_before")
        or not _valid_viewport_geometry(exact_equal.get("geometry_before"))
        or exact_equal.get("geometry_after") != exact_equal.get("geometry_before")
        or exact_equal.get("method_calls")
        != {
            "render_structure": 0,
            "update_rendered_model": 0,
            "reconcile_geometry": 0,
        }
    ):
        raise RuntimeError("exact-equal refresh dispatched widget or geometry work")
    same_structure = refresh.get("same_structure")
    if not isinstance(same_structure, dict) or set(same_structure) != {
        "signature_before",
        "signature_after",
        "widget_identity_before",
        "widget_identity_after",
        "geometry_before",
        "geometry_after",
        "method_calls",
    }:
        raise RuntimeError("same-structure refresh observation is incomplete")
    if (
        same_structure.get("signature_before") != [False, 6]
        or same_structure.get("signature_after") != [False, 6]
        or not valid_widget_identity(same_structure.get("widget_identity_before"))
        or same_structure.get("widget_identity_after")
        != same_structure.get("widget_identity_before")
        or not _valid_viewport_geometry(same_structure.get("geometry_before"))
        or same_structure.get("geometry_after")
        != same_structure.get("geometry_before")
        or same_structure.get("method_calls")
        != {
            "render_structure": 0,
            "update_rendered_model": 1,
            "reconcile_geometry": 0,
        }
    ):
        raise RuntimeError("same-structure refresh did not use one in-place update")

    idle = run.get("idle_observation")
    expected_idle_keys = {
        "normal_timeout_ms",
        "short_timeout_ms",
        "window_handle_before",
        "widget_identity_before",
        "idle_cursor_position",
        "idle_pointer_outside",
        "idle_elapsed_ms",
        "idle_withdrawn",
        "window_exists_after_idle",
        "first_reopen_visible",
        "hover_cursor_position",
        "hover_pointer_inside",
        "hover_elapsed_ms",
        "hover_visible",
        "interaction_depth",
        "interaction_elapsed_ms",
        "interaction_visible",
        "rearmed_cursor_position",
        "rearmed_pointer_outside",
        "rearmed_idle_elapsed_ms",
        "rearmed_idle_withdrawn",
        "reopened_after_interaction",
        "window_handle_after",
        "widget_identity_after",
        "same_window_reused",
        "normal_timeout_restored",
        "final_visible",
    }
    if not isinstance(idle, dict) or set(idle) != expected_idle_keys:
        raise RuntimeError("idle lifecycle observation is missing or unexpected")
    elapsed_fields = (
        "idle_elapsed_ms",
        "hover_elapsed_ms",
        "interaction_elapsed_ms",
        "rearmed_idle_elapsed_ms",
    )

    def valid_cursor_position(value: object) -> bool:
        return bool(
            isinstance(value, list)
            and len(value) == 2
            and all(type(coordinate) is int for coordinate in value)
        )

    checkpoint_geometry = states[-1]["window_geometry"]

    def cursor_inside_checkpoint(value: list[int]) -> bool:
        return bool(
            checkpoint_geometry["x"] <= value[0]
            < checkpoint_geometry["x"] + checkpoint_geometry["width"]
            and checkpoint_geometry["y"] <= value[1]
            < checkpoint_geometry["y"] + checkpoint_geometry["height"]
        )

    idle_cursor = idle.get("idle_cursor_position")
    hover_cursor = idle.get("hover_cursor_position")
    rearmed_cursor = idle.get("rearmed_cursor_position")
    if (
        idle.get("normal_timeout_ms") != CAPTURE_IDLE_TIMEOUT_MS
        or idle.get("short_timeout_ms") != SHORT_IDLE_TIMEOUT_MS
        or type(idle.get("window_handle_before")) is not int
        or idle["window_handle_before"] <= 0
        or idle.get("window_handle_after") != idle.get("window_handle_before")
        or not valid_widget_identity(idle.get("widget_identity_before"))
        or idle.get("widget_identity_after") != idle.get("widget_identity_before")
        or not valid_cursor_position(idle_cursor)
        or cursor_inside_checkpoint(idle_cursor)
        or not valid_cursor_position(hover_cursor)
        or not cursor_inside_checkpoint(hover_cursor)
        or not valid_cursor_position(rearmed_cursor)
        or cursor_inside_checkpoint(rearmed_cursor)
        or any(
            type(idle.get(field)) is not int
            or idle[field] < SHORT_IDLE_TIMEOUT_MS
            for field in elapsed_fields
        )
        or idle.get("interaction_depth") != 1
        or any(
            idle.get(field) is not True
            for field in (
                "idle_pointer_outside",
                "idle_withdrawn",
                "window_exists_after_idle",
                "first_reopen_visible",
                "hover_pointer_inside",
                "hover_visible",
                "interaction_visible",
                "rearmed_pointer_outside",
                "rearmed_idle_withdrawn",
                "reopened_after_interaction",
                "same_window_reused",
                "normal_timeout_restored",
                "final_visible",
            )
        )
    ):
        raise RuntimeError("idle dismiss/defer/reopen evidence is inconsistent")

    pointer = run.get("pointer_delivery_observation")
    if not isinstance(pointer, dict) or set(pointer) != {
        "cursor_backend",
        "binding",
        "delivery_timeout_ms",
        "transitions",
        "events",
    }:
        raise RuntimeError("native pointer delivery observation is missing")
    if (
        pointer.get("cursor_backend") != "Win32 SetCursorPos"
        or pointer.get("binding") != "additive Tk <Enter>/<Leave>"
        or pointer.get("delivery_timeout_ms") != POINTER_DELIVERY_TIMEOUT_MS
    ):
        raise RuntimeError("native pointer delivery provenance is invalid")
    transitions = pointer.get("transitions")
    expected_transitions = [
        ("idle", "leave"),
        ("hover", "enter"),
        ("interaction", "leave"),
        ("rearmed-enter", "enter"),
        ("rearmed-leave", "leave"),
    ]
    if not isinstance(transitions, list) or len(transitions) != len(
        expected_transitions
    ):
        raise RuntimeError("native pointer transition set is incomplete")
    for transition, (phase, expected) in zip(
        transitions,
        expected_transitions,
        strict=True,
    ):
        if not isinstance(transition, dict) or set(transition) != {
            "phase",
            "expected",
            "cursor_position",
            "delivery_elapsed_ms",
        }:
            raise RuntimeError("native pointer transition shape is invalid")
        cursor = transition.get("cursor_position")
        elapsed = transition.get("delivery_elapsed_ms")
        if (
            transition.get("phase") != phase
            or transition.get("expected") != expected
            or not valid_cursor_position(cursor)
            or cursor_inside_checkpoint(cursor) is (expected == "leave")
            or type(elapsed) is not int
            or not 0 <= elapsed <= POINTER_DELIVERY_TIMEOUT_MS
        ):
            raise RuntimeError(f"native pointer transition is invalid: {phase}")
    pointer_events = pointer.get("events")
    if not isinstance(pointer_events, list) or not pointer_events:
        raise RuntimeError("native Tk pointer events are missing")
    for event in pointer_events:
        if (
            not isinstance(event, dict)
            or set(event) != {"sequence", "widget", "elapsed_ms"}
            or event.get("sequence") not in {"enter", "leave"}
            or not isinstance(event.get("widget"), str)
            or not event["widget"]
            or type(event.get("elapsed_ms")) is not int
            or event["elapsed_ms"] < 0
        ):
            raise RuntimeError("native Tk pointer event is invalid")
    sequences = [event["sequence"] for event in pointer_events]
    if sequences.count("enter") < 2 or sequences.count("leave") < 3:
        raise RuntimeError("native Tk pointer binding deliveries are incomplete")

    if run.get("callbacks") != ["toggle_break", "prompt_snooze", "refresh"]:
        raise RuntimeError("synthetic callback evidence is incomplete")
    focus_order = run.get("focus_order")
    if not isinstance(focus_order, list) or not all(
        action in focus_order
        for action in ("08:05으로 출근", "시간 수정", "30분 후", "오늘 건너뛰기")
    ):
        raise RuntimeError("synthetic prompt focus evidence is incomplete")
    focus = run.get("nonactivating_focus_observation")
    if not isinstance(focus, dict):
        raise RuntimeError("nonactivating focus observation is missing")
    if (
        focus.get("scope") != FOCUS_SCOPE
        or focus.get("cross_process_focus_excluded") is not True
        or focus.get("repetitions") != NONACTIVATING_SHOW_REPETITIONS
        or type(focus.get("foreground_hwnd_before")) is not int
        or focus["foreground_hwnd_before"] <= 0
        or not _valid_viewport_geometry(focus.get("window_geometry_before"))
        or focus.get("window_size_before") != list(VIEWPORT)
        or focus.get("tk_focus_before_is_sentinel") is not True
    ):
        raise RuntimeError("nonactivating focus scope or precondition is invalid")
    observations = focus.get("observations")
    if not isinstance(observations, list) or len(observations) != NONACTIVATING_SHOW_REPETITIONS:
        raise RuntimeError("nonactivating focus repetitions are incomplete")
    if any(
        not isinstance(item, dict)
        or item.get("foreground_hwnd") != focus.get("foreground_hwnd_before")
        or item.get("tk_focus_is_sentinel") is not True
        or item.get("window_geometry") != focus.get("window_geometry_before")
        or item.get("window_size") != list(VIEWPORT)
        for item in observations
    ):
        raise RuntimeError("nonactivating focus/geometry evidence is inconsistent")
    return run, run_digest


def _review_receipt_path(output_dir: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.abspath(candidate))
    expected = output_dir / REVIEW_RECEIPT_FILENAME
    if os.path.normcase(str(candidate)) != os.path.normcase(str(expected)):
        raise RuntimeError(
            "--review-receipt must be the declared review-receipt.json sibling "
            f"of run.json: {expected}"
        )
    return candidate


def _validate_review_receipt(
    output_dir: Path,
    raw_receipt_path: str,
    run: dict[str, Any],
    run_digest: str,
) -> dict[str, Any]:
    receipt_path = _review_receipt_path(output_dir, raw_receipt_path)
    receipt = _load_json_object(receipt_path, "declared review receipt")
    expected_keys = {
        "schema_version",
        "run_json_sha256",
        "target_revision",
        "declared_review_provenance",
        "reviewed",
        "sensitive_reviewed",
        "checkpoints",
    }
    if set(receipt) != expected_keys:
        raise RuntimeError(
            "review receipt fields must be exactly: "
            + ", ".join(sorted(expected_keys))
        )
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 2:
        raise RuntimeError("unsupported review receipt schema")
    if receipt.get("run_json_sha256") != run_digest:
        raise RuntimeError("review receipt is not bound to the current run.json SHA256")
    if not _is_sha256(receipt.get("run_json_sha256")):
        raise RuntimeError("review receipt run.json SHA256 is invalid")
    if receipt.get("target_revision") != run.get("target_revision"):
        raise RuntimeError("review receipt target revision does not match run evidence")
    provenance = receipt.get("declared_review_provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "reviewer_label",
        "review_method",
        "identity_assurance",
        "signature",
    }:
        raise RuntimeError("declared review provenance is incomplete")
    reviewer_label = provenance.get("reviewer_label")
    if (
        not isinstance(reviewer_label, str)
        or not reviewer_label.strip()
        or len(reviewer_label) > 200
        or provenance.get("review_method") != "manual-visual-inspection"
        or provenance.get("identity_assurance") != "none"
        or provenance.get("signature") is not None
    ):
        raise RuntimeError(
            "review provenance must declare manual inspection, no identity "
            "assurance, and no signature"
        )
    if receipt.get("reviewed") is not True:
        raise RuntimeError("review receipt must explicitly set reviewed=true")
    if receipt.get("sensitive_reviewed") is not True:
        raise RuntimeError("review receipt must explicitly set sensitive_reviewed=true")

    receipt_checkpoints = receipt.get("checkpoints")
    states = run["states"]
    if not isinstance(receipt_checkpoints, list) or len(receipt_checkpoints) != len(states):
        raise RuntimeError("review receipt checkpoint set is incomplete")
    expected_checkpoint_keys = {
        "state",
        "path",
        "sha256",
        "dimensions",
        "reviewed",
        "sensitivity",
    }
    for receipt_checkpoint, state in zip(
        receipt_checkpoints,
        states,
        strict=True,
    ):
        if not isinstance(receipt_checkpoint, dict):
            raise RuntimeError("review receipt checkpoint must be an object")
        if set(receipt_checkpoint) != expected_checkpoint_keys:
            raise RuntimeError(
                "review receipt checkpoint fields must be exactly: "
                + ", ".join(sorted(expected_checkpoint_keys))
            )
        for field in ("state", "path", "sha256", "dimensions"):
            if receipt_checkpoint.get(field) != state.get(
                "screenshot" if field == "path" else field
            ):
                raise RuntimeError(
                    f"review receipt checkpoint {field} mismatch for {state['state']}"
                )
        if receipt_checkpoint.get("reviewed") is not True:
            raise RuntimeError(
                f"review receipt checkpoint is not reviewed: {state['state']}"
            )
        sensitivity = receipt_checkpoint.get("sensitivity")
        if sensitivity not in SENSITIVITY_VALUES:
            raise RuntimeError(
                f"invalid review receipt sensitivity for {state['state']}: {sensitivity!r}"
            )
    return {
        "path": str(receipt_path),
        "sha256": _sha256(receipt_path),
        "schema_version": receipt["schema_version"],
        "run_json_sha256": run_digest,
        "target_revision": run["target_revision"],
        "declared_review_provenance": dict(provenance),
        "reviewed": receipt["reviewed"],
        "sensitive_reviewed": receipt["sensitive_reviewed"],
        "checkpoints": [dict(item) for item in receipt_checkpoints],
    }


def _finalize(output_dir: Path, raw_receipt_path: str) -> dict[str, Any]:
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise FileNotFoundError(f"evidence root not found: {output_dir}")
    _require_exact_inventory(
        output_dir,
        FINALIZE_INPUT_FILENAMES,
        "before finalization",
    )
    current_revision_before = _target_revision()
    run, run_digest = _validate_run_evidence(output_dir)
    if current_revision_before != run["target_revision"]:
        raise RuntimeError("current target revision does not match captured evidence")
    review = _validate_review_receipt(
        output_dir,
        raw_receipt_path,
        run,
        run_digest,
    )

    verified_run, verified_run_digest = _validate_run_evidence(output_dir)
    if verified_run_digest != run_digest or verified_run != run:
        raise RuntimeError("run evidence changed during finalization")
    if _sha256(Path(review["path"])) != review["sha256"]:
        raise RuntimeError("review receipt changed during finalization")
    current_revision_after = _target_revision()
    if current_revision_after != run["target_revision"]:
        raise RuntimeError("target revision changed during finalization")

    review["finalize_start_revision"] = current_revision_before
    review["finalize_end_revision"] = current_revision_after
    manifest = _write_manifest(output_dir, run, review=review)
    pending_manifest = output_dir / "manifest.pending.json"
    if pending_manifest.is_symlink() or not pending_manifest.is_file():
        raise RuntimeError(f"pending manifest is not a regular file: {pending_manifest}")
    pending_manifest.unlink()
    _require_exact_inventory(
        output_dir,
        FINALIZED_FILENAMES,
        "after finalization",
    )
    validated = _validate_finalized(output_dir)
    return {
        **validated,
        "manifest": str(manifest),
        "review_receipt": review["path"],
    }


def _validate_finalized(output_dir: Path) -> dict[str, Any]:
    """Read-only validation for a finalized native evidence bundle."""
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise FileNotFoundError(f"evidence root not found: {output_dir}")
    _require_exact_inventory(
        output_dir,
        FINALIZED_FILENAMES,
        "before standalone validation",
    )
    run, run_digest = _validate_run_evidence(output_dir)
    receipt_path = output_dir / REVIEW_RECEIPT_FILENAME
    review = _validate_review_receipt(
        output_dir,
        str(receipt_path),
        run,
        run_digest,
    )
    manifest_path = output_dir / "manifest.json"
    _validate_manifest_evidence(manifest_path, run, review)

    verified_run, verified_run_digest = _validate_run_evidence(output_dir)
    if verified_run_digest != run_digest or verified_run != run:
        raise RuntimeError("run evidence changed during standalone validation")
    if _sha256(receipt_path) != review["sha256"]:
        raise RuntimeError("review receipt changed during standalone validation")
    _require_exact_inventory(
        output_dir,
        FINALIZED_FILENAMES,
        "after standalone validation",
    )
    return {
        "ok": True,
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "review_receipt": str(receipt_path),
        "review_receipt_sha256": review["sha256"],
        "run_json_sha256": run_digest,
        "target_revision": run["target_revision"],
        "declared_review_provenance": dict(
            review["declared_review_provenance"]
        ),
        "exact_inventory": sorted(FINALIZED_FILENAMES),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        output_dir = _external_output_dir(args.output_dir)
        if args.finalize_review:
            result = _finalize(output_dir, args.review_receipt)
        elif args.validate_finalized:
            result = _validate_finalized(output_dir)
        else:
            result = _run(output_dir)
    except Exception as exc:
        print(f"Wrike Quick Panel native QA failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
