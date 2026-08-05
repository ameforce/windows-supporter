from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable
import uuid


DEVICE_NOTIFY_WINDOW_HANDLE = 0
PBT_POWERSETTINGCHANGE = 0x8013

GUID_LIDSWITCH_STATE_CHANGE = uuid.UUID("ba3e0f4d-b817-4094-a2d1-d56379e6a0f3")
GUID_ACDC_POWER_SOURCE = uuid.UUID("5d3e9a59-e9d5-4b00-a6bd-ff34ff516548")
GUID_BATTERY_PERCENTAGE_REMAINING = uuid.UUID(
    "a7ad8041-b45a-4cae-87a3-ee40c0b59f81"
)
GUID_ACTIVE_POWERSCHEME = uuid.UUID("31f9f286-5084-42fe-b720-2b0264993763")
GUID_SUB_BUTTONS = uuid.UUID("4f971e89-eebd-4455-a8de-9e59040e7347")
GUID_LIDACTION = uuid.UUID("5ca83367-6e45-459f-a27b-476b1d01c936")

LID_ACTION_DO_NOTHING = 0


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: uuid.UUID | str) -> "GUID":
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return cls.from_buffer_copy(parsed.bytes_le)

    def to_uuid(self) -> uuid.UUID:
        return uuid.UUID(bytes_le=bytes(self))


class BATTERY_REPORTING_SCALE(ctypes.Structure):
    _fields_ = [
        ("Granularity", wintypes.DWORD),
        ("Capacity", wintypes.DWORD),
    ]


class SYSTEM_POWER_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("PowerButtonPresent", ctypes.c_ubyte),
        ("SleepButtonPresent", ctypes.c_ubyte),
        ("LidPresent", ctypes.c_ubyte),
        ("SystemS1", ctypes.c_ubyte),
        ("SystemS2", ctypes.c_ubyte),
        ("SystemS3", ctypes.c_ubyte),
        ("SystemS4", ctypes.c_ubyte),
        ("SystemS5", ctypes.c_ubyte),
        ("HiberFilePresent", ctypes.c_ubyte),
        ("FullWake", ctypes.c_ubyte),
        ("VideoDimPresent", ctypes.c_ubyte),
        ("ApmPresent", ctypes.c_ubyte),
        ("UpsPresent", ctypes.c_ubyte),
        ("ThermalControl", ctypes.c_ubyte),
        ("ProcessorThrottle", ctypes.c_ubyte),
        ("ProcessorMinThrottle", ctypes.c_ubyte),
        ("ProcessorMaxThrottle", ctypes.c_ubyte),
        ("FastSystemS4", ctypes.c_ubyte),
        ("Hiberboot", ctypes.c_ubyte),
        ("WakeAlarmPresent", ctypes.c_ubyte),
        ("AoAc", ctypes.c_ubyte),
        ("DiskSpinDown", ctypes.c_ubyte),
        ("HiberFileType", ctypes.c_ubyte),
        ("AoAcConnectivitySupported", ctypes.c_ubyte),
        ("spare3", ctypes.c_ubyte * 6),
        ("SystemBatteriesPresent", ctypes.c_ubyte),
        ("BatteriesAreShortTerm", ctypes.c_ubyte),
        ("BatteryScale", BATTERY_REPORTING_SCALE * 3),
        ("AcOnLineWake", wintypes.DWORD),
        ("SoftLidWake", wintypes.DWORD),
        ("RtcWake", wintypes.DWORD),
        ("MinDeviceWakeState", wintypes.DWORD),
        ("DefaultLowLatencyWake", wintypes.DWORD),
    ]


@dataclass(frozen=True, slots=True)
class PowerCapabilities:
    lid_present: bool
    system_batteries_present: bool
    batteries_are_short_term: bool
    error: str | None = None

    @property
    def eligible(self) -> bool:
        return bool(
            self.error is None
            and self.lid_present
            and self.system_batteries_present
            and not self.batteries_are_short_term
        )

    @property
    def reason(self) -> str:
        if self.error:
            return str(self.error)
        if not self.lid_present:
            return "lid_switch_not_present"
        if not self.system_batteries_present:
            return "system_battery_not_present"
        if self.batteries_are_short_term:
            return "short_term_battery_or_ups"
        return "supported"


