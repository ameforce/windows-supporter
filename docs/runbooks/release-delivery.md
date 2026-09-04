# Version delivery runbook

이 문서는 `AGENTS.md`의 delivery gate를 실행하는 명령 수준 절차다. root contract ID를 완화하지 않는다.

## 1. lane 선택과 preflight

1. `의도한 계약 → 현재 동작 → 차이 → 판정`을 기록하고 hotfix 또는 release lane을 선택한다.
2. 다음을 읽는다.
   - `git status --short --branch`
   - `git log --graph --decorate --oneline --branches --remotes --tags --max-count=12`
   - 예정 version의 local/remote branch와 tag
3. main 물리 worktree의 HEAD를 바꿀 예정이면 `docs/runbooks/runtime-registration.md`의 Git GUI guard를 먼저 통과한다.
4. hotfix는 확인된 clean `main`, release는 확인된 clean `develop`을 기준으로 한다. 별도 release plan이 다른 base를 명시하면 그 증거를 기록한다.
5. `hotfix/vX.Y.Z` 또는 `release/vX.Y.Z`를 만들고 remote exact ref를 read-back한다.

공개 `main`, `develop`, tag를 rewrite해서 base를 맞추지 않는다. unpublished local release graph 재구성도 destructive effect이므로 exact ref와 영향 범위를 제시하고 명시적 승인을 받은 뒤 수행한다.

## 2. task branch와 commit

- version branch에서 `task/`, `feat/`, `fix/`, `chore/`, `refact/`, `policy/` branch를 파생한다.
- `hotfix/설명`, `hotfix/<기능명>`, `codex/...` 같은 비버전 hotfix branch로 version lane을 대체하지 않는다.
- 진행 중인 lane에서 발견한 절차 보강은 [CLASS-INTENT-FIRST] 판정이 현재 lane과 호환될 때만 같은 lane의 별도 task branch/PR에 넣는다. 호환되지 않으면 현재 lane에 섞지 않고 completion을 중단한 뒤 올바르게 분류된 lane의 순서와 base를 결정한다.
- 원인이나 rollback 단위가 다르면 commit을 분리한다.
- commit message는 `fix|feat|chore|refact: ...` 형식을 사용한다.
- main/develop에 직접 commit하지 않는다. 잘못된 경로의 commit은 version branch/task PR 흐름으로 복구한다.

## 3. task PR merge

1. `docs/runbooks/validation-and-release-build.md`에서 task 범위에 맞는 검증을 완료한다.
2. PR base가 exact version branch인지 확인한다.
3. merge 직전에 PR의 base ref/SHA, head ref/SHA와 remote version tip을 다시 읽는다.
4. base/head가 기대값일 때만 다음과 동등한 expected-head precondition으로 merge한다.
   - `gh pr merge <N> --repo ameforce/windows-supporter --merge --match-head-commit <FINAL_HEAD_SHA>`
5. 최종 확인과 merge 사이에는 같은 base의 다른 PR을 merge하지 않는다.
6. `state=MERGED`, `mergedAt`, base branch/SHA, head SHA, merge commit을 read-back한다. closed-unmerged는 완료가 아니다.

## 4. main merge와 annotated tag

1. 모든 task PR이 version branch에 포함됐는지 exact merge commits로 확인한다.
2. main을 version branch와 `--no-ff` merge한다.
   - hotfix message: `Merge branch 'hotfix/vX.Y.Z'`
   - release message: `Merge branch 'release/vX.Y.Z'`
3. main merge가 두 parent이고 second parent가 final version tip인지 확인한다.
4. `docs/runbooks/topic-worktree-cleanup.md`의 full `cleanup-receipt-v1` JSON block을 준비한다.
5. 그 block을 빠짐없이 포함한 annotated `vX.Y.Z` tag를 main merge commit에 만든다.
6. tag peeled commit이 main merge commit과 같은지 확인한다.

## 5. clean tagged artifact

- `docs/runbooks/validation-and-release-build.md`의 release closure overlay를 실행한다.
- task가 policy/docs/ref/worktree-only여도 tagged artifact는 필요하지만 UI-visible test나 앱 launch는 필요하지 않다.
- build source가 clean exact tag인지, artifact metadata가 tag와 commit을 가리키는지 확인한다.
- runtime·packaging release는 candidate build와 transactional deploy를 분리하고, deploy success/rollback receipt와 readiness evidence를 고정한 뒤에만 publish로 진행한다.

## 6. develop back-merge

1. final version tip을 다시 읽는다.
2. 같은 tip을 develop에 `--no-ff` merge한다.
   - hotfix message: `Merge branch 'hotfix/vX.Y.Z' into develop`
   - release message: `Merge branch 'release/vX.Y.Z' into develop`
3. main 또는 release tag를 develop에 merge하지 않는다.
4. develop merge가 두 parent이고 second parent가 final version tip인지 확인한다.
5. conflict/content drift가 없으면 task 검증을 반복하지 않는다. drift가 있으면 그 차이만 검증한다.
6. develop 고유 packaging 변경이 없으면 develop build를 반복하지 않는다.

## 7. publish와 read-back

1. 최종 local main/tag/develop identity를 고정한다.
2. runtime·packaging release는 transactional deploy receipt가 success이고 120초 canary가 유지됐는지 다시 확인한다. readiness 또는 rollback 실패가 있으면 publish하지 않는다.
3. `git push origin main`, `git push origin vX.Y.Z`, `git push origin develop` 순으로 publish한다.
4. 각 push 후 exact remote ref/tag object/peeled commit을 read-back한다.
5. 공개 후 mismatch를 발견해도 force rewrite하지 않는다. revert 또는 다음 patch로 복구한다.
6. GitHub Actions run은 completion gate가 아니다.

## 8. cleanup과 종료

1. `docs/runbooks/topic-worktree-cleanup.md`로 merged task topics/worktrees를 정리한다.
2. receipt가 published tag annotation과 final report에 남았는지 확인한다.
3. `docs/runbooks/protected-version-branch-cleanup.md`로 version branch를 정리한다.
4. `docs/runbooks/release-evidence.md`와 `docs/runbooks/runtime-registration.md`의 final proof를 통과한다.
5. cleanup 불확실성, live freeze, canonical mismatch, wrong live ref가 하나라도 있으면 release cleanup 완료로 보고하지 않는다.
