# v0.8.4 AI 사용량 Hotfix RCA

## 분류 판정

v0.8.4는 기존 AI 사용량 기능의 의도한 동작을 복구하는 hotfix다. 저장 가능한 프로필 수와 작업표시줄 표시 상한이 구현에서 함께 2개로 묶였지만, 확인된 제품 계약은 `저장 N개, 작업표시줄 표시 최대 2개`였다. 따라서 가변 프로필 스키마와 내부 마이그레이션의 규모만으로 release로 승격하지 않는다.

- 의도한 계약: 프로필은 제품 상한 없이 저장하고, 작업표시줄에는 Codex/Cursor 합산 최대 2개만 선택한다.
- 현재 동작: `account_1`/`account_2` 고정 슬롯, provider별 불완전한 메트릭·상태·레이아웃, provider 전환 잔상.
- 차이: 버그, 회귀, 누락 및 불완전 구현.
- 판정: `hotfix/v0.8.4`.

## 공통 구조 원인

v0.8.0의 provider 일반화가 저장 모델, 수집 생명주기, 표시 메트릭, 상태 투영, UI 재구성까지 하나의 provider-neutral 계약으로 완성되지 않았다. 저장 상한과 표시 상한이 결합됐고, Codex 전용 필드와 고정 UI 행이 Cursor 경로에 남았으며, 수집 실패의 내부 상태가 사용자 상태로 바로 노출됐다. provider 변경도 child monitor, snapshot, label, metric UI를 한 트랜잭션으로 교체하지 않았다.

구현 후 exact-head 독립 리뷰에서는 child/browser worker 종료의 성공 여부가 일부 경로에서 증명되지 않는 문제도 확인됐다. `shutdown()`의 암묵적 `None`을 성공처럼 취급하거나, 종료를 기다리는 동안 settings/refresh lock 순서를 뒤집거나, URL·provider 변경 중 살아 있는 기존 owner 위에 새 session/child를 게시할 수 있었다. 최종 구현은 session → provider monitor → multi-manager 전 구간에서 명시적 `bool` 종료 계약을 사용하고, 결과가 정확히 `True`가 아니면 unsettled child와 recovery-pending 상태로 격리한다.

## 이슈별 판정 및 수정

