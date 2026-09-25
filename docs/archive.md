# Snapshot Archive and Evidence Store

**Roadmap:** #16 · **Depends on:** #5 · **Used by:** #12 (ledger), #13 (dispatch), #14 (replay)
**Code:** `stele/archive/`

Storage and identity only. Record states, delivery and replay are defined by the issues that use this store.

## Model

```
Blob        bytes, addressed by SHA-256
Source      where material came from (descriptive; not evidence)
Snapshot    the exact staged bytes of a source at a moment in time
Extraction  a parser run over a Snapshot -> artifact blobs
```

Only digests identify evidence. File paths are just locations.

## Layout

```
<root>/stele-archive.json               format marker, layout_version 1
<root>/blobs/ab/cdef...                 blob with SHA-256 "abcdef..."
<root>/meta/snapshots/<kind>/ab/cdef... Snapshot record (kind = file | tree)
<root>/meta/sources/ab/cdef...          Source record, keyed by source_id
<root>/tmp/                             in-flight writes (never read as evidence)
```

Keys use POSIX separators on every platform.

## Guarantees

- **Content addressing.** The store computes each digest from the bytes it writes. A caller-supplied `expected_digest` can only cause a write to be refused.
- **Immutability.** Nothing is ever overwritten. Writing content that is already stored re-verifies the stored copy and does nothing else. If the stored copy is corrupt, the write raises `IntegrityError` and leaves the damaged object in place. A metadata record that conflicts with an existing one also raises.
- **Atomic publish.** Each write goes through these steps:
  1. Write to a temp file in `tmp/`, hashing while writing.
  2. `fsync` the file.
  3. `os.replace` it into place.
  4. `fsync` the shard directory. Windows can't `fsync` a directory, so this step is skipped there.

  A crash leaves at most an orphaned temp file, never a partial object under a final name.
- **Verified reads.** `read()` and `export()` re-hash the bytes on every read. A mismatch raises `IntegrityError`. `export()` renames a copy into place only after verifying it, so a caller never sees unverified bytes.
- **No-follow ingest.** Staged inputs and parser artifacts are read with the same no-follow, stay-beneath-root opens the ledger uses to hash them, via `open_regular_file_beneath`.
- **No deletion.** There is no delete or garbage-collection API. Purging will be a separate, explicit operation that records its own evidence.

## Snapshot identity

A Snapshot's identity is `(kind, digest)`, and `digest` always equals `StagedInput.sha256`, the hash of the bytes the parser saw.

| Kind | What the digest addresses |
|---|---|
| `file` | The blob holding the file's bytes. |
| `tree` | A *tree object*: the blob `encode_manifest(manifest)` (entries sorted by path, each written as `path NUL digest NUL`) that lists every file's relative POSIX path and blob digest. |

The tree object is the same byte string that `sha256_manifest()` has always hashed. So a directory Snapshot's digest is the digest staging already reports. The same holds for the ledger's `artifact_hash`, which is the tree digest of a run's artifact bundle. No second directory-hash scheme exists, and existing ledgers stay valid.

The kind is part of the identity because a tree object's bytes can also be the contents of an ordinary file. For example, an empty file and an empty directory share a digest. Records are therefore keyed by kind, and the two never collide.

Empty directories are not part of a tree Snapshot, just as they are not part of the manifest digest.

## Records

Records are serialized as canonical JSON: sorted keys, compact separators, UTF-8, with explicit `schema` and `schema_version` fields. Parsing is strict and rejects:

- unknown fields
- a different version
- any encoding that isn't canonical

```json
{"digest":"…","file_count":3,"kind":"tree","schema":"stele.snapshot","schema_version":1,"size":11}
{"locator":"/corpus/doc.pdf","schema":"stele.source","schema_version":1}
```

A Snapshot record contains only fields derived from the content. It is published after all of its blobs, so its presence means the Snapshot is complete in the store.

A Source's `source_id` is the SHA-256 of its canonical record. Linking a Source to its Snapshots is a per-run fact: each ledger record stores its run's Snapshot digest (`source_hash`) and `source_id` (see [ledger.md](ledger.md)).

## Runs

`run_in_sandbox(config, store=BlobStore(...))` does three things:

- Archives the staged input as a Snapshot *before* the parser runs. The staging copy is deleted after the run.
- Stores every collected artifact by digest.
- Stores a tree object over the artifact manifest.

The result carries `input_snapshot`, `artifact_digests` and `artifact_bundle_digest`. Callers that don't pass a store see no change.
