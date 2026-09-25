"""Adapters: sealed parser output to SteleChunks (the #13 SteleAdapter contract).

chatgpt
    ChatGPTExportAdapter turns a sealed chatgpt-export-split bundle into one
    chunk per message on every conversation branch, streaming one
    conversation at a time.
extraction
    ExtractionAdapter delivers the canonical stele.extraction units of any
    bundle with a normalizer (MinerU, Marker, Docling Markdown; ChatGPT),
    checked against the evidence by the resolver first.
"""
from .chatgpt import ChatGPTExportAdapter, MalformedExportError
from .extraction import ExtractionAdapter

__all__ = ["ChatGPTExportAdapter", "ExtractionAdapter", "MalformedExportError"]
