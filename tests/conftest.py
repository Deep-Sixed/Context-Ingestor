"""Shared fixtures.

containment_backend parametrizes the Phase E containment proofs over every
registered sandbox backend (roadmap #5), so each new backend must pass the same
proofs. A backend that cannot run on this host is skipped with its reason.
"""
from __future__ import annotations

import pytest

from stele.containment import backend as backend_module

_REGISTERED = backend_module.default_backends()


@pytest.fixture(params=_REGISTERED, ids=[b.name for b in _REGISTERED])
def containment_backend(request, monkeypatch):
    chosen = request.param
    if not chosen.available():
        pytest.skip(f"{chosen.name} backend unavailable here: {chosen.unavailable_reason()}")
    monkeypatch.setattr(backend_module, "default_backends", lambda: [chosen])
    return chosen
