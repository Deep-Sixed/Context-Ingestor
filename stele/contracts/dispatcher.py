"""
Stele Phase H — write dispatcher, with durable delivery (roadmap #13).

The Dispatcher is the sole authorized path from adapter output to target
stores, and from invalidation to the removal of what was written.

Key invariants:
  - Only SEALED records are dispatched, checked against the live ledger at
    dispatch time. The whole bundle is re-verified in the evidence archive
    first, and adapters read it only through a SealedBundle.
  - Every write is announced by a durable intent and concluded by a receipt
    or a failure in the delivery log (stele/ledger/delivery.py), so a crash
    at any step leaves a log that explains what the target may hold.
  - Writers receive the delivery's dispatch_id as an idempotency key. A retry
    of the same record to the same target reuses it and cannot duplicate
    data; a delivered delivery is never written again.
  - Adapter or target failures never change the record's ledger state.
  - Invalidating a record removes every delivery it made that may have
    written data, through the writer that wrote it, with a receipt for each.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from ..archive.store import ArchiveError
from ..ledger.delivery import Delivery, DeliveryLog, DeliveryStatus, chunks_digest
from ..ledger.models import ArtifactRecord, ArtifactState
from ..ledger.store import ArtifactDriftError, LedgerStore, verify_archived_bundle
from .adapter import SealedBundle, SteleAdapter, SteleChunk, SteleTarget, validate_chunks


# ---------------------------------------------------------------------------
# Target writer interface
# ---------------------------------------------------------------------------

class PartialWriteError(Exception):
    """Raised by a writer that failed after writing some chunks.

    chunks_written is how many chunks reached the target. A writer that
    cannot tell raises any other exception, which is recorded as unknown.
    """

    def __init__(self, message: str, *, chunks_written: int) -> None:
        super().__init__(message)
        self.chunks_written = chunks_written


class TargetWriter(Protocol):
    """Interface for target write implementations (LightRAG, Hindsight, …).

    Adapters never see or hold a TargetWriter reference.
    """

    def write_chunks(
        self, chunks: list[SteleChunk], target: SteleTarget, *, dispatch_id: str
    ) -> None:
        """Write chunks to the target, idempotently per (dispatch_id, chunk_id).

        Writing the same chunks again under the same dispatch_id must not
        create duplicates. Must raise on failure (PartialWriteError when
        some chunks were written).
        """
        ...

    def remove_delivery(self, target: SteleTarget, *, dispatch_id: str) -> int | None:
        """Remove or tombstone everything written under dispatch_id.

        Idempotent. Returns the number of chunks removed, or None if unknown.
        Must raise on failure.
        """
        ...


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class DispatchRefusedError(Exception):
    """Raised when a record may not be dispatched; nothing is written or logged."""


class UnregisteredTargetError(Exception):
    """Raised when the Dispatcher has no writer registered for a target type."""


@dataclass
class DispatchResult:
    """Outcome of one dispatch() call. The durable history is the Delivery."""

    dispatch_id: str                          # the delivery's idempotency key
    record_id: str                            # ArtifactRecord.record_id
    target: SteleTarget
    chunks_submitted: int                     # chunks in the intent; 0 if none was made
    chunks_written: int | None                # None = unknown (the writer did not say)
    status: Literal["success", "failed"]
    error: str | None                         # populated on failure
    dispatched_at: datetime
    already_delivered: bool = False           # a previous call delivered it; nothing written now


@dataclass(frozen=True)
class RemovalResult:
    dispatch_id: str
    status: Literal["removed", "failed"]
    chunks_removed: int | None
    error: str | None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class Dispatcher:
    """Routes adapter-transformed chunks to registered target writers.

    Usage::

        dispatcher = Dispatcher(ledger)
        dispatcher.register_target(LightRAGTarget, my_lightrag_writer)

        result = dispatcher.dispatch(my_adapter, record_id, LightRAGTarget("default"))
        ...
        dispatcher.invalidate(record_id, "source_changed")  # removes its target data

    The adapter is called inside dispatch(). It never receives the target writer.
    """

    def __init__(self, ledger: LedgerStore) -> None:
        self._ledger = ledger
        self._log = DeliveryLog(ledger)
        self._target_writers: dict[type, TargetWriter] = {}

    def register_target(self, target_type: type, writer: TargetWriter) -> None:
        """Register a writer for a given target type."""
        self._target_writers[target_type] = writer

    # -- dispatch ------------------------------------------------------------

    def dispatch(
        self,
        adapter: SteleAdapter,
        record: ArtifactRecord | str,
        target: SteleTarget,
    ) -> DispatchResult:
        """Deliver one sealed record to one target.

        Steps:
          1. Re-read the record from the ledger; refuse unless SEALED.
          2. Re-verify the bundle in the archive; refuse if it is not intact.
          3. adapter.transform(SealedBundle) → chunks; validate them.
          4. Record the intent, write with the dispatch_id, record the receipt.

        Refusals (1, 2) raise DispatchRefusedError and log nothing. Failures
        in 3 and 4 are logged and returned as status="failed"; the record's
        ledger state is never modified by a failed dispatch.
        """
        record_id = record.record_id if isinstance(record, ArtifactRecord) else record
        live = self._ledger.get(record_id)
        if live.state is not ArtifactState.SEALED:
            raise DispatchRefusedError(
                f"record {record_id} is {live.state.value}; only sealed records are dispatched"
            )
        try:
            verify_archived_bundle(
                self._ledger.archive, live.artifact_hash, live.artifact_manifest
            )
        except (ArchiveError, ArtifactDriftError) as exc:
            raise DispatchRefusedError(
                f"record {record_id}'s archived bundle failed verification: {exc}"
            ) from exc

        dispatch_id = self._log.open_delivery(record_id, target)
        delivery = self._log.get(dispatch_id)
        if delivery.status is DeliveryStatus.DELIVERED:
            return self._result(delivery, target, already_delivered=True)

        try:
            chunks = adapter.transform(SealedBundle.from_record(live, self._ledger.archive))
            validate_chunks(chunks)
            writer = self._writer_for(type(target))
            digest = chunks_digest(chunks)
            if delivery.chunks_digest is not None and delivery.chunks_digest != digest:
                raise ValueError(
                    "adapter produced different chunks than this delivery's earlier "
                    "attempt; adapters must be deterministic"
                )
        except Exception as exc:
            self._log.append(dispatch_id, "failure", done=0, error=repr(exc))
            return self._result(self._log.get(dispatch_id), target)

        self._log.append(dispatch_id, "intent", planned=len(chunks), chunks_digest=digest)
        try:
            writer.write_chunks(chunks, target, dispatch_id=dispatch_id)
        except Exception as exc:
            done = exc.chunks_written if isinstance(exc, PartialWriteError) else None
            self._log.append(dispatch_id, "failure", done=done, error=repr(exc))
            return self._result(self._log.get(dispatch_id), target)
        self._log.append(dispatch_id, "receipt", done=len(chunks))

        # An invalidation that raced this write may have run its removals
        # before the data landed; remove it now rather than leave it live.
        if self._ledger.get(record_id).state is ArtifactState.INVALIDATED:
            self.retract(record_id)
        return self._result(self._log.get(dispatch_id), target)

    # -- invalidation --------------------------------------------------------

    def invalidate(self, record_id: str, reason: str) -> list[RemovalResult]:
        """Invalidate a record in the ledger, then remove what it delivered.

        Safe to call again: an already invalidated record just has its
        outstanding removals retried.
        """
        if self._ledger.get(record_id).state is not ArtifactState.INVALIDATED:
            self._ledger.invalidate(record_id, reason)
        return self.retract(record_id)

    def retract(self, record_id: str) -> list[RemovalResult]:
        """Remove the target data of every delivery an invalidated record made.

        Deliveries the log proves wrote nothing, and deliveries already
        removed, are skipped. Each removal is logged as intent, then receipt
        or failure; failed removals are retried by the next call.
        """
        state = self._ledger.get(record_id).state
        if state is not ArtifactState.INVALIDATED:
            raise ValueError(f"record {record_id} is {state.value}; only invalidated records are retracted")

        results: list[RemovalResult] = []
        for delivery in self._log.for_record(record_id):
            if delivery.status is DeliveryStatus.REMOVED or not delivery.possibly_written:
                continue
            results.append(self._remove(delivery))
        return results

    def retract_invalidated(self) -> list[RemovalResult]:
        """Catch up on removals for every invalidated record (e.g. invalidated
        directly in the ledger by stele.replay.invalidation)."""
        results: list[RemovalResult] = []
        for record in self._ledger.list_by_states([ArtifactState.INVALIDATED]):
            results.extend(self.retract(record.record_id))
        return results

    def _remove(self, delivery: Delivery) -> RemovalResult:
        dispatch_id = delivery.dispatch_id
        try:
            target_type, writer = self._writer_by_name(delivery.target_kind)
            target = target_type(**delivery.target)
        except Exception as exc:
            self._log.append(dispatch_id, "removal_failure", error=repr(exc))
            return RemovalResult(dispatch_id, "failed", None, repr(exc))

        self._log.append(dispatch_id, "removal_intent", planned=delivery.planned)
        try:
            removed = writer.remove_delivery(target, dispatch_id=dispatch_id)
        except Exception as exc:
            self._log.append(dispatch_id, "removal_failure", error=repr(exc))
            return RemovalResult(dispatch_id, "failed", None, repr(exc))
        self._log.append(dispatch_id, "removal_receipt", done=removed)
        return RemovalResult(dispatch_id, "removed", removed, None)

    # -- log -----------------------------------------------------------------

    def dispatch_log(self) -> list[Delivery]:
        """Every delivery in the ledger, oldest first, with its event history."""
        return self._log.all()

    def dispatch_log_for(self, record_id: str) -> list[Delivery]:
        """Every delivery of one record."""
        return self._log.for_record(record_id)

    def delivery(self, dispatch_id: str) -> Delivery:
        return self._log.get(dispatch_id)

    def close(self) -> None:
        self._log.close()

    # -- internal ------------------------------------------------------------

    def _writer_for(self, target_type: type) -> TargetWriter:
        writer = self._target_writers.get(target_type)
        if writer is None:
            raise UnregisteredTargetError(
                f"no writer registered for {target_type.__name__} — "
                f"call dispatcher.register_target({target_type.__name__}, writer) first"
            )
        return writer

    def _writer_by_name(self, target_kind: str) -> tuple[type, TargetWriter]:
        for target_type, writer in self._target_writers.items():
            if target_type.__name__ == target_kind:
                return target_type, writer
        raise UnregisteredTargetError(f"no writer registered for {target_kind}")

    def _result(
        self, delivery: Delivery, target: SteleTarget, *, already_delivered: bool = False
    ) -> DispatchResult:
        last = next(
            (e for e in reversed(delivery.events) if e.event in ("receipt", "failure")), None
        )
        succeeded = delivery.status is DeliveryStatus.DELIVERED
        return DispatchResult(
            dispatch_id=delivery.dispatch_id,
            record_id=delivery.record_id,
            target=target,
            chunks_submitted=delivery.planned,
            chunks_written=last.done if last is not None else None,
            status="success" if succeeded else "failed",
            error=None if succeeded else delivery.last_error,
            dispatched_at=datetime.now(timezone.utc),
            already_delivered=already_delivered,
        )
