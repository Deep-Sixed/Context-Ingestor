"""
Stele Phase F — pending → committed / failed protocol.

The ledger_transaction context manager is the canonical way to record a
sandboxed parse run.  It enforces the four-step invariant:

  1. artifact was emitted through /stele/output   ← caller passes SandboxResult
  2. artifact is hashed                           ← create_pending does this
  3. artifact is recorded as pending              ← create_pending
  4. commit is explicitly finalized               ← context exit (or fail on exc)

Usage::

    result = run_in_sandbox(config)

    with ledger_transaction(store, result) as record:
        # record.state is PENDING here
        # ... downstream adapter writes happen here (Phase H) ...
        pass  # clean exit → committed

    # record.state is now COMMITTED

On any exception inside the block, the record is marked FAILED and the
exception re-raised.  No partial state reaches downstream.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from ..containment.result import SandboxResult
from .models import ArtifactRecord
from .store import LedgerStore


class SandboxFailedError(Exception):
    """Raised when attempting to ledger a failed sandbox run."""


@contextmanager
def ledger_transaction(
    store: LedgerStore,
    sandbox_result: SandboxResult,
    *,
    source_path: str | None = None,
    source_hash: str | None = None,
    duplicate_policy: str = "raise",
) -> Generator[ArtifactRecord, None, None]:
    """Context manager wrapping a SandboxResult in a ledger transaction.

    On entry:
      - Rejects failed sandbox runs immediately (SandboxFailedError).
      - Creates a PENDING ledger record.

    On clean exit:
      - Commits the record (PENDING → COMMITTED).

    On any exception inside the block:
      - Marks the record FAILED with the exception message.
      - Re-raises the original exception.

    Yields the ArtifactRecord in PENDING state so callers can inspect
    it (e.g. to pass record_id to a downstream adapter) before committing.
    """
    if not sandbox_result.succeeded:
        raise SandboxFailedError(
            f"run_id={sandbox_result.run_id} failed "
            f"(exit_code={sandbox_result.exit_code}, "
            f"timed_out={sandbox_result.timed_out}) — "
            "cannot create ledger record for a failed run"
        )

    if not sandbox_result.produced_artifacts:
        raise SandboxFailedError(
            f"run_id={sandbox_result.run_id} produced no artifacts — "
            "nothing to ledger"
        )

    record = store.create_pending(
        run_id=str(sandbox_result.run_id),
        artifact_dir=sandbox_result.artifact_dir,
        artifact_paths=sandbox_result.artifact_paths,
        source_path=source_path,
        source_hash=source_hash,
        duplicate_policy=duplicate_policy,
    )

    try:
        yield record
    except Exception as exc:
        store.fail(record.record_id, error=repr(exc))
        raise

    store.commit(record.record_id)
