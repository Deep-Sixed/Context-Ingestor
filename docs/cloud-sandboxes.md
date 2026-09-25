# Cloud sandboxes as a Stele backend (design note)

**Status:** design note — not implemented. Candidate follow-up to roadmap
#5–#8 (see the tracking issue, #15).

Stele runs every parser through a `SandboxBackend`
(`stele/containment/backend.py`). Today the registered backends all run on the
local host: `bubblewrap` (Linux), `oci-runc` / `oci-runc-gpu` (Podman or
Docker), and `wasmtime` (any OS). A hosted "cloud sandbox" — a remote microVM
or container created on demand by a provider — fits the same interface as one
more backend. This note records how that would work and what it must
guarantee, so a public deployment can offer it without weakening the trust
boundary.

## When a cloud sandbox is the right choice

- **No local isolation available.** A macOS or Windows host without a
  container engine, or a Linux host without unprivileged user namespaces.
- **Hardware isolation for hostile input.** Several providers run each sandbox
  in its own Firecracker microVM, a stronger boundary than namespaces.
- **GPU on demand.** Heavy parsers (MinerU, Marker, Docling — roadmap #9, #10)
  can run on rented GPUs instead of local hardware.
- **Shared or multi-tenant deployments**, where untrusted documents from many
  users must not share a kernel with the Stele host.

## Provider landscape

A survey from an open-source proposal for sandboxed agent execution
([paperclipai/paperclip#248](https://github.com/paperclipai/paperclip/issues/248),
March 2026). Prices and features change; verify before relying on them.

| Provider | Isolation | Persistence | Reported price / hr (1 vCPU) |
|---|---|---|---|
| E2B | Firecracker microVM | Ephemeral | ~$0.05 |
| Cloudflare Sandbox | Container | Loses state on idle | ~$0.072 |
| Daytona | Docker container | Persistent; GPU support | ~$0.067 |
| Fly.io Sprites | Firecracker microVM | Persistent NVMe | not stated |
| Northflank | Kata / gVisor / Firecracker | Both | ~$0.017 |
| Vercel Sandbox | Firecracker microVM | Ephemeral | ~$0.128 |

Self-hostable options named in the same discussion: Alibaba **OpenSandbox**
(a lifecycle server over Docker or Kubernetes, with an in-container exec
sidecar) and plain **Docker**, which Stele already supports through the OCI
backend.

## How it maps onto the backend interface

The proposal's provider interface (create / exec / write file / read file /
destroy) is enough to implement `SandboxBackend.execute()`:

1. **Create** a fresh sandbox from a pinned image for this run only.
2. **Upload** the staged input (already a private, hashed copy — see
   `stele/containment/staging.py`) to `/stele/input/<name>`, and the parser to
   `/stele/parser`. Set `STELE_INPUT_PATH` and `STELE_OUTPUT_DIR`.
3. **Exec** the parser command with the run's timeout.
4. **Download** `/stele/output` into the local `artifact_dir`.
5. **Destroy** the sandbox, whatever happened.

`run_in_sandbox()` then runs its usual host-side checks on the downloaded
output (`collect_artifact_paths` rejects symlinks, FIFOs and unreadable
entries), and the evidence store (#16) hashes it. Nothing reported by the
remote side is trusted as a digest or a path.

## Non-negotiable requirements

A cloud backend may only claim a `Capability` it can actually enforce, like
every other backend.

- **No network for the parser.** Most cloud sandboxes have outbound internet
  by default. The backend must disable egress (provider network policy or
  firewall) and must not claim `NETWORK_ISOLATION` if it cannot. Parsers never
  get network access (#5); model weights are fetched in a separate trusted
  step and baked into the pinned image.
- **No secrets inside the sandbox.** Provider API keys stay on the Stele host
  and are used only for lifecycle calls. Nothing credential-like is uploaded,
  set as an environment variable, or reachable from the sandbox.
- **One sandbox per run, destroyed afterwards.** No reuse across runs, so one
  document cannot influence another's extraction. Persistent sandboxes are out.
- **Verify on download.** Treat downloaded output exactly like local parser
  output: collected by the host's no-follow checks, hashed locally, archived
  by digest. Remote-supplied hashes are informational only.
- **Record identity.** The run result should record the provider, the image
  digest and the sandbox ID, so replay (#14) knows what ran and where.
- **Refuse, never degrade.** If the provider is unreachable or cannot meet the
  parser's requirements, the backend reports itself unavailable and Stele
  refuses the run. It never falls back to a weaker sandbox.

## What changes in the trust model

A cloud backend moves the isolation boundary to the provider. Stele then
trusts the provider's hypervisor or container runtime, and the network path
used for upload and download, in the same way it trusts bubblewrap or the
container engine locally. The host-side checks above still apply to
everything that comes back, so a compromised or misbehaving sandbox can still
only produce rejected output or a failed run — it cannot plant files outside
the artifact directory or forge the ledger's hashes.

## Suggested first implementation

- Start with one provider that offers microVM isolation and egress control
  (E2B was the proposal's first target), behind an optional extra
  (`pip install stele[cloud]`) so the core stays dependency-free.
- Register it after the local backends in `default_backends()`, and only when
  credentials are configured, so local isolation stays the default.
- Reuse the shared containment proofs in `tests/test_phase_e_containment.py`,
  run live in CI only where provider credentials are available.
