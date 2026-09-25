"""
Overlapping write attempts on one delivery are told apart (ledger schema 7).

Two dispatchers can deliver the same record to the same target at once; #35
lets them when their payloads match. Each attempt's intent, receipt and
failure carry its attempt_id, so one attempt's receipt never closes the
other's intent. Without that, a receipt from attempt A made the log claim
that attempt B (which crashed after writing) left nothing at the target, and
invalidation skipped B's data.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stele.contracts.adapter import LightRAGTarget
from stele.contracts.dispatcher import Dispatcher
import stele.ledger.delivery as delivery_module
from stele.ledger.delivery import DeliveryLog, DeliveryStatus
from stele.ledger.store import SCHEMA_VERSION
from tests.ledger_helpers import open_ledger
from tests.test_durable_dispatch import WS, Crash, LinesAdapter, MemoryTarget, _dispatcher, _sealed


def test_a_crashed_attempt_overlapping_another_is_still_removed(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger_b, ledger_a = open_ledger(db), open_ledger(db)
    record = _sealed(ledger_b, tmp_path)
    target = MemoryTarget()
    dispatcher_a = _dispatcher(ledger_a, target)

    class OverlappedThenCrashes(MemoryTarget):
        """Attempt B's writer: while B is in flight, attempt A delivers the
        same payload and the record is invalidated; then B's write lands
        and B's process dies before it can log anything."""

        def write_chunks(self, chunks, t, *, dispatch_id):
            assert dispatcher_a.dispatch(LinesAdapter(), record, WS).status == "success"
            dispatcher_a.invalidate(record.record_id, "superseded")
            assert target.rows == {}  # A's rows were removed
            target.write_chunks(chunks, t, dispatch_id=dispatch_id)
            raise Crash()

    dispatcher_b = Dispatcher(ledger_b)
    dispatcher_b.register_target(LightRAGTarget, OverlappedThenCrashes())
    with pytest.raises(Crash):
        dispatcher_b.dispatch(LinesAdapter(), record, WS)
    assert len(target.rows) == 2  # B's data landed after the removal

    # A fresh process reads the log.
    dispatcher = _dispatcher(open_ledger(db), target)
    [delivery] = dispatcher.dispatch_log_for(record.record_id)
    assert [e.event for e in delivery.events] == [
        "intent",                              # B
        "intent", "receipt",                   # A
        "removal_intent", "removal_receipt",   # the invalidation
    ]
    b, a = delivery.events[0].attempt_id, delivery.events[1].attempt_id
    assert a and b and a != b and delivery.events[2].attempt_id == a
    assert delivery.possibly_written  # B never reported
    assert "may be at the target" in delivery.explain()

    [removal] = dispatcher.retract(record.record_id)
    assert removal.status == "removed" and target.rows == {}


def test_each_attempt_is_closed_only_by_its_own_outcome(ledger, tmp_path) -> None:
    record = _sealed(ledger, tmp_path)
    log = DeliveryLog(ledger)
    chunks = LinesAdapter().transform(_bundle(ledger, record))
    dispatch_id = log.open_delivery(record.record_id, WS)

    first = log.append_intent(dispatch_id, chunks)
    second = log.append_intent(dispatch_id, chunks)
    log.append(dispatch_id, "failure", done=0, error="nothing written", attempt_id=first)
    # The second attempt is still open: the first one's failure says nothing about it.
    assert log.get(dispatch_id).possibly_written

    log.append(dispatch_id, "failure", done=0, error="nothing written", attempt_id=second)
    assert not log.get(dispatch_id).possibly_written
    log.close()


def test_every_dispatch_gets_a_fresh_attempt(ledger, target, tmp_path) -> None:
    record = _sealed(ledger, tmp_path)
    target.fail_after = 1
    dispatcher = _dispatcher(ledger, target)
    dispatcher.dispatch(LinesAdapter(), record, WS)
    target.fail_after = None
    dispatcher.dispatch(LinesAdapter(), record, WS)

    [delivery] = dispatcher.dispatch_log_for(record.record_id)
    events = [(e.event, e.attempt_id) for e in delivery.events]
    assert [e for e, _ in events] == ["intent", "failure", "intent", "receipt"]
    (_, a1), (_, a1_out), (_, a2), (_, a2_out) = events
    assert a1 == a1_out and a2 == a2_out and a1 != a2
    assert delivery.status is DeliveryStatus.DELIVERED


