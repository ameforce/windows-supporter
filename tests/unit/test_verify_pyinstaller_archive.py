import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.verify_pyinstaller_archive import verify_archive_entry


class FakeArchiveReader:
    def __init__(self, entries: dict[str, bytes]) -> None:
        self._entries = entries
        self.toc = tuple(entries)

    def extract(self, name: str) -> bytes:
        return self._entries[name]


class VerifyPyInstallerArchiveUnitTest(unittest.TestCase):
    def test_verifies_entry_with_normalized_path_and_source_match(self) -> None:
        data = b"node-binary"
        archive_name = r"playwright\\driver\\node.exe"
        reader = FakeArchiveReader({archive_name: data})

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "node.exe"
            source_path.write_bytes(data)

            report = verify_archive_entry(
                reader,
                r"playwright\driver\node.exe",
                source_path,
            )

        self.assertEqual(report.archive_name, archive_name)
        self.assertEqual(report.length, len(data))
        self.assertEqual(report.sha256, hashlib.sha256(data).hexdigest().upper())

    def test_rejects_empty_entry(self) -> None:
        reader = FakeArchiveReader({r"playwright\driver\node.exe": b""})

        with self.assertRaisesRegex(RuntimeError, "extracted as empty"):
            verify_archive_entry(reader, r"playwright\driver\node.exe")

    def test_rejects_source_file_mismatch(self) -> None:
        reader = FakeArchiveReader({r"playwright\driver\node.exe": b"archive"})

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "node.exe"
            source_path.write_bytes(b"source")

            with self.assertRaisesRegex(RuntimeError, "does not match source file"):
                verify_archive_entry(
                    reader,
                    r"playwright\driver\node.exe",
                    source_path,
                )


if __name__ == "__main__":
    unittest.main()
