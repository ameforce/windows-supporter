import json
import os
import queue
import tempfile
import threading
import time
import unittest

from src.apps.codex_usage_multi_monitor import CodexUsageMultiMonitor


class _FakeChildMonitor:
    def __init__(self, config_dir: str, profile_dir: str) -> None:
        self.config_dir = config_dir
        self.profile_dir = profile_dir
        self.update_calls: list[dict] = []
        self.show_calls: list[dict] = []
        self.release_calls = 0
        self.shutdown_calls = 0
        self.attach_calls = []
        self.tooltip_duration_ms = 7000
        self.runtime = {
            "enabled": True,
            "monitor_state": "idle",
            "session_state": "logged_out",
            "collect_inflight": False,
            "auto_monitoring_active": True,
            "can_login": True,
            "can_logout": False,
            "usage_history": [],
        }
        self.last_snapshot = {
            "five_hour_limit": "",
            "weekly_limit": "",
            "gpt_5_3_codex_spark_five_hour_limit": "",
            "gpt_5_3_codex_spark_weekly_limit": "",
            "remaining_credit": "",
            "captured_at": "",
        }

    def get_settings_snapshot(self):
        return {
            "enabled": bool(self.runtime.get("enabled", True)),
            "interval_sec": 90.0,
            "tooltip_duration_ms": int(self.tooltip_duration_ms),
            "usage_url": "https://chatgpt.com/codex/cloud/settings/analytics#usage",
            "collection_mode": "playwright",
            "settings_path": os.path.join(self.config_dir, "codex_usage_settings.json"),
            "state_path": os.path.join(self.config_dir, "codex_usage_state.json"),
            "profile_dir": self.profile_dir,
        }

    def update_settings(self, data):
        self.update_calls.append(dict(data))
        self.runtime["enabled"] = bool(data.get("enabled", self.runtime["enabled"]))
        if "tooltip_duration_ms" in data:
            self.tooltip_duration_ms = int(data["tooltip_duration_ms"])
        return True, None

    def get_runtime_status(self):
        return dict(self.runtime)

    def get_last_snapshot(self):
        return dict(self.last_snapshot)

    def show_current_status(self, force_refresh=True, source="manual_query"):
        self.show_calls.append({"force_refresh": bool(force_refresh), "source": str(source)})
        return None

    def release_profile_session(self):
        self.release_calls += 1
        return True, "released"

    def attach(self, root, event_queue=None, start_monitor=True):
        self.attach_calls.append(
            {
                "root": root,
                "event_queue": event_queue,
                "start_monitor": bool(start_monitor),
            }
        )
        return None

    def shutdown(self):
        self.shutdown_calls += 1
        return None


class _BlockingChildMonitor(_FakeChildMonitor):
    def __init__(self, config_dir: str, profile_dir: str) -> None:
        super().__init__(config_dir, profile_dir)
        self.started = threading.Event()
        self.release = threading.Event()

    def show_current_status(self, force_refresh=True, source="manual_query"):
        self.started.set()
        self.release.wait(2.0)
        return super().show_current_status(force_refresh=force_refresh, source=source)


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls = []
        self.after_cancel_calls = []

    def after(self, delay_ms, callback):
        after_id = f"after-{len(self.after_calls) + 1}"
        self.after_calls.append(
            {
                "id": after_id,
                "delay_ms": int(delay_ms),
                "callback": callback,
            }
        )
        return after_id

    def after_cancel(self, after_id):
        self.after_cancel_calls.append(after_id)


class _FakeTaskbarOverlay:
    instances = []

    def __init__(self, root, runtime_getter):
        self.root = root
        self.runtime_getter = runtime_getter
        self.refresh_calls = 0
        self.hide_calls = 0
        self.invalidate_geometry_calls = 0
        self.runtime_snapshots = []
        self.instances.append(self)

    def refresh(self):
        self.refresh_calls += 1
        self.runtime_snapshots.append(self.runtime_getter())
        return True

    def hide(self):
        self.hide_calls += 1
        return None

    def invalidate_geometry(self):
        self.invalidate_geometry_calls += 1
        return None


