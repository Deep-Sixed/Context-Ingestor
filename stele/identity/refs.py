"""
Typed references to Stele evidence, each with exactly one canonical string.

    stele:source:<source_id>
    stele:snapshot:<file|tree>:<sha256>
    stele:record:<record_id>@<artifact_hash>
    stele:observation:<record_id>@<sha256 of the canonical Observation>
    stele:anchor:<record_id>@<artifact_hash>/<artifact>@<digest>#bytes=<start>-<end>
    stele:anchor:<record_id>@<artifact_hash>/<artifact>@<digest>#pointer=<pointer>&render=<name>
    stele:event:<seq>@<hash>

Every reference commits to content: a digest of the bytes, tree, record or
chain position it names. It therefore resolves (resolver.py) only while that
evidence is unchanged, and it can be passed around as a plain string.

parse_ref() is strict: a string is accepted only if formatting the parsed
reference gives it back byte for byte, so one reference has one spelling.
In anchors, the artifact path and the JSON pointer are percent-encoded
(RFC 3986: unreserved characters and "/" stay literal, everything else is
%XX with upper-case hex, as urllib.parse.quote writes it).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Union
from urllib.parse import quote, unquote

from ..archive.records import SnapshotKind, canonical_json, is_digest
from ..extraction.contract import Anchor, Extraction, ExtractionFormatError, Unit

PREFIX = "stele:"

# Record ids are UUIDs today; any id made of these characters can be named.
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_UINT = re.compile(r"0|[1-9][0-9]*")


class RefFormatError(ValueError):
    """A string that is not the canonical form of any Stele reference."""


def _check_record_id(record_id: object) -> str:
    if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
        raise RefFormatError(f"not a record id: {record_id!r}")
    return record_id


def _check_digest(value: object, what: str) -> str:
    if not is_digest(value):
        raise RefFormatError(f"{what} is not a SHA-256 hex digest: {value!r}")
    return value  # type: ignore[return-value]


def _split_at(text: str, what: str) -> tuple[str, str]:
    """'<left>@<digest>' -> (left, digest); the digest never contains '@'."""
    left, sep, digest = text.rpartition("@")
    if not sep:
        raise RefFormatError(f"{what}: expected <...>@<sha256>, got {text!r}")
    return left, digest


def _uint(text: str, what: str) -> int:
    if not _UINT.fullmatch(text):
        raise RefFormatError(f"{what} is not a non-negative integer: {text!r}")
    return int(text)


@dataclass(frozen=True)
class SourceRef:
    """A Source record (a descriptive locator) by its source_id."""

    source_id: str

    def __post_init__(self) -> None:
        _check_digest(self.source_id, "source_id")

    def __str__(self) -> str:
        return f"{PREFIX}source:{self.source_id}"

    @classmethod
    def _parse(cls, rest: str) -> "SourceRef":
        return cls(rest)


@dataclass(frozen=True)
class SnapshotRef:
    """The exact staged bytes a parser saw: (kind, digest)."""

    kind: SnapshotKind
    digest: str

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", SnapshotKind(self.kind))
        except ValueError:
            raise RefFormatError(f"not a snapshot kind: {self.kind!r}") from None
        _check_digest(self.digest, "snapshot digest")

    def __str__(self) -> str:
        return f"{PREFIX}snapshot:{self.kind.value}:{self.digest}"

    @classmethod
    def _parse(cls, rest: str) -> "SnapshotRef":
        kind, sep, digest = rest.partition(":")
        if not sep:
            raise RefFormatError(f"snapshot: expected <kind>:<sha256>, got {rest!r}")
        return cls(kind, digest)  # type: ignore[arg-type]


@dataclass(frozen=True)
class RecordRef:
    """A ledger record together with the output tree it sealed."""

    record_id: str
    artifact_hash: str

    def __post_init__(self) -> None:
        _check_record_id(self.record_id)
        _check_digest(self.artifact_hash, "artifact_hash")

    def __str__(self) -> str:
        return f"{PREFIX}record:{self.record_id}@{self.artifact_hash}"

    @classmethod
    def _parse(cls, rest: str) -> "RecordRef":
        return cls(*_split_at(rest, "record"))


@dataclass(frozen=True)
class Observation:
    """One run's observation: this Source was seen as this Snapshot at this time.

    observed_at is the record's created_at in ISO 8601 (UTC). source_id is None when the run was given no Source.
    """

    record_id: str
    source_id: str | None
    snapshot: SnapshotRef
    observed_at: str

    SCHEMA = "stele.observation"
    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        _check_record_id(self.record_id)
        if self.source_id is not None:
            _check_digest(self.source_id, "source_id")
        if not isinstance(self.snapshot, SnapshotRef):
            raise RefFormatError("observation.snapshot must be a SnapshotRef")
        try:
            datetime.fromisoformat(self.observed_at)
        except (TypeError, ValueError):
            raise RefFormatError(f"observed_at is not ISO 8601: {self.observed_at!r}") from None

    def to_canonical(self) -> bytes:
        return canonical_json({
            "schema": self.SCHEMA, "schema_version": self.SCHEMA_VERSION,
            "record_id": self.record_id, "source_id": self.source_id,
            "snapshot": {"kind": self.snapshot.kind.value, "digest": self.snapshot.digest},
            "observed_at": self.observed_at,
        })

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_canonical()).hexdigest()

    @property
    def ref(self) -> "ObservationRef":
        return ObservationRef(self.record_id, self.digest)


@dataclass(frozen=True)
class ObservationRef:
    """An Observation, named by its record and the digest of its canonical form."""

    record_id: str
    digest: str

    def __post_init__(self) -> None:
        _check_record_id(self.record_id)
        _check_digest(self.digest, "observation digest")

    def __str__(self) -> str:
        return f"{PREFIX}observation:{self.record_id}@{self.digest}"

    @classmethod
    def _parse(cls, rest: str) -> "ObservationRef":
        return cls(*_split_at(rest, "observation"))


@dataclass(frozen=True)
class AnchorRef:
    """One span of one sealed artifact: an extraction Anchor, made addressable."""

    record: RecordRef
    anchor: Anchor

    def __post_init__(self) -> None:
        if not isinstance(self.record, RecordRef) or not isinstance(self.anchor, Anchor):
            raise RefFormatError("an AnchorRef is a RecordRef and an extraction Anchor")

    def __str__(self) -> str:
        a = self.anchor
        if a.range is not None:
            fragment = f"bytes={a.range[0]}-{a.range[1]}"
        else:
            fragment = f"pointer={quote(a.pointer)}&render={quote(a.render, safe='')}"
        return (
            f"{PREFIX}anchor:{self.record.record_id}@{self.record.artifact_hash}"
            f"/{quote(a.artifact)}@{a.digest}#{fragment}"
        )

    @classmethod
    def _parse(cls, rest: str) -> "AnchorRef":
        location, sep, fragment = rest.partition("#")
        if not sep:
            raise RefFormatError(f"anchor: missing #bytes= or #pointer= in {rest!r}")
        record_part, slash, artifact_part = location.partition("/")
        if not slash:
            raise RefFormatError(f"anchor: expected <record>/<artifact>, got {location!r}")
        record = RecordRef(*_split_at(record_part, "anchor record"))
        artifact, digest = _split_at(artifact_part, "anchor artifact")
        try:
            if fragment.startswith("bytes="):
                start, dash, end = fragment[len("bytes="):].partition("-")
                if not dash:
                    raise RefFormatError(f"anchor: bad byte range {fragment!r}")
                anchor = Anchor(artifact=unquote(artifact), digest=digest,
                                range=(_uint(start, "range start"), _uint(end, "range end")))
            elif fragment.startswith("pointer="):
                pointer, amp, render = fragment[len("pointer="):].partition("&render=")
                if not amp:
                    raise RefFormatError(f"anchor: pointer without &render= in {fragment!r}")
                anchor = Anchor(artifact=unquote(artifact), digest=digest,
                                pointer=unquote(pointer), render=unquote(render))
            else:
                raise RefFormatError(f"anchor: unknown fragment {fragment!r}")
        except ExtractionFormatError as exc:
            raise RefFormatError(f"anchor: {exc}") from exc
        return cls(record, anchor)


@dataclass(frozen=True)
class EventRef:
    """An event in the ledger's hash chain: (seq, hash)."""

    seq: int
    hash: str

    def __post_init__(self) -> None:
        if type(self.seq) is not int or self.seq < 1:
            raise RefFormatError(f"event seq must be a positive int, got {self.seq!r}")
        _check_digest(self.hash, "event hash")

    def __str__(self) -> str:
        return f"{PREFIX}event:{self.seq}@{self.hash}"

    @classmethod
    def _parse(cls, rest: str) -> "EventRef":
        seq, digest = _split_at(rest, "event")
        return cls(_uint(seq, "event seq"), digest)


