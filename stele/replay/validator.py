"""
Stele Phase G — artifact validator.

Validates a single sealed ArtifactRecord against the filesystem by
re-hashing every file in its manifest and comparing to the ledger.

Does NOT modify the ledger.  Detection only — callers decide what to do
with ValidationResult (e.g. schedule invalidation or proceed with replay).
"""
from __future__ import annotations

from pathlib import Path

from ..ledger.hashing import UnsafeFileError, sha256_file_beneath
from ..ledger.models import ArtifactRecord
from .models import ValidationResult


def validate_artifact(record: ArtifactRecord) -> ValidationResult:
    """Re-hash every file in record.artifact_manifest and compare to stored hashes.

    Returns a ValidationResult with status:
      "ok"      — every file is present and hash matches
      "drift"   — every file is present but one or more hashes differ
      "missing" — one or more files are absent from artifact_dir

    Missing takes priority over drift: if any file is absent the status is
    "missing" regardless of whether other files drifted.
    """
    artifact_dir = Path(record.artifact_dir)
    drifted: list[str] = []
    missing: list[str] = []

    for rel_path, expected_hash in record.artifact_manifest.items():
        try:
            actual_hash = sha256_file_beneath(artifact_dir, Path(rel_path))
        except FileNotFoundError:
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
    else:
        status = "ok"

    return ValidationResult(
        record_id=record.record_id,
        status=status,
        drifted_files=tuple(sorted(drifted)),
        missing_files=tuple(sorted(missing)),
    )
