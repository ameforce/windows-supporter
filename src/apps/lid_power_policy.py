from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Callable
import uuid

from src.utils.windows_power import (
    LID_ACTION_DO_NOTHING,
    PowerCapabilities,
    PowerNotificationRegistration,
    WindowsPowerPolicyBackend,
    atomic_write_json,
    detect_power_capabilities,
    is_process_alive,
    spawn_policy_watchdog,
    wait_for_process_exit,
)


SETTINGS_SCHEMA_VERSION = 1
LEASE_SCHEMA_VERSION = 1
DEFAULT_LOW_BATTERY_PERCENT = 15


def default_config_dir() -> Path:
    root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    return Path(root or Path.home()) / "windows-supporter"


class PowerPolicyLeaseManager:
    def __init__(
        self,
        *,
        backend,
        journal_path: Path,
        owner_pid: int,
        spawn_watchdog: Callable[[int, Path, str], object],
    ) -> None:
        self._backend = backend
        self._journal_path = Path(journal_path)
        self._owner_pid = int(owner_pid)
        self._spawn_watchdog = spawn_watchdog
        self._lease_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._schemes: dict[str, dict[str, int]] = {}
        self._active_scheme: str | None = None
        self._session_active = False
        self._started = False

    @property
    def session_active(self) -> bool:
        with self._lock:
            return bool(self._session_active)

    def _snapshot(self) -> dict:
        return {
            "schema_version": LEASE_SCHEMA_VERSION,
            "lease_id": self._lease_id,
            "owner_pid": self._owner_pid,
            "session_active": bool(self._session_active),
            "active_scheme": self._active_scheme,
            "schemes": {
                scheme: {"ac": int(values["ac"]), "dc": int(values["dc"])}
                for scheme, values in sorted(self._schemes.items())
            },
        }

    def _persist(self) -> None:
        atomic_write_json(self._journal_path, self._snapshot())

    def _backup_scheme(self, scheme: str) -> None:
        if scheme in self._schemes:
            return
        self._schemes[scheme] = {
            "ac": int(self._backend.read_lid_action(scheme, "ac")),
            "dc": int(self._backend.read_lid_action(scheme, "dc")),
        }

    def _apply_managed_values(self, scheme: str) -> None:
        original = self._schemes[scheme]
        self._backend.write_lid_action(
            scheme,
            "ac",
            LID_ACTION_DO_NOTHING,
        )
        self._backend.write_lid_action(
            scheme,
            "dc",
            LID_ACTION_DO_NOTHING if self._session_active else int(original["dc"]),
        )
        self._backend.activate_scheme(scheme)

    def _restore_all(self) -> None:
        active = None
        try:
            active = self._backend.get_active_scheme()
        except Exception:
            active = self._active_scheme
        first_error: Exception | None = None
        for scheme, values in list(self._schemes.items()):
            for source in ("ac", "dc"):
                try:
                    self._backend.write_lid_action(
                        scheme,
                        source,
                        int(values[source]),
                    )
                except Exception as exc:
                    first_error = first_error or exc
        if active:
            try:
                self._backend.activate_scheme(active)
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
        self._started = False
        self._session_active = False
        self._active_scheme = None
        self._schemes = {}
        self._journal_path.unlink(missing_ok=True)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            scheme = str(self._backend.get_active_scheme())
            self._active_scheme = scheme
            self._backup_scheme(scheme)
            self._session_active = False
            self._persist()
            try:
                self._apply_managed_values(scheme)
                self._persist()
                watchdog = self._spawn_watchdog(
                    self._owner_pid,
                    self._journal_path,
                    self._lease_id,
                )
                poll = getattr(watchdog, "poll", None)
                if callable(poll):
                    time.sleep(0.05)
                    return_code = poll()
                    if return_code is not None:
                        raise RuntimeError(
                            f"lid power watchdog exited early: {int(return_code)}"
                        )
            except Exception:
                try:
                    self._restore_all()
                finally:
                    self._started = False
                raise
            self._started = True

    def begin_ac_clamshell_session(self) -> None:
        with self._lock:
            if not self._started:
                raise RuntimeError("power policy lease is not active")
            self._session_active = True
            self._persist()
            try:
                self.reconcile_active_scheme()
            except Exception:
                self._session_active = False
                try:
                    self._restore_all()
                finally:
                    self._started = False
                raise

    def end_ac_clamshell_session(self) -> None:
        with self._lock:
            if not self._started:
                self._session_active = False
                return
            self._session_active = False
            try:
                self.reconcile_active_scheme()
            except Exception:
                try:
                    self._restore_all()
                finally:
                    self._started = False
                raise

    def reconcile_active_scheme(self) -> None:
        with self._lock:
            if not self._started:
                return
            scheme = str(self._backend.get_active_scheme())
            previous = self._active_scheme
            if previous and previous != scheme and previous in self._schemes:
                values = self._schemes[previous]
                self._backend.write_lid_action(previous, "ac", int(values["ac"]))
                self._backend.write_lid_action(previous, "dc", int(values["dc"]))
            self._active_scheme = scheme
            self._backup_scheme(scheme)
            self._persist()
            self._apply_managed_values(scheme)
            self._persist()

    def shutdown(self) -> None:
        with self._lock:
            if not self._started and not self._schemes:
                return
            self._restore_all()

    @staticmethod
    def _read_journal(journal_path: Path) -> dict:
        value = json.loads(Path(journal_path).read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != LEASE_SCHEMA_VERSION
            or not isinstance(value.get("lease_id"), str)
            or not isinstance(value.get("owner_pid"), int)
            or not isinstance(value.get("schemes"), dict)
        ):
            raise ValueError("invalid lid power lease journal")
        for scheme, values in value["schemes"].items():
            if (
                not isinstance(scheme, str)
                or not isinstance(values, dict)
                or set(values) != {"ac", "dc"}
                or any(
                    not isinstance(values.get(source), int)
                    for source in ("ac", "dc")
                )
            ):
                raise ValueError("invalid lid power lease scheme backup")
        return value

    @classmethod
    def recover_stale(
        cls,
        *,
        backend,
        journal_path: Path,
        owner_is_alive: Callable[[int], bool] = is_process_alive,
        expected_lease_id: str | None = None,
    ) -> bool:
        path = Path(journal_path)
        if not path.is_file():
            return False
        value = cls._read_journal(path)
        if expected_lease_id is not None and value["lease_id"] != expected_lease_id:
            return False
        if owner_is_alive(int(value["owner_pid"])):
            return False
        active = None
        try:
            active = backend.get_active_scheme()
        except Exception:
            active = value.get("active_scheme")
        first_error: Exception | None = None
        for scheme, values in value["schemes"].items():
            for source in ("ac", "dc"):
                try:
                    backend.write_lid_action(scheme, source, int(values[source]))
                except Exception as exc:
                    first_error = first_error or exc
        if active:
            try:
                backend.activate_scheme(str(active))
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
        path.unlink(missing_ok=True)
        return True


