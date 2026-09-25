"""
Bounded capture of a sandboxed parser's stdout and stderr.

A parser is untrusted, and whatever it prints is buffered in the Stele
process, outside every limit the sandbox applies to the parser itself. So
each stream is kept up to a fixed size and the rest is read and discarded:
the parser never blocks on a full pipe, and Stele's memory never grows with
its output. A truncated stream ends with a note saying so.

The Wasm backend captures guest output in-process with BoundedCapture;
process backends (bubblewrap, OCI) run their command through run_bounded().
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from typing import IO, Sequence

# Per stream. Parsers report progress and errors on these streams; their
# output belongs in the artifact directory.
DEFAULT_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024

_READ_SIZE = 1 << 16
# After the process is gone, how long to wait for its pipes to reach EOF.
# Only a process that escaped the sandbox's kill could hold them open longer.
_DRAIN_GRACE_SECONDS = 10


class BoundedCapture:
    """Keeps the first `limit` bytes written to it and counts the rest."""

    def __init__(self, limit: int) -> None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        self.limit = limit
        self.chunks: list[bytes] = []
        self.size = 0
        self.truncated = False
        self._lock = threading.Lock()

    def __call__(self, data: bytes) -> int:
        with self._lock:
            room = self.limit - self.size
            if room > 0:
                kept = data[:room]
                self.chunks.append(kept)
                self.size += len(kept)
            if len(data) > max(room, 0):
                self.truncated = True
        return len(data)

    def text(self, stream: str) -> str:
        with self._lock:
            out = b"".join(self.chunks).decode("utf-8", errors="replace")
            truncated = self.truncated
        if truncated:
            out += f"\n[stele: {stream} truncated at {self.limit} bytes]\n"
        return out


@dataclass(frozen=True)
class BoundedRun:
    """What run_bounded() observed."""

    returncode: int          # meaningless when timed_out
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    limit: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    cwd: str | None = None,
    pass_fds: Sequence[int] = (),
) -> BoundedRun:
    """Run argv with no stdin, keeping at most `limit` bytes of each output stream.

    Like subprocess.run(..., capture_output=True, timeout=...), except that
    output beyond the limit is discarded as it arrives. On timeout the
    process is killed and timed_out is set; the output read so far is kept.
    Raises what Popen raises (e.g. FileNotFoundError for a missing program).
    """
    kwargs = {"pass_fds": tuple(pass_fds)} if pass_fds else {}
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        **kwargs,
    )
    out, err = BoundedCapture(limit), BoundedCapture(limit)
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        for reader in readers:
            reader.join(_DRAIN_GRACE_SECONDS)

    return BoundedRun(
        returncode=proc.returncode,
        stdout=_newlines(out.text("stdout")),
        stderr=_newlines(err.text("stderr")),
        timed_out=timed_out,
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
    )


def _drain(stream: IO[bytes], capture: BoundedCapture) -> None:
    try:
        while chunk := stream.read1(_READ_SIZE):
            capture(chunk)
    except (OSError, ValueError):
        pass  # the pipe was closed under us
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _newlines(text: str) -> str:
    # What text=True gave callers before: universal newlines.
    return text.replace("\r\n", "\n").replace("\r", "\n")
