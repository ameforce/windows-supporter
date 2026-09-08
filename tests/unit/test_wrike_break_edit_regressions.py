"""Independent headless regressions for zero-row compatibility and break edits."""

import copy
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.apps.Monitor import Monitor
from src.apps.Wrike import Wrike
from src.apps.wrike_timelog_details import TimelogDetailRow
from src.apps.wrike_worktime_state import WorktimeStateStore
from tests.unit.test_qa_wrike_timelog_details import _QaTk, _QaText, _details
from tests.unit import test_wrike_realtime_progress as _realtime
from tests.unit.test_wrike_worktime_panel import _FakeRoot, _make_panel, _model


DAY = "2026-04-06"


def _day(**extra):
    result = {"manual_breaks": [], "active_break_started_at": None}
    result.update(extra)
    return result


def _source(*, version=3, zero=True):
    start = "2026-04-06T12:00:00"
    return {"state_version": version, "days": {
        DAY: _day(manual_breaks=[
            {"start": start, "end": start if zero else "2026-04-06T12:15:00"},
            {"start": "2026-04-06T15:00:00", "end": "2026-04-06T15:20:00"},
        ], plan={"target_net_minutes": 480, "clock_in": "09:00"}),
        "2026-04-07": _day(plan={"target_net_minutes": 450, "clock_in": "08:30"}),
        "2026-04-08": _day(active_break_started_at="2026-04-08T14:00:00"),
    }}


class BreakPersistenceRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def load(self, source=None):
        source = _source() if source is None else source
        self.path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        return WorktimeStateStore(self.path)

    def test_legacy_zero_rows_preserve_all_days_across_read_save_and_reopen(self):
        for version in (2, 3):
            with self.subTest(version=version):
                original = _source(version=version)
                store = self.load(original)
                expected = copy.deepcopy(original)
                expected["state_version"] = 3
                self.assertEqual(store.snapshot(), expected)
                self.assertEqual(store.update_day_plan("2026-04-09", 420, "10:00"), (True, None))
                reopened = WorktimeStateStore(self.path)
                for key, value in original["days"].items():
                    self.assertEqual(reopened.snapshot()["days"][key], value)
                self.assertEqual(set(reopened.snapshot()["days"]), {*original["days"], "2026-04-09"})

    def test_malformed_and_negative_legacy_rows_still_fail_closed_without_file_loss(self):
        bad_spans = (
            {"start": "not-a-time", "end": "2026-04-06T12:00:00"},
            {"start": "2026-04-06T12:00:00", "end": "2026-04-06T11:59:59"},
            {"start": "2026-04-05T12:00:00", "end": "2026-04-05T12:00:00"},
        )
        for span in bad_spans:
            with self.subTest(span=span):
                source = _source()
                source["days"][DAY]["manual_breaks"][0] = span
                store = self.load(source)
                original_bytes = self.path.read_bytes()
                ok, error = store.update_day_plan("2026-04-09", 420, "10:00")
                self.assertFalse(ok)
                self.assertTrue(error)
                self.assertEqual(self.path.read_bytes(), original_bytes)

    def test_same_now_start_and_stop_does_not_create_new_zero_interval(self):
        store = WorktimeStateStore(self.path)
        now = datetime(2026, 4, 6, 12, 0)
        self.assertTrue(store.toggle_manual_break(now)["ok"])
        self.assertTrue(store.toggle_manual_break(now)["ok"])
        self.assertEqual(store.snapshot()["days"][DAY]["manual_breaks"], [])
        self.assertIsNone(store.snapshot()["days"][DAY]["active_break_started_at"])
        self.assertEqual(WorktimeStateStore(self.path).snapshot(), store.snapshot())

    def test_fractional_second_toggle_never_persists_a_serialized_zero_interval(self):
        for elapsed, count in ((0.7, 0), (1.0, 1)):
            with self.subTest(elapsed_seconds=elapsed):
                source = {"state_version": 3, "days": {DAY: _day(plan={"target_net_minutes": 480, "clock_in": "09:00"})}}
                store = self.load(source)
                start = datetime(2026, 4, 6, 12, 0, 0, 100000)
                self.assertTrue(store.toggle_manual_break(start)["ok"])
                self.assertTrue(store.toggle_manual_break(start + timedelta(seconds=elapsed))["ok"])
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                spans = saved["days"][DAY]["manual_breaks"]
                self.assertEqual(len(spans), count)
                for span in spans:
                    self.assertLess(datetime.fromisoformat(span["start"]), datetime.fromisoformat(span["end"]))
                self.assertEqual(saved["days"][DAY]["plan"], source["days"][DAY]["plan"])
                self.assertEqual(WorktimeStateStore(self.path).snapshot(), saved)

    def test_zero_raw_row_is_editable_and_unrelated_days_plan_and_active_break_survive(self):
        original = _source()
        store = self.load(original)
        selected = store.get_completed_manual_breaks(DAY)
        self.assertEqual(selected["date"], DAY)
        self.assertEqual(len(selected["breaks"]), 2)
        self.assertEqual(selected["breaks"][0]["start"], selected["breaks"][0]["end"])
        ok, error = store.update_completed_manual_break(
            DAY, 0, "2026-04-06T12:01:00", "2026-04-06T12:31:00", selected["fingerprint"],
        )
        self.assertTrue(ok, error)
        expected = copy.deepcopy(original)
        expected["days"][DAY]["manual_breaks"][0] = {
            "start": "2026-04-06T12:01:00", "end": "2026-04-06T12:31:00",
        }
        self.assertEqual(store.snapshot(), expected)
        self.assertEqual(WorktimeStateStore(self.path).snapshot(), expected)

    def test_existing_midnight_ending_row_remains_editable_without_cross_day_spill(self):
        original = _source(zero=False)
        original["days"][DAY]["manual_breaks"][0] = {
            "start": "2026-04-06T23:30:00", "end": "2026-04-07T00:00:00",
        }
        store = self.load(original)
        selected = store.get_completed_manual_breaks(DAY)
        ok, error = store.update_completed_manual_break(
            DAY, 0, "2026-04-06T23:25:00", "2026-04-07T00:00:00", selected["fingerprint"],
        )
        self.assertTrue(ok, error)
        saved = WorktimeStateStore(self.path).snapshot()
        self.assertEqual(saved["days"][DAY]["manual_breaks"][0], {
            "start": "2026-04-06T23:25:00", "end": "2026-04-07T00:00:00",
        })
        self.assertEqual(saved["days"]["2026-04-07"], original["days"]["2026-04-07"])

    def test_successful_edit_changes_fingerprint_and_refuses_stale_second_editor(self):
        store = self.load(_source(zero=False))
        selected = store.get_completed_manual_breaks(DAY)
        self.assertEqual(store.update_completed_manual_break(
            DAY, 1, "2026-04-06T15:01:00", "2026-04-06T15:21:00", selected["fingerprint"],
        ), (True, None))
        current = store.snapshot()
        disk = self.path.read_bytes()
        self.assertNotEqual(store.get_completed_manual_breaks(DAY)["fingerprint"], selected["fingerprint"])
        ok, error = store.update_completed_manual_break(
            DAY, 0, "2026-04-06T12:01:00", "2026-04-06T12:31:00", selected["fingerprint"],
        )
        self.assertFalse(ok)
        self.assertTrue(error)
        self.assertEqual(store.snapshot(), current)
        self.assertEqual(self.path.read_bytes(), disk)

    def test_reordering_selected_day_rows_invalidates_index_fingerprint(self):
        store = self.load(_source(zero=False))
        selected = store.get_completed_manual_breaks(DAY)
        with store._lock:
            store._state["days"][DAY]["manual_breaks"].reverse()
        current = store.snapshot()
        with patch.object(store, "_save_locked") as save:
            ok, error = store.update_completed_manual_break(
                DAY, 0, "2026-04-06T12:01:00", "2026-04-06T12:31:00", selected["fingerprint"],
            )
        self.assertFalse(ok)
        self.assertTrue(error)
        self.assertEqual(store.snapshot(), current)
        save.assert_not_called()

    def test_unrelated_day_change_is_preserved_when_editing_selected_day(self):
        store = self.load(_source(zero=False))
        selected = store.get_completed_manual_breaks(DAY)
        self.assertEqual(store.update_day_plan("2026-04-07", 360, "10:30"), (True, None))
        other_day = store.snapshot()["days"]["2026-04-07"]
        ok, error = store.update_completed_manual_break(
            DAY, 0, "2026-04-06T12:01:00", "2026-04-06T12:31:00", selected["fingerprint"],
        )
        self.assertTrue(ok, error)
        self.assertEqual(WorktimeStateStore(self.path).snapshot()["days"]["2026-04-07"], other_day)

    def test_invalid_edit_intervals_and_identity_never_write(self):
        store = self.load(_source(zero=False))
        selected = store.get_completed_manual_breaks(DAY)
        original = store.snapshot()
        disk = self.path.read_bytes()
        cases = (
            (DAY, 0, "2026-04-06T12:00:00", "2026-04-06T12:00:00"),
            (DAY, 0, "2026-04-06T12:01:00", "2026-04-06T12:00:00"),
            (DAY, 0, "2026-04-06T23:50:00", "2026-04-07T00:10:00"),
            (DAY, 0, "2026-04-06T12:00:00+09:00", "2026-04-06T12:30:00+09:00"),
            (DAY, 0, "garbage", "2026-04-06T12:30:00"),
            (DAY, -1, "2026-04-06T12:00:00", "2026-04-06T12:30:00"),
            (DAY, 99, "2026-04-06T12:00:00", "2026-04-06T12:30:00"),
            (DAY, True, "2026-04-06T12:00:00", "2026-04-06T12:30:00"),
            ("2026-04-07", 0, "2026-04-06T12:00:00", "2026-04-06T12:30:00"),
        )
        for day, index, start, end in cases:
            with self.subTest(day=day, index=index, start=start, end=end), patch.object(store, "_save_locked") as save:
                ok, error = store.update_completed_manual_break(day, index, start, end, selected["fingerprint"])
                self.assertFalse(ok)
                self.assertTrue(error)
                save.assert_not_called()
        self.assertEqual(store.snapshot(), original)
        self.assertEqual(self.path.read_bytes(), disk)

    def test_atomic_replace_failure_rolls_back_memory_disk_and_edit_fingerprint(self):
        store = self.load(_source(zero=False))
        selected = store.get_completed_manual_breaks(DAY)
        original = store.snapshot()
        disk = self.path.read_bytes()
        with patch("src.apps.wrike_worktime_state.os.replace", side_effect=OSError("QA injected replace failure")):
            ok, error = store.update_completed_manual_break(
                DAY, 0, datetime(2026, 4, 6, 12, 1), datetime(2026, 4, 6, 12, 31), selected["fingerprint"],
            )
        self.assertFalse(ok)
        self.assertTrue(error)
        self.assertEqual(store.snapshot(), original)
        self.assertEqual(self.path.read_bytes(), disk)
        self.assertEqual(store.get_completed_manual_breaks(DAY), selected)
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ["state.json"])

    def test_completed_break_view_is_detached_and_does_not_offer_active_session(self):
        store = self.load(_source(zero=False))
        selected = store.get_completed_manual_breaks(DAY)
        selected["breaks"][0]["start"] = "not-a-date"
        self.assertNotEqual(store.get_completed_manual_breaks(DAY)["breaks"][0]["start"], "not-a-date")
        self.assertEqual(store.get_completed_manual_breaks("2026-04-08")["breaks"], [])


class BreakActivityAndHotkeyRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.dict("os.environ", {"APPDATA": self.tmp.name}):
            self.app = Wrike()
        self.enterContext(patch.object(self.app, "_Wrike__plan_for_date", return_value={"explicit": True, "clock_in": None, "target_net_minutes": 480}))
        self.enterContext(patch.object(self.app, "_Wrike__vacation_result_for_date", return_value={"automatic_prompt_allowed": True, "all_day": False}))
        self.enterContext(patch.object(self.app._Wrike__worktime_state_store, "get_activity_prompt", return_value=None))

    def test_repeated_activity_save_failure_notifies_once_and_success_resets_suppression(self):
        record = self.enterContext(patch.object(self.app._Wrike__worktime_state_store, "record_activity_prompt_pending", return_value=(False, "QA save failure")))
        error = self.enterContext(patch.object(self.app, "_Wrike__show_panel_action_error"))
        surface = self.enterContext(patch.object(self.app, "_Wrike__surface_activity_panel", return_value=True))
        for seconds in range(60):
            self.app._Wrike__on_worktime_activity(datetime(2026, 4, 6, 9, 0, seconds))
        self.assertEqual(error.call_count, 1)
        self.assertEqual(record.call_count, 1)
        surface.assert_not_called()
        self.app._Wrike__on_worktime_activity(datetime(2026, 4, 6, 9, 1, 1))
        self.assertEqual(record.call_count, 2)
        self.assertEqual(error.call_count, 1, "Retrying the same failure must not show another notification")
        record.return_value = (False, "QA different save failure")
        self.app._Wrike__on_worktime_activity(datetime(2026, 4, 6, 9, 2, 1))
        self.assertEqual(record.call_count, 3)
        self.assertEqual(error.call_count, 2, "A changed failure reason must be reported")
        self.assertIn("QA different save failure", error.call_args.args[0])
        record.return_value = (True, None)
        self.app._Wrike__on_worktime_activity(datetime(2026, 4, 6, 9, 3, 1))
        self.assertEqual(record.call_count, 4)
        surface.assert_called_once_with(date(2026, 4, 6))
        record.return_value = (False, "QA save failure")
        self.app._Wrike__on_worktime_activity(datetime(2026, 4, 6, 9, 3, 2))
        self.assertEqual(record.call_count, 5)
        self.assertEqual(error.call_count, 3)
        self.assertIn("QA save failure", error.call_args.args[0])

    def test_background_hotkey_registration_does_not_bind_ctrl_alt_b(self):
        monitor = Monitor.__new__(Monitor)
        keyboard = Mock()
        monitor._Monitor__background_enabled = True
        monitor._Monitor__lib = SimpleNamespace(keyboard=keyboard)
        monitor._Monitor__register_hotkeys()
        bindings = [call.args[0].replace(" ", "").lower() for call in keyboard.add_hotkey.call_args_list]
        self.assertNotIn("ctrl+alt+b", bindings)
        self.assertIn("ctrl+alt+w", bindings)


