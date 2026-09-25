from __future__ import annotations

import re
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


_DEVICES = ("cpu", "gpu")
_MEMORY_RE = re.compile(r"^[1-9][0-9]*[bkmg]?$")


@dataclass(frozen=True)
class RunConditions:
    """How a run was executed: the device and the limits it ran under.

    Recorded by Stele's own runner (stele.parsers.replay.record_parser_run
    takes it from the ParserRun, which applied it), never asserted by a
    caller, so a replay can run the parser the same way (roadmap #30).
    """

    device: str = "cpu"                 # "cpu" or "gpu": which image variant ran
    memory: str | None = None           # container memory limit, e.g. "6g"
    cpus: float | None = None           # CPU allowance; thread pools follow it
    pids_limit: int | None = None
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.device not in _DEVICES:
            raise ValueError(f"RunConditions.device must be one of {_DEVICES}, not {self.device!r}")
        if self.memory is not None and (
            not isinstance(self.memory, str) or not _MEMORY_RE.match(self.memory.lower())
        ):
            raise ValueError(f"not a memory limit: {self.memory!r}")
        if self.cpus is not None:
            if isinstance(self.cpus, bool) or not isinstance(self.cpus, (int, float)) or self.cpus <= 0:
                raise ValueError(f"RunConditions.cpus must be positive, not {self.cpus!r}")
            object.__setattr__(self, "cpus", float(self.cpus))
        for field_name in ("pids_limit", "timeout_seconds"):
            value = getattr(self, field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"RunConditions.{field_name} must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunConditions":
        unknown = set(data) - {"device", "memory", "cpus", "pids_limit", "timeout_seconds"}
        if unknown:
            raise ValueError(f"unknown run condition(s): {sorted(unknown)}")
        return cls(**data)


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

    # The device and limits the run executed under (roadmap #30). None for
    # runs recorded without them (e.g. Wasm extractors) and for records made
    # before schema version 5.
    run_conditions: RunConditions | None = None
