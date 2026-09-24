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

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def produced_artifacts(self) -> bool:
        return bool(self.artifact_paths)
