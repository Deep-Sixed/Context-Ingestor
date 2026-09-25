"""
Adapter contract proof tests.

Seven required proofs:

  PASS 1 — Sealed artifact can be passed to adapter
  PASS 2 — Adapter returns typed SteleChunk records
  PASS 3 — Invalid adapter output is rejected
  PASS 4 — Adapter cannot bypass dispatcher write path
  PASS 5 — Target writes are represented as intents/results
  PASS 6 — Adapter failure does not invalidate source artifact automatically
  PASS 7 — Dispatcher records target write success/failure separately

Run:
    uv run pytest tests/test_adapter_contract.py -v
"""
from __future__ import annotations

import hashlib
import inspect
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from stele.contracts.adapter import (
    ChunkValidationError,
    LightRAGTarget,
    SealedBundle,
    SteleAdapter,
    SteleChunk,
    SteleTarget,
    make_chunk,
    validate_chunks,
)
from stele.contracts.dispatcher import (
    Dispatcher,
)
from stele.ledger.delivery import DeliveryStatus
from stele.ledger.models import ArtifactRecord, ArtifactState
from stele.ledger.store import LedgerStore
from tests.ledger_helpers import PROVENANCE, open_ledger


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeAdapter:
    """Minimal adapter that reads artifact JSON and returns one chunk per file."""

    def __init__(self, chunks: list[SteleChunk] | None = None) -> None:
        self._chunks = chunks  # if set, return these; otherwise derive from record

    def transform(self, bundle: SealedBundle) -> list[SteleChunk]:
        if self._chunks is not None:
            return self._chunks
        result = []
        for rel_path in bundle.paths():
            result.append(make_chunk(
                chunk_id=f"chunk:{rel_path}",
                content=bundle.read_text(rel_path),
                source_record_id=bundle.record_id,
            ))
        return result


@dataclass
class CapturingWriter:
    """Target writer stub that captures all calls for assertion."""
    received_chunks: list[SteleChunk] = None
    received_target: SteleTarget = None
    should_fail: bool = False

    def __post_init__(self) -> None:
        if self.received_chunks is None:
            self.received_chunks = []

    def write_chunks(
        self, chunks: list[SteleChunk], target: SteleTarget, *, dispatch_id: str
    ) -> None:
        if self.should_fail:
            raise RuntimeError("simulated target write failure")
        self.received_chunks.extend(chunks)
        self.received_target = target

    def remove_delivery(self, target: SteleTarget, *, dispatch_id: str) -> int:
        return 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> LedgerStore:
    return open_ledger(tmp_path / "ledger.db")


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


def _sealed_record(
    store: LedgerStore, artifact_dir: Path, content: str = '{"chunks": ["hello world"]}'
) -> ArtifactRecord:
    p = artifact_dir / f"out_{uuid.uuid4().hex[:8]}.json"
    p.write_text(content)
    record = store.create_pending(
        **PROVENANCE,
        run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
    )
    store.seal(record.record_id)
    return store.get(record.record_id)


def _dispatcher_with_writer(store: LedgerStore) -> tuple[Dispatcher, CapturingWriter]:
    dispatcher = Dispatcher(store)
    writer = CapturingWriter()
    dispatcher.register_target(LightRAGTarget, writer)
    return dispatcher, writer


# ---------------------------------------------------------------------------
# PASS 1 — Sealed artifact can be passed to adapter
# ---------------------------------------------------------------------------

class TestAdapterReceivesSealedRecord:

    def test_adapter_called_with_sealed_record(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        assert record.state is ArtifactState.SEALED

        dispatcher, _ = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("default"))

        assert result.status == "success"
        assert result.record_id == record.record_id

    def test_dispatch_result_links_to_record(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher, _ = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("test"))

        assert result.record_id == record.record_id
        assert result.dispatch_id != record.record_id  # separate identities


# ---------------------------------------------------------------------------
# PASS 2 — Adapter returns typed SteleChunk records
# ---------------------------------------------------------------------------

class TestAdapterReturnsTypedChunks:

    def test_chunks_are_stele_chunk_instances(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher, writer = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("default"))

        assert result.status == "success"
        assert result.chunks_submitted > 0
        assert all(isinstance(c, SteleChunk) for c in writer.received_chunks)

    def test_chunk_content_hash_matches_content(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher, writer = _dispatcher_with_writer(store)
        dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("default"))

        for chunk in writer.received_chunks:
            expected = hashlib.sha256(chunk.content.encode()).hexdigest()
            assert chunk.content_hash == expected, (
                f"chunk {chunk.chunk_id!r}: content_hash does not match content"
            )

    def test_chunk_metadata_preserved(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher, writer = _dispatcher_with_writer(store)
        dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("default"))

        for chunk in writer.received_chunks:
            assert "source_record_id" in chunk.metadata
            assert chunk.metadata["source_record_id"] == record.record_id


# ---------------------------------------------------------------------------
# PASS 3 — Invalid adapter output is rejected
# ---------------------------------------------------------------------------

