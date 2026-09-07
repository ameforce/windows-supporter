"""Independent headless acceptance checks for date drill-down and passive refresh.

No Tk interpreter, live Wrike request, or user configuration is used here.
Native foreground ownership remains a separate coordinated acceptance probe.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.apps.wrike_timelog_details import TimelogDayDetails, TimelogDetailRow
from src.apps.Wrike import Wrike
from src.apps.wrike_timelog_snapshot import (
    TimelogDay, make_fresh_snapshot, make_loading_snapshot, make_error_snapshot,
)
from tests.unit.test_wrike_worktime_panel import (
    _FakeRoot, _FakeTk, _FakeWidget, _make_panel, _model,
)


class _QaText(_FakeWidget):
    """Minimal Text contract; Tk Text indexes differ from Entry indexes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.first_visible = 0.0
        self.text_delete_calls = 0

    def delete(self, start, end=None):
        self.text_delete_calls += 1
        self.first_visible = 0.0
        return super().delete(start, end)

    def insert(self, index, value):
        if index != "1.0":
            raise AssertionError(f"unexpected Text insertion index: {index}")
        self.entry_text = str(value)

    def get(self, *args):
        return self.entry_text

    def yview(self, *args):
        return (self.first_visible, min(1.0, self.first_visible + 0.1))

    def yview_moveto(self, first):
        self.first_visible = float(first)


class _QaScrollbar(_FakeWidget):
    def set(self, *args):
        return None


class _QaTk(_FakeTk):
    def Text(self, parent, **kwargs):
        widget = _QaText(self, parent, **kwargs)
        self.labels.append(widget)
        return widget

    def Scrollbar(self, parent, **kwargs):
        widget = _QaScrollbar(self, parent, **kwargs)
        self.frames.append(widget)
        return widget


def _details(*, start=date(2026, 4, 6), state="available", rows=()):
    return tuple(
        TimelogDayDetails(
            (start + timedelta(days=index)).isoformat(), state,
            sum(row.minutes for row in rows if row.date_key == (start + timedelta(days=index)).isoformat()),
            tuple(row for row in rows if row.date_key == (start + timedelta(days=index)).isoformat()),
        )
        for index in range(7)
    )


