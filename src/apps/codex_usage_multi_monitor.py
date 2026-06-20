from __future__ import annotations

import inspect
import json
import os
import shutil
import threading
from dataclasses import dataclass
from typing import Any, Callable

from src.apps.codex_usage_monitor import CURRENT_CODEX_USAGE_URL, CodexUsageMonitor, UsageSnapshot
from src.apps.codex_usage_taskbar_overlay import CodexUsageTaskbarOverlay


ACCOUNT_IDS = ("account_1", "account_2")
DEFAULT_LABELS = {
    "account_1": "Codex 1",
    "account_2": "Codex 2",
}


@dataclass(frozen=True)
class _AccountPaths:
    account_id: str
    config_dir: str
    profile_dir: str

    @property
    def settings_path(self) -> str:
        return os.path.join(self.config_dir, "codex_usage_settings.json")

    @property
    def state_path(self) -> str:
        return os.path.join(self.config_dir, "codex_usage_state.json")


@dataclass
class _AccountSettings:
    account_id: str
    label: str
    enabled: bool = True


class CodexUsageMultiMonitor:
    def __init__(
        self,
        config_dir: str | None = None,
        local_base_dir: str | None = None,
        monitor_factory: Callable[[str, str], Any] | None = None,
        taskbar_progress_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.__config_dir = self.__resolve_config_dir(config_dir)
        self.__local_base_dir = self.__resolve_local_base_dir(local_base_dir)
        self.__settings_path = os.path.join(
            self.__config_dir,
            "codex_usage_multi_settings.json",
        )
        self.__state_path = os.path.join(
            self.__config_dir,
            "codex_usage_multi_state.json",
        )
        self.__default_account_id = "account_1"
        self.__account_order = list(ACCOUNT_IDS)
        self.__enabled = True
        self.__taskbar_overlay_enabled = True
        self.__interval_sec = 90.0
        self.__tooltip_duration_ms = 7000
        self.__usage_url = CURRENT_CODEX_USAGE_URL
        self.__collection_mode = "playwright"
        self.__refresh_inflight = False
        self.__refresh_lock = threading.Lock()
        self.__root = None
        self.__event_queue = None
        self.__taskbar_progress = None
        self.__taskbar_progress_factory = taskbar_progress_factory or CodexUsageTaskbarOverlay
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        self.__notification_events: list[dict[str, Any]] = []
        self.__account_settings = {
            account_id: _AccountSettings(
                account_id=account_id,
                label=DEFAULT_LABELS[account_id],
                enabled=True,
            )
            for account_id in ACCOUNT_IDS
        }
        self.__account_paths = self.__build_account_paths()
        has_manager_settings = bool(os.path.isfile(self.__settings_path))
        self.__load_manager_settings()
        legacy_settings = self.__read_legacy_settings()
        self.__migrate_legacy_single_account_files_if_needed()
        if not has_manager_settings:
            self.__apply_legacy_manager_settings(legacy_settings)
            self.__save_manager_settings()
        factory = monitor_factory or self.__create_child_monitor
        self.__children = {
            account_id: factory(paths.config_dir, paths.profile_dir)
            for account_id, paths in self.__account_paths.items()
        }
        return

    def attach(self, root, event_queue=None) -> None:
        self.__root = root
        self.__event_queue = event_queue
        for child in self.__children.values():
            self.__attach_child(child, root, event_queue)
        self.__refresh_taskbar_progress()
        self.__restart_monitor_scheduler(initial_delay_sec=1.0)
        return

    def get_settings_snapshot(self) -> dict[str, Any]:
        return {
            "settings_version": 2,
            "enabled": bool(self.__enabled),
            "taskbar_overlay_enabled": bool(self.__taskbar_overlay_enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
            "collection_mode": str(self.__collection_mode or "playwright"),
            "settings_path": str(self.__settings_path),
            "state_path": str(self.__state_path),
            "default_account_id": str(self.__default_account_id),
            "account_order": list(self.__ordered_account_ids()),
            "accounts": [
                self.__build_account_settings_snapshot(account_id)
                for account_id in self.__ordered_account_ids()
            ],
        }

    def update_settings(self, data: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"
        if "enabled" in data:
            self.__enabled = bool(data.get("enabled"))
        if "taskbar_overlay_enabled" in data:
            self.__taskbar_overlay_enabled = bool(data.get("taskbar_overlay_enabled"))
        if "interval_sec" in data:
            try:
                interval_sec = float(data.get("interval_sec"))
            except Exception:
                return False, "interval"
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
        accounts = data.get("accounts")
        if isinstance(accounts, list):
            requested_order: list[str] = []
            for raw in accounts:
                if not isinstance(raw, dict):
                    continue
                account_id = str(raw.get("id", "") or "")
                if account_id not in self.__account_settings:
                    continue
                if account_id not in requested_order:
                    requested_order.append(account_id)
                current = self.__account_settings[account_id]
                if "label" in raw:
                    label = str(raw.get("label", "") or "").strip()
                    if label:
                        current.label = label
                if "enabled" in raw:
                    current.enabled = bool(raw.get("enabled"))
            if requested_order:
                self.__account_order = requested_order + [
                    account_id for account_id in ACCOUNT_IDS if account_id not in requested_order
                ]
        default_account_id = str(data.get("default_account_id", "") or "")
        if default_account_id in self.__account_settings:
            self.__default_account_id = default_account_id
        elif isinstance(accounts, list) and self.__account_order:
            self.__default_account_id = self.__account_order[0]
        self.__save_manager_settings()
        self.__sync_child_settings()
        self.__restart_monitor_scheduler(initial_delay_sec=1.0)
        self.__refresh_taskbar_progress()
        return True, None

    def get_runtime_status(self) -> dict[str, Any]:
        account_entries = [
            self.__build_account_runtime_entry(account_id)
            for account_id in self.__ordered_account_ids()
        ]
        active_entries = [
            entry
            for entry in account_entries
            if bool(self.__enabled) and bool(entry.get("enabled"))
        ]
        runtimes = [entry.get("runtime", {}) for entry in active_entries]
        collect_inflight = bool(self.__refresh_inflight) or any(
            bool(runtime.get("collect_inflight")) for runtime in (entry.get("runtime", {}) for entry in account_entries)
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
            "accounts": account_entries,
        }

    def get_last_snapshot(self) -> Any:
        # Compatibility API for legacy single-account callers. Aggregate and
        # per-account consumers should use get_runtime_status()["accounts"].
        return self.__child(self.__default_account_id).get_last_snapshot()

    def on_display_topology_changed(self, reason: str = "display_change") -> None:
        _ = reason
        self.__refresh_taskbar_progress(invalidate_geometry=True)
        return

    def show_current_status(self, force_refresh: bool = True, source: str = "manual_query") -> None:
        source_key = str(source or "manual_query")
        if source_key == "manual_login":
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
        child = self.__child(self.__default_account_id)
        formatter = getattr(child, "format_captured_at_for_display", None)
        if callable(formatter):
            return str(formatter(value))
        return str(value or "")

    def format_reset_at_for_display(self, value: str, key: str = "") -> str:
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

    def __refresh_taskbar_progress(self, *, invalidate_geometry: bool = False) -> None:
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
        return bool(self.__enabled and self.__background_account_ids())

    def __background_account_ids(self) -> list[str]:
        account_ids = []
        for account_id in self.__ordered_account_ids():
            if not bool(self.__account_settings[account_id].enabled):
                continue
            runtime = self.__safe_child_runtime(account_id)
            session_state = str(runtime.get("session_state") or "logged_out")
            if (
                session_state == "logged_in"
                or bool(runtime.get("collect_inflight"))
                or bool(runtime.get("profile_session_present"))
                or bool(runtime.get("profile_cdp_available"))
            ):
                account_ids.append(account_id)
        return account_ids

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
            "label": account.label,
            "enabled": bool(account.enabled),
            "settings_path": str(child_settings.get("settings_path") or paths.settings_path),
            "state_path": str(child_settings.get("state_path") or paths.state_path),
            "profile_dir": str(child_settings.get("profile_dir") or paths.profile_dir),
        }

    def __build_account_runtime_entry(self, account_id: str) -> dict[str, Any]:
        account = self.__account_settings[account_id]
        runtime = self.__safe_child_runtime(account_id)
        return {
            "id": account.account_id,
            "label": self.__display_account_label(account, runtime),
            "configured_label": account.label,
            "enabled": bool(account.enabled),
            "runtime": runtime,
            "last_snapshot": self.__snapshot_to_dict(self.__child(account_id).get_last_snapshot()),
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
            updater = getattr(child, "update_settings", None)
            if not callable(updater):
                continue
            updater(
                {
                    "enabled": bool(self.__enabled and account.enabled),
                    "interval_sec": float(self.__interval_sec),
                    "tooltip_duration_ms": int(self.__tooltip_duration_ms),
                    "usage_url": str(self.__usage_url),
                    "collection_mode": "playwright",
                }
            )
        return

    def __child(self, account_id: str) -> Any:
        normalized = str(account_id or "")
        if normalized not in self.__children:
            raise ValueError(f"unknown Codex account id: {account_id}")
        return self.__children[normalized]

    def __ordered_account_ids(self) -> list[str]:
        ordered = []
        for account_id in self.__account_order:
            if account_id in ACCOUNT_IDS and account_id not in ordered:
                ordered.append(account_id)
        ordered.extend(account_id for account_id in ACCOUNT_IDS if account_id not in ordered)
        return ordered

    def __build_account_paths(self) -> dict[str, _AccountPaths]:
        local_app_base = os.path.join(self.__local_base_dir, "windows-supporter")
        return {
            "account_1": _AccountPaths(
                account_id="account_1",
                config_dir=os.path.join(self.__config_dir, "codex-account-1"),
                profile_dir=os.path.join(local_app_base, "chatgpt-profile-account-1"),
            ),
            "account_2": _AccountPaths(
                account_id="account_2",
                config_dir=os.path.join(self.__config_dir, "codex-account-2"),
                profile_dir=os.path.join(local_app_base, "chatgpt-profile-account-2"),
            ),
        }

    def __load_manager_settings(self) -> None:
        data = self.__read_json_file(self.__settings_path)
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
        account_order = data.get("account_order")
        if isinstance(account_order, list):
            ordered = [
                str(account_id)
                for account_id in account_order
                if str(account_id) in self.__account_settings
            ]
            if ordered:
                self.__account_order = ordered + [
                    account_id for account_id in ACCOUNT_IDS if account_id not in ordered
                ]
        default_account_id = str(data.get("default_account_id", "") or "")
        if default_account_id in self.__account_settings:
            self.__default_account_id = default_account_id
        accounts = data.get("accounts")
        if isinstance(accounts, list):
            for raw in accounts:
                if not isinstance(raw, dict):
                    continue
                account_id = str(raw.get("id", "") or "")
                if account_id not in self.__account_settings:
                    continue
                account = self.__account_settings[account_id]
                label = str(raw.get("label", "") or "").strip()
                if label:
                    account.label = label
                if "enabled" in raw:
                    account.enabled = bool(raw.get("enabled"))
        return

    def __save_manager_settings(self) -> None:
        payload = {
            "settings_version": 2,
            "enabled": bool(self.__enabled),
            "taskbar_overlay_enabled": bool(self.__taskbar_overlay_enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
            "default_account_id": str(self.__default_account_id),
            "account_order": list(self.__ordered_account_ids()),
            "accounts": [
                {
                    "id": account_id,
                    "label": self.__account_settings[account_id].label,
                    "enabled": bool(self.__account_settings[account_id].enabled),
                }
                for account_id in self.__ordered_account_ids()
            ],
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
        account_1 = self.__account_paths["account_1"]
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
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
        return

    def __snapshot_to_dict(self, snapshot: Any) -> dict[str, Any]:
        if isinstance(snapshot, dict):
            return dict(snapshot)
        to_dict = getattr(snapshot, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            if isinstance(data, dict):
                return dict(data)
        return UsageSnapshot().to_dict()

    def __usage_history_to_dicts(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def __create_child_monitor(self, config_dir: str, profile_dir: str) -> CodexUsageMonitor:
        return CodexUsageMonitor(
            config_dir=config_dir,
            profile_dir=profile_dir,
            notification_sink=self.__handle_child_notification,
            suppress_normal_tooltips=True,
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
