"""
Trust-boundary regression tests.

Everything in artifact_dir is written by an untrusted parser.  These tests
prove that nothing a parser leaves there can make the host hash, ledger, or
commit anything other than the regular files it actually wrote:

  - symlinks (to files, to directories, dangling) are rejected, never followed
  - FIFOs and other non-regular files are rejected without blocking
  - a run that leaves rejected entries does not succeed and cannot be ledgered
  - commit() re-hashes, so a file changed after hashing cannot be committed
  - the validator never follows a symlink swapped in after commit
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from stele.containment.result import SandboxResult
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import BubblewrapSandbox, SandboxConfig
from stele.ledger.hashing import (
    UnsafeArtifactError,
    build_manifest,
    collect_artifacts,
    resolve_artifact,
    sha256_file,
)
from stele.ledger.models import ArtifactState
from stele.ledger.store import ArtifactDriftError, LedgerStore
from stele.ledger.transaction import SandboxFailedError, ledger_transaction
from stele.replay.invalidation import auto_invalidate_drifted, invalidate_record
from stele.replay.models import InvalidationReason
from stele.replay.planner import plan_replay
from stele.replay.validator import validate_artifact

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON = os.path.realpath(os.environ.get("STELE_TEST_PYTHON", "/usr/bin/python3"))
needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap (bwrap) is not installed"
)


@pytest.fixture
def store(tmp_path: Path) -> LedgerStore:
    return LedgerStore(tmp_path / "ledger.db")


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    """A host file outside artifact_dir that a parser must never get hashed."""
    p = tmp_path / "host_secret.txt"
    p.write_text("host secret")
    return p


def _result(artifact_dir: Path, paths: list[Path], rejected: list[Path] | None = None) -> SandboxResult:
    return SandboxResult(
        run_id=uuid.uuid4(),
        exit_code=0,
        stdout="",
        stderr="",
        artifact_paths=paths,
        artifact_dir=artifact_dir,
        wall_time_seconds=0.1,
        rejected_paths=rejected or [],
    )


# ---------------------------------------------------------------------------
# Collection — what the runner picks up from artifact_dir
# ---------------------------------------------------------------------------

class TestCollectArtifacts:

    def test_regular_files_accepted_including_nested(self, artifact_dir: Path) -> None:
        (artifact_dir / "a.json").write_text("a")
        (artifact_dir / "sub").mkdir()
        (artifact_dir / "sub" / "b.json").write_text("b")

        accepted, rejected = collect_artifacts(artifact_dir)

        assert accepted == [artifact_dir / "a.json", artifact_dir / "sub" / "b.json"]
        assert rejected == []

    def test_file_symlink_rejected(self, artifact_dir: Path, secret: Path) -> None:
        (artifact_dir / "leak.txt").symlink_to(secret)
        accepted, rejected = collect_artifacts(artifact_dir)
        assert accepted == []
        assert rejected == [artifact_dir / "leak.txt"]

    def test_directory_symlink_rejected_and_not_descended(self, artifact_dir: Path, secret: Path) -> None:
        (artifact_dir / "leakdir").symlink_to(secret.parent, target_is_directory=True)
        accepted, rejected = collect_artifacts(artifact_dir)
        assert accepted == []
        assert rejected == [artifact_dir / "leakdir"]

    def test_dangling_symlink_rejected(self, artifact_dir: Path) -> None:
        (artifact_dir / "dangling").symlink_to("/does/not/exist")
        accepted, rejected = collect_artifacts(artifact_dir)
        assert accepted == []
        assert rejected == [artifact_dir / "dangling"]

    def test_fifo_rejected(self, artifact_dir: Path) -> None:
        os.mkfifo(artifact_dir / "pipe")
        accepted, rejected = collect_artifacts(artifact_dir)
        assert accepted == []
        assert rejected == [artifact_dir / "pipe"]


# ---------------------------------------------------------------------------
# Hashing — never follows a symlink, never blocks on a FIFO
# ---------------------------------------------------------------------------

class TestSafeHashing:

    def test_sha256_file_refuses_symlink(self, artifact_dir: Path, secret: Path) -> None:
        link = artifact_dir / "leak.txt"
        link.symlink_to(secret)
        with pytest.raises(UnsafeArtifactError):
            sha256_file(link)

    def test_sha256_file_refuses_fifo_without_blocking(self, artifact_dir: Path) -> None:
        pipe = artifact_dir / "pipe"
        os.mkfifo(pipe)
        with pytest.raises(UnsafeArtifactError):
            sha256_file(pipe)

    def test_build_manifest_refuses_symlink(self, artifact_dir: Path, secret: Path) -> None:
        link = artifact_dir / "leak.txt"
        link.symlink_to(secret)
        with pytest.raises(UnsafeArtifactError):
            build_manifest(artifact_dir, [link])

    def test_build_manifest_refuses_file_under_symlinked_dir(self, artifact_dir: Path, secret: Path) -> None:
        (artifact_dir / "leakdir").symlink_to(secret.parent, target_is_directory=True)
        with pytest.raises(UnsafeArtifactError):
            build_manifest(artifact_dir, [artifact_dir / "leakdir" / secret.name])

    @pytest.mark.parametrize("rel", ["../outside.txt", "/etc/passwd", "sub/../../outside.txt"])
    def test_resolve_artifact_refuses_escape(self, artifact_dir: Path, rel: str) -> None:
        with pytest.raises(UnsafeArtifactError):
            resolve_artifact(artifact_dir, rel)

    def test_create_pending_refuses_symlink(self, store: LedgerStore, artifact_dir: Path, secret: Path) -> None:
        link = artifact_dir / "leak.txt"
        link.symlink_to(secret)
        with pytest.raises(UnsafeArtifactError):
            store.create_pending(run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[link])


# ---------------------------------------------------------------------------
# Ledger — rejected runs cannot be ledgered; commit re-hashes
# ---------------------------------------------------------------------------

class TestLedgerBoundary:

    def test_run_with_rejected_paths_does_not_succeed(self, artifact_dir: Path) -> None:
        good = artifact_dir / "result.json"
        good.write_text("{}")
        result = _result(artifact_dir, [good], rejected=[artifact_dir / "leak.txt"])
        assert not result.succeeded

    def test_run_with_rejected_paths_cannot_be_ledgered(self, store: LedgerStore, artifact_dir: Path) -> None:
        good = artifact_dir / "result.json"
        good.write_text("{}")
        result = _result(artifact_dir, [good], rejected=[artifact_dir / "leak.txt"])
        with pytest.raises(SandboxFailedError):
            with ledger_transaction(store, result):
                pass
        assert store.list_by_states(list(ArtifactState)) == []

    def test_commit_refuses_content_changed_after_hashing(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = artifact_dir / "result.json"
        p.write_text("original")
        record = store.create_pending(run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p])

        p.write_text("tampered")

        with pytest.raises(ArtifactDriftError):
            store.commit(record.record_id)
        assert store.get(record.record_id).state is ArtifactState.PENDING

    def test_commit_refuses_file_swapped_for_symlink(
        self, store: LedgerStore, artifact_dir: Path, secret: Path
    ) -> None:
        p = artifact_dir / "result.json"
        p.write_text("original")
        record = store.create_pending(run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p])

        p.unlink()
        p.symlink_to(secret)

        with pytest.raises(ArtifactDriftError):
            store.commit(record.record_id)

    def test_transaction_fails_record_when_content_changes_inside_block(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = artifact_dir / "result.json"
        p.write_text("original")
        with pytest.raises(ArtifactDriftError):
            with ledger_transaction(store, _result(artifact_dir, [p])) as record:
                p.write_text("tampered")
        assert store.get(record.record_id).state is ArtifactState.PENDING


# ---------------------------------------------------------------------------
# Validator — a symlink swapped in after commit is drift, never followed
# ---------------------------------------------------------------------------

class TestValidatorBoundary:

    def test_symlink_swap_after_commit_is_drift(self, store: LedgerStore, artifact_dir: Path, secret: Path) -> None:
        p = artifact_dir / "result.json"
        p.write_text("original")
        with ledger_transaction(store, _result(artifact_dir, [p])) as record:
            pass

        p.unlink()
        p.symlink_to(secret)

        result = validate_artifact(store.get(record.record_id))
        assert result.status == "drift"
        assert result.drifted_files == ("result.json",)

    def test_directory_in_place_of_file_is_drift(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = artifact_dir / "result.json"
        p.write_text("original")
        with ledger_transaction(store, _result(artifact_dir, [p])) as record:
            pass

        p.unlink()
        p.mkdir()

        assert validate_artifact(store.get(record.record_id)).status == "drift"


# ---------------------------------------------------------------------------
# Ledger correctness fixes
# ---------------------------------------------------------------------------

class TestDuplicateIgnoreDoesNotMaskErrors:

    def test_exception_in_block_propagates_unmasked(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = artifact_dir / "result.json"
        p.write_text("same bytes")
        with ledger_transaction(store, _result(artifact_dir, [p])) as first:
            pass

        with pytest.raises(RuntimeError, match="adapter boom"):
            with ledger_transaction(store, _result(artifact_dir, [p]), duplicate_policy="ignore") as dup:
                assert dup.record_id == first.record_id
                raise RuntimeError("adapter boom")

        assert store.get(first.record_id).state is ArtifactState.COMMITTED

    def test_clean_exit_leaves_existing_record_untouched(self, store: LedgerStore, artifact_dir: Path) -> None:
        p = artifact_dir / "result.json"
        p.write_text("same bytes")
        with ledger_transaction(store, _result(artifact_dir, [p])) as first:
            pass
        invalidate_record(store, first.record_id, InvalidationReason.MANUAL)

        with ledger_transaction(store, _result(artifact_dir, [p]), duplicate_policy="ignore") as dup:
            assert dup.state is ArtifactState.INVALIDATED

        assert store.get(first.record_id).state is ArtifactState.INVALIDATED


class TestAutoInvalidateSkipsInvalidated:

    def test_batch_with_already_invalidated_candidate_completes(
        self, store: LedgerStore, tmp_path: Path
    ) -> None:
        records = []
        for name in ("a", "b"):
            d = tmp_path / name
            d.mkdir()
            p = d / "result.json"
            p.write_text(name)
            with ledger_transaction(store, _result(d, [p])) as rec:
                pass
            p.write_text("drifted " + name)
            records.append(rec)
        invalidate_record(store, records[0].record_id, InvalidationReason.MANUAL)

        plan = plan_replay(store, include_invalidated=True)
        assert plan.drifted_count == 2

        invalidated = auto_invalidate_drifted(store, plan.drifted)

        assert [r.record_id for r in invalidated] == [records[1].record_id]
        assert store.get(records[1].record_id).state is ArtifactState.INVALIDATED


# ---------------------------------------------------------------------------
# Runner / sandbox
# ---------------------------------------------------------------------------

class TestRunnerBoundary:

    def test_non_empty_artifact_dir_refused(self, artifact_dir: Path) -> None:
        (artifact_dir / "stale.json").write_text("from a previous run")
        with pytest.raises(ValueError, match="not empty"):
            run_in_sandbox(SandboxConfig(command=["true"], artifact_dir=artifact_dir))

    def test_argv_starts_new_session(self, artifact_dir: Path) -> None:
        argv = BubblewrapSandbox().build_argv(SandboxConfig(command=["true"], artifact_dir=artifact_dir))
        assert "--new-session" in argv

    @needs_bwrap
    def test_parser_planted_symlinks_are_rejected_end_to_end(
        self, store: LedgerStore, tmp_path: Path, secret: Path
    ) -> None:
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / "out",
            script_path=FIXTURES / "parser_emit_symlink.py",
            env={"STELE_TEST_SYMLINK_TARGET": str(secret)},
        )
        result = run_in_sandbox(config)

        assert result.exit_code == 0, result.stderr
        assert not result.succeeded
        assert [p.name for p in result.artifact_paths] == ["result.json"]
        assert sorted(p.name for p in result.rejected_paths) == ["dangling", "leak.txt", "leakdir"]

        with pytest.raises(SandboxFailedError):
            with ledger_transaction(store, result):
                pass
        assert store.list_by_states(list(ArtifactState)) == []
