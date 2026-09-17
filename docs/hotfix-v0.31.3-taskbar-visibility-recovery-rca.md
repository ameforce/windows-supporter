# Hotfix v0.31.3 — taskbar visibility recovery RCA

## Classification [CLASS-INTENT-FIRST]

- Intended contract: selected AI usage profiles return after transient slot loss,
  hidden/destroyed native surfaces, and recoverable geometry sampling failures.
  Slot stabilization must eventually accept a sustained valid candidate.
- Current behavior: geometry ticks compare model/content equality without checking
  surface liveness or visibility. Tick remapping never sets `_window_visible`.
  A width-changing reverse transition requiring four samples resets its counter
  to one indefinitely. Exceptions before drawing escape the timer callback.
- Difference: model state is incorrectly treated as proof of applied native state;
  the four-sample transition cannot converge in one branch.
- Decision: existing behavior repair, `hotfix/v0.31.3`, based on clean main
  `59ec5d6ea70afd01299e82889b8459894893a22b`.

## Evidence and causality boundaries

The user reports visibility before v0.31.2 and disappearance afterward on another
PC, currently unavailable. Static comparison confines executable changes to the
overlay module. This is not evidence that every disappearance has one cause.

1. `_geometry_monitor_tick` remaps a withdrawn window without setting
   `_window_visible=True`. Model-visible and surface-visible diverge. The new
   v0.31.2 pane snapshot exclusion depends on that flag; false visibility omits
   the restored overlay from self-exclusion. Content ticks also stop early.
2. Equal model geometry/content suppresses recreation even with `_window=None`
   or a destroyed native/Tk surface. Thus surviving model state prevents recovery.
3. v0.31.2 introduced four-sample reverse-move confirmation, but the width-change
   branch sets `_pending_regression_count=1` on every unaccepted sample. A
   sustained width-changing reverse candidate is never accepted.
4. Sampling/model/window-creation exceptions occur outside the drawing recovery
   handler after the timer ID is cleared; no replacement geometry timer remains.

Surface defects 1/2 and the exception boundary predate v0.31.2. The new sampler
adds another dependency on visibility, while defect 3 is introduced in v0.31.2.
The exact trigger on the other PC remains unverified; these are independently
reproduced code defects, not a claim of native reproduction on that PC.

A random A/B candidate was rejected as evidence: its only slot was 151px, below
176px minimum, and the old version overlapped occupied content by 186px.

## Fix

- Surface missing/hidden state independently triggers placement and drawing.
- Recreated/remapped surfaces receive geometry even when the model is unchanged.
- Successful deiconify updates visibility and restarts keepalive; failure leaves
  visibility false so the next tick retries.
- The geometry callback catches pre-draw failures and retains bounded polling.
- Consecutive identical width-changing candidates increment confirmation count;
  a changed candidate restarts confirmation. Existing anti-ping-pong remains.

## Validation scope [VAL-SCOPE-MINIMUM]

Files: overlay module, its existing fixture, dedicated recovery tests, this RCA.
Direct path: manager -> pane coordinator -> geometry tick -> surface mapping /
slot stabilizer. No provider collection, account storage, or update logic changed.

- Before fix: all five new regression tests fail (4 failures, 1 escaped exception).
- After fix: five new tests pass; 288 existing overlay/target/multimonitor/smoke
  tests pass. Existing time-only redraw fixture now explicitly starts visible.
- Native subset: real Tk window withdrawn and recovered at unchanged model
  geometry; HWND visible, positive rectangle `(2239,1221,2550,1259)`, timer active.
  This uses synthetic content/work area, not affected-PC acceptance.
- `git diff --check` passes.
- No full suite or unrelated browser/UI scenarios: direct scope is sufficient.
- Task build and release closure receipts are recorded separately; unit/native
  results alone do not establish successful artifact delivery.
