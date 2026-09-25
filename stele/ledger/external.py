"""
Stele — recording artifacts produced outside a Stele sandbox.

ledger_transaction / record_run record a sandboxed parser run: Stele ran the
parser, measured its identity, archived its input and captured its output.
Some evidence is produced elsewhere, by an application that wants the result
kept with the same guarantees (AgentSync's promoted SKILL.md files are the
first such caller). record_external_artifact is the supported way to ledger
it; callers should not drive create_pending / seal themselves.

Usage::

    archive = BlobStore(archive_root)
    ledger = LedgerStore(db_path, archive)

    record = record_external_artifact(
        ledger,
        run_id=run_id,                                   # the caller's UUID
        artifact_dir=out_dir,
        artifact_paths=[out_dir / "SKILL.md"],
        producer=ParserIdentity("agentsync-kanon", "0.1.0"),
        producer_config={"validation_level": "promote"},
    )
    assert record.state is ArtifactState.SEALED

What the record claims, and what it does not:

  - The bundle is hashed, archived and verified exactly as for a sandbox run,
    so SEALED means the same thing: the archive holds these bytes.
  - The producer is asserted by the caller, not measured. The record's
    backend is EXTERNAL_BACKEND ("external"), which no sandbox backend uses,
    and replay reports such records UNREPLAYABLE. A producer cannot carry an
    image_digest or module_sha256: those are digests a sandbox run measures.
  - An input is optional. If given, its Snapshot must already be in the
    ledger's archive, as for a sandbox run.

run_id is the caller's idempotency key. Calling again with a run_id the
ledger already holds, for the same bundle, producer, config and input,
returns the SEALED record (or seals a record an earlier call left PENDING).
Anything else for that run_id raises DuplicateRunError: a run has one record.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping

from ..archive.records import Snapshot, Source
from .hashing import build_manifest
from .models import EXTERNAL_BACKEND, ArtifactRecord, ArtifactState, ParserIdentity
from .store import DuplicateRunError, LedgerStore, ProvenanceError, canonical_parser_config
from .transaction import _fail_quietly, _state_or_none


def record_external_artifact(
    store: LedgerStore,
    *,
    run_id: str | uuid.UUID,
    artifact_dir: Path,
    artifact_paths: list[Path],
    producer: ParserIdentity,
    producer_config: Mapping[str, Any],
    input_snapshot: Snapshot | None = None,
    source: Source | None = None,
) -> ArtifactRecord:
    """Record a bundle produced outside a Stele sandbox and seal it.

    Returns the SEALED record. Raises ValueError for a run_id that is not a
    UUID, ProvenanceError for a producer carrying measured digests (or an
    input the archive does not hold), DuplicateRunError when run_id already
    has a different record, and the sealing error (after marking the record
    FAILED) when the bundle cannot be archived and verified.
    """
    run_id = _run_id(run_id)
    if not isinstance(producer, ParserIdentity):
        raise TypeError("producer must be a ParserIdentity")
    if producer.image_digest is not None or producer.module_sha256 is not None:
        raise ProvenanceError(
            "an external producer cannot assert image_digest or module_sha256; "
            "those are measured by a sandbox run"
        )

    existing = store.get_by_run_id(run_id)
    if existing is None:
        try:
            record = store.create_pending(
                run_id=run_id,
                artifact_dir=artifact_dir,
                artifact_paths=artifact_paths,
                parser=producer,
                parser_config=producer_config,
                input_snapshot=input_snapshot,
                source=source,
                backend=EXTERNAL_BACKEND,
            )
        except DuplicateRunError:
            # Another caller recorded this run_id first; treat it as a retry.
            existing = store.get_by_run_id(run_id)
            if existing is None:
                raise
        else:
            return _seal(store, record)

    _require_same_run(
        existing, artifact_dir, artifact_paths, producer, producer_config, input_snapshot,
    )
    if existing.state is ArtifactState.SEALED:
        return existing
    if existing.state is ArtifactState.PENDING:
        return _seal(store, existing)
    raise DuplicateRunError(
        f"run {run_id} already has record {existing.record_id} "
        f"(state={existing.state.value}); record it under a new run_id"
    )


def _run_id(run_id: str | uuid.UUID) -> str:
    try:
        return str(run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(run_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"run_id must be a UUID; got {run_id!r}") from None


def _require_same_run(
    record: ArtifactRecord,
    artifact_dir: Path,
    artifact_paths: list[Path],
    producer: ParserIdentity,
    producer_config: Mapping[str, Any],
    input_snapshot: Snapshot | None,
) -> None:
    """Raise DuplicateRunError unless this call describes the recorded run."""
    differences = []
    if record.backend != EXTERNAL_BACKEND:
        differences.append(f"it was recorded by backend {record.backend!r}")
    if record.parser != producer:
        differences.append("the producer differs")
    if canonical_parser_config(record.parser_config or {}) != canonical_parser_config(
        producer_config
    ):
        differences.append("the producer config differs")
    if record.source_hash != (input_snapshot.digest if input_snapshot is not None else None):
        differences.append("the input differs")
    if build_manifest(artifact_dir, artifact_paths) != record.artifact_manifest:
        differences.append("the artifacts differ")
    if differences:
        raise DuplicateRunError(
            f"run {record.run_id} already has record {record.record_id}, and "
            + "; ".join(differences)
        )


def _seal(store: LedgerStore, record: ArtifactRecord) -> ArtifactRecord:
    """Seal record, marking it FAILED (and re-raising) if sealing fails."""
    try:
        return store.seal(record.record_id)
    except BaseException as exc:
        if isinstance(exc, Exception) and _state_or_none(store, record.record_id) is (
            ArtifactState.SEALED
        ):
            # The SEALED write is durable; only the read-back after it failed.
            return store.get(record.record_id)
        _fail_quietly(store, record.record_id, exc)
        raise
