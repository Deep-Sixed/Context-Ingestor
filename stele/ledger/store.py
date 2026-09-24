"""
Stele Phase F — SQLite-backed artifact ledger store.

Schema is intentionally portable: column names and types map 1:1 to what
a future Postgres migration would look like.  The backend can be swapped
without changing the public API.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .hashing import UnsafeArtifactError, build_manifest, resolve_artifact, sha256_file, sha256_manifest
from .models import ArtifactRecord, ArtifactState


_DDL = """
CREATE TABLE IF NOT EXISTS artifact_records (
    record_id      TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    source_path    TEXT,
    source_hash    TEXT,
    artifact_dir   TEXT NOT NULL,
    artifact_manifest TEXT NOT NULL,   -- JSON: {rel_path: sha256}
    artifact_hash  TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,      -- ISO-8601 UTC
    finalized_at   TEXT,
    error          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_hash ON artifact_records(artifact_hash);
CREATE INDEX IF NOT EXISTS idx_run_id ON artifact_records(run_id);
"""


class DuplicateArtifactError(Exception):
    """Raised when a record with the same artifact_hash already exists."""


class RecordNotFoundError(Exception):
    """Raised when a record_id does not exist in the ledger."""


class InvalidStateTransitionError(Exception):
    """Raised when attempting a state transition that is not permitted."""


class MissingArtifactError(Exception):
    """Raised when an artifact file is absent at commit time."""


class ArtifactDriftError(Exception):
    """Raised when an artifact file no longer matches its recorded hash at commit time."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: sqlite3.Row) -> ArtifactRecord:
    def _dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    return ArtifactRecord(
        record_id=row["record_id"],
        run_id=row["run_id"],
        source_path=row["source_path"],
        source_hash=row["source_hash"],
        artifact_dir=row["artifact_dir"],
        artifact_manifest=json.loads(row["artifact_manifest"]),
        artifact_hash=row["artifact_hash"],
        state=ArtifactState(row["state"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        finalized_at=_dt(row["finalized_at"]),
        error=row["error"],
    )


class LedgerStore:
    """Append-mostly SQLite ledger for Stele artifact records.

    One instance per database file.  Thread-safe via SQLite WAL mode and
    the check_same_thread=False flag (callers serialize writes externally or
    rely on SQLite's own locking).
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def create_pending(
        self,
        *,
        run_id: str,
        artifact_dir: Path,
        artifact_paths: list[Path],
        source_path: str | None = None,
        source_hash: str | None = None,
        duplicate_policy: str = "raise",  # "raise" | "ignore"
    ) -> ArtifactRecord:
        """Record a new artifact bundle as PENDING.

        artifact_paths must be non-empty, all must exist, and all must be
        under artifact_dir (i.e. they came through /stele/output).

        duplicate_policy:
          "raise"  — raise DuplicateArtifactError if artifact_hash already exists
          "ignore" — return the existing committed/pending record silently
        """
        if not artifact_paths:
            raise ValueError("cannot create a pending record with no artifacts")

        manifest = build_manifest(artifact_dir, artifact_paths)
        artifact_hash = sha256_manifest(manifest)

        existing = self.find_by_artifact_hash(artifact_hash)
        if existing is not None:
            if duplicate_policy == "ignore":
                return existing
            raise DuplicateArtifactError(
                f"artifact_hash {artifact_hash[:12]}… already exists "
                f"as record {existing.record_id} (state={existing.state.value})"
            )

        record_id = str(uuid4())
        now = _now_iso()

        self._conn.execute(
            """
            INSERT INTO artifact_records
                (record_id, run_id, source_path, source_hash,
                 artifact_dir, artifact_manifest, artifact_hash,
                 state, created_at, finalized_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)
            """,
            (
                record_id,
                run_id,
                source_path,
                source_hash,
                str(artifact_dir),
                json.dumps(manifest),
                artifact_hash,
                now,
            ),
        )
        self._conn.commit()
        return self.get(record_id)

    def commit(self, record_id: str) -> ArtifactRecord:
        """Transition PENDING → COMMITTED.

        Re-hashes every artifact file and compares it to the manifest recorded
        at create_pending time, so what gets committed is exactly what was
        hashed.  Raises MissingArtifactError if a file is gone,
        ArtifactDriftError if a file changed or was replaced by a symlink or
        non-regular file, and InvalidStateTransitionError if the record is
        not in PENDING state.
        """
        record = self._require(record_id)

        if record.state is not ArtifactState.PENDING:
            raise InvalidStateTransitionError(
                f"cannot commit record {record_id}: state is {record.state.value!r} "
                f"(must be 'pending')"
            )

        # Re-verify every file — guards against the artifact_dir being cleaned
        # up or modified between create_pending and commit.
        artifact_dir = Path(record.artifact_dir)
        for rel_path, expected_hash in record.artifact_manifest.items():
            try:
                full = resolve_artifact(artifact_dir, rel_path)
                actual_hash = sha256_file(full)
            except FileNotFoundError:
                raise MissingArtifactError(
                    f"artifact file missing at commit time: {artifact_dir / rel_path} — "
                    "cannot commit; call fail() instead"
                )
            except UnsafeArtifactError as exc:
                raise ArtifactDriftError(
                    f"artifact file unsafe at commit time: {exc} — "
                    "cannot commit; call fail() instead"
                )
            if actual_hash != expected_hash:
                raise ArtifactDriftError(
                    f"artifact file changed since it was hashed: {full} — "
                    "cannot commit; call fail() instead"
                )

        self._conn.execute(
            "UPDATE artifact_records SET state='committed', finalized_at=? "
            "WHERE record_id=?",
            (_now_iso(), record_id),
        )
        self._conn.commit()
        return self.get(record_id)

    def fail(self, record_id: str, error: str) -> ArtifactRecord:
        """Transition PENDING → FAILED.

        A failed record is never committed.  Raises InvalidStateTransitionError
        if the record is already committed or invalidated.
        """
        record = self._require(record_id)

        if record.state is ArtifactState.COMMITTED:
            raise InvalidStateTransitionError(
                f"cannot fail record {record_id}: already committed"
            )
        if record.state is ArtifactState.INVALIDATED:
            raise InvalidStateTransitionError(
                f"cannot fail record {record_id}: already invalidated"
            )

        self._conn.execute(
            "UPDATE artifact_records SET state='failed', finalized_at=?, error=? "
            "WHERE record_id=?",
            (_now_iso(), error, record_id),
        )
        self._conn.commit()
        return self.get(record_id)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get(self, record_id: str) -> ArtifactRecord:
        row = self._conn.execute(
            "SELECT * FROM artifact_records WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(record_id)
        return _row_to_record(row)

    def find_by_artifact_hash(self, artifact_hash: str) -> ArtifactRecord | None:
        row = self._conn.execute(
            "SELECT * FROM artifact_records WHERE artifact_hash=?", (artifact_hash,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def find_by_run_id(self, run_id: str) -> list[ArtifactRecord]:
        rows = self._conn.execute(
            "SELECT * FROM artifact_records WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require(self, record_id: str) -> ArtifactRecord:
        try:
            return self.get(record_id)
        except RecordNotFoundError:
            raise RecordNotFoundError(f"no record with id={record_id}")

    def invalidate(self, record_id: str, reason_note: str) -> ArtifactRecord:
        """Transition PENDING or COMMITTED → INVALIDATED.

        INVALIDATED is a terminal state — it cannot be reversed.
        Use this when a previously committed artifact is found to be stale,
        incorrect, or superseded (Phase G).
        """
        record = self._require(record_id)

        if record.state is ArtifactState.INVALIDATED:
            raise InvalidStateTransitionError(
                f"record {record_id} is already invalidated"
            )
        if record.state is ArtifactState.FAILED:
            raise InvalidStateTransitionError(
                f"cannot invalidate a failed record {record_id} — already terminal"
            )

        self._conn.execute(
            "UPDATE artifact_records "
            "SET state='invalidated', finalized_at=?, error=? "
            "WHERE record_id=?",
            (_now_iso(), f"INVALIDATED: {reason_note}", record_id),
        )
        self._conn.commit()
        return self.get(record_id)

    def list_by_states(self, states: list[ArtifactState]) -> list[ArtifactRecord]:
        """Return all records whose state is in the given list, ordered by created_at."""
        placeholders = ",".join("?" * len(states))
        rows = self._conn.execute(
            f"SELECT * FROM artifact_records WHERE state IN ({placeholders}) "
            f"ORDER BY created_at",
            [s.value for s in states],
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
