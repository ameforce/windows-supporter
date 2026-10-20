# Hotfix v0.36.6 — 작업표시줄 좌우 균등 배치

## 의도한 계약 → 현재 동작 → 차이 → 판정

- 의도한 계약: 사용자는 작업표시줄 좌우 우선 방향을 고르고, 선택한 프로필의 표시 순서를 유지할 수 있다. v0.31.0에 기록된 우선 배치 동작은 기존 설정의 기본값으로 계속 유지한다.
- 현재 동작: 우선 방향에 프로필 두 개를 먼저 배치하고, 세 번째와 네 번째를 반대쪽에 둔다. 선택 프로필이 두 개뿐이면 두 프로필이 우선 방향 한쪽에 모인다.
- 차이: 사용자는 선택 순서대로 좌우에 한 개씩 배치하는 방법을 고를 수 없다.
- 판정: 새 프로필 수나 표시 정보 없이 배치만 조정하는 사용자 지시 개선이므로 hotfix로 분류한다. 기존 우선 배치는 기본값으로 남기고 새 균등 배치 선택지를 추가한다.

## 변경 계약

- `우선 쪽에 모으기`는 v0.36.5 동작을 유지한다.
- `좌우 균등`은 선택 순서대로 좌우 pane에 번갈아 배치한다. 프로필 수가 홀수면 우선 방향에 한 개를 더 둔다. 선택이 두 개면 좌우에 한 개씩 둔다.
- 설정 프로필 보드, 카드 드래그 재배치, 작업표시줄 오버레이는 같은 pane assignment 계약을 사용한다.
- 배치 방식은 AI 사용량 설정에 저장한다. 기존 설정에 값이 없으면 `priority`를 적용해 이전 동작을 보존한다. 프로필 설정 스키마 버전 변경은 필요하지 않다.
- 세로 작업표시줄에서는 균등 배치로 선택된 두 프로필을 유일하게 표시 가능한 pane에 함께 보여 숨겨지는 프로필이 없게 한다.

## 검증 범위

- 직접 호출 경로: 배치 계약 resolver/drop planner, 다중 프로필 설정 저장·복원, 설정 UI 초기 표시·저장·드래그, taskbar overlay pane getter와 세로 방향 전환.
- 단위 검증: `test_ai_usage_contracts.py`, `test_ai_usage_taskbar_panes.py`, `test_codex_usage_multi_monitor.py`, `test_codex_usage_ui.py`, `test_codex_usage_taskbar_overlay.py`, `test_qa_ai_usage_native_visual.py`.
- UI-visible 검증: `balanced-two-taskbar-profiles` native Tk 시나리오만 캡처한다. 다른 화면·전체 시나리오 캡처는 범위에 포함하지 않는다.
- build 검증: UI 동작 변경이므로 task artifact build와 clean tagged artifact build를 각각 수행한다. candidate build는 영구 runtime을 건드리지 않는다.

## 결과

- 구현/검증 증거는 task PR, annotated tag `v0.36.6`, final release evidence에 추가한다.
