# Phase G — Replay + Invalidation View

**Status:** COMPLETE — 2026-06-26  
**Depends on:** Phase F  
**Unblocks:** Phase H

Ledger states (`pending`, `sealed`, `failed`, `invalidated`) are defined once,
in [F-ledger.md](F-ledger.md#state-machine).

## Goal

Any ingestion run recorded in the Phase F ledger must be replayable and
invalidatable. This gives MetaRouter and downstream consumers confidence
that artifacts have a known, recoverable provenance.

## Replay

Given a `run_id`:
1. Locate ledger record
2. Materialize its input Snapshot (`source_hash`) from the archive
3. Re-execute the same parser (`parser`, including its image digest or module
   hash) in a Phase E sandbox with the same `parser_config`
4. The re-execution is a new run with its own record; compare its
   `artifact_hash` against the original and flag divergence
5. The original record is never modified by a replay

The replay engine itself is roadmap #14.

## Invalidation

Given a `run_id` or `source_hash`:
1. Mark ledger record `status = invalidated`
2. Emit invalidation event to downstream subscribers (LightRAG, Hindsight)
3. Downstream subscribers responsible for removing/tombstoning affected chunks
4. Invalidation is non-destructive to the ledger itself — record is kept

## Views required

- `stele.pending_runs` — runs not yet sealed
- `stele.sealed_runs` — archived and verified, queryable by source_hash
- `stele.invalidated_runs` — invalidated records with reason
- `stele.diverged_runs` — replays that produced a different artifact_hash

## Implementation

- `stele/replay/models.py` — `ValidationResult`, `ReplayCandidate`, `ReplayPlan`, `InvalidationReason`
- `stele/replay/validator.py` — `validate_artifact()`: ok / drift / missing
- `stele/replay/planner.py` — `plan_replay()` with run_id / source_path filters
- `stele/replay/invalidation.py` — single, by-source-hash, and auto-invalidate-drifted
- `stele/replay/views.py` — `LedgerViews`: all / pending / sealed / invalidated_or_failed
- `stele/ledger/store.py` — extended with `invalidate()` and `list_by_states()`
- `tests/test_phase_g_replay.py` — 24 tests, all passing

## Completion criteria

- [x] Replay plan selects sealed records, validates against filesystem
- [x] Drift detection (content changed, final symlink, or symlinked parent substitution) → status "drift"
- [x] Missing detection (file deleted) → status "missing"; takes priority over drift
- [x] Invalidation: single record, bulk by source_hash, auto-invalidate-drifted
- [x] Invalidated records excluded from default replay plan
- [x] include_invalidated=True re-includes for audit passes
- [x] All four ledger views created and tested
- [x] Views partition all records (pending ∪ sealed ∪ invalidated_or_failed = all)
