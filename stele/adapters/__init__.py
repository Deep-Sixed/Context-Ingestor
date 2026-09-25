"""Adapters: sealed parser output to SteleChunks (the #13 SteleAdapter contract).

chatgpt
    ChatGPTExportAdapter turns a sealed chatgpt-export-split bundle into one
    chunk per message on every conversation branch, streaming one
    conversation at a time.
"""
from .chatgpt import ChatGPTExportAdapter, MalformedExportError

__all__ = ["ChatGPTExportAdapter", "MalformedExportError"]
