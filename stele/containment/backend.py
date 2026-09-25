"""
Sandbox backends and capability matching (roadmap #5).

A backend is one isolation technology (bubblewrap today; Wasm/WASI and OCI
containers later). Each backend declares the guarantees it enforces and the
workloads it can host. Each parser declares its requirements. Stele runs a
parser only on a backend that satisfies every requirement and never falls back
to unsandboxed execution.

Parsers cannot request network access: there is deliberately no requirement or
capability for it. Model weights and other dependencies are fetched in a
separate trusted acquisition step and mounted read-only; parsers run offline.
"""
from __future__ import annotations

import enum
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Sequence

from .sandbox import BubblewrapSandbox, SandboxConfig


class Capability(enum.Enum):
    """A guarantee a backend enforces, or a workload it can host."""

    # Enforced guarantees
    FILESYSTEM_ISOLATION = "filesystem_isolation"  # only staged input + output visible/writable
    NETWORK_ISOLATION = "network_isolation"        # no network interfaces except loopback
    SYSCALL_FILTER = "syscall_filter"              # seccomp or equivalent
    RESOURCE_LIMITS = "resource_limits"            # memory/CPU caps beyond a wall-clock timeout
    DETERMINISTIC = "deterministic"                # fixed clock/entropy; same input -> same output

    # Hostable workloads
    GPU = "gpu"                                    # GPU devices passed through
    NATIVE_LIBS = "native_libs"                    # arbitrary native code (C extensions, PyTorch)


# Every parser run, whatever it declares, needs these.
BASELINE_CAPABILITIES = frozenset({
    Capability.FILESYSTEM_ISOLATION,
    Capability.NETWORK_ISOLATION,
})


@dataclass(frozen=True)
class ParserRequirements:
    """What a parser needs from its sandbox.

    There is intentionally no network option: a parser must not be able to
    weaken its own sandbox by declaring a need for it.
    """

    requires_gpu: bool = False
    requires_native_libs: bool = False
    deterministic: bool = False

    def required_capabilities(self) -> frozenset[Capability]:
        needed = set(BASELINE_CAPABILITIES)
        if self.requires_gpu:
            needed.add(Capability.GPU)
        if self.requires_native_libs:
            needed.add(Capability.NATIVE_LIBS)
        if self.deterministic:
            needed.add(Capability.DETERMINISTIC)
        return frozenset(needed)


class UnsupportedBackendError(RuntimeError):
    """No available backend satisfies a parser's requirements."""


class SandboxUnavailableError(UnsupportedBackendError):
    """The sandbox backend needed for this run cannot run on this host."""


BWRAP_MISSING = (
    "Stele parser containment requires Linux bubblewrap (bwrap), which was not "
    "found. On macOS or Windows, run Stele inside a Linux VM or container "
    "(e.g. WSL2, Lima, Docker)."
)


@dataclass(frozen=True)
class ExecutionOutcome:
    """What a backend reports about one execution, before artifact collection."""

    exit_code: int
    stdout: str
    stderr: str
    wall_time_seconds: float
    timed_out: bool = False


class SandboxBackend(ABC):
    """One isolation technology that can execute a staged parser run."""

    #: Short stable identifier, recorded on every run result.
    name: str

    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """Guarantees enforced and workloads supported on this host."""

    @abstractmethod
    def available(self) -> bool:
        """True when this backend can run on the current host."""

    @abstractmethod
    def unavailable_reason(self) -> str:
        """Human-readable explanation used when available() is False."""

    @abstractmethod
    def execute(self, config: SandboxConfig) -> ExecutionOutcome:
        """Run config.command in the sandbox.

        config.input_path, when set, is already a private staged copy. The
        caller owns output-directory checks and artifact collection.
        """


class BubblewrapBackend(SandboxBackend):
    """Linux namespaces via bubblewrap (bwrap)."""

    name = "bubblewrap"

    def __init__(self) -> None:
        self._sandbox = BubblewrapSandbox()

    def capabilities(self) -> frozenset[Capability]:
        # No SYSCALL_FILTER until #6 (seccomp), no RESOURCE_LIMITS beyond the
        # wall-clock timeout, no GPU (devices are not passed into /dev), and
        # not DETERMINISTIC (real clock and entropy are visible).
        return frozenset({
            Capability.FILESYSTEM_ISOLATION,
            Capability.NETWORK_ISOLATION,
            Capability.NATIVE_LIBS,
        })

    def available(self) -> bool:
        # Late import keeps runner.bwrap_available the single patchable probe.
        from . import runner

        return runner.bwrap_available()

    def unavailable_reason(self) -> str:
        return BWRAP_MISSING

    def execute(self, config: SandboxConfig) -> ExecutionOutcome:
        argv = self._sandbox.build_argv(config)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
            )
        except FileNotFoundError as exc:
            if exc.filename not in (None, argv[0]):
                raise
            raise SandboxUnavailableError(BWRAP_MISSING) from exc
        except subprocess.TimeoutExpired as exc:
            return ExecutionOutcome(
                exit_code=-1,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                wall_time_seconds=time.monotonic() - t0,
                timed_out=True,
            )
        return ExecutionOutcome(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            wall_time_seconds=time.monotonic() - t0,
        )


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


def default_backends() -> list[SandboxBackend]:
    """Registered backends in preference order."""
    return [BubblewrapBackend()]


def select_backend(
    requirements: ParserRequirements,
    backends: Sequence[SandboxBackend] | None = None,
) -> SandboxBackend:
    """Return the first available backend whose capabilities cover requirements.

    Raises UnsupportedBackendError naming, for every registered backend, why it
    was not chosen. Never returns None and never degrades requirements.
    """
    candidates = list(default_backends() if backends is None else backends)
    needed = requirements.required_capabilities()
    reasons: list[str] = []
    capable_but_unavailable: list[SandboxBackend] = []

    for backend in candidates:
        missing = needed - backend.capabilities()
        if missing:
            reasons.append(f"{backend.name}: lacks {_names(missing)}")
            continue
        if not backend.available():
            capable_but_unavailable.append(backend)
            reasons.append(f"{backend.name}: {backend.unavailable_reason()}")
            continue
        return backend

    if capable_but_unavailable and len(capable_but_unavailable) == len(candidates):
        # Every backend could satisfy the parser; this host just can't run any.
        raise SandboxUnavailableError(
            " ".join(b.unavailable_reason() for b in capable_but_unavailable)
        )
    detail = "; ".join(reasons) if reasons else "no sandbox backends are registered"
    raise UnsupportedBackendError(
        f"no sandbox backend can run a parser requiring {_names(needed)} — {detail}"
    )


def _names(caps: Iterable[Capability]) -> str:
    return ", ".join(sorted(c.value for c in caps))
