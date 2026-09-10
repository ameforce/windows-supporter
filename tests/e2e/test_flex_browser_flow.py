from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from urllib.parse import urlsplit

from src.apps.flex_worktime import FlexBrowserClient


class _FlexFixtureHandler(BaseHTTPRequestHandler):
    schedule = {
        "userWorkSchedules": [
            {
                "employeeNumber": "E-42",
                "days": [
                    {
                        "date": "2026-09-10",
                        "workBlocks": [
                            {
                                "type": "WORK_RECORD",
                                "formName": "기본 근무",
                                "blockFrom": "2026-09-10T09:00:00",
                                "blockTo": "2026-09-10T18:00:00",
                            },
                            {
                                "type": "WORK_RECORD",
                                "formName": "연장 근무",
                                "blockFrom": "2026-09-10T18:00:00",
                                "blockTo": "2026-09-10T20:00:00",
                            },
                        ],
                    }
                ],
            }
        ]
    }

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.endswith("/api/work-schedule"):
            # Deliberately exceed the old fixed 1.2-second wait.  The client
            # must wait for parseable schedule content, not for elapsed time.
            time.sleep(1.8)
            body = json.dumps(self.schedule).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError):
                return
            return
        if path.endswith("/time-tracking/my-work-record"):
            body = """<!doctype html>
            <html lang='ko'>
              <head>
                <meta name='viewport' content='width=device-width, initial-scale=1'>
                <style>
                  body { font-family: sans-serif; margin: 0; background: #f3f4f6; }
                  main { max-width: 720px; margin: 24px auto; padding: 24px; background: white; }
                  h1 { font-size: 22px; }
                  #status { padding: 12px; background: #eff6ff; }
                </style>
              </head>
              <body>
                <main>
                  <h1>Flex 근무 기록</h1>
                  <p>2026.09.10</p>
                  <p id='status'>근무 정보 불러오는 중...</p>
                  <p id='schedule' aria-live='polite'></p>
                </main>
                <script>
                  fetch('/api/work-schedule')
                    .then(response => response.json())
                    .then(data => {
                      const blocks = data.userWorkSchedules[0].days[0].workBlocks;
                      document.querySelector('#status').textContent = '근무 정보 확인됨';
                      document.querySelector('#schedule').textContent =
                        blocks.map(block => block.formName + ': ' + block.blockFrom.slice(11, 16) + ' - ' + block.blockTo.slice(11, 16)).join(' / ');
                    });
                </script>
              </body>
            </html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return


@unittest.skipUnless(
    os.environ.get("RUN_FLEX_BROWSER_E2E") == "1",
    "set RUN_FLEX_BROWSER_E2E=1 to run the Playwright browser-flow check",
)
class FlexBrowserFlowE2ETests(unittest.TestCase):
    def test_browser_session_reads_schedule_and_keeps_layout_usable(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FlexFixtureHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        profile_dir = Path(tempfile.mkdtemp(prefix="windows-supporter-flex-profile-"))
        artifact_dir = Path(
            os.environ.get(
                "FLEX_E2E_ARTIFACT_DIR",
                str(Path(tempfile.gettempdir()) / "windows-supporter" / "artifacts" / "e2e"),
            )
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        client = FlexBrowserClient(
            str(profile_dir),
            work_url=(
                f"http://127.0.0.1:{server.server_address[1]}"
                "/time-tracking/my-work-record"
            ),
            headless=True,
            timeout_ms=10_000,
            login_timeout_sec=10,
        )
        try:
            page = client._get_page()
            page.set_viewport_size({"width": 1280, "height": 800})
            client.open_work_record_page()
            page.screenshot(path=str(artifact_dir / "flex-desktop-initial.png"), full_page=True)

            result = client.fetch_schedule_period(
                date(2026, 9, 7),
                date(2026, 9, 13),
                now=datetime(2026, 9, 10, 19, 0),
                return_metadata=True,
            )
            page.screenshot(path=str(artifact_dir / "flex-desktop-loaded.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(artifact_dir / "flex-mobile-final.png"), full_page=True)

            self.assertEqual(result.employee_number, "E-42")
            schedule = result.schedules[date(2026, 9, 10)]
            self.assertEqual(schedule.regular_quit.strftime("%H:%M"), "18:00")
            self.assertEqual(schedule.overtime_scheduled_quit.strftime("%H:%M"), "20:00")
            self.assertIn("근무 정보 확인됨", page.locator("body").inner_text())
            self.assertIn("연장 근무: 18:00 - 20:00", page.locator("body").inner_text())
            for screenshot in (
                "flex-desktop-initial.png",
                "flex-desktop-loaded.png",
                "flex-mobile-final.png",
            ):
                self.assertTrue((artifact_dir / screenshot).is_file())
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            shutil.rmtree(profile_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
