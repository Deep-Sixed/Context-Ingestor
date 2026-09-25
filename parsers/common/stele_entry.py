"""
Shared entry-point helpers baked into every Stele parser image.

A parser image's entry script reads the staged document from STELE_INPUT_PATH,
its configuration from STELE_PARSER_CONFIG (canonical JSON set by Stele), and
writes everything under STELE_OUTPUT_DIR. It finishes by writing
``stele-parser.json`` there, which names the parser, its version and
configuration, and the role of each output file.

Exit statuses (documented in parsers/README.md):
  0  success
  2  unusable input or configuration
  3  model weights missing from the image (never downloaded at run time)
  4  the parser itself failed
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, NoReturn

EXIT_BAD_INPUT = 2
EXIT_MODELS_MISSING = 3
EXIT_PARSE_FAILED = 4

MANIFEST_NAME = "stele-parser.json"
MANIFEST_SCHEMA = "stele.parser-output/v1"

# Variables that make Hugging Face, transformers and friends refuse to touch
# the network. The sandbox has no network anyway; these turn a would-be
# download into an immediate, explicit error instead of a retry loop.
OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
}


class ParserError(Exception):
    """A failure with a specific exit status and a one-line reason."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def fail(status: int, message: str) -> NoReturn:
    raise ParserError(status, message)


def input_path() -> Path:
    raw = os.environ.get("STELE_INPUT_PATH")
    if not raw:
        fail(EXIT_BAD_INPUT, "STELE_INPUT_PATH is not set")
    path = Path(raw)
    if not path.is_file():
        fail(EXIT_BAD_INPUT, f"input {path} is not a regular file")
    return path


def output_dir() -> Path:
    raw = os.environ.get("STELE_OUTPUT_DIR")
    if not raw:
        fail(EXIT_BAD_INPUT, "STELE_OUTPUT_DIR is not set")
    return Path(raw)


def parser_config(defaults: dict[str, Any], allowed: dict[str, tuple[type, ...]]) -> dict[str, Any]:
    """Defaults overlaid with STELE_PARSER_CONFIG; unknown keys are refused."""
    raw = os.environ.get("STELE_PARSER_CONFIG", "{}")
    try:
        given = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(EXIT_BAD_INPUT, f"STELE_PARSER_CONFIG is not valid JSON: {exc}")
    if not isinstance(given, dict):
        fail(EXIT_BAD_INPUT, "STELE_PARSER_CONFIG must be a JSON object")
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        fail(EXIT_BAD_INPUT, f"unknown configuration keys: {', '.join(unknown)}")
    config = {**defaults, **given}
    for key, types in allowed.items():
        if key in config and not isinstance(config[key], types):
            fail(EXIT_BAD_INPUT, f"configuration key {key!r} has the wrong type")
    return config


def require_models(paths: list[Path]) -> None:
    """Fail with EXIT_MODELS_MISSING unless every path exists and is non-empty."""
    missing = [
        str(p) for p in paths
        if not p.exists() or (p.is_dir() and not any(p.iterdir()))
    ]
    if missing:
        fail(
            EXIT_MODELS_MISSING,
            "model weights missing from the image: " + ", ".join(missing)
            + " (models are baked in at build time; Stele never downloads at run time)",
        )


def thread_count() -> int:
    try:
        return max(1, int(os.environ.get("STELE_TORCH_THREADS", "1")))
    except ValueError:
        return 1


def write_manifest(
    out: Path,
    *,
    parser: str,
    version: str,
    config: dict[str, Any],
    source: Path,
    outputs: dict[str, str],
    extra: dict[str, Any] | None = None,
) -> None:
    """Write stele-parser.json; every path in `outputs` must exist under out."""
    for role, rel in outputs.items():
        if not (out / rel).is_file():
            fail(EXIT_PARSE_FAILED, f"expected {role} output {rel} was not produced")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "parser": parser,
        "version": version,
        "device": os.environ.get("STELE_PARSER_DEVICE", "cpu"),
        "config": config,
        "input": source.name,
        "outputs": outputs,
        **(extra or {}),
    }
    (out / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(run: Callable[[], None]) -> NoReturn:
    """Run an entry function, mapping failures to documented exit statuses."""
    for key, value in OFFLINE_ENV.items():
        os.environ.setdefault(key, value)
    try:
        run()
    except ParserError as exc:
        print(f"stele-parser: {exc}", file=sys.stderr)
        sys.exit(exc.status)
    except MemoryError:
        print("stele-parser: out of memory", file=sys.stderr)
        sys.exit(EXIT_PARSE_FAILED)
    except Exception as exc:  # noqa: BLE001 - report any parser crash as a failure
        traceback.print_exc()
        print(f"stele-parser: parse failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(EXIT_PARSE_FAILED)
    sys.exit(0)
