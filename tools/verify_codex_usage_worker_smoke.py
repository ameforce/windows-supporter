from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the frozen Codex usage worker spawn/Job boundary."
    )
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve()
    if not executable.is_file():
        print(f"worker smoke executable not found: {executable}", file=sys.stderr)
        return 1
    try:
        completed = subprocess.run(
            [str(executable), "--codex-usage-worker-smoke"],
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"worker smoke failed: {exc}", file=sys.stderr)
        return 1
    if completed.returncode != 0:
        print(
            f"worker smoke exited with code {completed.returncode}",
            file=sys.stderr,
        )
        return 1
    print("validated frozen Codex usage worker spawn and Job termination")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
