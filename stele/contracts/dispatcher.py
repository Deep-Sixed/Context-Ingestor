"""
Stele Phase H — write dispatcher.

The Dispatcher is the sole authorized path from adapter output to target stores.
It enforces the adapter contract, validates chunks, and records every write
attempt in its own dispatch log — separately from the artifact ledger.

Key invariants:
  - Adapter failure (transform() raises) → DispatchResult(status="failed");
    the source ArtifactRecord stays SEALED.
  - Target write failure → DispatchResult(status="failed");
    the source ArtifactRecord stays SEALED.
  - Invalidation is a caller decision based on DispatchResult, not automatic.
  - The dispatch log is separate from the ledger — callers join them by record_id
    if they need a combined view.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import uuid4

from ..ledger.models import ArtifactRecord
from .adapter import ChunkValidationError, SteleAdapter, SteleChunk, SteleTarget, validate_chunks


# ---------------------------------------------------------------------------
# Target writer interface
# ---------------------------------------------------------------------------

class TargetWriter(Protocol):
    """Stub interface for target write implementations.

    Real implementations will write to LightRAG, Hindsight, etc.
    For Phase H these are test doubles — the contract is proven against stubs.
    Adapters never see or hold a TargetWriter reference.
    """

    def write_chunks(self, chunks: list[SteleChunk], target: SteleTarget) -> None:
        """Write chunks to the target store.

        Must raise on failure so the Dispatcher can record a failed DispatchResult.
        """
        ...


# ---------------------------------------------------------------------------
# Dispatch record (separate from ArtifactRecord / ledger)
# ---------------------------------------------------------------------------

@dataclass
class DispatchResult:
    """Record of a single adapter dispatch attempt.

    Stored in the Dispatcher's own log, not in the artifact ledger.
    Join to ledger via record_id.
    """
    dispatch_id: str                          # UUID — unique per dispatch attempt
    record_id: str                            # ArtifactRecord.record_id
    target: SteleTarget
    chunks_submitted: int                     # 0 if transform() or validation failed
    status: Literal["success", "failed"]
    error: str | None                         # populated on failure
    dispatched_at: datetime


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class UnregisteredTargetError(Exception):
    """Raised when the Dispatcher has no writer registered for a target type."""


class Dispatcher:
    """Routes adapter-transformed chunks to registered target writers.

    Usage::

        dispatcher = Dispatcher()
        dispatcher.register_target(LightRAGTarget, my_lightrag_writer)

        result = dispatcher.dispatch(my_adapter, sealed_record, LightRAGTarget("default"))

    The adapter is called inside dispatch().  It never receives the target writer.
    """

    def __init__(self) -> None:
        self._target_writers: dict[type, TargetWriter] = {}
        self._log: list[DispatchResult] = []

    def register_target(self, target_type: type, writer: TargetWriter) -> None:
        """Register a writer for a given target type."""
        self._target_writers[target_type] = writer

    def dispatch(
        self,
        adapter: SteleAdapter,
        record: ArtifactRecord,
        target: SteleTarget,
    ) -> DispatchResult:
        """Run the full dispatch cycle for one sealed record.

        Steps:
          1. Call adapter.transform(record) → list[SteleChunk]
          2. Validate chunks (validate_chunks)
          3. Look up the target writer
          4. Call writer.write_chunks(chunks, target)
          5. Return DispatchResult

        On any failure in steps 1–4, return DispatchResult(status="failed").
        The ArtifactRecord's ledger state is NOT modified — that remains the
        caller's decision.
        """
        dispatch_id = str(uuid4())
        now = datetime.now(timezone.utc)
        chunks: list[SteleChunk] = []

        try:
            chunks = adapter.transform(record)
            validate_chunks(chunks)

            writer = self._target_writers.get(type(target))
            if writer is None:
                raise UnregisteredTargetError(
                    f"no writer registered for {type(target).__name__} — "
                    f"call dispatcher.register_target({type(target).__name__}, writer) first"
                )

            writer.write_chunks(chunks, target)

        except Exception as exc:
            result = DispatchResult(
                dispatch_id=dispatch_id,
                record_id=record.record_id,
                target=target,
                chunks_submitted=0,
                status="failed",
                error=repr(exc),
                dispatched_at=now,
            )
            self._log.append(result)
            return result

        result = DispatchResult(
            dispatch_id=dispatch_id,
            record_id=record.record_id,
            target=target,
            chunks_submitted=len(chunks),
            status="success",
            error=None,
            dispatched_at=now,
        )
        self._log.append(result)
        return result

    def dispatch_log(self) -> list[DispatchResult]:
        """Return all dispatch results in chronological order."""
        return list(self._log)

    def dispatch_log_for(self, record_id: str) -> list[DispatchResult]:
        """Return all dispatch results for a given artifact record_id."""
        return [r for r in self._log if r.record_id == record_id]
