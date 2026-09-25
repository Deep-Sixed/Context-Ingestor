from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from .telemetry import RunFailure, RunTelemetry

if TYPE_CHECKING:
    from ..archive.records import Snapshot


@dataclass
class SandboxResult:
    """Immutable record of a single sandboxed parser run."""

    run_id: UUID
    exit_code: int
    stdout: str
    stderr: str
    # Absolute host-side paths of every file present in artifact_dir after the run.
    # Empty if the parser produced no output or failed before writing.
    artifact_paths: list[Path]
    # Host-side directory that was bind-mounted as /stele/output inside the sandbox.
    artifact_dir: Path
    wall_time_seconds: float
    timed_out: bool = False
    # SHA-256 of the exact staged input bytes the parser saw (a manifest digest
    # for directory inputs); None when the run had no input. Computed by
    # Stele, never supplied by the caller.
    input_sha256: str | None = None
    # Name of the sandbox backend that executed the run, e.g. "bubblewrap".
    backend: str | None = None
    # Content digest of the container image that ran the parser (OCI backend);
    # None for backends that do not run images.
    image_digest: str | None = None
    # SHA-256 of the WebAssembly module binary that ran (Wasm backends only);
    # together with the backend name it identifies the parser.
    module_sha256: str | None = None
    # Kernel hardening layers the backend applied to this run, e.g.
    # ("seccomp", "landlock"). Empty when only namespaces were used.
    hardening: tuple[str, ...] = ()
    # Why the backend stopped the parser for breaking sandbox policy, e.g. a
    # blocked system call; None when no violation was detected.
    violation: str | None = None
    # Set only when the run was given an evidence store (roadmap #16):
    # the Snapshot of the staged input (its digest equals input_sha256),
    # {relative POSIX path: blob digest} for every artifact, and the digest of
    # the stored tree object over that manifest (the ledger's artifact_hash).
    input_snapshot: Snapshot | None = None
    artifact_digests: dict[str, str] = field(default_factory=dict)
    artifact_bundle_digest: str | None = None
    # Roadmap #11: the same telemetry from every backend, and on failure a
    # structured reason. A failed run keeps no output (artifact_paths is empty).
    telemetry: RunTelemetry | None = None
    failure: RunFailure | None = None

    @property
    def succeeded(self) -> bool:
        return self.failure is None and self.exit_code == 0 and not self.timed_out

    @property
    def produced_artifacts(self) -> bool:
        return bool(self.artifact_paths)
