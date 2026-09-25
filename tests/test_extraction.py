"""
Canonical extraction contract and trusted resolver (roadmap #15).

  1. stele.extraction/v1 is strict: canonical JSON round-trips, anything else
     (unknown fields, wrong schema, non-canonical bytes, bad anchors, broken
     order or parents) is refused.
  2. The Markdown normalizer (MinerU, Marker, Docling) splits blocks, types
     them and anchors each to its exact byte range.
  3. The resolver re-reads anchors from sealed, archive-verified bytes: an
     honest extraction verifies, and a tampered text, digest, artifact, range,
     renderer or record is caught. Only sealed records resolve.
  4. The ChatGPT normalizer anchors each message by JSON pointer; end to end
     through the Wasm splitter it verifies against the ledger.
  5. ExtractionAdapter delivers units through the Dispatcher, and refuses a
     normalizer that misquotes the evidence.
"""
from __future__ import annotations

import dataclasses
import json
import uuid
from pathlib import Path

import pytest

from stele.adapters import ExtractionAdapter
from stele.archive import Source
from stele.contracts.adapter import LightRAGTarget, SealedBundle
from stele.contracts.dispatcher import Dispatcher
from stele.extraction import (
    Anchor, BundleResolver, Extraction, ExtractionFormatError, NormalizeError, ResolutionError,
    Resolver, Unit, normalize,
)
from stele.extraction import normalizers as normalize_mod
from stele.extraction.normalizers import json_pointer, markdown_blocks
from stele.extraction.resolver import resolve_pointer
from stele.extractors import CHATGPT_EXPORT_SPLIT_SPEC
from stele.ledger.delivery import DeliveryStatus
from stele.ledger.models import ParserIdentity
from stele.ledger.transaction import record_run
from stele.replay.parsers import run_parser
from tests.ledger_helpers import open_ledger

DOC = (
    "# Results\n"
    "\n"
    "We measured *everything*.\n"
    "Twice — naïvely.\n"
    "\n"
    "## Method\n"
    "\n"
    "- step one\n"
    "- step two\n"
    "\n"
    "```python\n"
    "x = 1\n"
    "\n"
    "y = 2\n"
    "```\n"
    "\n"
    "| a | b |\n"
    "|---|---|\n"
    "| 1 | 2 |\n"
    "\n"
    "$$E = mc^2$$\n"
    "\n"
    "![Figure 1](images/fig1.png)\n"
    "\n"
    "Discussion\n"
    "==========\n"
    "\n"
    "Closing words.\n"
).encode("utf-8")


def _sealed_markdown(tmp_path: Path, markdown: bytes = DOC, *, parser: str = "marker"):
    """A SEALED record laid out the way the packaged parsers write their output."""
    ledger = open_ledger(tmp_path / "ledger.db")
    out = tmp_path / "out"
    out.mkdir()
    (out / "document.md").write_bytes(markdown)
    (out / "stele-parser.json").write_text(json.dumps({
        "parser": parser, "version": "1.0", "outputs": {"markdown": "document.md"},
    }))
    record = ledger.create_pending(
        run_id=str(uuid.uuid4()), artifact_dir=out,
        artifact_paths=sorted(out.iterdir()),
        parser=ParserIdentity(parser, "1.0"), parser_config={},
    )
    record = ledger.seal(record.record_id)
    return ledger, record


def _bundle(ledger, record) -> SealedBundle:
    return SealedBundle.from_record(ledger.get(record.record_id), ledger.archive)


def _replace_unit(extraction: Extraction, index: int, **changes) -> Extraction:
    units = list(extraction.units)
    units[index] = dataclasses.replace(units[index], **changes)
    return dataclasses.replace(extraction, units=tuple(units))


# ---------------------------------------------------------------------------
# 1 — the format
# ---------------------------------------------------------------------------

DIGEST = "a" * 64


def _unit(i: int, **kw) -> Unit:
    base = dict(id=f"u{i + 1:06d}", kind="paragraph", text=f"text {i}", order=i,
                anchor=Anchor(artifact="document.md", digest=DIGEST, range=(0, 4)))
    base.update(kw)
    return Unit(**base)


def _extraction(*units: Unit) -> Extraction:
    return Extraction(
        record_id="rec", artifact_hash=DIGEST, source_hash=None,
        parser={"name": "marker", "version": "1"}, normalizer={"name": "markdown", "version": "1"},
        units=units,
    )


