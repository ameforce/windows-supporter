import queue
import time
from collections.abc import Callable


class SharedUiEventPump:
    def __init__(
        self,
        root,
        event_queue: queue.SimpleQueue,
        *,
        callback_budget: int = 16,
        time_budget_ms: int = 8,
        backlog_rearm_ms: int = 1,
        idle_rearm_ms: int = 30,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._root = root
        self._event_queue = event_queue
        self._callback_budget = max(1, int(callback_budget))
        self._time_budget_seconds = max(0.0, float(time_budget_ms) / 1000.0)
        self._backlog_rearm_ms = int(backlog_rearm_ms)
        self._idle_rearm_ms = int(idle_rearm_ms)
        self._monotonic = monotonic or time.monotonic

    def start(self) -> None:
        self._schedule(self._idle_rearm_ms)

    def run_pass(self) -> None:
        started_at = self._monotonic()
        processed = 0

        while processed < self._callback_budget:
            try:
                callback = self._event_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break

            try:
                callback()
            except Exception:
                pass

            processed += 1
            if processed >= self._callback_budget:
                break
            if (self._monotonic() - started_at) >= self._time_budget_seconds:
                break

        self._schedule_next_pass()

    def __call__(self) -> None:
        self.run_pass()

    def _schedule_next_pass(self) -> None:
        delay_ms = (
            self._backlog_rearm_ms
            if self._has_pending_callbacks()
            else self._idle_rearm_ms
        )
        self._schedule(delay_ms)

    def _has_pending_callbacks(self) -> bool:
        try:
            return not self._event_queue.empty()
        except Exception:
            return False

    def _schedule(self, delay_ms: int) -> None:
        try:
            self._root.after(delay_ms, self)
        except Exception:
            pass
