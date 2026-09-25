"""
Regressions for the three-way code review of the hardening stack.

  1. An unreadable directory in parser output fails collection instead of
     being skipped (partial bundles must never be recorded as complete).
  2. ledger_transaction marks the record FAILED when seal-time verification
     fails or the block is interrupted, instead of leaving it PENDING.
  3. Ledger state transitions are conditional on the state that was checked,
     so a racing seal cannot overwrite an invalidation.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

import stele.ledger.store as store_module
from stele.containment.artifacts import UnsafeArtifactError, collect_artifact_paths
from stele.containment.result import SandboxResult
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig
from stele.ledger.models import ArtifactState
from stele.ledger.store import (
    ArtifactDriftError,
    InvalidStateTransitionError,
    LedgerStore,
)
from stele.ledger.transaction import ledger_transaction
from tests.ledger_helpers import PROVENANCE, open_ledger

PYTHON = str(Path(sys.executable).resolve())
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
requires_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="live containment proof requires Linux bubblewrap",
)
requires_non_root = pytest.mark.skipif(
    IS_ROOT or not hasattr(os, "geteuid"),
    reason="root ignores directory permissions; needs a POSIX non-root user",
)


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


def _result(artifact_dir: Path) -> SandboxResult:
    return SandboxResult(
        run_id=uuid.uuid4(),
        exit_code=0,
        stdout="",
        stderr="",
        artifact_paths=sorted(p for p in artifact_dir.iterdir() if p.is_file()),
        artifact_dir=artifact_dir,
        wall_time_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# 1 — Unreadable output directories fail closed
# ---------------------------------------------------------------------------

class TestUnreadableOutputDirectory:

    def test_listing_error_raises_instead_of_skipping(
        self, artifact_dir: Path, monkeypatch
    ) -> None:
        (artifact_dir / "visible.json").write_text("{}")
        hidden = artifact_dir / "hidden"
        hidden.mkdir()
        (hidden / "part.json").write_text("{}")

        real_scandir = os.scandir

        def scandir(path="."):
            if Path(path) == hidden:
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)
        with pytest.raises(UnsafeArtifactError, match="unreadable"):
            collect_artifact_paths(artifact_dir)

    @requires_non_root
    def test_chmod_000_subdirectory_is_refused(self, artifact_dir: Path) -> None:
        (artifact_dir / "visible.json").write_text("{}")
        hidden = artifact_dir / "hidden"
        hidden.mkdir()
        (hidden / "part.json").write_text("{}")
        hidden.chmod(0)
        try:
            with pytest.raises(UnsafeArtifactError, match="unreadable"):
                collect_artifact_paths(artifact_dir)
        finally:
            hidden.chmod(0o700)

    @requires_bwrap
    @requires_non_root
    def test_parser_cannot_hide_output_with_permissions(self, tmp_path: Path) -> None:
        probe = (
            "import os\n"
            "out = os.environ['STELE_OUTPUT_DIR']\n"
            "open(os.path.join(out, 'visible.json'), 'w').write('{}')\n"
            "os.mkdir(os.path.join(out, 'hidden'))\n"
            "open(os.path.join(out, 'hidden', 'part.json'), 'w').write('{}')\n"
            "os.chmod(os.path.join(out, 'hidden'), 0)\n"
        )
        out = tmp_path / "out"
        try:
            with pytest.raises(UnsafeArtifactError, match="unreadable"):
                run_in_sandbox(SandboxConfig(command=[PYTHON, "-c", probe], artifact_dir=out))
        finally:
            if (out / "hidden").exists():
                (out / "hidden").chmod(0o700)


# ---------------------------------------------------------------------------
# 2 — The transaction never leaves its record PENDING
# ---------------------------------------------------------------------------

class TestTransactionFinalizesRecord:

    def test_seal_failure_marks_record_failed(
        self, tmp_path: Path, artifact_dir: Path
    ) -> None:
        store = open_ledger(tmp_path / "ledger.db")
        (artifact_dir / "result.json").write_text("original")

        with pytest.raises(ArtifactDriftError):
            with ledger_transaction(store, _result(artifact_dir), **PROVENANCE) as record:
                # The bundle drifts before it is sealed.
                (artifact_dir / "result.json").write_text("tampered")

        final = store.get(record.record_id)
        assert final.state is ArtifactState.FAILED
        assert "ArtifactDriftError" in (final.error or "")

    def test_keyboard_interrupt_in_block_marks_record_failed(
        self, tmp_path: Path, artifact_dir: Path
    ) -> None:
        store = open_ledger(tmp_path / "ledger.db")
        (artifact_dir / "result.json").write_text("data")

        with pytest.raises(KeyboardInterrupt):
            with ledger_transaction(store, _result(artifact_dir), **PROVENANCE) as record:
                raise KeyboardInterrupt

        assert store.get(record.record_id).state is ArtifactState.FAILED

    def test_seal_failure_after_concurrent_invalidation_keeps_original_error(
        self, tmp_path: Path, artifact_dir: Path
    ) -> None:
        db = tmp_path / "ledger.db"
        store = open_ledger(db)
        other = open_ledger(db)
        (artifact_dir / "result.json").write_text("data")

        with pytest.raises(InvalidStateTransitionError):
            with ledger_transaction(store, _result(artifact_dir), **PROVENANCE) as record:
                other.invalidate(record.record_id, "superseded")

        assert store.get(record.record_id).state is ArtifactState.INVALIDATED


# ---------------------------------------------------------------------------
# 3 — State transitions cannot overwrite a concurrent transition
# ---------------------------------------------------------------------------

class TestConditionalTransitions:

    def _pending(self, store: LedgerStore, artifact_dir: Path) -> str:
        p = artifact_dir / f"r_{uuid.uuid4().hex[:8]}.json"
        p.write_text(uuid.uuid4().hex)
        rec = store.create_pending(
            **PROVENANCE,
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        return rec.record_id

    def test_seal_racing_invalidation_does_not_resurrect_record(
        self, tmp_path: Path, artifact_dir: Path, monkeypatch
    ) -> None:
        db = tmp_path / "ledger.db"
        a, b = open_ledger(db), open_ledger(db)
        record_id = self._pending(a, artifact_dir)

        real_archive = store_module.archive_bundle

        def archive_then_race(*args, **kwargs):
            # seal() has already checked state == pending; another connection
            # invalidates before seal() writes.
            b.invalidate(record_id, "superseded mid-seal")
            return real_archive(*args, **kwargs)

        monkeypatch.setattr(store_module, "archive_bundle", archive_then_race)
        with pytest.raises(InvalidStateTransitionError, match="concurrently"):
            a.seal(record_id)

        assert a.get(record_id).state is ArtifactState.INVALIDATED
        assert b.get(record_id).state is ArtifactState.INVALIDATED

    def test_stale_invalidate_after_fail_is_refused(
        self, tmp_path: Path, artifact_dir: Path, monkeypatch
    ) -> None:
        db = tmp_path / "ledger.db"
        a, b = open_ledger(db), open_ledger(db)
        record_id = self._pending(a, artifact_dir)

        real_require = LedgerStore._require
        raced = {"done": False}

        def require_then_race(self, rid):
            rec = real_require(self, rid)
            if self is a and not raced["done"]:
                raced["done"] = True
                b.fail(rid, "parser failed")
            return rec

        monkeypatch.setattr(LedgerStore, "_require", require_then_race)
        with pytest.raises(InvalidStateTransitionError, match="concurrently"):
            a.invalidate(record_id, "late")

        assert b.get(record_id).state is ArtifactState.FAILED

    def test_normal_transitions_still_work(self, tmp_path: Path, artifact_dir: Path) -> None:
        store = open_ledger(tmp_path / "ledger.db")
        sealed = self._pending(store, artifact_dir)
        store.seal(sealed)
        store.invalidate(sealed, "stale")
        assert store.get(sealed).state is ArtifactState.INVALIDATED

        failed = self._pending(store, artifact_dir)
        store.fail(failed, "boom")
        assert store.get(failed).state is ArtifactState.FAILED


# ---------------------------------------------------------------------------
# Second review round (commit b4bbb08)
# ---------------------------------------------------------------------------

class TestOriginalErrorIsNeverMasked:

    def test_database_error_in_fail_does_not_replace_block_error(
        self, tmp_path: Path, artifact_dir: Path, monkeypatch
    ) -> None:
        import sqlite3

        store = open_ledger(tmp_path / "ledger.db")
        (artifact_dir / "result.json").write_text("data")

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        class AdapterError(RuntimeError):
            pass

        with pytest.raises(AdapterError):
            with ledger_transaction(store, _result(artifact_dir), **PROVENANCE):
                monkeypatch.setattr(store, "fail", locked)
                raise AdapterError("downstream write failed")

    def test_read_back_failure_after_durable_seal_is_not_a_failure(
        self, tmp_path: Path, artifact_dir: Path, monkeypatch
    ) -> None:
        store = open_ledger(tmp_path / "ledger.db")
        (artifact_dir / "result.json").write_text("data")
        real_seal = store.seal

        def seal_then_readback_fails(record_id):
            real_seal(record_id)
            raise RuntimeError("read-back failed after SEALED was written")

        monkeypatch.setattr(store, "seal", seal_then_readback_fails)
        with ledger_transaction(store, _result(artifact_dir), **PROVENANCE) as record:
            pass

        assert store.get(record.record_id).state is ArtifactState.SEALED

    def test_interrupt_after_durable_seal_still_propagates(
        self, tmp_path: Path, artifact_dir: Path, monkeypatch
    ) -> None:
        store = open_ledger(tmp_path / "ledger.db")
        (artifact_dir / "result.json").write_text("data")
        real_seal = store.seal

        def seal_then_interrupt(record_id):
            real_seal(record_id)
            raise KeyboardInterrupt

        monkeypatch.setattr(store, "seal", seal_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            with ledger_transaction(store, _result(artifact_dir), **PROVENANCE) as record:
                pass
        assert store.get(record.record_id).state is ArtifactState.SEALED


class TestUnsearchableOutputDirectory:

    def test_lstat_error_becomes_unsafe_artifact_error(
        self, artifact_dir: Path, monkeypatch
    ) -> None:
        sub = artifact_dir / "sub"
        sub.mkdir()
        (sub / "f.json").write_text("{}")
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(path) == sub / "f.json":
                raise PermissionError(13, "Permission denied", str(path))
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)
        with pytest.raises(UnsafeArtifactError, match="unreadable"):
            collect_artifact_paths(artifact_dir)

    @requires_non_root
    def test_chmod_0444_subdirectory_is_refused(self, artifact_dir: Path) -> None:
        sub = artifact_dir / "sub"
        sub.mkdir()
        (sub / "f.json").write_text("{}")
        sub.chmod(0o444)
        try:
            with pytest.raises(UnsafeArtifactError, match="unreadable"):
                collect_artifact_paths(artifact_dir)
        finally:
            sub.chmod(0o700)
