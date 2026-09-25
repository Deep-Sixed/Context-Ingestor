# Stele — Parser Containment & Artifact Ledger

**Status:** COMPLETE — v1.0 milestone (extracted 2026-06-26)  
**Canonical repo:** `/mnt/jarvis-data/projects/Stele`  
**Created:** 2026-06-26

## Extraction note

This is the **standalone canonical home** for Stele v1.0.

**EVECOR continues to use its embedded copy** at `EVECOR/services/stele/` until
an explicit cutover. Do not develop two independent copies — changes land here
first after cutover; until then, production fixes may still go to the EVECOR
embedded path and be synced here at milestone boundaries.

## Purpose

Stele is the parser containment boundary and deterministic artifact ledger
that sits between untrusted parser execution (RAG-ANYTHING, MinerU, Marker)
and production write targets (Hindsight, Graphify/LightRAG, MetaRouter artifacts).

No parser-facing infrastructure may write to production except through the
Stele adapter contract (Phase H) and the Dispatcher write path.

## Milestone baseline (v1.0)

| Item | Status |
|------|--------|
| Phase E — containment | ✅ 23 tests |
| Phase F — ledger | ✅ 27 tests |
| Phase G — replay / invalidation | ✅ 24 tests |
| Phase H — adapter contract | ✅ 25 tests |
| Live ingestion (EVECOR RAG path) | ✅ proven |
| Scoped tombstones (`{run_id}:` prefix) | ✅ implemented in EVECOR RAG integration |

**99 Stele tests** in this repo. RAG×Stele integration tests (31) remain in
`EVECOR/DataCore/RAG/` until adapters move with cutover.

**Ledger redesign (#12), after v1.0:** records are per run, and a record is
`sealed` once its bundle is stored in the evidence archive and verified.
Sealed is not delivered, which replaces v1.0's `committed`. Each record
stores its input Snapshot digest and its parser identity and config. The
ledger API changed accordingly (`LedgerStore(db, archive)`, `seal()`,
`record_run()`). Existing ledgers migrate on open. See `docs/F-ledger.md`.

**Durable dispatch (#13):** the Dispatcher delivers only sealed records and
checks this against the live ledger. Adapters read verified bytes from the
archive (`SealedBundle`), never files. Each write has a durable intent and a
receipt or failure in an append-only delivery log. Writers get a `dispatch_id`
idempotency key. `Dispatcher.invalidate()` removes a record's delivered data
and records a receipt for each removal. See `docs/H-adapter.md`.

**Replay (#14):** replay re-runs a record's parser at its recorded identity,
with its recorded config, on its recorded input Snapshot. Each replay is
reported as exactly one of `REPRODUCED`, `EQUIVALENT` (under the parser's
comparison policy), `DIVERGED` or `UNREPLAYABLE`, and logged. Validation (the
re-hash of working copies) is a separate operation. A frozen-Snapshot harness
proves the Wasm extractor reproduces byte for byte on Linux, macOS and
Windows. See `docs/G-replay.md`.

**Run telemetry and faults (#11):** every run result carries the same
telemetry from every backend: wall and CPU time, peak memory, exit status,
the limits applied, and the backend and runtime that ran it. A measurement a
backend can't make is `None`. A failed run carries one structured reason:
timeout, out of memory, CPU limit, blocked syscall, Wasm trap, crash, exit
status, engine error or unsafe output. A failed or killed run leaves no
output, staging copy, process or container behind. See `docs/E-containment.md`.

## Gate

**RAG-ANYTHING: PROCEED — Stele-gated ingestion with scoped tombstone support.**

```
source → bubblewrap parser → ledger → SteleAdapter → Dispatcher → TargetWriter → target store
```

Integration adapters (e.g. `RagAnythingSteleAdapter`, `LightRAGTargetWriter`,
`lightrag_tombstone`) live in EVECOR until extracted alongside cutover.

**Caveat:** KG entity/relation enrichment requires valid `LITELLM_RAG_ANYTHING_KEY`.

See `docs/` for per-phase specs.  
Adapter and dispatcher protocols: `stele/contracts/`.

## Design sign-off

```
Parser → SteleAdapter → Dispatcher → TargetWriter → Hindsight / LightRAG / …
```

RAG-ANYTHING transforms only; the Dispatcher owns target writes; tombstone
cleanup is scoped to Stele-marked chunk rows and does not mutate the artifact
ledger.

---

## Directory layout

```
Stele/
├── README.md
├── docs/                   ← phase specs (E–H), evidence store (archive.md), cloud sandbox design note
├── stele/                  ← Python package
│   ├── containment/
│   ├── archive/            ← content-addressed evidence store (#16)
│   ├── extractors/         ← Wasm extractors (run on the Wasmtime backend)
│   ├── parsers/            ← packaged ML parsers in pinned images (#9, #10)
│   ├── ledger/
│   ├── replay/
│   └── contracts/
├── parsers/                ← parser image build files (MinerU, Marker, Docling)
├── tests/
├── pyproject.toml
└── uv.lock
```

## Develop

```bash
cd /mnt/jarvis-data/projects/Stele
uv sync --extra dev
uv run pytest tests/ -v
```
