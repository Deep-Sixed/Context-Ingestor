"""
The durable delivery log (roadmap #13).

A delivery is one ledger record sent to one target. It is identified by its
dispatch_id, which the target writer uses as its idempotency key: retrying a
delivery reuses the dispatch_id, so a retry cannot duplicate data. What
happened to a delivery is an append-only sequence of events, each committed
before the step it announces (intent) or after the step it reports (receipt,
failure):

    intent           chunks are about to be written (planned, chunks_digest)
    receipt          the writer returned; done = chunks written
    failure          the attempt failed; done = chunks written, NULL if unknown
    removal_intent   the delivery's target data is about to be removed
    removal_receipt  removal returned; done = chunks removed, NULL if unknown
    removal_failure  removal failed

A delivery writes exactly one payload. Its first intent binds the
delivery to that payload's chunks_digest; a retry, or a concurrent dispatch of
the same record to the same target, must present the same payload or it is
refused before its writer is called (append_intent, backed by a trigger).

Each write attempt has its own attempt_id, carried by its intent and by its
receipt or failure, so attempts that overlap (two dispatchers delivering the
same record to the same target) are told apart: one attempt's receipt never
closes another's intent. Events written before ledger schema 8 have no
attempt_id and are read as one attempt at a time, as they were written.

A crash at any point therefore leaves a log that bounds what the target
holds: an intent without an outcome of its own means the target may hold
anything from none to all of the planned chunks, under that dispatch_id. The log lives in
the ledger database, beside the records, and is never updated or deleted.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from ..archive.records import canonical_json
from .events import append_event, write_transaction
from .store import LedgerStore, connect

WRITE_EVENTS = ("intent", "receipt", "failure")
REMOVAL_EVENTS = ("removal_intent", "removal_receipt", "removal_failure")


class DeliveryStatus(str, Enum):
    """Where a delivery stands, from its last event."""

    NOT_ATTEMPTED = "not_attempted"    # no write was ever attempted
    IN_FLIGHT = "in_flight"            # intent without outcome: target holds 0..planned
    DELIVERED = "delivered"            # the writer confirmed every chunk
    FAILED = "failed"                  # the last attempt failed; see done
    REMOVING = "removing"              # removal intent without outcome
    REMOVED = "removed"                # the writer confirmed removal
    REMOVAL_FAILED = "removal_failed"  # the last removal attempt failed


@dataclass(frozen=True)
class DeliveryEvent:
    event_id: int
    event: str
    planned: int | None
    chunks_digest: str | None
    done: int | None
    error: str | None
    at: datetime
    # The write attempt an intent, receipt or failure belongs to; None for
    # removal events and for events written before ledger schema 8.
    attempt_id: str | None = None


@dataclass(frozen=True)
class Delivery:
    """One record sent to one target, with its full event history."""

    dispatch_id: str
    record_id: str
    target_kind: str
    target: dict[str, Any]
    created_at: datetime
    events: tuple[DeliveryEvent, ...]

    @property
    def status(self) -> DeliveryStatus:
        """From the last event: a write that lands after a removal counts again."""
        if not self.events:
            return DeliveryStatus.NOT_ATTEMPTED
        return {
            "intent": DeliveryStatus.IN_FLIGHT,
            "receipt": DeliveryStatus.DELIVERED,
            "failure": DeliveryStatus.FAILED,
            "removal_intent": DeliveryStatus.REMOVING,
            "removal_receipt": DeliveryStatus.REMOVED,
            "removal_failure": DeliveryStatus.REMOVAL_FAILED,
        }[self.events[-1].event]

    @property
    def chunks_digest(self) -> str | None:
        """Digest of the chunks this delivery writes; fixed by its first intent."""
        for e in self.events:
            if e.event == "intent":
                return e.chunks_digest
        return None

    @property
    def planned(self) -> int:
        """Chunks this delivery writes (0 if it never reached an intent)."""
        for e in self.events:
            if e.event == "intent":
                return e.planned
        return 0

    @property
    def last_error(self) -> str | None:
        for e in reversed(self.events):
            if e.event in ("failure", "removal_failure"):
                return e.error
        return None

    @property
    def possibly_written(self) -> bool:
        """True unless the log proves the target holds nothing of this delivery.

        An attempt wrote nothing only if its intent is followed by its own
        failure reporting done == 0. Any receipt, partial or unknown failure,
        or intent without an outcome of its own (a crash, or a write still
        running) may have left data. A removal receipt clears what was
        written before it, but not a write still in flight, which may land
        after the removal.

        Attempts are matched by attempt_id, so one attempt's outcome never
        closes another's intent. Events from before schema 8 carry none and
        are read in order, one attempt at a time.
        """
        written = False
        outstanding: set[str] = set()   # attempts with an intent and no outcome yet
        legacy_in_flight = False        # the same, for events without an attempt_id
        for e in self.events:
            if e.event == "intent":
                if e.attempt_id is None:
                    written = written or legacy_in_flight  # an earlier attempt never reported
                    legacy_in_flight = True
                else:
                    outstanding.add(e.attempt_id)
            elif e.event in ("receipt", "failure"):
                if e.event == "receipt" or e.done != 0:
                    written = True
                if e.attempt_id is None:
                    legacy_in_flight = False
                else:
                    outstanding.discard(e.attempt_id)
            elif e.event == "removal_receipt":
                written = False
        return written or legacy_in_flight or bool(outstanding)

    def explain(self) -> str:
        """What the target holds under this dispatch_id, per the log."""
        status = self.status
        if status is DeliveryStatus.NOT_ATTEMPTED:
            return "nothing was written"
        if status is DeliveryStatus.DELIVERED:
            return f"all {self.planned} chunks were written"
        if status is DeliveryStatus.REMOVED and not self.possibly_written:
            return "the delivery's data was removed"
        # A removal does not cover an attempt still in flight when it ran.
        if not self.possibly_written:
            return "nothing was written"
        return (
            f"between 0 and {self.planned} chunks may be at the target "
            f"under dispatch_id {self.dispatch_id} ({status.value})"
        )


class PayloadConflictError(ValueError):
    """A delivery's intent named a different payload than its first intent."""


