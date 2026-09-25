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
"""
from __future__ import annotations

from pathlib import Path

from ..containment.backend import ParserRequirements
from ..containment.sandbox import SandboxConfig

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
