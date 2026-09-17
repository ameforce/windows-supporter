# v0.25.3 UI footer fit classification

## Intent

- Keep the quick panel's primary action buttons fully readable and clickable on Windows high-DPI displays.
- Keep the selected-date timelog readable as a scrollable viewport, with ticket headings on one line and nested child bullets for each record.

## Current behavior

- The detail `Text` widget requests eight rows for an available timelog.
- The panel caps its window at the monitor work area and places the action footer at the bottom.
- When the natural content height exceeds the work area, Tk compresses the footer and its buttons instead of reducing the scrollable detail viewport.
- Model refreshes can also restore weekly rows to a larger font and reintroduce the same vertical pressure.

## Difference and decision

- This is an incomplete implementation of the existing compact-panel layout contract, not a new user capability.
- Classify as a hotfix: fit the detail viewport to the available work area, keep the footer's requested height, and preserve compact row density during updates.
- Add a native high-DPI regression scenario that verifies the footer buttons remain mapped, at least 20 pixels high, and inside the panel.

## Scope

- `src/apps/wrike_worktime_panel.py`
- `tests/unit/test_wrike_worktime_panel.py`
- No changes to the timelog grouping, nested-bullet text, or ticket-heading ellipsis rules.
