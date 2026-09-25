"""
The packaged ML parsers in the ledger and replay engine (roadmap #12, #14).

record_parser_run() records a successful stele.parsers.run_parser() run in the
ledger, under the parser's name and version, with the merged configuration it
was given; the image digest the run measured completes its identity.

replay_spec() describes the same parser to the replay engine: the same
command, configuration environment and thread caps as run_parser(), on the
CPU image, judged by the parser's comparison policy. A replay can therefore be
EQUIVALENT or DIVERGED, never REPRODUCED, and is UNREPLAYABLE when the image is
not present locally or no longer has the recorded digest.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..archive.records import Source, canonical_json
from ..containment.backend import ParserRequirements, SandboxBackend
from ..containment.sandbox import SandboxConfig
from ..ledger.models import ArtifactRecord, ParserIdentity
from ..ledger.store import LedgerStore
from ..ledger.transaction import record_run
from ..replay.parsers import ParserSpec
from . import CONFIG_ENV, ParserImage, ParserRun, _backend, thread_env


def record_parser_run(
    ledger: LedgerStore, run: ParserRun, *, source: Source | None = None
) -> ArtifactRecord:
    """Record and seal a successful packaged-parser run."""
    return record_run(
        ledger,
        run.result,
        parser=ParserIdentity(run.identity.parser, run.identity.version),
        parser_config=dict(run.identity.config),
        source=source,
    )


def replay_spec(
    parser: ParserImage,
    *,
    engine: str | None = None,
    backend: SandboxBackend | None = None,
) -> ParserSpec:
    """The ParserSpec that replays records of a packaged parser.

    The recorded parser_config is the merged configuration the run received,
    so it is passed on unchanged. `backend` replaces the CPU OciBackend (tests).
    """

    def build(input_path: Path | None, artifact_dir: Path, config: Mapping[str, Any]) -> SandboxConfig:
        if input_path is None:
            raise ValueError(f"{parser.name} needs an input document")
        env = {
            **thread_env(parser.cpus),
            CONFIG_ENV: canonical_json(dict(config)).decode("utf-8"),
            "STELE_PARSER_DEVICE": "cpu",
        }
        return SandboxConfig(
            command=list(parser.command),
            artifact_dir=artifact_dir,
            input_path=input_path,
            env=env,
            timeout_seconds=parser.timeout_seconds,
        )

    def make_backend(identity: ParserIdentity | None) -> SandboxBackend:
        if backend is not None:
            return backend
        return _backend(parser, "cpu", engine=engine, memory=parser.memory, cpus=parser.cpus)

    return ParserSpec(
        name=parser.name,
        version=parser.version,
        requirements=ParserRequirements(requires_native_libs=True, resource_limits=True),
        build_config=build,
        policy=parser.comparison,
        backend=make_backend,
    )
