from __future__ import annotations

import json
import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.apps.lid_power_policy import (
    LidPowerPolicyController,
    LidPowerPolicyService,
    PowerCapabilities,
    PowerPolicyLeaseManager,
    run_watchdog_mode,
)
from src.utils.windows_power import SYSTEM_POWER_CAPABILITIES


class _FakeLease:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.session_active = False

    def start(self) -> None:
        self.calls.append("start")

    def begin_ac_clamshell_session(self) -> None:
        self.session_active = True
        self.calls.append("begin_session")

    def end_ac_clamshell_session(self) -> None:
        self.session_active = False
        self.calls.append("end_session")

    def reconcile_active_scheme(self) -> None:
        self.calls.append("scheme")

    def shutdown(self) -> None:
        self.calls.append("shutdown")


class _FakePolicyBackend:
    def __init__(self) -> None:
        self.active_scheme = "scheme-a"
        self.values = {
            "scheme-a": {"ac": 1, "dc": 1},
            "scheme-b": {"ac": 2, "dc": 3},
        }
        self.write_log: list[tuple[str, str, int]] = []
        self.fail_write: tuple[str, str] | None = None

    def get_active_scheme(self) -> str:
        return self.active_scheme

    def read_lid_action(self, scheme: str, source: str) -> int:
        return int(self.values[scheme][source])

    def write_lid_action(self, scheme: str, source: str, value: int) -> None:
        if self.fail_write == (scheme, source):
            self.fail_write = None
            raise RuntimeError("write failed")
        self.values[scheme][source] = int(value)
        self.write_log.append((scheme, source, int(value)))

    def activate_scheme(self, scheme: str) -> None:
        self.active_scheme = scheme


class LidPowerPolicyControllerTest(unittest.TestCase):
    def _controller(self):
        lease = _FakeLease()
        sleeps: list[str] = []
        controller = LidPowerPolicyController(
            lease=lease,
            request_sleep=lambda reason: sleeps.append(reason),
            low_battery_percent=15,
        )
        controller.set_enabled(True)
        return controller, lease, sleeps

    def test_initial_hydration_never_becomes_a_transition(self) -> None:
        controller, lease, sleeps = self._controller()
        controller.on_lid_state(False)
        controller.on_power_source("dc")
        self.assertEqual([], sleeps)
        self.assertNotIn("begin_session", lease.calls)

    def test_dc_open_to_closed_requests_sleep_once(self) -> None:
        controller, _lease, sleeps = self._controller()
        controller.on_power_source("dc")
        controller.on_lid_state(True)
        controller.on_lid_state(False)
        controller.on_lid_state(False)
        self.assertEqual(["dc_lid_closed"], sleeps)

    def test_ac_close_starts_session_and_closed_ac_to_dc_stays_awake(self) -> None:
        controller, lease, sleeps = self._controller()
        controller.on_power_source("ac")
        controller.on_lid_state(True)
        controller.on_lid_state(False)
        controller.on_power_source("dc")
        self.assertTrue(controller.snapshot()["ac_clamshell_session"])
        self.assertIn("begin_session", lease.calls)
        self.assertEqual([], sleeps)

    def test_lid_open_resets_session_and_restores_dc_policy(self) -> None:
        controller, lease, _sleeps = self._controller()
        controller.on_power_source("ac")
        controller.on_lid_state(True)
        controller.on_lid_state(False)
        controller.on_lid_state(True)
        self.assertFalse(controller.snapshot()["ac_clamshell_session"])
        self.assertEqual("end_session", lease.calls[-1])

    def test_out_of_order_and_resume_values_rehydrate_without_action(self) -> None:
        controller, lease, sleeps = self._controller()
        controller.on_lid_state(True)
        controller.on_power_source("ac")
        controller.on_lid_state(False)
        self.assertIn("begin_session", lease.calls)
        controller.on_resume()
        controller.on_power_source("dc")
        controller.on_lid_state(False)
        self.assertEqual([], sleeps)
        self.assertFalse(controller.snapshot()["ac_clamshell_session"])

    def test_low_battery_fails_closed_during_dc_clamshell_session(self) -> None:
        controller, lease, sleeps = self._controller()
        controller.on_power_source("ac")
        controller.on_lid_state(True)
        controller.on_lid_state(False)
        controller.on_power_source("dc")
        controller.on_battery_percent(15)
        self.assertEqual(["low_battery"], sleeps)
        self.assertFalse(controller.snapshot()["ac_clamshell_session"])
        self.assertEqual("end_session", lease.calls[-1])

    def test_low_battery_hydrated_before_unplug_also_fails_closed(self) -> None:
        controller, lease, sleeps = self._controller()
        controller.on_battery_percent(10)
        controller.on_power_source("ac")
        controller.on_lid_state(True)
        controller.on_lid_state(False)
        controller.on_power_source("dc")
        self.assertEqual(["low_battery"], sleeps)
        self.assertFalse(controller.snapshot()["ac_clamshell_session"])
        self.assertEqual("end_session", lease.calls[-1])

    def test_rdp_is_not_a_controller_input_or_sleep_condition(self) -> None:
        controller, _lease, sleeps = self._controller()
        self.assertFalse(hasattr(controller, "on_rdp_session_change"))
        controller.on_power_source("dc")
        controller.on_lid_state(True)
        self.assertEqual([], sleeps)


