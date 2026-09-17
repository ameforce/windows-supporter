"""Regression contracts for native surface recovery and slot convergence."""
import unittest
from unittest.mock import patch

from src.apps import codex_usage_taskbar_overlay as overlay_module
from tests.unit.test_codex_usage_taskbar_overlay import (
    _FakeRoot,
    _FakeWindow,
    AiUsageTaskbarOverlayPaneTest,
)


class TaskbarVisibilityRecoveryTest(unittest.TestCase):
    def make_overlay(self):
        windows = []
        overlay = overlay_module.CodexUsageTaskbarOverlay(
            _FakeRoot(),
            lambda: AiUsageTaskbarOverlayPaneTest._runtime(2),
            window_factory=lambda root: windows.append(_FakeWindow()) or windows[-1],
            work_area_getter=lambda: (0, 0, 1920, 1040),
            occupied_span_getter=lambda *args: [(0, 100), (700, 1920)],
            fullscreen_detector=lambda window: False,
            stable_slot_selection=True,
        )
        self.assertTrue(overlay.refresh())
        return overlay, windows

    def test_geometry_tick_restores_hidden_surface_with_unchanged_model(self):
        overlay, windows = self.make_overlay()
        window = windows[0]
        window.withdraw()
        overlay._window_visible = False
        calls = window.deiconify_calls
        overlay._geometry_monitor_tick()
        self.assertTrue(overlay._window_visible)
        self.assertGreater(window.deiconify_calls, calls)
        self.assertIsNotNone(overlay._keepalive_after_id)

    def test_geometry_tick_recreates_missing_surface_with_unchanged_model(self):
        overlay, windows = self.make_overlay()
        overlay._discard_dead_window()
        overlay._geometry_monitor_tick()
        self.assertEqual(len(windows), 2)
        self.assertTrue(windows[-1].geometry_calls)
        self.assertTrue(windows[-1].draw_calls)
        self.assertTrue(overlay._window_visible)

    def test_geometry_tick_replaces_destroyed_surface_before_noop_comparison(self):
        overlay, windows = self.make_overlay()
        windows[0].winfo_exists = lambda: False
        overlay._geometry_monitor_tick()
        self.assertEqual(len(windows), 2)
        self.assertTrue(overlay._window_visible)

    def test_same_side_width_return_converges_after_four_samples(self):
        overlay, _ = self.make_overlay()
        a = dict(x=200, y=1041, width=300, height=38,
                 visible=True, orientation="bottom", _slot_side="left")
        b = dict(a, width=450)
        context = (1920, 1080, "bottom")
        overlay._remember_same_side_transition(a, b)
        results = [overlay._stabilize_transient_geometry_regression(
            b, a, previous_context=context, candidate_context=context,
        ) for _ in range(4)]
        self.assertEqual(results[:3], [b, b, b])
        self.assertEqual(results[3], a)

    def test_geometry_error_keeps_recovery_timer_alive(self):
        overlay, _ = self.make_overlay()
        with patch.object(overlay, "_calculate_geometry", side_effect=RuntimeError("sample")):
            overlay._geometry_monitor_tick()
        self.assertIsNotNone(overlay._geometry_after_id)
        overlay._geometry_monitor_tick()
        self.assertTrue(overlay._window_visible)


if __name__ == "__main__":
    unittest.main()
