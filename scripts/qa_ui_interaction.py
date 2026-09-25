"""Measure interaction stalls using the application's normal Tk event loop."""
import argparse
import copy
import json
from pathlib import Path
import statistics
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--repo', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.repo.resolve()))
from scripts.qa_ai_usage_native_visual import build_scenario_fixture, SyntheticAiUsageManager
from src.apps.main_ui import WindowsSupporterMainUI
import tkinter as tk
fixture = build_scenario_fixture('ten-mixed-profiles-150')
manager = SyntheticAiUsageManager(fixture)
root = tk.Tk(); root.withdraw()
args.output.parent.mkdir(parents=True, exist_ok=True)
ui = WindowsSupporterMainUI(root, object(), object(), state_path=str(args.output.with_suffix('.state.json')))
ui._get_ai_usage_monitor = lambda: manager
errors, gaps, callbacks = [], [], []
root.report_callback_exception = lambda *error: errors.append(str(error))
last = time.perf_counter()
phase = 'open'
result = {}
layout_calls = []
original_place = ui._shell_frame.place
def tracked_place(**options):
    if phase == 'resize':
        layout_calls.append(time.perf_counter())
    return original_place(**options)
ui._shell_frame.place = tracked_place

def heartbeat():
    global last
    now = time.perf_counter()
    if phase == 'resize':
        gaps.append((now-last)*1000)
    last = now
    root.after(16, heartbeat)

def show():
    start = time.perf_counter()
    ui.show('ai_usage')
    result['show_callback_ms'] = (time.perf_counter()-start)*1000
    root.after(1000, begin_resize)

def begin_resize():
    global phase, last
    ui._ai_usage_view._stop_runtime_refresh()
    root.geometry('1040x860+80+80')
    phase = 'resize'
    last = time.perf_counter()
    root.after(200, lambda: resize(0))

def resize(index):
    start = time.perf_counter()
    root.geometry(f'{1040+(index%16)*6}x860+80+80')
    callbacks.append((time.perf_counter()-start)*1000)
    root.after(50, (lambda: resize(index+1)) if index < 47 else (lambda: root.after(250, finish)))

def finish():
    result.update(content_layouts=len(layout_calls), resize_callbacks=len(callbacks),
                  resize_callback_max_ms=max(callbacks),
                  event_gap_median_ms=statistics.median(gaps),
                  event_gap_p95_ms=sorted(gaps)[int((len(gaps)-1)*0.95)],
                  event_gap_max_ms=max(gaps), callback_errors=errors)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2), flush=True)
    root.destroy()

root.after(16, heartbeat)
root.after(100, show)
root.after(60000, lambda: (print('FAIL: event-loop watchdog', flush=True), root.destroy()))
root.mainloop()
if not args.output.exists() or errors:
    raise SystemExit(1)
