## 변경 내용

- 변경 목적과 범위를 적습니다.

## 검증

- 실행한 테스트와 빌드 결과를 적습니다.
- UI 변경이면 head SHA에 결합된 캡처/비교 manifest의 SHA-256을 적습니다.

## 검토 결과

- `LOW/MEDIUM/HIGH/CRITICAL = 0`이 될 때까지 finding을 해결합니다.
- 아래 evidence는 PR을 draft로 만든 뒤 `scripts/validate_pull_request_gate.py render-attestation`으로 생성하고 그대로 붙여 넣습니다.
- 두 required check 성공 후에도 `merge-live` controller가 현재 PR과 evidence 만료를 다시 검사하므로 GitHub UI에서 직접 merge하지 않습니다.

<!-- windows-supporter-pr-attestation:v2
policy_version: 1.1.0
repository_id: 1202717044
repository_full_name: ameforce/windows-supporter
pull_request_number: 0
base_ref: hotfix/v0.0.0
base_sha: replace-with-40-char-sha
head_ref: task/replace-me
head_sha: replace-with-40-char-sha
reviewer_source: replace-with-reviewer-source
finding_low: 0
finding_medium: 0
finding_high: 0
finding_critical: 0
ui_evidence: not-applicable
generated_at: 2000-01-01T00:00:00Z
expires_at: 2000-01-02T00:00:00Z
review_evidence_digest: sha256:replace-with-64-char-hash
-->
