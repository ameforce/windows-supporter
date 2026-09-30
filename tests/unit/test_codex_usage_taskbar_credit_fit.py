"""Taskbar credit amount is never clipped (hotfix v0.35.1).

The credit segment draws "CR" and the amount inline. Its column width did
not depend on the amount, so a six-character balance such as "62,493" was
cut at the pane edge ("CR 62,49").
"""

import unittest
from unittest.mock import patch

import src.apps.codex_usage_taskbar_overlay as taskbar_overlay
from src.apps.codex_usage_taskbar_overlay import CodexUsageTaskbarOverlay


class _FakeFont:
    """Deterministic stand-in for a Tk font: digits 6px, separators 3px, others 7px."""

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = float(scale)

    def measure(self, text: str) -> int:
        width = 0
        for character in str(text or ""):
            if character.isdigit():
                width += 6
            elif character in ",.":
                width += 3
            else:
                width += 7
        return int(round(width * self.scale))


class _FakeRoot:
    def __init__(self):
        self.after_calls = []

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def after(self, delay_ms, callback):
        self.after_calls.append((int(delay_ms), callback))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, _after_id):
        return None

    def update_idletasks(self):
        return None


class _TextCanvas:
    def __init__(self):
        self.ops = []

    def create_text(self, *args, **kwargs):
        self.ops.append(("text", args, dict(kwargs)))
        return len(self.ops)

    def create_rectangle(self, *args, **kwargs):
        self.ops.append(("rectangle", args, dict(kwargs)))
        return len(self.ops)

    def texts(self):
        return [(op[1], op[2].get("text")) for op in self.ops if op[0] == "text"]


def _credit(value_text: str) -> dict:
    return {"key": "CR", "metric_key": "credit", "percent": None, "value_text": value_text}


def _weekly(percent: int, countdown: str) -> dict:
    return {
        "key": "7D",
        "metric_key": "weekly_limit",
        "percent": percent,
        "value_text": f"{percent}%",
        "reset_text": countdown,
        "reset_short_text": countdown.rsplit(" ", 1)[0],
    }


def _weekly_with_guidance(percent: int, countdown: str) -> dict:
    # Pace badge plus normal-pace guidance, as a live weekly column carries.
    # Guidance makes the preferred width wider than the compact one.
    return dict(
        _weekly(percent, countdown),
        reset_badge_label="정상",
        reset_badge_short_label="정",
        normal_guidance_text="N 64~66% / 4d 3h",
        normal_guidance_short_text="N 64~66%",
    )


class CreditFitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._context = patch.dict(
            taskbar_overlay._TK_MEASURE_CONTEXT,
            {"root": object(), "fonts": {pt: _FakeFont() for pt in (6, 7, 8)}},
        )
        self._context.start()
        taskbar_overlay._set_overlay_text_width_scale(1.0)
        taskbar_overlay._clear_overlay_width_caches()

    def tearDown(self) -> None:
        self._context.stop()
        taskbar_overlay._clear_overlay_width_caches()

    @staticmethod
    def _need(text: str, label_px: int = 14) -> int:
        # "CR" + gap (never closer than the historical 18px), amount, padding.
        return max(18, label_px + 3) + _FakeFont().measure(text) + 2

    def _credit_segment_widths(self, width: int, rows, profile_labels=None) -> list[int]:
        widths = []
        for layout in taskbar_overlay._metric_rows_layout_for_overlay_width(
            width, rows, profile_labels=profile_labels
        ):
            for index, metric in enumerate(layout.visible_metrics):
                if metric.get("metric_key") == "credit":
                    widths.append(int(layout.segment_geometry(index)[1]))
        return widths

    def test_credit_spellings_shorten_without_overstating_the_balance(self) -> None:
        cases = {
            "62,493": ("62,493", "62.4K", "62K"),
            "1,540": ("1,540", "1.5K", "1K"),
            "999,999": ("999,999", "999.9K", "999K"),
            "1,234,567": ("1,234,567", "1.2M", "1M"),
            "00,000": ("00,000", "00.0K", "00K"),
            "774": ("774",),
            "40.5": ("40.5",),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(taskbar_overlay._credit_value_spellings(value), expected)

    def test_shared_grid_gives_the_credit_column_room_for_the_whole_amount(self) -> None:
        rows = [
            (_weekly(7, "3d 18h 53m"), _credit("62,493")),
            (_weekly(0, "3d 11h 02m"),),
        ]
        for width in (360, 420, 480):
            with self.subTest(width=width):
                self.assertTrue(
                    all(
                        segment >= self._need("62,493")
                        for segment in self._credit_segment_widths(width, rows)
                    )
                )

    def test_preferred_pane_width_fits_the_whole_credit_amount(self) -> None:
        rows = [
            (_weekly(7, "3d 18h 53m"), _credit("62,493")),
            (_weekly(0, "3d 11h 02m"),),
        ]
        signature = tuple(
            tuple(taskbar_overlay._metric_width_signature(metric) for metric in row)
            for row in rows
        )
        labels = ("Codex 1", "Codex 2")
        preferred = taskbar_overlay._preferred_width_for_rows_cached(
            signature,
            labels,
            require_status_text=True,
            require_detail=True,
        )

        segments = self._credit_segment_widths(preferred, rows, profile_labels=labels)
        self.assertTrue(segments)
        self.assertTrue(all(segment >= self._need("62,493") for segment in segments))

    def test_credit_fit_check_requires_the_amount_to_fit(self) -> None:
        credit = _credit("62,493")

        self.assertFalse(taskbar_overlay._metric_fits_badge_mode(credit, 48, 0, "full"))
        self.assertTrue(
            taskbar_overlay._metric_fits_badge_mode(credit, self._need("62,493"), 0, "full")
        )
        self.assertTrue(taskbar_overlay._metric_fits_badge_mode(credit, self._need("62K"), 0, "short"))

    def test_cramped_segment_draws_a_shorter_spelling_instead_of_clipping(self) -> None:
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), lambda: {"enabled": True, "profiles": []})
        cases = (
            (self._need("62,493"), "62,493"),
            (self._need("62.4K"), "62.4K"),
            (self._need("62K"), "62K"),
            (20, "62K"),
        )
        for width, expected in cases:
            with self.subTest(width=width):
                canvas = _TextCanvas()
                overlay._draw_metric_segment(canvas, _credit("62,493"), 10, 2, width, 15)
                texts = canvas.texts()
                self.assertEqual(texts[0][1], "CR")
                self.assertEqual(texts[-1][1], expected)
                self.assertEqual(texts[-1][0][0], 10 + 18)

    def test_amount_starts_after_a_wider_label(self) -> None:
        # At a larger Tk scaling "CR" outgrows the historical 18px offset.
        wide = {pt: _FakeFont(scale=1.5) for pt in (6, 7, 8)}
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), lambda: {"enabled": True, "profiles": []})
        with patch.dict(taskbar_overlay._TK_MEASURE_CONTEXT, {"fonts": wide}):
            taskbar_overlay._clear_overlay_width_caches()
            canvas = _TextCanvas()
            overlay._draw_metric_segment(canvas, _credit("774"), 10, 2, 120, 15)
            taskbar_overlay._clear_overlay_width_caches()

        label_width = wide[7].measure("CR")
        self.assertGreaterEqual(canvas.texts()[-1][0][0], 10 + label_width + 3)

    def _drawn_credit_texts(self, overlay, width: int, rows, profile_labels=None):
        """(value_text, drawn text, text right edge, segment width, segment
        end in the pane) per credit segment."""
        drawn = []
        for layout in taskbar_overlay._metric_rows_layout_for_overlay_width(
            width, rows, profile_labels=profile_labels
        ):
            for index, metric in enumerate(layout.visible_metrics):
                if metric.get("metric_key") != "credit":
                    continue
                offset, segment, _progress = layout.segment_geometry(index)
                canvas = _TextCanvas()
                overlay._draw_metric_segment(canvas, metric, 0, 0, int(segment), 15)
                (x, _y), text = canvas.texts()[-1]
                right = x + taskbar_overlay._tk_measure_text(text, 7) + 2
                segment_end = int(layout.metrics_x) + int(offset) + int(segment)
                drawn.append((metric["value_text"], text, right, int(segment), segment_end))
        return drawn

    def test_from_the_compact_width_up_every_row_draws_its_whole_amount(self) -> None:
        # Rows share one credit column and the amount is not guidance: from
        # the compact width up, including the widths where the weekly
        # guidance is still being funded, every row's amount draws whole.
        rows = [
            (_weekly_with_guidance(7, "3d 18h 53m"), _credit("774")),
            (_weekly_with_guidance(0, "3d 11h 02m"), _credit("999,999")),
        ]
        signature = tuple(
            tuple(taskbar_overlay._metric_width_signature(metric) for metric in row)
            for row in rows
        )
        labels = ("Codex 8", "Codex 1")
        compact = taskbar_overlay._compact_preferred_width_for_rows_cached(signature, labels)
        preferred = taskbar_overlay._preferred_width_for_rows_cached(
            signature,
            labels,
            require_status_text=True,
            require_detail=True,
        )
        # The sweep must cross the band between the two widths.
        self.assertLess(compact, preferred)
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), lambda: {"enabled": True, "profiles": []})
        pane_padding = taskbar_overlay._OVERLAY_RIGHT_PADDING_PX
        for width in range(compact, preferred + 40, 2):
            for value_text, text, right, segment, segment_end in self._drawn_credit_texts(
                overlay, width, rows, profile_labels=labels
            ):
                with self.subTest(width=width, amount=value_text):
                    self.assertEqual(text, value_text)
                    self.assertLessEqual(right, segment)
                    # The column itself stays inside the pane and never takes
                    # more than the widest row's whole amount.
                    self.assertLessEqual(segment_end, width - pane_padding)
                    self.assertLessEqual(segment, self._need("999,999"))

    def test_short_amount_keeps_the_48px_credit_column(self) -> None:
        # A 3-digit amount needs less than 48px; with room to spare the column
        # keeps the width it always had.
        rows = [
            (_weekly(7, "3d 18h 53m"), _credit("774")),
            (_weekly(0, "3d 11h 02m"),),
        ]
        self.assertLess(self._need("774"), 48)
        for width in (360, 420, 480):
            with self.subTest(width=width):
                self.assertEqual(self._credit_segment_widths(width, rows), [48])

    def test_cramped_column_holds_every_rows_shortest_amount(self) -> None:
        # Below the middle band the credit column sits at its minimum, which
        # must hold the shortest spelling of every row's amount, not only the
        # first row's. At this scale "999K" outgrows the percent-column
        # minimum the credit column used to borrow.
        wide = {pt: _FakeFont(scale=1.5) for pt in (6, 7, 8)}
        rows = [
            (_weekly(7, "3d 18h 53m"), _credit("774")),
            (_weekly(0, "3d 11h 02m"), _credit("999,999")),
        ]
        overlay = CodexUsageTaskbarOverlay(_FakeRoot(), lambda: {"enabled": True, "profiles": []})
        with patch.dict(taskbar_overlay._TK_MEASURE_CONTEXT, {"fonts": wide}):
            taskbar_overlay._clear_overlay_width_caches()
            font = wide[7]
            offset = max(18, font.measure("CR") + 3)
            # Countdown minimum of the weekly column, and the credit minimum
            # the column owes: the percent-column floor or the widest row's
            # shortest spelling ("774" stays whole, "999,999" -> "999K").
            weekly_min = taskbar_overlay._metric_countdown_min_width(rows[0][0])
            credit_min = max(
                max(
                    14 + 3 + 3 + taskbar_overlay._value_column_width_for_text(value) + 2,
                    offset + font.measure(shortest) + 2,
                )
                for value, shortest in (("774", "774"), ("999,999", "999K"))
            )
            checked = shortened = 0
            for width in range(200, 460, 2):
                layout = taskbar_overlay._metric_rows_layout_for_overlay_width(width, rows)[0]
                if layout.metrics_width < weekly_min + credit_min + layout.segment_gap:
                    # Equal split: even the minimums do not fit (last resort).
                    continue
                checked += 1
                for value_text, text, right, segment, _end in self._drawn_credit_texts(
                    overlay, width, rows
                ):
                    with self.subTest(width=width, amount=value_text):
                        self.assertLessEqual(right, segment)
                    shortened += int(text != value_text)
            taskbar_overlay._clear_overlay_width_caches()

        self.assertGreater(checked, 0)
        # The sweep must reach widths where the amount has to shorten.
        self.assertGreater(shortened, 0)


if __name__ == "__main__":
    unittest.main()
