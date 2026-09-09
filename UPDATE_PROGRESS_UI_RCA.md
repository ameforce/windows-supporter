# 업데이트 진행 UI RCA

## 증상

- 창 둘레에 24px 안팎의 회색 matte가 보이고, 흰색 패널이 창 안에 한 번 더 들어간 것처럼 보인다.
- `최근 작업 로그`가 비어 보이지만 실제 최신 build 출력은 존재한다.
- `build.bat 단계: ... [ Not running ]` 같은 개발자 원문이 사용자 상태 문구와 함께 노출된다.
- 고정 `600x360`, borderless 창, 고정 wraplength 때문에 DPI·긴 문구·키보드 조작에서 잘림과 창 관리 위험이 있다.

## 실제 원인

1. root/outer 배경 `#EEF2F7`과 outer `padx=24`, `pady=22`가 흰 shell 밖으로 그대로 노출된다. 제공 PNG의 `(0,0)/(23,23)=#EEF2F7`, `(24,24)=#D8DEE9`, `(25,25)=#2563EB` 픽셀이 코드 경계와 일치했다.
2. 실제 Tk에서 root는 `600x360`인데 자연 요청 크기는 최대 `626x403`이었다. activity frame은 실제 `506x91`, 요청 `508x133`으로 42px 부족했다.
3. activity label 4개를 빈 값이어도 모두 pack하고, 한 줄 이력은 `[빈 행, 빈 행, 빈 행, 최신 행]` 순서로 배치한다. 최신 행이 `1x1`, `mapped=False`가 되어 로그가 없는 것처럼 보였다. 높이를 `403`으로 늘리거나 최신 행을 먼저 배치하면 즉시 보였고, 원상복구하면 다시 숨었다.
4. build stdout parser가 같은 원문을 `detail`과 `activity.line`에 복사한다. `[ Not running ]`은 정상적인 "이미 종료됨" 결과인데도 실패처럼 읽힌다. 미인식 PyInstaller 로그 줄 수가 percent를 1%씩 올려 실제 작업량과 진행률도 분리되어 있었다.
5. `overrideredirect(True)`가 native titlebar와 표준 창 이동·시스템 메뉴 책임을 제거했지만, 대체 drag/키보드/활성 상태 계약은 없었다.

## 개선 seam

- build stdout의 transport는 유지하고, `update_monitor.py`의 parser/reducer 경계에서 구조화 `stage id`, 사용자용 한국어 detail/activity, raw log를 분리한다.
- UI root는 짧게 표시되는 topmost helper라는 제품 성격과 사용자가 선택한 시안에 맞춰 borderless로 유지하되, client 영역을 edge-to-edge white shell, 1px 경계, 3px accent로 정리한다. 별도 가짜 캡션/X 버튼은 만들지 않고 제목·부제만 명시적 drag surface로 제공한다. 최초 map은 `withdraw → 구성 → overrideredirect → deiconify` 순서로 하고 4px 이동 임계값과 focus/unmap drag 취소를 둔다.
- activity frame은 실제 이력이 있을 때만 geometry manager에 넣고, 카드 경계 없이 점·guide 타임라인과 최대 3개 한국어 단계만 표시한다.
- 창 크기는 percent마다 바꾸지 않고 `진행/진행+activity/실패/완료` 레이아웃 상태가 바뀔 때만 DPI 배율에 맞춘 크기로 계산한다.
- 실패/취소/완료만 닫을 수 있게 하고, 실행 중 닫기 요청은 무시한다. 실패·취소 시 실제 조치 버튼과 로그 경로를 유지한다.
- `build.bat`에는 검증 전용 artifact-only 경로를 두어 동명 프로세스 종료, worktree root exe 교체, post-build launch를 모두 피한다.

## 회귀 위험

- raw 로그를 숨기면서 진단 정보까지 잃을 수 있음: 전체 stdout/stderr는 기존 update log에 유지한다.
- retry에서 이전 attempt의 percent/activity가 섞일 수 있음: `handoff_start`에서 UI 이력은 새로 시작하되, 마지막 유효 percent floor는 이어받는다.
- 동적 높이로 창이 흔들릴 수 있음: 상태 클래스와 activity 유무가 바뀔 때만 geometry를 재계산한다.
- borderless가 taskbar·Alt+Tab·Snap·시스템 메뉴를 제공하지 않음: 이 정책을 짧은 topmost 업데이트 helper로 한정하고, 제목·부제 drag와 Escape/Alt+F4/`WM_DELETE_WINDOW`의 동일한 상태 기반 close guard를 테스트로 고정한다.
- Tk의 `-10` geometry는 음수 절대 X가 아니라 우측 기준 offset으로 해석됨: drag 좌표는 `+-10` 형태가 되도록 각 축 앞에 `+` separator를 명시하고, `(-10, -20)` 실제 `winfo_x/y` 회귀 테스트와 smoke metric을 둔다.
- DPI 캡처가 개발용 Python과 frozen exe에서 다를 수 있음: fresh process의 Tk scaling/창 rect를 기록하고, 최종 frozen exe도 별도로 스모크한다.

