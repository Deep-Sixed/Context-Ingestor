"""
Ledger redesign proofs (roadmap #12).

  1. Two runs producing identical output keep separate records, each with
     its own input Snapshot.
  2. A record cannot carry a source_hash that differs from its Snapshot digest.
  3. Parser identity and config are stored and queryable.
  4. SEALED means the bundle is in the archive and verified.
  5. Migrating a pre-#12 ledger.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from stele.archive import BlobStore, IntegrityError, Snapshot, SnapshotKind, Source
from stele.containment.backend import Capability, ExecutionOutcome, SandboxBackend
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig
from stele.ledger.hashing import build_manifest, sha256_manifest
from stele.ledger.migration import LedgerMigrationError
from stele.ledger.models import ArtifactState, ParserIdentity
from stele.ledger.store import (
    SCHEMA_VERSION,
    LedgerSchemaError,
    LedgerStore,
    ProvenanceError,
)
from stele.ledger.transaction import ledger_transaction, record_run
from tests.ledger_helpers import PROVENANCE, open_ledger

requires_posix_staging = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fwalk"),
    reason="secure staging requires O_NOFOLLOW and os.fwalk",
)

IMAGE_DIGEST = "sha256:" + "d" * 64
PARSER = ParserIdentity(name="mineru", version="1.3.1")
CONFIG = {"ocr": True, "lang": "en"}


class ConstantBackend(SandboxBackend):
    """A parser in an image whose output does not depend on its input."""

    name = "probe"

    def capabilities(self):
        return frozenset({
            Capability.FILESYSTEM_ISOLATION,
            Capability.NETWORK_ISOLATION,
            Capability.HOST_PROCESS,
        })

    def available(self):
        return True

    def unavailable_reason(self):
        return ""

    def execute(self, config):
        config.artifact_dir.mkdir(parents=True, exist_ok=True)
        (config.artifact_dir / "parsed.txt").write_bytes(b"constant output")
        return ExecutionOutcome(
            exit_code=0, stdout="", stderr="", wall_time_seconds=0.0,
            image_digest=IMAGE_DIGEST,
        )


@pytest.fixture
def ledger(tmp_path: Path) -> LedgerStore:
    return open_ledger(tmp_path / "ledger.db")


def _run(tmp_path: Path, archive: BlobStore | None, content: bytes | None = None):
    name = uuid.uuid4().hex[:8]
    src = None
    if content is not None:
        src = tmp_path / f"doc-{name}.pdf"
        src.write_bytes(content)
    config = SandboxConfig(command=["x"], artifact_dir=tmp_path / f"out-{name}", input_path=src)
    return src, run_in_sandbox(config, backend=ConstantBackend(), store=archive)


# ---------------------------------------------------------------------------
# 1 — Identical output, separate records, separate Snapshots
# ---------------------------------------------------------------------------

@requires_posix_staging
class TestIdenticalOutputKeepsProvenance:

    def test_each_run_keeps_its_own_record_and_snapshot(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        src1, run1 = _run(tmp_path, ledger.archive, b"first document")
        src2, run2 = _run(tmp_path, ledger.archive, b"second document")
        rec1 = record_run(
            ledger, run1, parser=PARSER, parser_config=CONFIG, source=Source.from_path(src1)
        )
        rec2 = record_run(
            ledger, run2, parser=PARSER, parser_config=CONFIG, source=Source.from_path(src2)
        )

        assert rec1.state is rec2.state is ArtifactState.SEALED
        assert rec1.record_id != rec2.record_id
        assert rec1.artifact_hash == rec2.artifact_hash  # identical output
        assert (rec1.source_hash, rec2.source_hash) == (
            run1.input_snapshot.digest, run2.input_snapshot.digest
        )
        assert rec1.source_hash != rec2.source_hash
        for rec, src in ((rec1, src1), (rec2, src2)):
            assert rec.source_kind is SnapshotKind.FILE
            assert ledger.archive.has_snapshot(rec.source_hash, rec.source_kind)
            assert rec.source_path == Source.from_path(src).locator
            assert ledger.archive.get_source(rec.source_id) == Source.from_path(src)
        assert [r.record_id for r in ledger.find_by_artifact_hash(rec1.artifact_hash)] == [
            rec1.record_id, rec2.record_id
        ]
        assert ledger.find_by_source_hash(rec1.source_hash) == [rec1]

    def test_same_input_run_twice_gives_two_records(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run1 = _run(tmp_path, ledger.archive, b"same document")
        _, run2 = _run(tmp_path, ledger.archive, b"same document")
        rec1 = record_run(ledger, run1, parser=PARSER, parser_config=CONFIG)
        rec2 = record_run(ledger, run2, parser=PARSER, parser_config=CONFIG)

        assert rec1.record_id != rec2.record_id
        assert rec1.source_hash == rec2.source_hash
        assert {r.run_id for r in ledger.find_by_source_hash(rec1.source_hash)} == {
            str(run1.run_id), str(run2.run_id)
        }


# ---------------------------------------------------------------------------
# 2 — source_hash is the Snapshot digest, never a caller's claim
# ---------------------------------------------------------------------------

@requires_posix_staging
class TestSourceHashIsTheSnapshot:

    def test_input_that_was_not_archived_is_refused(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, None, b"doc")
        assert run.input_sha256 is not None and run.input_snapshot is None
        with pytest.raises(ProvenanceError, match="not archived"):
            record_run(ledger, run, parser=PARSER, parser_config=CONFIG)
        assert ledger.get_by_run_id(str(run.run_id)) is None

    def test_snapshot_from_another_archive_is_refused(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, BlobStore(tmp_path / "elsewhere"), b"doc")
        with pytest.raises(ProvenanceError, match="not in the ledger's archive"):
            record_run(ledger, run, parser=PARSER, parser_config=CONFIG)
        assert ledger.get_by_run_id(str(run.run_id)) is None

    def test_snapshot_that_differs_from_the_staged_hash_is_refused(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, ledger.archive, b"doc")
        forged = dataclasses.replace(run, input_sha256="0" * 64)
        with pytest.raises(ProvenanceError, match="differs from the staged input"):
            record_run(ledger, forged, parser=PARSER, parser_config=CONFIG)

    def test_forged_snapshot_record_is_refused(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, ledger.archive, b"doc")
        snap = run.input_snapshot
        forged = Snapshot(snap.kind, snap.digest, snap.size + 1, snap.file_count)
        with pytest.raises(ProvenanceError, match="differs from the archived record"):
            ledger.create_pending(
                **PROVENANCE, run_id=str(run.run_id), artifact_dir=run.artifact_dir,
                artifact_paths=run.artifact_paths, input_snapshot=forged,
            )

    def test_schema_refuses_a_hash_without_its_snapshot_kind(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, ledger.archive, b"doc")
        rec = record_run(ledger, run, parser=PARSER, parser_config=CONFIG)
        conn = sqlite3.connect(tmp_path / "ledger.db")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "UPDATE artifact_records SET source_kind=NULL WHERE record_id=?",
                (rec.record_id,),
            )
        conn.close()

    def test_source_needs_a_snapshot(self, ledger: LedgerStore, tmp_path: Path) -> None:
        _, run = _run(tmp_path, ledger.archive)
        with pytest.raises(ProvenanceError, match="only recorded with the Snapshot"):
            record_run(
                ledger, run, parser=PARSER, parser_config=CONFIG,
                source=Source(locator="/corpus/doc.pdf"),
            )

    def test_run_without_input_has_no_source(self, ledger: LedgerStore, tmp_path: Path) -> None:
        _, run = _run(tmp_path, ledger.archive)
        rec = record_run(ledger, run, parser=PARSER, parser_config=CONFIG)
        assert rec.source_hash is rec.source_kind is rec.source_id is None


# ---------------------------------------------------------------------------
# 3 — Parser identity and config
# ---------------------------------------------------------------------------

class TestParserIdentity:

    def test_identity_is_completed_from_the_run_and_queryable(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, ledger.archive)
        rec = record_run(ledger, run, parser=PARSER, parser_config=CONFIG)

        assert rec.parser == ParserIdentity("mineru", "1.3.1", image_digest=IMAGE_DIGEST)
        assert rec.parser_config == CONFIG
        assert rec.backend == "probe"
        assert ledger.get(rec.record_id) == rec

        assert ledger.find_by_parser("mineru") == [rec]
        assert ledger.find_by_parser("mineru", "1.3.1") == [rec]
        assert ledger.find_by_parser("mineru", "2.0") == []
        assert ledger.find_by_parser("marker") == []
        # Config matches by content, not key order.
        assert ledger.find_by_parser(
            "mineru", parser_config={"lang": "en", "ocr": True}
        ) == [rec]
        assert ledger.find_by_parser("mineru", parser_config={"ocr": False}) == []

    def test_claimed_digest_must_match_the_measured_one(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, ledger.archive)
        claimed = ParserIdentity("mineru", "1.3.1", image_digest="sha256:" + "e" * 64)
        with pytest.raises(ProvenanceError, match="image_digest"):
            record_run(ledger, run, parser=claimed, parser_config=CONFIG)
        assert ledger.get_by_run_id(str(run.run_id)) is None

    def test_parser_config_must_be_a_json_object(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, ledger.archive)
        for bad in (
            {"when": object()}, ["not", "a", "mapping"], {"x": float("nan")},
            {1: "int key"}, {"pages": (1, 2)},
        ):
            with pytest.raises(ValueError):
                record_run(ledger, run, parser=PARSER, parser_config=bad)

    def test_parser_identity_is_validated(self) -> None:
        with pytest.raises(ValueError):
            ParserIdentity("", "1.0")
        with pytest.raises(ValueError):
            ParserIdentity("p", "")
        with pytest.raises(ValueError):
            ParserIdentity("p", "1", module_sha256="not-a-digest")


# ---------------------------------------------------------------------------
# 4 — Sealing means archived and verified
# ---------------------------------------------------------------------------

class TestSealing:

    def test_output_changed_after_the_run_archived_it_is_refused(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, ledger.archive)
        (run.artifact_dir / "parsed.txt").write_bytes(b"swapped after the run")
        with pytest.raises(ProvenanceError, match="changed between"):
            with ledger_transaction(ledger, run, parser=PARSER, parser_config=CONFIG):
                pytest.fail("block must not run")
        assert ledger.get_by_run_id(str(run.run_id)).state is ArtifactState.FAILED

    def test_seals_from_the_archive_once_the_working_dir_is_gone(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        _, run = _run(tmp_path, ledger.archive)
        with ledger_transaction(ledger, run, parser=PARSER, parser_config=CONFIG) as rec:
            shutil.rmtree(run.artifact_dir)
        assert ledger.get(rec.record_id).state is ArtifactState.SEALED

    def test_corrupt_archive_blocks_sealing(self, ledger: LedgerStore, tmp_path: Path) -> None:
        _, run = _run(tmp_path, ledger.archive)
        [digest] = run.artifact_digests.values()
        blob = ledger.archive.root / "blobs" / digest[:2] / digest[2:]
        with pytest.raises(IntegrityError):
            with ledger_transaction(ledger, run, parser=PARSER, parser_config=CONFIG) as rec:
                shutil.rmtree(run.artifact_dir)
                os.chmod(blob, 0o600)
                blob.write_bytes(b"bit rot")
        assert ledger.get(rec.record_id).state is ArtifactState.FAILED


# ---------------------------------------------------------------------------
# 5 — Migration of a pre-#12 (schema version 0) ledger
# ---------------------------------------------------------------------------

_V0_DDL = """
CREATE TABLE IF NOT EXISTS artifact_records (
    record_id      TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    source_path    TEXT,
    source_hash    TEXT,
    artifact_dir   TEXT NOT NULL,
    artifact_manifest TEXT NOT NULL,
    artifact_hash  TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    finalized_at   TEXT,
    error          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_hash ON artifact_records(artifact_hash);
CREATE INDEX IF NOT EXISTS idx_run_id ON artifact_records(run_id);
"""


class TestMigration:

    @pytest.fixture
    def v0(self, tmp_path: Path):
        db = tmp_path / "ledger.db"
        conn = sqlite3.connect(db)
        conn.executescript(_V0_DDL)
        counter = iter(range(100))

        def add(state: str, *, content: str | None = None, run_id: str | None = None,
                source_path: str | None = None, source_hash: str | None = None,
                keep_files: bool = True, error: str | None = None) -> str:
            n = next(counter)
            out = tmp_path / f"v0-out-{n}"
            out.mkdir()
            f = out / "r.json"
            f.write_text(content or f"output {n}")
            manifest = build_manifest(out, [f])
            if not keep_files:
                shutil.rmtree(out)
            record_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO artifact_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, run_id or str(uuid.uuid4()), source_path, source_hash,
                 str(out), json.dumps(manifest), sha256_manifest(manifest), state,
                 f"2026-06-26T00:00:{n:02d}+00:00",
                 None if state == "pending" else "2026-06-26T01:00:00+00:00", error),
            )
            conn.commit()
            return record_id

        yield db, add
        conn.close()

    def test_states_and_bundles(self, v0, tmp_path: Path) -> None:
        db, add = v0
        sealed = add("committed")
        lost = add("committed", keep_files=False)
        pending = add("pending")
        failed = add("failed", error="parser crashed")
        withdrawn = add("invalidated", error="INVALIDATED: manual")

        ledger = open_ledger(db)
        rec = ledger.get(sealed)
        assert rec.state is ArtifactState.SEALED
        assert ledger.archive.read_tree(rec.artifact_hash) == rec.artifact_manifest

        rec = ledger.get(lost)
        assert rec.state is ArtifactState.INVALIDATED
        assert "migration" in rec.error and "could not be archived" in rec.error

        assert ledger.get(pending).state is ArtifactState.PENDING
        assert ledger.seal(pending).state is ArtifactState.SEALED
        assert ledger.get(failed).state is ArtifactState.FAILED
        assert ledger.get(failed).error == "parser crashed"
        assert ledger.get(withdrawn).state is ArtifactState.INVALIDATED
        for record_id in (sealed, lost, failed, withdrawn):
            assert ledger.get(record_id).parser is None
            assert ledger.get(record_id).parser_config is None

    def test_source_hash_is_kept_only_when_it_is_a_snapshot(self, v0, tmp_path: Path) -> None:
        db, add = v0
        archive = BlobStore(tmp_path / "archive")
        digest = archive.put_bytes(b"input document")
        archive.put_snapshot(Snapshot(SnapshotKind.FILE, digest, 14, 1))
        verified = add("committed", source_path="/corpus/doc.pdf", source_hash=digest)
        claimed = add("committed", source_hash="abc123")

        ledger = open_ledger(db)
        rec = ledger.get(verified)
        assert (rec.source_hash, rec.source_kind) == (digest, SnapshotKind.FILE)
        assert rec.legacy_source_hash is None
        assert rec.source_id == Source(locator="/corpus/doc.pdf").source_id
        assert ledger.archive.get_source(rec.source_id).locator == "/corpus/doc.pdf"

        rec = ledger.get(claimed)
        assert rec.source_hash is None and rec.source_kind is None
        assert rec.legacy_source_hash == "abc123"
        assert ledger.find_by_source_hash("abc123") == []

    def test_schema_is_current_and_migration_runs_once(self, v0) -> None:
        db, add = v0
        record_id = add("committed")
        open_ledger(db).close()
        conn = sqlite3.connect(db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.close()

        ledger = open_ledger(db)
        assert ledger.get(record_id).state is ArtifactState.SEALED

    def test_identical_output_is_allowed_after_migration(self, v0, tmp_path: Path) -> None:
        db, add = v0
        old = add("committed", content="same bytes")
        ledger = open_ledger(db)

        out = tmp_path / "new-out"
        out.mkdir()
        (out / "r.json").write_text("same bytes")
        new = ledger.create_pending(
            **PROVENANCE, run_id=str(uuid.uuid4()), artifact_dir=out,
            artifact_paths=[out / "r.json"],
        )
        assert new.artifact_hash == ledger.get(old).artifact_hash
        assert len(ledger.find_by_artifact_hash(new.artifact_hash)) == 2

    def test_two_records_for_one_run_stop_migration_and_change_nothing(self, v0) -> None:
        db, add = v0
        run_id = str(uuid.uuid4())
        add("committed", run_id=run_id)
        add("failed", run_id=run_id)

        with pytest.raises(LedgerMigrationError, match=run_id):
            open_ledger(db)

        conn = sqlite3.connect(db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM artifact_records").fetchone()[0] == 2
        assert {r[0] for r in conn.execute("SELECT state FROM artifact_records")} == {
            "committed", "failed"
        }
        conn.close()

    def test_newer_schema_is_refused(self, tmp_path: Path) -> None:
        db = tmp_path / "ledger.db"
        open_ledger(db).close()
        conn = sqlite3.connect(db)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        with pytest.raises(LedgerSchemaError, match="newer"):
            open_ledger(db)

    def test_ledger_needs_an_archive(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError):
            LedgerStore(tmp_path / "ledger.db", None)
