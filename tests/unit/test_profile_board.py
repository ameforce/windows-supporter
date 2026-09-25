import copy
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from scripts.qa_ai_usage_native_visual import build_scenario_fixture, SyntheticAiUsageManager, _walk_widgets
from src.apps.ai_usage_ui import AIUsageSettingsView
from src.apps.profile_controls import ProfileActionBinding


def make_manager(count):
    fixture = build_scenario_fixture("ten-mixed-profiles-150")
    for name in ("settings", "runtime"):
        original = fixture[name]["profiles"]
        result = []
        for index in range(count):
            profile = copy.deepcopy(original[index % len(original)])
            profile.update(id=f"profile_{index:032x}", label=f"Profile {index}", taskbar_selected=index < 4)
            result.append(profile)
        fixture[name]["profiles"] = result
    return SyntheticAiUsageManager(fixture)


class ActionBindingTest(unittest.TestCase):
    def test_inactive_profile_state_is_applied_on_selection(self):
        binding = ProfileActionBinding()
        binding.state(["disabled"])
        button = Mock()
        binding.attach(button)
        button.configure.assert_called_once_with(state="disabled")
        binding.detach()
        binding.state(["!disabled"])
        button.configure.assert_called_once()
        next_button = Mock()
        binding.attach(next_button)
        next_button.configure.assert_called_once_with(state="normal")


class ProfileBoardNativeTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.geometry("1100x800+80+80")
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.view = AIUsageSettingsView(self.root, make_manager(6))
        self.view.mount(self.root)
        self.view._stop_runtime_refresh()
        self.root.update_idletasks()

    def tearDown(self):
        self.view._stop_runtime_refresh()
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def test_profile_count_does_not_multiply_native_widgets(self):
        initial = len(_walk_widgets(self.root))
        self.view._codex = make_manager(30)
        self.view.mount(self.root)
        self.view._stop_runtime_refresh()
        self.root.update_idletasks()
        self.assertEqual(len(self.view._pane_card_widgets), 30)
        self.assertEqual(len(_walk_widgets(self.root)), initial)
        self.assertEqual(len({card.winfo_id() for card in self.view._pane_card_widgets.values()}), 1)

    def test_inspector_targets_selected_profile_without_mutating_other_profiles(self):
        first, second = self.view._account_order[:2]
        inspector = self.view._profile_inspector
        inspector.select(second)
        self.assertEqual(inspector.selected_id, second)
        self.assertEqual(str(inspector.enabled.cget("variable")), str(self.view._account_enabled_vars[second]))
        self.view._on_account_query = Mock()
        inspector.buttons["query"].invoke()
        self.view._on_account_query.assert_called_once_with(second)
        self.assertTrue(self.view._account_enabled_vars[first].get())
        self.assertTrue(self.view._pane_card_widgets[second].selected)
        self.assertFalse(self.view._pane_card_widgets[first].selected)

    def test_refresh_relayouts_only_the_changed_profile(self):
        first, second = self.view._account_order[:2]
        from unittest.mock import patch
        first_detail = self.view._account_detail_canvases[first]
        second_detail = self.view._account_detail_canvases[second]
        with patch.object(first_detail, "layout", wraps=first_detail.layout) as changed:
            with patch.object(second_detail, "layout", wraps=second_detail.layout) as unchanged:
                self.view._account_status_vars[first].set("Updated status")
                self.root.update_idletasks()
                changed.assert_called_once()
                unchanged.assert_not_called()

    def test_scroll_offset_is_applied_to_drop_hit_testing(self):
        self.root.deiconify()
        self.root.update()
        board = self.view._profile_board
        pool = self.view._rendered_pane_assignment["pool"][0]
        region = self.view._pane_card_widgets[pool]
        board.ensure_visible(pool)
        self.root.update_idletasks()
        self.assertGreater(board.canvas.canvasy(0), 0)
        self.assertAlmostEqual(region.winfo_rooty(), board.canvas.winfo_rooty()+region.y-board.canvas.canvasy(0), delta=1)
        target = self.view._pane_drop_target_at(region.winfo_rootx()+20, region.winfo_rooty()+20)
        self.assertIsNotNone(target)
        self.assertEqual(target[0], "pool")

    def test_full_pool_swap_preserves_the_single_canvas_and_all_profiles(self):
        order = list(self.view._account_order)
        selected = self.view._rendered_pane_assignment["left"][0]
        pool = self.view._rendered_pane_assignment["pool"][0]
        canvas = self.view._profile_board.canvas
        original = dict(self.view._pane_card_widgets)
        self.assertTrue(self.view._apply_pane_drop(pool, "left", 0, target_profile_id=selected))
        self.root.update_idletasks()
        self.assertIs(self.view._profile_board.canvas, canvas)
        self.assertEqual(original, self.view._pane_card_widgets)
        self.assertEqual(set(self.view._account_order), set(order))
        self.assertEqual(self.view._rendered_pane_assignment["left"][0], pool)
        self.assertEqual(self.view._rendered_pane_assignment["pool"][0], selected)

    def test_cancel_stops_edge_autoscroll(self):
        self.root.deiconify()
        self.root.update()
        board = self.view._profile_board
        profile = self.view._account_order[0]
        self.view._on_pane_drag_start(profile)
        self.view._drag_state["active"] = True
        event = SimpleNamespace(x_root=board.canvas.winfo_rootx()+30,
                                y_root=board.canvas.winfo_rooty()+board.canvas.winfo_height()-2)
        board._drag_motion(event)
        self.assertIsNotNone(board.autoscroll)
        board._cancel_drag(None)
        self.assertIsNone(board.autoscroll)
        self.assertIsNone(board.pointer)
        self.assertIsNone(self.view._drag_state)

    def test_remount_disposes_previous_board_and_layout_callbacks(self):
        board = self.view._profile_board
        old_details = list(self.view._account_detail_canvases.values())
        self.view._account_status_vars[self.view._account_order[0]].set("Pending value")
        self.assertIsNotNone(board.pending)
        self.view.mount(self.root)
        self.view._stop_runtime_refresh()
        self.root.update_idletasks()
        self.assertTrue(board.closed)
        self.assertIsNone(board.pending)
        self.assertTrue(all(detail._destroyed for detail in old_details))

    def test_cached_text_bounds_match_tk_after_translation_resize_and_text_change(self):
        board = self.view._profile_board
        for width in (1100, 900, 1100, 700):
            board.width = width
            board.layout()
            for region in board.regions:
                for item in region.local_coords:
                    actual = board.canvas.bbox(item)
                    expected = None if actual is None else (actual[0]-region.x, actual[1]-region.y,
                                                            actual[2]-region.x, actual[3]-region.y)
                    self.assertEqual(region.bbox(item), expected)
        first = self.view._account_order[0]
        self.view._account_label_vars[first].set("Long profile title " * 15)
        self.root.update_idletasks()
        region = self.view._pane_card_widgets[first]
        for item in region.local_coords:
            actual = board.canvas.bbox(item)
            if actual is not None:
                self.assertEqual(region.bbox(item), (actual[0]-region.x, actual[1]-region.y,
                                                     actual[2]-region.x, actual[3]-region.y))
