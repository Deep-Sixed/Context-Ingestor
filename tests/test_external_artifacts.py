"""
Recording artifacts produced outside a Stele sandbox (stele.ledger.external).

  - a bundle is recorded, archived and sealed; the record says "external"
  - the same call again is idempotent; a PENDING record left behind is sealed
  - anything else for a recorded run_id is refused
  - a producer cannot assert measured digests; sandbox runs cannot claim
    the external backend
  - an input Snapshot must already be archived
  - replay reports external records UNREPLAYABLE, and the event log verifies

Run:
    uv run pytest tests/test_external_artifacts.py -v
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from stele.adapters import ChatGPTExportAdapter, MalformedExportError
from stele.archive.records import Snapshot, SnapshotKind, Source
from stele.contracts.adapter import SealedBundle
from stele.extraction.normalizers import NormalizeError, normalize
from stele.containment.result import SandboxResult
from stele.ledger.events import verify_ledger
from stele.ledger.external import record_external_artifact
from stele.ledger.hashing import sha256_manifest
from stele.ledger.models import EXTERNAL_BACKEND, ArtifactState, ParserIdentity
from stele.ledger.store import (
    ArtifactDriftError,
    DuplicateRunError,
    LedgerStore,
    ProvenanceError,
)
from stele.ledger.transaction import ledger_transaction
from stele.replay.engine import ReplayOutcome, replay_record
from stele.replay.parsers import ParserCatalog
from tests.ledger_helpers import PROVENANCE, open_ledger

PRODUCER = ParserIdentity(name="agentsync-kanon", version="0.1.0")
CONFIG = {"validation_level": "promote"}


@pytest.fixture
def store(tmp_path: Path) -> LedgerStore:
    return open_ledger(tmp_path / "ledger.db")


@pytest.fixture
def bundle(tmp_path: Path) -> tuple[Path, list[Path]]:
    out = tmp_path / "out"
    out.mkdir()
    skill = out / "SKILL.md"
    skill.write_text("# a skill\n", encoding="utf-8")
    return out, [skill]


def _snapshot(data: bytes) -> Snapshot:
    return Snapshot(SnapshotKind.FILE, hashlib.sha256(data).hexdigest(), len(data), 1)


def _record(store, bundle, run_id=None, **overrides):
    artifact_dir, paths = bundle
    kwargs = dict(
        run_id=run_id or str(uuid.uuid4()),
        artifact_dir=artifact_dir,
        artifact_paths=paths,
        producer=PRODUCER,
        producer_config=CONFIG,
    )
    kwargs.update(overrides)
    return record_external_artifact(store, **kwargs)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_records_and_seals_bundle(store, bundle) -> None:
    run_id = str(uuid.uuid4())
    record = _record(store, bundle, run_id)

    assert record.state is ArtifactState.SEALED
    assert record.run_id == run_id
    assert record.backend == EXTERNAL_BACKEND
    assert record.parser == PRODUCER
    assert record.parser_config == CONFIG
    assert record.source_hash is None
    assert record.artifact_manifest == {"SKILL.md": record.artifact_manifest["SKILL.md"]}
    assert record.artifact_hash == sha256_manifest(record.artifact_manifest)
    assert store.get_by_run_id(run_id) == record


def test_sealed_bundle_survives_removal_from_disk(store, bundle) -> None:
    """Sealing archived the bytes: the staging directory is not the evidence."""
    record = _record(store, bundle)
    for path in bundle[1]:
        path.unlink()
    assert store.archive.read_tree(record.artifact_hash) == record.artifact_manifest


def test_accepts_uuid_objects(store, bundle) -> None:
    run_id = uuid.uuid4()
    assert _record(store, bundle, run_id).run_id == str(run_id)


@pytest.mark.parametrize("bad", ["", "not-a-uuid", None, 42])
def test_run_id_must_be_uuid(store, bundle, bad) -> None:
    with pytest.raises(ValueError, match="UUID"):
        record_external_artifact(
            store, run_id=bad, artifact_dir=bundle[0], artifact_paths=bundle[1],
            producer=PRODUCER, producer_config=CONFIG,
        )


def test_records_input_snapshot(store, bundle) -> None:
    store.archive.put_bytes(b"input document")
    snapshot = store.archive.put_snapshot(_snapshot(b"input document"))

    record = _record(store, bundle, input_snapshot=snapshot)

    assert record.source_hash == snapshot.digest


def test_unarchived_input_is_refused(store, bundle) -> None:
    with pytest.raises(ProvenanceError, match="not in the ledger's archive"):
        _record(store, bundle, input_snapshot=_snapshot(b"never archived"))
    assert store.list_by_states(list(ArtifactState)) == []


# ---------------------------------------------------------------------------
# Provenance guards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["image_digest", "module_sha256"])
def test_producer_cannot_assert_measured_digests(store, bundle, field) -> None:
    producer = ParserIdentity(name="x", version="1", **{field: "a" * 64})
    with pytest.raises(ProvenanceError, match="measured"):
        _record(store, bundle, producer=producer)


def test_sandbox_run_cannot_claim_external_backend(store, bundle) -> None:
    artifact_dir, paths = bundle
    result = SandboxResult(
        run_id=uuid.uuid4(), exit_code=0, stdout=b"", stderr=b"", wall_time_seconds=0.1,
        timed_out=False, artifact_dir=artifact_dir, artifact_paths=paths,
        backend=EXTERNAL_BACKEND,
    )
    with pytest.raises(ProvenanceError, match="reserved"):
        with ledger_transaction(store, result, **PROVENANCE):
            pass
    assert store.get_by_run_id(str(result.run_id)) is None


# ---------------------------------------------------------------------------
# Retries: run_id is the idempotency key
# ---------------------------------------------------------------------------

def test_same_call_again_returns_sealed_record(store, bundle) -> None:
    run_id = str(uuid.uuid4())
    first = _record(store, bundle, run_id)
    again = _record(store, bundle, run_id)

    assert again == first
    assert len(store.list_by_states(list(ArtifactState))) == 1


def test_pending_record_is_sealed_on_retry(store, bundle) -> None:
    run_id = str(uuid.uuid4())
    pending = store.create_pending(
        run_id=run_id, artifact_dir=bundle[0], artifact_paths=bundle[1],
        parser=PRODUCER, parser_config=CONFIG, backend=EXTERNAL_BACKEND,
    )

    record = _record(store, bundle, run_id)

    assert record.record_id == pending.record_id
    assert record.state is ArtifactState.SEALED


@pytest.mark.parametrize("change", ["content", "producer", "config"])
def test_different_call_for_recorded_run_is_refused(store, bundle, change) -> None:
    run_id = str(uuid.uuid4())
    first = _record(store, bundle, run_id)
    overrides = {}
    if change == "content":
        bundle[1][0].write_text("# edited\n", encoding="utf-8")
    elif change == "producer":
        overrides["producer"] = ParserIdentity(name="agentsync-kanon", version="0.2.0")
    else:
        overrides["producer_config"] = {"validation_level": "draft"}

    with pytest.raises(DuplicateRunError, match="already has record"):
        _record(store, bundle, run_id, **overrides)
    assert store.get(first.record_id) == first


def test_sandbox_record_is_not_reused(store, bundle) -> None:
    artifact_dir, paths = bundle
    result = SandboxResult(
        run_id=uuid.uuid4(), exit_code=0, stdout=b"", stderr=b"", wall_time_seconds=0.1,
        timed_out=False, artifact_dir=artifact_dir, artifact_paths=paths, backend="bubblewrap",
    )
    with ledger_transaction(store, result, parser=PRODUCER, parser_config=CONFIG):
        pass

    with pytest.raises(DuplicateRunError, match="backend 'bubblewrap'"):
        _record(store, bundle, str(result.run_id))


def test_retry_after_files_were_removed_returns_sealed_record(store, bundle) -> None:
    """The first call sealed the record but its answer was lost; the caller
    cleaned up its files and retries with the same run_id."""
    run_id = str(uuid.uuid4())
    first = _record(store, bundle, run_id)
    for path in bundle[1]:
        path.unlink()

    assert _record(store, bundle, run_id) == first


def test_retry_after_files_were_removed_still_compares_paths(store, bundle) -> None:
    run_id = str(uuid.uuid4())
    _record(store, bundle, run_id)
    for path in bundle[1]:
        path.unlink()

    with pytest.raises(DuplicateRunError, match="the artifacts differ"):
        _record(store, bundle, run_id, artifact_paths=[bundle[0] / "OTHER.md"])


def test_retry_with_some_files_removed_still_compares_the_rest(store, tmp_path) -> None:
    """One removed file must not excuse another that changed."""
    out = tmp_path / "two"
    out.mkdir()
    a, b = out / "a.md", out / "b.md"
    a.write_text("original A", encoding="utf-8")
    b.write_text("original B", encoding="utf-8")
    run_id = str(uuid.uuid4())
    first = _record(store, (out, [a, b]), run_id)
    a.unlink()

    assert _record(store, (out, [a, b]), run_id) == first   # b unchanged: a retry
    b.write_text("different B", encoding="utf-8")
    with pytest.raises(DuplicateRunError, match="the artifacts differ"):
        _record(store, (out, [a, b]), run_id)


def test_sandbox_record_is_not_reused_after_files_were_removed(store, bundle) -> None:
    artifact_dir, paths = bundle
    result = SandboxResult(
        run_id=uuid.uuid4(), exit_code=0, stdout=b"", stderr=b"", wall_time_seconds=0.1,
        timed_out=False, artifact_dir=artifact_dir, artifact_paths=paths, backend="bubblewrap",
    )
    with ledger_transaction(store, result, parser=PRODUCER, parser_config=CONFIG):
        pass
    for path in paths:
        path.unlink()

    with pytest.raises(DuplicateRunError, match="backend 'bubblewrap'"):
        _record(store, bundle, str(result.run_id))


def _archived_input(store, data: bytes, kind=SnapshotKind.FILE) -> Snapshot:
    store.archive.put_bytes(data)
    return store.archive.put_snapshot(
        Snapshot(kind, hashlib.sha256(data).hexdigest(), len(data), 1)
    )


def test_retry_with_a_different_source_is_refused(store, bundle) -> None:
    run_id = str(uuid.uuid4())
    snapshot = _archived_input(store, b"input document")
    _record(store, bundle, run_id, input_snapshot=snapshot, source=Source(locator="a.md"))

    with pytest.raises(DuplicateRunError, match="the source differs"):
        _record(store, bundle, run_id, input_snapshot=snapshot, source=Source(locator="b.md"))
    with pytest.raises(DuplicateRunError, match="the source differs"):
        _record(store, bundle, run_id, input_snapshot=snapshot)
    assert _record(
        store, bundle, run_id, input_snapshot=snapshot, source=Source(locator="a.md")
    ).state is ArtifactState.SEALED


def test_retry_with_a_different_snapshot_is_refused(store, bundle) -> None:
    run_id = str(uuid.uuid4())
    snapshot = _archived_input(store, b"input document")
    _record(store, bundle, run_id, input_snapshot=snapshot)

    same_digest_other_size = Snapshot(snapshot.kind, snapshot.digest, snapshot.size + 1, 1)
    with pytest.raises(DuplicateRunError, match="the input differs"):
        _record(store, bundle, run_id, input_snapshot=same_digest_other_size)


# ---------------------------------------------------------------------------
# An external producer is never read as a sandbox parser
# ---------------------------------------------------------------------------

def test_external_record_under_a_parser_name_is_not_read_as_that_parser(store, bundle) -> None:
    impostor = ParserIdentity(name="chatgpt-export-split", version="1")
    record = _record(store, bundle, producer=impostor, producer_config={})
    sealed = SealedBundle.from_record(record, store.archive)
    assert sealed.external

    with pytest.raises(NormalizeError, match="outside a Stele sandbox"):
        normalize(sealed)
    with pytest.raises(MalformedExportError, match="outside a Stele sandbox"):
        ChatGPTExportAdapter().transform(sealed)


def test_failed_record_is_not_resumed(store, bundle) -> None:
    run_id = str(uuid.uuid4())
    pending = store.create_pending(
        run_id=run_id, artifact_dir=bundle[0], artifact_paths=bundle[1],
        parser=PRODUCER, parser_config=CONFIG, backend=EXTERNAL_BACKEND,
    )
    store.fail(pending.record_id, error="gave up")

    with pytest.raises(DuplicateRunError, match="new run_id"):
        _record(store, bundle, run_id)


def test_drift_before_seal_marks_record_failed(store, bundle, monkeypatch) -> None:
    run_id = str(uuid.uuid4())
    real_seal = store.seal

    def drifting_seal(record_id):
        bundle[1][0].write_text("# changed underneath\n", encoding="utf-8")
        return real_seal(record_id)

    monkeypatch.setattr(store, "seal", drifting_seal)
    with pytest.raises(ArtifactDriftError):
        _record(store, bundle, run_id)
    assert store.get_by_run_id(run_id).state is ArtifactState.FAILED


# ---------------------------------------------------------------------------
# Replay and the event log
# ---------------------------------------------------------------------------

def test_replay_reports_external_record_unreplayable(store, bundle) -> None:
    record = _record(store, bundle)

    result = replay_record(store, ParserCatalog(), record)

    assert result.outcome is ReplayOutcome.UNREPLAYABLE
    assert "outside a Stele sandbox" in result.reason
    assert store.get(record.record_id).state is ArtifactState.SEALED


def test_event_log_verifies_external_records(store, bundle) -> None:
    _record(store, bundle)
    report = verify_ledger(store)
    assert report.ok, report.problems
