"""
The trusted resolver: what does an anchor say, according to the evidence?

An Extraction claims that each unit's text is what its anchor points at. The
resolver checks that claim from first principles instead of trusting whoever
produced the extraction:

- the record must be SEALED in the live ledger (INVALIDATED only when the
  caller asks for it explicitly), and the extraction must name that record's
  artifact_hash, source_hash and parser, and the normalizer registered for
  that parser;
- the anchored artifact must be in the record's manifest under the digest the
  anchor names;
- its bytes are read from the evidence archive, which re-hashes them;
- a range anchor resolves to that exact UTF-8 byte range; a pointer anchor
  resolves the RFC 6901 pointer in the artifact's JSON and renders the value
  with a renderer from this module's fixed registry (never code named by the
  extraction itself).

verify() then requires every unit's text to equal its resolution exactly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..adapters.chatgpt import _render as render_chatgpt_message
from ..archive.store import ArchiveError
from ..contracts.adapter import SealedBundle
from ..ledger.models import ArtifactState
from ..ledger.store import LedgerStore, RecordNotFoundError
from .contract import Anchor, Extraction
from .normalizers import CHATGPT_RENDERER, NormalizeError, normalizer_id, parser_id


class ResolutionError(ValueError):
    """An anchor that does not resolve against the sealed evidence."""


def _render_json_string(value: Any) -> str:
    if not isinstance(value, str):
        raise ResolutionError("json-string renderer: value is not a string")
    return value


def _render_chatgpt(value: Any) -> str:
    if not isinstance(value, dict):
        raise ResolutionError(f"{CHATGPT_RENDERER} renderer: value is not a message object")
    return render_chatgpt_message(value)


# The only renderers a pointer anchor may name.
RENDERERS: dict[str, Callable[[Any], str]] = {
    "json-string": _render_json_string,
    CHATGPT_RENDERER: _render_chatgpt,
}


def resolve_pointer(doc: Any, pointer: str) -> Any:
    """The value at an RFC 6901 JSON pointer (ResolutionError if absent)."""
    if pointer == "":
        return doc
    value = doc
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token not in value:
                raise ResolutionError(f"pointer {pointer!r}: no member {token!r}")
            value = value[token]
        elif isinstance(value, list):
            if not token.isdigit() or (token != "0" and token.startswith("0")):
                raise ResolutionError(f"pointer {pointer!r}: {token!r} is not an array index")
            index = int(token)
            if index >= len(value):
                raise ResolutionError(f"pointer {pointer!r}: index {index} is out of range")
            value = value[index]
        else:
            raise ResolutionError(f"pointer {pointer!r}: cannot descend into a scalar at {token!r}")
    return value


@dataclass
class _ArtifactCache:
    """The last artifact read (and parsed), so a run of units on one file reads it once."""
    digest: str | None = None
    data: bytes = b""
    parsed: Any = field(default=None)
    has_parsed: bool = False


class BundleResolver:
    """Resolves anchors against one sealed bundle's verified bytes."""

    def __init__(self, bundle: SealedBundle) -> None:
        self.bundle = bundle
        self._cache = _ArtifactCache()

    def _bytes(self, anchor: Anchor) -> bytes:
        sealed = self.bundle.manifest.get(anchor.artifact)
        if sealed is None:
            raise ResolutionError(
                f"{anchor.artifact!r} is not an artifact of record {self.bundle.record_id}"
            )
        if sealed != anchor.digest:
            raise ResolutionError(
                f"{anchor.artifact!r}: anchor digest {anchor.digest} is not the sealed {sealed}"
            )
        if self._cache.digest != sealed:
            try:
                data = self.bundle.read(anchor.artifact)
            except ArchiveError as exc:
                raise ResolutionError(f"{anchor.artifact!r}: evidence unreadable: {exc}") from exc
            self._cache = _ArtifactCache(digest=sealed, data=data)
        return self._cache.data

    def _json(self, anchor: Anchor) -> Any:
        data = self._bytes(anchor)
        if not self._cache.has_parsed:
            try:
                self._cache.parsed = json.loads(data)
            except ValueError as exc:
                raise ResolutionError(f"{anchor.artifact!r} is not JSON: {exc}") from exc
            self._cache.has_parsed = True
        return self._cache.parsed

    def resolve(self, anchor: Anchor) -> str:
        """The text the sealed evidence gives for this anchor."""
        if anchor.range is not None:
            data = self._bytes(anchor)
            start, end = anchor.range
            if end > len(data):
                raise ResolutionError(
                    f"{anchor.artifact!r}: range {start}-{end} is past the end ({len(data)} bytes)"
                )
            try:
                return data[start:end].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ResolutionError(
                    f"{anchor.artifact!r}: bytes {start}-{end} are not UTF-8: {exc}"
                ) from exc
        renderer = RENDERERS.get(anchor.render)
        if renderer is None:
            raise ResolutionError(f"unknown renderer {anchor.render!r}")
        return renderer(resolve_pointer(self._json(anchor), anchor.pointer))

    def verify(self, extraction: Extraction) -> list[str]:
        """Problems with the extraction against this bundle; [] means every unit checks out."""
        problems: list[str] = []
        if extraction.record_id != self.bundle.record_id:
            problems.append(
                f"extraction is of record {extraction.record_id}, not {self.bundle.record_id}"
            )
        if extraction.artifact_hash != self.bundle.artifact_hash:
            problems.append(
                f"extraction names artifact_hash {extraction.artifact_hash}, "
                f"the record sealed {self.bundle.artifact_hash}"
            )
        if extraction.source_hash != self.bundle.source_hash:
            problems.append(
                f"extraction names source_hash {extraction.source_hash}, "
                f"the record has {self.bundle.source_hash}"
            )
        if extraction.parser != parser_id(self.bundle):
            problems.append(
                f"extraction names parser {extraction.parser}, the record has {parser_id(self.bundle)}"
            )
        try:
            expected = normalizer_id(self.bundle)
        except NormalizeError as exc:
            problems.append(str(exc))
        else:
            if extraction.normalizer != expected:
                problems.append(
                    f"extraction names normalizer {extraction.normalizer}, "
                    f"the record's parser is normalized by {expected}"
                )
        if problems:
            return problems
        for unit in extraction.units:
            try:
                resolved = self.resolve(unit.anchor)
            except ResolutionError as exc:
                problems.append(f"unit {unit.id}: {exc}")
                continue
            if resolved != unit.text:
                problems.append(f"unit {unit.id}: text does not match its anchor")
        return problems


class Resolver:
    """Resolves anchors of ledger records, checking the live record state first."""

    def __init__(self, ledger: LedgerStore, *, allow_invalidated: bool = False) -> None:
        self.ledger = ledger
        self.allow_invalidated = allow_invalidated

    def bundle(self, record_id: str) -> SealedBundle:
        try:
            record = self.ledger.get(record_id)
        except RecordNotFoundError:
            raise ResolutionError(f"no record {record_id} in the ledger") from None
        allowed = {ArtifactState.SEALED}
        if self.allow_invalidated:
            allowed.add(ArtifactState.INVALIDATED)
        if record.state not in allowed:
            raise ResolutionError(
                f"record {record_id} is {record.state.value}; only sealed evidence resolves"
            )
        return SealedBundle.from_record(record, self.ledger.archive)

    def resolve(self, record_id: str, anchor: Anchor) -> str:
        return BundleResolver(self.bundle(record_id)).resolve(anchor)

    def verify(self, extraction: Extraction) -> list[str]:
        try:
            bundle = self.bundle(extraction.record_id)
        except ResolutionError as exc:
            return [str(exc)]
        return BundleResolver(bundle).verify(extraction)
