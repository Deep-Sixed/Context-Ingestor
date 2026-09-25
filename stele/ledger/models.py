from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from ..archive.records import SnapshotKind, is_digest


class ArtifactState(str, Enum):
    """Lifecycle states for a ledger record. See docs/ledger.md.

    States describe the integrity of the run's evidence only. Delivery to
    downstream targets is a separate set of facts (roadmap #13), so a SEALED
    record says nothing about whether any target has received it.
    """
    PENDING = "pending"          # hashed and recorded; bundle not yet archived
    SEALED = "sealed"            # bundle stored in the evidence archive and verified
    FAILED = "failed"            # parser or sealing failed; never becomes sealed
    INVALIDATED = "invalidated"  # retroactively withdrawn (see replay/); terminal


@dataclass(frozen=True)
class ParserIdentity:
    """Which parser produced a run's artifacts.

    name and version are what the caller ran (e.g. "mineru", "1.3.1").
    image_digest (OCI backend) and module_sha256 (Wasm backends) identify the
    exact executable; ledger_transaction takes them from the SandboxResult, so
    they are measured by Stele, not asserted by the caller.
    """

    name: str
    version: str
    image_digest: str | None = None
    module_sha256: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("name", "version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"ParserIdentity.{field_name} must be a non-empty string")
        if self.image_digest is not None and (
            not isinstance(self.image_digest, str) or not self.image_digest
        ):
            raise ValueError("ParserIdentity.image_digest must be a non-empty string")
        if self.module_sha256 is not None and not is_digest(self.module_sha256):
            raise ValueError(f"not a SHA-256 hex digest: {self.module_sha256!r}")


@dataclass(frozen=True)
class ArtifactRecord:
    """Immutable snapshot of one ledger record: exactly one sandbox run."""

    record_id: str                     # UUID
    run_id: str                        # UUID of the SandboxResult; unique in the ledger

    # Input provenance. source_hash is the digest of the input Snapshot in the
    # evidence archive (the bytes the parser saw) and source_kind its kind;
    # both are None for a run without input. source_path is the descriptive
    # locator of the Source, and source_id its id in the archive.
    source_path: str | None
    source_hash: str | None
    source_kind: SnapshotKind | None
    source_id: str | None

    # Parser provenance. None only on records migrated from a pre-#12 ledger,
    # which never stored it.
    parser: ParserIdentity | None
    parser_config: dict[str, Any] | None
    backend: str | None                # sandbox backend that ran the parser

    # Artifact accounting
    artifact_dir: str                  # host path of the sandbox output dir
    artifact_manifest: dict[str, str]  # {relative_path: sha256} — one entry per file
    artifact_hash: str                 # tree digest of the manifest in the archive

    state: ArtifactState
    created_at: datetime
    finalized_at: datetime | None      # set when state leaves pending
    error: str | None                  # set when state is failed or invalidated

    # A caller-supplied source hash from a pre-#12 ledger that matched no
    # Snapshot in the archive. Unverified; kept only for audit.
    legacy_source_hash: str | None = None

    # How the parser was run beyond its config (e.g. a packaged parser's
    # device, CPU and memory limits and timeout), for replay. None when the
    # recorder gave none, and on records from before schema version 5.
    run_settings: dict[str, Any] | None = None