class PowerPolicyLeaseManagerTest(unittest.TestCase):
    def test_preserves_and_restores_original_ac_dc_values(self) -> None:
        backend = _FakePolicyBackend()
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "lease.json"
            lease = PowerPolicyLeaseManager(
                backend=backend,
                journal_path=journal,
                owner_pid=123,
                spawn_watchdog=lambda *_args, **_kwargs: None,
            )
            lease.start()
            self.assertEqual(0, backend.values["scheme-a"]["ac"])
            self.assertEqual(1, backend.values["scheme-a"]["dc"])
            lease.begin_ac_clamshell_session()
            self.assertEqual(0, backend.values["scheme-a"]["dc"])
            lease.end_ac_clamshell_session()
            self.assertEqual(1, backend.values["scheme-a"]["dc"])
            lease.shutdown()
            self.assertEqual({"ac": 1, "dc": 1}, backend.values["scheme-a"])
            self.assertFalse(journal.exists())

    def test_watchdog_fails_closed_after_active_clamshell_owner_exits(self) -> None:
        backend = _FakePolicyBackend()
        backend.values["scheme-a"] = {"ac": 0, "dc": 0}
        backend.request_sleep = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "lease.json"
            journal.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "lease_id": "active-session",
                        "owner_pid": 123,
                        "session_active": True,
                        "active_scheme": "scheme-a",
                        "schemes": {"scheme-a": {"ac": 1, "dc": 1}},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch(
                    "src.apps.lid_power_policy.wait_for_process_exit"
                ) as wait_for_exit,
                patch(
                    "src.apps.lid_power_policy.WindowsPowerPolicyBackend",
                    return_value=backend,
                ),
            ):
                result = run_watchdog_mode(
                    parent_pid=123,
                    journal_path=journal,
                    lease_id="active-session",
                )
            self.assertEqual(0, result)
            wait_for_exit.assert_called_once_with(123)
            backend.request_sleep.assert_called_once_with()
            self.assertEqual({"ac": 1, "dc": 1}, backend.values["scheme-a"])
            self.assertFalse(journal.exists())

    def test_scheme_change_restores_old_scheme_and_manages_new_scheme(self) -> None:
        backend = _FakePolicyBackend()
        with tempfile.TemporaryDirectory() as tmp:
            lease = PowerPolicyLeaseManager(
                backend=backend,
                journal_path=Path(tmp) / "lease.json",
                owner_pid=123,
                spawn_watchdog=lambda *_args, **_kwargs: None,
            )
            lease.start()
            lease.begin_ac_clamshell_session()
            backend.active_scheme = "scheme-b"
            lease.reconcile_active_scheme()
            self.assertEqual({"ac": 1, "dc": 1}, backend.values["scheme-a"])
            self.assertEqual({"ac": 0, "dc": 0}, backend.values["scheme-b"])
            lease.shutdown()
            self.assertEqual({"ac": 2, "dc": 3}, backend.values["scheme-b"])

    def test_failed_write_rolls_back_and_removes_lease(self) -> None:
        backend = _FakePolicyBackend()
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "lease.json"
            lease = PowerPolicyLeaseManager(
                backend=backend,
                journal_path=journal,
                owner_pid=123,
                spawn_watchdog=lambda *_args, **_kwargs: None,
            )
            lease.start()
            backend.fail_write = ("scheme-a", "dc")
            with self.assertRaises(RuntimeError):
                lease.begin_ac_clamshell_session()
            self.assertEqual({"ac": 1, "dc": 1}, backend.values["scheme-a"])
            self.assertFalse(journal.exists())

    def test_stale_journal_is_recovered_after_restart(self) -> None:
        backend = _FakePolicyBackend()
        backend.values["scheme-a"] = {"ac": 0, "dc": 0}
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "lease.json"
            journal.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "lease_id": "stale",
                        "owner_pid": 999999,
                        "session_active": True,
                        "schemes": {"scheme-a": {"ac": 1, "dc": 1}},
                    }
                ),
                encoding="utf-8",
            )
            recovered = PowerPolicyLeaseManager.recover_stale(
                backend=backend,
                journal_path=journal,
                owner_is_alive=lambda _pid: False,
            )
            self.assertTrue(recovered)
            self.assertEqual({"ac": 1, "dc": 1}, backend.values["scheme-a"])
            self.assertFalse(journal.exists())


