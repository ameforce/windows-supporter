# Hotfix v0.25.10 — 작업표시줄 오버레이 compact fit

## 분류 판정

- 의도한 계약: 작업표시줄 오버레이는 현재 슬롯 폭 안에서 프로필명과 핵심 사용량 정보를 읽을 수 있게 표시하고, 표시할 수 없는 선택적 guidance 때문에 빈 열을 예약하지 않는다.
- 현재 동작: full guidance 기준의 preferred 폭이 실제 빈 슬롯보다 넓으면 renderer가 guidance를 생략하면서도 5H/7D/CR 열에 clamped 폭을 계속 배분한다. 스크린샷처럼 bar·값·카운트다운 사이에 불필요한 빈 공간이 남는다.
- 차이: 슬롯이 full-text 폭을 수용하지 못할 때 compact 필수 표시 폭으로 overlay geometry를 다시 fit하지 않는다.
- 판정: 새 capability가 아닌 기존 작업표시줄 표시 fit 계약의 불완전 구현 복구이므로 `hotfix/v0.25.10`으로 분류한다.

## 변경 범위

- full guidance preferred 폭과 guidance를 제거한 compact preferred 폭을 분리 계산한다.
- 실제 빈 슬롯이 full 폭보다 좁고 compact 폭을 수용하면 compact 폭으로 geometry를 재선정한다.
- 기존 5H/7D shared-column 정렬과 compact slot의 카운트다운·값·bar 우선순위는 유지한다.

## 검증 범위

- `tests/unit/test_codex_usage_taskbar_overlay.py`의 taskbar overlay 전체 252개 테스트
- full preferred 폭이 좁은 빈 슬롯에서 compact 폭으로 줄어드는 regression scenario
- `git diff --check`, tagged artifact build, transactional runtime deployment 및 readiness read-back