# Digests written before schema 6 covered only chunk ids and content hashes;
# they are bare hex. Current digests carry this prefix.
_DIGEST_PREFIX = "chunks-v2:"


def chunks_digest(chunks: list[Any]) -> str:
    """Digest of everything a target receives in a chunk list, in order.

    Covers each chunk's id, content hash, token count and metadata (as
    canonical JSON, which validate_chunks guarantees it has): a retry that
    changes only metadata or a token count is a different payload.
    """
    body = [
        {
            "chunk_id": c.chunk_id,
            "content_hash": c.content_hash,
            "token_count": c.token_count,
            "metadata": c.metadata,
        }
        for c in chunks
    ]
    digest = hashlib.sha256(canonical_json({"chunks": body})).hexdigest()
    return _DIGEST_PREFIX + digest


def _legacy_chunks_digest(chunks: list[Any]) -> str:
    body = [[c.chunk_id, c.content_hash] for c in chunks]
    return hashlib.sha256(canonical_json({"chunks": body})).hexdigest()


def payload_matches(bound_digest: str, chunks: list[Any]) -> bool:
    """True if chunks are the payload a delivery's first intent recorded.

    A delivery bound before schema 6 can only be checked as far as its
    digest reaches (chunk ids and content hashes).
    """
    if bound_digest.startswith(_DIGEST_PREFIX):
        return bound_digest == chunks_digest(chunks)
    return bound_digest == _legacy_chunks_digest(chunks)


