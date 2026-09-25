"""
Phase G replay proof tests.

Seven required proofs:

  PASS 1 — Sealed artifact can be selected for replay
  PASS 2 — Artifact re-hash matches ledger hash (ok)
  PASS 3 — Changed artifact is detected as drift
  PASS 4 — Missing artifact is detected as unreplayable
  PASS 5 — Invalidated artifact is excluded from default replay
  PASS 6 — Failed/pending records are excluded from replay
  PASS 7 — Explicit include_invalidated mode works for audit

Run:
    cd EVECOR/services/stele
    uv run pytest tests/test_phase_g_replay.py -v
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from stele.archive import Snapshot, SnapshotKind
from stele.ledger.models import ArtifactState
from stele.ledger.store import LedgerStore
from stele.replay.invalidation import (
    InvalidationReason,
    auto_invalidate_drifted,
    invalidate_by_source_hash,
    invalidate_record,
)
from stele.replay.models import ValidationResult
from stele.replay.planner import plan_validation
from stele.replay.validator import validate_artifact
from stele.replay.views import LedgerViews
from tests.ledger_helpers import PROVENANCE, open_ledger


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> LedgerStore:
    return open_ledger(tmp_path / "ledger.db")


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


def _write(artifact_dir: Path, name: str, content: str) -> Path:
    p = artifact_dir / name
    p.write_text(content)
    return p


def _sealed_record(store: LedgerStore, artifact_dir: Path, content: str = "{}") -> str:
    """Helper: write an artifact, create pending, seal, return record_id."""
    p = _write(artifact_dir, f"out_{uuid.uuid4().hex[:8]}.json", content)
    record = store.create_pending(
        **PROVENANCE,
        run_id=str(uuid.uuid4()),
        artifact_dir=artifact_dir,
        artifact_paths=[p],
    )
    store.seal(record.record_id)
    return record.record_id


# ---------------------------------------------------------------------------
# PASS 1 — Sealed artifact can be selected for replay
# ---------------------------------------------------------------------------

class TestReplaySelection:

    def test_committed_record_appears_in_plan(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record_id = _sealed_record(store, artifact_dir)
        plan = plan_validation(store)
        assert any(c.record.record_id == record_id for c in plan.candidates)

    def test_plan_summary_counts_are_correct(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        _sealed_record(store, artifact_dir, '{"v": 1}')
        _sealed_record(store, artifact_dir, '{"v": 2}')
        plan = plan_validation(store)
        assert plan.intact_count == 2
        assert plan.drifted_count == 0
        assert plan.missing_count == 0

    def test_filter_by_run_id(self, store: LedgerStore, artifact_dir: Path) -> None:
        p1 = _write(artifact_dir, "r1.json", "run1")
        rec1 = store.create_pending(
            **PROVENANCE,
            run_id="run-aaa", artifact_dir=artifact_dir, artifact_paths=[p1]
        )
        store.seal(rec1.record_id)

        p2 = _write(artifact_dir, "r2.json", "run2")
        rec2 = store.create_pending(
            **PROVENANCE,
            run_id="run-bbb", artifact_dir=artifact_dir, artifact_paths=[p2]
        )
        store.seal(rec2.record_id)

        plan = plan_validation(store, run_id="run-aaa")
        assert len(plan.candidates) == 1
        assert plan.candidates[0].record.run_id == "run-aaa"


# ---------------------------------------------------------------------------
# PASS 2 — Artifact re-hash matches ledger hash
# ---------------------------------------------------------------------------

class TestHashMatchOk:

    def test_unchanged_artifact_validates_ok(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write(artifact_dir, "result.json", '{"stable": true}')
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)
        sealed = store.get(record.record_id)

        result = validate_artifact(sealed)
        assert result.status == "ok"
        assert result.is_intact
        assert not result.drifted_files
        assert not result.missing_files

    def test_ok_artifact_appears_in_replayable(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        _sealed_record(store, artifact_dir)
        plan = plan_validation(store)
        assert plan.intact_count == 1
        assert plan.drifted_count == 0


# ---------------------------------------------------------------------------
# PASS 3 — Changed artifact is detected as drift
# ---------------------------------------------------------------------------

class TestDriftDetection:

    def test_changed_content_detected_as_drift(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write(artifact_dir, "result.json", "original content")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)
        sealed = store.get(record.record_id)

        # Mutate the file after sealing
        p.write_text("tampered content")

        result = validate_artifact(sealed)
        assert result.status == "drift"
        assert not result.is_intact
        assert "result.json" in result.drifted_files

    def test_symlinked_artifact_is_drift_not_followed(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        p = _write(artifact_dir, "result.json", "original")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)
        sealed = store.get(record.record_id)

        outside = tmp_path / "outside.txt"
        outside.write_text("host content")
        p.unlink()
        p.symlink_to(outside)

        result = validate_artifact(sealed)
        assert result.status == "drift"
        assert "result.json" in result.drifted_files

    def test_symlinked_parent_directory_is_drift_not_followed(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        nested = artifact_dir / "nested"
        nested.mkdir()
        p = _write(nested, "result.json", "original")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)
        sealed = store.get(record.record_id)

        p.unlink()
        nested.rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "result.json").write_text("host content")
        nested.symlink_to(outside, target_is_directory=True)

        result = validate_artifact(sealed)
        assert result.status == "drift"
        assert "nested/result.json" in result.drifted_files

    def test_drifted_artifact_appears_in_plan_drifted(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write(artifact_dir, "out.json", "v1")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)

        p.write_text("v2 — mutated")
        plan = plan_validation(store)

        assert plan.drifted_count == 1
        assert plan.intact_count == 0

    def test_drift_summary_includes_filename(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write(artifact_dir, "chunk.json", "original")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)
        p.write_text("changed")

        result = validate_artifact(store.get(record.record_id))
        assert "chunk.json" in result.drift_summary


# ---------------------------------------------------------------------------
# PASS 4 — Missing artifact is detected as unreplayable
# ---------------------------------------------------------------------------

class TestMissingDetection:

    def test_deleted_file_detected_as_missing(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write(artifact_dir, "result.json", "content")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)
        sealed = store.get(record.record_id)

        p.unlink()  # delete after sealing

        result = validate_artifact(sealed)
        assert result.status == "missing"
        assert not result.is_intact
        assert "result.json" in result.missing_files

    def test_missing_takes_priority_over_drift(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        """If some files are missing and others drifted, status is 'missing'."""
        p1 = _write(artifact_dir, "a.json", "aaa")
        p2 = _write(artifact_dir, "b.json", "bbb")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p1, p2]
        )
        store.seal(record.record_id)

        p1.unlink()          # missing
        p2.write_text("BBB")  # drifted

        result = validate_artifact(store.get(record.record_id))
        assert result.status == "missing"
        assert "a.json" in result.missing_files

    def test_missing_appears_in_plan(self, store: LedgerStore, artifact_dir: Path) -> None:
        """Missing means gone from disk and from the archive."""
        p = _write(artifact_dir, "r.json", "data")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)
        p.unlink()
        digest = record.artifact_manifest["r.json"]
        blob = store.archive.root / "blobs" / digest[:2] / digest[2:]
        blob.chmod(0o600)
        blob.write_bytes(b"corrupt")  # the archived copy no longer verifies

        plan = plan_validation(store)
        assert plan.missing_count == 1
        assert plan.intact_count == 0


class TestSealedFromArchive:
    """A sealed record whose working copy is gone but whose bundle is verified
    in the archive is intact, never missing (review of #23)."""

    def _sealed_then_cleaned(self, store: LedgerStore, artifact_dir: Path, *names: str):
        paths = [_write(artifact_dir, n, f"content of {n}") for n in names]
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=paths
        )
        return store.seal(record.record_id), paths

    def test_cleaned_up_output_is_archived_not_missing(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record, paths = self._sealed_then_cleaned(store, artifact_dir, "a.json", "b.json")
        for p in paths:
            p.unlink()

        plan = plan_validation(store)
        [candidate] = plan.candidates
        assert candidate.validation.status == "archived"
        assert candidate.validation.archived_files == ("a.json", "b.json")
        assert candidate.is_intact
        assert (plan.missing_count, plan.intact_count) == (0, 1)

        assert auto_invalidate_drifted(store, list(plan.candidates)) == []
        assert store.get(record.record_id).state is ArtifactState.SEALED

    def test_without_an_archive_the_working_copy_alone_decides(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record, paths = self._sealed_then_cleaned(store, artifact_dir, "a.json")
        paths[0].unlink()
        assert validate_artifact(record).status == "missing"
        assert validate_artifact(record, store.archive).status == "archived"

    def test_drift_on_disk_outranks_archived(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record, (a, b) = self._sealed_then_cleaned(store, artifact_dir, "a.json", "b.json")
        a.unlink()
        b.write_text("tampered")
        result = validate_artifact(record, store.archive)
        assert result.status == "drift"
        assert (result.drifted_files, result.archived_files) == (("b.json",), ("a.json",))
        assert not result.is_intact


# ---------------------------------------------------------------------------
# PASS 5 — Invalidated artifact excluded from default replay
# ---------------------------------------------------------------------------

class TestInvalidatedExcludedByDefault:

    def test_invalidated_record_excluded_from_default_plan(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record_id = _sealed_record(store, artifact_dir)
        invalidate_record(store, record_id, InvalidationReason.MANUAL, note="test")

        plan = plan_validation(store)
        assert not any(c.record.record_id == record_id for c in plan.candidates)
        assert len(plan.candidates) == 0

    def test_invalidated_state_persists_in_ledger(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record_id = _sealed_record(store, artifact_dir)
        invalidate_record(store, record_id, InvalidationReason.SOURCE_CHANGED)

        record = store.get(record_id)
        assert record.state is ArtifactState.INVALIDATED
        assert "source_changed" in (record.error or "")

    def test_bulk_invalidate_by_source_hash(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        def snapshot(data: bytes) -> Snapshot:
            digest = store.archive.put_bytes(data)
            return store.archive.put_snapshot(
                Snapshot(SnapshotKind.FILE, digest, len(data), 1)
            )

        old, other = snapshot(b"source v1"), snapshot(b"another source")
        # Two sealed runs over the same input Snapshot, one over another.
        dirs = [tmp_path / f"art{i}" for i in range(3)]
        records = []
        for d, snap in zip(dirs, [old, old, other]):
            d.mkdir()
            p = _write(d, "r.json", f"content-{d.name}")
            rec = store.create_pending(
                **PROVENANCE,
                run_id=str(uuid.uuid4()),
                artifact_dir=d,
                artifact_paths=[p],
                input_snapshot=snap,
            )
            records.append(store.seal(rec.record_id))

        invalidated = invalidate_by_source_hash(
            store, old.digest, InvalidationReason.SOURCE_CHANGED
        )
        assert {r.record_id for r in invalidated} == {r.record_id for r in records[:2]}
        assert all(r.state is ArtifactState.INVALIDATED for r in invalidated)
        assert store.get(records[2].record_id).state is ArtifactState.SEALED

    def test_auto_invalidate_drifted_from_plan(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write(artifact_dir, "r.json", "original")
        rec = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(rec.record_id)
        p.write_text("drifted")

        plan = plan_validation(store)
        assert plan.drifted_count == 1

        auto_invalidate_drifted(store, plan.drifted)
        assert store.get(rec.record_id).state is ArtifactState.INVALIDATED

        # After invalidation, default replay plan is empty
        clean_plan = plan_validation(store)
        assert len(clean_plan.candidates) == 0


# ---------------------------------------------------------------------------
# PASS 6 — Failed/pending records excluded from replay
# ---------------------------------------------------------------------------

class TestNonSealedExcluded:

    def test_pending_record_excluded(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = _write(artifact_dir, "r.json", "{}")
        store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        plan = plan_validation(store)
        assert len(plan.candidates) == 0

    def test_failed_record_excluded(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = _write(artifact_dir, "r.json", "{}")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.fail(record.record_id, "parse error")

        plan = plan_validation(store)
        assert len(plan.candidates) == 0

    def test_only_committed_records_in_replay(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        # One sealed, one pending, one failed
        dirs = [artifact_dir / f"d{i}" for i in range(3)]
        for d in dirs:
            d.mkdir()

        p0 = _write(dirs[0], "r.json", "sealed")
        rec0 = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=dirs[0], artifact_paths=[p0]
        )
        store.seal(rec0.record_id)

        p1 = _write(dirs[1], "r.json", "pending")
        store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=dirs[1], artifact_paths=[p1]
        )

        p2 = _write(dirs[2], "r.json", "failed")
        rec2 = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=dirs[2], artifact_paths=[p2]
        )
        store.fail(rec2.record_id, "error")

        plan = plan_validation(store)
        assert len(plan.candidates) == 1
        assert plan.candidates[0].record.record_id == rec0.record_id


# ---------------------------------------------------------------------------
# PASS 7 — Explicit include_invalidated mode works for audit
# ---------------------------------------------------------------------------

class TestIncludeInvalidatedMode:

    def test_include_invalidated_returns_invalidated_records(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record_id = _sealed_record(store, artifact_dir)
        invalidate_record(store, record_id, InvalidationReason.MANUAL)

        default_plan = plan_validation(store)
        audit_plan = plan_validation(store, include_invalidated=True)

        assert len(default_plan.candidates) == 0
        assert len(audit_plan.candidates) == 1
        assert audit_plan.candidates[0].record.record_id == record_id

    def test_include_invalidated_mixed_with_sealed(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        dir2 = tmp_path / "art2"
        dir2.mkdir()

        r1_id = _sealed_record(store, artifact_dir, '{"v":1}')
        r2_id = _sealed_record(store, dir2, '{"v":2}')

        invalidate_record(store, r1_id, InvalidationReason.DATA_QUALITY)

        default_plan = plan_validation(store)
        audit_plan = plan_validation(store, include_invalidated=True)

        # Default: only the non-invalidated sealed record
        assert len(default_plan.candidates) == 1
        assert default_plan.candidates[0].record.record_id == r2_id

        # Audit: both
        assert len(audit_plan.candidates) == 2

    def test_views_invalidated_or_failed_includes_both(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        dir2 = tmp_path / "art2"
        dir2.mkdir()

        r1_id = _sealed_record(store, artifact_dir)
        invalidate_record(store, r1_id, InvalidationReason.MANUAL)

        p2 = _write(dir2, "r.json", "{}")
        rec2 = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=dir2, artifact_paths=[p2]
        )
        store.fail(rec2.record_id, "crash")

        views = LedgerViews(store)
        bad = views.invalidated_or_failed()
        bad_ids = {r.record_id for r in bad}

        assert r1_id in bad_ids
        assert rec2.record_id in bad_ids
        assert len(bad) == 2

    def test_four_views_partition_all_records(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        dirs = [tmp_path / f"d{i}" for i in range(4)]
        for d in dirs:
            d.mkdir()

        # 1 pending
        p0 = _write(dirs[0], "r.json", "pending")
        store.create_pending(**PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=dirs[0], artifact_paths=[p0])

        # 1 sealed
        p1 = _write(dirs[1], "r.json", "sealed")
        rec1 = store.create_pending(**PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=dirs[1], artifact_paths=[p1])
        store.seal(rec1.record_id)

        # 1 failed
        p2 = _write(dirs[2], "r.json", "failed")
        rec2 = store.create_pending(**PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=dirs[2], artifact_paths=[p2])
        store.fail(rec2.record_id, "err")

        # 1 invalidated
        p3 = _write(dirs[3], "r.json", "invalidated")
        rec3 = store.create_pending(**PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=dirs[3], artifact_paths=[p3])
        store.seal(rec3.record_id)
        store.invalidate(rec3.record_id, "manual test")

        views = LedgerViews(store)
        all_ids = {r.record_id for r in views.all()}
        pending_ids = {r.record_id for r in views.pending()}
        sealed_ids = {r.record_id for r in views.sealed()}
        bad_ids = {r.record_id for r in views.invalidated_or_failed()}

        assert len(all_ids) == 4
        assert len(pending_ids) == 1
        assert len(sealed_ids) == 1
        assert len(bad_ids) == 2
        # union of all partitions == all
        assert pending_ids | sealed_ids | bad_ids == all_ids
