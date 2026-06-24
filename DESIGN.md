# Windows Supporter Updater Design

## 1. Product Context

Windows Supporter is a small operator utility that updates itself from the main
Git checkout. The updater must feel immediate and procedural: the user should
always know whether it is checking, preparing Git state, building, relaunching,
or waiting for a blocking app to close.

## 2. UX Principles

- Show update progress as soon as the user accepts an update.
- Prefer Korean copy that names the concrete blocker and the next action.
- Never silently return to the same update prompt after a known blocker.
- Keep destructive or state-changing actions explicitly user-approved.
- Treat Git GUI apps such as Fork as a recoverable blocker, not a generic error.

## 3. Interaction Model

The primary update sequence is:

1. Update available prompt.
2. Immediate preparation progress.
3. Git GUI blocker prompt when needed.
4. Optional close-and-continue flow.
5. Git sync, build handoff, relaunch, completion.

When Fork or another Git GUI is detected, the next dialog should explain that the
app is using the checkout and ask whether Windows Supporter may close it, proceed
with the update, and reopen it afterward. A rejection cancels this update attempt
and suppresses the same repeated prompt for the current tag.

## 4. Visual System

- Use the native Tk/ttk Windows controls and Segoe UI.
- Keep update dialogs compact, left-aligned, and readable.
- Keep progress in determinate mode with low early percentages for checking and
preflight, mid-range percentages for Git sync, and later percentages for build.
- Use clear stage labels instead of generic "working" text.

## 5. Progress Semantics

Progress percentages represent user-perceived lifecycle stage, not elapsed time.
Preflight and Git GUI resolution happen before build, so they must appear before
the progress bar reaches the high build range. Build output may refine progress
inside the build range, but it should not be the first visible progress state.

## 6. Failure And Recovery

Failures must expose:

- failed stage,
- concise reason,
- retry availability,
- manual action availability when user intervention is required.

Known Git GUI blockers should use a dedicated prompt and status detail rather
than the generic failure copy.

## 7. QA Targets

Automated and smoke QA must cover:

- update acceptance publishes early progress before Git commands,
- Fork/Git GUI blocker prompts for close-and-continue,
- user rejection cancels and suppresses the same prompt,
- user approval records close/relaunch metadata in handoff state,
- handoff relaunches Windows Supporter and any approved Git GUI app,
- build progress starts in the build range after earlier stages are visible.
