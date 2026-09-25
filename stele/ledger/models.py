from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ArtifactState(str, Enum):
    """Lifecycle states for a ledger record."""
    PENDING = "pending"       # emitted, hashed, not yet committed downstream
    COMMITTED = "committed"   # downstream write confirmed
    FAILED = "failed"         # parser failed or commit failed — never becomes committed
    INVALIDATED = "invalidated"  # retroactively invalidated via replay/


@dataclass(frozen=True)
class ArtifactRecord:
    """Immutable snapshot of one ledger record."""

    record_id: str                     # UUID
    run_id: str                        # UUID — ties back to a SandboxResult

    # Provenance of the input that was parsed
    source_path: str | None            # host path of the input file/dir, if known
    source_hash: str | None            # sha256 of the input at ingest time

    # Artifact accounting
    artifact_dir: str                  # host path of the sandbox output dir
    artifact_manifest: dict[str, str]  # {relative_path: sha256} — one entry per file
    artifact_hash: str                 # deterministic hash of the full manifest

    state: ArtifactState
    created_at: datetime
    finalized_at: datetime | None      # set when state moves to committed/failed
    error: str | None                  # set when state is failed
