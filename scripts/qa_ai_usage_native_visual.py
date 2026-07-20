# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
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


SCENARIO_NAMES = (
    "zero-profiles",
    "mixed-ready-standard",
    "one-profile-125",
    "dynamic-three-profiles",
    "ten-mixed-profiles-150",
    "long-label-narrow",
    "cursor-long-amount-150",
    "cursor-logged-out",
    "cursor-stale-rate-limited",
)
METADATA_FILENAME = "ai-usage-native-visual-metadata.json"


def _iso(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _profile_settings(
    profile_id: str,
    label: str,
    provider: str,
    *,
    selected: bool = True,
) -> dict[str, Any]:
    safe_root = f"C:\\QA\\AIUsage\\{profile_id}"
    return {
        "id": profile_id,
        "label": label,
        "provider": provider,
        "enabled": True,
        "taskbar_selected": bool(selected),
        "settings_path": f"{safe_root}\\settings.json",
        "state_path": f"{safe_root}\\state.json",
        "profile_dir": f"{safe_root}\\browser-profile",
    }


def _ready_runtime() -> dict[str, Any]:
    return {
        "monitor_state": "idle",
        "provider_state": "ready",
        "session_state": "logged_in",
        "browser_state": "ready",
        "browser_last_error": "",
        "failure_count": 0,
        "collect_inflight": False,
        "can_login": False,
        "can_logout": True,
    }


def _codex_snapshot(captured_at: str) -> dict[str, Any]:
    return {
        "captured_at": captured_at,
        "remaining_credit": "$12.34",
        "five_hour_limit": "68% left",
        "five_hour_limit_reset_at": "2026-07-18T08:30:00Z",
        "weekly_limit": "52% left",
        "weekly_limit_reset_at": "2026-07-21T00:00:00Z",
        "gpt_5_3_codex_spark_five_hour_limit": "81% left",
        "gpt_5_3_codex_spark_five_hour_limit_reset_at": "2026-07-18T09:00:00Z",
        "gpt_5_3_codex_spark_weekly_limit": "73% left",
        "gpt_5_3_codex_spark_weekly_limit_reset_at": "2026-07-22T00:00:00Z",
    }


def _cursor_snapshot(captured_at: str) -> dict[str, Any]:
    return {
        "captured_at": captured_at,
        "included_usage": "42% used",
        "billing_reset_at": "2026-08-01T00:00:00Z",
        "on_demand_status": "Enabled · $8.20 used",
    }


def _runtime_profile(
    profile: dict[str, Any],
    runtime: dict[str, Any],
    snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        **copy.deepcopy(profile),
        "settings": {"interval_sec": 90.0},
        "runtime": copy.deepcopy(runtime),
        "last_snapshot": copy.deepcopy(snapshot),
    }


def build_scenario_fixture(
    name: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    scenario = str(name or "").strip()
    if scenario not in SCENARIO_NAMES:
        raise ValueError(f"unknown scenario: {scenario}")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    recent = _iso(current - timedelta(seconds=30))
    stale = _iso(current - timedelta(days=3))

    ui_scale_percent = 100
    if scenario == "long-label-narrow":
        codex_label = "Codex 장기 프로젝트 품질 검증 및 릴리스 자동화 업무용 프로필 아주 긴 표시 이름"
        cursor_label = "Cursor 다중 모니터 고해상도 한국어 레이아웃 검증 전용 프로필 아주 긴 표시 이름"
        window_size = [700, 680]
        phase = "interaction"
        interaction = {
            "action": "manual_query_and_toggle_taskbar_selection",
            "profile_id": "cursor_long",
        }
        codex_id = "codex_long"
        cursor_id = "cursor_long"
    else:
        codex_label = "Codex 업무"
        cursor_label = "Cursor 개발"
        window_size = (
            [900, 620]
            if scenario == "one-profile-125"
            else [1040, 760]
            if scenario in {"dynamic-three-profiles", "ten-mixed-profiles-150"}
            else [1040, 660]
        )
        phase = (
            "initial"
            if scenario in {"zero-profiles", "mixed-ready-standard"}
            else "final"
        )
        interaction = {"action": "none", "profile_id": ""}
        codex_id = "codex_primary"
        cursor_id = "cursor_primary"

    if scenario == "cursor-long-amount-150":
        ui_scale_percent = 150
        window_size = [960, 720]
        phase = "interaction"
        interaction = {"action": "keyboard_end_scroll", "profile_id": ""}

    codex_profile = _profile_settings(codex_id, codex_label, "codex")
    cursor_profile = _profile_settings(cursor_id, cursor_label, "cursor")
    codex_runtime = _ready_runtime()
    cursor_runtime = _ready_runtime()
    cursor_captured_at = recent

    if scenario == "cursor-logged-out":
        cursor_runtime.update(
            {
                "monitor_state": "paused_auth_required",
                "provider_state": "logged_out",
                "session_state": "logged_out",
                "browser_state": "stopped",
                "auth_attention_required": True,
                "auth_attention_reason": "login_required",
                "can_login": True,
                "can_logout": False,
            }
        )
        cursor_captured_at = stale
    elif scenario == "cursor-stale-rate-limited":
        cursor_runtime.update(
            {
                "monitor_state": "rate_limited",
                "provider_state": "rate_limited",
                "session_state": "logged_in",
                "browser_state": "ready",
                "browser_last_error": "http_429",
                "failure_count": 1,
                "next_collect_in_sec": 120,
                "can_login": False,
                "can_logout": True,
            }
        )
        cursor_captured_at = stale

    runtime_profiles = [
        _runtime_profile(codex_profile, codex_runtime, _codex_snapshot(recent)),
        _runtime_profile(
            cursor_profile,
            cursor_runtime,
            _cursor_snapshot(cursor_captured_at),
        ),
    ]
    if scenario == "cursor-long-amount-150":
        runtime_profiles[1]["last_snapshot"]["on_demand_status"] = (
            "Enabled · US$1,234,567,890.12 used"
        )
    settings_profiles = [codex_profile, cursor_profile]
    if scenario == "zero-profiles":
        settings_profiles = []
        runtime_profiles = []
    elif scenario == "one-profile-125":
        ui_scale_percent = 125
        settings_profiles = [codex_profile]
        runtime_profiles = [runtime_profiles[0]]
    elif scenario == "dynamic-three-profiles":
        third_profile = _profile_settings(
            "profile_00000000000000000000000000000003",
            "Codex 릴리스 검증",
            "codex",
            selected=False,
        )
        settings_profiles.append(third_profile)
        runtime_profiles.append(
            _runtime_profile(third_profile, _ready_runtime(), _codex_snapshot(recent))
        )
        interaction = {"action": "mousewheel_scroll", "profile_id": ""}
    elif scenario == "ten-mixed-profiles-150":
        ui_scale_percent = 150
        for index in range(3, 11):
            provider = "codex" if index % 2 else "cursor"
            profile = _profile_settings(
                f"profile_{index:032x}",
                f"{provider.title()} QA 프로필 {index} · {'한국어 긴 이름' if index % 3 == 0 else 'English long label'}",
                provider,
                selected=False,
            )
            settings_profiles.append(profile)
            snapshot = _codex_snapshot(recent) if provider == "codex" else _cursor_snapshot(recent)
            runtime_profiles.append(_runtime_profile(profile, _ready_runtime(), snapshot))
        phase = "interaction"
        interaction = {"action": "keyboard_end_scroll", "profile_id": ""}
    settings = {
        "enabled": True,
        "taskbar_overlay_enabled": True,
        "interval_sec": 90.0,
        "tooltip_duration_ms": 7000,
        "usage_url": "https://example.invalid/ai-usage",
        "settings_path": "C:\\QA\\AIUsage\\settings.json",
        "state_path": "C:\\QA\\AIUsage\\state.json",
        "profile_dir": "C:\\QA\\AIUsage\\profiles",
        "profiles": copy.deepcopy(settings_profiles),
        "accounts": copy.deepcopy(settings_profiles),
        "profile_order": [str(profile["id"]) for profile in settings_profiles],
        "selected_profile_ids": [
            str(profile["id"])
            for profile in settings_profiles
            if bool(profile.get("taskbar_selected"))
        ],
    }
    runtime = {
        "enabled": True,
        "monitor_state": "mixed",
        "session_state": "mixed",
        "profiles": copy.deepcopy(runtime_profiles),
        "accounts": copy.deepcopy(runtime_profiles),
    }
    return {
        "name": scenario,
        "phase": phase,
        "screenshot_name": f"{scenario}.png",
        "window_size": window_size,
        "ui_scale_percent": ui_scale_percent,
        "interaction": interaction,
        "settings": settings,
        "runtime": runtime,
        "fixture_contains_real_identity": False,
    }


class SyntheticAiUsageManager:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self._settings = copy.deepcopy(fixture.get("settings", {}))
        self._runtime = copy.deepcopy(fixture.get("runtime", {}))
        self.calls: list[dict[str, Any]] = []

    def get_settings_snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._settings)

    def update_settings(self, payload: dict[str, Any]) -> tuple[bool, str | None]:
        self._settings.update(copy.deepcopy(payload))
        self.calls.append({"action": "update_settings"})
        return True, None

    def get_runtime_status(self) -> dict[str, Any]:
        return copy.deepcopy(self._runtime)

    def get_last_snapshot(self) -> dict[str, Any]:
        profiles = self._runtime.get("profiles", [])
        if isinstance(profiles, list) and profiles and isinstance(profiles[0], dict):
            snapshot = profiles[0].get("last_snapshot")
            return copy.deepcopy(snapshot) if isinstance(snapshot, dict) else {}
        return {}

    def show_account_status(
        self,
        account_id: str,
        *,
        force_refresh: bool = False,
        source: str = "",
    ) -> None:
        normalized = str(account_id or "")
        self.calls.append(
            {
                "action": "show_account_status",
                "profile_id": normalized,
                "force_refresh": bool(force_refresh),
                "source": str(source or ""),
            }
        )
        for collection_name in ("profiles", "accounts"):
            collection = self._runtime.get(collection_name, [])
            if not isinstance(collection, list):
                continue
            for entry in collection:
                if not isinstance(entry, dict) or str(entry.get("id") or "") != normalized:
                    continue
                runtime = entry.setdefault("runtime", {})
                if isinstance(runtime, dict):
                    runtime["monitor_state"] = "running"
                    runtime["collect_inflight"] = True
                    runtime["collect_source"] = "manual_query"

    def login_account(self, account_id: str) -> None:
        self.calls.append({"action": "login_account", "profile_id": str(account_id or "")})

    def release_account_profile_session(self, account_id: str) -> tuple[bool, str]:
        self.calls.append({"action": "release_account", "profile_id": str(account_id or "")})
        return True, "Synthetic logout completed."

    def format_captured_at_for_display(self, value: Any) -> str:
        return str(value or "-").replace("T", " ").replace("Z", " UTC")

    def format_reset_at_for_display(self, value: Any, _key: str = "") -> str:
        return self.format_captured_at_for_display(value)


def validate_output_dir(value: str | os.PathLike[str]) -> Path:
    output_dir = Path(value).expanduser().resolve(strict=False)
    repo_root = REPO_ROOT.resolve()
    try:
        common = Path(os.path.commonpath([str(repo_root), str(output_dir)]))
    except ValueError:
        common = Path()
    if os.path.normcase(str(common)) == os.path.normcase(str(repo_root)):
        raise ValueError(f"--output-dir must be outside the repository: {output_dir}")
    return output_dir


def _enable_per_monitor_dpi_awareness() -> str:
    if os.name != "nt":
        return "unsupported"
    try:
        import ctypes

        if bool(ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        import ctypes

        if int(ctypes.windll.shcore.SetProcessDpiAwareness(2)) in {0, 0x80070005}:
            return "per-monitor"
    except Exception:
        pass
    try:
        import ctypes

        if bool(ctypes.windll.user32.SetProcessDPIAware()):
            return "system"
    except Exception:
        pass
    return "unchanged"


def _window_dpi(window: Any) -> int:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = int(user32.GetAncestor(int(window.winfo_id()), 2) or window.winfo_id())
        get_dpi = getattr(user32, "GetDpiForWindow", None)
        return int(get_dpi(hwnd)) if callable(get_dpi) else 0
    except Exception:
        return 0


def _system_dpi() -> int:
    try:
        import ctypes

        get_dpi = getattr(ctypes.windll.user32, "GetDpiForSystem", None)
        return int(get_dpi()) if callable(get_dpi) else 0
    except Exception:
        return 0


def _virtual_screen_metrics() -> list[int]:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return [
            int(user32.GetSystemMetrics(76)),
            int(user32.GetSystemMetrics(77)),
            int(user32.GetSystemMetrics(78)),
            int(user32.GetSystemMetrics(79)),
        ]
    except Exception:
        return [0, 0, 0, 0]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _write_rgb_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    row_size = int(width) * 3
    rows = bytearray()
    for row in range(int(height)):
        start = row * row_size
        rows.append(0)
        rows.extend(rgb[start : start + row_size])
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=6))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _capture_window_png(window: Any, output_path: Path) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("native Tk visual capture requires Windows")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    hwnd = int(user32.GetAncestor(wintypes.HWND(window.winfo_id()), 2) or window.winfo_id())
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise RuntimeError("window bounds are empty")

    screen_dc = user32.GetDC(0)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    old_object = gdi32.SelectObject(memory_dc, bitmap)
    try:
        copied = gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            screen_dc,
            int(rect.left),
            int(rect.top),
            0x40CC0020,
        )
        if not copied:
            raise RuntimeError("BitBlt failed")

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

        info = BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        info.bmiHeader.biSizeImage = width * height * 4
        buffer = ctypes.create_string_buffer(width * height * 4)
        rows = gdi32.GetDIBits(
            screen_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(info),
            0,
        )
        if int(rows) != height:
            raise RuntimeError("GetDIBits failed")
        rgb = bytearray()
        raw = buffer.raw
        for offset in range(0, len(raw), 4):
            rgb.extend((raw[offset + 2], raw[offset + 1], raw[offset]))
        _write_rgb_png(output_path, width, height, bytes(rgb))
    finally:
        if old_object:
            gdi32.SelectObject(memory_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if screen_dc:
            user32.ReleaseDC(0, screen_dc)

    return {
        "engine": "win32-bitblt",
        "window_rect_px": [
            int(rect.left),
            int(rect.top),
            width,
            height,
        ],
        "pixel_size": [width, height],
    }


def _walk_widgets(widget: Any) -> list[Any]:
    descendants = [widget]
    try:
        children = list(widget.winfo_children())
    except Exception:
        children = []
    for child in children:
        descendants.extend(_walk_widgets(child))
    return descendants


def _widget_text(widget: Any) -> str:
    try:
        value = widget.cget("text")
        if value:
            return str(value)
    except Exception:
        pass
    try:
        variable_name = str(widget.cget("textvariable") or "")
        if variable_name:
            return str(widget.getvar(variable_name) or "")
    except Exception:
        pass
    return ""


def _collect_widget_metrics(root: Any) -> dict[str, Any]:
    root_left = int(root.winfo_rootx())
    root_top = int(root.winfo_rooty())
    root_right = root_left + int(root.winfo_width())
    root_bottom = root_top + int(root.winfo_height())
    widgets: list[dict[str, Any]] = []
    clipped: list[str] = []
    text_overflow: list[str] = []
    for widget in _walk_widgets(root):
        if widget is root:
            continue
        try:
            if not widget.winfo_manager() or not widget.winfo_ismapped():
                continue
            left = int(widget.winfo_rootx())
            top = int(widget.winfo_rooty())
            width = int(widget.winfo_width())
            height = int(widget.winfo_height())
            requested_width = int(widget.winfo_reqwidth())
            requested_height = int(widget.winfo_reqheight())
            path = str(widget)
            widget_class = str(widget.winfo_class())
        except Exception:
            continue
        text = _widget_text(widget)
        widgets.append(
            {
                "path": path,
                "class": widget_class,
                "rect_px": [left, top, width, height],
                "requested_px": [requested_width, requested_height],
                "text": text,
            }
        )
        if left < root_left or top < root_top or left + width > root_right + 1 or top + height > root_bottom + 1:
            clipped.append(path)
        intersects_viewport = bool(
            left < root_right
            and left + width > root_left
            and top < root_bottom
            and top + height > root_top
        )
        if text and intersects_viewport and requested_width > width + 1:
            text_overflow.append(path)
    return {
        "widget_count": len(widgets),
        "clipped_widgets": clipped,
        "text_overflow_widgets": text_overflow,
        "widgets": widgets,
    }


def _apply_interaction(view: Any, manager: SyntheticAiUsageManager, fixture: dict[str, Any]) -> dict[str, Any]:
    interaction = fixture.get("interaction", {})
    if not isinstance(interaction, dict) or interaction.get("action") == "none":
        return {"action": "none", "applied": True, "manager_calls": []}
    action = str(interaction.get("action") or "")
    if action in {"mousewheel_scroll", "keyboard_end_scroll"}:
        canvas = getattr(view, "_scroll_canvas", None)
        if canvas is None:
            return {"action": action, "applied": False, "manager_calls": []}
        try:
            before = tuple(float(value) for value in canvas.yview())
            canvas.focus_force()
            canvas.update()
            if action == "mousewheel_scroll":
                canvas.event_generate("<MouseWheel>", delta=-480)
            else:
                canvas.event_generate("<End>")
            canvas.update()
            after = tuple(float(value) for value in canvas.yview())
        except Exception:
            return {"action": action, "applied": False, "manager_calls": []}
        return {
            "action": action,
            "applied": bool(after != before and after[0] > before[0]),
            "scroll_before": list(before),
            "scroll_after": list(after),
            "manager_calls": [],
        }
    profile_id = str(interaction.get("profile_id") or "")
    selected_var = getattr(view, "_account_taskbar_selected_vars", {}).get(profile_id)
    if selected_var is not None:
        view._loading_settings = True
        try:
            selected_var.set(False)
        finally:
            view._loading_settings = False
    view._on_account_query(profile_id)
    view._refresh_runtime_status()
    button = getattr(view, "_account_query_buttons", {}).get(profile_id)
    if button is not None:
        try:
            button.focus_set()
        except Exception:
            pass
    query_applied = any(
        call.get("action") == "show_account_status"
        and call.get("profile_id") == profile_id
        for call in manager.calls
    )
    selection_applied = selected_var is not None and not bool(selected_var.get())
    return {
        "action": str(interaction.get("action") or ""),
        "applied": bool(query_applied and selection_applied),
        "profile_id": profile_id,
        "taskbar_selected_after": bool(selected_var.get()) if selected_var is not None else None,
        "manager_calls": copy.deepcopy(manager.calls),
    }


def capture_scenario(
    fixture: dict[str, Any],
    output_dir: Path,
    *,
    settle_ms: int = 180,
) -> dict[str, Any]:
    import tkinter as tk

    from src.apps.ai_usage_ui import AIUsageSettingsView

    manager = SyntheticAiUsageManager(fixture)
    root = tk.Tk()
    view = None
    try:
        name = str(fixture.get("name") or "scenario")
        width, height = [int(value) for value in fixture.get("window_size", [1040, 660])]
        ui_scale_percent = int(fixture.get("ui_scale_percent", 100) or 100)
        root.tk.call("tk", "scaling", (96.0 * ui_scale_percent / 100.0) / 72.0)
        root.title(f"Windows Supporter · AI 사용량 · {name}")
        root.geometry(f"{width}x{height}+40+40")
        root.minsize(700, 560)
        root.configure(bg="#F3F4F6")
        root.attributes("-topmost", True)
        parent = tk.Frame(root, bg="#F3F4F6")
        parent.pack(fill="both", expand=True)
        view = AIUsageSettingsView(root, manager, ui_post=lambda fn: root.after(0, fn))
        view.mount(parent)
        root.update_idletasks()
        root.update()
        interaction = _apply_interaction(view, manager, fixture)
        root.update_idletasks()
        root.update()
        root.lift()
        try:
            root.focus_force()
        except Exception:
            pass
        time.sleep(max(0, int(settle_ms)) / 1000.0)
        root.update_idletasks()
        root.update()

        screenshot_path = output_dir / str(fixture["screenshot_name"])
        capture = _capture_window_png(root, screenshot_path)
        widget_metrics = _collect_widget_metrics(root)
        png_bytes = screenshot_path.read_bytes()
        return {
            "ok": bool(
                png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
                and interaction.get("applied", False)
            ),
            "scenario": name,
            "phase": str(fixture.get("phase") or ""),
            "screenshot": screenshot_path.name,
            "screenshot_sha256": hashlib.sha256(png_bytes).hexdigest(),
            "capture": capture,
            "interaction": interaction,
            "window": {
                "geometry": str(root.geometry()),
                "client_size_px": [int(root.winfo_width()), int(root.winfo_height())],
                "requested_size_px": [int(root.winfo_reqwidth()), int(root.winfo_reqheight())],
                "screen_size_px": [int(root.winfo_screenwidth()), int(root.winfo_screenheight())],
                "virtual_screen_px": _virtual_screen_metrics(),
            },
            "dpi": {
                "requested_ui_scale_percent": ui_scale_percent,
                "window_dpi": _window_dpi(root),
                "system_dpi": _system_dpi(),
                "tk_scaling": float(root.tk.call("tk", "scaling")),
                "tk_fpixels_1i": float(root.winfo_fpixels("1i")),
            },
            "widget_metrics": widget_metrics,
        }
    finally:
        if view is not None:
            try:
                view._stop_runtime_refresh()
            except Exception:
                pass
        try:
            root.destroy()
        except Exception:
            pass


def capture_provenance() -> dict[str, Any]:
    git_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
    ).strip()
    if len(git_sha) != 40:
        raise RuntimeError(f"expected a full Git SHA, got: {git_sha!r}")
    worktree_status = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
    )
    return {
        "git_sha": git_sha,
        "worktree_clean": not bool(worktree_status.strip()),
    }


