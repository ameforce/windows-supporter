# Hotfix v0.12.1 — Wrike runtime panel 및 private iCal 안정화

## 변경 분류

- 의도한 계약: 출근 시간이 있으면 휴가 조회 상태와 무관하게 알려진 근무 데이터로 현재 기대·편차·예상 퇴근을 표시하고, 휴가가 확정되지 않은 값만 명확히 임시로 구분한다. Google private iCal은 비밀 URL이나 calendar 내용을 노출하지 않으면서 제한된 입력만 안전하게 해석한다. Quick Panel은 동일 모델 polling에서 깜빡이지 않고 기존 tooltip처럼 짧은 유휴 시간 뒤 사라져야 한다.
- 현재 동작: 휴가가 loading/stale/error이면 출근 시간이 있어도 현재 기대가 `조회 불가`가 됐다. private iCal 실패는 원인 분류와 입력 경계가 불완전했고, panel은 매초 geometry/render 경로를 거치거나 닫을 때까지 남을 수 있었다.
- 차이: v0.12.0에서 의도한 실시간 진척, credential 비노출, 재사용 panel 수명주기 계약의 누락 또는 불완전 구현이다.
- 판정: 새 제품 기능이 아니라 공개된 v0.12.0 계약을 복구하는 버그 수정이므로 `hotfix/v0.12.1`로 분류한다. 공개된 `v0.12.0`, `main`, `develop` 이력은 재작성하지 않는다.

## 휴가 미확정 시 임시 계산

- 휴가를 사용할 수 없어도 기본 순근무 목표, 출근 시각, 현재 시각과 알려진 병합 휴게로 현재 기대를 계산한다.
- 현재 기대·현재 기준 편차·예상 퇴근에는 `(임시)`를 표시한다.
- 휴가 행에는 `휴가 미확정 (<state>) · 휴가 미반영 임시 목표 <시간> (임시)`를 표시한다.
- 휴가 값이 확정되면 기존 `휴가 차감 · 적용 목표` 계산으로 돌아간다.
- 자동 첫 활동 출근 prompt는 임시 계산 가능 여부와 분리한다. 휴가가 loading/stale/error인 동안에는 `automatic_prompt_allowed`가 계속 fail-closed이며, 이미 저장된 pending prompt도 render와 accept/edit/snooze/skip 직전에 live 상태를 다시 검사해 숨기고 action을 거부한다. 일시적 unavailable 전환만으로 pending 상태를 삭제하지 않는다.

## private iCal 입력 및 진단 경계

- 외부 상태에는 다음 closed error code만 전달한다: `invalid_endpoint`, `redirect_rejected`, `http_4xx`, `http_5xx`, `dns_or_connect`, `timeout`, `tls_validation`, `body_too_large`, `unsupported_encoding`, `utf8_decode`, `empty_body`, `invalid_ical`.
- orchestration 호환 상태는 `secret_unavailable`, `calendar_name_mismatch`와 legacy `calendar_fetch_failed`를 유지한다.
- `identity`와 `gzip` content encoding만 허용하고 wire body와 decompressed body를 각각 2 MiB로 제한한다.
- UTF-8은 BOM만 선택적으로 제거한 뒤 strict decode한다. 문서는 정확히 하나의 root `VCALENDAR`여야 하며 top-level `NAME`을 calendar identity fallback으로 허용한다.
- redirect는 원 URL과 같은 HTTPS allowlist(`calendar.google.com` 또는 `*.googleusercontent.com`, 기본 443 port, userinfo 없음) 안의 absolute target만 따라가며 final URL도 같은 규칙으로 다시 검증한다. 그 밖의 redirect는 `redirect_rejected`로 거부한다. URL의 path/query, `Location`, request/response header, exception 원문, body, calendar identity와 event title은 UI·로그·장수명 cache·증거에 남기지 않는다.
- URL 교체 또는 삭제 뒤 이전 calendar 값을 fallback할 때는 현재 설정의 성공값으로 오인하지 않도록 `has_last_good=False`로 표시한다.

## Quick Panel 안정성 및 수명주기

- immutable model이 정확히 같으면 Tk widget, configure, geometry, reconciliation 경로에 들어가지 않는다.
- prompt 존재 여부와 오늘 상세 행 수가 같은 text-only 변경은 기존 widget을 in-place 갱신하고 geometry를 보존한다.
- 기본 유휴 종료 시간은 6초이며 최소 설정값은 1.2초다. 유휴 만료는 window를 destroy하지 않고 withdraw해 다음 표시에서 같은 singleton window를 재사용한다.
- hover, pointer/key activity와 callback 실행 중에는 종료를 미룬다. nested/modal callback은 `_interaction_depth`가 0이 된 뒤에만 전체 timeout을 다시 설정한다.
- Escape, close와 toggle은 즉시 숨기며 refresh/dismiss timer를 함께 정리한다.

## 검증 및 증거 경계

- unit test는 임시 기대 계산, 자동 prompt fail-closed, typed iCal 오류·size/encoding/document 경계, stale fallback, exact-equal no-op, same-structure in-place update와 idle lifecycle을 검증한다.
- native synthetic runner 2.3은 800x540에서 7일 행·오늘 강조·prompt focus·error-last-good·임시 휴가 문구를 캡처한다. 일반 캡처에는 60초 timeout을 사용하고 별도 1.2초 시나리오에서 Win32 pointer 이동이 실제 additive Tk `<Enter>/<Leave>` binding으로 전달되는지, idle withdraw, hover/interaction defer와 same-window reopen을 검증한다.
- exact-equal에는 별도이지만 값이 같은 model instance를 공급하고 `_render_structure`/`_update_rendered_model`/`_reconcile_geometry` 호출이 모두 0인지 계측한다. same-structure 변경은 각각 0/1/0 호출이어야 한다.
- native PNG는 target client `GetDC`/`BitBlt`로만 캡처하고 전체 decode, revision seal과 exact inventory로 묶는다. review receipt schema 2는 reviewer label과 manual review method를 선언하되 `identity_assurance="none"`, signature 없음으로 한계를 명시한다.
- finalized manifest는 `run.json`과 `review-receipt.json`의 SHA-256, declared provenance와 exact inventory를 결속한다. `--validate-finalized`는 renderer를 로드하거나 파일을 수정하지 않고 최종 bundle을 독립적으로 재검증한다.
- synthetic evidence는 production Wrike snapshot/cache, 실제 private iCal fetch·계산, state persistence, tray, hotkey, packaged EXE와 cross-process focus를 검증한다고 주장하지 않는다.
- 실제 회사 Google private iCal URL로 outbound request를 수행하지 않으므로 특정 endpoint의 DNS, proxy, TLS 또는 HTTP 실패 원인은 확정하지 않는다.
- packaged `windows-supporter.exe`는 별도 startup/shutdown smoke로 확인하고, 최종 영구 산출물은 clean tagged `main@v0.12.1`에서 다시 빌드한다.
