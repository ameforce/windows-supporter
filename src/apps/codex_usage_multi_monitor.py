from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from src.apps.codex_local_usage import find_latest_windows_codex_usage
from src.apps.codex_usage_monitor import CURRENT_CODEX_USAGE_URL, CodexUsageMonitor, UsageSnapshot
from src.apps.codex_usage_taskbar_overlay import AiUsageTaskbarOverlay


LEGACY_ACCOUNT_IDS = ("account_1", "account_2")
SUPPORTED_PROVIDERS = ("codex", "cursor")
AI_USAGE_SETTINGS_VERSION = 4
TASKBAR_PROFILE_LIMIT = 2
PROFILE_ID_PATTERN = re.compile(r"^(?:account_[12]|profile_[0-9a-f]{32})$")
DEFAULT_LABELS = {
    "account_1": "Codex 1",
    "account_2": "Codex 2",
}
@dataclass(frozen=True)
class _AccountPaths:
    account_id: str
    provider: str
    config_dir: str
    profile_dir: str

    @property
    def settings_path(self) -> str:
        filename = "codex_usage_settings.json" if self.provider == "codex" else "cursor_usage_settings.json"
        return os.path.join(self.config_dir, filename)

    @property
    def state_path(self) -> str:
        filename = "codex_usage_state.json" if self.provider == "codex" else "cursor_usage_state.json"
        return os.path.join(self.config_dir, filename)


@dataclass
class _AccountSettings:
    account_id: str
    label: str
    enabled: bool = True
    provider: str = "codex"
    taskbar_selected: bool = True
    provider_settings: dict[str, dict[str, Any]] = field(default_factory=dict)


