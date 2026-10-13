"""Own fresh build scratch until the synchronous child has exported its results.

The child must wait for all consumers and return nonzero if validation or final
export fails. Neither this wrapper nor the compatibility fallback deletes files.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from typing import Any


WORKSPACE_ENV = {
    "build": "WINDOWS_SUPPORTER_BUILD_WORKSPACE",
    "installer": "WINDOWS_SUPPORTER_INSTALLER_WORKSPACE",
}


class RetainedTemporaryWorkspace:
    """Compatibility scratch when the personal provider is not installed."""

    def __init__(self, kind: str):
        self.path = Path(tempfile.mkdtemp(prefix=f"windows-supporter-{kind}-"))

    def retain(self) -> None:
        pass


def allocate_workspace(kind: str) -> tuple[Any, bool]:
    provider_root = Path.home() / ".codex" / "tools" / "codex-disposable-workspace"
    if not provider_root.exists():
        return RetainedTemporaryWorkspace(kind), False

    # Load this exact installation, without trusting sys.path or a previously
    # imported package with the same public name. Relative imports stay here.
    package_dir = provider_root / "disposable_workspace"
    module_name = f"_windows_supporter_disposable_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load disposable workspace provider: {package_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        owner = module.allocate()
    except BaseException:
        for name in list(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                del sys.modules[name]
        raise
    return owner, True


def _retain(owner: Any) -> None:
    try:
        owner.retain()
    except BaseException as exc:
        print(f"Scratch retention handle close failed; no cleanup requested: {exc}", file=sys.stderr)


def run_workspace(
    owner: Any,
    command: Sequence[str],
    *,
    kind: str,
    keep: bool = False,
    managed: bool = True,
    environ: Mapping[str, str] | None = None,
    runner: Any = None,
) -> int:
    """Run one synchronous payload; failures never relinquish scratch ownership."""
    if runner is None:
        runner = subprocess.run
    try:
        child_env = dict(os.environ if environ is None else environ)
        child_env[WORKSPACE_ENV[kind]] = str(owner.path)
        print(f"Windows Supporter {kind} scratch: {owner.path}", flush=True)
        if not managed:
            print("Disposable workspace provider absent; temporary scratch will be retained.", file=sys.stderr)
        completed = runner(list(command), env=child_env, shell=False, check=False)
        status = completed.returncode
    except BaseException:
        _retain(owner)
        raise

    if status != 0 or keep or not managed:
        _retain(owner)
        print(f"Scratch retained: {owner.path}", file=sys.stderr)
        return status

    # The payload's zero exit asserts that final export and every synchronous
    # consumer completed. Handoff failure must not alter that result or retry.
    try:
        handed_off = owner.release()
    except BaseException as exc:
        _retain(owner)
        print(f"Scratch handoff failed; retention/cleanup unconfirmed: {owner.path}: {exc}", file=sys.stderr)
    else:
        if handed_off:
            print(f"Scratch handoff sent; cleanup unconfirmed: {owner.path}", file=sys.stderr)
        else:
            _retain(owner)
            print(f"Scratch handoff incomplete; retention/cleanup unconfirmed: {owner.path}", file=sys.stderr)
    return status


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=tuple(WORKSPACE_ENV))
    parser.add_argument("--keep", action="store_true", help="Retain scratch even after success.")
    if "--" not in argv:
        parser.error("A child command is required after --.")
    boundary = argv.index("--")
    args = parser.parse_args(argv[:boundary])
    args.command = list(argv[boundary + 1:])
    if not args.command:
        parser.error("A child command is required after --.")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        owner, managed = allocate_workspace(args.kind)
        return run_workspace(owner, args.command, kind=args.kind, keep=args.keep, managed=managed)
    except KeyboardInterrupt:
        print("Build interrupted; scratch retained if allocated, child completion uncertain.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Build workspace failed; no cleanup requested: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
