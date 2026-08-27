# Release v0.11.0 — Wrike 근무시간 가시화 및 휴게 관리

## 요구 → 구현 매핑

| 요구사항 | 구현 위치 |
| --- | --- |
| 출근 시각 표시 | `Wrike.__resolve_clock_in_today` — 오늘 최초 timelog datetime(`trackedDate` 비자정 우선, `createdDate` fallback)과 앱 세션 시작 시각 중 최솟값 |
| 예상 퇴근 시각 | `wrike_worktime.project_quit_at` — 목표 순수 근무분이 소진되는 시각을 계획된 휴게와의 고정점 반복으로 산출 |
| 휴게 3원 소스 | 점심 고정 창(기본 12:00~13:00, 설정 변경 가능) + 구글 캘린더 iCal 일정(키워드 필터) + 수동 토글(ctrl+alt/b/버튼) |
| 현재 시각 기준 잔여 | `WorkdayOverview.as_lines` — "잔여 부족 N시간 M분 (HH:MM 기준)", 툴팁 표시 중 1초 주기 재렌더 (`RefreshableLines`, ToolTip refresh 계약 재사용) |

## 데이터/보안 경계

- Wrike API는 읽기만 하며 어떤 보정 기록도 쓰지 않는다(제품 계약).
- iCal 비공개 URL은 API 토큰과 동일하게 DPAPI(secret_store)로 보호 저장하며 UI 에코하지 않는다.
- 키워드가 비면 이벤트 매칭은 fail-closed로 아무 일정도 휴게로 취급하지 않는다.
- 설정 스키마 v5로 상승: 신규 기본키 자동 보정(기존 파일 무손실).

## 알려진 한계

- 툴팁 갱신은 'now' 기준 값만 다시 계산한다. 새 Wrike 기록 반영은 다음 폴링 팝업 주기를 따른다.
- TZID 파라미터(UTC 제외)는 로컬 벽시시각으로 취급한다(KST 국내 일정 전제, DST 없음).
- 수동 휴게 진행 중에는 예상 퇴근이 "-" 로 표시된다(재개 시각 미지).

## 검증

- `uv run python -m unittest discover -s tests -p "test_*.py"` → 1047 tests OK
- `cmd /c build.bat` 스모크 빌드 실행 후 exe 버전 확인
