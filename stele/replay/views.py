"""
Stele replay — ledger views.

Four read-only projections over the ledger that cover the operationally
relevant states.  All views return records in created_at order.
"""
from __future__ import annotations

from ..ledger.models import ArtifactRecord, ArtifactState
from ..ledger.store import LedgerStore


class LedgerViews:
    """Convenience read-only views over a LedgerStore."""

    def __init__(self, store: LedgerStore) -> None:
        self._store = store

    def all(self) -> list[ArtifactRecord]:
        """Every record in the ledger regardless of state."""
        return self._store.list_by_states(list(ArtifactState))

    def pending(self) -> list[ArtifactRecord]:
        """Records that have been emitted and hashed but not yet committed."""
        return self._store.list_by_states([ArtifactState.PENDING])

    def committed(self) -> list[ArtifactRecord]:
        """Records whose downstream writes were confirmed."""
        return self._store.list_by_states([ArtifactState.COMMITTED])

    def invalidated_or_failed(self) -> list[ArtifactRecord]:
        """Records that are no longer trustworthy.

        Includes both INVALIDATED (retroactively marked stale) and FAILED
        (parser or commit error).  Downstream adapters should use this view
        to tombstone data they previously wrote.
        """
        return self._store.list_by_states(
            [ArtifactState.INVALIDATED, ArtifactState.FAILED]
        )