def detect_power_capabilities() -> PowerCapabilities:
    if os.name != "nt":
        return PowerCapabilities(False, False, False, "windows_only")
    try:
        powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
        get_capabilities = powrprof.GetPwrCapabilities
        get_capabilities.argtypes = [ctypes.POINTER(SYSTEM_POWER_CAPABILITIES)]
        get_capabilities.restype = wintypes.BOOL
        value = SYSTEM_POWER_CAPABILITIES()
        if not bool(get_capabilities(ctypes.byref(value))):
            error = ctypes.get_last_error()
            return PowerCapabilities(
                False,
                False,
                False,
                f"GetPwrCapabilities failed: winerror={int(error)}",
            )
        return PowerCapabilities(
            bool(value.LidPresent),
            bool(value.SystemBatteriesPresent),
            bool(value.BatteriesAreShortTerm),
            None,
        )
    except Exception as exc:
        return PowerCapabilities(
            False,
            False,
            False,
            f"GetPwrCapabilities failed: {type(exc).__name__}: {exc}",
        )


class PowerNotificationRegistration:
    _SETTING_KINDS = (
        (GUID_LIDSWITCH_STATE_CHANGE, "lid"),
        (GUID_ACDC_POWER_SOURCE, "acdc"),
        (GUID_BATTERY_PERCENTAGE_REMAINING, "battery"),
        (GUID_ACTIVE_POWERSCHEME, "scheme"),
    )

    def __init__(self, callback: Callable[[str, object], None]) -> None:
        self._callback = callback
        self._handles: list[int] = []
        self._guid_kinds = {guid: kind for guid, kind in self._SETTING_KINDS}
        self._user32 = None

    @property
    def registered_count(self) -> int:
        return len(self._handles)

    def _load_api(self):
        if self._user32 is not None:
            return self._user32
        if os.name != "nt":
            raise OSError("power notifications require Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterPowerSettingNotification.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(GUID),
            wintypes.DWORD,
        ]
        user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
        user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
        user32.UnregisterPowerSettingNotification.restype = wintypes.BOOL
        self._user32 = user32
        return user32

    def register(self, hwnd: int) -> None:
        if self._handles:
            return
        if not hwnd:
            raise ValueError("a valid window handle is required")
        user32 = self._load_api()
        try:
            for setting, _kind in self._SETTING_KINDS:
                native_guid = GUID.from_uuid(setting)
                handle = user32.RegisterPowerSettingNotification(
                    wintypes.HANDLE(int(hwnd)),
                    ctypes.byref(native_guid),
                    DEVICE_NOTIFY_WINDOW_HANDLE,
                )
                if not handle:
                    error = ctypes.get_last_error()
                    raise OSError(
                        int(error),
                        f"RegisterPowerSettingNotification failed for {setting}",
                    )
                self._handles.append(int(handle))
        except Exception:
            self.unregister()
            raise

    def unregister(self) -> None:
        handles, self._handles = list(reversed(self._handles)), []
        if not handles:
            return
        user32 = self._load_api()
        for handle in handles:
            try:
                user32.UnregisterPowerSettingNotification(
                    wintypes.HANDLE(int(handle))
                )
            except Exception:
                continue

    def handle_message(self, lparam: int) -> bool:
        if not lparam:
            return False
        try:
            address = int(lparam)
            header = ctypes.string_at(address, 20)
            setting = uuid.UUID(bytes_le=header[:16])
            length = int.from_bytes(header[16:20], "little", signed=False)
            if length < 0 or length > 64:
                return False
            data = ctypes.string_at(address + 20, length) if length else b""
        except Exception:
            return False
        kind = self._guid_kinds.get(setting)
        if kind is None:
            return False
        if kind == "scheme":
            value: object = (
                str(uuid.UUID(bytes_le=data[:16])) if len(data) >= 16 else None
            )
        else:
            value = int.from_bytes(data[:4], "little", signed=False) if data else None
        try:
            self._callback(kind, value)
        except Exception:
            return False
        return True