def encode_target(target: Any) -> tuple[str, str]:
    """(target_kind, canonical JSON of its fields) for a dataclass target."""
    from dataclasses import asdict, is_dataclass

    if not is_dataclass(target) or isinstance(target, type):
        raise TypeError(f"a target must be a dataclass instance, got {target!r}")
    return type(target).__name__, canonical_json(asdict(target)).decode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeliveryLog:
    """Reads and appends the delivery log in a ledger's database."""

    def __init__(self, ledger: LedgerStore) -> None:
        self._conn = connect(ledger.db_path)

    def close(self) -> None:
        self._conn.close()

    def open_delivery(self, record_id: str, target: Any) -> str:
        """dispatch_id of record_id's delivery to target, created if new."""
        kind, encoded = encode_target(target)
        dispatch_id = str(uuid4())
        with write_transaction(self._conn):
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO deliveries "
                "(dispatch_id, record_id, target_kind, target, created_at) VALUES (?,?,?,?,?)",
                (dispatch_id, record_id, kind, encoded, _now()),
            )
            if cur.rowcount == 1:
                append_event(self._conn, "delivery.opened", dispatch_id, {
                    "record_id": record_id, "target_kind": kind, "target": encoded,
                })
        row = self._conn.execute(
            "SELECT dispatch_id FROM deliveries "
            "WHERE record_id=? AND target_kind=? AND target=?",
            (record_id, kind, encoded),
        ).fetchone()
        return row["dispatch_id"]

    def append(
        self,
        dispatch_id: str,
        event: str,
        *,
        planned: int | None = None,
        chunks_digest: str | None = None,
        done: int | None = None,
        error: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        """Durably append one event (committed, with its chain event, before this returns).

        A receipt or failure passes the attempt_id its intent was given.
        """
        try:
            with write_transaction(self._conn):
                self._insert(dispatch_id, event, planned, chunks_digest, done, error, attempt_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"cannot record {event!r} for {dispatch_id}: {exc}") from exc

    def _insert(
        self, dispatch_id: str, event: str, planned: int | None, chunks_digest: str | None,
        done: int | None, error: str | None, attempt_id: str | None,
    ) -> None:
        """Insert one delivery event and its chain event (inside a write transaction)."""
        self._conn.execute(
            "INSERT INTO delivery_events "
            "(dispatch_id, event, planned, chunks_digest, done, error, at, attempt_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (dispatch_id, event, planned, chunks_digest, done, error, _now(), attempt_id),
        )
        append_event(self._conn, "delivery.event", dispatch_id, {
            "event": event, "planned": planned, "chunks_digest": chunks_digest,
            "done": done, "error": error, "attempt_id": attempt_id,
        })

    def append_intent(self, dispatch_id: str, chunks: list[Any]) -> str:
        """Durably record the intent to write chunks; return its attempt_id.

        In one write transaction: if the delivery already has an intent, the
        chunks must be that intent's payload (else PayloadConflictError and
        nothing is recorded); otherwise this intent binds the delivery to
        them. Two dispatchers racing on one delivery therefore cannot both
        reach their writers with different payloads under its dispatch_id.
        The attempt's receipt or failure must carry the returned attempt_id.
        """
        attempt_id = str(uuid4())
        with write_transaction(self._conn):
            first = self._conn.execute(
                "SELECT planned, chunks_digest FROM delivery_events "
                "WHERE dispatch_id=? AND event='intent' ORDER BY event_id LIMIT 1",
                (dispatch_id,),
            ).fetchone()
            if first is None:
                digest = chunks_digest(chunks)
            elif first["planned"] == len(chunks) and payload_matches(first["chunks_digest"], chunks):
                digest = first["chunks_digest"]
            else:
                raise PayloadConflictError(
                    f"delivery {dispatch_id} is bound to a different payload than the "
                    "chunks presented now; adapters must be deterministic"
                )
            try:
                self._insert(dispatch_id, "intent", len(chunks), digest, None, None, attempt_id)
            except sqlite3.IntegrityError as exc:  # the payload-binding trigger
                raise PayloadConflictError(f"delivery {dispatch_id}: {exc}") from exc
        return attempt_id

    def append_unwritten_failure(self, dispatch_id: str, error: str) -> None:
        """Record a failure that happened before this attempt's writer was called.

        The attempt never recorded an intent and wrote nothing, so it gets an
        attempt_id of its own with done=0. That says nothing about any other
        attempt of the delivery, whose outstanding intent stays outstanding.
        """
        self.append(dispatch_id, "failure", done=0, error=error, attempt_id=str(uuid4()))

    def get(self, dispatch_id: str) -> Delivery:
        row = self._conn.execute(
            "SELECT * FROM deliveries WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no delivery {dispatch_id}")
        return self._delivery(row)

    def for_record(self, record_id: str) -> list[Delivery]:
        rows = self._conn.execute(
            "SELECT * FROM deliveries WHERE record_id=? ORDER BY created_at, dispatch_id",
            (record_id,),
        ).fetchall()
        return [self._delivery(r) for r in rows]

    def all(self) -> list[Delivery]:
        rows = self._conn.execute(
            "SELECT * FROM deliveries ORDER BY created_at, dispatch_id"
        ).fetchall()
        return [self._delivery(r) for r in rows]

    def _delivery(self, row: sqlite3.Row) -> Delivery:
        events = self._conn.execute(
            "SELECT * FROM delivery_events WHERE dispatch_id=? ORDER BY event_id",
            (row["dispatch_id"],),
        ).fetchall()
        return Delivery(
            dispatch_id=row["dispatch_id"],
            record_id=row["record_id"],
            target_kind=row["target_kind"],
            target=json.loads(row["target"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            events=tuple(
                DeliveryEvent(
                    event_id=e["event_id"],
                    event=e["event"],
                    planned=e["planned"],
                    chunks_digest=e["chunks_digest"],
                    done=e["done"],
                    error=e["error"],
                    at=datetime.fromisoformat(e["at"]),
                    attempt_id=e["attempt_id"],
                )
                for e in events
            ),
        )
