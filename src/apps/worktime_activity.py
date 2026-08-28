"""Poll Windows last-input state without installing keyboard or mouse hooks."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from datetime import datetime


class LASTINPUTINFO(ctypes.Structure):
    """Win32 ``LASTINPUTINFO`` structure used by ``GetLastInputInfo``."""

    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


class LastInputUnavailableError(OSError):
    """Raised when the Windows last-input API is unavailable or fails."""


class WindowsLastInputProvider:
    """Strict wrapper around ``user32!GetLastInputInfo``."""

    def __init__(self, *, os_name=None, ctypes_module=None) -> None:
        resolved_os_name = os.name if os_name is None else os_name
        if resolved_os_name != "nt":
            raise LastInputUnavailableError(
                "GetLastInputInfo is available only on Windows."
            )

        ctypes_api = ctypes if ctypes_module is None else ctypes_module
        try:
            user32 = ctypes_api.WinDLL("user32", use_last_error=True)
            get_last_input_info = user32.GetLastInputInfo
            get_last_input_info.argtypes = [ctypes_api.POINTER(LASTINPUTINFO)]
            get_last_input_info.restype = wintypes.BOOL
        except Exception as exc:
            raise LastInputUnavailableError(
                "GetLastInputInfo could not be initialized."
            ) from exc

        self._ctypes = ctypes_api
        self._get_last_input_info = get_last_input_info

    def get_last_input_tick(self) -> int:
        """Return the unsigned 32-bit tick of the latest user input."""

        info = LASTINPUTINFO()
        info.cbSize = self._ctypes.sizeof(LASTINPUTINFO)
        set_last_error = getattr(self._ctypes, "set_last_error", None)
        if callable(set_last_error):
            set_last_error(0)

        try:
            succeeded = self._get_last_input_info(self._ctypes.byref(info))
        except Exception as exc:
            raise LastInputUnavailableError("GetLastInputInfo call failed.") from exc
        if not succeeded:
            get_last_error = getattr(self._ctypes, "get_last_error", None)
            error_code = int(get_last_error()) if callable(get_last_error) else 0
            win_error = getattr(self._ctypes, "WinError", None)
            if error_code:
                cause = OSError(error_code, "GetLastInputInfo returned failure.")
                if callable(win_error):
                    try:
                        cause = win_error(error_code)
                    except Exception as exc:
                        cause = exc
                raise LastInputUnavailableError(
                    error_code,
                    "GetLastInputInfo returned failure.",
                ) from cause
            raise LastInputUnavailableError(
                "GetLastInputInfo returned failure without an error code."
            )
        return int(info.dwTime)

    def __call__(self) -> int:
        return self.get_last_input_tick()


LastInputInfoProvider = WindowsLastInputProvider


def get_last_input_tick() -> int:
    """Read the latest Windows input tick with the strict default provider."""

    return WindowsLastInputProvider().get_last_input_tick()


_UNSET = object()
_PENDING_AFTER = object()
_SCHEDULED_WITHOUT_ID = object()


class WorktimeActivityWatcher:
    """Detect changes in last-input ticks through a Tk-style ``root.after``.

    The first successful read establishes a baseline. Only later tick changes
    call ``callback(now())``. API/read/callback failures never install hooks and
    do not stop later polls.
    """

    def __init__(
        self,
        root,
        callback,
        provider=None,
        now=None,
        poll_interval_ms: int = 500,
        *,
        now_provider=None,
    ) -> None:
        if not callable(getattr(root, "after", None)):
            raise ValueError("root.after must be callable.")
        if not callable(callback):
            raise ValueError("callback must be callable.")
        if isinstance(poll_interval_ms, bool) or not isinstance(poll_interval_ms, int):
            raise ValueError("poll_interval_ms must be a positive integer.")
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be a positive integer.")
        if now is not None and now_provider is not None:
            raise ValueError("Specify only one now provider.")

        resolved_now = now_provider if now_provider is not None else now
        if resolved_now is None:
            resolved_now = datetime.now
        if not callable(resolved_now):
            raise ValueError("now must be callable.")

        resolved_provider = provider
        if resolved_provider is None:
            resolved_provider = WindowsLastInputProvider()
        provider_getter = getattr(resolved_provider, "get_last_input_tick", None)
        if not callable(provider_getter):
            provider_getter = resolved_provider if callable(resolved_provider) else None
        if not callable(provider_getter):
            raise ValueError("provider must be callable or expose get_last_input_tick().")

        self._root = root
        self._callback = callback
        self._provider_getter = provider_getter
        self._now = resolved_now
        self._poll_interval_ms = poll_interval_ms
        self._running = False
        self._generation = 0
        self._after_id = None
        self._baseline = _UNSET

    @property
    def is_running(self) -> bool:
        return bool(self._running)

    @property
    def poll_interval_ms(self) -> int:
        return int(self._poll_interval_ms)

    def _read_tick(self):
        try:
            value = self._provider_getter()
        except Exception:
            return _UNSET
        if isinstance(value, bool) or not isinstance(value, int):
            return _UNSET
        if value < 0 or value > 0xFFFFFFFF:
            return _UNSET
        return int(value)

    def start(self) -> None:
        """Establish a baseline and start polling; repeated calls are no-ops."""

        if self._running:
            return
        self._running = True
        self._generation += 1
        generation = self._generation
        self._baseline = self._read_tick()
        self._schedule(generation)

    def stop(self) -> None:
        """Stop polling and invalidate any callback already queued by Tk."""

        if not self._running and self._after_id is None:
            self._baseline = _UNSET
            return
        self._running = False
        self._generation += 1
        after_id = self._after_id
        self._after_id = None
        self._baseline = _UNSET
        if after_id in {None, _PENDING_AFTER, _SCHEDULED_WITHOUT_ID}:
            return
        cancel = getattr(self._root, "after_cancel", None)
        if callable(cancel):
            try:
                cancel(after_id)
            except Exception:
                pass

    def reset_baseline(self) -> None:
        """Replace the baseline with the current tick without emitting an event."""

        self._baseline = self._read_tick()

    def _schedule(self, generation: int) -> None:
        if not self._running or generation != self._generation:
            return
        if self._after_id is not None:
            return

        self._after_id = _PENDING_AFTER
        try:
            after_id = self._root.after(
                self._poll_interval_ms,
                lambda generation=generation: self._poll(generation),
            )
        except Exception:
            if generation == self._generation:
                self._after_id = None
                self._running = False
                self._generation += 1
            return
        if self._after_id is _PENDING_AFTER:
            self._after_id = (
                after_id if after_id is not None else _SCHEDULED_WITHOUT_ID
            )

    def _poll(self, generation: int) -> None:
        if not self._running or generation != self._generation:
            return
        self._after_id = None

        current_tick = self._read_tick()
        baseline = self._baseline
        if current_tick is not _UNSET:
            self._baseline = current_tick
            if baseline is not _UNSET and current_tick != baseline:
                try:
                    now_value = self._now()
                    self._callback(now_value)
                except Exception:
                    pass
        elif baseline is _UNSET:
            self._baseline = _UNSET

        if self._running and generation == self._generation:
            self._schedule(generation)


LastInputActivityWatcher = WorktimeActivityWatcher
ActivityWatcher = WorktimeActivityWatcher
