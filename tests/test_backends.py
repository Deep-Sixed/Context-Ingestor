"""
Roadmap #5 — sandbox backend interface, capability matching, input staging.

  - Requirements map to capabilities; a parser can never request network.
  - select_backend picks the first capable, available backend and otherwise
    refuses with the reason — before anything is staged or executed.
  - Bubblewrap claims only what it enforces.
  - The staged input hash travels in the run result.
  - Directory inputs are staged file by file with the same no-follow checks.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from stele.containment import backend as backend_module
from stele.containment.backend import (
    BubblewrapBackend,
    Capability,
    ExecutionOutcome,
    ParserRequirements,
    SandboxBackend,
    SandboxUnavailableError,
    UnsupportedBackendError,
    select_backend,
)
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig
from stele.containment.staging import InputStagingError, stage_directory, stage_input
from stele.ledger.hashing import sha256_manifest

PYTHON = str(Path(sys.executable).resolve())
requires_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="live containment proof requires Linux bubblewrap",
)
requires_posix_staging = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fwalk"),
    reason="secure staging requires O_NOFOLLOW and os.fwalk",
)

ISOLATION = {Capability.FILESYSTEM_ISOLATION, Capability.NETWORK_ISOLATION}
# What a default (host-process) parser needs.
BASE = ISOLATION | {Capability.HOST_PROCESS}


class FakeBackend(SandboxBackend):
    def __init__(self, name, caps, *, available=True, reason="not installed"):
        self.name = name
        self._caps = frozenset(caps)
        self._available = available
        self._reason = reason
        self.executed: list[SandboxConfig] = []

    def capabilities(self):
        return self._caps

    def available(self):
        return self._available

    def unavailable_reason(self):
        return self._reason

    def execute(self, config):
        self.executed.append(config)
        seen = None
        if config.input_path is not None:
            seen = str(config.input_path)
        return ExecutionOutcome(exit_code=0, stdout=seen or "", stderr="", wall_time_seconds=0.0)


# ---------------------------------------------------------------------------
# Requirements and capabilities
# ---------------------------------------------------------------------------

class TestRequirements:

    def test_default_requires_isolation_and_a_process_host(self) -> None:
        assert ParserRequirements().required_capabilities() == ISOLATION | {
            Capability.HOST_PROCESS,
        }

    def test_wasm_parser_requires_a_wasm_host_instead(self) -> None:
        assert ParserRequirements(wasm_module=True).required_capabilities() == ISOLATION | {
            Capability.WASM_MODULE,
        }

    def test_flags_map_to_capabilities(self) -> None:
        req = ParserRequirements(requires_gpu=True, requires_native_libs=True, deterministic=True)
        assert req.required_capabilities() == BASE | {
            Capability.GPU, Capability.NATIVE_LIBS, Capability.DETERMINISTIC,
        }

    def test_parsers_cannot_request_network(self) -> None:
        field_names = {f.name for f in dataclasses.fields(ParserRequirements)}
        assert not any("network" in name for name in field_names)
        with pytest.raises(TypeError):
            ParserRequirements(allow_network=True)  # type: ignore[call-arg]
        # The only network-related capability is the isolation guarantee itself.
        assert [c for c in Capability if "network" in c.value] == [Capability.NETWORK_ISOLATION]

    def test_bubblewrap_claims_only_what_it_enforces(self) -> None:
        caps = BubblewrapBackend().capabilities()
        assert caps == ISOLATION | {Capability.HOST_PROCESS, Capability.NATIVE_LIBS}
        for not_yet in (Capability.SYSCALL_FILTER, Capability.RESOURCE_LIMITS,
                        Capability.GPU, Capability.DETERMINISTIC, Capability.WASM_MODULE):
            assert not_yet not in caps


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

class TestSelectBackend:

    def test_first_capable_available_backend_wins(self) -> None:
        a = FakeBackend("a", BASE)
        b = FakeBackend("b", BASE | {Capability.GPU})
        assert select_backend(ParserRequirements(), [a, b]) is a
        assert select_backend(ParserRequirements(requires_gpu=True), [a, b]) is b

    def test_unavailable_backend_is_skipped(self) -> None:
        a = FakeBackend("a", BASE, available=False)
        b = FakeBackend("b", BASE)
        assert select_backend(ParserRequirements(), [a, b]) is b

    def test_missing_capability_is_refused_with_reason(self) -> None:
        a = FakeBackend("a", BASE)
        with pytest.raises(UnsupportedBackendError, match="a: lacks gpu") as info:
            select_backend(ParserRequirements(requires_gpu=True), [a])
        assert not isinstance(info.value, SandboxUnavailableError)

    def test_capable_but_unavailable_everywhere_is_sandbox_unavailable(self) -> None:
        a = FakeBackend("a", BASE, available=False, reason="needs Linux")
        with pytest.raises(SandboxUnavailableError, match="needs Linux"):
            select_backend(ParserRequirements(), [a])

    def test_capable_but_unavailable_beside_incapable_is_sandbox_unavailable(self) -> None:
        # A backend for another workload kind must not hide the real cause:
        # the capable backend just cannot run on this host.
        process = FakeBackend("process", BASE, available=False, reason="needs Linux")
        wasm = FakeBackend("wasm", ISOLATION | {Capability.WASM_MODULE})
        with pytest.raises(SandboxUnavailableError, match="needs Linux"):
            select_backend(ParserRequirements(), [process, wasm])

    def test_no_backends_registered(self) -> None:
        with pytest.raises(UnsupportedBackendError, match="no sandbox backends"):
            select_backend(ParserRequirements(), [])

    def test_backend_without_isolation_is_never_chosen(self) -> None:
        leaky = FakeBackend("leaky", {Capability.NATIVE_LIBS})
        with pytest.raises(UnsupportedBackendError, match="network_isolation"):
            select_backend(ParserRequirements(), [leaky])


class TestRunInSandboxRouting:

    def test_refuses_before_staging_or_executing(self, tmp_path: Path) -> None:
        cpu_only = FakeBackend("cpu", BASE)
        source = tmp_path / "in.txt"
        source.write_text("data")
        with pytest.raises(UnsupportedBackendError):
            run_in_sandbox(
                SandboxConfig(command=["x"], artifact_dir=tmp_path / "o", input_path=source),
                requirements=ParserRequirements(requires_gpu=True),
                backend=cpu_only,
            )
        assert cpu_only.executed == []

    def test_uses_registry_when_no_backend_passed(self, tmp_path: Path, monkeypatch) -> None:
        fake = FakeBackend("fake", BASE)
        monkeypatch.setattr(backend_module, "default_backends", lambda: [fake])
        result = run_in_sandbox(SandboxConfig(command=["x"], artifact_dir=tmp_path / "o"))
        assert result.backend == "fake"
        assert result.input_sha256 is None
        assert len(fake.executed) == 1

    @requires_posix_staging
    def test_backend_receives_staged_copy_and_hash_is_recorded(self, tmp_path: Path) -> None:
        fake = FakeBackend("fake", BASE)
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.7 payload")
        result = run_in_sandbox(
            SandboxConfig(command=["x"], artifact_dir=tmp_path / "o", input_path=source),
            backend=fake,
        )
        staged = fake.executed[0].input_path
        assert staged != source and staged.name == "doc.pdf"
        assert result.input_sha256 == hashlib.sha256(b"%PDF-1.7 payload").hexdigest()


# ---------------------------------------------------------------------------
# Directory inputs
# ---------------------------------------------------------------------------

def _tree(root: Path) -> dict[str, bytes]:
    files = {"a.txt": b"alpha", "sub/b.json": b"{}", "sub/deeper/c.md": b"# c"}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return files


@requires_posix_staging
class TestDirectoryStaging:

    def test_tree_is_copied_and_hashed(self, tmp_path: Path) -> None:
        src = tmp_path / "corpus"
        files = _tree(src)
        staged = stage_directory(src, tmp_path / "stage")

        expected = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
        assert staged.manifest == expected
        assert staged.sha256 == sha256_manifest(expected)
        for rel, data in files.items():
            assert (staged.staged_path / rel).read_bytes() == data
        assert staged.staged_path.name == "corpus"

    def test_hash_is_stable_and_content_sensitive(self, tmp_path: Path) -> None:
        src = tmp_path / "corpus"
        _tree(src)
        first = stage_input(src, tmp_path / "s1").sha256
        assert stage_input(src, tmp_path / "s2").sha256 == first
        (src / "a.txt").write_bytes(b"changed")
        assert stage_input(src, tmp_path / "s3").sha256 != first

    def test_symlinked_file_inside_is_refused(self, tmp_path: Path) -> None:
        src = tmp_path / "corpus"
        _tree(src)
        secret = tmp_path / "secret.txt"
        secret.write_text("host secret")
        (src / "leak.txt").symlink_to(secret)
        with pytest.raises(InputStagingError, match="symlink"):
            stage_directory(src, tmp_path / "stage")

    def test_symlinked_directory_inside_is_refused(self, tmp_path: Path) -> None:
        src = tmp_path / "corpus"
        _tree(src)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "x.txt").write_text("host")
        (src / "linkdir").symlink_to(outside, target_is_directory=True)
        with pytest.raises(InputStagingError, match="symlink"):
            stage_directory(src, tmp_path / "stage")

    def test_symlinked_root_is_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        _tree(real)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(InputStagingError):
            stage_input(link, tmp_path / "stage")

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
    def test_fifo_inside_is_refused(self, tmp_path: Path) -> None:
        src = tmp_path / "corpus"
        _tree(src)
        os.mkfifo(src / "pipe")
        with pytest.raises(InputStagingError, match="non-regular"):
            stage_directory(src, tmp_path / "stage")

    def test_unreadable_subdirectory_fails_instead_of_skipping(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        src = tmp_path / "corpus"
        _tree(src)
        real_scandir = os.scandir

        def scandir(path="."):
            if isinstance(path, int) and os.path.samestat(
                os.fstat(path), os.stat(src / "sub" / "deeper")
            ):
                raise PermissionError(13, "Permission denied", "deeper")
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)
        with pytest.raises(InputStagingError, match="unreadable"):
            stage_directory(src, tmp_path / "stage")

    def test_file_input_still_works(self, tmp_path: Path) -> None:
        f = tmp_path / "one.txt"
        f.write_bytes(b"one")
        staged = stage_input(f, tmp_path / "stage")
        assert staged.sha256 == hashlib.sha256(b"one").hexdigest()
        assert staged.manifest == {}


# ---------------------------------------------------------------------------
# Live bubblewrap: the parser sees exactly the staged bytes
# ---------------------------------------------------------------------------

@requires_bwrap
class TestLiveInputs:

    PROBE = (
        "import hashlib, json, os\n"
        "base = os.environ['STELE_INPUT_PATH']\n"
        "out = {}\n"
        "if os.path.isdir(base):\n"
        "    for root, _, files in os.walk(base):\n"
        "        for f in files:\n"
        "            p = os.path.join(root, f)\n"
        "            out[os.path.relpath(p, base)] = hashlib.sha256(open(p, 'rb').read()).hexdigest()\n"
        "else:\n"
        "    out['file'] = hashlib.sha256(open(base, 'rb').read()).hexdigest()\n"
        "open(os.path.join(os.environ['STELE_OUTPUT_DIR'], 'seen.json'), 'w').write(json.dumps(out))\n"
    )

    def test_file_input_hash_matches_what_parser_read(self, tmp_path: Path) -> None:
        src = tmp_path / "note.md"
        src.write_bytes(b"# staged bytes")
        out = tmp_path / "out"
        result = run_in_sandbox(SandboxConfig(
            command=[PYTHON, "-c", self.PROBE], artifact_dir=out, input_path=src,
        ))
        assert result.succeeded, result.stderr
        assert result.backend == "bubblewrap"
        seen = json.loads((out / "seen.json").read_text())
        assert seen["file"] == result.input_sha256 == hashlib.sha256(b"# staged bytes").hexdigest()

    def test_directory_input_is_visible_and_hash_matches(self, tmp_path: Path) -> None:
        src = tmp_path / "corpus"
        files = _tree(src)
        out = tmp_path / "out"
        result = run_in_sandbox(SandboxConfig(
            command=[PYTHON, "-c", self.PROBE], artifact_dir=out, input_path=src,
        ))
        assert result.succeeded, result.stderr
        seen = json.loads((out / "seen.json").read_text())
        expected = {rel: hashlib.sha256(d).hexdigest() for rel, d in files.items()}
        assert seen == expected
        assert result.input_sha256 == sha256_manifest(expected)
