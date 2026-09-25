"""
Durable dispatch and invalidation proofs (roadmap #13).

  1. Dispatch refuses PENDING, FAILED and INVALIDATED records, checked live.
  2. Changing or corrupting stored artifact bytes after sealing makes
     dispatch refuse; adapters read verified bytes, never live files.
  3. Crash injection after the intent, mid-write and before the receipt
     leaves a log that explains what the target received, and a retry with
     the same dispatch_id does not duplicate data.
  4. Invalidating a delivered record removes its target data and records a
     receipt.
"""
from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

import stele.ledger.delivery as delivery_module
from stele.archive import IntegrityError
from stele.contracts.adapter import (
    HindsightTarget,
    LightRAGTarget,
    SealedBundle,
    SteleChunk,
    make_chunk,
)
from stele.contracts.dispatcher import (
    DispatchRefusedError,
    Dispatcher,
    PartialWriteError,
)
from stele.ledger.delivery import DeliveryStatus
from stele.ledger.models import ArtifactRecord, ArtifactState
from stele.ledger.store import SCHEMA_VERSION, LedgerStore
from stele.replay.invalidation import InvalidationReason, invalidate_record
from tests.ledger_helpers import PROVENANCE, open_ledger


class Crash(BaseException):
    """Stands in for the process dying: nothing after it runs."""


class LinesAdapter:
    """One chunk per line of every artifact, read from the sealed bundle."""

    def transform(self, bundle: SealedBundle) -> list[SteleChunk]:
        return [
            make_chunk(f"{path}:{i}", line)
            for path in bundle.paths()
            for i, line in enumerate(bundle.read_text(path).splitlines())
        ]


