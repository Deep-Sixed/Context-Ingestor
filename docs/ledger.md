# Artifact Ledger

## Goal

Every sandboxed parser run is recorded in the ledger before its output can
count as evidence. A record says exactly which input the parser read, which
parser read it, with what configuration, and what it produced. It is enough
to reconstruct or invalidate everything derived from the run.

## State machine

This is the one definition of ledger states. Other docs and code link here.

```
            create_pending()
                  │
                  ▼
             ┌─────────┐   seal(): bundle archived + verified   ┌────────┐
             │ PENDING │ ─────────────────────────────────────▶ │ SEALED │
             └─────────┘                                        └────────┘
               │     │                                              │
     fail()    │     │ invalidate()                   invalidate()  │
               ▼     ▼                                              ▼
         ┌────────┐ ┌─────────────┐ ◀───────────────────────────────┘
         │ FAILED │ │ INVALIDATED │
         └────────┘ └─────────────┘
```

| State | Meaning | Leaves by |
|-------|---------|-----------|
| `pending` | The bundle is hashed and recorded; it is not yet archived. | `seal`, `fail`, `invalidate` |
| `sealed` | Every artifact and the bundle's tree object are stored in the evidence archive and were verified against the recorded manifest. | `invalidate` |
| `failed` | The parser failed, a check before sealing failed, or sealing failed. Never becomes sealed. | terminal |
| `invalidated` | Withdrawn after the fact (see [replay.md](replay.md)). The record is kept. | terminal |

