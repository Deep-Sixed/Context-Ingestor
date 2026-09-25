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
| `bubblewrap` | host processes | filesystem isolation, network isolation, native libraries; syscall filter on x86_64/aarch64 Linux with seccomp filter support (roadmap #6) |
| `oci-runc` | host processes | filesystem isolation, network isolation, native libraries; syscall filter when the engine's default seccomp profile is active; resource limits when the engine enforces them |
| `oci-runc-gpu` | host processes | as `oci-runc`, plus GPU — available only when the engine exposes an NVIDIA GPU |
| `oci-runsc` (opt-in, not registered) | host processes | as `oci-runc`, with gVisor's user-space kernel as the syscall filter; never GPU |
| `wasmtime` | Wasm modules | filesystem isolation, network isolation, resource limits, deterministic |

Registry order is `bubblewrap`, `oci-runc`, `oci-runc-gpu`, `wasmtime`. On
Linux bubblewrap needs no daemon, image or engine and covers every
host-process parser that does not need a GPU, so it stays first. The OCI
backend is chosen where bubblewrap cannot run (macOS, Windows, hosts without
unprivileged user namespaces). The GPU variant is the only one that passes
GPUs through, so only parsers that declare `requires_gpu` ever see a GPU.
Routing: deterministic Wasm extractors get `wasmtime`; a deterministic
host-process parser is refused (no backend fixes the clock and entropy for
native code yet).

Hosted cloud sandboxes (E2B, Daytona, Fly.io and similar) could be added as
another backend; the requirements they must meet are in
[cloud-sandboxes.md](cloud-sandboxes.md). Not implemented yet.

### Packaged parsers (roadmap #9, #10)

Heavy ML parsers (MinerU, Marker, Docling) run on the OCI backend in pinned images with
model weights baked in: `stele.parsers.run_parser()` or
`python -m stele.parsers run`. On top of the backend's guarantees, these runs
require enforced resource limits (`ParserRequirements.resource_limits`), keep
no output from a failed, timed-out or OOM-killed run
(`run_in_sandbox(discard_failed_output=True)`), and record the image digest
and a digest of the parser configuration as the parser's identity. Build
files, output layout and exit statuses are in
[parsers/README.md](../parsers/README.md).

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
per registered host-process backend; Wasm backends prove the same guarantees in
`tests/test_wasm_backend.py`.

### What is enforced today vs. still a goal

| Goal | Status |
|------|--------|
| No filesystem access outside the sandbox layout | **Enforced** — mount namespace; only `/usr`, libs, staged input, script and `/stele/output` are visible; `/home`, `/mnt`, `/root`, `/run`, `/var`, `/tmp` are empty tmpfs; the tmpfs root itself is remounted read-only |
| No network | **Enforced** — `--unshare-net` (loopback only) |
| No direct writes to production targets | **Enforced** — only `/stele/output` survives; everything else is ephemeral or read-only |
| Writes only under `/stele/output` | **Enforced where the kernel has Landlock** — writes to the other tmpfs mounts fail with `EACCES`. Each run's own ephemeral `/tmp` is writable scratch space by default (`writable_scratch=True`, matching the container backend) and is discarded when the run ends; set `writable_scratch=False` to allow writes only under `/stele/output`. Device sinks (`/dev/null`, `/dev/zero`, `/dev/full`) stay writable. Without Landlock the mount layout alone applies: writes outside `/stele/output` never persist |
| No subprocesses without an allowlist | **Enforced where the kernel has Landlock** — `execve` is allowed only for `command[0]`, `SandboxConfig.exec_allowlist`, and the ELF/`#!` interpreters they need; other binaries, including ones the parser writes, fail with `EACCES`. See limits below |
| seccomp syscall filtering | **Enforced on x86_64 and aarch64** — denylist passed via `bwrap --seccomp`; blocked calls kill the parser and the run result reports it. Other architectures do not claim `SYSCALL_FILTER` |

The run result lists the layers applied (`SandboxResult.hardening`, e.g.
`("seccomp", "landlock")`) and sets `SandboxResult.violation` when the parser
was killed by the syscall filter.

### Syscall filter (roadmap #6)

`stele/containment/seccomp.py` generates a classic-BPF program in pure Python
(no libseccomp dependency) and the bubblewrap backend passes it to bwrap on a
memfd with `--seccomp <fd>`. bwrap installs it immediately before exec, so it
covers the parser and every process it starts.

It is a **denylist, not an allowlist**. Python parser runtimes (CPython,
NumPy, PyTorch, OCR engines) use a broad, version-dependent set of syscalls;
an allowlist tight enough to matter breaks them unpredictably. The filter
instead blocks the dangerous families explicitly:

- **Killed** (`SECCOMP_RET_KILL_PROCESS`, exit status `128 + SIGSYS` = 159):
  `ptrace`, `process_vm_readv/writev`, `kcmp`, `pidfd_getfd`,
  `process_madvise`, `keyctl`, `add_key`, `request_key`, `bpf`,
  `perf_event_open`, `userfaultfd`, `kexec_load`, `kexec_file_load`,
  `init_module`, `finit_module`, `delete_module`, `mount`, `umount2`,
  `pivot_root`, `chroot`, `unshare`, `setns`, the new mount API
  (`open_tree`, `move_mount`, `fsopen`, `fsconfig`, `fsmount`, `fspick`,
  `mount_setattr`), `open_by_handle_at`, `name_to_handle_at`, `acct`,
  `swapon/off`, `reboot`, `syslog`, `quotactl(_fd)`, `lookup_dcookie`, and on
  x86_64 `iopl`, `ioperm`, `uselib`, `_sysctl`.
- **Killed:** `clone` with any `CLONE_NEW*` flag, so namespace creation cannot
  bypass the `unshare` ban.
- **Killed:** any syscall through a non-native ABI (i386 `int 0x80`, x32,
  32-bit ARM), whose different numbering would otherwise bypass every rule.
- **ENOSYS:** `clone3` (flags live in memory the filter cannot read; glibc
  falls back to `clone`) and io_uring (its operations bypass the filter;
  runtimes fall back to ordinary I/O).
- **EPERM:** the `TIOCSTI`/`TIOCLINUX` terminal-injection ioctls.

A parser can fake a violation report only by exiting 159 itself, which fails
its own run.

### Landlock and the exec allowlist (roadmap #6)

When the kernel offers Landlock, the command runs through a trusted launcher
(`stele/containment/landlock.py`, bound read-only at `/stele/landlock` and run
by the system Python under `/usr`). The launcher restricts itself with
Landlock and then execs the parser, which starts already confined. It fails
closed with exit 126 if any Landlock step is refused. Reads are not
restricted by Landlock; the mount layout already limits what exists.

Limits of the exec allowlist: it restricts what `execve` may start. A parser is
already arbitrary code, so it can still map and run code in its own process
(e.g. through `ctypes`, or by running the dynamic loader, which every dynamic
binary needs, as a program). Commands must name the real program: `command[0]`
= `/usr/bin/env` would allow only `env`.

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
- `stele/containment/seccomp.py` — seccomp BPF generator (x86_64, aarch64)
- `stele/containment/landlock.py` — Landlock probe and in-sandbox launcher (write + exec rules)
- `stele/containment/runner.py` — `run_in_sandbox()`, CLI entry point
- `stele/containment/result.py` — `SandboxResult`
- `tests/test_phase_e_containment.py` — 16 live/structural containment tests
- `tests/test_input_staging.py` — 4 trusted-input staging regression tests
- `tests/test_artifact_boundary.py` — 3 trusted-output artifact regressions
- `tests/test_linux_hardening.py` — seccomp program checks (BPF interpreter), live seccomp/Landlock/exec-allowlist/mount-layout proofs
- `tests/test_followup_hardening.py` — 11 regressions: FIFO no-block, session/stdin isolation, fresh output dir, ledger duplicate/invalidation fixes

Python binary inside sandbox: selected by the caller; bubblewrap tests use `sys.executable` (CI invokes `/usr/bin/python3`), container tests use the image's `python3`  
User namespace: unshared (`--unshare-user`, uid/gid 0 inside namespace only)  
Network namespace: unshared (`--unshare-net`)  
Cgroup namespace: unshared where supported (`--unshare-cgroup-try`)  
Terminal: own session (`--new-session`) and stdin from `/dev/null`, so a parser cannot inject keystrokes (TIOCSTI) into the caller's terminal  
Output directory: must be empty (or absent) at run start; leftovers are refused rather than credited to the new run  
Ephemeral mounts: `/tmp`, `/home`, `/mnt`, `/root`, `/run`, `/var`  
Writable path: `/stele/output` only (bind-mounted from `artifact_dir`)  
Syscall filter: seccomp denylist via `--seccomp` (x86_64, aarch64)  
Landlock: writes only under `/stele/output`, exec only of allowlisted programs, where the kernel supports it

## Completion criteria

- [x] bwrap wrapper script written (`sandbox.py`)
- [x] parser invocation tested with fake parser inside sandbox
- [x] confirmed: no writes outside staging path during parser run
- [x] confirmed: network blocked inside sandbox
- [x] untrusted input symlinks/non-regular files refused before sandbox bind; copied and hashed from one `O_NOFOLLOW` descriptor
- [x] parser-created symlink/non-regular output refused before it can reach the ledger
- [x] network namespace proof sees only loopback inside the parser sandbox
- [x] artifact/input opens use `O_NONBLOCK`, so a parser-planted FIFO is rejected instead of hanging hashing, commit, or replay
- [x] seccomp filter kills blocked syscalls and non-native ABIs; violation reported in the run result
- [x] Landlock refuses writes outside `/stele/output` and exec outside the allowlist
- [x] Phase F staging path: `artifact_dir` (caller-supplied); ledger schema TBD in Phase F