Ref = Union[SourceRef, SnapshotRef, RecordRef, ObservationRef, AnchorRef, EventRef]

_TYPES: dict[str, Any] = {
    "source": SourceRef, "snapshot": SnapshotRef, "record": RecordRef,
    "observation": ObservationRef, "anchor": AnchorRef, "event": EventRef,
}


def parse_ref(text: str) -> Ref:
    """The reference a canonical string names (RefFormatError otherwise)."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise RefFormatError(f"not a Stele reference: {text!r}")
    kind, sep, rest = text[len(PREFIX):].partition(":")
    if not sep or kind not in _TYPES:
        raise RefFormatError(f"unknown reference type in {text!r}")
    ref = _TYPES[kind]._parse(rest)
    if str(ref) != text:
        raise RefFormatError(f"not the canonical form (that is {str(ref)!r}): {text!r}")
    return ref


# ---------------------------------------------------------------------------
# From existing objects
# ---------------------------------------------------------------------------

def record_ref(record: Any) -> RecordRef:
    """The reference of a ledger ArtifactRecord (or SealedBundle)."""
    return RecordRef(record.record_id, record.artifact_hash)


def observation_of(record: Any) -> Observation:
    """What a record observed: its Source (if any) as its input Snapshot."""
    if record.source_hash is None or record.source_kind is None:
        raise ValueError(f"record {record.record_id} has no input Snapshot to have observed")
    return Observation(
        record_id=record.record_id,
        source_id=record.source_id,
        snapshot=SnapshotRef(record.source_kind, record.source_hash),
        observed_at=record.created_at.isoformat(),
    )


def anchor_ref(extraction: Extraction, unit: Unit) -> AnchorRef:
    """The reference of one unit's anchor in an Extraction."""
    return AnchorRef(RecordRef(extraction.record_id, extraction.artifact_hash), unit.anchor)


def event_ref(event: Any) -> EventRef:
    """The reference of an event from the hash-chained log."""
    return EventRef(event.seq, event.hash)
