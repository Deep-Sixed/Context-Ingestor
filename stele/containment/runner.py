"""
Stele Phase E — sandboxed parser runner.

Usage as a library:
    from stele.containment.runner import run_in_sandbox
    from stele.containment.sandbox import SandboxConfig

    result = run_in_sandbox(SandboxConfig(
        command=["/usr/bin/python3", "/stele/parser"],
        artifact_dir=Path("/tmp/stele-run-xyz"),
        script_path=Path("/path/to/my_parser.py"),
    ))

Usage as a CLI:
    python -m stele.containment.runner \\
        --artifact-dir /tmp/stele-out \\
        --script /path/to/parser.py \\
        -- /usr/bin/python3 /stele/parser
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from .artifacts import collect_artifact_paths
from .backend import (
    BWRAP_MISSING as _BWRAP_MISSING,
    ParserRequirements,
    SandboxBackend,
    SandboxUnavailableError,
    UnsupportedBackendError,
    select_backend,
)
from .result import SandboxResult
from .sandbox import SandboxConfig
from .staging import stage_input

if TYPE_CHECKING:
    from ..archive.records import Snapshot
    from ..archive.store import BlobStore

__all__ = [
    "ParserRequirements",
    "SandboxUnavailableError",
    "UnsupportedBackendError",
    "bwrap_available",
    "run_in_sandbox",
]


def bwrap_available() -> bool:
    """True when the bubblewrap binary is on PATH."""
    return shutil.which("bwrap") is not None


def run_in_sandbox(
    config: SandboxConfig,
    *,
    requirements: ParserRequirements | None = None,
    backend: SandboxBackend | None = None,
    store: BlobStore | None = None,
) -> SandboxResult:
    """Execute config.command in a sandbox and return the result.

    The backend is chosen by capability matching against requirements (default:
    a host-process parser with no GPU or native-library needs, not
    deterministic; pass ParserRequirements(wasm_module=True) for a Wasm
    module) unless one is passed
    explicitly; an explicit backend must still satisfy the requirements. If no
    backend qualifies, UnsupportedBackendError (or its subclass
    SandboxUnavailableError) is raised before anything is staged or executed.

    The sandbox exits (and all ephemeral writes are discarded) before this
    function returns. Only regular files found in config.artifact_dir are
    captured in the returned SandboxResult.

    Raises ValueError if config.artifact_dir already has contents: leftover
    files would otherwise be credited to this run.

    With an evidence store (roadmap #16), the staged input is archived as a
    Snapshot before the parser runs (the staging copy is deleted afterwards),
    and every collected artifact is stored by digest, together with a tree
    object over the artifact manifest. Paths in the result stay locations only.
    """
    if store is not None:
        from ..archive.ingest import ingest_artifacts, snapshot_staged_input

    if config.artifact_dir.exists() and any(config.artifact_dir.iterdir()):
        raise ValueError(
            f"artifact_dir {config.artifact_dir} is not empty — "
            "each run needs a fresh output directory"
        )

    requirements = requirements or ParserRequirements()
    # Select before staging so an unsupported host gets the real cause rather
    # than a staging error (staging needs Linux/macOS no-follow opens).
    chosen = select_backend(requirements, None if backend is None else [backend])

    run_id = uuid4()
    runtime_config = config
    input_sha256: str | None = None
    input_snapshot: Snapshot | None = None
    staging: TemporaryDirectory[str] | None = None

    try:
        # Never bind an untrusted source path directly into the sandbox. Stage a
        # private copy first; staging hashes exactly the bytes it copies and
        # refuses symlinks and non-regular entries.
        if config.input_path is not None:
            staging = TemporaryDirectory(prefix=f"stele-{run_id}-")
            staged = stage_input(config.input_path, Path(staging.name) / "input")
            runtime_config = replace(config, input_path=staged.staged_path)
            input_sha256 = staged.sha256
            if store is not None:
                # The staged bytes are the Snapshot; archive them before the
                # staging copy is cleaned up.
                input_snapshot = snapshot_staged_input(store, staged)

        outcome = chosen.execute(runtime_config)
    finally:
        if staging is not None:
            staging.cleanup()

    artifact_paths = collect_artifact_paths(config.artifact_dir)
    artifact_digests: dict[str, str] = {}
    artifact_bundle_digest: str | None = None
    if store is not None:
        artifact_digests = ingest_artifacts(store, config.artifact_dir, artifact_paths)
        artifact_bundle_digest = store.put_tree(artifact_digests)

    return SandboxResult(
        run_id=run_id,
        exit_code=outcome.exit_code,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        artifact_paths=artifact_paths,
        artifact_dir=config.artifact_dir,
        wall_time_seconds=outcome.wall_time_seconds,
        timed_out=outcome.timed_out,
        input_sha256=input_sha256,
        backend=chosen.name,
        module_sha256=outcome.module_sha256,
        hardening=outcome.hardening,
        violation=outcome.violation,
        input_snapshot=input_snapshot,
        artifact_digests=artifact_digests,
        artifact_bundle_digest=artifact_bundle_digest,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m stele.containment.runner",
        description="Run COMMAND inside a Stele sandbox (bubblewrap, or Wasmtime with --wasm).",
    )
    ap.add_argument("--artifact-dir", required=True, type=Path, metavar="DIR",
                    help="Host directory bind-mounted as /stele/output (created if absent).")
    ap.add_argument("--input", type=Path, default=None, dest="input_path", metavar="PATH",
                    help="Read-only input file or directory, staged privately and exposed "
                         "under /stele/input.")
    ap.add_argument("--script", type=Path, default=None, dest="script_path", metavar="PATH",
                    help="Parser script exposed at /stele/parser (read-only).")
    ap.add_argument("--timeout", type=int, default=300, metavar="SECONDS")
    ap.add_argument("--wasm", action="store_true",
                    help="COMMAND[0] is a WebAssembly module (.wasm or .wat) run on the "
                         "Wasmtime backend.")
    ap.add_argument("--deterministic", action="store_true",
                    help="Require a deterministic backend (fixed clock and entropy).")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="Environment variable to inject (repeatable).")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="Command to run inside sandbox (after --).")

    args = ap.parse_args()

    command: list[str] = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        ap.error("no COMMAND provided — use: runner [options] -- COMMAND [ARGS...]")

    env: dict[str, str] = {}
    for kv in args.env:
        k, _, v = kv.partition("=")
        env[k] = v

    config = SandboxConfig(
        command=command,
        artifact_dir=args.artifact_dir,
        input_path=args.input_path,
        script_path=args.script_path,
        timeout_seconds=args.timeout,
        env=env,
    )

    result = run_in_sandbox(config, requirements=ParserRequirements(
        wasm_module=args.wasm, deterministic=args.deterministic,
    ))

    print(json.dumps({
        "run_id": str(result.run_id),
        "exit_code": result.exit_code,
        "succeeded": result.succeeded,
        "timed_out": result.timed_out,
        "wall_time_seconds": round(result.wall_time_seconds, 3),
        "backend": result.backend,
        "hardening": list(result.hardening),
        "violation": result.violation,
        "input_sha256": result.input_sha256,
        "module_sha256": result.module_sha256,
        "artifact_paths": [str(p) for p in result.artifact_paths],
        "stdout": result.stdout,
        "stderr": result.stderr,
    }, indent=2))

    sys.exit(result.exit_code)


if __name__ == "__main__":
    _main()