| 이슈 | 합리성 | 직접 원인 | 구조 원인 | 수정 및 회귀 잠금 | 종료 판정 |
|---|---|---|---|---|---|
| #3 Cursor 표시명 | 합리적 | Cursor runtime에 안정적인 `profile_name` 전달 경로가 없고 설정 라벨만 사용했다. | identity와 label 정책이 Codex 전용이었다. | `label_mode=auto/custom`, provider가 제공한 안정적 표시명 우선, custom fallback과 provider 왕복 테스트를 추가했다. | 구현 완료, 실계정 Cursor 확인 전 열린 상태 |
| #4 빈 Codex 5H | 합리적 | snapshot에 실제 5H 값이 없어도 고정 5H metric/GUI 행을 만들었다. | 렌더링 계약이 snapshot 존재 여부가 아니라 레거시 Codex 스키마에 결합됐다. | 실제 값이 없는 5H는 생성하지 않는 red test와 manager/UI/taskbar 계약 테스트를 추가했다. | 구현 완료, 실 Codex 응답 확인 전 열린 상태 |
| #5 reset/압박 표시 | 합리적 | reset 키 불일치와 ISO·한국어 날짜 정밀도 손실로 taskbar `reset_at`이 비었다. | provider boundary 정규화 없이 각 표시 계층이 원문을 다시 해석했다. | reset을 provider boundary에서 ISO/date precision으로 정규화하고 date-only는 `D-n`, datetime은 시·분 countdown으로 표시한다. | 구현 완료, 실 provider reset 확인 전 열린 상태 |
| #6 Cursor OD OFF | 합리적 | `on_demand_enabled=false`여도 OD metric을 항상 생성했다. | provider metric 목록이 실제 capability/state와 무관하게 고정됐다. | OD OFF/부재 시 OD를 만들지 않는 red test와 taskbar 계약을 추가했다. | 구현 완료, 실 Cursor capability 확인 전 열린 상태 |
| #7 주기적 ERR | 증상은 합리적, 최초 실환경 트리거는 미확정 | 기존 구현은 transient/recycle/auth/profile-in-use를 구분하지 않고 ERR로 투영했고 Cursor 연속 실패 정보가 부족했다. | 성공 cache, typed error, retry, scheduler owner가 하나의 상태 기계로 관리되지 않았다. | 성공 cache+transient=`STALE`, auth=`LOGIN`, profile-in-use=`PAUSED`, usable cache 없는 terminal failure만 `ERR`; Cursor `failure_count`·typed error·structured log, profile별 due-time와 global single-flight serial queue를 추가했다. 복구·삭제 race·batch starvation red test도 추가했다. | 원래 실환경 주기 트리거를 직접 관측하지 못했으므로 열린 상태 유지 |
| #8 taskbar 값 겹침 | 합리적 | 긴 full amount가 percent용 고정 폭 bar 위에 그려졌다. | GUI 상세값과 taskbar compact 값의 표현 계약이 분리되지 않았다. | GUI/tooltip은 full amount, taskbar는 compact percent/short value를 사용하고 긴 값 fixture를 추가했다. | 구현 완료, 물리 taskbar/DPI 확인 전 열린 상태 |
| #9 AI 탭 잘림 | 합리적 | 고정 geometry가 동적 카드 높이와 작은 work area를 반영하지 않았다. | 프로필 수가 변해도 정적 2카드 레이아웃을 유지했다. | scrollable dynamic cards, content/work-area 기반 sizing, mouse wheel·keyboard scroll을 구현하고 시각 fixture를 추가했다. | 구현 완료, 물리 DPI/work area 확인 전 열린 상태 |
| #10 저장 프로필 2개 상한 | 제품 계약상 결함 | `ACCOUNT_IDS`와 `[:2]`가 저장·수집·UI 전체를 고정했다. | 저장 상한과 작업표시줄 표시 상한을 같은 정책으로 취급했다. | provider-neutral v4 settings/state, path-safe opaque ID, add/delete/reorder, v2/v3 rollback 보존, 기존 `account_1/account_2` 무손실 이전을 구현했다. 세 번째 taskbar 선택은 manager와 UI에서 원자적으로 거부한다. | 구현 완료, release migration/live 확인 전 열린 상태 |
| #11 provider 전환 잔상 | 합리적 | provider 변경 후 UI를 remount하지 않았고 구 label payload가 기본 label 교체를 되돌렸다. | child runtime, snapshot, label, metric UI 교체가 원자적이지 않았다. | provider switch가 child monitor/runtime/snapshot/label/metric UI를 함께 교체하고 실패 시 rollback하며 custom label은 보존한다. | 구현 완료, 실 provider 전환 확인 전 열린 상태 |
| #12 집계 | 합리적 | 위 결함들의 공통 추적 이슈다. | v0.8.0 AI 사용량 일반화의 미완성 경계를 한곳에서 추적할 필요가 있다. | #3~#11 결과와 v0.8.4 검증을 집계한다. | #7 실환경 RCA가 남아 열린 상태 유지 |

## 이슈별 증거와 closure 한계

