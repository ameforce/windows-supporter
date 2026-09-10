from __future__ import annotations

from datetime import datetime
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
            self.assertEqual(store.snapshot(), {"state_version": 1, "days": {}})
            self.assertFalse(store.set_pending(
                datetime(2026, 9, 10),
                datetime(2026, 9, 10, 18),
                datetime(2026, 9, 10, 18),
            )[0])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["state_version"],
                99,
            )
