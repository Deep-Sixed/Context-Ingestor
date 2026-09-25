# Phase H — Adapter Contract

**Status:** COMPLETE  
**Depends on:** Phase G  

---

## Goal

Define and enforce the typed interface that any downstream writer must
implement to write through Stele.  After this phase, no parser-facing component
may write to a target store via any other path.

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

> **Trust note:** adapters run in the host process as trusted code. "Must not
> write to any external store" is a contract, not an enforced capability
> boundary — Python cannot stop an adapter from opening its own connections
> (see `test_phase_h_adapter_contract.py`, side-channel test). Only parser
> execution is sandboxed. The Dispatcher also does not yet check the record's
> ledger state or call `on_invalidation()`; both are tracked for the
> Snapshot/Extraction redesign.

`SteleChunk` fields: `chunk_id`, `content`, `content_hash` (sha256 verified),
`token_count`, `metadata`.

`SteleTarget` union:
- `LightRAGTarget(workspace: str)`
- `HindsightTarget(instance: str)`

These are example target types; register a `TargetWriter` for each target
type you use.

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

## Invariants proven

| Proof | Test class |
|-------|-----------|
| Committed artifact passed to adapter | `TestAdapterReceivesCommittedRecord` |
| Adapter returns typed SteleChunk records | `TestAdapterReturnsTypedChunks` |
| Invalid adapter output rejected (empty, bad hash, dup ids) | `TestInvalidAdapterOutputRejected` |
| Adapter cannot bypass dispatcher write path | `TestAdapterCannotBypassDispatcher` |
| Target writes are represented as intents/results | `TestTargetWritesAsResults` |
| Adapter failure does not invalidate artifact automatically | `TestAdapterFailureIsolation` |
| Dispatcher records success/failure separately from ledger | `TestDispatchLog` |