class TestInvalidAdapterOutputRejected:

    def test_empty_chunk_list_rejected(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        adapter = FakeAdapter(chunks=[])
        dispatcher, writer = _dispatcher_with_writer(store)

        result = dispatcher.dispatch(adapter, record, LightRAGTarget("default"))
        assert result.status == "failed"
        assert "no chunks" in (result.error or "").lower()
        assert len(writer.received_chunks) == 0  # target never called

    def test_wrong_content_hash_rejected(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        bad_chunk = SteleChunk(
            chunk_id="c1",
            content="real content",
            content_hash="0" * 64,  # wrong hash
            token_count=2,
        )
        adapter = FakeAdapter(chunks=[bad_chunk])
        dispatcher, writer = _dispatcher_with_writer(store)

        result = dispatcher.dispatch(adapter, record, LightRAGTarget("default"))
        assert result.status == "failed"
        assert "mismatch" in (result.error or "").lower()
        assert len(writer.received_chunks) == 0

    def test_duplicate_chunk_ids_rejected(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        chunks = [make_chunk("same-id", "content A"), make_chunk("same-id", "content B")]
        adapter = FakeAdapter(chunks=chunks)
        dispatcher, writer = _dispatcher_with_writer(store)

        result = dispatcher.dispatch(adapter, record, LightRAGTarget("default"))
        assert result.status == "failed"
        assert "duplicate" in (result.error or "").lower()

    def test_validate_chunks_raises_on_empty(self) -> None:
        with pytest.raises(ChunkValidationError, match="no chunks"):
            validate_chunks([])

    def test_validate_chunks_raises_on_hash_mismatch(self) -> None:
        chunk = SteleChunk(
            chunk_id="c1", content="hello", content_hash="badhash", token_count=1
        )
        with pytest.raises(ChunkValidationError, match="mismatch"):
            validate_chunks([chunk])


# ---------------------------------------------------------------------------
# PASS 4 — Adapter cannot bypass dispatcher write path
# ---------------------------------------------------------------------------

class TestAdapterCannotBypassDispatcher:

    def test_adapter_protocol_has_no_write_methods(self) -> None:
        """The SteleAdapter Protocol exposes no method that accepts a target or DB handle."""
        members = {
            name: obj
            for name, obj in inspect.getmembers(SteleAdapter)
            if not name.startswith("_")
        }
        # Only transform — no write_chunks, no removal hook, no db, no target
        assert "transform" in members
        forbidden = {
            "write_chunks", "write", "db", "connection", "conn", "target",
            "on_invalidation", "remove_delivery",
        }
        assert not (set(members) & forbidden), (
            f"SteleAdapter Protocol has unexpected write-path members: "
            f"{set(members) & forbidden}"
        )

    def test_adapter_transform_signature_has_no_target_param(self) -> None:
        """transform() must not accept a target or writer parameter."""
        sig = inspect.signature(FakeAdapter.transform)
        param_names = set(sig.parameters) - {"self"}
        # Only the sealed bundle is allowed
        assert param_names == {"bundle"}, (
            f"transform() must accept only 'bundle', got: {param_names}"
        )

    def test_side_channel_write_produces_no_dispatch_result(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        """An adapter that writes to a side-channel list does not produce a DispatchResult."""
        side_channel: list[str] = []

        class SideChannelAdapter:
            def transform(self, bundle: SealedBundle) -> list[SteleChunk]:
                # Simulate direct write attempt via side channel
                side_channel.append(f"direct_write:{bundle.record_id}")
                return [make_chunk("c1", "content")]

        record = _sealed_record(store, artifact_dir)
        dispatcher, writer = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(SideChannelAdapter(), record, LightRAGTarget("default"))

        # Side channel was written (can't prevent in Python), but:
        assert len(side_channel) == 1                        # adapter ran
        assert result.status == "success"                    # dispatcher succeeded
        assert len(writer.received_chunks) == 1              # Stele write went through dispatcher
        # The side-channel write is NOT tracked in the dispatch log
        assert all(
            r.record_id == record.record_id for r in dispatcher.dispatch_log()
        )

    def test_unregistered_target_fails_dispatch(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        """Dispatching to an unregistered target fails without calling any writer."""
        from stele.contracts.adapter import HindsightTarget

        record = _sealed_record(store, artifact_dir)
        dispatcher = Dispatcher(store)  # no writers registered
        result = dispatcher.dispatch(
            FakeAdapter(), record, HindsightTarget("default")
        )
        assert result.status == "failed"
        assert "no writer" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# PASS 5 — Target writes are represented as intents/results
# ---------------------------------------------------------------------------

class TestTargetWritesAsResults:

    def test_successful_dispatch_records_chunk_count(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher, writer = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("ws1"))

        assert result.status == "success"
        assert result.chunks_submitted == len(writer.received_chunks)
        assert result.chunks_submitted > 0

    def test_target_receives_correct_workspace(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher, writer = _dispatcher_with_writer(store)
        target = LightRAGTarget(workspace="my-workspace")
        dispatcher.dispatch(FakeAdapter(), record, target)

        assert isinstance(writer.received_target, LightRAGTarget)
        assert writer.received_target.workspace == "my-workspace"

    def test_failed_dispatch_has_zero_chunks_submitted(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        adapter = FakeAdapter(chunks=[])  # will fail validation
        dispatcher, _ = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(adapter, record, LightRAGTarget("default"))

        assert result.status == "failed"
        assert result.chunks_submitted == 0


# ---------------------------------------------------------------------------
# PASS 6 — Adapter failure does not invalidate source artifact automatically
# ---------------------------------------------------------------------------

class TestAdapterFailureIsolation:

    def test_adapter_transform_exception_leaves_record_sealed(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        class CrashingAdapter:
            def transform(self, bundle: SealedBundle) -> list[SteleChunk]:
                raise RuntimeError("parse error inside adapter")

        record = _sealed_record(store, artifact_dir)
        dispatcher, _ = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(CrashingAdapter(), record, LightRAGTarget("default"))

        assert result.status == "failed"
        # Artifact record is unmodified in the ledger
        refetched = store.get(record.record_id)
        assert refetched.state is ArtifactState.SEALED, (
            "adapter failure must not automatically invalidate the artifact record"
        )

    def test_target_write_failure_leaves_record_sealed(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher = Dispatcher(store)
        failing_writer = CapturingWriter(should_fail=True)
        dispatcher.register_target(LightRAGTarget, failing_writer)

        result = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("default"))

        assert result.status == "failed"
        assert "simulated target write failure" in (result.error or "")
        refetched = store.get(record.record_id)
        assert refetched.state is ArtifactState.SEALED

    def test_caller_must_explicitly_invalidate_after_failure(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        """Invalidation after dispatch failure is the caller's decision, not automatic."""
        from stele.replay.invalidation import InvalidationReason, invalidate_record

        record = _sealed_record(store, artifact_dir)
        dispatcher = Dispatcher(store)
        dispatcher.register_target(LightRAGTarget, CapturingWriter(should_fail=True))
        result = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("default"))

        assert result.status == "failed"
        assert store.get(record.record_id).state is ArtifactState.SEALED

        # Caller explicitly decides to invalidate
        invalidated = invalidate_record(
            store, record.record_id, InvalidationReason.DATA_QUALITY,
            note="adapter failed"
        )
        assert invalidated.state is ArtifactState.INVALIDATED


# ---------------------------------------------------------------------------
# PASS 7 — Dispatcher records target write success/failure separately
# ---------------------------------------------------------------------------

class TestDispatchLog:

    def test_successful_dispatch_in_log(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher, _ = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("default"))

        log = dispatcher.dispatch_log()
        assert len(log) == 1
        assert log[0].dispatch_id == result.dispatch_id
        assert log[0].status is DeliveryStatus.DELIVERED

    def test_failed_dispatch_in_log(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        adapter = FakeAdapter(chunks=[])  # fails validation
        dispatcher, _ = _dispatcher_with_writer(store)
        dispatcher.dispatch(adapter, record, LightRAGTarget("default"))

        log = dispatcher.dispatch_log()
        assert len(log) == 1
        assert log[0].status is DeliveryStatus.FAILED

    def test_dispatch_log_is_separate_from_ledger(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        """DispatchResult.dispatch_id must differ from the artifact record_id."""
        record = _sealed_record(store, artifact_dir)
        dispatcher, _ = _dispatcher_with_writer(store)
        result = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("default"))

        assert result.dispatch_id != record.record_id
        assert result.record_id == record.record_id

    def test_multiple_dispatches_for_same_record_all_logged(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record = _sealed_record(store, artifact_dir)
        dispatcher, _ = _dispatcher_with_writer(store)

        r1 = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("ws-a"))
        r2 = dispatcher.dispatch(FakeAdapter(), record, LightRAGTarget("ws-b"))

        log = dispatcher.dispatch_log_for(record.record_id)
        assert len(log) == 2
        dispatch_ids = {r.dispatch_id for r in log}
        assert r1.dispatch_id in dispatch_ids
        assert r2.dispatch_id in dispatch_ids

    def test_dispatch_log_separate_per_record(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        dir2 = tmp_path / "art2"
        dir2.mkdir()
        r1 = _sealed_record(store, artifact_dir, '{"a": 1}')
        r2 = _sealed_record(store, dir2, '{"b": 2}')

        dispatcher, _ = _dispatcher_with_writer(store)
        dispatcher.dispatch(FakeAdapter(), r1, LightRAGTarget("ws"))
        dispatcher.dispatch(FakeAdapter(), r2, LightRAGTarget("ws"))

        assert len(dispatcher.dispatch_log_for(r1.record_id)) == 1
        assert len(dispatcher.dispatch_log_for(r2.record_id)) == 1
        assert len(dispatcher.dispatch_log()) == 2
