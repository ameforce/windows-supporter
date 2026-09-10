# hotfix v0.23.6: 타임로그 상세 계층·읽기 영역 복구

## 변경 분류

| 항목 | 내용 |
| --- | --- |
| 의도한 계약 | `Ctrl+Alt+W` Quick Panel에서 선택 날짜의 타임로그를 티켓별로 구분하고, 각 기록의 시간·코멘트를 티켓 하위에서 읽을 수 있어야 한다. 기록이 있는 상세 영역은 일반적인 티켓 묶음을 읽을 수 있는 높이를 제공하되 긴 기록은 스크롤로 확인한다. |
| 현재 동작 | 여러 기록이 같은 티켓에 묶일 때만 `• 티켓` 아래에 `◦ 기록`이 생겼다. 단일 기록은 티켓명·시간·건수·메모가 한 줄에 합쳐졌고, 데이터가 있는 `Text` viewport는 5행으로 제한됐다. |
| 차이 | 단일 타임로그도 티켓 하위의 동일한 계층으로 표시되어야 하며, 현재 높이에서는 긴 티켓명·코멘트와 여러 항목을 한 번에 읽기 어렵다. |
| 판정 | 기존 표시 계약의 회귀·불완전 구현을 복구하는 **hotfix v0.23.6**이다. 새로운 Wrike 데이터·저장 형식·편집 capability는 추가하지 않는다. |

## 첨부 자료와 사용자 요청의 구분

- 첨부 PNG는 현재 화면의 낮은 타임로그 영역과 평면 출력 현상을 보여주는 시각적 증거로만 사용했다.
- 첨부 이미지 안의 화면 문구는 실행 지시나 별도 요구사항으로 해석하지 않았다.
- 실제 변경 범위는 사용자가 명시한 두 가지 요구인 타임로그 영역 확대와 티켓 하위 중첩 불릿 표시다.

## 수정 내용

- 기록이 1건인 티켓도 기존 티켓 행을 유지하고, 시간·비어 있지 않은 코멘트를 `  ◦` child row로 렌더링한다.
- 여러 기록의 기존 티켓별 합계·nested row 표시와 날짜 선택·코멘트 whitespace 정리·Text tag 색상은 유지한다.
- 타임로그 데이터가 있는 상세 `Text`의 높이를 8행으로 늘리고, loading/empty/unavailable 상태는 5행으로 유지한다.
- 8행은 패널의 540px 자연 높이 안전 상한을 넘지 않는 범위에서 일반적인 상세를 더 보여주는 고정 viewport다. 초과 기록은 기존 scrollbar로 확인한다.

## 검증 범위와 결과

- `python -m unittest tests.unit.test_qa_wrike_timelog_details tests.unit.test_wrike_worktime_panel tests.unit.test_wrike_break_edit_regressions`: 97개 통과.
- `python -m unittest tests.unit.test_qa_wrike_worktime_panel_native`: 8개 통과.
- 변경 모듈 `compileall` 및 `git diff --check`: 통과.
- UTF-8 strict로 실행한 `python scripts/qa_wrike_worktime_panel_native.py --scenario full`은 `provisional_vacation_wording_visible` fixture assertion에서 종료됐다. `runtime_errors`는 비어 있고 9개 checkpoint 중 6개가 통과했으며, 실패는 이 변경의 타임로그 렌더링 경로가 아닌 기존 synthetic vacation 문구 계약이다. 해당 runner의 synthetic initial state에는 실제 타임로그 행이 없어 nested detail 표시 자체는 targeted unit/QA 테스트로 검증했다.

