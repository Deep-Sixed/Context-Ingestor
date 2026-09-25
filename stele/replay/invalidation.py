"""
Stele replay — invalidation operations.

Invalidation marks committed (or pending) records as INVALIDATED so that
adapters know to remove or tombstone the corresponding data from
their target stores.

Invalidation is non-destructive: the ledger record and its metadata are
preserved. Only the state transitions and the reason_note are written.
"""
from __future__ import annotations

from ..ledger.models import ArtifactRecord, ArtifactState
from ..ledger.store import LedgerStore
from .models import InvalidationReason


def invalidate_record(
    store: LedgerStore,
    record_id: str,
    reason: InvalidationReason,
    *,
    note: str = "",
) -> ArtifactRecord:
    """Invalidate a single record by ID.

    Transitions PENDING or COMMITTED → INVALIDATED.
    The reason and optional note are stored in the record's error field as:
      "INVALIDATED: <reason.value> — <note>"
    """
    reason_note = reason.value
    if note:
        reason_note = f"{reason.value} — {note}"
    return store.invalidate(record_id, reason_note)


def invalidate_by_source_hash(
    store: LedgerStore,
    source_hash: str,
    reason: InvalidationReason,
    *,
    note: str = "",
) -> list[ArtifactRecord]:
    """Invalidate every COMMITTED or PENDING record derived from source_hash.

    Useful when the upstream source file is updated — all previously
    committed artifacts derived from the old version become invalid.
    Returns the list of newly invalidated records.
    """
    from ..ledger.models import ArtifactState

    candidates = [
        r for r in store.list_by_states([ArtifactState.COMMITTED, ArtifactState.PENDING])
        if r.source_hash == source_hash
    ]

    invalidated: list[ArtifactRecord] = []
    for record in candidates:
        invalidated.append(invalidate_record(store, record.record_id, reason, note=note))

    return invalidated


def auto_invalidate_drifted(
    store: LedgerStore,
    plan_candidates: list,  # list[ReplayCandidate]
    *,
    note: str = "detected by validator",
) -> list[ArtifactRecord]:
    """Invalidate all drifted or missing candidates from a ReplayPlan pass.

    Convenience wrapper: after plan_replay() detects drift or missing files,
    call this to bulk-invalidate those records so they are excluded from
    subsequent replay passes.  Candidates that are already INVALIDATED (from
    plan_replay(include_invalidated=True)) are skipped rather than aborting
    the batch partway through.
    """
    invalidated: list[ArtifactRecord] = []
    for candidate in plan_candidates:
        if candidate.is_replayable or candidate.record.state is ArtifactState.INVALIDATED:
            continue
        reason = (
            InvalidationReason.DRIFT_DETECTED
            if candidate.validation.status in ("drift", "missing")
            else InvalidationReason.MANUAL
        )
        full_note = f"{note}: {candidate.validation.drift_summary}"
        invalidated.append(
            invalidate_record(store, candidate.record.record_id, reason, note=full_note)
        )
    return invalidated
