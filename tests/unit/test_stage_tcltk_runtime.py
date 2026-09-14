from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import _tkinter

from tools.stage_tcltk_runtime import main, stage_tcltk_runtime


TCL_VERSION = str(_tkinter.TCL_VERSION)
TK_VERSION = str(_tkinter.TK_VERSION)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _tcl_entries() -> dict[str, bytes]:
    return {
        "tcl_library/init.tcl": b"# init",
        "tcl_library/auto.tcl": b"# auto",
        "tcl_library/tclIndex": b"# index",
        "tcl_library/clock.tcl": b"# clock",
    }


def _tk_entries() -> dict[str, bytes]:
    return {
        "tk_library/tk.tcl": b"# tk",
        "tk_library/ttk/ttk.tcl": b"# ttk",
    }


def _fake_prefix(root: Path) -> Path:
    prefix = root / "fake-python"
    (prefix / "tcl").mkdir(parents=True)
    (prefix / "DLLs").mkdir(parents=True)
    return prefix


class StageTclTkRuntimeUnitTest(unittest.TestCase):
    def test_stages_real_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = _fake_prefix(Path(temp_dir))
            for name, content in _tcl_entries().items():
                target = prefix / "tcl" / f"tcl{TCL_VERSION}" / Path(name).name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            for name, content in _tk_entries().items():
                rel = Path(*Path(name).parts[1:])
                target = prefix / "tcl" / f"tk{TK_VERSION}" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

            dest = Path(temp_dir) / "out"
            result = stage_tcltk_runtime(dest, prefix)

            self.assertTrue((dest / "_tcl_data" / "init.tcl").is_file())
            self.assertTrue((dest / "_tk_data" / "ttk" / "ttk.tcl").is_file())
            self.assertIn(f"tcl{TCL_VERSION}", result["tcl_source"])

    def test_stages_companion_zip_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = _fake_prefix(Path(temp_dir))
            (prefix / "tcl" / f"libtcl{TCL_VERSION}.4.zip").write_bytes(
                _zip_bytes(_tcl_entries())
            )
            (prefix / "tcl" / f"libtk{TK_VERSION}.4.zip").write_bytes(
                _zip_bytes(_tk_entries())
            )

            dest = Path(temp_dir) / "out"
            stage_tcltk_runtime(dest, prefix)

            self.assertEqual((dest / "_tcl_data" / "auto.tcl").read_bytes(), b"# auto")
            self.assertEqual((dest / "_tk_data" / "tk.tcl").read_bytes(), b"# tk")

    def test_stages_dll_embedded_zipfs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = _fake_prefix(Path(temp_dir))
            # PE-like blob with the library zip appended, matching the
            # CPython 3.14 tcl90.dll layout.
            (prefix / "DLLs" / "tcl90.dll").write_bytes(
                b"MZ" + b"\x00" * 1024 + _zip_bytes(_tcl_entries())
            )
            (prefix / "DLLs" / "tcl9tk90.dll").write_bytes(
                b"MZ" + b"\x00" * 1024 + _zip_bytes(_tk_entries())
            )

            dest = Path(temp_dir) / "out"
            result = stage_tcltk_runtime(dest, prefix)

            self.assertTrue((dest / "_tcl_data" / "tclIndex").is_file())
            self.assertTrue((dest / "_tk_data" / "tk.tcl").is_file())
            self.assertTrue(result["tcl_source"].endswith("tcl90.dll"))

    def test_fails_when_no_library_source_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = _fake_prefix(Path(temp_dir))
            with self.assertRaises(RuntimeError):
                stage_tcltk_runtime(Path(temp_dir) / "out", prefix)

    def test_main_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = _fake_prefix(Path(temp_dir))
            (prefix / "tcl" / f"libtcl{TCL_VERSION}.4.zip").write_bytes(
                _zip_bytes(_tcl_entries())
            )
            (prefix / "tcl" / f"libtk{TK_VERSION}.4.zip").write_bytes(
                _zip_bytes(_tk_entries())
            )
            dest = Path(temp_dir) / "staged"
            dest.mkdir()

            exit_code = main(["--dest", str(dest), "--base-prefix", str(prefix)])

            self.assertEqual(exit_code, 0)
            self.assertTrue((dest / "_tcl_data" / "init.tcl").is_file())
            self.assertTrue((dest / "_tk_data" / "tk.tcl").is_file())


if __name__ == "__main__":
    unittest.main()
