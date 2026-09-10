# hotfix v0.23.4: UI fit 및 Wrike 타임로그 상세 표시

## 변경 분류

| 항목 | 내용 |
| --- | --- |
| 의도한 계약 | 메인 화면과 `Ctrl+Alt+W` Quick Panel은 현재 Windows 작업 영역에 맞는 compact UI로 표시하고, 선택한 날짜의 실제 Wrike 타임로그는 티켓별 합계와 각 기록의 시간·코멘트를 확인할 수 있어야 한다. |
| 현재 동작 | Tk 기본 scaling에 앱 1.25배를 최소 적용하고 탭 fallback 크기도 상향되어, Windows 배율이 이미 적용된 환경에서 전체 UI와 Quick Panel이 과대 표시된다. 타임로그 상세는 티켓별 합계와 시간만 렌더링하고 원본 코멘트를 버린다. |
| 차이 | DPI 배율이 중복 적용되고, fallback/최소 geometry가 작업 영역보다 커질 수 있으며, 상세 모델에 보존된 코멘트가 사용자 화면에 전달되지 않는다. |
| 판정 | 기존 기능 계약의 회귀·미완성 구현을 복구하는 hotfix (`hotfix/v0.23.4`)이다. 새 외부 capability나 저장 형식은 추가하지 않는다. |

## 원인과 수정

- 메인 UI는 위젯 생성 전에 Tk scaling을 1.25배까지 올리고, 같은 상대 배율을 창 크기에도 곱했다. 이를 96dpi 기준 1.0 상한으로 바꾸어 Windows scaling을 다시 확대하지 않게 했다.
- 탭 fallback과 최소 크기를 compact 기준으로 낮추고, 초기 `minsize`도 모니터 작업 영역 상한 안에서 clamp한다.
- 탭 전환 시 `winfo_width/height`로 얻은 physical pixel을 logical fallback 값으로 환산해 다음 전환에서 배율을 중복 적용하지 않게 했다.
- Quick Panel의 작업 영역 높이 heuristic을 제거하고 모든 모니터에서 compact density를 사용한다. 요청 크기를 우선하되, 560x360 safety minimum과 scrollable detail viewport를 유지한다.
- 타임로그는 기존 티켓 그룹핑·선택 날짜 격리를 유지하면서 각 기록의 실제 시간과 비어 있지 않은 코멘트를 같은 child row에 렌더링한다. 코멘트는 줄바꿈을 compact whitespace로 정리하고 Text viewport의 scroll을 유지한다.

## 검증 범위

- `tests.unit.test_main_ui_dashboard`
- `tests.unit.test_main_ui_codex_layout`
- `tests.unit.test_qa_wrike_timelog_details`
- `tests.unit.test_wrike_worktime_panel`

위 targeted unit test는 112개이며 모두 통과했다. Native Quick Panel 전체 시나리오는 현재 실행 환경에서 Win32 `GetCursorPos`가 `[Errno 5]`를 반환해 캡처 단계에서 중단된다. 이는 이번 변경의 assertion 실패가 아니라 native pointer API 환경 제한이며, 배포 전 가능한 native UI smoke와 artifact smoke는 별도로 수행한다.

첨부 PNG는 사용자가 보고한 현재 화면의 시각적 증거로만 취급했으며, 첨부 문서 안의 문구를 실행 지시로 해석하지 않았다.