- #3: auto/custom label 및 provider round-trip **fixture/unit test**로 경로를 증명했다. 실제 Cursor 계정의 안정적 profile_name 수집은 자격 증명 부재로 미검증이므로 이슈 댓글에는 그 한계를 남기고 release 후 실계정 확인 전 닫지 않는다.
- #4: 빈 metric fixture와 manager/UI/taskbar **red test**로 실제 값 없는 5H 행의 비생성을 증명했다. 실제 Codex 페이지의 모든 변형은 미검증이다.
- #5: 한국어·ISO·date/datetime reset **unit fixture**로 provider-boundary 정규화와 표시 정밀도를 증명했다. 실제 provider 응답의 미지 포맷은 release 후 관찰 대상이다.
- #6: `on_demand_enabled=false` fixture와 taskbar **contract test**로 OD 비생성을 증명했다. 실제 Cursor 계정 capability 응답은 미검증이다.
- #7: typed error/cache/retry/global queue **unit·race test**는 수정했지만 최초 실환경 DOM/recycle 트리거는 관측하지 못했다. 따라서 열어 둔다.
- #8: 긴 금액 fixture와 taskbar compact rendering **test**로 겹침 방지 경로를 증명했다. 실제 Windows taskbar 폭/DPI는 물리 검증 한계다.
- #9: native Tk 0/1/3/10 profile × 100/125/150% scaling capture 및 mouse wheel/keyboard test가 있다. 물리 이종-DPI·OS work area는 미검증이다.
- #10: v3→v4, 0/1/3/20, legacy ID/path/order, add/delete/reorder, third selection rejection **migration·manager/UI test**로 증명했다. 실제 장기 사용자 설정의 모든 변형은 backup/rollback 관찰 대상이다.
- #11: provider round-trip, snapshot/label/metric remount, shutdown-failure recovery-pending **unit test**로 증명했다. 실제 provider 로그인 전환은 자격 증명 부재로 미검증이다.
- #12: 위 증거와 한계를 집계하며 #7 및 실계정·물리 DPI 한계가 남아 있으므로 열어 둔다.

## 데이터 및 삭제 안전성

- v4가 canonical settings/state이며 기존 v3/v2 파일은 rollback 자료로 보존한다.
- 기존 `account_1`/`account_2`의 ID, provider, 로그인 상태, 관리 경로와 순서는 유지한다.
- 새 ID는 path-safe opaque ID로 만든다.
- 삭제는 명시 확인 뒤 app-owned root 경계와 reparse/path traversal을 검증하고 해당 프로필 데이터만 처리한다.
- 삭제 journal은 경로 격리 전에 durable하게 기록하고, 취소·종료가 불확실하면 삭제와 fresh child 게시를 중단한다.
- add/delete/provider switch/settings rollback은 기존 owner의 종료가 확인되기 전 같은 프로필 경로에 새 owner를 정상 게시하지 않는다.
- 공개 CLI나 외부 설정 API를 비호환 변경하지 않는다.

## 구현 후 리뷰에서 보강한 경합 방어

