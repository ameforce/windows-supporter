import queue
import tempfile
import unittest

from src.apps.codex_usage_monitor import CodexUsageMonitor, UsageSnapshot


class CodexUsageMonitorUiCoalescingTest(unittest.TestCase):
    def _make_monitor(self) -> CodexUsageMonitor:
        self.addCleanup(getattr(self, "_tempdir").cleanup)
        return CodexUsageMonitor(
            config_dir=self._tempdir.name,
            profile_dir=self._tempdir.name,
        )

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()

    def test_ui_post_coalesced_enqueues_single_callback_and_preserves_order(self) -> None:
        monitor = self._make_monitor()
        event_queue = queue.Queue()
        monitor.attach(root=None, event_queue=event_queue)

        calls: list[str] = []

        monitor._CodexUsageMonitor__ui_post_coalesced(  # noqa: SLF001
            lambda: calls.append("first"),
            lambda: calls.append("second"),
        )

        self.assertEqual(event_queue.qsize(), 1)

        posted = event_queue.get_nowait()
        posted()

        self.assertEqual(calls, ["first", "second"])

    def test_manual_query_collect_guarded_coalesces_pause_and_progress_post(self) -> None:
        monitor = self._make_monitor()
        event_queue = queue.Queue()
        monitor.attach(root=None, event_queue=event_queue)

        calls: list[str] = []
        monitor._CodexUsageMonitor__set_monitor_state = lambda *_args, **_kwargs: None  # noqa: SLF001
        monitor._CodexUsageMonitor__acquire_collect_lock_non_blocking = lambda: True  # noqa: SLF001
        monitor._CodexUsageMonitor__collect_snapshot = (  # noqa: SLF001
            lambda source="": (UsageSnapshot(), None)
        )
        monitor._CodexUsageMonitor__pause_monitor_countdown_for_manual_query = (  # noqa: SLF001
            lambda: calls.append("pause")
        )
        monitor._CodexUsageMonitor__reset_monitor_countdown_after_manual_query = (  # noqa: SLF001
            lambda: calls.append("reset")
        )

        snapshot, error = monitor._CodexUsageMonitor__collect_snapshot_guarded(  # noqa: SLF001
            "manual_query",
            on_acquired=lambda: calls.append("progress"),
        )

        self.assertIsNotNone(snapshot)
        self.assertIsNone(error)
        self.assertEqual(event_queue.qsize(), 1)

        posted = event_queue.get_nowait()
        posted()

        self.assertEqual(calls, ["pause", "progress"])


if __name__ == "__main__":
    unittest.main()
