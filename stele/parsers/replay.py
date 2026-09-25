"""
The packaged ML parsers in the ledger and replay engine (roadmap #12, #14).

record_parser_run() records a successful stele.parsers.run_parser() run in the
ledger, under the parser's name and version, with the merged configuration it
was given; the image digest the run measured completes its identity.

replay_spec() describes the same parser to the replay engine: the same
command, configuration environment and thread caps as run_parser(), judged by
the parser's comparison policy. A record made by record_parser_run() also
carries the run's settings (device, memory and CPU limits, timeout); replay
uses them, so a GPU run replays on the GPU image and a run given a longer
timeout gets it again. Records without settings replay on the CPU image with
the parser's defaults. A replay can therefore be EQUIVALENT or DIVERGED, never
REPRODUCED, and is UNREPLAYABLE when the image is not present locally, no
longer has the recorded digest, or needs a GPU this host cannot provide.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Mapping

from ..archive.records import Source, canonical_json
from ..containment.backend import ParserRequirements, SandboxBackend
from ..containment.sandbox import SandboxConfig
from ..ledger.models import ArtifactRecord, ParserIdentity
from ..ledger.store import LedgerStore
from ..ledger.transaction import record_run
from ..replay.parsers import ParserSpec
from . import CONFIG_ENV, ParserImage, ParserRun, _backend, thread_env


def record_parser_run(ledger: LedgerStore, run: ParserRun, *, source: Source) -> ArtifactRecord:
    """Record and seal a successful packaged-parser run.

    source is required: packaged parsers choose their reader by the input's
    file suffix, so a replay needs the original name (Source.from_path(doc)).
    """
    identity = run.identity
    return record_run(
        ledger,
        run.result,
        parser=ParserIdentity(identity.parser, identity.version),
        parser_config=dict(identity.config),
        source=source,
        run_settings={
            "device": identity.device,
            "memory": identity.memory,
            "cpus": identity.cpus,
            "timeout_seconds": identity.timeout_seconds,
        },
    )


def replay_spec(
    parser: ParserImage,
    *,
    engine: str | None = None,
    backend: SandboxBackend | None = None,
    run_settings: Mapping[str, Any] | None = None,
) -> ParserSpec:
    """The ParserSpec that replays records of a packaged parser.

    The recorded parser_config is the merged configuration the run received,
    so it is passed on unchanged. run_settings (from a record, see
    record_parser_run) select the device and limits; without them the CPU
    image and the parser's defaults are used. The replay engine binds each
    record's settings through the spec's with_run_settings. `backend`
    replaces the OciBackend (tests).
    """
    device, memory, cpus, timeout = _settings(parser, run_settings or {})

    def build(input_path: Path | None, artifact_dir: Path, config: Mapping[str, Any]) -> SandboxConfig:
        if input_path is None:
            raise ValueError(f"{parser.name} needs an input document")
        if parser.formats and input_path.suffix.lower() not in parser.formats:
            raise ValueError(
                f"{parser.name} does not accept {input_path.name!r}; the record's "
                "Source must carry the document's original file name"
            )
        env = {
            **thread_env(cpus),
            CONFIG_ENV: canonical_json(dict(config)).decode("utf-8"),
            "STELE_PARSER_DEVICE": device,
        }
        return SandboxConfig(
            command=list(parser.command),
            artifact_dir=artifact_dir,
            input_path=input_path,
            env=env,
            timeout_seconds=timeout,
        )

    def make_backend(identity: ParserIdentity | None) -> SandboxBackend:
        if backend is not None:
            return backend
        return _backend(parser, device, engine=engine, memory=memory, cpus=cpus)

    return ParserSpec(
        name=parser.name,
        version=parser.version,
        requirements=ParserRequirements(
            requires_gpu=device == "gpu", requires_native_libs=True, resource_limits=True,
        ),
        build_config=build,
        policy=parser.comparison,
        backend=make_backend,
        with_run_settings=lambda settings: replay_spec(
            parser, engine=engine, backend=backend, run_settings=settings
        ),
    )


def _settings(
    parser: ParserImage, settings: Mapping[str, Any]
) -> tuple[Literal["cpu", "gpu"], str, float, int]:
    """(device, memory, cpus, timeout) from recorded run settings, else defaults."""
    device = settings.get("device", "cpu")
    if device not in ("cpu", "gpu"):
        raise ValueError(f"unknown device {device!r}")
    if device == "gpu" and parser.gpu == "never":
        raise ValueError(f"{parser.name} was recorded on a GPU but has no GPU build")
    if device == "cpu" and parser.gpu == "required":
        raise ValueError(f"{parser.name} requires a GPU but was recorded on the CPU")
    memory = settings.get("memory", parser.memory)
    cpus = settings.get("cpus", parser.cpus)
    timeout = settings.get("timeout_seconds", parser.timeout_seconds)
    if not isinstance(memory, str):
        raise ValueError(f"memory limit must be a string, got {memory!r}")
    if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or cpus <= 0:
        raise ValueError(f"CPU limit must be a positive number, got {cpus!r}")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(f"timeout must be a positive integer, got {timeout!r}")
    return device, memory, float(cpus), timeout
