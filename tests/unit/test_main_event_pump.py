import queue
import unittest

from src.utils.ui_event_pump import SharedUiEventPump


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls: list[tuple[int, object]] = []

    def after(self, delay_ms, callback):
        self.after_calls.append((int(delay_ms), callback))
        return len(self.after_calls)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def advance_ms(self, milliseconds: float) -> None:
        self.value += milliseconds / 1000.0


class SharedUiEventPumpUnitTest(unittest.TestCase):
    def test_preserves_fifo_across_split_passes(self) -> None:
        root = _FakeRoot()
        event_queue: queue.SimpleQueue = queue.SimpleQueue()
        clock = _FakeClock()
        seen: list[int] = []

        for value in range(5):
            event_queue.put(lambda value=value: seen.append(value))

        pump = SharedUiEventPump(
            root=root,
            event_queue=event_queue,
            monotonic=clock.monotonic,
            callback_budget=2,
        )

        pump.run_pass()
        self.assertEqual(seen, [0, 1])
        self.assertEqual(root.after_calls[-1][0], 1)

        root.after_calls[-1][1]()
        self.assertEqual(seen, [0, 1, 2, 3])

        root.after_calls[-1][1]()
        self.assertEqual(seen, [0, 1, 2, 3, 4])

    def test_stops_at_callback_budget(self) -> None:
        root = _FakeRoot()
        event_queue: queue.SimpleQueue = queue.SimpleQueue()
        seen: list[int] = []

        for value in range(20):
            event_queue.put(lambda value=value: seen.append(value))

        pump = SharedUiEventPump(root=root, event_queue=event_queue)

        pump.run_pass()

        self.assertEqual(seen, list(range(16)))
        self.assertEqual(root.after_calls[-1][0], 1)

    def test_stops_at_time_budget(self) -> None:
        root = _FakeRoot()
        event_queue: queue.SimpleQueue = queue.SimpleQueue()
        clock = _FakeClock()
        seen: list[int] = []

        for value in range(5):
            def _callback(value=value) -> None:
                seen.append(value)
                clock.advance_ms(5)

            event_queue.put(_callback)

        pump = SharedUiEventPump(
            root=root,
            event_queue=event_queue,
            monotonic=clock.monotonic,
        )

        pump.run_pass()

        self.assertEqual(seen, [0, 1])
        self.assertEqual(root.after_calls[-1][0], 1)

    def test_rearms_quickly_when_backlog_remains(self) -> None:
        root = _FakeRoot()
        event_queue: queue.SimpleQueue = queue.SimpleQueue()

        for _ in range(17):
            event_queue.put(lambda: None)

        pump = SharedUiEventPump(root=root, event_queue=event_queue)

        pump.run_pass()

        self.assertEqual(root.after_calls[-1][0], 1)

    def test_rearms_idle_when_queue_is_empty(self) -> None:
        root = _FakeRoot()
        event_queue: queue.SimpleQueue = queue.SimpleQueue()
        seen: list[str] = []
        event_queue.put(lambda: seen.append("done"))

        pump = SharedUiEventPump(root=root, event_queue=event_queue)

        pump.run_pass()

        self.assertEqual(seen, ["done"])
        self.assertEqual(root.after_calls[-1][0], 30)

    def test_eventually_drains_all_callbacks(self) -> None:
        root = _FakeRoot()
        event_queue: queue.SimpleQueue = queue.SimpleQueue()
        seen: list[int] = []

        for value in range(35):
            event_queue.put(lambda value=value: seen.append(value))

        pump = SharedUiEventPump(root=root, event_queue=event_queue)

        pump.run_pass()
        iterations = 0
        while root.after_calls[-1][0] == 1:
            iterations += 1
            self.assertLess(iterations, 10)
            root.after_calls[-1][1]()

        self.assertEqual(seen, list(range(35)))
        self.assertEqual(root.after_calls[-1][0], 30)


if __name__ == "__main__":
    unittest.main()
