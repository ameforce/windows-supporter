from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.apps.wrike_timelog_snapshot import (
    CACHE_VERSION,
    LEGACY_CACHE_VERSION,
    SOURCE_SCOPE_ALL_MY_TIMELOGS,
    CacheWriteBlockedError,
    TimelogDay,
    TimelogSnapshotState,
    WrikeTimelogSnapshotStore,
    apply_stale_threshold,
    error_from_last_good,
    loading_from_last_good,
    make_error_snapshot,
    make_fresh_snapshot,
)


class WrikeTimelogSnapshotUnitTest(unittest.TestCase):
    WEEK_START = date(2026, 3, 2)  # Monday
    FETCHED_AT = datetime(2026, 3, 4, 10, 30, 15)
    ACCOUNT_A = "a" * 64
    ACCOUNT_B = "b" * 64

    def _days(self, minutes: tuple[int, ...] | None = None) -> tuple[TimelogDay, ...]:
        values = minutes if minutes is not None else (0, 15, 30, 45, 60, 75, 90)
        return tuple(
            TimelogDay(
                date=self.WEEK_START + timedelta(days=index),
                recorded_minutes=value,
            )
            for index, value in enumerate(values)
        )

    def _fresh(
        self,
        *,
        minutes: tuple[int, ...] | None = None,
        fetched_at: datetime | None = None,
        generation: int = 3,
        partial: bool = False,
    ):
        return make_fresh_snapshot(
            days=self._days(minutes),
            display_name="Alice Example",
            fetched_at=fetched_at or self.FETCHED_AT,
            generation=generation,
            partial=partial,
        )

    def _payload(self) -> dict:
        snapshot = self._fresh()
        return {
            "version": CACHE_VERSION,
            "account_fingerprint": self.ACCOUNT_A,
            "days": [
                {
                    "date": item.date.isoformat(),
                    "recorded_minutes": item.recorded_minutes,
                }
                for item in snapshot.days
            ],
            "display_name": snapshot.display_name,
            "fetched_at": self.FETCHED_AT.isoformat(),
            "source_scope": SOURCE_SCOPE_ALL_MY_TIMELOGS,
        }

    def test_fresh_zero_is_distinct_from_failure_without_data(self) -> None:
        fresh = self._fresh(minutes=(0, 0, 0, 0, 0, 0, 0))
        failed = make_error_snapshot(
            generation=4,
            error_code="request_failed",
            display_name="Alice Example",
        )

        self.assertEqual(fresh.state, TimelogSnapshotState.FRESH)
        self.assertEqual(fresh.total_recorded_minutes, 0)
        self.assertEqual(fresh.recorded_minutes_for(self.WEEK_START), 0)
        self.assertTrue(fresh.has_last_good_data)
        self.assertEqual(failed.state, TimelogSnapshotState.ERROR)
        self.assertIsNone(failed.total_recorded_minutes)
        self.assertIsNone(failed.recorded_minutes_for(self.WEEK_START))
        self.assertFalse(failed.has_last_good_data)
        with self.assertRaises(FrozenInstanceError):
            fresh.generation = 99  # type: ignore[misc]

    def test_day_lookup_and_weekly_total(self) -> None:
        snapshot = self._fresh()

        thursday = self.WEEK_START + timedelta(days=3)
        self.assertEqual(snapshot.get_day(thursday), TimelogDay(thursday, 45))
        self.assertEqual(snapshot.recorded_minutes_for(thursday), 45)
        self.assertEqual(snapshot.total_recorded_minutes, 315)
        self.assertIsNone(
            snapshot.get_day(self.WEEK_START + timedelta(days=7))
        )

    def test_loading_and_error_transitions_retain_last_good(self) -> None:
        last_good = self._fresh(partial=True)

        loading = loading_from_last_good(last_good, generation=4)
        failed = error_from_last_good(
            last_good,
            generation=5,
            error_code="network_timeout",
        )

        self.assertEqual(loading.state, TimelogSnapshotState.LOADING)
        self.assertEqual(loading.generation, 4)
        self.assertIsNone(loading.error_code)
        self.assertEqual(loading.days, last_good.days)
        self.assertEqual(loading.fetched_at, last_good.fetched_at)
        self.assertTrue(loading.partial)
        self.assertEqual(failed.state, TimelogSnapshotState.ERROR)
        self.assertEqual(failed.generation, 5)
        self.assertEqual(failed.error_code, "network_timeout")
        self.assertEqual(failed.total_recorded_minutes, 315)
        self.assertEqual(failed.display_name, last_good.display_name)
        self.assertTrue(failed.partial)

    def test_stale_threshold_uses_naive_local_age_and_boundary(self) -> None:
        cached = self._fresh()
        threshold = timedelta(minutes=30)

        still_fresh = apply_stale_threshold(
            cached,
            now=self.FETCHED_AT + threshold - timedelta(microseconds=1),
            stale_after=threshold,
        )
        stale = apply_stale_threshold(
            cached,
            now=self.FETCHED_AT + threshold,
            stale_after=threshold,
        )

        self.assertEqual(still_fresh.state, TimelogSnapshotState.FRESH)
        self.assertEqual(stale.state, TimelogSnapshotState.STALE)
        self.assertEqual(stale.days, cached.days)
        with self.assertRaises(ValueError):
            apply_stale_threshold(
                cached,
                now=self.FETCHED_AT.replace(tzinfo=timezone.utc),
                stale_after=threshold,
            )
        with self.assertRaises(ValueError):
            apply_stale_threshold(
                cached,
                now=self.FETCHED_AT,
                stale_after=timedelta(seconds=-1),
            )

    def test_models_validate_minutes_local_datetime_and_monday_week(self) -> None:
        with self.assertRaises(ValueError):
            TimelogDay(self.WEEK_START, -1)
        with self.assertRaises(ValueError):
            TimelogDay(self.WEEK_START, True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            make_fresh_snapshot(
                days=self._days()[:6],
                display_name="Alice Example",
                fetched_at=self.FETCHED_AT,
                generation=1,
            )
        with self.assertRaises(ValueError):
            make_fresh_snapshot(
                days=tuple(
                    TimelogDay(
                        self.WEEK_START + timedelta(days=index + 1),
                        index,
                    )
                    for index in range(7)
                ),
                display_name="Alice Example",
                fetched_at=self.FETCHED_AT,
                generation=1,
            )
        duplicated = list(self._days())
        duplicated[1] = TimelogDay(self.WEEK_START, 15)
        with self.assertRaises(ValueError):
            make_fresh_snapshot(
                days=duplicated,
                display_name="Alice Example",
                fetched_at=self.FETCHED_AT,
                generation=1,
            )
        with self.assertRaises(ValueError):
            make_fresh_snapshot(
                days=self._days(),
                display_name="Alice Example",
                fetched_at=self.FETCHED_AT.replace(tzinfo=timezone.utc),
                generation=1,
            )

    def test_cache_round_trip_has_minimal_strict_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "caller-appdata" / "wrike-timelog.json"
            store = WrikeTimelogSnapshotStore(path)
            original = self._fresh()

            store.save(original, account_fingerprint=self.ACCOUNT_A)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload),
                {
                    "version",
                    "account_fingerprint",
                    "days",
                    "display_name",
                    "fetched_at",
                    "source_scope",
                },
            )
            self.assertEqual(payload["version"], CACHE_VERSION)
            self.assertEqual(payload["account_fingerprint"], self.ACCOUNT_A)
            self.assertEqual(payload["fetched_at"], self.FETCHED_AT.isoformat())
            self.assertEqual(payload["source_scope"], "all_my_timelogs")
            self.assertTrue(
                all(set(item) == {"date", "recorded_minutes"} for item in payload["days"])
            )
            serialized = path.read_text(encoding="utf-8").lower()
            self.assertNotIn('"token"', serialized)
            self.assertNotIn('"task_title"', serialized)
            self.assertNotIn('"url"', serialized)

            restored = WrikeTimelogSnapshotStore(path).load(
                expected_account_fingerprint=self.ACCOUNT_A,
                generation=12,
            )
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.days, original.days)
            self.assertEqual(restored.display_name, original.display_name)
            self.assertEqual(restored.fetched_at, original.fetched_at)
            self.assertEqual(restored.state, TimelogSnapshotState.FRESH)
            self.assertEqual(restored.generation, 12)
            self.assertFalse(restored.partial)

            stale = WrikeTimelogSnapshotStore(path).load_with_freshness(
                expected_account_fingerprint=self.ACCOUNT_A,
                now=self.FETCHED_AT + timedelta(hours=2),
                stale_after=timedelta(hours=1),
                generation=13,
            )
            self.assertIsNotNone(stale)
            assert stale is not None
            self.assertEqual(stale.state, TimelogSnapshotState.STALE)
            self.assertEqual(stale.generation, 13)
            self.assertIsNone(
                WrikeTimelogSnapshotStore(path).load(
                    expected_account_fingerprint=self.ACCOUNT_B,
                )
            )

    def test_legacy_unbound_cache_is_ignored_but_can_be_replaced_by_bound_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            legacy = self._payload()
            legacy["version"] = LEGACY_CACHE_VERSION
            legacy.pop("account_fingerprint")
            path.write_text(
                json.dumps(legacy, ensure_ascii=False),
                encoding="utf-8",
            )
            store = WrikeTimelogSnapshotStore(path)

            self.assertIsNone(
                store.load(expected_account_fingerprint=self.ACCOUNT_A)
            )
            self.assertFalse(store.write_blocked)
            replacement = self._fresh(minutes=(1, 2, 3, 4, 5, 6, 7))
            store.save(replacement, account_fingerprint=self.ACCOUNT_A)

            restored = WrikeTimelogSnapshotStore(path).load(
                expected_account_fingerprint=self.ACCOUNT_A
            )
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.days, replacement.days)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["version"],
                CACHE_VERSION,
            )

    def test_partial_snapshot_is_not_promoted_to_persistent_last_good(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            store = WrikeTimelogSnapshotStore(path)

            with self.assertRaises(ValueError):
                store.save(
                    self._fresh(partial=True),
                    account_fingerprint=self.ACCOUNT_A,
                )

            self.assertFalse(path.exists())

    def test_malformed_and_unknown_cache_are_ignored_and_write_blocked(self) -> None:
        cases = {
            "malformed": b'{"version":',
            "unknown-version": json.dumps(
                {**self._payload(), "version": CACHE_VERSION + 1},
                sort_keys=True,
            ).encode("utf-8"),
        }
        replacement = self._fresh(minutes=(1, 1, 1, 1, 1, 1, 1))

        for name, original_bytes in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "cache.json"
                path.write_bytes(original_bytes)
                store = WrikeTimelogSnapshotStore(path)

                self.assertIsNone(
                    store.load(expected_account_fingerprint=self.ACCOUNT_A)
                )
                self.assertTrue(store.write_blocked)
                self.assertIsNotNone(store.write_blocked_reason)
                with self.assertRaises(CacheWriteBlockedError):
                    store.save(
                        replacement,
                        account_fingerprint=self.ACCOUNT_A,
                    )
                self.assertEqual(path.read_bytes(), original_bytes)

    def test_strict_cache_validation_rejects_noncanonical_or_invalid_values(self) -> None:
        invalid_payloads: dict[str, dict] = {}

        noncanonical_date = self._payload()
        noncanonical_date["days"][0]["date"] = "20260302"
        invalid_payloads["noncanonical-date"] = noncanonical_date

        noncanonical_datetime = self._payload()
        noncanonical_datetime["fetched_at"] = "2026-03-04 10:30:15"
        invalid_payloads["noncanonical-datetime"] = noncanonical_datetime

        negative_minutes = self._payload()
        negative_minutes["days"][0]["recorded_minutes"] = -1
        invalid_payloads["negative-minutes"] = negative_minutes

        bool_minutes = self._payload()
        bool_minutes["days"][0]["recorded_minutes"] = True
        invalid_payloads["bool-minutes"] = bool_minutes

        duplicate_date = self._payload()
        duplicate_date["days"][1]["date"] = duplicate_date["days"][0]["date"]
        invalid_payloads["duplicate-date"] = duplicate_date

        short_week = self._payload()
        short_week["days"] = short_week["days"][:6]
        invalid_payloads["short-week"] = short_week

        for name, payload in invalid_payloads.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "cache.json"
                original = json.dumps(payload, sort_keys=True).encode("utf-8")
                path.write_bytes(original)
                store = WrikeTimelogSnapshotStore(path)

                self.assertIsNone(
                    store.load(expected_account_fingerprint=self.ACCOUNT_A)
                )
                self.assertTrue(store.write_blocked)
                self.assertEqual(path.read_bytes(), original)

    def test_atomic_replace_failure_preserves_previous_cache_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            store = WrikeTimelogSnapshotStore(path)
            store.save(self._fresh(), account_fingerprint=self.ACCOUNT_A)
            original_bytes = path.read_bytes()
            replacement = self._fresh(
                minutes=(1, 2, 3, 4, 5, 6, 7),
                fetched_at=self.FETCHED_AT + timedelta(minutes=5),
                generation=4,
            )

            with patch(
                "src.apps.wrike_timelog_snapshot.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.save(
                        replacement,
                        account_fingerprint=self.ACCOUNT_A,
                    )

            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(list(Path(tmp).glob(f".{path.name}.*.tmp")), [])
            self.assertFalse(store.write_blocked)


if __name__ == "__main__":
    unittest.main()