- refresh enqueue와 shutdown은 atomic cancel/token 경계를 공유하고, settings lock을 잡은 채 worker quiesce를 기다리지 않는다.
- startup·dispatch·queue-put 사이 취소 창을 닫고, shutdown 뒤 queued callback이 child state를 부활시키지 못하게 worker epoch와 manager token을 검증한다.
- `CodexUsagePlaywrightSession.shutdown()`은 실제 owner thread 종료 여부를 반환한다. poisoned owner가 살아 있으면 `False/FAILED`, 이후 정리되면 재호출에서 `True/STOPPED`다.
- usage URL 변경은 기존 browser session의 종료 결과가 `True`일 때만 새 session을 만든다. `False`·`None`·예외이면 기존 URL/session을 보존하고 상위 settings transaction에 실패를 전달한다.
- 새 URL session과 이전 URL rollback session 생성이 모두 실패하면 이미 종료된 session을 되살리지 않고 명시적 `FAILED` sentinel을 게시한다. 이후 fresh session 생성이 성공해야만 recovery 상태를 해제한다.
- provider뿐 아니라 실제로 달라지는 Codex usage URL도 profile refresh quiescence 대상이다. active refresh 중 URL-only 저장은 `profile_refresh_busy`로 거절하고 autosave로 다시 수렴한다.
- provider 변경 autosave가 active refresh로 `profile_refresh_busy`를 받으면 dirty UI payload를 재예약해 durable settings와 자동 수렴한다.
- Tk `after()` 등록 자체가 실패한 teardown 경로에서는 transient autosave를 동기 재호출하지 않아 무한 재귀를 막는다.
- add/delete/external settings mutation 동안 프로필 query/login/disconnect는 handler에서 거부하고 버튼도 비활성화한다.
- profile release worker도 action token으로 추적한다. release가 끝나기 전 같은 profile 삭제나 설정 변경을 시작하지 않고, UI post·thread start 실패에서도 token을 해제한다.
- UI queue가 callback 게시를 거부해 `False`를 반환하면 성공으로 오인하지 않는다. release/add/delete worker는 게시 실패를 직접 인지해 action token과 mutation guard를 해제한다.
- profile release 전에 manager 범용 cancel을 호출하지 않는다. 범용 cancel은 Codex/Cursor browser session을 terminal로 만들므로 provider의 non-terminal `release_profile_session()`이 자체 collect lock과 취소를 담당하고, 성공 뒤 manager active refresh가 끝났는지 확인한다.
- release/delete/provider switch/shutdown은 별도 lifecycle lock으로 직렬화한다. child release와 refresh quiescence 중에는 settings lock을 놓아 child callback의 `get_runtime_status()`와 lock inversion이 생기지 않게 하고, shutdown은 lifecycle lock을 기다리기 전에 closing을 설정해 늦은 mutation을 즉시 거부한다.
- browser `collect()`의 factory-start·retry-wait 경계를 별도로 추적한다. 취소할 driver가 없으면 완료를 거짓 보고하지 않고 retry wait는 cancel event로 즉시 깨운다.
- Cursor lazy browser도 inner session 생성 중 terminal 요청을 별도 상태로 기록한다. 생성 완료 뒤 cancel+shutdown이 `True`로 확인되기 전에는 session을 게시하거나 collect/login poll을 시작하지 않는다.
- Cursor lazy browser의 terminal cleanup이 `False`이면 생성된 inner session 참조를 버리지 않고 보존한다. 이후 `shutdown()` 재호출이 정확히 `True`가 될 때만 참조와 terminal cleanup 상태를 정리한다.
- multi-manager shutdown은 refresh quiescence와 모든 child의 최종 `shutdown() is True`를 집계해 호출자에게 `bool`로 전파한다.

## 검증 계약

- 마이그레이션: v3→v4, 0/1/3/20 profiles, 기존 경로·ID·순서·기본값 보존.
- 프로필: add/delete/reorder, provider round-trip, 세 번째 taskbar 선택의 원자적 거부.
- 메트릭: 빈 5H, OD OFF, 한국어/ISO reset, date precision, compact amount.
- 런타임: cached success→transient error→recovery, auth, profile-in-use, retry exhaustion, global serial collection, 삭제/provider switch race, batch 예외 격리.
- UI: 1/3/10 mixed profiles, 긴 한국어·영문 이름, 작은 창, 100/125/150% Tk scaling, keyboard/mouse wheel scroll, taskbar 선택 정확히 2개.

`40f71fd785036a571713c643bb907f6a255a7bac`의 917-test/build/capture 수치는 **초기 task PR의 역사적 증거**일 뿐, 현재 hotfix tip 또는 release closure 증거가 아니다. 현재 release 전 final evidence는 모든 task PR merge 뒤 hotfix exact tip에서 새로 실행해 기록한다. task PR base/head가 바뀌면 해당 PR의 review·test·build·visual 증거는 stale이며 같은 exact pair에서 다시 확인한다.

## 검증 한계

- 125/150% 결과는 동일 Windows 호스트의 Tk scaling 시뮬레이션이며 물리 모니터 DPI 전환 증거가 아니다.
- 작은 work area 검증은 축소 창 fixture이며 실제 OS 작업 영역을 바꾼 결과가 아니다.
- #7의 원래 주기적 ERR를 발생시킨 실제 Cursor DOM/recycle 이벤트는 관측하지 못했다. 구조적 상태 투영 결함과 재현 가능한 race는 수정했지만, #7은 live 장기 관찰 전까지 닫지 않는다.
