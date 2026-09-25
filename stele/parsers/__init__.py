"""
Packaged ML parsers that run in pinned OCI images (roadmap #9, #10).

A ParserImage describes one parser packaged as a container image: the image
reference, the command baked into it, its default configuration, the
resources it may use, and whether it can use a GPU. run_parser() runs a
document through it on the OCI backend with Stele's usual guarantees (no
network, read-only root, staged input, output only under /stele/output) plus:

- Resource limits that are enforced, not just requested. The run is refused on
  a host whose container engine cannot enforce memory/CPU/PID caps. Thread
  pools are capped to the CPU allowance through the usual environment
  variables (OMP_NUM_THREADS, MKL_NUM_THREADS, ...).
- No partial output. A run that fails, times out or is killed (e.g. at the
  memory limit) keeps nothing it wrote, and nothing is stored.
- Identity. Every run records the image's content digest and a digest of the
  exact parser configuration it was given, for the ledger (#12) and replay
  (#14).

The images are built in a separate, trusted step (``parsers/`` holds their
build files and ``python -m stele.parsers build-command`` prints the command).
Model weights are baked into the image at build time; nothing is downloaded
when a document is parsed, because the parser has no network.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping

from ..archive.records import canonical_json
from ..containment.backend import (
    ParserRequirements,
    SandboxBackend,
    SandboxUnavailableError,
    select_backend,
)
from ..containment.oci import OciBackend
from ..containment.result import SandboxResult
from ..containment.runner import run_in_sandbox
from ..containment.sandbox import SandboxConfig

if TYPE_CHECKING:
    from ..archive.store import BlobStore

__all__ = [
    "GpuPolicy",
    "ParserIdentity",
    "ParserImage",
    "ParserRun",
    "UnsupportedInputError",
    "run_parser",
    "thread_env",
]

# never: CPU only. optional: use a GPU when the host has one, CPU otherwise.
# required: refuse to run without a GPU.
GpuPolicy = Literal["never", "optional", "required"]
Device = Literal["auto", "cpu", "gpu"]

# Environment variable carrying the parser configuration (canonical JSON).
CONFIG_ENV = "STELE_PARSER_CONFIG"

# Exit status of a process killed by SIGKILL, which is how the kernel's OOM
# killer ends a process at the container's memory limit.
_SIGKILL_EXIT = 137

# Thread-pool variables honoured by OpenMP, BLAS, onnxruntime, PyTorch (via
# the entry scripts) and friends. Capping them to the CPU allowance keeps a
# parser from oversubscribing the cores it is limited to.
_THREAD_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "STELE_TORCH_THREADS",
)


class UnsupportedInputError(ValueError):
    """The parser does not accept this kind of document."""


def thread_env(cpus: float) -> dict[str, str]:
    """Thread-pool caps for a CPU allowance of `cpus` (at least one thread)."""
    threads = str(max(1, math.floor(cpus)))
    return {name: threads for name in _THREAD_VARS}


@dataclass(frozen=True)
class ParserImage:
    """One parser packaged as a pinned container image."""

    name: str
    # Upstream parser version installed in the image.
    version: str
    # Local image reference the build step tags. The content digest the engine
    # reports for it (not this name) is what identifies the parser.
    image: str
    # Command run inside the image; reads STELE_INPUT_PATH, writes under
    # STELE_OUTPUT_DIR.
    command: tuple[str, ...] = ("python3", "/opt/stele/entry.py")
    # Default configuration, passed to the parser as canonical JSON in
    # STELE_PARSER_CONFIG and recorded (with its digest) as part of the
    # parser's identity.
    config: Mapping[str, Any] = field(default_factory=dict)
    # Lower-case file suffixes the parser accepts, e.g. (".pdf",).
    formats: tuple[str, ...] = ()
    gpu: GpuPolicy = "never"
    # Image used on a GPU host when it differs from the CPU image.
    gpu_image: str | None = None
    # Resource limits enforced by the container engine.
    memory: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 1024
    tmpfs_size: str = "1g"
    timeout_seconds: int = 1800
    # Build context and Containerfile, relative to the repository root.
    build_context: str = "parsers"
    containerfile: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        if self.gpu not in ("never", "optional", "required"):
            raise ValueError(f"invalid GPU policy {self.gpu!r}")

    def image_for(self, device: Literal["cpu", "gpu"]) -> str:
        return self.gpu_image if device == "gpu" and self.gpu_image else self.image

    def build_command(self, engine: str = "docker", device: Literal["cpu", "gpu"] = "cpu") -> list[str]:
        """The trusted build step for this image (run from the repo root)."""
        containerfile = self.containerfile
        if device == "gpu" and self.gpu_image:
            containerfile = containerfile + ".gpu"
        return [
            engine, "build",
            "--file", containerfile,
            "--tag", self.image_for(device),
            self.build_context,
        ]


@dataclass(frozen=True)
class ParserIdentity:
    """What ran: recorded with every parser run for the ledger and replay."""

    parser: str
    version: str
    image: str
    image_digest: str | None
    device: Literal["cpu", "gpu"]
    config: Mapping[str, Any]
    config_sha256: str
    memory: str
    cpus: float
    pids_limit: int
    timeout_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "parser": self.parser,
            "version": self.version,
            "image": self.image,
            "image_digest": self.image_digest,
            "device": self.device,
            "config": dict(self.config),
            "config_sha256": self.config_sha256,
            "limits": {
                "memory": self.memory,
                "cpus": self.cpus,
                "pids": self.pids_limit,
                "timeout_seconds": self.timeout_seconds,
            },
        }


@dataclass(frozen=True)
class ParserRun:
    """The outcome of run_parser()."""

    result: SandboxResult
    identity: ParserIdentity
    # Why the run failed, in words; None when it succeeded.
    failure: str | None

    @property
    def succeeded(self) -> bool:
        return self.failure is None


def config_digest(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(config))).hexdigest()


def _backend(parser: ParserImage, device: Literal["cpu", "gpu"], *, engine: str | None,
             memory: str, cpus: float) -> OciBackend:
    return OciBackend(
        image=parser.image_for(device),
        engine=engine,
        gpu=device == "gpu",
        memory=memory,
        cpus=cpus,
        pids_limit=parser.pids_limit,
        tmpfs_size=parser.tmpfs_size,
    )


def choose_backend(
    parser: ParserImage,
    *,
    device: Device = "auto",
    engine: str | None = None,
    memory: str | None = None,
    cpus: float | None = None,
    backends: list[SandboxBackend] | None = None,
) -> tuple[SandboxBackend, Literal["cpu", "gpu"], ParserRequirements]:
    """Pick the backend (and so the device) for a parser run.

    A parser whose GPU policy is "optional" runs on a GPU when this host can
    pass one through and on the CPU otherwise; that fallback is the parser's
    own declaration, never a silent downgrade. "required" never falls back.
    `backends` replaces the OCI backends that would be built (for tests).
    """
    if device == "gpu" and parser.gpu == "never":
        raise UnsupportedInputError(f"parser {parser.name} has no GPU build")
    if device == "cpu" and parser.gpu == "required":
        raise UnsupportedInputError(f"parser {parser.name} requires a GPU")

    memory = memory or parser.memory
    cpus = cpus or parser.cpus
    wanted: list[Literal["cpu", "gpu"]]
    if device != "auto":
        wanted = [device]
    elif parser.gpu == "required":
        wanted = ["gpu"]
    elif parser.gpu == "optional":
        wanted = ["gpu", "cpu"]
    else:
        wanted = ["cpu"]

    errors: list[str] = []
    for i, dev in enumerate(wanted):
        requirements = ParserRequirements(
            requires_gpu=dev == "gpu",
            requires_native_libs=True,
            resource_limits=True,
        )
        candidates = (
            [backends[i]] if backends is not None
            else [_backend(parser, dev, engine=engine, memory=memory, cpus=cpus)]
        )
        try:
            return select_backend(requirements, candidates), dev, requirements
        except SandboxUnavailableError as exc:
            errors.append(str(exc))
            if dev == "gpu" and len(wanted) > 1:
                continue  # optional GPU: fall back to the CPU image
            raise SandboxUnavailableError("; ".join(errors)) from exc
    raise SandboxUnavailableError("; ".join(errors))


def describe_failure(result: SandboxResult, *, memory: str, timeout: int) -> str | None:
    """Explain an unsuccessful run in words; None if it succeeded."""
    if result.timed_out:
        return f"timed out after {timeout}s; the run was stopped and its output discarded"
    if result.violation:
        return f"sandbox policy violation: {result.violation}"
    if result.exit_code == _SIGKILL_EXIT:
        return (
            f"killed by SIGKILL (exit {_SIGKILL_EXIT}), most likely at the "
            f"{memory} memory limit; output discarded"
        )
    if result.exit_code != 0:
        detail = _last_line(result.stderr)
        return f"parser exited with status {result.exit_code}" + (f": {detail}" if detail else "")
    return None


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:500] if lines else ""


def run_parser(
    parser: ParserImage,
    input_path: Path,
    artifact_dir: Path,
    *,
    store: BlobStore | None = None,
    config: Mapping[str, Any] | None = None,
    device: Device = "auto",
    engine: str | None = None,
    memory: str | None = None,
    cpus: float | None = None,
    timeout_seconds: int | None = None,
    backend: SandboxBackend | None = None,
) -> ParserRun:
    """Run one document through a packaged parser.

    `config` replaces keys of the parser's default configuration; the merged
    configuration is what the parser receives and what the identity records.
    Raises UnsupportedInputError for a document type the parser does not
    accept, and SandboxUnavailableError when no suitable backend (image,
    engine, enforced limits, GPU if required) is available. Parser failures
    are not raised: they come back as ParserRun.failure, with no output kept.
    """
    input_path = Path(input_path)
    suffix = input_path.suffix.lower()
    if parser.formats and suffix not in parser.formats:
        raise UnsupportedInputError(
            f"{parser.name} does not accept {suffix or 'files without a suffix'} "
            f"(accepted: {', '.join(parser.formats)})"
        )

    merged = {**parser.config, **(config or {})}
    memory = memory or parser.memory
    cpus = cpus or parser.cpus
    timeout = timeout_seconds or parser.timeout_seconds

    if backend is not None:
        chosen = backend
        # Record the limits the given backend actually applies.
        memory = getattr(backend, "memory", memory)
        cpus = getattr(backend, "cpus", cpus)
        dev: Literal["cpu", "gpu"] = "gpu" if getattr(backend, "gpu", False) else "cpu"
        requirements = ParserRequirements(
            requires_gpu=dev == "gpu", requires_native_libs=True, resource_limits=True,
        )
    else:
        chosen, dev, requirements = choose_backend(
            parser, device=device, engine=engine, memory=memory, cpus=cpus,
        )

    env = {**thread_env(cpus), CONFIG_ENV: canonical_json(merged).decode("utf-8")}
    env["STELE_PARSER_DEVICE"] = dev
    result = run_in_sandbox(
        SandboxConfig(
            command=list(parser.command),
            artifact_dir=Path(artifact_dir),
            input_path=input_path,
            env=env,
            timeout_seconds=timeout,
        ),
        requirements=requirements,
        backend=chosen,
        store=store,
        discard_failed_output=True,
    )
    identity = ParserIdentity(
        parser=parser.name,
        version=parser.version,
        image=getattr(chosen, "image", parser.image_for(dev)),
        image_digest=result.image_digest,
        device=dev,
        config=MappingProxyType(merged),
        config_sha256=config_digest(merged),
        memory=memory,
        cpus=cpus,
        pids_limit=getattr(chosen, "pids_limit", parser.pids_limit),
        timeout_seconds=timeout,
    )
    return ParserRun(
        result=result,
        identity=identity,
        failure=describe_failure(result, memory=memory, timeout=timeout),
    )
