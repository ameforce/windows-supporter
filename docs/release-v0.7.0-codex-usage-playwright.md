# v0.7.0 Codex usage Playwright 운영 계약

## 수집 경로

Codex usage는 계정별 `CodexUsagePlaywrightSession`의 전용 daemon thread에서만 브라우저를 다룹니다. 이 thread가 Playwright runtime, persistent `BrowserContext`, `Page`의 생성부터 종료까지 전 수명주기를 소유합니다.

- 브라우저는 Playwright가 설치된 Google Chrome을 `channel="chrome"`으로 실행합니다.
- 프로필은 기존 `chatgpt-profile-account-1`, `chatgpt-profile-account-2`를 그대로 사용합니다.
- 자동 조회와 수동 사용량 조회는 headless context만 사용합니다.
- 명시적인 로그인 요청만 headed context를 열며, 로그인 확인 후 headed context를 닫고 headless context로 복귀합니다.
- Codex usage에는 bundled Chromium, raw CDP, WebSocket, 직접 프로세스 실행, Win32 창 제어 fallback이 없습니다.
- 빌드에 포함된 bundled Chromium은 Wrike 등 다른 기능의 기존 경로를 위한 것이며 Codex usage에서는 사용하지 않습니다.

## 상태와 복구

브라우저 상태는 `stopped`, `starting`, `headless_ready`, `headed_login`, `recovering`, `profile_in_use`, `failed` 중 하나입니다. UI에는 `browser_state`, `login_window_open`, `browser_last_error`만 전달합니다.

- 같은 usage URL이면 `reload`, 다른 URL이면 `goto`합니다.
- 정상 수집에서는 context와 page를 재사용합니다.
- 실패 시 page를 한 번, context를 한 번만 재생성합니다.
- 다른 Chrome이 같은 프로필을 점유하면 `profile_in_use`로 자동 조회를 중지합니다. Chrome을 탐색하거나 종료하지 않으며 사용자가 수동으로 다시 조회할 때만 재시도합니다.
- 설치된 Chrome channel이 없으면 `browser_channel_unavailable`을 표시하고 마지막 성공 snapshot을 유지합니다. bundled Chromium으로 전환하지 않습니다.
- 로그인 창은 최대 15분 동안 확인합니다. 창이 닫히거나 시간이 지나면 context를 닫고 `logged_out`으로 돌아갑니다.

## 검증

릴리스 후보와 clean tagged main에서 다음 순서로 검증합니다.

1. `uv run python -m unittest tests.unit.test_codex_usage_browser_architecture tests.unit.test_codex_usage_playwright_driver tests.unit.test_codex_usage_playwright_session`
2. `uv run python -m unittest discover -s tests -p "test_*.py"`
3. 변경 파일 `ruff check`
4. `uv run python -m compileall src tests`
5. `git diff --check`
6. `cmd /c build.bat`

실제 운영 승인에서는 account 1의 `captured_at` 증가, 연속 성공 로그, 화면 표시나 포커스 탈취 부재, account 2 미기동, 프로필 Chrome orphan 부재를 함께 확인합니다.
