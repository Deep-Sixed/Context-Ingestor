"""Shared fixtures.

containment_backend parametrizes the Phase E containment proofs over every
registered sandbox backend that hosts processes (roadmap #5), so each new
process backend must pass the same proofs. A backend that cannot run on this
host is skipped with its reason.

The Phase E proofs run Python scripts, which only process backends can host.
Wasm backends prove the same guarantees with Wasm probe modules in
tests/test_wasm_backend.py (wasm_backend fixture).
"""
from __future__ import annotations

import pytest

from stele.containment import backend as backend_module

_REGISTERED = backend_module.default_backends()
_PROCESS_BACKENDS = [
    b for b in _REGISTERED if backend_module.Capability.HOST_PROCESS in b.capabilities()
]
_WASM_BACKENDS = [
    b for b in _REGISTERED if backend_module.Capability.WASM_MODULE in b.capabilities()
]


@pytest.fixture(params=_PROCESS_BACKENDS, ids=[b.name for b in _PROCESS_BACKENDS])
def containment_backend(request, monkeypatch):
    chosen = request.param
    if not chosen.available():
        pytest.skip(f"{chosen.name} backend unavailable here: {chosen.unavailable_reason()}")
    monkeypatch.setattr(backend_module, "default_backends", lambda: [chosen])
    return chosen


@pytest.fixture(params=_WASM_BACKENDS, ids=[b.name for b in _WASM_BACKENDS])
def wasm_backend(request, monkeypatch):
    """Each registered Wasm backend, as the only registered backend."""
    chosen = request.param
    if not chosen.available():
        pytest.skip(f"{chosen.name} backend unavailable here: {chosen.unavailable_reason()}")
    monkeypatch.setattr(backend_module, "default_backends", lambda: [chosen])
    return chosen
