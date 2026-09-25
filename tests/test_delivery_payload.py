"""
A delivery is bound to one payload (roadmap #13).

  1. Two dispatches of the same record to the same target that race with
     different adapter output cannot both reach their writers: the first
     intent binds the delivery, the other is refused before writing.
  2. The binding fingerprint covers every chunk's id, content hash, token
     count and metadata, so a retry that changes only metadata or a token
     count is refused.
  3. Metadata must have one canonical JSON encoding.
  4. The database itself refuses a second intent with another payload, and
     deliveries bound before schema 6 still retry.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from stele.contracts.adapter import (
    ChunkValidationError,
    LightRAGTarget,
    SealedBundle,
    SteleChunk,
    make_chunk,
    validate_chunks,
)
from stele.contracts.dispatcher import Dispatcher
from stele.ledger.delivery import (
    DeliveryLog,
    DeliveryStatus,
    PayloadConflictError,
    _legacy_chunks_digest,
    chunks_digest,
)
from tests.ledger_helpers import open_ledger
from tests.test_durable_dispatch import WS, Crash, LinesAdapter, MemoryTarget, _dispatcher, _sealed


@pytest.fixture
def ledger(tmp_path: Path):
    return open_ledger(tmp_path / "ledger.db")


@pytest.fixture
def target() -> MemoryTarget:
    return MemoryTarget()


class Fixed:
    """An adapter returning a fixed chunk list."""

    def __init__(self, chunks: list[SteleChunk]) -> None:
        self.chunks = chunks

    def transform(self, bundle: SealedBundle) -> list[SteleChunk]:
        return [replace(c, metadata=dict(c.metadata)) for c in self.chunks]


def _crash_mid_write(ledger, target, record, adapter) -> None:
    target.crash_after = 1
    with pytest.raises(Crash):
        _dispatcher(ledger, target).dispatch(adapter, record, WS)
    target.crash_after = None


# ---------------------------------------------------------------------------
# 1 — Concurrent dispatch
# ---------------------------------------------------------------------------

def test_racing_dispatches_with_different_payloads_write_only_one(tmp_path, target) -> None:
    db = tmp_path / "ledger.db"
    first, second = open_ledger(db), open_ledger(db)
    record = _sealed(first, tmp_path)
    # Both adapters finish transforming before either records an intent: the
    # interleaving the delivery log previously accepted twice.
    both_transformed = threading.Barrier(2, timeout=10)

    class Racing:
        def __init__(self, text: str) -> None:
            self.text = text

        def transform(self, bundle):
            chunks = [make_chunk("c0", self.text), make_chunk("c1", self.text + "!")]
            both_transformed.wait()
            return chunks

    results = {}

    def run(name, ledger, text) -> None:
        results[name] = _dispatcher(ledger, target).dispatch(Racing(text), record, WS)

    threads = [
        threading.Thread(target=run, args=("x", first, "payload X")),
        threading.Thread(target=run, args=("y", second, "payload Y")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["x"].dispatch_id == results["y"].dispatch_id
    statuses = sorted(r.status for r in results.values())
    assert statuses == ["failed", "success"]
    loser = next(r for r in results.values() if r.status == "failed")
    assert "different payload" in loser.error

    # The target holds exactly one payload under the dispatch_id, and it is
    # the one the log's intent names.
    winner_text = {content.rstrip("!") for content in target.rows.values()}
    assert len(winner_text) == 1 and len(target.rows) == 2
    [delivery] = Dispatcher(first).dispatch_log_for(record.record_id)
    assert [e.event for e in delivery.events].count("intent") == 1
    winner = [make_chunk("c0", t) for t in winner_text][0]
    expected = chunks_digest([winner, make_chunk("c1", winner.content + "!")])
    assert delivery.chunks_digest == expected
    assert delivery.status is DeliveryStatus.DELIVERED


def test_a_refused_payload_leaves_the_delivery_log_unchanged(ledger, target, tmp_path) -> None:
    record = _sealed(ledger, tmp_path)
    _crash_mid_write(ledger, target, record, LinesAdapter())
    dispatcher = _dispatcher(ledger, target)
    [before] = dispatcher.dispatch_log_for(record.record_id)
    result = dispatcher.dispatch(Fixed([make_chunk("x", "something else")]), record, WS)
    assert result.status == "failed" and (result.chunks_submitted, result.chunks_written) == (0, 0)
    [after] = dispatcher.dispatch_log_for(record.record_id)
    assert after.events == before.events
    assert after.status is DeliveryStatus.IN_FLIGHT and after.possibly_written


def test_adapter_failure_after_an_outstanding_intent_does_not_claim_nothing_was_written(
    ledger, target, tmp_path
) -> None:
    class Broken:
        def transform(self, bundle):
            raise ValueError("cannot parse")

    record = _sealed(ledger, tmp_path)
    _crash_mid_write(ledger, target, record, LinesAdapter())
    result = _dispatcher(ledger, target).dispatch(Broken(), record, WS)
    assert result.status == "failed"
    [delivery] = _dispatcher(ledger, target).dispatch_log_for(record.record_id)
    # Another attempt's write may have landed; this failure must not hide it.
    # It reports only its own attempt, which wrote nothing.
    last = delivery.events[-1]
    assert last.event == "failure" and last.done == 0
    assert last.attempt_id not in {e.attempt_id for e in delivery.events[:-1]}
    assert delivery.possibly_written


# ---------------------------------------------------------------------------
# 2 — The fingerprint covers the whole chunk
# ---------------------------------------------------------------------------

BASE = [
    make_chunk("message-42", "hello", parent="message-10", path=["a", "b"]),
    make_chunk("message-43", "world"),
]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda c: replace(c, metadata={**c.metadata, "parent": "message-37"}), id="metadata"),
        pytest.param(lambda c: replace(c, token_count=c.token_count + 1), id="token_count"),
        pytest.param(lambda c: replace(c, metadata={**c.metadata, "path": ["b", "a"]}), id="list-order"),
    ],
)
def test_retry_changing_only_non_content_fields_is_refused(
    ledger, target, tmp_path, change
) -> None:
    record = _sealed(ledger, tmp_path)
    _crash_mid_write(ledger, target, record, Fixed(BASE))
    rows_before = dict(target.rows)

    changed = [change(BASE[0]), BASE[1]]
    assert chunks_digest(changed) != chunks_digest(BASE)
    result = _dispatcher(ledger, target).dispatch(Fixed(changed), record, WS)
    assert result.status == "failed" and "different payload" in result.error
    assert target.rows == rows_before

    # The original payload still completes the delivery.
    assert _dispatcher(ledger, target).dispatch(Fixed(BASE), record, WS).status == "success"


def test_nested_metadata_key_order_does_not_change_the_digest() -> None:
    a = make_chunk("c", "text", info={"x": 1, "y": {"p": True, "q": None}})
    b = make_chunk("c", "text", info={"y": {"q": None, "p": True}, "x": 1})
    assert chunks_digest([a]) == chunks_digest([b])


def test_chunk_order_changes_the_digest() -> None:
    assert chunks_digest(BASE) != chunks_digest(list(reversed(BASE)))


# ---------------------------------------------------------------------------
# 3 — Metadata has one canonical encoding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"k": (1, 2)}, id="tuple"),
        pytest.param({"k": {1, 2}}, id="set"),
        pytest.param({1: "int key"}, id="int-key"),
        pytest.param({"k": float("nan")}, id="nan"),
        pytest.param({"k": float("inf")}, id="inf"),
        pytest.param({"k": object()}, id="object"),
        pytest.param({"k": b"bytes"}, id="bytes"),
        pytest.param({"k": [{"n": (1,)}]}, id="nested-tuple"),
        pytest.param([("k", "v")], id="not-a-dict"),
    ],
)
def test_non_canonical_metadata_is_rejected(metadata) -> None:
    chunk = replace(make_chunk("c", "text"), metadata=metadata)
    with pytest.raises(ChunkValidationError, match="metadata"):
        validate_chunks([chunk])


@pytest.mark.parametrize("token_count", [1.0, True, "3"])
def test_token_count_must_be_an_int(token_count) -> None:
    with pytest.raises(ChunkValidationError, match="token_count"):
        validate_chunks([replace(make_chunk("c", "text"), token_count=token_count)])


def test_json_metadata_is_accepted() -> None:
    validate_chunks([make_chunk("c", "text", a=1, b=1.5, c=[None, True, "s"], d={"e": {}})])


def test_non_canonical_metadata_fails_the_dispatch_before_any_write(
    ledger, target, tmp_path
) -> None:
    record = _sealed(ledger, tmp_path)
    bad = replace(make_chunk("c", "text"), metadata={"k": (1, 2)})
    result = _dispatcher(ledger, target).dispatch(Fixed([bad]), record, WS)
    assert result.status == "failed" and "metadata" in result.error
    assert target.write_calls == 0


# ---------------------------------------------------------------------------
# 4 — Database enforcement and pre-schema-6 deliveries
# ---------------------------------------------------------------------------

def test_the_database_refuses_a_second_payload(ledger, tmp_path) -> None:
    record = _sealed(ledger, tmp_path)
    log = DeliveryLog(ledger)
    dispatch_id = log.open_delivery(record.record_id, WS)
    log.append_intent(dispatch_id, BASE)

    with pytest.raises(PayloadConflictError):
        log.append_intent(dispatch_id, BASE[:1])
    with pytest.raises(ValueError, match="bound to the payload"):
        log.append(dispatch_id, "intent", planned=2, chunks_digest=chunks_digest(BASE[::-1]))
    with pytest.raises(ValueError, match="bound to the payload"):
        log.append(dispatch_id, "intent", planned=2)  # an intent must name its payload
    log.append_intent(dispatch_id, BASE)  # the same payload again is a retry
    assert [e.event for e in log.get(dispatch_id).events] == ["intent", "intent"]
    log.close()


def test_a_delivery_bound_before_schema_5_still_retries(ledger, target, tmp_path) -> None:
    record = _sealed(ledger, tmp_path)
    log = DeliveryLog(ledger)
    dispatch_id = log.open_delivery(record.record_id, WS)
    legacy = _legacy_chunks_digest(BASE)
    log.append(dispatch_id, "intent", planned=2, chunks_digest=legacy)
    log.close()

    dispatcher = _dispatcher(ledger, target)
    assert dispatcher.dispatch(Fixed(BASE), record, WS).status == "success"
    delivery = dispatcher.delivery(dispatch_id)
    assert {e.chunks_digest for e in delivery.events if e.event == "intent"} == {legacy}

    # The legacy digest still refuses other content.
    other = _sealed(ledger, tmp_path, "other\n")
    log = DeliveryLog(ledger)
    other_id = log.open_delivery(other.record_id, LightRAGTarget("legacy"))
    log.append(other_id, "intent", planned=2, chunks_digest=legacy)
    with pytest.raises(PayloadConflictError):
        log.append_intent(other_id, [BASE[0], make_chunk("message-43", "changed")])
    log.close()


def test_version_5_ledger_gains_payload_binding(tmp_path: Path) -> None:
    from stele.ledger.store import REPLAY_DDL, SCHEMA_VERSION

    db = tmp_path / "ledger.db"
    ledger = open_ledger(db)
    record = _sealed(ledger, tmp_path)
    ledger.close()

    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER delivery_events_one_payload")
    conn.execute("ALTER TABLE delivery_events DROP COLUMN attempt_id")  # added by version 7
    conn.execute("DROP TABLE replays")
    for statement in REPLAY_DDL:  # the v5 replay log, without 'failed'
        conn.execute(statement)
    conn.execute(
        "INSERT INTO replays (replay_id, record_id, outcome, reason, differences, platform, "
        "replayed_at) VALUES ('r1', ?, 'diverged', 'old', '[]', 'linux', '2026-01-01')",
        (record.record_id,),
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    ledger = open_ledger(db)
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    assert conn.execute("SELECT outcome, reason FROM replays").fetchall() == [("diverged", "old")]
    conn.execute(
        "INSERT INTO replays (replay_id, record_id, outcome, reason, differences, platform, "
        "replayed_at) VALUES ('r2', ?, 'failed', 'new', '[]', 'linux', '2026-01-02')",
        (record.record_id,),
    )
    conn.commit()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM replays")
    conn.close()

    log = DeliveryLog(ledger)
    dispatch_id = log.open_delivery(record.record_id, WS)
    log.append_intent(dispatch_id, BASE)
    with pytest.raises(ValueError, match="bound to the payload"):
        log.append(dispatch_id, "intent", planned=1, chunks_digest=chunks_digest(BASE[:1]))
    log.close()
