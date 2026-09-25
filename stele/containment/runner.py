"""
Stele Phase E — sandboxed parser runner.

Usage as a library:
    from stele.containment.runner import run_in_sandbox
    from stele.containment.sandbox import SandboxConfig

    result = run_in_sandbox(SandboxConfig(
        command=["/usr/bin/python3", "/stele/parser"],
        artifact_dir=Path("/tmp/stele-run-xyz"),
        script_path=Path("/path/to/my_parser.py"),
    ))

Usage as a CLI:
    python -m stele.containment.runner \\
        --artifact-dir /tmp/stele-out \\
        --script /path/to/parser.py \\
        -- /usr/bin/python3 /stele/parser
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .result import SandboxResult
from .artifacts import collect_artifact_paths
from .sandbox import BubblewrapSandbox, SandboxConfig
from .staging import stage_regular_file


class SandboxUnavailableError(RuntimeError):
    """Raised when the bubblewrap sandbox cannot run on this host."""


def run_in_sandbox(config: SandboxConfig) -> SandboxResult:
    """Execute config.command inside a bubblewrap sandbox and return the result.

    The sandbox exits (and all ephemeral writes are discarded) before this
    function returns. Only files found in config.artifact_dir are captured
    in the returned SandboxResult.

    Raises ValueError if config.artifact_dir already has contents: leftover
    files would otherwise be credited to this run.
    """
    if config.artifact_dir.exists() and any(config.artifact_dir.iterdir()):
        raise ValueError(
            f"artifact_dir {config.artifact_dir} is not empty — "
            "each run needs a fresh output directory"
        )

    run_id = uuid4()
    sandbox = BubblewrapSandbox()
    runtime_config = config
    staging: TemporaryDirectory[str] | None = None

    try:
        # Never bind an untrusted source path directly into the sandbox. Stage a
        # private regular-file copy first; stage_regular_file hashes exactly the
        # descriptor it copies from and refuses symlinks and non-regular files.
        if config.input_path is not None:
            staging = TemporaryDirectory(prefix=f"stele-{run_id}-")
            staged = stage_regular_file(
                config.input_path,
                Path(staging.name) / "input",
            )
            runtime_config = replace(config, input_path=staged.staged_path)

        argv = sandbox.build_argv(runtime_config)

        t0 = time.monotonic()
        timed_out = False

        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=runtime_config.timeout_seconds,
            )
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except FileNotFoundError as exc:
            if exc.filename not in (None, argv[0]):
                raise
            raise SandboxUnavailableError(
                "Stele parser containment requires Linux bubblewrap (bwrap), "
                "which was not found. On macOS or Windows, run Stele inside a "
                "Linux VM or container (e.g. WSL2, Lima, Docker)."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            raw_out = exc.stdout or b""
            raw_err = exc.stderr or b""
            stdout = raw_out.decode("utf-8", errors="replace") if isinstance(raw_out, bytes) else raw_out
            stderr = raw_err.decode("utf-8", errors="replace") if isinstance(raw_err, bytes) else raw_err

        wall_time = time.monotonic() - t0
    finally:
        if staging is not None:
            staging.cleanup()

    artifact_paths = collect_artifact_paths(config.artifact_dir)

    return SandboxResult(
        run_id=run_id,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        artifact_paths=artifact_paths,
        artifact_dir=config.artifact_dir,
        wall_time_seconds=wall_time,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m stele.containment.runner",
        description="Run COMMAND inside the Stele bubblewrap sandbox.",
    )
    ap.add_argument("--artifact-dir", required=True, type=Path, metavar="DIR",
                    help="Host directory bind-mounted as /stele/output (created if absent).")
    ap.add_argument("--input", type=Path, default=None, dest="input_path", metavar="PATH",
                    help="Read-only input file or directory exposed at /stele/input.")
    ap.add_argument("--script", type=Path, default=None, dest="script_path", metavar="PATH",
                    help="Parser script exposed at /stele/parser (read-only).")
    ap.add_argument("--timeout", type=int, default=300, metavar="SECONDS")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="Environment variable to inject (repeatable).")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="Command to run inside sandbox (after --).")

    args = ap.parse_args()

    command: list[str] = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        ap.error("no COMMAND provided — use: runner [options] -- COMMAND [ARGS...]")

    env: dict[str, str] = {}
    for kv in args.env:
        k, _, v = kv.partition("=")
        env[k] = v

    config = SandboxConfig(
        command=command,
        artifact_dir=args.artifact_dir,
        input_path=args.input_path,
        script_path=args.script_path,
        timeout_seconds=args.timeout,
        env=env,
    )

    result = run_in_sandbox(config)

    print(json.dumps({
        "run_id": str(result.run_id),
        "exit_code": result.exit_code,
        "succeeded": result.succeeded,
        "timed_out": result.timed_out,
        "wall_time_seconds": round(result.wall_time_seconds, 3),
        "artifact_paths": [str(p) for p in result.artifact_paths],
        "stdout": result.stdout,
        "stderr": result.stderr,
    }, indent=2))

    sys.exit(result.exit_code)


if __name__ == "__main__":
    _main()
