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
