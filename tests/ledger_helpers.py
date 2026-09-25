"""Shared construction for ledger tests (roadmap #12).

A LedgerStore is bound to the evidence archive its records point into, and
every record names the parser that produced it.
"""
from __future__ import annotations

from pathlib import Path

from stele.archive import BlobStore
from stele.ledger.models import ParserIdentity
from stele.ledger.store import LedgerStore

TEST_PARSER = ParserIdentity(name="test-parser", version="1.0")
TEST_CONFIG = {"mode": "test"}
# Keyword arguments every create_pending / ledger_transaction call needs.
PROVENANCE = {"parser": TEST_PARSER, "parser_config": TEST_CONFIG}


def open_ledger(db_path: Path) -> LedgerStore:
    """A ledger at db_path with its archive next to it (shared by every opener)."""
    db_path = Path(db_path)
    return LedgerStore(db_path, BlobStore(db_path.parent / "archive"))