class _RecoveryPendingChild:
    def __init__(self, paths: _AccountPaths) -> None:
        self.__paths = paths

    def get_settings_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "provider": self.__paths.provider,
            "collection_supported": False,
            "settings_path": self.__paths.settings_path,
            "state_path": self.__paths.state_path,
            "profile_dir": self.__paths.profile_dir,
        }

    def get_runtime_status(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "provider": self.__paths.provider,
            "monitor_state": "recovery_pending",
            "provider_status": "recovery_pending",
            "session_state": "unknown",
            "collect_inflight": False,
            "auto_monitoring_active": False,
            "can_login": False,
            "can_logout": False,
            "recovery_pending": True,
        }

    def get_last_snapshot(self) -> dict[str, Any]:
        return {}

    def update_settings(self, _data: dict[str, Any]) -> tuple[bool, None]:
        return True, None

    def show_current_status(self, **_kwargs: Any) -> None:
        return None

    def attach(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None


class CodexUsageMultiMonitor:
    def __init__(
        self,
        config_dir: str | None = None,
        local_base_dir: str | None = None,
        monitor_factory: Callable[[str, str], Any] | None = None,
        taskbar_progress_factory: Callable[..., Any] | None = None,
        unrecoverable_timeout_handler: Callable[[], bool] | None = None,
    ) -> None:
        self.__config_dir = self.__resolve_config_dir(config_dir)
        self.__local_base_dir = self.__resolve_local_base_dir(local_base_dir)
        self.__settings_path = os.path.join(
            self.__config_dir,
            "ai_usage_settings.json",
        )
        self.__legacy_manager_settings_path = os.path.join(
            self.__config_dir,
            "codex_usage_multi_settings.json",
        )
        self.__state_path = os.path.join(
            self.__config_dir,
            "codex_usage_multi_state.json",
        )
        self.__cleanup_state_path = os.path.join(
            self.__config_dir,
            "ai_usage_cleanup_state.json",
        )
        self.__settings_recovery_state_path = os.path.join(
            self.__config_dir,
            "ai_usage_settings_recovery.json",
        )
        self.__settings_recovery_profile_ids = self.__read_settings_recovery_profile_ids()
        self.__default_account_id = "account_1"
        self.__account_order = list(LEGACY_ACCOUNT_IDS)
        self.__enabled = True
        self.__taskbar_overlay_enabled = True
        self.__interval_sec = 90.0
        self.__tooltip_duration_ms = 7000
        self.__usage_url = CURRENT_CODEX_USAGE_URL
        self.__refresh_inflight = False
        self.__refresh_lock = threading.Lock()
        self.__unsettled_children: dict[str, list[Any]] = {}
        self.__deferred_cleanup_transaction_ids: set[str] = set()
        self.__root = None
        self.__event_queue = None
        self.__taskbar_progress = None
        self.__taskbar_progress_factory = taskbar_progress_factory or AiUsageTaskbarOverlay
        self.__unrecoverable_timeout_handler = unrecoverable_timeout_handler
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        self.__notification_events: list[dict[str, Any]] = []
        self.__recovery_pending_profile_ids: set[str] = set()
        self.__account_settings = {
            account_id: _AccountSettings(
                account_id=account_id,
                label=DEFAULT_LABELS[account_id],
                enabled=True,
                provider_settings={
                    "codex": {
                        "label": DEFAULT_LABELS[account_id],
                        "enabled": True,
                    }
                },
            )
            for account_id in LEGACY_ACCOUNT_IDS
        }
        manager_settings = self.__read_json_file(self.__settings_path)
        has_manager_settings = isinstance(manager_settings, dict)
        manager_settings_version = (
            _safe_int(manager_settings.get("settings_version", 0), 0)
            if isinstance(manager_settings, dict)
            else 0
        )
        if has_manager_settings and manager_settings_version == 3:
            self.__backup_file_once(
                self.__settings_path,
                os.path.join(self.__config_dir, "ai_usage_settings.v3.backup.json"),
            )
        if not isinstance(manager_settings, dict):
            manager_settings = self.__read_json_file(self.__legacy_manager_settings_path)
            if isinstance(manager_settings, dict):
                self.__backup_file_once(
                    self.__legacy_manager_settings_path,
                    os.path.join(
                        self.__config_dir,
                        "codex_usage_multi_settings.v2.backup.json",
                    ),
                )
        source_settings_version = (
            _safe_int(manager_settings.get("settings_version", 0), 0)
            if isinstance(manager_settings, dict)
            else 0
        )
        self.__load_manager_settings(manager_settings)
        self.__retry_pending_profile_cleanup()
        self.__account_paths = self.__build_account_paths()
        legacy_settings = self.__read_legacy_settings()
        if (
            source_settings_version < AI_USAGE_SETTINGS_VERSION
            and "account_1" in self.__account_settings
            and "account_1" not in self.__recovery_pending_profile_ids
        ):
            self.__migrate_legacy_single_account_files_if_needed()
        if not has_manager_settings or manager_settings_version < AI_USAGE_SETTINGS_VERSION:
            if not isinstance(manager_settings, dict):
                self.__apply_legacy_manager_settings(legacy_settings)
            self.__save_manager_settings()
        self.__monitor_factory = monitor_factory or self.__create_child_monitor
        self.__children = {
            account_id: (
                _RecoveryPendingChild(paths)
                if account_id in self.__recovery_pending_profile_ids
                else self.__invoke_monitor_factory(
                    self.__account_settings[account_id].provider,
                    paths.config_dir,
                    paths.profile_dir,
                    account_id,
                )
            )
            for account_id, paths in self.__account_paths.items()
        }
        self.__retry_pending_settings_recovery()
        return

    def attach(self, root, event_queue=None) -> None:
        self.__root = root
        self.__event_queue = event_queue
        for child in self.__children.values():
            self.__attach_child(child, root, event_queue)
        self.__refresh_taskbar_progress()
        self.__restart_monitor_scheduler(initial_delay_sec=1.0)
        return

    def shutdown(self) -> None:
        self.__clear_monitor_schedule()
        shutdown_children = [
            *self.__children.values(),
            *[
                child
                for children in self.__unsettled_children.values()
                for child in children
            ],
        ]
        self.__unsettled_children = {}
        for child in shutdown_children:
            shutdown = getattr(child, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    pass
        self.__root = None
        self.__event_queue = None
        return

    def get_settings_snapshot(self) -> dict[str, Any]:
        profiles = [
            self.__build_account_settings_snapshot(account_id)
            for account_id in self.__ordered_account_ids()
        ]
        return {
            "settings_version": AI_USAGE_SETTINGS_VERSION,
            "enabled": bool(self.__enabled),
            "taskbar_overlay_enabled": bool(self.__taskbar_overlay_enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
            "collection_mode": "playwright",
            "settings_path": str(self.__settings_path),
            "legacy_settings_path": str(self.__legacy_manager_settings_path),
            "state_path": str(self.__state_path),
            "default_account_id": str(self.__default_account_id),
            "account_order": list(self.__ordered_account_ids()),
            "profile_order": list(self.__ordered_account_ids()),
            "selected_profile_ids": [
                account_id
                for account_id in self.__ordered_account_ids()
                if bool(self.__account_settings[account_id].taskbar_selected)
            ],
            "profiles": profiles,
            "accounts": profiles,
        }

    def update_settings(self, data: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"
        profiles = data.get("profiles")
        legacy_accounts = data.get("accounts")
        raw_profiles = profiles if isinstance(profiles, list) else legacy_accounts
        candidate_settings = {
            profile_id: replace(
                settings,
                provider_settings={
                    provider: dict(provider_data)
                    for provider, provider_data in settings.provider_settings.items()
                },
            )
            for profile_id, settings in self.__account_settings.items()
        }
        candidate_order = list(self.__ordered_account_ids())
        submitted_profile_ids: set[str] | None = None
        if isinstance(raw_profiles, list):
            seen_ids: set[str] = set()
            requested_order: list[str] = []
            for raw in raw_profiles:
                if not isinstance(raw, dict):
                    return False, "invalid_profile"
                profile_id = str(raw.get("id", "") or "")
                if not _is_valid_profile_id(profile_id) or profile_id in seen_ids:
                    return False, "invalid_profile"
                seen_ids.add(profile_id)
                requested_order.append(profile_id)
                current = candidate_settings.get(profile_id)
                provider = str(
                    raw.get("provider", current.provider if current is not None else "codex")
                    or ""
                ).lower()
                if provider not in SUPPORTED_PROVIDERS:
                    return False, "provider"
                if current is None:
                    return False, "invalid_profile"
                else:
                    previous_label = current.label
                    previous_enabled = current.enabled
                    previous_provider = current.provider
                    profile_label_index = self.__profile_label_index(
                        profile_id,
                        previous_provider,
                        previous_label,
                    )
                    previous_label_was_default = previous_label == _default_profile_label(
                        previous_provider,
                        profile_label_index,
                    )
                    current.provider_settings[current.provider] = {
                        "label": current.label,
                        "enabled": bool(current.enabled),
                    }
                    provider_changed = provider != current.provider
                    current.provider = provider
                    had_saved_provider = False
                    if provider_changed:
                        saved_provider = current.provider_settings.get(provider)
                        had_saved_provider = isinstance(saved_provider, dict)
                        if isinstance(saved_provider, dict):
                            current.label = str(
                                saved_provider.get("label")
                                or _default_profile_label(provider, profile_label_index)
                            )
                            current.enabled = bool(saved_provider.get("enabled", True))
                        else:
                            current.label = (
                                _default_profile_label(provider, profile_label_index)
                                if previous_label_was_default
                                else previous_label
                            )
                            current.enabled = previous_enabled
                if "label" in raw:
                    label = str(raw.get("label", "") or "").strip()
                    if label and (
                        not provider_changed
                        or label != previous_label
                        or (not had_saved_provider and not previous_label_was_default)
                    ):
                        current.label = label
                if "enabled" in raw:
                    requested_enabled = bool(raw.get("enabled"))
                    if (
                        not provider_changed
                        or not had_saved_provider
                        or requested_enabled != previous_enabled
                    ):
                        current.enabled = requested_enabled
                if "taskbar_selected" in raw:
                    current.taskbar_selected = bool(raw.get("taskbar_selected"))
                current.provider_settings[current.provider] = {
                    "label": current.label,
                    "enabled": bool(current.enabled),
                }
            candidate_order = requested_order + [
                profile_id for profile_id in candidate_order if profile_id not in seen_ids
            ]
            submitted_profile_ids = set(seen_ids)
        selected_profile_ids = data.get("selected_profile_ids")
        if isinstance(selected_profile_ids, list):
            if len(selected_profile_ids) > TASKBAR_PROFILE_LIMIT:
                return False, "taskbar_profile_limit"
            normalized_selected = [str(item or "") for item in selected_profile_ids]
            if len(set(normalized_selected)) != len(normalized_selected) or any(
                item not in candidate_settings for item in normalized_selected
            ):
                return False, "invalid_taskbar_profile"
            selected = set(normalized_selected)
            selection_scope = set(candidate_settings)
            if (
                submitted_profile_ids is not None
                and len(submitted_profile_ids) < len(candidate_settings)
            ):
                selection_scope = submitted_profile_ids | selected
            for profile_id, settings in candidate_settings.items():
                if profile_id in selection_scope:
                    settings.taskbar_selected = profile_id in selected
        else:
            normalized_selected = None
        requested_profile_order = data.get("profile_order")
        if isinstance(requested_profile_order, list):
            normalized_profile_order = [str(item or "") for item in requested_profile_order]
            if (
                len(normalized_profile_order) != len(set(normalized_profile_order))
                or any(item not in candidate_settings for item in normalized_profile_order)
            ):
                return False, "invalid_profile_order"
            candidate_order = normalized_profile_order + [
                profile_id
                for profile_id in candidate_order
                if profile_id not in normalized_profile_order
            ]
        else:
            normalized_profile_order = None
        if sum(bool(item.taskbar_selected) for item in candidate_settings.values()) > TASKBAR_PROFILE_LIMIT:
            return False, "taskbar_profile_limit"

        requested_default = str(data.get("default_account_id", "") or "")
        if requested_default in candidate_settings:
            candidate_default = requested_default
        elif isinstance(raw_profiles, list):
            candidate_default = candidate_order[0] if candidate_order else ""
        elif self.__default_account_id in candidate_settings:
            candidate_default = self.__default_account_id
        else:
            candidate_default = candidate_order[0] if candidate_order else ""

        candidate_enabled = self.__enabled
        candidate_taskbar_overlay_enabled = self.__taskbar_overlay_enabled
        candidate_interval_sec = self.__interval_sec
        candidate_tooltip_duration_ms = self.__tooltip_duration_ms
        candidate_usage_url = self.__usage_url
        if "enabled" in data:
            candidate_enabled = bool(data.get("enabled"))
        if "taskbar_overlay_enabled" in data:
            candidate_taskbar_overlay_enabled = bool(data.get("taskbar_overlay_enabled"))
        if "interval_sec" in data:
            try:
                interval_sec = float(data.get("interval_sec"))
            except Exception:
                return False, "interval"
            candidate_interval_sec = max(10.0, interval_sec)
        if "tooltip_duration_ms" in data:
            candidate_tooltip_duration_ms = _normalize_tooltip_duration_ms(
                data.get("tooltip_duration_ms"),
                self.__tooltip_duration_ms,
            )
        usage_url = data.get("usage_url")
        if isinstance(usage_url, str) and usage_url.strip():
            candidate_usage_url = usage_url.strip()

        old_settings = self.__account_settings
        old_ids = set(old_settings)
        old_enabled = self.__enabled
        old_interval_sec = self.__interval_sec
        old_tooltip_duration_ms = self.__tooltip_duration_ms
        old_usage_url = self.__usage_url
        changed_providers = [
            profile_id
            for profile_id in candidate_order
            if profile_id in old_ids
            if candidate_settings[profile_id].provider != old_settings[profile_id].provider
        ]
        staged_children: dict[str, tuple[_AccountPaths, Any]] = {}
        staged_rollback_settings: dict[str, dict[str, Any]] = {}
        updated_existing_ids: list[str] = []
        existing_update_ids = [
            profile_id
            for profile_id in candidate_order
            if profile_id not in changed_providers
        ]
        transaction_recovery_ids = list(candidate_order)
        if any(
            not self.__shutdown_unsettled_children(profile_id)
            for profile_id in transaction_recovery_ids
        ):
            return False, "settings_save_failed"
        self.__retry_pending_settings_recovery()
        if any(
            profile_id in self.__settings_recovery_profile_ids
            and isinstance(self.__children.get(profile_id), _RecoveryPendingChild)
            for profile_id in transaction_recovery_ids
        ):
            return False, "settings_save_failed"
        preexisting_recovery_ids = set(self.__settings_recovery_profile_ids)
        try:
            self.__prepare_settings_recovery(transaction_recovery_ids)
            for profile_id in changed_providers:
                staged_children[profile_id] = self.__stage_child_monitor(
                    profile_id,
                    candidate_settings[profile_id],
                )
                staged_child = staged_children[profile_id][1]
                getter = getattr(staged_child, "get_settings_snapshot", None)
                if callable(getter):
                    snapshot = getter()
                    if isinstance(snapshot, dict):
                        staged_rollback_settings[profile_id] = {
                            key: snapshot[key]
                            for key in (
                                "enabled",
                                "interval_sec",
                                "tooltip_duration_ms",
                                "usage_url",
                            )
                            if key in snapshot
                        }
            for profile_id in candidate_order:
                account = candidate_settings[profile_id]
                if profile_id in staged_children:
                    child = staged_children[profile_id][1]
                else:
                    child = self.__children[profile_id]
                    updated_existing_ids.append(profile_id)
                self.__apply_child_settings(
                    child,
                    account,
                    enabled=candidate_enabled,
                    interval_sec=candidate_interval_sec,
                    tooltip_duration_ms=candidate_tooltip_duration_ms,
                    usage_url=candidate_usage_url,
                )
            self.__save_manager_settings(
                account_settings=candidate_settings,
                account_order=candidate_order,
                default_account_id=candidate_default,
                enabled=candidate_enabled,
                taskbar_overlay_enabled=candidate_taskbar_overlay_enabled,
                interval_sec=candidate_interval_sec,
                tooltip_duration_ms=candidate_tooltip_duration_ms,
                usage_url=candidate_usage_url,
            )
        except Exception:
            rollback_failed_ids: set[str] = set()
            for profile_id in reversed(updated_existing_ids):
                try:
                    self.__apply_child_settings(
                        self.__children[profile_id],
                        old_settings[profile_id],
                        enabled=old_enabled,
                        interval_sec=old_interval_sec,
                        tooltip_duration_ms=old_tooltip_duration_ms,
                        usage_url=old_usage_url,
                    )
                except Exception:
                    recovered = self.__replace_child_after_failed_settings_rollback(
                        profile_id,
                        old_settings[profile_id],
                        enabled=old_enabled,
                        interval_sec=old_interval_sec,
                        tooltip_duration_ms=old_tooltip_duration_ms,
                        usage_url=old_usage_url,
                    )
                    if not recovered:
                        rollback_failed_ids.add(profile_id)
            for profile_id, (_paths, child) in staged_children.items():
                rollback = staged_rollback_settings.get(profile_id)
                updater = getattr(child, "update_settings", None)
                if rollback and callable(updater):
                    try:
                        updater(rollback)
                    except Exception:
                        pass
                shutdown = getattr(child, "shutdown", None)
                if callable(shutdown):
                    try:
                        shutdown()
                    except Exception:
                        self.__track_unsettled_child(profile_id, child)
                        rollback_failed_ids.add(profile_id)
            clear_ids = (
                set(transaction_recovery_ids)
                - preexisting_recovery_ids
                - rollback_failed_ids
            )
            self.__complete_settings_recovery(clear_ids)
            return False, "settings_save_failed"
        old_children = {
            profile_id: self.__children.get(profile_id)
            for profile_id in changed_providers
        }
        self.__enabled = candidate_enabled
        self.__taskbar_overlay_enabled = candidate_taskbar_overlay_enabled
        self.__interval_sec = candidate_interval_sec
        self.__tooltip_duration_ms = candidate_tooltip_duration_ms
        self.__usage_url = candidate_usage_url
        self.__account_settings = candidate_settings
        self.__account_order = candidate_order
        self.__default_account_id = candidate_default
        for profile_id, (paths, child) in staged_children.items():
            self.__account_paths[profile_id] = paths
            self.__children[profile_id] = child
        provider_shutdown_failed_ids: set[str] = set()
        for profile_id, old_child in old_children.items():
            shutdown = getattr(old_child, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    self.__track_unsettled_child(profile_id, old_child)
                    provider_shutdown_failed_ids.add(profile_id)
                    replacement = self.__children.get(profile_id)
                    shutdown_replacement = getattr(replacement, "shutdown", None)
                    if callable(shutdown_replacement):
                        try:
                            shutdown_replacement()
                        except Exception:
                            self.__track_unsettled_child(profile_id, replacement)
                    paths = self.__account_paths[profile_id]
                    self.__children[profile_id] = _RecoveryPendingChild(paths)
        self.__complete_settings_recovery(
            set(transaction_recovery_ids)
            - preexisting_recovery_ids
            - provider_shutdown_failed_ids
        )
        self.__restart_monitor_scheduler(initial_delay_sec=1.0)
        self.__refresh_taskbar_progress()
        return True, None

    def add_profile(
        self,
        provider: str = "codex",
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        provider_id = str(provider or "").strip().lower()
        if provider_id not in SUPPORTED_PROVIDERS:
            return False, "provider", None
        profile_id = f"profile_{uuid.uuid4().hex}"
        while profile_id in self.__account_settings:
            profile_id = f"profile_{uuid.uuid4().hex}"
        settings = _AccountSettings(
            account_id=profile_id,
            label=_default_profile_label(provider_id, len(self.__account_settings) + 1),
            enabled=True,
            provider=provider_id,
            taskbar_selected=False,
            provider_settings={
                provider_id: {
                    "label": _default_profile_label(
                        provider_id,
                        len(self.__account_settings) + 1,
                    ),
                    "enabled": True,
                }
            },
        )
        candidate_settings = dict(self.__account_settings)
        candidate_settings[profile_id] = settings
        candidate_order = [*self.__account_order, profile_id]
        candidate_default = self.__default_account_id or profile_id
        planned_paths = self.__build_profile_paths(profile_id, provider_id)
        path_existed_before = {
            os.path.normcase(os.path.abspath(path)): os.path.lexists(path)
            for path in (planned_paths.config_dir, planned_paths.profile_dir)
        }
        add_transaction_id = uuid.uuid4().hex
        local_app_root = os.path.join(self.__local_base_dir, "windows-supporter")
        add_cleanup_entries = [
            {
                "transaction_id": add_transaction_id,
                "profile_id": profile_id,
                "provider": provider_id,
                "path_kind": path_kind,
                "original": path,
                "path": f"{path}.delete-{uuid.uuid4().hex}",
                "root": root,
                "delete_original": not path_existed_before[
                    os.path.normcase(os.path.abspath(path))
                ],
            }
            for path_kind, path, root in (
                ("config", planned_paths.config_dir, self.__config_dir),
                ("profile", planned_paths.profile_dir, local_app_root),
            )
        ]
        if any(
            self.__normalize_cleanup_entry(entry) is None
            for entry in add_cleanup_entries
        ):
            return False, "profile_add_failed", None
        try:
            self.__persist_pending_profile_cleanup(
                [*self.__read_valid_cleanup_entries(), *add_cleanup_entries]
            )
        except Exception:
            return False, "profile_add_failed", None
        staged_paths = None
        staged_child = None
        try:
            staged_paths, staged_child = self.__stage_child_monitor(profile_id, settings)
            self.__apply_child_settings(
                staged_child,
                settings,
                enabled=self.__enabled,
                interval_sec=self.__interval_sec,
                tooltip_duration_ms=self.__tooltip_duration_ms,
                usage_url=self.__usage_url,
            )
            self.__save_manager_settings(
                account_settings=candidate_settings,
                account_order=candidate_order,
                default_account_id=candidate_default,
            )
        except Exception:
            shutdown_succeeded = True
            shutdown = getattr(staged_child, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    shutdown_succeeded = False
            if shutdown_succeeded:
                self.__cleanup_failed_add_paths(
                    profile_id,
                    staged_paths or planned_paths,
                    planned_paths,
                    path_existed_before,
                )
                self.__discard_cleanup_transaction(add_transaction_id)
            elif staged_child is not None:
                self.__track_unsettled_child(profile_id, staged_child)
                self.__deferred_cleanup_transaction_ids.add(add_transaction_id)
            return False, "profile_add_failed", None
        self.__account_settings = candidate_settings
        self.__account_order = candidate_order
        self.__default_account_id = candidate_default
        self.__account_paths[profile_id] = staged_paths
        self.__children[profile_id] = staged_child
        self.__discard_cleanup_transaction(add_transaction_id)
        self.__restart_monitor_scheduler(initial_delay_sec=1.0)
        self.__refresh_taskbar_progress()
        return True, None, self.__build_account_settings_snapshot(profile_id)

    def delete_profile(self, profile_id: str, *, confirmed: bool = False) -> tuple[bool, str | None]:
        normalized = str(profile_id or "")
        if normalized not in self.__account_settings:
            return False, "invalid_profile"
        if not bool(confirmed):
            return False, "confirmation_required"
        if not self.__shutdown_unsettled_children(normalized):
            return False, "profile_delete_failed"
        local_app_root = os.path.join(self.__local_base_dir, "windows-supporter")
        transaction_id = uuid.uuid4().hex
        deletion_entries: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for provider in SUPPORTED_PROVIDERS:
            paths = self.__build_profile_paths(normalized, provider)
            for path_kind, path, root in (
                ("config", paths.config_dir, self.__config_dir),
                ("profile", paths.profile_dir, local_app_root),
            ):
                path_key = os.path.normcase(os.path.abspath(path))
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                deletion_entries.append(
                    {
                        "transaction_id": transaction_id,
                        "profile_id": normalized,
                        "provider": provider,
                        "path_kind": path_kind,
                        "original": path,
                        "path": f"{path}.delete-{uuid.uuid4().hex}",
                        "root": root,
                        "delete_original": True,
                    }
                )
        if any(
            self.__normalize_cleanup_entry(entry) is None
            for entry in deletion_entries
        ):
            return False, "unsafe_profile_path"
        try:
            self.__persist_pending_profile_cleanup(
                [*self.__read_valid_cleanup_entries(), *deletion_entries]
            )
        except Exception:
            return False, "profile_delete_failed"
        child = self.__children.get(normalized)
        recovery_was_pending = normalized in self.__settings_recovery_profile_ids
        try:
            self.__prepare_settings_recovery([normalized])
        except Exception:
            self.__discard_cleanup_transaction(transaction_id)
            return False, "profile_delete_failed"
        shutdown = getattr(child, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                self.__discard_cleanup_transaction(transaction_id)
                if child is not None:
                    self.__track_unsettled_child(normalized, child)
                paths = self.__account_paths.get(normalized)
                if paths is not None:
                    recovery_child = _RecoveryPendingChild(paths)
                    self.__children[normalized] = recovery_child
                    if self.__root is not None:
                        self.__attach_child(
                            recovery_child,
                            self.__root,
                            self.__event_queue,
                        )
                return False, "profile_delete_failed"
        if not recovery_was_pending:
            self.__complete_settings_recovery({normalized})
        quarantined: list[dict[str, Any]] = []
        try:
            for entry in deletion_entries:
                path = entry["original"]
                if not os.path.lexists(path):
                    continue
                os.replace(path, entry["path"])
                quarantined.append(entry)
        except Exception:
            restored = self.__restore_quarantined_profile_paths(quarantined)
            if restored:
                self.__discard_cleanup_transaction(transaction_id)
                self.__restore_child_monitor_or_mark_recovery_pending(normalized)
            else:
                self.__mark_profile_recovery_pending(normalized)
            return False, "profile_delete_failed"
        deleted_settings = replace(
            self.__account_settings[normalized],
            provider_settings={
                provider: dict(provider_data)
                for provider, provider_data in self.__account_settings[
                    normalized
                ].provider_settings.items()
            },
        )
        deleted_paths = self.__account_paths.get(normalized)
        previous_order = list(self.__account_order)
        previous_default = self.__default_account_id
        self.__children.pop(normalized, None)
        self.__account_paths.pop(normalized, None)
        self.__account_settings.pop(normalized, None)
        self.__account_order = [item for item in self.__account_order if item != normalized]
        if self.__default_account_id == normalized:
            self.__default_account_id = self.__account_order[0] if self.__account_order else ""
        try:
            self.__save_manager_settings()
        except Exception:
            self.__account_settings[normalized] = deleted_settings
            self.__account_order = previous_order
            self.__default_account_id = previous_default
            if deleted_paths is not None:
                self.__account_paths[normalized] = deleted_paths
            restored = self.__restore_quarantined_profile_paths(quarantined)
            if restored:
                self.__discard_cleanup_transaction(transaction_id)
                self.__restore_child_monitor_or_mark_recovery_pending(normalized)
            else:
                self.__mark_profile_recovery_pending(normalized)
            return False, "profile_delete_failed"
        self.__restart_monitor_scheduler(initial_delay_sec=1.0)
        self.__refresh_taskbar_progress()
        self.__retry_pending_profile_cleanup()
        self.__set_settings_recovery_pending(normalized, False)
        return True, None

    def get_runtime_status(self) -> dict[str, Any]:
        profile_entries = [
            self.__build_account_runtime_entry(account_id)
            for account_id in self.__ordered_account_ids()
        ]
        active_entries = [
            entry
            for entry in profile_entries
            if bool(self.__enabled) and bool(entry.get("enabled"))
        ]
        runtimes = [entry.get("runtime", {}) for entry in active_entries]
        collect_inflight = bool(self.__refresh_inflight) or any(
            bool(runtime.get("collect_inflight")) for runtime in (entry.get("runtime", {}) for entry in profile_entries)
        )
        return {
            "enabled": bool(self.__enabled),
            "taskbar_overlay_enabled": bool(self.__taskbar_overlay_enabled),
            "monitor_state": self.__aggregate_monitor_state(runtimes),
            "session_state": self.__aggregate_session_state(runtimes),
            "auto_monitoring_active": bool(self.__should_run_background_collection()),
            "collect_inflight": collect_inflight,
            "next_collect_in_sec": self.__get_next_collect_remaining_sec(),
            "can_login": bool(
                not self.__refresh_inflight
                and any(bool(runtime.get("can_login")) for runtime in runtimes)
            ),
            "can_logout": bool(
                not self.__refresh_inflight
                and any(bool(runtime.get("can_logout")) for runtime in runtimes)
            ),
            "default_account_id": str(self.__default_account_id),
            "profile_order": list(self.__ordered_account_ids()),
            "selected_profile_ids": [
                entry["id"] for entry in profile_entries if bool(entry.get("taskbar_selected"))
            ],
            "profiles": profile_entries,
            "accounts": profile_entries,
        }

    def get_last_snapshot(self) -> Any:
        # Compatibility API for legacy single-account callers. Aggregate and
        # per-account consumers should use get_runtime_status()["accounts"].
        if not self.__default_account_id:
            return None
        return self.__child(self.__default_account_id).get_last_snapshot()

    def on_display_topology_changed(self, reason: str = "display_change") -> None:
        self.__refresh_taskbar_progress(
            invalidate_geometry=True,
            rebind_native_owner=str(reason or "") == "taskbar_created",
        )
        return

    def show_current_status(self, force_refresh: bool = True, source: str = "manual_query") -> None:
        source_key = str(source or "manual_query")
        if source_key == "manual_login":
            if self.__default_account_id:
                self.login_account(self.__default_account_id)
            return
        if bool(force_refresh):
            if self.__dispatch_refresh_worker(
                lambda: self.__refresh_accounts(source=source_key, manage_inflight=False),
                refresh_taskbar=True,
            ):
                return
            self.__refresh_accounts(source=source_key)
        self.__refresh_taskbar_progress()
        return

    def show_account_status(
        self,
        account_id: str,
        force_refresh: bool = True,
        source: str = "manual_query",
    ) -> None:
        self.__show_account_status(
            account_id,
            force_refresh=bool(force_refresh),
            source=str(source or "manual_query"),
            refresh_taskbar=True,
        )
        return

    def login_account(self, account_id: str) -> None:
        self.show_account_status(account_id, force_refresh=True, source="manual_login")
        return

    def release_account_profile_session(self, account_id: str) -> tuple[bool, str]:
        child = self.__child(account_id)
        return child.release_profile_session()

    def format_captured_at_for_display(self, value: str) -> str:
        if not self.__default_account_id:
            return str(value or "")
        child = self.__child(self.__default_account_id)
        formatter = getattr(child, "format_captured_at_for_display", None)
        if callable(formatter):
            return str(formatter(value))
        return str(value or "")

    def format_reset_at_for_display(self, value: str, key: str = "") -> str:
        if not self.__default_account_id:
            return str(value or "")
        child = self.__child(self.__default_account_id)
        formatter = getattr(child, "format_reset_at_for_display", None)
        if callable(formatter):
            return str(formatter(value, key=key))
        return str(value or "")

    def pop_notification_events(self) -> list[dict[str, Any]]:
        events = list(self.__notification_events)
        self.__notification_events.clear()
        return events

    def __attach_child(self, child: Any, root: Any, event_queue: Any) -> None:
        attach = getattr(child, "attach", None)
        if not callable(attach):
            return
        if self.__call_supports_keyword(attach, "start_monitor"):
            attach(root, event_queue, start_monitor=False)
            return
        attach(root, event_queue)
        return

    def __track_unsettled_child(self, profile_id: str, child: Any) -> None:
        if child is None:
            return
        tracked = self.__unsettled_children.setdefault(str(profile_id or ""), [])
        if all(existing is not child for existing in tracked):
            tracked.append(child)
        return

    def __shutdown_unsettled_children(self, profile_id: str) -> bool:
        normalized = str(profile_id or "")
        unsettled = self.__unsettled_children.get(normalized, [])
        remaining: list[Any] = []
        for child in unsettled:
            shutdown = getattr(child, "shutdown", None)
            if not callable(shutdown):
                remaining.append(child)
                continue
            try:
                shutdown()
            except Exception:
                remaining.append(child)
        if remaining:
            self.__unsettled_children[normalized] = remaining
            return False
        self.__unsettled_children.pop(normalized, None)
        return True

    def __call_supports_keyword(self, fn: Any, keyword: str) -> bool:
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return True
        for name, parameter in signature.parameters.items():
            if name == keyword:
                return True
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                return True
        return False

    def __refresh_taskbar_progress(
        self,
        *,
        invalidate_geometry: bool = False,
        rebind_native_owner: bool = False,
    ) -> None:
        if self.__root is None:
            return

        def action() -> None:
            try:
                if not bool(self.__taskbar_overlay_enabled):
                    if self.__taskbar_progress is not None:
                        self.__taskbar_progress.hide()
                    return
                progress = self.__ensure_taskbar_progress()
                if progress is not None:
                    if bool(rebind_native_owner):
                        rebind = getattr(progress, "invalidate_native_owner", None)
                        if callable(rebind):
                            rebind()
                    if bool(invalidate_geometry):
                        invalidator = getattr(progress, "invalidate_geometry", None)
                        if callable(invalidator):
                            invalidator()
                    progress.refresh()
            except Exception:
                pass
            return

        if not self.__post_ui(action):
            action()
        return

    def __ensure_taskbar_progress(self):
        if self.__taskbar_progress is not None:
            return self.__taskbar_progress
        factory = self.__taskbar_progress_factory
        self.__taskbar_progress = factory(
            self.__root,
            self.get_runtime_status,
        )
        return self.__taskbar_progress

    def __post_ui(self, fn) -> bool:
        if not callable(fn):
            return False
        queue_obj = self.__event_queue
        if queue_obj is None:
            return False
        try:
            queue_obj.put(fn)
            return True
        except Exception:
            return False

    def __dispatch_refresh_worker(self, fn, *, refresh_taskbar: bool) -> bool:
        if self.__event_queue is None or not callable(fn):
            return False
        with self.__refresh_lock:
            if bool(self.__refresh_inflight):
                if bool(refresh_taskbar):
                    self.__refresh_taskbar_progress()
                return True
            self.__refresh_inflight = True

        def worker() -> None:
            try:
                fn()
            finally:
                with self.__refresh_lock:
                    self.__refresh_inflight = False
                if bool(refresh_taskbar):
                    self.__refresh_taskbar_progress()
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            with self.__refresh_lock:
                self.__refresh_inflight = False
            return False
        if bool(refresh_taskbar):
            self.__refresh_taskbar_progress()
        return True

    def __restart_monitor_scheduler(self, initial_delay_sec: float | None = None) -> None:
        self.__clear_monitor_schedule()
        if not self.__should_run_background_collection():
            return
        self.__schedule_monitor_tick(initial_delay_sec=initial_delay_sec)
        return

    def __clear_monitor_schedule(self) -> None:
        root = self.__root
        after_id = self.__monitor_after_id
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        if root is None or after_id is None:
            return
        try:
            root.after_cancel(after_id)
        except Exception:
            pass
        return

    def __schedule_monitor_tick(self, initial_delay_sec: float | None = None) -> None:
        if not self.__should_run_background_collection():
            self.__monitor_after_id = None
            self.__next_collect_due_ts = 0.0
            return
        root = self.__root
        if root is None:
            return
        delay_sec = float(self.__interval_sec if initial_delay_sec is None else initial_delay_sec)
        if delay_sec < 1.0:
            delay_sec = 1.0
        try:
            import time

            self.__next_collect_due_ts = float(time.monotonic()) + delay_sec
        except Exception:
            self.__next_collect_due_ts = 0.0
        try:
            self.__monitor_after_id = root.after(int(delay_sec * 1000), self.__monitor_tick)
        except Exception:
            self.__monitor_after_id = None
            self.__next_collect_due_ts = 0.0
        return

    def __monitor_tick(self) -> None:
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        if not self.__should_run_background_collection():
            return
        if not self.__dispatch_refresh_worker(
            lambda: self.__refresh_background_accounts(
                source="auto_monitor",
                manage_inflight=False,
            ),
            refresh_taskbar=True,
        ):
            self.__refresh_background_accounts(source="auto_monitor")
            self.__refresh_taskbar_progress()
        self.__schedule_monitor_tick(initial_delay_sec=self.__interval_sec)
        return

    def __refresh_background_accounts(self, source: str, *, manage_inflight: bool = True) -> None:
        if not bool(self.__enabled):
            return
        if bool(manage_inflight):
            with self.__refresh_lock:
                self.__refresh_inflight = True
        try:
            for account_id in self.__background_account_ids():
                self.__show_account_status(
                    account_id,
                    force_refresh=True,
                    source=source,
                    refresh_taskbar=False,
                    allow_async_dispatch=False,
                )
        finally:
            if bool(manage_inflight):
                with self.__refresh_lock:
                    self.__refresh_inflight = False
        return

    def __should_run_background_collection(self) -> bool:
        return bool(self.__enabled and self.__background_scheduler_account_ids())

    def __background_scheduler_account_ids(self) -> list[str]:
        account_ids = []
        for account_id in self.__ordered_account_ids():
            if not bool(self.__account_settings[account_id].enabled):
                continue
            runtime = self.__safe_child_runtime(account_id)
            session_state = str(runtime.get("session_state") or "logged_out")
            if session_state == "logged_in" or bool(runtime.get("collect_inflight")):
                account_ids.append(account_id)
        return account_ids

    def __background_account_ids(self) -> list[str]:
        account_ids = []
        for account_id in self.__ordered_account_ids():
            if not bool(self.__account_settings[account_id].enabled):
                continue
            runtime = self.__safe_child_runtime(account_id)
            if self.__is_background_account_paused(runtime):
                continue
            session_state = str(runtime.get("session_state") or "logged_out")
            if session_state == "logged_in" or bool(runtime.get("collect_inflight")):
                account_ids.append(account_id)
        return account_ids

    def __is_background_account_paused(self, runtime: dict[str, Any]) -> bool:
        if not isinstance(runtime, dict):
            return False
        if bool(runtime.get("collect_inflight")):
            return False
        if bool(runtime.get("auth_attention_required")):
            return True
        monitor_state = str(runtime.get("monitor_state") or "")
        return monitor_state in {"paused_auth_required", "paused_profile_in_use"}

    def __get_next_collect_remaining_sec(self) -> float | None:
        due_ts = float(self.__next_collect_due_ts or 0.0)
        if due_ts <= 0.0:
            return None
        try:
            import time

            remaining = due_ts - float(time.monotonic())
        except Exception:
            return None
        if remaining < 0.0:
            remaining = 0.0
        return remaining

    def __refresh_accounts(self, source: str, *, manage_inflight: bool = True) -> None:
        if not bool(self.__enabled):
            return
        if bool(manage_inflight):
            with self.__refresh_lock:
                self.__refresh_inflight = True
        try:
            for account_id in self.__ordered_account_ids():
                if not bool(self.__account_settings[account_id].enabled):
                    continue
                self.__show_account_status(
                    account_id,
                    force_refresh=True,
                    source=source,
                    refresh_taskbar=False,
                    allow_async_dispatch=False,
                )
        finally:
            if bool(manage_inflight):
                with self.__refresh_lock:
                    self.__refresh_inflight = False
        return

    def __show_account_status(
        self,
        account_id: str,
        *,
        force_refresh: bool,
        source: str,
        refresh_taskbar: bool,
        allow_async_dispatch: bool = True,
    ) -> None:
        child = self.__child(account_id)
        source_key = str(source or "manual_query")
        if (
            bool(allow_async_dispatch)
            and bool(force_refresh)
            and source_key == "manual_login"
            and self.__event_queue is not None
        ):
            with self.__refresh_lock:
                refresh_inflight = bool(self.__refresh_inflight)
            if bool(refresh_inflight):
                def manual_login_worker() -> None:
                    try:
                        child.show_current_status(
                            force_refresh=True,
                            source="manual_login",
                        )
                    finally:
                        if bool(refresh_taskbar):
                            self.__refresh_taskbar_progress()
                    return

                try:
                    threading.Thread(target=manual_login_worker, daemon=True).start()
                except Exception:
                    child.show_current_status(force_refresh=True, source="manual_login")
                if bool(refresh_taskbar):
                    self.__refresh_taskbar_progress()
                return
        if bool(allow_async_dispatch) and bool(force_refresh) and self.__dispatch_refresh_worker(
            lambda: child.show_current_status(
                force_refresh=True,
                source=source_key,
            ),
            refresh_taskbar=bool(refresh_taskbar),
        ):
            return
        child.show_current_status(force_refresh=bool(force_refresh), source=source_key)
        if bool(refresh_taskbar):
            self.__refresh_taskbar_progress()
        return

    def __build_account_settings_snapshot(self, account_id: str) -> dict[str, Any]:
        account = self.__account_settings[account_id]
        child_settings = self.__safe_child_settings(account_id)
        paths = self.__account_paths[account_id]
        return {
            "id": account.account_id,
            "profile_id": account.account_id,
            "provider": account.provider,
            "label": account.label,
            "enabled": bool(account.enabled),
            "taskbar_selected": bool(account.taskbar_selected),
            "provider_settings": {
                provider: dict(provider_data)
                for provider, provider_data in account.provider_settings.items()
            },
            "collection_supported": bool(child_settings.get("collection_supported", True)),
            "config_dir": str(paths.config_dir),
            "settings_path": str(child_settings.get("settings_path") or paths.settings_path),
            "state_path": str(child_settings.get("state_path") or paths.state_path),
            "profile_dir": str(child_settings.get("profile_dir") or paths.profile_dir),
        }

    def __build_account_runtime_entry(self, account_id: str) -> dict[str, Any]:
        account = self.__account_settings[account_id]
        runtime = self.__safe_child_runtime(account_id)
        snapshot = self.__snapshot_to_dict(
            self.__child(account_id).get_last_snapshot(),
            provider=account.provider,
        )
        freshness = str(runtime.get("freshness") or "").strip().lower()
        if not freshness:
            if bool(runtime.get("last_snapshot_is_stale")):
                freshness = "stale"
            elif snapshot.get("captured_at"):
                freshness = "fresh"
            else:
                freshness = "unavailable"
        return {
            "id": account.account_id,
            "profile_id": account.account_id,
            "provider": account.provider,
            "label": self.__display_account_label(account, runtime),
            "configured_label": account.label,
            "enabled": bool(account.enabled),
            "taskbar_selected": bool(account.taskbar_selected),
            "freshness": freshness,
            "provider_status": str(
                runtime.get("provider_status")
                or runtime.get("monitor_state")
                or "unknown"
            ),
            "runtime": runtime,
            "last_snapshot": snapshot,
            "metrics": self.__build_provider_metrics(account.provider, snapshot),
            "usage_history": self.__usage_history_to_dicts(runtime.get("usage_history")),
            "settings": self.__safe_child_settings(account_id),
        }

    def __display_account_label(self, account: _AccountSettings, runtime: dict[str, Any]) -> str:
        profile_name = ""
        if isinstance(runtime, dict):
            profile_name = str(runtime.get("profile_name") or "").strip()
        if profile_name:
            return profile_name
        return str(account.label or DEFAULT_LABELS.get(account.account_id, account.account_id))

    def __safe_child_settings(self, account_id: str) -> dict[str, Any]:
        try:
            data = self.__child(account_id).get_settings_snapshot()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            pass
        paths = self.__account_paths[account_id]
        return {
            "enabled": bool(self.__account_settings[account_id].enabled),
            "provider": self.__account_settings[account_id].provider,
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
            "collection_mode": "playwright",
            "settings_path": paths.settings_path,
            "state_path": paths.state_path,
            "profile_dir": paths.profile_dir,
        }

    def __safe_child_runtime(self, account_id: str) -> dict[str, Any]:
        try:
            data = self.__child(account_id).get_runtime_status()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            pass
        return {
            "enabled": bool(self.__account_settings[account_id].enabled),
            "provider": self.__account_settings[account_id].provider,
            "monitor_state": "unknown",
            "session_state": "unknown",
            "collect_inflight": False,
            "auto_monitoring_active": False,
            "can_login": False,
            "can_logout": False,
        }

    def __aggregate_monitor_state(self, runtimes: list[dict[str, Any]]) -> str:
        if not runtimes:
            return "idle"
        states = [str(runtime.get("monitor_state") or "idle") for runtime in runtimes]
        if any(bool(runtime.get("collect_inflight")) or state == "running" for runtime, state in zip(runtimes, states)):
            return "running"
        if "paused_auth_required" in states:
            return "paused_auth_required"
        if "paused_profile_in_use" in states:
            return "paused_profile_in_use"
        unique = {state for state in states if state}
        if len(unique) > 1:
            return "mixed"
        return states[0] if states else "idle"

    def __aggregate_session_state(self, runtimes: list[dict[str, Any]]) -> str:
        if not runtimes:
            return "logged_out"
        states = {str(runtime.get("session_state") or "unknown") for runtime in runtimes}
        if len(states) > 1:
            return "mixed"
        return next(iter(states), "unknown")

    def __sync_child_settings(self) -> None:
        for account_id in self.__ordered_account_ids():
            account = self.__account_settings[account_id]
            child = self.__child(account_id)
            self.__apply_child_settings(
                child,
                account,
                enabled=self.__enabled,
                interval_sec=self.__interval_sec,
                tooltip_duration_ms=self.__tooltip_duration_ms,
                usage_url=self.__usage_url,
            )
        return

    def __apply_child_settings(
        self,
        child: Any,
        account: _AccountSettings,
        *,
        enabled: bool,
        interval_sec: float,
        tooltip_duration_ms: int,
        usage_url: str,
    ) -> None:
        updater = getattr(child, "update_settings", None)
        if not callable(updater):
            return
        child_data = {
            "enabled": bool(enabled and account.enabled),
            "interval_sec": float(interval_sec),
            "tooltip_duration_ms": int(tooltip_duration_ms),
        }
        if account.provider == "codex":
            child_data["usage_url"] = str(usage_url)
        result = updater(child_data)
        if isinstance(result, tuple) and result and result[0] is False:
            raise RuntimeError(str(result[1] if len(result) > 1 else "child_settings_failed"))
        return

    def __child(self, account_id: str) -> Any:
        normalized = str(account_id or "")
        if normalized not in self.__children:
            raise ValueError(f"unknown AI usage profile id: {account_id}")
        return self.__children[normalized]

    def __ordered_account_ids(self) -> list[str]:
        ordered = []
        for account_id in self.__account_order:
            if account_id in self.__account_settings and account_id not in ordered:
                ordered.append(account_id)
        ordered.extend(
            account_id for account_id in self.__account_settings if account_id not in ordered
        )
        return ordered

    def __build_account_paths(self) -> dict[str, _AccountPaths]:
        result: dict[str, _AccountPaths] = {}
        for account_id in self.__ordered_account_ids():
            provider = self.__account_settings[account_id].provider
            result[account_id] = self.__build_profile_paths(account_id, provider)
        return result

    def __profile_label_index(
        self,
        profile_id: str,
        provider: str,
        label: str,
    ) -> int:
        normalized_id = str(profile_id or "")
        if normalized_id in LEGACY_ACCOUNT_IDS:
            return max(1, _safe_int(normalized_id.rsplit("_", 1)[-1], 1))
        provider_name = "Cursor" if str(provider or "").lower() == "cursor" else "Codex"
        match = re.fullmatch(rf"{re.escape(provider_name)} ([1-9]\d*)", str(label or ""))
        if match is not None:
            return max(1, _safe_int(match.group(1), 1))
        ordered = self.__ordered_account_ids()
        try:
            return ordered.index(normalized_id) + 1
        except ValueError:
            return len(ordered) + 1

    def __build_profile_paths(self, account_id: str, provider: str) -> _AccountPaths:
        normalized_id = str(account_id or "")
        normalized_provider = str(provider or "").lower()
        local_app_base = os.path.join(self.__local_base_dir, "windows-supporter")
        if normalized_id in LEGACY_ACCOUNT_IDS:
            slot_number = normalized_id.rsplit("_", 1)[-1]
            if normalized_provider == "codex":
                config_dir = os.path.join(self.__config_dir, f"codex-account-{slot_number}")
                profile_dir = os.path.join(
                    local_app_base,
                    f"chatgpt-profile-account-{slot_number}",
                )
            else:
                config_dir = os.path.join(self.__config_dir, f"cursor-account-{slot_number}")
                profile_dir = os.path.join(
                    local_app_base,
                    f"cursor-profile-account-{slot_number}",
                )
        else:
            config_dir = os.path.join(
                self.__config_dir,
                "ai-profiles",
                normalized_id,
                normalized_provider,
            )
            profile_dir = os.path.join(
                local_app_base,
                "ai-profiles",
                normalized_id,
                normalized_provider,
            )
        return _AccountPaths(
            account_id=normalized_id,
            provider=normalized_provider,
            config_dir=config_dir,
            profile_dir=profile_dir,
        )

    def __load_manager_settings(self, data: dict | None) -> None:
        if not isinstance(data, dict):
            return
        if "enabled" in data:
            self.__enabled = bool(data.get("enabled"))
        if "taskbar_overlay_enabled" in data:
            self.__taskbar_overlay_enabled = bool(data.get("taskbar_overlay_enabled"))
        try:
            self.__interval_sec = float(data.get("interval_sec", self.__interval_sec))
        except Exception:
            self.__interval_sec = 90.0
        self.__tooltip_duration_ms = _normalize_tooltip_duration_ms(
            data.get("tooltip_duration_ms", self.__tooltip_duration_ms),
            self.__tooltip_duration_ms,
        )
        usage_url = data.get("usage_url")
        if isinstance(usage_url, str) and usage_url.strip():
            self.__usage_url = usage_url.strip()
        profiles = data.get("profiles")
        accounts = profiles if isinstance(profiles, list) else data.get("accounts")
        settings_version = _safe_int(data.get("settings_version", 0), 0)
        is_v4 = settings_version >= AI_USAGE_SETTINGS_VERSION
        selected_profile_ids = data.get("selected_profile_ids")
        selected_ids = (
            {str(item or "") for item in selected_profile_ids}
            if isinstance(selected_profile_ids, list)
            else None
        )
        if isinstance(accounts, list):
            loaded_settings: dict[str, _AccountSettings] = (
                {}
                if is_v4
                else {
                    profile_id: replace(
                        settings,
                        provider_settings={
                            provider: dict(provider_data)
                            for provider, provider_data in settings.provider_settings.items()
                        },
                    )
                    for profile_id, settings in self.__account_settings.items()
                }
            )
            seen_loaded: set[str] = set()
            for index, raw in enumerate(accounts):
                if not isinstance(raw, dict):
                    continue
                account_id = str(raw.get("id", "") or "")
                if not _is_valid_profile_id(account_id) or account_id in seen_loaded:
                    continue
                seen_loaded.add(account_id)
                provider = str(raw.get("provider", "codex") or "codex").lower()
                if provider not in SUPPORTED_PROVIDERS:
                    provider = "codex"
                enabled = bool(raw.get("enabled", True))
                if selected_ids is not None:
                    taskbar_selected = account_id in selected_ids
                elif "taskbar_selected" in raw:
                    taskbar_selected = bool(raw.get("taskbar_selected"))
                else:
                    taskbar_selected = bool(enabled and not is_v4)
                provider_settings = _normalize_provider_settings(raw.get("provider_settings"))
                provider_settings.setdefault(
                    provider,
                    {
                        "label": str(
                            raw.get("label") or _default_profile_label(provider, index + 1)
                        ),
                        "enabled": enabled,
                    },
                )
                loaded_settings[account_id] = _AccountSettings(
                    account_id=account_id,
                    label=str(raw.get("label") or _default_profile_label(provider, index + 1)),
                    enabled=enabled,
                    provider=provider,
                    taskbar_selected=taskbar_selected,
                    provider_settings=provider_settings,
                )
            if is_v4 or loaded_settings:
                self.__account_settings = loaded_settings

        legacy_codex_accounts = data.get("legacy_codex_accounts")
        if isinstance(legacy_codex_accounts, list):
            for raw in legacy_codex_accounts:
                if not isinstance(raw, dict):
                    continue
                account_id = str(raw.get("id", "") or "")
                settings = self.__account_settings.get(account_id)
                if settings is None:
                    continue
                settings.provider_settings["codex"] = {
                    "label": str(raw.get("label") or _default_profile_label("codex", 1)),
                    "enabled": bool(raw.get("enabled", True)),
                }

        account_order = data.get("profile_order", data.get("account_order"))
        if isinstance(account_order, list):
            ordered = []
            for account_id in account_order:
                normalized = str(account_id or "")
                if normalized in self.__account_settings and normalized not in ordered:
                    ordered.append(normalized)
            self.__account_order = ordered + [
                account_id for account_id in self.__account_settings if account_id not in ordered
            ]
        else:
            self.__account_order = list(self.__account_settings)

        selected_count = 0
        for profile_id in self.__ordered_account_ids():
            settings = self.__account_settings[profile_id]
            if not settings.taskbar_selected:
                continue
            selected_count += 1
            if selected_count > TASKBAR_PROFILE_LIMIT:
                settings.taskbar_selected = False

        default_account_id = str(data.get("default_account_id", "") or "")
        if default_account_id in self.__account_settings:
            self.__default_account_id = default_account_id
        else:
            ordered_ids = self.__ordered_account_ids()
            self.__default_account_id = ordered_ids[0] if ordered_ids else ""
        return

    def __save_manager_settings(
        self,
        *,
        account_settings: dict[str, _AccountSettings] | None = None,
        account_order: list[str] | None = None,
        default_account_id: str | None = None,
        enabled: bool | None = None,
        taskbar_overlay_enabled: bool | None = None,
        interval_sec: float | None = None,
        tooltip_duration_ms: int | None = None,
        usage_url: str | None = None,
    ) -> None:
        settings = account_settings if account_settings is not None else self.__account_settings
        requested_order = account_order if account_order is not None else self.__ordered_account_ids()
        ordered_ids: list[str] = []
        for account_id in requested_order:
            if account_id in settings and account_id not in ordered_ids:
                ordered_ids.append(account_id)
        ordered_ids.extend(account_id for account_id in settings if account_id not in ordered_ids)
        profiles = [
            {
                "id": account_id,
                "provider": settings[account_id].provider,
                "label": settings[account_id].label,
                "enabled": bool(settings[account_id].enabled),
                "taskbar_selected": bool(settings[account_id].taskbar_selected),
                "provider_settings": {
                    provider: dict(provider_data)
                    for provider, provider_data in settings[account_id].provider_settings.items()
                },
            }
            for account_id in ordered_ids
        ]
        payload = {
            "settings_version": AI_USAGE_SETTINGS_VERSION,
            "enabled": bool(self.__enabled if enabled is None else enabled),
            "taskbar_overlay_enabled": bool(
                self.__taskbar_overlay_enabled
                if taskbar_overlay_enabled is None
                else taskbar_overlay_enabled
            ),
            "interval_sec": float(self.__interval_sec if interval_sec is None else interval_sec),
            "tooltip_duration_ms": int(
                self.__tooltip_duration_ms
                if tooltip_duration_ms is None
                else tooltip_duration_ms
            ),
            "usage_url": str(self.__usage_url if usage_url is None else usage_url),
            "default_account_id": str(
                self.__default_account_id
                if default_account_id is None
                else default_account_id
            ),
            "account_order": list(ordered_ids),
            "profile_order": list(ordered_ids),
            "selected_profile_ids": [
                account_id
                for account_id in ordered_ids
                if bool(settings[account_id].taskbar_selected)
            ],
            "profiles": profiles,
            "accounts": profiles,
        }
        self.__write_json_file(self.__settings_path, payload)
        return

    def __read_legacy_settings(self) -> dict | None:
        return self.__read_json_file(os.path.join(self.__config_dir, "codex_usage_settings.json"))

    def __apply_legacy_manager_settings(self, data: dict | None) -> None:
        if not isinstance(data, dict):
            return
        if "enabled" in data:
            self.__enabled = bool(data.get("enabled"))
        if "taskbar_overlay_enabled" in data:
            self.__taskbar_overlay_enabled = bool(data.get("taskbar_overlay_enabled"))
        if "interval_sec" in data:
            try:
                interval_sec = float(data.get("interval_sec"))
            except Exception:
                interval_sec = self.__interval_sec
            if interval_sec < 10.0:
                interval_sec = 10.0
            self.__interval_sec = float(interval_sec)
        if "tooltip_duration_ms" in data:
            self.__tooltip_duration_ms = _normalize_tooltip_duration_ms(
                data.get("tooltip_duration_ms"),
                self.__tooltip_duration_ms,
            )
        usage_url = data.get("usage_url")
        if isinstance(usage_url, str) and usage_url.strip():
            self.__usage_url = usage_url.strip()
        return

    def __migrate_legacy_single_account_files_if_needed(self) -> None:
        local_app_base = os.path.join(self.__local_base_dir, "windows-supporter")
        account_1 = _AccountPaths(
            account_id="account_1",
            provider="codex",
            config_dir=os.path.join(self.__config_dir, "codex-account-1"),
            profile_dir=os.path.join(local_app_base, "chatgpt-profile-account-1"),
        )
        legacy_settings = os.path.join(self.__config_dir, "codex_usage_settings.json")
        legacy_state = os.path.join(self.__config_dir, "codex_usage_state.json")
        self.__copy_if_missing(legacy_settings, account_1.settings_path)
        self.__copy_if_missing(legacy_state, account_1.state_path)
        return

    def __copy_if_missing(self, source: str, target: str) -> None:
        if not os.path.isfile(source) or os.path.exists(target):
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        return

    def __backup_file_once(self, source: str, target: str) -> None:
        if not os.path.isfile(source) or os.path.exists(target):
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temp_path = f"{target}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copy2(source, temp_path)
            if not os.path.exists(target):
                os.replace(temp_path, target)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
        return

    def __restore_quarantined_profile_paths(
        self,
        quarantined: list[dict[str, Any]],
    ) -> bool:
        restored = True
        for entry in reversed(quarantined):
            original = entry["original"]
            quarantine = entry["path"]
            try:
                if os.path.lexists(quarantine):
                    if os.path.lexists(original):
                        restored = False
                    else:
                        os.replace(quarantine, original)
            except Exception:
                restored = False
        return restored

    def __read_valid_cleanup_entries(self) -> list[dict[str, Any]]:
        data = self.__read_json_file(self.__cleanup_state_path)
        raw_paths = data.get("paths") if isinstance(data, dict) else None
        if not isinstance(raw_paths, list):
            return []
        return [
            normalized
            for raw in raw_paths
            if (normalized := self.__normalize_cleanup_entry(raw)) is not None
        ]

    def __retry_pending_profile_cleanup(self) -> None:
        previous_recovery_pending = set(self.__recovery_pending_profile_ids)
        if not os.path.isfile(self.__cleanup_state_path):
            self.__recovery_pending_profile_ids.clear()
            self.__reconcile_recovery_children(previous_recovery_pending)
            if isinstance(getattr(self, "_CodexUsageMultiMonitor__children", None), dict):
                self.__retry_pending_settings_recovery()
            return
        pending: list[dict[str, Any]] = []
        recovery_pending: set[str] = set()
        for entry in self.__read_valid_cleanup_entries():
            path = entry["path"]
            original = entry["original"]
            if entry["transaction_id"] in self.__deferred_cleanup_transaction_ids:
                pending.append(entry)
                continue
            try:
                if entry["profile_id"] in self.__account_settings:
                    if os.path.lexists(path) and not os.path.lexists(original):
                        os.replace(path, original)
                    elif os.path.lexists(path):
                        pending.append(entry)
                        recovery_pending.add(entry["profile_id"])
                    continue
                cleanup_candidates = [path]
                if bool(entry.get("delete_original", True)):
                    cleanup_candidates.insert(0, original)
                for candidate in cleanup_candidates:
                    if os.path.isdir(candidate):
                        shutil.rmtree(candidate)
                    elif os.path.lexists(candidate):
                        os.remove(candidate)
            except Exception:
                pending.append(entry)
                if entry["profile_id"] in self.__account_settings:
                    recovery_pending.add(entry["profile_id"])
        self.__recovery_pending_profile_ids = recovery_pending
        try:
            self.__persist_pending_profile_cleanup(pending)
        except Exception:
            pass
        self.__reconcile_recovery_children(previous_recovery_pending)
        if isinstance(getattr(self, "_CodexUsageMultiMonitor__children", None), dict):
            self.__retry_pending_settings_recovery()
        return

    def __reconcile_recovery_children(self, previous_profile_ids: set[str]) -> None:
        children = getattr(self, "_CodexUsageMultiMonitor__children", None)
        if not isinstance(children, dict):
            return
        current_profile_ids = set(self.__recovery_pending_profile_ids)
        for profile_id in current_profile_ids - set(previous_profile_ids):
            if profile_id in self.__account_settings:
                self.__mark_profile_recovery_pending(profile_id)
        for profile_id in set(previous_profile_ids) - current_profile_ids:
            if profile_id not in self.__account_settings:
                continue
            try:
                self.__replace_child_monitor(profile_id)
            except Exception:
                self.__mark_profile_recovery_pending(profile_id)
        return

    def __mark_profile_recovery_pending(self, profile_id: str) -> None:
        normalized = str(profile_id or "")
        paths = self.__build_account_paths().get(normalized)
        if paths is None:
            return
        old_child = self.__children.get(normalized)
        shutdown = getattr(old_child, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                pass
        self.__recovery_pending_profile_ids.add(normalized)
        child = _RecoveryPendingChild(paths)
        self.__account_paths[normalized] = paths
        self.__children[normalized] = child
        if self.__root is not None:
            self.__attach_child(child, self.__root, self.__event_queue)
        return

    def __restore_child_monitor_or_mark_recovery_pending(self, profile_id: str) -> None:
        try:
            self.__replace_child_monitor(profile_id)
        except Exception:
            self.__mark_profile_recovery_pending(profile_id)
        return

    def __discard_cleanup_transaction(self, transaction_id: str) -> None:
        pending = [
            entry
            for entry in self.__read_valid_cleanup_entries()
            if entry["transaction_id"] != transaction_id
        ]
        try:
            self.__persist_pending_profile_cleanup(pending)
        except Exception:
            pass
        return

    def __read_settings_recovery_profile_ids(self) -> set[str]:
        data = self.__read_json_file(self.__settings_recovery_state_path)
        if not isinstance(data, dict) or _safe_int(data.get("schema_version", 0), 0) != 1:
            return set()
        raw_ids = data.get("profile_ids")
        if not isinstance(raw_ids, list):
            return set()
        return {
            str(profile_id)
            for profile_id in raw_ids
            if _is_valid_profile_id(str(profile_id or ""))
        }

    def __persist_settings_recovery_profile_ids(self) -> None:
        if self.__settings_recovery_profile_ids:
            self.__write_json_file(
                self.__settings_recovery_state_path,
                {
                    "schema_version": 1,
                    "profile_ids": sorted(self.__settings_recovery_profile_ids),
                },
            )
            return
        if os.path.isfile(self.__settings_recovery_state_path):
            os.remove(self.__settings_recovery_state_path)
        return

    def __set_settings_recovery_pending(self, profile_id: str, pending: bool) -> None:
        normalized = str(profile_id or "")
        if not _is_valid_profile_id(normalized):
            return
        if pending:
            self.__settings_recovery_profile_ids.add(normalized)
        else:
            self.__settings_recovery_profile_ids.discard(normalized)
        try:
            self.__persist_settings_recovery_profile_ids()
        except Exception:
            pass
        return

    def __prepare_settings_recovery(self, profile_ids: list[str]) -> None:
        previous = set(self.__settings_recovery_profile_ids)
        self.__settings_recovery_profile_ids.update(
            profile_id for profile_id in profile_ids if _is_valid_profile_id(profile_id)
        )
        try:
            self.__persist_settings_recovery_profile_ids()
        except Exception:
            self.__settings_recovery_profile_ids = previous
            raise
        return

    def __complete_settings_recovery(self, profile_ids: set[str]) -> None:
        if not profile_ids:
            return
        previous = set(self.__settings_recovery_profile_ids)
        self.__settings_recovery_profile_ids.difference_update(profile_ids)
        try:
            self.__persist_settings_recovery_profile_ids()
        except Exception:
            self.__settings_recovery_profile_ids = previous
        return

    def __retry_pending_settings_recovery(self) -> None:
        for profile_id in list(self.__settings_recovery_profile_ids):
            account = self.__account_settings.get(profile_id)
            child = self.__children.get(profile_id)
            paths = self.__account_paths.get(profile_id)
            if account is None or child is None or paths is None:
                self.__settings_recovery_profile_ids.discard(profile_id)
                continue
            if (
                profile_id in self.__recovery_pending_profile_ids
                or bool(self.__unsettled_children.get(profile_id))
            ):
                continue
            if isinstance(child, _RecoveryPendingChild):
                try:
                    replacement_paths, replacement_child = self.__stage_child_monitor(
                        profile_id,
                        account,
                    )
                except Exception:
                    continue
                self.__account_paths[profile_id] = replacement_paths
                self.__children[profile_id] = replacement_child
                child = replacement_child
            try:
                self.__apply_child_settings(
                    child,
                    account,
                    enabled=self.__enabled,
                    interval_sec=self.__interval_sec,
                    tooltip_duration_ms=self.__tooltip_duration_ms,
                    usage_url=self.__usage_url,
                )
            except Exception:
                shutdown = getattr(child, "shutdown", None)
                if callable(shutdown):
                    try:
                        shutdown()
                    except Exception:
                        self.__track_unsettled_child(profile_id, child)
                self.__children[profile_id] = _RecoveryPendingChild(paths)
                continue
            self.__settings_recovery_profile_ids.discard(profile_id)
        try:
            self.__persist_settings_recovery_profile_ids()
        except Exception:
            pass
        return

    def __normalize_cleanup_entry(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        transaction_id = str(raw.get("transaction_id") or "")
        profile_id = str(raw.get("profile_id") or "")
        provider = str(raw.get("provider") or "").lower()
        path_kind = str(raw.get("path_kind") or "").lower()
        original = os.path.abspath(str(raw.get("original") or ""))
        quarantine = os.path.abspath(str(raw.get("path") or ""))
        delete_original = raw.get("delete_original", True)
        if (
            re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None
            or not _is_valid_profile_id(profile_id)
            or provider not in SUPPORTED_PROVIDERS
            or path_kind not in {"config", "profile"}
            or not isinstance(delete_original, bool)
        ):
            return None
        paths = self.__build_profile_paths(profile_id, provider)
        expected_original = paths.config_dir if path_kind == "config" else paths.profile_dir
        expected_root = (
            self.__config_dir
            if path_kind == "config"
            else os.path.join(self.__local_base_dir, "windows-supporter")
        )
        expected_prefix = f"{os.path.abspath(expected_original)}.delete-"
        if (
            os.path.normcase(original) != os.path.normcase(os.path.abspath(expected_original))
            or not os.path.normcase(quarantine).startswith(os.path.normcase(expected_prefix))
            or re.fullmatch(r"[0-9a-f]{32}", quarantine[len(expected_prefix) :]) is None
            or not self.__is_owned_deletion_path(original, expected_root)
            or not self.__is_owned_deletion_path(quarantine, expected_root)
        ):
            return None
        return {
            "transaction_id": transaction_id,
            "profile_id": profile_id,
            "provider": provider,
            "path_kind": path_kind,
            "original": os.path.abspath(expected_original),
            "path": quarantine,
            "root": os.path.abspath(expected_root),
            "delete_original": delete_original,
        }

    def __persist_pending_profile_cleanup(self, pending: list[dict[str, Any]]) -> None:
        if pending:
            self.__write_json_file(
                self.__cleanup_state_path,
                {
                    "schema_version": 1,
                    "paths": pending,
                },
            )
            return
        try:
            if os.path.isfile(self.__cleanup_state_path):
                os.remove(self.__cleanup_state_path)
        except Exception:
            pass
        return

    def __cleanup_failed_add_paths(
        self,
        profile_id: str,
        staged_paths: _AccountPaths,
        planned_paths: _AccountPaths,
        path_existed_before: dict[str, bool],
    ) -> None:
        local_app_root = os.path.join(self.__local_base_dir, "windows-supporter")
        for staged_path, planned_path, root in (
            (staged_paths.config_dir, planned_paths.config_dir, self.__config_dir),
            (staged_paths.profile_dir, planned_paths.profile_dir, local_app_root),
        ):
            staged_key = os.path.normcase(os.path.abspath(staged_path))
            planned_key = os.path.normcase(os.path.abspath(planned_path))
            if staged_key != planned_key or path_existed_before.get(planned_key, True):
                continue
            if not self.__is_owned_deletion_path(staged_path, root):
                continue
            try:
                if os.path.isdir(staged_path):
                    shutil.rmtree(staged_path)
                elif os.path.lexists(staged_path):
                    os.remove(staged_path)
                profile_root = os.path.dirname(staged_path)
                if (
                    os.path.basename(profile_root) == profile_id
                    and self.__is_owned_deletion_path(profile_root, root)
                    and os.path.isdir(profile_root)
                    and not os.listdir(profile_root)
                ):
                    os.rmdir(profile_root)
            except Exception:
                pass
        return

    def __is_owned_deletion_path(self, path: str, root: str) -> bool:
        candidate = os.path.abspath(path)
        boundary = os.path.abspath(root)
        try:
            if os.path.normcase(os.path.commonpath((candidate, boundary))) != os.path.normcase(boundary):
                return False
            if os.path.normcase(candidate) == os.path.normcase(boundary):
                return False
            real_candidate = os.path.realpath(candidate)
            real_boundary = os.path.realpath(boundary)
            if os.path.normcase(os.path.commonpath((real_candidate, real_boundary))) != os.path.normcase(
                real_boundary
            ):
                return False
            relative = os.path.relpath(candidate, boundary)
        except (OSError, ValueError):
            return False
        current = boundary
        owned_components = [boundary]
        for part in relative.split(os.sep):
            current = os.path.join(current, part)
            owned_components.append(current)
        for current in owned_components:
            if not os.path.lexists(current):
                continue
            try:
                info = os.lstat(current)
            except OSError:
                return False
            attributes = int(getattr(info, "st_file_attributes", 0) or 0)
            reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
            if stat.S_ISLNK(info.st_mode) or (reparse_flag and attributes & reparse_flag):
                return False
        return True

    def __read_json_file(self, path: str) -> dict | None:
        try:
            if not os.path.isfile(path):
                return None
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def __write_json_file(self, path: str, payload: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
        return

    def __snapshot_to_dict(self, snapshot: Any, *, provider: str = "codex") -> dict[str, Any]:
        if isinstance(snapshot, dict):
            return dict(snapshot)
        to_dict = getattr(snapshot, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            if isinstance(data, dict):
                return dict(data)
        if str(provider or "") != "codex":
            return {}
        return UsageSnapshot().to_dict()

    def __build_provider_metrics(
        self,
        provider: str,
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        existing = snapshot.get("metrics")
        if isinstance(existing, list):
            return [dict(item) for item in existing if isinstance(item, dict)][:2]
        if str(provider or "") == "cursor":
            return [
                {
                    "key": "included_usage",
                    "short_label": "INC",
                    "percent": _optional_percent(snapshot.get("included_remaining_percent")),
                    "value_text": str(snapshot.get("included_usage") or "조회 불가"),
                    "reset_at": str(snapshot.get("billing_reset_at") or ""),
                    "state": str(snapshot.get("state") or "unavailable"),
                },
                {
                    "key": "on_demand",
                    "short_label": "OD",
                    "percent": None,
                    "value_text": str(snapshot.get("on_demand_status") or "조회 불가"),
                    "reset_at": "",
                    "state": str(snapshot.get("on_demand_state") or snapshot.get("state") or "unavailable"),
                },
            ]
        return [
            {
                "key": "five_hour_limit",
                "short_label": "5H",
                "percent": _optional_percent(snapshot.get("five_hour_limit")),
                "value_text": str(snapshot.get("five_hour_limit") or "--"),
                "reset_at": str(snapshot.get("five_hour_reset_at") or ""),
                "state": "ready" if snapshot.get("five_hour_limit") else "unavailable",
            },
            {
                "key": "weekly_limit",
                "short_label": "7D",
                "percent": _optional_percent(snapshot.get("weekly_limit")),
                "value_text": str(snapshot.get("weekly_limit") or "--"),
                "reset_at": str(snapshot.get("weekly_reset_at") or ""),
                "state": "ready" if snapshot.get("weekly_limit") else "unavailable",
            },
        ]

    def __usage_history_to_dicts(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def __invoke_monitor_factory(
        self,
        provider: str,
        config_dir: str,
        profile_dir: str,
        profile_id: str,
    ) -> Any:
        factory = self.__monitor_factory
        candidates = (
            (provider, config_dir, profile_dir, profile_id),
            (provider, config_dir, profile_dir),
            (config_dir, profile_dir),
        )
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return factory(provider, config_dir, profile_dir, profile_id)
        for args in candidates:
            try:
                signature.bind(*args)
            except TypeError:
                continue
            return factory(*args)
        return factory(config_dir, profile_dir)

    def __replace_child_monitor(self, account_id: str) -> None:
        old_child = self.__children.get(account_id)
        paths, child = self.__stage_child_monitor(
            account_id,
            self.__account_settings[account_id],
        )
        self.__account_paths[account_id] = paths
        self.__children[account_id] = child
        shutdown_old = getattr(old_child, "shutdown", None)
        if callable(shutdown_old):
            try:
                shutdown_old()
            except Exception:
                pass
        return

    def __replace_child_after_failed_settings_rollback(
        self,
        account_id: str,
        account: _AccountSettings,
        *,
        enabled: bool,
        interval_sec: float,
        tooltip_duration_ms: int,
        usage_url: str,
    ) -> bool:
        original_child = self.__children.get(account_id)
        original_paths = self.__account_paths[account_id]
        replacement_paths = original_paths
        replacement_child = None
        try:
            replacement_paths, replacement_child = self.__stage_child_monitor(
                account_id,
                account,
            )
            self.__apply_child_settings(
                replacement_child,
                account,
                enabled=enabled,
                interval_sec=interval_sec,
                tooltip_duration_ms=tooltip_duration_ms,
                usage_url=usage_url,
            )
        except Exception:
            shutdown_replacement = getattr(replacement_child, "shutdown", None)
            if callable(shutdown_replacement):
                try:
                    shutdown_replacement()
                except Exception:
                    pass
            replacement_paths = original_paths
            replacement_child = _RecoveryPendingChild(original_paths)
            if self.__root is not None:
                self.__attach_child(replacement_child, self.__root, self.__event_queue)
            recovered = False
        else:
            recovered = True
        shutdown_original = getattr(original_child, "shutdown", None)
        if callable(shutdown_original):
            try:
                shutdown_original()
            except Exception:
                self.__track_unsettled_child(account_id, original_child)
                shutdown_replacement = getattr(replacement_child, "shutdown", None)
                if callable(shutdown_replacement):
                    try:
                        shutdown_replacement()
                    except Exception:
                        self.__track_unsettled_child(account_id, replacement_child)
                replacement_paths = original_paths
                replacement_child = _RecoveryPendingChild(original_paths)
                if self.__root is not None:
                    self.__attach_child(
                        replacement_child,
                        self.__root,
                        self.__event_queue,
                    )
                recovered = False
        self.__account_paths[account_id] = replacement_paths
        self.__children[account_id] = replacement_child
        return recovered

    def __stage_child_monitor(
        self,
        account_id: str,
        account: _AccountSettings,
    ) -> tuple[_AccountPaths, Any]:
        paths = self.__build_profile_paths(account_id, account.provider)
        child = None
        try:
            child = (
                _RecoveryPendingChild(paths)
                if account_id in self.__recovery_pending_profile_ids
                else self.__invoke_monitor_factory(
                    account.provider,
                    paths.config_dir,
                    paths.profile_dir,
                    account_id,
                )
            )
            if self.__root is not None:
                self.__attach_child(child, self.__root, self.__event_queue)
        except Exception:
            shutdown_new = getattr(child, "shutdown", None)
            if callable(shutdown_new):
                try:
                    shutdown_new()
                except Exception:
                    pass
            raise
        return paths, child

    def __create_child_monitor(
        self,
        provider: str,
        config_dir: str,
        profile_dir: str,
        profile_id: str,
    ) -> Any:
        if str(provider or "").lower() == "cursor":
            from src.apps.cursor_usage_monitor import CursorUsageMonitor

            return CursorUsageMonitor(
                config_dir=config_dir,
                profile_dir=profile_dir,
                notification_sink=self.__handle_child_notification,
                suppress_normal_tooltips=True,
                unrecoverable_timeout_handler=self.__unrecoverable_timeout_handler,
                profile_id=profile_id,
            )
        return CodexUsageMonitor(
            config_dir=config_dir,
            profile_dir=profile_dir,
            notification_sink=self.__handle_child_notification,
            suppress_normal_tooltips=True,
            local_usage_provider=find_latest_windows_codex_usage,
            unrecoverable_timeout_handler=self.__unrecoverable_timeout_handler,
        )

    def __handle_child_notification(self, event: dict[str, Any]) -> None:
        if isinstance(event, dict):
            self.__notification_events.append(dict(event))
            if len(self.__notification_events) > 20:
                self.__notification_events = self.__notification_events[-20:]
            self.__refresh_taskbar_progress()
        return

    def __resolve_config_dir(self, config_dir: str | None) -> str:
        normalized = str(config_dir or "").strip()
        if normalized:
            return normalized
        base_dir = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base_dir, "windows-supporter")

    def __resolve_local_base_dir(self, local_base_dir: str | None) -> str:
        normalized = str(local_base_dir or "").strip()
        if normalized:
            return normalized
        return os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.path.expanduser("~")


def _normalize_tooltip_duration_ms(value: Any, fallback: int) -> int:
    try:
        duration = int(value)
    except Exception:
        duration = int(fallback)
    if duration < 1200:
        duration = 1200
    return int(duration)


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(fallback)


def _normalize_provider_settings(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for provider in SUPPORTED_PROVIDERS:
        raw = value.get(provider)
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or "").strip()
        if not label:
            continue
        normalized[provider] = {
            "label": label,
            "enabled": bool(raw.get("enabled", True)),
        }
    return normalized


def _is_valid_profile_id(value: str) -> bool:
    return PROFILE_ID_PATTERN.fullmatch(str(value or "")) is not None


def _default_profile_label(provider: str, index: int) -> str:
    provider_name = "Cursor" if str(provider or "").lower() == "cursor" else "Codex"
    return f"{provider_name} {max(1, int(index))}"


def _optional_percent(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        if match is None:
            return None
        try:
            numeric = float(match.group(0))
        except Exception:
            return None
    if numeric < 0.0 or numeric > 100.0:
        return None
    return numeric
