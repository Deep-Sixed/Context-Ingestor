# Canonical Extraction Contract

Every parser writes its own output format: MinerU's Markdown and middle JSON,
Marker's block tree, Docling's DoclingDocument, the ChatGPT splitter's
conversation files. `stele.extraction` v1 is the one shape downstream code
reads instead. It is an ordered list of units of content, and each unit is
tied by an **anchor** to the exact sealed bytes it came from.

An Extraction is derived by trusted host code (a *normalizer*) from a sealed
bundle. A parser never asserts it. The anchors make every unit checkable: the
**trusted resolver** re-reads each anchor from the evidence archive and
requires the unit's text to equal what it finds there.

## The format (`stele/extraction/contract.py`)

```
Extraction
  schema, schema_version        "stele.extraction", 1
  record_id, artifact_hash      the sealed ledger record it was derived from
  source_hash                   the input Snapshot (null for input-less runs)
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
  parent                        an earlier unit's id (sections, conversation trees), or null
  anchor                        where the text lives in the sealed bundle, one of:
      {artifact, digest, range: [start, end]}                   a UTF-8 byte range
      {artifact, digest, pointer: "/json/pointer", render: R}   a JSON value rendered by R
  attributes                    a small JSON object of kind-specific facts
```

`anchor.artifact` is a path in the record's manifest, and `anchor.digest` is
that artifact's SHA-256 at sealing.

Serialization is canonical JSON: sorted keys, compact, UTF-8. Parsing
(`Extraction.from_canonical`) is strict, so one extraction has exactly one
byte form and one digest. It refuses:

- unknown or missing fields;
- another schema or version;
- an unknown kind or empty text;
- units out of order, with duplicate ids, or with a parent that isn't an earlier
  unit (so the units always form a forest, never a cycle);
- an anchor with both or neither of range and pointer, or a pointer with no renderer;
- any bytes that are not the canonical encoding.

## Normalizers (`stele/extraction/normalizers.py`)

`normalize(bundle)` picks the normalizer by the record's parser name. A
normalizer reads only through the `SealedBundle`, so every byte it sees was
just re-verified against the sealed digest.

| Normalizer | Parsers | Units |
|------------|---------|-------|
| `markdown/1` | `mineru`, `marker`, `docling` | One unit per Markdown block of the file `stele-parser.json` names under `outputs.markdown` |
| `chatgpt/1` | `chatgpt-export-split` | One `message` unit per node with content, on every branch |

### `markdown/1`

- **Block splitting.** Blank lines separate blocks. An ATX heading is always
  a block of its own. A setext underline ends a one-line heading. A fenced
  code block is one block from its opening fence to its closing fence,
  blank lines included; if it never closes, it runs to the end of the file.
- **Text and anchor.** A unit's text is the block's exact bytes (without the
  final line ending), and its anchor is that byte range.
- **Kinds.** Each block's syntax gives its kind: `#` or a setext underline is
  a heading (with its level), a fence is code, `|` or `<table` is a table, a
  bullet or number is a list, `$$` is a formula, and a lone `![...]` is a
  figure. Anything else is a paragraph.
- **Sections.** Headings nest by level. Every other block's parent is the
  heading it sits under.

### `chatgpt/1`

- **Anchor.** The anchor is `{artifact: conversation-NNNNNN.json, pointer:
  /mapping/<node>/message, render: chatgpt-message}`. Node ids are escaped
  per RFC 6901.
- **Text.** The `chatgpt-message` renderer is the same trusted function
  `ChatGPTExportAdapter` uses.
- **Parent.** A unit's parent is its nearest ancestor message.
- **Attributes.** They carry the role, conversation, node and branch facts.

Not normalized yet: page numbers and bounding boxes from the parsers' layout
JSON (Docling's `document.docling.json`, Marker's `document.json`, MinerU's
`model_output.json`). Units from Markdown carry `page = bbox = null`. Those
normalizers need real parser outputs to test against, so they are follow-ups.

## The trusted resolver (`stele/extraction/resolver.py`)

`Resolver(ledger).verify(extraction)` returns a list of problems. An empty
list means every unit checks out. The resolver trusts nothing the extraction
says about itself:

1. **The record.** It must be `SEALED` in the live ledger (or `INVALIDATED`
   with `allow_invalidated=True`). The extraction's `artifact_hash`,
   `source_hash` and `parser` must be the record's, and its `normalizer` must
   be the one registered for that parser.
2. **The artifact.** The anchored artifact must be in the record's manifest,
   under the digest the anchor names.
3. **The bytes.** They are read from the evidence archive, which re-hashes
   them. Tampered evidence is reported as unreadable.
4. **The anchor.**
   - A range must lie inside the file and decode as UTF-8 on its own.
   - A pointer must resolve in the artifact's JSON. The value is then rendered
     by a renderer from the resolver's fixed registry (`json-string`,
     `chatgpt-message`). An extraction can name a renderer, but it cannot
     supply one.
5. **The text.** The unit's text must equal the resolution exactly.

`Resolver.resolve(record_id, anchor)` resolves one anchor.
`BundleResolver(bundle)` runs the same checks against a `SealedBundle` that
is already open.

## Delivery: `ExtractionAdapter` (`stele/adapters/extraction.py`)

A `SteleAdapter` for any bundle that has a normalizer.

- It normalizes the bundle, then verifies the result with `BundleResolver`
  before returning anything. A normalizer bug that misquotes the evidence
  fails the transform, and nothing reaches a target.
- It emits one chunk per unit. The chunk id is `<record_id>:<unit id>`.
- The chunk metadata holds `kind`, `order`, `page`, `bbox`, `level`,
  `parent` (as a chunk id), `anchor`, `attributes`, `schema`,
  `normalizer`, and `stele_anchor`: the unit's anchor as a
  [Stele reference](identity.md) (`stele:anchor:...`) that a consumer can
  resolve to check a citation against the evidence.

## CLI

```bash
python -m stele.extraction extract ledger.db archive/ <record_id> -o extraction.json
python -m stele.extraction verify  ledger.db archive/ extraction.json [--allow-invalidated]
```

- `extract` writes the canonical bytes and prints their SHA-256 to stderr.
- `verify` prints `{"ok": ..., "problems": [...]}` and exits 1 on any problem.

## Invariants proven (`tests/test_extraction.py`)

| Proof | Test |
|-------|------|
| Canonical round trip; every malformed or non-canonical extraction refused | `TestFormat` |
| Markdown blocks, kinds, sections and byte-exact anchors for all three parsers | `TestMarkdown` |
| Honest extractions verify, including after a round trip | `TestResolver::test_honest_extraction_verifies` |
| Tampered text, digest, artifact, range, renderer, record or evidence is caught | `TestResolver` |
| Only sealed records resolve (invalidated ones on request) | `TestResolver::test_only_sealed_records_resolve` |
| ChatGPT messages anchor by JSON pointer and verify end to end through the Wasm splitter | `TestChatGPT` |
| Units are delivered through the Dispatcher; a misquoting normalizer fails the transform | `TestAdapter` |
