"""
Stele containment — sandboxed parser runner.

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

from .artifacts import UnsafeArtifactError, collect_artifact_paths, discard_artifact_dir_contents
from .backend import (
    BWRAP_MISSING as _BWRAP_MISSING,
    ExecutionOutcome,
    ParserRequirements,
    SandboxBackend,
    SandboxUnavailableError,
    UnsupportedBackendError,
    select_backend,
)
from .result import SandboxResult
from .sandbox import SandboxConfig
from .staging import stage_input
from .telemetry import (
    FailureReason,
    RunFailure,
    RunTelemetry,
    exit_status_failure,
    timeout_failure,
)

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

    Every result carries normalized telemetry (roadmap #11), and a run that
    did not succeed carries a structured failure reason (timeout, out of
    memory, CPU limit, blocked syscall, Wasm trap, crash, exit status, engine
    error, or unsafe output such as a symlink, FIFO or unreadable directory).

    A failed run keeps nothing it wrote: the artifact directory is emptied (and
    removed if this run created it), no artifacts are collected, and nothing
    is stored, so a partial extraction can never be recorded. The staging copy
    is always removed. The input Snapshot is still archived, since it records
    what the parser was given.
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
    created_artifact_dir = not config.artifact_dir.exists()
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

        try:
            outcome = chosen.execute(runtime_config)
        except BaseException:
            _remove_output(config.artifact_dir, created_artifact_dir)
            raise
    finally:
        if staging is not None:
            staging.cleanup()

    failure = outcome.failure or _generic_failure(outcome, config)
    artifact_paths: list[Path] = []
    if failure is None:
        try:
            artifact_paths = collect_artifact_paths(config.artifact_dir)
        except UnsafeArtifactError as exc:
            failure = RunFailure(FailureReason.UNSAFE_ARTIFACT, str(exc), exit_code=outcome.exit_code)
    if failure is not None:
        artifact_paths = []
        _remove_output(config.artifact_dir, created_artifact_dir)

    artifact_digests: dict[str, str] = {}
    artifact_bundle_digest: str | None = None
    if store is not None and failure is None:
        artifact_digests = ingest_artifacts(store, config.artifact_dir, artifact_paths)
        artifact_bundle_digest = store.put_tree(artifact_digests)

    telemetry = RunTelemetry(
        backend=chosen.name,
        runtime=outcome.runtime,
        wall_time_seconds=outcome.wall_time_seconds,
        exit_code=outcome.exit_code,
        cpu_time_seconds=outcome.cpu_time_seconds,
        peak_memory_bytes=outcome.peak_memory_bytes,
        limits=outcome.limits or {"timeout_seconds": config.timeout_seconds},
        counters=outcome.counters,
    )

    return SandboxResult(
        run_id=run_id,
        exit_code=outcome.exit_code,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        artifact_paths=artifact_paths,
        artifact_dir=config.artifact_dir,
        wall_time_seconds=outcome.wall_time_seconds,
        # The engine can enforce the deadline itself (podman's conmon) and win
        # the race; the failure reason is the authority.
        timed_out=outcome.timed_out or (
            failure is not None and failure.reason is FailureReason.TIMEOUT
        ),
        input_sha256=input_sha256,
        backend=chosen.name,
        image_digest=outcome.image_digest,
        module_sha256=outcome.module_sha256,
        hardening=outcome.hardening,
        violation=outcome.violation,
        input_snapshot=input_snapshot,
        artifact_digests=artifact_digests,
        artifact_bundle_digest=artifact_bundle_digest,
        telemetry=telemetry,
        failure=failure,
    )


def _generic_failure(outcome: ExecutionOutcome, config: SandboxConfig) -> RunFailure | None:
    """A reason for backends that do not classify their own failures."""
    if outcome.timed_out:
        return timeout_failure(config.timeout_seconds, outcome.exit_code, "the run was stopped")
    if outcome.violation:
        return RunFailure(FailureReason.SYSCALL_BLOCKED, outcome.violation, exit_code=outcome.exit_code)
    if outcome.exit_code != 0:
        return exit_status_failure(outcome.exit_code)
    return None


def _remove_output(artifact_dir: Path, created: bool) -> None:
    """Remove everything a failed run wrote, and the directory if the run made it."""
    try:
        discard_artifact_dir_contents(artifact_dir)
        if created:
            artifact_dir.rmdir()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m stele.containment.runner",
        description="Run COMMAND inside a Stele sandbox backend (bubblewrap or OCI; Wasmtime with --wasm).",
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
        "image_digest": result.image_digest,
        "hardening": list(result.hardening),
        "violation": result.violation,
        "failure": None if result.failure is None else {
            "reason": result.failure.reason.value,
            "detail": result.failure.detail,
            "signal": result.failure.signal_name,
        },
        "telemetry": result.telemetry.to_dict() if result.telemetry else None,
        "input_sha256": result.input_sha256,
        "module_sha256": result.module_sha256,
        "artifact_paths": [str(p) for p in result.artifact_paths],
        "stdout": result.stdout,
        "stderr": result.stderr,
    }, indent=2))

    sys.exit(result.exit_code)


if __name__ == "__main__":
    _main()
