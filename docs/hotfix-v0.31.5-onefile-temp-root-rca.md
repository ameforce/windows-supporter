# Onefile temporary-directory startup failure RCA

## Classification

Intended contract: installed Windows Supporter starts independently of the caller's working directory.
Observed behavior: v0.31.4 embeds `pyi-runtime-tmpdir .`; a caller's CWD must permit creating a child directory before Python starts.
Gap: `.` was documented as the executable directory, but resolves relative to CWD.
Decision: hotfix/v0.31.5; restore startup reliability, no new capability.
Base: c239afdb770a8c9066135148a5829b7cba9c911e.

## Evidence and limits

The installed executable at `C:\Users\enmso\AppData\Local\Programs\Windows Supporter\windows-supporter.exe` reports v0.31.4 (c239afd). Its PyInstaller archive contains `pyi-runtime-tmpdir .`.
The canonical checkout's root executable is older (v0.22.0); it was not used as the incident reproduction target.

A task-owned empty directory denied FILE_ADD_SUBDIRECTORY to the current user. A mkdir preflight verified Windows error 5 even though the runner was elevated. No system-directory ACL or system environment was changed.

| Scenario | Installed v0.31.4 | Candidate |
| --- | --- | --- |
| Writable CWD | exit 0 | exit 0 |
| Directory creation denied in CWD | exit 0xFFFFFFFF, exact reported dialog | exit 0, no dialog |
| Denied CWD and TEMP/TMP pointing to a file | same dialog | exit 0, no dialog |
| System32 CWD | not used as a denied-write fixture | exit 0 |

The native dialog text captured from the exact launched process was `Could not create temporary directory!`.
The original incident's CWD was not captured: this establishes a reproducible root-cause mechanism, not the historical caller identity. Disk free space was approximately 313 GB and the normal TEMP directory existed during investigation.
Two initial runner attempts failed before product execution (elevated-token guard; unsupported PyACL.AddAce). A foreground tool timeout was not counted as product evidence; the corrected bounded runner used separate raw stdout/stderr files and completed all scenarios. The fixture ACL was restored in finally.

## Fix

`build.bat` passes `--runtime-tmpdir "%%LOCALAPPDATA%%\windows-supporter\runtime"`.
Batch escaping preserves the variable until the Windows bootloader expands it for the launching user, before Python imports. This avoids both CWD write dependency and inherited TEMP/TMP dependency without embedding the build account's absolute path.
Tcl/Tk script staging, archive validation, and frozen worker smoke remain enabled. Python startup hooks cannot repair an extraction failure that precedes Python initialization.
No PyInstaller installation patch, broad temp cleanup, system permission change, or UI exception suppression is used.

A valid absolute writable LOCALAPPDATA remains required. Disk exhaustion, an inaccessible runtime root, or corrupted environment can still cause startup failure; this is not a guarantee against every filesystem failure.

## Validation

- 10 targeted tests passed: test_onefile_temp_root, test_stage_tcltk_runtime, test_verify_pyinstaller_archive.
- Actual cmd.exe test proves literal percent preservation, not merely source-text matching.
- Artifact-only build completed with resource archive, frozen OAuth resource, Tcl/Tk initialization, and frozen worker-boundary checks passing.
- Candidate archive option: `pyi-runtime-tmpdir %LOCALAPPDATA%/windows-supporter/runtime`.
- Tcl/Tk smoke from System32 exited 0. Process-environment observation proved extraction under `C:\Users\enmso\AppData\Local\windows-supporter\runtime\_MEI00005a882`.
- Candidate SHA-256: F3C8313BA2AFB6440BB1CF4628AB439DB12495DFF855344BF52D0517CE5D3687.
- Candidate FileVersion/ProductVersion: 0.31.4.1; Comments: v0.31.4.1 (c239afd-dirty). This is a development candidate, NOT a clean tagged v0.31.5 release.
- Independent read-only semantic review found no concrete regression; native evidence was gathered separately afterward.
- No full suite or unrelated UI tests were run: the change affects packaging/startup only.

Task-local raw evidence is outside the source worktree at `C:\workspace\daeng\worktrees\onefile-cwd-v0.31.5` (before.json, after.json, tcl-smoke.json, build.stdout.log, build.stderr.log).

## Delivery state / blocker

Source modifications and candidate verification are complete. Commit, PR, tagged release, installer publication, and runtime replacement are NOT complete.
The governing INV-MAIN-RUNTIME path is `C:\workspace\daeng\git\tools\windows-supporter\windows-supporter.exe`, while the current process and HKCU Run value both use the installed Programs path above. Governing instructions require stopping mutations when policy and live evidence conflict. Resolve the authorized permanent target before release deployment; do not overwrite or re-register either path by assumption.
Task branch/worktree and candidate are retained. Existing runtime and startup registration were not changed. No release completion or cleanup receipt is claimed.
