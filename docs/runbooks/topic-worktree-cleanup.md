# Merged topic and worktree cleanup runbook

이 transaction은 release publication 후 protected version branch cleanup 전에 실행한다. topic cleanup 때문에 canonical ruleset을 변경하지 않는다.

## immutable cleanup receipt

published annotated tag에는 아래 단일 JSON shape의 `cleanup-receipt-v1` block을 넣는다. `topics`는 이번 version lane에 merge된 task PR마다 정확히 한 entry를 가지며 PR number와 `final_sha`는 중복될 수 없다.

```json
{
  "schema_version": 1,
  "release_tag": "<vX.Y.Z>",
  "version_lane": {
    "ref": "<refs/heads/hotfix/vX.Y.Z-or-release/vX.Y.Z>",
    "tip_sha": "<final-version-tip-sha>"
  },
  "topics": [
    {
      "pr": {
        "number": 0,
        "state": "MERGED",
        "merged_at": "<iso-8601>"
      },
      "base": {
        "repository": "<owner/repo>",
        "ref": "<version-lane-ref>",
        "sha_before_merge": "<base-sha>"
      },
      "head": {
        "ref": "<topic-ref>",
        "final_sha": "<final-head-sha>"
      },
      "worktree": {
        "path": null,
        "creation_provenance": null
      }
    }
  ]
}
```

worktree가 있었으면 `path`와 task creation evidence를 식별하는 `creation_provenance`를 모두 문자열로 기록하고, 없었으면 둘 다 `null`로 둔다. 일부 field만 적은 요약이나 다른 schema marker를 receipt로 인정하지 않는다. published annotated tag가 immutable durable receipt의 source of truth이고 final report는 mutation 결과와 보존 사유를 추가하되 receipt를 대체하지 않는다. protected version branch가 사라진 뒤 retry할 때도 이 full receipt와 `version_lane.tip_sha` commit을 사용한다.

## preflight

1. PR API와 tag annotation receipt가 일치하는지 확인한다.
2. topic tip이 receipt의 final head SHA와 같은지 확인한다.
3. final head가 version-tip commit, final main, final develop, release tag peeled commit의 ancestor인지 확인한다.
4. local과 remote ref를 독립적으로 판단한다.
   - 존재하면 exact expected SHA여야 한다.
   - 없으면 already-clean으로 기록하고 final read-back에서도 없어야 한다.
   - 어느 한쪽이라도 다른 SHA면 모든 topic mutation을 중단한다.
5. linked worktree가 없으면 ref cleanup으로 진행한다. 존재하면 아래 worktree gate를 통과한다.

## worktree identity와 data gate

1. task creation 기록으로 exact path ownership을 증명한다.
2. 제거 직전에 `git worktree list --porcelain`을 다시 읽고 exact path가 expected `refs/heads/<topic>`과 final head SHA에 유일하게 binding됐는지 확인한다.
3. detached, locked, prunable, 다른 path/branch/HEAD mapping, shared ownership, 변경되거나 불명확한 registration이면 보존한다.
4. staged, unstaged, ordinary untracked 상태가 모두 clean인지 확인한다.
5. root 아래 ignored entry를 `git ls-files --others --ignored --exclude-standard -z` 같은 NUL-safe 방식으로 exhaustive inventory한다.
6. 모든 entry를 creation provenance와 대조해 task-owned 또는 보존 대상으로 분류한다. `.venv`, `build`, `dist`, generated spec/exe/cache 같은 이름만으로 ownership을 추정하지 않는다.
7. 제거 직전에 같은 inventory를 다시 읽고 byte-for-byte 일치하는지 확인한다. 새 항목, 미분류 항목, 보존 대상이 하나라도 있으면 제거하지 않는다.
8. worktree 아래 executable/tool을 쓰는 process가 없고 시작프로그램, 자동 업데이트, 주기 실행이 exact path를 가리키지 않는지 확인한다.
9. main physical tagged artifact와 persistent runtime 경로가 정상인지 확인한다.

## mutation

1. 모든 topic의 preflight를 완료한 뒤 mutation을 시작한다.
2. exact path에 non-force `git worktree remove`를 사용한다.
3. path 부재와 `git worktree list --porcelain` administrative entry 부재를 확인한다.
4. local topic ref를 즉시 다시 읽는다.
   - expected SHA면 `git update-ref -d refs/heads/<topic> <EXPECTED_SHA>`로 old-OID compare-and-delete한다.
   - 없으면 absence를 다시 확인한다.
   - 다른 SHA면 삭제하지 않는다.
5. remote topic ref를 삭제 직전에 각각 다시 읽는다.
   - expected SHA면 `--force-with-lease=refs/heads/<topic>:<EXPECTED_SHA>`를 사용한다.
   - 여러 ref는 단일 atomic push로 compare-and-delete한다.
   - 없으면 already-clean으로 유지한다.
6. exact remote ref 부재를 확인하고 remote-tracking refs를 prune한다.
7. local/remote/topic worktree exact absence를 마지막으로 다시 읽는다.

## 금지와 보존

- `git worktree remove --force`, `git branch -D`, recursive force deletion, 광역 `git clean -fdx`를 사용하지 않는다.
- dirty, shared, unmerged, runtime-active, ownership-uncertain 대상은 보존한다.
- task-owned임을 증명하지 못한 ignored evidence, log, 사용자 자료는 보존한다.
- 보존 시 exact path/ref/SHA, 이유와 immutable receipt를 final report에 남긴다.
- 일부 ref가 이미 없다는 이유로 나머지 verified ref cleanup을 막지 않되, mismatched ref가 있으면 mutation을 중단한다.
