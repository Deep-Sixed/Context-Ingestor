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
| `backend` | Sandbox backend that ran the parser, or `external` for an artifact recorded outside a sandbox |
| `run_conditions` | `RunConditions`: the `device` (`cpu` or `gpu`) and the `memory`, `cpus`, `pids_limit` and `timeout_seconds` the run executed under. Recorded by Stele's runner (`record_parser_run`), so a replay runs the parser the same way (#30). Every stated condition must match the limits the backend reported applying (`SandboxResult.telemetry.limits`); a mismatch, or a condition the backend does not report, raises `ProvenanceError` and nothing is recorded. `None` for runs recorded without them and for records made before schema version 5. |
| `artifact_dir` | Host path of the run's output directory (a location, not evidence) |
| `artifact_manifest` | `{relative POSIX path: sha256}` for every artifact |
| `artifact_hash` | Digest of the bundle's tree object in the archive (`sha256_manifest(manifest)`) |
| `state` | See the state machine above |
| `created_at`, `finalized_at` | ISO-8601 UTC; `finalized_at` is set when the record leaves `pending` |
| `error` | Why the record failed or was invalidated |
| `legacy_source_hash` | Only on records migrated from a pre-#12 ledger: a caller-supplied source hash that matched no Snapshot. Unverified; never used as provenance. |

Queries: `get`, `get_by_run_id`, `find_by_artifact_hash`, `find_by_source_hash`,
`find_by_parser(name, version=None, parser_config=None, device=None)` and
`list_by_states`.

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

## Recording an artifact made outside a sandbox

Some evidence is produced by an application rather than a sandboxed parser,
for example a document it validated and wants kept (AgentSync's promoted
`SKILL.md` files). `record_external_artifact` is the supported way to ledger
it. Callers should not drive `create_pending` / `seal` themselves.

```python
from stele.ledger.external import record_external_artifact

record = record_external_artifact(
    ledger,
    run_id=run_id,                                   # the caller's UUID
    artifact_dir=out_dir,
    artifact_paths=[out_dir / "SKILL.md"],
    producer=ParserIdentity("agentsync-kanon", "0.1.0"),
    producer_config={"validation_level": "promote"},
    input_snapshot=None,                             # or an archived Snapshot
)
assert record.state is ArtifactState.SEALED
```

- **Same integrity.** The bundle is hashed, archived and verified as for a
  sandbox run, so `sealed` means the same thing.
- **Asserted, not measured, provenance.** The producer is whatever the caller
  says. Such records have `backend = "external"` (`EXTERNAL_BACKEND`), a
  name no sandbox backend uses; `ledger_transaction` refuses a run that
  claims it. The producer can't carry `image_digest` or `module_sha256`,
  since those are measured by a sandbox run (`ProvenanceError`).
- **Never replayed.** There is no parser run to repeat, so replay reports
  these records `UNREPLAYABLE` and never invalidates them.
- **Never read as a sandbox parser.** An external record keeps its
  producer's name, but the parser-specific consumers refuse it: the
  extraction normalizers (`NormalizeError`) and `ChatGPTExportAdapter`
  (`MalformedExportError`). A producer named like a sandbox parser can't
  pass its bytes off as that parser's output. `SealedBundle.backend` and
  `SealedBundle.external` let other adapters make the same check.
- **Input is optional.** An `input_snapshot` must already be in the ledger's
  archive (`ProvenanceError` otherwise).
- **`run_id` is the idempotency key.** Calling again for a recorded `run_id`
  with the same bundle, producer, config, input Snapshot and Source returns
  the sealed record, or seals a record an earlier call left `pending`. A
  retry may come after the caller removed its artifact files: the archive
  vouches for the bundle, and only the artifact paths are compared. Anything else
  for that `run_id`, including a record that is `failed`, raises
  `DuplicateRunError`: record it under a new `run_id`.