class LidPowerPolicyController:
    def __init__(
        self,
        *,
        lease,
        request_sleep: Callable[[str], None],
        low_battery_percent: int = DEFAULT_LOW_BATTERY_PERCENT,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._lease = lease
        self._request_sleep = request_sleep
        self._low_battery_percent = int(low_battery_percent)
        self._on_failure = on_failure
        self._lock = threading.RLock()
        self._enabled = False
        self._lid_open: bool | None = None
        self._power_source: str | None = None
        self._battery_percent: int | None = None
        self._ac_clamshell_session = False

    def _fail(self, exc: Exception) -> None:
        self._enabled = False
        self._ac_clamshell_session = False
        try:
            self._lease.shutdown()
        except Exception:
            pass
        if self._on_failure is not None:
            self._on_failure(f"{type(exc).__name__}: {exc}")

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            value = bool(enabled)
            if not value and self._enabled and self._ac_clamshell_session:
                try:
                    self._lease.end_ac_clamshell_session()
                except Exception as exc:
                    self._fail(exc)
                    return
            self._enabled = value
            self._lid_open = None
            self._power_source = None
            self._battery_percent = None
            self._ac_clamshell_session = False

    def on_lid_state(self, is_open: bool) -> None:
        with self._lock:
            if not self._enabled:
                return
            value = bool(is_open)
            previous = self._lid_open
            self._lid_open = value
            if previous is None or previous == value:
                return
            try:
                if value:
                    if self._ac_clamshell_session:
                        self._lease.end_ac_clamshell_session()
                    self._ac_clamshell_session = False
                    return
                if self._power_source == "dc":
                    self._request_sleep("dc_lid_closed")
                elif self._power_source == "ac":
                    self._lease.begin_ac_clamshell_session()
                    self._ac_clamshell_session = True
            except Exception as exc:
                self._fail(exc)

    def on_power_source(self, source: str) -> None:
        with self._lock:
            if not self._enabled:
                return
            normalized = str(source or "").strip().lower()
            if normalized not in {"ac", "dc", "short_term"}:
                return
            previous = self._power_source
            self._power_source = normalized
            if previous is None or previous == normalized:
                return
            if (
                normalized == "dc"
                and self._ac_clamshell_session
                and self._lid_open is False
                and self._battery_percent is not None
                and self._battery_percent <= self._low_battery_percent
            ):
                try:
                    self._lease.end_ac_clamshell_session()
                    self._ac_clamshell_session = False
                    self._request_sleep("low_battery")
                except Exception as exc:
                    self._fail(exc)
                return
            if normalized == "short_term":
                try:
                    if self._ac_clamshell_session:
                        self._lease.end_ac_clamshell_session()
                    self._ac_clamshell_session = False
                except Exception as exc:
                    self._fail(exc)

    def on_battery_percent(self, percent: int) -> None:
        with self._lock:
            if not self._enabled:
                return
            value = max(0, min(100, int(percent)))
            self._battery_percent = value
            if not (
                self._ac_clamshell_session
                and self._lid_open is False
                and self._power_source == "dc"
                and value <= self._low_battery_percent
            ):
                return
            try:
                self._lease.end_ac_clamshell_session()
                self._ac_clamshell_session = False
                self._request_sleep("low_battery")
            except Exception as exc:
                self._fail(exc)

    def on_active_scheme_changed(self) -> None:
        with self._lock:
            if not self._enabled:
                return
            try:
                self._lease.reconcile_active_scheme()
            except Exception as exc:
                self._fail(exc)

    def on_resume(self) -> None:
        with self._lock:
            if not self._enabled:
                return
            try:
                if self._ac_clamshell_session:
                    self._lease.end_ac_clamshell_session()
            except Exception as exc:
                self._fail(exc)
                return
            self._lid_open = None
            self._power_source = None
            self._battery_percent = None
            self._ac_clamshell_session = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": bool(self._enabled),
                "lid_open": self._lid_open,
                "power_source": self._power_source,
                "battery_percent": self._battery_percent,
                "ac_clamshell_session": bool(self._ac_clamshell_session),
                "low_battery_percent": int(self._low_battery_percent),
            }


