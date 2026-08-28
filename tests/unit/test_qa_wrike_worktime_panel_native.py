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
    fill: int = 0,
) -> bytes:
    row_filters = filters if filters is not None else tuple(0 for _ in range(height))
    if len(row_filters) != height:
        raise ValueError("filter count must match height")
    if type(fill) is not int or not 0 <= fill <= 255:
        raise ValueError("fill must be one byte")
    scanlines = b"".join(
        bytes([filter_value]) + bytes([fill]) * (width * 4)
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
        run_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run"
        )
        forbidden_pointer_calls = {
            node.func.attr
            for node in ast.walk(run_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"_on_pointer_enter", "_on_pointer_leave"}
        }
        self.assertEqual(forbidden_pointer_calls, set())

        args = argparse.Namespace(
            output_dir="unused",
            finalize_review=True,
            validate_finalized=False,
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

        validate_args = argparse.Namespace(
            output_dir="unused",
            finalize_review=False,
            validate_finalized=True,
            review_receipt=None,
        )
        validated = {"ok": True, "manifest": "manifest.json"}
        with patch.object(
            qa,
            "_parse_args",
            return_value=validate_args,
        ), patch.object(
            qa,
            "_external_output_dir",
            return_value=Path("C:/external/evidence"),
        ), patch.object(
            qa,
            "_validate_finalized",
            return_value=validated,
        ) as validate, patch.object(qa, "_run") as run, patch.object(
            qa,
            "_load_renderer",
        ) as loader, redirect_stdout(io.StringIO()):
            self.assertEqual(qa.main([]), 0)

        validate.assert_called_once_with(Path("C:/external/evidence"))
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
        renderer_module = qa.importlib.import_module(
            "src.apps.wrike_worktime_panel"
        )
        renderer = qa._RendererApi(
            WorktimeActivityPrompt=renderer_module.WorktimeActivityPrompt,
            WorktimePanelDayRow=renderer_module.WorktimePanelDayRow,
            WorktimePanelLine=renderer_module.WorktimePanelLine,
            WorktimePanelModel=renderer_module.WorktimePanelModel,
            WorktimeQuickPanel=renderer_module.WorktimeQuickPanel,
        )
        updated_model = qa._update_model_target(
            qa._model(renderer),
            "2026-08-25",
            450,
        )
        updated_row = next(
            row for row in updated_model.rows if row.date_key == "2026-08-25"
        )
        self.assertEqual(updated_row.target_minutes, 450)
        self.assertEqual(
            updated_row.summary,
            "Wrike 7시간 30분 · 목표 7시간 30분 · 딱 맞음",
        )

        work_area = {"left": 0, "top": 0, "right": 1920, "bottom": 1080}
        initial_cursor = [100, 100]
        geometry = {"x": 116, "y": 116, "width": 800, "height": 640}
        reopen_cursor = [1800, 900]
        reopen_geometry = {"x": 984, "y": 244, "width": 800, "height": 640}
        idle_reopen_geometry = {"x": 16, "y": 16, "width": 800, "height": 640}
        identity = [".!toplevel", ".!toplevel.!frame", ".!toplevel.!frame.!label"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            states = []
            for index, (state_name, filename) in enumerate(
                qa.CHECKPOINT_FILENAMES.items(),
                start=1,
            ):
                screenshot = self._write(
                    root,
                    filename,
                    _rgba_png(width=800, height=640, fill=index),
                )
                decoded = qa._decode_png(screenshot)
                labels = [qa.NORMAL_SYNC_LABEL]
                buttons = []
                entries = []
                shell_border_color = "#E5E7EB"
                if state_name == "initial":
                    labels.append("60초 후 닫힘")
                elif state_name == "hover-active":
                    labels.append("59초 후 닫힘")
                    shell_border_color = "#2563EB"
                elif state_name == "target-editor-prefill":
                    labels.extend(
                        [
                            "2026-08-28 목표 순근무 시간",
                            "편집 중 · 자동 닫힘 일시정지",
                        ]
                    )
                    buttons = ["저장", "취소"]
                    entries = ["08:00"]
                elif state_name == "target-editor-validation":
                    labels.extend(
                        [
                            "24시간은 24:00으로만 입력할 수 있습니다.",
                            "편집 중 · 자동 닫힘 일시정지",
                        ]
                    )
                    buttons = ["저장", "취소"]
                    entries = ["24:30"]
                elif state_name == "target-editor-save-cancel":
                    labels.extend(
                        [
                            "2026-08-28 목표 순근무 시간",
                            "편집 중 · 자동 닫힘 일시정지",
                        ]
                    )
                    buttons = ["저장", "취소"]
                    entries = ["07:30"]
                elif state_name == "vacation-provisional":
                    labels.extend(
                        [
                            "Wrike 기록 5시간 30분 · 현재 기대 5시간 (임시)",
                            "현재 기준 초과 30분 (임시)",
                            "휴가 미확정 (loading) · 휴가 미반영 임시 목표 8시간 (임시)",
                        ]
                    )
                elif state_name == "error-last-good":
                    labels = [qa.ERROR_SYNC_LABEL]
                states.append(
                    {
                        "state": state_name,
                        "screenshot": str(screenshot),
                        "sha256": decoded["sha256"],
                        "dimensions": decoded["dimensions"],
                        "png_signature": qa.PNG_SIGNATURE.hex(),
                        "png_fully_decoded": True,
                        "png_decoded_bytes": decoded["decoded_bytes"],
                        "window_size": [800, 640],
                        "window_geometry": dict(geometry),
                        "labels": labels,
                        "buttons": buttons,
                        "entries": entries,
                        "focus_text": "",
                        "focus_entry_value": entries[0] if entries else "",
                        "shell_border_color": shell_border_color,
                        "capture_provenance": {
                            **qa.CAPTURE_PROVENANCE,
                            "window_handle": 123,
                            "client_dimensions": [800, 640],
                        },
                        "ok": True,
                    }
                )
            observations = [
                {
                    "attempt": attempt,
                    "foreground_hwnd": 456,
                    "tk_focus_is_sentinel": True,
                    "window_geometry": dict(reopen_geometry),
                    "window_size": [800, 640],
                }
                for attempt in range(1, qa.NONACTIVATING_SHOW_REPETITIONS + 1)
            ]
            target_revision = "fixture-revision"
            run = {
                "schema_version": 5,
                "runner_version": qa.RUNNER_VERSION,
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
                "viewport": [800, 640],
                "states": states,
                "assertions": {
                    name: True for name in qa.REQUIRED_ASSERTIONS
                },
                "refresh_observation": {
                    "normal_capture_timeout_ms": qa.CAPTURE_IDLE_TIMEOUT_MS,
                    "exact_equal": {
                        "provider_returned_distinct_equal_instance": True,
                        "widget_identity_before": list(identity),
                        "widget_identity_after": list(identity),
                        "geometry_before": dict(geometry),
                        "geometry_after": dict(geometry),
                        "method_calls": {
                            "render_structure": 0,
                            "update_rendered_model": 0,
                            "reconcile_geometry": 0,
                        },
                    },
                    "same_structure": {
                        "signature_before": [False, qa.SYNTHETIC_TODAY_LINE_COUNT],
                        "signature_after": [False, qa.SYNTHETIC_TODAY_LINE_COUNT],
                        "widget_identity_before": list(identity),
                        "widget_identity_after": list(identity),
                        "geometry_before": dict(geometry),
                        "geometry_after": dict(geometry),
                        "selection_before": "2026-08-28",
                        "selection_after": "2026-09-03",
                        "fallback_today_date_key": "2026-09-03",
                        "target_editor_active_before": True,
                        "target_editor_context_before": "2026-08-28",
                        "target_editor_active_after": False,
                        "method_calls": {
                            "render_structure": 0,
                            "update_rendered_model": 1,
                            "reconcile_geometry": 1,
                        },
                    },
                },
                "native_window_observation": {
                    "window_handle": 123,
                    "tk_overrideredirect": True,
                    "tk_topmost": True,
                    "tk_resizable": [False, False],
                    "style": 0,
                    "extended_style": 8,
                    "has_caption": False,
                    "has_thickframe": False,
                    "native_topmost": True,
                    "foreground_hwnd_after_active_show": 123,
                    "foreground_matches_panel": True,
                },
                "placement_observation": {
                    "pointer_offset_px": qa.POINTER_OFFSET_PX,
                    "initial": {
                        "cursor_position": list(initial_cursor),
                        "work_area": dict(work_area),
                        "expected_geometry": dict(geometry),
                        "window_geometry": dict(geometry),
                    },
                    "reopen": {
                        "cursor_position": list(reopen_cursor),
                        "work_area": dict(work_area),
                        "previous_geometry": dict(geometry),
                        "expected_geometry": dict(reopen_geometry),
                        "window_geometry": dict(reopen_geometry),
                    },
                },
                "hover_observation": {
                    "normal_border_color": "#E5E7EB",
                    "active_border_color": "#2563EB",
                    "active_countdown_text": "59초 후 닫힘",
                    "enter_cursor_position": [200, 200],
                    "enter_delivery_elapsed_ms": 1,
                    "exit_cursor_position": [1919, 1079],
                    "exit_delivery_elapsed_ms": 1,
                    "window_geometry": dict(geometry),
                },
                "target_editor_observation": {
                    "selected_date_key": "2026-08-28",
                    "selected_date_after_click": "2026-08-28",
                    "selected_row_index": 4,
                    "selected_row_widget_index": 2,
                    "selected_row_highlight_color": "#BFDBFE",
                    "editor_title": "2026-08-28 목표 순근무 시간",
                    "prefill_value": "08:00",
                    "invalid_value": "24:30",
                    "validation_message": "24시간은 24:00으로만 입력할 수 있습니다.",
                    "invalid_callback_unchanged": True,
                    "saved_value": "07:30",
                    "saved_minutes": 450,
                    "saved_callback": "edit_plan:2026-08-28:450",
                    "saved_editor_closed": True,
                    "saved_prefill": "07:30",
                    "cancel_attempt_value": "06:00",
                    "cancel_skipped_callback": True,
                    "prefill_after_cancel": "07:30",
                },
                "idle_observation": {
                    "normal_timeout_ms": qa.CAPTURE_IDLE_TIMEOUT_MS,
                    "short_timeout_ms": qa.SHORT_IDLE_TIMEOUT_MS,
                    "window_handle_before": 123,
                    "widget_identity_before": list(identity),
                    "idle_window_geometry": dict(reopen_geometry),
                    "idle_cursor_position": [0, 0],
                    "idle_pointer_outside": True,
                    "idle_elapsed_ms": qa.SHORT_IDLE_TIMEOUT_MS,
                    "idle_withdrawn": True,
                    "window_exists_after_idle": True,
                    "first_reopen_visible": True,
                    "first_reopen_geometry": dict(idle_reopen_geometry),
                    "hover_cursor_position": [100, 100],
                    "hover_pointer_inside": True,
                    "hover_elapsed_ms": qa.SHORT_IDLE_TIMEOUT_MS,
                    "hover_visible": True,
                    "interaction_depth": 1,
                    "interaction_window_geometry": dict(idle_reopen_geometry),
                    "interaction_elapsed_ms": qa.SHORT_IDLE_TIMEOUT_MS,
                    "interaction_visible": True,
                    "rearmed_enter_window_geometry": dict(idle_reopen_geometry),
                    "rearmed_leave_window_geometry": dict(idle_reopen_geometry),
                    "rearmed_cursor_position": [1919, 1079],
                    "rearmed_pointer_outside": True,
                    "rearmed_idle_elapsed_ms": qa.SHORT_IDLE_TIMEOUT_MS,
                    "rearmed_idle_withdrawn": True,
                    "reopened_after_interaction": True,
                    "window_handle_after": 123,
                    "widget_identity_after": list(identity),
                    "same_window_reused": True,
                    "normal_timeout_restored": True,
                    "final_visible": True,
                },
                "pointer_delivery_observation": {
                    "cursor_backend": "Win32 SetCursorPos",
                    "binding": "additive Tk <Enter>/<Leave>",
                    "delivery_timeout_ms": qa.POINTER_DELIVERY_TIMEOUT_MS,
                    "transitions": [
                        {
                            "phase": "idle",
                            "expected": "leave",
                            "window_geometry": dict(reopen_geometry),
                            "cursor_position": [0, 0],
                            "delivery_elapsed_ms": 1,
                        },
                        {
                            "phase": "hover",
                            "expected": "enter",
                            "window_geometry": dict(idle_reopen_geometry),
                            "cursor_position": [100, 100],
                            "delivery_elapsed_ms": 1,
                        },
                        {
                            "phase": "interaction",
                            "expected": "leave",
                            "window_geometry": dict(idle_reopen_geometry),
                            "cursor_position": [1919, 1079],
                            "delivery_elapsed_ms": 1,
                        },
                        {
                            "phase": "rearmed-enter",
                            "expected": "enter",
                            "window_geometry": dict(idle_reopen_geometry),
                            "cursor_position": [100, 100],
                            "delivery_elapsed_ms": 1,
                        },
                        {
                            "phase": "rearmed-leave",
                            "expected": "leave",
                            "window_geometry": dict(idle_reopen_geometry),
                            "cursor_position": [1919, 1079],
                            "delivery_elapsed_ms": 1,
                        },
                    ],
                    "events": [
                        {"sequence": "leave", "widget": ".!toplevel", "elapsed_ms": 1},
                        {"sequence": "enter", "widget": ".!toplevel", "elapsed_ms": 2},
                        {"sequence": "leave", "widget": ".!toplevel", "elapsed_ms": 3},
                        {"sequence": "enter", "widget": ".!toplevel", "elapsed_ms": 4},
                        {"sequence": "leave", "widget": ".!toplevel", "elapsed_ms": 5},
                    ],
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
                    "window_size_before": [800, 640],
                    "reopen_cursor_position": list(reopen_cursor),
                    "reopen_work_area": dict(work_area),
                    "reopen_expected_geometry": dict(reopen_geometry),
                    "observations": observations,
                },
                "callbacks": [
                    "edit_plan:2026-08-28:450",
                    "toggle_break",
                    "prompt_snooze",
                    "refresh",
                ],
                "runtime_errors": [],
                "first_failure": None,
                "fixture_contains_real_identity": False,
                "attempts": 1,
            }
            run_path = root / "run.json"

            def write_run() -> None:
                run_path.write_text(
                    json.dumps(run, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

            write_run()
            qa._write_manifest(root, run, review=None)
            qa._require_exact_inventory(
                root,
                qa.CAPTURE_COMPLETE_FILENAMES,
                "fixture capture",
            )
            validated, run_digest = qa._validate_run_evidence(root)
            self.assertEqual(validated, run)

            run["refresh_observation"]["exact_equal"]["method_calls"][
                "reconcile_geometry"
            ] = 1
            write_run()
            with self.assertRaisesRegex(RuntimeError, "exact-equal refresh"):
                qa._validate_run_evidence(root)
            run["refresh_observation"]["exact_equal"]["method_calls"][
                "reconcile_geometry"
            ] = 0
            provisional = next(
                state
                for state in states
                if state["state"] == "vacation-provisional"
            )
            provisional_labels = provisional["labels"]
            provisional["labels"] = [qa.NORMAL_SYNC_LABEL]
            write_run()
            with self.assertRaisesRegex(RuntimeError, "provisional vacation wording"):
                qa._validate_run_evidence(root)
            provisional["labels"] = provisional_labels
            write_run()
            validated, run_digest = qa._validate_run_evidence(root)
            self.assertEqual(validated, run)

            receipt = {
                "schema_version": 2,
                "run_json_sha256": run_digest,
                "target_revision": target_revision,
                "declared_review_provenance": {
                    "reviewer_label": "fixture manual reviewer",
                    "review_method": "manual-visual-inspection",
                    "identity_assurance": "none",
                    "signature": None,
                },
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
            overstated_receipt = json.loads(
                receipt_path.read_text(encoding="utf-8")
            )
            overstated_receipt["declared_review_provenance"][
                "identity_assurance"
            ] = "authenticated"
            receipt_path.write_text(
                json.dumps(overstated_receipt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "no identity assurance"):
                qa._validate_review_receipt(
                    root,
                    str(receipt_path),
                    run,
                    run_digest,
                )
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
            self.assertEqual(qa.RUNNER_VERSION, "3.1")
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["runner"]["version"], qa.RUNNER_VERSION)
            requirement_ids = [item["id"] for item in qa._requirements()]
            scenario = manifest["scenarios"][0]
            execution = scenario["executions"][0]
            self.assertEqual(scenario["requirement_ids"], requirement_ids)
            self.assertEqual(execution["visual_states"], list(qa.CHECKPOINT_FILENAMES))
            self.assertEqual(
                [item["state"] for item in execution["checkpoints"]],
                list(qa.CHECKPOINT_FILENAMES),
            )
            self.assertTrue(all(item["reviewed"] for item in execution["checkpoints"]))
            self.assertTrue(any("60-second" in step for step in scenario["steps"]))
            self.assertTrue(any("1.2-second" in step for step in scenario["steps"]))
            self.assertTrue(any("non-today row selection" in step for step in scenario["steps"]))
            self.assertTrue(any("today fallback" in step for step in scenario["steps"]))
            self.assertTrue(any("provisional-vacation" in step for step in scenario["steps"]))
            self.assertEqual(manifest["result"]["status"], "passed")
            bindings = manifest["evidence_bindings"]
            self.assertEqual(bindings["run_json"]["sha256"], run_digest)
            self.assertEqual(
                bindings["review_receipt"]["sha256"],
                result["review_receipt_sha256"],
            )
            self.assertEqual(
                bindings["review_receipt"]["declared_review_provenance"],
                receipt["declared_review_provenance"],
            )
            self.assertEqual(
                bindings["exact_inventory"],
                sorted(qa.FINALIZED_FILENAMES),
            )

            inventory_before = qa._root_entry_names(root)
            digests_before = {
                name: qa._sha256(root / name)
                for name in inventory_before
            }
            standalone = qa._validate_finalized(root)
            self.assertTrue(standalone["ok"])
            self.assertEqual(standalone["run_json_sha256"], run_digest)
            self.assertEqual(standalone["exact_inventory"], inventory_before)
            self.assertEqual(qa._root_entry_names(root), inventory_before)
            self.assertEqual(
                {name: qa._sha256(root / name) for name in inventory_before},
                digests_before,
            )

            original_manifest = (root / "manifest.json").read_text(encoding="utf-8")
            tampered_manifest = json.loads(original_manifest)
            tampered_manifest["evidence_bindings"]["run_json"]["sha256"] = "0" * 64
            (root / "manifest.json").write_text(
                json.dumps(tampered_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "digest bindings"):
                qa._validate_finalized(root)
            (root / "manifest.json").write_text(
                original_manifest,
                encoding="utf-8",
            )

            original_receipt = receipt_path.read_text(encoding="utf-8")
            tampered_receipt = json.loads(original_receipt)
            tampered_receipt["declared_review_provenance"]["reviewer_label"] = (
                "different declared reviewer"
            )
            receipt_path.write_text(
                json.dumps(tampered_receipt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "digest bindings"):
                qa._validate_finalized(root)
            receipt_path.write_text(original_receipt, encoding="utf-8")

            extra = root / "unexpected.txt"
            extra.write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "artifact set mismatch"):
                qa._validate_finalized(root)
            extra.unlink()
            self.assertTrue(qa._validate_finalized(root)["ok"])

    def test_full_geometry_contract_requires_xy_and_exact_viewport(self) -> None:
        self.assertTrue(
            qa._valid_viewport_geometry(
                {"x": -900, "y": 40, "width": 800, "height": 640}
            )
        )
        self.assertFalse(
            qa._valid_viewport_geometry({"width": 800, "height": 640})
        )
        self.assertFalse(
            qa._valid_viewport_geometry(
                {"x": 0, "y": 0, "width": 801, "height": 640}
            )
        )


if __name__ == "__main__":
    unittest.main()
