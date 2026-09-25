"""
Stele Phase G — artifact validator.

Validates a single sealed ArtifactRecord against the filesystem by
re-hashing every file in its manifest and comparing to the ledger.

Does NOT modify the ledger.  Detection only — callers decide what to do
with ValidationResult (e.g. schedule invalidation). Validation never re-runs
a parser; that is replay (stele.replay.engine).
"""
from __future__ import annotations

from pathlib import Path

from ..archive.store import ArchiveError, BlobStore
from ..ledger.hashing import UnsafeFileError, sha256_file_beneath
from ..ledger.models import ArtifactRecord
from .models import ValidationResult


def validate_artifact(record: ArtifactRecord, archive: BlobStore | None = None) -> ValidationResult:
    """Re-hash every file in record.artifact_manifest and compare to stored hashes.

    A sealed record's working copy may legitimately be gone: sealing can
    verify the bundle in the evidence archive instead (roadmap #12). With an
    archive, a file absent from artifact_dir is re-verified there and counted
    as archived, not missing.

    Returns a ValidationResult with status:
      "ok"       — every file is present and hash matches
      "archived" — files absent from disk are all verified in the archive
      "drift"    — one or more files present on disk changed
      "missing"  — one or more files are absent from disk and from the archive

    Priority is missing, then drift, then archived.
    """
    artifact_dir = Path(record.artifact_dir)
    drifted: list[str] = []
    missing: list[str] = []
    archived: list[str] = []

    for rel_path, expected_hash in record.artifact_manifest.items():
        try:
            actual_hash = sha256_file_beneath(artifact_dir, Path(rel_path))
        except FileNotFoundError:
            if archive is not None and _verified_in_archive(archive, expected_hash):
                archived.append(rel_path)
            else:
                missing.append(rel_path)
            continue
        except UnsafeFileError:
            drifted.append(rel_path)
            continue

        if actual_hash != expected_hash:
            drifted.append(rel_path)

    if missing:
        status = "missing"
    elif drifted:
        status = "drift"
    elif archived:
        status = "archived"
    else:
        status = "ok"

    return ValidationResult(
        record_id=record.record_id,
        status=status,
        drifted_files=tuple(sorted(drifted)),
        missing_files=tuple(sorted(missing)),
        archived_files=tuple(sorted(archived)),
    )


def _verified_in_archive(archive: BlobStore, digest: str) -> bool:
    """True if the archive holds digest and its bytes still hash to it."""
    try:
        for _ in archive.iter_verified(digest):
            pass
    except ArchiveError:
        return False
    return True
