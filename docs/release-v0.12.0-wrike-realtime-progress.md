# Release v0.12.0 — Wrike 실제 기록 기반 실시간 근무 진척

## 변경 분류

- 의도한 계약: Wrike에 기재된 순시간을 실제값으로 사용하고, 출근·휴게·현재 시각·적용 목표로 현재까지 기대되는 순근무시간을 계산해 실시간 부족·초과를 보여준다.
- v0.11.0 동작: 현재 wall-clock 순경과를 최종 목표와 비교했다. Wrike 기록은 실시간 부족·초과 계산 입력으로 사용하지 않았다.
- 차이: 실제값과 기대값의 역할이 뒤바뀌어, PC를 오래 켜 둔 시간이 Wrike 진척과 무관한 초과로 표시될 수 있었다.
- 판정: 계산 계약 교정과 함께 월~일 Quick Panel 및 첫 활동 출근 확인이라는 새 사용자 기능을 도입하므로 `release/v0.12.0`으로 분류한다.

## 계산 계약

| 값 | 계약 |
| --- | --- |
| 실제값 | 현재 사용자의 해당 날짜 전체 Wrike timelog 합계다. monitor folder 선택과 무관하다. |
| 적용 목표 | `max(0, 날짜 목표 순근무 - 휴가 차감)`이다. |
| 현재 기대 | `min(적용 목표, max(0, 현재 시각 - 출근 시각 - 현재까지 경과한 병합 휴게))`다. |
| 실시간 편차 | `Wrike 실제 - 현재 기대`다. 음수는 부족, 0은 딱 맞음, 양수는 초과다. |
| 출근 전 | 현재 기대는 0이다. |
| 휴게 중 | 현재 기대는 증가하지 않는다. |
| 목표 도달 후 | 현재 기대는 적용 목표에서 고정된다. |
| Wrike 기록 고정 | 시간이 흐르며 현재 기대만 증가하므로 부족분이 실시간으로 커진다. |

점심, 별도 휴게 캘린더, 수동 휴게와 시간 지정 휴가 구간은 겹치거나 인접한 범위를 한 번만 차감한다. 시간 지정 휴가는 적용 목표를 줄이는 동시에 해당 시간 동안 기대 진척을 멈춘다. 종일 휴가는 적용 목표를 0으로 만들며 별도 경과 휴게로 중복 표시하지 않는다.

## Wrike snapshot 계약

- authoritative source는 `/contacts/{contact_id}/timelogs`로 조회한 현재 사용자 전체 기록이다.
- 월요일부터 일요일까지 정확히 7일을 한 snapshot으로 만들고 ID 중복을 제거한다.
- pagination token 순환, page 상한 도달, 잘못된 날짜·시간·ID 또는 부분 응답은 fail-closed 오류로 처리한다.
- 상태는 `unconfigured`, `loading`, `fresh`, `stale`, `error`로 구분한다.
- 갱신 실패 시 마지막으로 성공한 7일 값을 유지하되 stale/error 상태를 명시한다. 0분이나 wall-clock 값으로 조용히 대체하지 않는다.
- `%APPDATA%\windows-supporter\wrike_timelog_cache.json`에는 정규화한 날짜별 분, 표시 이름, 조회 시각, source scope만 atomic replace로 저장한다.
- cache는 token SHA-256 fingerprint와 결합해 다른 Wrike 계정의 값을 복원하지 않는다. token과 URL은 cache에 저장하지 않는다.

## Quick Panel

- `Ctrl+Alt+W` 또는 tray의 `Wrike 근무시간...` 메뉴로 singleton nonmodal panel을 toggle한다.
- 오늘 상세에는 Wrike 기록, 현재 기대, 현재 기준 편차, 출근, 예상 퇴근, 병합 휴게, 휴가 차감, 적용 목표와 동기화 상태를 표시한다.
- 주간 영역은 월요일부터 일요일까지 항상 7행을 표시한다.
- 과거일은 Wrike 실제와 최종 적용 목표를 비교하고, 오늘은 Wrike 실제와 현재 기대를 비교한다.
- 미래일에는 계획·휴가·적용 목표만 표시하고 부족 판정을 하지 않는다.
- 주말 기본 목표는 0이지만 사용자가 명시한 계획은 우선한다.
- 새로고침, 지금 출근/출근 수정, 휴게 시작/종료, 계획 수정, 설정 버튼을 제공한다.
- 1초 polling 중 동일 모델은 다시 그리지 않아 widget focus와 화면을 보존한다. model이 바뀔 때만 갱신한다.
- 활동 감지로 여는 panel은 keyboard focus를 빼앗지 않되 다른 창 뒤에 숨지 않도록 raise한다.

## 첫 활동 출근 확인

- Windows `GetLastInputInfo`를 `root.after`로 polling하며 raw keyboard/mouse hook을 설치하지 않는다.
- 오전 08:00 이후 해당 근무일의 첫 활동에서 출근 계획이 없고 목표가 양수이면 감지 시각을 제안한다.
- 사용자가 확인하기 전에는 출근 시각을 저장하지 않는다.
- 선택지는 감지 시각으로 출근, 시간 수정, 30분 후 다시 알림, 오늘 건너뛰기다.
- pending prompt는 계속되는 입력마다 다시 surface하지 않는다. 같은 프로세스에서 하루 한 번만 자동으로 올리고, 사용자가 30분 후를 선택한 경우에만 만료 뒤 한 번 더 올릴 수 있다.
- 암묵적 주말 휴무, 목표 0, 종일 휴가, 이미 출근한 날, 오늘 건너뛴 날에는 자동 prompt를 표시하지 않는다.
- prompt 상태는 `%APPDATA%\windows-supporter\wrike_worktime_state.json` schema v3에 저장하며 v2를 strict migration한다.

## 수명주기·보안 경계

- background stop은 activity watcher, refresh 요청과 panel polling을 중단하고 진행 중 generation을 무효화한다.
- session unlock은 last-input baseline을 재설정해 잠금 중 입력을 첫 활동으로 오인하지 않는다.
- shutdown은 watcher와 panel을 한 번만 정리하고 뒤늦은 snapshot 결과를 거부한다.
- Wrike token, 비공개 iCal URL, 이벤트 제목과 calendar ID는 panel, cache, 테스트 evidence 또는 로그에 출력하지 않는다.
- 실제 credential이 없는 검증은 synthetic snapshot/iCal을 사용한다.

## 릴리스 검증

1. 관련 계산·state·activity·snapshot·panel·integration·tray unit test
2. `uv run python -m unittest discover -s tests -p "test_*.py"`
3. `git diff --check`
4. `cmd /c build.bat`
5. packaged `windows-supporter.exe` startup/shutdown smoke
6. 800x540 native Tk UI에서 7일 행, 오늘 강조, 버튼 접근성, prompt, focus 비탈취, clipping 부재와 실제-vs-기대 변화 검증
7. clean tagged `main` 산출물의 `FileVersion`, `ProductVersion`, `Comments` read-back 및 `release-chain-gate` 확인
