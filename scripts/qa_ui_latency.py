"""Repeatable, synthetic-only Tk open/reopen/resize responsiveness check."""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import statistics
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profiles", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.profiles <= 100:
        parser.error("profiles must be between 1 and 100")
    sys.path.insert(0, str(args.repo.resolve()))
    import tkinter as tk
    from scripts.qa_ai_usage_native_visual import build_scenario_fixture, SyntheticAiUsageManager, _walk_widgets
    from src.apps.main_ui import WindowsSupporterMainUI
    fixture = build_scenario_fixture("ten-mixed-profiles-150")
    for section in ("settings", "runtime"):
        templates = fixture[section]["profiles"]
        fixture[section]["profiles"] = []
        for index in range(args.profiles):
            profile = copy.deepcopy(templates[index % len(templates)])
            profile.update(id=f"profile_{index:032x}", label=f"QA profile {index + 1}",
                           taskbar_selected=index < 4)
            fixture[section]["profiles"].append(profile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    state_path = args.output.with_suffix(".state.json")
    if state_path.exists():
        parser.error("use a fresh output path to avoid reusing saved window state")
    manager = SyntheticAiUsageManager(fixture)
    root = tk.Tk()
    root.withdraw()
    errors: list[str] = []
    root.report_callback_exception = lambda *error: errors.append(str(error))
    ui = WindowsSupporterMainUI(root, object(), object(), state_path=str(state_path))
    ui._get_ai_usage_monitor = lambda: manager
    result = {"profiles": args.profiles, "repo": str(args.repo.resolve()),
              "tk_version": str(root.tk.call("package", "provide", "Tk"))}
    def measure(callback):
        started = time.perf_counter()
        callback()
        root.update()
        viewport = getattr(ui, "_shell_viewport", None)
        if viewport is not None:
            # Include actual settled rendering, not merely queueing a timer.
            # The deliberately idle 180 ms gesture-settling delay is excluded.
            viewport.flush()
            root.update()
            assert viewport.committed == (root.winfo_width(), root.winfo_height())
        return round((time.perf_counter()-started)*1000, 3)
    try:
        result["first_open_ms"] = measure(lambda: ui.show("ai_usage"))
        view = ui._ai_usage_view
        assert view is not None
        view._stop_runtime_refresh()
        result["native_widget_count"] = len(_walk_widgets(root))
        result["reopen_ms"] = measure(lambda: (ui.hide(), ui.show("ai_usage")))
        root.geometry("1100x860+80+80")
        root.update()
        resize_times = []
        for index in range(8):
            width = 1040 if index % 2 else 1140
            resize_times.append(measure(lambda width=width: root.geometry(f"{width}x860+80+80")))
        result["resize_ms"] = resize_times
        result["resize_median_ms"] = round(statistics.median(resize_times), 3)
        result["resize_max_ms"] = max(resize_times)
        result["callback_errors"] = errors
        result["final_geometry"] = root.geometry()
        assert not errors, errors
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2), flush=True)
    finally:
        if ui._ai_usage_view is not None:
            ui._ai_usage_view._stop_runtime_refresh()
        root.destroy()


if __name__ == "__main__":
    main()
