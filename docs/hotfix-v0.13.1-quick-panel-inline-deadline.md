# Hotfix v0.13.1 — Quick Panel inline 편집·hover deadline

## 변경 분류 게이트

- **의도한 계약:** `Ctrl+Alt+W` Quick Panel에서 사용자가 직접 값을 입력하는 단순 시간 편집은 별도 dialog를 만들지 않고 동일 panel 안의 하나의 공통 inline editor에서 수행한다. Auto-dismiss는 panel 표시 시 정한 absolute deadline까지 hover와 무관하게 계속 진행한다. Deadline 도달 시 pointer가 panel 안에 있으면 닫기만 보류하고 hover hold 상태를 표시하며, pointer가 벗어나는 즉시 새 timeout 없이 닫는다.
- **현재 동작:** `계획 수정`만 panel-local editor를 사용한다. 기존 출근의 `출근 수정`과 activity prompt의 `시간 수정`은 `tkinter.simpledialog.askstring`을 호출해 별도 modal을 만든다. Hover 진입은 dismiss callback과 `_dismiss_deadline`을 모두 취소하며, leave 시점부터 전체 timeout을 새로 부여한다.
- **차이:** 동일 작업이 target 전용 inline 경로와 clock-in modal 경로로 분기되어 하나의 Quick Panel이라는 UX 계약이 불완전하다. Hover는 deadline 만료 시 dismissal만 hold해야 하지만 현재는 deadline 자체를 정지·재설정한다. v0.13.0 release 문서·unit expectation·native evidence schema도 이 잘못된 동작을 계약으로 고정했다.
- **판정:** 새 기능 추가가 아니라 v0.13.0에서 의도했던 Quick Panel-local 조작과 기존 absolute timeout/hover hold 동작의 누락·회귀를 복구한다. 따라서 `hotfix/v0.13.1`로 분류한다. 검증 정책 정정(`AGENTS.md`)도 이 hotfix의 task PR에 포함한다.

## 구현 계약

1. `계획 수정`, 기존 출근의 `출근 수정`, activity prompt의 `시간 수정`은 같은 inline editor frame·entry·error label·저장/취소 control을 재사용한다.
2. Quick Panel 시간 편집 경로는 `simpledialog`, 추가 `Toplevel`, modal grab 또는 nested wait를 만들지 않는다.
3. 출근 시각은 `00:00`–`23:59`, 목표 시간은 `00:00`–`24:00`으로 의미별 validation을 유지한다. 저장 실패 시 editor와 입력값을 유지하고 오류를 inline으로 표시한다.
4. Prompt 수정은 editor를 연 시점의 detected time을 context로 보존하고 저장 직전에 live prompt identity·vacation gate를 다시 검사한다. Stale prompt는 저장하지 않는다.
5. Hover enter/motion/leave는 active dismiss deadline이나 token을 취소·재arm하지 않는다. Deadline 전에는 hover 중에도 countdown이 감소한다.
6. Deadline 도달 시 hover 중이면 panel과 reusable window를 유지하고 `마우스 호버 중 · 이동 시 닫힘`을 표시한다. 이후 실제 leave가 전달되면 full timeout을 다시 시작하지 않고 즉시 withdraw한다.
7. Explicit inline editor와 command interaction의 기존 pause/rearm 정책은 유지한다. Hide/destroy/reopen 및 timeout 설정 변경은 stale callback이 새 lifecycle을 닫지 못하도록 token을 무효화한다.

## 변경 범위와 최소 검증

- Production: `src/apps/wrike_worktime_panel.py`, `src/apps/Wrike.py`
- Direct unit tests: 관련 case만 선택한 `tests/unit/test_wrike_worktime_panel.py`, `tests/unit/test_wrike_realtime_progress.py`
- Native contract: `scripts/qa_wrike_worktime_panel_native.py`의 inline-edit/hover-deadline scenario와 그 validator fixture인 `tests/unit/test_qa_wrike_worktime_panel_native.py`
- Contract correction: 이 문서 및 `docs/release-v0.13.0-quick-panel-private-ical.md`
- Artifact: targeted 검증 통과 후 `cmd /c build.bat`, final clean tagged-main metadata/read-back

전체 unittest discovery, private iCal E2E, vacation/break/focus 등 이번 변경과 무관한 native scenario, browser E2E는 실행하지 않는다. 동일 source tree에서 통과한 targeted evidence는 main/develop merge 후 반복하지 않는다.
