"""
Parser specifications: how to run a recorded parser again (roadmap #14).

The ledger records a run's parser as a ParserIdentity (name, version, and the
measured image digest or Wasm module hash) plus its parser_config. A
ParserSpec is the other half: for one name and version, how to build the
SandboxConfig from an input, an output directory and a config; which
capabilities the parser needs; which Wasm module or OCI image it is; and, for
a parser that is not deterministic, its comparison policy.

The same spec runs the parser the first time (run_parser) and on replay, so a
replay goes through exactly the code path that produced the record.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..archive.store import BlobStore
from ..containment.backend import ParserRequirements, SandboxBackend
from ..containment.result import SandboxResult
from ..containment.runner import run_in_sandbox
from ..containment.sandbox import SandboxConfig
from ..ledger.models import ParserIdentity
from .policy import ComparisonPolicy

# (input path or None, artifact dir, parser_config) -> SandboxConfig
ConfigBuilder = Callable[[Path | None, Path, Mapping[str, Any]], SandboxConfig]
# The recorded identity on replay (None on a first run) -> the backend to use.
BackendFactory = Callable[[ParserIdentity | None], SandboxBackend]
# A record's run_settings -> the spec that runs the parser with them.
RunSettingsBinder = Callable[[Mapping[str, Any]], "ParserSpec"]


@dataclass(frozen=True)
class ParserSpec:
    name: str
    version: str
    requirements: ParserRequirements
    build_config: ConfigBuilder
    # Wasm parsers: the module the backend runs. Replay refuses (UNREPLAYABLE)
    # unless its hash is the recorded module_sha256.
    module_path: Path | None = None
    # How to judge a replay of a non-deterministic parser. Without one, such a
    # parser's records are UNREPLAYABLE: nothing defines what agreement means.
    policy: ComparisonPolicy | None = None
    # Explicit backend, e.g. an OciBackend pinned to the recorded image.
    backend: BackendFactory | None = None
    # For parsers whose runs record run_settings (device, limits, timeout):
    # returns the spec that runs the parser as a given record was run. Raises
    # ValueError for settings this spec cannot honour. Replay calls it for
    # every record that has run_settings.
    with_run_settings: RunSettingsBinder | None = None

    def __post_init__(self) -> None:
        # Validates name and version the same way the ledger does.
        ParserIdentity(self.name, self.version)

    @property
    def deterministic(self) -> bool:
        return self.requirements.deterministic

    def identity(self) -> ParserIdentity:
        """The identity to record; the run fills in the measured digests."""
        return ParserIdentity(self.name, self.version)


class ParserCatalog:
    """The parsers this deployment can run, by (name, version)."""

    def __init__(self, specs: list[ParserSpec] | None = None) -> None:
        self._specs: dict[tuple[str, str], ParserSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: ParserSpec) -> None:
        key = (spec.name, spec.version)
        if key in self._specs:
            raise ValueError(f"parser {spec.name} {spec.version} is already registered")
        self._specs[key] = spec

    def get(self, name: str, version: str) -> ParserSpec | None:
        return self._specs.get((name, version))


def run_parser(
    spec: ParserSpec,
    *,
    artifact_dir: Path,
    parser_config: Mapping[str, Any],
    input_path: Path | None = None,
    store: BlobStore | None = None,
    identity: ParserIdentity | None = None,
) -> SandboxResult:
    """Run a parser from its spec: the path both first runs and replays take."""
    config = spec.build_config(input_path, artifact_dir, parser_config)
    backend = spec.backend(identity) if spec.backend is not None else None
    return run_in_sandbox(config, requirements=spec.requirements, backend=backend, store=store)
