"""
Sandbox backends and capability matching (roadmap #5).

A backend is one isolation technology (bubblewrap and Wasmtime today; OCI
containers later). Each backend declares the guarantees it enforces and the
workloads it can host. Each parser declares its requirements. Stele runs a
parser only on a backend that satisfies every requirement and never falls back
to unsandboxed execution.

Every parser is exactly one kind of workload: a host process (config.command
is an executable run in the sandbox, the default) or a WebAssembly module
(config.command[0] is a .wasm/.wat file, ParserRequirements.wasm_module=True).
The workload kind is a required capability like any other, so a Python-script
parser can never be routed to the Wasm backend and a Wasm module can never be
routed to bubblewrap, whatever the registry order.

Parsers cannot request network access: there is deliberately no requirement or
capability for it. Model weights and other dependencies are fetched in a
separate trusted acquisition step and mounted read-only; parsers run offline.
"""
from __future__ import annotations

import enum
import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import landlock, seccomp
from .sandbox import BubblewrapSandbox, LandlockLaunch, SandboxConfig


class Capability(enum.Enum):
    """A guarantee a backend enforces, or a workload it can host."""

    # Enforced guarantees
    FILESYSTEM_ISOLATION = "filesystem_isolation"  # only staged input + output visible/writable
    NETWORK_ISOLATION = "network_isolation"        # no network interfaces except loopback
    SYSCALL_FILTER = "syscall_filter"              # seccomp or equivalent
    RESOURCE_LIMITS = "resource_limits"            # memory/CPU caps beyond a wall-clock timeout
    DETERMINISTIC = "deterministic"                # fixed clock/entropy; same input -> same output

    # Hostable workloads
    HOST_PROCESS = "host_process"                  # runs a host executable as config.command
    WASM_MODULE = "wasm_module"                    # runs a WebAssembly/WASI module
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
    # The parser is a WebAssembly module rather than a host executable.
    wasm_module: bool = False

    def required_capabilities(self) -> frozenset[Capability]:
        needed = set(BASELINE_CAPABILITIES)
        needed.add(Capability.WASM_MODULE if self.wasm_module else Capability.HOST_PROCESS)
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
    # SHA-256 of the executed WebAssembly module binary, part of the parser's
    # identity. None for host-process backends.
    module_sha256: str | None = None
    # Kernel hardening layers applied to this run, e.g. ("seccomp", "landlock").
    hardening: tuple[str, ...] = ()
    # Set when the parser was stopped for breaking sandbox policy.
    violation: str | None = None


SECCOMP_VIOLATION = (
    "seccomp: the parser made a blocked system call and was killed (SIGSYS)"
)


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
        # SYSCALL_FILTER only where execute() will really install the seccomp
        # filter. No RESOURCE_LIMITS beyond the wall-clock timeout, no GPU
        # (devices are not passed into /dev), and not DETERMINISTIC (real
        # clock and entropy are visible).
        caps = {
            Capability.FILESYSTEM_ISOLATION,
            Capability.NETWORK_ISOLATION,
            Capability.HOST_PROCESS,
            Capability.NATIVE_LIBS,
        }
        if self.seccomp_program() is not None:
            caps.add(Capability.SYSCALL_FILTER)
        return frozenset(caps)

    def seccomp_program(self) -> bytes | None:
        """The BPF filter execute() installs, or None on unsupported hosts."""
        return seccomp.filter_for_host()

    def landlock_launch(self) -> LandlockLaunch | None:
        """How execute() applies Landlock, or None when it cannot.

        Landlock is defense in depth under the mount namespace, so a kernel
        without it still runs parsers; the run result's ``hardening`` says
        whether it was applied.
        """
        if landlock.abi_version() < 1:
            return None
        python = _sandbox_python()
        if python is None:
            return None
        return LandlockLaunch(python=python, script=landlock.__file__)

    def available(self) -> bool:
        # Late import keeps runner.bwrap_available the single patchable probe.
        from . import runner

        return runner.bwrap_available()

    def unavailable_reason(self) -> str:
        return BWRAP_MISSING

    def execute(self, config: SandboxConfig) -> ExecutionOutcome:
        program = self.seccomp_program()
        launch = self.landlock_launch()
        hardening = tuple(
            name for name, on in (("seccomp", program), ("landlock", launch)) if on
        )
        filter_fd = seccomp.filter_fd(program) if program is not None else None
        try:
            return self._run(config, filter_fd, launch, hardening)
        finally:
            if filter_fd is not None:
                os.close(filter_fd)

    def _run(
        self,
        config: SandboxConfig,
        filter_fd: int | None,
        launch: LandlockLaunch | None,
        hardening: tuple[str, ...],
    ) -> ExecutionOutcome:
        argv = self._sandbox.build_argv(config, seccomp_fd=filter_fd, landlock=launch)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                pass_fds=() if filter_fd is None else (filter_fd,),
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
                hardening=hardening,
            )
        # bwrap's init reports a signal death as 128 + signo. A parser could
        # exit with that status on purpose, but that only marks its own run
        # as failed.
        violation = None
        if filter_fd is not None and proc.returncode == 128 + seccomp.SIGSYS:
            violation = SECCOMP_VIOLATION
        return ExecutionOutcome(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            wall_time_seconds=time.monotonic() - t0,
            hardening=hardening,
            violation=violation,
        )


def _sandbox_python() -> str | None:
    """An interpreter that exists inside the sandbox, for the launcher.

    Only /usr is bound from the host, so prefer the running interpreter when
    it lives there (a known-good version), else the system python3.
    """
    for candidate in (os.path.realpath(sys.executable), "/usr/bin/python3"):
        if candidate.startswith("/usr/") and os.path.isfile(candidate):
            return candidate
    return None


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


def default_backends() -> list[SandboxBackend]:
    """Registered backends in preference order.

    The two backends host disjoint workloads (host processes vs. Wasm modules),
    so their relative order never changes which one a parser gets: selection
    is decided by the workload capability, and order only breaks ties between
    backends hosting the same kind of workload. Bubblewrap stays first so the
    default (host-process) parser keeps its existing backend.
    """
    from .wasm import WasmtimeBackend

    return [BubblewrapBackend(), WasmtimeBackend()]


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

    if capable_but_unavailable:
        # Some backend could satisfy the parser; this host just can't run it.
        raise SandboxUnavailableError(
            " ".join(b.unavailable_reason() for b in capable_but_unavailable)
        )
    detail = "; ".join(reasons) if reasons else "no sandbox backends are registered"
    raise UnsupportedBackendError(
        f"no sandbox backend can run a parser requiring {_names(needed)} — {detail}"
    )


def _names(caps: Iterable[Capability]) -> str:
    return ", ".join(sorted(c.value for c in caps))
