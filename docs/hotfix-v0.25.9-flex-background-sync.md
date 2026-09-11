# Hotfix v0.25.9 — Flex 백그라운드 동기화

## RCA와 분류

### 의도한 계약

Flex 근무정보 동기화는 저장된 앱 전용 브라우저 세션을 읽어야 하며, 동기화 중 사용자의 포커스를 빼앗거나 브라우저 창을 노출하지 않아야 한다. 사용자가 로그인해야 하는 경우에만 `Flex 웹 열기`를 통해 명시적으로 headed 브라우저를 연다. 동기화가 끝나면 기존 설정 화면의 상태와 완료 툴팁으로 결과를 알린다.

### 현재 동작

자동 polling은 headless 세션을 사용했지만, 설정 화면의 `Flex 로그인 · 지금 동기화`가 `interactive=True`를 전달했다. worker가 이를 `headless=False`로 변환하면서 headed Chromium을 띄웠고, 콘텐츠 readiness를 기다린 뒤 성공하면 컨텍스트를 닫고 완료 툴팁을 표시했다.

### 차이와 근본 원인

로그인용 headed 브라우저와 근무정보 읽기용 동기화 job이 같은 `interactive` 플래그와 worker 경로에 결합되어 있었다. 성공 후 창을 닫는 동작은 수명주기 문제를 가렸을 뿐, 동기화가 headed로 시작되는 원인을 제거하지 못했다.

### 판정

새 사용자 capability나 제품 정책을 추가하지 않고 기존 백그라운드 동기화 계약을 복구하는 변경이므로 `hotfix/v0.25.9`로 분류한다.

## 개선 내용

- `sync` job은 호출 경로의 payload와 무관하게 항상 headless Chromium을 사용한다.
- `sync` job은 성공·실패 모두 컨텍스트를 닫아 백그라운드 실행 경계를 보장한다.
- `open` job만 headed Chromium을 유지하며, 최초 로그인·로그인 만료 시 명시적 로그인 표면으로 사용한다.
- `FlexBrowserClient`의 기본값을 headless로 바꾸고, 로그인 오류 안내를 `Flex 웹 열기` 후 재동기화 흐름으로 정정한다.
- 설정 화면의 안내와 진행 상태를 실제 백그라운드 동작에 맞춘다.

## 검증 계약

- 수동 동기화 요청에 `interactive` 옵션이 더 이상 전달되지 않는다.
- worker가 sync payload의 기존 interactive 값과 무관하게 headless client를 생성한다.
- sync 실패 시에도 headless context를 닫고, headed context는 `open` 명령에서만 유지한다.
- 로그인되지 않은 headless session은 창을 자동으로 띄우지 않고 `Flex 웹 열기`를 안내한다.
- Flex parser와 browser-flow의 기존 readiness 계약은 유지한다.

전체 unittest discovery, 실제 사용자 Flex 계정 로그인, 운영 계정 기반 browser E2E는 이 hotfix의 직접 영향 범위를 벗어나므로 별도 실행하지 않는다. 변경된 worker/client 계약과 Flex browser flow만 targeted validation 대상으로 삼는다.
