"""
Stele Phase G — validation planner.

Validation re-hashes the working copies of sealed artifacts and reports which
are intact and which have drifted or gone missing. It never runs a parser.

Replay is a different operation: stele.replay.engine re-runs the recorded
parser on the recorded input Snapshot and compares the output (roadmap #14).
"""
from __future__ import annotations

from ..ledger.models import ArtifactState
from ..ledger.store import LedgerStore
from .models import ValidationCandidate, ValidationPlan
from .validator import validate_artifact


def plan_validation(
    store: LedgerStore,
    *,
    include_invalidated: bool = False,
    run_id: str | None = None,
    source_path: str | None = None,
) -> ValidationPlan:
    """Build a ValidationPlan from sealed ledger records.

    By default only SEALED records are considered.  Set
    include_invalidated=True to also include INVALIDATED records (useful
    for audit or forced re-ingestion passes).

    Optional filters:
      run_id       — restrict to records from a specific sandbox run
      source_path  — restrict to records from a specific input path

    Each candidate is validated against the filesystem (validate_artifact),
    falling back to the evidence archive for files gone from disk, so the
    plan reflects drift and truly missing files but never reports a sealed
    record whose bundle is verified in the archive as missing.
    """
    states = [ArtifactState.SEALED]
    if include_invalidated:
        states.append(ArtifactState.INVALIDATED)

    records = store.list_by_states(states)

    # Apply optional filters
    if run_id is not None:
        records = [r for r in records if r.run_id == run_id]
    if source_path is not None:
        records = [r for r in records if r.source_path == source_path]

    candidates = tuple(
        ValidationCandidate(record=r, validation=validate_artifact(r, store.archive))
        for r in records
    )

    return ValidationPlan(candidates=candidates)
