"""
Sandbox backends and capability matching (roadmap #5).

A backend is one isolation technology (bubblewrap, Wasmtime and OCI
containers today). Each backend declares the guarantees it enforces and the
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
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import landlock, seccomp
from .capture import DEFAULT_OUTPUT_LIMIT_BYTES, BoundedCapture, run_bounded
from .sandbox import BubblewrapSandbox, LandlockLaunch, SandboxConfig
from .telemetry import (
    FailureReason,
    RunFailure,
    exit_status_failure,
    signal_failure,
    timeout_failure,
)


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
    # Memory/CPU/PID caps must actually be enforced (not merely requested), so
    # a large or hostile document cannot exhaust the host. Heavy ML parsers
    # (roadmap #9, #10) set this.
    resource_limits: bool = False

    def required_capabilities(self) -> frozenset[Capability]:
        needed = set(BASELINE_CAPABILITIES)
        needed.add(Capability.WASM_MODULE if self.wasm_module else Capability.HOST_PROCESS)
        if self.requires_gpu:
            needed.add(Capability.GPU)
        if self.requires_native_libs:
            needed.add(Capability.NATIVE_LIBS)
        if self.deterministic:
            needed.add(Capability.DETERMINISTIC)
        if self.resource_limits:
            needed.add(Capability.RESOURCE_LIMITS)
        return frozenset(needed)


class UnsupportedBackendError(RuntimeError):
    """No available backend satisfies a parser's requirements."""


class SandboxUnavailableError(UnsupportedBackendError):
    """The sandbox backend needed for this run cannot run on this host."""


class ContainmentCleanupError(RuntimeError):
    """A stopped run's sandbox could not be proven gone.

    The parser may still be running, with its output directory mounted
    writable. Nothing in that directory may be treated as the run's output:
    the run is not reported as finished, so nothing is collected or sealed.
    """

    def __init__(self, message: str, *, artifact_dir: Path | None = None) -> None:
        super().__init__(message)
        self.artifact_dir = artifact_dir


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
    # Content digest of the container image that ran, for backends that run
    # images (part of the parser's identity, roadmap #12); None otherwise.
    image_digest: str | None = None
    # SHA-256 of the executed WebAssembly module binary, part of the parser's
    # identity. None for host-process backends.
    module_sha256: str | None = None
    # Kernel hardening layers applied to this run, e.g. ("seccomp", "landlock").
    hardening: tuple[str, ...] = ()
    # Set when the parser was stopped for breaking sandbox policy.
    violation: str | None = None
    # Telemetry (roadmap #11). None means the backend cannot measure it.
    cpu_time_seconds: float | None = None
    peak_memory_bytes: int | None = None
    runtime: str | None = None
    limits: Mapping[str, Any] = field(default_factory=dict)
    counters: Mapping[str, Any] = field(default_factory=dict)
    # The backend's own account of why the run failed; None lets the runner
    # derive a generic reason from exit_code / timed_out / violation.
    failure: RunFailure | None = None


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

    def __init__(self, *, output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES) -> None:
        self._sandbox = BubblewrapSandbox()
        # Kept of each of the parser's stdout and stderr; the rest is discarded.
        self.output_limit_bytes = output_limit_bytes

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
            proc = run_process(
                argv,
                timeout=config.timeout_seconds,
                pass_fds=() if filter_fd is None else (filter_fd,),
                output_limit_bytes=self.output_limit_bytes,
            )
        except FileNotFoundError as exc:
            if exc.filename not in (None, argv[0]):
                raise
            raise SandboxUnavailableError(BWRAP_MISSING) from exc
        wall = time.monotonic() - t0

        violation = None
        failure: RunFailure | None = None
        exit_code = -1 if proc.timed_out else proc.returncode
        if proc.timed_out:
            failure = timeout_failure(
                config.timeout_seconds, exit_code,
                "the sandbox and every process in it were killed",
            )
        # bwrap's init reports a signal death as 128 + signo. A parser could
        # exit with that status on purpose, but that only marks its own run
        # as failed.
        elif filter_fd is not None and exit_code == 128 + seccomp.SIGSYS:
            violation = SECCOMP_VIOLATION
            failure = RunFailure(
                FailureReason.SYSCALL_BLOCKED, SECCOMP_VIOLATION,
                exit_code=exit_code, signal=seccomp.SIGSYS,
            )
        elif 128 < exit_code <= 128 + 64:
            failure = signal_failure(exit_code - 128, exit_code)
        elif exit_code < 0:
            failure = signal_failure(-exit_code, exit_code, context=" (the sandbox itself)")
        elif exit_code != 0:
            failure = exit_status_failure(exit_code)

        return ExecutionOutcome(
            exit_code=exit_code,
            stdout=proc.stdout,
            stderr=proc.stderr,
            wall_time_seconds=wall,
            timed_out=proc.timed_out,
            hardening=hardening,
            violation=violation,
            cpu_time_seconds=proc.cpu_time_seconds,
            peak_memory_bytes=proc.peak_memory_bytes,
            runtime="bwrap",
            limits={
                "timeout_seconds": config.timeout_seconds,
                "output_bytes": self.output_limit_bytes,
            },
            failure=failure,
        )


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    cpu_time_seconds: float | None
    peak_memory_bytes: int | None


