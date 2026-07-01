from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from src.apps.codex_usage_taskbar_overlay import (
    capture_local_taskbar_overlay_geometry_snapshot,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--interval", type=float, default=0.25)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        snapshot: dict[str, Any] = capture_local_taskbar_overlay_geometry_snapshot(
            sample_count=max(1, int(args.samples or 1)),
            sample_interval_sec=max(0.0, float(args.interval or 0.0)),
        )
        output_path = Path(str(args.output))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"taskbar overlay smoke failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
