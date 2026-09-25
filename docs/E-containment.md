# Phase E — bubblewrap Parser-Containment Gate

**Status:** COMPLETE — 2026-06-26  
**Unblocks:** Phase F

## Goal

Parser execution (RAG-ANYTHING, MinerU, Marker) must run inside an isolated
execution context. Parsers may not:
- open arbitrary filesystem paths outside a designated input sandbox
- make outbound network calls
- write directly to any Hindsight, Graphify, LightRAG, or MetaRouter target
- spawn subprocesses without explicit allowlist

## Mechanism

Use Linux namespaces via `bubblewrap` (bwrap) to wrap parser invocations.
Alternatively, a Docker-based sandbox with no volume mounts to production
paths and no network access.

### Sandbox backends (roadmap #5)

Parser execution goes through `stele.containment.backend.SandboxBackend`.
Each backend declares the `Capability` values it enforces or supports; each
parser declares `ParserRequirements` (`requires_gpu`, `requires_native_libs`,
`deterministic`, `wasm_module`). `run_in_sandbox()` picks the first available
backend that covers every requirement — always including filesystem and
network isolation — and otherwise raises `UnsupportedBackendError` (or
`SandboxUnavailableError` when a capable backend exists but cannot run on this
host) before anything is staged or executed. There is no unsandboxed fallback,
and parsers cannot request network access.

Every parser is one kind of workload, and the kind is a required capability:
a host process (`host_process`, the default: `config.command` is an
executable) or a WebAssembly module (`wasm_module`, with
`ParserRequirements(wasm_module=True)`). Because no backend hosts both, a
Python-script parser can never be routed to the Wasm backend, and a Wasm module
never to bubblewrap, whatever the registry order. The registry lists
bubblewrap first, then Wasmtime; order would only matter between two backends
for the same kind of workload.

| Backend | Hosts | Enforces / supports |
|---------|-------|---------------------|
| `bubblewrap` | host processes | filesystem isolation, network isolation, native libraries |
| `wasmtime` | Wasm modules | filesystem isolation, network isolation, resource limits, deterministic |

Routing: deterministic Wasm extractors get `wasmtime`; native-library parsers
get `bubblewrap`; a deterministic host-process parser is refused (no backend
fixes the clock and entropy for native code yet).

Every run result records the backend that executed it (`SandboxResult.backend`)
and the SHA-256 of the exact staged input bytes (`SandboxResult.input_sha256`;
a manifest digest for directory inputs). Inputs may be a single regular file or
a directory tree of regular files, staged by descriptor without following
symlinks. The containment proofs in `tests/test_phase_e_containment.py` run once
per registered host-process backend; Wasm backends prove the same guarantees in
`tests/test_wasm_backend.py`.

### What is enforced today vs. still a goal

| Goal | Status |
|------|--------|
| No filesystem access outside the sandbox layout | **Enforced** — mount namespace; only `/usr`, libs, staged input, script and `/stele/output` are visible; `/home`, `/mnt`, `/root`, `/run`, `/var`, `/tmp` are empty tmpfs |
| No network | **Enforced** — `--unshare-net` (loopback only) |
| No direct writes to production targets | **Enforced** — only `/stele/output` survives; everything else is ephemeral |
| No subprocesses without an allowlist | **Not enforced** — parsers may exec any binary under the read-only `/usr`, still inside the same namespaces |
| seccomp syscall filtering | **Not enforced** — no seccomp filter is passed to bwrap yet |

Bubblewrap is Linux-only. On macOS and Windows the containment layer cannot
run host-process parsers natively; use a Linux VM or container (e.g. WSL2,
Lima, Docker Desktop). Wasm parsers run natively on all three.

### Wasm/WASI backend (roadmap #7)

`stele.containment.wasm.WasmtimeBackend` runs WebAssembly modules with the
`wasmtime` Python package (`pip install 'stele[wasm]'`; included in `dev`). It
is available on Linux, macOS and Windows, x86 and ARM.

