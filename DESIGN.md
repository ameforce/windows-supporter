# Windows Supporter Design System

## 1. Atmosphere & Identity

Windows Supporter is a quiet desktop operations panel. It should feel compact, native, and trustworthy: status first, controls close to the thing they affect, and no decorative surface that competes with operational text. The signature is a bordered white command sheet over a soft gray shell.

## 2. Color

### Palette

| Role | Token | Light | Dark | Usage |
|------|-------|-------|------|-------|
| Surface/primary | --surface-primary | #F3F4F6 | #111827 | Window background |
| Surface/elevated | --surface-elevated | #FFFFFF | #1F2937 | Cards, settings panels |
| Text/primary | --text-primary | #111827 | #F9FAFB | Headings and body |
| Text/secondary | --text-secondary | #6B7280 | #D1D5DB | Hints, separators |
| Border/default | --border-default | #E5E7EB | #374151 | Card outlines and dividers |
| Accent/primary | --accent-primary | #2563EB | #60A5FA | Links and focusable file paths |
| Status/success | --status-success | #059669 | #34D399 | Enabled/available states |
| Status/error | --status-error | #DC2626 | #F87171 | Disabled, failed, unavailable states |

### Rules

- Use status colors only for state, never decoration.
- White elevated panels sit on the gray shell with a single 1px border.
- Extend this table before introducing any new semantic color.

## 3. Typography

### Scale

| Level | Size | Weight | Line Height | Tracking | Usage |
|-------|------|--------|-------------|----------|-------|
| H1 | 14px | 700 | 1.3 | 0 | Dashboard title |
| H2 | 11px | 700 | 1.3 | 0 | Section title |
| Body | 9px | 400 | 1.45 | 0 | Tkinter row text |
| Body/strong | 9px | 700 | 1.45 | 0 | Status keywords |
| Caption | 8px | 400 | 1.4 | 0 | Dense help text |

### Font Stack

- Primary: Segoe UI, system-ui, sans-serif
- Mono: Consolas, monospace

### Rules

- Use tabular or monospace text for counts, versions, and percentages when alignment matters.
- Do not use hero-scale type in settings panels.

## 4. Spacing & Layout

### Base Unit

All spacing derives from a base of 4px.

| Token | Value | Usage |
|-------|-------|-------|
| --space-1 | 4px | Tight inline gaps |
| --space-2 | 8px | Row gaps and button grouping |
| --space-3 | 12px | Window card margin |
| --space-4 | 16px | Card inner padding |

### Grid

- Desktop window widths stay compact: 940-1000px for dashboard-scale views.
- Rows use a flexible status column and fixed command column.
- Settings tabs keep controls scan-friendly in two-column grids when possible.

### Rules

- Prefer compact vertical rhythm over marketing whitespace.
- Keep command buttons at stable widths so status updates do not resize rows.

## 5. Components

### Dashboard status row

- Structure: title, status part list, fixed command button.
- Variants: normal feature row, update row.
- Spacing: 4px title-to-row, 8px section separators.
- States: enabled, disabled, updating, error, unavailable.
- Accessibility: every command remains a real `ttk.Button`.
- Motion: none.

### Settings panel

- Structure: bordered white panel with grouped controls and status summary.
- Variants: feature settings, update settings.
- Spacing: 12px outer padding, 8px group gaps.
- States: loading, current, unavailable, disabled.
- Accessibility: controls are native Tkinter/ttk widgets.
- Motion: none.

## 6. Motion & Interaction

### Timing

| Type | Duration | Easing | Usage |
|------|----------|--------|-------|
| Micro | immediate | native | Tkinter button press |
| Standard | 250ms | native timer | Dashboard refresh after commands |

### Rules

- Do not animate layout in the desktop UI.
- Keep progress responsive by pumping Tk while long subprocesses run.

## 7. Depth & Surface

### Strategy

Use borders-only. Panels use `--border-default`; avoid shadows in Tkinter windows.