def run_process(
    argv: list[str],
    *,
    timeout: float,
    pass_fds: Sequence[int] = (),
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> ProcessResult:
    """Run argv with no stdin, capture its output, and enforce the timeout.

    At most output_limit_bytes of each of stdout and stderr is kept; the rest
    is read and discarded (stele.containment.capture), since the captured
    output lives in this process, outside every limit on the parser.

    The process gets its own process group, and the whole group is killed
    with SIGKILL at the timeout. It is reaped with wait4, whose resource
    usage covers it and every descendant it waited for, which gives the
    run's CPU time and the peak resident memory of its largest process.
    Hosts without wait4 (Windows) fall back to subprocess.run with no usage.
    """
    if not hasattr(os, "wait4"):
        done = run_bounded(argv, timeout=timeout, limit=output_limit_bytes, pass_fds=pass_fds)
        return ProcessResult(
            -1 if done.timed_out else done.returncode,
            done.stdout, done.stderr, done.timed_out, None, None,
        )

    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        pass_fds=tuple(pass_fds), start_new_session=True,
    )
    streams = {
        "stdout": BoundedCapture(output_limit_bytes),
        "stderr": BoundedCapture(output_limit_bytes),
    }
    readers = [
        threading.Thread(target=_drain, args=(pipe, streams[name]), daemon=True)
        for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for reader in readers:
        reader.start()

    lock = threading.Lock()
    state = {"reaped": False, "killed": False}

    def kill_group() -> None:
        with lock:
            # Never signal a pid that was already reaped (it may be reused).
            if state["reaped"]:
                return
            state["killed"] = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    timer = threading.Timer(timeout, kill_group)
    timer.daemon = True
    timer.start()
    try:
        _, status, usage = os.wait4(proc.pid, 0)
    finally:
        with lock:
            state["reaped"] = True
        timer.cancel()
    proc.returncode = os.waitstatus_to_exitcode(status)
    for reader in readers:
        reader.join(timeout=10)
    for pipe in (proc.stdout, proc.stderr):
        pipe.close()

    # ru_maxrss is in KiB on Linux and bytes on macOS.
    scale = 1 if sys.platform == "darwin" else 1024
    return ProcessResult(
        returncode=proc.returncode,
        stdout=streams["stdout"].text("stdout"),
        stderr=streams["stderr"].text("stderr"),
        timed_out=state["killed"],
        cpu_time_seconds=usage.ru_utime + usage.ru_stime,
        peak_memory_bytes=usage.ru_maxrss * scale,
    )


def _drain(pipe: Any, capture: BoundedCapture) -> None:
    # Keeps reading past the limit so the parser never blocks on a full pipe.
    while chunk := pipe.read(65536):
        capture(chunk)


def _sandbox_python() -> str | None:
    """An interpreter that exists inside the sandbox, for the launcher.

    Only /usr is bound from the host, so prefer the running interpreter when
    it lives there (a known-good version), else the system python3.
    """
    for candidate in (os.path.realpath(sys.executable), "/usr/bin/python3"):
        if candidate.startswith("/usr/") and os.path.isfile(candidate):
            return candidate
    return None


def default_backends() -> list[SandboxBackend]:
    """Registered backends in preference order.

    Host-process backends: bubblewrap comes first; on Linux it needs no daemon,
    image or privileged engine, and it covers every parser that does not need a
    GPU or resource limits. The OCI container backend (runc) follows; it is
    chosen when bubblewrap is unavailable (macOS, Windows, hosts without user
    namespaces) or lacks a required capability. The GPU variant comes last of
    those and is only ever chosen for parsers that declare requires_gpu, so no
    other parser is handed GPU devices. gVisor (runsc) is opt-in and never
    registered by default.

    Wasmtime hosts a disjoint workload (Wasm modules, not host processes), so
    its position never changes which backend a parser gets: selection is
    decided by the workload capability, and order only breaks ties between
    backends hosting the same kind of workload.
    """
    from .oci import OciBackend
    from .wasm import WasmtimeBackend

    return [BubblewrapBackend(), OciBackend(), OciBackend(gpu=True), WasmtimeBackend()]


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