def test_a_failure_before_writing_does_not_close_an_outstanding_attempt(
    ledger, target, tmp_path
) -> None:
    class Broken:
        def transform(self, bundle):
            raise ValueError("cannot parse")

    record = _sealed(ledger, tmp_path)
    log = DeliveryLog(ledger)
    dispatch_id = log.open_delivery(record.record_id, WS)
    log.append_intent(dispatch_id, LinesAdapter().transform(_bundle(ledger, record)))
    log.close()

    _dispatcher(ledger, target).dispatch(Broken(), record, WS)
    delivery = _dispatcher(ledger, target).delivery(dispatch_id)
    assert delivery.events[-1].done == 0  # exact for its own attempt, which wrote nothing
    assert delivery.possibly_written      # the other attempt is still outstanding


# ---------------------------------------------------------------------------
# Events from before schema 7
# ---------------------------------------------------------------------------

def _legacy_ledger(tmp_path: Path, events: list[tuple[str, int | None]]):
    """A version-6 ledger whose one delivery has these (event, done) events."""
    db = tmp_path / "ledger.db"
    ledger = open_ledger(db)
    record = _sealed(ledger, tmp_path)
    log = DeliveryLog(ledger)
    dispatch_id = log.open_delivery(record.record_id, WS)
    log.close()
    ledger.close()

    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE delivery_events DROP COLUMN attempt_id")
    for i, (event, done) in enumerate(events):
        conn.execute(
            "INSERT INTO delivery_events (dispatch_id, event, planned, chunks_digest, done, at) "
            "VALUES (?,?,?,?,?,?)",
            (dispatch_id, event, 2 if event == "intent" else None,
             "0" * 64 if event == "intent" else None, done, f"2026-01-01T00:00:{i:02d}+00:00"),
        )
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()
    return open_ledger(db), record, dispatch_id


def test_version_6_events_migrate_and_read_as_before(tmp_path: Path) -> None:
    # A crashed attempt, a retry that delivered, then a removal: before
    # attempt ids the log read this as "nothing left", and still does.
    ledger, record, dispatch_id = _legacy_ledger(tmp_path, [
        ("intent", None), ("intent", None), ("receipt", 2),
        ("removal_intent", None), ("removal_receipt", 2),
    ])
    conn = sqlite3.connect(ledger.db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    conn.close()

    log = DeliveryLog(ledger)
    delivery = log.get(dispatch_id)
    assert all(e.attempt_id is None for e in delivery.events)
    assert not delivery.possibly_written
    log.close()


def test_a_new_attempt_does_not_close_a_legacy_intent(tmp_path: Path, monkeypatch) -> None:
    ledger, record, dispatch_id = _legacy_ledger(tmp_path, [("intent", None)])
    # The legacy intent recorded a made-up digest; let two chunks match it.
    monkeypatch.setattr(delivery_module, "_legacy_chunks_digest", lambda chunks: "0" * 64)
    log = DeliveryLog(ledger)
    attempt = log.append_intent(dispatch_id, [object(), object()])
    log.append(dispatch_id, "failure", done=0, error="refused", attempt_id=attempt)
    assert log.get(dispatch_id).possibly_written  # the legacy intent never reported
    log.close()


# ---------------------------------------------------------------------------

@pytest.fixture
def ledger(tmp_path: Path):
    return open_ledger(tmp_path / "ledger.db")


@pytest.fixture
def target() -> MemoryTarget:
    return MemoryTarget()


def _bundle(ledger, record):
    from stele.contracts.adapter import SealedBundle

    return SealedBundle.from_record(ledger.get(record.record_id), ledger.archive)

