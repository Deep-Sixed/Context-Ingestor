"""
Lanes for the parsers Stele knows how to run, by backend name.

    bubblewrap     Linux namespaces + seccomp + Landlock (today's production baseline)
    oci-runc       Podman/Docker container, runc
    oci-runsc      Podman/Docker container, gVisor
    oci-runc-gpu   Podman/Docker container with NVIDIA GPUs
    wasmtime       WebAssembly/WASI (Wasm extractors only)

A script parser (a Python file run as /stele/parser) runs on bubblewrap with
the host interpreter and in a container with the image's python3. A packaged
parser (MinerU, Marker, Docling) runs its pinned image on the OCI lanes. A
Wasm extractor runs on wasmtime.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Mapping

from ..containment.backend import BubblewrapBackend, ParserRequirements, SandboxBackend
from ..containment.oci import DEFAULT_OCI_IMAGE, OciBackend
from ..containment.sandbox import SandboxConfig
from ..replay.parsers import ParserSpec
from ..replay.policy import ComparisonPolicy
from .campaign import Lane

BACKEND_NAMES = ("bubblewrap", "oci-runc", "oci-runsc", "oci-runc-gpu", "wasmtime")


def _oci(name: str, *, image: str, engine: str | None, **limits: Any) -> OciBackend:
    runtime = "runsc" if name == "oci-runsc" else "runc"
    return OciBackend(image=image, engine=engine, runtime=runtime,
                      gpu=name.endswith("-gpu"), **limits)


def backend_named(name: str, *, image: str = DEFAULT_OCI_IMAGE, engine: str | None = None,
                  **limits: Any) -> SandboxBackend:
    if name == "bubblewrap":
        return BubblewrapBackend()
    if name.startswith("oci-"):
        if name not in BACKEND_NAMES:
            raise ValueError(f"unknown backend {name!r}; expected one of {BACKEND_NAMES}")
        return _oci(name, image=image, engine=engine, **limits)
    if name == "wasmtime":
        from ..containment.wasm import WasmtimeBackend

        return WasmtimeBackend()
    raise ValueError(f"unknown backend {name!r}; expected one of {BACKEND_NAMES}")


def script_lanes(
    script: Path,
    backends: list[str],
    *,
    name: str | None = None,
    version: str | None = None,
    deterministic: bool = False,
    timeout_seconds: int = 300,
    image: str = DEFAULT_OCI_IMAGE,
    engine: str | None = None,
) -> list[Lane]:
    """Lanes for a Python script parser. Its version defaults to the script's digest."""
    script = Path(script).resolve()
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    name = name or script.stem
    version = version or f"sha256-{digest[:16]}"
    host_python = str(Path(sys.executable).resolve())
    lanes = []
    for backend_name in backends:
        if backend_name == "wasmtime":
            raise ValueError("a script parser cannot run on wasmtime")
        interpreter = host_python if backend_name == "bubblewrap" else "python3"

        def build(input_path: Path | None, artifact_dir: Path, config: Mapping[str, Any],
                  interpreter: str = interpreter) -> SandboxConfig:
            return SandboxConfig(
                command=[interpreter, "/stele/parser"], artifact_dir=artifact_dir,
                input_path=input_path, script_path=script, timeout_seconds=timeout_seconds,
            )

        spec = ParserSpec(
            name=name, version=version,
            requirements=ParserRequirements(deterministic=deterministic),
            build_config=build,
        )
        lanes.append(Lane(backend_name, backend_named(backend_name, image=image, engine=engine), spec))
    return lanes


def packaged_lanes(
    parser_name: str, backends: list[str], *, engine: str | None = None,
) -> tuple[list[Lane], dict[str, Any], ComparisonPolicy | None]:
    """Lanes, merged default config and comparison policy of a packaged ML parser."""
    from ..parsers.catalog import get_parser
    from ..parsers.replay import replay_spec

    parser = get_parser(parser_name)
    lanes = []
    for backend_name in backends:
        if not backend_name.startswith("oci-"):
            raise ValueError(f"{parser_name} runs only in its container image (oci-* lanes)")
        device = "gpu" if backend_name.endswith("-gpu") else "cpu"
        backend = backend_named(
            backend_name, image=parser.image_for(device), engine=engine,
            memory=parser.memory, cpus=parser.cpus, pids_limit=parser.pids_limit,
            tmpfs_size=parser.tmpfs_size,
        )
        lanes.append(Lane(backend_name, backend, replay_spec(parser, backend=backend)))
    return lanes, dict(parser.config), parser.comparison


def extractor_lanes(name: str, backends: list[str]) -> list[Lane]:
    """Lanes of a Wasm extractor (e.g. chatgpt-export-split)."""
    from ..extractors import EXTRACTOR_SPECS

    specs = {spec.name: spec for spec in EXTRACTOR_SPECS}
    if name not in specs:
        raise ValueError(f"unknown extractor {name!r}")
    if any(b != "wasmtime" for b in backends):
        raise ValueError(f"{name} is a Wasm module; its only lane is wasmtime")
    return [Lane(b, backend_named(b), specs[name]) for b in backends]
