# v0.8.2 release-chain 시간대 hotfix

## 목적

v0.8.1의 애플리케이션 기능과 공개 ref는 유지하면서, GitHub `release-chain-gate`가 Windows runner의 로컬 시간대 차이 때문에 실패한 문제를 다음 patch release에서 교정한다.

## RCA

- v0.8.1의 main run `29647557986`, tag run `29647558966`, develop run `29647560153`은 모두 `test_model_treats_naive_overlay_now_as_local_for_utc_reset_time`에서 실패했다.
- 기대값과 관찰값의 차이는 정확히 UTC와 KST의 9시간 차이였다.
- 같은 코드의 PR quality run `29647296386`은 먼저 `Korea Standard Time`을 설정한 뒤 775개 테스트와 artifact-only 빌드를 통과했다.
- 따라서 직접 원인은 release workflow가 naive local datetime 계약을 실행하면서 PR quality와 달리 로컬 시간대를 정규화하지 않은 것이다.
- 장기적인 구조 문제는 naive datetime의 의미가 환경에 암묵적으로 의존한다는 점이다. 이번 patch에서는 데이터·애플리케이션 계약을 바꾸지 않고 release CI 환경만 정규화한다.

## 변경 내역

- release test 전에 `tzutil /s "Korea Standard Time"`을 실행한다.
- `tzutil /g` 결과를 `Korea Standard Time`과 exact 비교하고 불일치 시 즉시 실패한다.
- timezone step 전체와 test step 직전 순서를 contract test로 고정한다.
- `workflow_dispatch`를 `policy/*`나 `hotfix/*`에서 실행해도 artifact 이름에 `/`가 들어가지 않도록 `${{ github.run_id }}`와 `${{ github.sha }}`를 사용한다.
- 공개된 v0.8.1의 `main`, `develop`, `v0.8.1` tag는 재작성하지 않는다.

## 검증

- 수정 전 timezone contract test가 실패하고 수정 후 통과했다.
- exact read-back 비교와 slash branch artifact 이름 contract도 각각 red에서 green으로 전환했다.
- 로컬 전체 suite: 777 tests, OK.
- 수동 release-chain run `29648696752`:
  - head SHA `3483ff1569d39718b064cca9ce4751178abb43fe`
  - `Korea Standard Time` exact read-back 통과
  - 777 tests 통과
  - artifact-only build 통과
  - artifact SHA-256 `7eaf336e502fd480171d1883cd718ef0b6e8b662bc3e133398b564d72398a9e7`
  - artifact ID `8430842470` 업로드 성공

## 릴리스 순서와 중단 조건

1. 당시 lane 종료 PR에서 `active-release.json`을 inactive template로 되돌린다.
2. hotfix tip을 `main`에 `--no-ff`로 통합하고 main release-chain 성공을 확인한다.
3. 성공한 main SHA에 annotated `v0.8.2` tag를 만들고 tag release-chain 성공을 확인한다.
4. 같은 hotfix tip을 `develop`에 `--no-ff`로 통합하고 develop release-chain 성공을 확인한다.
5. 세 자동 gate와 tagged clean build·실행 검증이 모두 끝난 뒤 GitHub Release와 실행 파일을 공개한다.
6. 어느 단계든 실패하면 다음 공개 ref를 만들지 않는다. 이미 공개된 ref는 force rewrite하지 않고 다음 patch에서 교정한다.

## 후속 개선

- PR quality가 synthetic merge revision을 테스트하면서 artifact 이름에 PR head SHA를 사용하는 provenance 표현을 별도 정책 변경으로 정리한다.
- naive datetime 제거 또는 명시적 timezone 주입, aware ISO timestamp 마이그레이션은 데이터 호환성 검토가 필요한 후속 구조 개선으로 분리한다.
