"""
ExtractionAdapter: deliver a sealed bundle's canonical Extraction, one chunk per unit.

The bundle is normalized by the trusted normalizer for its parser
(stele.extraction.normalize), and the result is checked against the same
verified bytes by the resolver before anything is returned, so a normalizer
bug that misquotes the evidence fails the transform instead of reaching a
target. Chunk ids are "<record_id>:<unit id>"; metadata carries the unit's
kind, order, page, bbox, level, parent, anchor and attributes, plus the
extraction's schema and normalizer.
"""
from __future__ import annotations

from ..contracts.adapter import SealedBundle, SteleChunk, make_chunk
from ..extraction.contract import SCHEMA, SCHEMA_VERSION


class ExtractionAdapter:
    """SteleAdapter for any bundle stele.extraction has a normalizer for."""

    def transform(self, bundle: SealedBundle) -> list[SteleChunk]:
        # Imported here: the normalizers build on the other adapters in this package.
        from ..extraction.normalizers import normalize
        from ..extraction.resolver import BundleResolver, ResolutionError

        extraction = normalize(bundle)
        problems = BundleResolver(bundle).verify(extraction)
        if problems:
            raise ResolutionError(
                f"extraction of {bundle.record_id} does not match its evidence: {problems[:5]}"
            )
        return [
            make_chunk(
                f"{bundle.record_id}:{unit.id}",
                unit.text,
                source="stele-extraction",
                schema=f"{SCHEMA}/v{SCHEMA_VERSION}",
                normalizer=f"{extraction.normalizer['name']}/{extraction.normalizer['version']}",
                stele_record_id=bundle.record_id,
                unit_id=unit.id,
                kind=unit.kind,
                order=unit.order,
                page=unit.page,
                bbox=list(unit.bbox) if unit.bbox is not None else None,
                level=unit.level,
                parent=f"{bundle.record_id}:{unit.parent}" if unit.parent else None,
                anchor=unit.anchor.to_json(),
                attributes=dict(unit.attributes),
            )
            for unit in extraction.units
        ]
