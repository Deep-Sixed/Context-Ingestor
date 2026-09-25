"""
Stele ledger — pending → committed / failed protocol.

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
        # ... downstream adapter writes happen here (see contracts/) ...
        pass  # clean exit → committed

    # record.state is now COMMITTED

On any exception inside the block, the record is marked FAILED and the
exception re-raised.  No partial state reaches downstream.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from ..containment.result import SandboxResult
from .models import ArtifactRecord, ArtifactState
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

    With duplicate_policy="ignore", a duplicate yields the existing record
    unchanged: this transaction did not create it, so it neither commits nor
    fails it, and exceptions from the block propagate untouched.
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

    if record.run_id != str(sandbox_result.run_id):
        # Existing record returned by duplicate_policy="ignore" — not ours to finalize.
        yield record
        return

    try:
        yield record
    except BaseException as exc:
        # BaseException: a Ctrl-C or SystemExit inside the block must not leave
        # the record PENDING forever.
        _fail_quietly(store, record.record_id, exc)
        raise

    try:
        store.commit(record.record_id)
    except BaseException as exc:
        if isinstance(exc, Exception) and _state_or_none(store, record.record_id) is (
            ArtifactState.COMMITTED
        ):
            # The COMMITTED write is durable; only the read-back after it
            # failed. Reporting failure here would make callers undo downstream
            # work the ledger records as committed.
            return
        # The block already ran (downstream writes may exist) but the bundle
        # failed commit-time verification: record that outcome explicitly.
        _fail_quietly(store, record.record_id, exc)
        raise


def _state_or_none(store: LedgerStore, record_id: str) -> ArtifactState | None:
    try:
        return store.get(record_id).state
    except Exception:
        return None


def _fail_quietly(store: LedgerStore, record_id: str, exc: BaseException) -> None:
    """Mark record FAILED without masking the original exception.

    Any error from fail() itself — the record already left PENDING, or the
    database is locked/unavailable — is swallowed so the caller always sees
    the original exception. In the second case the record may stay PENDING.
    """
    try:
        store.fail(record_id, error=repr(exc))
    except Exception:
        pass
