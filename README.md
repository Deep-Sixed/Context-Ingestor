# Stele — Parser Containment & Artifact Ledger

Stele runs untrusted document parsers in a sandbox and keeps a verifiable
record of everything they produce, so parser output reaches your downstream
stores only through one audited path.

Document parsers (PDF, Office, OCR, ML layout models) are large, fast-moving
codebases that handle hostile input. Stele treats every parser as untrusted:

- **Contain the parser.** Each run gets a staged read-only copy of its input,
  no network, and exactly one writable directory. It runs in bubblewrap, an
  OCI container (Podman/Docker, optionally gVisor or GPU), or a
  WebAssembly/WASI runtime.
- **Record what it produced.** Every run gets its own ledger record: the
  digest of the exact input the parser read, the parser's identity (name,
  version, image or module digest) and config, and a manifest of its output.
  A record is `sealed` once its whole bundle is stored in the evidence archive
  and verified.
- **Keep evidence.** A content-addressed archive holds the input bytes the
  parser saw and every artifact it produced. Nothing in it is overwritten, and
  every read re-hashes the bytes.
- **Replay and invalidate.** A sealed run can be re-checked against the
  archive, or replayed by running the recorded parser on the recorded input
  again. Each replay is reported as `REPRODUCED`, `EQUIVALENT`, `DIVERGED` or
  `UNREPLAYABLE`. Invalidating a run withdraws it and removes what it
  delivered, without rewriting history.
- **Write through one door.** Adapters turn a sealed bundle into typed,
  hash-checked chunks. Only the Dispatcher, using writers you register, sends
  them to target stores. It logs an intent before each write and a receipt or
  failure after it, so a crash never leaves you guessing what a target holds.
- **Report every run the same way.** Each run result carries the same
  telemetry from every backend: wall and CPU time, peak memory, exit status,
  the limits applied, and the backend and runtime. A failed run carries one
  structured reason (timeout, out of memory, CPU limit, blocked syscall, Wasm
  trap, crash, exit status, engine error, unsafe output) and leaves no output,
  staging copy, process or container behind.
- **Detect tampering.** Every ledger fact (a record created, sealed, failed
  or invalidated, each delivery event, each replay) is appended to a
  hash-chained event log in the same transaction as the change.
  `python -m stele.ledger.events` verifies the chain, and checks that the
  ledger's tables still match what it records. With a published anchor, it
  also catches a truncated and rewritten log.
- **Ingest ChatGPT exports whole.** A Wasm extractor splits `conversations.json`
  byte for byte, and `ChatGPTExportAdapter` turns the sealed result into one
  chunk per message on every conversation branch (edits and regenerations
  included), reading one conversation at a time.

```
source ─▶ staging ─▶ sandboxed parser ─▶ /stele/output ─▶ ledger (pending)
                                                              │ archive + verify
                                                              ▼
 target store ◀─ TargetWriter ◀─ Dispatcher ◀─ Adapter ◀─ ledger (sealed)
                                     │
                                     └─▶ delivery log (intent → receipt)
```

## Quick start

Requirements: Python 3.12+. You also need at least one sandbox backend:
`bubblewrap` (Linux), Podman or Docker, or the `wasm` extra, which runs
anywhere.

```bash
uv sync --extra dev
uv run pytest tests/ -v
```

Run a parser script in the sandbox from the command line:

```bash
python -m stele.containment.runner \
    --input paper.pdf --script my_parser.py --artifact-dir out/ \
    -- /usr/bin/python3 /stele/parser
```

Inside the sandbox the parser reads `$STELE_INPUT_PATH` and writes only to
`$STELE_OUTPUT_DIR` (`/stele/output`).

Or from Python, from sandbox run to sealed record to target:

```python
from pathlib import Path

from stele.archive.records import Source
from stele.archive.store import BlobStore
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig
from stele.contracts.dispatcher import Dispatcher
from stele.ledger.models import ParserIdentity
from stele.ledger.store import LedgerStore
from stele.ledger.transaction import record_run

archive = BlobStore(Path("archive"))
ledger = LedgerStore(Path("ledger.db"), archive)

result = run_in_sandbox(SandboxConfig(
    command=["/usr/bin/python3", "/stele/parser"],
    script_path=Path("my_parser.py"),
    input_path=Path("paper.pdf"),
    artifact_dir=Path("out"),
), store=archive)                      # archives the input Snapshot and artifacts

record = record_run(                   # PENDING -> SEALED, or FAILED
    ledger, result,
    parser=ParserIdentity("my_parser", "1.0"),
    parser_config={},
    source=Source.from_path("paper.pdf"),
)

dispatcher = Dispatcher(ledger)
dispatcher.register_target(MyTarget, my_writer)          # your TargetWriter
dispatcher.dispatch(my_adapter, record.record_id, MyTarget("docs"))

# Later, if the run should no longer count:
dispatcher.invalidate(record.record_id, "source_changed")  # removes delivered data
```

### Packaged parsers

MinerU, Marker and Docling run in pinned container images with their model
weights built in. Parsing never uses the network:

```bash
python -m stele.parsers build-command mineru   # prints the image build command
python -m stele.parsers run mineru --input paper.pdf --artifact-dir out/
```

See [parsers/README.md](parsers/README.md).

## What Stele guarantees and what it doesn't

| Guarantee | Scope |
|-----------|-------|
| No network for parsers | Every backend. Parsers cannot request it. |
| No writes outside `/stele/output` | Enforced by mount layout; also by Landlock where the kernel has it |
| Syscall denylist (seccomp) | bubblewrap on x86_64/aarch64; OCI when the engine's profile is active; gVisor |
| No unsandboxed fallback | A run with no capable backend is refused before anything executes |
| Tamper-evident artifacts | Hashes are computed on the host from no-follow opens, and archived bytes are re-verified on every read, seal, dispatch and replay |
| Only sealed records are delivered | The Dispatcher checks the live ledger before every write |
| Adapter isolation | **Contract only.** Adapters run in-process as trusted code. Only parsers are sandboxed. |

Details and known limits: [docs/containment.md](docs/containment.md).

## Documentation

| Doc | Covers |
|-----|--------|
| [Containment](docs/containment.md) | Sandbox backends, seccomp, Landlock, OCI and Wasm details, run telemetry and failure reasons |
| [Ledger](docs/ledger.md) | State machine, record fields, parser identity, migration, the hash-chained event log |
| [Replay](docs/replay.md) | Validation, replay outcomes, invalidation, ledger views |
| [Adapter contract](docs/adapter.md) | `SteleAdapter`, `TargetWriter`, the Dispatcher and delivery log, the ChatGPT export adapter |
| [Evidence store](docs/archive.md) | Content-addressed Snapshot and artifact archive |
| [Cloud sandboxes](docs/cloud-sandboxes.md) | Design note for hosted sandbox backends (not implemented) |
| [Parser images](parsers/README.md) | Building and running MinerU, Marker, Docling |

## Layout

```
stele/
├── containment/   sandbox backends (bubblewrap, OCI, Wasmtime), staging, seccomp, Landlock
├── archive/       content-addressed evidence store
├── ledger/        artifact ledger, delivery log and hash-chained event log (SQLite, WAL)
├── replay/        validation, replay engine, invalidation, views
├── contracts/     adapter, dispatcher and target-writer protocols
├── adapters/      adapters (ChatGPT export: every branch, streamed)
├── extractors/    deterministic Wasm extractors
└── parsers/       packaged ML parsers in pinned images
parsers/           parser image build files (MinerU, Marker, Docling)
docs/              design docs
tests/
```
