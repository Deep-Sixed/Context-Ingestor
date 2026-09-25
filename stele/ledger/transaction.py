"""
Stele — pending → sealed / failed protocol.

The ledger_transaction context manager is the canonical way to record a
sandboxed parse run.  It enforces the invariant:

  1. artifact was emitted through /stele/output   ← caller passes SandboxResult
  2. artifact is hashed                           ← create_pending does this
  3. artifact is recorded as pending              ← create_pending
  4. the bundle is archived, verified and sealed  ← context exit (or fail on exc)

Usage::

    archive = BlobStore(archive_root)
    result = run_in_sandbox(config, store=archive)
    parser = ParserIdentity("mineru", "1.3.1")

    with ledger_transaction(ledger, result, parser=parser, parser_config=cfg) as record:
        # record.state is PENDING here; run any checks that must pass
        # before the output counts as evidence.
        pass  # clean exit → sealed

    # record.state is now SEALED. Delivery to downstream targets happens
    # after this, from the sealed record (roadmap #13), not inside the block.

On any exception inside the block, the record is marked FAILED and the
exception re-raised.  record_run() is the same without a block.

Input and parser provenance come from the SandboxResult, not from the caller:
the input Snapshot digest (the bytes the parser saw) becomes source_hash, and
the image digest or Wasm module hash completes the ParserIdentity.
"""
from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from typing import Any, Generator, Mapping

from ..archive.records import Source
from ..containment.result import SandboxResult
from .models import ArtifactRecord, ArtifactState, ParserIdentity, RunConditions
from .store import LedgerStore, ProvenanceError


class SandboxFailedError(Exception):
    """Raised when attempting to ledger a failed sandbox run."""


def measured_parser_identity(
    parser: ParserIdentity, sandbox_result: SandboxResult
) -> ParserIdentity:
    """parser completed with the executable digests the run measured.

    A digest the caller set must agree with the one the run reports.
    """
    measured = {
        "image_digest": sandbox_result.image_digest,
        "module_sha256": sandbox_result.module_sha256,
    }
    for field_name, value in measured.items():
        claimed = getattr(parser, field_name)
        if claimed is not None and claimed != value:
            raise ProvenanceError(
                f"parser {field_name} {claimed!r} differs from the one the run "
                f"measured ({value!r})"
            )
    return dataclasses.replace(parser, **measured)


def _check_input(sandbox_result: SandboxResult) -> None:
    snapshot = sandbox_result.input_snapshot
    if sandbox_result.input_sha256 is None:
        if snapshot is not None:
            raise ProvenanceError("run reports an input Snapshot but no staged input")
        return
    if snapshot is None:
        raise ProvenanceError(
            f"run_id={sandbox_result.run_id} read an input that was not archived — "
            "run it with run_in_sandbox(config, store=<the ledger's archive>)"
        )
    if snapshot.digest != sandbox_result.input_sha256:
        raise ProvenanceError(
            f"input Snapshot {snapshot.digest[:12]}… differs from the staged input "
            f"hash {sandbox_result.input_sha256[:12]}…"
        )


# RunConditions field → the key under which a backend reports the limit it
# applied (ExecutionOutcome.limits, carried as SandboxResult.telemetry.limits).
_APPLIED_LIMITS = {
    "memory": "memory",
    "cpus": "cpus",
    "pids_limit": "pids",
    "timeout_seconds": "timeout_seconds",
}


def _check_run_conditions(
    sandbox_result: SandboxResult, run_conditions: RunConditions | None
) -> None:
    """Refuse run conditions the backend does not confirm it applied.

    A replay runs the parser under the recorded conditions, so they must be
    what the run executed under, not what a caller says it did. Every stated
    condition is compared with the limits the backend reported; one it did
    not report cannot be vouched for and is refused too.
    """
    if run_conditions is None:
        return
    telemetry = sandbox_result.telemetry
    applied = dict(telemetry.limits) if telemetry is not None else {}
    problems: list[str] = []

    if "gpu" in applied:
        ran_on = "gpu" if applied["gpu"] else "cpu"
        if ran_on != run_conditions.device:
            problems.append(f"device {run_conditions.device!r}, but the run used the {ran_on}")
    elif run_conditions.device == "gpu":
        problems.append("device 'gpu', but the backend reports no GPU")

    for field_name, key in _APPLIED_LIMITS.items():
        stated = getattr(run_conditions, field_name)
        if stated is None:
            continue
        if key not in applied:
            problems.append(f"{field_name}={stated!r}, which the backend does not report applying")
        elif not _same_limit(stated, applied[key]):
            problems.append(f"{field_name}={stated!r}, but the backend applied {applied[key]!r}")

    if problems:
        raise ProvenanceError(
            f"run_id={sandbox_result.run_id}: the stated run conditions are not the ones "
            "the run executed under: " + "; ".join(problems)
        )


