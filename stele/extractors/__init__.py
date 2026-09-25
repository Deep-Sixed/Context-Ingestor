"""
Wasm extractors that ship with Stele (roadmap #7).

Each extractor is a WebAssembly module run on the Wasmtime backend. Modules
ship as WebAssembly text (.wat) so they are reviewable and need no compiler
toolchain; the backend compiles them and records the SHA-256 of the resulting
binary as the parser's identity (SandboxResult.module_sha256).

chatgpt_export_split
    Splits a ChatGPT export's conversations.json (one JSON array of
    conversation objects) into conversation-NNNNNN.json files holding each
    element's exact bytes, plus index.jsonl with each element's byte offset and
    length in the input. See chatgpt_export_split.wat for the format.

Each extractor also has a ParserSpec (roadmap #14), so its records can be
replayed; EXTRACTOR_SPECS lists them for a ParserCatalog.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..containment.backend import ParserRequirements
from ..containment.sandbox import SandboxConfig
from ..replay.parsers import ParserSpec

CHATGPT_EXPORT_SPLIT = Path(__file__).with_name("chatgpt_export_split.wat")

# Wasm extractors are pure functions of their input: route them to a
# deterministic Wasm backend.
WASM_EXTRACTOR_REQUIREMENTS = ParserRequirements(wasm_module=True, deterministic=True)


def chatgpt_export_split_config(
    input_path: Path, artifact_dir: Path, *, timeout_seconds: int = 300
) -> SandboxConfig:
    """SandboxConfig that splits the ChatGPT export at input_path.

    Run it with run_in_sandbox(config, requirements=WASM_EXTRACTOR_REQUIREMENTS).
    """
    return SandboxConfig(
        command=[str(CHATGPT_EXPORT_SPLIT)],
        artifact_dir=artifact_dir,
        input_path=input_path,
        timeout_seconds=timeout_seconds,
    )


def _chatgpt_export_split(
    input_path: Path | None, artifact_dir: Path, parser_config: Mapping[str, Any]
) -> SandboxConfig:
    unknown = set(parser_config) - {"timeout_seconds"}
    if unknown:
        raise ValueError(f"chatgpt-export-split takes no config {sorted(unknown)}")
    if input_path is None:
        raise ValueError("chatgpt-export-split needs an input")
    return chatgpt_export_split_config(
        input_path, artifact_dir, timeout_seconds=parser_config.get("timeout_seconds", 300)
    )


CHATGPT_EXPORT_SPLIT_SPEC = ParserSpec(
    name="chatgpt-export-split",
    version="1",
    requirements=WASM_EXTRACTOR_REQUIREMENTS,
    build_config=_chatgpt_export_split,
    module_path=CHATGPT_EXPORT_SPLIT,
)

EXTRACTOR_SPECS = [CHATGPT_EXPORT_SPLIT_SPEC]
