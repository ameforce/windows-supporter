# v0.23.5 UI/UX fit hotfix 판정

| 항목 | 확인 결과 |
| --- | --- |
| 의도한 계약 | 각 설정 화면은 콘텐츠에 맞는 compact 크기로 전환되고, Ctrl+Alt+W Quick Panel은 선택한 날짜의 타임로그를 읽기 쉽게 표시한다. |
| 현재 동작 | 긴 탭을 떠날 때의 창 크기가 다음 탭 fallback에 덮어써져 작은 Update 화면도 큰 빈 카드로 남는다. Quick Panel은 상세 내용이 줄어도 이전 geometry를 유지하고, 단일 타임로그는 티켓 합계와 기록 시간이 중복되어 보인다. |
| 차이 | 기존 UI fit 및 읽기 쉬운 상세 계약이 불완전하게 구현되어 있다. API, 저장 형식, 새로운 사용자 capability의 도입은 필요하지 않다. |
| 판정 | **hotfix v0.23.5** — 기존 화면 레이아웃과 타임로그 표현의 회귀·불완전 구현을 복구한다. |

## 범위

- 탭 전환 자동 geometry와 사용자의 명시적 resize를 분리하고, 좁은 폭에서는 탭 제목을 축약한다.
- Dashboard와 Update의 여백·상태 밀도를 compact하게 조정한다.
- Quick Panel이 content-driven bounded geometry로 다시 맞춰지고, 단일/다중 타임로그를 구분해 표시하도록 한다.

## 검증 범위

- 직접 영향 unit: main shell/dashboard/update, Quick Panel/timelog detail 및 break-edit regression.
- UI 변경이므로 변경 화면·상태의 headless Tk subset을 실행한다. 전체 suite는 공용 기반 변경이 아니므로 실행하지 않는다.
- native screenshot QA는 host input API가 `Win32 GetCursorPos` 접근 거부를 반환하는 경우에만 그 제한을 기록하고, 우회 자동화로 대체하지 않는다.