class LidPowerPolicyService:
    def __init__(
        self,
        *,
        capabilities: PowerCapabilities,
        lease,
        request_sleep: Callable[[str], None],
        settings_path: Path | None,
        low_battery_percent: int = DEFAULT_LOW_BATTERY_PERCENT,
    ) -> None:
        self.capabilities = capabilities
        self._lease = lease
        self._settings_path = Path(settings_path) if settings_path is not None else None
        self._settings_enabled = self._load_enabled()
        self._lock = threading.RLock()
        self._runtime_enabled = False
        self._last_error: str | None = None
        self._observed_lid: int | None = None
        self._observed_acdc: int | None = None
        self._observed_battery: int | None = None
        self._controller = LidPowerPolicyController(
            lease=lease,
            request_sleep=request_sleep,
            low_battery_percent=low_battery_percent,
            on_failure=self._on_controller_failure,
        )

    @property
    def is_supported(self) -> bool:
        return bool(self.capabilities.eligible)

    @classmethod
    def create_default(
        cls,
        *,
        exclusive_instance: bool = False,
    ) -> "LidPowerPolicyService":
        config_dir = default_config_dir()
        settings_path = config_dir / "lid_power_policy.json"
        journal_path = config_dir / "lid_power_policy_lease.json"
        capabilities = detect_power_capabilities()
        backend = None
        recovery_error = None
        try:
            backend = WindowsPowerPolicyBackend()
            if journal_path.is_file():
                PowerPolicyLeaseManager.recover_stale(
                    backend=backend,
                    journal_path=journal_path,
                    owner_is_alive=(
                        (lambda _pid: False)
                        if exclusive_instance
                        else is_process_alive
                    ),
                )
        except Exception as exc:
            recovery_error = f"stale_policy_recovery_failed: {type(exc).__name__}: {exc}"
        if recovery_error is not None:
            capabilities = PowerCapabilities(
                capabilities.lid_present,
                capabilities.system_batteries_present,
                capabilities.batteries_are_short_term,
                recovery_error,
            )
        if capabilities.eligible and backend is not None:
            lease = PowerPolicyLeaseManager(
                backend=backend,
                journal_path=journal_path,
                owner_pid=os.getpid(),
                spawn_watchdog=spawn_policy_watchdog,
            )
            request_sleep = lambda _reason: backend.request_sleep()
        else:
            lease = _NoopLease()
            request_sleep = lambda _reason: None
        return cls(
            capabilities=capabilities,
            lease=lease,
            request_sleep=request_sleep,
            settings_path=settings_path,
        )

    def _load_enabled(self) -> bool:
        path = self._settings_path
        if path is None or not path.is_file():
            return False
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return bool(
            isinstance(value, dict)
            and value.get("schema_version") == SETTINGS_SCHEMA_VERSION
            and value.get("enabled") is True
        )

    def _save_enabled(self, enabled: bool) -> None:
        if self._settings_path is None:
            return
        atomic_write_json(
            self._settings_path,
            {
                "schema_version": SETTINGS_SCHEMA_VERSION,
                "enabled": bool(enabled),
            },
        )

    def _on_controller_failure(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message)
            self._runtime_enabled = False
            self._settings_enabled = False
            try:
                self._save_enabled(False)
            except Exception:
                pass

    def start(self) -> None:
        if self._settings_enabled and self.is_supported:
            self.update_enabled(True)

    def update_enabled(self, enabled: bool) -> tuple[bool, str | None]:
        with self._lock:
            value = bool(enabled)
            if value and not self.is_supported:
                return False, self.capabilities.reason
            try:
                if value:
                    if not self._runtime_enabled:
                        self._lease.start()
                        self._controller.set_enabled(True)
                        self._runtime_enabled = True
                        source = self._observed_power_source()
                        if source is not None:
                            self._controller.on_power_source(source)
                        if self._observed_lid is not None:
                            self._controller.on_lid_state(bool(self._observed_lid))
                        if self._observed_battery is not None:
                            self._controller.on_battery_percent(
                                int(self._observed_battery)
                            )
                else:
                    self._controller.set_enabled(False)
                    self._lease.shutdown()
                    self._runtime_enabled = False
                self._settings_enabled = value
                self._last_error = None
                self._save_enabled(value)
                return True, None
            except Exception as exc:
                try:
                    self._controller.set_enabled(False)
                    self._lease.shutdown()
                except Exception:
                    pass
                self._runtime_enabled = False
                self._settings_enabled = False
                self._last_error = f"{type(exc).__name__}: {exc}"
                try:
                    self._save_enabled(False)
                except Exception:
                    pass
                return False, self._last_error

    def handle_power_setting(self, kind: str, value: object) -> None:
        if not self.is_supported:
            return
        with self._lock:
            try:
                if kind == "lid" and value in {0, 1}:
                    self._observed_lid = int(value)
                    if not self._runtime_enabled:
                        return
                    self._controller.on_lid_state(bool(int(value)))
                elif kind == "acdc" and value in {0, 1, 2}:
                    self._observed_acdc = int(value)
                    if not self._runtime_enabled:
                        return
                    source = self._observed_power_source()
                    if source is not None:
                        self._controller.on_power_source(source)
                elif kind == "battery" and value is not None:
                    self._observed_battery = int(value)
                    if not self._runtime_enabled:
                        return
                    self._controller.on_battery_percent(int(value))
                elif kind == "scheme":
                    if not self._runtime_enabled:
                        return
                    self._controller.on_active_scheme_changed()
            except Exception as exc:
                self._on_controller_failure(f"{type(exc).__name__}: {exc}")

    def on_resume(self) -> None:
        with self._lock:
            self._observed_lid = None
            self._observed_acdc = None
            self._observed_battery = None
            self._controller.on_resume()

    def _observed_power_source(self) -> str | None:
        if self._observed_acdc is None:
            return None
        return {0: "ac", 1: "dc", 2: "short_term"}.get(
            int(self._observed_acdc)
        )

    def notification_failure(self, message: str) -> None:
        self._on_controller_failure(str(message or "notification registration failed"))
        try:
            self._controller.set_enabled(False)
        except Exception:
            pass
        try:
            self._lease.shutdown()
        except Exception:
            pass

    def get_settings_snapshot(self) -> dict:
        with self._lock:
            snapshot = self._controller.snapshot()
            snapshot.update(
                {
                    "supported": self.is_supported,
                    "support_reason": self.capabilities.reason,
                    "enabled": bool(self._settings_enabled),
                    "runtime_enabled": bool(self._runtime_enabled),
                    "last_error": self._last_error,
                }
            )
            return snapshot

    def shutdown(self) -> None:
        with self._lock:
            try:
                self._controller.set_enabled(False)
            finally:
                try:
                    self._lease.shutdown()
                finally:
                    self._runtime_enabled = False


