from __future__ import annotations

import argparse
import hashlib
import sys
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


DEFAULT_REQUIRED_ENTRIES = (r"playwright\driver\node.exe",)


@dataclass(frozen=True, slots=True)
class ArchiveEntryReport:
    requested_name: str
    archive_name: str
    length: int
    sha256: str


def normalize_archive_name(name: str) -> str:
    normalized = name.replace("/", "\\")
    while "\\\\" in normalized:
        normalized = normalized.replace("\\\\", "\\")
    return normalized.casefold()


def find_archive_name(available_names: Sequence[str], requested_name: str) -> str:
    if requested_name in available_names:
        return requested_name
    requested_normalized = normalize_archive_name(requested_name)
    for available_name in available_names:
        if normalize_archive_name(available_name) == requested_normalized:
            return available_name
    raise KeyError(f"archive entry not found: {requested_name}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_archive_entry(
    reader,
    requested_name: str,
    match_file: Path | None = None,
) -> ArchiveEntryReport:
    archive_name = find_archive_name(tuple(reader.toc), requested_name)
    data = reader.extract(archive_name)
    if not data:
        raise RuntimeError(f"archive entry extracted as empty: {archive_name}")

    digest = hashlib.sha256(data).hexdigest().upper()
    if match_file is not None:
        expected_digest = file_sha256(match_file)
        if len(data) != match_file.stat().st_size or digest != expected_digest:
            raise RuntimeError(
                f"archive entry does not match source file: {archive_name}"
            )

    return ArchiveEntryReport(
        requested_name=requested_name,
        archive_name=archive_name,
        length=len(data),
        sha256=digest,
    )


def verify_archive(
    archive_path: Path,
    entries: Sequence[str],
    match_files: Sequence[Path],
) -> tuple[ArchiveEntryReport, ...]:
    if match_files and len(match_files) != len(entries):
        raise ValueError("--match-file count must match --entry count")

    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(archive_path))
    reports = []
    for index, entry in enumerate(entries):
        match_file = match_files[index] if match_files else None
        reports.append(verify_archive_entry(reader, entry, match_file))
    return tuple(reports)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify that required PyInstaller archive entries decompress."
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--entry",
        action="append",
        dest="entries",
        default=[],
        help="Required archive entry. May be repeated.",
    )
    parser.add_argument(
        "--match-file",
        action="append",
        dest="match_files",
        default=[],
        type=Path,
        help="Source file whose size and sha256 must match the corresponding entry.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    entries = tuple(args.entries or DEFAULT_REQUIRED_ENTRIES)
    match_files = tuple(args.match_files)
    try:
        reports = verify_archive(args.archive, entries, match_files)
    except (ImportError, KeyError, OSError, RuntimeError, ValueError, zlib.error) as exc:
        print(f"PyInstaller archive validation failed: {exc}", file=sys.stderr)
        return 1

    for report in reports:
        print(
            f"validated {report.archive_name}: {report.length} bytes sha256={report.sha256}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
