from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from ..ledger.models import ArtifactRecord


class InvalidationReason(str, Enum):
    """Named reasons for invalidating a committed artifact record."""
    DRIFT_DETECTED = "drift_detected"       # artifact files changed since commit
    SOURCE_CHANGED = "source_changed"       # upstream source was updated
    PARSER_UPDATED = "parser_updated"       # parser version changed, re-parse needed
    DATA_QUALITY = "data_quality"           # quality check failed on committed artifact
    MANUAL = "manual"                       # explicit operator decision


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of re-hashing one committed artifact record against the filesystem."""

    record_id: str
    # "ok"      — all files present and hashes match
    # "drift"   — all files present but one or more hashes changed
    # "missing" — one or more files are absent from artifact_dir
    status: Literal["ok", "drift", "missing"]
    drifted_files: tuple[str, ...]  # relative paths whose content changed
    missing_files: tuple[str, ...]  # relative paths that no longer exist

    @property
    def is_replayable(self) -> bool:
        return self.status == "ok"

    @property
    def drift_summary(self) -> str:
        if self.status == "ok":
            return "ok"
        parts = []
        if self.missing_files:
            parts.append(f"missing={list(self.missing_files)}")
        if self.drifted_files:
            parts.append(f"drift={list(self.drifted_files)}")
        return "; ".join(parts)


@dataclass(frozen=True)
class ReplayCandidate:
    """A committed record paired with its current validation result."""

    record: ArtifactRecord
    validation: ValidationResult

    @property
    def is_replayable(self) -> bool:
        return self.validation.is_replayable


@dataclass(frozen=True)
class ReplayPlan:
    """The full set of candidates selected for a replay pass."""

    candidates: tuple[ReplayCandidate, ...]

    @property
    def replayable(self) -> list[ReplayCandidate]:
        return [c for c in self.candidates if c.is_replayable]

    @property
    def drifted(self) -> list[ReplayCandidate]:
        return [c for c in self.candidates if c.validation.status == "drift"]

    @property
    def missing(self) -> list[ReplayCandidate]:
        return [c for c in self.candidates if c.validation.status == "missing"]

    @property
    def replayable_count(self) -> int:
        return len(self.replayable)

    @property
    def drifted_count(self) -> int:
        return len(self.drifted)

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    def summary(self) -> str:
        return (
            f"ReplayPlan: total={len(self.candidates)} "
            f"replayable={self.replayable_count} "
            f"drifted={self.drifted_count} "
            f"missing={self.missing_count}"
        )
