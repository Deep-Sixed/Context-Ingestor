"""
Follow-up hardening regressions on top of the containment baseline.

  1. A FIFO planted in place of an artifact is rejected, never waited on.
  2. The sandbox runs in its own session with no stdin (no TIOCSTI injection).
  3. A non-empty artifact_dir is refused before the parser runs.
  4. duplicate_policy="ignore" never finalizes a record it did not create,
     so errors raised in the block propagate unchanged.
  5. Bulk invalidation skips records that are already INVALIDATED.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from stele.containment.result import SandboxResult
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import BubblewrapSandbox, SandboxConfig
from stele.ledger.hashing import UnsafeFileError, sha256_file_beneath
from stele.ledger.models import ArtifactState
from stele.ledger.store import ArtifactDriftError, LedgerStore
from stele.ledger.transaction import ledger_transaction
from stele.replay.invalidation import auto_invalidate_drifted, invalidate_record
from stele.replay.models import InvalidationReason
from stele.replay.planner import plan_replay
from stele.replay.validator import validate_artifact

PYTHON = str(Path(sys.executable).resolve())
requires_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="live containment proof requires Linux bubblewrap",
)
requires_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")


@pytest.fixture
def store(tmp_path: Path) -> LedgerStore:
    return LedgerStore(tmp_path / "ledger.db")


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


def _within(seconds: float, fn):
    """Run fn in a daemon thread so a regression fails instead of hanging the suite."""
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            outcome["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        pytest.fail(f"call blocked for more than {seconds}s (FIFO open hang)")
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome.get("value")


def _commit(store: LedgerStore, artifact_dir: Path, name: str, content: str) -> str:
    p = artifact_dir / name
    p.write_text(content)
    rec = store.create_pending(
        run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
    )
    store.commit(rec.record_id)
    return rec.record_id


def _result(artifact_dir: Path) -> SandboxResult:
    return SandboxResult(
        run_id=uuid.uuid4(),
        exit_code=0,
        stdout="",
        stderr="",
        artifact_paths=sorted(artifact_dir.iterdir()),
        artifact_dir=artifact_dir,
        wall_time_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# 1 — FIFOs are rejected without blocking
# ---------------------------------------------------------------------------

@requires_fifo
class TestFifoNeverBlocks:

    def test_hashing_a_fifo_raises_instead_of_blocking(self, artifact_dir: Path) -> None:
        os.mkfifo(artifact_dir / "pipe")
        with pytest.raises(UnsafeFileError):
            _within(5, lambda: sha256_file_beneath(artifact_dir, Path("pipe")))

    def test_validator_reports_drift_for_fifo_replacement(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        record_id = _commit(store, artifact_dir, "result.json", "original")
        (artifact_dir / "result.json").unlink()
        os.mkfifo(artifact_dir / "result.json")

        result = _within(5, lambda: validate_artifact(store.get(record_id)))
        assert result.status == "drift"
        assert "result.json" in result.drifted_files

    def test_commit_refuses_fifo_replacement(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        p = artifact_dir / "result.json"
        p.write_text("original")
        rec = store.create_pending(
            run_id=str(uuid.uuid4()), artifact_dir=artifact_dir, artifact_paths=[p]
        )
        p.unlink()
        os.mkfifo(p)

        with pytest.raises(ArtifactDriftError):
            _within(5, lambda: store.commit(rec.record_id))
        assert store.get(rec.record_id).state is ArtifactState.PENDING


# ---------------------------------------------------------------------------
# 2 — Own session, no stdin
# ---------------------------------------------------------------------------

class TestSessionIsolation:

    def test_argv_requests_new_session_and_cgroup_namespace(self, tmp_path: Path) -> None:
        argv = BubblewrapSandbox().build_argv(
            SandboxConfig(command=["/usr/bin/true"], artifact_dir=tmp_path / "out")
        )
        assert "--new-session" in argv
        assert "--unshare-cgroup-try" in argv
        assert argv.index("--new-session") < argv.index("--")

    def test_runner_gives_parser_no_stdin(self, tmp_path: Path, monkeypatch) -> None:
        seen: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        run_in_sandbox(SandboxConfig(command=["/usr/bin/true"], artifact_dir=tmp_path / "out"))
        assert seen["stdin"] is subprocess.DEVNULL

    @requires_bwrap
    def test_parser_is_session_leader_without_stdin(self, tmp_path: Path) -> None:
        probe = (
            "import json, os, sys\n"
            "data = sys.stdin.read()\n"
            "out = {'sid': os.getsid(0), 'pid': os.getpid(), 'stdin': data}\n"
            "open(os.path.join(os.environ['STELE_OUTPUT_DIR'], 'probe.json'), 'w')"
            ".write(json.dumps(out))\n"
        )
        out = tmp_path / "out"
        result = run_in_sandbox(SandboxConfig(command=[PYTHON, "-c", probe], artifact_dir=out))
        assert result.succeeded, result.stderr

        seen = json.loads((out / "probe.json").read_text())
        # getsid() reports 0 when the session leader lives outside the PID
        # namespace, i.e. the parser still shares the caller's terminal session.
        assert seen["sid"] != 0, "parser must run in a session created inside the sandbox"
        assert seen["stdin"] == ""


# ---------------------------------------------------------------------------
# 3 — Stale output is refused
# ---------------------------------------------------------------------------

class TestFreshOutputDirectory:

    def test_non_empty_artifact_dir_is_refused_before_running(
        self, artifact_dir: Path, monkeypatch
    ) -> None:
        (artifact_dir / "stale.json").write_text("{}")

        def must_not_run(*args, **kwargs):
            raise AssertionError("sandbox must not start with a non-empty artifact_dir")

        monkeypatch.setattr(subprocess, "run", must_not_run)
        with pytest.raises(ValueError, match="not empty"):
            run_in_sandbox(SandboxConfig(command=["/usr/bin/true"], artifact_dir=artifact_dir))

    def test_empty_existing_artifact_dir_is_allowed(
        self, artifact_dir: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
        )
        result = run_in_sandbox(SandboxConfig(command=["/usr/bin/true"], artifact_dir=artifact_dir))
        assert result.artifact_paths == []


# ---------------------------------------------------------------------------
# 4 — duplicate_policy="ignore" leaves the existing record alone
# ---------------------------------------------------------------------------

class TestDuplicateIgnoreTransaction:

    def test_block_error_propagates_and_existing_record_untouched(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        (artifact_dir / "result.json").write_text("same bytes")
        with ledger_transaction(store, _result(artifact_dir)) as first:
            pass
        assert store.get(first.record_id).state is ArtifactState.COMMITTED

        class AdapterError(RuntimeError):
            pass

        with pytest.raises(AdapterError):
            with ledger_transaction(
                store, _result(artifact_dir), duplicate_policy="ignore"
            ) as existing:
                assert existing.record_id == first.record_id
                raise AdapterError("downstream write failed")

        assert store.get(first.record_id).state is ArtifactState.COMMITTED

    def test_clean_exit_does_not_recommit_existing_record(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        (artifact_dir / "result.json").write_text("same bytes")
        with ledger_transaction(store, _result(artifact_dir)) as first:
            pass

        with ledger_transaction(
            store, _result(artifact_dir), duplicate_policy="ignore"
        ) as existing:
            assert existing.record_id == first.record_id

        assert store.get(first.record_id).state is ArtifactState.COMMITTED


# ---------------------------------------------------------------------------
# 5 — Bulk invalidation skips already-invalidated records
# ---------------------------------------------------------------------------

class TestBulkInvalidation:

    def test_already_invalidated_candidates_are_skipped(
        self, store: LedgerStore, artifact_dir: Path
    ) -> None:
        done = _commit(store, artifact_dir, "a.json", "a")
        pending = _commit(store, artifact_dir, "b.json", "b")
        (artifact_dir / "a.json").write_text("a drifted")
        (artifact_dir / "b.json").write_text("b drifted")
        invalidate_record(store, done, InvalidationReason.MANUAL, note="already handled")

        plan = plan_replay(store, include_invalidated=True)
        assert len(plan.drifted) == 2

        invalidated = auto_invalidate_drifted(store, plan.drifted)

        assert [r.record_id for r in invalidated] == [pending]
        assert store.get(pending).state is ArtifactState.INVALIDATED
        assert store.get(done).state is ArtifactState.INVALIDATED
