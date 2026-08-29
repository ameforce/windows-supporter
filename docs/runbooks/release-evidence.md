# Release evidence runbook

## source와 validation identity

- version lane final tip, task PR merge commits와 heads를 기록한다.
- main merge, annotated tag object/peeled commit, develop back-merge를 기록한다.
- task validation이 적용된 source tree와 final trees가 동일한지 확인한다.
- validation matrix에서 선택한 test/static/semantic evidence와 실행하지 않은 full suite/UI E2E의 이유를 기록한다.
- conflict/content drift가 있으면 그 diff와 추가 targeted evidence를 기록한다.

## artifact evidence

- final tagged build command와 exit result
- post-build launch suppression `WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN=1`
- executable absolute path, SHA-256, size와 timestamp
- `FileVersion`, `ProductVersion`, `Comments`
- artifact가 가리키는 release tag와 commit
- build/dist/spec/backup/promotion marker 잔류 여부

명령 성공만으로 artifact를 증명하지 않고 파일 metadata와 hash를 read-back한다.

## ref와 topology evidence

1. main과 develop tip의 parent를 `git rev-list --parents -n 1`로 확인하고 각각 두 parent인지 확인한다.
2. 두 merge의 second parent가 같은 final version tip인지 확인한다.
3. annotated tag peeled commit이 main release merge인지 확인한다.
4. version tip이 main과 develop 양쪽의 ancestor인지 확인한다.
5. 최종 graph는 다음 형식을 사용한다.
   - `git log --graph --decorate --oneline --branches --remotes --tags --max-count=<N>`
6. `git log main develop`처럼 argument order가 출력을 왜곡하는 명령을 최종 graph 근거로 쓰지 않는다.
7. `git log --all`은 `refs/codex/turn-diffs/...`를 포함할 수 있으므로 release graph 근거로 쓰지 않는다. 필요하면 `git for-each-ref --format='%(refname) %(objectname)' refs/codex`로 별도 확인한다.
8. develop back-merge가 main release merge보다 위에 표시되는지 확인하고 불명확하면 parent/ref evidence를 함께 제시한다.

## actual refs

시각적 graph만으로 cleanup을 판정하지 않는다. 다음 surface를 함께 읽는다.

- `git show-ref`
- `git for-each-ref --format='%(refname:short) %(objectname:short)' refs/heads refs/remotes refs/tags`
- `git ls-remote --heads --tags origin`

reflog, fsck, unreachable object, 장식 없는 commit은 live ref가 아니다. 반대로 잘못된 이름/SHA를 가리키는 live ref가 하나라도 있으면 cleanup 완료가 아니다.

## cleanup evidence

- annotated tag의 `cleanup-receipt-v1` JSON block과 tag object SHA
- 각 topic PR/base/head/version-tip identity
- 삭제한 local/remote topic refs와 expected/final SHA
- 제거한 worktree와 exhaustive inventory로 승인한 ignored artifact
- 보존한 대상의 exact path/ref/SHA, 보존 이유와 retry receipt
- protected version local/remote ref 삭제 결과
- canonical ruleset restore snapshot과 final live read-back
- temporary freeze ID/name 부재

## runtime evidence

`docs/runbooks/runtime-registration.md`에 따라 다음을 기록한다.

- current branch `main`
- `main...origin/main` clean/synced status
- main physical executable path와 version/hash
- startup registry exact path
- temporary worktree executable이 persistent runtime으로 등록되지 않았음

## final report minimum

- version lane, main merge, tag, develop merge와 parent SHA
- main/tag/develop push/read-back
- validation matrix decision과 test/build 결과
- artifact metadata/SHA-256와 skipped UI/full-suite validation
- topic cleanup receipts와 삭제/보존 결과
- protected version cleanup, canonical restore, freeze removal
- final local/remote refs/worktrees와 release graph order
- permanent executable와 startup path

검증하지 못한 항목은 성공으로 표현하지 않고 gap 또는 blocker로 보고한다.
