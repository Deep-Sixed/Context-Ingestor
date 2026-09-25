"""The canonical extraction contract (stele.extraction/v1) and its trusted resolver.

contract
    Extraction, Unit, Anchor: one parser-independent shape for extracted
    content, every unit anchored into the sealed bundle; canonical JSON with
    strict parsing.
normalize
    Trusted normalizers from a sealed bundle to an Extraction (Markdown from
    MinerU, Marker and Docling; ChatGPT conversations).
resolver
    Re-reads each anchor from archive-verified bytes and checks the unit's
    text against it.

CLI: python -m stele.extraction {extract,verify} ... (see __main__.py).
"""
from .contract import (
    KINDS, SCHEMA, SCHEMA_VERSION, Anchor, Extraction, ExtractionFormatError, Unit, number_units,
)
from .normalizers import NormalizeError, normalize, normalizer_for
from .resolver import RENDERERS, BundleResolver, ResolutionError, Resolver

__all__ = [
    "KINDS", "SCHEMA", "SCHEMA_VERSION", "Anchor", "Extraction", "ExtractionFormatError", "Unit",
    "number_units", "NormalizeError", "normalize", "normalizer_for", "RENDERERS",
    "BundleResolver", "ResolutionError", "Resolver",
]
