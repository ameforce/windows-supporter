from __future__ import annotations

import unittest

from src.apps.ai_usage_contracts import (
    plan_taskbar_drop,
    resolve_taskbar_pane_assignment,
)
from src.apps.codex_usage_ui import CodexUsageSettingsView


class _FakeVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        return None


class _FakePaneWidget:
    def __init__(self):
        self.configure_calls = []
        self.grid_calls = []
        self.grid_remove_calls = 0

    def configure(self, **kwargs):
        self.configure_calls.append(kwargs)

    def grid(self, **kwargs):
        self.grid_calls.append(kwargs)

    def grid_remove(self):
        self.grid_remove_calls += 1


class ResolveTaskbarPaneAssignmentTest(unittest.TestCase):
    def test_left_priority_maps_first_two_to_left(self) -> None:
        assignment = resolve_taskbar_pane_assignment(
            ["a", "b", "c", "d", "e"], ["a", "b", "c", "d"], "left"
        )
        self.assertEqual(assignment["left"], ["a", "b"])
        self.assertEqual(assignment["right"], ["c", "d"])
        self.assertEqual(assignment["pool"], ["e"])

    def test_right_priority_swaps_panes(self) -> None:
        assignment = resolve_taskbar_pane_assignment(
            ["a", "b", "c", "d"], ["a", "b", "c", "d"], "right"
        )
        self.assertEqual(assignment["right"], ["a", "b"])
        self.assertEqual(assignment["left"], ["c", "d"])
        self.assertEqual(assignment["pool"], [])

    def test_unselected_profiles_fall_into_pool_in_order(self) -> None:
        assignment = resolve_taskbar_pane_assignment(
            ["a", "b", "c"], ["b"], "left"
        )
        self.assertEqual(assignment["left"], ["b"])
        self.assertEqual(assignment["right"], [])
        self.assertEqual(assignment["pool"], ["a", "c"])

    def test_selection_truncates_to_four(self) -> None:
        assignment = resolve_taskbar_pane_assignment(
            ["a", "b", "c", "d", "e"], ["a", "b", "c", "d", "e"], "left"
        )
        self.assertEqual(assignment["left"] + assignment["right"], ["a", "b", "c", "d"])
        self.assertEqual(assignment["pool"], ["e"])


class PlanTaskbarDropTest(unittest.TestCase):
    def test_reorder_within_left_pane(self) -> None:
        plan = plan_taskbar_drop(
            ["a", "b", "c", "d"], ["a", "b", "c", "d"], "left", "b", "left", 0
        )
        self.assertEqual(plan["order"][:4], ["b", "a", "c", "d"])
        self.assertEqual(plan["selected"], ["b", "a", "c", "d"])

    def test_move_across_panes_left_priority(self) -> None:
        plan = plan_taskbar_drop(
            ["a", "b", "c", "d"], ["a", "b", "c", "d"], "left", "d", "left", 0
        )
        # "c" slides back to the right pane; selection stays within limits.
        self.assertEqual(plan["selected"], ["d", "a", "b", "c"])
        self.assertEqual(plan["order"][:4], ["d", "a", "b", "c"])

    def test_move_across_panes_right_priority(self) -> None:
        plan = plan_taskbar_drop(
            ["a", "b", "c", "d"], ["a", "b", "c", "d"], "right", "c", "right", 0
        )
        self.assertEqual(plan["selected"], ["c", "a", "b", "d"])

    def test_drop_into_pool_deselects(self) -> None:
        plan = plan_taskbar_drop(
            ["a", "b", "c"], ["a", "b"], "left", "b", "pool", 0
        )
        self.assertEqual(plan["selected"], ["a"])
        self.assertIn("b", plan["order"])
        self.assertGreater(plan["order"].index("b"), 0)

    def test_promote_from_pool_selects(self) -> None:
        plan = plan_taskbar_drop(
            ["a", "b", "c"], ["a"], "left", "c", "left", 1
        )
        self.assertEqual(plan["selected"], ["a", "c"])

    def test_full_selection_rejects_pool_promotion(self) -> None:
        with self.assertRaises(ValueError):
            plan_taskbar_drop(
                ["a", "b", "c", "d", "e"],
                ["a", "b", "c", "d"],
                "left",
                "e",
                "left",
                0,
            )

    def test_priority_pane_bottom_gap_aliases_to_peer_pane_top(self) -> None:
        # Linear-sequence semantics: left-bottom (position 2) and
        # right-top (position 0) address the same global index 2.
        left_plan = plan_taskbar_drop(
            ["a", "b", "c", "d"], ["a", "b", "c", "d"], "left", "c", "left", 2
        )
        right_plan = plan_taskbar_drop(
            ["a", "b", "c", "d"], ["a", "b", "c", "d"], "left", "c", "right", 0
        )
        self.assertEqual(left_plan, right_plan)
        self.assertEqual(left_plan["selected"], ["a", "b", "c", "d"])

    def test_selected_limit_mirrors_profile_limit(self) -> None:
        from src.apps.ai_usage_contracts import TASKBAR_SELECTED_LIMIT
        from src.apps.codex_usage_multi_monitor import TASKBAR_PROFILE_LIMIT

        self.assertEqual(int(TASKBAR_SELECTED_LIMIT), int(TASKBAR_PROFILE_LIMIT))

    def test_unknown_profile_rejects_drop(self) -> None:
        with self.assertRaises(ValueError):
            plan_taskbar_drop(["a"], ["a"], "left", "zzz", "left", 0)


