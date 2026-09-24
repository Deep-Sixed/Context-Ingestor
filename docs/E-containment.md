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

Use Linux namespaces + seccomp via `bubblewrap` (bwrap) to wrap parser
invocations. Alternatively, a Docker-based sandbox with no volume mounts to
production paths and no network access.

## Inputs allowed inside sandbox

- designated input staging path (read-only)
- model weights / cache (read-only, bind-mounted)
- temp scratch space (ephemeral, not persisted)

## Outputs produced by sandbox

- structured artifact bundle written to `stele/ledger/staging/<run_id>/`
- nothing else leaves the sandbox

## Implementation

- `stele/containment/sandbox.py` — `BubblewrapSandbox.build_argv()`, `SandboxConfig`
- `stele/containment/runner.py` — `run_in_sandbox()`, CLI entry point
- `stele/containment/result.py` — `SandboxResult`
- `tests/test_phase_e_containment.py` — 15 tests, all passing
- `tests/test_trust_boundary.py` — output-boundary regression tests (see below)

Python binary inside sandbox: any interpreter under `/usr` (tests use `STELE_TEST_PYTHON`, default `/usr/bin/python3`, resolved on the host)  
Network namespace: unshared (`--unshare-net`)  
Ephemeral mounts: `/tmp`, `/home`, `/mnt`, `/root`, `/run`, `/var`  
Writable path: `/stele/output` only (bind-mounted from `artifact_dir`)

## Completion criteria

- [x] bwrap wrapper script written (`sandbox.py`)
- [x] parser invocation tested with fake parser inside sandbox
- [x] confirmed: no writes outside staging path during parser run
- [x] confirmed: network blocked inside sandbox
- [x] Phase F staging path: `artifact_dir` (caller-supplied); ledger schema TBD in Phase F

## Output trust boundary

Everything in `artifact_dir` is written by an untrusted parser, so the host
never trusts a path found there:

- Artifacts are collected without following symlinks. Symlinks (to files or
  directories, dangling or not), FIFOs, sockets and devices are reported in
  `SandboxResult.rejected_paths`, and any rejected entry makes the run not
  `succeeded`, so it cannot be ledgered.
- Files are hashed through an `O_NOFOLLOW` descriptor checked with `fstat`,
  and paths under a symlinked directory are refused.
- `LedgerStore.commit()` re-hashes every file against the manifest; content
  changed after hashing raises `ArtifactDriftError`.
- The replay validator reports a file swapped for a symlink or directory as
  drift and never follows it.
- `run_in_sandbox()` refuses a non-empty `artifact_dir`, so leftover files are
  never attributed to a new run.
- bwrap runs with `--new-session` and stdin from `/dev/null` (no terminal
  injection), plus `--unshare-cgroup-try`.
