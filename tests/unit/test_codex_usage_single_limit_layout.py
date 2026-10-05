"""Single-limit profiles share a column without losing metric identity."""

import unittest
from unittest.mock import patch

from src.apps import codex_usage_taskbar_overlay as overlay
from tests.unit.test_codex_usage_taskbar_credit_fit import _FakeFont, _credit, _weekly


def _limit(key):
    metric = _weekly(89, "04d 17h 49m 29s")
    metric.update(metric_key=key, key={
        "five_hour_limit": "5H", "weekly_limit": "7D", "monthly_limit": "1M",
    }[key])
    if key == "five_hour_limit":
        metric.update(reset_text="02h 31m 04s", reset_short_text="02h 31m 04s")
    elif key == "monthly_limit":
        metric.update(reset_text="29d 21h 59m 00s", reset_short_text="29d 21h 59m 00s")
    return metric


class SingleLimitLayoutTest(unittest.TestCase):
    def setUp(self):
        self.fonts = patch.dict(overlay._TK_MEASURE_CONTEXT, {
            "root": object(), "fonts": {pt: _FakeFont() for pt in (6, 7, 8)},
        })
        self.fonts.start()
        self.addCleanup(self.fonts.stop)
        self.addCleanup(overlay._clear_overlay_width_caches)
        overlay._set_overlay_text_width_scale(1.0)
        overlay._clear_overlay_width_caches()

    def test_different_single_limits_share_start_width_and_credit_column(self):
        for keys in (("weekly_limit", "monthly_limit"),
                     ("five_hour_limit", "weekly_limit"),
                     ("monthly_limit", "five_hour_limit")):
            rows = [(_limit(key), _credit("62,499")) for key in keys]
            for width in (360, 480, 720, 960):
                with self.subTest(keys=keys, width=width):
                    layouts = overlay._metric_rows_layout_for_overlay_width(width, rows)
                    self.assertEqual(layouts[0].segment_geometry(0)[0], 0)
                    self.assertEqual(layouts[1].segment_geometry(0)[0], 0)
                    self.assertEqual(layouts[0].segment_geometry(0), layouts[1].segment_geometry(0))
                    self.assertEqual(layouts[0].segment_geometry(1), layouts[1].segment_geometry(1))
                    offset, size, _ = layouts[0].segment_geometry(0)
                    self.assertEqual(layouts[0].segment_geometry(1)[0], offset + size + layouts[0].segment_gap)
                    self.assertEqual(layouts[0].visible_metrics, rows[0])
                    self.assertEqual(layouts[1].visible_metrics, rows[1])

    def test_single_limit_layout_is_independent_of_row_order(self):
        rows = [(_limit("five_hour_limit"), _credit("774")),
                (_limit("monthly_limit"), _credit("62,499"))]
        for width in range(300, 801, 10):
            forward = overlay._metric_rows_layout_for_overlay_width(width, rows)
            reverse = overlay._metric_rows_layout_for_overlay_width(width, rows[::-1])
            for original, reordered in zip(forward, reversed(reverse)):
                self.assertEqual(original.segment_geometry(0), reordered.segment_geometry(0))
                self.assertEqual(original.segment_geometry(1), reordered.segment_geometry(1))

    def test_mixed_periods_share_the_widest_percentage_in_their_column(self):
        weekly, monthly = _limit("weekly_limit"), _limit("monthly_limit")
        weekly.update(percent=9, value_text="9%")
        monthly.update(percent=100, value_text="100%")
        layouts = overlay._metric_rows_layout_for_overlay_width(720, [(weekly,), (monthly,)])
        values = overlay._slot_value_widths(layouts)
        self.assertEqual(values["weekly_limit"], values["monthly_limit"])
        self.assertEqual(values["monthly_limit"], overlay._value_column_width_for_text("100%"))

    def test_empty_and_credit_only_rows_do_not_reserve_another_limit(self):
        rows = [(), (_credit("62,499"),), (_limit("weekly_limit"),),
                (_limit("monthly_limit"), _credit("62,462"))]
        layouts = overlay._metric_rows_layout_for_overlay_width(720, rows)
        self.assertEqual(layouts[2].segment_geometry(0), layouts[3].segment_geometry(0))
        self.assertEqual(layouts[3].segment_geometry(0)[0], 0)
        self.assertEqual(layouts[1].segment_geometry(0), layouts[3].segment_geometry(1))

    def test_a_second_limit_restores_metric_columns_and_switching_back_compacts(self):
        weekly, monthly, hourly = (_limit(key) for key in
                                   ("weekly_limit", "monthly_limit", "five_hour_limit"))
        rows = [(weekly,), (monthly,)]
        compact = overlay._metric_rows_layout_for_overlay_width(720, rows)
        expanded = overlay._metric_rows_layout_for_overlay_width(720, [(weekly,), (hourly, weekly)])
        self.assertGreater(expanded[0].segment_geometry(0)[0], 0)
        self.assertEqual(expanded[0].segment_geometry(0), expanded[1].segment_geometry(1))
        restored = overlay._metric_rows_layout_for_overlay_width(720, rows)
        self.assertEqual(compact, restored)
        self.assertEqual(restored[1].segment_geometry(0)[0], 0)

    def test_unrelated_metrics_keep_their_own_columns(self):
        other = {"key": "SP", "metric_key": "spend", "value_text": "$8", "percent": None}
        layouts = overlay._metric_rows_layout_for_overlay_width(720, [(_limit("weekly_limit"),), (other,)])
        self.assertNotEqual(layouts[0].segment_geometry(0)[0], layouts[1].segment_geometry(0)[0])


if __name__ == "__main__":
    unittest.main()
