# Release v0.11.0 — 실시간 근무시간·휴게·휴가 관리

## 변경 분류

- 의도한 계약: 날짜별 목표 순근무시간과 출근 시각을 사용자가 직접 입력하고, 현재 시각에서 병합된 휴게를 뺀 순근무시간으로 부족·초과와 예상 퇴근을 계산한다. 회사 휴가 캘린더는 해당 날짜의 목표시간을 자동 차감한다.
- 기존 v0.11.0 동작: 출근과 잔여시간을 Wrike timelog 및 앱 최초 관측 시각에서 유도하고, iCal 일정을 휴게로만 취급했다. 날짜별 계획과 수동 휴게 세션도 재시작 후 복원하지 못했다.
- 차이: 최초 요청 기능의 핵심 입력과 계산 기준이 누락되거나 반대로 구현됐다.
- 판정: 원격 `main`과 tag에 공개되지 않은 진행 중 `release/v0.11.0`에서 같은 기능의 불완전 구현을 교정한다.

## 제품 계약

| 요구사항 | 구현 계약 |
| --- | --- |
| 날짜별 계획 | 사용자가 `YYYY-MM-DD`별 목표 순근무 분과 `HH:MM` 출근 시각을 저장·불러오기·초기화한다. 날짜 목표 `0`도 허용한다. |
| 영속 상태 | `%APPDATA%\windows-supporter\wrike_worktime_state.json`의 state schema v2에 explicit `plan`과 수동 휴게 기록을 분리한다. 휴게만 있는 날짜는 당시 기본 목표를 고정하지 않는다. |
| 실시간 순근무 | `now - 수동 출근 시각 - 병합된 경과 휴게`로 계산한다. Wrike timelog, 앱 최초 관측 시각, 주간 목표는 입력으로 사용하지 않는다. |
| 부족·초과 | `적용 목표 - 실시간 순근무`가 양수면 부족, 0이면 달성, 음수면 초과로 표시한다. |
| 예상 퇴근 | 출근 시각, 적용 목표, 종료 시각이 알려진 휴게를 고정점 반복으로 반영한다. 수동 휴게 진행 중에는 재개 시각을 모르므로 `-`로 표시한다. |
| 휴게 | 설정 가능한 점심 창, 키워드로 선택한 별도 휴게 iCal 일정, 수동 휴게 타이머를 합친 뒤 겹치거나 인접한 구간을 한 번만 차감한다. |
| 휴게 타이머 | 버튼 또는 `Ctrl+Alt+B`로 시작·종료한다. 진행 상태와 완료 구간은 재시작 후 복원되며 자정을 넘는 구간은 날짜별로 분할한다. |
| 실시간 툴팁 | `Ctrl+Alt+W`는 Wrike API token 유무와 무관하게 `근무시간 (실시간)` 제목과 정확히 5개 행을 즉시 표시한다. 표시 중 값은 현재 시각 기준으로 갱신된다. |

## 휴가 캘린더

- 휴게 캘린더와 별도의 Google Calendar 비공개 iCal URL을 사용한다.
- 캘린더 이름은 정확히 `김종인-ePapyrus`여야 한다.
- 취소되지 않은 이벤트 중 NFKC 정규화한 `SUMMARY`에 `휴가`가 포함된 이벤트만 적용한다.
- 종일 휴가는 해당 날짜의 수동 목표 전부를 차감한다.
- 시간 지정 및 다일 휴가는 날짜 경계로 잘라 적용한다. 겹치거나 인접한 휴가 구간은 합친 뒤 한 번만 계산하고, 차감분은 해당 날짜 목표를 넘지 않는다.
- 적용 목표는 `max(0, 수동 목표 - 휴가 차감)`이다. 휴가는 휴게 구간으로 추가하지 않는다.
- 조회 실패나 캘린더 이름 불일치 시 기존 cache를 즉시 비워 오래된 휴가 차감이 남지 않게 한다.

## 데이터·보안 경계

- Wrike API는 기존 기록 조회 기능에만 사용하며 실시간 근무 계산에는 사용하지 않는다.
- Wrike API token과 휴게 iCal URL은 기존 Wrike DPAPI store의 서로 다른 protected field로 저장하고, 휴가 iCal URL은 별도 DPAPI purpose/store로 저장한다.
- 휴가 URL은 HTTPS의 `calendar.google.com` 또는 `*.googleusercontent.com`만 허용하며 redirect 최종 URL도 다시 검증한다.
- iCal 응답은 최대 2 MiB까지만 읽고 UTF-8(BOM 허용)을 strict decode한다.
- UI와 로그에는 비공개 URL, calendar ID, 이벤트 제목, token을 출력하지 않는다. UI에는 설정 여부, 기대/확인 캘린더 이름, 성공 시각, 고정 오류 코드의 번역만 표시한다.
- 실제 비공개 URL이 없을 때는 synthetic iCal로 계산·오류 상태를 검증하며, URL 입력 후 자동 polling 가능한 상태까지를 릴리스 acceptance로 삼는다.

## 알려진 한계

- UTC 이외 `TZID` 파라미터는 로컬 wall-clock 시각으로 취급한다. 이 릴리스의 회사 캘린더는 `Asia/Seoul`을 전제로 한다.
- 반복 일정은 현재 `DAILY`·`WEEKLY`, `INTERVAL`, `BYDAY`, `UNTIL`, `COUNT`, `EXDATE` 범위를 지원하며 탐색을 900일로 제한한다.
- 휴가 iCal URL의 실제 운영 조회 성공은 사용자가 전용 비공개 URL을 입력한 이후에만 확인할 수 있다.

## 릴리스 검증

1. `uv run python -m unittest discover -s tests -p "test_*.py"`
2. `cmd /c build.bat`
3. `windows-supporter.exe`의 `FileVersion`, `ProductVersion`, `Comments` read-back
4. 800x540 native Tk UI에서 초기·계획 저장·휴게 진행·휴가 설정·입력 오류·부족/초과 툴팁 상태의 시각 및 상호작용 검증
5. synthetic iCal로 종일·시간·다일·중복/인접·이름 불일치·취소 이벤트 및 URL 오류 검증