class _NoopLease:
    def start(self) -> None:
        return

    def begin_ac_clamshell_session(self) -> None:
        return

    def end_ac_clamshell_session(self) -> None:
        return

    def reconcile_active_scheme(self) -> None:
        return

    def shutdown(self) -> None:
        return


def run_watchdog_mode(
    *,
    parent_pid: int,
    journal_path: Path,
    lease_id: str,
) -> int:
    wait_for_process_exit(int(parent_pid))
    path = Path(journal_path)
    if not path.is_file():
        return 0
    journal = PowerPolicyLeaseManager._read_journal(path)
    if journal["lease_id"] != str(lease_id):
        return 0
    fail_closed_sleep = bool(journal.get("session_active"))
    backend = WindowsPowerPolicyBackend()
    recovery_error: Exception | None = None
    try:
        PowerPolicyLeaseManager.recover_stale(
            backend=backend,
            journal_path=path,
            owner_is_alive=lambda _pid: False,
            expected_lease_id=str(lease_id),
        )
    except Exception as exc:
        recovery_error = exc
    if fail_closed_sleep:
        try:
            backend.request_sleep()
        except Exception:
            if recovery_error is None:
                raise
    if recovery_error is not None:
        raise recovery_error
    return 0


def run_runtime_canary(output_path: Path, *, exercise_policy: bool = False) -> int:
    capabilities = detect_power_capabilities()
    result = {
        "schema_version": 1,
        "capabilities": {
            "lid_present": capabilities.lid_present,
            "system_batteries_present": capabilities.system_batteries_present,
            "batteries_are_short_term": capabilities.batteries_are_short_term,
            "eligible": capabilities.eligible,
            "reason": capabilities.reason,
        },
        "registered_notification_count": 0,
        "policy_before": None,
        "policy_after": None,
        "policy_during": None,
        "policy_unchanged": True,
        "policy_write_exercised": False,
        "lease_journal_removed": True,
        "error": None,
    }
    hwnd = None
    class_name = f"WindowsSupporterLidPowerCanary_{os.getpid()}"
    registration = PowerNotificationRegistration(lambda _kind, _value: None)
    try:
        if capabilities.eligible:
            import win32api
            import win32con
            import win32gui

            backend = WindowsPowerPolicyBackend()
            scheme = backend.get_active_scheme()
            result["policy_before"] = {
                "scheme": scheme,
                "ac": backend.read_lid_action(scheme, "ac"),
                "dc": backend.read_lid_action(scheme, "dc"),
            }
            if exercise_policy:
                journal_path = Path(output_path).with_suffix(".lease.json")
                lease = PowerPolicyLeaseManager(
                    backend=backend,
                    journal_path=journal_path,
                    owner_pid=os.getpid(),
                    spawn_watchdog=lambda *_args, **_kwargs: None,
                )
                try:
                    lease.start()
                    result["policy_during"] = {
                        "scheme": backend.get_active_scheme(),
                        "ac": backend.read_lid_action(scheme, "ac"),
                        "dc": backend.read_lid_action(scheme, "dc"),
                    }
                    result["policy_write_exercised"] = True
                finally:
                    lease.shutdown()
                result["lease_journal_removed"] = not journal_path.exists()
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = class_name
            wc.lpfnWndProc = {
                getattr(win32con, "WM_POWERBROADCAST", 0x0218): (
                    lambda _hwnd, _msg, _wparam, _lparam: 1
                )
            }
            atom = win32gui.RegisterClass(wc)
            hwnd = win32gui.CreateWindow(
                atom,
                class_name,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                wc.hInstance,
                None,
            )
            registration.register(int(hwnd))
            result["registered_notification_count"] = registration.registered_count
            registration.unregister()
            result["policy_after"] = {
                "scheme": backend.get_active_scheme(),
                "ac": backend.read_lid_action(scheme, "ac"),
                "dc": backend.read_lid_action(scheme, "dc"),
            }
            result["policy_unchanged"] = (
                result["policy_before"] == result["policy_after"]
            )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            registration.unregister()
        except Exception:
            pass
        if hwnd:
            try:
                import win32gui

                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
        atomic_write_json(Path(output_path), result)
    if not capabilities.eligible:
        return 2
    return (
        0
        if result["error"] is None
        and result["registered_notification_count"] == 4
        and result["policy_unchanged"] is True
        and (
            not exercise_policy
            or (
                result["policy_write_exercised"] is True
                and result["lease_journal_removed"] is True
            )
        )
        else 1
    )


def run_lid_power_special_mode(argv: list[str]) -> int | None:
    values = list(argv)
    if "--lid-power-watchdog" in values:
        try:
            parent_pid = int(values[values.index("--parent-pid") + 1])
            journal = Path(values[values.index("--journal") + 1])
            lease_id = values[values.index("--lease-id") + 1]
        except (ValueError, IndexError):
            return 64
        return run_watchdog_mode(
            parent_pid=parent_pid,
            journal_path=journal,
            lease_id=lease_id,
        )
    if "--lid-power-runtime-canary" in values:
        try:
            output_path = Path(values[values.index("--output") + 1])
        except (ValueError, IndexError):
            return 64
        return run_runtime_canary(
            output_path,
            exercise_policy="--exercise-policy" in values,
        )
    return None