class MemoryTarget:
    """An idempotent store: rows are keyed by (dispatch_id, chunk_id)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}
        self.write_calls = 0
        self.crash_after: int | None = None       # chunks written before a Crash
        self.fail_after: int | None = None        # chunks written before a PartialWriteError
        self.fail_removal = False

    def write_chunks(self, chunks, target, *, dispatch_id: str) -> None:
        self.write_calls += 1
        for i, chunk in enumerate(chunks):
            if self.crash_after == i:
                raise Crash()
            if self.fail_after == i:
                raise PartialWriteError("target went away", chunks_written=i)
            self.rows[(dispatch_id, chunk.chunk_id)] = chunk.content

    def remove_delivery(self, target, *, dispatch_id: str) -> int:
        if self.fail_removal:
            raise RuntimeError("target unavailable for removal")
        keys = [k for k in self.rows if k[0] == dispatch_id]
        for k in keys:
            del self.rows[k]
        return len(keys)


@pytest.fixture
def ledger(tmp_path: Path) -> LedgerStore:
    return open_ledger(tmp_path / "ledger.db")


@pytest.fixture
def target() -> MemoryTarget:
    return MemoryTarget()


def _dispatcher(ledger: LedgerStore, target: MemoryTarget) -> Dispatcher:
    dispatcher = Dispatcher(ledger)
    dispatcher.register_target(LightRAGTarget, target)
    dispatcher.register_target(HindsightTarget, target)
    return dispatcher


def _pending(ledger: LedgerStore, tmp_path: Path, text: str = "alpha\nbeta\n") -> ArtifactRecord:
    out = tmp_path / f"out-{uuid.uuid4().hex[:8]}"
    out.mkdir()
    (out / "doc.txt").write_text(text)
    return ledger.create_pending(
        **PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=out,
        artifact_paths=[out / "doc.txt"],
    )


def _sealed(ledger: LedgerStore, tmp_path: Path, text: str = "alpha\nbeta\n") -> ArtifactRecord:
    return ledger.seal(_pending(ledger, tmp_path, text).record_id)


WS = LightRAGTarget("ws")


# ---------------------------------------------------------------------------
# 1 — Only sealed records, checked against the live ledger
# ---------------------------------------------------------------------------

class TestOnlySealedRecords:

    def test_pending_is_refused(self, ledger, target, tmp_path) -> None:
        record = _pending(ledger, tmp_path)
        with pytest.raises(DispatchRefusedError, match="pending"):
            _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS)
        assert target.write_calls == 0

    def test_failed_is_refused(self, ledger, target, tmp_path) -> None:
        record = ledger.fail(_pending(ledger, tmp_path).record_id, "parser crashed")
        with pytest.raises(DispatchRefusedError, match="failed"):
            _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS)

    def test_invalidated_is_refused(self, ledger, target, tmp_path) -> None:
        record = ledger.invalidate(_sealed(ledger, tmp_path).record_id, "stale")
        with pytest.raises(DispatchRefusedError, match="invalidated"):
            _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS)

    def test_a_stale_sealed_copy_is_refused(self, ledger, target, tmp_path) -> None:
        stale = _sealed(ledger, tmp_path)
        ledger.invalidate(stale.record_id, "superseded")
        assert stale.state is ArtifactState.SEALED  # the caller's in-memory copy

        dispatcher = _dispatcher(ledger, target)
        with pytest.raises(DispatchRefusedError, match="invalidated"):
            dispatcher.dispatch(LinesAdapter(), stale, WS)
        assert dispatcher.dispatch_log() == []
        assert target.rows == {}

    def test_sealed_is_delivered_by_id_or_record(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        dispatcher = _dispatcher(ledger, target)
        by_record = dispatcher.dispatch(LinesAdapter(), record, WS)
        by_id = dispatcher.dispatch(LinesAdapter(), record.record_id, LightRAGTarget("ws2"))
        assert by_record.status == by_id.status == "success"
        assert by_record.chunks_written == 2


# ---------------------------------------------------------------------------
# 2 — Verified bytes only
# ---------------------------------------------------------------------------

class TestVerifiedBytes:

    def _blob(self, ledger: LedgerStore, digest: str) -> Path:
        path = ledger.archive.root / "blobs" / digest[:2] / digest[2:]
        path.chmod(0o600)
        return path

    def test_corrupted_artifact_bytes_are_refused(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        self._blob(ledger, record.artifact_manifest["doc.txt"]).write_bytes(b"alpha\nEVIL\n")

        dispatcher = _dispatcher(ledger, target)
        with pytest.raises(DispatchRefusedError) as info:
            dispatcher.dispatch(LinesAdapter(), record, WS)
        assert isinstance(info.value.__cause__, IntegrityError)
        assert target.rows == {} and dispatcher.dispatch_log() == []

    def test_corrupted_tree_is_refused(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        self._blob(ledger, record.artifact_hash).write_bytes(b"not the tree")
        with pytest.raises(DispatchRefusedError):
            _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS)

    def test_missing_artifact_blob_is_refused(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        self._blob(ledger, record.artifact_manifest["doc.txt"]).unlink()
        with pytest.raises(DispatchRefusedError):
            _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS)

    def test_adapter_reads_sealed_bytes_not_live_files(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path, "sealed line\n")
        (Path(record.artifact_dir) / "doc.txt").write_text("edited after sealing\n")

        _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS)
        assert list(target.rows.values()) == ["sealed line"]

        shutil.rmtree(record.artifact_dir)
        result = _dispatcher(ledger, target).dispatch(
            LinesAdapter(), record, LightRAGTarget("other")
        )
        assert result.status == "success"

    def test_bundle_has_no_host_paths(self, ledger, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        bundle = SealedBundle.from_record(record, ledger.archive)
        assert not hasattr(bundle, "artifact_dir")
        assert record.artifact_dir not in repr(bundle)
        with pytest.raises(TypeError):
            bundle.manifest["doc.txt"] = "0" * 64  # read-only view
        with pytest.raises(KeyError):
            bundle.read("../ledger.db")


# ---------------------------------------------------------------------------
# 3 — Durable, idempotent delivery under crashes
# ---------------------------------------------------------------------------

class TestCrashRecovery:

    def _crash(self, ledger, target, tmp_path, monkeypatch, where: str):
        record = _sealed(ledger, tmp_path)
        if where == "after_intent":
            target.crash_after = 0
        elif where == "mid_write":
            target.crash_after = 1
        else:  # before_receipt
            real_append = delivery_module.DeliveryLog.append

            def append(self, dispatch_id, event, **kwargs):
                if event == "receipt":
                    raise Crash()
                return real_append(self, dispatch_id, event, **kwargs)

            monkeypatch.setattr(delivery_module.DeliveryLog, "append", append)

        with pytest.raises(Crash):
            _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS)
        monkeypatch.undo()
        target.crash_after = None
        return record

    @pytest.mark.parametrize(
        "where, rows_at_target",
        [("after_intent", 0), ("mid_write", 1), ("before_receipt", 2)],
    )
    def test_log_explains_target_and_retry_does_not_duplicate(
        self, tmp_path, target, monkeypatch, where, rows_at_target
    ) -> None:
        db = tmp_path / "ledger.db"
        record = self._crash(open_ledger(db), target, tmp_path, monkeypatch, where)
        assert len(target.rows) == rows_at_target

        # A fresh process reads the log.
        ledger = open_ledger(db)
        dispatcher = _dispatcher(ledger, target)
        [delivery] = dispatcher.dispatch_log_for(record.record_id)
        assert delivery.status is DeliveryStatus.IN_FLIGHT
        assert delivery.planned == 2
        assert delivery.possibly_written
        assert "between 0 and 2 chunks" in delivery.explain()
        assert delivery.dispatch_id in delivery.explain()
        assert {k[0] for k in target.rows} <= {delivery.dispatch_id}

        result = dispatcher.dispatch(LinesAdapter(), record, WS)
        assert result.status == "success"
        assert result.dispatch_id == delivery.dispatch_id
        assert len(target.rows) == 2  # no duplicates
        assert {k[0] for k in target.rows} == {delivery.dispatch_id}

        [delivery] = dispatcher.dispatch_log_for(record.record_id)
        assert delivery.status is DeliveryStatus.DELIVERED
        assert [e.event for e in delivery.events] == ["intent", "intent", "receipt"]

    def test_delivered_delivery_is_not_written_again(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        dispatcher = _dispatcher(ledger, target)
        first = dispatcher.dispatch(LinesAdapter(), record, WS)
        again = dispatcher.dispatch(LinesAdapter(), record, WS)

        assert target.write_calls == 1
        assert again.already_delivered and again.status == "success"
        assert again.dispatch_id == first.dispatch_id

    def test_partial_write_count_is_recorded(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        target.fail_after = 1
        dispatcher = _dispatcher(ledger, target)
        result = dispatcher.dispatch(LinesAdapter(), record, WS)

        assert result.status == "failed"
        assert (result.chunks_submitted, result.chunks_written) == (2, 1)
        [delivery] = dispatcher.dispatch_log_for(record.record_id)
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.events[-1].done == 1 and delivery.possibly_written

        target.fail_after = None
        assert dispatcher.dispatch(LinesAdapter(), record, WS).status == "success"
        assert len(target.rows) == 2

    def test_unknown_write_count_is_not_recorded_as_zero(self, ledger, tmp_path) -> None:
        class Opaque(MemoryTarget):
            def write_chunks(self, chunks, target, *, dispatch_id):
                raise RuntimeError("connection reset")

        record = _sealed(ledger, tmp_path)
        dispatcher = _dispatcher(ledger, Opaque())
        result = dispatcher.dispatch(LinesAdapter(), record, WS)
        assert result.chunks_written is None
        assert dispatcher.dispatch_log_for(record.record_id)[0].possibly_written

    def test_adapter_failure_writes_nothing(self, ledger, target, tmp_path) -> None:
        class Broken:
            def transform(self, bundle):
                raise ValueError("cannot parse")

        record = _sealed(ledger, tmp_path)
        dispatcher = _dispatcher(ledger, target)
        result = dispatcher.dispatch(Broken(), record, WS)
        assert (result.status, result.chunks_submitted, result.chunks_written) == ("failed", 0, 0)
        [delivery] = dispatcher.dispatch_log_for(record.record_id)
        assert not delivery.possibly_written
        assert ledger.get(record.record_id).state is ArtifactState.SEALED

    def test_retry_with_different_chunks_is_refused(self, ledger, target, tmp_path, monkeypatch) -> None:
        record = self._crash(ledger, target, tmp_path, monkeypatch, "mid_write")

        class Different:
            def transform(self, bundle):
                return [make_chunk("x", "not what the intent announced")]

        dispatcher = _dispatcher(ledger, target)
        result = dispatcher.dispatch(Different(), record, WS)
        assert result.status == "failed" and "deterministic" in result.error
        assert len(target.rows) == 1  # nothing new written

    def test_the_log_is_append_only(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS)
        conn = sqlite3.connect(ledger.db_path)
        for statement in (
            "UPDATE delivery_events SET done=0",
            "DELETE FROM delivery_events",
            "DELETE FROM deliveries",
            "UPDATE deliveries SET target='{}'",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(statement)
        conn.close()


# ---------------------------------------------------------------------------
# 4 — Invalidation removes delivered data, with receipts
# ---------------------------------------------------------------------------

class TestInvalidationRemovesDeliveries:

    def test_invalidate_removes_every_delivery(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        keep = _sealed(ledger, tmp_path, "unrelated\n")
        dispatcher = _dispatcher(ledger, target)
        dispatcher.dispatch(LinesAdapter(), record, WS)
        dispatcher.dispatch(LinesAdapter(), record, HindsightTarget("default"))
        dispatcher.dispatch(LinesAdapter(), keep, WS)
        assert len(target.rows) == 5

        removals = dispatcher.invalidate(record.record_id, "source_changed")

        assert ledger.get(record.record_id).state is ArtifactState.INVALIDATED
        assert [(r.status, r.chunks_removed) for r in removals] == [("removed", 2)] * 2
        assert list(target.rows.values()) == ["unrelated"]
        for delivery in dispatcher.dispatch_log_for(record.record_id):
            assert delivery.status is DeliveryStatus.REMOVED
            assert [e.event for e in delivery.events][-2:] == ["removal_intent", "removal_receipt"]
            assert delivery.events[-1].done == 2
        assert dispatcher.dispatch_log_for(keep.record_id)[0].status is DeliveryStatus.DELIVERED

    def test_deliveries_that_wrote_nothing_are_skipped(self, ledger, target, tmp_path) -> None:
        class Broken:
            def transform(self, bundle):
                raise ValueError("cannot parse")

        record = _sealed(ledger, tmp_path)
        dispatcher = _dispatcher(ledger, target)
        dispatcher.dispatch(Broken(), record, WS)
        assert dispatcher.invalidate(record.record_id, "bad") == []

    def test_crashed_delivery_is_removed_too(self, ledger, target, tmp_path, monkeypatch) -> None:
        record = TestCrashRecovery()._crash(ledger, target, tmp_path, monkeypatch, "mid_write")
        removals = _dispatcher(ledger, target).invalidate(record.record_id, "stale")
        assert [r.chunks_removed for r in removals] == [1]
        assert target.rows == {}

    def test_failed_removal_is_logged_and_retried(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        dispatcher = _dispatcher(ledger, target)
        dispatcher.dispatch(LinesAdapter(), record, WS)

        target.fail_removal = True
        [failed] = dispatcher.invalidate(record.record_id, "stale")
        assert failed.status == "failed"
        [delivery] = dispatcher.dispatch_log_for(record.record_id)
        assert delivery.status is DeliveryStatus.REMOVAL_FAILED
        assert len(target.rows) == 2

        target.fail_removal = False
        [removed] = dispatcher.invalidate(record.record_id, "stale")  # idempotent retry
        assert removed.status == "removed" and target.rows == {}
        assert dispatcher.retract(record.record_id) == []  # nothing left to do

    def test_ledger_only_invalidation_is_caught_up(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        dispatcher = _dispatcher(ledger, target)
        dispatcher.dispatch(LinesAdapter(), record, WS)

        invalidate_record(ledger, record.record_id, InvalidationReason.SOURCE_CHANGED)
        assert len(target.rows) == 2  # the ledger alone cannot reach the target

        [removed] = dispatcher.retract_invalidated()
        assert removed.status == "removed" and target.rows == {}

    def test_invalidation_racing_a_write_still_removes_it(self, tmp_path, target) -> None:
        db = tmp_path / "ledger.db"
        ledger, other = open_ledger(db), open_ledger(db)
        record = _sealed(ledger, tmp_path)
        other_dispatcher = _dispatcher(other, target)

        class RacingTarget(MemoryTarget):
            def write_chunks(self, chunks, t, *, dispatch_id):
                # Another process invalidates while this write is in flight:
                # its removal pass runs before the data has landed.
                other_dispatcher.invalidate(record.record_id, "superseded")
                target.write_chunks(chunks, t, dispatch_id=dispatch_id)

            def remove_delivery(self, t, *, dispatch_id):
                return target.remove_delivery(t, dispatch_id=dispatch_id)

        dispatcher = Dispatcher(ledger)
        dispatcher.register_target(LightRAGTarget, RacingTarget())
        dispatcher.dispatch(LinesAdapter(), record, WS)

        assert target.rows == {}
        [delivery] = dispatcher.dispatch_log_for(record.record_id)
        assert delivery.status is DeliveryStatus.REMOVED
        assert [e.event for e in delivery.events] == [
            "intent", "removal_intent", "removal_receipt",  # the racing invalidation
            "receipt",                                      # the write landed after it
            "removal_intent", "removal_receipt",            # so it was removed again
        ]
        assert not delivery.possibly_written

    def test_retract_needs_an_invalidated_record(self, ledger, target, tmp_path) -> None:
        record = _sealed(ledger, tmp_path)
        with pytest.raises(ValueError, match="only invalidated"):
            _dispatcher(ledger, target).retract(record.record_id)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_version_2_ledger_gains_the_delivery_log(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = open_ledger(db)
    record = _sealed(ledger, tmp_path)
    ledger.close()

    conn = sqlite3.connect(db)
    for name in (
        "deliveries_append_only_u", "deliveries_append_only_d",
        "delivery_events_append_only_u", "delivery_events_append_only_d",
        "replays_append_only_u", "replays_append_only_d",
        "events_append_only_u", "events_append_only_d",
    ):
        conn.execute(f"DROP TRIGGER {name}")
    conn.execute("DROP TABLE events")
    conn.execute("DROP TABLE replays")  # a v2 ledger has none of these logs
    conn.execute("DROP TABLE delivery_events")
    conn.execute("DROP TABLE deliveries")
    conn.execute("ALTER TABLE artifact_records DROP COLUMN run_conditions")  # added in v5
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    ledger = open_ledger(db)
    assert ledger.get(record.record_id).state is ArtifactState.SEALED
    target = MemoryTarget()
    assert _dispatcher(ledger, target).dispatch(LinesAdapter(), record, WS).status == "success"
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()
