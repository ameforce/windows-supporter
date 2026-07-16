# 업데이트 진행 UI Design QA

## 판정 범위

- 방향: `design-reference-1-seamless-shell.png`의 안정적 구조와 `design-reference-3-status-focus.png`의 가벼운 점·선 타임라인 조합
- 실제 구현: `UpdateHandoffProgressUi`의 borderless Windows Tk 화면
- 상태: 초기, 중간 activity 없음/있음, 실패, 완료, 긴 문자열
- 배율: 100%, 125%, 150% 동등 Tk scaling; 200% canary 추가

## 비교 증거

- reference 1: `1672x941`, SHA256 `87FED64795666085C2834A0C4E3DF78AFEF58C95C3C9E6B6F7D2F163BF2BA0CA`
- reference 3: `1774x887`, SHA256 `4D3D33AB285049C532C4122C178DAF411306B1EF6EFB4BB99B355F40DE1135AE`
- actual 100% activity: `600x362`, SHA256 `E647B2F61D79940E9296108778B7081BF288DF839D48ACD62DD4FE5813BB7E0E`
- actual 100% 실패: `600x377`, SHA256 `5298B79C6640BC3FE2B82C9B56C2E20825B81E20B4B81CC0C01FB05F35044C1F`
- actual 100% 완료: `600x362`, SHA256 `081C1FC6EC6F41CBA8F0148796E8E5146D55BC9A6448DF503EDF01249E4F71B4`
- actual 150% 긴 문자열: `899x706`, SHA256 `8924AF5094B3A2DDAFCE4D5954B5D58A728875A9937A1144AA32E41E4BA333A6`

참조는 고해상도 방향성 mock이고 실제 구현은 기존 제품의 600px 정보 구조와 Segoe UI 크기를 보존하므로 원시 픽셀 일치율은 합격 기준으로 사용하지 않았다. 두 독립 reviewer에는 참조 2장과 fresh actual 상태 캡처를 같은 검토 packet으로 제공했다.

## 자동·행동 검증

- `matrix-final6-summary.json`: 18/18 `ok=true`, `clipped_widgets=[]`
- 모든 캡처: `borderless_shell=true`, Alt+F4 binding 존재
- drag: 양수 이동 `(+24,+16)`과 음수 절대 좌표 `(-10,-20)` 모두 실제 `winfo_x/y` 일치
- activity: 내용이 없으면 geometry manager에서 제거, 있으면 최근 3개만 표시
- 200% canary: 고정 timeline 간격 문제를 발견해 실제 label 요청 높이 기반으로 수정 후 재현 캡처 통과
- 최초 map: `withdraw → 구성 → overrideredirect → deiconify`, map 후 0% progress track 재그리기 검증

## 독립 검토

- pixel/layout reviewer: 최종 `matrix-final6` PASS, blocking issue 없음
- code-level reviewer: 음수 geometry 결함을 1차 발견; 수정 후 최종 `matrix-final6` 재검토 PASS, blocking issue 없음

## 잔여 위험

- `overrideredirect(True)`는 native taskbar, Alt+Tab, Snap, 시스템 메뉴를 제공하지 않는다. 사용자가 승인한 짧은 topmost updater helper에만 한정한다.
- 100/125/150%는 동일 디스플레이에서 Tk scaling을 바꾼 동등 검증이다. 실제 서로 다른 DPI 모니터 사이의 `WM_DPICHANGED`, taskbar/Alt+Tab 복구성은 별도 운영 환경 검증이 필요하다.
- Figma Starter MCP 호출 한도 때문에 기존 감사 보드의 최종 after 갱신은 차단됐다. 유료 업그레이드는 요청하지 않았다.

final result: passed
