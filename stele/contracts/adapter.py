"""
Stele Phase H — adapter contract.

The SteleAdapter protocol is the only authorized write boundary between
parser output and downstream storage targets.

Rules:
  - Adapters receive a committed ArtifactRecord and return list[SteleChunk].
  - Adapters do NOT receive DB connections, file handles, or target objects.
  - Adapters do NOT call target-store APIs (e.g. LightRAG, Hindsight) directly.
  - All target writes are routed through the Dispatcher (contracts/dispatcher.py).
  - Invalid adapter output (wrong hashes, empty chunks) is rejected by the
    Dispatcher before any write reaches a target.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from ..ledger.models import ArtifactRecord


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

        if chunk.token_count < 0:
            raise ChunkValidationError(
                f"chunk[{i}] ({chunk.chunk_id!r}): token_count must be >= 0"
            )


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------

class SteleAdapter(Protocol):
    """
    Implement this protocol to route parser artifacts through Stele.

    transform() is the ONLY method called by the Dispatcher.  It receives a
    committed ArtifactRecord (no DB handles, no target connections) and returns
    the chunks to be written.

    on_invalidation() is intended to be called when a previously dispatched
    run_id is invalidated (Phase G).  Implementations must tombstone or remove
    the data they previously wrote to their target store.  NOTE: the
    Dispatcher does not call it yet — callers must invoke it themselves.
    """

    def transform(self, record: ArtifactRecord) -> list[SteleChunk]:
        """Read the committed artifact and return normalized chunks.

        Must not write to any external store.
        Must not hold references to DB connections or target objects.
        """
        ...

    def on_invalidation(self, run_id: UUID, reason: str) -> None:
        """Remove or tombstone all chunks associated with run_id."""
        ...
