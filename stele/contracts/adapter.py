"""
Stele contracts — adapter contract.

The SteleAdapter protocol is the only authorized write boundary between
parser output and downstream storage targets.

Rules:
  - Adapters receive a SealedBundle and return list[SteleChunk].
  - A SealedBundle serves the sealed record's artifact bytes from the
    evidence archive, re-verified against their digest on every read.
    Adapters never receive file paths, DB connections, file handles, or
    target objects.
  - Adapters do NOT call target-store APIs (e.g. LightRAG, Hindsight) directly.
  - All target writes, and their removal on invalidation, are routed
    through the Dispatcher (contracts/dispatcher.py).
  - Invalid adapter output (wrong hashes, empty chunks) is rejected by the
    Dispatcher before any write reaches a target.
  - Adapters are trusted code running in the host process: the rules above
    are a contract, not a capability boundary (see docs/adapter.md).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from ..archive.records import SnapshotKind, canonical_json
from ..archive.store import BlobStore
from ..ledger.models import EXTERNAL_BACKEND, ArtifactRecord, ParserIdentity


# ---------------------------------------------------------------------------
# Chunk model
# ---------------------------------------------------------------------------

@dataclass
class SteleChunk:
    """A single normalized content chunk produced by an adapter.

    content_hash must be sha256(content.encode()).  The Dispatcher verifies
    this — adapters that return mismatched hashes are rejected.
    """
    chunk_id: str
    content: str
    content_hash: str       # sha256(content.encode()) — verified by Dispatcher
    token_count: int        # must be >= 0
    # A JSON object with one canonical encoding (validated by the Dispatcher);
    # part of the delivery's fingerprint, like the content.
    metadata: dict[str, Any] = field(default_factory=dict)


def make_chunk(chunk_id: str, content: str, **metadata: Any) -> SteleChunk:
    """Convenience constructor that computes content_hash automatically."""
    return SteleChunk(
        chunk_id=chunk_id,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        token_count=len(content.split()),
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# What an adapter reads
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SealedBundle:
    """A sealed record's artifacts, served by digest from the evidence archive.

    read() returns bytes the archive has just re-hashed against the digest the
    ledger recorded, so an adapter can only ever see the bytes that were
    sealed. The bundle carries the record's provenance but no host paths.
    """

    record_id: str
    run_id: str
    artifact_hash: str
    manifest: Mapping[str, str]  # {relative POSIX path: sha256}
    parser: ParserIdentity | None
    parser_config: Mapping[str, Any] | None
    source_hash: str | None
    source_kind: SnapshotKind | None
    source_path: str | None      # descriptive locator of the input, not a readable path
    _archive: BlobStore = field(repr=False, compare=False)
    # The sandbox backend that made the record; EXTERNAL_BACKEND for artifacts
    # recorded with record_external_artifact, whose producer is asserted.
    backend: str | None = None

    @property
    def external(self) -> bool:
        """True if the bundle was recorded outside a Stele sandbox."""
        return self.backend == EXTERNAL_BACKEND

    @classmethod
    def from_record(cls, record: ArtifactRecord, archive: BlobStore) -> "SealedBundle":
        return cls(
            record_id=record.record_id,
            run_id=record.run_id,
            artifact_hash=record.artifact_hash,
            manifest=MappingProxyType(dict(record.artifact_manifest)),
            parser=record.parser,
            parser_config=(
                MappingProxyType(dict(record.parser_config))
                if record.parser_config is not None else None
            ),
            source_hash=record.source_hash,
            source_kind=record.source_kind,
            source_path=record.source_path,
            _archive=archive,
            backend=record.backend,
        )

    def paths(self) -> list[str]:
        """Relative POSIX paths of every artifact, sorted."""
        return sorted(self.manifest)

    def read(self, path: str) -> bytes:
        """Verified bytes of one artifact (IntegrityError if they changed)."""
        try:
            digest = self.manifest[path]
        except KeyError:
            raise KeyError(f"no artifact {path!r} in record {self.record_id}") from None
        return self._archive.read(digest)

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read(path).decode(encoding)


# ---------------------------------------------------------------------------
# Target types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LightRAGTarget:
    workspace: str


@dataclass(frozen=True)
class HindsightTarget:
    instance: str


SteleTarget = LightRAGTarget | HindsightTarget


# ---------------------------------------------------------------------------
# Chunk validation — enforced by Dispatcher before any write
# ---------------------------------------------------------------------------

class ChunkValidationError(Exception):
    """Raised when an adapter returns chunks that fail contract validation."""


def validate_chunks(chunks: list[SteleChunk]) -> None:
    """Validate that adapter output satisfies the SteleChunk contract.

    Raises ChunkValidationError on the first violation found.
    """
    if not chunks:
        raise ChunkValidationError("adapter returned no chunks — empty output is not permitted")

    seen_ids: set[str] = set()
    for i, chunk in enumerate(chunks):
        if not chunk.chunk_id:
            raise ChunkValidationError(f"chunk[{i}].chunk_id is empty")
        if chunk.chunk_id in seen_ids:
            raise ChunkValidationError(f"duplicate chunk_id: {chunk.chunk_id!r}")
        seen_ids.add(chunk.chunk_id)

        if not chunk.content:
            raise ChunkValidationError(f"chunk[{i}] ({chunk.chunk_id!r}): content is empty")

        if not chunk.content_hash:
            raise ChunkValidationError(f"chunk[{i}] ({chunk.chunk_id!r}): content_hash is empty")

        expected = hashlib.sha256(chunk.content.encode()).hexdigest()
        if chunk.content_hash != expected:
            raise ChunkValidationError(
                f"chunk[{i}] ({chunk.chunk_id!r}): content_hash mismatch — "
                f"expected {expected[:12]}…, got {chunk.content_hash[:12]}…"
            )

        if not isinstance(chunk.token_count, int) or isinstance(chunk.token_count, bool):
            raise ChunkValidationError(
                f"chunk[{i}] ({chunk.chunk_id!r}): token_count must be an int"
            )
        if chunk.token_count < 0:
            raise ChunkValidationError(
                f"chunk[{i}] ({chunk.chunk_id!r}): token_count must be >= 0"
            )

        _check_metadata(i, chunk)


def _check_metadata(i: int, chunk: SteleChunk) -> None:
    """Metadata must be a JSON object with exactly one canonical encoding.

    The delivery log fingerprints it (stele.ledger.delivery.chunks_digest),
    so it must survive a JSON round trip unchanged: no tuples, non-string
    keys, NaN or infinities, or objects JSON cannot represent.
    """
    where = f"chunk[{i}] ({chunk.chunk_id!r}): metadata"
    if not isinstance(chunk.metadata, dict):
        raise ChunkValidationError(f"{where} must be a dict, got {type(chunk.metadata).__name__}")
    try:
        encoded = canonical_json(chunk.metadata)
    except (TypeError, ValueError) as exc:
        raise ChunkValidationError(f"{where} is not JSON-serializable: {exc}") from None
    if not _json_equal(json.loads(encoded), chunk.metadata):
        raise ChunkValidationError(f"{where} does not survive a JSON round trip unchanged")


def _json_equal(decoded: Any, original: Any) -> bool:
    """Equality that also tells 1, 1.0 and True apart, as JSON does."""
    if isinstance(original, dict):
        return (
            isinstance(decoded, dict)
            and decoded.keys() == original.keys()
            and all(_json_equal(decoded[k], original[k]) for k in original)
        )
    if isinstance(original, list):
        return (
            isinstance(decoded, list)
            and len(decoded) == len(original)
            and all(_json_equal(d, o) for d, o in zip(decoded, original))
        )
    return type(decoded) is type(original) and decoded == original


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------

class SteleAdapter(Protocol):
    """
    Implement this protocol to route parser artifacts through Stele.

    transform() is the only method the Dispatcher calls. It receives a
    SealedBundle (verified bytes, no paths, no DB handles, no target
    connections) and returns the chunks to be written.

    Adapters have no invalidation hook: removing a delivery's data is a
    target write, so the Dispatcher asks the TargetWriter that wrote it
    (TargetWriter.remove_delivery) and records the removal.
    """

    def transform(self, bundle: SealedBundle) -> list[SteleChunk]:
        """Read the sealed artifacts and return normalized chunks.

        Must be deterministic: a retried delivery must produce the same chunks.
        Must not write to any external store.
        Must not hold references to DB connections or target objects.
        """
        ...
