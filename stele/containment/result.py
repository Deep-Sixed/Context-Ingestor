from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


@dataclass
class SandboxResult:
    """Record of a single sandboxed parser run."""

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
    # Host-side paths the parser left in artifact_dir that are not regular
    # files (symlinks, FIFOs, sockets, devices).  They are never hashed or
    # followed, and any entry here means the run did not succeed.
    rejected_paths: list[Path] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.rejected_paths

    @property
    def produced_artifacts(self) -> bool:
        return bool(self.artifact_paths)
