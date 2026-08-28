# Release v0.14.0 — 날짜별 목표 근무시간·Quick Panel 통합

## 변경 분류

- 의도한 계약: Windows Supporter는 Wrike timelog를 읽기 전용 snapshot으로만 사용한다. 사용자는 Quick Panel에서 선택한 날짜의 목표 근무시간을 로컬 계획으로 변경할 수 있고, timelog 증가 알림과 `Ctrl+Alt+W`는 같은 reusable Quick Panel을 사용한다.
- 현재 동작: `계획 수정`은 오늘의 목표만 변경하고 월~일 행에는 선택 identity가 없다. `Ctrl+Alt+W`와 활동 감지는 `WorktimeQuickPanel`을 재사용하지만 timelog 총합 증가는 별도 `ToolTip`과 별도 `Toplevel`을 만든다.
- 차이: 날짜별 목표를 선택·편집할 UI context가 없으며, 동일한 근무시간 정보를 표시하는 두 UI surface와 lifecycle이 공존한다.
- 판정: 기존 의도에 없던 날짜 선택 편집과 자동 알림 표시 정책을 도입하는 사용자 기능이므로 patch hotfix가 아니라 `release/v0.14.0`으로 분류한다.

## 제품 계약

1. 월요일~일요일 행에서 날짜를 선택하고 `목표 수정`을 누르면 같은 Quick Panel의 공통 inline editor가 열린다.
2. editor는 선택 날짜와 휴가 차감 전 목표 근무시간을 `HH:MM`으로 표시하고 `00:00`~`24:00`만 허용한다.
3. 저장은 `%APPDATA%\windows-supporter\wrike_worktime_state.json`의 해당 날짜 plan만 atomic하게 변경한다. 기존 출근 시각과 다른 날짜 plan은 보존한다.
4. 오늘 목표가 바뀌면 현재 기대, 부족·초과, 적용 목표와 예상 퇴근을 즉시 재계산한다. 과거·미래 날짜는 해당 행의 목표·휴가·부족·초과 표시를 즉시 재계산한다.
5. Wrike 기록과 timelog는 모든 Quick Panel action에서 읽기 전용이다. 목표 저장 경로는 Wrike HTTP client를 호출하지 않으며 원격 timelog 생성·수정·삭제 기능을 추가하지 않는다.
6. timelog total 증가 감지는 별도 `ToolTip`을 만들지 않고 singleton `WorktimeQuickPanel`을 `activate=False`로 표시한다. `Ctrl+Alt+W`는 기존처럼 같은 panel을 foreground toggle한다.
7. 날짜 선택, inline 편집, 자동 알림은 기존 absolute dismiss deadline, hover expiry hold, reusable Toplevel 계약을 공유한다.

## 직접 검증 범위

- `tests.unit.test_wrike_worktime_panel`의 row 선택·목표 inline editor·저장/취소·deadline 관련 기존 case
- `tests.unit.test_wrike_realtime_progress`의 날짜별 plan 보존·timelog 증가 notification·singleton lifecycle 관련 기존 case
- `tests.unit.test_qa_wrike_worktime_panel_native`의 변경된 evidence schema case
- `scripts/qa_wrike_worktime_panel_native.py --scenario inline-edit-hover-deadline`의 선택 날짜 목표 editor와 동일 Toplevel/absolute deadline checkpoint
- Python compile, `git diff --check`, semantic review, `cmd /c build.bat`

변경과 무관한 전체 unittest discovery, private iCal·vacation·break·focus 전체 E2E는 실행하지 않는다.
