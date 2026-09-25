"""
The packaged ML parsers in the ledger and replay engine (roadmap #12, #14).

record_parser_run() records a successful stele.parsers.run_parser() run in the
ledger, under the parser's name and version, with the merged configuration it
was given; the image digest the run measured completes its identity.

record_parser_run() also records the run conditions Stele applied: the device
(CPU or GPU image) and the memory, CPU, PID and time limits (roadmap #30).

replay_spec() describes the same parser to the replay engine: the same
command, configuration environment and thread caps as run_parser(), judged by
the parser's comparison policy. For each record the engine specializes it to
the recorded conditions, so a GPU run replays on the GPU image and a run with
non-default limits replays under the same limits. A replay can therefore be
EQUIVALENT or DIVERGED, never REPRODUCED, and is UNREPLAYABLE when the image
is not present locally, no longer has the recorded digest, or the recorded
device (a GPU) is not available here; it never falls back to another device.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..archive.records import Source, canonical_json
from ..containment.backend import ParserRequirements, SandboxBackend
from ..containment.sandbox import SandboxConfig
from ..containment.oci import OciBackend
from ..ledger.models import ArtifactRecord, ParserIdentity, RunConditions
from ..ledger.store import LedgerStore
from ..ledger.transaction import record_run
from ..replay.parsers import ParserSpec
from . import CONFIG_ENV, ParserImage, ParserRun, thread_env


def record_parser_run(ledger: LedgerStore, run: ParserRun, *, source: Source) -> ArtifactRecord:
    """Record and seal a successful packaged-parser run.

    source is required: packaged parsers choose their reader by the input's
    file suffix, so a replay needs the original name (Source.from_path(doc)).
    """
    ident = run.identity
    return record_run(
        ledger,
        run.result,
        parser=ParserIdentity(ident.parser, ident.version),
        parser_config=dict(ident.config),
        source=source,
        run_conditions=RunConditions(
            device=ident.device,
            memory=ident.memory,
            cpus=ident.cpus,
            pids_limit=ident.pids_limit,
            timeout_seconds=ident.timeout_seconds,
        ),
    )


def replay_spec(
    parser: ParserImage,
    *,
    engine: str | None = None,
    backend: SandboxBackend | None = None,
    conditions: RunConditions | None = None,
) -> ParserSpec:
    """The ParserSpec that replays records of a packaged parser.

    The recorded parser_config is the merged configuration the run received,
    so it is passed on unchanged. conditions are the recorded device and
    limits; without them (records made before they were recorded) the parser's
    defaults on the CPU image are used. `backend` replaces the OciBackend
    (tests).
    """
    device = conditions.device if conditions is not None else "cpu"
    if device == "gpu" and parser.gpu == "never":
        raise ValueError(f"{parser.name} has no GPU image, but the record ran on a GPU")

    def pick(name: str, default: Any) -> Any:
        value = getattr(conditions, name, None) if conditions is not None else None
        return default if value is None else value

    memory = pick("memory", parser.memory)
    cpus = pick("cpus", parser.cpus)
    pids_limit = pick("pids_limit", parser.pids_limit)
    timeout = pick("timeout_seconds", parser.timeout_seconds)

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
        return OciBackend(
            image=parser.image_for(device),
            engine=engine,
            gpu=device == "gpu",
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            tmpfs_size=parser.tmpfs_size,
        )

    return ParserSpec(
        name=parser.name,
        version=parser.version,
        requirements=ParserRequirements(
            requires_gpu=device == "gpu", requires_native_libs=True, resource_limits=True,
        ),
        build_config=build,
        policy=parser.comparison,
        backend=make_backend,
        with_conditions=lambda recorded: replay_spec(
            parser, engine=engine, backend=backend, conditions=recorded
        ),
    )
