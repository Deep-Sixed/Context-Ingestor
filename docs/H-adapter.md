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
    def transform(self, bundle: SealedBundle) -> list[SteleChunk]: ...
```

`transform()` is separated from writing. The adapter reads a sealed bundle and
returns typed chunks. It never receives a file path, DB handle, target
connection or writer reference. All writes, and their removal, are owned by
the Dispatcher.

**Adapters read verified bytes, not files.** A `SealedBundle` carries the
record's provenance (`record_id`, `run_id`, `artifact_hash`, `manifest`,
`parser`, `parser_config`, `source_hash`, `source_path`) and serves each
artifact by digest from the evidence archive: `bundle.read(path)` re-hashes
the bytes on every read, so a file edited after sealing, or a corrupted blob,
can never reach an adapter. There is no `artifact_dir`.

**Adapters must be deterministic.** A retried delivery re-runs `transform()`
and must get the same chunks; the Dispatcher refuses a retry whose chunks
differ from the ones its earlier attempt announced.

**Adapters are trusted code.** They run in the host process. "Must not write
to any external store" and "must be deterministic" are a contract, not an
enforced capability boundary: Python cannot stop an adapter from opening its
own connections (see the side-channel test in
`test_phase_h_adapter_contract.py`). Only parser execution is sandboxed. An
adapter is reviewed and deployed like any other Stele code, never loaded from
a parser or a document.

Adapters have no invalidation hook (v1.0's `on_invalidation()`, which nothing
called, is gone). Removing delivered data is a target write, done by the
writer that wrote it.

`SteleChunk` fields: `chunk_id`, `content`, `content_hash` (sha256 verified),
`token_count`, `metadata`.

`SteleTarget` union:
- `LightRAGTarget(workspace: str)`
- `HindsightTarget(instance: Literal["jarvis", "nexus", "crms"])`

---

## Target writers (`contracts/dispatcher.py`)

```python
class TargetWriter(Protocol):
    def write_chunks(self, chunks, target, *, dispatch_id: str) -> None: ...
    def remove_delivery(self, target, *, dispatch_id: str) -> int | None: ...
```

- `dispatch_id` is the **idempotency key**: writing the same chunks again
  under the same `dispatch_id` must not create duplicates (e.g. key rows by
  `(dispatch_id, chunk_id)`, or prefix ids with it).
- A writer that fails after writing some chunks raises
  `PartialWriteError(chunks_written=n)`. Any other exception is recorded as
  "unknown how many were written", never as zero.
- `remove_delivery` removes or tombstones everything written under a
  `dispatch_id`, idempotently, and returns how many chunks it removed (or
  `None` if unknown).

---

## Dispatcher and the delivery log

```
Dispatcher.dispatch(adapter, record_id, target)
      │
      ├── re-read the record from the ledger   →  DispatchRefusedError unless SEALED
      ├── re-verify the bundle in the archive  →  DispatchRefusedError if not intact
      ├── adapter.transform(SealedBundle)      →  list[SteleChunk]
      ├── validate_chunks(chunks)              →  logged failure if invalid
      ├── log  intent  (planned, chunks digest)          ── durable before the write
      ├── TargetWriter.write_chunks(chunks, target, dispatch_id=…)
      └── log  receipt (done)  |  failure (done or unknown, error)
```

Refusals log nothing and write nothing. The state check is against the live
ledger, so a stale in-memory copy of a record that has since been
invalidated is refused.

A **delivery** is one record sent to one target, identified by its
`dispatch_id`. Its history is an append-only list of events in the ledger
database (`stele/ledger/delivery.py`; `UPDATE` and `DELETE` are refused by
triggers): `intent`, `receipt`, `failure`, `removal_intent`,
`removal_receipt`, `removal_failure`. Because each intent is committed before
its write and each outcome after it, a crash at any point leaves a log that
bounds what the target holds:

| Crash | Log shows | Target holds |
|-------|-----------|--------------|
| after the intent, before writing | `in_flight` | nothing |
| mid-write | `in_flight` | some of the planned chunks |
| after writing, before the receipt | `in_flight` | all of them |

`Delivery.explain()` states that bound ("between 0 and N chunks … under
dispatch_id …"). Dispatching the same record to the same target again reuses
the delivery's `dispatch_id`, so the retry completes it without duplicates;
a delivery that already has a receipt is not written again
(`already_delivered=True`).

A failed dispatch never changes the record's ledger state; invalidating
after a failure is the caller's decision.

## Invalidation

```
Dispatcher.invalidate(record_id, reason)
      ├── ledger: SEALED → INVALIDATED
      └── for every delivery that may have written data (per its log):
            log removal_intent → writer.remove_delivery(target, dispatch_id=…)
            → log removal_receipt (done) | removal_failure (error)
```

- Deliveries the log proves wrote nothing are skipped, and removed ones are
  not removed again. A failed removal is retried by calling `invalidate()`
  or `retract()` again.
- A write that races an invalidation, and lands after the removal pass, is
  removed by the dispatching Dispatcher as soon as it records the receipt.
- A record invalidated directly in the ledger (`stele.replay.invalidation`)
  has its deliveries removed by `Dispatcher.retract_invalidated()`.

Ledger states are defined once, in [F-ledger.md](F-ledger.md#state-machine).
Delivery is never a ledger state.

---

## Invariants proven (25 tests / all green)

| Proof | Test class |
|-------|-----------|
| Sealed artifact passed to adapter | `TestAdapterReceivesSealedRecord` |
| Adapter returns typed SteleChunk records | `TestAdapterReturnsTypedChunks` |
| Invalid adapter output rejected (empty, bad hash, dup ids) | `TestInvalidAdapterOutputRejected` |
| Adapter cannot bypass dispatcher write path | `TestAdapterCannotBypassDispatcher` |
| Target writes are represented as intents/results | `TestTargetWritesAsResults` |
| Adapter failure does not invalidate artifact automatically | `TestAdapterFailureIsolation` |
| Dispatcher records success/failure separately from ledger | `TestDispatchLog` |

Durable dispatch and invalidation (#13) are proved in
`tests/test_durable_dispatch.py`: refusal of non-sealed and stale records,
refusal of corrupted archived bytes, crash injection after the intent,
mid-write and before the receipt with duplicate-free retries, and removal
with receipts on invalidation.

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
