import json
import os
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

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
                {"label": "Codex 보존", "enabled": False},
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
