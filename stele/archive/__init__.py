"""Snapshot archive and content-addressed evidence store (roadmap #16).

Model::

    Blob       bytes, addressed by SHA-256
    Source     where material came from (descriptive; not evidence)
    Snapshot   the exact staged bytes of a source at a moment in time
    Extraction a parser run over a Snapshot -> artifact blobs

Only digests identify evidence; file paths are just locations.
"""
from .ingest import ingest_artifacts, materialize_snapshot, snapshot_staged_input
from .records import (
    SCHEMA_VERSION,
    RecordFormatError,
    Snapshot,
    SnapshotKind,
    Source,
    canonical_json,
)
from .store import (
    ArchiveError,
    BlobStore,
    IntegrityError,
    InvalidDigestError,
    MissingObjectError,
    decode_tree,
)

__all__ = [
    "SCHEMA_VERSION",
    "ArchiveError",
    "BlobStore",
    "IntegrityError",
    "InvalidDigestError",
    "MissingObjectError",
    "RecordFormatError",
    "Snapshot",
    "SnapshotKind",
    "Source",
    "canonical_json",
    "decode_tree",
    "ingest_artifacts",
    "materialize_snapshot",
    "snapshot_staged_input",
]