A Wasm parser uses the same `SandboxConfig`: `command[0]` is the host path of
a `.wasm` binary or `.wat` text module and the rest are guest arguments (the
guest's argv is `[<module file name>, *command[1:]]`). `script_path` is
refused; `extra_ro_binds` directories become extra read-only preopens.

| Guarantee | How |
|-----------|-----|
| Filesystem | Only `/stele/output` (read-write, fd 3) and `/stele/input` (read-only, fd 4, the private staging directory holding just the staged input) are preopened; WASI resolves every path beneath a preopen, so `..`, absolute paths and other host paths are unreachable. Symlink and hard-link creation are refused. |
| Network | WASI preview 1 cannot create sockets and none is preopened; a module importing anything outside `wasi_snapshot_preview1` (e.g. `wasi:sockets`) fails to link. |
| Environment | Empty apart from `STELE_OUTPUT_DIR`, `STELE_INPUT_PATH` and `config.env`; stdin is empty. |
| Memory | Store limits (default 512 MiB): `memory.grow` fails at the cap and a module needing more initial memory fails to instantiate. |
| CPU | Fuel (default 5·10¹⁰ units): exhaustion traps, deterministically. |
| Wall clock | `timeout_seconds` via epoch interruption; reported as `timed_out`. |
| Determinism | Fresh engine, store and WASI context per run. `clock_time_get` starts at 2000-01-01T00:00:00Z and advances 1 µs per read; `random_get` is a SHA-256 counter stream from a fixed seed; `poll_oneoff` (sleep) is refused. File timestamps are fixed, inode numbers are per-run sequence numbers, link counts are 1, directory sizes 0, and directory listings are sorted by name. NaNs are canonicalised; threads, relaxed SIMD, multi-memory and memory64 are disabled. |
| Identity | SHA-256 of the executed module binary (after WAT compilation) in `SandboxResult.module_sha256`; `wasm_module_sha256(path)` computes it ahead of a run. Recording it in the ledger is roadmap #12. |

A trap (including the memory and fuel limits) fails the run with exit code
134 and the reason on stderr; a module that cannot be loaded, compiled or
linked fails with 127. Host filesystem naming rules (case sensitivity,
characters Windows forbids) still apply; deterministic parsers should write
portable names.

On Windows, which lacks `O_NOFOLLOW`, single-file inputs are staged by
`lstat`-checking the file (refusing symlinks and reparse points) and accepting
the opened file only if its volume and file index match. Directory inputs
still need `os.fwalk` and are refused there.

The first Wasm extractor is `stele/extractors/chatgpt_export_split.wat`: it
streams a ChatGPT export's `conversations.json` and writes each conversation's
exact bytes to `conversation-NNNNNN.json`, with `index.jsonl` giving each
one's byte offset and length in the input. Run it with
`chatgpt_export_split_config()` and `WASM_EXTRACTOR_REQUIREMENTS` from
`stele.extractors`. `tests/test_wasm_backend.py` proves the guarantees above
with a probe module (`tests/fixtures/wasm/probe.wat`) and checks that the
extractor's output digest is identical on Linux, macOS and Windows.

## Inputs allowed inside sandbox

- designated regular input file, copied first into a private Stele-owned staging directory and then mounted read-only
- model weights / cache (read-only, bind-mounted)
- temp scratch space (ephemeral, not persisted)

## Outputs produced by sandbox

- structured artifact bundle written to `stele/ledger/staging/<run_id>/`
- nothing else leaves the sandbox

## Implementation

- `stele/containment/sandbox.py` — `BubblewrapSandbox.build_argv()`, `SandboxConfig`
- `stele/containment/staging.py` — `lstat` + `O_NOFOLLOW` trusted input staging; hashes the same descriptor it copies
- `stele/containment/artifacts.py` — post-sandbox `lstat` collection; rejects symlink and non-regular parser outputs
- `stele/containment/runner.py` — `run_in_sandbox()`, CLI entry point
- `stele/containment/result.py` — `SandboxResult`
- `tests/test_phase_e_containment.py` — 16 live/structural containment tests
- `tests/test_input_staging.py` — 4 trusted-input staging regression tests
- `tests/test_artifact_boundary.py` — 3 trusted-output artifact regressions
- `tests/test_followup_hardening.py` — 11 regressions: FIFO no-block, session/stdin isolation, fresh output dir, ledger duplicate/invalidation fixes

Python binary inside sandbox: selected by the caller; tests use `sys.executable` (CI invokes `/usr/bin/python3`)  
User namespace: unshared (`--unshare-user`, uid/gid 0 inside namespace only)  
Network namespace: unshared (`--unshare-net`)  
Cgroup namespace: unshared where supported (`--unshare-cgroup-try`)  
Terminal: own session (`--new-session`) and stdin from `/dev/null`, so a parser cannot inject keystrokes (TIOCSTI) into the caller's terminal  
Output directory: must be empty (or absent) at run start; leftovers are refused rather than credited to the new run  
Ephemeral mounts: `/tmp`, `/home`, `/mnt`, `/root`, `/run`, `/var`  
Writable path: `/stele/output` only (bind-mounted from `artifact_dir`)

## Completion criteria

- [x] bwrap wrapper script written (`sandbox.py`)
- [x] parser invocation tested with fake parser inside sandbox
- [x] confirmed: no writes outside staging path during parser run
- [x] confirmed: network blocked inside sandbox
- [x] untrusted input symlinks/non-regular files refused before sandbox bind; copied and hashed from one `O_NOFOLLOW` descriptor
- [x] parser-created symlink/non-regular output refused before it can reach the ledger
- [x] network namespace proof sees only loopback inside the parser sandbox
- [x] artifact/input opens use `O_NONBLOCK`, so a parser-planted FIFO is rejected instead of hanging hashing, commit, or replay
- [x] Phase F staging path: `artifact_dir` (caller-supplied); ledger schema TBD in Phase F
