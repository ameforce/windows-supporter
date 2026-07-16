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

- Use Segoe UI inside a compact, application-owned borderless Tk shell. Keep the
  1 px neutral boundary and 3 px blue accent; do not add a fake caption bar or
  duplicate window controls. Render the progress bar with an application-owned
  canvas so Windows theme defaults do not force the old green treatment.
- Keep update dialogs compact, left-aligned, and readable.
- Treat the title/subtitle header as the drag surface. Start moving only after a
  4 px pointer threshold and clear drag state on release, focus loss, or unmap.
  Configure the withdrawn root before the first borderless deiconify so native
  chrome cannot flash during initial mapping. Escape, Alt+F4, and close requests
  must use the same state guard: ignore them while work is running and allow
  them only for failed, cancelled, or complete states.
- The helper is intentionally a short-lived topmost surface. Removing native
  chrome also removes native minimize, snap, system-menu, taskbar, and Alt+Tab
  affordances; do not extend this borderless policy to long-lived app windows.
- During normal progress, show the current stage, percent, detail, and log
  action only. Do not show disabled retry/manual/close controls while work is
  still running.
- Show recent activity only when structured activity exists. Use at most three
  Korean stage summaries with a lightweight dot-and-guide timeline, not a
  permanently reserved bordered log card.
- Keep progress in determinate mode with low early percentages for checking and
preflight, mid-range percentages for Git sync, and later percentages for build.
- Use clear stage labels instead of generic "working" text.

## 5. Progress Semantics

Progress percentages represent user-perceived lifecycle stage, not elapsed time.
Preflight and Git GUI resolution happen before build, so they must appear before
the progress bar reaches the high build range. Build output may refine progress
inside the build range, but it should not be the first visible progress state.
The handoff helper owns a separate visible lifecycle after the original app
starts exiting: it starts at 0%, shows old-app cleanup in the low range, then
maps build.bat output from early to late percentages instead of beginning around
80%.

## 6. Process Cleanup

When an update handoff starts, the original Windows Supporter process must clean
its child process tree before it exits. The handoff helper PID is excluded so the
update flow is not killed by its own cleanup. `build.bat` still uses a process
tree kill as a defensive fallback for any still-running `windows-supporter.exe`
instance.

## 7. Failure And Recovery

Failures must expose:

- failed stage,
- concise reason,
- retry availability,
- manual action availability when user intervention is required.

Known Git GUI blockers should use a dedicated prompt and status detail rather
than the generic failure copy.

## 8. QA Targets

Automated and smoke QA must cover:

- update acceptance publishes early progress before Git commands,
- Fork/Git GUI blocker prompts for close-and-continue,
- user rejection cancels and suppresses the same prompt,
- user approval records close/relaunch metadata in handoff state,
- handoff relaunches Windows Supporter and any approved Git GUI app,
- handoff helper progress starts at 0% and build output advances through the
  full visible range,
- update handoff cleanup terminates original child processes without terminating
  the helper.
