"""The identity contract (roadmap #42): typed, self-verifying references to evidence.

refs
    SourceRef, SnapshotRef, RecordRef, ObservationRef, AnchorRef, EventRef:
    one canonical string each ("stele:<type>:..."), strict parse_ref(), and
    constructors from ledger records, extraction units and events.
resolver
    IdentityResolver(ledger).resolve(ref) checks a reference against the
    ledger, the evidence archive and the event chain, and returns what it names.

CLI: python -m stele.identity resolve <ledger.db> <archive-dir> <ref>...
"""
from .refs import (
    PREFIX, AnchorRef, EventRef, Observation, ObservationRef, RecordRef, Ref, RefFormatError,
    SnapshotRef, SourceRef, anchor_ref, event_ref, observation_of, parse_ref, record_ref,
)
from .resolver import IdentityResolver, ResolveError

__all__ = [
    "PREFIX", "AnchorRef", "EventRef", "Observation", "ObservationRef", "RecordRef", "Ref",
    "RefFormatError", "SnapshotRef", "SourceRef", "anchor_ref", "event_ref", "observation_of",
    "parse_ref", "record_ref", "IdentityResolver", "ResolveError",
]
