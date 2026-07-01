import unittest

from src.apps.codex_usage_taskbar_targets import (
    TaskbarMonitorSnapshot,
    TaskbarWindowSnapshot,
    build_taskbar_overlay_targets,
    target_cache_key,
)


def _monitor(
    *,
    handle: int = 1,
    device: str = r"\\.\DISPLAY1",
    display_num: int | None = 1,
    is_primary: bool = True,
    monitor: tuple[int, int, int, int] = (0, 0, 1920, 1080),
    work: tuple[int, int, int, int] = (0, 0, 1920, 1040),
) -> TaskbarMonitorSnapshot:
    return TaskbarMonitorSnapshot(
        handle=handle,
        device=device,
        display_num=display_num,
        is_primary=is_primary,
        monitor=monitor,
        work=work,
    )


def _window(
    *,
    hwnd: int = 10,
    class_name: str = "Shell_TrayWnd",
    rect: tuple[int, int, int, int] = (0, 1040, 1920, 1080),
    visible: bool = True,
) -> TaskbarWindowSnapshot:
    return TaskbarWindowSnapshot(
        hwnd=hwnd,
        class_name=class_name,
        rect=rect,
        visible=visible,
    )


def _target(
    monitor: TaskbarMonitorSnapshot,
    window: TaskbarWindowSnapshot | None,
):
    windows = () if window is None else (window,)
    targets = build_taskbar_overlay_targets((monitor,), windows)
    return targets[0]


class CodexUsageTaskbarTargetsTest(unittest.TestCase):
    def test_bottom_taskbar_is_displayable_with_rca_class_visible_horizontal(self):
        # Given
        monitor = _monitor()
        window = _window()

        # When
        target = _target(monitor, window)

        # Then
        self.assertTrue(target.displayable)
        self.assertTrue(target.taskbar_visible)
        self.assertEqual(target.orientation, "bottom")
        self.assertEqual(target.orientation_source, "work_area_reserved")
        self.assertEqual(target.orientation_confidence, "high")
        self.assertEqual(target.displayable_reason, "displayable")
        self.assertEqual(target.fallback_reason, "")
        self.assertEqual(target.rca_class, "displayable_horizontal_taskbar")

    def test_top_taskbar_is_displayable_with_work_area_orientation_source(self):
        # Given
        monitor = _monitor(work=(0, 40, 1920, 1080))
        window = _window(rect=(0, 0, 1920, 40))

        # When
        target = _target(monitor, window)

        # Then
        self.assertTrue(target.displayable)
        self.assertTrue(target.taskbar_visible)
        self.assertEqual(target.orientation, "top")
        self.assertEqual(target.orientation_source, "work_area_reserved")
        self.assertEqual(target.orientation_confidence, "high")
        self.assertEqual(target.displayable_reason, "displayable")
        self.assertEqual(target.fallback_reason, "")
        self.assertEqual(target.rca_class, "displayable_horizontal_taskbar")

    def test_side_taskbars_are_not_displayable_with_unsupported_side_reason(self):
        # Given
        left_monitor = _monitor(work=(80, 0, 1920, 1080))
        left_window = _window(rect=(0, 0, 80, 1080))
        right_monitor = _monitor(work=(0, 0, 1840, 1080))
        right_window = _window(rect=(1840, 0, 1920, 1080))

        # When
        left = _target(left_monitor, left_window)
        right = _target(right_monitor, right_window)

        # Then
        for target, orientation in ((left, "left"), (right, "right")):
            self.assertFalse(target.displayable)
            self.assertTrue(target.taskbar_visible)
            self.assertEqual(target.orientation, orientation)
            self.assertEqual(target.orientation_source, "work_area_reserved")
            self.assertEqual(target.orientation_confidence, "high")
            self.assertEqual(target.displayable_reason, "unsupported_side_taskbar")
            self.assertEqual(target.fallback_reason, "unsupported_side_taskbar")
            self.assertEqual(target.rca_class, "unsupported_orientation")

    def test_auto_hide_work_equals_monitor_is_not_displayable_with_no_reserved_edge(self):
        # Given
        monitor = _monitor(work=(0, 0, 1920, 1080))
        window = _window()

        # When
        target = _target(monitor, window)

        # Then
        self.assertFalse(target.displayable)
        self.assertTrue(target.taskbar_visible)
        self.assertEqual(target.orientation, "")
        self.assertEqual(target.orientation_source, "work_area_unreserved")
        self.assertEqual(target.orientation_confidence, "low")
        self.assertEqual(target.displayable_reason, "no_reserved_taskbar_edge")
        self.assertEqual(target.fallback_reason, "no_reserved_taskbar_edge")
        self.assertEqual(target.rca_class, "work_area_unreserved")

    def test_hidden_taskbar_window_is_not_displayable_with_hidden_reason(self):
        # Given
        monitor = _monitor()
        window = _window(visible=False)

        # When
        target = _target(monitor, window)

        # Then
        self.assertFalse(target.displayable)
        self.assertFalse(target.taskbar_visible)
        self.assertEqual(target.orientation, "bottom")
        self.assertEqual(target.orientation_source, "work_area_reserved")
        self.assertEqual(target.orientation_confidence, "high")
        self.assertEqual(target.displayable_reason, "taskbar_hidden")
        self.assertEqual(target.fallback_reason, "taskbar_hidden")
        self.assertEqual(target.rca_class, "taskbar_window_unavailable")

    def test_missing_matching_taskbar_is_not_displayable_with_missing_window_reason(self):
        # Given
        monitor = _monitor(is_primary=False)
        primary_window = _window()

        # When
        target = _target(monitor, primary_window)

        # Then
        self.assertFalse(target.displayable)
        self.assertFalse(target.taskbar_visible)
        self.assertEqual(target.orientation, "bottom")
        self.assertEqual(target.orientation_source, "work_area_reserved")
        self.assertEqual(target.orientation_confidence, "high")
        self.assertEqual(target.displayable_reason, "taskbar_window_missing")
        self.assertEqual(target.fallback_reason, "taskbar_window_missing")
        self.assertEqual(target.rca_class, "taskbar_window_unavailable")

    def test_negative_coordinate_secondary_keeps_physical_rects_and_stable_cache_key(self):
        # Given
        monitor = _monitor(
            handle=2,
            device=r"\\.\DISPLAY2",
            display_num=2,
            is_primary=False,
            monitor=(-1920, 0, 0, 1080),
            work=(-1920, 0, 0, 1040),
        )
        window = _window(
            hwnd=20,
            class_name="Shell_SecondaryTrayWnd",
            rect=(-1920, 1040, 0, 1080),
        )

        # When
        first = _target(monitor, window)
        second = _target(monitor, window)

        # Then
        self.assertTrue(first.displayable)
        self.assertEqual(first.monitor.monitor, (-1920, 0, 0, 1080))
        self.assertEqual(first.monitor.work, (-1920, 0, 0, 1040))
        self.assertEqual(first.taskbar_rect, (-1920, 1040, 0, 1080))
        self.assertEqual(first.taskbar_hwnd, 20)
        self.assertEqual(first.taskbar_class, "Shell_SecondaryTrayWnd")
        self.assertTrue(first.taskbar_visible)
        self.assertEqual(first.displayable_reason, "displayable")
        self.assertEqual(first.rca_class, "displayable_horizontal_taskbar")
        self.assertEqual(target_cache_key(first), target_cache_key(second))


if __name__ == "__main__":
    unittest.main()
