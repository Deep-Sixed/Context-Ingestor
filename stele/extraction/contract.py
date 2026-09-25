"""
The canonical extraction contract: `stele.extraction`, version 1.

Every parser writes its own output format (MinerU's middle.json, Marker's
block tree, Docling's DoclingDocument, the ChatGPT splitter's conversation
files). An Extraction is the one shape downstream code reads instead: an
ordered list of units of content, each tied by an anchor to the exact sealed
bytes it came from.

    Extraction
      schema, schema_version        "stele.extraction", 1
      record_id, artifact_hash      the sealed ledger record it was derived from
      source_hash                   the input Snapshot (None for input-less runs)
      parser {name, version}        what produced the bundle
      normalizer {name, version}    the trusted code that derived the extraction
      units [Unit, ...]             in reading order

    Unit
      id                            "u000001", unique in the extraction
      kind                          heading | paragraph | list | table | code | formula |
                                    figure | caption | message | other
      text                          the unit's content, exactly as its anchor resolves
      order                         0, 1, 2, ... (reading order)
      page, bbox, level             when the source knows them, else null
      parent                        another unit's id (sections, conversation trees), or null
      anchor                        where the text lives in the sealed bundle:
          {artifact, digest, range: [start, end]}                   a UTF-8 byte range, or
          {artifact, digest, pointer: "/json/pointer", render: R}   a JSON value rendered by R
      attributes                    small JSON object of kind-specific facts

An Extraction is data derived by trusted host code (a normalizer), not
something a parser asserts. Its anchors make every unit checkable: the
trusted resolver (resolver.py) re-reads the anchored bytes from the evidence
archive and requires the unit's text to equal them exactly.

Serialization is canonical JSON (sorted keys, compact, UTF-8) and parsing is
strict: unknown fields, a different schema or version, a malformed anchor, or
any non-canonical encoding is refused, so one extraction has one digest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..archive.records import canonical_json, is_digest

SCHEMA = "stele.extraction"
SCHEMA_VERSION = 1

KINDS = frozenset({
    "heading", "paragraph", "list", "table", "code", "formula",
    "figure", "caption", "message", "other",
})


class ExtractionFormatError(ValueError):
    """Bytes or values that are not a valid stele.extraction/v1."""


@dataclass(frozen=True)
class Anchor:
    artifact: str                       # relative path in the bundle manifest
    digest: str                         # that artifact's sha256 at sealing
    range: tuple[int, int] | None = None
    pointer: str | None = None          # RFC 6901 JSON pointer
    render: str | None = None           # renderer name for pointer anchors

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, str) or not self.artifact:
            raise ExtractionFormatError("anchor.artifact must be a non-empty path")
        if not is_digest(self.digest):
            raise ExtractionFormatError(f"anchor.digest is not a SHA-256 digest: {self.digest!r}")
        if (self.range is None) == (self.pointer is None):
            raise ExtractionFormatError("an anchor has exactly one of range or pointer")
        if self.range is not None:
            start, end = self.range
            if not (type(start) is int and type(end) is int and 0 <= start <= end):
                raise ExtractionFormatError(f"invalid anchor range {self.range!r}")
            object.__setattr__(self, "range", (start, end))
            if self.render is not None:
                raise ExtractionFormatError("a range anchor has no renderer")
        else:
            if not isinstance(self.pointer, str) or not (self.pointer == "" or self.pointer.startswith("/")):
                raise ExtractionFormatError(f"invalid JSON pointer {self.pointer!r}")
            if not isinstance(self.render, str) or not self.render:
                raise ExtractionFormatError("a pointer anchor names its renderer")

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"artifact": self.artifact, "digest": self.digest}
        if self.range is not None:
            out["range"] = list(self.range)
        else:
            out["pointer"], out["render"] = self.pointer, self.render
        return out

    @classmethod
    def from_json(cls, obj: Any) -> "Anchor":
        if not isinstance(obj, dict):
            raise ExtractionFormatError("anchor is not an object")
        allowed = {"artifact", "digest", "range", "pointer", "render"}
        if set(obj) - allowed:
            raise ExtractionFormatError(f"unknown anchor fields {sorted(set(obj) - allowed)}")
        rng = obj.get("range")
        if rng is not None and (not isinstance(rng, list) or len(rng) != 2):
            raise ExtractionFormatError(f"invalid anchor range {rng!r}")
        return cls(
            artifact=obj.get("artifact"), digest=obj.get("digest"),
            range=tuple(rng) if rng is not None else None,
            pointer=obj.get("pointer"), render=obj.get("render"),
        )


@dataclass(frozen=True)
class Unit:
    id: str
    kind: str
    text: str
    order: int
    anchor: Anchor
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    level: int | None = None
    parent: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ExtractionFormatError("unit.id must be a non-empty string")
        if self.kind not in KINDS:
            raise ExtractionFormatError(f"unit {self.id}: unknown kind {self.kind!r}")
        if not isinstance(self.text, str) or not self.text:
            raise ExtractionFormatError(f"unit {self.id}: text must be a non-empty string")
        if type(self.order) is not int or self.order < 0:
            raise ExtractionFormatError(f"unit {self.id}: order must be a non-negative int")
        if self.page is not None and (type(self.page) is not int or self.page < 0):
            raise ExtractionFormatError(f"unit {self.id}: page must be a non-negative int")
        if self.bbox is not None:
            if len(self.bbox) != 4 or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                              for v in self.bbox):
                raise ExtractionFormatError(f"unit {self.id}: bbox must be four numbers")
            object.__setattr__(self, "bbox", tuple(float(v) for v in self.bbox))
        if self.level is not None and (type(self.level) is not int or self.level < 0):
            raise ExtractionFormatError(f"unit {self.id}: level must be a non-negative int")
        if not isinstance(self.attributes, dict):
            raise ExtractionFormatError(f"unit {self.id}: attributes must be an object")
        try:
            canonical_json(self.attributes)
        except (TypeError, ValueError) as exc:
            raise ExtractionFormatError(f"unit {self.id}: attributes are not JSON: {exc}") from exc

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "text": self.text, "order": self.order,
            "page": self.page, "bbox": list(self.bbox) if self.bbox is not None else None,
            "level": self.level, "parent": self.parent,
            "anchor": self.anchor.to_json(), "attributes": self.attributes,
        }

    @classmethod
    def from_json(cls, obj: Any) -> "Unit":
        fields = {"id", "kind", "text", "order", "page", "bbox", "level", "parent",
                  "anchor", "attributes"}
        if not isinstance(obj, dict) or set(obj) != fields:
            raise ExtractionFormatError(f"unit fields must be exactly {sorted(fields)}")
        return cls(
            id=obj["id"], kind=obj["kind"], text=obj["text"], order=obj["order"],
            anchor=Anchor.from_json(obj["anchor"]), page=obj["page"],
            bbox=tuple(obj["bbox"]) if obj["bbox"] is not None else None,
            level=obj["level"], parent=obj["parent"], attributes=obj["attributes"],
        )


@dataclass(frozen=True)
class Extraction:
    record_id: str
    artifact_hash: str
    source_hash: str | None
    parser: dict[str, str]
    normalizer: dict[str, Any]
    units: tuple[Unit, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", tuple(self.units))
        if not is_digest(self.artifact_hash):
            raise ExtractionFormatError("artifact_hash is not a SHA-256 digest")
        if self.source_hash is not None and not is_digest(self.source_hash):
            raise ExtractionFormatError("source_hash is not a SHA-256 digest")
        for name in ("parser", "normalizer"):
            value = getattr(self, name)
            if not isinstance(value, dict) or set(value) != {"name", "version"}:
                raise ExtractionFormatError(f"{name} must be {{name, version}}")
        ids = [u.id for u in self.units]
        if len(set(ids)) != len(ids):
            raise ExtractionFormatError("unit ids are not unique")
        if [u.order for u in self.units] != list(range(len(self.units))):
            raise ExtractionFormatError("units must be in reading order 0, 1, 2, ...")
        known = set(ids)
        for unit in self.units:
            if unit.parent is not None and (unit.parent not in known or unit.parent == unit.id):
                raise ExtractionFormatError(f"unit {unit.id}: parent {unit.parent!r} is not another unit")

    def to_canonical(self) -> bytes:
        return canonical_json({
            "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
            "record_id": self.record_id, "artifact_hash": self.artifact_hash,
            "source_hash": self.source_hash, "parser": self.parser,
            "normalizer": self.normalizer,
            "units": [u.to_json() for u in self.units],
        })

    @classmethod
    def from_canonical(cls, data: bytes) -> "Extraction":
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ExtractionFormatError(f"not UTF-8 JSON: {exc}") from exc
        fields = {"schema", "schema_version", "record_id", "artifact_hash", "source_hash",
                  "parser", "normalizer", "units"}
        if not isinstance(obj, dict) or set(obj) != fields:
            raise ExtractionFormatError(f"extraction fields must be exactly {sorted(fields)}")
        if obj["schema"] != SCHEMA or obj["schema_version"] != SCHEMA_VERSION:
            raise ExtractionFormatError(
                f"expected {SCHEMA} v{SCHEMA_VERSION}, got {obj['schema']!r} v{obj['schema_version']!r}"
            )
        if not isinstance(obj["units"], list):
            raise ExtractionFormatError("units must be a list")
        extraction = cls(
            record_id=obj["record_id"], artifact_hash=obj["artifact_hash"],
            source_hash=obj["source_hash"], parser=obj["parser"], normalizer=obj["normalizer"],
            units=tuple(Unit.from_json(u) for u in obj["units"]),
        )
        if extraction.to_canonical() != data:
            raise ExtractionFormatError("extraction is not in canonical form")
        return extraction


def number_units(units: Iterable[dict[str, Any]]) -> list[Unit]:
    """Build Units in order from dicts without id/order; ids are u000001, ..."""
    out = []
    for order, unit in enumerate(units):
        out.append(Unit(id=f"u{order + 1:06d}", order=order, **unit))
    return out
