"""The worker process routes the sign-in commands to the driver."""

from __future__ import annotations

import threading
import unittest
from multiprocessing import Pipe
from typing import Any
from unittest.mock import patch

from src.apps.codex_usage_browser_types import (
    BrowserOperationResult,
    BrowserRuntimeStatus,
    BrowserState,
    PlaywrightSessionConfig,
)
from src.apps.codex_usage_playwright_worker import run_playwright_worker


class _Driver:
    calls: list[str] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def open_api_credit_login(self) -> BrowserOperationResult:
        self.calls.append("open_api_credit_login")
        return BrowserOperationResult(error="login_required")

    def poll_api_credit_login(self) -> BrowserOperationResult:
        self.calls.append("poll_api_credit_login")
        return BrowserOperationResult(error="login_window_closed")

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def get_runtime_status(self) -> BrowserRuntimeStatus:
        return BrowserRuntimeStatus(BrowserState.STOPPED, False, "")

    def export_session_cookies(self) -> list[dict[str, Any]]:
        return []


class WorkerApiCreditCommandTest(unittest.TestCase):
    def test_sign_in_commands_reach_the_driver_and_answer(self) -> None:
        _Driver.calls = []
        parent, child = Pipe()
        with patch(
            "src.apps.codex_usage_playwright_worker.CodexUsagePlaywrightDriver", _Driver
        ):
            worker = threading.Thread(target=run_playwright_worker, args=(child,), daemon=True)
            worker.start()
            parent.send(("bootstrap", PlaywrightSessionConfig("profile", "url", "probe"), 1, []))
            self.assertEqual(parent.recv()[0], "ready")
            answers = []
            for request_id, command in enumerate(
                ("open_api_credit_login", "poll_api_credit_login", "shutdown")
            ):
                parent.send(("command", request_id, command))
                answers.append(parent.recv())
            worker.join(5.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(
            _Driver.calls,
            ["open_api_credit_login", "poll_api_credit_login", "shutdown"],
        )
        self.assertEqual(answers[0][0:2], ("result", 0))
        self.assertEqual(answers[0][2].error, "login_required")
        self.assertEqual(answers[1][2].error, "login_window_closed")


if __name__ == "__main__":
    unittest.main()
