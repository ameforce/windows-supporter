# Release v0.13.0 — Quick Panel UX·private iCal 연결 진단

## 변경 분류

- 의도한 계약: `Ctrl+Alt+W`로 여는 Quick Panel은 사용자가 즉시 확인·조작할 수 있도록 전면에 표시되고, 회사 휴가 캘린더는 인증정보를 노출하지 않으면서 지원 가능한 Google 비공개 iCal 주소와 실패 원인을 구분해 안내해야 한다.
- 현재 동작: `v0.12.1`은 active hotkey 표시의 foreground 소유권을 보장하지 못했고 동기화 문구가 중복됐다. 제목 표시줄 제거, hover 외곽 강조, 오늘 목표 inline 편집, 남은 시간 표시, 매 표시 시 pointer anchoring은 기존 계약에 없었으며 계획 수정은 상세 설정 화면으로 이동했다. private iCal 실패는 로그인/HTML 응답과 일반 형식 오류를 충분히 구분하지 못했다.
- 차이: foreground 회귀·중복 정보·불완전한 iCal 오류 분류를 교정하는 범위와, 사용자가 요청한 5개의 새 Quick Panel UX 계약이 함께 존재한다.
- 판정: 기존 의도의 버그만 복원하는 patch hotfix가 아니라 사용자 가시 동작과 제품 정책을 추가하는 minor release이므로 전체 범위를 `release/v0.13.0`으로 분류한다.

## Quick Panel 계약

| # | 사용자 요구 | v0.13.0 계약 |
| --- | --- | --- |
| 1 | 창을 맨 앞으로 표시 | active hotkey 표시는 native topmost 창을 show한 뒤 foreground input queue와 bounded하게 연결해 active/focus/foreground를 요청하고 HWND를 read-back한다. 이미 보이지만 foreground가 아닌 panel은 다시 전면화하고, foreground panel에서 hotkey를 누르면 숨긴다. background activity prompt의 nonactivating 표시는 기존 foreground와 keyboard focus를 유지한다. |
| 2 | 제목 표시줄 제거 | Quick Panel은 `overrideredirect` chromeless, topmost, fixed-size shell로 표시한다. native caption과 thick frame을 사용하지 않는다. |
| 3 | hover 외곽 표시 | 기본 외곽선과 구분되는 파란 hover 외곽선을 사용한다. native pointer enter/leave로 실제 상태 전환을 확인한다. |
| 4 | 동기화 문구 중복 제거 | 동기화 상태의 소유자는 header 하나뿐이다. 정상·오류를 포함한 모든 panel 상태에서 동기화 label은 정확히 하나만 표시한다. |
| 5 | Quick Panel에서 근무시간 변경 | `계획 수정`은 오늘의 목표 순근무시간만 `HH:MM` 형식(`00:00`–`24:00`)으로 inline 편집한다. 저장·취소·validation을 제공하고 오늘의 출근 시각과 인접 날짜 계획은 변경하지 않는다. 상세 설정 화면은 다른 날짜와 전체 설정 용도로 유지한다. |
| 6 | 자동 닫힘 남은 시간 표시 | 기존 `tooltip_duration_ms` 설정을 따르는 idle deadline(기본 6초)의 남은 시간을 1초 단위로 표시한다. hover·inline 편집·명시적 상호작용 중에는 자동 닫힘을 일시정지한다. countdown은 영속 domain state가 아닌 view-local 상태다. |
| 7 | 마우스 위치 기반 표시 | 최초 표시와 재표시 모두 현재 pointer에서 16px 떨어진 위치를 사용한다. pointer monitor의 work area를 기준으로 오른쪽/아래 공간이 부족하면 반대편으로 뒤집고 마지막으로 clamp한다. 최초 native mapping이 geometry를 보정하는 경우 mapped 후 같은 pointer 계약을 다시 적용한다. |

## private iCal 계약

- 지원 범위는 별도 Cookie, OAuth 또는 `Authorization` header 없이 읽을 수 있는 Google Calendar의 **비공개 주소(iCal 형식)** 이다. 브라우저에서 로그인해야 열리는 일반 캘린더 페이지나 Microsoft 로그인 URL을 secret iCal 주소의 대체물로 취급하지 않는다.
- URL 및 redirect의 scheme/host 보호, 최대 응답 크기, strict UTF-8 decode와 기존 iCalendar parsing 경계를 유지한다.
- HTTP `401`/`403`, HTML/XHTML 로그인 응답, Google·Microsoft 로그인 redirect는 `authentication_required`로 분류한다.
- 정상 HTTP 응답이더라도 calendar가 아닌 JSON·HTML 등 media type이면 `unexpected_content_type`으로 분류한다. `text/calendar`는 calendar parser로 전달한다.
- 설정 화면은 저장 직후 연결 상태를 확인하고 `연결 다시 확인`으로 동일 generation-safe 조회를 재시도한다. provider 또는 DPAPI 보호 저장 실패도 성공처럼 닫지 않고 사용자에게 표시한다.
- 안내는 URL, path, query, calendar identity, event title, response body와 raw exception을 노출하지 않는 단계별 privacy-safe 문구만 사용한다.
- 휴가 계산과 cache에는 마지막으로 확인된 안전한 calendar 결과만 사용하며, 늦게 도착한 이전 generation 결과는 현재 상태를 덮어쓰지 못한다.

