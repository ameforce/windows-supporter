# Hotfix v0.0.3 WATCH follow-ups

This hotfix intentionally stays limited to the Kakao worker latch cleanup and
the empty-monitor selector retry/rehydration path. The review `WATCH` items
below remain out of scope so the hotfix keeps a small, verifiable risk surface.

## PR checklist

- [ ] Follow-up: Kakao legacy helper cleanup — confirm snapshot/plan/apply path
      coverage, then delete or reduce the legacy inline helpers in a separate
      cleanup PR.
- [ ] Follow-up: Codex metric duplication — design a central metric
      registry/alias mapping after schema and DOM-contract tests are in place,
      then remove the `gpt_5_3_codex_spark_*` duplication in a separate
      refactor PR.
- [ ] Follow-up: old `code_review` state compatibility — decide whether to
      migrate, ignore, or mark old state as version-incompatible, then document
      the policy in an ADR or README/test update.

## Deferral rationale

- The three items are review `WATCH` findings, not the HIGH/MEDIUM defects that
  can leave Kakao refresh or the embedded Kakao tab in a stuck state.
- Each item needs separate coverage or migration policy before code changes:
  legacy Kakao helper cleanup needs snapshot/plan/apply path confidence, Codex
  metric cleanup needs a registry/alias contract, and old `code_review`
  compatibility needs an explicit state-policy decision.
- Handling them in this hotfix would widen the touched files beyond
  `KakaoManager.py`, `main_ui.py`, and focused regression tests, making review
  and rollback harder.

