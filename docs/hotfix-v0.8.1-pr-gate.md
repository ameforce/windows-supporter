# v0.8.1 PR 검토 게이트 hotfix

## 목적

- 모든 일반 변경은 활성 `hotfix/vX.Y.Z` 또는 `release/vX.Y.Z`에서 파생한 task branch의 PR로만 합친다.
- merge 전 `pr-policy-gate`와 `pr-quality-gate`를 모두 필수 상태 체크로 고정한다.
- 검토 evidence는 repository ID와 PR 번호, 정확한 base/head ref·SHA, finding 0건, 검토 출처, 유효 기간과 UI evidence 전체를 canonical digest로 결합한다.
- `main`과 `develop` 통합은 기존 Git-flow의 `--no-ff` release controller 경로를 유지하며 task PR hard gate와 구분한다.

## 운영 경계

- 이 저장소는 public personal repository이며 write/admin collaborator는 `ameforce` 한 명뿐이다. 이 gate는 악성 관리자를 방어하는 암호학적 보안 경계가 아니라 단독 관리자와 자동화가 합의된 PR 절차를 빠뜨리지 않도록 하는 운영 게이트다.
- 두 workflow는 `pull_request`의 test-merge revision에 check를 남긴다. `pr-policy-gate`는 exact base SHA를 checkout해 base의 validator로 현재 PR metadata와 changed-files를 검증하고, `pr-quality-gate`는 test-merge revision에서 전체 테스트와 artifact-only 빌드를 수행한다.
- workflow wrapper 자체는 head가 포함된 test-merge revision에서 실행된다. repository 기본 `GITHUB_TOKEN` 권한이 read여도 같은 repository의 write actor가 workflow `permissions`를 변경할 수 있으므로, 이 구조는 악성 write actor에 대한 신뢰 경계가 아니다.
- policy workflow는 body, label, draft 상태 변경에 다시 실행되고 두 workflow의 concurrency group은 서로 다른 prefix, PR 번호, base SHA, head SHA로 분리된다. 같은 revision의 최신 metadata event만 이전 실행을 취소하며, 늦게 시작한 과거 revision 실행이 최신 revision 실행을 취소하지 못한다. quality도 base retarget을 포함한 `edited`에서 재실행한다.
- gate workflow, validator, ruleset, `AGENTS.md` 변경은 `policy/*` branch와 `pr-gate-policy-change` label을 동시에 요구한다. 일반 task PR은 이 경로를 사용하지 않는다.
- repository에 다른 write collaborator나 write-capable integration을 추가하기 전에는 전용 GitHub App이 exact evaluated SHA에 check를 게시하고 ruleset이 그 `integration_id`를 고정하는 구조로 승격해야 한다.
- `hotfix/*`, `release/*` ruleset은 PR, 두 고정 status context, 대화 해결, branch 삭제와 force-push 금지를 요구한다. `main`/`develop`은 trusted release controller가 정확한 merge SHA와 CI를 검증한다.

## 최초 도입 bootstrap

1. `hotfix/v0.8.1`을 기존 `v0.8.0` main에서 만들었다.
2. 이 문서와 gate 파일을 추가하는 첫 PR은 base에 workflow와 validator가 없으므로 required check를 소급 생성할 수 없다. `policy/*` branch와 label, 현재 task tree의 `validate-live`, 로컬 전체 테스트와 artifact-only build로 보완하고 `merge-live --allow-bootstrap-local-source`를 사용한다. 이 flag는 `bootstrap_local_source_allowed=true`이면서 PR base SHA가 `source_main_sha`와 정확히 같을 때만 허용되며 v0.8.1의 단 한 번뿐인 bootstrap 예외다.
3. bootstrap PR을 검토·병합한 뒤 `windows-supporter-task-pr-gate` ruleset을 적용한다.
4. 보호 파일을 label 없이 바꾸는 음성 canary PR이 `pr-policy-gate`에서 실패하고 병합되지 않는지 확인한다.
5. 정상 문서 변경 양성 canary PR이 두 gate를 통과하고 병합되는지 확인한다.
6. `policy/*` branch와 `pr-gate-policy-change` label을 사용한 lane 종료 PR에서 `active-release.json`을 inactive template로 되돌린 뒤 v0.8.1을 main/develop에 통합한다.

## 향후 release lane 시작

