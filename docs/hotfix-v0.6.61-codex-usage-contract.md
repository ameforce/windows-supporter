# v0.6.61 Codex usage 데이터 계약 hotfix

## 재현

2026-07-13 09:48 KST에 같은 계정과 reset window를 비교했다.

- ChatGPT Codex analytics live DOM: 주간 `97% remaining`, 5시간 항목 없음, 주간 reset `2026-07-20 04:01 KST`
- windows-supporter persisted/UI snapshot: 5시간 `0%`, 주간 `97%`, 새 `captured_at`
- Windows native Codex rollout: 주간 `4~6% used`, 즉 `96~94% remaining`, `window_minutes=10080`, `secondary=null`, reset `2026-07-20 04:01:12 KST`
- WSL Codex는 `/home/enmso/.local/bin/codex`와 `/home/enmso/.codex`를 사용하는 별도 설치·세션이며 현재값 근거에서 제외했다.

## 근본 원인

세 가지 데이터 계약 결함이 겹쳤다.

1. partial snapshot merge가 현재 DOM에 없는 metric을 이전 snapshot에서 복사하면서 새 `captured_at`을 부여했다. 과거 5시간 `0%`가 현재값처럼 계속 표시됐다.
2. limit parser가 `N% used`, `N% remaining`, `used / limit`의 의미를 보존하지 않고 숫자 문자열만 저장했다. taskbar는 모든 퍼센트를 remaining으로 해석했다.
3. 웹 analytics는 진행 중인 Codex session rate-limit event보다 늦게 갱신될 수 있었다. 또한 현재 Codex payload는 `primary=5시간`, `secondary=주간`이 아니라 `primary.window_minutes=10080`, `secondary=null`이었다.

## 수정

- live semantic blocks에서 현재 보고된 metric key를 snapshot provenance로 전달한다.
- 현재 페이지에 없는 metric은 이전 값으로 backfill하지 않는다. 페이지에 label이 존재하지만 값 파싱만 일시 실패한 경우의 기존 backfill은 유지한다.
- 명시적 used percentage와 legacy used/limit ratio를 parser/cache 경계에서 remaining percentage로 정규화한다.
- Windows native `CODEX_HOME`의 최신 rollout `token_count.payload.rate_limits`를 읽고 `window_minutes`로 5시간/주간을 판별한다.
- web/local capture 시각이 5분 이내이고 reset 시각이 2분 이내로 일치할 때만 local Codex 값을 해당 웹 계정 snapshot에 결합한다. WSL 또는 다른 계정/reset은 결합하지 않는다.
- local payload가 두 시간창을 보고하면 두 reset이 모두 일치해야 결합한다. 한 시간창만 일치하는 partial match는 계정/세션 오염 가능성이 있으므로 web snapshot을 유지한다.
- rollout timestamp는 timezone-aware ISO 값만 허용하고, 세션 시작 날짜와 무관하게 최근 수정된 rollout 16개를 검사한다. 탐색 중 사라진 파일은 해당 후보만 건너뛴다.

## 영향 범위

- Codex usage 설정 화면, tooltip, taskbar overlay가 같은 canonical remaining-percentage snapshot을 사용한다.
- 여러 웹 계정 중 Windows Codex와 reset window가 일치하지 않는 계정은 기존 web snapshot을 유지한다.
- local rollout이 없거나 오래됐거나 포맷이 인식되지 않으면 web 경로를 유지한다.
- 기존 로그인, profile binding, Playwright/CDP 수집, Spark metric, credits 동작은 유지한다.

## 기존 검증이 놓친 이유

- DOM fixture는 bare `80%`, `68%` 숫자 추출만 검사했고 used/remaining qualifier를 검증하지 않았다.
- overlay 테스트는 입력 퍼센트가 이미 remaining이라고 가정했다.
- partial merge 테스트는 누락 field 보존만 검사했고 “현재 source에서 metric 자체가 제거된 경우”를 구분하지 않았다.
- web DOM, persisted cache, Windows Codex rollout을 같은 시점에 연결한 회귀 시나리오가 없었다.

## 디버깅 runtime audit

1. **stale cache 가설 — 확정.** live DOM에는 5시간 metric이 없었지만 persisted snapshot에는 과거 `0%`와 새 `captured_at`이 함께 있었다. field-level backfill이 source 부재를 파싱 실패로 취급했다.
2. **percentage 의미 반전 가설 — 확정.** parser가 `used`, `remaining`, `used / limit` qualifier를 제거했고 표시 계층은 모든 숫자를 remaining으로 간주했다. explicit-used와 ratio 회귀 테스트가 수정 전 각각 반전된 값을 재현했다.
3. **Windows/WSL 또는 다른 계정 선택 가설 — 배제.** 실행 중 앱은 Windows native `CODEX_HOME`과 main physical worktree EXE를 사용했다. WSL은 별도 binary/home/session이었다. web과 Windows rollout의 capture 시각 및 weekly reset이 일치했고, 다른 reset fixture는 local 보정을 거부했다.
4. **API/CLI 포맷 및 analytics 지연 가설 — 확정.** 실제 payload는 `primary.window_minutes=10080`, `secondary=null`이었고 web analytics가 rollout보다 낮은 used 값을 보였다. 위치가 아니라 `window_minutes`로 시간창을 판별하고 동일 reset/time에서 local event를 authoritative current 값으로 사용했다.

## 검증

- 신규 RED/GREEN 회귀: absent metric stale backfill, explicit used percentage, used/limit ratio, legacy cache migration, zero-used boundary, window mapping, reset/account matching, timezone-less timestamp, partial reset match, older-start active session, transient file race
- Codex usage 관련 481 tests 통과
- 전체 734 tests 통과
- Ruff changed-file lint 통과
- 신규 adapter와 테스트 basedpyright `0 errors, 0 warnings`
- 실제 raw CDP 사용자 경로에서 weekly `94%`, 5시간 미제공, reset `04:01:12`를 얻었고 같은 시점 Windows Codex rollout의 `6% used`, 10080분, 동일 reset과 일치함을 확인했다.
