from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlparse

from src.apps.Monitor import Monitor
from src.apps.Wrike import Wrike
from src.apps.wrike_worktime import BreakInterval
from src.apps.wrike_timelog_snapshot import (
    TimelogDay,
    TimelogSnapshotState,
    WrikeTimelogSnapshotStore,
    apply_stale_threshold,
    make_fresh_snapshot,
)
from src.utils.secret_store import SecretStore


class _FrozenDateTime(datetime):
    current = datetime(2026, 4, 6, 9, 0)

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return tz.fromutc(cls.current.replace(tzinfo=tz))
        return cls.current


class _FakeThread:
    created = []

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon
        self.started = False
        self.__class__.created.append(self)

    def start(self):
        self.started = True


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls = []
        self.cancelled = []
        self._next_id = 0

    def after(self, delay_ms, callback):
        self._next_id += 1
        after_id = f"after-{self._next_id}"
        self.after_calls.append((after_id, int(delay_ms), callback))
        return after_id

    def after_cancel(self, after_id):
        self.cancelled.append(after_id)
        self.after_calls = [item for item in self.after_calls if item[0] != after_id]


class _FakePanel:
    instances = []

    def __init__(self, root=None, model_provider=None, **callbacks) -> None:
        self.root = root
        self.model_provider = model_provider
        self.callbacks = callbacks
        self.visible = False
        self.show_result = True
        self.show_maps = True
        self.toggle_calls = []
        self.show_calls = []
        self.hide_calls = 0
        self.destroy_calls = 0
        self.__class__.instances.append(self)

    def toggle(self, activate=True):
        self.toggle_calls.append(bool(activate))
        self.visible = not self.visible

    def show(self, activate=True):
        self.show_calls.append(bool(activate))
        self.visible = bool(self.show_result and self.show_maps)
        return bool(self.show_result)

    def is_visible(self):
        return self.visible

    def hide(self):
        self.hide_calls += 1
        self.visible = False

    def destroy(self):
        self.destroy_calls += 1
        self.visible = False


class _FakeWatcher:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.reset_calls = 0

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1

    def reset_baseline(self):
        self.reset_calls += 1


class _FakeKeyboard:
    def add_hotkey(self, *_args, **_kwargs):
        return object()

    def unhook_all(self):
        return None

    def stash_state(self):
        return None


class WrikeRealtimeProgressIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.appdata = Path(self.temp_dir.name)
        _FakeThread.created = []
        _FakePanel.instances = []
        _FrozenDateTime.current = datetime(2026, 4, 6, 9, 0)

    def _new_wrike(self) -> Wrike:
        with patch.dict(os.environ, {"APPDATA": str(self.appdata)}, clear=False):
            wrike = Wrike()
        wrike._Wrike__lib.datetime = _FrozenDateTime
        return wrike

    @staticmethod
    def _week_datetimes(start: date = date(2026, 4, 6)) -> list[datetime]:
        return [
            datetime.combine(start + timedelta(days=index), datetime.min.time())
            for index in range(7)
        ]

    @staticmethod
    def _fresh_snapshot(
        minutes=(0, 0, 0, 0, 0, 0, 0),
        *,
        generation=1,
        fetched_at=datetime(2026, 4, 6, 9, 0),
    ):
        start = date(2026, 4, 6)
        return make_fresh_snapshot(
            days=tuple(
                TimelogDay(start + timedelta(days=index), int(value))
                for index, value in enumerate(minutes)
            ),
            display_name="Integration User",
            fetched_at=fetched_at,
            generation=generation,
        )

    def _install_snapshot(self, wrike: Wrike, snapshot) -> None:
        wrike._Wrike__timelog_snapshot = snapshot
        wrike._Wrike__timelog_last_good = snapshot
        wrike._Wrike__timelog_refresh_generation = snapshot.generation
        wrike._Wrike__timelog_refresh_running = False
        wrike._Wrike__timelog_refresh_running_generation = None

    def test_authoritative_contact_query_ignores_folder_and_paginates_with_dedupe(self) -> None:
        wrike = self._new_wrike()
        wrike._Wrike__monitor_folder_path = [{"id": "folder-secret", "title": "Private"}]
        pages = [
            {
                "data": [
                    {"id": "a", "trackedDate": "2026-04-06", "hours": 1},
                    {"id": "b", "trackedDate": "2026-04-07", "hours": 2},
                ],
                "nextPageToken": "page-2",
            },
            {
                "data": [
                    {"id": "b", "trackedDate": "2026-04-07", "hours": 2},
                    {"id": "c", "trackedDate": "2026-04-08", "hours": 3},
                ]
            },
        ]
        get_json = Mock(side_effect=pages)
        wrike._Wrike__api_get_json = get_json

        items, error = wrike._Wrike__query_authoritative_timelogs_week(
            "token-value",
            "KU 123/45",
            self._week_datetimes(),
        )

        self.assertIsNone(error)
        self.assertEqual([item["id"] for item in items], ["a", "b", "c"])
        self.assertEqual(get_json.call_count, 2)
        urls = [call.args[0] for call in get_json.call_args_list]
        self.assertTrue(all("/contacts/KU%20123%2F45/timelogs?" in url for url in urls))
        self.assertTrue(all("/folders/" not in url for url in urls))
        first_query = parse_qs(urlparse(urls[0]).query)
        self.assertEqual(
            json.loads(first_query["trackedDate"][0]),
            {"start": "2026-04-06", "end": "2026-04-12"},
        )
        second_query = parse_qs(urlparse(urls[1]).query)
        self.assertEqual(second_query["nextPageToken"], ["page-2"])

    def test_authoritative_pagination_token_is_strict_and_opaque(self) -> None:
        malformed_tokens = (
            None,
            "",
            " page-2",
            "page-2 ",
            7,
            ["page-2"],
            {"token": "page-2"},
        )
        for raw_token in malformed_tokens:
            with self.subTest(raw_token=raw_token):
                wrike = self._new_wrike()
                wrike._Wrike__api_get_json = Mock(
                    return_value={"data": [], "nextPageToken": raw_token}
                )

                items, error = wrike._Wrike__query_authoritative_timelogs_week(
                    "token",
                    "contact",
                    self._week_datetimes(),
                )

                self.assertIsNone(items)
                self.assertEqual(error, "invalid_response")

        opaque_token = "opaque token/+=:value"
        wrike = self._new_wrike()
        get_json = Mock(
            side_effect=[
                {"data": [], "nextPageToken": opaque_token},
                {"data": []},
            ]
        )
        wrike._Wrike__api_get_json = get_json

        items, error = wrike._Wrike__query_authoritative_timelogs_week(
            "token",
            "contact",
            self._week_datetimes(),
        )

        self.assertEqual(items, [])
        self.assertIsNone(error)
        second_query = parse_qs(urlparse(get_json.call_args_list[1].args[0]).query)
        self.assertEqual(second_query["nextPageToken"], [opaque_token])

    def test_authoritative_parser_rejects_noncanonical_identity_date_and_duration(self) -> None:
        invalid_entries = {
            "non-string-id": {
                "id": {"not": "a string"},
                "trackedDate": "2026-04-06",
                "hours": 1,
            },
            "date-suffix": {
                "id": "bad-date",
                "trackedDate": "2026-04-06Tnot-a-time",
                "hours": 1,
            },
            "numeric-string": {
                "id": "bad-string",
                "trackedDate": "2026-04-06",
                "hours": "1.5",
            },
            "scaled-overflow": {
                "id": "bad-overflow",
                "trackedDate": "2026-04-06",
                "hours": 1e308,
            },
            "integer-overflow": {
                "id": "bad-integer-overflow",
                "trackedDate": "2026-04-06",
                "hours": 10**10000,
            },
        }
        for name, entry in invalid_entries.items():
            with self.subTest(name=name):
                wrike = self._new_wrike()
                wrike._Wrike__api_get_json = Mock(return_value={"data": [entry]})
                items, error = wrike._Wrike__query_authoritative_timelogs_week(
                    "token",
                    "contact",
                    self._week_datetimes(),
                )
                self.assertIsNone(items)
                self.assertEqual(error, "invalid_response")

    def test_authoritative_pagination_cycle_and_limit_fail_closed(self) -> None:
        wrike = self._new_wrike()
        wrike._Wrike__api_get_json = Mock(
            side_effect=[
                {
                    "data": [
                        {"id": "a", "trackedDate": "2026-04-06", "hours": 1}
                    ],
                    "nextPageToken": "repeat",
                },
                {
                    "data": [
                        {"id": "b", "trackedDate": "2026-04-07", "hours": 1}
                    ],
                    "nextPageToken": "repeat",
                },
            ]
        )
        items, error = wrike._Wrike__query_authoritative_timelogs_week(
            "token", "contact", self._week_datetimes()
        )
        self.assertIsNone(items)
        self.assertEqual(error, "pagination_cycle")

        wrike._Wrike__wrike_api_max_pages = 1
        wrike._Wrike__api_get_json = Mock(
            return_value={
                "data": [
                    {
                        "id": "partial",
                        "trackedDate": "2026-04-06",
                        "minutes": 15,
                    }
                ],
                "nextPageToken": "more",
            }
        )
        items, error = wrike._Wrike__query_authoritative_timelogs_week(
            "token", "contact", self._week_datetimes()
        )
        self.assertIsNone(items)
        self.assertEqual(error, "pagination_limit")

    def test_malformed_authoritative_entry_is_error_and_never_cached_as_fresh(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        wrike._Wrike__root = root
        wrike._Wrike__background_active = True
        wrike._Wrike__wrike_api_token_session = "token"
        wrike._Wrike__resolve_contact_identity = Mock(
            return_value=("contact", "Integration User", None)
        )
        wrike._Wrike__api_get_json = Mock(
            return_value={
                "data": [
                    {
                        "id": "broken",
                        "trackedDate": "2026-04-06",
                        "hours": "NaN",
                    }
                ]
            }
        )

        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            generation = wrike._Wrike__request_timelog_snapshot_refresh(force=True)
        _FakeThread.created[0].target()
        wrike._Wrike__drain_ui_queue()

        snapshot = wrike.get_timelog_snapshot()
        self.assertEqual(snapshot.generation, generation)
        self.assertEqual(snapshot.state, TimelogSnapshotState.ERROR)
        self.assertEqual(snapshot.error_code, "invalid_response")
        self.assertFalse(snapshot.has_last_good_data)
        self.assertFalse(
            (self.appdata / "windows-supporter" / "wrike_timelog_cache.json").exists()
        )

    def test_refresh_success_cache_error_retention_and_late_generation_rejection(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        wrike._Wrike__root = root
        wrike._Wrike__background_active = True
        wrike._Wrike__wrike_api_token_session = "token"
        wrike._Wrike__resolve_contact_identity = Mock(
            return_value=("contact-id", "Integration User", None)
        )
        wrike._Wrike__query_authoritative_timelogs_week = Mock(
            return_value=(
                [
                    {
                        "id": "entry-1",
                        "trackedDate": "2026-04-06",
                        "hours": 2.5,
                    }
                ],
                None,
            )
        )

        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            generation = wrike._Wrike__request_timelog_snapshot_refresh(force=True)

        self.assertEqual(len(_FakeThread.created), 1)
        self.assertEqual(wrike.get_timelog_snapshot().state, TimelogSnapshotState.LOADING)
        _FakeThread.created[0].target()
        wrike._Wrike__drain_ui_queue()

        snapshot = wrike.get_timelog_snapshot()
        self.assertEqual(snapshot.state, TimelogSnapshotState.FRESH)
        self.assertEqual(snapshot.generation, generation)
        self.assertEqual(snapshot.recorded_minutes_for(date(2026, 4, 6)), 150)
        self.assertEqual(len(snapshot.days), 7)
        cache_path = self.appdata / "windows-supporter" / "wrike_timelog_cache.json"
        self.assertTrue(cache_path.exists())
        cache_text = cache_path.read_text(encoding="utf-8")
        self.assertNotIn("token", cache_text.lower())
        self.assertNotIn("url", cache_text.lower())

        wrike._Wrike__timelog_refresh_generation = int(generation) + 1
        wrike._Wrike__timelog_refresh_running_generation = int(generation) + 1
        self.assertFalse(
            wrike._Wrike__apply_timelog_snapshot_result(
                generation,
                snapshot=snapshot,
                account_fingerprint=wrike._Wrike__timelog_token_fingerprint("token"),
            )
        )
        self.assertIs(wrike.get_timelog_snapshot(), snapshot)

        current_generation = int(generation) + 1
        self.assertTrue(
            wrike._Wrike__apply_timelog_snapshot_result(
                current_generation,
                error_code="request_failed",
            )
        )
        failed = wrike.get_timelog_snapshot()
        self.assertEqual(failed.state, TimelogSnapshotState.ERROR)
        self.assertEqual(failed.error_code, "request_failed")
        self.assertEqual(failed.days, snapshot.days)

        wrike._Wrike__wrike_api_token_session = ""
        missing_generation = wrike._Wrike__request_timelog_snapshot_refresh(force=True)
        missing = wrike.get_timelog_snapshot()
        self.assertEqual(missing.generation, missing_generation)
        self.assertEqual(missing.error_code, "api_token_missing")
        self.assertEqual(missing.days, snapshot.days)

        restored = WrikeTimelogSnapshotStore(cache_path).load(
            expected_account_fingerprint=wrike._Wrike__timelog_token_fingerprint(
                "token"
            ),
            generation=int(generation),
        )
        self.assertIsNotNone(restored)
        assert restored is not None
        restored = apply_stale_threshold(
            restored,
            now=_FrozenDateTime.current,
            stale_after=timedelta(0),
        )
        self.assertEqual(restored.state, TimelogSnapshotState.STALE)
        self.assertEqual(restored.days, snapshot.days)

    def test_replacement_token_restart_does_not_restore_previous_account_cache(self) -> None:
        def protect_value(_store, value):
            return f"protected:{value}"

        def unprotect_value(_store, value):
            raw = str(value or "")
            return raw.split("protected:", 1)[1] if raw.startswith("protected:") else ""

        with patch.object(
            SecretStore,
            "protect",
            autospec=True,
            side_effect=protect_value,
        ), patch.object(
            SecretStore,
            "unprotect",
            autospec=True,
            side_effect=unprotect_value,
        ):
            wrike = self._new_wrike()
            ok, error = wrike.update_settings({"api_token": "account-a-token"})
            self.assertTrue(ok, error)
            generation = wrike._Wrike__timelog_refresh_generation
            account_a = self._fresh_snapshot(
                (60, 0, 0, 0, 0, 0, 0),
                generation=generation,
            )
            self.assertTrue(
                wrike._Wrike__apply_timelog_snapshot_result(
                    generation,
                    snapshot=account_a,
                    account_fingerprint=wrike._Wrike__timelog_token_fingerprint(
                        "account-a-token"
                    ),
                )
            )

            ok, error = wrike.update_settings({"api_token": "account-b-token"})
            self.assertTrue(ok, error)
            restarted = self._new_wrike()

        snapshot = restarted.get_timelog_snapshot()
        self.assertEqual(snapshot.state, TimelogSnapshotState.LOADING)
        self.assertFalse(snapshot.has_last_good_data)
        self.assertNotEqual(snapshot.display_name, "Integration User")
        self.assertTrue(
            (self.appdata / "windows-supporter" / "wrike_timelog_cache.json").exists()
        )

    def test_failed_token_update_rolls_back_runtime_and_cannot_rebind_cache(self) -> None:
        def protect_value(_store, value):
            return f"protected:{value}"

        def unprotect_value(_store, value):
            raw = str(value or "")
            return raw.split("protected:", 1)[1] if raw.startswith("protected:") else ""

        with patch.object(
            SecretStore,
            "protect",
            autospec=True,
            side_effect=protect_value,
        ), patch.object(
            SecretStore,
            "unprotect",
            autospec=True,
            side_effect=unprotect_value,
        ):
            wrike = self._new_wrike()
            ok, error = wrike.update_settings({"api_token": "account-a-token"})
            self.assertTrue(ok, error)
            generation = wrike._Wrike__timelog_refresh_generation
            account_a = self._fresh_snapshot(
                (60, 0, 0, 0, 0, 0, 0),
                generation=generation,
            )
            fingerprint_a = wrike._Wrike__timelog_token_fingerprint(
                "account-a-token"
            )
            self.assertTrue(
                wrike._Wrike__apply_timelog_snapshot_result(
                    generation,
                    snapshot=account_a,
                    account_fingerprint=fingerprint_a,
                )
            )

            with patch.object(wrike, "_Wrike__save_settings", return_value=False):
                ok, error = wrike.update_settings(
                    {"api_token": "account-b-token"}
                )
            self.assertFalse(ok)
            self.assertEqual(error, "settings save failed")
            self.assertEqual(
                wrike._Wrike__wrike_api_token_session,
                "account-a-token",
            )
            self.assertEqual(wrike.get_timelog_snapshot(), account_a)

            account_b = self._fresh_snapshot(
                (120, 0, 0, 0, 0, 0, 0),
                generation=generation,
            )
            self.assertFalse(
                wrike._Wrike__apply_timelog_snapshot_result(
                    generation,
                    snapshot=account_b,
                    account_fingerprint=wrike._Wrike__timelog_token_fingerprint(
                        "account-b-token"
                    ),
                )
            )
            restarted = self._new_wrike()

        restored = restarted.get_timelog_snapshot()
        self.assertEqual(restored.state, TimelogSnapshotState.STALE)
        self.assertEqual(restored.recorded_minutes_for(date(2026, 4, 6)), 60)

    def test_weekly_model_uses_explicit_plan_weekends_future_vacation_and_recorded_actual(self) -> None:
        wrike = self._new_wrike()
        _FrozenDateTime.current = datetime(2026, 4, 9, 23, 55)
        wrike.update_workday_plan(date(2026, 4, 9), 9 * 60, "08:00")
        wrike.update_workday_plan(date(2026, 4, 11), 120, None)
        snapshot = self._fresh_snapshot(
            (480, 480, 480, 510, 0, 0, 0),
            generation=7,
            fetched_at=_FrozenDateTime.current,
        )
        self._install_snapshot(wrike, snapshot)

        def vacation_for_day(target_day):
            if target_day == date(2026, 4, 10):
                return {"all_day": True, "intervals": [], "event_count": 1}
            return {}

        with patch.object(
            wrike,
            "_Wrike__vacation_result_for_date",
            side_effect=vacation_for_day,
        ):
            model = wrike._Wrike__build_worktime_panel_model()
            overview_rows = wrike._Wrike__build_overview_rows([], 9 * 60)

        self.assertEqual(len(model.rows), 7)
        self.assertEqual([row.weekday for row in model.rows], ["월", "화", "수", "목", "금", "토", "일"])
        self.assertIn("부족 30분", overview_rows[-1][0])
        self.assertNotIn("5시간 55분", overview_rows[-1][0])
        today_text = "\n".join(line.text for line in model.today_lines)
        self.assertIn("Wrike 기록 8시간 30분", today_text)
        self.assertIn("현재 기대 9시간", today_text)
        self.assertIn("현재 기준 부족 30분", today_text)
        self.assertIn("출근 08:00", today_text)
        self.assertIn("예상 퇴근 18:00", today_text)
        self.assertIn("적용 목표 9시간", today_text)
        self.assertIn("동기화 fresh", today_text)

        friday = model.rows[4]
        saturday = model.rows[5]
        sunday = model.rows[6]
        self.assertIn("휴가 8시간", friday.summary)
        self.assertNotIn("부족", friday.summary)
        self.assertIn("목표 2시간", saturday.summary)
        self.assertNotEqual(saturday.summary, "휴무")
        self.assertEqual(sunday.summary, "휴무")
        self.assertNotIn("부족", sunday.summary)

    def test_weekly_timed_vacation_excludes_break_overlap_for_past_and_future_rows(self) -> None:
        wrike = self._new_wrike()
        _FrozenDateTime.current = datetime(2026, 4, 8, 10, 0)
        monday = date(2026, 4, 6)
        tuesday = date(2026, 4, 7)
        wednesday = date(2026, 4, 8)
        friday = date(2026, 4, 10)
        wrike.update_workday_plan(monday, 8 * 60, None)
        wrike.update_workday_plan(tuesday, 8 * 60, None)
        wrike.update_workday_plan(wednesday, 8 * 60, "08:00")
        wrike.update_workday_plan(friday, 8 * 60, None)
        self._install_snapshot(
            wrike,
            self._fresh_snapshot(
                (7 * 60, 7 * 60, 2 * 60, 0, 0, 0, 0),
                generation=8,
                fetched_at=_FrozenDateTime.current,
            ),
        )
        vacations = {
            monday: {
                "all_day": False,
                "intervals": [
                    (datetime(2026, 4, 6, 12, 30), datetime(2026, 4, 6, 14, 0)),
                ],
                "event_count": 1,
            },
            tuesday: {
                "all_day": False,
                "intervals": [
                    (datetime(2026, 4, 7, 10, 30), datetime(2026, 4, 7, 12, 0)),
                ],
                "event_count": 1,
            },
            friday: {
                "all_day": False,
                "intervals": [
                    (datetime(2026, 4, 10, 15, 30), datetime(2026, 4, 10, 17, 0)),
                ],
                "event_count": 1,
            },
        }
        breaks = {
            monday: [
                BreakInterval(
                    datetime(2026, 4, 6, 12, 0),
                    datetime(2026, 4, 6, 13, 0),
                    "점심",
                ),
            ],
            tuesday: [
                BreakInterval(
                    datetime(2026, 4, 7, 10, 0),
                    datetime(2026, 4, 7, 11, 0),
                    "캘린더",
                ),
            ],
            friday: [
                BreakInterval(
                    datetime(2026, 4, 10, 15, 0),
                    datetime(2026, 4, 10, 16, 0),
                    "수동",
                ),
            ],
        }
        collector_calls = []

        def vacation_for_day(target_day):
            return vacations.get(
                target_day,
                {"all_day": False, "intervals": [], "event_count": 0},
            )

        def breaks_for_day(target_day, now):
            collector_calls.append((target_day, now))
            return list(breaks.get(target_day, []))

        with (
            patch.object(
                wrike,
                "_Wrike__vacation_result_for_date",
                side_effect=vacation_for_day,
            ),
            patch.object(
                wrike,
                "_Wrike__collect_break_intervals_for_day",
                side_effect=breaks_for_day,
            ),
        ):
            model = wrike._Wrike__build_worktime_panel_model()

        self.assertEqual(
            model.rows[0].summary,
            "Wrike 7시간 · 목표 7시간 · 딱 맞음",
        )
        self.assertEqual(
            model.rows[1].summary,
            "Wrike 7시간 · 목표 7시간 · 딱 맞음",
        )
        self.assertNotIn("초과 30분", model.rows[0].summary)
        self.assertNotIn("초과 30분", model.rows[1].summary)
        self.assertEqual(
            model.rows[4].summary,
            "목표 8시간 · 휴가 1시간 · 적용 7시간",
        )
        self.assertEqual(
            {target_day for target_day, _now in collector_calls},
            {monday, tuesday, wednesday, friday},
        )
        self.assertTrue(
            all(now == _FrozenDateTime.current for _target_day, now in collector_calls)
        )

    def test_partial_vacation_pauses_expected_progress_and_moves_quit_time(self) -> None:
        wrike = self._new_wrike()
        _FrozenDateTime.current = datetime(2026, 4, 6, 15, 0)
        wrike.update_workday_plan(date(2026, 4, 6), 8 * 60, "08:00")
        snapshot = self._fresh_snapshot(
            (5 * 60, 0, 0, 0, 0, 0, 0),
            generation=8,
            fetched_at=_FrozenDateTime.current,
        )
        self._install_snapshot(wrike, snapshot)
        vacation = {
            "all_day": False,
            "intervals": [
                (datetime(2026, 4, 6, 14, 0), datetime(2026, 4, 6, 16, 0)),
            ],
            "event_count": 1,
        }

        with patch.object(
            wrike,
            "_Wrike__vacation_result_for_date",
            return_value=vacation,
        ):
            during_vacation = wrike._Wrike__today_overview(
                _FrozenDateTime.current,
                snapshot,
            )
            after_vacation = wrike._Wrike__today_overview(
                datetime(2026, 4, 6, 16, 30),
                snapshot,
            )

        self.assertEqual(during_vacation.vacation_minutes, 120)
        self.assertEqual(during_vacation.effective_target_minutes, 360)
        self.assertEqual(during_vacation.expected_now_minutes, 300)
        self.assertEqual(during_vacation.realtime_delta_minutes, 0)
        self.assertEqual(during_vacation.break_total_minutes, 120)
        self.assertIn("휴가 1시간", during_vacation.break_labels)
        self.assertEqual(during_vacation.projected_quit, datetime(2026, 4, 6, 17, 0))
        self.assertEqual(after_vacation.expected_now_minutes, 330)
        self.assertEqual(after_vacation.realtime_delta_minutes, -30)
        self.assertEqual(after_vacation.break_total_minutes, 180)
        self.assertIn("휴가 2시간", after_vacation.break_labels)

    def test_timed_vacation_overlapping_lunch_is_credited_only_once(self) -> None:
        wrike = self._new_wrike()
        _FrozenDateTime.current = datetime(2026, 4, 6, 14, 0)
        wrike.update_workday_plan(date(2026, 4, 6), 8 * 60, "08:00")
        snapshot = self._fresh_snapshot(
            (4 * 60, 0, 0, 0, 0, 0, 0),
            generation=9,
            fetched_at=_FrozenDateTime.current,
        )
        self._install_snapshot(wrike, snapshot)
        vacation = {
            "all_day": False,
            "intervals": [
                (datetime(2026, 4, 6, 12, 30), datetime(2026, 4, 6, 14, 0)),
            ],
            "event_count": 1,
        }

        with patch.object(
            wrike,
            "_Wrike__vacation_result_for_date",
            return_value=vacation,
        ):
            overview = wrike._Wrike__today_overview(
                _FrozenDateTime.current,
                snapshot,
            )

        self.assertEqual(overview.vacation_minutes, 60)
        self.assertEqual(overview.effective_target_minutes, 7 * 60)
        self.assertEqual(overview.break_total_minutes, 2 * 60)
        self.assertEqual(overview.expected_now_minutes, 4 * 60)
        self.assertEqual(overview.realtime_delta_minutes, 0)
        self.assertEqual(overview.projected_quit, datetime(2026, 4, 6, 17, 0))
        self.assertIn("점심/휴가 2시간", overview.break_labels)

    def test_visible_provider_updates_dynamic_expected_and_requests_only_one_stale_refresh(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        panel = _FakePanel()
        panel.visible = True
        wrike._Wrike__root = root
        wrike._Wrike__background_active = True
        wrike._Wrike__worktime_panel = panel
        wrike._Wrike__worktime_panel_root = root
        wrike._Wrike__wrike_api_token_session = "token"
        wrike.update_workday_plan(date(2026, 4, 6), 9 * 60, "08:00")
        old_snapshot = self._fresh_snapshot(
            (60, 0, 0, 0, 0, 0, 0),
            generation=3,
            fetched_at=datetime(2026, 4, 6, 8, 0),
        )
        self._install_snapshot(wrike, old_snapshot)
        _FrozenDateTime.current = datetime(2026, 4, 6, 10, 0)

        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            first = wrike._Wrike__build_worktime_panel_model()
            _FrozenDateTime.current = datetime(2026, 4, 6, 10, 1)
            second = wrike._Wrike__build_worktime_panel_model()

        first_text = "\n".join(line.text for line in first.today_lines)
        second_text = "\n".join(line.text for line in second.today_lines)
        self.assertIn("현재 기대 2시간", first_text)
        self.assertIn("현재 기대 2시간 1분", second_text)
        self.assertEqual(len(_FakeThread.created), 1)
        self.assertTrue(wrike._Wrike__timelog_refresh_running)

    def test_first_activity_conditions_snooze_accept_edit_skip_and_explicit_weekend(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        panel = _FakePanel()
        wrike._Wrike__root = root
        wrike._Wrike__worktime_panel = panel
        wrike._Wrike__worktime_panel_root = root

        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 7, 59))
        self.assertIsNone(wrike._Wrike__worktime_state_store.get_activity_prompt(date(2026, 4, 6)))

        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 5))
        prompt = wrike._Wrike__worktime_state_store.get_activity_prompt(date(2026, 4, 6))
        self.assertEqual(prompt["status"], "pending")
        self.assertEqual(panel.show_calls, [False])
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 6))
        self.assertEqual(panel.show_calls, [False])

        _FrozenDateTime.current = datetime(2026, 4, 6, 8, 6)
        with patch.object(
            wrike._Wrike__worktime_state_store,
            "clear_activity_prompt",
            side_effect=AssertionError("clock-in must use one state transaction"),
        ) as clear_prompt:
            self.assertTrue(wrike._Wrike__save_clock_in("08:05"))
        clear_prompt.assert_not_called()
        plan = wrike.get_workday_plan(date(2026, 4, 6))
        self.assertEqual(plan["clock_in"], "08:05")
        self.assertEqual(plan["target_net_minutes"], 480)
        self.assertIsNone(wrike._Wrike__worktime_state_store.get_activity_prompt(date(2026, 4, 6)))
        with patch.object(
            wrike._Wrike__worktime_state_store,
            "get_activity_prompt",
            return_value={
                "status": "pending",
                "detected_at": "2026-04-06T08:05:00",
            },
        ):
            self.assertIsNone(
                wrike._Wrike__visible_activity_prompt(
                    _FrozenDateTime.current,
                    plan,
                )
            )

        wrike.clear_workday_plan(date(2026, 4, 6))
        wrike._Wrike__worktime_state_store.record_activity_prompt_pending(
            date(2026, 4, 6), datetime(2026, 4, 6, 9, 0)
        )
        _FrozenDateTime.current = datetime(2026, 4, 6, 9, 1)
        wrike._Wrike__panel_prompt_snooze()
        self.assertEqual(
            wrike._Wrike__worktime_state_store.get_activity_prompt(date(2026, 4, 6))["status"],
            "snoozed",
        )
        shown_before = len(panel.show_calls)
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 9, 20))
        self.assertEqual(len(panel.show_calls), shown_before)
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 9, 32))
        self.assertEqual(len(panel.show_calls), shown_before + 1)
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 9, 33))
        self.assertEqual(len(panel.show_calls), shown_before + 1)
        self.assertEqual(
            wrike._Wrike__worktime_state_store.get_activity_prompt(date(2026, 4, 6))["status"],
            "pending",
        )

        _FrozenDateTime.current = datetime(2026, 4, 6, 9, 33)
        ask = Mock(return_value="09:30")
        fake_simpledialog = Mock(askstring=ask)
        with patch.dict(
            "sys.modules",
            {"tkinter.simpledialog": fake_simpledialog},
        ):
            wrike._Wrike__panel_prompt_edit("09:32")
        ask.assert_called_once()
        self.assertEqual(wrike.get_workday_plan(date(2026, 4, 6))["clock_in"], "09:30")
        self.assertIsNone(wrike._Wrike__worktime_state_store.get_activity_prompt(date(2026, 4, 6)))

        saturday = date(2026, 4, 11)
        shown_before = len(panel.show_calls)
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 11, 9, 0))
        self.assertEqual(len(panel.show_calls), shown_before)
        wrike.update_workday_plan(saturday, 60, None)
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 11, 9, 1))
        self.assertEqual(len(panel.show_calls), shown_before + 1)
        _FrozenDateTime.current = datetime(2026, 4, 11, 9, 1)
        wrike._Wrike__panel_prompt_skip()
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 11, 9, 2))
        self.assertEqual(len(panel.show_calls), shown_before + 1)

        wrike.clear_workday_plan(saturday)
        wrike._Wrike__worktime_state_store.clear_activity_prompt(saturday)
        with patch.object(
            wrike,
            "_Wrike__vacation_result_for_date",
            return_value={"all_day": True},
        ):
            wrike._Wrike__on_worktime_activity(datetime(2026, 4, 7, 9, 0))
        self.assertIsNone(wrike._Wrike__worktime_state_store.get_activity_prompt(date(2026, 4, 7)))

    def test_activity_panel_surface_retries_until_show_is_mapped(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        panel = _FakePanel()
        wrike._Wrike__root = root
        wrike._Wrike__worktime_panel = panel
        wrike._Wrike__worktime_panel_root = root
        detected_day = date(2026, 4, 6)

        panel.show_result = False
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 5))
        self.assertEqual(panel.show_calls, [False])
        self.assertEqual(wrike._Wrike__activity_prompt_surfaced_day, "")

        panel.show_result = True
        panel.show_maps = False
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 6))
        self.assertEqual(panel.show_calls, [False, False])
        self.assertEqual(wrike._Wrike__activity_prompt_surfaced_day, "")

        panel.show_maps = True
        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 7))
        self.assertEqual(panel.show_calls, [False, False, False])
        self.assertEqual(
            wrike._Wrike__activity_prompt_surfaced_day,
            detected_day.isoformat(),
        )

    def test_existing_clock_in_edit_prefills_and_preserves_value_on_cancel(self) -> None:
        wrike = self._new_wrike()
        wrike.update_workday_plan(date(2026, 4, 6), 480, "08:00")
        _FrozenDateTime.current = datetime(2026, 4, 6, 15, 0)

        cancel = Mock(return_value=None)
        with patch.dict(
            "sys.modules",
            {"tkinter.simpledialog": Mock(askstring=cancel)},
        ):
            wrike._Wrike__panel_edit_clock_in()
        self.assertEqual(
            cancel.call_args.kwargs.get("initialvalue"),
            "08:00",
        )
        self.assertEqual(
            wrike.get_workday_plan(date(2026, 4, 6))["clock_in"],
            "08:00",
        )

        confirm = Mock(return_value="08:15")
        with patch.dict(
            "sys.modules",
            {"tkinter.simpledialog": Mock(askstring=confirm)},
        ):
            wrike._Wrike__panel_edit_clock_in()
        self.assertEqual(
            wrike.get_workday_plan(date(2026, 4, 6))["clock_in"],
            "08:15",
        )

    def test_clock_in_now_preserves_implicit_weekend_zero_target(self) -> None:
        wrike = self._new_wrike()
        _FrozenDateTime.current = datetime(2026, 4, 11, 10, 15)

        wrike._Wrike__panel_clock_in_now()

        plan = wrike.get_workday_plan(date(2026, 4, 11))
        self.assertTrue(plan["explicit"])
        self.assertEqual(plan["target_net_minutes"], 0)
        self.assertEqual(plan["clock_in"], "10:15")

    def test_configured_vacation_loading_and_error_fail_closed_with_last_good_rules(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        panel = _FakePanel()
        wrike._Wrike__root = root
        wrike._Wrike__worktime_panel = panel
        wrike._Wrike__worktime_panel_root = root
        wrike._Wrike__vacation_ical_url_session = (
            "https://calendar.google.com/calendar/ical/source-a/basic.ics"
        )
        wrike._Wrike__vacation_ical_state = "loading"

        wrike._Wrike__on_worktime_activity(datetime(2026, 4, 6, 8, 5))
        self.assertIsNone(
            wrike._Wrike__worktime_state_store.get_activity_prompt(
                date(2026, 4, 6)
            )
        )
        self.assertEqual(panel.show_calls, [])

        wrike.update_workday_plan(date(2026, 4, 6), 480, "08:00")
        snapshot = self._fresh_snapshot(
            (120, 0, 0, 0, 0, 0, 0),
            generation=9,
            fetched_at=datetime(2026, 4, 6, 10, 0),
        )
        self._install_snapshot(wrike, snapshot)
        loading = wrike._Wrike__today_overview(
            datetime(2026, 4, 6, 10, 0),
            snapshot,
        )
        self.assertFalse(loading.vacation_available)
        self.assertFalse(loading.expected_available)
        self.assertIsNone(loading.realtime_delta_minutes)
        self.assertIsNone(loading.projected_quit)

        wrike._Wrike__vacation_ical_state = "error"
        wrike._Wrike__vacation_ical_last_error = "calendar_fetch_failed"
        no_last_good = wrike._Wrike__vacation_result_for_date(date(2026, 4, 6))
        self.assertFalse(no_last_good["available"])
        self.assertFalse(no_last_good["automatic_prompt_allowed"])

        wrike._Wrike__vacation_ical_calendar = {
            "calendar_name": "김종인-ePapyrus",
            "events": [],
        }
        vacation = {
            "calendar_matched": True,
            "all_day": False,
            "intervals": [],
            "event_count": 0,
        }
        with patch(
            "src.apps.Wrike.vacation_events_for_day",
            return_value=vacation,
        ):
            retained = wrike._Wrike__vacation_result_for_date(date(2026, 4, 6))
        self.assertTrue(retained["available"])
        self.assertTrue(retained["using_last_good"])
        self.assertFalse(retained["automatic_prompt_allowed"])

        wrike._Wrike__vacation_secret_store.protect = Mock(
            return_value="dpapi:new-source"
        )
        ok, error = wrike.update_settings(
            {
                "vacation_ical_url": (
                    "https://calendar.google.com/calendar/ical/source-b/basic.ics"
                )
            }
        )
        self.assertTrue(ok, error)
        self.assertEqual(wrike._Wrike__vacation_ical_calendar, {})
        self.assertEqual(wrike._Wrike__vacation_ical_state, "loading")

    def test_stale_vacation_callback_cannot_clear_new_fetch_owner(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        wrike._Wrike__root = root
        wrike._Wrike__background_active = True
        wrike._Wrike__vacation_ical_url_session = (
            "https://calendar.google.com/calendar/ical/synthetic/basic.ics"
        )
        wrike._Wrike__fetch_vacation_calendar_text = Mock(
            return_value="synthetic calendar"
        )
        parsed = {
            "calendar_name": "김종인-ePapyrus",
            "events": [],
        }

        with patch("src.apps.Wrike.threading.Thread", _FakeThread), patch(
            "src.apps.Wrike.parse_calendar",
            return_value=parsed,
        ):
            wrike._Wrike__start_vacation_ical_polling()
            first_generation = wrike._Wrike__vacation_ical_generation
            wrike._Wrike__vacation_ical_tick(first_generation)
            first_thread = _FakeThread.created[-1]
            first_owner = wrike._Wrike__vacation_ical_fetch_owner

            wrike._Wrike__start_vacation_ical_polling()
            second_generation = wrike._Wrike__vacation_ical_generation
            wrike._Wrike__vacation_ical_tick(second_generation)
            second_thread = _FakeThread.created[-1]
            second_owner = wrike._Wrike__vacation_ical_fetch_owner

            self.assertIsNot(first_owner, second_owner)
            self.assertTrue(wrike.get_vacation_ical_status_snapshot()["fetch_running"])

            first_thread.target()
            wrike._Wrike__drain_ui_queue()
            self.assertIs(wrike._Wrike__vacation_ical_fetch_owner, second_owner)
            self.assertTrue(wrike.get_vacation_ical_status_snapshot()["fetch_running"])

            second_thread.target()
            wrike._Wrike__drain_ui_queue()

        self.assertIsNone(wrike._Wrike__vacation_ical_fetch_owner)
        status = wrike.get_vacation_ical_status_snapshot()
        self.assertFalse(status["fetch_running"])
        self.assertEqual(status["state"], "fresh")
        self.assertIs(wrike._Wrike__vacation_ical_calendar, parsed)

    def test_weekly_vacation_results_are_cached_per_calendar_and_date(self) -> None:
        wrike = self._new_wrike()
        _FrozenDateTime.current = datetime(2026, 4, 8, 10, 0)
        self._install_snapshot(
            wrike,
            self._fresh_snapshot(
                generation=5,
                fetched_at=_FrozenDateTime.current,
            ),
        )
        wrike._Wrike__vacation_ical_url_session = (
            "https://calendar.google.com/calendar/ical/synthetic/basic.ics"
        )
        wrike._Wrike__vacation_ical_state = "fresh"
        wrike._Wrike__vacation_ical_calendar = {
            "calendar_name": "김종인-ePapyrus",
            "events": [],
        }
        result = {
            "calendar_matched": True,
            "all_day": False,
            "intervals": [],
            "event_count": 0,
        }

        with patch(
            "src.apps.Wrike.vacation_events_for_day",
            return_value=result,
        ) as lookup:
            wrike._Wrike__build_worktime_panel_model()
            wrike._Wrike__build_worktime_panel_model()
            self.assertEqual(lookup.call_count, 7)

            wrike._Wrike__vacation_ical_calendar = {
                "calendar_name": "김종인-ePapyrus",
                "events": [],
            }
            wrike._Wrike__build_worktime_panel_model()
            self.assertEqual(lookup.call_count, 14)

    def test_monitor_tooltip_uses_one_authoritative_snapshot_for_all_rows(self) -> None:
        wrike = self._new_wrike()
        _FrozenDateTime.current = datetime(2026, 4, 6, 10, 0)
        wrike.update_workday_plan(date(2026, 4, 6), 480, "08:00")
        authoritative = self._fresh_snapshot(
            (120, 0, 0, 0, 0, 0, 0),
            generation=14,
            fetched_at=_FrozenDateTime.current,
        )
        wrike._Wrike__monitor_folder_path = [
            {"id": "legacy-folder", "title": "Legacy scope"}
        ]

        rows = wrike._Wrike__compose_timelog_summary_rows(authoritative)
        text = "\n".join(row[0] for row in rows)

        self.assertIn("범위: 내 전체 타임로그 · snapshot generation 14", text)
        self.assertIn("Wrike 기록 2시간 · 현재 기대 2시간", text)
        self.assertIn("2026-04-06 (월): Wrike 기록 2시간", text)
        self.assertNotIn("폴더:", text)
        self.assertNotIn("Wrike 기록 조회 불가", text)

    def test_background_stop_blocks_panel_refresh_and_shutdown_reinvalidates(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        panel = _FakePanel()
        panel.visible = True
        watcher = _FakeWatcher()
        wrike._Wrike__root = root
        wrike._Wrike__background_active = True
        wrike._Wrike__worktime_panel = panel
        wrike._Wrike__worktime_panel_root = root
        wrike._Wrike__activity_watcher = watcher
        wrike._Wrike__wrike_api_token_session = "token"
        self._install_snapshot(
            wrike,
            self._fresh_snapshot(
                generation=4,
                fetched_at=datetime(2026, 4, 6, 8, 0),
            ),
        )
        _FrozenDateTime.current = datetime(2026, 4, 6, 10, 0)

        wrike.stop_background()
        generation_after_stop = wrike._Wrike__timelog_refresh_generation
        self.assertEqual(panel.hide_calls, 1)
        panel.visible = True
        with patch("src.apps.Wrike.threading.Thread", _FakeThread):
            wrike._Wrike__build_worktime_panel_model()
            requested = wrike._Wrike__request_timelog_snapshot_refresh(force=True)
        self.assertIsNone(requested)
        self.assertEqual(_FakeThread.created, [])

        wrike._Wrike__timelog_refresh_running = True
        wrike._Wrike__timelog_refresh_running_generation = generation_after_stop
        wrike.shutdown()
        self.assertGreater(
            wrike._Wrike__timelog_refresh_generation,
            generation_after_stop,
        )
        self.assertFalse(wrike._Wrike__timelog_refresh_running)
        self.assertIsNone(wrike._Wrike__root)

    def test_singleton_toggle_shutdown_and_session_unlock_lifecycle(self) -> None:
        wrike = self._new_wrike()
        root = _FakeRoot()
        wrike._Wrike__root = root
        wrike._Wrike__background_active = True
        watcher = _FakeWatcher()
        wrike._Wrike__activity_watcher = watcher

        with patch("src.apps.Wrike.WorktimeQuickPanel", _FakePanel), patch.object(
            wrike,
            "_Wrike__request_timelog_snapshot_refresh",
        ) as request_refresh:
            wrike.show_weekly_timelog_summary(root)
            wrike.show_weekly_timelog_summary(root)

        self.assertEqual(len(_FakePanel.instances), 1)
        panel = _FakePanel.instances[0]
        self.assertEqual(panel.toggle_calls, [True, True])
        request_refresh.assert_called_once_with(force=True)

        wrike.on_session_unlock()
        self.assertEqual(watcher.reset_calls, 1)
        generation_before = wrike._Wrike__timelog_refresh_generation
        wrike.shutdown()
        wrike.shutdown()
        self.assertGreater(wrike._Wrike__timelog_refresh_generation, generation_before)
        self.assertEqual(watcher.stop_calls, 1)
        self.assertEqual(panel.destroy_calls, 1)
        self.assertIsNone(wrike._Wrike__root)

    def test_monitor_quick_panel_cold_disabled_is_noop(self) -> None:
        monitor = Monitor()
        root = _FakeRoot()
        event_queue = queue.SimpleQueue()
        monitor._Monitor__background_enabled = False
        monitor._Monitor__root = root
        monitor._Monitor__event_queue = event_queue

        with patch("src.apps.Monitor.Wrike") as wrike_type:
            monitor.show_worktime_quick_panel()

        wrike_type.assert_not_called()
        with self.assertRaises(queue.Empty):
            event_queue.get_nowait()

    def test_monitor_quick_panel_disabled_after_attach_is_noop(self) -> None:
        monitor = Monitor()
        root = _FakeRoot()
        event_queue = queue.SimpleQueue()
        wrike = MagicMock()
        monitor._Monitor__background_enabled = True
        monitor._Monitor__root = root
        monitor._Monitor__event_queue = event_queue
        monitor._Monitor__wrike = wrike
        monitor._Monitor__wrike_attached = True
        monitor._Monitor__lib.keyboard = _FakeKeyboard()

        with patch.object(monitor, "_Monitor__save_background_enabled"):
            self.assertFalse(monitor.set_background_enabled(False))
        wrike.stop_background.assert_called_once_with()
        wrike.reset_mock()

        monitor.show_worktime_quick_panel()

        wrike.attach.assert_not_called()
        wrike.start_background.assert_not_called()
        wrike.show_weekly_timelog_summary.assert_not_called()
        with self.assertRaises(queue.Empty):
            event_queue.get_nowait()

    def test_monitor_quick_panel_queued_action_rechecks_background_state(self) -> None:
        monitor = Monitor()
        root = _FakeRoot()
        event_queue = queue.SimpleQueue()
        wrike = MagicMock()
        monitor._Monitor__background_enabled = True
        monitor._Monitor__root = root
        monitor._Monitor__event_queue = event_queue
        monitor._Monitor__wrike = wrike
        monitor._Monitor__wrike_attached = False
        monitor._Monitor__lib.keyboard = _FakeKeyboard()

        monitor.show_worktime_quick_panel()
        queued = event_queue.get_nowait()
        with patch.object(monitor, "_Monitor__save_background_enabled"):
            self.assertFalse(monitor.set_background_enabled(False))
        wrike.reset_mock()

        queued()

        wrike.attach.assert_not_called()
        wrike.start_background.assert_not_called()
        wrike.show_weekly_timelog_summary.assert_not_called()

    def test_monitor_keeps_ctrl_alt_w_ui_queue_and_forwards_wrike_lifecycle(self) -> None:
        monitor = Monitor()
        root = _FakeRoot()
        event_queue = queue.SimpleQueue()
        wrike = MagicMock()
        monitor._Monitor__background_enabled = True
        monitor._Monitor__root = root
        monitor._Monitor__event_queue = event_queue
        monitor._Monitor__wrike = wrike
        monitor._Monitor__wrike_attached = True
        monitor._Monitor__lib.keyboard = _FakeKeyboard()

        monitor.show_worktime_quick_panel()
        wrike.show_weekly_timelog_summary.assert_not_called()
        queued = event_queue.get_nowait()
        queued()
        wrike.show_weekly_timelog_summary.assert_called_once_with(root)

        monitor.on_session_unlock()
        wrike.on_session_unlock.assert_called_once_with()
        monitor._Monitor__stop_background_tasks()
        self.assertGreaterEqual(wrike.stop_background.call_count, 1)
        monitor.shutdown()
        wrike.shutdown.assert_called_once_with()
        self.assertIsNone(monitor._Monitor__root)
        self.assertIsNone(monitor._Monitor__event_queue)


if __name__ == "__main__":
    unittest.main()
