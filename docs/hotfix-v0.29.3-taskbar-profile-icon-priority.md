# Hotfix v0.29.3 — 작업표시줄 프로필 아이콘 우선순위

## 분류 판정

- 의도한 계약: 작업표시줄 사용량 오버레이의 각 프로필명 앞에는 현재 provider를 식별하는 아이콘이 항상 표시된다.
- 현재 동작: 메트릭의 full-text 폭이 현재 슬롯에 들어가지 않으면 레이아웃이 `icon_width`를 0으로 만들고 아이콘을 그리지 않았다.
- 차이: 좁은 슬롯에서 프로필의 provider 식별 정보가 메트릭 텍스트보다 먼저 사라졌다.
- 판정: 새 capability가 아닌 기존 프로필 아이콘 표시 계약의 불완전 구현 복구이므로 `hotfix/v0.29.3`으로 분류한다.

## RCA와 수정

아이콘은 `_draw()`에서 `row_layout.icon_width > 0`일 때만 그려진다. 기존 compact 분기는
메트릭 폭이 부족하면 아이콘 열을 먼저 제거했기 때문에, 그리기 함수 자체가 정상이어도
작업표시줄 슬롯이 좁아지는 순간 아이콘이 사라졌다.

- provider 아이콘 열은 모든 폭에서 유지한다.
- 좁은 슬롯에서는 선택적 상태 텍스트와 headless 기본 프로필 여백을 먼저 회수한다.
- 이후 메트릭은 기존 countdown·percent 우선 compact fit으로 축소한다.
- preferred 폭 계산은 정상 폭에서 상태 텍스트가 다시 표시될 수 있는 지점을 유지한다.

## 검증

- `tests/unit/test_codex_usage_taskbar_overlay.py` 266개
- taskbar overlay smoke/multimonitor/target tests 17개
- AI usage UI/native visual contract tests 95개
- 좁은 298px 슬롯에서 Codex/Cursor 아이콘이 그려지고 프로필명이 아이콘 뒤에서 시작하는 regression test
- `git diff --check`, Python compile check
