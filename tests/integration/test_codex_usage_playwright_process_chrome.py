from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from playwright.sync_api import sync_playwright

from src.apps.codex_usage_browser_types import PlaywrightSessionConfig
from src.apps.codex_usage_browser_types import BrowserErrorCode
from src.apps.codex_usage_playwright_session import CodexUsagePlaywrightSession


class _ServerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.probe_requests = 0
        self.usage_requests = 0
        self.hang_probe_number = 2
        self.release_first_probe = threading.Event()


def _handler_factory(state: _ServerState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/usage"):
                is_seed_request = self.path.endswith("?seed=1")
                usage_request_number = 0
                if not is_seed_request:
                    with state.lock:
                        state.usage_requests += 1
                        usage_request_number = state.usage_requests
                body = b"<!doctype html><title>Usage fixture</title><main>usage</main>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if usage_request_number == 1:
                    self.send_header(
                        "Set-Cookie",
                        "session-only=preserved; Path=/; HttpOnly; SameSite=Lax",
                    )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/probe":
                with state.lock:
                    state.probe_requests += 1
                    request_number = state.probe_requests
                if request_number == state.hang_probe_number:
                    state.release_first_probe.wait(30.0)
                cookie_header = self.headers.get("Cookie", "")
                payload = json.dumps(
                    {
                        "url": "/usage",
                        "mainText": "usage limit",
                        "metricBlocks": [{"metric_key": "weekly_limit"}],
                        "planType": (
                            "session-cookie-preserved"
                            if "session-only=preserved" in cookie_header
                            else "session-cookie-missing"
                        ),
                    }
                ).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_error(404)

    return Handler


PROBE_SCRIPT = """
async () => {
  const response = await fetch('/probe');
  const payload = await response.json();
  payload.profileName = localStorage.getItem('session-marker') || '';
  payload.accountId = document.cookie.includes('session-cookie=preserved')
    ? 'cookie-preserved'
    : '';
  payload.url = location.href;
  return payload;
}
"""


class CodexUsagePlaywrightProcessChromeIntegrationTest(unittest.TestCase):
    def test_actual_renderer_crash_is_classified_and_worker_is_reaped(self) -> None:
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as profile_dir:
            config = PlaywrightSessionConfig(
                profile_dir=profile_dir,
                usage_url="chrome://crash",
                probe_script="() => ({ url: location.href, metricBlocks: [] })",
                navigation_timeout_ms=5_000,
                command_timeout_sec=8.0,
                collect_timeout_sec=8.0,
                timeout_retry_delays_sec=(),
                timeout_recovery_grace_sec=2.0,
            )
            session = CodexUsagePlaywrightSession(config, logs.append)
            try:
                result = session.collect()
            finally:
                session.shutdown()

        self.assertEqual(
            result.error,
            BrowserErrorCode.RENDERER_CRASHED.value,
            logs,
        )
        self.assertTrue(any("browser page crashed" in line for line in logs), logs)
        self.assertTrue(
            any("reason=renderer_crashed" in line for line in logs), logs
        )

    def test_never_resolving_evaluate_is_killed_and_retry_reuses_profile(self) -> None:
        state = _ServerState()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(state))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        usage_url = f"http://127.0.0.1:{server.server_port}/usage"
        logs: list[str] = []
        try:
            with tempfile.TemporaryDirectory() as profile_dir:
                self._seed_profile(profile_dir, usage_url)
                config = PlaywrightSessionConfig(
                    profile_dir=profile_dir,
                    usage_url=usage_url,
                    probe_script=PROBE_SCRIPT,
                    navigation_timeout_ms=5_000,
                    command_timeout_sec=8.0,
                    collect_timeout_sec=8.0,
                    timeout_retry_delays_sec=(0.0,),
                    timeout_recovery_grace_sec=2.0,
                    worker_cleanup_timeout_sec=1.0,
                    worker_bootstrap_timeout_sec=10.0,
                )
                session = CodexUsagePlaywrightSession(config, logs.append)
                started_at = time.monotonic()
                try:
                    first = session.collect()
                    result = session.collect()
                finally:
                    session.shutdown()

                self.assertIsNotNone(first.probe)
                if first.probe is None:
                    self.fail("initial collect did not capture browser session state")
                self.assertEqual(
                    first.probe.get("planType"),
                    "session-cookie-preserved",
                )
                self.assertLess(time.monotonic() - started_at, 20.0)
                self.assertIsNotNone(result.probe)
                if result.probe is None:
                    self.fail("retry did not produce a usage probe")
                self.assertEqual(result.probe.get("profileName"), "preserved")
                self.assertEqual(result.probe.get("accountId"), "cookie-preserved")
                self.assertEqual(
                    result.probe.get("planType"),
                    "session-cookie-preserved",
                    "a session-only HttpOnly cookie must survive worker replacement",
                )
                self.assertGreaterEqual(state.probe_requests, 3)
                self.assertGreaterEqual(
                    sum("browser worker ready" in line for line in logs),
                    2,
                    logs,
                )
                self.assertTrue(
                    any("stage=evaluate_probe" in line for line in logs),
                    logs,
                )
                self.assertTrue(any("hard cancel" in line for line in logs), logs)
        finally:
            state.release_first_probe.set()
            server.shutdown()
            server.server_close()

    def test_planned_worker_recycle_preserves_session_only_cookie(self) -> None:
        state = _ServerState()
        state.hang_probe_number = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(state))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        usage_url = f"http://127.0.0.1:{server.server_port}/usage"
        logs: list[str] = []
        try:
            with tempfile.TemporaryDirectory() as profile_dir:
                self._seed_profile(profile_dir, usage_url)
                config = PlaywrightSessionConfig(
                    profile_dir=profile_dir,
                    usage_url=usage_url,
                    probe_script=PROBE_SCRIPT,
                    navigation_timeout_ms=5_000,
                    command_timeout_sec=8.0,
                    collect_timeout_sec=8.0,
                    timeout_retry_delays_sec=(),
                    timeout_recovery_grace_sec=2.0,
                    worker_cleanup_timeout_sec=1.0,
                    worker_bootstrap_timeout_sec=10.0,
                    worker_recycle_success_count=1,
                )
                session = CodexUsagePlaywrightSession(config, logs.append)
                try:
                    first = session.collect()
                    second = session.collect()
                finally:
                    session.shutdown()

            self.assertIsNotNone(first.probe)
            self.assertIsNotNone(second.probe)
            if second.probe is None:
                self.fail("planned recycle did not return a usage probe")
            self.assertEqual(
                second.probe.get("planType"),
                "session-cookie-preserved",
            )
            self.assertTrue(
                any("reason=success_count" in line for line in logs),
                logs,
            )
        finally:
            state.release_first_probe.set()
            server.shutdown()
            server.server_close()

    def _seed_profile(self, profile_dir: str, usage_url: str) -> None:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                profile_dir,
                channel="chrome",
                headless=True,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"{usage_url}?seed=1", wait_until="domcontentloaded")
                page.evaluate(
                    """
                    () => {
                      localStorage.setItem('session-marker', 'preserved');
                      document.cookie = 'session-cookie=preserved; path=/; max-age=3600; SameSite=Lax';
                    }
                    """
                )
            finally:
                context.close()


if __name__ == "__main__":
    unittest.main()