## 요구사항별 acceptance

| 요구사항 | 자동 검증 | native UI evidence |
| --- | --- | --- |
| #1 foreground | active/obscured toggle 및 nonactivating helper unit test | active show 후 foreground HWND가 panel HWND와 일치하고, nonactivating 반복 표시 3회가 sentinel foreground/focus를 보존 |
| #2 chromeless | shell 구성 unit test | `tk_overrideredirect=true`, topmost/fixed-size, caption/thickframe 부재 read-back |
| #3 hover | enter/leave와 border state unit test | `initial.png`과 `hover-active.png`의 외곽 색 및 native pointer delivery 비교 |
| #4 sync dedup | 정상·오류 header 단일 소유권 unit test | 9개 checkpoint 각각 sync label 정확히 1개 |
| #5 inline 목표 | `0`, `1440`, validation, 저장·취소, 출근/인접 날짜 보존 unit test | prefill `08:00`, invalid `24:30`, save `07:30`, cancel 후 `07:30` 보존과 callback `edit_plan:450` |
| #6 countdown | view-local deadline·설정값 연동·pause unit test | 안정적인 다단계 캡처를 위해 synthetic runner에 60초를 주입해 `60초 후 닫힘`을 확인하고, 별도 short-idle phase에서 hover/editor/interaction defer와 withdraw lifecycle 확인 |
| #7 pointer placement | monitor work-area flip/clamp unit test | initial·reopen geometry를 실제 Win32 cursor/work-area 기대값과 비교 |
| #8 private iCal | HTML/XHTML/JSON/calendar content type, login redirect, retry generation/state, 저장 오류 fixture | 실제 비공개 endpoint를 캡처하지 않으며 synthetic fixture 결과만 evidence로 사용 |

Native runner는 synthetic renderer를 800x640 client viewport로 캡처하며, 여러 checkpoint를 안정적으로 수집하기 위해 production 기본값과 독립적으로 60초 idle timeout을 주입한다. checkpoint는 `initial`, `hover-active`, `target-editor-prefill`, `target-editor-validation`, `target-editor-save-cancel`, `vacation-provisional`, `break-active`, `activity-prompt-focus`, `error-last-good`이며 각 PNG는 full decode, exact inventory, digest binding과 수동 시각 검토 receipt를 거쳐 finalized validation한다.

## 확인 가능한 경계와 알려진 한계

- 실제 회사 private iCal URL과 응답은 제공되지 않았으므로 해당 endpoint의 DNS, proxy, TLS, redirect, 인증 정책 또는 실제 calendar identity는 이 릴리스에서 확정할 수 없다.
- synthetic fixture는 오류 분류와 privacy-safe UI 경계를 검증하지만 운영 endpoint 성공을 증명하지 않는다. 사용자는 Google Calendar 설정의 `비공개 주소(iCal 형식)`을 저장한 뒤 `연결 다시 확인` 결과로 운영 연결을 확인해야 한다.
- Cookie/OAuth/Authorization이 필요한 사내 Microsoft·Google 로그인 흐름은 의도적으로 지원하지 않는다. host allowlist 확장만으로 인증 URL을 허용하지 않는다.
- native harness의 focus 보존 범위는 same-process Tk sentinel이다. global hotkey integration, production Wrike snapshot/cache, 실제 vacation fetch, tray와 packaged executable은 별도 smoke/build 검증 대상이다.

## 릴리스 검증

1. `uv run python -m unittest tests.unit.test_wrike_worktime_panel tests.unit.test_wrike_realtime_progress tests.unit.test_wrike_ical`
2. `uv run python -m unittest tests.unit.test_qa_wrike_worktime_panel_native`
3. `uv run python scripts/qa_wrike_worktime_panel_native.py --output-dir <external-empty-directory>` 및 9개 PNG 시각 검토
4. review receipt를 적용한 finalize와 `--validate-finalized` exact inventory/digest read-back
5. `uv run python -m unittest discover -s tests -p "test_*.py"`
6. `git diff --check`
7. `cmd /c build.bat`
8. packaged `windows-supporter.exe` startup/shutdown 및 가능한 `Ctrl+Alt+W` foreground smoke
9. clean tagged `main` 산출물의 `FileVersion`, `ProductVersion`, `Comments` read-back과 `main`, `v0.13.0`, `develop`의 `release-chain-gate` 확인
