"""
python -m stele.parsers list
python -m stele.parsers build-command NAME [--engine podman] [--device gpu]
python -m stele.parsers run NAME --input DOC --artifact-dir DIR [options]

`run` prints a JSON summary (identity, outcome, artifacts) and exits 0 on
success, 1 when the parser failed, 2 when the run could not start.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from ..containment.backend import UnsupportedBackendError
from ..ledger.models import is_memory_limit
from . import UnsupportedInputError, run_parser
from .catalog import PARSERS, get_parser


def _memory_limit(value: str) -> str:
    if not is_memory_limit(value):
        raise argparse.ArgumentTypeError(f"not a memory limit: {value!r} (e.g. 4g, 1.5g, 512m)")
    return value


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m stele.parsers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List packaged parsers.")

    build = sub.add_parser("build-command", help="Print the trusted image build command.")
    build.add_argument("name")
    build.add_argument("--engine", default="docker", choices=["docker", "podman"])
    build.add_argument("--device", default="cpu", choices=["cpu", "gpu"])

    run = sub.add_parser("run", help="Parse one document in the parser's image.")
    run.add_argument("name")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--artifact-dir", required=True, type=Path)
    run.add_argument("--config", default=None, metavar="JSON",
                     help="JSON object overriding the parser's default configuration.")
    run.add_argument("--device", default="auto", choices=["auto", "cpu", "gpu"])
    run.add_argument("--engine", default=None, choices=["docker", "podman"])
    run.add_argument("--memory", default=None, type=_memory_limit, help="Memory limit, e.g. 4g.")
    run.add_argument("--cpus", default=None, type=float)
    run.add_argument("--timeout", default=None, type=int, metavar="SECONDS")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        for p in PARSERS.values():
            print(f"{p.name}\t{p.version}\t{p.image}\tgpu={p.gpu}\t{' '.join(p.formats)}")
        return 0

    try:
        parser = get_parser(args.name)
    except KeyError as exc:
        ap.error(str(exc.args[0]))

    if args.cmd == "build-command":
        if args.device == "gpu" and not parser.gpu_image:
            ap.error(f"{parser.name} has no GPU image")
        print(shlex.join(parser.build_command(args.engine, args.device)))
        return 0

    config = None
    if args.config is not None:
        try:
            config = json.loads(args.config)
        except json.JSONDecodeError as exc:
            ap.error(f"--config is not valid JSON: {exc}")
        if not isinstance(config, dict):
            ap.error("--config must be a JSON object")

    try:
        outcome = run_parser(
            parser, args.input, args.artifact_dir,
            config=config, device=args.device, engine=args.engine,
            memory=args.memory, cpus=args.cpus, timeout_seconds=args.timeout,
        )
    except (UnsupportedInputError, UnsupportedBackendError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = outcome.result
    print(json.dumps({
        "succeeded": outcome.succeeded,
        "failure": outcome.failure,
        "identity": outcome.identity.to_dict(),
        "run_id": str(result.run_id),
        "backend": result.backend,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "wall_time_seconds": round(result.wall_time_seconds, 3),
        "input_sha256": result.input_sha256,
        "artifact_paths": [str(p) for p in result.artifact_paths],
        "stderr_tail": result.stderr[-2000:],
    }, indent=2))
    return 0 if outcome.succeeded else 1


if __name__ == "__main__":
    sys.exit(_main())