def _same_limit(stated: Any, applied: Any) -> bool:
    if isinstance(stated, float) and isinstance(applied, (int, float)) and not isinstance(applied, bool):
        return float(applied) == stated
    return type(stated) is type(applied) and stated == applied


@contextmanager
def ledger_transaction(
    store: LedgerStore,
    sandbox_result: SandboxResult,
    *,
    parser: ParserIdentity,
    parser_config: Mapping[str, Any],
    source: Source | None = None,
    run_conditions: RunConditions | None = None,
) -> Generator[ArtifactRecord, None, None]:
    """Context manager wrapping a SandboxResult in a ledger transaction.

    On entry:
      - Rejects failed sandbox runs immediately (SandboxFailedError).
      - Rejects runs whose input was not archived (ProvenanceError).
      - Creates a PENDING ledger record for this run.

    On clean exit:
      - Seals the record (PENDING → SEALED).

    On any exception inside the block, or if sealing fails:
      - Marks the record FAILED with the exception message.
      - Re-raises the original exception.

    run_conditions is the device and limits Stele's runner applied to the
    run (stele.parsers.replay.record_parser_run passes them from the
    ParserRun); a replay uses them to run the parser the same way. Each one
    must match the limits the backend reported applying
    (sandbox_result.telemetry.limits), or ProvenanceError is raised and
    nothing is recorded.

    Yields the ArtifactRecord in PENDING state.
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

    _check_input(sandbox_result)
    _check_run_conditions(sandbox_result, run_conditions)
    identity = measured_parser_identity(parser, sandbox_result)

    record = store.create_pending(
        run_id=str(sandbox_result.run_id),
        artifact_dir=sandbox_result.artifact_dir,
        artifact_paths=sandbox_result.artifact_paths,
        parser=identity,
        parser_config=parser_config,
        input_snapshot=sandbox_result.input_snapshot,
        source=source,
        backend=sandbox_result.backend,
        run_conditions=run_conditions,
    )

    if (
        sandbox_result.artifact_bundle_digest is not None
        and sandbox_result.artifact_bundle_digest != record.artifact_hash
    ):
        exc = ProvenanceError(
            "artifact bundle changed between the run archiving it and the ledger "
            "recording it"
        )
        _fail_quietly(store, record.record_id, exc)
        raise exc

    try:
        yield record
    except BaseException as exc:
        # BaseException: a Ctrl-C or SystemExit inside the block must not leave
        # the record PENDING forever.
        _fail_quietly(store, record.record_id, exc)
        raise

    try:
        store.seal(record.record_id)
    except BaseException as exc:
        if isinstance(exc, Exception) and _state_or_none(store, record.record_id) is (
            ArtifactState.SEALED
        ):
            # The SEALED write is durable; only the read-back after it failed.
            return
        # The bundle failed seal-time verification: record that outcome
        # explicitly instead of leaving the record PENDING.
        _fail_quietly(store, record.record_id, exc)
        raise


def record_run(
    store: LedgerStore,
    sandbox_result: SandboxResult,
    *,
    parser: ParserIdentity,
    parser_config: Mapping[str, Any],
    source: Source | None = None,
    run_conditions: RunConditions | None = None,
) -> ArtifactRecord:
    """Record one run and seal it; return the SEALED record."""
    with ledger_transaction(
        store, sandbox_result, parser=parser, parser_config=parser_config, source=source,
        run_conditions=run_conditions,
    ) as record:
        pass
    return store.get(record.record_id)


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
