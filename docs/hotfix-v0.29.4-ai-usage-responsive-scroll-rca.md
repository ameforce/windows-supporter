# AI 사용량 탭 반응형·스크롤 지연 RCA

## 분류

| 항목 | 내용 |
| --- | --- |
| 의도한 계약 | AI 사용량 탭의 설정·프로필·상태 항목은 현재 viewport 폭 안에서 잘리지 않고, 좁은 폭에서는 열을 줄이거나 행을 감싸서 조작할 수 있어야 한다. 빠른 wheel 입력과 주기적인 상태 polling이 서로의 화면 반응성을 떨어뜨리지 않아야 한다. |
| 현재 동작 | 프로필 섹션 제목이 본문 4열 grid의 첫 열에만 놓여 첫 열의 최소 폭을 키웠고, 프로필 metric·실시간 상태는 항상 4열/2쌍으로 배치됐다. URL 입력·긴 label·상태 값이 좁은 창에서 오른쪽으로 밀리거나 text overflow를 만들 수 있었다. wheel 이벤트는 body의 모든 하위 widget에 개별 binding됐고, 각 이벤트마다 즉시 `yview_scroll`을 호출했다. 동시에 1초 polling이 값이 같아도 모든 `StringVar`, metric visibility, action button 상태를 다시 썼다. |
| 차이 | 폭에 따른 layout breakpoint와 text wrapping이 입력·카드 내부·상태 grid 전체에 적용되지 않았고, scroll burst와 steady-state polling에 대한 UI work coalescing/diffing이 없었다. remount 때 toplevel wheel binding을 회수하지 않으면 재진입 횟수에 비례해 callback도 누적될 수 있었다. |
| 판정 | 새 사용자 capability가 아니라 기존 AI 사용량 설정 화면의 표시·반응성 계약을 복구하는 작업이므로 `hotfix/v0.29.4`로 분류한다. |

## 원인 증거

- v0.29.3 baseline의 native Tk matrix는 targeted unit 95개와 10개 scenario를 통과했지만, `mixed-ready-standard`와 cursor 상태 fixture에서 고정 metric grid의 text overflow가 각각 8개로 보고됐다.
- baseline 구현은 섹션 heading을 `body`의 `column=0`에, metric을 라벨-값-라벨-값 4열에 두었다. wide fixture에서는 우연히 보이지만, 실제 compact viewport에서는 child requested width가 viewport보다 커질 수 있는 구조였다.
- `_refresh_runtime_status()`는 1초마다 계정별 상태·metric 변수와 action button을 모두 갱신했다. Tk 변수 값이 같아도 trace/label repaint가 발생하고, visibility 행에는 매 tick `grid()`/`grid_remove()`가 호출됐다.
- `_bind_mousewheel_tree()`는 body의 모든 descendant에 wheel binding을 복제했다. 빠른 wheel burst 중에는 각 event가 즉시 canvas redraw를 요청하고, remount 후 이전 toplevel callback도 남을 수 있었다.

## 수정 내용

1. 섹션 heading을 독립 frame으로 분리하고 본문 전체 열을 사용하도록 했다. URL·interval 입력은 유연한 value column을 사용한다.
2. option/checkbutton, 프로필 control/action row를 실제 container 폭에 따라 2열에서 1열로 reflow한다. 프로필 카드는 기존 카드 수·DPI 정책을 유지하되, header label/path/status의 wraplength를 카드 폭에 맞춘다.
3. metric과 실시간 상태를 넓은 화면에서는 두 쌍, 좁은 화면에서는 한 쌍씩 배치한다. 값 cell은 남은 폭을 채우고 긴 값은 줄바꿈한다.
4. wheel은 canvas 직접 binding과 toplevel fallback만 유지하고 descendant별 binding을 제거했다. `after_idle`에서 wheel units를 합쳐 burst당 한 번만 `yview_scroll`하며, remount 전에 이전 toplevel binding을 해제한다.
5. `_set_var_if_changed`, metric visibility cache, live Spark visibility cache, button state cache를 적용해 steady-state polling에서 동일한 Tk 작업을 생략했다. action permission lookup은 runtime profile map을 한 번 만들어 계정 수가 늘어도 반복 선형 탐색을 피한다.

## 검증 계약

- `tests.unit.test_codex_usage_ui`: 97개 targeted unit 통과.
- `tests.unit.test_qa_ai_usage_native_visual`: 11개 synthetic native Tk scenario와 compact 520×560 scenario 정의 통과.
- native capture는 저장소 밖 임시 경로에 생성하며, `compact-narrow`에서 URL·긴 프로필·metric·action row의 폭 overflow가 없고 mousewheel interaction이 적용되는지 확인한다.
- 전체 suite와 무관한 browser/E2E는 변경 경로에 직접 필요하지 않아 task validation에서 실행하지 않는다. 최종 tagged release closure의 build 검증은 release runbook phase에서 별도로 수행한다.
