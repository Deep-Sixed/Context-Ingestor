"""Snapshot and Source identity records with canonical serialization.

Records are small immutable dataclasses. Their canonical form is UTF-8 JSON
with sorted keys, no insignificant whitespace, and an explicit ``schema`` and
``schema_version``. Parsing is strict: unknown fields, a different schema
version, or any non-canonical encoding is rejected, so one record has exactly
one byte representation and one digest.
"""
from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

SCHEMA_VERSION = 1

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class RecordFormatError(ValueError):
    """Raised when bytes are not a canonical record of the expected schema."""


def is_digest(value: object) -> bool:
    """True for a lowercase hex SHA-256 digest."""
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def canonical_json(obj: dict[str, Any]) -> bytes:
    """Sorted-key, compact, UTF-8 JSON: the one byte form of a record."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _parse(data: bytes, schema: str, fields: set[str]) -> dict[str, Any]:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecordFormatError(f"{schema} record is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RecordFormatError(f"{schema} record is not a JSON object")
    if obj.get("schema") != schema:
        raise RecordFormatError(f"expected schema {schema!r}, got {obj.get('schema')!r}")
    if obj.get("schema_version") != SCHEMA_VERSION:
        raise RecordFormatError(
            f"unsupported {schema} schema_version {obj.get('schema_version')!r} "
            f"(this Stele reads version {SCHEMA_VERSION})"
        )
    expected = fields | {"schema", "schema_version"}
    if set(obj) != expected:
        raise RecordFormatError(
            f"{schema} record fields {sorted(obj)} != expected {sorted(expected)}"
        )
    if canonical_json(obj) != data:
        raise RecordFormatError(f"{schema} record is not in canonical form")
    return obj


class SnapshotKind(str, enum.Enum):
    """What a Snapshot digest addresses."""

    FILE = "file"  # the digest is the blob of the file's bytes
    TREE = "tree"  # the digest is the blob of a canonical tree object


@dataclass(frozen=True)
class Snapshot:
    """The exact staged bytes of a source at a moment in time.

    Identity is (kind, digest), and digest always equals the StagedInput.sha256
    that staging computed, i.e. the hash of the bytes the parser saw:

    - FILE: the SHA-256 of the file bytes, stored as one blob.
    - TREE: the SHA-256 of the canonical tree object (see
      stele.ledger.hashing.encode_manifest) listing every file's relative
      POSIX path and blob digest. That is exactly sha256_manifest(manifest).

    The kind is part of identity because a tree object's bytes could also be
    the contents of some file (an empty file and an empty directory share a
    digest). size and file_count are derived from content; neither names nor
    paths are recorded: paths are locations, not evidence.
    """

    kind: SnapshotKind
    digest: str
    size: int        # total content bytes (file size, or sum over tree files)
    file_count: int  # 1 for FILE; number of files for TREE

    SCHEMA = "stele.snapshot"

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", SnapshotKind(self.kind))
        if not is_digest(self.digest):
            raise ValueError(f"not a SHA-256 hex digest: {self.digest!r}")
        for name in ("size", "file_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        if self.kind is SnapshotKind.FILE and self.file_count != 1:
            raise ValueError("a FILE snapshot has exactly one file")

    def to_canonical(self) -> bytes:
        return canonical_json({
            "schema": self.SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind.value,
            "digest": self.digest,
            "size": self.size,
            "file_count": self.file_count,
        })

    @classmethod
    def from_canonical(cls, data: bytes) -> "Snapshot":
        obj = _parse(data, cls.SCHEMA, {"kind", "digest", "size", "file_count"})
        try:
            return cls(
                kind=SnapshotKind(obj["kind"]),
                digest=obj["digest"],
                size=obj["size"],
                file_count=obj["file_count"],
            )
        except ValueError as exc:
            raise RecordFormatError(f"invalid snapshot record: {exc}") from exc


@dataclass(frozen=True)
class Source:
    """Where material came from, e.g. a URI or an original host path.

    The locator is descriptive only. It does not identify evidence (a path can
    point at different bytes tomorrow); Snapshots do. A Source's identity,
    source_id, is the SHA-256 of its canonical serialization. Linking a Source
    to the Snapshots taken of it is a per-run fact recorded by the ledger.
    """

    locator: str

    SCHEMA = "stele.source"

    def __post_init__(self) -> None:
        if not isinstance(self.locator, str) or not self.locator:
            raise ValueError("Source.locator must be a non-empty string")

    @classmethod
    def from_path(cls, path: PurePath | str) -> "Source":
        """Source for a host path, with POSIX separators on every platform."""
        return cls(locator=PurePath(path).as_posix())

    @property
    def source_id(self) -> str:
        return hashlib.sha256(self.to_canonical()).hexdigest()

    def to_canonical(self) -> bytes:
        return canonical_json({
            "schema": self.SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "locator": self.locator,
        })

    @classmethod
    def from_canonical(cls, data: bytes) -> "Source":
        obj = _parse(data, cls.SCHEMA, {"locator"})
        try:
            return cls(locator=obj["locator"])
        except ValueError as exc:
            raise RecordFormatError(f"invalid source record: {exc}") from exc