class TestFormat:

    def test_canonical_round_trip(self) -> None:
        extraction = _extraction(
            _unit(0, kind="heading", level=1),
            _unit(1, parent="u000001", page=3, bbox=(1, 2, 3.5, 4), attributes={"lang": "en"}),
            _unit(2, kind="message", anchor=Anchor(
                artifact="c.json", digest=DIGEST, pointer="/mapping/a~1b/message",
                render="chatgpt-message")),
        )
        data = extraction.to_canonical()
        again = Extraction.from_canonical(data)
        assert again == extraction
        assert again.to_canonical() == data
        assert again.units[1].bbox == (1.0, 2.0, 3.5, 4.0)

    def test_non_canonical_bytes_are_refused(self) -> None:
        data = _extraction(_unit(0)).to_canonical()
        pretty = json.dumps(json.loads(data), indent=2).encode()
        with pytest.raises(ExtractionFormatError, match="canonical"):
            Extraction.from_canonical(pretty)

    @pytest.mark.parametrize("mutate, message", [
        (lambda o: o.update(extra=1), "fields"),
        (lambda o: o.update(schema="other"), "expected"),
        (lambda o: o.update(schema_version=2), "expected"),
        (lambda o: o["units"][0].update(colour="red"), "unit fields"),
        (lambda o: o["units"][0].update(kind="banner"), "unknown kind"),
        (lambda o: o["units"][0].update(text=""), "non-empty"),
        (lambda o: o["units"][0].update(order=1), "reading order"),
        (lambda o: o["units"][0].update(parent="u999999"), "parent"),
        (lambda o: o["units"][0]["anchor"].update(pointer="/x", render="json-string"), "exactly one"),
        (lambda o: o["units"][0]["anchor"].update(range=[5, 2]), "range"),
        (lambda o: o["units"][0]["anchor"].update(digest="xyz"), "digest"),
        (lambda o: o["units"][0]["anchor"].update(render="json-string"), "no renderer"),
        (lambda o: o["units"].append(dict(o["units"][0], order=1)), "unique"),
        (lambda o: o["units"][0].update(bbox=5), "bbox"),
        (lambda o: o["units"][0].update(bbox="abcd"), "bbox"),
        (lambda o: o.update(record_id=5), "record_id"),
        (lambda o: o["parser"].update(version=1), "parser"),
    ])
    def test_invalid_extractions_are_refused(self, mutate, message) -> None:
        obj = json.loads(_extraction(_unit(0)).to_canonical())
        mutate(obj)
        data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        with pytest.raises(ExtractionFormatError, match=message):
            Extraction.from_canonical(data)

    def test_parents_come_earlier_so_there_are_no_cycles(self) -> None:
        with pytest.raises(ExtractionFormatError, match="earlier unit"):
            _extraction(_unit(0), _unit(1, parent="u000003"), _unit(2, parent="u000002"))
        with pytest.raises(ExtractionFormatError, match="earlier unit"):
            _extraction(_unit(0, parent="u000001"))
        assert _extraction(_unit(0), _unit(1, parent="u000001"), _unit(2, parent="u000001"))

    def test_pointer_anchor_needs_a_renderer(self) -> None:
        with pytest.raises(ExtractionFormatError, match="renderer"):
            Anchor(artifact="c.json", digest=DIGEST, pointer="/mapping")

    def test_json_pointer_escaping(self) -> None:
        pointer = json_pointer("mapping", "a/b~c", "message")
        assert pointer == "/mapping/a~1b~0c/message"
        doc = {"mapping": {"a/b~c": {"message": "hit"}}, "list": [10, 11]}
        assert resolve_pointer(doc, pointer) == "hit"
        assert resolve_pointer(doc, "/list/1") == 11
        for bad in ("/list/01", "/list/2", "/list/x", "/mapping/nope", "/list/0/deeper"):
            with pytest.raises(ResolutionError):
                resolve_pointer(doc, bad)


# ---------------------------------------------------------------------------
# 2 — Markdown normalizer
# ---------------------------------------------------------------------------

