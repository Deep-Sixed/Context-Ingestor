from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


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

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def produced_artifacts(self) -> bool:
        return bool(self.artifact_paths)
