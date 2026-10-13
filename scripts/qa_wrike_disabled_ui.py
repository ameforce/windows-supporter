"""Targeted native Tk acceptance for Wrike automatic-notification disable.

Uses production settings/widgets/lifecycle with temporary APPDATA, no credentials,
no network/browser, and a synthetic last-input source. Does not test deployment.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 4, 6, 9, 0)
        return value if tz is None else value.replace(tzinfo=tz)


class IsolatedSecretStore:
    def __init__(self, *_args, **_kwargs):
        pass

    def protect(self, _value):
        raise AssertionError("Native acceptance must not save credentials")

    def unprotect(self, value):
        if value:
            raise AssertionError("Native acceptance must not read credentials")
        return ""


class SyntheticWatcher:
    instances = []

    def __init__(self, root, callback, **kwargs):
        self.root = root
        self.callback = callback
        self.idle_callback = kwargs.get("idle_callback")
        self.started = False
        self.stopped = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def reset_baseline(self):
        pass


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def pump(root, seconds=0.10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()


def run(output: Path, capture: bool) -> dict:
    import tkinter as tk
    from src.apps.Wrike import Wrike
    from src.apps.main_ui import WindowsSupporterMainUI
    from src.apps.wrike_timelog_snapshot import TimelogDay, make_fresh_snapshot

    output.mkdir(parents=True, exist_ok=True)
    result = {
        "scope": "native Tk source harness; installed runtime not inspected",
        "isolation": "temporary APPDATA; synthetic input; network/browser/credentials blocked",
        "checks": [],
        "callback_errors": [],
        "source_sha256": {
            name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
            for name in (
                "src/apps/Wrike.py", "src/apps/wrike_ui.py",
                "src/apps/main_ui.py", "src/apps/dashboard_ui.py",
            )
        },
    }
    root = None
    wrike = None

    def check(name, condition, **observed):
        entry = {"name": name, "passed": bool(condition), **observed}
        result["checks"].append(entry)
        if not condition:
            raise AssertionError(json.dumps(entry, ensure_ascii=False))

    try:
        with tempfile.TemporaryDirectory(prefix="wrike-disabled-ui-") as scratch, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "APPDATA": scratch, "WRIKE_ACCESS_TOKEN": "",
            }))
            stack.enter_context(patch("src.apps.Wrike.SecretStore", IsolatedSecretStore))
            stack.enter_context(patch("src.apps.Wrike.WorktimeActivityWatcher", SyntheticWatcher))
            stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("network forbidden")))
            stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")))
            stack.enter_context(patch("webbrowser.open", side_effect=AssertionError("browser forbidden")))
            wrike = Wrike()
            wrike._Wrike__lib.datetime = FrozenDateTime
            for method in (
                "__start_ical_polling", "__start_vacation_ical_polling",
                "__start_flex_polling", "__schedule_flex_poll",
                "__prewarm_wrike_form_browser_async", "__request_timelog_snapshot_refresh",
            ):
                stack.enter_context(patch.object(wrike, "_Wrike" + method, return_value=None))
            now = FrozenDateTime.now()
            snapshot = make_fresh_snapshot(
                days=tuple(TimelogDay(date(2026, 4, 6) + timedelta(days=i), 0) for i in range(7)),
                display_name="Isolated Native QA", fetched_at=now, generation=1,
            )
            wrike._Wrike__timelog_snapshot = snapshot
            wrike._Wrike__timelog_last_good = snapshot
            wrike._Wrike__timelog_refresh_generation = 1
            root = tk.Tk()
            root.title("Wrike disable acceptance — isolated QA")
            root.report_callback_exception = lambda kind, error, _tb: result["callback_errors"].append(
                f"{kind.__name__}: {error}"
            )
            ui = WindowsSupporterMainUI(
                root, startup_manager=None,
                monitor=SimpleNamespace(
                    get_wrike=lambda: wrike,
                    get_dashboard_status_snapshot=lambda: {"enabled": True},
                ),
                state_path=str(Path(scratch) / "ui-state.json"),
            )
            ui.show_wrike()
            pump(root)
            check("production settings view mounted", ui._wrike_built)
            view = ui._wrike_view
            checkbox = next(
                widget for widget in descendants(view._win)
                if widget.winfo_class() == "Checkbutton"
                and str(widget.cget("variable")) == str(view._monitor_enabled_var)
            )
            settings_path = Path(wrike.get_settings_snapshot()["settings_path"])
            check("settings file isolated", settings_path.is_relative_to(Path(scratch)))

            def disk_enabled():
                return bool(json.loads(settings_path.read_text(encoding="utf-8"))["monitor_enabled"])

            def enabled():
                return bool(wrike.get_settings_snapshot()["monitor_enabled"])

            def visible():
                panel = wrike._Wrike__worktime_panel
                return bool(panel and panel.is_visible() and panel._window.winfo_ismapped())

            # Native callback enters the real production activity/visibility path.
            wrike.attach(root)
            old_watcher = SyntheticWatcher.instances[-1]
            old_watcher.callback(now)
            pump(root)
            check("eligible automatic panel maps while enabled", visible(), mapped=visible())
            checkbox.invoke()
            check("checkbox persists disable immediately", not enabled() and not disk_enabled()
                  and not view._monitor_enabled_var.get(), runtime=enabled(), disk=disk_enabled())
            check("Wrike disable retains Background on", wrike._Wrike__background_active,
                  background_active=wrike._Wrike__background_active)
            pump(root)
            check("disable hides open automatic panel", not visible(), mapped=visible(), watcher_stopped=old_watcher.stopped)
            check("disable stops activity source", old_watcher.stopped)
            root.after(1, lambda: old_watcher.callback(now))
            pump(root)
            check("delayed callback cannot reopen while disabled", not visible(), mapped=visible())
            check("direct automatic request rejected while disabled", not wrike._Wrike__show_activity_panel())
            wrike.show_weekly_timelog_summary(root)
            pump(root)
            check("manual panel maps while disabled", visible() and not enabled(), mapped=visible(), runtime=enabled())
            if capture:
                from scripts.native_window_capture import capture_window
                result["manual_panel_capture"] = capture_window(
                    wrike._Wrike__worktime_panel._window, output / "manual-panel-disabled.png"
                )
            wrike._Wrike__worktime_panel.hide()
            checkbox.invoke()
            check("reenable saved immediately", enabled() and disk_enabled() and view._monitor_enabled_var.get())
            root.after(1, lambda: old_watcher.callback(now))
            pump(root)
            check("old callback rejected after off-on cycle", not visible(), mapped=visible())
            new_watcher = SyntheticWatcher.instances[-1]
            check("reenable starts fresh activity source", new_watcher is not old_watcher and new_watcher.started)
            new_watcher.callback(now)
            pump(root)
            check("reenable restores eligible automatic display", visible(), mapped=visible())

            with patch.object(wrike, "_Wrike__save_settings", return_value=False):
                checkbox.invoke()
            check("failed checkbox save reverts widget runtime and disk", enabled() and disk_enabled()
                  and view._monitor_enabled_var.get(), runtime=enabled(), disk=disk_enabled(),
                  checkbox=bool(view._monitor_enabled_var.get()), status=view._status_var.get())
            check("failed disable preserves automatic panel", visible(), mapped=visible())
            check("checkbox save failure shown", "저장 실패" in view._status_var.get())

            # A pending invalid draft must not prevent the dashboard toggle save.
            view._daily_var.set("unfinished draft")
            pending = view._autosave_after_id
            ui._dashboard_wrike_toggle_enabled()
            check("dashboard toggle saves and synchronizes existing form", not enabled() and not disk_enabled()
                  and not view._monitor_enabled_var.get())
            check("dashboard preserves pending other-field draft", view._daily_var.get() == "unfinished draft"
                  and view._autosave_after_id == pending)
            if pending is not None:
                view._win.after_cancel(pending)
                view._autosave_after_id = None
            view._loading_settings = True
            view._daily_var.set("8")
            view._loading_settings = False
            ui.show_dashboard()
            ui.show_wrike()
            pump(root)
            view = ui._wrike_view
            check("production settings reentry mounted", ui._wrike_built and view._win.winfo_exists())
            check("dashboard to settings reentry reads disabled state", not view._monitor_enabled_var.get()
                  and not enabled())
            with patch.object(wrike, "_Wrike__save_settings", return_value=False):
                ui._dashboard_wrike_toggle_enabled()
            check("dashboard failed save preserves disabled state", not enabled() and not disk_enabled()
                  and not view._monitor_enabled_var.get())
            check("dashboard save failure exposed", "저장 실패" in ui._get_wrike_dashboard_status().get("settings_error", ""))
            check("no native Tk callback errors", not result["callback_errors"])
            if capture:
                result["settings_capture"] = capture_window(root, output / "settings-disabled.png")
            wrike.shutdown()
            wrike = None
            root.destroy()
            root = None
        result["passed"] = True
        result["scratch_cleaned"] = not Path(scratch).exists()
    except Exception as error:
        result["passed"] = False
        result["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc(file=sys.stderr)
    finally:
        if wrike is not None:
            wrike.shutdown()
        if root is not None:
            root.destroy()
        (output / "native-disabled-ui.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    result = run(args.output.resolve(), args.capture)
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]),
                      "evidence": str(args.output.resolve() / "native-disabled-ui.json")}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
