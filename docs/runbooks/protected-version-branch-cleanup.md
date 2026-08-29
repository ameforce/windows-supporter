# Protected version branch cleanup runbook

이 transaction은 topic/worktree cleanup과 분리한다. `.github/pr-protection/ruleset.json`의 canonical contract와 live ruleset을 모두 보존한다.

## admission

1. main, tag, develop push와 exact remote read-back이 완료됐는지 확인한다.
2. version branch tip을 다시 읽고 receipt의 final version-tip SHA와 같은지 확인한다.
3. 그 tip이 final main과 develop 양쪽의 ancestor인지 확인한다.
4. published annotated tag의 `cleanup-receipt-v1` block이 full schema이고 preserved topic의 retry evidence가 남았는지 확인한다.
5. local/remote exact version ref와 expected SHA를 기록한다.
6. version branch가 어떤 worktree에도 binding되지 않았고 local ref가 absent 또는 expected SHA인지 확인한다.
7. canonical live ruleset의 full JSON, ID, name, enforcement, include/exclude, rules를 저장하고 checked-in `.github/pr-protection/ruleset.json`과 의미가 일치하는지 확인한다.

admission 하나라도 실패하면 ruleset이나 ref를 변경하지 않는다.

## temporary freeze

1. exact `refs/heads/<version-lane>`만 대상으로 하는 별도 temporary ruleset을 만든다.
2. `creation`과 `update`를 차단하고 `deletion`은 허용한다.
3. live ruleset ID/name, exact target, enforcement와 effective behavior를 read-back한다.
4. freeze가 effective하지 않으면 중단하고 생성한 임시 상태를 안전하게 복구한다.

freeze는 remote tip 이동과 recreation을 막으며 local/remote final proof가 모두 끝날 때까지 유지한다.

## canonical exclusion과 remote compare-delete

1. canonical live ruleset의 다른 설정과 기존 exclude를 그대로 보존한다.
2. 삭제할 exact version ref만 canonical exclude에 일시 추가한다.
3. live read-back으로 exact 추가와 다른 field 불변을 확인한다.
4. remote ref를 다시 읽어 expected SHA인지 확인한다.
5. `git push origin --force-with-lease=refs/heads/<version-lane>:<EXPECTED_SHA> :refs/heads/<version-lane>`와 동등한 compare-and-delete를 실행한다.
6. exact remote ref 부재를 확인한다.

## canonical restore와 freeze 아래 final proof

remote 삭제 성공 여부와 관계없이 canonical 복원을 먼저 수행한다.

1. canonical ruleset의 원래 exclude와 모든 field를 복원한다.
2. live read-back으로 checked-in canonical contract와 원래 live snapshot의 의미가 복구됐는지 확인한다.
3. remote 삭제가 성공한 경우 freeze를 유지한 채 version branch가 worktree에 binding되지 않았는지 다시 확인한다.
4. local ref를 다시 읽는다.
   - expected SHA면 `git update-ref -d refs/heads/<version-lane> <EXPECTED_SHA>`로 compare-and-delete한다.
   - 없으면 absence를 다시 확인한다.
   - 다른 SHA면 삭제하지 않고 freeze를 유지한 채 중단한다.
5. remote exact ref, local exact ref, remote-tracking ref가 모두 없는지 확인한다.
6. freeze가 유지된 상태에서 remote exact ref 부재와 canonical 일치를 다시 확인한다.
7. local/remote final proof가 모두 성공한 그 뒤에만 temporary freeze ruleset을 제거한다.
8. API 성공 응답만 신뢰하지 않고 freeze ID와 name이 live 목록에 없는지 확인한다.
9. canonical 일치와 local/remote ref 부재를 최종 확인한다.

freeze가 남거나 canonical mismatch, local/remote ref가 있으면 cleanup 완료가 아니다.

## 복구 실패와 remote 삭제 실패

- canonical 복원이나 read-back이 실패하면 temporary freeze에 `deletion`도 추가해 exact ref의 creation/update/deletion을 모두 차단한다.
- emergency full freeze가 effective한지 read-back한다.
- 이 보호도 확인되지 않으면 즉시 중단하고 live ruleset/ref 상태를 보고한다.
- canonical 보호가 복구되기 전에는 emergency freeze를 제거하지 않는다.
- remote ref 삭제가 실패했지만 canonical 복원은 성공했다면 local ref를 삭제하지 않는다. remote ref가 남아 canonical 보호를 받는지 확인한 뒤 temporary freeze를 제거하고 cleanup 미완료로 보고한다.

## final proof

1. remote exact ref, local exact ref, remote-tracking ref가 없는지 확인한다.
2. canonical ruleset이 checked-in contract와 일치하는지 확인한다.
3. temporary freeze ID/name이 live 목록에 없는지 확인한다.
4. checked-in canonical JSON은 transaction 중 수정하지 않는다.