## 검증 방법

- 메시지 정규화, stage 기반 percent, raw 원문 비노출, activity 실제 collapse/visibility, 실패·취소 action, 긴 문자열 정규화, artifact-only guard를 단위 테스트로 고정한다.
- 초기/중간(activity 없음·있음)/실패/완료/긴 문자열을 100%·125%·150% 동등 Tk scaling의 fresh process에서 캡처하고 widget rect/clipping을 함께 검사한다.
- updater 대상 테스트, 전체 unittest discovery, compile 검사, artifact-only `build.bat`, 완성 EXE 단기 실행을 순서대로 수행한다.

## 검증 경계

- 125%·150%는 Tk client 영역의 동등 scaling으로 검증한다. 실제 Windows 디스플레이 DPI 120/144에서 모니터 간 이동과 `WM_DPICHANGED`는 이번 환경에서 별도로 검증하지 못한다. 음수 좌표를 막지 않는 drag geometry와 캡처별 widget rect로 최소 회귀를 확인한다.
- 완료 상태 fixture의 렌더링은 검증했지만, production helper가 완료 snapshot 직후 창을 닫는 기존 수명 주기는 보존했다. 따라서 완료 화면의 실제 체류 시간은 별도 UX 후속 검토 대상이다.
- 이번 산출물은 commit 전 dirty worktree 검증용이다. 후속 릴리스에서는 clean commit 기준으로 테스트 결과와 최종 EXE SHA256을 다시 연결해야 한다.

## 진행률 점프 RCA (2026-09-09)

### 증상

- Release installer 다운로드가 새 helper 프로세스에서 `0%`로 다시 시작한 뒤, 고정된 `54%`로 점프했다.
- 부모 updater의 Git 사전 처리와 child handoff가 서로 다른 진행률 lifecycle을 사용해 단계 사이의 큰 점프가 발생했다.
- build output은 `20/24/32/...`처럼 드문 marker만 고정 percent에 매핑되어, 실제 빌드 초반 작업이 진행되어도 화면이 오래 멈추거나 다음 marker에서 크게 뛰었다.

### 근본 원인

1. `UpdateProgressStep`이 단계별 단일 percent만 보유해 단계 내부의 진행을 표현할 수 없었다.
2. 부모 프로세스가 handoff state를 기록해도 child helper가 `handoff_start=0`으로 lifecycle을 다시 만들었다. Release 경로는 이 reset 직후 고정 `release_download=54`를 게시했다.
3. 다운로드는 1 MiB 단위로만 읽고 callback이 없어 byte 진행률을 계산할 근거가 없었다.
4. build/deploy/cleanup은 실제 세부 marker와 별도의 고정 지점만 사용했고, deployment 이후 구간도 relaunch percent로 한 번에 건너뛰었다.

### 개선 계약

- 모든 visible phase는 start/end percent range를 갖고, 알려진 byte/build 이벤트는 해당 range 안에서만 보간한다.
- handoff payload와 child helper는 마지막 유효 percent를 공유하며, helper와 retry 모두 monotonic floor 아래로 내려가지 않는다.
- Release 다운로드는 64 KiB 단위 callback으로 누적 byte/Content-Length를 게시한다. 중간까지 길이를 알 수 없으면 시작 percent와 byte detail을 유지하고, 완료 callback에서는 누적 byte를 실제 total로 사용해 종료 구간까지 반영한다.
- Git handoff는 `git_shutdown → build_prepare → build → deploy → cleanup_build → relaunch`로 이어지며 build marker와 배포/정리 경계를 각각 화면에 게시한다.
- installer 실행처럼 내부 진행률을 관찰할 수 없는 작업은 가짜 시간 기반 증가를 만들지 않고 시작/완료 경계만 표시한다.

### 검증

- Release download callback, phase range/monotonic floor, handoff state carry, build/deploy/cleanup stage를 targeted unit test로 고정한다.
- Tk UI smoke에서 inherited percent와 build marker percent를 확인한다.
- artifact-only build로 실제 runtime 교체 없이 packaging과 update handoff 경로를 다시 확인한다.
