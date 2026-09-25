"""Native, synthetic-only regression for persistent profile cards and swaps."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from scripts.qa_ai_usage_native_visual import SyntheticAiUsageManager, build_scenario_fixture, _capture_window_png, _walk_widgets
from src.apps.main_ui import WindowsSupporterMainUI
from src.utils.reset_fanfare import play_reset_fanfare
import tkinter as tk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--play-sound', action='store_true')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    fixture = build_scenario_fixture('ten-mixed-profiles-150')
    for name in ('settings', 'runtime'):
        fixture[name]['profiles'] = fixture[name]['profiles'][:6]
        for index, profile in enumerate(fixture[name]['profiles']):
            profile['taskbar_selected'] = index < 4
    manager = SyntheticAiUsageManager(fixture)
    root = tk.Tk(); root.withdraw()
    root.attributes('-topmost', True)
    errors = []
    root.report_callback_exception = lambda *error: errors.append(str(error))
    ui = WindowsSupporterMainUI(root, object(), object(), state_path=str(args.output_dir / 'qa-state.json'))
    ui._get_ai_usage_monitor = lambda: manager
    results = {}
    started = time.perf_counter()
    ui.show('ai_usage'); root.update()
    results['first_open_ms'] = round((time.perf_counter()-started)*1000, 3)
    root.geometry('1200x900+100+100'); root.update()
    view = ui._ai_usage_view
    assert view is not None
    view._stop_runtime_refresh()
    original_widgets = dict(view._pane_card_widgets)
    all_ids = set(view._account_order)
    def settle():
        root.update_idletasks(); root.update()
    def bounds():
        return {key:(card.winfo_rootx(),card.winfo_rooty(),card.winfo_width(),card.winfo_height())
                for key, card in view._pane_card_widgets.items()}
    def assert_retained():
        assert set(view._account_order) == all_ids
        assert all(view._pane_card_widgets[key] is card for key, card in original_widgets.items())
        for index, key in enumerate(view._account_order):
            up, down = view._account_move_buttons[key]
            assert up.instate(['disabled']) == (index == 0)
            assert down.instate(['disabled']) == (index == len(view._account_order)-1)
        for side, ids in view._rendered_pane_assignment.items():
            for index, key in enumerate(ids):
                info = view._pane_card_widgets[key].grid_info()
                assert str(info['in']) == str(view._pane_lists[side])
                assert int(info['row']) == index
    left = view._rendered_pane_assignment['left'][0]
    right = view._rendered_pane_assignment['right'][0]
    source = view._pane_card_widgets[left].winfo_children()[0]
    target = view._pane_card_widgets[right]
    source.event_generate('<ButtonPress-1>', x=8, y=8,
                          rootx=source.winfo_rootx()+8, rooty=source.winfo_rooty()+8)
    settle()
    assert view._drag_state['id'] == left
    before = bounds()
    x, y = target.winfo_rootx()+40, target.winfo_rooty()+40
    started = time.perf_counter()
    source.event_generate('<B1-Motion>', x=10, y=10, rootx=x, rooty=y, state=256)
    settle()
    results['drag_motion_ms'] = round((time.perf_counter()-started)*1000, 3)
    assert view._drag_state['swap'] == right
    assert before == bounds(), 'hover must never change card geometry'
    source.event_generate('<ButtonRelease-1>', x=10, y=10, rootx=x, rooty=y)
    settle(); assert_retained()
    assert view._rendered_pane_assignment['left'][0] == right
    assert view._rendered_pane_assignment['right'][0] == left
    results['native_event_swap'] = True
    pool = view._rendered_pane_assignment['pool'][0]
    displaced = view._rendered_pane_assignment['left'][1]
    assert view._apply_pane_drop(pool, 'left', 1, target_profile_id=displaced)
    settle(); assert_retained()
    assert view._rendered_pane_assignment['left'][1] == pool
    assert view._rendered_pane_assignment['pool'][0] == displaced
    results['full_pool_exchange'] = True
    view._taskbar_side_priority_var.set('right')
    assert view._autosave_now()
    settle(); assert_retained()
    results['right_priority_retains_widgets'] = True
    stacking_checks = 0
    for container in _walk_widgets(root):
        frames = getattr(container, '_windows_supporter_row_frames', [])
        if not frames:
            continue
        siblings = container.winfo_children()
        for widget in siblings:
            try:
                host = widget.pack_info().get('in')
            except tk.TclError:
                continue
            if host in frames:
                assert siblings.index(widget) > siblings.index(host), 'row frame covers a control'
                stacking_checks += 1
    assert stacking_checks > 0
    results['visible_control_stacking_checks'] = stacking_checks
    view._on_pane_drag_start(pool)
    view._on_pane_drag_motion(SimpleNamespace(x_root=x,y_root=y))
    order_before = list(view._account_order)
    assert view._cancel_pane_drag() == 'break'
    settle()
    assert view._account_order == order_before and view._drag_state is None
    results['cancel_preserves_order'] = True
    view._stop_runtime_refresh()
    root.lift(); root.update(); time.sleep(0.1); root.update()
    _capture_window_png(root, args.output_dir / 'verified-ai-usage.png')
    results['callback_errors'] = errors
    assert not errors, errors
    if args.play_sound:
        results['native_async_sound_accepted'] = play_reset_fanfare()
        assert results['native_async_sound_accepted']
        time.sleep(0.9)
    root.destroy()
    (args.output_dir / 'native-regression.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2), flush=True)


if __name__ == '__main__':
    main()