def run_capture_matrix(output_dir: Path, *, settle_ms: int = 180) -> dict[str, Any]:
    output_dir = validate_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    provenance = capture_provenance()
    dpi_awareness = _enable_per_monitor_dpi_awareness()
    generated_at = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    for name in SCENARIO_NAMES:
        fixture = build_scenario_fixture(name, now=generated_at)
        try:
            results.append(
                capture_scenario(
                    fixture,
                    output_dir,
                    settle_ms=settle_ms,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "ok": False,
                    "scenario": name,
                    "phase": str(fixture.get("phase") or ""),
                    "screenshot": str(fixture.get("screenshot_name") or ""),
                    "error": repr(exc),
                }
            )

    generated_pngs = [
        name
        for name in (f"{scenario}.png" for scenario in SCENARIO_NAMES)
        if (output_dir / name).is_file()
    ]
    phases = {str(result.get("phase") or "") for result in results if result.get("ok")}
    final_provenance = capture_provenance()
    provenance_stable = provenance == final_provenance
    report = {
        "schema_version": 1,
        "ok": (
            provenance["worktree_clean"]
            and
            final_provenance["worktree_clean"]
            and
            provenance_stable
            and
            len(results) == len(SCENARIO_NAMES)
            and all(bool(result.get("ok")) for result in results)
            and len(generated_pngs) == len(SCENARIO_NAMES)
            and {"initial", "interaction", "final"}.issubset(phases)
        ),
        "generated_at_utc": _iso(generated_at),
        "git_sha": provenance["git_sha"],
        "git_worktree_clean": provenance["worktree_clean"],
        "git_end_sha": final_provenance["git_sha"],
        "git_end_worktree_clean": final_provenance["worktree_clean"],
        "git_provenance_stable": provenance_stable,
        "capture_surface": "native-tk",
        "dpi_awareness": dpi_awareness,
        "fixture_policy": {
            "synthetic_only": True,
            "contains_credentials": False,
            "contains_cookies": False,
            "contains_real_user_identity": False,
            "network_access": False,
        },
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "required_phases": ["initial", "interaction", "final"],
        "generated_pngs": generated_pngs,
        "results": results,
    }
    (output_dir / METADATA_FILENAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture synthetic native Tk evidence for the AI usage settings view."
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Evidence directory outside the repository.",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=180,
        help="Short UI settle interval before each capture.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if os.name != "nt":
        print("native Tk visual capture requires Windows", file=sys.stderr)
        return 2
    try:
        report = run_capture_matrix(
            validate_output_dir(args.output_dir),
            settle_ms=max(0, min(2000, int(args.settle_ms))),
        )
    except Exception as exc:
        print(f"AI usage native visual capture failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": bool(report.get("ok")),
                "metadata": METADATA_FILENAME,
                "generated_pngs": report.get("generated_pngs", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
