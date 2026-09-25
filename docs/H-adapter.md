# Phase H — Adapter Contract

**Status:** COMPLETE  
**Depends on:** Phase G  
**Unblocks:** RAG-ANYTHING — PROCEED (Stele-gated ingestion with scoped tombstone support)

---

## Goal

Define and enforce the typed interface that any downstream writer must
implement to write through Stele.  After this phase, no parser-facing component
may write to production via any other path.

---

## Adapter contract (`contracts/adapter.py`)

```python
class SteleAdapter(Protocol):
    def transform(self, record: ArtifactRecord) -> list[SteleChunk]: ...
    def on_invalidation(self, run_id: UUID, reason: str) -> None: ...
```

Key design decision vs. earlier stub: `transform()` is separated from writing.
The adapter reads a committed artifact and returns typed chunks.  It never
receives a DB handle, target connection, or writer reference.  All writes are
owned by the Dispatcher.

`SteleChunk` fields: `chunk_id`, `content`, `content_hash` (sha256 verified),
`token_count`, `metadata`.

`SteleTarget` union:
- `LightRAGTarget(workspace: str)`
- `HindsightTarget(instance: Literal["jarvis", "nexus", "crms"])`

---

## Dispatcher (`contracts/dispatcher.py`)

```
committed ArtifactRecord
      │
      ▼
Dispatcher.dispatch(adapter, record, target)
      │
      ├── adapter.transform(record)  →  list[SteleChunk]
      ├── validate_chunks(chunks)    →  ChunkValidationError if invalid
      ├── TargetWriter.write_chunks(chunks, target)
      └── DispatchResult(status, chunks_submitted, error)  →  dispatch_log
```

`DispatchResult` is stored in the Dispatcher's own log — separate from the
artifact ledger.  Join on `record_id` if a combined view is needed.

---

## Invariants proven (25 tests / all green)

| Proof | Test class |
|-------|-----------|
| Committed artifact passed to adapter | `TestAdapterReceivesCommittedRecord` |
| Adapter returns typed SteleChunk records | `TestAdapterReturnsTypedChunks` |
| Invalid adapter output rejected (empty, bad hash, dup ids) | `TestInvalidAdapterOutputRejected` |
| Adapter cannot bypass dispatcher write path | `TestAdapterCannotBypassDispatcher` |
| Target writes are represented as intents/results | `TestTargetWritesAsResults` |
| Adapter failure does not invalidate artifact automatically | `TestAdapterFailureIsolation` |
| Dispatcher records success/failure separately from ledger | `TestDispatchLog` |

---

## Full Stele test counts (E + F + G + H)

| Phase | Tests |
|-------|-------|
| E — Containment | 23 |
| F — Ledger | 27 |
| G — Replay / Invalidation | 24 |
| H — Adapter Contract | 25 |
| **Total** | **99** |

All 99 pass.

---

## Production wiring — COMPLETE 2026-06-26

| Component | Path | Status |
|-----------|------|--------|
| `RagAnythingSteleAdapter` | `EVECOR/DataCore/RAG/rag_anything_stele_adapter.py` | ✅ |
| `LightRAGTargetWriter` | `EVECOR/DataCore/RAG/lightrag_target_writer.py` | ✅ `_get_or_create_lightrag()` + `insert()` + `tombstone_by_run_id()` |
| `lightrag_tombstone` | `EVECOR/DataCore/RAG/lightrag_tombstone.py` | ✅ narrow `{run_id}:` prefix delete; separate JSONL audit |
| `lightrag_config` | `EVECOR/DataCore/RAG/lightrag_config.py` | ✅ shared LLM/embed factory |
| `stele_ingest` | `EVECOR/DataCore/RAG/stele_ingest.py` | ✅ full orchestration |
| `stele_rag_parser` | `EVECOR/DataCore/RAG/stele_rag_parser.py` | ✅ sandbox parser |
| `ingest-lightrag` | `nexus/bin/ingest-lightrag` | ✅ Stele-gated live path |

### Test counts

| Suite | Tests |
|-------|-------|
| Integration (`test_rag_anything_stele_integration.py`) | 19 |
| Production gate (`test_rag_anything_production_smoke.py`) | 6 |
| Tombstone (`test_lightrag_tombstone.py`) | 6 |
| **RAG × Stele total** | **31** |

**Caveat:** KG entity/relation enrichment requires valid `LITELLM_RAG_ANYTHING_KEY`.
Chunk insert and tombstone work without it; graph extraction returns 401 until provisioned.

### Production gate proofs

| Proof | Test class |
|-------|-----------|
| LightRAG `insert()` via writer | `TestLiveLightRAGInsert` |
| dispatch_log records result | `TestDispatchLogRecordsResult` |
| Ledger unchanged after dispatch | `TestLedgerUnchangedAfterDispatch` |
| Replay re-selects artifact | `TestReplayReselectsArtifact` |
| Invalidation tombstones matching chunks | `TestInvalidationTombstonesMatchingChunks` |
| Tombstone proofs (6) | `test_lightrag_tombstone.py` |