class BreakWrikeWiringRegressions(unittest.TestCase):
    def setUp(self):
        fixture = _realtime.WrikeRealtimeProgressIntegrationTest(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.app = fixture._new_wrike()
        _realtime._FrozenDateTime.current = datetime(2026, 4, 8, 18, 0)
        fixture._install_snapshot(self.app, fixture._fresh_snapshot(fetched_at=_realtime._FrozenDateTime.current))
        self.path = fixture.appdata / "qa-break-state.json"
        self.original = _source()
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.store = WorktimeStateStore(self.path)
        self.app._Wrike__worktime_state_store = self.store
        self.app._Wrike__lunch_break_enabled = False
        self.enterContext(patch.object(self.app, "_Wrike__request_timelog_snapshot_refresh", side_effect=AssertionError("Network refresh is outside this test")))
        self.enterContext(patch.object(self.app, "_Wrike__vacation_result_for_date", return_value={"all_day": False, "intervals": [], "event_count": 0, "automatic_prompt_allowed": True}))
        self.enterContext(patch.object(self.app, "_Wrike__ensure_ical_day_cache", side_effect=lambda day: [{
            "summary": "private calendar summary must not appear",
            "intervals": [(datetime(2026, 4, 7, 16), datetime(2026, 4, 7, 16, 30))],
        }] if day.date() == date(2026, 4, 7) else []))
        with patch("src.apps.Wrike.WorktimeQuickPanel", _realtime._FakePanel):
            self.panel = self.app._Wrike__ensure_worktime_panel(object())
        self.assertIsNotNone(self.panel)

    def test_actual_provider_exposes_legacy_zero_completed_and_readonly_calendar_rows(self):
        self.assertEqual(self.panel.model_provider, self.app._Wrike__build_worktime_panel_model)
        self.assertEqual(self.panel.callbacks["edit_manual_break"], self.app._Wrike__panel_edit_manual_break)
        model = self.panel.model_provider()
        rows = model.manual_breaks
        self.assertEqual(len(rows), 3)
        editable = [row for row in rows if row.editable]
        self.assertEqual([(row.date_key, row.index, row.start_time, row.end_time) for row in editable], [
            (DAY, 0, "12:00", "12:00"), (DAY, 1, "15:00", "15:20"),
        ])
        self.assertEqual({row.fingerprint for row in editable}, {self.store.get_completed_manual_breaks(DAY)["fingerprint"]})
        calendar = next(row for row in rows if not row.editable)
        self.assertEqual((calendar.date_key, calendar.label, calendar.start_time, calendar.end_time), ("2026-04-07", "캘린더", "16:00", "16:30"))
        self.assertNotIn("private calendar summary", repr(rows))
        self.assertEqual(self.store.snapshot(), self.original)

    def test_bound_editor_saves_selected_day_zero_and_midnight_with_reopen(self):
        edit = self.panel.callbacks["edit_manual_break"]
        initial = self.panel.model_provider().manual_breaks
        self.assertEqual(edit(initial[0], "12:10", "12:25"), (True, None))
        refreshed = self.panel.model_provider().manual_breaks
        self.assertEqual(edit(refreshed[1], "23:25", "24:00"), (True, None))
        expected = copy.deepcopy(self.original)
        expected["days"][DAY]["manual_breaks"] = [
            {"start": "2026-04-06T12:10:00", "end": "2026-04-06T12:25:00"},
            {"start": "2026-04-06T23:25:00", "end": "2026-04-07T00:00:00"},
        ]
        self.assertEqual(WorktimeStateStore(self.path).snapshot(), expected)
        self.assertEqual(self.store.snapshot(), expected)
        after = self.panel.model_provider().manual_breaks
        self.assertEqual((after[1].date_key, after[1].end_time), (DAY, "24:00"))
        saved = self.path.read_bytes()
        self.assertFalse(edit(initial[0], "12:15", "12:30")[0])
        self.assertEqual(self.path.read_bytes(), saved)

    def test_bound_editor_refuses_invalid_times_identity_and_readonly_rows_without_write(self):
        edit = self.panel.callbacks["edit_manual_break"]
        rows = self.panel.model_provider().manual_breaks
        manual = rows[0]
        readonly = next(row for row in rows if not row.editable)
        original_bytes = self.path.read_bytes()
        with self.assertRaises(ValueError):
            replace(manual, date_key="2026-4-6")
        cases = [
            (manual, "garbage", "12:30"), (manual, "24:00", "24:00"),
            (manual, "12:30", "12:30"), (manual, "12:31", "12:30"),
            (replace(manual, date_key="2026-04-07"), "12:00", "12:30"),
            (readonly, "16:05", "16:35"), (object(), "12:00", "12:30"),
        ]
        with patch.object(self.store, "_save_locked") as save:
            for row, start, end in cases:
                with self.subTest(row=row, start=start, end=end):
                    ok, error = edit(row, start, end)
                    self.assertFalse(ok)
                    self.assertTrue(error)
            save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(self.store.snapshot(), self.original)


class BreakPanelReadabilityRegressions(unittest.TestCase):
    def test_detail_refresh_keeps_readonly_content_scroll_and_passive_focus_contract(self):
        root, tk = _FakeRoot(), _QaTk()
        rows = tuple(TimelogDetailRow(DAY, f"L{i}", "T1", 15, f"긴 상세 내용 {i}", "Ticket title", "ready") for i in range(30))
        holder = {"model": replace(_model(), day_details=_details(rows=rows))}
        panel, _, _ = _make_panel(root, tk, holder)
        with patch("src.apps.wrike_worktime_panel._show_window_without_activation", side_effect=lambda window: window.native_show()), patch(
            "src.apps.wrike_worktime_panel._show_window_activated", side_effect=AssertionError("default show must remain passive"),
        ):
            self.assertTrue(panel.show())
            text = panel._widgets["detail_text"]
            text.yview_moveto(0.8)
            holder["model"] = replace(holder["model"], sync_text="갱신 완료")
            self.assertTrue(panel.refresh_now())
        self.assertEqual(text.yview()[0], 0.8)
        self.assertEqual(text.kwargs["state"], "disabled")
        self.assertIn("긴 상세 내용 29", text.get("1.0", "end-1c"))
        self.assertEqual(tk.toplevels[0].focus_force_calls, 0)


class _ReviewTaggedText(_QaText):
    """Observe Text insertions and tags without interpreting product row structure."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fragments = []
        self.tag_options = {}

    def tag_configure(self, tag, **kwargs):
        self.tag_options[tag] = kwargs

    def delete(self, *args):
        self.fragments.clear()
        return super().delete(*args)

    def insert(self, index, value, tag=None):
        if index not in ("1.0", "end"):
            raise AssertionError(index)
        if index == "1.0":
            self.entry_text = str(value)
        else:
            self.entry_text += str(value)
        self.fragments.append((str(value), tag))

    def tags_for(self, needle):
        return [tag for text, tag in self.fragments if needle in text]


class _ReviewTaggedTk(_QaTk):
    def Text(self, parent, **kwargs):
        widget = _ReviewTaggedText(self, parent, **kwargs)
        self.labels.append(widget)
        return widget


class ReviewFindingRegressions(unittest.TestCase):
    def wiring(self):
        fixture = BreakWrikeWiringRegressions(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def panel(self, model, edit=None):
        root, tk = _FakeRoot(), _ReviewTaggedTk()
        holder = {"model": model}
        panel, _, _ = _make_panel(root, tk, holder)
        panel._on_edit_manual_break = edit
        self.enterContext(patch("src.apps.wrike_worktime_panel._show_window_without_activation", side_effect=lambda window: window.native_show()))
        self.assertTrue(panel.show())
        return panel, holder

    def test_3946451098_calendar_cross_midnight_clips_both_selected_days(self):
        fixture = self.wiring()
        app = fixture.app
        app._Wrike__ical_parsed_events = [{
            "summary": "QA calendar break", "dtstart": datetime(2026, 4, 6, 23),
            "dtend": datetime(2026, 4, 7, 1), "all_day": False,
            "rrule": {}, "exdates": [],
        }]
        app._Wrike__ical_keywords = ["QA calendar break"]
        app._Wrike__ical_events_for_date = None
        app._Wrike__ical_matched = None
        # Restore the real calendar cache reader over the fixture's synthetic shortcut.
        with patch.object(app, "_Wrike__ensure_ical_day_cache", Wrike._Wrike__ensure_ical_day_cache.__get__(app, Wrike)):
            rows = fixture.panel.model_provider().manual_breaks
        actual = [(row.date_key, row.start_time, row.end_time) for row in rows if not row.editable]
        print("3946451098 calendar rows:", actual)
        self.assertEqual(actual, [(DAY, "23:00", "24:00"), ("2026-04-07", "00:00", "01:00")])

    def test_3946451101_multiline_comment_does_not_change_semantic_styles(self):
        rows = (
            TimelogDetailRow(DAY, "L1", "T1", 15, "comment first\ncomment second", "First heading", "ready"),
            TimelogDetailRow(DAY, "L2", "T2", 30, "last comment", "Second heading", "ready"),
        )
        panel, _ = self.panel(replace(_model(), day_details=_details(rows=rows)))
        widget = panel._widgets["detail_text"]
        actual = {text: widget.tags_for(text) for text in ("First heading", "Second heading", "comment first", "comment second", "last comment")}
        print("3946451101 semantic styles:", actual)
        self.assertEqual(actual, {
            "First heading": ["detail_heading"], "Second heading": ["detail_heading"],
            "comment first": ["detail_comment"], "comment second": ["detail_comment"],
            "last comment": ["detail_comment"],
        })

    def test_3946451105_other_date_cannot_silently_save_hidden_day(self):
        fixture = self.wiring()
        model = fixture.panel.model_provider()
        panel, _ = self.panel(model, fixture.panel.callbacks["edit_manual_break"])
        panel._select_row_command(0)
        panel._edit_manual_break_command(model.manual_breaks[0])
        entry = panel._widgets["inline_entry"]
        entry.delete(0, "end")
        entry.insert(0, "12:05 - 12:25")
        panel._select_row_command(1)
        active = panel._inline_editor_active
        title = panel._widgets["inline_title"].kwargs.get("text", "")
        before = fixture.path.read_bytes()
        panel._save_inline_editor_command()
        changed = fixture.path.read_bytes() != before
        print("3946451105 selection/editor/save:", panel._selected_date_key, active, title, "Monday changed", changed)
        clearly_bound = DAY in title or "04/06" in title or "4월 6일" in title
        self.assertTrue(not active or clearly_bound, "An editor left active on another selected day must visibly name its bound date")
        if not active:
            self.assertFalse(changed)

    def test_3946451109_retry_retains_first_activity_in_persisted_prompt(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict("os.environ", {"APPDATA": temp.name}):
            app = Wrike()
        store = WorktimeStateStore(Path(temp.name) / "qa-state.json")
        app._Wrike__worktime_state_store = store
        self.enterContext(patch.object(app, "_Wrike__plan_for_date", return_value={"explicit": True, "clock_in": None, "target_net_minutes": 480}))
        self.enterContext(patch.object(app, "_Wrike__vacation_result_for_date", return_value={"automatic_prompt_allowed": True, "all_day": False}))
        self.enterContext(patch.object(app, "_Wrike__show_panel_action_error"))
        surface = self.enterContext(patch.object(app, "_Wrike__surface_activity_panel", return_value=True))
        first, retry = datetime(2026, 4, 6, 8, 5, 50), datetime(2026, 4, 6, 8, 6, 50)
        with patch("src.apps.wrike_worktime_state.os.replace", side_effect=OSError("QA injected transient save failure")):
            app._Wrike__on_worktime_activity(first)
        self.assertIsNone(store.get_activity_prompt(DAY))
        surface.assert_not_called()
        app._Wrike__on_worktime_activity(retry)
        persisted = WorktimeStateStore(Path(temp.name) / "qa-state.json").get_activity_prompt(DAY)
        print("3946451109 persisted prompt:", persisted)
        self.assertEqual(persisted["detected_at"], first.isoformat())
        surface.assert_called_once_with(date(2026, 4, 6))

    def test_3946451112_view_switch_then_same_date_refresh_keeps_scroll(self):
        fixture = self.wiring()
        panel, holder = self.panel(fixture.panel.model_provider())
        panel._select_row_command(0)
        panel._set_detail_view("breaks")
        widget = panel._widgets["detail_text"]
        widget.yview_moveto(0.8)
        holder["model"] = replace(holder["model"], sync_text="QA refreshed")
        self.assertTrue(panel.refresh_now())
        print("3946451112 scroll after same-date refresh:", widget.yview()[0])
        self.assertEqual(widget.yview()[0], 0.8)

    def test_same_date_append_preserves_draft_and_stale_save_is_refused(self):
        fixture = self.wiring()
        # This fixture also has an unrelated Wednesday active break. Remove it
        # from this synthetic setup so Monday's public toggle can append a row.
        fixture.original["days"]["2026-04-08"]["active_break_started_at"] = None
        fixture.path.write_text(json.dumps(fixture.original), encoding="utf-8")
        fixture.store = WorktimeStateStore(fixture.path)
        fixture.app._Wrike__worktime_state_store = fixture.store
        model = fixture.panel.model_provider()
        panel, holder = self.panel(model, fixture.panel.callbacks["edit_manual_break"])
        panel._select_row_command(0)
        panel._edit_manual_break_command(model.manual_breaks[0])
        original_identity = panel._manual_break_edit
        entry = panel._widgets["inline_entry"]
        entry.delete(0, "end")
        entry.insert(0, "12:05 - 12:25")
        self.assertTrue(fixture.store.toggle_manual_break(datetime(2026, 4, 6, 17))["ok"])
        self.assertTrue(fixture.store.toggle_manual_break(datetime(2026, 4, 6, 17, 15))["ok"])
        holder["model"] = fixture.panel.model_provider()
        self.assertTrue(panel.refresh_now())
        self.assertTrue(panel._inline_editor_active)
        self.assertEqual(entry.get(), "12:05 - 12:25")
        self.assertEqual(panel._manual_break_edit, original_identity)
        before = fixture.path.read_bytes()
        panel._save_inline_editor_command()
        self.assertEqual(fixture.path.read_bytes(), before)
        self.assertTrue(panel._inline_editor_active)
        self.assertTrue(panel._widgets["inline_error"].kwargs.get("text"))

    def test_pre_break_001_reselect_same_row_keeps_draft_and_fingerprint_together(self):
        fixture = self.wiring()
        model = fixture.panel.model_provider()
        results = []
        real_edit = fixture.panel.callbacks["edit_manual_break"]

        def edit(*args):
            result = real_edit(*args)
            results.append(result)
            return result

        panel, holder = self.panel(model, edit)
        panel._select_row_command(0)
        original_row = model.manual_breaks[0]
        panel._edit_manual_break_command(original_row)
        entry = panel._widgets["inline_entry"]
        draft = "12:05 - 12:35"
        entry.delete(0, "end")
        entry.insert(0, draft)
        # Model a second editor through the real public store API and real disk.
        self.assertEqual(fixture.store.update_completed_manual_break(
            DAY, original_row.index, "2026-04-06T12:10:00", "2026-04-06T12:40:00",
            original_row.fingerprint,
        ), (True, None))
        holder["model"] = fixture.panel.model_provider()
        self.assertTrue(panel.refresh_now())
        latest_row = holder["model"].manual_breaks[0]
        self.assertNotEqual(latest_row.fingerprint, original_row.fingerprint)
        latest_bytes = fixture.path.read_bytes()
        panel._save_inline_editor_command()
        self.assertFalse(results[-1][0])
        self.assertEqual(fixture.path.read_bytes(), latest_bytes)
        panel._edit_manual_break_command(latest_row)
        retained_input = entry.get()
        retained_fingerprint = panel._manual_break_edit.fingerprint
        preserved = retained_input == draft and retained_fingerprint == original_row.fingerprint
        restarted = retained_input == "12:10 - 12:40" and retained_fingerprint == latest_row.fingerprint
        panel._save_inline_editor_command()
        final_span = WorktimeStateStore(fixture.path).get_completed_manual_breaks(DAY)["breaks"][0]
        print("PRE-BREAK-001 reselect:", {
            "first_save": results[0][0], "input": retained_input,
            "fingerprint_rebound": retained_fingerprint == latest_row.fingerprint,
            "second_save": results[-1][0], "final_span": final_span,
        })
        self.assertTrue(preserved or restarted, "Draft text and original fingerprint must stay together unless both restart from the latest row")
        self.assertEqual(final_span["start"], "2026-04-06T12:10:00")
        self.assertEqual(final_span["end"], "2026-04-06T12:40:00")


class ActivityRetryBoundaryRegressions(unittest.TestCase):
    """Persisted observations across retry and user-intent boundaries."""

    def make_app(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict("os.environ", {"APPDATA": temp.name}):
            app = Wrike()
        path = Path(temp.name) / "qa-retry-state.json"
        store = WorktimeStateStore(path)
        app._Wrike__worktime_state_store = store
        self.assertEqual(store.update_day_plan(DAY, 480, None), (True, None))
        self.enterContext(patch.object(app, "_Wrike__vacation_result_for_date", return_value={"automatic_prompt_allowed": True, "all_day": False}))
        self.enterContext(patch.object(app, "_Wrike__show_panel_action_error"))
        surface = self.enterContext(patch.object(app, "_Wrike__surface_activity_panel", return_value=True))
        return app, store, path, surface

    def fail_activity_write(self, app, detected):
        with patch("src.apps.wrike_worktime_state.os.replace", side_effect=OSError("QA injected transient write failure")):
            app._Wrike__on_worktime_activity(detected)

    def test_next_day_discards_failed_previous_day_activity(self):
        app, store, path, surface = self.make_app()
        self.fail_activity_write(app, datetime(2026, 4, 6, 23, 59, 50))
        self.assertIsNone(store.get_activity_prompt(DAY))
        next_activity = datetime(2026, 4, 7, 8, 5, 50)
        app._Wrike__on_worktime_activity(next_activity)
        reopened = WorktimeStateStore(path)
        self.assertIsNone(reopened.get_activity_prompt(DAY))
        self.assertEqual(reopened.get_activity_prompt("2026-04-07")["detected_at"], next_activity.isoformat())
        surface.assert_called_once_with(next_activity.date())

    def test_intentional_clock_in_cancels_retry_before_new_eligible_activity(self):
        app, store, path, surface = self.make_app()
        self.fail_activity_write(app, datetime(2026, 4, 6, 8, 5, 50))
        self.assertEqual(store.update_day_plan(DAY, 480, "08:00"), (True, None))
        app._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 6))
        self.assertIsNone(store.get_activity_prompt(DAY))
        surface.assert_not_called()
        self.assertEqual(store.update_day_plan(DAY, 480, None), (True, None))
        next_activity = datetime(2026, 4, 6, 8, 6, 10)
        app._Wrike__on_worktime_activity(next_activity)
        self.assertEqual(WorktimeStateStore(path).get_activity_prompt(DAY)["detected_at"], next_activity.isoformat())

    def test_observed_pending_skipped_or_snoozed_prompt_cancels_previous_retry(self):
        for status in ("pending", "skipped", "snoozed"):
            with self.subTest(status=status):
                app, store, path, surface = self.make_app()
                self.fail_activity_write(app, datetime(2026, 4, 6, 8, 5, 50))
                confirmed = datetime(2026, 4, 6, 8, 5, 55)
                self.assertEqual(store.record_activity_prompt_pending(DAY, confirmed), (True, None))
                if status == "skipped":
                    self.assertEqual(store.skip_activity_prompt(DAY), (True, None))
                elif status == "snoozed":
                    self.assertEqual(store.snooze_activity_prompt(DAY, datetime(2026, 4, 6, 8, 30)), (True, None))
                before = path.read_bytes()
                app._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 6))
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(surface.call_count, 1 if status == "pending" else 0)
                self.assertEqual(store.clear_activity_prompt(DAY), (True, None))
                next_activity = datetime(2026, 4, 6, 8, 6, 10)
                app._Wrike__on_worktime_activity(next_activity)
                self.assertEqual(WorktimeStateStore(path).get_activity_prompt(DAY)["detected_at"], next_activity.isoformat())

    def test_retry_after_snooze_expiry_preserves_first_post_snooze_activity(self):
        app, store, path, surface = self.make_app()
        self.assertEqual(store.record_activity_prompt_pending(DAY, datetime(2026, 4, 6, 8)), (True, None))
        self.assertEqual(store.snooze_activity_prompt(DAY, datetime(2026, 4, 6, 8, 30)), (True, None))
        first = datetime(2026, 4, 6, 8, 30, 50)
        self.fail_activity_write(app, first)
        self.assertEqual(store.get_activity_prompt(DAY)["status"], "snoozed")
        surface.assert_not_called()
        app._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 31, 50))
        self.assertEqual(WorktimeStateStore(path).get_activity_prompt(DAY), {"status": "pending", "detected_at": first.isoformat()})
        surface.assert_called_once_with(first.date())


if __name__ == "__main__":
    unittest.main()