class CodexUsageMultiMonitorUnitTest(unittest.TestCase):
    def _build_manager(self, tmp: str, taskbar_progress_factory=None):
        children: list[_FakeChildMonitor] = []

        def factory(config_dir: str, profile_dir: str):
            child = _FakeChildMonitor(config_dir=config_dir, profile_dir=profile_dir)
            children.append(child)
            return child

        manager = CodexUsageMultiMonitor(
            config_dir=os.path.join(tmp, "config"),
            local_base_dir=os.path.join(tmp, "local"),
            monitor_factory=factory,
            taskbar_progress_factory=taskbar_progress_factory,
        )
        return manager, children

    def _wait_until(self, predicate, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_shutdown_cancels_scheduler_and_shuts_down_every_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            root = _FakeRoot()
            manager.attach(root)
            scheduled_id = "after-owned"
            manager._CodexUsageMultiMonitor__monitor_after_id = scheduled_id

            manager.shutdown()

            self.assertIn(scheduled_id, root.after_cancel_calls)
            self.assertEqual([child.shutdown_calls for child in children], [1, 1])
            self.assertIsNone(manager._CodexUsageMultiMonitor__monitor_after_id)

    def test_settings_snapshot_creates_two_isolated_accounts_and_migrates_legacy_files_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = os.path.join(tmp, "config")
            os.makedirs(config_dir, exist_ok=True)
            legacy_settings = os.path.join(config_dir, "codex_usage_settings.json")
            legacy_state = os.path.join(config_dir, "codex_usage_state.json")
            with open(legacy_settings, "w", encoding="utf-8") as fp:
                json.dump({"enabled": False, "interval_sec": 33.0}, fp)
            with open(legacy_state, "w", encoding="utf-8") as fp:
                json.dump({"session_state": "logged_in"}, fp)

            manager, children = self._build_manager(tmp)
            snapshot = manager.get_settings_snapshot()

            self.assertEqual(snapshot["settings_version"], 2)
            self.assertEqual(snapshot["default_account_id"], "account_1")
            self.assertEqual([a["id"] for a in snapshot["accounts"]], ["account_1", "account_2"])
            self.assertEqual([a["label"] for a in snapshot["accounts"]], ["Codex 1", "Codex 2"])
            self.assertNotEqual(
                snapshot["accounts"][0]["profile_dir"],
                snapshot["accounts"][1]["profile_dir"],
            )
            self.assertEqual(len(children), 2)

            account_1_settings = snapshot["accounts"][0]["settings_path"]
            account_1_state = snapshot["accounts"][0]["state_path"]
            account_2_settings = snapshot["accounts"][1]["settings_path"]
            self.assertTrue(os.path.isfile(account_1_settings))
            self.assertTrue(os.path.isfile(account_1_state))
            self.assertFalse(os.path.exists(account_2_settings))
            self.assertTrue(os.path.isfile(legacy_settings))
            self.assertTrue(os.path.isfile(legacy_state))

            with open(legacy_settings, "w", encoding="utf-8") as fp:
                json.dump({"enabled": True, "interval_sec": 999.0}, fp)
            manager_again, _ = self._build_manager(tmp)
            again = manager_again.get_settings_snapshot()

            with open(again["accounts"][0]["settings_path"], encoding="utf-8") as fp:
                copied = json.load(fp)
            self.assertEqual(copied["interval_sec"], 33.0)

    def test_runtime_snapshot_aggregates_enabled_accounts_and_routes_account_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime.update(
                {"session_state": "logged_out", "can_login": True, "can_logout": False}
            )
            children[1].runtime.update(
                {"session_state": "logged_in", "can_login": False, "can_logout": True}
            )

            runtime = manager.get_runtime_status()

            self.assertEqual(runtime["session_state"], "mixed")
            self.assertTrue(runtime["can_login"])
            self.assertTrue(runtime["can_logout"])

            ok, error = manager.update_settings(
                {
                    "accounts": [
                        {"id": "account_1", "enabled": False},
                        {"id": "account_2", "enabled": True},
                    ]
                }
            )
            self.assertTrue(ok, error)
            runtime = manager.get_runtime_status()

            self.assertEqual(runtime["accounts"][0]["enabled"], False)
            self.assertEqual(runtime["session_state"], "logged_in")
            self.assertFalse(runtime["can_login"])
            self.assertTrue(runtime["can_logout"])

            manager.login_account("account_1")
            ok, message = manager.release_account_profile_session("account_2")

            self.assertEqual(children[0].show_calls, [{"force_refresh": True, "source": "manual_login"}])
            self.assertTrue(ok, message)
            self.assertEqual(children[1].release_calls, 1)
            self.assertFalse(hasattr(manager, "release_profile_session"))

    def test_runtime_snapshot_prefers_profile_name_for_account_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime["profile_name"] = "Daeng"

            runtime = manager.get_runtime_status()

            self.assertEqual(runtime["accounts"][0]["label"], "Daeng")
            self.assertEqual(runtime["accounts"][0]["configured_label"], "Codex 1")

    def test_logged_out_accounts_with_profile_cdp_do_not_drive_background_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            for child in children:
                child.runtime.update(
                    {
                        "session_state": "logged_out",
                        "collect_inflight": False,
                        "profile_session_present": True,
                        "profile_cdp_available": True,
                        "can_login": True,
                        "can_logout": False,
                    }
                )
            root = _FakeRoot()

            manager.attach(root)
            runtime = manager.get_runtime_status()

            self.assertFalse(runtime["auto_monitoring_active"])
            self.assertEqual(root.after_calls, [])

    def test_account_order_round_trips_and_changes_default_without_moving_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            before = manager.get_settings_snapshot()
            profile_by_id = {
                account["id"]: account["profile_dir"]
                for account in before["accounts"]
            }

            ok, error = manager.update_settings(
                {
                    "default_account_id": "account_2",
                    "accounts": [
                        {"id": "account_2", "label": "Second", "enabled": True},
                        {"id": "account_1", "label": "First", "enabled": True},
                    ],
                }
            )

            self.assertTrue(ok, error)
            snapshot = manager.get_settings_snapshot()
            self.assertEqual(snapshot["default_account_id"], "account_2")
            self.assertEqual([a["id"] for a in snapshot["accounts"]], ["account_2", "account_1"])
            self.assertEqual(
                {account["id"]: account["profile_dir"] for account in snapshot["accounts"]},
                profile_by_id,
            )

            manager.show_current_status(source="manual_login")
            self.assertEqual(children[1].show_calls, [{"force_refresh": True, "source": "manual_login"}])

            manager_again, _ = self._build_manager(tmp)
            again = manager_again.get_settings_snapshot()
            self.assertEqual(again["default_account_id"], "account_2")
            self.assertEqual([a["id"] for a in again["accounts"]], ["account_2", "account_1"])

    def test_tooltip_duration_round_trips_and_propagates_to_child_monitors(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)

            ok, error = manager.update_settings({"tooltip_duration_ms": 4321})
            snapshot = manager.get_settings_snapshot()
            manager_again, _ = self._build_manager(tmp)

            self.assertTrue(ok, error)
            self.assertEqual(snapshot["tooltip_duration_ms"], 4321)
            self.assertEqual(manager_again.get_settings_snapshot()["tooltip_duration_ms"], 4321)
            self.assertEqual(
                [child.update_calls[-1]["tooltip_duration_ms"] for child in children],
                [4321, 4321],
            )

    def test_taskbar_overlay_enabled_round_trips_and_controls_overlay_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            _FakeTaskbarOverlay.instances = []
            manager, _children = self._build_manager(
                tmp,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            manager.attach(object(), event_queue=None)

            self.assertTrue(manager.get_settings_snapshot()["taskbar_overlay_enabled"])
            self.assertTrue(manager.get_runtime_status()["taskbar_overlay_enabled"])

            ok, error = manager.update_settings({"taskbar_overlay_enabled": False})
            self.assertTrue(ok, error)
            self.assertFalse(manager.get_settings_snapshot()["taskbar_overlay_enabled"])
            self.assertEqual(len(_FakeTaskbarOverlay.instances), 1)
            self.assertEqual(_FakeTaskbarOverlay.instances[0].hide_calls, 1)

            manager_again, _children_again = self._build_manager(
                tmp,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            self.assertFalse(manager_again.get_settings_snapshot()["taskbar_overlay_enabled"])

            ok, error = manager_again.update_settings({"taskbar_overlay_enabled": True})
            self.assertTrue(ok, error)
            manager_again.attach(object(), event_queue=None)
            self.assertEqual(len(_FakeTaskbarOverlay.instances), 2)
            self.assertEqual(_FakeTaskbarOverlay.instances[-1].refresh_calls, 1)

    def test_show_current_status_refreshes_enabled_accounts_in_fixed_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            manager.update_settings(
                {
                    "accounts": [
                        {"id": "account_1", "enabled": True},
                        {"id": "account_2", "enabled": False},
                    ]
                }
            )

            manager.show_current_status(force_refresh=True)

            self.assertEqual(children[0].show_calls, [{"force_refresh": True, "source": "manual_query"}])
            self.assertEqual(children[1].show_calls, [])

    def test_runtime_entry_exposes_account_level_usage_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime["usage_history"] = [
                {"captured_at": "2026-06-01T10:00:00+09:00", "five_hour_limit": "80%"}
            ]
            children[1].runtime["usage_history"] = [
                {"captured_at": "2026-06-01T10:00:00+09:00", "five_hour_limit": "60%"}
            ]

            runtime = manager.get_runtime_status()

            self.assertEqual(
                runtime["accounts"][0]["usage_history"],
                children[0].runtime["usage_history"],
            )
            self.assertEqual(
                runtime["accounts"][1]["usage_history"],
                children[1].runtime["usage_history"],
            )
            self.assertIsNot(
                runtime["accounts"][0]["usage_history"],
                children[0].runtime["usage_history"],
            )
            self.assertIsNot(
                runtime["accounts"][0]["usage_history"][0],
                children[0].runtime["usage_history"][0],
            )

    def test_attached_show_current_status_dispatches_refresh_without_blocking_ui_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            children: list[_BlockingChildMonitor] = []

            def factory(config_dir: str, profile_dir: str):
                child = _BlockingChildMonitor(config_dir=config_dir, profile_dir=profile_dir)
                child.runtime["session_state"] = "logged_in"
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            event_queue = queue.Queue()
            manager.attach(object(), event_queue=event_queue)

            call_finished = threading.Event()
            caller = threading.Thread(
                target=lambda: (manager.show_current_status(force_refresh=True), call_finished.set()),
                daemon=True,
            )
            caller.start()

            try:
                self.assertTrue(call_finished.wait(0.25))
                manager.show_current_status(force_refresh=True)
                self.assertTrue(children[0].started.wait(1.0))
                self.assertTrue(manager.get_runtime_status()["collect_inflight"])
                self.assertEqual([child.show_calls for child in children], [[], []])
            finally:
                for child in children:
                    child.release.set()
                caller.join(1.0)

            self.assertTrue(
                self._wait_until(lambda: all(len(child.show_calls) == 1 for child in children))
            )
            self.assertEqual(
                [child.show_calls for child in children],
                [
                    [{"force_refresh": True, "source": "manual_query"}],
                    [{"force_refresh": True, "source": "manual_query"}],
                ],
            )
            self.assertTrue(self._wait_until(lambda: not manager.get_runtime_status()["collect_inflight"]))

    def test_attached_show_account_status_dispatches_single_account_without_blocking_ui_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            children: list[_BlockingChildMonitor] = []

            def factory(config_dir: str, profile_dir: str):
                child = _BlockingChildMonitor(config_dir=config_dir, profile_dir=profile_dir)
                child.runtime["session_state"] = "logged_in"
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            manager.attach(object(), event_queue=queue.Queue())
            call_finished = threading.Event()
            caller = threading.Thread(
                target=lambda: (
                    manager.show_account_status("account_1", force_refresh=True),
                    call_finished.set(),
                ),
                daemon=True,
            )
            caller.start()

            try:
                self.assertTrue(call_finished.wait(0.25))
                self.assertTrue(children[0].started.wait(1.0))
                self.assertFalse(children[1].started.is_set())
                self.assertEqual([child.show_calls for child in children], [[], []])
            finally:
                for child in children:
                    child.release.set()
                caller.join(1.0)

            self.assertTrue(self._wait_until(lambda: len(children[0].show_calls) == 1))
            self.assertEqual(
                [child.show_calls for child in children],
                [[{"force_refresh": True, "source": "manual_query"}], []],
            )
            self.assertTrue(self._wait_until(lambda: not manager.get_runtime_status()["collect_inflight"]))

    def test_manual_login_account_request_is_not_dropped_while_refresh_is_inflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            children: list[_BlockingChildMonitor] = []

            def factory(config_dir: str, profile_dir: str):
                child = _BlockingChildMonitor(config_dir=config_dir, profile_dir=profile_dir)
                child.runtime["session_state"] = "logged_out"
                child.runtime["monitor_state"] = "running"
                child.runtime["collect_inflight"] = True
                child.runtime["can_login"] = False
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            manager.attach(object(), event_queue=queue.Queue())
            caller = threading.Thread(
                target=lambda: manager.show_account_status("account_2", force_refresh=True),
                daemon=True,
            )
            caller.start()

            try:
                self.assertTrue(children[1].started.wait(1.0))
                manager.login_account("account_1")
                self.assertTrue(children[0].started.wait(1.0))
            finally:
                for child in children:
                    child.release.set()
                caller.join(1.0)

            self.assertTrue(
                self._wait_until(
                    lambda: any(call["source"] == "manual_login" for call in children[0].show_calls)
                )
            )
            self.assertEqual(
                children[0].show_calls[-1],
                {"force_refresh": True, "source": "manual_login"},
            )

    def test_attach_gives_children_ui_context_without_starting_independent_monitoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            root = object()
            event_queue = object()

            manager.attach(root, event_queue)

            self.assertEqual(
                [child.attach_calls for child in children],
                [
                    [{"root": root, "event_queue": event_queue, "start_monitor": False}],
                    [{"root": root, "event_queue": event_queue, "start_monitor": False}],
                ],
            )

    def test_attach_starts_manager_level_periodic_monitoring_for_logged_in_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime["session_state"] = "logged_in"
            children[1].runtime["session_state"] = "logged_out"
            root = _FakeRoot()

            manager.attach(root, event_queue=None)

            self.assertEqual(len(root.after_calls), 1)
            self.assertLessEqual(root.after_calls[0]["delay_ms"], 1000)

            root.after_calls[0]["callback"]()

            self.assertEqual(
                children[0].show_calls,
                [{"force_refresh": True, "source": "auto_monitor"}],
            )
            self.assertEqual(children[1].show_calls, [])
            self.assertGreaterEqual(len(root.after_calls), 2)

    def test_background_monitor_skips_paused_auth_accounts_without_stopping_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime["session_state"] = "logged_in"
            children[0].runtime["monitor_state"] = "paused_auth_required"
            children[0].runtime["auth_attention_required"] = True
            children[1].runtime["session_state"] = "logged_out"
            root = _FakeRoot()

            manager.attach(root, event_queue=None)

            self.assertEqual(len(root.after_calls), 1)
            root.after_calls[0]["callback"]()

            self.assertEqual(children[0].show_calls, [])
            self.assertEqual(children[1].show_calls, [])
            self.assertGreaterEqual(len(root.after_calls), 2)

    def test_attach_does_not_revalidate_logged_out_account_when_profile_session_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime["session_state"] = "logged_out"
            children[0].runtime["profile_session_present"] = True
            children[1].runtime["session_state"] = "logged_out"
            root = _FakeRoot()

            manager.attach(root, event_queue=None)

            self.assertEqual(root.after_calls, [])
            self.assertEqual(children[0].show_calls, [])
            self.assertEqual(children[1].show_calls, [])

    def test_attach_does_not_mask_child_attach_type_errors(self):
        class BuggyAttachChild(_FakeChildMonitor):
            def attach(self, root, event_queue=None, **kwargs):
                if "start_monitor" in kwargs:
                    raise TypeError("internal attach failure")
                return super().attach(root, event_queue)

        with tempfile.TemporaryDirectory() as tmp:
            children = []

            def factory(config_dir: str, profile_dir: str):
                child = BuggyAttachChild(config_dir=config_dir, profile_dir=profile_dir)
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )

            with self.assertRaisesRegex(TypeError, "internal attach failure"):
                manager.attach(object(), event_queue=None)

    def test_partial_v2_state_still_migrates_legacy_account_1_files_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = os.path.join(tmp, "config")
            os.makedirs(config_dir, exist_ok=True)
            with open(os.path.join(config_dir, "codex_usage_multi_state.json"), "w", encoding="utf-8") as fp:
                json.dump({"account_expanded": {"account_2": True}}, fp)
            with open(os.path.join(config_dir, "codex_usage_settings.json"), "w", encoding="utf-8") as fp:
                json.dump({"enabled": False, "interval_sec": 33.0}, fp)
            with open(os.path.join(config_dir, "codex_usage_state.json"), "w", encoding="utf-8") as fp:
                json.dump({"session_state": "logged_in"}, fp)

            manager, _ = self._build_manager(tmp)
            snapshot = manager.get_settings_snapshot()

            self.assertFalse(snapshot["enabled"])
            self.assertEqual(snapshot["interval_sec"], 33.0)
            self.assertTrue(os.path.isfile(snapshot["accounts"][0]["settings_path"]))
            self.assertTrue(os.path.isfile(snapshot["accounts"][0]["state_path"]))
            self.assertFalse(os.path.exists(snapshot["accounts"][1]["settings_path"]))

    def test_attach_updates_taskbar_progress_without_querying_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _FakeTaskbarOverlay.instances = []
            root = object()
            manager, children = self._build_manager(
                tmp,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )

            manager.attach(root, event_queue=None)

            self.assertEqual(len(_FakeTaskbarOverlay.instances), 1)
            self.assertIs(_FakeTaskbarOverlay.instances[0].root, root)
            self.assertEqual(_FakeTaskbarOverlay.instances[0].refresh_calls, 1)
            self.assertEqual([child.show_calls for child in children], [[], []])

    def test_display_topology_change_refreshes_taskbar_overlay_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            _FakeTaskbarOverlay.instances = []
            manager, _children = self._build_manager(
                tmp,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            manager.attach(object(), event_queue=None)

            manager.on_display_topology_changed("display_change")

            self.assertEqual(len(_FakeTaskbarOverlay.instances), 1)
            self.assertEqual(_FakeTaskbarOverlay.instances[0].invalidate_geometry_calls, 1)
            self.assertEqual(_FakeTaskbarOverlay.instances[0].refresh_calls, 2)

    def test_update_settings_refreshes_taskbar_overlay_after_disabling_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            _FakeTaskbarOverlay.instances = []
            manager, _children = self._build_manager(
                tmp,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            manager.attach(object(), event_queue=None)

            ok, error = manager.update_settings({"enabled": False})

            self.assertTrue(ok, error)
            self.assertEqual(len(_FakeTaskbarOverlay.instances), 1)
            self.assertEqual(_FakeTaskbarOverlay.instances[0].refresh_calls, 2)
            self.assertFalse(
                _FakeTaskbarOverlay.instances[0].runtime_snapshots[-1]["enabled"]
            )

    def test_show_current_status_updates_taskbar_progress_after_refreshing_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _FakeTaskbarOverlay.instances = []
            root = object()
            manager, children = self._build_manager(
                tmp,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            manager.attach(root, event_queue=None)

            manager.show_current_status(force_refresh=True)

            self.assertEqual(len(_FakeTaskbarOverlay.instances), 1)
            self.assertGreaterEqual(_FakeTaskbarOverlay.instances[0].refresh_calls, 2)
            self.assertEqual(
                [child.show_calls for child in children],
                [
                    [{"force_refresh": True, "source": "manual_query"}],
                    [{"force_refresh": True, "source": "manual_query"}],
                ],
            )

    def test_manual_login_status_updates_taskbar_progress_and_routes_default_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            _FakeTaskbarOverlay.instances = []
            manager, children = self._build_manager(
                tmp,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            manager.attach(object(), event_queue=None)

            manager.show_current_status(force_refresh=True, source="manual_login")

            self.assertEqual(len(_FakeTaskbarOverlay.instances), 1)
            self.assertGreaterEqual(_FakeTaskbarOverlay.instances[0].refresh_calls, 2)
            self.assertEqual(children[0].show_calls, [{"force_refresh": True, "source": "manual_login"}])
            self.assertEqual(children[1].show_calls, [])


if __name__ == "__main__":
    unittest.main()
