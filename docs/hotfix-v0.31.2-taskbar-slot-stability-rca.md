# Hotfix v0.31.2 — 작업표시줄 좌우 pane 슬롯 진동 RCA

## 분류 판정

- 의도한 계약: 사용자가 아무 동작을 하지 않고 작업표시줄의 실제 점유 상태도
  유지되면 좌·우 pane은 현재 슬롯에 남아 있어야 한다. 슬롯이 실제로 사라지거나
  디스플레이 토폴로지가 바뀐 경우에만 새 영역을 선택한다.
- 현재 동작: geometry tick이 500ms마다 점유 영역을 다시 계산한다. 기존 선택기는
  같은 쪽의 여러 free span 중 rightmost를 우선하고, 직전 pane과 겹치는 span이
  compact minimum보다 넓을 때만 overlap hold를 적용했다. 같은 쪽에서 한 번
  이동한 뒤 이전 슬롯으로 돌아오는 후보는 별도 cycle guard 없이 두 번 연속이면
  적용됐다.
- 차이: 점유 샘플의 일시적인 변화 또는 오버레이 외곽 pixel dilation이 두 번의
  샘플 동안 유지되면 `A → B → A` 왕복이 실제 native geometry 적용으로 이어질
  수 있었다. `AiUsageTaskbarOverlay`는 좌 pane을 먼저 샘플·이동한 뒤 우 pane을
  샘플하므로, 두 pane이 같은 순간의 점유 증거를 보장하지도 않았다.
- 판정: 기존 표시 계약의 버그·불완전 구현 복구이므로 `hotfix/v0.31.2`로
  분류한다.

## 재현 및 사실 판단

`spans_a = [(0, 100), (400, 600), (960, 1920)]`와
`spans_b = [(0, 100), (700, 900), (960, 1920)]`를 번갈아 주면 순수 슬롯 계산은
좌측 내부에서 각각 `x=652`, `x=392`를 선택했다. 기존 live stabilizer도
`A, B, B, A, A` 샘플에서 native geometry를
`+652 → +392 → +652`로 적용했다. 따라서 보고된 현상은 코드상 가능성이 아니라
현재 구조에서 재현되는 사실이다.

2026-09-16 로컬 taskbar detector를 0.5초 간격 7회 반복한 baseline은
`repeated_sample_stability.stable=true`였다. 이 결과는 해당 순간의 Explorer
상태가 안정적이었다는 뜻이지, overlay가 보이는 모든 상태에서 회귀가 없다는
뜻은 아니다. 위의 고정 회귀 재현이 사용자 증상의 가능 경로를 직접 증명한다.

## 근본 원인

1. 슬롯 선택과 검증이 한 번의 관측에 종속됐다. `rightmost` 재선택과
   self-exclusion 주변의 dilation이 매 tick마다 후보를 바꿀 수 있었다.
2. 같은 쪽 이동의 reverse 후보가 직전 이동의 반대편이라는 사실을 상태로 보존하지
   않았다. 두 샘플 확인은 최초 relocation에는 충분하지만 왕복 feedback에는
   부족하다.
3. 두 pane이 독립적으로 detector를 호출해 첫 pane의 resize와 peer span 주입이
   둘째 pane의 관측 시점을 바꿨다.

## 수정 내용

- live renderer에 compact-safe previous-slot witness를 추가했다. 직전 슬롯의 중심
  또는 충분한 면적이 아직 free이면 rightmost/ wider-slot 후보가 있어도 그 슬롯을
  유지한다. cold-start용 public pure geometry 정책은 보존했다.
- 이미 승인된 같은 쪽 이동을 즉시 되돌리는 후보는 4회 연속 동일 증거가 있어야
  적용한다. 짧은 `A → B → A` feedback에서는 B를 유지하고, 지속적인 실제 변화는
  결국 수렴한다. display invalidation·hide 시 stale transition state도 지운다.
- pixel occupancy dilation 결과를 `_exclude_spans`에서 다시 빼서 오버레이 외곽이
  자기 점유 영역으로 재유입되지 않게 했다. UI Automation의 실제 taskbar leaf
  span은 기존대로 exclusion을 우회해 underneath icon을 보존한다.
- 좌·우 pane은 동일한 taskbar occupancy snapshot을 한 cycle 동안 공유하고, 두
  live pane rect를 detector exclusion에 함께 넣는다. 다음 cycle과 topology
  invalidation에서는 cache를 폐기한다.

## 검증

- 직접 영향 unit/targeted regression: `287 tests`, `OK`
- 신규 RCA regression: `test_geometry_monitor_does_not_ping_pong_between_same_side_slots`
  및 pixel exclusion·pane snapshot 공유 테스트 포함
- 로컬 native taskbar smoke: 0.5초 간격 7회 반복, `stable=true`
- `uv`는 저장소 요구 버전 `0.10.2`가 필요하지만 현재 설치 버전이 `0.9.24`라
  실행하지 못했다. 동일 targeted suite는 저장소 Python으로 직접 실행했다.
