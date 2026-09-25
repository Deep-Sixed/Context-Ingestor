"""
Normalizers: trusted host code that turns a sealed bundle into an Extraction.

A normalizer reads only through the SealedBundle, so every byte it sees has
just been re-verified against the sealed digest, and it anchors every unit it
emits so the resolver can check it later without trusting the normalizer's
word. The registry picks one by the record's parser name.

markdown   (mineru, marker, docling)
    The packaged parsers all write Markdown (stele-parser.json names it under
    outputs.markdown). The document is split into blocks: blank lines separate
    blocks except inside fenced code. Each block's text is its exact bytes, and
    its anchor is that byte range. Kind comes from the block's syntax
    (ATX or setext heading, fence, pipe or HTML table, list item, image,
    $$ formula); headings nest by level, and every other block's parent is
    the heading it sits under.

chatgpt    (chatgpt-export-split)
    One message unit per node with content, on every branch, via
    ChatGPTExportAdapter. The anchor is a JSON pointer to the node's message in
    its conversation file, rendered by the same trusted renderer the adapter
    uses; parent is the nearest ancestor message's unit.

Parser-specific layout (pages and bboxes from Docling, Marker or MinerU JSON)
is not normalized yet: those units carry page = bbox = null.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..adapters.chatgpt import SPLITTER, ChatGPTExportAdapter
from ..contracts.adapter import SealedBundle
from .contract import Anchor, Extraction, ExtractionFormatError, number_units

PARSER_MANIFEST = "stele-parser.json"
DEFAULT_MARKDOWN = "document.md"
MARKDOWN_PARSERS = frozenset({"mineru", "marker", "docling"})

MARKDOWN_NORMALIZER = {"name": "markdown", "version": "1"}
CHATGPT_NORMALIZER = {"name": "chatgpt", "version": "1"}

CHATGPT_RENDERER = "chatgpt-message"


class NormalizeError(ValueError):
    """The bundle cannot be normalized (no normalizer, or malformed output)."""


def normalize(bundle: SealedBundle) -> Extraction:
    """The canonical Extraction of a sealed bundle, by its parser's normalizer."""
    return normalizer_for(bundle)(bundle)


def normalizer_for(bundle: SealedBundle) -> Callable[[SealedBundle], Extraction]:
    name = bundle.parser.name if bundle.parser is not None else None
    if name in MARKDOWN_PARSERS:
        return normalize_markdown
    if name == SPLITTER:
        return normalize_chatgpt
    raise NormalizeError(f"no extraction normalizer for parser {name!r}")


