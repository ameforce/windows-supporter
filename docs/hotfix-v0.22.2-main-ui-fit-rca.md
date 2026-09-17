# hotfix/v0.22.2 메인 UI fit RCA

## 분류와 의도

- 의도한 계약: 트레이 아이콘을 더블클릭하면 Windows Supporter 메인 UI가
  선택된 탭의 콘텐츠를 읽을 수 있는 크기로 한 번만 표시되고, 현재 모니터의
  작업 영역 밖으로 나가지 않아야 한다. 작은 작업 영역에서는 콘텐츠 스크롤로
  접근성을 보존한다.
- 현재 동작: 메인 창은 탭별 고정 크기를 Tk scaling에 곱해 시작하고, 창을 먼저
  표시한 뒤 탭을 mount하고 다시 geometry를 바꾼다. Dashboard는 canvas 안의
  frame을 사용해 실제 콘텐츠 요구 크기가 root의 requested size로 전파되지
  않는다.
- 차이: 2560x1440 / Tk scaling 125% fixture에서 Dashboard 콘텐츠는
  `965x491`인데 root client는 `1350x825`로 열려 하단에 큰 빈 영역이 남았다.
  반대로 withdrawn root는 최초 layout 시 1x1로 측정되어 Dashboard가 1열로
  접힌 `497x764` 요청 크기를 만들 수 있었다. 탭 전환 시에는 작업 영역의
  크기만 clamp하고 origin을 사용하지 않아 resize 후 창 위치가 화면 밖으로
  밀릴 여지도 있었다.
- 판정: 새 제품 기능이 아니라 기존 "콘텐츠가 보이는 메인 UI" 계약의 불완전
  구현을 복구하는 hotfix다.

## 근본 원인

1. `_tab_sizes`가 실제 콘텐츠를 측정하지 않는 fallback인데도 정상 경로의
   preferred geometry처럼 사용되어 Dashboard의 `1350x825` 빈 영역을
   예약했다.
2. Dashboard canvas의 requested size는 embedded container에 의존하지 않아
   Tk geometry manager가 `948x491` 콘텐츠 요구를 root로 알릴 수 없었다.
3. `show()`가 `deiconify()`를 콘텐츠 build/geometry 적용보다 먼저 호출했다.
   또한 숨겨진 Tk root는 최초 layout 중 1x1이므로 측정 시점에 Dashboard가
   잘못된 1열 상태로 collapse될 수 있었다.
4. geometry 적용은 작업 영역의 width/height만 계산하고 monitor origin을
   반영하지 않았다. 기존 최소 크기 계산도 scaling된 값을 계산한 뒤 raw
   logical 값을 다시 사용해 고배율에서 일관성이 없었다.

## 개선 내용

- Dashboard가 embedded frame과 scrollbar를 포함한 `preferred_size()`를
  노출하고, 메인 shell은 실제 탭 requested size + notebook/footer chrome를
  기준으로 창을 계산한다. 측정할 수 없는 탭만 기존 `_tab_sizes` fallback을
  사용한다.
- 첫 측정이 withdrawn 1x1 layout에 오염되지 않도록 Dashboard intrinsic
  2열 layout을 먼저 seed한다. 최종 window가 좁아지면 기존 Configure binding이
  1열과 vertical scroll로 전환한다.
- 숨겨진 창은 탭 build와 fit을 끝낸 뒤 `deiconify()`한다. geometry는 현재
  monitor work-area rect의 origin을 포함해 center/clamp한다.
- 콘텐츠 측정 후 작업 영역 상한을 적용하고, 좁은 창에서 wrap 높이가 바뀌는
  경우를 위해 최대 2회만 재측정한다. 상태 refresh마다 geometry를 바꾸지는
  않는다.

## 검증

- `tests.unit.test_main_ui_dashboard`
- `tests.unit.test_main_ui_codex_layout`
- Dashboard native fixture: 2560x1440 / Tk 125%에서 root
  `969x552`, Dashboard content `965x491`, 2열 카드 유지, client clipping 없음.
- Tk 150% fixture에서는 root `1047x609`, Dashboard content `1043x550`,
  2열 카드가 유지됐다.
- `800x500` small-work-area fixture에서는 root `768x452`, content canvas
  `747x401`, 카드가 1열로 전환되고 content frame은 `747x847`로 세로
  스크롤됐다. 가로 overflow는 발생하지 않았다.

실제 서로 다른 DPI 모니터 사이의 `WM_DPICHANGED` 이동은 이 환경에서 별도로
검증하지 못했으며, Win32 작업 영역 rect를 매번 다시 읽는 경로와 scroll fallback을
대상으로 회귀를 고정한다.
