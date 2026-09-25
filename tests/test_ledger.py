"""
Ledger proof tests.

Six required proofs:

  PASS 1 — Artifact record created as pending
  PASS 2 — Hash is deterministic
  PASS 3 — Pending can become sealed (bundle archived and verified)
  PASS 4 — Failed parser marks record failed, not sealed
  PASS 5 — Missing artifact cannot be sealed
  PASS 6 — Identical output from separate runs keeps separate records

Provenance (input Snapshot, parser identity) and migration are proved in
tests/test_ledger_provenance.py.

Run:
    uv run pytest tests/test_ledger.py -v
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from stele.containment.result import SandboxResult
from stele.ledger.hashing import UnsafeFileError, build_manifest, sha256_file, sha256_manifest
from stele.ledger.models import ArtifactState
from stele.ledger.store import (
    ArtifactDriftError,
    DuplicateRunError,
    InvalidStateTransitionError,
    LedgerStore,
    MissingArtifactError,
)
from stele.ledger.transaction import SandboxFailedError, ledger_transaction
from tests.ledger_helpers import PROVENANCE, open_ledger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> LedgerStore:
    return open_ledger(tmp_path / "ledger.db")


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


def _write_artifact(artifact_dir: Path, name: str, content: str) -> Path:
    p = artifact_dir / name
    p.write_text(content)
    return p


def _make_sandbox_result(
    artifact_dir: Path,
    artifact_paths: list[Path],
    exit_code: int = 0,
    timed_out: bool = False,
) -> SandboxResult:
    return SandboxResult(
        run_id=uuid.uuid4(),
        exit_code=exit_code,
        stdout="",
        stderr="",
        artifact_paths=artifact_paths,
        artifact_dir=artifact_dir,
        wall_time_seconds=0.1,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# PASS 1 — Artifact record created as pending
# ---------------------------------------------------------------------------

class TestPendingCreation:

    def test_record_state_is_pending(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = _write_artifact(artifact_dir, "result.json", '{"chunks": []}')
        result = _make_sandbox_result(artifact_dir, [p])

        record = store.create_pending(
            **PROVENANCE,
            run_id=str(result.run_id),
            artifact_dir=artifact_dir,
            artifact_paths=result.artifact_paths,
        )

        assert record.state is ArtifactState.PENDING
        assert record.finalized_at is None
        assert record.error is None

    def test_record_fields_populated(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = _write_artifact(artifact_dir, "out.txt", "hello")
        run_id = str(uuid.uuid4())

        record = store.create_pending(
            **PROVENANCE,
            run_id=run_id,
            artifact_dir=artifact_dir,
            artifact_paths=[p],
        )

        assert record.run_id == run_id
        assert record.source_path is None and record.source_hash is None
        assert record.parser == PROVENANCE["parser"]
        assert record.parser_config == PROVENANCE["parser_config"]
        assert "out.txt" in record.artifact_manifest
        assert record.artifact_hash  # non-empty

    def test_source_hash_cannot_be_supplied(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write_artifact(artifact_dir, "out.txt", "hello")
        with pytest.raises(TypeError, match="source_hash"):
            store.create_pending(
                **PROVENANCE,
                run_id=str(uuid.uuid4()),
                artifact_dir=artifact_dir,
                artifact_paths=[p],
                source_hash="abc123",
            )

    def test_record_retrievable_by_id(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = _write_artifact(artifact_dir, "x.json", "{}")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()),
            artifact_dir=artifact_dir,
            artifact_paths=[p],
        )
        fetched = store.get(record.record_id)
        assert fetched.record_id == record.record_id
        assert fetched.state is ArtifactState.PENDING

    def test_empty_artifact_list_raises(self, store: LedgerStore, artifact_dir: Path) -> None:
        with pytest.raises(ValueError, match="no artifacts"):
            store.create_pending(
                **PROVENANCE,
                run_id=str(uuid.uuid4()),
                artifact_dir=artifact_dir,
                artifact_paths=[],
            )


# ---------------------------------------------------------------------------
# PASS 2 — Hash is deterministic
# ---------------------------------------------------------------------------

class TestDeterministicHash:

    def test_same_content_same_file_hash(self, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("identical content")
        b.write_text("identical content")
        assert sha256_file(a) == sha256_file(b)

    def test_different_content_different_file_hash(self, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("content A")
        b.write_text("content B")
        assert sha256_file(a) != sha256_file(b)

    def test_file_hash_rejects_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("host content")
        link = tmp_path / "artifact.txt"
        link.symlink_to(target)

        with pytest.raises(UnsafeFileError, match="not a regular file"):
            sha256_file(link)

    def test_manifest_rejects_dotdot_escape(
        self, artifact_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("host content")
        escaped = artifact_dir / ".." / "outside.txt"

        with pytest.raises(ValueError, match="unsafe path"):
            build_manifest(artifact_dir, [escaped])

    def test_manifest_rejects_symlinked_parent_directory(
        self, artifact_dir: Path, tmp_path: Path
    ) -> None:
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        secret = outside_dir / "secret.txt"
        secret.write_text("host content")
        linked = artifact_dir / "linked"
        linked.symlink_to(outside_dir, target_is_directory=True)

        with pytest.raises(UnsafeFileError, match="directory component"):
            build_manifest(artifact_dir, [linked / "secret.txt"])

    def test_manifest_hash_is_order_independent(self, artifact_dir: Path) -> None:
        """Two manifests with the same entries in different dict order hash identically."""
        p1 = _write_artifact(artifact_dir, "a.txt", "alpha")
        p2 = _write_artifact(artifact_dir, "b.txt", "beta")

        m1 = {"a.txt": sha256_file(p1), "b.txt": sha256_file(p2)}
        m2 = {"b.txt": sha256_file(p2), "a.txt": sha256_file(p1)}

        assert sha256_manifest(m1) == sha256_manifest(m2)

    def test_same_artifacts_produce_same_artifact_hash(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        """Two separate artifact_dirs with identical content → same artifact_hash."""
        dir2 = tmp_path / "artifacts2"
        dir2.mkdir()

        content = "reproducible parse output"
        p1 = _write_artifact(artifact_dir, "out.txt", content)
        p2 = _write_artifact(dir2, "out.txt", content)

        m1 = build_manifest(artifact_dir, [p1])
        m2 = build_manifest(dir2, [p2])

        assert sha256_manifest(m1) == sha256_manifest(m2)

    def test_changed_content_changes_artifact_hash(self, artifact_dir: Path) -> None:
        p = _write_artifact(artifact_dir, "out.txt", "version 1")
        m1 = build_manifest(artifact_dir, [p])
        h1 = sha256_manifest(m1)

        p.write_text("version 2")
        m2 = build_manifest(artifact_dir, [p])
        h2 = sha256_manifest(m2)

        assert h1 != h2


# ---------------------------------------------------------------------------
# PASS 3 — Pending can become sealed
# ---------------------------------------------------------------------------

class TestSeal:

    def test_pending_transitions_to_sealed(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write_artifact(artifact_dir, "result.json", "{}")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        sealed = store.seal(record.record_id)

        assert sealed.state is ArtifactState.SEALED
        assert sealed.finalized_at is not None
        # Sealed means the bundle is in the archive and verified.
        assert store.archive.read_tree(sealed.artifact_hash) == sealed.artifact_manifest
        for digest in sealed.artifact_manifest.values():
            assert store.archive.has(digest)

    def test_sealed_state_persists(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = _write_artifact(artifact_dir, "r.json", "{}")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)
        refetched = store.get(record.record_id)
        assert refetched.state is ArtifactState.SEALED

    def test_changed_pending_artifact_cannot_seal(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write_artifact(artifact_dir, "r.json", "before")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        p.write_text("after")

        with pytest.raises(ArtifactDriftError, match="changed after pending"):
            store.seal(record.record_id)

        assert store.get(record.record_id).state is ArtifactState.PENDING

    def test_seal_already_sealed_raises(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write_artifact(artifact_dir, "r.json", "{}")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.seal(record.record_id)

        with pytest.raises(InvalidStateTransitionError):
            store.seal(record.record_id)

    def test_ledger_transaction_seals_on_clean_exit(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write_artifact(artifact_dir, "result.json", '{"ok": true}')
        result = _make_sandbox_result(artifact_dir, [p])

        with ledger_transaction(store, result, **PROVENANCE) as record:
            assert record.state is ArtifactState.PENDING
            pending_id = record.record_id

        final = store.get(pending_id)
        assert final.state is ArtifactState.SEALED


# ---------------------------------------------------------------------------
# PASS 4 — Failed parser marks record failed, not sealed
# ---------------------------------------------------------------------------

class TestFailedParser:

    def test_failed_sandbox_raises_before_creating_record(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        """A sandbox that exited non-zero must never produce a pending record."""
        result = _make_sandbox_result(artifact_dir, artifact_paths=[], exit_code=1)

        with pytest.raises(SandboxFailedError):
            with ledger_transaction(store, result, **PROVENANCE):
                pass  # should not reach here

    def test_exception_inside_block_marks_record_failed(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write_artifact(artifact_dir, "r.json", "{}")
        result = _make_sandbox_result(artifact_dir, [p])
        record_id: str | None = None

        with pytest.raises(RuntimeError, match="downstream write failed"):
            with ledger_transaction(store, result, **PROVENANCE) as record:
                record_id = record.record_id
                raise RuntimeError("downstream write failed")

        assert record_id is not None
        final = store.get(record_id)
        assert final.state is ArtifactState.FAILED
        assert final.state is not ArtifactState.SEALED
        assert "downstream write failed" in (final.error or "")

    def test_failed_record_cannot_be_sealed(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write_artifact(artifact_dir, "r.json", "{}")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        store.fail(record.record_id, error="something went wrong")

        with pytest.raises(InvalidStateTransitionError):
            store.seal(record.record_id)

    def test_timed_out_sandbox_raises_before_record(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        result = _make_sandbox_result(artifact_dir, [], exit_code=-1, timed_out=True)
        with pytest.raises(SandboxFailedError):
            with ledger_transaction(store, result, **PROVENANCE):
                pass


# ---------------------------------------------------------------------------
# PASS 5 — Missing artifact cannot be sealed
# ---------------------------------------------------------------------------

class TestMissingArtifact:

    def test_nonexistent_path_rejected_at_create_pending(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        ghost = artifact_dir / "ghost.json"
        # do NOT create the file
        with pytest.raises(FileNotFoundError):
            store.create_pending(
                **PROVENANCE,
                run_id=str(uuid.uuid4()),
                artifact_dir=artifact_dir,
                artifact_paths=[ghost],
            )

    def test_artifact_outside_artifact_dir_rejected(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "sneaky.json"
        outside.write_text("{}")
        with pytest.raises(ValueError, match="outside artifact_dir"):
            store.create_pending(
                **PROVENANCE,
                run_id=str(uuid.uuid4()),
                artifact_dir=artifact_dir,
                artifact_paths=[outside],
            )

    def test_artifact_deleted_after_pending_blocks_seal(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = _write_artifact(artifact_dir, "r.json", "{}")
        record = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        p.unlink()  # delete the file after pending is created

        with pytest.raises(MissingArtifactError):
            store.seal(record.record_id)

        # Record must remain PENDING, not sealed
        assert store.get(record.record_id).state is ArtifactState.PENDING


# ---------------------------------------------------------------------------
# PASS 6 — Identical output from separate runs keeps separate records
# ---------------------------------------------------------------------------

class TestPerRunRecords:

    def _identical_runs(self, store: LedgerStore, artifact_dir: Path, tmp_path: Path):
        content = "deterministic parse output"
        p1 = _write_artifact(artifact_dir, "r.json", content)
        dir2 = tmp_path / "artifacts2"
        dir2.mkdir()
        p2 = _write_artifact(dir2, "r.json", content)
        r1 = store.create_pending(
            **PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p1]
        )
        r2 = store.create_pending(
            **PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=dir2, artifact_paths=[p2]
        )
        return r1, r2

    def test_identical_output_gets_its_own_record(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        r1, r2 = self._identical_runs(store, artifact_dir, tmp_path)

        assert r1.record_id != r2.record_id
        assert r1.run_id != r2.run_id
        assert r1.artifact_hash == r2.artifact_hash
        assert {r.record_id for r in store.find_by_artifact_hash(r1.artifact_hash)} == {
            r1.record_id, r2.record_id
        }

    def test_identical_output_is_archived_once_and_both_seal(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        r1, r2 = self._identical_runs(store, artifact_dir, tmp_path)
        store.seal(r1.record_id)
        store.seal(r2.record_id)

        [digest] = r1.artifact_manifest.values()
        blob_files = [p for p in (store.archive.root / "blobs").rglob("*") if p.is_file()]
        # One content blob plus one tree object, shared by both runs.
        assert sorted(p.name for p in blob_files) == sorted(
            [digest[2:], r1.artifact_hash[2:]]
        )
        assert store.get(r1.record_id).state is ArtifactState.SEALED
        assert store.get(r2.record_id).state is ArtifactState.SEALED

    def test_an_earlier_run_state_never_leaks_into_a_new_run(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        """Pre-#12, a duplicate got back the old record, even a FAILED one."""
        r1, r2 = self._identical_runs(store, artifact_dir, tmp_path)
        store.fail(r1.record_id, "first run's downstream failed")

        assert store.get(r2.record_id).state is ArtifactState.PENDING
        assert store.seal(r2.record_id).state is ArtifactState.SEALED
        assert store.get(r1.record_id).state is ArtifactState.FAILED

    def test_second_record_for_the_same_run_is_refused(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        run_id = str(uuid.uuid4())
        p1 = _write_artifact(artifact_dir, "a.json", "a")
        p2 = _write_artifact(artifact_dir, "b.json", "b")
        first = store.create_pending(
            **PROVENANCE, run_id=run_id, artifact_dir=artifact_dir, artifact_paths=[p1]
        )
        with pytest.raises(DuplicateRunError, match=first.record_id):
            store.create_pending(
                **PROVENANCE, run_id=run_id, artifact_dir=artifact_dir, artifact_paths=[p2]
            )
        assert store.get_by_run_id(run_id) == first

    def test_different_content_different_records(
        self, store: LedgerStore, artifact_dir: Path, tmp_path: Path
    ) -> None:
        p1 = _write_artifact(artifact_dir, "r.json", "output version A")
        r1 = store.create_pending(
            **PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p1]
        )

        dir2 = tmp_path / "artifacts2"
        dir2.mkdir()
        p2 = _write_artifact(dir2, "r.json", "output version B")
        r2 = store.create_pending(
            **PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=dir2, artifact_paths=[p2]
        )

        assert r1.record_id != r2.record_id
        assert r1.artifact_hash != r2.artifact_hash