class LidPowerPolicyServiceTest(unittest.TestCase):
    def test_desktop_ups_and_detection_error_are_complete_noops(self) -> None:
        cases = [
            PowerCapabilities(False, False, False, None),
            PowerCapabilities(True, True, True, None),
            PowerCapabilities(False, False, False, "GetPwrCapabilities failed"),
        ]
        for capabilities in cases:
            with self.subTest(capabilities=capabilities):
                lease = _FakeLease()
                service = LidPowerPolicyService(
                    capabilities=capabilities,
                    lease=lease,
                    request_sleep=lambda _reason: self.fail("must not sleep"),
                    settings_path=None,
                )
                self.assertFalse(service.is_supported)
                self.assertFalse(service.update_enabled(True)[0])
                service.handle_power_setting("lid", 0)
                service.handle_power_setting("acdc", 1)
                self.assertEqual([], lease.calls)

    def test_supported_device_is_opt_in_and_defaults_disabled(self) -> None:
        lease = _FakeLease()
        service = LidPowerPolicyService(
            capabilities=PowerCapabilities(True, True, False, None),
            lease=lease,
            request_sleep=lambda _reason: None,
            settings_path=None,
        )
        self.assertTrue(service.is_supported)
        self.assertFalse(service.get_settings_snapshot()["enabled"])
        ok, error = service.update_enabled(True)
        self.assertTrue(ok, error)
        self.assertIn("start", lease.calls)

    def test_corrupt_settings_fail_closed_to_disabled(self) -> None:
        lease = _FakeLease()
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "lid_power_policy.json"
            settings_path.write_text("{not-json", encoding="utf-8")
            service = LidPowerPolicyService(
                capabilities=PowerCapabilities(True, True, False, None),
                lease=lease,
                request_sleep=lambda _reason: None,
                settings_path=settings_path,
            )
            self.assertFalse(service.get_settings_snapshot()["enabled"])
            service.start()
            self.assertEqual([], lease.calls)

    def test_disabled_observations_hydrate_enable_without_false_transition(self) -> None:
        lease = _FakeLease()
        sleeps: list[str] = []
        service = LidPowerPolicyService(
            capabilities=PowerCapabilities(True, True, False, None),
            lease=lease,
            request_sleep=lambda reason: sleeps.append(reason),
            settings_path=None,
        )
        service.handle_power_setting("acdc", 1)
        service.handle_power_setting("lid", 0)
        self.assertEqual([], lease.calls)
        ok, error = service.update_enabled(True)
        self.assertTrue(ok, error)
        self.assertEqual([], sleeps)
        service.handle_power_setting("lid", 1)
        service.handle_power_setting("lid", 0)
        self.assertEqual(["dc_lid_closed"], sleeps)

    def test_notification_failure_disables_and_restores_policy(self) -> None:
        lease = _FakeLease()
        service = LidPowerPolicyService(
            capabilities=PowerCapabilities(True, True, False, None),
            lease=lease,
            request_sleep=lambda _reason: None,
            settings_path=None,
        )
        self.assertTrue(service.update_enabled(True)[0])
        service.notification_failure("registration failed")
        snapshot = service.get_settings_snapshot()
        self.assertFalse(snapshot["enabled"])
        self.assertFalse(snapshot["runtime_enabled"])
        self.assertIn("shutdown", lease.calls)

    def test_windows_capability_layout_exposes_required_gate_fields(self) -> None:
        self.assertEqual(76, ctypes.sizeof(SYSTEM_POWER_CAPABILITIES))
        self.assertEqual(2, SYSTEM_POWER_CAPABILITIES.LidPresent.offset)
        self.assertEqual(30, SYSTEM_POWER_CAPABILITIES.SystemBatteriesPresent.offset)
        self.assertEqual(31, SYSTEM_POWER_CAPABILITIES.BatteriesAreShortTerm.offset)


if __name__ == "__main__":
    unittest.main()
