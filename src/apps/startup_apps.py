from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
import threading
import time
from typing import Any, Pattern

import win32gui
import psutil

from src.utils.subprocess_utils import popen_no_window
from src.utils.windows_process import (
    cmdline_matches_pwa,
    get_process_info,
    snapshot_running_processes,
    snapshot_running_name_pids,
)
from src.utils.windows_shortcut import (
    parse_chrome_pwa_args,
    read_shortcut_target_args,
    split_args,
)
from src.utils.windows_window import (
    apply_window_action,
    get_window_pid,
    get_window_text,
    is_tool_window,
    resize_window_to_monitor,
)

_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class HideRule:
    action: str  # "hide" | "minimize" | "show" | "close"
    title_re: Pattern[str] | None = None
    app_id: str | None = None
    profile_directory: str | None = None
    process_name: str | None = None
    resize_to_monitor: bool = False


class StartupAppManager:
    def __init__(self) -> None:
        appdata = os.environ.get("APPDATA")
        base_dir = appdata if appdata else os.path.expanduser("~")
        self._config_dir = os.path.join(base_dir, "windows-supporter")
        self._config_path = os.path.join(self._config_dir, "startup_apps.json")
        self._log_path = os.path.join(self._config_dir, "startup_apps.log")

        self._hide_deadline = 0.0
        self._hide_poll_interval_sec = 0.25
        self._hide_rules: list[HideRule] = []
        self._hide_generation = 0
        self._hide_thread: threading.Thread | None = None

        self._start_menu_index: list[tuple[str, str]] | None = None
        self._settings_window = None

        self._state_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._baseline_hwnds_union: set[int] = set()
        self._managed_hwnds: set[int] = set()
        self._launched_pids: set[int] = set()
        self._launched_pwas: set[tuple[str, str]] = set()
        return

    def _normalize_slack_defaults(self, cfg: dict[str, Any]) -> bool:
        instances = cfg.get("instances")
        if not isinstance(instances, list) or not instances:
            return False

        changed = False
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            inst_id = str(inst.get("id", "")).strip().casefold()
            inst_app = str(inst.get("app", "")).strip().casefold()
            is_slack = inst_id.startswith("slack:") or inst_app == "slack"
            if not is_slack:
                continue

            act = str(inst.get("hide_action", "")).strip().lower()
            if act != "show":
                inst["hide_action"] = "show"
                changed = True

        return changed

    def _snapshot_top_level_hwnds(self) -> set[int]:
        hwnds: set[int] = set()

        def cb(hwnd: int, _param: Any) -> None:
            try:
                hwnds.add(int(hwnd))
            except Exception:
                return

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return set()
        return hwnds

    def _remember_launched_pid(self, pid: int) -> None:
        try:
            p = int(pid)
        except Exception:
            return
        if p <= 0:
            return
        try:
            with self._state_lock:
                self._launched_pids.add(p)
        except Exception:
            return

    def _remember_launched_pwa(self, app_id: str, profile_directory: str | None) -> None:
        aid = str(app_id or "").strip()
        if not aid:
            return
        prof = str(profile_directory or "").strip()
        try:
            with self._state_lock:
                self._launched_pwas.add((aid, prof))
        except Exception:
            return

    def _remember_managed_hwnd(self, hwnd: int) -> None:
        try:
            h = int(hwnd)
        except Exception:
            return
        if h <= 0:
            return
        try:
            with self._state_lock:
                self._managed_hwnds.add(h)
        except Exception:
            return

    def load_config(self) -> dict[str, Any]:
        config, _ = self._load_or_create_config()
        return config

    def save_config(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        data["schema_version"] = _SCHEMA_VERSION
        if "instances" not in data or not isinstance(data.get("instances"), list):
            data["instances"] = []
        self._write_config(data)
        return

    def get_instances_runtime(
        self,
        instances: list[Any],
    ) -> dict[str, tuple[bool, int | None]]:

        try:
            name_to_pid = snapshot_running_name_pids()
        except Exception:
            name_to_pid = {}

        try:
            with self._state_lock:
                managed_hwnds = set(self._managed_hwnds)
        except Exception:
            managed_hwnds = set()
        out: dict[str, tuple[bool, int | None]] = {}
        pwa_groups: dict[str, tuple[Pattern[str] | None, list[str]]] = {}

        for inst in instances:
            if not isinstance(inst, dict):
                continue

            inst_id = str(inst.get("id", "")).strip()
            if not inst_id:
                inst_id = str(inst.get("lnk_path", "")).strip()
            if not inst_id:
                continue

            inst_type = str(inst.get("type", "")).strip().lower()
            pid: int | None = None

            if inst_type == "chrome_pwa":
                title_pat = str(inst.get("window_title_regex", "")).strip()
                if not title_pat:
                    app = str(inst.get("app", "")).strip()
                    title_pat = re.escape(app) if app else ""

                group = pwa_groups.get(title_pat)
                if group is None:
                    title_re = None
                    if title_pat:
                        try:
                            title_re = re.compile(title_pat)
                        except re.error:
                            title_re = None
                    pwa_groups[title_pat] = (title_re, [inst_id])
                else:
                    group[1].append(inst_id)

                out[inst_id] = (False, None)
            else:
                exe = str(inst.get("exe", "")).strip()
                if exe:
                    proc_name = os.path.basename(exe).casefold()
                    pid = name_to_pid.get(proc_name)
                out[inst_id] = (
                    pid is not None,
                    int(pid) if pid is not None else None,
                )

        if pwa_groups and managed_hwnds:
            windows: list[tuple[int, str, int]] = []

            for hwnd in managed_hwnds:
                try:
                    h = int(hwnd)
                except Exception:
                    continue
                try:
                    if not win32gui.IsWindow(h):
                        continue
                    if is_tool_window(h):
                        continue
                    title = get_window_text(h)
                    if not title:
                        continue
                    try:
                        cls = win32gui.GetClassName(h) or ""
                    except Exception:
                        cls = ""
                    if not str(cls).startswith("Chrome_WidgetWin"):
                        continue
                    pid_val = get_window_pid(h)
                    if pid_val <= 0:
                        continue
                    windows.append((int(h), title, int(pid_val)))
                except Exception:
                    continue

            windows.sort(key=lambda x: x[0])

            for title_pat, (title_re, inst_ids) in pwa_groups.items():
                if title_re is None:
                    continue
                matched: list[int] = []
                for hwnd, title, pid_val in windows:
                    try:
                        if title_re.search(title):
                            matched.append(int(pid_val))
                    except Exception:
                        continue
                for idx, inst_id in enumerate(inst_ids):
                    pid_val = matched[idx] if idx < len(matched) else None
                    out[inst_id] = (
                        pid_val is not None,
                        int(pid_val) if pid_val is not None else None,
                    )

        return out

    def get_enabled_state(self) -> bool:
        try:
            if os.path.isfile(self._config_path):
                with open(self._config_path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict):
                    return bool(data.get("enabled", True))
        except Exception:
            pass
        return True

    def toggle_enabled(self) -> bool:
        cfg = self.load_config()
        current = bool(cfg.get("enabled", True))
        cfg["enabled"] = (not current)
        self.save_config(cfg)
        return bool(cfg.get("enabled", True))

    def open_config_file(self) -> None:
        try:
            self.load_config()
        except Exception:
            pass
        try:
            os.startfile(self._config_path)
        except Exception:
            return
        return

    def open_config_dir(self) -> None:
        try:
            os.makedirs(self._config_dir, exist_ok=True)
        except Exception:
            return
        try:
            os.startfile(self._config_dir)
        except Exception:
            return
        return

    def rescan_defaults_merge(self) -> None:
        cfg = self.load_config()
        existing = cfg.get("instances", [])
        if not isinstance(existing, list):
            existing = []
        existing_items = [x for x in existing if isinstance(x, dict)]

        by_lnk: dict[str, dict[str, Any]] = {}
        for it in existing_items:
            lp = str(it.get("lnk_path", "")).strip()
            if lp:
                by_lnk[lp] = it

        discovered = self._discover_default_instances()
        merged: list[dict[str, Any]] = list(existing_items)
        for d in discovered:
            lp = str(d.get("lnk_path", "")).strip()
            if lp and lp in by_lnk:
                cur = by_lnk[lp]
                for k in (
                    "type",
                    "exe",
                    "raw_args",
                    "profile_directory",
                    "app_id",
                    "extra_args",
                    "window_title_regex",
                ):
                    if k in d:
                        cur[k] = d.get(k)
                continue
            merged.append(d)

        cfg["instances"] = merged
        self.save_config(cfg)
        return

    def read_shortcut_public(self, lnk_path: str) -> dict[str, str] | None:
        return read_shortcut_target_args(lnk_path, log=self._log)

    def parse_chrome_pwa_args_public(
        self, raw_args: str
    ) -> tuple[str | None, str | None, list[str]]:
        return parse_chrome_pwa_args(raw_args)

    def apply_instance_window_action(self, inst: dict[str, Any], action: str) -> int:
        act = str(action).strip().lower()
        if act not in {"show", "hide", "minimize", "close"}:
            return 0
        if not isinstance(inst, dict):
            return 0

        inst_type = str(inst.get("type", "")).strip().lower()
        title_pat = str(inst.get("window_title_regex", "")).strip()
        if not title_pat:
            app = str(inst.get("app", "")).strip()
            title_pat = re.escape(app) if app else ""

        title_re: Pattern[str] | None = None
        if title_pat:
            try:
                title_re = re.compile(title_pat)
            except re.error:
                title_re = None

        if inst_type == "chrome_pwa":
            app_id = str(inst.get("app_id", "")).strip() or None
            profile_directory = str(inst.get("profile_directory", "")).strip() or None

            if app_id:
                try:
                    n = self._hide_matching_windows(
                        [
                            HideRule(
                                action=act,
                                title_re=None,
                                app_id=app_id,
                                profile_directory=profile_directory,
                                process_name=None,
                                resize_to_monitor=False,
                            )
                        ],
                        baseline_hwnds=None,
                        record_managed=False,
                    )
                except Exception:
                    n = 0
                if n > 0:
                    return int(n)

            if title_re is None:
                return 0
            try:
                return int(
                    self._hide_matching_windows(
                        [
                            HideRule(
                                action=act,
                                title_re=title_re,
                                app_id=None,
                                profile_directory=None,
                                process_name=None,
                                resize_to_monitor=False,
                            )
                        ],
                        baseline_hwnds=None,
                        record_managed=False,
                    )
                )
            except Exception:
                return 0

        exe = str(inst.get("exe", "")).strip()
        proc_name = os.path.basename(exe).casefold() if exe else ""
        if not proc_name:
            return 0
        try:
            return int(
                self._hide_matching_windows(
                    [
                        HideRule(
                            action=act,
                            title_re=None,
                            app_id=None,
                            profile_directory=None,
                            process_name=proc_name,
                            resize_to_monitor=False,
                        )
                    ],
                    baseline_hwnds=None,
                    record_managed=False,
                )
            )
        except Exception:
            return 0

    def shutdown(self, cleanup: bool = True, timeout_sec: float = 2.5) -> None:
        try:
            self._hide_generation += 1
            self._hide_deadline = 0.0
        except Exception:
            pass

        if not cleanup:
            return

        try:
            with self._state_lock:
                baseline = set(self._baseline_hwnds_union)
                managed_hwnds = list(self._managed_hwnds)
                launched_pids = list(self._launched_pids)
                launched_pwas = set(self._launched_pwas)
                self._managed_hwnds.clear()
                self._launched_pids.clear()
                self._launched_pwas.clear()
        except Exception:
            baseline = set()
            managed_hwnds = []
            launched_pids = []
            launched_pwas = set()

        closed = 0

        for hwnd in managed_hwnds:
            try:
                if baseline and int(hwnd) in baseline:
                    continue
            except Exception:
                pass
            try:
                if win32gui.IsWindow(int(hwnd)):
                    if apply_window_action(int(hwnd), "close"):
                        closed += 1
            except Exception:
                continue

        if launched_pwas:
            pid_cache: dict[int, tuple[str, list[str]]] = {}

            def cb(hwnd: int, _param: Any) -> None:
                nonlocal closed
                try:
                    if is_tool_window(hwnd):
                        return
                    if baseline and int(hwnd) in baseline:
                        return
                    pid = get_window_pid(hwnd)
                    if pid <= 0:
                        return
                    _name, cmdline = get_process_info(pid, cache=pid_cache)
                    if not cmdline:
                        return
                    for app_id, prof in launched_pwas:
                        if cmdline_matches_pwa(cmdline, app_id, prof or None):
                            if apply_window_action(hwnd, "close"):
                                closed += 1
                            return
                except Exception:
                    return

            try:
                win32gui.EnumWindows(cb, None)
            except Exception:
                pass

        to_term: list[psutil.Process] = []
        for pid in launched_pids:
            try:
                proc = psutil.Process(int(pid))
            except Exception:
                continue
            try:
                name = str(proc.name() or "").casefold()
            except Exception:
                name = ""
            if name == "chrome.exe":
                continue
            to_term.append(proc)

        for proc in to_term:
            try:
                proc.terminate()
            except Exception:
                continue

        try:
            _gone, alive = psutil.wait_procs(to_term, timeout=float(timeout_sec))
        except Exception:
            alive = to_term

        killed = 0
        for proc in alive:
            try:
                proc.kill()
                killed += 1
            except Exception:
                continue

        try:
            self._log(
                f"shutdown cleanup: close_windows={closed}, terminate={len(to_term)}, kill={killed}"
            )
        except Exception:
            pass
        return

    def open_settings_window(self, root: Any) -> None:
        try:
            ui = getattr(root, "_ws_main_ui", None)
            if ui is not None:
                ui.show_startup_apps()
                return
        except Exception:
            pass

        try:
            from src.apps.startup_apps_ui import StartupAppsWindow
        except Exception:
            return

        if self._settings_window is None or (not self._settings_window.is_open()):
            self._settings_window = StartupAppsWindow(root, self)
        try:
            self._settings_window.show()
        except Exception:
            return
        return

    def open_log_file(self) -> None:
        try:
            os.startfile(self._log_path)
        except Exception:
            return
        return

    def start(self, root: Any) -> None:
        acquired = False
        try:
            acquired = bool(self._start_lock.acquire(blocking=False))
        except Exception:
            acquired = False
        if not acquired:
            return

        created = False
        try:
            try:
                config, created = self._load_or_create_config()
            except Exception as exc:
                self._log(f"config load failed: {exc!r}")
                return

            if not bool(config.get("enabled", True)):
                return

            try:
                self._hide_poll_interval_sec = float(
                    config.get("hide_poll_interval_sec", 0.25)
                )
            except Exception:
                self._hide_poll_interval_sec = 0.25

            try:
                hide_timeout_sec = float(config.get("hide_timeout_sec", 10.0))
            except Exception:
                hide_timeout_sec = 10.0

            instances = config.get("instances", [])
            if not isinstance(instances, list) or not instances:
                return

            baseline_hwnds: set[int] = set()
            try:
                baseline_hwnds = self._snapshot_top_level_hwnds()
            except Exception:
                baseline_hwnds = set()

            try:
                if baseline_hwnds:
                    with self._state_lock:
                        self._baseline_hwnds_union.update(baseline_hwnds)
            except Exception:
                pass

            try:
                self._launch_instances(instances)
            except Exception as exc:
                self._log(f"launch failed: {exc!r}")

            self._hide_rules = self._build_hide_rules(instances)
            if not self._hide_rules:
                return

            self._hide_generation += 1
            gen = int(self._hide_generation)
            deadline = time.monotonic() + max(0.5, hide_timeout_sec)
            self._hide_deadline = float(deadline)
            poll = float(max(0.05, self._hide_poll_interval_sec))
            rules = list(self._hide_rules)

            try:
                t = threading.Thread(
                    target=self._hide_worker,
                    args=(gen, deadline, poll, rules, baseline_hwnds),
                    daemon=True,
                )
                self._hide_thread = t
                t.start()
            except Exception as exc:
                self._log(f"start hide worker failed: {exc!r}")
            finally:
                if created:
                    self._log("startup_apps.json created with defaults")
            return
        finally:
            try:
                self._start_lock.release()
            except Exception:
                pass

    def _default_config(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "enabled": True,
            "hide_poll_interval_sec": 0.25,
            "hide_timeout_sec": 10.0,
            "instances": [],
        }

    def _discover_default_instances(self) -> list[dict[str, Any]]:
        app_defs = [
            {
                "app": "Google Calendar",
                "base_id": "google_calendar",
                "max_instances": 2,
                "shortcut_name_regex": r"(?i)^Google Calendar",
                "hide_action": "hide",
                "window_title_regex": r"(?i)Google Calendar",
            },
            {
                "app": "Gmail",
                "base_id": "gmail",
                "max_instances": 1,
                "shortcut_name_regex": r"(?i)^Gmail",
                "hide_action": "hide",
                "window_title_regex": r"(?i)Gmail",
            },
            {
                "app": "Slack",
                "base_id": "slack",
                "max_instances": 1,
                "shortcut_name_regex": r"(?i)^Slack",
                "hide_action": "show",
                "window_title_regex": r"(?i)Slack",
            },
        ]

        instances: list[dict[str, Any]] = []
        used_ids: set[str] = set()

        for app_def in app_defs:
            max_instances = int(app_def.get("max_instances", 1))
            name_re = str(app_def.get("shortcut_name_regex", "")).strip()
            if max_instances <= 0 or not name_re:
                continue

            lnk_paths = self._discover_start_menu_shortcuts(
                name_re,
                max_results=max_instances * 2,
            )
            if not lnk_paths:
                continue

            app_instances: list[dict[str, Any]] = []
            for lnk_path in lnk_paths:
                shortcut = read_shortcut_target_args(lnk_path, log=self._log)
                if not shortcut:
                    continue

                target = shortcut.get("target", "").strip()
                raw_args = shortcut.get("args", "").strip()
                if not target:
                    continue

                stem = os.path.splitext(os.path.basename(str(lnk_path)))[0].strip()
                profile_dir, app_id, extra_args = parse_chrome_pwa_args(raw_args)
                inst_type = "chrome_pwa" if app_id else "exe"

                key = stem if stem else str(profile_dir or "")
                if not key:
                    key = "1"

                base_id = str(app_def.get("base_id", "app")).strip() or "app"
                candidate_id = f"{base_id}:{key}"
                if candidate_id in used_ids:
                    suffix = 2
                    while f"{candidate_id}:{suffix}" in used_ids:
                        suffix += 1
                    candidate_id = f"{candidate_id}:{suffix}"
                used_ids.add(candidate_id)

                inst: dict[str, Any] = {
                    "id": candidate_id,
                    "type": inst_type,
                    "app": str(app_def.get("app", stem)).strip() or stem or base_id,
                    "name": stem or str(app_def.get("app", base_id)),
                    "enabled": True,
                    "hide_action": str(app_def.get("hide_action", "hide")).strip().lower()
                    or "hide",
                    "window_title_regex": str(app_def.get("window_title_regex", "")).strip(),
                    "lnk_path": str(lnk_path),
                    "exe": target,
                    "raw_args": raw_args,
                }

                if inst_type == "chrome_pwa":
                    inst["profile_directory"] = profile_dir
                    inst["app_id"] = app_id
                    inst["extra_args"] = extra_args

                app_instances.append(inst)
                if len(app_instances) >= max_instances:
                    break

            instances.extend(app_instances)

        return instances

    def _load_or_create_config(self) -> tuple[dict[str, Any], bool]:
        created = False
        if os.path.isfile(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict):
                    migrated = self._migrate_config(data)
                    if migrated is not None:
                        try:
                            self._write_config(migrated)
                        except Exception:
                            pass
                        return migrated, False

                    try:
                        if self._normalize_slack_defaults(data):
                            self.save_config(data)
                    except Exception:
                        pass
                    return data, False
            except Exception as exc:
                self._log(f"config parse error: {exc!r}")

        data = self._default_config()
        os.makedirs(self._config_dir, exist_ok=True)
        try:
            data["instances"] = self._discover_default_instances()
        except Exception as exc:
            self._log(f"default discovery failed: {exc!r}")

        try:
            try:
                self._normalize_slack_defaults(data)
            except Exception:
                pass
            self._write_config(data)
            created = True
        except Exception as exc:
            self._log(f"config write failed: {exc!r}")
        return data, created

    def _write_config(self, data: dict[str, Any]) -> None:
        os.makedirs(self._config_dir, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        return

    def _migrate_config(self, data: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if int(data.get("schema_version", 0)) == int(_SCHEMA_VERSION):
                if isinstance(data.get("instances"), list):
                    return None
        except Exception:
            pass

        if isinstance(data.get("apps"), list):
            migrated = self._default_config()
            migrated["enabled"] = bool(data.get("enabled", True))

            try:
                migrated["hide_poll_interval_sec"] = float(
                    data.get(
                        "hide_poll_interval_sec",
                        migrated["hide_poll_interval_sec"],
                    )
                )
            except Exception:
                pass

            try:
                migrated["hide_timeout_sec"] = float(
                    data.get("hide_timeout_sec", migrated["hide_timeout_sec"])
                )
            except Exception:
                pass

            instances: list[dict[str, Any]] = []
            for app in data.get("apps", []):
                if not isinstance(app, dict):
                    continue
                base_id = str(app.get("id", "app")).strip() or "app"
                hide_action = (
                    str(app.get("hide_action", "hide")).strip().lower() or "hide"
                )
                title_re = str(app.get("window_title_regex", "")).strip()
                enabled = bool(app.get("enabled", True))

                paths = app.get("shortcut_paths", [])
                if not isinstance(paths, list):
                    paths = []
                for idx, p in enumerate(paths):
                    if not isinstance(p, str) or not p.strip():
                        continue
                    lnk_path = p.strip()
                    shortcut = read_shortcut_target_args(lnk_path, log=self._log) or {}
                    target = str(shortcut.get("target", "")).strip()
                    raw_args = str(shortcut.get("args", "")).strip()

                    profile_dir, app_id, extra_args = parse_chrome_pwa_args(raw_args)
                    inst_type = "chrome_pwa" if app_id else "exe"

                    inst: dict[str, Any] = {
                        "id": f"{base_id}:{idx + 1}",
                        "type": inst_type,
                        "app": base_id,
                        "name": os.path.splitext(os.path.basename(lnk_path))[0] or base_id,
                        "enabled": enabled,
                        "hide_action": hide_action,
                        "window_title_regex": title_re,
                        "lnk_path": lnk_path,
                        "exe": target,
                        "raw_args": raw_args,
                    }
                    if inst_type == "chrome_pwa":
                        inst["profile_directory"] = profile_dir
                        inst["app_id"] = app_id
                        inst["extra_args"] = extra_args
                    instances.append(inst)

            if not instances:
                try:
                    instances = self._discover_default_instances()
                except Exception:
                    instances = []

            migrated["instances"] = instances
            try:
                self._normalize_slack_defaults(migrated)
            except Exception:
                pass
            return migrated

        migrated = self._default_config()
        try:
            migrated["instances"] = self._discover_default_instances()
        except Exception:
            migrated["instances"] = []
        try:
            self._normalize_slack_defaults(migrated)
        except Exception:
            pass
        return migrated

    def _start_menu_dirs(self) -> list[str]:
        dirs: list[str] = []
        appdata = os.environ.get("APPDATA")
        programdata = os.environ.get("PROGRAMDATA")
        localappdata = os.environ.get("LOCALAPPDATA")

        if appdata:
            dirs.append(
                os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
            )
        if programdata:
            dirs.append(
                os.path.join(
                    programdata, "Microsoft", "Windows", "Start Menu", "Programs"
                )
            )
        if localappdata:
            dirs.append(
                os.path.join(
                    localappdata, "Microsoft", "Windows", "Start Menu", "Programs"
                )
            )

        out: list[str] = []
        seen: set[str] = set()
        for d in dirs:
            if not d:
                continue
            nd = os.path.normpath(d)
            if nd in seen:
                continue
            seen.add(nd)
            if os.path.isdir(nd):
                out.append(nd)
        return out

    def _startup_dirs(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for base in self._start_menu_dirs():
            p = os.path.normpath(os.path.join(base, "Startup"))
            if p in seen:
                continue
            seen.add(p)
            if os.path.isdir(p):
                out.append(p)
        return out

    def _is_in_startup_dir(self, path: str) -> bool:
        try:
            target = os.path.normcase(os.path.normpath(path))
        except Exception:
            return False
        for startup_dir in self._startup_dirs():
            try:
                base = os.path.normcase(os.path.normpath(startup_dir))
                if target.startswith(base + os.sep):
                    return True
            except Exception:
                continue
        return False

    def _discover_start_menu_shortcuts(
        self, name_regex: str, max_results: int
    ) -> list[str]:
        if max_results <= 0:
            return []
        try:
            compiled = re.compile(name_regex)
        except re.error:
            return []

        results: list[str] = []
        seen_stems: set[str] = set()

        def consider(stem: str, path: str) -> None:
            key = stem.casefold()
            if key in seen_stems:
                return
            seen_stems.add(key)
            results.append(path)

        index = self._get_start_menu_index()
        for prefer_startup in (False, True):
            for stem, path in index:
                if not compiled.search(stem):
                    continue
                if self._is_in_startup_dir(path) != prefer_startup:
                    continue
                consider(stem, path)
                if len(results) >= max_results:
                    return results
        return results

    def _get_start_menu_index(self) -> list[tuple[str, str]]:
        if self._start_menu_index is not None:
            return self._start_menu_index

        items: list[tuple[str, str]] = []
        for base in self._start_menu_dirs():
            try:
                for root, _, files in os.walk(base):
                    for filename in files:
                        if not filename.lower().endswith(".lnk"):
                            continue
                        stem = filename[:-4]
                        items.append((stem, os.path.join(root, filename)))
            except Exception:
                continue

        items.sort(key=lambda x: (x[0].casefold(), x[1].casefold()))
        self._start_menu_index = items
        return items

    def _build_hide_rules(self, instances: list[Any]) -> list[HideRule]:
        rules: list[HideRule] = []
        for inst in instances:
            if not isinstance(inst, dict) or not bool(inst.get("enabled", True)):
                continue

            inst_id = str(inst.get("id", "")).strip().casefold()
            inst_app = str(inst.get("app", "")).strip().casefold()
            is_slack = inst_id.startswith("slack:") or inst_app == "slack"

            action = str(inst.get("hide_action", "hide")).strip().lower()
            if is_slack:
                action = "show"
            if action not in {"hide", "minimize", "show"}:
                action = "hide"

            title_re = None
            title_pat = str(inst.get("window_title_regex", "")).strip()
            if title_pat:
                try:
                    title_re = re.compile(title_pat)
                except re.error:
                    title_re = None

            inst_type = str(inst.get("type", "")).strip().lower()
            app_id = str(inst.get("app_id", "")).strip() or None
            profile_directory = str(inst.get("profile_directory", "")).strip() or None

            process_name = None
            if action != "show" and inst_type != "chrome_pwa":
                exe = str(inst.get("exe", "")).strip()
                if exe:
                    process_name = os.path.basename(exe).casefold()

            resize_to_monitor = bool(inst.get("resize_to_monitor")) or bool(is_slack)

            rules.append(
                HideRule(
                    action=action,
                    title_re=title_re,
                    app_id=app_id,
                    profile_directory=profile_directory,
                    process_name=process_name,
                    resize_to_monitor=resize_to_monitor,
                )
            )
        return rules

    def _launch_instances(self, instances: list[Any]) -> None:
        try:
            running_names, running_pwas = snapshot_running_processes()
        except Exception:
            running_names = set()
            running_pwas = set()

        pwa_budget: dict[str, int] = {}
        pwa_re: dict[str, Pattern[str] | None] = {}

        for inst in instances:
            if not isinstance(inst, dict) or not bool(inst.get("enabled", True)):
                continue
            if str(inst.get("type", "")).strip().lower() != "chrome_pwa":
                continue

            try:
                raw_args = str(inst.get("raw_args", "")).strip()
            except Exception:
                raw_args = ""
            app_id = str(inst.get("app_id", "")).strip()
            if not app_id and raw_args:
                try:
                    _pd, parsed_app_id, _ea = parse_chrome_pwa_args(raw_args)
                    app_id = str(parsed_app_id or "").strip()
                except Exception:
                    app_id = ""
            if app_id:
                continue

            title_pat = str(inst.get("window_title_regex", "")).strip()
            if not title_pat:
                app = str(inst.get("app", "")).strip()
                title_pat = re.escape(app) if app else ""

            if title_pat not in pwa_re:
                title_re = None
                if title_pat:
                    try:
                        title_re = re.compile(title_pat)
                    except re.error:
                        title_re = None
                pwa_re[title_pat] = title_re
            pwa_budget[title_pat] = int(pwa_budget.get(title_pat, 0)) + 1

        if pwa_budget:
            titles: list[str] = []

            def cb(hwnd: int, _param: Any) -> None:
                try:
                    if is_tool_window(hwnd):
                        return
                    title = get_window_text(hwnd)
                    if not title:
                        return
                    try:
                        cls = win32gui.GetClassName(hwnd) or ""
                    except Exception:
                        cls = ""
                    if not str(cls).startswith("Chrome_WidgetWin"):
                        return
                    titles.append(title)
                except Exception:
                    return

            try:
                win32gui.EnumWindows(cb, None)
            except Exception:
                titles = []

            for title_pat, desired in list(pwa_budget.items()):
                title_re = pwa_re.get(title_pat)
                if title_re is None:
                    continue
                existing = 0
                for t in titles:
                    try:
                        if title_re.search(t):
                            existing += 1
                    except Exception:
                        continue
                missing = int(desired) - int(existing)
                pwa_budget[title_pat] = missing if missing > 0 else 0

        for inst in instances:
            if not isinstance(inst, dict) or not bool(inst.get("enabled", True)):
                continue

            inst_type = str(inst.get("type", "")).strip().lower()
            lnk_path = str(inst.get("lnk_path", "")).strip()
            exe = str(inst.get("exe", "")).strip()
            raw_args = str(inst.get("raw_args", "")).strip()

            if (not exe) and lnk_path:
                shortcut = read_shortcut_target_args(lnk_path, log=self._log)
                if shortcut:
                    exe = str(shortcut.get("target", "")).strip()
                    raw_args = str(shortcut.get("args", "")).strip()

            if not exe:
                self._log(f"missing exe for instance: {inst.get('id')!r}")
                continue

            if inst_type == "chrome_pwa":
                inst_id_cf = str(inst.get("id", "")).strip().casefold()
                inst_app_cf = str(inst.get("app", "")).strip().casefold()
                force_launch = bool(
                    inst_id_cf.startswith("google_calendar:")
                    or inst_id_cf.startswith("gmail:")
                    or inst_app_cf in {"google calendar", "gmail"}
                )

                profile_dir = str(inst.get("profile_directory", "")).strip() or None
                app_id = str(inst.get("app_id", "")).strip() or None
                extra_args = inst.get("extra_args", [])
                if not isinstance(extra_args, list):
                    extra_args = []

                if not app_id:
                    profile_dir, app_id, extra_args = parse_chrome_pwa_args(raw_args)

                if app_id and (not force_launch):
                    prof_key = str(profile_dir or "").strip()
                    key = (str(app_id).strip(), prof_key)
                    if key in running_pwas or (prof_key and (str(app_id).strip(), "") in running_pwas):
                        continue

                title_pat = str(inst.get("window_title_regex", "")).strip()
                if not title_pat:
                    app = str(inst.get("app", "")).strip()
                    title_pat = re.escape(app) if app else ""
                if (not app_id) and (not force_launch):
                    if int(pwa_budget.get(title_pat, 0)) <= 0:
                        continue

                if app_id:
                    try:
                        self._remember_launched_pwa(app_id, profile_dir)
                    except Exception:
                        pass

                argv: list[str] = [exe]
                if profile_dir:
                    argv.append(f"--profile-directory={profile_dir}")
                if app_id:
                    argv.append(f"--app-id={app_id}")
                for a in extra_args:
                    if a:
                        argv.append(str(a))
                popen_no_window(argv, log=self._log)
                if app_id:
                    try:
                        running_pwas.add((str(app_id).strip(), str(profile_dir or "").strip()))
                    except Exception:
                        pass
                else:
                    pwa_budget[title_pat] = int(pwa_budget.get(title_pat, 0)) - 1
                continue

            proc_name = os.path.basename(exe).casefold()
            if proc_name and proc_name in running_names:
                continue

            argv = [exe] + split_args(raw_args)
            p = popen_no_window(argv, log=self._log)
            try:
                if p is not None and getattr(p, "pid", None):
                    self._remember_launched_pid(int(p.pid))
            except Exception:
                pass
            try:
                if proc_name:
                    running_names.add(proc_name)
            except Exception:
                pass
        return

    def _hide_worker(
        self,
        gen: int,
        deadline: float,
        poll_interval_sec: float,
        rules: list[HideRule],
        baseline_hwnds: set[int],
    ) -> None:
        poll = float(max(0.05, poll_interval_sec))
        show_once_hwnds: set[int] = set()
        try:
            while True:
                if int(gen) != int(self._hide_generation):
                    return
                now = time.monotonic()
                if now >= float(deadline):
                    return
                try:
                    self._hide_matching_windows(
                        rules,
                        baseline_hwnds=baseline_hwnds,
                        record_managed=True,
                        show_once_hwnds=show_once_hwnds,
                    )
                except Exception as exc:
                    self._log(f"hide worker tick failed: {exc!r}")
                try:
                    time.sleep(poll)
                except Exception:
                    return
        except Exception:
            return

    def _hide_tick(self, root: Any, gen: int) -> None:
        if int(gen) != int(self._hide_generation):
            return
        now = time.monotonic()
        if now >= self._hide_deadline:
            return

        try:
            self._hide_matching_windows(self._hide_rules, record_managed=False)
        except Exception as exc:
            self._log(f"hide tick failed: {exc!r}")

        try:
            delay_ms = int(max(0.05, self._hide_poll_interval_sec) * 1000)
            root.after(delay_ms, self._hide_tick, root, gen)
        except Exception:
            return
        return

    def _hide_matching_windows(
        self,
        rules: list[HideRule],
        baseline_hwnds: set[int] | None = None,
        record_managed: bool = False,
        show_once_hwnds: set[int] | None = None,
    ) -> int:
        if not rules:
            return 0

        hidden = 0
        pid_cache: dict[int, tuple[str, list[str]]] = {}

        def cb(hwnd: int, _param: Any) -> None:
            nonlocal hidden
            try:
                hwnd_int = 0
                try:
                    hwnd_int = int(hwnd)
                except Exception:
                    hwnd_int = 0

                if is_tool_window(hwnd):
                    return
                is_baseline = False
                if baseline_hwnds and hwnd_int > 0:
                    try:
                        is_baseline = hwnd_int in baseline_hwnds
                    except Exception:
                        is_baseline = False

                title = get_window_text(hwnd)

                pid = 0
                proc_name = ""
                cmdline: list[str] = []

                for rule in rules:
                    if is_baseline and rule.action in {"hide", "minimize", "close"}:
                        continue

                    if (
                        show_once_hwnds is not None
                        and rule.action == "show"
                        and hwnd_int > 0
                        and hwnd_int in show_once_hwnds
                    ):
                        continue

                    needs_pid = bool(rule.process_name or rule.app_id)
                    matched_by = ""

                    if needs_pid:
                        if pid == 0:
                            pid = get_window_pid(hwnd)
                            if pid:
                                proc_name, cmdline = get_process_info(pid, cache=pid_cache)
                        if pid == 0:
                            continue

                        if rule.app_id:
                            if cmdline and cmdline_matches_pwa(
                                cmdline,
                                rule.app_id,
                                rule.profile_directory,
                            ):
                                matched_by = "app_id"
                            elif rule.title_re is not None and title:
                                try:
                                    if rule.title_re.search(title):
                                        matched_by = "pwa_title_fallback"
                                except Exception:
                                    matched_by = ""
                            if not matched_by:
                                continue
                        else:
                            if rule.process_name and proc_name == str(rule.process_name):
                                matched_by = "process_name"
                            elif rule.title_re is not None and title:
                                try:
                                    if rule.title_re.search(title):
                                        matched_by = "title_fallback"
                                except Exception:
                                    matched_by = ""
                            if not matched_by:
                                continue

                        if (
                            record_managed
                            and (not is_baseline)
                            and matched_by in {"app_id", "process_name", "pwa_title_fallback"}
                            and hwnd_int > 0
                        ):
                            self._remember_managed_hwnd(hwnd_int)

                        did = apply_window_action(hwnd, rule.action)
                        if rule.action == "show" and rule.resize_to_monitor:
                            try:
                                did = (
                                    resize_window_to_monitor(hwnd, use_work_area=True) or did
                                )
                            except Exception:
                                pass
                        if (
                            show_once_hwnds is not None
                            and rule.action == "show"
                            and hwnd_int > 0
                        ):
                            show_once_hwnds.add(hwnd_int)
                        if did:
                            hidden += 1
                        return

                    if rule.title_re is not None and title:
                        try:
                            if rule.title_re.search(title):
                                if (
                                    record_managed
                                    and (not is_baseline)
                                    and rule.action in {"hide", "minimize", "close"}
                                    and hwnd_int > 0
                                ):
                                    self._remember_managed_hwnd(hwnd_int)
                                did = apply_window_action(hwnd, rule.action)
                                if rule.action == "show" and rule.resize_to_monitor:
                                    try:
                                        did = (
                                            resize_window_to_monitor(hwnd, use_work_area=True)
                                            or did
                                        )
                                    except Exception:
                                        pass
                                if (
                                    show_once_hwnds is not None
                                    and rule.action == "show"
                                    and hwnd_int > 0
                                ):
                                    show_once_hwnds.add(hwnd_int)
                                if did:
                                    hidden += 1
                                return
                        except Exception:
                            continue
            except Exception:
                return

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return hidden
        return hidden

    def _log(self, message: str) -> None:
        try:
            os.makedirs(self._config_dir, exist_ok=True)
        except Exception:
            return

        try:
            ts = datetime.now().isoformat(timespec="seconds")
        except Exception:
            ts = "time"
        line = f"[{ts}] {message}\n"

        try:
            with open(self._log_path, "a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            return
