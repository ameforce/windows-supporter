# v0.8.0 AI 사용량 릴리스

## 변경 개요

- 사용자 노출 기능명을 `AI 사용량`으로 통일하고 Codex 전용 구조를 provider-neutral 프로필 구조로 확장한다.
- 지원 provider는 `codex`, `cursor`이며 전체 프로필 슬롯과 작업표시줄 표시 선택은 각각 최대 2개다.
- `Codex/Codex`, `Codex/Cursor`, `Cursor/Codex`, `Cursor/Cursor` 조합과 표시 순서를 지원한다.
- 기존 `codex_usage` route, getter, class, import는 호환 facade와 alias로 유지한다.

## 설정과 마이그레이션

- 기본 설정은 `ai_usage_settings.json`의 schema v3로 원자적 저장한다.
- 기존 `codex_usage_multi_settings.json` schema v2는 최초 실행 시 읽어 v3로 무손실 변환한다.
- 기존 Codex 자식 설정, 상태, Playwright profile 경로는 그대로 사용한다.
- rollback을 위해 `codex_usage_multi_settings.json`에 Codex 전용 v2 mirror를 계속 원자적 저장한다. Cursor 슬롯으로 전환해도 마지막 Codex label과 활성화 설정을 보존한다.
- v3 설정과 legacy mirror의 읽기/쓰기, 재시작, 반복 마이그레이션은 멱등성을 유지한다.

## Cursor 수집 경계

- Windows Supporter가 관리하는 별도 persistent browser profile에서 사용자가 직접 로그인한다.
- `https://cursor.com/dashboard/usage`의 화면에 보이는 현재 사용자 상단 요약만 낮은 빈도로 확인한다.
- 수집 값은 Included usage의 사용액/포함 한도와 비율, reset, On-Demand 상태다. 값이 없으면 `0`으로 대체하지 않고 조회 불가 상태로 유지한다.
- 기존 Chrome 또는 Cursor IDE의 쿠키·세션 파일은 읽거나 복사하지 않는다.
- private/internal API, usage events 표, 다른 팀원 데이터, CAPTCHA·Cloudflare 우회, rate-limit 회피는 사용하지 않는다.
- 표·row·grid 또는 이를 포함하는 상위 컨테이너, `Usage Events`, 이메일 형식이 포함된 후보는 실패-폐쇄로 제외한다.
- Cursor Terms의 자동 수집 제한에 따른 계정 제한·정지 가능성은 사용자가 명시적으로 인지하고 수용한 범위다. 수집 빈도는 기본 600초, 최소 300초로 제한한다.

## 안정성

- Cursor 수집도 Codex의 별도 worker process, command timeout, page/worker recycle, crash 복구 경계를 재사용한다.
- provider별 timeout, 로그인 필요, DOM drift, rate-limit, crash는 다른 provider 갱신을 막지 않는다.
- 마지막 성공값은 최소 필드만 원자적으로 저장하며 실패 시 `stale`과 실제 실패 원인을 함께 노출한다. 원문 DOM, 계정 식별자, 쿠키는 저장하지 않는다.
- 작업표시줄 render signature는 provider, profile, metric, freshness, status를 포함한다. 동일 상태는 다시 그리지 않고 상태 변경 시 한 번만 갱신한다.
- `TaskbarCreated` 시 native owner를 다시 결합하고 기존 geometry, DPI, multi-monitor, fullscreen hide 경로를 유지한다.

## 검증과 롤백

- provider 조합, 0/1/2 표시, 제3 프로필 거부, 순서, v2→v3 마이그레이션, legacy alias, 실패 격리, stale/rate-limit/login/timeout/crash/recycle, render fast path를 자동 테스트한다.
- native Tk 캡처는 표준 폭과 좁은 폭, mixed ready, 긴 한글/영문 label, Cursor logged-out, stale/rate-limited 상태를 저장소 밖 evidence로 검증한다.
- 실제 Windows에서는 작업표시줄 owner 재결합, multi-monitor, fullscreen hide, DPI, Explorer 재생성, repaint/CPU churn을 확인한다.
- 문제가 발생하면 v0.7.9 실행 파일과 legacy v2 mirror로 되돌린다. v3 파일과 Cursor 전용 profile/state는 삭제하지 않고 보존해 재업그레이드 시 재사용한다.
