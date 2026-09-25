# Phase F — Deterministic Artifact/Chunk Ledger

**Status:** COMPLETE — 2026-06-26  
**Depends on:** Phase E  
**Unblocks:** Phase G

## Goal

Every parse artifact produced by a sandboxed parser run must be recorded in
a ledger before any downstream write is permitted. The ledger entry must be
sufficient to reconstruct or invalidate the downstream state.

## Ledger record fields

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | uuid | Unique identifier for this parse run |
| `source_path` | str | Original input file path (outside sandbox) |
| `source_hash` | sha256 | Hash of the input file at ingest time |
| `parser` | str | Parser name and version (e.g., `raganything:1.3.1`) |
| `parser_config` | jsonb | Config/options passed to parser |
| `artifact_path` | str | Path to output bundle in staging |
| `artifact_hash` | sha256 | Hash of full output bundle |
| `chunks` | jsonb | List of `{chunk_id, content_hash, token_count}` |
| `status` | enum | `pending` / `committed` / `invalidated` |
| `created_at` | timestamptz | |
| `committed_at` | timestamptz | Null until downstream write confirmed |
| `invalidated_at` | timestamptz | Null unless invalidated |
| `invalidation_reason` | str | |

## Storage

Ledger lives in a dedicated Postgres schema `stele` on an appropriate DB.
TBD: whether to co-locate on `graphify-core-db` or a standalone Stele DB.

## Commit protocol

1. Parser runs inside Phase E sandbox → produces artifact bundle in staging
2. Ledger record inserted with `status = pending`
3. Downstream adapter reads artifact from staging via ledger `run_id`
4. Adapter writes to target (LightRAG, Hindsight, etc.)
5. On success: ledger record updated to `status = committed`
6. On failure: ledger record stays `pending`; no partial state in production

## Implementation

- `stele/ledger/models.py` — `ArtifactRecord`, `ArtifactState`
- `stele/ledger/hashing.py` — `sha256_file`, `sha256_manifest`, `build_manifest`
- `stele/ledger/store.py` — `LedgerStore` (SQLite, WAL mode)
- `stele/ledger/transaction.py` — `ledger_transaction` context manager
- `tests/test_phase_f_ledger.py` — 27 tests, all passing

## Completion criteria

- [x] Ledger schema defined (SQLite; portable to Postgres)
- [x] Ledger writer implemented (`store.create_pending`)
- [x] Commit/pending/failed status transitions implemented
- [x] Round-trip test: create_pending → commit → verify COMMITTED
- [x] Failure path: exception inside ledger_transaction → state FAILED
- [x] Missing artifact blocked at create_pending and at commit
- [x] Artifact hashing stays beneath `artifact_dir`; dot-dot traversal and symlinked parent components are refused
- [x] PENDING → COMMITTED re-hashes the complete manifest and refuses drift
- [x] Duplicate artifact_hash: raise (default) or ignore (idempotent)
