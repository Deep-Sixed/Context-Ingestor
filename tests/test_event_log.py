"""
Hash-chained event log (roadmap #15: "Append-only, hash-chained event log").

  1. Every ledger fact is an event, committed with the change it describes:
     records, deliveries and their events, replays.
  2. The chain verifies, and so does the ledger replayed from it.
  3. Tampering is detected: an altered, deleted or reordered event; a record
     row edited by hand; a delivery or replay row changed or removed; a
     truncated-and-rewritten chain against a published anchor.
  4. Migration logs an existing ledger as imported events.
  5. The CLI reports the head and any problem.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from stele.contracts.adapter import LightRAGTarget, make_chunk
from stele.contracts.dispatcher import Dispatcher
from stele.ledger.events import GENESIS, EventLog, event_hash, verify_ledger
from stele.ledger.store import SCHEMA_VERSION, LedgerStore
from stele.replay.engine import replay_record
from stele.replay.parsers import ParserCatalog
from tests.ledger_helpers import PROVENANCE, open_ledger


@pytest.fixture
def ledger(tmp_path: Path) -> LedgerStore:
    return open_ledger(tmp_path / "ledger.db")


def _pending(ledger: LedgerStore, tmp_path: Path, text: str = "hello"):
    out = tmp_path / f"out-{uuid.uuid4().hex[:6]}"
    out.mkdir()
    (out / "doc.txt").write_text(text)
    return ledger.create_pending(
        **PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=out,
        artifact_paths=[out / "doc.txt"],
    )


class Target:
    def __init__(self) -> None:
        self.rows: dict = {}

    def write_chunks(self, chunks, target, *, dispatch_id):
        for c in chunks:
            self.rows[(dispatch_id, c.chunk_id)] = c.content

    def remove_delivery(self, target, *, dispatch_id):
        keys = [k for k in self.rows if k[0] == dispatch_id]
        for k in keys:
            del self.rows[k]
        return len(keys)


class Adapter:
    def transform(self, bundle):
        return [make_chunk(p, bundle.read_text(p)) for p in bundle.paths()]


def _lifecycle(ledger: LedgerStore, tmp_path: Path) -> dict[str, str]:
    """Every kind of fact: sealed, failed, invalidated, delivered, removed, replayed."""
    sealed = ledger.seal(_pending(ledger, tmp_path, "keep").record_id)
    failed = ledger.fail(_pending(ledger, tmp_path, "bad").record_id, "parser crashed")
    withdrawn = ledger.seal(_pending(ledger, tmp_path, "stale").record_id)

    dispatcher = Dispatcher(ledger)
    dispatcher.register_target(LightRAGTarget, Target())
    dispatcher.dispatch(Adapter(), sealed, LightRAGTarget("ws"))
    dispatcher.dispatch(Adapter(), withdrawn, LightRAGTarget("ws"))
    dispatcher.invalidate(withdrawn.record_id, "superseded")
    replay = replay_record(ledger, ParserCatalog(), sealed)  # UNREPLAYABLE: no spec
    return {"sealed": sealed.record_id, "failed": failed.record_id,
            "withdrawn": withdrawn.record_id, "replay": replay.replay_id}


def _raw(ledger: LedgerStore) -> sqlite3.Connection:
    """A connection with the append-only triggers dropped, as an attacker would."""
    conn = sqlite3.connect(ledger.db_path, isolation_level=None)
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f"DROP TRIGGER {name}")
    return conn


# ---------------------------------------------------------------------------
# 1 + 2 — every fact is chained, and the chain verifies
# ---------------------------------------------------------------------------

class TestChain:

    def test_every_fact_is_an_event(self, ledger, tmp_path) -> None:
        ids = _lifecycle(ledger, tmp_path)
        log = EventLog(ledger)
        kinds = [e.kind for e in log.events()]
        assert kinds.count("record.created") == 3
        assert kinds.count("record.sealed") == 2
        assert kinds.count("record.failed") == 1
        assert kinds.count("record.invalidated") == 1
        assert kinds.count("delivery.opened") == 2
        # intent + receipt for each delivery, then removal intent + receipt.
        assert kinds.count("delivery.event") == 6
        assert kinds.count("replay.logged") == 1

        [created, sealed] = log.for_subject(ids["sealed"])
        assert created.kind == "record.created"
        assert created.body["artifact_hash"] == ledger.get(ids["sealed"]).artifact_hash
        assert created.body["parser"]["name"] == PROVENANCE["parser"].name
        assert sealed.body == {"artifact_hash": created.body["artifact_hash"]}
        assert log.for_subject(ids["withdrawn"])[-1].body == {"reason": "superseded"}

    def test_bound_intents_and_unwritten_failures_are_events(self, ledger, tmp_path) -> None:
        from stele.ledger.delivery import DeliveryLog, PayloadConflictError

        record = ledger.seal(_pending(ledger, tmp_path).record_id)
        log = DeliveryLog(ledger)
        dispatch_id = log.open_delivery(record.record_id, LightRAGTarget("docs"))
        log.append_intent(dispatch_id, [make_chunk("a", "one")])
        with pytest.raises(PayloadConflictError):
            log.append_intent(dispatch_id, [make_chunk("a", "two")])   # refused: logs nothing
        log.append_unwritten_failure(dispatch_id, "adapter crashed")
        log.close()
        bodies = [e.body for e in EventLog(ledger).for_subject(dispatch_id)
                  if e.kind == "delivery.event"]
        # The failure wrote nothing under its own attempt; the intent's attempt
        # stays open (the chain commits to both attempt ids).
        assert [(b["event"], b["done"]) for b in bodies] == [("intent", None), ("failure", 0)]
        assert bodies[0]["attempt_id"] and bodies[1]["attempt_id"]
        assert bodies[0]["attempt_id"] != bodies[1]["attempt_id"]
        log = DeliveryLog(ledger)
        assert log.get(dispatch_id).possibly_written
        log.close()
        assert verify_ledger(ledger).ok

    def test_chain_links_and_verifies(self, ledger, tmp_path) -> None:
        _lifecycle(ledger, tmp_path)
        log = EventLog(ledger)
        events = list(log.events())
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert events[0].prev_hash == GENESIS
        for before, after in zip(events, events[1:]):
            assert after.prev_hash == before.hash
        assert log.head() == (events[-1].seq, events[-1].hash)

        report = verify_ledger(ledger)
        assert report.ok, (report.chain.problems, report.problems)
        assert report.chain.length == len(events)

    def test_a_refused_transition_logs_nothing(self, ledger, tmp_path) -> None:
        record = ledger.fail(_pending(ledger, tmp_path).record_id, "boom")
        before = EventLog(ledger).head()
        with pytest.raises(Exception):
            ledger.seal(record.record_id)
        with pytest.raises(Exception):
            ledger.invalidate(record.record_id, "no")
        assert EventLog(ledger).head() == before
        assert verify_ledger(ledger).ok

    def test_duplicate_run_logs_nothing(self, ledger, tmp_path) -> None:
        record = _pending(ledger, tmp_path)
        head = EventLog(ledger).head()
        out = tmp_path / "again"
        out.mkdir()
        (out / "x").write_text("x")
        with pytest.raises(Exception):
            ledger.create_pending(**PROVENANCE, run_id=record.run_id, artifact_dir=out,
                                  artifact_paths=[out / "x"])
        assert EventLog(ledger).head() == head

    def test_the_event_table_is_append_only(self, ledger, tmp_path) -> None:
        _pending(ledger, tmp_path)
        conn = sqlite3.connect(ledger.db_path)
        for statement in ("UPDATE events SET body='{}'", "DELETE FROM events"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(statement)
        conn.close()

    def test_hash_covers_every_field(self) -> None:
        base = event_hash(1, "k", "s", "{}", "t", GENESIS)
        for changed in (
            event_hash(2, "k", "s", "{}", "t", GENESIS), event_hash(1, "x", "s", "{}", "t", GENESIS),
            event_hash(1, "k", "x", "{}", "t", GENESIS), event_hash(1, "k", "s", "[]", "t", GENESIS),
            event_hash(1, "k", "s", "{}", "x", GENESIS), event_hash(1, "k", "s", "{}", "t", "1" * 64),
        ):
            assert changed != base


# ---------------------------------------------------------------------------
# 3 — tampering
# ---------------------------------------------------------------------------

class TestTampering:

    def test_altered_event(self, ledger, tmp_path) -> None:
        ids = _lifecycle(ledger, tmp_path)
        conn = _raw(ledger)
        conn.execute(
            "UPDATE events SET body=? WHERE subject=? AND kind='record.invalidated'",
            (json.dumps({"reason": "nothing to see"}), ids["withdrawn"]),
        )
        report = verify_ledger(ledger)
        assert not report.chain.ok
        assert any("was altered" in p for p in report.chain.problems)

    def test_deleted_event(self, ledger, tmp_path) -> None:
        ids = _lifecycle(ledger, tmp_path)
        _raw(ledger).execute(
            "DELETE FROM events WHERE subject=? AND kind='record.failed'", (ids["failed"],)
        )
        problems = verify_ledger(ledger).chain.problems
        assert any("is missing" in p for p in problems)
        assert any("does not follow" in p for p in problems)

    def test_reordered_events(self, ledger, tmp_path) -> None:
        _lifecycle(ledger, tmp_path)
        conn = _raw(ledger)
        a, b = conn.execute("SELECT seq, body FROM events WHERE seq IN (2, 3) ORDER BY seq").fetchall()
        conn.execute("UPDATE events SET body=? WHERE seq=?", (b[1], a[0]))
        conn.execute("UPDATE events SET body=? WHERE seq=?", (a[1], b[0]))
        assert not verify_ledger(ledger).chain.ok

    def test_record_flipped_back_by_hand(self, ledger, tmp_path) -> None:
        """The case triggers cannot stop: artifact_records rows legitimately change."""
        ids = _lifecycle(ledger, tmp_path)
        sqlite3.connect(ledger.db_path, isolation_level=None).execute(
            "UPDATE artifact_records SET state='sealed', error=NULL WHERE record_id=?",
            (ids["withdrawn"],),
        )
        report = verify_ledger(ledger)
        assert report.chain.ok and not report.ok
        assert report.problems == (
            f"record {ids['withdrawn']}: state is 'sealed', the chain says 'invalidated'",
        )

    def test_record_identity_edited(self, ledger, tmp_path) -> None:
        ids = _lifecycle(ledger, tmp_path)
        sqlite3.connect(ledger.db_path, isolation_level=None).execute(
            "UPDATE artifact_records SET parser_version='9.9', artifact_manifest='{}' "
            "WHERE record_id=?", (ids["sealed"],),
        )
        problems = verify_ledger(ledger).problems
        assert any("parser is" in p for p in problems)
        assert any("artifact_manifest no longer matches" in p for p in problems)

    def test_unlogged_record(self, ledger, tmp_path) -> None:
        ids = _lifecycle(ledger, tmp_path)
        conn = _raw(ledger)
        row = conn.execute("SELECT * FROM artifact_records WHERE record_id=?", (ids["sealed"],))
        cols = [d[0] for d in row.description]
        values = dict(zip(cols, row.fetchone()))
        values.update(record_id="forged", run_id="forged-run")
        conn.execute(
            f"INSERT INTO artifact_records ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            tuple(values.values()),
        )
        assert "record forged is in the ledger but was never logged" in verify_ledger(ledger).problems

    def test_event_body_that_is_not_json_is_reported(self, ledger, tmp_path) -> None:
        """A tampered body is a finding, never a crash of the verification."""
        _lifecycle(ledger, tmp_path)
        _raw(ledger).execute("UPDATE events SET body='not json' WHERE seq=1")
        report = verify_ledger(ledger)
        assert any("event 1 " in p and "was altered" in p for p in report.chain.problems)
        assert any(p.startswith("event 1 ") and "malformed body" in p for p in report.problems)

    def test_record_columns_that_are_not_json_are_reported(self, ledger, tmp_path) -> None:
        ids = _lifecycle(ledger, tmp_path)
        sqlite3.connect(ledger.db_path, isolation_level=None).execute(
            "UPDATE artifact_records SET parser_config='{' WHERE record_id=?", (ids["sealed"],),
        )
        sqlite3.connect(ledger.db_path, isolation_level=None).execute(
            "UPDATE artifact_records SET artifact_manifest='[1, 2]' WHERE record_id=?",
            (ids["withdrawn"],),
        )
        problems = verify_ledger(ledger).problems
        assert any(p.startswith(f"record {ids['sealed']} ") and "malformed" in p for p in problems)
        assert any(p.startswith(f"record {ids['withdrawn']} ") and "malformed" in p for p in problems)

    def test_delivery_and_replay_rows_changed(self, ledger, tmp_path) -> None:
        ids = _lifecycle(ledger, tmp_path)
        conn = _raw(ledger)
        conn.execute("DELETE FROM delivery_events WHERE event='removal_receipt'")
        conn.execute("UPDATE replays SET outcome='reproduced' WHERE replay_id=?", (ids["replay"],))
        problems = verify_ledger(ledger).problems
        assert any("its events differ from the chain" in p for p in problems)
        assert f"replay {ids['replay']} differs from the chain" in problems

    def test_truncation_is_caught_by_an_anchor(self, ledger, tmp_path) -> None:
        _lifecycle(ledger, tmp_path)
        anchor = EventLog(ledger).head()
        conn = _raw(ledger)
        conn.execute("DELETE FROM events WHERE seq > ?", (anchor[0] - 3,))
        conn.execute("DELETE FROM replays")  # keep the tables consistent with the cut
        assert EventLog(ledger).verify().ok  # a clean prefix on its own
        report = EventLog(ledger).verify(anchor=anchor)
        assert any("truncated" in p for p in report.problems)

    def test_rewritten_history_is_caught_by_an_anchor(self, ledger, tmp_path) -> None:
        record = ledger.seal(_pending(ledger, tmp_path).record_id)
        anchor = EventLog(ledger).head()
        conn = _raw(ledger)
        conn.execute("DELETE FROM events")
        # Rebuild a self-consistent chain with a different history.
        ledger2 = open_ledger(tmp_path / "ledger.db")
        from stele.ledger.events import append_event, write_transaction

        with write_transaction(ledger2._conn):
            append_event(ledger2._conn, "record.imported", record.record_id, {"forged": True})
            append_event(ledger2._conn, "record.imported", "x", {"forged": True})
        report = EventLog(ledger2).verify(anchor=anchor)
        assert report.problems == (f"event {anchor[0]} no longer has the anchored hash",)


# ---------------------------------------------------------------------------
# 4 — migration
# ---------------------------------------------------------------------------

def test_migration_logs_existing_state(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = open_ledger(db)
    ids = _lifecycle(ledger, tmp_path)
    ledger.close()

    conn = _raw(ledger)
    conn.execute("DROP TABLE events")
    conn.execute("ALTER TABLE delivery_events DROP COLUMN attempt_id")  # added in v8
    conn.execute("PRAGMA user_version = 6")  # v6: payload binding (#35), no event log yet
    conn.close()

    migrated = open_ledger(db)
    log = EventLog(migrated)
    kinds = [e.kind for e in log.events()]
    assert kinds[:3] == ["record.imported"] * 3
    assert kinds.count("delivery.imported") == 2
    assert kinds.count("delivery_event.imported") == 6
    assert kinds.count("replay.imported") == 1
    imported = {e.subject: e.body for e in log.events() if e.kind == "record.imported"}
    assert imported[ids["withdrawn"]]["state"] == "invalidated"
    assert imported[ids["failed"]]["error"] == "parser crashed"
    assert verify_ledger(migrated).ok

    # New facts chain on after the imported ones.
    migrated.invalidate(ids["sealed"], "later")
    assert verify_ledger(migrated).ok
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 8
    c.close()


# ---------------------------------------------------------------------------
# 5 — CLI
# ---------------------------------------------------------------------------

def _cli(ledger: LedgerStore, *extra: str) -> tuple[int, dict]:
    done = subprocess.run(
        [sys.executable, "-m", "stele.ledger.events", str(ledger.db_path),
         str(ledger.archive.root), *extra],
        capture_output=True, text=True,
    )
    return done.returncode, json.loads(done.stdout)


def test_cli_reports_head_and_problems(ledger, tmp_path) -> None:
    ids = _lifecycle(ledger, tmp_path)
    code, report = _cli(ledger)
    assert code == 0 and report["ok"] and report["problems"] == []
    seq, digest = EventLog(ledger).head()
    assert report["head"] == f"{seq}:{digest}"

    sqlite3.connect(ledger.db_path, isolation_level=None).execute(
        "UPDATE artifact_records SET state='sealed' WHERE record_id=?", (ids["withdrawn"],)
    )
    code, report = _cli(ledger, "--anchor", f"{seq}:{digest}")
    assert code == 1 and not report["ok"]
    assert any("the chain says 'invalidated'" in p for p in report["problems"])
