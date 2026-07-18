from __future__ import annotations

import unittest
from unittest.mock import patch

from src.apps.codex_usage_browser_types import (
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    PlaywrightSessionConfig,
)
from src.apps.cursor_usage_playwright_worker import run_cursor_playwright_worker


class _Connection:
    def __init__(self, incoming: list[object]) -> None:
        self.incoming = list(incoming)
        self.sent: list[object] = []

    def recv(self) -> object:
        return self.incoming.pop(0)

    def send(self, message: object) -> None:
        self.sent.append(message)


class _Driver:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.shutdown_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def export_session_cookies(self) -> list[dict[str, object]]:
        raise AssertionError("Cursor worker must never export cookies")


class CursorUsagePlaywrightWorkerUnitTest(unittest.TestCase):
    def test_worker_ignores_cookie_slot_and_returns_no_cookie_payload(self) -> None:
        config = PlaywrightSessionConfig(
            profile_dir="C:/app-owned/cursor",
            usage_url="https://cursor.com/dashboard/usage",
            probe_script="probe",
        )
        connection = _Connection(
            [
                ("bootstrap", config, 7, [{"name": "must-not-import"}]),
                ("command", 1, "shutdown"),
            ]
        )

        with patch(
            "src.apps.cursor_usage_playwright_worker.CursorUsagePlaywrightDriver",
            _Driver,
        ):
            run_cursor_playwright_worker(connection)  # type: ignore[arg-type]

        self.assertEqual(connection.sent[0][0], "ready")  # type: ignore[index]
        result_message = connection.sent[1]
        self.assertIsInstance(result_message, tuple)
        assert isinstance(result_message, tuple)
        self.assertEqual(result_message[0], "result")
        self.assertEqual(len(result_message), 4)
        self.assertIsInstance(result_message[2], BrowserOperationResult)


if __name__ == "__main__":
    unittest.main()
