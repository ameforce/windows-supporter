from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from scripts import qa_wrike_worktime_panel_native as qa


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _rgba_png(
    *,
    width: int = 2,
    height: int = 2,
    filters: tuple[int, ...] | None = None,
    interlace: int = 0,
    compressed_suffix: bytes = b"",
) -> bytes:
    row_filters = filters if filters is not None else tuple(0 for _ in range(height))
    if len(row_filters) != height:
        raise ValueError("filter count must match height")
    scanlines = b"".join(
        bytes([filter_value]) + bytes(width * 4)
        for filter_value in row_filters
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, interlace)
    return b"".join(
        (
            qa.PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(scanlines) + compressed_suffix),
            _chunk(b"IEND", b""),
        )
    )


class QaWrikeWorktimePanelNativeTest(unittest.TestCase):
    def _write(self, root: Path, name: str, content: bytes) -> Path:
        path = root / name
        path.write_bytes(content)
        return path

    def test_module_has_no_eager_renderer_import_and_finalize_route_does_not_load_it(self) -> None:
        source = Path(qa.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        eager_modules = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("src.apps.wrike_worktime_panel", eager_modules)

        args = argparse.Namespace(
            output_dir="unused",
            finalize_review=True,
            review_receipt="receipt.json",
        )
        finalized = {"ok": True, "manifest": "manifest.json"}
        with patch.object(qa, "_parse_args", return_value=args), patch.object(
            qa,
            "_external_output_dir",
            return_value=Path("C:/external/evidence"),
        ), patch.object(qa, "_finalize", return_value=finalized) as finalize, patch.object(
            qa,
            "_run",
        ) as run, patch.object(qa, "_load_renderer") as loader, redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(qa.main([]), 0)

        finalize.assert_called_once()
        run.assert_not_called()
        loader.assert_not_called()

    def test_renderer_loader_checks_revision_before_and_after_import(self) -> None:
        module = SimpleNamespace(
            WorktimeActivityPrompt=object,
            WorktimePanelDayRow=object,
            WorktimePanelLine=object,
            WorktimePanelModel=object,
            WorktimeQuickPanel=object,
        )
        with patch.object(
            qa,
            "_target_revision",
            side_effect=["sealed", "sealed"],
        ) as revision, patch.object(
            qa.importlib,
            "import_module",
            return_value=module,
        ) as importer:
            renderer = qa._load_renderer("sealed")

        self.assertIs(renderer.WorktimeQuickPanel, object)
        self.assertEqual(revision.call_count, 2)
        importer.assert_called_once_with("src.apps.wrike_worktime_panel")

        with patch.object(
            qa,
            "_target_revision",
            side_effect=["sealed", "changed"],
        ), patch.object(qa.importlib, "import_module", return_value=module):
            with self.assertRaisesRegex(RuntimeError, "after renderer import"):
                qa._load_renderer("sealed")

    def test_capture_root_must_be_new_or_empty_without_deleting_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "new-evidence"
            qa._prepare_empty_output_dir(root)
            self.assertTrue(root.is_dir())
            qa._prepare_empty_output_dir(root)

            private = root / "private.txt"
            private.write_bytes(b"must remain")
            with self.assertRaisesRegex(RuntimeError, "new or empty"):
                qa._prepare_empty_output_dir(root)
            self.assertEqual(private.read_bytes(), b"must remain")

    def test_artifact_inventory_is_exact_and_rejects_nonfiles_or_extras(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in qa.CAPTURE_COMPLETE_FILENAMES:
                (root / name).write_bytes(b"fixture")
            qa._require_exact_inventory(
                root,
                qa.CAPTURE_COMPLETE_FILENAMES,
                "test capture",
            )

            extra = root / "unrelated-secret.txt"
            extra.write_bytes(b"preserve")
            with self.assertRaisesRegex(RuntimeError, "artifact set mismatch"):
                qa._require_exact_inventory(
                    root,
                    qa.CAPTURE_COMPLETE_FILENAMES,
                    "test capture",
                )
            self.assertEqual(extra.read_bytes(), b"preserve")

            extra.unlink()
            regular = root / "run.json"
            regular.unlink()
            regular.mkdir()
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                qa._require_exact_inventory(
                    root,
                    qa.CAPTURE_COMPLETE_FILENAMES,
                    "test capture",
                )

    def test_complete_png_decode_returns_dimensions_and_digest_from_same_bytes(self) -> None:
        content = _rgba_png(width=3, height=2, filters=(0, 4))
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "valid.png", content)
            decoded = qa._decode_png(path)

        self.assertEqual(decoded["dimensions"], [3, 2])
        self.assertEqual(decoded["decoded_bytes"], 2 * (1 + 3 * 4))
        self.assertEqual(decoded["sha256"], hashlib.sha256(content).hexdigest())
        self.assertTrue(decoded["fully_decoded"])

    def test_complete_png_decode_rejects_crc_order_trailing_and_decode_errors(self) -> None:
        valid = _rgba_png()
        scanlines = b"\x00" + bytes(8) + b"\x00" + bytes(8)
        compressed = zlib.compress(scanlines)
        header = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
        split = max(1, len(compressed) // 2)
        nonconsecutive_idat = b"".join(
            (
                qa.PNG_SIGNATURE,
                _chunk(b"IHDR", header),
                _chunk(b"IDAT", compressed[:split]),
                _chunk(b"tEXt", b"key\x00value"),
                _chunk(b"IDAT", compressed[split:]),
                _chunk(b"IEND", b""),
            )
        )
        bad_crc = bytearray(valid)
        bad_crc[-1] ^= 0x01
        cases = {
            "trailing": valid + b"private trailing bytes",
            "truncated": valid[:-1],
            "bad-crc": bytes(bad_crc),
            "missing-iend": valid[:-12],
            "nonconsecutive-idat": nonconsecutive_idat,
            "interlaced": _rgba_png(interlace=1),
            "invalid-filter": _rgba_png(filters=(0, 5)),
            "unused-zlib-stream": _rgba_png(
                compressed_suffix=zlib.compress(b""),
            ),
            "short-scanlines": b"".join(
                (
                    qa.PNG_SIGNATURE,
                    _chunk(b"IHDR", header),
                    _chunk(b"IDAT", zlib.compress(scanlines[:-1])),
                    _chunk(b"IEND", b""),
                )
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in cases.items():
                with self.subTest(name=name):
                    path = self._write(root, f"{name}.png", content)
                    with self.assertRaises(RuntimeError):
                        qa._decode_png(path)

    def test_schema_contract_fixture_finalizes_with_exact_artifact_transition(self) -> None:
        geometry = {"x": -320, "y": 40, "width": 800, "height": 540}
        png_content = _rgba_png(width=800, height=540)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            states = []
            for state_name, filename in qa.CHECKPOINT_FILENAMES.items():
                screenshot = self._write(root, filename, png_content)
                decoded = qa._decode_png(screenshot)
                states.append(
                    {
                        "state": state_name,
                        "screenshot": str(screenshot),
                        "sha256": decoded["sha256"],
                        "dimensions": decoded["dimensions"],
                        "png_signature": qa.PNG_SIGNATURE.hex(),
                        "png_fully_decoded": True,
                        "png_decoded_bytes": decoded["decoded_bytes"],
                        "window_size": [800, 540],
                        "window_geometry": dict(geometry),
                        "capture_provenance": {
                            **qa.CAPTURE_PROVENANCE,
                            "window_handle": 123,
                            "client_dimensions": [800, 540],
                        },
                        "ok": True,
                    }
                )
            observations = [
                {
                    "attempt": attempt,
                    "foreground_hwnd": 456,
                    "tk_focus_is_sentinel": True,
                    "window_geometry": dict(geometry),
                    "window_size": [800, 540],
                }
                for attempt in range(1, qa.NONACTIVATING_SHOW_REPETITIONS + 1)
            ]
            target_revision = "fixture-revision"
            run = {
                "schema_version": 2,
                "ok": True,
                "output_root": str(root),
                "capture_start_revision": target_revision,
                "capture_end_revision": target_revision,
                "target_revision": target_revision,
                "capture_provenance": dict(qa.CAPTURE_PROVENANCE),
                "scope": {
                    "claims": list(qa.SCOPE_CLAIMS),
                    "focus": qa.FOCUS_SCOPE,
                    "exclusions": [dict(item) for item in qa.SCOPE_EXCLUSIONS],
                },
                "viewport": [800, 540],
                "states": states,
                "assertions": {
                    name: True for name in qa.REQUIRED_ASSERTIONS
                },
                "focus_order": [
                    "08:05으로 출근",
                    "시간 수정",
                    "30분 후",
                    "오늘 건너뛰기",
                ],
                "nonactivating_focus_observation": {
                    "scope": qa.FOCUS_SCOPE,
                    "cross_process_focus_excluded": True,
                    "repetitions": qa.NONACTIVATING_SHOW_REPETITIONS,
                    "sentinel_hwnd": 456,
                    "foreground_hwnd_before": 456,
                    "tk_focus_before_is_sentinel": True,
                    "window_geometry_before": dict(geometry),
                    "window_size_before": [800, 540],
                    "observations": observations,
                },
                "callbacks": ["toggle_break", "prompt_snooze"],
                "runtime_errors": [],
                "first_failure": None,
                "fixture_contains_real_identity": False,
                "attempts": 1,
            }
            run_path = root / "run.json"
            run_path.write_text(
                json.dumps(run, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            qa._write_manifest(root, run, review=None)
            qa._require_exact_inventory(
                root,
                qa.CAPTURE_COMPLETE_FILENAMES,
                "fixture capture",
            )
            validated, run_digest = qa._validate_run_evidence(root)
            self.assertEqual(validated, run)

            receipt = {
                "schema_version": 1,
                "run_json_sha256": run_digest,
                "target_revision": target_revision,
                "reviewed": True,
                "sensitive_reviewed": True,
                "checkpoints": [
                    {
                        "state": state["state"],
                        "path": state["screenshot"],
                        "sha256": state["sha256"],
                        "dimensions": state["dimensions"],
                        "reviewed": True,
                        "sensitivity": "none",
                    }
                    for state in states
                ],
            }
            receipt_path = root / qa.REVIEW_RECEIPT_FILENAME
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            qa._require_exact_inventory(
                root,
                qa.FINALIZE_INPUT_FILENAMES,
                "fixture finalize input",
            )

            with patch.object(qa, "_target_revision", return_value=target_revision):
                result = qa._finalize(root, str(receipt_path))

            self.assertTrue(result["ok"])
            qa._require_exact_inventory(
                root,
                qa.FINALIZED_FILENAMES,
                "fixture finalized",
            )
            manifest = qa._load_json_object(root / "manifest.json", "manifest")
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["result"]["status"], "passed")

    def test_full_geometry_contract_requires_xy_and_exact_viewport(self) -> None:
        self.assertTrue(
            qa._valid_viewport_geometry(
                {"x": -900, "y": 40, "width": 800, "height": 540}
            )
        )
        self.assertFalse(
            qa._valid_viewport_geometry({"width": 800, "height": 540})
        )
        self.assertFalse(
            qa._valid_viewport_geometry(
                {"x": 0, "y": 0, "width": 801, "height": 540}
            )
        )


if __name__ == "__main__":
    unittest.main()