class IndependentDatePanelAcceptance(unittest.TestCase):
    def setUp(self):
        self.passive = self.enterContext(patch(
            "src.apps.wrike_worktime_panel._show_window_without_activation",
            side_effect=lambda window: window.native_show(),
        ))
        self.active = self.enterContext(patch(
            "src.apps.wrike_worktime_panel._show_window_activated",
            side_effect=lambda window: window.native_show(),
        ))
        self.enterContext(patch(
            "src.apps.wrike_worktime_panel._window_is_foreground", return_value=False,
        ))
        self.root = _FakeRoot()
        self.tk = _QaTk()

    def make(self, details):
        self.holder = {"model": replace(_model(), day_details=details)}
        self.panel, _, self.callbacks = _make_panel(self.root, self.tk, self.holder)
        self.assertTrue(self.panel.show(activate=False))
        return self.panel

    def detail_text(self):
        return self.panel._widgets["detail_text"].get("1.0", "end-1c")

    def select(self, index):
        self.panel._widgets["rows"][index][1].bindings["<Button-1>"](SimpleNamespace())

    def test_selecting_date_keeps_each_log_and_excludes_other_dates(self):
        rows = (
            TimelogDetailRow("2026-04-06", "L1", "T1", 15, "first action", "Shared ticket", "ready"),
            TimelogDetailRow("2026-04-06", "L2", "T1", 45, "second action", "Shared ticket", "ready"),
            TimelogDetailRow("2026-04-07", "L3", "T2", 30, "Tuesday only", "Other ticket", "ready"),
        )
        self.make(_details(rows=rows))
        self.assertEqual(self.detail_text().count("Shared ticket"), 2)
        self.assertIn("first action", self.detail_text())
        self.assertIn("second action", self.detail_text())
        self.assertNotIn("Other ticket", self.detail_text())
        self.select(1)
        self.assertIn("Other ticket", self.detail_text())
        self.assertIn("Tuesday only", self.detail_text())
        self.assertNotIn("Shared ticket", self.detail_text())
        self.assertEqual(self.panel._selected_date_key, "2026-04-07")

    def test_loading_and_unavailable_never_claim_empty(self):
        self.make(_details(state="loading"))
        seen = []
        for state in ("loading", "unavailable", "available"):
            self.holder["model"] = replace(_model(), day_details=_details(state=state))
            self.assertTrue(self.panel.refresh_now())
            seen.append(self.detail_text())
            if state != "available":
                self.assertNotIn("합계 0분", self.detail_text())
                self.assertNotIn("기록이 없습니다", self.detail_text())
        self.assertEqual(len(set(seen)), 3)
        self.assertIn("기록이 없습니다", seen[-1])

    def test_title_completion_preserves_selected_date_and_unsaved_editor(self):
        row = TimelogDetailRow("2026-04-07", "L1", "T1", 45, "pending title")
        self.make(_details(rows=(row,)))
        self.select(1)
        self.tk.button("목표 수정").invoke()
        entry = self.panel._widgets["inline_entry"]
        entry.delete(0, "end")
        entry.insert(0, "07:37")
        focus_calls = entry.focus_set_calls
        detail_widget = self.panel._widgets["detail_text"]
        self.holder["model"] = replace(
            _model(sync_text="갱신 완료"),
            day_details=_details(rows=(replace(row, task_title="Resolved ticket", title_state="ready"),)),
        )
        self.assertTrue(self.panel.refresh_now())
        self.assertEqual(self.panel._selected_date_key, "2026-04-07")
        self.assertIs(self.panel._widgets["inline_entry"], entry)
        self.assertEqual(entry.get(), "07:37")
        self.assertEqual(entry.focus_set_calls, focus_calls)
        self.assertTrue(self.panel._inline_editor_active)
        self.assertIs(self.panel._widgets["detail_text"], detail_widget)
        self.assertIn("Resolved ticket", self.detail_text())
        self.active.assert_not_called()

    def test_week_rollover_cannot_display_previous_week_details_or_editor(self):
        row = TimelogDetailRow("2026-04-07", "L1", "T1", 45, "OLD PRIVATE COMMENT")
        self.make(_details(rows=(row,)))
        self.select(1)
        self.tk.button("목표 수정").invoke()
        self.holder["model"] = replace(
            _model(week_start=date(2026, 4, 13)),
            day_details=_details(start=date(2026, 4, 13), state="loading"),
        )
        self.assertTrue(self.panel.refresh_now())
        self.assertEqual(self.panel._selected_date_key, "2026-04-13")
        self.assertFalse(self.panel._inline_editor_active)
        self.assertNotIn("OLD PRIVATE COMMENT", self.detail_text())
        self.assertNotIn("기록이 없습니다", self.detail_text())

    def test_missing_title_preserves_minutes_and_comment_with_distinct_fallback(self):
        rows = tuple(
            TimelogDetailRow("2026-04-06", f"L{index}", "T" if index else "", 15, f"comment-{state}", title_state=state)
            for index, state in enumerate(("missing", "loading", "unavailable"))
        )
        self.make(_details(rows=rows))
        for row in rows:
            self.assertIn(row.comment, self.detail_text())
            self.assertIn(row.ticket_text, self.detail_text())
        self.assertNotIn("기록이 없습니다", self.detail_text())

    def test_default_show_reopen_toggle_and_refresh_use_passive_path(self):
        self.make(_details())
        self.panel.hide()
        self.assertTrue(self.panel.show())
        self.panel.hide()
        self.panel.toggle()
        self.assertTrue(self.panel.is_visible())
        self.holder["model"] = replace(self.holder["model"], sync_text="갱신 완료")
        self.assertTrue(self.panel.refresh_now())
        self.active.assert_not_called()
        window = self.tk.toplevels[0]
        self.assertEqual(window.focus_force_calls, 0)
        self.assertEqual(window.deiconify_calls, 0)
        self.assertEqual(window.lift_calls, 0)

    def test_sync_only_refresh_preserves_detail_scroll_without_replacing_text(self):
        rows = tuple(
            TimelogDetailRow("2026-04-06", f"L{i}", "T1", 15, f"Action {i}")
            for i in range(30)
        )
        self.make(_details(rows=rows))
        text = self.panel._widgets["detail_text"]
        text.yview_moveto(0.8)
        deletions = text.text_delete_calls
        self.holder["model"] = replace(self.holder["model"], sync_text="동기화 2초 전")
        self.assertTrue(self.panel.refresh_now())
        self.assertEqual(text.yview()[0], 0.8)
        self.assertEqual(text.text_delete_calls, deletions)
        self.assertIs(self.panel._widgets["detail_text"], text)

    def test_same_date_title_update_preserves_detail_scroll(self):
        rows = tuple(
            TimelogDetailRow("2026-04-06", f"L{i}", "T1", 15, f"Action {i}")
            for i in range(30)
        )
        self.make(_details(rows=rows))
        text = self.panel._widgets["detail_text"]
        text.yview_moveto(0.8)
        self.holder["model"] = replace(
            self.holder["model"], day_details=_details(rows=tuple(
                replace(row, task_title="Resolved ticket", title_state="ready") for row in rows
            )),
        )
        self.assertTrue(self.panel.refresh_now())
        self.assertEqual(text.yview()[0], 0.8)
        self.assertIn("Resolved ticket", self.detail_text())

    def test_date_switch_resets_scroll_even_if_rendered_contents_are_identical(self):
        rows = tuple(
            TimelogDetailRow(day, f"{day}-L{i}", "T1", 15, f"Action {i}")
            for day in ("2026-04-06", "2026-04-07") for i in range(30)
        )
        self.make(_details(rows=rows))
        text = self.panel._widgets["detail_text"]
        first_text = self.detail_text()
        text.yview_moveto(0.8)
        self.select(1)
        self.assertEqual(self.detail_text(), first_text)
        self.assertEqual(text.yview()[0], 0.0)
        self.assertEqual(self.panel._selected_date_key, "2026-04-07")


class IndependentDetailWorkerAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.dict("os.environ", {"APPDATA": self.tmp.name}))
        self.app = Wrike()
        self.app._Wrike__wrike_api_token_session = "QA-SYNTHETIC-TOKEN-A"
        self.fingerprint = self.app._Wrike__timelog_token_fingerprint("QA-SYNTHETIC-TOKEN-A")
        self.week = [datetime(2026, 4, 6) + timedelta(days=index) for index in range(7)]
        self.enterContext(patch.object(self.app, "_Wrike__update_monitor_from_snapshot"))

    def rows(self, comment="PRIVATE-DETAIL-CANARY"):
        return [
            {"id": "L1", "taskId": "T1", "trackedDate": "2026-04-06", "minutes": 15, "comment": comment},
            {"id": "L2", "taskId": "T1", "trackedDate": "2026-04-06", "minutes": 45, "comment": "Second action"},
            {"id": "L3", "taskId": "T2", "trackedDate": "2026-04-07", "minutes": 30, "comment": "Tuesday"},
        ]

    def publish(self, rows=None, *, generation=1):
        details = self.app._Wrike__build_timelog_day_details(self.rows() if rows is None else rows, self.week)
        snapshot = make_fresh_snapshot(
            days=tuple(TimelogDay(day.date(), detail.total_minutes) for day, detail in zip(self.week, details)),
            display_name="QA Synthetic", fetched_at=self.week[0], generation=generation,
        )
        self.app._Wrike__timelog_refresh_generation = generation
        with patch.object(self.app, "_Wrike__start_timelog_title_lookup") as start:
            self.assertTrue(self.app._Wrike__apply_timelog_snapshot_result(
                generation, snapshot=snapshot, day_details=details, account_fingerprint=self.fingerprint,
            ))
        return snapshot, details, start

    def test_authoritative_publish_deduplicates_title_ids_but_keeps_timelog_rows(self):
        snapshot, details, start = self.publish()
        self.assertEqual([row.timelog_id for row in details[0].rows], ["L1", "L2"])
        self.assertEqual(details[0].total_minutes, 60)
        self.assertEqual(details[1].total_minutes, 30)
        start.assert_called_once_with(1, self.fingerprint, ("T1", "T2"))
        self.assertTrue(self.app._Wrike__detail_days_match_snapshot(details, snapshot))

    def test_successful_refresh_replaces_deleted_and_moved_logs(self):
        self.publish()
        rows = [{"id": "L3", "taskId": "T2", "trackedDate": "2026-04-08", "minutes": 75, "comment": "MOVED"}]
        snapshot, _, _ = self.publish(rows, generation=2)
        details = self.app._Wrike__panel_day_details(snapshot, [day.date() for day in self.week])
        self.assertEqual(details[0].rows, ())
        self.assertEqual(details[1].rows, ())
        self.assertEqual(details[2].rows[0].comment, "MOVED")
        self.assertEqual(details[2].total_minutes, 75)

    def test_old_generation_and_wrong_account_cannot_supply_titles(self):
        self.publish(generation=2)
        apply = self.app._Wrike__apply_timelog_title_lookup
        self.assertFalse(apply(1, self.fingerprint, ("T1",), {"T1": "OLD-GENERATION"}))
        self.assertFalse(apply(2, "b" * 64, ("T1",), {"T1": "OTHER-ACCOUNT"}))
        self.assertEqual(self.app._Wrike__timelog_title_cache, {})
        self.assertEqual(self.app._Wrike__timelog_day_details[0].rows[0].task_title, "")

    def test_token_change_discards_all_details_and_rejects_inflight_result(self):
        self.publish()
        self.app._Wrike__apply_timelog_title_lookup(1, self.fingerprint, ("T1",), {"T1": "OLD ACCOUNT TITLE"})
        self.app._Wrike__timelog_title_lookup_generations.add(1)
        self.app._Wrike__set_wrike_api_token_session("QA-SYNTHETIC-TOKEN-B")
        self.assertEqual(self.app._Wrike__timelog_day_details, ())
        self.assertEqual(self.app._Wrike__timelog_title_cache, {})
        self.assertEqual(self.app._Wrike__timelog_title_lookup_generations, set())
        self.assertIsNone(self.app.get_timelog_snapshot().total_recorded_minutes)
        self.assertFalse(self.app._Wrike__apply_timelog_title_lookup(1, self.fingerprint, ("T1",), {"T1": "DELAYED PRIVATE TITLE"}))

    def test_failed_and_missing_titles_do_not_remove_logged_minutes(self):
        self.publish()
        apply = self.app._Wrike__apply_timelog_title_lookup
        self.assertTrue(apply(1, self.fingerprint, ("T1", "T2"), None))
        self.assertEqual(self.app._Wrike__timelog_day_details[0].total_minutes, 60)
        self.assertEqual(self.app._Wrike__timelog_day_details[0].rows[0].title_state, "unavailable")
        self.assertTrue(apply(1, self.fingerprint, ("T1", "T2"), {"T1": "EXPLICIT-TITLE"}))
        self.assertEqual(self.app._Wrike__timelog_day_details[0].rows[1].task_title, "EXPLICIT-TITLE")
        self.assertEqual(self.app._Wrike__timelog_day_details[1].rows[0].title_state, "missing")
        self.assertEqual(self.app._Wrike__timelog_day_details[1].rows[0].minutes, 30)

    def test_successful_refresh_retries_transient_title_failure(self):
        self.publish()
        self.assertTrue(self.app._Wrike__apply_timelog_title_lookup(1, self.fingerprint, ("T1", "T2"), None))
        _, _, start = self.publish(generation=2)
        start.assert_called_once_with(2, self.fingerprint, ("T1", "T2"))

    def test_cache_remains_aggregate_only_after_title_resolution(self):
        self.publish()
        self.app._Wrike__apply_timelog_title_lookup(1, self.fingerprint, ("T1",), {"T1": "PRIVATE-TITLE-CANARY"})
        files = tuple(Path(self.tmp.name).rglob("*"))
        cached = Path(self.app._Wrike__timelog_cache_path).read_text(encoding="utf-8")
        payload = json.loads(cached)
        self.assertEqual(set(payload["days"][0]), {"date", "recorded_minutes"})
        for path in files:
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(b"PRIVATE-DETAIL-CANARY", data, str(path))
                self.assertNotIn(b"PRIVATE-TITLE-CANARY", data, str(path))

    def test_absent_detail_cache_and_next_week_never_claim_empty(self):
        fresh, _, _ = self.publish()
        days = [day.date() for day in self.week]
        next_week = [day + timedelta(days=7) for day in days]
        self.assertEqual({detail.state for detail in self.app._Wrike__panel_day_details(fresh, next_week)}, {"unavailable"})
        self.app._Wrike__timelog_day_details = ()
        self.app._Wrike__timelog_day_details_generation = 0
        for snapshot, state in (
            (fresh, "unavailable"),
            (make_loading_snapshot(generation=2), "loading"),
            (make_error_snapshot(generation=2, error_code="request_failed"), "unavailable"),
        ):
            with self.subTest(state=state, snapshot=snapshot.state):
                self.assertEqual({detail.state for detail in self.app._Wrike__panel_day_details(snapshot, days)}, {state})

    def test_title_query_is_bounded_deduplicated_and_ignores_unrequested_results(self):
        ids = tuple(f"T{index}" for index in range(105))
        requests = []
        def response(url, token):
            self.assertEqual(token, "QA-SYNTHETIC-TOKEN-A")
            batch = url.rsplit("/", 1)[1].split(",")
            requests.append(batch)
            return {"data": [{"id": task_id, "title": "Title " + task_id} for task_id in batch] + [{"id": "UNREQUESTED", "title": "Private"}]}
        with patch.object(self.app, "_Wrike__api_get_json", side_effect=response):
            titles = self.app._Wrike__query_task_titles("QA-SYNTHETIC-TOKEN-A", ids + ids)
        self.assertEqual([len(batch) for batch in requests], [100, 5])
        self.assertEqual(set(titles), set(ids))

    def test_title_dispatch_is_async_single_flight_and_applies_only_on_ui_queue(self):
        self.publish()
        self.app._Wrike__root = object()
        workers = []
        pending_ui = []
        def thread_factory(*, target, daemon):
            self.assertTrue(daemon)
            workers.append(target)
            return Mock()
        def queue_result(root, callback):
            pending_ui.append(callback)
            return True
        with patch("src.apps.Wrike.threading.Thread", side_effect=thread_factory), patch.object(
            self.app, "_Wrike__query_task_titles", return_value={"T1": "ASYNC-TITLE"},
        ) as query, patch.object(self.app, "_Wrike__ui_safe", side_effect=queue_result):
            start = self.app._Wrike__start_timelog_title_lookup
            start(1, self.fingerprint, ("T1",))
            start(1, self.fingerprint, ("T1",))
            self.assertEqual(len(workers), 1)
            query.assert_not_called()
            workers[0]()
            self.assertEqual(self.app._Wrike__timelog_day_details[0].rows[0].task_title, "")
            self.assertEqual(len(pending_ui), 1)
            pending_ui[0]()
        self.assertEqual(self.app._Wrike__timelog_day_details[0].rows[0].task_title, "ASYNC-TITLE")


if __name__ == "__main__":
    unittest.main()
