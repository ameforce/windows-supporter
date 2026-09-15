from __future__ import annotations

import ctypes
import unittest
from ctypes import wintypes
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from src.apps import worktime_activity
from src.apps.worktime_activity import (
    LASTINPUTINFO,
    LastInputUnavailableError,
    WindowsLastInputProvider,
    WorktimeActivityWatcher,
)


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls: list[tuple[str, int, object]] = []
        self.cancelled: list[str] = []
        self._next_id = 0

    def after(self, delay, callback):
        self._next_id += 1
        after_id = f"after-{self._next_id}"
        self.after_calls.append((after_id, delay, callback))
        return after_id

    def after_cancel(self, after_id) -> None:
        self.cancelled.append(after_id)
        self.after_calls = [item for item in self.after_calls if item[0] != after_id]

    def run_next(self) -> None:
        _after_id, _delay, callback = self.after_calls.pop(0)
        callback()


class _MutableProvider:
    def __init__(self, tick: int) -> None:
        self.tick = tick
        self.calls = 0

    def get_last_input_tick(self) -> int:
        self.calls += 1
        return self.tick


class WorktimeActivityWatcherTests(unittest.TestCase):
    def test_start_uses_baseline_then_reports_each_new_tick_once(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(100)
        moments = iter(
            [
                datetime(2026, 4, 6, 9, 1),
                datetime(2026, 4, 6, 9, 2),
            ]
        )
        seen = []
        watcher = WorktimeActivityWatcher(
            root,
            seen.append,
            provider=provider,
            now=lambda: next(moments),
            poll_interval_ms=250,
        )

        watcher.start()
        watcher.start()
        self.assertTrue(watcher.is_running)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(root.after_calls), 1)
        self.assertEqual(root.after_calls[0][1], 250)

        root.run_next()
        self.assertEqual(seen, [])
        provider.tick = 101
        root.run_next()
        self.assertEqual(seen, [datetime(2026, 4, 6, 9, 1)])

        root.run_next()
        self.assertEqual(len(seen), 1)
        provider.tick = 102
        root.run_next()
        self.assertEqual(
            seen,
            [
                datetime(2026, 4, 6, 9, 1),
                datetime(2026, 4, 6, 9, 2),
            ],
        )
        self.assertEqual(len(root.after_calls), 1)

    def test_first_successful_tick_after_unavailable_start_is_only_a_baseline(self) -> None:
        class _SequenceProvider:
            def __init__(self) -> None:
                self.values = [OSError("unavailable"), 40, 41]

            def get_last_input_tick(self):
                value = self.values.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

        root = _FakeRoot()
        seen = []
        watcher = WorktimeActivityWatcher(
            root,
            seen.append,
            provider=_SequenceProvider(),
            now=lambda: datetime(2026, 4, 6, 9, 5),
        )

        watcher.start()
        root.run_next()
        self.assertEqual(seen, [])
        root.run_next()
        self.assertEqual(seen, [datetime(2026, 4, 6, 9, 5)])

    def test_stop_is_idempotent_and_stale_callback_cannot_fire_or_reschedule(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(100)
        seen = []
        watcher = WorktimeActivityWatcher(
            root,
            seen.append,
            provider=provider,
            now=lambda: datetime(2026, 4, 6, 9, 10),
        )

        watcher.start()
        after_id, _delay, stale_callback = root.after_calls[0]
        provider.tick = 101
        watcher.stop()
        watcher.stop()
        self.assertFalse(watcher.is_running)
        self.assertEqual(root.cancelled, [after_id])

        stale_callback()
        self.assertEqual(seen, [])
        self.assertEqual(root.after_calls, [])

        watcher.start()
        watcher.start()
        self.assertEqual(len(root.after_calls), 1)
        root.run_next()
        self.assertEqual(seen, [])

    def test_reset_baseline_is_idempotent_and_suppresses_already_seen_input(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(10)
        seen = []
        watcher = WorktimeActivityWatcher(
            root,
            seen.append,
            provider=provider,
            now=lambda: datetime(2026, 4, 6, 9, 15),
        )

        watcher.start()
        provider.tick = 11
        watcher.reset_baseline()
        watcher.reset_baseline()
        self.assertEqual(len(root.after_calls), 1)
        root.run_next()
        self.assertEqual(seen, [])

        provider.tick = 12
        root.run_next()
        self.assertEqual(seen, [datetime(2026, 4, 6, 9, 15)])

    def test_callback_failure_does_not_stop_future_polling(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(20)
        calls = []

        def callback(now_value) -> None:
            calls.append(now_value)
            if len(calls) == 1:
                raise RuntimeError("UI closed")

        watcher = WorktimeActivityWatcher(
            root,
            callback,
            provider=provider,
            now=lambda: datetime(2026, 4, 6, 9, 20),
        )
        watcher.start()
        provider.tick = 21
        root.run_next()
        provider.tick = 22
        root.run_next()

        self.assertEqual(len(calls), 2)
        self.assertTrue(watcher.is_running)
        self.assertEqual(len(root.after_calls), 1)

    def test_idle_callback_fires_once_at_threshold_and_rearms_on_input(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(100)
        now_box = [datetime(2026, 4, 6, 19, 0)]
        seen = []
        idles = []
        watcher = WorktimeActivityWatcher(
            root,
            seen.append,
            provider=provider,
            now=lambda: now_box[0],
            poll_interval_ms=250,
            idle_callback=idles.append,
            idle_threshold_seconds=300,
        )

        watcher.start()
        # First stable read seeds the last-input moment without firing.
        root.run_next()
        self.assertEqual(idles, [])

        now_box[0] = datetime(2026, 4, 6, 19, 4)
        root.run_next()
        self.assertEqual(idles, [])

        now_box[0] = datetime(2026, 4, 6, 19, 5)
        root.run_next()
        self.assertEqual(idles, [datetime(2026, 4, 6, 19, 5)])

        # The same idle crossing is reported only once.
        now_box[0] = datetime(2026, 4, 6, 19, 10)
        root.run_next()
        self.assertEqual(len(idles), 1)

        # New input re-arms idle reporting.
        provider.tick = 101
        now_box[0] = datetime(2026, 4, 6, 19, 11)
        root.run_next()
        self.assertEqual(seen, [datetime(2026, 4, 6, 19, 11)])

        now_box[0] = datetime(2026, 4, 6, 19, 15, 59)
        root.run_next()
        self.assertEqual(len(idles), 1)
        now_box[0] = datetime(2026, 4, 6, 19, 16)
        root.run_next()
        self.assertEqual(idles, [datetime(2026, 4, 6, 19, 5), datetime(2026, 4, 6, 19, 16)])

    def test_idle_threshold_callable_disables_and_reenables_reporting(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(50)
        now_box = [datetime(2026, 4, 6, 19, 0)]
        threshold_box = [None]
        idles = []
        watcher = WorktimeActivityWatcher(
            root,
            lambda _now: None,
            provider=provider,
            now=lambda: now_box[0],
            idle_callback=idles.append,
            idle_threshold_seconds=lambda: threshold_box[0],
        )
        watcher.start()
        root.run_next()

        now_box[0] = datetime(2026, 4, 6, 20, 0)
        root.run_next()
        self.assertEqual(idles, [])

        threshold_box[0] = 60
        root.run_next()
        self.assertEqual(idles, [datetime(2026, 4, 6, 20, 0)])

    def test_idle_requires_callback_and_validates_threshold(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(50)
        with self.assertRaises(ValueError):
            WorktimeActivityWatcher(
                root,
                lambda _now: None,
                provider=provider,
                idle_callback="not-callable",
                idle_threshold_seconds=60,
            )
        with self.assertRaises(ValueError):
            WorktimeActivityWatcher(
                root,
                lambda _now: None,
                provider=provider,
                idle_callback=lambda _now: None,
                idle_threshold_seconds=0,
            )
        with self.assertRaises(ValueError):
            WorktimeActivityWatcher(
                root,
                lambda _now: None,
                provider=provider,
                idle_callback=lambda _now: None,
                idle_threshold_seconds=True,
            )

        watcher = WorktimeActivityWatcher(
            root,
            lambda _now: None,
            provider=provider,
            now=lambda: datetime(2026, 4, 6, 19, 0),
            idle_threshold_seconds=60,
        )
        watcher.start()
        root.run_next()
        root.run_next()
        self.assertTrue(watcher.is_running)
        self.assertTrue(root.after_calls)
        watcher.stop()

    def test_idle_callback_failure_is_reported_once_and_polling_continues(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(70)
        now_box = [datetime(2026, 4, 6, 19, 0)]
        calls = []

        def idle_callback(now_value) -> None:
            calls.append(now_value)
            raise RuntimeError("UI closed")

        watcher = WorktimeActivityWatcher(
            root,
            lambda _now: None,
            provider=provider,
            now=lambda: now_box[0],
            idle_callback=idle_callback,
            idle_threshold_seconds=60,
        )
        watcher.start()
        root.run_next()
        now_box[0] = datetime(2026, 4, 6, 19, 2)
        root.run_next()
        now_box[0] = datetime(2026, 4, 6, 19, 3)
        root.run_next()

        self.assertEqual(len(calls), 1)
        self.assertTrue(watcher.is_running)
        self.assertEqual(len(root.after_calls), 1)

    def test_stop_and_reset_baseline_clear_idle_state(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(80)
        now_box = [datetime(2026, 4, 6, 19, 0)]
        idles = []
        watcher = WorktimeActivityWatcher(
            root,
            lambda _now: None,
            provider=provider,
            now=lambda: now_box[0],
            idle_callback=idles.append,
            idle_threshold_seconds=60,
        )
        watcher.start()
        root.run_next()
        now_box[0] = datetime(2026, 4, 6, 19, 2)
        root.run_next()
        self.assertEqual(len(idles), 1)

        watcher.reset_baseline()
        now_box[0] = datetime(2026, 4, 6, 19, 3)
        root.run_next()
        self.assertEqual(len(idles), 1)

        watcher.stop()
        watcher.start()
        now_box[0] = datetime(2026, 4, 6, 19, 4)
        root.run_next()
        self.assertEqual(len(idles), 1)
        now_box[0] = datetime(2026, 4, 6, 19, 5)
        root.run_next()
        self.assertEqual(len(idles), 2)

    def test_non_finite_threshold_literal_is_rejected(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(90)
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                WorktimeActivityWatcher(
                    root,
                    lambda _now: None,
                    provider=provider,
                    idle_callback=lambda _now: None,
                    idle_threshold_seconds=bad,
                )

    def test_non_finite_callable_threshold_disables_idle_and_rearms(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(90)
        now_box = [datetime(2026, 4, 6, 19, 0)]
        threshold_box = [float("nan")]
        idles = []
        watcher = WorktimeActivityWatcher(
            root,
            lambda _now: None,
            provider=provider,
            now=lambda: now_box[0],
            idle_callback=idles.append,
            idle_threshold_seconds=lambda: threshold_box[0],
        )
        watcher.start()
        root.run_next()
        now_box[0] = datetime(2026, 4, 6, 19, 2)
        root.run_next()
        self.assertEqual(idles, [])
        self.assertTrue(watcher.is_running)
        self.assertTrue(root.after_calls)

        threshold_box[0] = 60.0
        now_box[0] = datetime(2026, 4, 6, 19, 4)
        root.run_next()
        self.assertEqual(len(idles), 1)
        self.assertTrue(watcher.is_running)
        self.assertTrue(root.after_calls)
        watcher.stop()

    def test_unexpected_tracking_failure_still_reschedules_polling(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(95)
        now_box = [datetime(2026, 4, 6, 19, 0)]
        watcher = WorktimeActivityWatcher(
            root,
            lambda _now: None,
            provider=provider,
            now=lambda: now_box[0],
            idle_callback=lambda _now: None,
            idle_threshold_seconds=60,
        )
        watcher.start()
        root.run_next()

        now_box[0] = datetime(2026, 4, 6, 19, 2, tzinfo=None)
        root.run_next()
        self.assertTrue(watcher.is_running)
        self.assertTrue(root.after_calls)

        aware = datetime(2026, 4, 6, 19, 4).astimezone()
        now_box[0] = aware
        root.run_next()
        self.assertTrue(watcher.is_running)
        self.assertTrue(root.after_calls)

        now_box[0] = datetime(2026, 4, 6, 19, 6)
        root.run_next()
        self.assertTrue(watcher.is_running)
        self.assertTrue(root.after_calls)
        watcher.stop()

    def test_non_datetime_now_result_does_not_stall_idle_tracking(self) -> None:
        root = _FakeRoot()
        provider = _MutableProvider(96)
        now_box = ["not-a-datetime"]
        idles = []
        watcher = WorktimeActivityWatcher(
            root,
            lambda _now: None,
            provider=provider,
            now=lambda: now_box[0],
            idle_callback=idles.append,
            idle_threshold_seconds=60,
        )
        watcher.start()
        root.run_next()
        root.run_next()
        self.assertEqual(idles, [])
        self.assertTrue(watcher.is_running)
        self.assertTrue(root.after_calls)

        now_box[0] = datetime(2026, 4, 6, 19, 0)
        root.run_next()
        now_box[0] = datetime(2026, 4, 6, 19, 2)
        root.run_next()
        self.assertEqual(len(idles), 1)
        watcher.stop()


class WindowsLastInputProviderTests(unittest.TestCase):
    def test_non_windows_guard_runs_before_loading_user32(self) -> None:
        with mock.patch.object(ctypes, "WinDLL", create=True) as win_dll:
            with self.assertRaises(LastInputUnavailableError):
                WindowsLastInputProvider(os_name="posix")
        win_dll.assert_not_called()

    def test_wrapper_uses_win_dll_signature_and_returns_tick(self) -> None:
        class _GetLastInputInfo:
            argtypes = None
            restype = None

            def __call__(self, info_pointer):
                info = ctypes.cast(
                    info_pointer,
                    ctypes.POINTER(LASTINPUTINFO),
                ).contents
                self.cb_size = int(info.cbSize)
                info.dwTime = 0xFFFFFFFE
                return 1

        api = _GetLastInputInfo()
        user32 = SimpleNamespace(GetLastInputInfo=api)
        with mock.patch.object(
            ctypes,
            "WinDLL",
            create=True,
            return_value=user32,
        ) as win_dll:
            provider = WindowsLastInputProvider(os_name="nt")
            tick = provider.get_last_input_tick()

        win_dll.assert_called_once_with("user32", use_last_error=True)
        self.assertEqual(api.argtypes, [ctypes.POINTER(LASTINPUTINFO)])
        self.assertIs(api.restype, wintypes.BOOL)
        self.assertEqual(api.cb_size, ctypes.sizeof(LASTINPUTINFO))
        self.assertEqual(tick, 0xFFFFFFFE)

    def test_false_api_result_raises_the_reported_windows_error(self) -> None:
        class _GetLastInputInfo:
            argtypes = None
            restype = None

            def __call__(self, _info_pointer):
                return 0

        user32 = SimpleNamespace(GetLastInputInfo=_GetLastInputInfo())
        with mock.patch.object(
            ctypes,
            "WinDLL",
            create=True,
            return_value=user32,
        ), mock.patch.object(
            ctypes,
            "set_last_error",
            create=True,
        ) as set_last_error, mock.patch.object(
            ctypes,
            "get_last_error",
            create=True,
            return_value=5,
        ), mock.patch.object(
            ctypes,
            "WinError",
            create=True,
            side_effect=lambda code: OSError(code, "access denied"),
        ):
            provider = WindowsLastInputProvider(os_name="nt")
            with self.assertRaises(LastInputUnavailableError) as raised:
                provider.get_last_input_tick()

        set_last_error.assert_called_once_with(0)
        self.assertEqual(raised.exception.errno, 5)
        self.assertIsInstance(raised.exception.__cause__, OSError)


if __name__ == "__main__":
    unittest.main()
