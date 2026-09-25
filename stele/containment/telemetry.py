"""
Normalized run telemetry and structured failure reasons (roadmap #11).

Every backend reports the same RunTelemetry for every run, and every run that
does not succeed carries exactly one RunFailure naming why, whatever the
backend-specific signal was (an exit status, a signal, a container engine
error, a Wasm trap, a fuel or epoch limit, a seccomp kill, or unsafe output).

A measurement a backend cannot make is None, never zero: the OCI backend, for
example, runs containers with --rm, so the engine's CPU and memory counters
are gone when the run ends.
"""
from __future__ import annotations

import enum
import signal as _signal
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


class FailureReason(str, enum.Enum):
    """Why a run did not succeed, independent of the backend that ran it."""

    TIMEOUT = "timeout"                  # wall-clock limit (process killed / epoch interrupt)
    OUT_OF_MEMORY = "out_of_memory"      # memory limit (cgroup/container OOM kill, Wasm memory cap)
    CPU_LIMIT = "cpu_limit"              # CPU budget exhausted (Wasm fuel)
    SYSCALL_BLOCKED = "syscall_blocked"  # killed by the seccomp filter (#6)
    WASM_TRAP = "wasm_trap"              # the module trapped (unreachable, bounds, ...)
    CRASHED = "crashed"                  # the parser died from a signal (SIGSEGV, SIGABRT, ...)
    EXIT_STATUS = "exit_status"          # the parser exited non-zero on its own
    ENGINE_ERROR = "engine_error"        # the sandbox could not start the parser (container
                                         # engine errors 125-127, Wasm load/link failure)
    UNSAFE_ARTIFACT = "unsafe_artifact"  # output held a symlink, FIFO, device or unreadable dir
    OUTPUT_LIMIT = "output_limit"        # output exceeded max_output_bytes / max_output_files


@dataclass(frozen=True)
class RunFailure:
    reason: FailureReason
    detail: str                    # one line, for people
    exit_code: int | None = None
    signal: int | None = None      # the terminating signal, when there was one

    @property
    def signal_name(self) -> str | None:
        if self.signal is None:
            return None
        try:
            return _signal.Signals(self.signal).name
        except ValueError:
            return f"signal {self.signal}"


@dataclass(frozen=True)
class RunTelemetry:
    backend: str                         # e.g. "bubblewrap", "oci-runc", "wasmtime"
    runtime: str | None                  # what executed it, e.g. "bwrap", "docker/runsc", "wasmtime 49.0.0"
    wall_time_seconds: float
    exit_code: int
    cpu_time_seconds: float | None = None   # user + system CPU; None when unmeasurable
    peak_memory_bytes: int | None = None    # peak resident / linear memory; None when unmeasurable
    limits: Mapping[str, Any] = field(default_factory=dict)    # the limits actually applied
    counters: Mapping[str, Any] = field(default_factory=dict)  # backend-specific, e.g. fuel_consumed

    def __post_init__(self) -> None:
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))
        object.__setattr__(self, "counters", MappingProxyType(dict(self.counters)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "runtime": self.runtime,
            "wall_time_seconds": self.wall_time_seconds,
            "exit_code": self.exit_code,
            "cpu_time_seconds": self.cpu_time_seconds,
            "peak_memory_bytes": self.peak_memory_bytes,
            "limits": dict(self.limits),
            "counters": dict(self.counters),
        }


def signal_failure(sig: int, exit_code: int, *, context: str = "") -> RunFailure:
    """A death by signal, with SIGSYS left to the caller (it means seccomp)."""
    try:
        name = _signal.Signals(sig).name
    except ValueError:
        name = f"signal {sig}"
    return RunFailure(
        FailureReason.CRASHED,
        f"the parser was killed by {name}{context}",
        exit_code=exit_code,
        signal=sig,
    )


def exit_status_failure(exit_code: int) -> RunFailure:
    return RunFailure(
        FailureReason.EXIT_STATUS, f"the parser exited with status {exit_code}",
        exit_code=exit_code,
    )


def timeout_failure(timeout_seconds: float, exit_code: int, how: str) -> RunFailure:
    return RunFailure(
        FailureReason.TIMEOUT,
        f"wall-clock limit of {timeout_seconds}s exceeded; {how}",
        exit_code=exit_code,
    )