class TestMarkdown:

    def test_blocks_kinds_and_anchors(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        extraction = normalize(_bundle(ledger, record))

        assert [(u.kind, u.level) for u in extraction.units] == [
            ("heading", 1), ("paragraph", None), ("heading", 2), ("list", None),
            ("code", None), ("table", None), ("formula", None), ("figure", None),
            ("heading", 1), ("paragraph", None),
        ]
        for unit in extraction.units:
            start, end = unit.anchor.range
            assert DOC[start:end].decode("utf-8") == unit.text
            assert unit.anchor.artifact == "document.md"
            assert unit.anchor.digest == record.artifact_manifest["document.md"]
        code = extraction.units[4]
        assert code.text == "```python\nx = 1\n\ny = 2\n```"     # blank line kept inside the fence
        assert extraction.units[1].text == "We measured *everything*.\nTwice — naïvely."
        assert extraction.normalizer == {"name": "markdown", "version": "1"}
        assert extraction.parser == {"name": "marker", "version": "1.0"}
        assert extraction.artifact_hash == record.artifact_hash

    def test_sections_nest_by_heading_level(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        units = normalize(_bundle(ledger, record)).units
        by_text = {u.text.splitlines()[0]: u for u in units}
        results, method = by_text["# Results"], by_text["## Method"]
        discussion = by_text["Discussion"]
        assert results.parent is None and discussion.parent is None
        assert method.parent == results.id
        assert by_text["We measured *everything*."].parent == results.id
        assert by_text["- step one"].parent == method.id
        assert by_text["Closing words."].parent == discussion.id

    def test_deterministic(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        bundle = _bundle(ledger, record)
        assert normalize(bundle).to_canonical() == normalize(bundle).to_canonical()

    @pytest.mark.parametrize("parser", ["mineru", "marker", "docling"])
    def test_every_packaged_parser_normalizes(self, tmp_path, parser) -> None:
        ledger, record = _sealed_markdown(tmp_path, parser=parser)
        assert len(normalize(_bundle(ledger, record)).units) == 10

    def test_crlf_and_unclosed_fence(self) -> None:
        data = b"Para\r\nnext\r\n\r\n~~~\r\ncode\r\n\r\nstill code"
        assert [data[s:e] for s, e in markdown_blocks(data)] == [
            b"Para\r\nnext", b"~~~\r\ncode\r\n\r\nstill code",
        ]

    def test_non_utf8_markdown_fails(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path, b"caf\xe9\n")
        with pytest.raises(NormalizeError, match="UTF-8"):
            normalize(_bundle(ledger, record))

    def test_unknown_parser_has_no_normalizer(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path, parser="homegrown")
        with pytest.raises(NormalizeError, match="homegrown"):
            normalize(_bundle(ledger, record))


# ---------------------------------------------------------------------------
# 3 — the resolver
# ---------------------------------------------------------------------------

class TestResolver:

    def test_honest_extraction_verifies(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        extraction = normalize(_bundle(ledger, record))
        assert Resolver(ledger).verify(extraction) == []
        # Verification survives a canonical round trip (what a consumer receives).
        assert Resolver(ledger).verify(Extraction.from_canonical(extraction.to_canonical())) == []

    def test_resolve_one_anchor(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        unit = normalize(_bundle(ledger, record)).units[3]
        assert Resolver(ledger).resolve(record.record_id, unit.anchor) == "- step one\n- step two"

    def test_tampered_text_is_caught(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        extraction = _replace_unit(normalize(_bundle(ledger, record)), 1, text="We measured nothing.")
        assert Resolver(ledger).verify(extraction) == [
            "unit u000002: text does not match its anchor"
        ]

    @pytest.mark.parametrize("anchor_change, message", [
        (dict(digest="b" * 64), "is not the sealed"),
        (dict(artifact="other.md"), "is not an artifact"),
        (dict(range=(0, 10**6)), "past the end"),
    ])
    def test_forged_anchors_are_caught(self, tmp_path, anchor_change, message) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        extraction = normalize(_bundle(ledger, record))
        unit = extraction.units[0]
        forged = _replace_unit(extraction, 0, anchor=dataclasses.replace(unit.anchor, **anchor_change))
        problems = Resolver(ledger).verify(forged)
        assert len(problems) == 1 and message in problems[0]

    @pytest.mark.parametrize("change, message", [
        (dict(parser={"name": "docling", "version": "1.0"}), "names parser"),
        (dict(parser={"name": "marker", "version": "9"}), "names parser"),
        (dict(normalizer={"name": "chatgpt", "version": "1"}), "names normalizer"),
        (dict(normalizer={"name": "markdown", "version": "2"}), "names normalizer"),
    ])
    def test_forged_provenance_is_caught(self, tmp_path, change, message) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        forged = dataclasses.replace(normalize(_bundle(ledger, record)), **change)
        problems = Resolver(ledger).verify(forged)
        assert problems and message in problems[0], problems

    def test_range_splitting_a_character_is_caught(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        start = DOC.index("ï".encode())
        anchor = Anchor(artifact="document.md", digest=record.artifact_manifest["document.md"],
                        range=(start, start + 1))
        with pytest.raises(ResolutionError, match="UTF-8"):
            Resolver(ledger).resolve(record.record_id, anchor)

    def test_unknown_renderer_is_refused(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        anchor = Anchor(artifact="stele-parser.json",
                        digest=record.artifact_manifest["stele-parser.json"],
                        pointer="/parser", render="eval")
        with pytest.raises(ResolutionError, match="unknown renderer"):
            Resolver(ledger).resolve(record.record_id, anchor)
        anchor = dataclasses.replace(anchor, render="json-string")
        assert Resolver(ledger).resolve(record.record_id, anchor) == "marker"

    def test_extraction_of_another_record_is_caught(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        extraction = normalize(_bundle(ledger, record))
        wrong = dataclasses.replace(extraction, artifact_hash="c" * 64)
        assert "artifact_hash" in Resolver(ledger).verify(wrong)[0]
        missing = dataclasses.replace(extraction, record_id="no-such-record")
        assert "no record" in Resolver(ledger).verify(missing)[0]

    def test_only_sealed_records_resolve(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        extraction = normalize(_bundle(ledger, record))
        ledger.invalidate(record.record_id, "source_changed")
        problems = Resolver(ledger).verify(extraction)
        assert len(problems) == 1 and "invalidated" in problems[0]
        assert Resolver(ledger, allow_invalidated=True).verify(extraction) == []

    def test_tampered_evidence_is_caught(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        extraction = normalize(_bundle(ledger, record))
        digest = record.artifact_manifest["document.md"]
        blob = ledger.archive._blob_path(digest)
        blob.chmod(0o644)
        blob.write_bytes(DOC.replace(b"everything", b"EVERYTHING"))
        problems = Resolver(ledger).verify(extraction)
        assert problems and all("evidence unreadable" in p for p in problems)


# ---------------------------------------------------------------------------
# 4 — ChatGPT normalizer, end to end through the Wasm splitter
# ---------------------------------------------------------------------------

def _msg(node_id, role, text):
    return {"id": f"m-{node_id}", "author": {"role": role},
            "content": {"content_type": "text", "parts": [text]}}


CONVERSATION = {
    "conversation_id": "conv/1",
    "title": "Branches",
    "current_node": "a1r",
    "mapping": {
        "root": {"id": "root", "message": None, "parent": None, "children": ["q1"]},
        "q1": {"id": "q1", "message": _msg("q1", "user", "What is 2+2?"),
               "parent": "root", "children": ["a1", "a1r"]},
        "a1": {"id": "a1", "message": _msg("a1", "assistant", "5"), "parent": "q1", "children": []},
        "a1r": {"id": "a1r", "message": _msg("a1r", "assistant", "4"), "parent": "q1", "children": []},
        "odd/~id": {"id": "odd/~id", "message": _msg("odd", "user", "slash ~ tilde"),
                    "parent": None, "children": []},
    },
}


@pytest.mark.usefixtures("wasm_backend")
class TestChatGPT:

    def _sealed(self, tmp_path):
        ledger = open_ledger(tmp_path / "ledger.db")
        source = tmp_path / "export" / "conversations.json"
        source.parent.mkdir()
        source.write_bytes(json.dumps([CONVERSATION], ensure_ascii=False, indent=1).encode())
        run = run_parser(CHATGPT_EXPORT_SPLIT_SPEC, artifact_dir=tmp_path / "out",
                         parser_config={}, input_path=source, store=ledger.archive)
        assert run.succeeded, run.stderr
        record = record_run(ledger, run, parser=CHATGPT_EXPORT_SPLIT_SPEC.identity(),
                            parser_config={}, source=Source.from_path(source))
        return ledger, record

    def test_messages_anchor_by_pointer_and_verify(self, tmp_path) -> None:
        ledger, record = self._sealed(tmp_path)
        extraction = normalize(_bundle(ledger, record))
        assert extraction.normalizer == {"name": "chatgpt", "version": "1"}
        assert extraction.source_hash == record.source_hash
        by_node = {u.attributes["node_id"]: u for u in extraction.units}
        assert set(by_node) == {"q1", "a1", "a1r", "odd/~id"}
        assert by_node["a1"].parent == by_node["q1"].id == by_node["a1r"].parent
        assert by_node["a1r"].attributes["on_current_branch"] is True
        assert by_node["a1"].attributes["on_current_branch"] is False
        assert by_node["odd/~id"].anchor.pointer == "/mapping/odd~1~0id/message"
        assert all(u.kind == "message" and u.anchor.render == "chatgpt-message"
                   for u in extraction.units)
        assert Resolver(ledger).verify(extraction) == []

    def test_tampered_message_is_caught(self, tmp_path) -> None:
        ledger, record = self._sealed(tmp_path)
        extraction = normalize(_bundle(ledger, record))
        index = next(i for i, u in enumerate(extraction.units) if u.text == "5")
        forged = _replace_unit(extraction, index, text="4")
        assert Resolver(ledger).verify(forged) == [
            f"unit {extraction.units[index].id}: text does not match its anchor"
        ]


# ---------------------------------------------------------------------------
# 5 — ExtractionAdapter through the Dispatcher
# ---------------------------------------------------------------------------

class MemoryTarget:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, dict]] = {}

    def write_chunks(self, chunks, target, *, dispatch_id):
        for c in chunks:
            self.rows[(dispatch_id, c.chunk_id)] = (c.content, c.metadata)

    def remove_delivery(self, target, *, dispatch_id):
        keys = [k for k in self.rows if k[0] == dispatch_id]
        for k in keys:
            del self.rows[k]
        return len(keys)


class TestAdapter:

    def test_dispatches_one_chunk_per_unit(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        target = MemoryTarget()
        dispatcher = Dispatcher(ledger)
        dispatcher.register_target(LightRAGTarget, target)
        result = dispatcher.dispatch(ExtractionAdapter(), record.record_id, LightRAGTarget("docs"))
        assert result.status == "success" and result.chunks_written == 10
        [delivery] = dispatcher.dispatch_log_for(record.record_id)
        assert delivery.status is DeliveryStatus.DELIVERED
        rows = {chunk_id: row for (_, chunk_id), row in target.rows.items()}
        assert len(rows) == 10
        text, meta = rows[f"{record.record_id}:u000004"]
        assert text == "- step one\n- step two"
        assert meta["kind"] == "list" and meta["order"] == 3
        assert meta["parent"] == f"{record.record_id}:u000003"
        assert meta["anchor"]["range"] == list(normalize(_bundle(ledger, record)).units[3].anchor.range)
        assert meta["schema"] == "stele.extraction/v1" and meta["normalizer"] == "markdown/1"

    def test_misquoting_normalizer_fails_the_transform(self, tmp_path, monkeypatch) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        honest = normalize_mod.normalize_markdown

        def misquoting(bundle):
            return _replace_unit(honest(bundle), 0, text="# Something else")

        monkeypatch.setattr(normalize_mod, "normalize_markdown", misquoting)
        with pytest.raises(ResolutionError, match="does not match its evidence"):
            ExtractionAdapter().transform(_bundle(ledger, record))

    def test_bundle_resolver_matches_ledger_resolver(self, tmp_path) -> None:
        ledger, record = _sealed_markdown(tmp_path)
        bundle = _bundle(ledger, record)
        extraction = normalize(bundle)
        assert BundleResolver(bundle).verify(extraction) == Resolver(ledger).verify(extraction) == []


def test_cli_extract_and_verify(tmp_path, capsys) -> None:
    from stele.extraction.__main__ import main

    ledger, record = _sealed_markdown(tmp_path)
    ledger.close()
    db, archive = str(tmp_path / "ledger.db"), str(tmp_path / "archive")
    out = tmp_path / "extraction.json"
    assert main(["extract", db, archive, record.record_id, "-o", str(out)]) == 0
    assert main(["verify", db, archive, str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    obj = json.loads(out.read_bytes())
    obj["units"][0]["text"] = "# Forged"
    out.write_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    assert main(["verify", db, archive, str(out)]) == 1
    assert json.loads(capsys.readouterr().out)["problems"] == [
        "unit u000001: text does not match its anchor"
    ]
