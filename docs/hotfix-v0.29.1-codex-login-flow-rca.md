# v0.29.1 Codex 계정 연결 로그인 흐름 RCA

## 분류 판정

이번 변경은 새 사용량 기능을 추가하는 release가 아니라, 기존 Codex 연결 계약을 복구하는 hotfix다.

- 의도한 계약: WS의 Codex 연결을 누르면 로그인 화면이 바로 열리고, 로그인 완료 뒤 사용량 페이지로 자동 이동해 사용량을 수집한다.
- 현재 동작: 연결 시 `#usage` 페이지가 먼저 열려 사용자가 우측 상단 로그인 버튼을 눌러야 한다. 로그인 후 ChatGPT 홈으로 이동하면 WS가 인증 완료를 확정하지 못해 사용자가 연결 버튼을 다시 눌러야 한다.
- 차이: 로그인 진입과 로그인 완료 후 usage 복귀가 브라우저 세션 계약으로 연결되지 않았다.
- 판정: 기존 의도한 연결·수집 동작의 누락과 오판을 복구하므로 `hotfix/v0.29.1`로 분류한다.

## RCA

### 1. 로그인 진입 URL이 생성되지만 실제 세션에 주입되지 않음

`build_codex_login_entry_url()`은 이미 `auth/login?next=...#usage` URL을 만들고 있었고 단위 테스트도 있었지만, `CodexUsageMonitor`가 만든 `PlaywrightSessionConfig`에 전달되지 않았다. `open_login()`은 설정에 로그인 대상이 없으므로 `usage_url`로 직접 이동했고, 결과적으로 사용자가 페이지의 로그인 버튼을 한 번 더 눌러야 했다.

### 2. 홈 화면의 일반 문구가 인증 완료로 오인됨

기존 `_probe_is_authenticated()`는 로그인 문구가 없고 본문에 `usage`, `limit`, `사용`, `한도` 중 하나가 있으면 인증 완료로 판정했다. 로그인 후 홈 화면도 이런 일반 문구를 포함할 수 있으므로, 실제 usage 페이지에 도착하지 않았는데 성공으로 처리할 수 있었다.

### 3. 오인 판정 때문에 기존 복귀 로직이 실행되지 않음

로그인 polling은 인증으로 보이지 않을 때만 `_recover_post_login_landing()`을 호출했다. 홈 화면이 2번 조건으로 인증처럼 보이면 이 복귀 로직을 건너뛰고 홈 probe를 상위 monitor에 반환한다. 상위 monitor는 usage URL이 아닌 probe를 snapshot으로 만들 수 없어 로그인 상태를 다시 요구했고, 사용자가 연결 버튼을 재실행해야 했다.

## 수정된 계약

- `PlaywrightSessionConfig.login_url`을 추가하고 Codex monitor가 usage URL에서 로그인 entry URL을 매번 파생해 세션과 worker에 전달한다.
- `open_login()`은 로그인 entry URL로 바로 이동한다. 이미 인증된 profile이면 usage로 즉시 통과하고, 홈으로 착지하면 usage URL로 이동해 수집한다.
- 로그인 완료 성공 조건을 본문 키워드에서 분리했다. probe URL이 설정된 usage URL과 동등하고, `remaining_credit`만이 아닌 실제 사용량 metric block이 있어야만 성공한다.
- `poll_login()`은 login/auth 경로, Google/OpenAI OAuth 경로, 로그인 문구가 남아 있는 동안 headed 로그인 창을 유지한다.
- 로그인 완료 후 홈 등 비-usage 화면으로 착지하면 자동으로 usage URL로 이동하고, 유효한 metric이 준비될 때까지 기다린다. 유효한 usage probe를 얻기 전에는 headed context를 닫거나 headless 수집으로 전환하지 않는다.

## 검증 범위

- Codex driver·monitor 단위 검증: 87 tests, `OK`
  - direct auth entry URL 이동
  - 홈 화면의 `Usage and limits` 문구 오인 방지
  - OAuth 중간 화면 steering 금지
  - usage URL 및 metric 준비 후 headless 전환
  - monitor → browser session login URL 전달
- session/process/boundary 검증: 33 tests, `OK`
- 통합 targeted 검증: 120 tests, `OK`

검증은 저장소가 요구하는 `uv 0.10.2`를 격리 실행해 수행했다. 전체 unittest discover, 실제 계정 자격 증명을 사용하는 브라우저 E2E, native UI launch는 이번 비-UI 런타임 계약 변경의 최소 검증 범위에 포함하지 않았다.

## 검증 한계

실제 ChatGPT 계정의 로그인·OAuth callback·사용량 DOM은 자격 증명과 외부 세션 상태에 의존하므로 targeted fake browser로 상태 전이를 증명했다. release 후에는 실제 Codex profile에서 `auth/login` 진입 → 로그인 완료 → `codex/cloud/settings/analytics#usage` 도착 → 사용량 수집 결과를 한 번 확인해야 한다.