- **Sealed is integrity, not delivery.** A sealed record says nothing about
  whether any downstream target has received it. Delivery is a separate set
  of facts in the same database: an intent, then a receipt or a failure,
  per target, in the append-only delivery log (see
  [adapter.md](adapter.md#dispatcher-and-the-delivery-log)). A delivery
  failure never changes the ledger state.
- **Only sealed records are delivered**, and invalidating one removes what it
  delivered (`Dispatcher.invalidate`).
- **Transitions are conditional.** Each transition is one `UPDATE … WHERE
  state IN (…)`, so a transition that raced another (e.g. a seal racing an
  invalidation) fails with `InvalidStateTransitionError` instead of
  overwriting it.
- **The transaction always finishes its record.** `ledger_transaction` seals
  on a clean exit and marks the record `failed` on any exception in the block
  (including `KeyboardInterrupt`) or when sealing fails. The original
  exception always propagates.

## One record per run

Every run keeps its own record, keyed by `run_id` (unique). Two runs whose
output is byte-identical share one `artifact_hash` and the archive stores the
content once, but each run keeps its own record, input Snapshot, parser and
state. A later run can never get back, or finalize, an earlier run's record.
A second record for the same `run_id` raises `DuplicateRunError`.

## Ledger record fields

| Field | Description |
|-------|-------------|
| `record_id` | UUID of the record |
| `run_id` | UUID of the sandbox run; unique |
| `source_hash` | Digest of the input **Snapshot** in the archive: the exact bytes the parser saw. Taken from the run (`SandboxResult.input_snapshot`); callers cannot supply it. `None` for a run without input. |
| `source_kind` | The Snapshot's kind (`file` or `tree`); set exactly when `source_hash` is |
| `source_path`, `source_id` | The descriptive **Source** of that input (a locator) and its id in the archive. Descriptive only, never evidence. |
| `parser` | `ParserIdentity`: `name`, `version`, and the executable's digest: `image_digest` (OCI backend) or `module_sha256` (Wasm backends). The digests are measured by the run, not asserted by the caller. |
| `parser_config` | The configuration passed to the parser, stored as canonical JSON |
| `backend` | Sandbox backend that ran the parser |
| `artifact_dir` | Host path of the run's output directory (a location, not evidence) |
| `artifact_manifest` | `{relative POSIX path: sha256}` for every artifact |
| `artifact_hash` | Digest of the bundle's tree object in the archive (`sha256_manifest(manifest)`) |
| `state` | See the state machine above |
| `created_at`, `finalized_at` | ISO-8601 UTC; `finalized_at` is set when the record leaves `pending` |
| `error` | Why the record failed or was invalidated |
| `legacy_source_hash` | Only on records migrated from a pre-#12 ledger: a caller-supplied source hash that matched no Snapshot. Unverified; never used as provenance. |

Queries: `get`, `get_by_run_id`, `find_by_artifact_hash`, `find_by_source_hash`,
`find_by_parser(name, version=None, parser_config=None)` and `list_by_states`.

## Recording a run

```python
archive = BlobStore(archive_root)
ledger = LedgerStore(db_path, archive)          # bound to the archive it points into

result = run_in_sandbox(config, store=archive)  # archives the input Snapshot and artifacts
record = record_run(
    ledger, result,
    parser=ParserIdentity("mineru", "1.3.1"),
    parser_config={"ocr": True},
    source=Source.from_path(input_path),
)
assert record.state is ArtifactState.SEALED
```

`ledger_transaction(...)` is the same with a block that runs while the
record is `pending`, for checks that must pass before the output counts as
evidence. Delivery happens after sealing, from the sealed record, not inside
the block.

Before creating the record, the transaction refuses:

- a failed or timed-out run, or one without artifacts (`SandboxFailedError`);
- a run that read an input that was not archived, or whose Snapshot is not
  in this ledger's archive or differs from the staged input hash
  (`ProvenanceError`);
- a `ParserIdentity` whose `image_digest` / `module_sha256` differs from the
  one the run measured (`ProvenanceError`).

If the bundle on disk no longer matches what the run archived, the record is
created and immediately marked `failed`.

## Sealing

`seal()` stores every artifact in the archive (content that is already stored
is re-verified rather than rewritten) and requires the stored digests to equal
the recorded manifest. If the files are gone from disk, the bundle must
already be in the archive; every blob is then re-hashed. Drift raises
`ArtifactDriftError`, a bundle that is neither on disk nor archived raises
`MissingArtifactError`, and corrupt archived bytes raise `IntegrityError`.
In each case the record stays `pending` (and `ledger_transaction` marks it
`failed`).

## Storage and migration

SQLite in WAL mode, with a schema that maps 1:1 to Postgres. The schema
version is `PRAGMA user_version` (currently 5: records, the delivery log
added by #13, the replay log added by #14, and records' `run_settings`). Opening a ledger of an older version migrates it in one write
transaction (`stele/ledger/migration.py`): either the migration completes or
the database is left unchanged. Versions 2 → 3 → 4 → 5 only add the delivery
and replay logs and the `run_settings` column (`NULL` on existing records).

Migrating a pre-#12 (version 0) ledger:

| Old record | Becomes |
|------------|---------|
| `pending`, `failed`, `invalidated` | Same state |
| `committed`, bundle archivable and matching its manifest (from disk, or already in the archive) | `sealed` |
| `committed`, bundle missing, changed or unsafe | `invalidated`, with the reason in `error` — the ledger can no longer vouch for it |
| `source_hash` that is the digest of exactly one Snapshot in the archive | Kept, with its `source_kind` |
| any other `source_hash` | Moved to `legacy_source_hash` |
| `source_path` | Kept, and recorded as a Source in the archive |
| parser identity and config | Unknown: `NULL` |

Two records for one `run_id` cannot be represented; migration stops with
`LedgerMigrationError` and changes nothing. A ledger newer than this Stele
raises `LedgerSchemaError`.

## Implementation

- `stele/ledger/models.py` — `ArtifactRecord`, `ArtifactState`, `ParserIdentity`
- `stele/ledger/hashing.py` — `sha256_file`, `sha256_manifest`, `build_manifest`
- `stele/ledger/store.py` — `LedgerStore` (SQLite, WAL mode)
- `stele/ledger/transaction.py` — `ledger_transaction`, `record_run`
- `stele/ledger/migration.py` — migration from schema versions 0, 2, 3 and 4
- `stele/ledger/delivery.py` — the delivery log (#13)
- `tests/test_ledger.py` — state machine, hashing, per-run records
- `tests/test_ledger_provenance.py` — Snapshot provenance, parser identity, sealing, migration

## Completion criteria

- [x] Ledger schema defined (SQLite; portable to Postgres)
- [x] Pending / sealed / failed / invalidated transitions, conditional on the checked state
- [x] Failure path: exception inside ledger_transaction → state FAILED
- [x] Missing artifact blocked at create_pending and at seal
- [x] Artifact hashing stays beneath `artifact_dir`; dot-dot traversal and symlinked parent components are refused
- [x] PENDING → SEALED archives the bundle and verifies it against the manifest
- [x] Two runs producing identical output keep separate records with their own Snapshots
- [x] A record can't carry a `source_hash` that differs from its Snapshot digest
- [x] Parser identity and config are stored and queryable
- [x] The state machine is documented once, here
- [x] Migrating an existing ledger is tested
