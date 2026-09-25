"""
A parser's stdout and stderr are captured within a fixed size.

The captured output lives in the Stele process, outside every limit the
sandbox puts on the parser, so an unbounded capture lets any parser exhaust
the host's memory by printing. Every process backend keeps at most
output_limit_bytes of each stream, drains the rest, and says it truncated.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stele.containment.capture import (
    DEFAULT_OUTPUT_LIMIT_BYTES,
    BoundedCapture,
    run_bounded,
)
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig

PYTHON = str(Path(sys.executable).resolve())

# Writes `mib` MiB to stdout and to stderr, in 64 KiB writes.
FLOOD = (
    "import sys\n"
    "block = b'x' * 65536\n"
    "for _ in range({mib} * 16):\n"
    "    sys.stdout.buffer.write(block)\n"
    "    sys.stderr.buffer.write(block)\n"
    "sys.stdout.flush()\n"
    "sys.stderr.flush()\n"
)


def test_capture_keeps_the_first_bytes_and_marks_truncation() -> None:
    capture = BoundedCapture(5)
    assert capture(b"abc") == 3
    assert capture(b"defgh") == 5  # every byte is accepted, only 5 are kept
    assert capture.size == 5 and capture.truncated
    assert capture.text("stdout") == "abcde\n[stele: stdout truncated at 5 bytes]\n"


def test_capture_under_the_limit_is_verbatim() -> None:
    capture = BoundedCapture(10)
    capture(b"hello")
    assert not capture.truncated and capture.text("stderr") == "hello"


def test_run_bounded_discards_output_beyond_the_limit() -> None:
    limit = 64 * 1024
    run = run_bounded([PYTHON, "-c", FLOOD.format(mib=32)], timeout=120, limit=limit)
    assert run.returncode == 0 and not run.timed_out
    assert run.stdout_truncated and run.stderr_truncated
    for text, stream in ((run.stdout, "stdout"), (run.stderr, "stderr")):
        assert text.startswith("x" * limit)
        assert text.endswith(f"[stele: {stream} truncated at {limit} bytes]\n")
        assert len(text) < limit + 100


def test_run_bounded_keeps_output_read_before_a_timeout() -> None:
    script = "import sys, time\nprint('started', flush=True)\ntime.sleep(60)\n"
    run = run_bounded([PYTHON, "-c", script], timeout=2, limit=1024)
    assert run.timed_out
    assert run.stdout == "started\n"


def test_run_bounded_gives_no_stdin_and_universal_newlines() -> None:
    script = "import sys\nsys.stdout.buffer.write(repr(sys.stdin.read()).encode() + b'\\r\\n')\n"
    run = run_bounded([PYTHON, "-c", script], timeout=30)
    assert run.stdout == "''\n"


def test_run_bounded_reports_a_missing_program() -> None:
    with pytest.raises(FileNotFoundError):
        run_bounded(["/nonexistent/stele-no-such-program"], timeout=5)


def test_default_limit_is_shared_by_every_backend() -> None:
    from stele.containment.backend import BubblewrapBackend
    from stele.containment.oci import OciBackend
    from stele.containment.wasm import WasmtimeBackend

    assert BubblewrapBackend().output_limit_bytes == DEFAULT_OUTPUT_LIMIT_BYTES
    assert OciBackend().output_limit_bytes == DEFAULT_OUTPUT_LIMIT_BYTES
    assert WasmtimeBackend().output_limit_bytes == DEFAULT_OUTPUT_LIMIT_BYTES


def test_a_flooding_parser_is_truncated_in_every_backend(
    tmp_path: Path, containment_backend, sandbox_python: str, monkeypatch
) -> None:
    limit = 64 * 1024
    monkeypatch.setattr(containment_backend, "output_limit_bytes", limit)
    script = FLOOD.format(mib=64) + (
        "import os\n"
        "open(os.path.join(os.environ['STELE_OUTPUT_DIR'], 'done'), 'w').write('ok')\n"
    )
    result = run_in_sandbox(SandboxConfig(
        command=[sandbox_python, "-c", script], artifact_dir=tmp_path / "out", timeout_seconds=300,
    ))
    assert result.succeeded, result.stderr[-500:]
    assert (tmp_path / "out" / "done").read_text() == "ok"  # the parser was never blocked
    assert len(result.stdout) < limit + 100 and "stdout truncated" in result.stdout
    assert len(result.stderr) < limit + 100 and "stderr truncated" in result.stderr
