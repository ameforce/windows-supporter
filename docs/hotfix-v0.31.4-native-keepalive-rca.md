# v0.31.4 — native taskbar keepalive RCA

## Classification [CLASS-INTENT-FIRST]

Intended: keepalive restores an externally hidden taskbar overlay without moving
or resizing it. Current: the native reassert operation returns zero with Win32
error 1400 on this 64-bit machine even for a valid HWND. Difference: recovery is
silently unsuccessful. Decision: hotfix/v0.31.4 from clean tagged main
9ede74099b72dec445755b1d5663974484979dbd. No new product capability.

## Evidence

A real Tk overlay was rendered, its HWND hidden with ShowWindow(SW_HIDE), and
production `_keepalive_tick` invoked without an intervening geometry tick or
root.update. Before the fix the same HWND remained hidden. Changing only the
visibility predicate still failed. Changing the reassert call to pywin32 made
the identical test pass, preserving HWND and rectangle.

The old untyped ctypes SetWindowPos call returned 0 / ERROR_INVALID_WINDOW_HANDLE
(1400), while pywin32 SetWindowPos succeeded for the same HWND and flags. The
pointer-sized HWND_TOPMOST pseudo handle (-1) was passed through ctypes' default
c_int conversion instead of a typed HWND binding. Return failure was ignored.
Other untyped handle-returning visibility APIs also had no pointer-width contract;
API errors and degenerate rectangles were treated as visible, skipping recovery.

A real message-loop experiment sometimes recovered via another path. That did
not prove keepalive correctness. The committed native regression prevents other
ticks from masking failure of this particular recovery path.

This defect predates v0.31.2. v0.31.3 repaired independently demonstrated surface
state and slot convergence defects. The reported v0.31.2 symptom on another PC
has not been reproduced there, so these findings are confirmed code defects,
not proof of a unique trigger on that PC. Earlier claims that absent AI settings
or MainWindowHandle=0 proved overlay absence were not sufficiently supported.

## Fix and validation selection [VAL-SCOPE-MINIMUM]

Only native visibility/reassert helpers and a native integration test change.
Use existing pywin32 bindings (already used for initial placement), explicitly
check IsWindow/IsWindowVisible, and return not-visible on native inspection
failure. Reassert exceptions reach existing keepalive surface recovery.

- Native regression: fails before, fails with predicate-only change, passes after
  typed reassert change. It tests real HWND, unchanged model-visible state,
  production keepalive, same HWND, same rectangle, and surviving timer.
- Direct affected unit modules: 292 tests passed.
- External-process native check: typed ShowWindowAsync hides the fixture HWND;
  only the production scheduled keepalive remains enabled (geometry/content
  timers cancelled). Recovery observed at 2.18 seconds and visibility maintained
  for 3.09 seconds afterward, with identical HWND and rectangle. PASS.
- git diff --check passed.
- No full suite, unrelated browser scenarios, account changes, singleton bypass,
  runtime shutdown experiments, or diagnostic product flags are required.
- Artifact-only task/final build and transactional deployment are separate gates.

## Rollback and evidence

Use a subsequent patch/revert; never rewrite published tags/main/develop.
Runtime deployment is through tools/deploy_runtime.py with readiness/rollback
receipts. Test UI is confined to a short-lived isolated renderer; permanent
runtime and startup registration remain the main physical executable.