- If sealing fails, the record is marked `failed` and the error propagates,
  as with `ledger_transaction`.

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
version is `PRAGMA user_version` (currently 8: records, the delivery log
added by #13, the replay log added by #14, the `run_conditions` column added
by #30, version 6's binding of each delivery to one payload plus the `failed`
replay outcome, the hash-chained event log, and version 8's `attempt_id` on
delivery events). Opening a ledger of an older version migrates it in one
write transaction (`stele/ledger/migration.py`): either the migration
completes or the database is left unchanged. Versions 2 → 3 → 4 → 5 only add
the delivery log, the replay log and the `run_conditions` column (NULL for
existing records: their conditions were never recorded); 5 → 6 adds the
payload-binding trigger and rebuilds the replay log table (to widen its
outcome check), copying every row unchanged; 6 → 7 adds the event log.
Reaching 7 logs every existing record, delivery and replay as an
`*.imported` event. 7 → 8 adds the `attempt_id` column to delivery events
(NULL for existing events, which are read one attempt at a time, as before).

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

## Hash-chained event log

Every fact the ledger learns is also appended to `events`
(`stele/ledger/events.py`), in the same transaction as the change itself, so
the two can't diverge:

| Event | When |
|---|---|
| `record.created` | `create_pending`: run, artifact hash, input Snapshot, Source, parser identity and config, backend |
| `record.sealed` / `record.failed` / `record.invalidated` | each state transition, with its error or reason |
| `delivery.opened` / `delivery.event` | each delivery and each intent, receipt, failure or removal in the delivery log |
| `replay.logged` | each replay and its outcome |
| `*.imported` | the state found when a ledger was migrated to version 5 |

**The chain.**
- Each event's hash is SHA-256 over the canonical JSON of its sequence number,
  kind, subject, body, timestamp and the previous event's hash. The first
  event follows 64 zeros.
- Changing, deleting or reordering an event breaks every hash after it, and
  the head `(seq, hash)` commits to the whole history.
- The table is append-only (triggers refuse `UPDATE` and `DELETE`), but
  verification doesn't trust that: it recomputes everything.

**Verification.** `verify_ledger(ledger, anchor=None)` does two things:
1. It verifies the chain: every hash, every link, no gaps in the sequence.
2. It replays the chain to rebuild what the ledger's tables must contain
   (each record's state and identity, each delivery and its events, each
   replay) and reports every difference. This catches edits to
   `artifact_records`, the one table whose rows legitimately change, such as
   an `invalidated` record flipped back to `sealed` by hand.

A tampered event body is reported, never a crash.

**Anchors.** Publish the head (`EventLog(ledger).head()`) somewhere Stele
doesn't control. Verifying with `anchor=(seq, hash)` then also proves the
chain still contains that exact event, so truncating the log and rewriting
it from there is caught.

```
python -m stele.ledger.events ledger.db archive/ [--anchor SEQ:HASH]
```

This prints `ok`, the chain length, the head and every problem, and exits 1
if there is any problem.

## Implementation

- `stele/ledger/models.py` — `ArtifactRecord`, `ArtifactState`, `ParserIdentity`
- `stele/ledger/hashing.py` — `sha256_file`, `sha256_manifest`, `build_manifest`
- `stele/ledger/store.py` — `LedgerStore` (SQLite, WAL mode)
- `stele/ledger/transaction.py` — `ledger_transaction`, `record_run`
- `stele/ledger/external.py` — `record_external_artifact` for artifacts made outside a sandbox
- `stele/ledger/migration.py` — migration from schema versions 0, 2, 3, 4, 5, 6 and 7
- `stele/ledger/events.py` — the hash-chained event log, `verify_ledger`, CLI
- `stele/ledger/delivery.py` — the delivery log (#13)
- `tests/test_ledger.py` — state machine, hashing, per-run records
- `tests/test_ledger_provenance.py` — Snapshot provenance, parser identity, sealing, migration
- `tests/test_event_log.py` — chaining, verification, tamper detection, anchors, migration, CLI
- `tests/test_external_artifacts.py` — external records: sealing, retries, provenance guards, replay

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