- release controller는 main에서 버전형 lane을 로컬로 만들고, 첫 원격 push 전에 `active-release.json`의 `state`, `lane`, `active_base`, `source_main_sha`, `expected_version`을 새 lane에 맞춘 activation commit을 만든다.
- ruleset의 `do_not_enforce_on_create`는 최초 branch 생성만 허용한다. deletion rule 때문에 같은 이름의 branch를 삭제·재생성해 gate를 우회할 수 없다.
- task PR은 GitHub UI나 `gh pr merge`로 직접 병합하지 않는다. `scripts/validate_pull_request_gate.py merge-live`가 최신 PR snapshot과 정확한 changed-file 수, expected head SHA를 다시 검증하고, GitHub `Date` header 기준으로 evidence가 최소 300초 남았는지 C snapshot 뒤 재검증한다. controller와 config의 local bytes가 해당 immutable base SHA의 GitHub contents와 같은지도 확인한 직후 merge API를 호출한다. 응답이 유실되면 merged state와 exact head를 재조회해 성공을 채택하며, 이어서 `verify-merged`로 exact base/head와 merge commit을 확인한다. 동일 관리자 credential의 UI/CLI/controller를 GitHub가 구분하지는 않으므로 이 항목은 단독 관리자 runbook이며 전용 App 보안 경계가 아니다.
- lane 종료 후에는 main/develop에 exact tip이 통합됐음을 확인하고 `scripts/configure_github_pr_gate.ps1 -Mode DeleteLane -LaneRef <lane>`를 사용한다. 이 명령은 repository numeric ID를 확인하고 해당 exact ref에 creation/update 금지 freeze ruleset을 먼저 건다. freeze 뒤 tip과 양쪽 ancestor를 다시 검증하고 main ruleset에서 exact ref를 잠시 제외한 다음 `git push --force-with-lease=<exact SHA>`로 조건부 삭제한다. canonical 보호를 먼저 복원한 후 freeze를 제거하며, canonical 복원이 실패하면 freeze를 남겨 재생성·이동을 차단한다.

## ruleset 운영과 롤백

- `scripts/configure_github_pr_gate.ps1 -Mode Plan`으로 canonical diff를 확인한다.
- `-Mode Apply`는 전체 기존 ruleset을 저장소 밖 임시 경로에 export하고 stable name 중복을 거부한 뒤 적용·재조회·미래 branch 이름의 effective rule을 검증한다.
- 적용 실패 시 신규 ruleset 삭제 또는 이전 JSON 복원을 자동 시도한다.
- 정상 lane 정리는 `-Mode DeleteLane`을 사용하며 ruleset 전체를 비활성화하지 않는다. `-Mode Disable`과 export JSON의 `-Mode Restore`는 ruleset 자체 장애의 긴급 rollback에만 사용한다. 이미 공개한 `main`, `develop`, tag를 force rewrite하지 않는다.

## 완료 evidence

- bootstrap PR #1과 gate 보강 PR #2, #13, #14를 trusted controller로 병합했다.
- 정상 문서 변경 양성 canary PR #15는 두 gate green, green 이후 PR body 변조 시 차단, evidence 원복 후 exact head 병합을 확인했다.
- 동일 이름 check 음성 canary PR #16은 `pr-quality-gate-canary`가 성공해도 필수 `pr-quality-gate`가 없어 병합이 차단되는 것을 확인하고 closed-unmerged 처리했다.
- 보호 파일 변경 음성 canary PR #17은 label과 attestation이 없을 때 `pr-policy-gate`가 실패하고 병합이 차단되는 것을 확인하고 closed-unmerged 처리했다.
- post-green metadata 변경은 PR #15의 body 변조로 `pr-policy-gate` 재실행과 차단을 확인했다. 시간 만료는 GitHub 서버 시각과 300초 merge safety margin을 검증하는 unit/controller 테스트로 보완했다.
- 이 lane 종료 PR은 `active-release.json`의 lane 식별자와 bootstrap 예외를 제거하고 inactive template로 되돌린다.
- ruleset apply/export/verify 출력
- main merge commit, annotated `v0.8.1` tag, develop back-merge commit
- main/tag/develop `release-chain-gate`, 로컬 전체 테스트, clean main 빌드, 실행 파일 버전 및 실제 시작 evidence