class ApplyPaneDropTest(unittest.TestCase):
    def _build_view(self, *, order, selected, priority="left"):
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        view._account_order = list(order)
        view._account_taskbar_selected_vars = {
            profile_id: _FakeVar(profile_id in set(selected)) for profile_id in order
        }
        view._taskbar_side_priority_var = _FakeVar(priority)
        view._autosave_calls = 0
        view._remount_calls = 0

        def _autosave_now():
            view._autosave_calls += 1
            return True

        def _remount():
            view._remount_calls += 1
            return None

        view._autosave_now = _autosave_now  # type: ignore[method-assign]
        view._remount = _remount  # type: ignore[method-assign]
        return view

    def test_drop_reorders_and_persists_selection(self) -> None:
        view = self._build_view(order=["a", "b", "c"], selected=["a", "b"])
        view._apply_pane_drop("b", "left", 0)
        self.assertEqual(view._account_order[:2], ["b", "a"])
        self.assertTrue(view._account_taskbar_selected_vars["b"].get())
        self.assertEqual(view._autosave_calls, 1)
        # Box re-rendering is owned by the save path
        # (_pane_assignment_rendered_stale), not by the drop handler.
        self.assertEqual(view._remount_calls, 0)

    def test_drop_into_pool_clears_selection_flag(self) -> None:
        view = self._build_view(order=["a", "b", "c"], selected=["a", "b"])
        view._apply_pane_drop("b", "pool", 0)
        self.assertFalse(view._account_taskbar_selected_vars["b"].get())
        self.assertTrue(view._account_taskbar_selected_vars["a"].get())
        self.assertEqual(view._autosave_calls, 1)
        self.assertEqual(view._remount_calls, 0)

    def test_stale_assignment_detects_priority_flip(self) -> None:
        view = self._build_view(
            order=["a", "b", "c", "d"],
            selected=["a", "b", "c", "d"],
            priority="left",
        )
        view._rendered_side_priority = "left"
        view._rendered_pane_assignment = resolve_taskbar_pane_assignment(
            ["a", "b", "c", "d"], ["a", "b", "c", "d"], "left"
        )
        view._pane_lists = {"left": object(), "right": object(), "pool": object()}
        self.assertFalse(view._pane_assignment_rendered_stale())
        view._taskbar_side_priority_var.set("right")
        self.assertTrue(view._pane_assignment_rendered_stale())


