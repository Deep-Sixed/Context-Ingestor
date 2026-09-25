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
| `bubblewrap` | filesystem isolation, network isolation, native libraries; syscall filter on x86_64/aarch64 Linux with seccomp filter support (roadmap #6) |

Every run result records the backend that executed it (`SandboxResult.backend`)
and the SHA-256 of the exact staged input bytes (`SandboxResult.input_sha256`;
a manifest digest for directory inputs). Inputs may be a single regular file or
a directory tree of regular files, staged by descriptor without following
symlinks. The containment proofs in `tests/test_phase_e_containment.py` run once
per registered backend.

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
- `stele/containment/seccomp.py` — seccomp BPF generator (x86_64, aarch64)
- `stele/containment/landlock.py` — Landlock probe and in-sandbox launcher (write + exec rules)
- `stele/containment/runner.py` — `run_in_sandbox()`, CLI entry point
- `stele/containment/result.py` — `SandboxResult`
- `tests/test_phase_e_containment.py` — 16 live/structural containment tests
- `tests/test_input_staging.py` — 4 trusted-input staging regression tests
- `tests/test_artifact_boundary.py` — 3 trusted-output artifact regressions
- `tests/test_linux_hardening.py` — seccomp program checks (BPF interpreter), live seccomp/Landlock/exec-allowlist/mount-layout proofs
- `tests/test_followup_hardening.py` — 11 regressions: FIFO no-block, session/stdin isolation, fresh output dir, ledger duplicate/invalidation fixes

Python binary inside sandbox: selected by the caller; tests use `sys.executable` (CI invokes `/usr/bin/python3`)  
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
