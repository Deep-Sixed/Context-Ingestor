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
`deterministic`). `run_in_sandbox()` picks the first available backend that
covers every requirement — always including filesystem and network isolation —
and otherwise raises `UnsupportedBackendError` (or `SandboxUnavailableError`
when a capable backend exists but cannot run on this host) before anything is
staged or executed. There is no unsandboxed fallback, and parsers cannot
request network access.

| Backend | Enforces / supports |
|---------|---------------------|
| `bubblewrap` | filesystem isolation, network isolation, native libraries |
| `oci-runc` | filesystem isolation, network isolation, native libraries; syscall filter when the engine's default seccomp profile is active; resource limits when the engine enforces them |
| `oci-runc-gpu` | as `oci-runc`, plus GPU — available only when the engine exposes an NVIDIA GPU |
| `oci-runsc` (opt-in, not registered) | as `oci-runc`, with gVisor's user-space kernel as the syscall filter; never GPU |

Registry order is `bubblewrap`, `oci-runc`, `oci-runc-gpu`. On Linux bubblewrap
needs no daemon, image or engine and covers every parser that does not need a
GPU, so it stays first. The OCI backend is chosen where bubblewrap cannot run
(macOS, Windows, hosts without unprivileged user namespaces). The GPU variant
is last and is the only one that passes GPUs through, so only parsers that
declare `requires_gpu` ever see a GPU.

### OCI container backend (roadmap #8)

`stele.containment.oci.OciBackend` drives the Podman or Docker CLI (Podman is
preferred when both exist). Every run gets `--network none`, `--read-only`,
`--cap-drop ALL`, `--security-opt no-new-privileges`, a non-root user,
`--memory`/`--memory-swap`/`--cpus`/`--pids-limit`, tmpfs scratch at `/tmp`,
`/home`, `/mnt`, `/root`, `/run` and `/var`, `--rm`, and a unique `--name` that
is force-removed if the run times out. The container presents the bubblewrap
layout exactly: staged input at `/stele/input/<name>`, parser at
`/stele/parser`, `/stele/output` as the only writable bind, `STELE_INPUT_PATH`
and `STELE_OUTPUT_DIR` set, working directory `/stele/output`. The command runs
as the entrypoint of the image, so it must resolve inside the image (e.g.
`python3 /stele/parser`, not a host interpreter path).

- **User.** Rootless Podman uses `--userns keep-id`; otherwise the parser runs
  as the invoking uid:gid. When Stele itself runs as root, the parser runs as
  `nobody` (65534); the staged input and output directory are handed to that
  uid for the run and handed back afterwards, without following symlinks.
  Rootless Docker is refused (its subordinate-uid mapping cannot preserve
  output ownership); use rootless Podman.
- **Images.** The default image is the official `python:3.12-slim`, pinned by
  digest. Images are never pulled at run time (`--pull never`): fetching is the
  trusted acquisition step, and the backend reports itself unavailable until
  the image is present. The run uses the local image id that was inspected, and
  the image digest is recorded as `SandboxResult.image_digest` for parser
  identity (#12).
- **Runtimes.** `runc` by default; `OciBackend(runtime="runsc")` selects
  gVisor. A runtime the engine does not have makes the backend unavailable
  with that reason — it is never replaced by another runtime. Register gVisor
  with `runsc install -- --network=none` so it builds no network stack at all;
  CI does this, and some Docker hosts refuse runsc with `--network none` alone.
- **GPU.** `OciBackend(gpu=True)` passes NVIDIA GPUs through with CDI
  (`--device nvidia.com/gpu=all`) or Docker's `--gpus all`, and is unavailable
  ("no usable GPU") when the engine exposes none. gVisor supports GPUs only via
  nvproxy for specific drivers, which Stele cannot verify, so GPU is never
  combined with `runsc`.
- **Capabilities are claimed from what the engine reports.** `SYSCALL_FILTER`
  only when `info` shows an active (non-unconfined) seccomp profile, or under
  gVisor. `RESOURCE_LIMITS` only when the engine enforces memory, CPU and PID
  limits — rootless Podman on cgroup v1 accepts the flags but ignores them.
- **Known limits.** Mount paths containing commas or quotes are refused rather
  than escaped. An engine error and a parser exiting 125 are indistinguishable.
  SELinux-enforcing hosts may need volume relabeling, which is not done yet.
  If the Stele process is killed, a running Docker container is not stopped
  (Podman gets `--timeout` as a backstop).

Every run result records the backend that executed it (`SandboxResult.backend`)
and the SHA-256 of the exact staged input bytes (`SandboxResult.input_sha256`;
a manifest digest for directory inputs). Inputs may be a single regular file or
a directory tree of regular files, staged by descriptor without following
symlinks. The containment proofs in `tests/test_phase_e_containment.py` run once
per registered backend.

### What is enforced today vs. still a goal

| Goal | Status |
|------|--------|
| No filesystem access outside the sandbox layout | **Enforced** — mount namespace; only `/usr`, libs, staged input, script and `/stele/output` are visible; `/home`, `/mnt`, `/root`, `/run`, `/var`, `/tmp` are empty tmpfs |
| No network | **Enforced** — `--unshare-net` (loopback only) |
| No direct writes to production targets | **Enforced** — only `/stele/output` survives; everything else is ephemeral |
| No subprocesses without an allowlist | **Not enforced** — parsers may exec any binary under the read-only `/usr`, still inside the same namespaces |
| seccomp syscall filtering | **Not enforced** — no seccomp filter is passed to bwrap yet |

Bubblewrap is Linux-only. On macOS and Windows the containment layer cannot
run natively; use a Linux VM or container (e.g. WSL2, Lima, Docker Desktop).

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

Python binary inside sandbox: selected by the caller; bubblewrap tests use `sys.executable` (CI invokes `/usr/bin/python3`), container tests use the image's `python3`  
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