class PaneDragFeedbackTest(unittest.TestCase):
    def _build_view(self, target):
        view = CodexUsageSettingsView(root=None, codex_monitor=None)
        profile_ids = [f"profile-{index}" for index in range(8)]
        view._profile_deletions_inflight = set()
        view._profile_actions_inflight = set()
        view._drag_state = {"id": profile_ids[0], "target": None, "index": 0}
        view._pane_boxes = {
            side: _FakePaneWidget() for side in ("left", "right", "pool")
        }
        view._pane_lists = {
            side: _FakePaneWidget() for side in ("left", "right", "pool")
        }
        view._drop_indicators = {
            side: _FakePaneWidget() for side in ("left", "right", "pool")
        }
        view._pane_card_widgets = {
            profile_id: _FakePaneWidget() for profile_id in profile_ids
        }
        view._rendered_pane_assignment = {
            "left": [],
            "right": [],
            "pool": profile_ids,
        }
        view._pane_drop_target_at = lambda *_args: target[0]
        return view, profile_ids

    def test_repeated_motion_in_same_slot_does_not_reflow_eight_profiles(self) -> None:
        target = [("pool", 2)]
        view, profile_ids = self._build_view(target)
        event = type("Event", (), {"x_root": 10, "y_root": 20})()

        view._on_pane_drag_motion(event)
        box_configure_counts = {
            side: len(box.configure_calls)
            for side, box in view._pane_boxes.items()
        }

        for _ in range(99):
            view._on_pane_drag_motion(event)

        self.assertEqual(
            {side: len(box.configure_calls) for side, box in view._pane_boxes.items()},
            box_configure_counts,
        )

        self.assertEqual(view._drag_state["target"], "pool")
        self.assertEqual(view._drag_state["index"], 2)
        self.assertEqual(
            [len(view._pane_card_widgets[item].grid_calls) for item in profile_ids[1:]],
            [1] * 7,
        )
        self.assertEqual(len(view._drop_indicators["pool"].grid_calls), 1)

        target[0] = ("pool", 3)
        view._on_pane_drag_motion(event)

        self.assertEqual(view._drag_state["index"], 3)
        self.assertEqual(
            [len(view._pane_card_widgets[item].grid_calls) for item in profile_ids[1:]],
            [2] * 7,
        )
        self.assertEqual(len(view._drop_indicators["pool"].grid_calls), 2)

        target[0] = ("left", 0)
        view._on_pane_drag_motion(event)

        self.assertEqual(view._drag_state["target"], "left")
        self.assertEqual(view._drag_state["index"], 0)
        self.assertEqual(len(view._drop_indicators["left"].grid_calls), 1)
        self.assertEqual(view._drop_indicators["pool"].grid_remove_calls, 3)

    def test_repeated_motion_outside_panes_clears_feedback_once(self) -> None:
        target = [("pool", 2)]
        view, _profile_ids = self._build_view(target)
        event = type("Event", (), {"x_root": 10, "y_root": 20})()
        view._on_pane_drag_motion(event)

        target[0] = None
        for _ in range(100):
            view._on_pane_drag_motion(event)

        self.assertIsNone(view._drag_state["target"])
        self.assertEqual(view._drop_indicators["pool"].grid_remove_calls, 2)
        self.assertEqual(
            [widget.grid_remove_calls for widget in view._drop_indicators.values()],
            [2, 2, 2],
        )

    def test_inflight_transition_clears_target_highlight_once(self) -> None:
        target = [("left", 0)]
        view, _profile_ids = self._build_view(target)
        event = type("Event", (), {"x_root": 10, "y_root": 20})()
        view._on_pane_drag_motion(event)
        self.assertEqual(
            view._pane_boxes["left"].configure_calls[-1],
            {"highlightbackground": "#2563EB", "highlightthickness": 2},
        )

        view._profile_actions_inflight.add("profile-1")
        view._on_pane_drag_motion(event)
        self.assertIsNone(view._drag_state["target"])
        self.assertEqual(
            view._pane_boxes["left"].configure_calls[-1],
            {"highlightbackground": "#E5E7EB", "highlightthickness": 1},
        )
        box_call_counts = {
            side: len(box.configure_calls)
            for side, box in view._pane_boxes.items()
        }
        indicator_remove_counts = {
            side: indicator.grid_remove_calls
            for side, indicator in view._drop_indicators.items()
        }

        for _ in range(100):
            view._on_pane_drag_motion(event)

        self.assertEqual(
            {side: len(box.configure_calls) for side, box in view._pane_boxes.items()},
            box_call_counts,
        )
        self.assertEqual(
            {side: indicator.grid_remove_calls for side, indicator in view._drop_indicators.items()},
            indicator_remove_counts,
        )


if __name__ == "__main__":
    unittest.main()
