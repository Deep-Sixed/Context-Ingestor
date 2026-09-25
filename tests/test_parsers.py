"""
Roadmap #9/#10 — packaged ML parsers on the OCI backend (stele.parsers).

Structural tests use a fake backend, so they run everywhere:

  - The parser gets its merged configuration (STELE_PARSER_CONFIG) and thread
    caps matching its CPU allowance; the identity records the image digest,
    device, limits and a digest of that exact configuration.
  - A failed, timed-out or killed run keeps nothing: the artifact directory is
    emptied and nothing is stored, while the input Snapshot is still archived.
  - GPU "optional" falls back to the CPU image when no GPU can be passed
    through; "required" never does. Enforced resource limits are required.
  - Unsupported document types are refused before anything runs.

Live tests run the same flows through a real container engine with the
pinned test image standing in for a parser image (the real parser images are
built and exercised by the parser-images workflow, tests/test_parser_images.py).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from stele.archive.store import BlobStore
from stele.containment.backend import (
    Capability,
    ExecutionOutcome,
    SandboxBackend,
    SandboxUnavailableError,
    UnsupportedBackendError,
)
from stele.containment.oci import DEFAULT_OCI_IMAGE, OciBackend
from stele.parsers import (
    CONFIG_ENV,
    ParserImage,
    UnsupportedInputError,
    choose_backend,
    config_digest,
    describe_failure,
    run_parser,
    thread_env,
)
from stele.parsers.__main__ import _main as cli_main
from stele.parsers.catalog import PARSERS, get_parser

requires_posix_staging = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fwalk"),
    reason="secure staging requires O_NOFOLLOW and os.fwalk",
)

FULL = frozenset({
    Capability.FILESYSTEM_ISOLATION,
    Capability.NETWORK_ISOLATION,
    Capability.HOST_PROCESS,
    Capability.NATIVE_LIBS,
    Capability.RESOURCE_LIMITS,
})
DIGEST = "sha256:" + "ef" * 32

PARSER = ParserImage(
    name="fake",
    version="1.0",
    image="localhost/stele/fake:1.0",
    config={"mode": "fast", "pages": ""},
    formats=(".pdf",),
    memory="512m",
    cpus=2.5,
)


class FakeBackend(SandboxBackend):
    """Records the config it was given and plays a scripted parser."""

    def __init__(self, *, caps=FULL, available=True, reason="", gpu=False,
                 exit_code=0, timed_out=False, write=None, name="oci-runc"):
        self.name = name
        self.caps = frozenset(caps) | ({Capability.GPU} if gpu else frozenset())
        self._available = available
        self._reason = reason
        self.gpu = gpu
        self.memory = "512m"
        self.cpus = 2.5
        self.pids_limit = 1024
        self.image = "localhost/stele/fake-gpu:1.0" if gpu else PARSER.image
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.write = write if write is not None else {"document.md": b"# parsed\n"}
        self.seen = None

    def capabilities(self):
        return self.caps

    def available(self):
        return self._available

    def unavailable_reason(self):
        return self._reason

    def execute(self, config):
        self.seen = config
        for rel, data in self.write.items():
            path = config.artifact_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return ExecutionOutcome(
            exit_code=self.exit_code, stdout="", stderr="boom: bad page 3\n",
            wall_time_seconds=0.1, timed_out=self.timed_out, image_digest=DIGEST,
        )


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    path = tmp_path / "input" / "report.pdf"
    path.parent.mkdir()
    path.write_bytes(b"%PDF-1.7 fake")
    return path


@requires_posix_staging
class TestRunParser:

    def test_config_threads_and_identity(self, doc: Path, tmp_path: Path) -> None:
        backend = FakeBackend()
        run = run_parser(PARSER, doc, tmp_path / "out", config={"pages": "1-2"}, backend=backend)

        assert run.succeeded and run.failure is None
        merged = {"mode": "fast", "pages": "1-2"}
        assert json.loads(backend.seen.env[CONFIG_ENV]) == merged
        assert backend.seen.env["OMP_NUM_THREADS"] == "2"
        assert backend.seen.env["STELE_TORCH_THREADS"] == "2"
        assert backend.seen.env["STELE_PARSER_DEVICE"] == "cpu"
        assert backend.seen.command == list(PARSER.command)

        ident = run.identity.to_dict()
        assert ident["image_digest"] == DIGEST
        assert ident["device"] == "cpu"
        assert ident["config"] == merged
        assert ident["config_sha256"] == config_digest(merged)
        assert ident["limits"] == {"memory": "512m", "cpus": 2.5, "pids": 1024,
                                   "timeout_seconds": PARSER.timeout_seconds}
        assert [p.name for p in run.result.artifact_paths] == ["document.md"]

    def test_config_digest_is_order_independent(self) -> None:
        assert config_digest({"a": 1, "b": 2}) == config_digest({"b": 2, "a": 1})
        assert config_digest({"a": 1}) != config_digest({"a": 2})

    def test_defaults_are_not_mutated_by_overrides(self, doc: Path, tmp_path: Path) -> None:
        run_parser(PARSER, doc, tmp_path / "out", config={"mode": "slow"}, backend=FakeBackend())
        assert dict(PARSER.config) == {"mode": "fast", "pages": ""}

    @pytest.mark.parametrize("outcome", [
        {"exit_code": 4},
        {"exit_code": 137},
        {"exit_code": -1, "timed_out": True},
    ], ids=["parser-error", "killed", "timeout"])
    def test_failure_keeps_and_stores_nothing(self, outcome, doc: Path, tmp_path: Path) -> None:
        store = BlobStore(tmp_path / "store")
        backend = FakeBackend(write={"partial.md": b"half", "images/p1.png": b"\x89PNG"}, **outcome)
        out = tmp_path / "out"
        run = run_parser(PARSER, doc, out, store=store, backend=backend)

        assert not run.succeeded and run.failure
        assert run.result.artifact_paths == []
        assert run.result.artifact_digests == {}
        assert run.result.artifact_bundle_digest is None
        assert out.is_dir() and list(out.iterdir()) == []
        # The input is still evidence of what the parser was given.
        assert run.result.input_snapshot is not None
        assert run.result.input_snapshot.digest == run.result.input_sha256

    def test_success_stores_the_bundle(self, doc: Path, tmp_path: Path) -> None:
        store = BlobStore(tmp_path / "store")
        run = run_parser(PARSER, doc, tmp_path / "out", store=store, backend=FakeBackend())
        assert set(run.result.artifact_digests) == {"document.md"}
        assert run.result.artifact_bundle_digest is not None

    def test_unsupported_input_is_refused_before_running(self, tmp_path: Path) -> None:
        other = tmp_path / "notes.txt"
        other.write_text("hi")
        backend = FakeBackend()
        with pytest.raises(UnsupportedInputError, match=r"\.txt"):
            run_parser(PARSER, other, tmp_path / "out", backend=backend)
        assert backend.seen is None

    def test_backend_without_enforced_limits_is_refused(self, doc: Path, tmp_path: Path) -> None:
        backend = FakeBackend(caps=FULL - {Capability.RESOURCE_LIMITS})
        with pytest.raises(UnsupportedBackendError, match="resource_limits"):
            run_parser(PARSER, doc, tmp_path / "out", backend=backend)
        assert backend.seen is None


class TestDeviceChoice:

    def test_optional_gpu_uses_gpu_when_available(self) -> None:
        parser = ParserImage(name="p", version="1", image="i", gpu="optional", gpu_image="g")
        gpu, cpu = FakeBackend(gpu=True, name="oci-runc-gpu"), FakeBackend()
        chosen, device, req = choose_backend(parser, backends=[gpu, cpu])
        assert chosen is gpu and device == "gpu" and req.requires_gpu

    def test_optional_gpu_falls_back_to_cpu(self) -> None:
        parser = ParserImage(name="p", version="1", image="i", gpu="optional", gpu_image="g")
        gpu = FakeBackend(gpu=True, available=False, reason="no usable GPU", name="oci-runc-gpu")
        cpu = FakeBackend()
        chosen, device, req = choose_backend(parser, backends=[gpu, cpu])
        assert chosen is cpu and device == "cpu" and not req.requires_gpu

    def test_required_gpu_never_falls_back(self) -> None:
        parser = ParserImage(name="p", version="1", image="i", gpu="required")
        gpu = FakeBackend(gpu=True, available=False, reason="no usable GPU", name="oci-runc-gpu")
        with pytest.raises(SandboxUnavailableError, match="no usable GPU"):
            choose_backend(parser, backends=[gpu])

    def test_cpu_only_parser_refuses_gpu(self) -> None:
        with pytest.raises(UnsupportedInputError, match="no GPU build"):
            choose_backend(PARSER, device="gpu", backends=[FakeBackend()])

    def test_gpu_backend_uses_the_gpu_image(self) -> None:
        parser = ParserImage(name="p", version="1", image="cpu-img", gpu="optional", gpu_image="gpu-img")
        assert parser.image_for("gpu") == "gpu-img"
        assert parser.image_for("cpu") == "cpu-img"
        assert ParserImage(name="q", version="1", image="only").image_for("gpu") == "only"

    def test_real_backends_carry_image_and_limits(self) -> None:
        parser = get_parser("mineru")
        # Nothing is probed until capabilities()/available() are asked for.
        from stele.parsers import _backend
        backend = _backend(parser, "gpu", engine="docker", memory="3g", cpus=1.5)
        assert isinstance(backend, OciBackend)
        assert backend.image == parser.gpu_image and backend.gpu
        assert (backend.memory, backend.cpus, backend.tmpfs_size) == ("3g", 1.5, parser.tmpfs_size)


class TestFailureWords:

    def _result(self, **kw):
        from uuid import uuid4

        from stele.containment.result import SandboxResult
        base = dict(run_id=uuid4(), exit_code=0, stdout="", stderr="", artifact_paths=[],
                    artifact_dir=Path("."), wall_time_seconds=0.0)
        base.update(kw)
        return SandboxResult(**base)

    def test_messages(self) -> None:
        assert describe_failure(self._result(), memory="1g", timeout=5) is None
        assert "memory limit" in describe_failure(self._result(exit_code=137), memory="1g", timeout=5)
        assert "timed out after 5s" in describe_failure(
            self._result(exit_code=-1, timed_out=True), memory="1g", timeout=5)
        msg = describe_failure(self._result(exit_code=3, stderr="x\nstele-parser: model weights missing\n"),
                               memory="1g", timeout=5)
        assert msg == "parser exited with status 3: stele-parser: model weights missing"

    def test_thread_env(self) -> None:
        assert thread_env(0.5)["OMP_NUM_THREADS"] == "1"
        assert set(thread_env(4).values()) == {"4"}


class TestCatalogAndCli:

    def test_catalog_images_are_buildable_from_the_repo(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for parser in PARSERS.values():
            assert (root / parser.containerfile).is_file(), parser.name
            if parser.gpu_image:
                assert (root / (parser.containerfile + ".gpu")).is_file(), parser.name
            assert parser.formats and parser.gpu in ("never", "optional", "required")

    def test_unknown_parser(self) -> None:
        with pytest.raises(KeyError, match="known parsers"):
            get_parser("nope")

    def test_list_and_build_command(self, capsys) -> None:
        assert cli_main(["list"]) == 0
        assert "mineru" in capsys.readouterr().out
        assert cli_main(["build-command", "mineru", "--engine", "podman"]) == 0
        out = capsys.readouterr().out.split()
        assert out[:2] == ["podman", "build"] and "parsers/mineru/Containerfile" in out

    def test_run_reports_unavailable_backend(self, tmp_path: Path, capsys, monkeypatch) -> None:
        def unavailable(*a, **k):
            raise SandboxUnavailableError("image localhost/stele/mineru:4.0.7 is not present locally")
        monkeypatch.setattr("stele.parsers.choose_backend", unavailable)
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        code = cli_main(["run", "mineru", "--input", str(doc), "--artifact-dir", str(tmp_path / "o")])
        assert code == 2
        assert "not present locally" in capsys.readouterr().err


# -- live: a real container engine, the pinned test image as the "parser" ------

def _live_backend(engine: str, **kw) -> OciBackend:
    backend = OciBackend(image=DEFAULT_OCI_IMAGE, engine=engine, **kw)
    if not backend.available():
        pytest.skip(f"OCI backend unavailable here: {backend.unavailable_reason()}")
    if Capability.RESOURCE_LIMITS not in backend.capabilities():
        pytest.skip(f"{engine} does not enforce resource limits here")
    return backend


LIVE_PARSER = ParserImage(
    name="probe", version="0", image=DEFAULT_OCI_IMAGE, formats=(".pdf",),
    command=("python3", "-c", ""), memory="256m", cpus=1.0,
)


def _probe(code: str) -> ParserImage:
    from dataclasses import replace
    return replace(LIVE_PARSER, command=("python3", "-c", code))


@pytest.fixture(params=["docker", "podman"])
def engine(request) -> str:
    if sys.platform != "linux":
        pytest.skip("live OCI parser runs need a Linux container engine")
    return request.param


@requires_posix_staging
class TestLive:

    def test_parser_reads_config_and_writes_output(self, engine, doc, tmp_path) -> None:
        backend = _live_backend(engine, memory="256m", cpus=1.0)
        code = (
            "import json, os, pathlib; "
            "c = json.loads(os.environ['STELE_PARSER_CONFIG']); "
            "out = pathlib.Path(os.environ['STELE_OUTPUT_DIR']); "
            "(out / 'seen.json').write_text(json.dumps({'config': c, "
            "'omp': os.environ['OMP_NUM_THREADS'], "
            "'input': open(os.environ['STELE_INPUT_PATH'], 'rb').read().decode()}))"
        )
        run = run_parser(_probe(code), doc, tmp_path / "out", config={"k": 1}, backend=backend)
        assert run.succeeded, run.result.stderr
        seen = json.loads((tmp_path / "out" / "seen.json").read_text())
        assert seen == {"config": {"k": 1}, "omp": "1", "input": "%PDF-1.7 fake"}
        assert run.identity.image_digest and run.identity.image_digest.startswith("sha256:")

    def test_memory_limit_is_a_clean_failure(self, engine, doc, tmp_path, monkeypatch) -> None:
        backend = _live_backend(engine, memory="128m", cpus=1.0)
        store = BlobStore(tmp_path / "store")
        code = (
            "import os, pathlib; "
            "pathlib.Path(os.environ['STELE_OUTPUT_DIR'], 'partial.md').write_text('half'); "
            # b'x' * n writes every page (calloc'd zero pages would not count).
            "hog = [b'x' * (16 * 1024 * 1024) for _ in range(64)]"  # 1 GiB > 128m
        )
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        run = run_parser(_probe(code), doc, tmp_path / "out", store=store, backend=backend)
        assert not run.succeeded
        assert "memory limit" in run.failure, (run.failure, run.result.stderr)
        assert list((tmp_path / "out").iterdir()) == []
        assert run.result.artifact_bundle_digest is None
        # Nothing lands in the caller's working directory (Podman's conmon
        # writes an "oom" file into its own working directory on OOM kills).
        assert list(cwd.iterdir()) == []

    def test_timeout_is_a_clean_failure(self, engine, doc, tmp_path) -> None:
        backend = _live_backend(engine, memory="256m", cpus=1.0)
        code = (
            "import os, pathlib, time; "
            "pathlib.Path(os.environ['STELE_OUTPUT_DIR'], 'partial.md').write_text('half'); "
            "time.sleep(120)"
        )
        run = run_parser(_probe(code), doc, tmp_path / "out", backend=backend, timeout_seconds=5)
        assert run.result.timed_out and "timed out" in run.failure
        assert list((tmp_path / "out").iterdir()) == []
