"""Real Chromium check of the Flex login window against a local fixture.

The fixture imitates Flex: the work record redirects to ``/auth/login`` until a
session cookie exists, and the login page sets that cookie after a short delay
(the employee logging in).  The browser runs headless so no window appears;
the worker still treats the client as its headed login window.
"""

from __future__ import annotations

import http.server
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from src.apps.Wrike import Wrike
from src.apps.flex_worktime import FlexBrowserClient


WORK_PATH = "/time-tracking/my-work-record"


def _handler(*, auto_login: bool, interstitial: bool = False):
    after_login = "/select-company" if interstitial else WORK_PATH

    class _FlexFixture(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = urlsplit(self.path).path
            if path == "/select-company":
                # A post-login interstitial outside /auth with no password
                # field: not the finished login yet.
                self._send(
                    200,
                    "<html><head><title>회사 선택</title></head><body>"
                    "<h1>회사를 선택하세요</h1>"
                    "<script>setTimeout(function () {"
                    f"location.href = '{WORK_PATH}';"
                    "}, 2500);</script></body></html>",
                )
                return
            if path == WORK_PATH:
                if "flex_session=ok" in str(self.headers.get("Cookie") or ""):
                    self._send(
                        200,
                        "<html><head><title>Flex</title></head><body>"
                        "<h1>내 근무 기록</h1><p>09:00 - 18:00</p></body></html>",
                    )
                    return
                self.send_response(302)
                self.send_header("Location", f"/auth/login?nextUrl={WORK_PATH}")
                self.end_headers()
                return
            if path == "/auth/login":
                script = (
                    "<script>setTimeout(function () {"
                    "document.cookie = 'flex_session=ok; path=/';"
                    f"location.href = '{after_login}';"
                    "}, 600);</script>"
                    if auto_login
                    else ""
                )
                self._send(
                    200,
                    "<html><head><title>flex 로그인</title></head><body>"
                    "<form><input type='email'><input type='password'></form>"
                    f"{script}</body></html>",
                )
                return
            self._send(404, "<html><body>not found</body></html>")

        def _send(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return _FlexFixture


class FlexLoginWindowChromiumTest(unittest.TestCase):
    def _serve(self, *, auto_login: bool, interstitial: bool = False) -> str:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _handler(auto_login=auto_login, interstitial=interstitial),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}{WORK_PATH}"

    def _profile_dir(self) -> str:
        # Chromium can hold profile files for a moment after closing.
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        return temp.name

    @staticmethod
    def _wait_for(predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return bool(predicate())

    def test_client_sees_the_login_finish_in_a_real_browser(self) -> None:
        work_url = self._serve(auto_login=True)
        client = FlexBrowserClient(
            self._profile_dir(), headless=True, work_url=work_url
        )
        self.addCleanup(client.close)

        client.open_login_page(activate=False)

        self.assertFalse(client.login_completed())
        self.assertTrue(self._wait_for(client.login_completed, 15.0))
        self.assertEqual(urlsplit(client._page.url).path, WORK_PATH)

    def test_post_login_interstitial_is_not_a_finished_login(self) -> None:
        work_url = self._serve(auto_login=True, interstitial=True)
        client = FlexBrowserClient(
            self._profile_dir(), headless=True, work_url=work_url
        )
        self.addCleanup(client.close)
        client.open_login_page(activate=False)
        seen: list = []

        def completed() -> bool:
            done = client.login_completed()
            seen.append((urlsplit(client._page.url).path, done))
            return done

        self.assertTrue(self._wait_for(completed, 20.0))
        self.assertIn("/select-company", [path for path, _done in seen])
        self.assertEqual(
            {path for path, done in seen if done},
            {WORK_PATH},
        )

    def _worker(self, work_url: str, client_cls=FlexBrowserClient):
        app = Wrike.__new__(Wrike)
        app._Wrike__flex_browser_queue = queue.Queue()
        app._Wrike__flex_browser_profile_dir = self._profile_dir()
        app._Wrike__time_log_login_timeout_sec = 10.0
        app._Wrike__flex_browser_stop_event = threading.Event()
        app._Wrike__ensure_playwright_ready = lambda: True
        app._Wrike__log_exception = lambda *_args: None
        events: list = []
        app._Wrike__notify_flex_window_event = (
            lambda event, mode, detail=None: events.append(
                (event, mode, dict(detail or {}))
            )
        )
        created: list = []

        def factory(profile_dir, **kwargs):
            kwargs["work_url"] = work_url
            # The worker asks for a headed window; the test keeps it hidden.
            kwargs["headless"] = True
            client = client_cls(profile_dir, **kwargs)
            created.append(client)
            return client

        patches = [
            patch("src.apps.Wrike.FlexBrowserClient", factory),
            patch("src.apps.Wrike.FLEX_WINDOW_WATCH_INTERVAL_SEC", 0.2),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        worker = threading.Thread(
            target=app._Wrike__flex_browser_worker_loop, daemon=True
        )
        worker.start()

        def stop() -> None:
            response = queue.Queue(maxsize=1)
            app._Wrike__flex_browser_queue.put(("close", None, response))
            worker.join(timeout=15.0)

        self.addCleanup(stop)
        return app, events, created

    @staticmethod
    def _submit(app, kind, payload=None, timeout: float = 30.0):
        response = queue.Queue(maxsize=1)
        app._Wrike__flex_browser_queue.put((kind, payload, response))
        return response.get(timeout=timeout)

    def test_worker_closes_the_login_window_when_the_login_finishes(self) -> None:
        app, events, created = self._worker(self._serve(auto_login=True))

        result = self._submit(app, "login", {"source": "periodic", "activate": False})

        self.assertEqual(result, (True, "opened"))
        self.assertTrue(
            self._wait_for(
                lambda: any(event == "logged_in" for event, _m, _d in events), 20.0
            )
        )
        self.assertEqual(
            [event[:2] for event in events],
            [("opened", "login"), ("logged_in", "login")],
        )
        self.assertIsNone(created[0]._context)

    def test_worker_reports_a_closed_login_window(self) -> None:
        class _ClosingClient(FlexBrowserClient):
            """Closes its pages from the worker thread, like the employee."""

            checks = 0

            def has_open_page(self) -> bool:
                type(self).checks += 1
                if type(self).checks == 3 and self._context is not None:
                    for page in list(self._context.pages):
                        page.close()
                return super().has_open_page()

        app, events, created = self._worker(
            self._serve(auto_login=False), _ClosingClient
        )

        result = self._submit(app, "login", {"source": "manual", "activate": False})

        self.assertEqual(result, (True, "opened"))
        self.assertTrue(
            self._wait_for(
                lambda: any(event == "closed" for event, _m, _d in events), 20.0
            )
        )
        self.assertEqual(
            [event[:2] for event in events],
            [("opened", "login"), ("closed", "login")],
        )
        self.assertIsNone(created[0]._context)


if __name__ == "__main__":
    unittest.main()
