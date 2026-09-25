"""Shared fixtures.

containment_backend parametrizes the Phase E containment proofs over every
registered sandbox backend (roadmap #5), so each new backend must pass the same
proofs. The OCI backend (roadmap #8) is also proved explicitly on Docker (the
default prefers Podman) and with the opt-in gVisor runtime (runsc), which CI
registers with Docker. A backend that cannot run on this host is skipped with
its reason.

sandbox_python is the interpreter command for the chosen backend: bubblewrap
exposes the host /usr, so it is the host interpreter; a container runs its
image's own python3.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stele.containment import backend as backend_module
from stele.containment.oci import OciBackend

_PROVED = [
    *backend_module.default_backends(),
    OciBackend(engine="docker"),
    OciBackend(engine="docker", runtime="runsc"),
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