class WindowsPowerPolicyBackend:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows power policy requires Windows")
        self._powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        p = self._powrprof
        p.PowerGetActiveScheme.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.POINTER(GUID)),
        ]
        p.PowerGetActiveScheme.restype = wintypes.DWORD
        for name in ("PowerReadACValueIndex", "PowerReadDCValueIndex"):
            fn = getattr(p, name)
            fn.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(GUID),
                ctypes.POINTER(GUID),
                ctypes.POINTER(GUID),
                ctypes.POINTER(wintypes.DWORD),
            ]
            fn.restype = wintypes.DWORD
        for name in ("PowerWriteACValueIndex", "PowerWriteDCValueIndex"):
            fn = getattr(p, name)
            fn.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(GUID),
                ctypes.POINTER(GUID),
                ctypes.POINTER(GUID),
                wintypes.DWORD,
            ]
            fn.restype = wintypes.DWORD
        p.PowerSetActiveScheme.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(GUID),
        ]
        p.PowerSetActiveScheme.restype = wintypes.DWORD
        p.SetSuspendState.argtypes = [
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.BOOL,
        ]
        p.SetSuspendState.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if int(status) != 0:
            raise OSError(int(status), operation)

    def get_active_scheme(self) -> str:
        pointer = ctypes.POINTER(GUID)()
        status = self._powrprof.PowerGetActiveScheme(None, ctypes.byref(pointer))
        self._check(status, "PowerGetActiveScheme")
        if not pointer:
            raise OSError("PowerGetActiveScheme returned no GUID")
        try:
            return str(pointer.contents.to_uuid())
        finally:
            self._kernel32.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))

    def read_lid_action(self, scheme: str, source: str) -> int:
        scheme_guid = GUID.from_uuid(scheme)
        subgroup = GUID.from_uuid(GUID_SUB_BUTTONS)
        setting = GUID.from_uuid(GUID_LIDACTION)
        value = wintypes.DWORD()
        if source == "ac":
            fn = self._powrprof.PowerReadACValueIndex
        elif source == "dc":
            fn = self._powrprof.PowerReadDCValueIndex
        else:
            raise ValueError("source must be ac or dc")
        status = fn(
            None,
            ctypes.byref(scheme_guid),
            ctypes.byref(subgroup),
            ctypes.byref(setting),
            ctypes.byref(value),
        )
        self._check(status, f"PowerRead{source.upper()}ValueIndex")
        return int(value.value)

    def write_lid_action(self, scheme: str, source: str, value: int) -> None:
        scheme_guid = GUID.from_uuid(scheme)
        subgroup = GUID.from_uuid(GUID_SUB_BUTTONS)
        setting = GUID.from_uuid(GUID_LIDACTION)
        if source == "ac":
            fn = self._powrprof.PowerWriteACValueIndex
        elif source == "dc":
            fn = self._powrprof.PowerWriteDCValueIndex
        else:
            raise ValueError("source must be ac or dc")
        status = fn(
            None,
            ctypes.byref(scheme_guid),
            ctypes.byref(subgroup),
            ctypes.byref(setting),
            wintypes.DWORD(int(value)),
        )
        self._check(status, f"PowerWrite{source.upper()}ValueIndex")

    def activate_scheme(self, scheme: str) -> None:
        scheme_guid = GUID.from_uuid(scheme)
        status = self._powrprof.PowerSetActiveScheme(
            None,
            ctypes.byref(scheme_guid),
        )
        self._check(status, "PowerSetActiveScheme")

    def request_sleep(self) -> None:
        if not bool(self._powrprof.SetSuspendState(False, False, False)):
            error = ctypes.get_last_error()
            raise OSError(int(error), "SetSuspendState")


def is_process_alive(pid: int) -> bool:
    if int(pid) <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == 259
    finally:
        kernel32.CloseHandle(handle)


def wait_for_process_exit(pid: int) -> None:
    if os.name != "nt":
        while is_process_alive(pid):
            time.sleep(0.5)
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    synchronize = 0x00100000
    infinite = 0xFFFFFFFF
    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, infinite)
    finally:
        kernel32.CloseHandle(handle)


def spawn_hidden_process(argv: list[str]) -> subprocess.Popen:
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    return subprocess.Popen(list(argv), **kwargs)


def watchdog_command(owner_pid: int, journal_path: Path, lease_id: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            str(Path(sys.executable).resolve()),
            "--lid-power-watchdog",
            "--parent-pid",
            str(int(owner_pid)),
            "--journal",
            str(journal_path),
            "--lease-id",
            str(lease_id),
        ]
    main_path = Path(__file__).resolve().parents[2] / "main.py"
    return [
        str(Path(sys.executable).resolve()),
        str(main_path),
        "--lid-power-watchdog",
        "--parent-pid",
        str(int(owner_pid)),
        "--journal",
        str(journal_path),
        "--lease-id",
        str(lease_id),
    ]


def spawn_policy_watchdog(
    owner_pid: int,
    journal_path: Path,
    lease_id: str,
) -> subprocess.Popen:
    return spawn_hidden_process(
        watchdog_command(owner_pid, journal_path, lease_id)
    )


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
