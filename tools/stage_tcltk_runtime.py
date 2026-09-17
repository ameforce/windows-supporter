"""Stage Tcl/Tk script libraries as real directories for the frozen bundle.

CPython 3.14+ on Windows ships the Tcl/Tk script libraries as ZipFS archives
embedded inside tcl90.dll / tcl9tk90.dll (or as companion lib*.zip files under
the tcl/ directory) instead of a plain tcl/tclX.Y directory. PyInstaller detects
the //zipfs:/ library path and collects no script files, relying on Tcl to
self-mount the DLL's embedded archive at runtime.

That self-mount goes through Tcl_FSGetNormalizedPath, which canonicalizes the
extracted DLL path component-by-component via FindFirstFileW. In a onefile
bundle the DLL lives under <TEMP>/_MEIxxxxx; when the parent temp directory is
not enumerable (for example C:\\Windows\\Temp under an elevated context) the
probe fails and normalization silently drops the _MEIxxxxx component, so the
mount targets a path that does not exist. Tcl then reports
"Cannot find a usable init.tcl" and every tkinter call fails at startup.

To keep the frozen app independent of that lookup we extract the same script
libraries into plain directories staged as _tcl_data and _tk_data. PyInstaller's
stock _tkinter run-time hook sets TCL_LIBRARY/TK_LIBRARY to those directories
when they exist, so Tcl_Init and Tk_Init read init.tcl / tk.tcl through direct
file access and never consult the zipfs mount.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

TCL_DEST_NAME = "_tcl_data"
TK_DEST_NAME = "_tk_data"

# Files that must exist for a usable staged library. init.tcl/auto.tcl/tclIndex
# are required by Tcl_Init and the auto-load machinery; tk.tcl/ttk/ttk.tcl are
# required by Tk_Init and the themed widget set.
REQUIRED_TCL_FILES = ("init.tcl", "auto.tcl", "tclIndex")
REQUIRED_TK_FILES = ("tk.tcl", "ttk/ttk.tcl")


def _zip_prefix_entries(archive: zipfile.ZipFile, prefix: str) -> list[str]:
    marker = prefix + "/"
    return [n for n in archive.namelist() if n.startswith(marker) and not n.endswith("/")]


def _extract_embedded_library(
    archive_path: Path,
    library_prefix: str,
    dest_dir: Path,
) -> bool:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile):
        return False
    with archive:
        members = _zip_prefix_entries(archive, library_prefix)
        if not members:
            return False
        for member in members:
            relative = PurePosixPath(member).relative_to(library_prefix)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            target = dest_dir.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    return True


def _stage_one_library(
    *,
    base_prefix: Path,
    dest_dir: Path,
    library_prefix: str,
    library_dir_name: str,
    library_zip_pattern: str,
    library_dll_names: Sequence[str],
    required_files: Sequence[str],
) -> Path:
    """Stage one script library into dest_dir and return the source used."""
    candidates: list[Path] = []

    # Classic layout: real directory such as <prefix>/tcl/tcl9.0.
    real_dir = base_prefix / "tcl" / library_dir_name
    if real_dir.is_dir():
        candidates.append(real_dir)

    # Companion zip such as <prefix>/tcl/libtcl9.0.4.zip.
    tcl_dir = base_prefix / "tcl"
    if tcl_dir.is_dir():
        candidates.extend(sorted(tcl_dir.glob(library_zip_pattern)))

    # ZipFS archive embedded in the shared library, e.g. DLLs/tcl90.dll.
    dlls_dir = base_prefix / "DLLs"
    for dll_name in library_dll_names:
        candidates.append(dlls_dir / dll_name)

    for candidate in candidates:
        if candidate.is_dir():
            shutil.copytree(candidate, dest_dir)
        elif candidate.is_file():
            if not _extract_embedded_library(candidate, library_prefix, dest_dir):
                continue
        else:
            continue
        missing = [name for name in required_files if not (dest_dir / name).is_file()]
        if not missing:
            return candidate
        shutil.rmtree(dest_dir, ignore_errors=True)

    searched = ", ".join(str(p) for p in candidates) or str(base_prefix)
    raise RuntimeError(
        f"could not stage Tcl/Tk library '{library_prefix}' from: {searched}"
    )


def stage_tcltk_runtime(dest_root: Path, base_prefix: Path | None = None) -> dict[str, str]:
    prefix = Path(base_prefix) if base_prefix is not None else Path(sys.base_prefix)

    import _tkinter

    tcl_version = str(_tkinter.TCL_VERSION)
    tk_version = str(_tkinter.TK_VERSION)
    tcl_major = tcl_version.split(".")[0]
    tk_major = tk_version.split(".")[0]

    # Tcl 9 names the DLLs tcl90.dll / tcl9tk90.dll; Tcl 8.6 uses tcl86.dll /
    # tk86.dll. Keep both spellings so the tool still works on older runtimes.
    if int(tcl_major) >= 9:
        tcl_dll_names = (f"tcl{tcl_major}0.dll", "tcl90.dll")
        tk_dll_names = (f"tcl{tcl_major}tk{tk_major}0.dll", "tcl9tk90.dll", f"tk{tk_major}0.dll")
    else:
        tcl_dll_names = (f"tcl{tcl_major}{tcl_version.split('.')[1]}.dll",)
        tk_dll_names = (f"tk{tk_major}{tk_version.split('.')[1]}.dll",)

    tcl_dest = dest_root / TCL_DEST_NAME
    tk_dest = dest_root / TK_DEST_NAME
    dest_root.mkdir(parents=True, exist_ok=True)

    tcl_source = _stage_one_library(
        base_prefix=prefix,
        dest_dir=tcl_dest,
        library_prefix="tcl_library",
        library_dir_name=f"tcl{tcl_version}",
        library_zip_pattern="libtcl*.zip",
        library_dll_names=tcl_dll_names,
        required_files=REQUIRED_TCL_FILES,
    )
    tk_source = _stage_one_library(
        base_prefix=prefix,
        dest_dir=tk_dest,
        library_prefix="tk_library",
        library_dir_name=f"tk{tk_version}",
        library_zip_pattern="libtk*.zip",
        library_dll_names=tk_dll_names,
        required_files=REQUIRED_TK_FILES,
    )
    return {"tcl_source": str(tcl_source), "tk_source": str(tk_source)}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract the Tcl/Tk script libraries bundled with this Python "
            "into _tcl_data/_tk_data directories for the frozen build."
        )
    )
    parser.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Output directory that will receive _tcl_data and _tk_data.",
    )
    parser.add_argument(
        "--base-prefix",
        type=Path,
        default=None,
        help="Python installation prefix to source Tcl/Tk from (defaults to sys.base_prefix).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    dest_root: Path = args.dest.resolve()
    temp_root = Path(tempfile.mkdtemp(prefix="tcltk-stage-", dir=str(dest_root.parent)))
    try:
        result = stage_tcltk_runtime(temp_root, args.base_prefix)
        dest_root.mkdir(parents=True, exist_ok=True)
        for name in (TCL_DEST_NAME, TK_DEST_NAME):
            final = dest_root / name
            if final.exists():
                shutil.rmtree(final)
            (temp_root / name).rename(final)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        print(f"Tcl/Tk runtime staging failed: {exc}", file=sys.stderr)
        return 1
    shutil.rmtree(temp_root, ignore_errors=True)
    print(
        "staged Tcl/Tk runtime: "
        f"_tcl_data <- {result['tcl_source']}, _tk_data <- {result['tk_source']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
