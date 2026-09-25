from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from ..ledger.models import ArtifactRecord


class InvalidationReason(str, Enum):
    """Named reasons for invalidating a sealed artifact record."""
    DRIFT_DETECTED = "drift_detected"       # artifact files changed since sealing
    SOURCE_CHANGED = "source_changed"       # upstream source was updated
    PARSER_UPDATED = "parser_updated"       # parser version changed, re-parse needed
    DATA_QUALITY = "data_quality"           # quality check failed on sealed artifact
    MANUAL = "manual"                       # explicit operator decision
    REPLAY_DIVERGED = "replay_diverged"     # a replay was outside policy (roadmap #14)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of re-hashing one sealed artifact record against the filesystem."""

    record_id: str
    # "ok"       — all files present and hashes match
    # "archived" — some files are gone from artifact_dir, but the evidence
    #              archive holds and verifies each of them; still intact
    # "drift"    — one or more files present on disk changed
    # "missing"  — one or more files are gone from artifact_dir and from the archive
    status: Literal["ok", "archived", "drift", "missing"]
    drifted_files: tuple[str, ...]  # relative paths whose content changed
    missing_files: tuple[str, ...]  # relative paths that no longer exist anywhere
    archived_files: tuple[str, ...] = ()  # gone from disk, verified in the archive

    @property
    def is_intact(self) -> bool:
        return self.status in ("ok", "archived")

    @property
    def drift_summary(self) -> str:
        if self.status == "ok":
            return "ok"
        parts = []
        if self.archived_files:
            parts.append(f"archived_only={list(self.archived_files)}")
        if self.missing_files:
            parts.append(f"missing={list(self.missing_files)}")
        if self.drifted_files:
            parts.append(f"drift={list(self.drifted_files)}")
        return "; ".join(parts)


@dataclass(frozen=True)
class ValidationCandidate:
    """A sealed record paired with its current validation result."""

    record: ArtifactRecord
    validation: ValidationResult

    @property
    def is_intact(self) -> bool:
        return self.validation.is_intact


@dataclass(frozen=True)
class ValidationPlan:
    """The full set of candidates selected for a validation pass."""

    candidates: tuple[ValidationCandidate, ...]

    @property
    def intact(self) -> list[ValidationCandidate]:
        return [c for c in self.candidates if c.is_intact]

    @property
    def drifted(self) -> list[ValidationCandidate]:
        return [c for c in self.candidates if c.validation.status == "drift"]

    @property
    def missing(self) -> list[ValidationCandidate]:
        return [c for c in self.candidates if c.validation.status == "missing"]

    @property
    def intact_count(self) -> int:
        return len(self.intact)

    @property
    def drifted_count(self) -> int:
        return len(self.drifted)

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    def summary(self) -> str:
        return (
            f"ValidationPlan: total={len(self.candidates)} "
            f"intact={self.intact_count} "
            f"drifted={self.drifted_count} "
            f"missing={self.missing_count}"
        )
