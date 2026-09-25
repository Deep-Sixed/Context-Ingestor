"""Shared fixtures.

containment_backend parametrizes the Phase E containment proofs over every
registered sandbox backend that hosts processes (roadmap #5), so each new
process backend must pass the same proofs. The OCI backend (roadmap #8) is also
proved explicitly on Docker (the default prefers Podman) and with the opt-in
gVisor runtime (runsc), which CI registers with Docker. A backend that cannot
run on this host is skipped with its reason.

sandbox_python is the interpreter command for the chosen backend: bubblewrap
exposes the host /usr, so it is the host interpreter; a container runs its
image's own python3.

The Phase E proofs run Python scripts, which only process backends can host.
Wasm backends prove the same guarantees with Wasm probe modules in
tests/test_wasm_backend.py (wasm_backend fixture).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stele.containment import backend as backend_module
from stele.containment.oci import OciBackend

_REGISTERED = backend_module.default_backends()
_PROVED = [
    b for b in [
        *_REGISTERED,
        OciBackend(engine="docker"),
        OciBackend(engine="docker", runtime="runsc"),
    ]
    if backend_module.Capability.HOST_PROCESS in b.capabilities()
]
_WASM_BACKENDS = [
    b for b in _REGISTERED if backend_module.Capability.WASM_MODULE in b.capabilities()
]


def _proof_id(backend) -> str:
    engine = getattr(backend, "engine", None)
    return f"{backend.name}-{engine}" if engine else backend.name


HOST_PYTHON = str(Path(sys.executable).resolve())


@pytest.fixture(params=_PROVED, ids=[_proof_id(b) for b in _PROVED])
def containment_backend(request, monkeypatch):
    chosen = request.param
    if not chosen.available():
        pytest.skip(f"{chosen.name} backend unavailable here: {chosen.unavailable_reason()}")
    monkeypatch.setattr(backend_module, "default_backends", lambda: [chosen])
    return chosen


@pytest.fixture
def sandbox_python(containment_backend) -> str:
    if isinstance(containment_backend, OciBackend):
        return "python3"
    return HOST_PYTHON


@pytest.fixture(params=_WASM_BACKENDS, ids=[b.name for b in _WASM_BACKENDS])
def wasm_backend(request, monkeypatch):
    """Each registered Wasm backend, as the only registered backend."""
    chosen = request.param
    if not chosen.available():
        pytest.skip(f"{chosen.name} backend unavailable here: {chosen.unavailable_reason()}")
    monkeypatch.setattr(backend_module, "default_backends", lambda: [chosen])
    return chosen