def _extraction(bundle: SealedBundle, normalizer: dict[str, str], units: list[dict]) -> Extraction:
    parser = bundle.parser
    return Extraction(
        record_id=bundle.record_id,
        artifact_hash=bundle.artifact_hash,
        source_hash=bundle.source_hash,
        parser={"name": parser.name, "version": parser.version} if parser else
               {"name": "unknown", "version": "unknown"},
        normalizer=dict(normalizer),
        units=number_units(units),
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_FENCE = re.compile(rb"^ {0,3}(`{3,}|~{3,})")
_ATX = re.compile(rb"^ {0,3}(#{1,6})(?:[ \t]|$)")
_SETEXT = re.compile(rb"^ {0,3}(=+|-+)[ \t]*$")
_LIST = re.compile(rb"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]")
_TABLE = re.compile(rb"^[ \t]*(?:\||<table\b)", re.IGNORECASE)


def markdown_path(bundle: SealedBundle) -> str:
    """The Markdown output the parser's stele-parser.json names."""
    if PARSER_MANIFEST in bundle.manifest:
        try:
            outputs = json.loads(bundle.read(PARSER_MANIFEST)).get("outputs") or {}
        except (ValueError, AttributeError) as exc:
            raise NormalizeError(f"{PARSER_MANIFEST} is malformed: {exc}") from exc
        path = outputs.get("markdown") if isinstance(outputs, dict) else None
        if path is not None:
            if path not in bundle.manifest:
                raise NormalizeError(f"{PARSER_MANIFEST} names {path!r}, which is not in the bundle")
            return path
    if DEFAULT_MARKDOWN in bundle.manifest:
        return DEFAULT_MARKDOWN
    raise NormalizeError("bundle has no Markdown output")


def markdown_blocks(data: bytes) -> list[tuple[int, int]]:
    """Byte ranges of the blocks of a Markdown document, in order.

    A block runs from its first non-blank line to the end of its last line,
    excluding that line's line ending. Blank lines end a block; so do an ATX
    heading (always a block of its own), a setext underline under a one-line
    paragraph, and a fenced code block, which is one block from its opening
    to its closing fence (or to the end of the document if it never closes)
    whatever blank lines it holds.
    """
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    lines_in_block = 0
    fence: bytes | None = None
    pos = 0

    def close() -> None:
        nonlocal start, lines_in_block
        if start is not None:
            blocks.append((start, end))
        start, lines_in_block = None, 0

    for line in data.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        line_end = pos + len(body)
        if fence is not None:
            end = line_end
            m = _FENCE.match(body)
            if m and m.group(1)[:1] == fence[:1] and len(m.group(1)) >= len(fence) \
                    and not body[m.end():].strip():
                fence = None
                close()
        elif not body.strip():
            close()
        elif m := _FENCE.match(body):
            close()                                # a fence interrupts a paragraph
            start, end, fence = pos, line_end, m.group(1)
        elif _ATX.match(body):
            close()
            start, end = pos, line_end
            close()
        else:
            if start is None:
                start = pos
            end = line_end
            lines_in_block += 1
            if lines_in_block == 2 and _SETEXT.match(body) \
                    and not _LIST.match(data[start:end].splitlines()[0]):
                close()
        pos += len(line)
    close()
    return blocks


def markdown_kind(block: bytes) -> tuple[str, int | None]:
    """(kind, heading level or None) of one block from its syntax."""
    lines = block.splitlines()
    first = lines[0]
    if m := _ATX.match(first):
        return "heading", len(m.group(1))
    if len(lines) == 2 and (m := _SETEXT.match(lines[1])) and not _LIST.match(first):
        return "heading", 1 if m.group(1).startswith(b"=") else 2
    if _FENCE.match(first):
        return "code", None
    if _TABLE.match(first):
        return "table", None
    if _LIST.match(first):
        return "list", None
    stripped = block.strip()
    if stripped.startswith(b"$$"):
        return "formula", None
    if stripped.startswith(b"![") and b"\n" not in stripped:
        return "figure", None
    return "paragraph", None


def normalize_markdown(bundle: SealedBundle) -> Extraction:
    path = markdown_path(bundle)
    data = bundle.read(path)
    digest = bundle.manifest[path]
    units: list[dict[str, Any]] = []
    headings: list[tuple[int, str]] = []    # open sections: (level, unit id)
    for start, end in markdown_blocks(data):
        block = data[start:end]
        try:
            text = block.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NormalizeError(f"{path} bytes {start}-{end} are not UTF-8: {exc}") from exc
        kind, level = markdown_kind(block)
        unit_id = f"u{len(units) + 1:06d}"       # the id number_units will assign
        if kind == "heading":
            while headings and headings[-1][0] >= level:
                headings.pop()
        parent = headings[-1][1] if headings else None
        if kind == "heading":
            headings.append((level, unit_id))
        units.append({
            "kind": kind, "text": text, "level": level, "parent": parent,
            "anchor": Anchor(artifact=path, digest=digest, range=(start, end)),
        })
    return _extraction(bundle, MARKDOWN_NORMALIZER, units)


# ---------------------------------------------------------------------------
# ChatGPT
# ---------------------------------------------------------------------------

def json_pointer(*tokens: str) -> str:
    """RFC 6901 pointer from raw reference tokens."""
    return "".join("/" + t.replace("~", "~0").replace("/", "~1") for t in tokens)


_CHATGPT_ATTRIBUTES = (
    "conversation_id", "conversation_title", "node_id", "message_id", "role",
    "author_name", "recipient", "content_type", "create_time", "depth",
    "sibling_index", "sibling_count", "is_leaf", "on_current_branch",
)


def normalize_chatgpt(bundle: SealedBundle) -> Extraction:
    units: list[dict[str, Any]] = []
    unit_of: dict[tuple[str, str], str] = {}     # (conversation, node) -> unit id
    for chunk in ChatGPTExportAdapter().iter_chunks(bundle):
        meta = chunk.metadata
        conv, node = meta["conversation_id"], meta["node_id"]
        unit_id = f"u{len(units) + 1:06d}"
        unit_of[(conv, node)] = unit_id
        parent = meta["parent_node_id"]
        artifact = meta["export_file"]
        units.append({
            "kind": "message",
            "text": chunk.content,
            "parent": unit_of[(conv, parent)] if parent is not None else None,
            "anchor": Anchor(
                artifact=artifact, digest=bundle.manifest[artifact],
                pointer=json_pointer("mapping", node, "message"), render=CHATGPT_RENDERER,
            ),
            "attributes": {k: meta[k] for k in _CHATGPT_ATTRIBUTES if meta.get(k) is not None},
        })
    try:
        return _extraction(bundle, CHATGPT_NORMALIZER, units)
    except ExtractionFormatError as exc:
        raise NormalizeError(str(exc)) from exc
