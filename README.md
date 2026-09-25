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
- **Record what it produced.** Output is collected without following
  symlinks, hashed, and recorded in a ledger as `pending`. A record becomes
  `committed` only after the downstream write succeeds and the bundle is
  re-verified.
- **Keep evidence.** A content-addressed store can archive the exact input
  bytes the parser saw and every artifact it produced.
- **Replay and invalidate.** Committed runs can be re-checked for drift or
  missing files, and invalidated by run or by source, without rewriting
  history.
- **Write through one door.** Adapters turn a ledger record into typed,
  hash-checked chunks. Only a Dispatcher, using writers you register, sends
  them to target stores.

```
source ─▶ staging ─▶ sandboxed parser ─▶ /stele/output ─▶ ledger (pending)
                                                              │
                        target store ◀─ TargetWriter ◀─ Dispatcher ◀─ Adapter
                                                              │
                                                     ledger (committed)
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

Or from Python, from sandbox run to ledger to target:

```python
from pathlib import Path

from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig
from stele.contracts.dispatcher import Dispatcher
from stele.ledger.store import LedgerStore
from stele.ledger.transaction import ledger_transaction

result = run_in_sandbox(SandboxConfig(
    command=["/usr/bin/python3", "/stele/parser"],
    script_path=Path("my_parser.py"),
    input_path=Path("paper.pdf"),
    artifact_dir=Path("out"),
))

store = LedgerStore(Path("ledger.db"))
dispatcher = Dispatcher()
dispatcher.register_target(MyTarget, my_writer)    # your TargetWriter

with ledger_transaction(store, result) as record:  # PENDING
    outcome = dispatcher.dispatch(my_adapter, record, MyTarget("docs"))
    if outcome.status != "success":
        raise RuntimeError(outcome.error)          # record becomes FAILED
# clean exit: record is COMMITTED
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
| Tamper-evident artifacts | Hashes are computed on the host from no-follow opens and re-verified at commit and replay |
| Adapter isolation | **Contract only.** Adapters run in-process as trusted code. Only parsers are sandboxed. |

Details and known limits: [docs/containment.md](docs/containment.md).

## Documentation

| Doc | Covers |
|-----|--------|
| [Containment](docs/containment.md) | Sandbox backends, seccomp, Landlock, OCI and Wasm details |
| [Ledger](docs/ledger.md) | Record fields, pending → committed protocol |
| [Replay](docs/replay.md) | Drift detection, invalidation, ledger views |
| [Adapter contract](docs/adapter.md) | `SteleAdapter`, `Dispatcher`, `TargetWriter` |
| [Evidence store](docs/archive.md) | Content-addressed Snapshot and artifact archive |
| [Cloud sandboxes](docs/cloud-sandboxes.md) | Design note for hosted sandbox backends (not implemented) |
| [Parser images](parsers/README.md) | Building and running MinerU, Marker, Docling |

## Layout

```
stele/
├── containment/   sandbox backends (bubblewrap, OCI, Wasmtime), staging, seccomp, Landlock
├── archive/       content-addressed evidence store
├── ledger/        artifact ledger (SQLite, WAL)
├── replay/        drift detection, invalidation, views
├── contracts/     adapter, dispatcher and target-writer protocols
├── extractors/    deterministic Wasm extractors
└── parsers/       packaged ML parsers in pinned images
parsers/           parser image build files (MinerU, Marker, Docling)
docs/              design docs
tests/
```
