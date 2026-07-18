# v0.8.3 PR 리뷰 게이트 교정

## 결론

`v0.8.1`에서 도입한 `pr-policy-gate`와 self-attestation은 실제 PR 리뷰를 실행하거나 검증하지 않았다. 이 구현은 릴리스 lane과 PR 메타데이터를 검사하는 CI를 리뷰 게이트로 잘못 표현했다. `v0.8.3`은 해당 리뷰 흉내를 제거하고 실제 exact-head 이중 리뷰 절차로 복구한다. 테스트·빌드는 명확히 비리뷰인 `pull-request-validation`으로 유지하고, server-side ruleset은 status check 없는 branch 보호로 축소한다.

## RCA

- `money-flow-service`의 절차는 최신 head SHA에 대한 `@codex review`, 별도의 read-only 독립 리뷰, push 후 stale 처리, finding과 review thread 해소, Jenkins 증거 분리를 요구한다. `.github/workflows`를 리뷰 실행기로 사용하지 않는다.
- 이를 참조하는 과정에서 절차의 결과 조건을 GitHub Actions status check로 자동화해야 한다고 잘못 추론했다. 사용자가 Actions 도입을 요청했는지 다시 검증하지 않았다.
- `scripts/validate_pull_request_gate.py`는 실제 GitHub review, review thread, Codex reviewed commit을 조회하지 않았다. `reviewer_source`는 정규식만 검사했고 `render-attestation`은 `finding_low`, `finding_medium`, `finding_high`, `finding_critical`을 자체적으로 0으로 생성했다.
- validator는 draft PR을 즉시 실패시켰다. 따라서 draft 생성 직후의 실패 메일은 실제 리뷰 finding이 아니라 PR 형식 정책 실패였다.
- PR #19와 #20은 `pr-policy-gate`와 `pr-quality-gate` 결과만 있고 `chatgpt-codex-connector` review가 없다. Actions green을 리뷰 완료로 취급한 것이 직접적인 완료 판단 오류다.

## 복구된 리뷰 절차

1. 버전형 hotfix/release branch를 base로 task PR을 연다.
2. PR의 정확한 최신 40자리 head SHA를 기록한다.
3. PR에서 그 SHA를 명시해 `@codex review`를 요청하고, `chatgpt-codex-connector`가 남긴 GitHub review object의 `commit_id`가 정확히 일치하는지 확인한다.
4. 동시에 native Codex subagent 또는 별도 Codex task가 같은 exact-head diff를 독립적으로 read-only 리뷰한다. 두 reviewer에게 상대 리뷰 결과를 미리 전달하지 않는다.
5. finding을 `P0/P1/P2/P3`로 정규화해 모든 등급을 0으로 만들고 unresolved review thread도 0으로 만든다.
6. 수정 push가 발생하면 두 리뷰를 모두 stale로 처리하고 새 head에서 3~5단계를 반복한다.
7. 두 리뷰가 동일한 최종 head를 검토했고 finding과 unresolved thread가 모두 0인 뒤에만 테스트, 빌드, 실행 검증과 릴리스 통합을 진행한다.

PR 본문에 작성자가 넣은 reviewer 이름, finding 0, digest 또는 Actions 결과는 리뷰 실행 증거가 아니다. 실제 GitHub review object와 독립 reviewer 결과가 증거다.

## 변경 범위

- 제거: `pr-policy-gate`, `pr-quality-gate` 이름, 관련 lane config, validator/controller, self-attestation 형식과 단위 테스트.
- 재설계: `pull-request-validation`은 exact PR head의 테스트·artifact 빌드만 수행하며 draft PR에서는 실패하지 않는다. `windows-supporter-release-pr-protection` ruleset은 PR-only merge, stale review dismiss, unresolved thread, force-push·deletion 보호만 유지하고 required status check는 두지 않는다.
- 교체: PR template은 실제 review object와 독립 reviewer 결과를 찾기 위한 비권위 체크리스트만 제공한다.
- 유지: `release-chain-gate`. 이는 `main`, `develop`, tag의 테스트·artifact 빌드를 수행하는 릴리스 CI이며 리뷰 게이트가 아니다.
- 이전 release-chain 시간대·artifact 이름 contract test는 별도 `test_release_chain_gate.py`로 이동한다.
- `AGENTS.md`는 exact-head 이중 리뷰와 stale-on-push 반복을 권위 있는 절차로 명시한다.

## 공개 ref 안전

- 공개된 `v0.8.2` annotated tag object `42665d1061e208d195d41d16e499e55307199f1e`는 `main` commit `dfb36e407c55e01d83f236faef9cb9521f134a72`를 가리킨다.
- `v0.8.2` GitHub Release는 생성되지 않았다.
- 공개 branch와 tag는 force push, 재작성, 삭제하지 않는다. 교정 코드는 다음 patch인 `v0.8.3`으로 배포한다.

## 라이브 원격 상태 교정

- 기존 ruleset `19143432`와 그 required status checks는 코드 교정 전에 제거했다.
- 기존 `PR policy gate`와 `PR quality gate` workflow는 코드 교정 PR이 열리기 전에 수동 비활성화해 draft 실패 메일과 잘못된 gate 사용을 중단했다.
- 이 PR의 교정된 ruleset을 동일 exact head에서 read-back 검증한 뒤 적용한다. 이는 branch 보호 설정이며 실제 이중 리뷰 증거로 계산하지 않는다.
