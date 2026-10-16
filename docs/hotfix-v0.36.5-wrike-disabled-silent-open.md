# Wrike가 꺼져 있을 때 직접 열기의 안내 제거

## 분류와 계약

- 의도: Wrike를 끄면 근무시간 창은 자동으로도 직접 열어서도 나타나지 않아야 한다(2026-10-14 요청, v0.36.2 문서의 의도 1).
- 기존 동작: v0.36.2부터 Wrike가 꺼져 있을 때 `Ctrl+Alt+W`, 트레이 메뉴, 대시보드의 근무시간 열기가 창을 열지 않는 대신 "Wrike가 꺼져 있어 근무시간 창을 열지 않습니다. 대시보드나 Wrike 설정 탭에서 Wrike를 켜 주세요." 안내 풍선을 2초 동안 표시했다. 사용자가 화면 캡처로 이 풍선을 보이고, 아무것도 뜨지 않고 무반응이기를 원한다고 지시했다(2026-10-16).
- 차이: 직접 열어도 아무것도 나타나지 않는다는 의도에 안내 풍선이 맞지 않았다. 새 기능이나 새 정책 없이 화면 요소 하나를 없애는 표시 정리이며, 저장소 분류 기준(새 기능 없는 표시 정리는 hotfix)과 일치한다.
- 판정: 의도한 계약의 완성이므로 `hotfix/v0.36.5` 차선에서 고친다.

## 수정 범위

- `show_weekly_timelog_summary`(`src/apps/Wrike.py`)는 Wrike가 꺼져 있으면 아무 일도 하지 않고 돌아온다. 창을 열지 않고 Wrike를 조회하지 않으며 풍선, 대화 상자, 소리, 로그 줄도 만들지 않는다. `Ctrl+Alt+W`, 트레이 메뉴, 대시보드가 모두 이 함수를 쓴다.
- 안내 문구 상수 `WRIKE_DISABLED_NOTICE`와 안내 실패 로그(`disabled notice failed`)를 없앴다.
- 다시 켜면 이전처럼 열리고 Wrike를 조회한다. 자동 표시를 막는 규칙, 설정 저장, 대시보드의 "비활성화" 표시는 바꾸지 않는다.
- 안내가 사라지므로 꺼진 상태에서 단축키를 눌러도 이유를 알려 주는 곳이 없다. 켜는 곳은 대시보드의 Wrike 카드와 설정 탭이다.
- 확인 스크립트 `scripts/qa_wrike_disabled_ui.py`의 기대값과 v0.35.11, v0.36.2 문서의 해당 문장을 같은 변경에서 정정한다.

## 검증 범위

- 직접 영향 단위시험: `test_wrike_disabled_lifecycle`, `test_wrike_break_return_prompt`, `test_wrike_worktime_panel`(`test_real_tk_*` 제외), `test_wrike_break_edit_regressions`, `test_wrike_realtime_progress`, `test_monitor_hotkey`, `test_monitor_hotkeys`, `test_runtime_lifecycle`.
  나머지 시험까지 모두 318개가 통과한다(실제 `Tk` 창을 띄우는 `test_real_tk_*` 5개는 이 변경과 무관한 패널 배치 시험이라 제외).
- 바뀐 시험(`test_disabled_blocks_manual_open_without_any_reaction`, 이전 이름 `...and_explains_why`): 꺼진 상태의 직접 열기가 패널, 조회, 풍선, 활성 풍선 상태, 로그, 예약된 화면 작업 어느 것도 만들지 않고 안내 상수가 남아 있지 않음을 확인한다. 안내 실패 시험은 안내가 없어져 지웠다. 다시 켜면 열리는 기존 시험은 그대로 통과한다.
- 수정 전 소스(v0.36.4)에 덮어 실행하면 바뀐 시험이 실패한다. 변이 시험 8개(풍선을 다시 띄움, 로그 줄을 씀, Wrike를 조회함, 화면 작업을 예약함, 막는 분기를 없앰, 켜진 상태도 막음, 안내 상수를 남김, 풍선 상태를 남김)를 직접 영향 시험이 모두 잡는다.
- 변경된 화면 상태의 네이티브 확인: `scripts/qa_wrike_disabled_ui.py`(임시 설정, 네트워크와 자격 증명 차단) 26개 검사가 통과한다. 꺼진 상태의 직접 열기가 풍선을 만들지 않음("blocked manual open shows nothing at all")과 재활성화 뒤 복구를 포함한다.
- 전체 시험은 실행하지 않는다. 변경은 꺼진 상태의 한 분기에 한정되고 다른 소비 모듈을 모두 실행했다. 배포 산출물은 태그에서 한 번 만든다.
