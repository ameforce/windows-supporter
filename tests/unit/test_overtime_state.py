from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from src.apps.overtime_state import OvertimeStateStore, STATE_VERSION


class OvertimeStateStoreTests(unittest.TestCase):
    def test_pending_active_completed_round_trip_is_atomic_and_reloadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            store = OvertimeStateStore(path)
            detected = datetime(2026, 9, 10, 18, 5)
            quit_at = datetime(2026, 9, 10, 18, 0)
            started = datetime(2026, 9, 10, 18, 6)
            ended = datetime(2026, 9, 10, 19, 21)

            self.assertEqual(
                store.set_pending(
                    detected.date(),
                    detected,
                    quit_at,
                    assigned_minutes=60,
                ),
                (True, None),
            )
            self.assertEqual(store.get(detected.date())["status"], "pending")
            self.assertEqual(store.start(detected.date(), started), (True, None))
            self.assertEqual(store.get(detected.date())["status"], "active")
            self.assertEqual(store.complete(detected.date(), ended), (True, None))
            self.assertEqual(store.get(detected.date())["status"], "completed")

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["state_version"], STATE_VERSION)
            self.assertEqual(raw["days"]["2026-09-10"]["assigned_minutes"], 60)
            reloaded = OvertimeStateStore(path)
            self.assertEqual(reloaded.snapshot(), store.snapshot())

    def test_skipped_state_cannot_be_started_or_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OvertimeStateStore(Path(tmp) / "overtime.json")
            day = datetime(2026, 9, 10)
            self.assertEqual(
                store.set_pending(day, day.replace(hour=18), day.replace(hour=18)),
                (True, None),
            )
            self.assertEqual(store.skip(day), (True, None))
            ok, error = store.start(day, day.replace(hour=18, minute=1))
            self.assertFalse(ok)
            self.assertIn("대기", error or "")

    def test_active_overtime_can_finish_after_midnight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OvertimeStateStore(Path(tmp) / "overtime.json")
            detected = datetime(2026, 9, 10, 23, 55)
            scheduled_quit = datetime(2026, 9, 11, 0, 30)
            started = datetime(2026, 9, 10, 23, 56)
            ended = datetime(2026, 9, 11, 0, 10)

            self.assertEqual(
                store.set_pending(
                    detected.date(),
                    detected,
                    scheduled_quit,
                ),
                (True, None),
            )
            self.assertEqual(store.start(detected.date(), started), (True, None))
            self.assertEqual(store.complete(detected.date(), ended), (True, None))
            self.assertEqual(store.get(detected.date())["status"], "completed")

    def test_malformed_existing_file_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            path.write_text(json.dumps({"state_version": 99, "days": {}}), encoding="utf-8")
            store = OvertimeStateStore(path)
            self.assertEqual(
                store.snapshot(), {"state_version": STATE_VERSION, "days": {}}
            )
            self.assertFalse(store.set_pending(
                datetime(2026, 9, 10),
                datetime(2026, 9, 10, 18),
                datetime(2026, 9, 10, 18),
            )[0])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["state_version"],
                99,
            )

    def test_pause_resume_accumulates_and_complete_closes_open_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            store = OvertimeStateStore(path)
            day = datetime(2026, 9, 10)

            self.assertEqual(
                store.set_pending(
                    day, day.replace(hour=18, minute=5), day.replace(hour=18)
                ),
                (True, None),
            )
            self.assertEqual(
                store.start(day, day.replace(hour=18, minute=6)), (True, None)
            )
            self.assertEqual(
                store.pause(day, day.replace(hour=19, minute=0)), (True, None)
            )
            entry = store.get(day)
            self.assertEqual(entry["paused_at"], "2026-09-10T19:00:00")
            self.assertEqual(entry["paused_seconds"], 0)
            ok, error = store.pause(day, day.replace(hour=19, minute=5))
            self.assertFalse(ok)
            self.assertIn("이미 일시정지", error or "")
            self.assertEqual(
                store.resume(day, day.replace(hour=19, minute=30)), (True, None)
            )
            entry = store.get(day)
            self.assertIsNone(entry["paused_at"])
            self.assertEqual(entry["paused_seconds"], 1800)
            self.assertEqual(
                store.pause(day, day.replace(hour=20, minute=0)), (True, None)
            )
            self.assertEqual(
                store.complete(day, day.replace(hour=21, minute=0)), (True, None)
            )
            entry = store.get(day)
            self.assertEqual(entry["status"], "completed")
            self.assertIsNone(entry["paused_at"])
            self.assertEqual(entry["paused_seconds"], 5400)
            self.assertTrue(entry["flex_pending"])

            reloaded = OvertimeStateStore(path)
            self.assertEqual(reloaded.snapshot(), store.snapshot())

    def test_pause_resume_require_matching_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OvertimeStateStore(Path(tmp) / "overtime.json")
            day = datetime(2026, 9, 10)
            ok, error = store.pause(day, day.replace(hour=19))
            self.assertFalse(ok)
            self.assertIn("진행 중인", error or "")
            ok, error = store.resume(day, day.replace(hour=19))
            self.assertFalse(ok)

            self.assertEqual(
                store.set_pending(
                    day, day.replace(hour=18, minute=5), day.replace(hour=18)
                ),
                (True, None),
            )
            self.assertEqual(
                store.start(day, day.replace(hour=18, minute=6)), (True, None)
            )
            ok, error = store.resume(day, day.replace(hour=19))
            self.assertFalse(ok)
            self.assertIn("일시정지 중인", error or "")
            self.assertEqual(
                store.pause(day, day.replace(hour=19)), (True, None)
            )
            ok, error = store.resume(day, day.replace(hour=18, minute=30))
            self.assertFalse(ok)
            self.assertIn("재개 시간", error or "")

    def test_update_started_adjusts_active_start_with_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OvertimeStateStore(Path(tmp) / "overtime.json")
            day = datetime(2026, 9, 10)
            self.assertEqual(
                store.set_pending(
                    day, day.replace(hour=18, minute=5), day.replace(hour=18)
                ),
                (True, None),
            )
            self.assertEqual(
                store.start(day, day.replace(hour=18, minute=30)), (True, None)
            )
            self.assertEqual(
                store.update_started(day, day.replace(hour=18, minute=0)),
                (True, None),
            )
            self.assertEqual(
                store.get(day)["started_at"], "2026-09-10T18:00:00"
            )
            ok, error = store.update_started(
                day, day.replace(hour=18, minute=10)
            )
            self.assertTrue(ok)
            ok, error = store.update_started(
                day, day + timedelta(days=1)
            )
            self.assertFalse(ok)
            self.assertIn("날짜", error or "")
            store.pause(day, day.replace(hour=19))
            ok, error = store.update_started(day, day.replace(hour=19, minute=30))
            self.assertFalse(ok)
            self.assertIn("시작 시간", error or "")

    def test_completed_entry_stays_flex_pending_until_marked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            store = OvertimeStateStore(path)
            day = datetime(2026, 9, 10)
            self.assertEqual(
                store.set_pending(
                    day, day.replace(hour=18, minute=5), day.replace(hour=18)
                ),
                (True, None),
            )
            self.assertEqual(
                store.start(day, day.replace(hour=18, minute=6)), (True, None)
            )
            self.assertEqual(
                store.complete(day, day.replace(hour=20, minute=6)), (True, None)
            )
            self.assertTrue(store.get(day)["flex_pending"])
            self.assertEqual(store.flex_pending_days(), ["2026-09-10"])

            reloaded = OvertimeStateStore(path)
            self.assertEqual(reloaded.flex_pending_days(), ["2026-09-10"])

            self.assertEqual(
                store.mark_flex_registered(day), (True, None)
            )
            entry = store.get(day)
            self.assertEqual(entry["status"], "completed")
            self.assertFalse(entry["flex_pending"])
            self.assertEqual(store.flex_pending_days(), [])

            ok, error = store.mark_flex_registered(day)
            self.assertFalse(ok)
            self.assertIn("이미", error or "")

    def test_flex_pending_lists_multiple_completed_days_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OvertimeStateStore(Path(tmp) / "overtime.json")
            for offset in (2, 1, 0):
                day = datetime(2026, 9, 10) - timedelta(days=offset)
                self.assertEqual(
                    store.set_pending(
                        day,
                        day.replace(hour=18, minute=5),
                        day.replace(hour=18),
                    ),
                    (True, None),
                )
                self.assertEqual(
                    store.start(day, day.replace(hour=18, minute=6)),
                    (True, None),
                )
                self.assertEqual(
                    store.complete(day, day.replace(hour=19, minute=6)),
                    (True, None),
                )
            self.assertEqual(
                store.flex_pending_days(),
                ["2026-09-10", "2026-09-09", "2026-09-08"],
            )
            store.mark_flex_registered("2026-09-09")
            self.assertEqual(
                store.flex_pending_days(), ["2026-09-10", "2026-09-08"]
            )

    def test_update_completed_edits_interval_and_keeps_pending_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OvertimeStateStore(Path(tmp) / "overtime.json")
            day = datetime(2026, 9, 10)
            self.assertEqual(
                store.set_pending(
                    day, day.replace(hour=18, minute=5), day.replace(hour=18)
                ),
                (True, None),
            )
            self.assertEqual(
                store.start(day, day.replace(hour=18, minute=6)), (True, None)
            )
            self.assertEqual(
                store.pause(day, day.replace(hour=19)), (True, None)
            )
            self.assertEqual(
                store.resume(day, day.replace(hour=19, minute=30)), (True, None)
            )
            self.assertEqual(
                store.complete(day, day.replace(hour=20, minute=6)), (True, None)
            )

            self.assertEqual(
                store.update_completed(
                    day,
                    day.replace(hour=18, minute=30),
                    day.replace(hour=20, minute=45),
                ),
                (True, None),
            )
            entry = store.get(day)
            self.assertEqual(entry["started_at"], "2026-09-10T18:30:00")
            self.assertEqual(entry["ended_at"], "2026-09-10T20:45:00")
            self.assertEqual(entry["paused_seconds"], 1800)
            self.assertTrue(entry["flex_pending"])

            ok, error = store.update_completed(
                day, day.replace(hour=20), day.replace(hour=19)
            )
            self.assertFalse(ok)
            self.assertIn("종료", error or "")
            ok, error = store.update_completed(
                day, day.replace(hour=18) + timedelta(days=1), day.replace(hour=20)
            )
            self.assertFalse(ok)
            ok, error = store.update_completed(
                "2026-09-11",
                day.replace(hour=18),
                day.replace(hour=20),
            )
            self.assertFalse(ok)
            self.assertIn("완료된", error or "")

    def test_flex_pending_field_is_strictly_decoded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            base = {
                "status": "completed",
                "detected_at": "2026-09-10T18:05:00",
                "scheduled_quit": "2026-09-10T18:00:00",
                "assigned_minutes": 0,
                "started_at": "2026-09-10T18:06:00",
                "ended_at": "2026-09-10T19:06:00",
                "paused_at": None,
                "paused_seconds": 0,
                "flex_pending": True,
            }
            for mutated in (
                {**base, "flex_pending": "yes"},
                {**base, "status": "skipped", "started_at": None, "ended_at": None},
            ):
                path.write_text(
                    json.dumps(
                        {"state_version": STATE_VERSION, "days": {"2026-09-10": mutated}}
                    ),
                    encoding="utf-8",
                )
                store = OvertimeStateStore(path)
                self.assertIsNone(store.get("2026-09-10"))

    def test_v2_state_file_is_upgraded_with_flex_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            completed = {
                "status": "completed",
                "detected_at": "2026-09-10T18:05:00",
                "scheduled_quit": "2026-09-10T18:00:00",
                "assigned_minutes": 15,
                "started_at": "2026-09-10T18:06:00",
                "ended_at": "2026-09-10T19:06:00",
                "paused_at": None,
                "paused_seconds": 0,
            }
            skipped = {
                "status": "skipped",
                "detected_at": "2026-09-09T18:05:00",
                "scheduled_quit": "2026-09-09T18:00:00",
                "assigned_minutes": 0,
                "started_at": None,
                "ended_at": None,
                "paused_at": None,
                "paused_seconds": 0,
            }
            path.write_text(
                json.dumps(
                    {
                        "state_version": 2,
                        "days": {"2026-09-10": completed, "2026-09-09": skipped},
                    }
                ),
                encoding="utf-8",
            )
            store = OvertimeStateStore(path)
            self.assertEqual(store.flex_pending_days(), ["2026-09-10"])
            self.assertTrue(store.get("2026-09-10")["flex_pending"])
            self.assertFalse(store.get("2026-09-09")["flex_pending"])

            store.mark_flex_registered("2026-09-10")
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["state_version"], STATE_VERSION)

    def test_v2_file_with_v3_only_fields_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            bad = {
                "state_version": 2,
                "days": {
                    "2026-09-10": {
                        "status": "completed",
                        "detected_at": "2026-09-10T18:05:00",
                        "scheduled_quit": "2026-09-10T18:00:00",
                        "assigned_minutes": 0,
                        "started_at": "2026-09-10T18:06:00",
                        "ended_at": "2026-09-10T19:06:00",
                        "paused_at": None,
                        "paused_seconds": 0,
                        "flex_pending": False,
                    }
                },
            }
            path.write_text(json.dumps(bad), encoding="utf-8")
            store = OvertimeStateStore(path)
            self.assertIsNone(store.get("2026-09-10"))

    def test_v1_state_file_is_upgraded_and_rewritten_as_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            legacy = {
                "state_version": 1,
                "days": {
                    "2026-09-10": {
                        "status": "active",
                        "detected_at": "2026-09-10T18:05:00",
                        "scheduled_quit": "2026-09-10T18:00:00",
                        "assigned_minutes": 30,
                        "started_at": "2026-09-10T18:06:00",
                        "ended_at": None,
                    }
                },
            }
            path.write_text(json.dumps(legacy), encoding="utf-8")
            store = OvertimeStateStore(path)
            entry = store.get("2026-09-10")
            self.assertEqual(entry["status"], "active")
            self.assertIsNone(entry["paused_at"])
            self.assertEqual(entry["paused_seconds"], 0)

            self.assertEqual(store.pause("2026-09-10", datetime(2026, 9, 10, 19)), (True, None))
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["state_version"], STATE_VERSION)
            self.assertEqual(
                raw["days"]["2026-09-10"]["paused_at"], "2026-09-10T19:00:00"
            )

    def test_v1_file_with_v2_only_fields_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overtime.json"
            bad = {
                "state_version": 1,
                "days": {
                    "2026-09-10": {
                        "status": "active",
                        "detected_at": "2026-09-10T18:05:00",
                        "scheduled_quit": "2026-09-10T18:00:00",
                        "assigned_minutes": 0,
                        "started_at": "2026-09-10T18:06:00",
                        "ended_at": None,
                        "paused_at": "2026-09-10T19:00:00",
                        "paused_seconds": 5,
                    }
                },
            }
            path.write_text(json.dumps(bad), encoding="utf-8")
            store = OvertimeStateStore(path)
            self.assertIsNone(store.get("2026-09-10"))
            self.assertFalse(store.pause("2026-09-10", datetime(2026, 9, 10, 19))[0])
