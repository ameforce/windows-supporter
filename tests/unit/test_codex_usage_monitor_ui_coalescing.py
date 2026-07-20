import queue
import tempfile
import threading
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

    def test_external_scheduler_attach_prevents_child_timer_resume(self) -> None:
        class _Root:
            def __init__(self):
                self.after_calls = []

            def after(self, delay_ms, callback):
                self.after_calls.append((delay_ms, callback))
                return f"after-{len(self.after_calls)}"

        monitor = self._make_monitor()
        root = _Root()
        monitor.attach(root=root, event_queue=None, start_monitor=False)
        monitor._CodexUsageMonitor__enabled = True  # noqa: SLF001
        monitor._CodexUsageMonitor__session_state = "logged_in"  # noqa: SLF001

        monitor._CodexUsageMonitor__resume_background_monitor_if_needed()  # noqa: SLF001
        monitor._CodexUsageMonitor__reset_monitor_countdown_after_manual_query()  # noqa: SLF001

        self.assertEqual(root.after_calls, [])

    def test_shutdown_from_worker_posts_tk_cleanup_to_ui_queue(self) -> None:
        class _Root:
            def __init__(self):
                self.after_cancel_calls = []

            def after_cancel(self, after_id):
                self.after_cancel_calls.append(
                    (after_id, threading.current_thread().name)
                )

        class _Tooltip:
            def __init__(self):
                self.hide_threads = []

            def hide_tooltip(self):
                self.hide_threads.append(threading.current_thread().name)

        monitor = self._make_monitor()
        root = _Root()
        event_queue = queue.Queue()
        tooltip = _Tooltip()
        monitor.attach(root=root, event_queue=event_queue, start_monitor=False)
        monitor._CodexUsageMonitor__monitor_after_id = "after-monitor"  # noqa: SLF001
        monitor._CodexUsageMonitor__pending_login_after_id = "after-login"  # noqa: SLF001
        monitor._CodexUsageMonitor__active_tooltip = tooltip  # noqa: SLF001
        worker = threading.Thread(target=monitor.shutdown, name="shutdown-worker")

        worker.start()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(root.after_cancel_calls, [])
        self.assertEqual(tooltip.hide_threads, [])
        self.assertGreaterEqual(event_queue.qsize(), 3)

        while not event_queue.empty():
            event_queue.get_nowait()()

        self.assertEqual(
            {after_id for after_id, _thread in root.after_cancel_calls},
            {"after-monitor", "after-login"},
        )
        self.assertTrue(
            all(thread_name != "shutdown-worker" for _after_id, thread_name in root.after_cancel_calls)
        )
        self.assertEqual(tooltip.hide_threads, [threading.current_thread().name])


if __name__ == "__main__":
    unittest.main()
