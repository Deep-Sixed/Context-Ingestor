# Phase G — Replay + Invalidation View

**Status:** COMPLETE — 2026-06-26  
**Depends on:** Phase F  
**Unblocks:** Phase H

## Goal

Any ingestion run recorded in the Phase F ledger must be replayable and
invalidatable. This gives MetaRouter and downstream consumers confidence
that artifacts have a known, recoverable provenance.

## Replay

Given a `run_id`:
1. Locate ledger record
2. Verify `source_hash` against current input file (detect if source changed)
3. Re-execute parser in Phase E sandbox with same `parser_config`
4. Compare new `artifact_hash` against ledger; flag if diverged
5. If match: promote to `committed` (idempotent replay)
6. If diverged: create new `run_id`, leave original intact

## Invalidation

Given a `run_id` or `source_hash`:
1. Mark ledger record `status = invalidated`
2. Emit invalidation event to downstream subscribers (LightRAG, Hindsight)
3. Downstream subscribers responsible for removing/tombstoning affected chunks
4. Invalidation is non-destructive to the ledger itself — record is kept

## Views required

- `stele.pending_runs` — runs not yet committed
- `stele.committed_runs` — successfully ingested, queryable by source_hash
- `stele.invalidated_runs` — invalidated records with reason
- `stele.diverged_runs` — replays that produced a different artifact_hash

## Implementation

- `stele/replay/models.py` — `ValidationResult`, `ReplayCandidate`, `ReplayPlan`, `InvalidationReason`
- `stele/replay/validator.py` — `validate_artifact()`: ok / drift / missing
- `stele/replay/planner.py` — `plan_replay()` with run_id / source_path filters
- `stele/replay/invalidation.py` — single, by-source-hash, and auto-invalidate-drifted
- `stele/replay/views.py` — `LedgerViews`: all / pending / committed / invalidated_or_failed
- `stele/ledger/store.py` — extended with `invalidate()` and `list_by_states()`
- `tests/test_phase_g_replay.py` — 22 tests, all passing

## Completion criteria

- [x] Replay plan selects committed records, validates against filesystem
- [x] Drift detection (content changed) → status "drift"
- [x] Missing detection (file deleted) → status "missing"; takes priority over drift
- [x] Invalidation: single record, bulk by source_hash, auto-invalidate-drifted
- [x] Invalidated records excluded from default replay plan
- [x] include_invalidated=True re-includes for audit passes
- [x] All four ledger views created and tested
- [x] Views partition all records (pending ∪ committed ∪ invalidated_or_failed = all)
