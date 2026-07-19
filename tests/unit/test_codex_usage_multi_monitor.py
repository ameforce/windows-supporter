import json
import os
import queue
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.apps.codex_usage_multi_monitor import CodexUsageMultiMonitor, _RecoveryPendingChild


class _FakeChildMonitor:
    def __init__(self, config_dir: str, profile_dir: str) -> None:
        self.config_dir = config_dir
        self.profile_dir = profile_dir
        self.update_calls: list[dict] = []
        self.show_calls: list[dict] = []
        self.release_calls = 0
        self.cancel_calls = 0
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

    def request_collect_cancel(self):
        self.cancel_calls += 1
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

    def test_background_settings_mutation_posts_scheduler_restart_to_ui_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _children = self._build_manager(tmp)
            root = _FakeRoot()
            event_queue = queue.Queue()
            manager.attach(root, event_queue=event_queue)
            root.after_calls.clear()
            root.after_cancel_calls.clear()
            manager._CodexUsageMultiMonitor__monitor_after_id = "after-existing"
            finished = threading.Event()

            worker = threading.Thread(
                target=lambda: (manager.toggle_enabled(), finished.set()),
                daemon=True,
            )
            worker.start()

            self.assertTrue(finished.wait(1.0))
            self.assertEqual(root.after_calls, [])
            self.assertEqual(root.after_cancel_calls, [])
            self.assertGreaterEqual(event_queue.qsize(), 1)

            while not event_queue.empty():
                event_queue.get_nowait()()

            self.assertIn("after-existing", root.after_cancel_calls)

    def test_ui_thread_settings_mutation_restarts_scheduler_without_requeueing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _children = self._build_manager(tmp)
            root = _FakeRoot()
            event_queue = queue.Queue()
            manager.attach(root, event_queue=event_queue)
            while not event_queue.empty():
                event_queue.get_nowait()()
            root.after_calls.clear()
            root.after_cancel_calls.clear()
            manager._CodexUsageMultiMonitor__monitor_after_id = "after-ui"

            ok, error = manager.update_settings({"enabled": False})

            self.assertTrue(ok, error)
            self.assertEqual(root.after_cancel_calls, ["after-ui"])
            self.assertEqual(event_queue.qsize(), 1)

    def test_queued_ui_callbacks_do_not_resurrect_taskbar_or_scheduler_after_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _FakeTaskbarOverlay.instances = []
            manager, children = self._build_manager(
                tmp,
                taskbar_progress_factory=_FakeTaskbarOverlay,
            )
            for child in children:
                child.runtime["session_state"] = "logged_in"
            root = _FakeRoot()
            event_queue = queue.Queue()
            manager.attach(root, event_queue=event_queue)
            restart_thread = threading.Thread(
                target=lambda: manager._CodexUsageMultiMonitor__request_monitor_scheduler_restart(
                    initial_delay_sec=5.0
                )
            )
            restart_thread.start()
            restart_thread.join(1.0)

            self.assertFalse(restart_thread.is_alive())
            self.assertGreaterEqual(event_queue.qsize(), 2)
            manager.shutdown()
            self.assertEqual(
                manager._CodexUsageMultiMonitor__profile_next_collect_due_ts,
                {},
            )

            while not event_queue.empty():
                event_queue.get_nowait()()

            self.assertEqual(_FakeTaskbarOverlay.instances, [])
            self.assertEqual(
                manager._CodexUsageMultiMonitor__profile_next_collect_due_ts,
                {},
            )
            self.assertIsNone(manager._CodexUsageMultiMonitor__monitor_after_id)

    def test_manager_preserves_date_only_reset_before_default_child_formatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].format_reset_at_for_display = (
                lambda value, key="": f"{value} 00:00:00 (invented by {key})"
            )

            rendered = manager.format_reset_at_for_display(
                "2026-08-13",
                key="billing_reset_at",
            )

            self.assertEqual(rendered, "2026-08-13")

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

            self.assertEqual(snapshot["settings_version"], 4)
            self.assertEqual(snapshot["profile_order"], ["account_1", "account_2"])
            self.assertEqual(snapshot["selected_profile_ids"], ["account_1", "account_2"])
            self.assertEqual(
                [profile["provider"] for profile in snapshot["profiles"]],
                ["codex", "codex"],
            )
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

    def test_v4_restart_does_not_recreate_deleted_legacy_account_1_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = os.path.join(tmp, "config")
            os.makedirs(config_dir, exist_ok=True)
            with open(
                os.path.join(config_dir, "codex_usage_settings.json"),
                "w",
                encoding="utf-8",
            ) as fp:
                json.dump({"enabled": True}, fp)

            manager, _ = self._build_manager(tmp)
            account_1 = next(
                item
                for item in manager.get_settings_snapshot()["profiles"]
                if item["id"] == "account_1"
            )
            self.assertTrue(os.path.isfile(account_1["settings_path"]))

            ok, error = manager.delete_profile("account_1", confirmed=True)

            self.assertTrue(ok, error)
            self.assertFalse(os.path.exists(account_1["config_dir"]))
            restarted, _ = self._build_manager(tmp)
            self.assertNotIn(
                "account_1",
                restarted.get_settings_snapshot()["profile_order"],
            )
            self.assertFalse(os.path.exists(account_1["config_dir"]))

    def test_v4_recovery_conflict_does_not_copy_legacy_files_into_original_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = os.path.join(tmp, "config")
            os.makedirs(config_dir, exist_ok=True)
            original = os.path.join(config_dir, "codex-account-1")
            quarantine = f"{original}.delete-{'b' * 32}"
            os.makedirs(original, exist_ok=True)
            os.makedirs(quarantine, exist_ok=True)
            with open(
                os.path.join(config_dir, "codex_usage_settings.json"),
                "w",
                encoding="utf-8",
            ) as fp:
                json.dump({"enabled": True}, fp)
            with open(
                os.path.join(config_dir, "ai_usage_settings.json"),
                "w",
                encoding="utf-8",
            ) as fp:
                json.dump(
                    {
                        "settings_version": 4,
                        "profiles": [
                            {
                                "id": "account_1",
                                "provider": "codex",
                                "enabled": True,
                                "taskbar_selected": True,
                            }
                        ],
                        "profile_order": ["account_1"],
                        "selected_profile_ids": ["account_1"],
                        "default_account_id": "account_1",
                    },
                    fp,
                )
            with open(
                os.path.join(config_dir, "ai_usage_cleanup_state.json"),
                "w",
                encoding="utf-8",
            ) as fp:
                json.dump(
                    {
                        "schema_version": 1,
                        "paths": [
                            {
                                "transaction_id": "a" * 32,
                                "profile_id": "account_1",
                                "provider": "codex",
                                "path_kind": "config",
                                "original": original,
                                "path": quarantine,
                                "root": config_dir,
                            }
                        ],
                    },
                    fp,
                )

            manager, _ = self._build_manager(tmp)

            runtime = manager.get_runtime_status()["profiles"][0]["runtime"]
            self.assertEqual(runtime["monitor_state"], "recovery_pending")
            self.assertFalse(
                os.path.exists(os.path.join(original, "codex_usage_settings.json"))
            )

    def test_v2_manager_settings_migrate_atomically_to_v4_and_keep_rollback_files_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = os.path.join(tmp, "config")
            os.makedirs(config_dir, exist_ok=True)
            legacy_manager_path = os.path.join(config_dir, "codex_usage_multi_settings.json")
            with open(legacy_manager_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "settings_version": 2,
                        "enabled": True,
                        "account_order": ["account_2", "account_1"],
                        "default_account_id": "account_2",
                        "accounts": [
                            {"id": "account_2", "label": "기존 B", "enabled": False},
                            {"id": "account_1", "label": "기존 A", "enabled": True},
                        ],
                    },
                    fp,
                    ensure_ascii=False,
                )

            manager, _ = self._build_manager(tmp)
            snapshot = manager.get_settings_snapshot()

            self.assertEqual(snapshot["settings_version"], 4)
            self.assertEqual(snapshot["profile_order"], ["account_2", "account_1"])
            self.assertEqual(snapshot["selected_profile_ids"], ["account_1"])
            self.assertEqual(
                [(item["id"], item["provider"], item["label"]) for item in snapshot["profiles"]],
                [("account_2", "codex", "기존 B"), ("account_1", "codex", "기존 A")],
            )
            ai_settings_path = os.path.join(config_dir, "ai_usage_settings.json")
            self.assertTrue(os.path.isfile(ai_settings_path))
            with open(ai_settings_path, encoding="utf-8") as fp:
                persisted = json.load(fp)
            self.assertEqual(persisted["settings_version"], 4)

            with open(legacy_manager_path, encoding="utf-8") as fp:
                rollback_before = json.load(fp)

            ok, error = manager.update_settings(
                {
                    "profiles": [
                        {
                            "id": "account_2",
                            "provider": "cursor",
                            "label": "Cursor 개인",
                            "enabled": True,
                            "taskbar_selected": True,
                        },
                        {
                            "id": "account_1",
                            "provider": "codex",
                            "label": "새 Codex",
                            "enabled": True,
                            "taskbar_selected": True,
                        },
                    ]
                }
            )
            self.assertTrue(ok, error)
            with open(legacy_manager_path, encoding="utf-8") as fp:
                rollback = json.load(fp)
            self.assertEqual(rollback, rollback_before)
            self.assertTrue(os.path.isfile(os.path.join(config_dir, "codex_usage_multi_settings.v2.backup.json")))

            manager_again, _ = self._build_manager(tmp)
            self.assertEqual(manager_again.get_settings_snapshot()["profiles"][0]["provider"], "cursor")

    def test_profiles_allow_all_provider_combinations_and_more_than_two_saved_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            for providers in (
                ("codex", "codex"),
                ("codex", "cursor"),
                ("cursor", "codex"),
                ("cursor", "cursor"),
            ):
                manager, _ = self._build_manager(os.path.join(tmp, "-".join(providers)))
                ok, error = manager.update_settings(
                    {
                        "profiles": [
                            {
                                "id": "account_1",
                                "provider": providers[0],
                                "enabled": True,
                                "taskbar_selected": True,
                            },
                            {
                                "id": "account_2",
                                "provider": providers[1],
                                "enabled": True,
                                "taskbar_selected": True,
                            },
                        ]
                    }
                )
                self.assertTrue(ok, error)
                snapshot = manager.get_settings_snapshot()
                self.assertEqual([item["provider"] for item in snapshot["profiles"]], list(providers))
                self.assertEqual(len(snapshot["selected_profile_ids"]), 2)

            manager, _ = self._build_manager(os.path.join(tmp, "dynamic"))
            ok, error = manager.update_settings(
                {
                    "profiles": [
                        {"id": "account_1", "provider": "codex", "taskbar_selected": True},
                        {"id": "account_2", "provider": "cursor", "taskbar_selected": True},
                    ]
                }
            )
            self.assertTrue(ok, error)
            ok, error, created = manager.add_profile("cursor")
            self.assertTrue(ok, error)
            dynamic_profile_id = created["id"]
            self.assertEqual(len(manager.get_settings_snapshot()["profiles"]), 3)

            before = manager.get_settings_snapshot()

            ok, error = manager.update_settings(
                {
                    "selected_profile_ids": [
                        "account_1",
                        "account_2",
                        dynamic_profile_id,
                    ]
                }
            )
            self.assertFalse(ok)
            self.assertEqual(error, "taskbar_profile_limit")
            self.assertEqual(manager.get_settings_snapshot(), before)

    def test_update_settings_rejects_unregistered_profile_id_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            before = manager.get_settings_snapshot()

            ok, error = manager.update_settings(
                {
                    "profiles": [
                        *before["profiles"],
                        {
                            "id": "profile_00000000000000000000000000000003",
                            "provider": "cursor",
                        },
                    ]
                }
            )

            self.assertFalse(ok)
            self.assertEqual(error, "invalid_profile")
            self.assertEqual(manager.get_settings_snapshot(), before)

    def test_partial_profiles_payload_preserves_omitted_taskbar_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("cursor")
            self.assertTrue(ok, error)
            hidden_profile_id = created["id"]
            ok, error = manager.update_settings(
                {"selected_profile_ids": ["account_1", hidden_profile_id]}
            )
            self.assertTrue(ok, error)
            partial_profiles = [
                dict(item)
                for item in manager.get_settings_snapshot()["profiles"]
                if item["id"] in {"account_1", "account_2"}
            ]

            ok, error = manager.update_settings(
                {
                    "profiles": partial_profiles,
                    "selected_profile_ids": ["account_1"],
                    "tooltip_duration_ms": 8000,
                }
            )

            self.assertTrue(ok, error)
            self.assertEqual(
                manager.get_settings_snapshot()["selected_profile_ids"],
                ["account_1", hidden_profile_id],
            )

    def test_update_settings_save_failure_restores_provider_scalars_and_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            before = manager.get_settings_snapshot()
            profiles = [dict(item) for item in before["profiles"]]
            profiles[0]["provider"] = "cursor"

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=OSError("disk full"),
            ):
                ok, error = manager.update_settings(
                    {
                        "profiles": profiles,
                        "enabled": False,
                        "interval_sec": 123,
                    }
                )

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertEqual(manager.get_settings_snapshot(), before)
            child = manager._CodexUsageMultiMonitor__children["account_1"]
            self.assertIn("codex-account-1", child.config_dir)

    def test_provider_factory_failure_keeps_original_child_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            fail_factory = False
            children = []

            def factory(config_dir, profile_dir):
                if fail_factory:
                    raise RuntimeError("factory unavailable")
                child = _FakeChildMonitor(config_dir, profile_dir)
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            original = manager._CodexUsageMultiMonitor__children["account_1"]
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"
            fail_factory = True

            ok, error = manager.update_settings({"profiles": profiles})

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertIs(
                manager._CodexUsageMultiMonitor__children["account_1"],
                original,
            )
            self.assertEqual(original.shutdown_calls, 0)

    def test_multi_provider_factory_failure_keeps_every_original_child_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory_calls = 0
            created = []

            def factory(provider, config_dir, profile_dir):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls == 4:
                    raise RuntimeError("second replacement unavailable")
                child = _FakeChildMonitor(config_dir, profile_dir)
                child.provider = provider
                created.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            original_1 = manager._CodexUsageMultiMonitor__children["account_1"]
            original_2 = manager._CodexUsageMultiMonitor__children["account_2"]
            profiles = manager.get_settings_snapshot()["profiles"]
            for profile in profiles:
                profile["provider"] = "cursor"

            ok, error = manager.update_settings({"profiles": profiles})

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertIs(
                manager._CodexUsageMultiMonitor__children["account_1"],
                original_1,
            )
            self.assertIs(
                manager._CodexUsageMultiMonitor__children["account_2"],
                original_2,
            )
            self.assertEqual(original_1.shutdown_calls, 0)
            self.assertEqual(original_2.shutdown_calls, 0)
            self.assertEqual(created[2].shutdown_calls, 1)

    def test_staged_provider_child_settings_failure_keeps_original_transaction(self):
        for failure_mode in ("raise", "reject"):
            with self.subTest(failure_mode=failure_mode), tempfile.TemporaryDirectory() as tmp:
                factory_calls = 0
                staged_children = []

                class _FailingSettingsChild(_FakeChildMonitor):
                    def update_settings(self, data):
                        self.update_calls.append(dict(data))
                        if failure_mode == "raise":
                            raise OSError("child settings unavailable")
                        return False, "child_settings_failed"

                def factory(provider, config_dir, profile_dir):
                    nonlocal factory_calls
                    factory_calls += 1
                    if factory_calls <= 2:
                        return _FakeChildMonitor(config_dir, profile_dir)
                    child = _FailingSettingsChild(config_dir, profile_dir)
                    staged_children.append(child)
                    return child

                manager = CodexUsageMultiMonitor(
                    config_dir=os.path.join(tmp, "config"),
                    local_base_dir=os.path.join(tmp, "local"),
                    monitor_factory=factory,
                )
                original = manager._CodexUsageMultiMonitor__children["account_1"]
                profiles = manager.get_settings_snapshot()["profiles"]
                profiles[0]["provider"] = "cursor"

                ok, error = manager.update_settings({"profiles": profiles})

                self.assertFalse(ok)
                self.assertEqual(error, "settings_save_failed")
                self.assertIs(
                    manager._CodexUsageMultiMonitor__children["account_1"],
                    original,
                )
                self.assertEqual(original.shutdown_calls, 0)
                self.assertEqual(staged_children[0].shutdown_calls, 1)
                with open(
                    os.path.join(tmp, "config", "ai_usage_settings.json"),
                    encoding="utf-8",
                ) as fp:
                    persisted = json.load(fp)
                self.assertEqual(persisted["profiles"][0]["provider"], "codex")

    def test_staged_attach_and_shutdown_failure_tracks_child_and_keeps_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory_calls = 0

            class _UnsettledStagedChild(_FakeChildMonitor):
                def attach(self, root, event_queue=None, start_monitor=True):
                    raise RuntimeError("attach failed after resource start")

                def shutdown(self):
                    raise RuntimeError("resource still alive")

            def factory(provider, config_dir, profile_dir):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls <= 2:
                    return _FakeChildMonitor(config_dir, profile_dir)
                return _UnsettledStagedChild(config_dir, profile_dir)

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            manager.attach(_FakeRoot())
            original = manager._CodexUsageMultiMonitor__children["account_1"]
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"

            ok, error = manager.update_settings({"profiles": profiles})

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertIs(manager._CodexUsageMultiMonitor__children["account_1"], original)
            self.assertEqual(
                len(manager._CodexUsageMultiMonitor__unsettled_children["account_1"]),
                1,
            )
            with open(
                os.path.join(tmp, "config", "ai_usage_settings_recovery.json"),
                encoding="utf-8",
            ) as fp:
                self.assertIn("account_1", json.load(fp)["profile_ids"])

    def test_provider_save_failure_keeps_original_without_rollback_factory(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory_calls = 0

            def factory(config_dir, profile_dir):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls >= 4:
                    raise RuntimeError("rollback factory unavailable")
                return _FakeChildMonitor(config_dir, profile_dir)

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            original = manager._CodexUsageMultiMonitor__children["account_1"]
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=OSError("disk full"),
            ):
                ok, error = manager.update_settings({"profiles": profiles})

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertIs(
                manager._CodexUsageMultiMonitor__children["account_1"],
                original,
            )
            self.assertEqual(original.shutdown_calls, 0)
            self.assertEqual(factory_calls, 3)

    def test_failed_rollback_shutdown_is_tracked_and_keeps_recovery_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory_calls = 0

            class _OriginalChild(_FakeChildMonitor):
                def update_settings(self, data):
                    self.update_calls.append(dict(data))
                    if len(self.update_calls) >= 2 and bool(data.get("enabled")):
                        return False, "rollback_rejected"
                    self.runtime["enabled"] = bool(data.get("enabled", True))
                    return True, None

                def shutdown(self):
                    raise RuntimeError("original still alive")

            def factory(config_dir, profile_dir):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls == 1:
                    return _OriginalChild(config_dir, profile_dir)
                return _FakeChildMonitor(config_dir, profile_dir)

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=OSError("disk full"),
            ):
                ok, error = manager.update_settings({"enabled": False})

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertIsInstance(
                manager._CodexUsageMultiMonitor__children["account_1"],
                _RecoveryPendingChild,
            )
            self.assertEqual(
                len(manager._CodexUsageMultiMonitor__unsettled_children["account_1"]),
                1,
            )
            with open(
                os.path.join(tmp, "config", "ai_usage_settings_recovery.json"),
                encoding="utf-8",
            ) as fp:
                self.assertIn("account_1", json.load(fp)["profile_ids"])

    def test_provider_switch_shutdown_failure_publishes_recovery_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory_calls = 0

            class _UnstoppableCodexChild(_FakeChildMonitor):
                def shutdown(self):
                    raise RuntimeError("codex child still alive")

            def factory(provider, config_dir, profile_dir):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls == 1:
                    return _UnstoppableCodexChild(config_dir, profile_dir)
                child = _FakeChildMonitor(config_dir, profile_dir)
                child.provider = provider
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"

            ok, error = manager.update_settings({"profiles": profiles})

            self.assertTrue(ok, error)
            self.assertEqual(manager.get_settings_snapshot()["profiles"][0]["provider"], "cursor")
            self.assertIsInstance(
                manager._CodexUsageMultiMonitor__children["account_1"],
                _RecoveryPendingChild,
            )
            self.assertEqual(
                len(manager._CodexUsageMultiMonitor__unsettled_children["account_1"]),
                1,
            )
            with open(
                os.path.join(tmp, "config", "ai_usage_settings_recovery.json"),
                encoding="utf-8",
            ) as fp:
                self.assertIn("account_1", json.load(fp)["profile_ids"])

    def test_save_failure_replaces_child_when_canonical_rollback_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            children = []

            class _RollbackRejectingChild(_FakeChildMonitor):
                def update_settings(self, data):
                    self.update_calls.append(dict(data))
                    if len(self.update_calls) >= 2 and bool(data.get("enabled")):
                        return False, "rollback_rejected"
                    self.runtime["enabled"] = bool(data.get("enabled", True))
                    return True, None

            def factory(config_dir, profile_dir):
                child = _RollbackRejectingChild(config_dir, profile_dir)
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            original = manager._CodexUsageMultiMonitor__children["account_1"]

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=OSError("disk full"),
            ):
                ok, error = manager.update_settings({"enabled": False})

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertTrue(manager.get_settings_snapshot()["enabled"])
            live = manager._CodexUsageMultiMonitor__children["account_1"]
            self.assertIsNot(live, original)
            self.assertTrue(live.get_runtime_status()["enabled"])
            self.assertEqual(original.shutdown_calls, 1)

    def test_add_profile_child_settings_rejection_rolls_back_model_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory_calls = 0
            staged_children = []

            class _RejectingSettingsChild(_FakeChildMonitor):
                def update_settings(self, data):
                    self.update_calls.append(dict(data))
                    return False, "persist_failed"

            def factory(config_dir, profile_dir):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls <= 2:
                    return _FakeChildMonitor(config_dir, profile_dir)
                os.makedirs(config_dir, exist_ok=True)
                with open(
                    os.path.join(config_dir, "codex_usage_settings.json"),
                    "w",
                    encoding="utf-8",
                ) as fp:
                    json.dump({"enabled": True}, fp)
                child = _RejectingSettingsChild(config_dir, profile_dir)
                staged_children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )

            ok, error, profile = manager.add_profile("codex")

            self.assertFalse(ok)
            self.assertEqual(error, "profile_add_failed")
            self.assertIsNone(profile)
            self.assertEqual(len(manager.get_settings_snapshot()["profiles"]), 2)
            with open(
                os.path.join(tmp, "config", "ai_usage_settings.json"),
                encoding="utf-8",
            ) as fp:
                persisted = json.load(fp)
            self.assertEqual(len(persisted["profiles"]), 2)
            self.assertEqual(staged_children[0].shutdown_calls, 1)
            self.assertFalse(os.path.exists(os.path.dirname(staged_children[0].config_dir)))

    def test_failed_canonical_replacement_is_journaled_and_retried_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            account_1_instances = 0
            reject_replacement = True

            class _PersistentSettingsChild(_FakeChildMonitor):
                def __init__(self, config_dir, profile_dir, *, replacement=False):
                    super().__init__(config_dir, profile_dir)
                    self.replacement = replacement
                    settings_path = os.path.join(config_dir, "codex_usage_settings.json")
                    if os.path.isfile(settings_path):
                        with open(settings_path, encoding="utf-8") as fp:
                            self.runtime["enabled"] = bool(json.load(fp).get("enabled", True))

                def update_settings(self, data):
                    self.update_calls.append(dict(data))
                    enabled = bool(data.get("enabled", True))
                    if enabled and (
                        (not self.replacement and len(self.update_calls) >= 2)
                        or (self.replacement and reject_replacement)
                    ):
                        return False, "rollback_rejected"
                    self.runtime["enabled"] = enabled
                    os.makedirs(self.config_dir, exist_ok=True)
                    with open(
                        os.path.join(self.config_dir, "codex_usage_settings.json"),
                        "w",
                        encoding="utf-8",
                    ) as fp:
                        json.dump({"enabled": enabled}, fp)
                    return True, None

            def factory(config_dir, profile_dir):
                nonlocal account_1_instances
                if config_dir.endswith("codex-account-1"):
                    account_1_instances += 1
                    return _PersistentSettingsChild(
                        config_dir,
                        profile_dir,
                        replacement=account_1_instances > 1,
                    )
                return _FakeChildMonitor(config_dir, profile_dir)

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=OSError("disk full"),
            ):
                ok, error = manager.update_settings({"enabled": False})

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertEqual(
                manager._CodexUsageMultiMonitor__children[
                    "account_1"
                ].get_runtime_status()["monitor_state"],
                "recovery_pending",
            )
            recovery_path = os.path.join(tmp, "config", "ai_usage_settings_recovery.json")
            with open(recovery_path, encoding="utf-8") as fp:
                self.assertEqual(json.load(fp)["profile_ids"], ["account_1"])

            reject_replacement = False
            restarted = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )

            self.assertTrue(restarted.get_settings_snapshot()["enabled"])
            self.assertTrue(
                restarted._CodexUsageMultiMonitor__children[
                    "account_1"
                ].get_runtime_status()["enabled"]
            )
            self.assertFalse(os.path.exists(recovery_path))

    def test_settings_recovery_wal_failure_aborts_before_child_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            child = manager._CodexUsageMultiMonitor__children["account_1"]

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__persist_settings_recovery_profile_ids",
                side_effect=OSError("recovery wal unavailable"),
            ):
                ok, error = manager.update_settings({"enabled": False})

            self.assertFalse(ok)
            self.assertEqual(error, "settings_save_failed")
            self.assertTrue(manager.get_settings_snapshot()["enabled"])
            self.assertEqual(child.update_calls, [])

    def test_cleanup_recovery_does_not_consume_settings_recovery_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            recovery_path = os.path.join(tmp, "config", "ai_usage_settings_recovery.json")
            manager._CodexUsageMultiMonitor__settings_recovery_profile_ids = {"account_1"}
            manager._CodexUsageMultiMonitor__persist_settings_recovery_profile_ids()
            paths = manager._CodexUsageMultiMonitor__account_paths["account_1"]
            manager._CodexUsageMultiMonitor__recovery_pending_profile_ids = {"account_1"}
            manager._CodexUsageMultiMonitor__children["account_1"] = _RecoveryPendingChild(paths)

            manager._CodexUsageMultiMonitor__retry_pending_settings_recovery()

            self.assertTrue(os.path.isfile(recovery_path))
            self.assertEqual(
                manager._CodexUsageMultiMonitor__settings_recovery_profile_ids,
                {"account_1"},
            )

            manager._CodexUsageMultiMonitor__retry_pending_profile_cleanup()

            self.assertFalse(os.path.exists(recovery_path))
            self.assertNotIsInstance(
                manager._CodexUsageMultiMonitor__children["account_1"],
                _RecoveryPendingChild,
            )

    def test_cleanup_recovery_tracks_unsettled_child_and_blocks_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            class _UnstoppableChild(_FakeChildMonitor):
                def shutdown(self):
                    raise RuntimeError("still alive")

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=lambda config_dir, profile_dir: _UnstoppableChild(
                    config_dir,
                    profile_dir,
                ),
            )
            profile = manager.get_settings_snapshot()["profiles"][0]
            for path in (profile["config_dir"], profile["profile_dir"]):
                os.makedirs(path, exist_ok=True)
                with open(os.path.join(path, "keep.txt"), "w", encoding="utf-8") as fp:
                    fp.write("keep")

            manager._CodexUsageMultiMonitor__mark_profile_recovery_pending("account_1")
            ok, error = manager.delete_profile("account_1", confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            self.assertIsInstance(
                manager._CodexUsageMultiMonitor__children["account_1"],
                _RecoveryPendingChild,
            )
            self.assertEqual(
                len(manager._CodexUsageMultiMonitor__unsettled_children["account_1"]),
                1,
            )
            self.assertTrue(os.path.isfile(os.path.join(profile["config_dir"], "keep.txt")))
            self.assertTrue(os.path.isfile(os.path.join(profile["profile_dir"], "keep.txt")))
            with open(
                os.path.join(tmp, "config", "ai_usage_settings_recovery.json"),
                encoding="utf-8",
            ) as fp:
                self.assertIn("account_1", json.load(fp)["profile_ids"])

    def test_failed_add_shutdown_keeps_paths_for_durable_restart_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory_calls = 0
            failed_children = []

            class _RejectingLiveChild(_FakeChildMonitor):
                fail_shutdown = True

                def update_settings(self, data):
                    os.makedirs(self.config_dir, exist_ok=True)
                    os.makedirs(self.profile_dir, exist_ok=True)
                    for path in (self.config_dir, self.profile_dir):
                        with open(os.path.join(path, "keep.txt"), "w", encoding="utf-8") as fp:
                            fp.write("keep")
                    return False, "settings_rejected"

                def shutdown(self):
                    if self.fail_shutdown:
                        raise RuntimeError("still alive")
                    return super().shutdown()

            def factory(config_dir, profile_dir):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls <= 2:
                    return _FakeChildMonitor(config_dir, profile_dir)
                child = _RejectingLiveChild(config_dir, profile_dir)
                failed_children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )

            ok, error, profile = manager.add_profile("codex")

            self.assertFalse(ok)
            self.assertEqual(error, "profile_add_failed")
            self.assertIsNone(profile)
            child = failed_children[0]
            self.assertTrue(os.path.isdir(child.config_dir))
            self.assertTrue(os.path.isdir(child.profile_dir))
            self.assertTrue(
                os.path.isfile(os.path.join(tmp, "config", "ai_usage_cleanup_state.json"))
            )
            manager._CodexUsageMultiMonitor__retry_pending_profile_cleanup()
            self.assertTrue(os.path.isdir(child.config_dir))
            self.assertTrue(os.path.isdir(child.profile_dir))
            child.fail_shutdown = False
            manager.shutdown()

            CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=lambda config_dir, profile_dir: _FakeChildMonitor(
                    config_dir,
                    profile_dir,
                ),
            )

            self.assertFalse(os.path.exists(child.config_dir))
            self.assertFalse(os.path.exists(child.profile_dir))
            self.assertFalse(
                os.path.exists(os.path.join(tmp, "config", "ai_usage_cleanup_state.json"))
            )

    def test_add_crash_recovery_preserves_preexisting_opaque_profile_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            fixed_hex = "a" * 32
            profile_id = f"profile_{fixed_hex}"
            planned = manager._CodexUsageMultiMonitor__build_profile_paths(profile_id, "codex")
            for path in (planned.config_dir, planned.profile_dir):
                os.makedirs(path, exist_ok=True)
                with open(os.path.join(path, "preexisting.txt"), "w", encoding="utf-8") as fp:
                    fp.write("keep")
            original_persist = manager._CodexUsageMultiMonitor__persist_pending_profile_cleanup

            def persist_then_crash(pending):
                original_persist(pending)
                raise KeyboardInterrupt("simulated crash")

            fixed_uuid = type("FixedUuid", (), {"hex": fixed_hex})()
            with patch(
                "src.apps.codex_usage_multi_monitor.uuid.uuid4",
                return_value=fixed_uuid,
            ), patch.object(
                manager,
                "_CodexUsageMultiMonitor__persist_pending_profile_cleanup",
                side_effect=persist_then_crash,
            ), self.assertRaises(KeyboardInterrupt):
                manager.add_profile("codex")

            CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=lambda config_dir, profile_dir: _FakeChildMonitor(
                    config_dir,
                    profile_dir,
                ),
            )

            self.assertTrue(os.path.isfile(os.path.join(planned.config_dir, "preexisting.txt")))
            self.assertTrue(os.path.isfile(os.path.join(planned.profile_dir, "preexisting.txt")))
            self.assertFalse(
                os.path.exists(os.path.join(tmp, "config", "ai_usage_cleanup_state.json"))
            )

    def test_invalid_interval_rejects_all_scalar_changes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            before = manager.get_settings_snapshot()

            ok, error = manager.update_settings(
                {
                    "enabled": False,
                    "taskbar_overlay_enabled": False,
                    "interval_sec": "not-a-number",
                }
            )

            self.assertFalse(ok)
            self.assertEqual(error, "interval")
            self.assertEqual(manager.get_settings_snapshot(), before)

    def test_v3_settings_migrate_once_to_v4_and_preserve_raw_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = os.path.join(tmp, "config")
            os.makedirs(config_dir, exist_ok=True)
            source = {
                "settings_version": 3,
                "default_account_id": "account_2",
                "profile_order": ["account_2", "account_1"],
                "selected_profile_ids": ["account_1"],
                "profiles": [
                    {"id": "account_2", "provider": "cursor", "label": "Cursor 기존", "enabled": True},
                    {"id": "account_1", "provider": "codex", "label": "Codex 기존", "enabled": False},
                ],
            }
            settings_path = os.path.join(config_dir, "ai_usage_settings.json")
            with open(settings_path, "w", encoding="utf-8") as fp:
                json.dump(source, fp, ensure_ascii=False)

            manager, _ = self._build_manager(tmp)
            snapshot = manager.get_settings_snapshot()

            self.assertEqual(snapshot["settings_version"], 4)
            self.assertEqual(snapshot["profile_order"], ["account_2", "account_1"])
            self.assertEqual(snapshot["default_account_id"], "account_2")
            backup_path = os.path.join(config_dir, "ai_usage_settings.v3.backup.json")
            with open(backup_path, encoding="utf-8") as fp:
                self.assertEqual(json.load(fp), source)

            manager.update_settings({"profiles": snapshot["profiles"]})
            with open(backup_path, encoding="utf-8") as fp:
                self.assertEqual(json.load(fp), source)

    def test_profiles_support_zero_one_three_and_twenty_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            for count in (0, 1, 3, 20):
                case_dir = os.path.join(tmp, str(count))
                config_dir = os.path.join(case_dir, "config")
                os.makedirs(config_dir, exist_ok=True)
                profiles = []
                for index in range(count):
                    profile_id = (
                        f"account_{index + 1}"
                        if index < 2
                        else f"profile_{index:032x}"
                    )
                    profiles.append(
                        {
                            "id": profile_id,
                            "provider": "cursor" if index % 2 else "codex",
                            "label": f"Profile {index + 1}",
                            "enabled": True,
                            "taskbar_selected": index < 2,
                        }
                    )
                with open(os.path.join(config_dir, "ai_usage_settings.json"), "w", encoding="utf-8") as fp:
                    json.dump(
                        {
                            "settings_version": 4,
                            "profiles": profiles,
                            "profile_order": [item["id"] for item in profiles],
                            "selected_profile_ids": [item["id"] for item in profiles[:2]],
                            "default_account_id": profiles[0]["id"] if profiles else "",
                        },
                        fp,
                    )
                manager, _ = self._build_manager(case_dir)
                snapshot = manager.get_settings_snapshot()
                self.assertEqual(len(snapshot["profiles"]), count)
                self.assertEqual(snapshot["profile_order"], [item["id"] for item in profiles])
                self.assertLessEqual(len(snapshot["selected_profile_ids"]), 2)
                if count == 0:
                    self.assertEqual(snapshot["default_account_id"], "")
                if count >= 1:
                    self.assertTrue(snapshot["profiles"][0]["config_dir"].endswith("codex-account-1"))

    def test_add_delete_profile_uses_opaque_id_and_removes_only_owned_profile_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)

            ok, error, profile = manager.add_profile("cursor")

            self.assertTrue(ok, error)
            self.assertIsNotNone(profile)
            profile_id = profile["id"]
            self.assertRegex(profile_id, r"^profile_[0-9a-f]{32}$")
            self.assertTrue(profile["enabled"])
            self.assertFalse(profile["taskbar_selected"])
            self.assertEqual(manager.get_settings_snapshot()["profile_order"][-1], profile_id)

            for key in ("config_dir", "profile_dir"):
                os.makedirs(profile[key], exist_ok=True)
                with open(os.path.join(profile[key], "owned.txt"), "w", encoding="utf-8") as fp:
                    fp.write("owned")
            unrelated = os.path.join(tmp, "unrelated")
            os.makedirs(unrelated, exist_ok=True)

            ok, error = manager.delete_profile(profile_id, confirmed=False)
            self.assertFalse(ok)
            self.assertEqual(error, "confirmation_required")
            self.assertTrue(os.path.isdir(profile["config_dir"]))

            ok, error = manager.delete_profile(profile_id, confirmed=True)
            self.assertTrue(ok, error)
            self.assertFalse(os.path.exists(profile["config_dir"]))
            self.assertFalse(os.path.exists(profile["profile_dir"]))
            self.assertTrue(os.path.isdir(unrelated))
            self.assertNotIn(profile_id, manager.get_settings_snapshot()["profile_order"])

    def test_provider_round_trip_preserves_dynamic_profile_id_and_provider_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            codex_paths = (created["config_dir"], created["profile_dir"])

            payload = manager.get_settings_snapshot()["profiles"]
            for item in payload:
                if item["id"] == profile_id:
                    item["provider"] = "cursor"
            ok, error = manager.update_settings({"profiles": payload})
            self.assertTrue(ok, error)
            cursor_profile = next(
                item for item in manager.get_settings_snapshot()["profiles"] if item["id"] == profile_id
            )
            self.assertNotEqual((cursor_profile["config_dir"], cursor_profile["profile_dir"]), codex_paths)

            payload = manager.get_settings_snapshot()["profiles"]
            for item in payload:
                if item["id"] == profile_id:
                    item["provider"] = "codex"
            ok, error = manager.update_settings({"profiles": payload})
            self.assertTrue(ok, error)
            restored = next(
                item for item in manager.get_settings_snapshot()["profiles"] if item["id"] == profile_id
            )
            self.assertEqual(restored["id"], profile_id)
            self.assertEqual((restored["config_dir"], restored["profile_dir"]), codex_paths)

    def test_dynamic_cursor_child_receives_opaque_profile_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_id = f"profile_{'1' * 32}"
            config_dir = os.path.join(tmp, "config")
            os.makedirs(config_dir, exist_ok=True)
            with open(
                os.path.join(config_dir, "ai_usage_settings.json"),
                "w",
                encoding="utf-8",
            ) as fp:
                json.dump(
                    {
                        "settings_version": 4,
                        "profiles": [
                            {
                                "id": profile_id,
                                "provider": "cursor",
                                "enabled": True,
                            }
                        ],
                        "profile_order": [profile_id],
                        "default_account_id": profile_id,
                    },
                    fp,
                )

            manager = CodexUsageMultiMonitor(
                config_dir=config_dir,
                local_base_dir=os.path.join(tmp, "local"),
            )

            child = manager._CodexUsageMultiMonitor__children[profile_id]
            self.assertEqual(child.profile_id, profile_id)

    def test_first_provider_switch_preserves_explicit_label_and_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error = manager.update_settings(
                {
                    "profiles": [
                        {
                            "id": "account_1",
                            "provider": "codex",
                            "label": "내 업무",
                            "enabled": False,
                        },
                        {
                            "id": "account_2",
                            "provider": "codex",
                        },
                    ]
                }
            )
            self.assertTrue(ok, error)
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"

            ok, error = manager.update_settings({"profiles": profiles})

            self.assertTrue(ok, error)
            switched = manager.get_settings_snapshot()["profiles"][0]
            self.assertEqual(switched["provider"], "cursor")
            self.assertEqual(switched["label"], "내 업무")
            self.assertFalse(switched["enabled"])

    def test_provider_switch_rejects_notifications_from_replaced_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
            )
            old_child = manager._CodexUsageMultiMonitor__children["account_1"]
            old_sink = old_child._CodexUsageMonitor__notification_sink
            old_sink({"text": "current-codex-event"})
            self.assertEqual(
                manager.pop_notification_events(),
                [{"text": "current-codex-event"}],
            )

            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"
            ok, error = manager.update_settings({"profiles": profiles})
            self.assertTrue(ok, error)

            old_sink({"text": "stale-codex-event"})
            self.assertEqual(manager.pop_notification_events(), [])
            new_child = manager._CodexUsageMultiMonitor__children["account_1"]
            new_child._notification_sink({"text": "current-cursor-event"})
            self.assertEqual(
                manager.pop_notification_events(),
                [{"text": "current-cursor-event"}],
            )
            manager.shutdown()

    def test_partial_provider_switch_preserves_omitted_custom_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error = manager.update_settings(
                {
                    "profiles": [
                        {
                            "id": "account_1",
                            "provider": "codex",
                            "label": "자동화 계정",
                            "enabled": False,
                        },
                        {
                            "id": "account_2",
                            "provider": "codex",
                            "enabled": False,
                        },
                    ]
                }
            )
            self.assertTrue(ok, error)

            ok, error = manager.update_settings(
                {
                    "profiles": [
                        {"id": "account_1", "provider": "cursor"},
                        {"id": "account_2", "provider": "cursor"},
                    ]
                }
            )

            self.assertTrue(ok, error)
            profiles = manager.get_settings_snapshot()["profiles"]
            self.assertEqual(profiles[0]["label"], "자동화 계정")
            self.assertFalse(profiles[0]["enabled"])
            self.assertEqual(profiles[1]["label"], "Cursor 2")
            self.assertFalse(profiles[1]["enabled"])

    def test_provider_default_label_is_stable_across_reordered_and_partial_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            reordered, _ = self._build_manager(os.path.join(tmp, "reordered"))
            profiles = reordered.get_settings_snapshot()["profiles"]
            profiles.reverse()
            profiles[0]["provider"] = "cursor"

            ok, error = reordered.update_settings({"profiles": profiles})

            self.assertTrue(ok, error)
            account_2 = next(
                item
                for item in reordered.get_settings_snapshot()["profiles"]
                if item["id"] == "account_2"
            )
            self.assertEqual(account_2["label"], "Cursor 2")

            partial, _ = self._build_manager(os.path.join(tmp, "partial"))
            ok, error = partial.update_settings(
                {"profiles": [{"id": "account_2", "provider": "cursor"}]}
            )

            self.assertTrue(ok, error)
            account_2 = next(
                item
                for item in partial.get_settings_snapshot()["profiles"]
                if item["id"] == "account_2"
            )
            self.assertEqual(account_2["label"], "Cursor 2")

    def test_partial_accounts_payload_keeps_legacy_first_profile_default_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)

            ok, error = manager.update_settings({"accounts": [{"id": "account_2"}]})

            self.assertTrue(ok, error)
            snapshot = manager.get_settings_snapshot()
            self.assertEqual(snapshot["profile_order"], ["account_2", "account_1"])
            self.assertEqual(snapshot["default_account_id"], "account_2")

    def test_delete_profile_rejects_path_outside_app_owned_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside, exist_ok=True)
            marker = os.path.join(outside, "keep.txt")
            with open(marker, "w", encoding="utf-8") as fp:
                fp.write("keep")

            builder = manager._CodexUsageMultiMonitor__build_profile_paths
            current_paths = manager._CodexUsageMultiMonitor__account_paths[profile_id]

            def unsafe_builder(account_id, provider):
                paths = builder(account_id, provider)
                if provider != "codex":
                    return paths
                return type(current_paths)(
                    account_id=account_id,
                    provider=provider,
                    config_dir=outside,
                    profile_dir=paths.profile_dir,
                )

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__build_profile_paths",
                side_effect=unsafe_builder,
            ):
                ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "unsafe_profile_path")
            self.assertTrue(os.path.isfile(marker))
            self.assertIn(profile_id, manager.get_settings_snapshot()["profile_order"])

    def test_delete_profile_rejects_reparse_app_owned_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            os.makedirs(created["profile_dir"], exist_ok=True)
            marker = os.path.join(created["profile_dir"], "keep.txt")
            with open(marker, "w", encoding="utf-8") as fp:
                fp.write("keep")
            local_app_root = os.path.abspath(os.path.join(tmp, "local", "windows-supporter"))
            real_lstat = os.lstat

            def reparse_boundary_lstat(path):
                info = real_lstat(path)
                if os.path.normcase(os.path.abspath(path)) != os.path.normcase(local_app_root):
                    return info
                return type(
                    "ReparseStat",
                    (),
                    {
                        "st_mode": info.st_mode,
                        "st_file_attributes": stat.FILE_ATTRIBUTE_REPARSE_POINT,
                    },
                )()

            with patch(
                "src.apps.codex_usage_multi_monitor.os.lstat",
                side_effect=reparse_boundary_lstat,
            ):
                ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "unsafe_profile_path")
            self.assertTrue(os.path.isfile(marker))
            self.assertIn(profile_id, manager.get_settings_snapshot()["profile_order"])

    def test_delete_profile_shutdown_failure_preserves_profile_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            children = []

            class _ShutdownFailingChild(_FakeChildMonitor):
                fail_shutdown = False

                def shutdown(self):
                    if self.fail_shutdown:
                        raise RuntimeError("child still alive")
                    return super().shutdown()

            def factory(config_dir, profile_dir):
                child = _ShutdownFailingChild(config_dir, profile_dir)
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            child = manager._CodexUsageMultiMonitor__children[profile_id]
            child.fail_shutdown = True
            for path in (created["config_dir"], created["profile_dir"]):
                os.makedirs(path, exist_ok=True)
                with open(os.path.join(path, "keep.txt"), "w", encoding="utf-8") as fp:
                    fp.write("keep")

            ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            self.assertIn(profile_id, manager.get_settings_snapshot()["profile_order"])
            recovery_child = manager._CodexUsageMultiMonitor__children[profile_id]
            self.assertIsNot(recovery_child, child)
            self.assertEqual(
                recovery_child.get_runtime_status()["monitor_state"],
                "recovery_pending",
            )
            self.assertTrue(os.path.isfile(os.path.join(created["config_dir"], "keep.txt")))
            self.assertTrue(os.path.isfile(os.path.join(created["profile_dir"], "keep.txt")))

            retry_ok, retry_error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(retry_ok)
            self.assertEqual(retry_error, "profile_delete_failed")
            self.assertIn(profile_id, manager.get_settings_snapshot()["profile_order"])
            self.assertTrue(os.path.isfile(os.path.join(created["config_dir"], "keep.txt")))
            self.assertTrue(os.path.isfile(os.path.join(created["profile_dir"], "keep.txt")))
            self.assertFalse(
                os.path.exists(os.path.join(tmp, "config", "ai_usage_cleanup_state.json"))
            )
            with open(
                os.path.join(tmp, "config", "ai_usage_settings_recovery.json"),
                encoding="utf-8",
            ) as fp:
                self.assertEqual(json.load(fp)["profile_ids"], [profile_id])

    def test_profile_mutations_serialize_delete_behind_inflight_settings_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _children = self._build_manager(tmp)
            stale_profiles = manager.get_settings_snapshot()["profiles"]
            update_before_save = threading.Event()
            release_update = threading.Event()
            delete_finished = threading.Event()
            update_result = []
            delete_result = []
            original_save = manager._CodexUsageMultiMonitor__save_manager_settings

            def block_update_before_save(*args, **kwargs):
                if threading.current_thread().name == "stale-settings-update":
                    update_before_save.set()
                    release_update.wait(2.0)
                return original_save(*args, **kwargs)

            def update_settings():
                update_result.append(
                    manager.update_settings(
                        {
                            "profiles": stale_profiles,
                            "interval_sec": 91,
                        }
                    )
                )

            def delete_profile():
                delete_result.append(manager.delete_profile("account_1", confirmed=True))
                delete_finished.set()

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=block_update_before_save,
            ):
                update_thread = threading.Thread(
                    target=update_settings,
                    name="stale-settings-update",
                )
                delete_thread = threading.Thread(target=delete_profile)
                update_thread.start()
                self.assertTrue(update_before_save.wait(1.0))
                delete_thread.start()
                delete_completed_while_update_blocked = delete_finished.wait(0.2)
                release_update.set()
                update_thread.join(2.0)
                delete_thread.join(2.0)

            self.assertFalse(delete_completed_while_update_blocked)
            self.assertEqual(update_result, [(True, None)])
            self.assertEqual(delete_result, [(True, None)])
            snapshot = manager.get_settings_snapshot()
            self.assertNotIn("account_1", snapshot["profile_order"])
            self.assertNotIn(
                "account_1",
                manager._CodexUsageMultiMonitor__children,
            )
            self.assertNotIn(
                "account_1",
                manager._CodexUsageMultiMonitor__account_paths,
            )

    def test_shutdown_serializes_with_provider_switch_and_rejects_later_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory_calls = 0
            staged_update_started = threading.Event()
            release_staged_update = threading.Event()
            shutdown_finished = threading.Event()
            staged_children = []

            class _BlockingStagedChild(_FakeChildMonitor):
                def update_settings(self, data):
                    staged_update_started.set()
                    release_staged_update.wait(2.0)
                    return super().update_settings(data)

            def factory(provider, config_dir, profile_dir):
                nonlocal factory_calls
                factory_calls += 1
                if factory_calls <= 2:
                    return _FakeChildMonitor(config_dir, profile_dir)
                child = _BlockingStagedChild(config_dir, profile_dir)
                child.provider = provider
                staged_children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            original = manager._CodexUsageMultiMonitor__children["account_1"]
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"
            update_result = []

            update_thread = threading.Thread(
                target=lambda: update_result.append(
                    manager.update_settings({"profiles": profiles})
                ),
                name="provider-switch-worker",
            )
            shutdown_thread = threading.Thread(
                target=lambda: (manager.shutdown(), shutdown_finished.set()),
                name="manager-shutdown",
            )
            update_thread.start()
            self.assertTrue(staged_update_started.wait(1.0))
            shutdown_thread.start()

            shutdown_completed_while_update_blocked = shutdown_finished.wait(0.2)
            self.assertTrue(
                self._wait_until(
                    lambda: manager._CodexUsageMultiMonitor__closing,
                    timeout=1.0,
                )
            )
            late_toggle_started = time.monotonic()
            late_toggle_result = manager.toggle_enabled()
            late_toggle_elapsed = time.monotonic() - late_toggle_started
            release_staged_update.set()
            update_thread.join(2.0)
            shutdown_thread.join(2.0)

            self.assertFalse(shutdown_completed_while_update_blocked)
            self.assertEqual(late_toggle_result, (False, "shutdown"))
            self.assertLess(late_toggle_elapsed, 0.2)
            self.assertFalse(update_thread.is_alive())
            self.assertFalse(shutdown_thread.is_alive())
            self.assertEqual(update_result, [(True, None)])
            self.assertEqual(original.shutdown_calls, 1)
            self.assertEqual(staged_children[0].shutdown_calls, 1)
            self.assertEqual(manager.update_settings({"interval_sec": 92}), (False, "shutdown"))
            self.assertEqual(manager.toggle_enabled(), (False, "shutdown"))
            self.assertEqual(manager.add_profile("codex"), (False, "shutdown", None))
            self.assertEqual(
                manager.delete_profile("account_1", confirmed=True),
                (False, "shutdown"),
            )

    def test_shutdown_requests_cancel_then_waits_for_active_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            refresh_started = threading.Event()
            release_refresh = threading.Event()
            shutdown_finished = threading.Event()
            children = []

            class _ActiveChild(_FakeChildMonitor):
                def __init__(self, config_dir, profile_dir):
                    super().__init__(config_dir, profile_dir)
                    self.cancel_calls = 0
                    self.events = []

                def show_current_status(self, force_refresh=True, source="manual_query"):
                    self.events.append("refresh_started")
                    refresh_started.set()
                    release_refresh.wait(2.0)
                    self.events.append("refresh_finished")
                    return super().show_current_status(force_refresh=force_refresh, source=source)

                def request_collect_cancel(self):
                    self.cancel_calls += 1
                    self.events.append("cancel_requested")
                    release_refresh.set()

                def shutdown(self):
                    self.events.append("shutdown")
                    return super().shutdown()

            def factory(config_dir, profile_dir):
                child = (
                    _ActiveChild(config_dir, profile_dir)
                    if not children
                    else _FakeChildMonitor(config_dir, profile_dir)
                )
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            manager.attach(_FakeRoot(), queue.Queue())
            manager.show_current_status(force_refresh=True)
            self.assertTrue(refresh_started.wait(1.0))

            shutdown_thread = threading.Thread(
                target=lambda: (manager.shutdown(), shutdown_finished.set())
            )
            shutdown_thread.start()
            shutdown_completed_after_cancel = shutdown_finished.wait(1.0)
            if not shutdown_completed_after_cancel:
                release_refresh.set()
            shutdown_thread.join(2.0)

            self.assertTrue(shutdown_completed_after_cancel)
            self.assertFalse(shutdown_thread.is_alive())
            self.assertEqual(children[0].cancel_calls, 1)
            self.assertEqual(children[1].show_calls, [])
            self.assertEqual([child.shutdown_calls for child in children], [1, 1])
            self.assertEqual(
                children[0].events,
                ["refresh_started", "cancel_requested", "refresh_finished", "shutdown"],
            )

    def test_shutdown_uses_final_shutdown_when_cancel_request_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            refresh_started = threading.Event()
            release_refresh = threading.Event()
            shutdown_finished = threading.Event()
            children = []

            class _CancelFailureChild(_FakeChildMonitor):
                def show_current_status(self, force_refresh=True, source="manual_query"):
                    refresh_started.set()
                    release_refresh.wait(2.0)
                    return super().show_current_status(force_refresh=force_refresh, source=source)

                def request_collect_cancel(self):
                    self.cancel_calls += 1
                    raise RuntimeError("cancel boundary unavailable")

                def shutdown(self):
                    release_refresh.set()
                    return super().shutdown()

            def factory(config_dir, profile_dir):
                child = (
                    _CancelFailureChild(config_dir, profile_dir)
                    if not children
                    else _FakeChildMonitor(config_dir, profile_dir)
                )
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            manager.attach(_FakeRoot(), queue.Queue())
            manager.show_current_status(force_refresh=True)
            self.assertTrue(refresh_started.wait(1.0))

            shutdown_thread = threading.Thread(
                target=lambda: (manager.shutdown(), shutdown_finished.set()),
                daemon=True,
            )
            shutdown_thread.start()
            completed_with_fallback = shutdown_finished.wait(1.0)
            if not completed_with_fallback:
                release_refresh.set()
                shutdown_thread.join(2.0)

            self.assertTrue(completed_with_fallback)
            self.assertEqual(children[0].cancel_calls, 1)
            self.assertEqual(children[0].shutdown_calls, 1)

    def test_shutdown_releases_settings_lock_before_waiting_for_refresh_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker_waiting = threading.Event()
            allow_worker_lock = threading.Event()
            shutdown_acquired = threading.Event()
            shutdown_finished = threading.Event()

            class _GatedRLock:
                def __init__(self):
                    self._lock = threading.RLock()

                def __enter__(self):
                    thread_name = threading.current_thread().name
                    if thread_name != "MainThread" and thread_name != "manager-shutdown":
                        worker_waiting.set()
                        allow_worker_lock.wait(2.0)
                    self._lock.acquire()
                    if thread_name == "manager-shutdown":
                        shutdown_acquired.set()
                    return self

                def __exit__(self, _exc_type, _exc, _traceback):
                    self._lock.release()
                    return False

            manager, _children = self._build_manager(tmp)
            manager.attach(_FakeRoot(), queue.Queue())
            manager._CodexUsageMultiMonitor__settings_mutation_lock = _GatedRLock()

            manager.show_current_status(force_refresh=True)
            self.assertTrue(worker_waiting.wait(1.0))
            shutdown_thread = threading.Thread(
                target=lambda: (manager.shutdown(), shutdown_finished.set()),
                name="manager-shutdown",
                daemon=True,
            )
            shutdown_thread.start()
            self.assertTrue(shutdown_acquired.wait(1.0))

            allow_worker_lock.set()
            shutdown_thread.join(2.0)

            self.assertTrue(shutdown_finished.is_set())
            self.assertFalse(shutdown_thread.is_alive())
            self.assertFalse(manager._CodexUsageMultiMonitor__refresh_inflight)

    def test_delete_requests_cancel_then_waits_before_removing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            refresh_started = threading.Event()
            release_refresh = threading.Event()
            delete_finished = threading.Event()
            delete_result = []
            children = []

            class _LateWritingChild(_FakeChildMonitor):
                def __init__(self, config_dir, profile_dir):
                    super().__init__(config_dir, profile_dir)
                    self.cancel_calls = 0
                    self.events = []

                def show_current_status(self, force_refresh=True, source="manual_query"):
                    self.events.append("refresh_started")
                    refresh_started.set()
                    release_refresh.wait(2.0)
                    os.makedirs(self.config_dir, exist_ok=True)
                    with open(os.path.join(self.config_dir, "late_state.json"), "w", encoding="utf-8") as fp:
                        fp.write("late")
                    self.events.append("refresh_finished")
                    return super().show_current_status(force_refresh=force_refresh, source=source)

                def request_collect_cancel(self):
                    self.cancel_calls += 1
                    self.events.append("cancel_requested")
                    release_refresh.set()

                def shutdown(self):
                    self.events.append("shutdown")
                    return super().shutdown()

            def factory(config_dir, profile_dir):
                child = (
                    _LateWritingChild(config_dir, profile_dir)
                    if not children
                    else _FakeChildMonitor(config_dir, profile_dir)
                )
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            manager.attach(_FakeRoot(), queue.Queue())
            account_path = manager.get_settings_snapshot()["profiles"][0]["config_dir"]
            os.makedirs(account_path, exist_ok=True)
            manager.show_account_status("account_1")
            self.assertTrue(refresh_started.wait(1.0))

            delete_thread = threading.Thread(
                target=lambda: (
                    delete_result.append(manager.delete_profile("account_1", confirmed=True)),
                    delete_finished.set(),
                )
            )
            delete_thread.start()
            delete_completed_after_cancel = delete_finished.wait(1.0)
            if not delete_completed_after_cancel:
                release_refresh.set()
            delete_thread.join(2.0)

            self.assertTrue(delete_completed_after_cancel)
            self.assertFalse(delete_thread.is_alive())
            self.assertEqual(children[0].cancel_calls, 1)
            self.assertEqual(delete_result, [(True, None)])
            self.assertFalse(os.path.exists(account_path))
            self.assertEqual(
                children[0].events,
                ["refresh_started", "cancel_requested", "refresh_finished", "shutdown"],
            )

    def test_delete_cleanup_journal_failure_does_not_cancel_retained_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            retained_child = children[0]

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__persist_pending_profile_cleanup",
                side_effect=OSError("cleanup journal unavailable"),
            ):
                result = manager.delete_profile("account_1", confirmed=True)

            self.assertEqual(result, (False, "profile_delete_failed"))
            self.assertEqual(retained_child.cancel_calls, 0)
            self.assertIs(
                manager._CodexUsageMultiMonitor__children["account_1"],
                retained_child,
            )
            self.assertIn(
                "account_1",
                manager.get_settings_snapshot()["profile_order"],
            )

    def test_delete_recovery_marker_failure_does_not_cancel_retained_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            retained_child = children[0]

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__prepare_settings_recovery",
                side_effect=OSError("recovery marker unavailable"),
            ):
                result = manager.delete_profile("account_1", confirmed=True)

            self.assertEqual(result, (False, "profile_delete_failed"))
            self.assertEqual(retained_child.cancel_calls, 0)
            self.assertIs(
                manager._CodexUsageMultiMonitor__children["account_1"],
                retained_child,
            )

    def test_snapshot_readers_share_provider_publish_mutation_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _children = self._build_manager(tmp)
            reader_finished = threading.Event()
            results = []

            def read_snapshots():
                results.append(manager.get_settings_snapshot())
                results.append(manager.get_runtime_status())
                reader_finished.set()

            with manager._CodexUsageMultiMonitor__settings_mutation_lock:
                reader = threading.Thread(target=read_snapshots)
                reader.start()
                completed_inside_publish = reader_finished.wait(0.2)

            reader.join(2.0)

            self.assertFalse(completed_inside_publish)
            self.assertFalse(reader.is_alive())
            self.assertEqual(len(results), 2)

    def test_provider_switch_is_rejected_while_profile_refresh_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            refresh_started = threading.Event()
            release_refresh = threading.Event()
            children = []

            class _ActiveChild(_FakeChildMonitor):
                def show_current_status(self, force_refresh=True, source="manual_query"):
                    refresh_started.set()
                    release_refresh.wait(2.0)
                    return super().show_current_status(force_refresh=force_refresh, source=source)

            def factory(config_dir, profile_dir):
                child = (
                    _ActiveChild(config_dir, profile_dir)
                    if not children
                    else _FakeChildMonitor(config_dir, profile_dir)
                )
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            manager.attach(_FakeRoot(), queue.Queue())
            manager.show_account_status("account_1")
            self.assertTrue(refresh_started.wait(1.0))
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"

            result = manager.update_settings({"profiles": profiles})

            self.assertEqual(result, (False, "profile_refresh_busy"))
            self.assertEqual(
                manager.get_settings_snapshot()["profiles"][0]["provider"],
                "codex",
            )
            release_refresh.set()
            self.assertTrue(
                self._wait_until(
                    lambda: not manager._CodexUsageMultiMonitor__refresh_inflight
                )
            )

    def test_delete_profile_move_failure_restores_all_paths_and_live_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            codex_paths = (created["config_dir"], created["profile_dir"])
            for path in codex_paths:
                os.makedirs(path, exist_ok=True)

            profiles = manager.get_settings_snapshot()["profiles"]
            for item in profiles:
                if item["id"] == profile_id:
                    item["provider"] = "cursor"
            ok, error = manager.update_settings({"profiles": profiles})
            self.assertTrue(ok, error)
            cursor = next(
                item for item in manager.get_settings_snapshot()["profiles"] if item["id"] == profile_id
            )
            cursor_paths = (cursor["config_dir"], cursor["profile_dir"])
            for path in cursor_paths:
                os.makedirs(path, exist_ok=True)

            real_replace = os.replace
            move_count = 0
            failed_once = False

            def fail_second_profile_move(source, target):
                nonlocal move_count, failed_once
                if ".delete-" in str(target):
                    move_count += 1
                    if move_count == 2 and not failed_once:
                        failed_once = True
                        raise PermissionError("locked")
                return real_replace(source, target)

            with patch("src.apps.codex_usage_multi_monitor.os.replace", side_effect=fail_second_profile_move):
                ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            for path in (*codex_paths, *cursor_paths):
                self.assertTrue(os.path.isdir(path), path)
            self.assertIn(profile_id, manager.get_settings_snapshot()["profile_order"])
            active_child = manager._CodexUsageMultiMonitor__children[profile_id]
            self.assertEqual(active_child.shutdown_calls, 0)
            self.assertGreater(len(children), 4)

    def test_delete_profile_rollback_failure_is_journaled_and_restored_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            owned_paths = (created["config_dir"], created["profile_dir"])
            for path in owned_paths:
                os.makedirs(path, exist_ok=True)

            real_replace = os.replace
            move_count = 0

            def fail_move_and_first_restore(source, target):
                nonlocal move_count
                if ".delete-" in str(target):
                    move_count += 1
                    if move_count == 2:
                        raise PermissionError("second move locked")
                if ".delete-" in str(source) and str(target) == owned_paths[0]:
                    raise PermissionError("restore locked")
                return real_replace(source, target)

            with patch(
                "src.apps.codex_usage_multi_monitor.os.replace",
                side_effect=fail_move_and_first_restore,
            ):
                ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            self.assertFalse(os.path.exists(owned_paths[0]))
            cleanup_state = os.path.join(tmp, "config", "ai_usage_cleanup_state.json")
            self.assertTrue(os.path.isfile(cleanup_state))
            with open(cleanup_state, encoding="utf-8") as fp:
                quarantines = [item["path"] for item in json.load(fp)["paths"]]
            self.assertTrue(any(os.path.exists(path) for path in quarantines))
            runtime = next(
                item
                for item in manager.get_runtime_status()["profiles"]
                if item["id"] == profile_id
            )
            self.assertEqual(runtime["runtime"]["monitor_state"], "recovery_pending")
            ok, error = manager.update_settings({"interval_sec": 91})
            self.assertTrue(ok, error)
            self.assertFalse(os.path.exists(owned_paths[0]))
            self.assertTrue(os.path.isfile(cleanup_state))

            self._build_manager(tmp)

            self.assertTrue(os.path.isdir(owned_paths[0]))
            self.assertFalse(os.path.exists(cleanup_state))

    def test_delete_profile_restore_conflict_keeps_journal_and_recovery_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            owned_paths = (created["config_dir"], created["profile_dir"])
            for path in owned_paths:
                os.makedirs(path, exist_ok=True)

            real_replace = os.replace
            move_count = 0

            def recreate_original_before_move_failure(source, target):
                nonlocal move_count
                if ".delete-" in str(target):
                    move_count += 1
                    if move_count == 2:
                        os.makedirs(owned_paths[0], exist_ok=True)
                        raise PermissionError("second move locked")
                return real_replace(source, target)

            with patch(
                "src.apps.codex_usage_multi_monitor.os.replace",
                side_effect=recreate_original_before_move_failure,
            ):
                ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            cleanup_state = os.path.join(tmp, "config", "ai_usage_cleanup_state.json")
            self.assertTrue(os.path.isfile(cleanup_state))
            with open(cleanup_state, encoding="utf-8") as fp:
                quarantines = [item["path"] for item in json.load(fp)["paths"]]
            self.assertTrue(any(os.path.exists(path) for path in quarantines))
            self.assertTrue(os.path.isdir(owned_paths[0]))
            runtime = next(
                item
                for item in manager.get_runtime_status()["profiles"]
                if item["id"] == profile_id
            )
            self.assertEqual(runtime["runtime"]["monitor_state"], "recovery_pending")

    def test_delete_profile_cleanup_journal_rejects_unrelated_app_owned_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            settings_path = os.path.join(tmp, "config", "ai_usage_settings.json")
            cleanup_state = os.path.join(tmp, "config", "ai_usage_cleanup_state.json")
            with open(cleanup_state, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "schema_version": 1,
                        "paths": [
                            {
                                "transaction_id": "a" * 32,
                                "profile_id": "account_1",
                                "provider": "codex",
                                "path_kind": "config",
                                "original": os.path.join(tmp, "config", "codex-account-1"),
                                "path": settings_path,
                                "root": os.path.join(tmp, "config"),
                            }
                        ],
                    },
                    fp,
                )

            manager.shutdown()
            self._build_manager(tmp)

            self.assertTrue(os.path.isfile(settings_path))
            self.assertFalse(os.path.exists(cleanup_state))

    def test_delete_profile_cleanup_journal_write_failure_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            owned_paths = (created["config_dir"], created["profile_dir"])
            for path in owned_paths:
                os.makedirs(path, exist_ok=True)
            writer = manager._CodexUsageMultiMonitor__write_json_file

            def fail_cleanup_journal(path, payload):
                if os.path.basename(path) == "ai_usage_cleanup_state.json":
                    raise OSError("journal unavailable")
                return writer(path, payload)

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__write_json_file",
                side_effect=fail_cleanup_journal,
            ):
                ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            self.assertIn(profile_id, manager.get_settings_snapshot()["profile_order"])
            self.assertTrue(all(os.path.isdir(path) for path in owned_paths))
            active_child = manager._CodexUsageMultiMonitor__children[profile_id]
            self.assertEqual(active_child.shutdown_calls, 0)
            self.assertEqual(len(children), 3)

    def test_delete_profile_removes_original_path_recreated_after_settings_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("cursor")
            self.assertTrue(ok, error)
            for path in (created["config_dir"], created["profile_dir"]):
                os.makedirs(path, exist_ok=True)
            real_save = manager._CodexUsageMultiMonitor__save_manager_settings

            def save_then_recreate_original():
                real_save()
                os.makedirs(created["config_dir"], exist_ok=True)
                with open(
                    os.path.join(created["config_dir"], "recreated.txt"),
                    "w",
                    encoding="utf-8",
                ) as fp:
                    fp.write("late writer")

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=save_then_recreate_original,
            ):
                ok, error = manager.delete_profile(created["id"], confirmed=True)

            self.assertTrue(ok, error)
            self.assertFalse(os.path.exists(created["config_dir"]))

    def test_cleanup_retry_rebuilds_live_recovery_pending_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            owned_paths = (created["config_dir"], created["profile_dir"])
            for path in owned_paths:
                os.makedirs(path, exist_ok=True)
            real_replace = os.replace
            move_count = 0

            def fail_move_and_restore(source, target):
                nonlocal move_count
                if ".delete-" in str(target):
                    move_count += 1
                    if move_count == 2:
                        raise PermissionError("move locked")
                if ".delete-" in str(source):
                    raise PermissionError("restore locked")
                return real_replace(source, target)

            with patch(
                "src.apps.codex_usage_multi_monitor.os.replace",
                side_effect=fail_move_and_restore,
            ):
                ok, error = manager.delete_profile(profile_id, confirmed=True)
            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            before = next(
                item
                for item in manager.get_runtime_status()["profiles"]
                if item["id"] == profile_id
            )
            self.assertEqual(before["runtime"]["monitor_state"], "recovery_pending")

            manager._CodexUsageMultiMonitor__retry_pending_profile_cleanup()

            after = next(
                item
                for item in manager.get_runtime_status()["profiles"]
                if item["id"] == profile_id
            )
            self.assertNotEqual(after["runtime"]["monitor_state"], "recovery_pending")
            self.assertFalse(
                os.path.exists(os.path.join(tmp, "config", "ai_usage_cleanup_state.json"))
            )

    def test_delete_profile_move_failure_and_factory_failure_returns_tuple(self):
        with tempfile.TemporaryDirectory() as tmp:
            fail_factory = False

            def factory(config_dir, profile_dir):
                if fail_factory:
                    raise RuntimeError("factory unavailable")
                return _FakeChildMonitor(config_dir, profile_dir)

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            for path in (created["config_dir"], created["profile_dir"]):
                os.makedirs(path, exist_ok=True)
            fail_factory = True
            real_replace = os.replace
            move_count = 0

            def fail_second_move(source, target):
                nonlocal move_count
                if ".delete-" in str(target):
                    move_count += 1
                    if move_count == 2:
                        raise PermissionError("locked")
                return real_replace(source, target)

            with patch(
                "src.apps.codex_usage_multi_monitor.os.replace",
                side_effect=fail_second_move,
            ):
                ok, error = manager.delete_profile(created["id"], confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            runtime = next(
                item
                for item in manager.get_runtime_status()["profiles"]
                if item["id"] == created["id"]
            )
            self.assertEqual(runtime["runtime"]["monitor_state"], "recovery_pending")

    def test_delete_profile_removes_owned_paths_for_every_provider_in_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            codex_paths = (created["config_dir"], created["profile_dir"])
            for path in codex_paths:
                os.makedirs(path, exist_ok=True)

            profiles = manager.get_settings_snapshot()["profiles"]
            for item in profiles:
                if item["id"] == profile_id:
                    item["provider"] = "cursor"
            ok, error = manager.update_settings({"profiles": profiles})
            self.assertTrue(ok, error)
            cursor = next(
                item for item in manager.get_settings_snapshot()["profiles"] if item["id"] == profile_id
            )
            cursor_paths = (cursor["config_dir"], cursor["profile_dir"])
            for path in cursor_paths:
                os.makedirs(path, exist_ok=True)

            ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertTrue(ok, error)
            for path in (*codex_paths, *cursor_paths):
                self.assertFalse(os.path.exists(path), path)

    def test_delete_profile_persists_and_retries_quarantine_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            for key in ("config_dir", "profile_dir"):
                os.makedirs(created[key], exist_ok=True)

            real_rmtree = __import__("shutil").rmtree

            def fail_quarantine_cleanup(path, *args, **kwargs):
                if ".delete-" in str(path):
                    raise PermissionError("locked cleanup")
                return real_rmtree(path, *args, **kwargs)

            with patch(
                "src.apps.codex_usage_multi_monitor.shutil.rmtree",
                side_effect=fail_quarantine_cleanup,
            ):
                ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertTrue(ok, error)
            cleanup_state = os.path.join(tmp, "config", "ai_usage_cleanup_state.json")
            self.assertTrue(os.path.isfile(cleanup_state))
            with open(cleanup_state, encoding="utf-8") as fp:
                pending_paths = [item["path"] for item in json.load(fp)["paths"]]
            self.assertTrue(pending_paths)
            self.assertTrue(all(os.path.exists(path) for path in pending_paths))

            self._build_manager(tmp)

            self.assertFalse(os.path.exists(cleanup_state))
            self.assertTrue(all(not os.path.exists(path) for path in pending_paths))

    def test_delete_profile_save_failure_restores_model_paths_and_live_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            ok, error, created = manager.add_profile("codex")
            self.assertTrue(ok, error)
            profile_id = created["id"]
            owned_paths = (created["config_dir"], created["profile_dir"])
            for path in owned_paths:
                os.makedirs(path, exist_ok=True)

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=OSError("disk full"),
            ):
                ok, error = manager.delete_profile(profile_id, confirmed=True)

            self.assertFalse(ok)
            self.assertEqual(error, "profile_delete_failed")
            self.assertIn(profile_id, manager.get_settings_snapshot()["profile_order"])
            for path in owned_paths:
                self.assertTrue(os.path.isdir(path), path)
            active_child = manager._CodexUsageMultiMonitor__children[profile_id]
            self.assertEqual(active_child.shutdown_calls, 0)
            self.assertGreater(len(children), 3)

    def test_add_profile_save_failure_restores_model_order_default_and_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            before = manager.get_settings_snapshot()

            with patch.object(
                manager,
                "_CodexUsageMultiMonitor__save_manager_settings",
                side_effect=OSError("disk full"),
            ):
                ok, error, created = manager.add_profile("cursor")

            self.assertFalse(ok)
            self.assertEqual(error, "profile_add_failed")
            self.assertIsNone(created)
            after = manager.get_settings_snapshot()
            self.assertEqual(after["profile_order"], before["profile_order"])
            self.assertEqual(after["default_account_id"], before["default_account_id"])
            self.assertEqual(
                set(manager._CodexUsageMultiMonitor__children),
                set(before["profile_order"]),
            )

    def test_v3_migration_preserves_codex_label_and_enabled_for_provider_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = os.path.join(tmp, "config")
            os.makedirs(config_dir, exist_ok=True)
            with open(os.path.join(config_dir, "ai_usage_settings.json"), "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "settings_version": 3,
                        "profiles": [
                            {
                                "id": "account_1",
                                "provider": "cursor",
                                "label": "Cursor 현재",
                                "enabled": True,
                                "taskbar_selected": True,
                            }
                        ],
                        "legacy_codex_accounts": [
                            {"id": "account_1", "label": "Codex 보존", "enabled": False}
                        ],
                    },
                    fp,
                    ensure_ascii=False,
                )

            manager, _ = self._build_manager(tmp)
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "codex"
            ok, error = manager.update_settings({"profiles": profiles})

            self.assertTrue(ok, error)
            restored = manager.get_settings_snapshot()["profiles"][0]
            self.assertEqual(restored["label"], "Codex 보존")
            self.assertFalse(restored["enabled"])
            with open(os.path.join(config_dir, "ai_usage_settings.json"), encoding="utf-8") as fp:
                persisted = json.load(fp)
            self.assertEqual(
                persisted["profiles"][0]["provider_settings"]["codex"],
                {
                    "label": "Codex 보존",
                    "label_mode": "auto",
                    "custom_label": "Codex 보존",
                    "enabled": False,
                },
            )

    def test_partial_profile_order_keeps_omitted_profiles_in_existing_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)

            ok, error = manager.update_settings({"profile_order": ["account_2"]})

            self.assertTrue(ok, error)
            self.assertEqual(manager.get_settings_snapshot()["profile_order"], ["account_2", "account_1"])

    def test_runtime_exposes_provider_neutral_profiles_and_legacy_accounts_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].last_snapshot.update(
                {"five_hour_limit": "80%", "five_hour_reset_at": "2026-07-18T10:00:00+00:00"}
            )

            ok, error = manager.update_settings(
                {
                    "profiles": [
                        {
                            "id": "account_1",
                            "provider": "codex",
                            "enabled": True,
                            "taskbar_selected": True,
                        },
                        {
                            "id": "account_2",
                            "provider": "cursor",
                            "enabled": False,
                            "taskbar_selected": False,
                        },
                    ]
                }
            )
            self.assertTrue(ok, error)

            runtime = manager.get_runtime_status()

            self.assertIs(runtime["accounts"], runtime["profiles"])
            self.assertEqual(runtime["profiles"][0]["provider"], "codex")
            self.assertEqual(runtime["profiles"][0]["profile_id"], "account_1")
            self.assertTrue(runtime["profiles"][0]["taskbar_selected"])
            self.assertEqual(runtime["profiles"][0]["metrics"][0]["key"], "five_hour_limit")

    def test_runtime_omits_unreported_codex_five_hour_and_uses_limit_reset_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].last_snapshot.update(
                {
                    "five_hour_limit": "",
                    "weekly_limit": "80%",
                    "weekly_limit_reset_at": "2026-07-20T10:00:00+09:00",
                }
            )

            metrics = manager.get_runtime_status()["profiles"][0]["metrics"]

            self.assertEqual([item["key"] for item in metrics], ["weekly_limit"])
            self.assertEqual(metrics[0]["reset_at"], "2026-07-20T10:00:00+09:00")
            self.assertEqual(metrics[0]["reset_precision"], "datetime")

    def test_cursor_metrics_omit_disabled_on_demand_and_separate_full_compact_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "cursor"
            ok, error = manager.update_settings({"profiles": profiles})
            self.assertTrue(ok, error)
            cursor_child = children[-1]
            cursor_child.last_snapshot = {
                "state": "ready",
                "included_remaining_percent": 100,
                "included_usage": "US$0 / US$20",
                "billing_reset_at": "2026-08-13",
                "reset_precision": "date",
                "on_demand_enabled": False,
                "on_demand_status": "OFF",
            }

            metrics = manager.get_runtime_status()["profiles"][0]["metrics"]

            self.assertEqual([item["key"] for item in metrics], ["included_usage"])
            self.assertEqual(metrics[0]["value_text"], "US$0 / US$20")
            self.assertEqual(metrics[0]["short_value_text"], "100%")
            self.assertEqual(metrics[0]["reset_precision"], "date")

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

    def test_provider_scoped_label_mode_round_trips_and_controls_runtime_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0].update(
                {
                    "label": "내 Codex",
                    "label_mode": "custom",
                    "custom_label": "내 Codex",
                }
            )

            ok, error = manager.update_settings({"profiles": profiles})

            self.assertTrue(ok, error)
            children[0].runtime["profile_name"] = "Provider Codex"
            runtime = manager.get_runtime_status()["profiles"][0]
            self.assertEqual(runtime["label"], "내 Codex")
            self.assertEqual(runtime["label_mode"], "custom")
            self.assertEqual(runtime["custom_label"], "내 Codex")

            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0].update(
                {
                    "provider": "cursor",
                    "label": "Cursor fallback",
                    "label_mode": "auto",
                    "custom_label": "Cursor fallback",
                }
            )
            ok, error = manager.update_settings({"profiles": profiles})
            self.assertTrue(ok, error)
            cursor_child = manager._CodexUsageMultiMonitor__children["account_1"]
            cursor_child.runtime["profile_name"] = "Stable Cursor"
            self.assertEqual(
                manager.get_runtime_status()["profiles"][0]["label"],
                "Stable Cursor",
            )
            cursor_child.runtime["profile_name"] = ""
            self.assertEqual(
                manager.get_runtime_status()["profiles"][0]["label"],
                "Cursor fallback",
            )

            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "codex"
            ok, error = manager.update_settings({"profiles": profiles})
            self.assertTrue(ok, error)
            restored = manager.get_settings_snapshot()["profiles"][0]
            self.assertEqual(restored["label_mode"], "custom")
            self.assertEqual(restored["custom_label"], "내 Codex")
            with open(
                os.path.join(tmp, "config", "ai_usage_settings.json"),
                encoding="utf-8",
            ) as fp:
                persisted = json.load(fp)
            self.assertEqual(
                persisted["profiles"][0]["provider_settings"]["codex"]["label_mode"],
                "custom",
            )

    def test_provider_switch_applies_explicit_label_mode_against_saved_target_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._build_manager(tmp)
            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0].update(
                {
                    "provider": "cursor",
                    "label": "Cursor custom",
                    "label_mode": "custom",
                    "custom_label": "Cursor custom",
                }
            )
            ok, error = manager.update_settings({"profiles": profiles})
            self.assertTrue(ok, error)
            self.assertEqual(
                manager.get_settings_snapshot()["profiles"][0]["label_mode"],
                "custom",
            )

            profiles = manager.get_settings_snapshot()["profiles"]
            profiles[0]["provider"] = "codex"
            ok, error = manager.update_settings({"profiles": profiles})
            self.assertTrue(ok, error)
            self.assertEqual(
                manager.get_settings_snapshot()["profiles"][0]["label_mode"],
                "auto",
            )

            ok, error = manager.update_settings(
                {
                    "profiles": [
                        {
                            "id": "account_1",
                            "provider": "cursor",
                            "label_mode": "auto",
                        }
                    ]
                }
            )
            self.assertTrue(ok, error)
            self.assertEqual(
                manager.get_settings_snapshot()["profiles"][0]["label_mode"],
                "auto",
            )

    def test_logged_out_accounts_with_stopped_browser_do_not_drive_background_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            for child in children:
                child.runtime.update(
                    {
                        "session_state": "logged_out",
                        "collect_inflight": False,
                        "profile_session_present": True,
                        "browser_state": "stopped",
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
                self._wait_until(lambda: all(len(child.show_calls) == 2 for child in children))
            )
            self.assertEqual(
                [child.show_calls for child in children],
                [
                    [
                        {"force_refresh": True, "source": "manual_query"},
                        {"force_refresh": True, "source": "manual_query"},
                    ],
                    [
                        {"force_refresh": True, "source": "manual_query"},
                        {"force_refresh": True, "source": "manual_query"},
                    ],
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
                self.assertFalse(children[0].started.wait(0.1))
                children[1].release.set()
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

            with patch("src.apps.codex_usage_multi_monitor.time.monotonic", return_value=100.0):
                manager.attach(root, event_queue=None)

            self.assertEqual(len(root.after_calls), 1)
            self.assertLessEqual(root.after_calls[0]["delay_ms"], 1000)

            with patch("src.apps.codex_usage_multi_monitor.time.monotonic", return_value=101.0):
                root.after_calls[0]["callback"]()

            self.assertEqual(
                children[0].show_calls,
                [{"force_refresh": True, "source": "auto_monitor"}],
            )
            self.assertEqual(children[1].show_calls, [])
            self.assertGreaterEqual(len(root.after_calls), 2)

    def test_background_monitor_stops_when_every_account_requires_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime["session_state"] = "logged_in"
            children[0].runtime["monitor_state"] = "paused_auth_required"
            children[0].runtime["auth_attention_required"] = True
            children[1].runtime["session_state"] = "logged_out"
            root = _FakeRoot()

            manager.attach(root, event_queue=None)

            self.assertEqual(root.after_calls, [])
            self.assertEqual(children[0].show_calls, [])
            self.assertEqual(children[1].show_calls, [])

    def test_background_monitor_keeps_no_cache_transient_failure_eligible_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime["session_state"] = "unknown"
            children[0].runtime["provider_status"] = "retrying"
            children[0].runtime["retry_after_sec"] = 30.0
            children[1].runtime["session_state"] = "logged_out"
            root = _FakeRoot()

            with patch("src.apps.codex_usage_multi_monitor.time.monotonic", return_value=100.0):
                manager.attach(root, event_queue=None)

            self.assertEqual(len(root.after_calls), 1)
            self.assertEqual(root.after_calls[0]["delay_ms"], 30000)

    def test_background_monitor_does_not_retry_logged_out_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime.update(
                {
                    "session_state": "logged_out",
                    "provider_status": "retrying",
                    "retry_after_sec": 30.0,
                }
            )
            children[1].runtime["session_state"] = "logged_out"
            root = _FakeRoot()

            manager.attach(root, event_queue=None)

            self.assertEqual(root.after_calls, [])
            self.assertEqual(children[0].show_calls, [])

    def test_background_monitor_stops_retry_exhausted_profile_without_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            children[0].runtime.update(
                {
                    "session_state": "logged_in",
                    "provider_status": "error",
                    "retry_exhausted": True,
                    "collect_inflight": False,
                }
            )
            children[1].runtime["session_state"] = "logged_out"
            root = _FakeRoot()

            manager.attach(root, event_queue=None)

            self.assertEqual(root.after_calls, [])
            self.assertEqual(children[0].show_calls, [])

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


    def test_background_tick_collects_only_profiles_whose_individual_due_time_arrived(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            root = _FakeRoot()
            for child in children:
                child.runtime["session_state"] = "logged_in"
            manager.attach(root)
            manager._CodexUsageMultiMonitor__profile_next_collect_due_ts = {
                "account_1": 10.0,
                "account_2": 20.0,
            }

            with patch("src.apps.codex_usage_multi_monitor.time.monotonic", return_value=10.0):
                manager._CodexUsageMultiMonitor__monitor_tick()

            self.assertEqual(len(children[0].show_calls), 1)
            self.assertEqual(children[1].show_calls, [])

            with patch("src.apps.codex_usage_multi_monitor.time.monotonic", return_value=20.0):
                manager._CodexUsageMultiMonitor__monitor_tick()

            self.assertEqual(len(children[0].show_calls), 1)
            self.assertEqual(len(children[1].show_calls), 1)

    def test_manager_queue_keeps_overlapping_profile_requests_serial_and_does_not_drop_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_started = threading.Event()
            release_first = threading.Event()
            second_finished = threading.Event()
            state_lock = threading.Lock()
            active = 0
            max_active = 0
            call_order: list[str] = []

            class _SerialChild(_FakeChildMonitor):
                def show_current_status(self, force_refresh=True, source="manual_query"):
                    nonlocal active, max_active
                    account_id = "account_1" if "account-1" in self.config_dir else "account_2"
                    with state_lock:
                        active += 1
                        max_active = max(max_active, active)
                        call_order.append(account_id)
                    if account_id == "account_1":
                        first_started.set()
                        release_first.wait(2.0)
                    else:
                        second_finished.set()
                    with state_lock:
                        active -= 1
                    return super().show_current_status(
                        force_refresh=force_refresh,
                        source=source,
                    )

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=lambda config_dir, profile_dir: _SerialChild(
                    config_dir,
                    profile_dir,
                ),
            )
            manager.attach(_FakeRoot(), queue.Queue())

            manager.show_account_status("account_1")
            self.assertTrue(first_started.wait(1.0))
            manager.show_account_status("account_2")
            release_first.set()

            self.assertTrue(second_finished.wait(2.0))
            self.assertEqual(call_order, ["account_1", "account_2"])
            self.assertEqual(max_active, 1)

    def test_manager_queue_does_not_strand_request_during_empty_to_idle_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_finished = threading.Event()
            worker_observed_empty = threading.Event()
            allow_empty_worker_exit = threading.Event()
            second_finished = threading.Event()

            class _RaceChild(_FakeChildMonitor):
                def show_current_status(self, force_refresh=True, source="manual_query"):
                    if "account-1" in self.config_dir:
                        first_finished.set()
                    else:
                        second_finished.set()
                    return super().show_current_status(
                        force_refresh=force_refresh,
                        source=source,
                    )

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=lambda config_dir, profile_dir: _RaceChild(
                    config_dir,
                    profile_dir,
                ),
            )
            manager.attach(_FakeRoot(), queue.Queue())
            original_condition = manager._CodexUsageMultiMonitor__refresh_condition

            class _EmptyExitGateCondition:
                def __init__(self):
                    self._local = threading.local()
                    self._gated = False
                    self._empty_after_first_count = 0

                def __enter__(self):
                    entered = original_condition.__enter__()
                    self._local.queue_size = len(
                        manager._CodexUsageMultiMonitor__refresh_queue
                    )
                    if (
                        threading.current_thread().name != "MainThread"
                        and first_finished.is_set()
                        and self._local.queue_size == 0
                    ):
                        self._empty_after_first_count += 1
                    self._local.empty_after_first_count = (
                        self._empty_after_first_count
                    )
                    return entered

                def __exit__(self, exc_type, exc, traceback):
                    queue_was_empty = self._local.queue_size == 0
                    queue_is_empty = not manager._CodexUsageMultiMonitor__refresh_queue
                    should_gate = (
                        threading.current_thread().name != "MainThread"
                        and queue_was_empty
                        and queue_is_empty
                        and self._local.empty_after_first_count >= 2
                        and not self._gated
                    )
                    result = original_condition.__exit__(exc_type, exc, traceback)
                    if should_gate:
                        self._gated = True
                        worker_observed_empty.set()
                        allow_empty_worker_exit.wait(2.0)
                    return result

                def wait(self, timeout=None):
                    return original_condition.wait(timeout=timeout)

                def notify_all(self):
                    return original_condition.notify_all()

            manager._CodexUsageMultiMonitor__refresh_condition = (
                _EmptyExitGateCondition()
            )

            try:
                manager.show_account_status("account_1")
                self.assertTrue(first_finished.wait(1.0))
                self.assertTrue(worker_observed_empty.wait(1.0))

                manager.show_account_status("account_2")
                allow_empty_worker_exit.set()

                self.assertTrue(second_finished.wait(1.0))
                self.assertTrue(
                    self._wait_until(
                        lambda: not manager._CodexUsageMultiMonitor__refresh_inflight
                    )
                )
                self.assertEqual(
                    list(manager._CodexUsageMultiMonitor__refresh_queue),
                    [],
                )
            finally:
                allow_empty_worker_exit.set()
                manager.shutdown()

    def test_manager_queue_drains_accepted_requests_when_worker_start_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            start_entered = threading.Event()
            allow_start_failure = threading.Event()
            first_finished = threading.Event()
            second_finished = threading.Event()

            class _RaceChild(_FakeChildMonitor):
                def show_current_status(self, force_refresh=True, source="manual_query"):
                    if "account-1" in self.config_dir:
                        first_finished.set()
                    else:
                        second_finished.set()
                    return super().show_current_status(
                        force_refresh=force_refresh,
                        source=source,
                    )

            class _GatedFailThread:
                def __init__(self, target=None, daemon=None):
                    _ = (target, daemon)

                def start(self):
                    start_entered.set()
                    allow_start_failure.wait(2.0)
                    raise RuntimeError("thread start failed")

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=lambda config_dir, profile_dir: _RaceChild(
                    config_dir,
                    profile_dir,
                ),
            )
            manager.attach(_FakeRoot(), queue.Queue())
            first_caller = threading.Thread(
                target=lambda: manager.show_account_status("account_1"),
                daemon=True,
            )

            try:
                with patch(
                    "src.apps.codex_usage_multi_monitor.threading.Thread",
                    _GatedFailThread,
                ):
                    first_caller.start()
                    self.assertTrue(start_entered.wait(1.0))
                    manager.show_account_status("account_2")
                    allow_start_failure.set()
                    first_caller.join(2.0)

                self.assertFalse(first_caller.is_alive())
                self.assertTrue(first_finished.is_set())
                self.assertTrue(second_finished.is_set())
                self.assertFalse(manager._CodexUsageMultiMonitor__refresh_inflight)
                self.assertEqual(
                    list(manager._CodexUsageMultiMonitor__refresh_queue),
                    [],
                )
            finally:
                allow_start_failure.set()
                manager.shutdown()

    def test_queued_refresh_resolves_current_child_after_profile_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_started = threading.Event()
            release_active = threading.Event()
            queue_drained = threading.Event()

            class _QueuedChild(_FakeChildMonitor):
                def show_current_status(self, force_refresh=True, source="manual_query"):
                    if "account-2" in self.config_dir:
                        active_started.set()
                        release_active.wait(2.0)
                    else:
                        queue_drained.set()
                    return super().show_current_status(
                        force_refresh=force_refresh,
                        source=source,
                    )

            children: list[_QueuedChild] = []

            def factory(config_dir, profile_dir):
                child = _QueuedChild(config_dir, profile_dir)
                children.append(child)
                return child

            manager = CodexUsageMultiMonitor(
                config_dir=os.path.join(tmp, "config"),
                local_base_dir=os.path.join(tmp, "local"),
                monitor_factory=factory,
            )
            manager.attach(_FakeRoot(), queue.Queue())

            manager.show_account_status("account_2")
            self.assertTrue(active_started.wait(1.0))
            manager.show_account_status("account_1")
            ok, error = manager.delete_profile("account_1", confirmed=True)
            self.assertTrue(ok, error)
            release_active.set()
            self.assertTrue(
                self._wait_until(
                    lambda: not manager._CodexUsageMultiMonitor__refresh_inflight,
                )
            )

            self.assertFalse(queue_drained.is_set())
            self.assertEqual(children[0].show_calls, [])

    def test_background_batch_continues_after_one_profile_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, children = self._build_manager(tmp)
            for child in children:
                child.runtime["session_state"] = "logged_in"

            def fail_first(*_args, **_kwargs):
                raise RuntimeError("first profile failed")

            children[0].show_current_status = fail_first
            manager._CodexUsageMultiMonitor__profile_next_collect_due_ts = {
                "account_1": float("inf"),
                "account_2": float("inf"),
            }

            manager._CodexUsageMultiMonitor__refresh_background_accounts(
                source="auto_monitor",
                manage_inflight=False,
                account_ids=("account_1", "account_2"),
            )

            self.assertEqual(
                children[1].show_calls,
                [{"force_refresh": True, "source": "auto_monitor"}],
            )
            due = manager._CodexUsageMultiMonitor__profile_next_collect_due_ts
            self.assertNotEqual(due["account_1"], float("inf"))
            self.assertNotEqual(due["account_2"], float("inf"))


if __name__ == "__main__":
    unittest.main()
