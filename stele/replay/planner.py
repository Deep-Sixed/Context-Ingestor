"""
Stele replay — replay planner.

Selects committed ledger records and validates them against the filesystem
to produce a ReplayPlan.  The plan tells callers which records are clean and
replayable versus which have drifted or lost their artifact files.

Does NOT re-run parsers — that would be a higher-level orchestration step
using the adapter contract (see contracts/).  This module only answers the question:
"are these committed artifacts still trustworthy?"
"""
from __future__ import annotations

from ..ledger.models import ArtifactState
from ..ledger.store import LedgerStore
from .models import ReplayCandidate, ReplayPlan
from .validator import validate_artifact


def plan_replay(
    store: LedgerStore,
    *,
    include_invalidated: bool = False,
    run_id: str | None = None,
    source_path: str | None = None,
) -> ReplayPlan:
    """Build a ReplayPlan from committed ledger records.

    By default only COMMITTED records are considered.  Set
    include_invalidated=True to also include INVALIDATED records (useful
    for audit or forced re-ingestion passes).

    Optional filters:
      run_id       — restrict to records from a specific sandbox run
      source_path  — restrict to records from a specific input path

    Each candidate is validated against the filesystem (validate_artifact)
    so the plan immediately reflects any drift or missing files.
    """
    states = [ArtifactState.COMMITTED]
    if include_invalidated:
        states.append(ArtifactState.INVALIDATED)

    records = store.list_by_states(states)

    # Apply optional filters
    if run_id is not None:
        records = [r for r in records if r.run_id == run_id]
    if source_path is not None:
        records = [r for r in records if r.source_path == source_path]

    candidates = tuple(
        ReplayCandidate(record=r, validation=validate_artifact(r))
        for r in records
    )

    return ReplayPlan(candidates=candidates)
