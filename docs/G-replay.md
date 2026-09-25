# Phase G — Validation, Replay and Invalidation

**Status:** COMPLETE — 2026-06-26 · true replay added for roadmap #14
**Depends on:** Phase F, the evidence archive (#16), durable dispatch (#13), the Wasm backend (#7)
**Unblocks:** Phase H

Ledger states (`pending`, `sealed`, `failed`, `invalidated`) are defined once,
in [F-ledger.md](F-ledger.md#state-machine).

## Goal

Any ingestion run recorded in the Phase F ledger must be checkable,
replayable and invalidatable. This gives MetaRouter and downstream consumers
confidence that artifacts have a known, recoverable provenance.

Phase G has two distinct operations. They answer different questions and are
never reported as each other.

| | Validation | Replay |
|---|---|---|
| Question | Are the files I have still the bytes that were sealed? | Does the recorded parser, run again on the recorded input, still produce this output? |
| Runs a parser | Never | Always (unless `UNREPLAYABLE`) |
| Reads | The record's working copy (`artifact_dir`), falling back to the archive for files gone from disk | The input Snapshot and parser identity from the archive and catalog |
| Result | `ok` / `archived` / `drift` / `missing` per record | `REPRODUCED` / `EQUIVALENT` / `DIVERGED` / `UNREPLAYABLE` per replay |
| Code | `planner.plan_validation`, `validator.validate_artifact` | `engine.replay_record` |

## Validation

`plan_validation(store)` selects sealed records (optionally invalidated ones
too, for audit) and re-hashes every file of each working copy against the
recorded manifest, without following symlinks:

- `ok`: every file is present and matches.
- `archived`: some files are gone from the working copy, but the evidence
  archive holds each of them and they re-verify there. Still intact: sealing
  may legitimately happen after the working copy is cleaned up (#12).
- `drift`: a file on disk changed, or was replaced by a symlink, FIFO or
  symlinked parent.
- `missing`: a file is gone from disk and from the archive (or its archived
  copy no longer verifies).

Priority is missing, then drift, then archived.

`auto_invalidate_drifted()` invalidates the records that are not intact.
The sealed bundle in the archive is unaffected by working-copy drift.

## Replay (`stele/replay/engine.py`)

`replay_record(ledger, catalog, record)`:

1. Looks up the record's `ParserSpec` in the catalog by the recorded parser
   name and version (`stele/replay/parsers.py`). The spec says how to build
   the parser's `SandboxConfig` and what it needs from a backend, and which
   Wasm module or OCI image it is.
2. Checks the parser is available **at its recorded identity**: the spec's
   Wasm module must hash to the recorded `module_sha256`, and the run must
   measure the recorded `image_digest` / `module_sha256`.
3. Materializes the input Snapshot (`source_hash`) from the archive, under the
   input's original file name, and checks the staged bytes hash to it.
4. Re-runs the parser with the recorded `parser_config`, through the same
   `run_parser()` path that produced the record, on a backend with the
   capabilities the spec requires. The replay's output is stored in the archive.
5. Compares the new artifact digests with the recorded manifest.
6. Appends the result to the append-only replay log. The record itself is
   never modified by a replay.

### Outcomes

Four named outcomes, never merged into each other:

| Outcome | Meaning |
|---|---|
| `REPRODUCED` | Byte-identical output from a deterministic replay. Only parsers whose spec requires a `DETERMINISTIC` backend (Wasm, #7) can produce it. |
| `EQUIVALENT` | Accepted under the parser's comparison policy. **Not** proof of reproduction, and never reported as `REPRODUCED`, even when the bytes happen to match. |
| `DIVERGED` | Outside policy. For a parser without a policy, any byte difference. A replay run that fails is also `DIVERGED`. |
| `UNREPLAYABLE` | The parser cannot be run as recorded: no parser identity (a migrated pre-#12 record), no spec in the catalog, a missing or different Wasm module or image, a missing input Snapshot, no capable backend, or a non-deterministic parser with no comparison policy. |

`DIVERGED` feeds invalidation: `invalidate_diverged(dispatcher, results)`
invalidates each diverged record through the Dispatcher, which also removes
what it delivered (#13), with the reason `replay_diverged`.

### Comparison policies (`stele/replay/policy.py`)

A parser that is not deterministic (ML/GPU parsers such as MinerU, Marker
and Docling, #9/#10) carries an explicit `ComparisonPolicy` in its
`ParserSpec`, next to its identity. The policy's `describe()` (name, version,
parameters) is written to the replay log with every verdict.

- A deterministic parser without a policy can only ever be `REPRODUCED`,
  `DIVERGED` or `UNREPLAYABLE`.
- A non-deterministic parser without a policy is always `UNREPLAYABLE`:
  nothing defines what agreement means for it.

`JsonTolerancePolicy(rel_tol, abs_tol, ignore_keys)` is the built-in
structural policy:
- both runs must produce the same set of files;
- `.json` / `.jsonl` files are compared structurally, with floats within
  tolerance and `ignore_keys` (e.g. timestamps) skipped at any depth;
- every other file must be byte-identical.

The packaged ML parsers (MinerU, Marker, Docling; `stele/parsers/catalog.py`)
each carry `ML_REPLAY_POLICY`:
- `abs_tol=0.5` on coordinates in points or pixels, which absorbs float noise
  from thread-order effects but not a moved block;
- `rel_tol=1e-6`;
- `device` ignored in `stele-parser.json`;
- text must match exactly.

`stele.parsers.replay.record_parser_run()` records a packaged-parser run in
the ledger. `replay_spec(parser)` replays it on the CPU image through the same
command and configuration environment as `run_parser()`. The parser-images
workflow replays a real document through each built image and expects
`EQUIVALENT`.

### Cross-platform harness

`tests/test_replay_engine.py` replays a frozen Snapshot fixture
(`tests/fixtures/replay/conversations.json`) through the ChatGPT export
splitter (a Wasm extractor). It asserts `REPRODUCED` and the frozen digests in
`chatgpt-export-split.expected.json` (input, module, every artifact, bundle).
CI runs it on Linux, macOS and Windows. `.gitattributes` keeps git from
rewriting the fixture's bytes on checkout.

The harness also:
- injects non-determinism (unseeded Wasm entropy) and expects `DIVERGED`;
- runs an ML-style parser under a tolerance policy and expects `EQUIVALENT`,
  never `REPRODUCED`;
- expects `UNREPLAYABLE` when the module, image, Snapshot, spec or backend
  is missing.

## Invalidation

Given a `run_id` or `source_hash`:
1. Mark ledger record `status = invalidated`
2. Remove or tombstone everything the record delivered, through the writer
   that wrote it, with a receipt per delivery (`Dispatcher.invalidate`, or
   `Dispatcher.retract_invalidated()` after the functions in
   `invalidation.py`, which only touch the ledger; see
   [H-adapter.md](H-adapter.md#invalidation))
3. Invalidation is non-destructive to the ledger itself — record is kept

## Views

- `LedgerViews.pending()` — runs not yet sealed
- `LedgerViews.sealed()` — archived and verified; `LedgerStore.find_by_source_hash`
- `LedgerViews.invalidated_or_failed()` — invalidated records with reason
- `ReplayLog.with_outcome(ReplayOutcome.DIVERGED)` — replays outside policy

## Implementation

- `stele/replay/models.py` — `ValidationResult`, `ValidationCandidate`, `ValidationPlan`, `InvalidationReason`
- `stele/replay/validator.py` — `validate_artifact()`: ok / drift / missing
- `stele/replay/planner.py` — `plan_validation()` with run_id / source_path filters
- `stele/replay/invalidation.py` — single, by-source-hash, and auto-invalidate-drifted
- `stele/replay/views.py` — `LedgerViews`: all / pending / sealed / invalidated_or_failed
- `stele/replay/parsers.py` — `ParserSpec`, `ParserCatalog`, `run_parser()`
- `stele/replay/policy.py` — `ComparisonPolicy`, `JsonTolerancePolicy`
- `stele/replay/engine.py` — `replay_record()`, `ReplayOutcome`, `ReplayLog`, `invalidate_diverged()`
- `stele/extractors/__init__.py` — `CHATGPT_EXPORT_SPLIT_SPEC`, `EXTRACTOR_SPECS`
- `tests/test_phase_g_replay.py` — validation, invalidation and views
- `tests/test_replay_engine.py` — replay outcomes and the cross-platform harness

## Completion criteria

- [x] Validation plan selects sealed records, validates the working copy
- [x] Drift detection (content changed, final symlink, or symlinked parent substitution) → status "drift"
- [x] Missing detection (file deleted and not verifiable in the archive) → status "missing"; takes priority over drift
- [x] A working copy cleaned up after sealing → status "archived", still intact, never invalidated as missing
- [x] Invalidation: single record, bulk by source_hash, auto-invalidate-drifted
- [x] Invalidated records excluded from the default validation plan
- [x] include_invalidated=True re-includes for audit passes
- [x] Views partition all records (pending ∪ sealed ∪ invalidated_or_failed = all)
- [x] Replaying a Wasm extractor from a frozen Snapshot is `REPRODUCED` on Linux, macOS and Windows
- [x] Injected non-determinism is `DIVERGED` and feeds invalidation
- [x] An ML parser within its policy is `EQUIVALENT`, never `REPRODUCED`
- [x] A missing image, module or Snapshot is `UNREPLAYABLE`
- [x] Validation and replay are distinct operations
