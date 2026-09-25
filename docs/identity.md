# Identity Contract

Each layer of Stele names its evidence in its own way:

- a Snapshot by `(kind, digest)`
- a Source by `source_id`
- a ledger record by `record_id` and `artifact_hash`
- an extraction unit by an anchor into a sealed artifact
- an event by `(seq, hash)` in the hash-chained log

`stele.identity` gives each of these one typed reference with one canonical
string. A downstream system can store, pass on and cite that string, and check
it later.

Every reference **commits to content**: it contains the digest of what it
names. It therefore resolves only while that evidence is unchanged.

## References (`stele/identity/refs.py`)

| Type | Canonical string | Commits to |
|------|------------------|------------|
| `SourceRef` | `stele:source:<source_id>` | the Source record (a descriptive locator) |
| `SnapshotRef` | `stele:snapshot:<file\|tree>:<sha256>` | the exact bytes a parser saw |
| `RecordRef` | `stele:record:<record_id>@<artifact_hash>` | one run's sealed output tree |
| `ObservationRef` | `stele:observation:<record_id>@<sha256>` | one run's Observation (below) |
| `AnchorRef` | `stele:anchor:<record_id>@<artifact_hash>/<artifact>@<digest>#bytes=<start>-<end>` | a byte range of one sealed artifact |
| | `stele:anchor:<record_id>@<artifact_hash>/<artifact>@<digest>#pointer=<pointer>&render=<name>` | a JSON value of one sealed artifact, rendered by a named trusted renderer |
| `EventRef` | `stele:event:<seq>@<hash>` | one event, and through the chain, all history before it |

**An Observation** records that one run saw its input:

```
{schema: "stele.observation", schema_version: 1,
 record_id, source_id (or null), snapshot: {kind, digest}, observed_at}
```

- `observed_at` is the record's `created_at`, in ISO 8601 (UTC).
- Its reference names the record plus the SHA-256 of that canonical JSON. Each
  field is covered, so a different source, snapshot or time gives a different
  reference.

**Canonical strings.** `parse_ref()` accepts a string only if formatting the
parsed reference gives the same string back, so each reference has exactly one
spelling.

- In anchors, the artifact path and the JSON pointer are percent-encoded.
  Unreserved characters and `/` stay literal. Everything else becomes `%XX`
  with upper-case hex, as `urllib.parse.quote` writes it.
- A renderer name is encoded the same way, but with `/` escaped too.
- Numbers have no leading zeros. Digests are lower-case hex.

**Constructors:**

| Function | Returns |
|---|---|
| `record_ref(record)` | the `RecordRef` of an `ArtifactRecord` or `SealedBundle` |
| `observation_of(record)` | the record's `Observation`; its `.ref` is the `ObservationRef` |
| `anchor_ref(extraction, unit)` | the `AnchorRef` of one extraction unit |
| `event_ref(event)` | the `EventRef` of an event from `EventLog` |

## Resolution (`stele/identity/resolver.py`)

`IdentityResolver(ledger).resolve(ref)` accepts a reference or its string. It
returns what the reference names or raises `ResolveError`. `check(ref)` returns
the problems as a list instead. Each type is checked against the evidence,
never against the caller's word:

| Reference | Resolves when | Returns |
|---|---|---|
| `SourceRef` | The Source record is in the archive and hashes to `source_id`. | `Source` |
| `SnapshotRef` | The Snapshot record is in the archive, and every blob it covers re-hashes: the file, or the tree object and each file it lists. | `Snapshot` |
| `RecordRef` | The record exists and is `SEALED` (or `INVALIDATED` with `allow_invalidated=True`). It must have that `artifact_hash`, and the archived tree object must list exactly the record's manifest. | `ArtifactRecord` |
| `ObservationRef` | The Observation rebuilt from the record has that digest, and its Snapshot and Source resolve. An observation stays true whatever happened to the run later, so it resolves in any record state. | `Observation` |
| `AnchorRef` | The record resolves as a `RecordRef`. The [extraction resolver](extraction.md) then reads the span from bytes the archive has just re-hashed. | the anchored text |
| `EventRef` | The chain verifies with this event as its anchor: the event is at `seq` with this hash, and nothing before or after it has been altered, removed or reordered. | `Event` |

**What resolution does not check:**
- A `RecordRef` checks the tree object but not every artifact blob. An
  `AnchorRef` re-hashes the one blob it reads.
- An `EventRef` depends on the whole chain being intact. A chain broken
  anywhere resolves no event, because none of its hashes can be trusted until
  the break is explained.
- As with the event log, anyone with write access to the ledger and archive
  can rewrite them consistently. A reference held outside that access, such
  as a published `EventRef`, is what detects that.

## Citations

`ExtractionAdapter` chunks carry their unit's anchor reference in
`metadata["stele_anchor"]`. A system that quotes a chunk can keep that string
next to the quote. Resolving it later checks that the quoted text is still
exactly what the sealed evidence says, and that the record still stands.

## CLI

```bash
python -m stele.identity refs ledger.db archive/ <record_id>
    # {"record": "stele:record:...", "observation": "...", "snapshot": "...", "events": [...]}
python -m stele.identity resolve ledger.db archive/ <ref>... [--allow-invalidated]
    # one JSON line per reference; exit 1 if any does not resolve
```

## Tests

`tests/test_identity.py`:

| Property | Tests |
|---|---|
| Every type round-trips through one canonical string, and other spellings are refused | `TestFormat` |
| Every type resolves to what it names | `TestResolve::test_every_type_resolves_to_what_it_names` |
| Wrong digests, kinds, records, artifacts and ranges are refused | `TestResolve::test_wrong_digests_are_refused` |
| A different observation is refused | `TestResolve::test_a_different_observation_is_refused` |
| Invalidated records resolve only on request; their observations still hold | `TestResolve::test_invalidated_record_needs_permission` |
| Tampered output and input blobs are refused | `TestResolve::test_tampered_evidence_is_refused` |
| A rewritten event is refused | `TestResolve::test_rewritten_event_is_refused` |
| Extraction chunks cite resolvable anchors | `test_extraction_chunks_carry_resolvable_anchor_refs` |
| CLI | `test_cli_refs_and_resolve` |
