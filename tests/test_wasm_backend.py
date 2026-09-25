"""
Roadmap #7 — Wasm/WASI deterministic backend (Wasmtime).

  - Capabilities: isolation, resource limits and determinism; hosts only Wasm
    modules, never host processes, native libraries or GPUs.
  - Routing: deterministic Wasm parsers get Wasmtime, native-library parsers
    get bubblewrap, whatever the registry order.
  - The Phase E containment proofs, restated for Wasm with a probe module:
    artifacts reach the host only through /stele/output; no path outside the
    preopens can be reached; exit status and output are captured; failed runs
    commit nothing; there is no network.
  - Limits: memory cap, fuel (CPU) and wall-clock timeout.
  - Determinism: fixed clock and entropy, normalised file metadata, sorted
    directory listings, no state carried between runs, byte-identical output
    (the expected digests below are asserted on Linux, macOS and Windows CI).
  - The first real extractor: the ChatGPT export splitter.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from stele.containment import backend as backend_module
from stele.containment.backend import (
    BubblewrapBackend,
    Capability,
    ParserRequirements,
    SandboxUnavailableError,
    UnsupportedBackendError,
    select_backend,
)
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig
from stele.containment.wasm import (
    CLOCK_STEP_NS,
    FIXED_EPOCH_NS,
    WASM_LOAD_EXIT_CODE,
    WASM_TRAP_EXIT_CODE,
    WasmtimeBackend,
    load_wasm_module,
    wasm_module_sha256,
)
from stele.extractors import (
    CHATGPT_EXPORT_SPLIT,
    WASM_EXTRACTOR_REQUIREMENTS,
    chatgpt_export_split_config,
)
from stele.ledger.hashing import build_manifest, sha256_manifest

PROBE = Path(__file__).parent / "fixtures" / "wasm" / "probe.wat"
WASM = ParserRequirements(wasm_module=True)
DETERMINISTIC_WASM = ParserRequirements(wasm_module=True, deterministic=True)
MiB = 1024 * 1024


def _probe(tmp_path: Path, mode: str, *, input_bytes: bytes | None = b"hello input\n",
           backend=None, timeout: int = 30, env: dict[str, str] | None = None,
           out: str = "out"):
    source = None
    if input_bytes is not None:
        source = tmp_path / f"{out}-in" / "sample.txt"
        source.parent.mkdir()
        source.write_bytes(input_bytes)
    return run_in_sandbox(
        SandboxConfig(
            command=[str(PROBE), mode, "sample.txt"],
            artifact_dir=tmp_path / out,
            input_path=source,
            timeout_seconds=timeout,
            env=env or {},
        ),
        requirements=DETERMINISTIC_WASM,
        backend=backend,
    )


def _limited(wasm_backend, **limits):
    return type(wasm_backend)(**limits)


def _bundle_digest(artifact_dir: Path, paths: list[Path]) -> str:
    return sha256_manifest(build_manifest(artifact_dir, paths))


# ---------------------------------------------------------------------------
# Capabilities and routing (no wasmtime needed)
# ---------------------------------------------------------------------------

class TestCapabilities:

    def test_claims_isolation_limits_and_determinism(self) -> None:
        assert WasmtimeBackend().capabilities() == {
            Capability.FILESYSTEM_ISOLATION,
            Capability.NETWORK_ISOLATION,
            Capability.RESOURCE_LIMITS,
            Capability.DETERMINISTIC,
            Capability.WASM_MODULE,
        }

    def test_hosts_no_processes_native_code_or_gpu(self) -> None:
        caps = WasmtimeBackend().capabilities()
        for absent in (Capability.HOST_PROCESS, Capability.NATIVE_LIBS,
                       Capability.GPU, Capability.SYSCALL_FILTER):
            assert absent not in caps

    def test_registered_after_bubblewrap(self) -> None:
        names = [b.name for b in backend_module.default_backends()]
        # Wasmtime hosts a disjoint workload, so it follows every host-process
        # backend; its position cannot change which backend a parser gets.
        assert names[0] == "bubblewrap" and names[-1] == "wasmtime"

    def test_limits_are_validated(self) -> None:
        with pytest.raises(ValueError):
            WasmtimeBackend(memory_limit_bytes=1024)
        with pytest.raises(ValueError):
            WasmtimeBackend(fuel=0)


class TestRouting:

    @pytest.fixture(autouse=True)
    def everything_available(self, monkeypatch):
        monkeypatch.setattr(BubblewrapBackend, "available", lambda self: True)
        monkeypatch.setattr(WasmtimeBackend, "available", lambda self: True)

    @pytest.fixture(params=["bubblewrap-first", "wasmtime-first"])
    def registry(self, request):
        backends = [BubblewrapBackend(), WasmtimeBackend()]
        return backends if request.param == "bubblewrap-first" else backends[::-1]

    def test_deterministic_wasm_parser_gets_wasmtime(self, registry) -> None:
        assert select_backend(DETERMINISTIC_WASM, registry).name == "wasmtime"
        assert select_backend(WASM, registry).name == "wasmtime"

    def test_native_lib_parser_gets_bubblewrap(self, registry) -> None:
        req = ParserRequirements(requires_native_libs=True)
        assert select_backend(req, registry).name == "bubblewrap"

    def test_default_process_parser_gets_bubblewrap(self, registry) -> None:
        assert select_backend(ParserRequirements(), registry).name == "bubblewrap"

    @pytest.mark.parametrize("req", [
        ParserRequirements(deterministic=True),                     # no deterministic process host
        ParserRequirements(wasm_module=True, requires_native_libs=True),
        ParserRequirements(wasm_module=True, requires_gpu=True),
    ], ids=["deterministic-process", "wasm-native-libs", "wasm-gpu"])
    def test_unsatisfiable_combinations_are_refused(self, registry, req) -> None:
        with pytest.raises(UnsupportedBackendError) as info:
            select_backend(req, registry)
        assert not isinstance(info.value, SandboxUnavailableError)

    def test_python_parser_never_reaches_wasm_when_bwrap_missing(self, monkeypatch) -> None:
        monkeypatch.setattr(BubblewrapBackend, "available", lambda self: False)
        with pytest.raises(SandboxUnavailableError, match="bubblewrap"):
            select_backend(ParserRequirements(), [BubblewrapBackend(), WasmtimeBackend()])

    def test_wasm_parser_without_wasmtime_is_sandbox_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(WasmtimeBackend, "available", lambda self: False)
        with pytest.raises(SandboxUnavailableError, match="pip install"):
            select_backend(WASM, [BubblewrapBackend(), WasmtimeBackend()])

    def test_extractor_requirements_route_to_wasmtime(self, registry) -> None:
        assert select_backend(WASM_EXTRACTOR_REQUIREMENTS, registry).name == "wasmtime"


# ---------------------------------------------------------------------------
# Phase E proofs, restated for Wasm
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("wasm_backend")
class TestWasmAllowedArtifact:

    def test_artifact_written_to_output_dir(self, tmp_path: Path) -> None:
        result = _probe(tmp_path, "a")
        assert result.succeeded, result.stderr
        assert result.backend == "wasmtime"
        assert [p.name for p in result.artifact_paths] == ["result.json"]
        data = json.loads((result.artifact_dir / "result.json").read_text())
        assert data["parser"] == "wasm_probe_v0"
        assert result.stdout == "wrote result.json\n"

    def test_module_hash_is_recorded(self, tmp_path: Path) -> None:
        result = _probe(tmp_path, "a")
        expected = hashlib.sha256(load_wasm_module(PROBE)).hexdigest()
        assert result.module_sha256 == expected == wasm_module_sha256(PROBE)

    def test_input_is_readable_and_hash_matches(self, tmp_path: Path) -> None:
        payload = b"line one\r\nline two\n\x00\x1a binary tail"
        result = _probe(tmp_path, "r", input_bytes=payload)
        assert result.succeeded, result.stderr
        copied = (result.artifact_dir / "copy.bin").read_bytes()
        assert copied == payload
        assert result.input_sha256 == hashlib.sha256(copied).hexdigest()


@pytest.mark.usefixtures("wasm_backend")
class TestWasmFilesystemIsolation:

    ATTEMPTS = (
        "Y"  # control: create ok.txt in /stele/output
        "N"  # ../escape.txt from the output directory
        "N"  # /etc/passwd (absolute path)
        "N"  # ../../../../../../etc/passwd from the input directory
        "N"  # create a file in the read-only input directory
        "N"  # symlink to a host path
        "Y"  # fd 3 is a preopen (/stele/output)
        "Y"  # fd 4 is a preopen (/stele/input)
        "N"  # fd 5 is not
        "N"  # open through fd 5
        "N"  # truncate the staged input for writing
    )

    def test_nothing_outside_the_preopens_is_reachable(self, tmp_path: Path) -> None:
        result = _probe(tmp_path, "f")
        assert result.succeeded, result.stderr
        assert (result.artifact_dir / "attempts.txt").read_text() == self.ATTEMPTS
        assert not (tmp_path / "escape.txt").exists()
        assert sorted(p.name for p in result.artifact_paths) == ["attempts.txt", "ok.txt"]

    def test_source_input_is_untouched(self, tmp_path: Path) -> None:
        result = _probe(tmp_path, "f", input_bytes=b"original")
        assert result.succeeded, result.stderr
        assert (tmp_path / "out-in" / "sample.txt").read_bytes() == b"original"

    def test_preopen_names(self, tmp_path: Path, wasm_backend) -> None:
        # The environment names the fixed guest paths; nothing else is set.
        result = _probe(tmp_path, "v", env={"FOO": "bar"})
        assert result.succeeded, result.stderr
        environ, argv = (result.artifact_dir / "env.bin").read_bytes().split(b"|")
        assert environ == (
            b"STELE_OUTPUT_DIR=/stele/output\0"
            b"STELE_INPUT_PATH=/stele/input/sample.txt\0"
            b"FOO=bar\0"
        )
        assert argv == b"probe.wat\0v\0sample.txt\0"

    def test_host_environment_is_not_inherited(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("STELE_HOST_SECRET", "leak")
        result = _probe(tmp_path, "v")
        assert result.succeeded, result.stderr
        assert b"STELE_HOST_SECRET" not in (result.artifact_dir / "env.bin").read_bytes()

    def test_unstaged_input_with_siblings_is_refused(self, tmp_path: Path, wasm_backend) -> None:
        (tmp_path / "secret.txt").write_text("sibling")
        (tmp_path / "doc.txt").write_text("doc")
        with pytest.raises(ValueError, match="alone in its directory"):
            wasm_backend.execute(SandboxConfig(
                command=[str(PROBE), "a"], artifact_dir=tmp_path / "o",
                input_path=tmp_path / "doc.txt",
            ))

    def test_extra_read_only_directories_are_preopened_after_input(
        self, tmp_path: Path, wasm_backend
    ) -> None:
        weights = tmp_path / "weights"
        weights.mkdir()
        source = tmp_path / "in" / "sample.txt"
        source.parent.mkdir()
        source.write_text("x")
        result = run_in_sandbox(SandboxConfig(
            command=[str(PROBE), "f", "sample.txt"], artifact_dir=tmp_path / "out",
            input_path=source, extra_ro_binds=[(str(weights), "/weights")],
        ), requirements=WASM)
        attempts = (result.artifact_dir / "attempts.txt").read_text()
        assert attempts[8] == "Y"          # fd 5 is now a preopen
        assert not list(weights.iterdir())  # and nothing was written into it

    def test_extra_read_only_files_are_refused(self, tmp_path: Path, wasm_backend) -> None:
        with pytest.raises(ValueError, match="must be directories"):
            wasm_backend.execute(SandboxConfig(
                command=[str(PROBE), "a"], artifact_dir=tmp_path / "o",
                extra_ro_binds=[(str(PROBE), "/probe.wat")],
            ))

    def test_script_path_is_refused(self, tmp_path: Path, wasm_backend) -> None:
        with pytest.raises(ValueError, match="script_path"):
            wasm_backend.execute(SandboxConfig(
                command=[str(PROBE), "a"], artifact_dir=tmp_path / "o",
                script_path=PROBE,
            ))


@pytest.mark.usefixtures("wasm_backend")
class TestWasmNetworkIsolation:

    def test_every_socket_call_fails(self, tmp_path: Path) -> None:
        result = _probe(tmp_path, "n")
        assert result.succeeded, result.stderr
        # accept/recv/send/shutdown on fds 0-9: no socket exists to use.
        assert (result.artifact_dir / "network.txt").read_text() == "N" * 40

    @pytest.mark.parametrize("module, name", [
        ("wasi:sockets/tcp", "create-tcp-socket"),
        ("wasi_snapshot_preview1", "sock_open"),
        ("env", "connect"),
    ])
    def test_socket_imports_do_not_link(self, tmp_path: Path, module: str, name: str) -> None:
        wat = tmp_path / "net.wat"
        wat.write_text(
            f'(module (import "{module}" "{name}" (func (param i32) (result i32)))'
            ' (memory (export "memory") 1) (func (export "_start")))'
        )
        result = run_in_sandbox(
            SandboxConfig(command=[str(wat)], artifact_dir=tmp_path / "out"),
            requirements=WASM,
        )
        assert result.exit_code == WASM_LOAD_EXIT_CODE
        assert "unknown import" in result.stderr
        assert not result.produced_artifacts


@pytest.mark.usefixtures("wasm_backend")
class TestWasmExitStatusCapture:

    @pytest.mark.parametrize("code", [0, 1, 2, 7])
    def test_exit_code_and_streams_captured(self, tmp_path: Path, code: int) -> None:
        result = _probe(tmp_path, f"x{code}")
        assert result.exit_code == code
        assert result.stdout == f"exiting with code {code}\n"
        assert result.stderr == "diagnostic on stderr\n"

    def test_failed_parser_produces_no_artifacts(self, tmp_path: Path) -> None:
        result = _probe(tmp_path, "x1")
        assert not result.succeeded
        assert not result.produced_artifacts

    def test_unloadable_module_fails_cleanly(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.wasm"
        bad.write_bytes(b"\0asm not really")
        result = run_in_sandbox(
            SandboxConfig(command=[str(bad)], artifact_dir=tmp_path / "out"), requirements=WASM,
        )
        assert result.exit_code == WASM_LOAD_EXIT_CODE
        assert "invalid wasm module" in result.stderr

    def test_missing_module_fails_cleanly(self, tmp_path: Path) -> None:
        result = run_in_sandbox(
            SandboxConfig(command=[str(tmp_path / "absent.wasm")], artifact_dir=tmp_path / "out"),
            requirements=WASM,
        )
        assert result.exit_code == WASM_LOAD_EXIT_CODE
        assert "cannot load wasm module" in result.stderr


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------

class TestWasmLimits:

    def test_memory_growth_stops_at_the_cap_and_traps(self, tmp_path: Path, wasm_backend) -> None:
        limited = _limited(wasm_backend, memory_limit_bytes=4 * MiB)
        result = _probe(tmp_path, "g", backend=limited)
        assert result.exit_code == WASM_TRAP_EXIT_CODE
        assert not result.timed_out
        assert "unreachable" in result.stderr
        # 4 MiB of 64 KiB pages: growth was refused exactly at the cap.
        (pages,) = struct.unpack("<I", (result.artifact_dir / "pages.bin").read_bytes())
        assert pages == 64

    def test_initial_memory_over_the_cap_fails(self, tmp_path: Path, wasm_backend) -> None:
        wat = tmp_path / "big.wat"
        wat.write_text('(module (memory (export "memory") 200) (func (export "_start")))')
        limited = _limited(wasm_backend, memory_limit_bytes=4 * MiB)
        result = run_in_sandbox(
            SandboxConfig(command=[str(wat)], artifact_dir=tmp_path / "out"),
            requirements=WASM, backend=limited,
        )
        assert result.exit_code == WASM_TRAP_EXIT_CODE
        assert "memory limit" in result.stderr

    def test_fuel_exhaustion_is_a_cpu_limit_failure(self, tmp_path: Path, wasm_backend) -> None:
        limited = _limited(wasm_backend, fuel=5_000_000)
        result = _probe(tmp_path, "l", backend=limited)
        assert result.exit_code == WASM_TRAP_EXIT_CODE
        assert not result.timed_out
        assert "CPU limit exceeded" in result.stderr

    def test_fuel_limit_is_deterministic(self, tmp_path: Path, wasm_backend) -> None:
        # The same budget stops the same program at the same instruction, so
        # a run that fits once always fits (unlike a wall-clock limit).
        limited = _limited(wasm_backend, fuel=5_000_000)
        first = _probe(tmp_path, "a", backend=limited, out="one")
        second = _probe(tmp_path, "a", backend=limited, out="two")
        assert first.succeeded and second.succeeded

    def test_wall_clock_timeout_interrupts(self, tmp_path: Path, wasm_backend) -> None:
        unmetered = _limited(wasm_backend, fuel=10**18)
        result = _probe(tmp_path, "l", backend=unmetered, timeout=1)
        assert result.timed_out
        assert result.exit_code == -1
        assert not result.succeeded
        assert "wall-clock timeout" in result.stderr
        assert result.wall_time_seconds < 30


# ---------------------------------------------------------------------------
# Determinism and isolation between runs
# ---------------------------------------------------------------------------

# probe.bin from the "d" mode on the 12-byte input b"hello input\n". The same
# digest must come out on every OS and CPU.
PROBE_DIGEST = "b36b76cba1ef75c73d5bf8dc500659ef53a80077576128c3ff5ba4428e33f4ba"


@pytest.mark.usefixtures("wasm_backend")
class TestWasmDeterminism:

    def test_clock_entropy_and_metadata_are_fixed(self, tmp_path: Path) -> None:
        result = _probe(tmp_path, "d")
        assert result.succeeded, result.stderr
        probe = (result.artifact_dir / "probe.bin").read_bytes()

        def field(offset: int, fmt: str = "<Q"):
            return struct.unpack_from(fmt, probe, offset)[0]

        # errno byte, then value, for each call
        assert probe[0] == 0 and field(1) == FIXED_EPOCH_NS + CLOCK_STEP_NS
        assert probe[9] == 0 and field(10) == FIXED_EPOCH_NS + 2 * CLOCK_STEP_NS
        assert probe[18] == 0 and field(19) == 3 * CLOCK_STEP_NS          # monotonic
        assert probe[27] == 0 and field(28) == CLOCK_STEP_NS              # resolution
        seed = b"stele-wasm-deterministic-entropy-v1"
        assert probe[36] == 0
        assert probe[37:69] == hashlib.sha256(seed + bytes(8)).digest()

        # input file filestat: dev 1, first inode, regular, nlink 1, size 12, fixed times
        assert probe[69] == 0
        dev, ino, ftype, nlink, size, atim, mtim, ctim = struct.unpack_from(
            "<QQB7xQQQQQ", probe, 70
        )
        assert (dev, ino, ftype, nlink, size) == (1, 1, 4, 1, 12)
        assert atim == mtim == ctim == FIXED_EPOCH_NS
        # input directory: directory type, size 0
        dev, ino, ftype, nlink, size, *_ = struct.unpack_from("<QQB7xQQ", probe, 135)
        assert (ftype, nlink, size) == (3, 1, 0)

        # the output listing is sorted by name, whatever order files were made in
        assert probe[199:203] == b"\0\0\0\0"          # three writes succeeded + readdir errno
        used = field(203, "<I")
        listing, pos, names = probe[207:207 + used], 0, []
        while pos < len(listing):
            namlen = struct.unpack_from("<I", listing, pos + 16)[0]
            names.append(listing[pos + 24:pos + 24 + namlen])
            pos += 24 + namlen
        assert names == [b".", b"..", b"a.txt", b"b.txt", b"c"]

        # a file written during the run still shows the fixed timestamps
        after = 207 + used
        assert probe[after] == 0
        assert struct.unpack_from("<Q", probe, after + 1 + 40)[0] == FIXED_EPOCH_NS
        # sleeping is refused (ENOTSUP)
        assert probe[after + 65] == 58
        assert len(probe) == after + 66

    def test_two_runs_are_byte_identical(self, tmp_path: Path) -> None:
        first = _probe(tmp_path, "d", out="one")
        second = _probe(tmp_path, "d", out="two")
        assert first.succeeded and second.succeeded
        assert (first.artifact_dir / "probe.bin").read_bytes() == (
            second.artifact_dir / "probe.bin"
        ).read_bytes()
        assert _bundle_digest(first.artifact_dir, first.artifact_paths) == _bundle_digest(
            second.artifact_dir, second.artifact_paths
        )

    def test_output_matches_across_platforms(self, tmp_path: Path) -> None:
        result = _probe(tmp_path, "d")
        assert result.succeeded, result.stderr
        digest = hashlib.sha256((result.artifact_dir / "probe.bin").read_bytes()).hexdigest()
        assert digest == PROBE_DIGEST

    def test_entropy_seed_is_configurable(self, tmp_path: Path, wasm_backend) -> None:
        default = _probe(tmp_path, "s", out="one")
        reseeded = _probe(tmp_path, "s", out="two",
                          backend=_limited(wasm_backend, entropy_seed=b"other"))
        assert (default.artifact_dir / "state.bin").read_bytes()[5:13] != (
            reseeded.artifact_dir / "state.bin"
        ).read_bytes()[5:13]

    def test_no_state_leaks_between_consecutive_runs(self, tmp_path: Path, wasm_backend) -> None:
        # One backend instance, two runs: globals, memory, clock and entropy
        # all start fresh, so the second run sees exactly what the first did.
        runs = [_probe(tmp_path, "s", backend=wasm_backend, out=f"run{i}") for i in range(2)]
        states = [(r.artifact_dir / "state.bin").read_bytes() for r in runs]
        assert all(r.succeeded for r in runs)
        assert states[0] == states[1]
        assert states[0][:5] == b"\x01\x00\x00\x00\x01"   # counter 1, memory cell 1

    def test_earlier_output_is_invisible_to_later_runs(self, tmp_path: Path) -> None:
        first = _probe(tmp_path, "d", out="one")
        second = _probe(tmp_path, "d", out="two")
        # Each run lists only its own output directory (".", "..", a, b, c).
        for result in (first, second):
            assert sorted(p.name for p in result.artifact_paths) == [
                "a.txt", "b.txt", "c", "probe.bin",
            ]


# ---------------------------------------------------------------------------
# First real extractor: ChatGPT export splitter
# ---------------------------------------------------------------------------

def _export() -> bytes:
    """A small but realistic conversations.json (built in code, so the bytes
    are identical on every OS regardless of git line-ending settings)."""
    conversations = [
        {
            "title": "Bracket [soup] {inside} strings",
            "create_time": 1700000000.123,
            "mapping": {
                "node-1": {"message": {"author": {"role": "user"},
                                       "content": {"parts": ["What is \"quoted\" \\ here? ]}"]}},
                           "children": ["node-2"]},
                "node-2": {"message": {"author": {"role": "assistant"},
                                       "content": {"parts": ["Nested: [[1, 2], {\"a\": []}]"]}},
                           "children": []},
            },
        },
        {
            "title": "Unicode — naïve café ☕ 𝄞",
            "create_time": 1700000100.0,
            "mapping": {},
        },
        {"title": "", "mapping": {"n": {"message": None, "children": []}}},
    ]
    return (json.dumps(conversations, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# Digest of the split bundle (file names and bytes) for _export().
SPLIT_DIGEST = "81f5d92a5878060d1e8c3010204829eebddaf94825e71d70341b7f9926cdac71"


@pytest.mark.usefixtures("wasm_backend")
class TestChatGPTExportSplitter:

    def _split(self, tmp_path: Path, data: bytes, out: str = "out"):
        source = tmp_path / f"{out}-src" / "conversations.json"
        source.parent.mkdir()
        source.write_bytes(data)
        return run_in_sandbox(
            chatgpt_export_split_config(source, tmp_path / out),
            requirements=WASM_EXTRACTOR_REQUIREMENTS,
        )

    def test_splits_each_conversation_byte_for_byte(self, tmp_path: Path) -> None:
        data = _export()
        result = self._split(tmp_path, data)
        assert result.succeeded, result.stderr
        assert result.backend == "wasmtime"
        assert result.stdout == "split 3 conversations\n"
        assert result.module_sha256 == wasm_module_sha256(CHATGPT_EXPORT_SPLIT)

        index = [json.loads(line) for line in
                 (result.artifact_dir / "index.jsonl").read_text().splitlines()]
        assert [e["path"] for e in index] == [
            "conversation-000000.json", "conversation-000001.json", "conversation-000002.json",
        ]
        originals = json.loads(data)
        for entry, original in zip(index, originals):
            piece = (result.artifact_dir / entry["path"]).read_bytes()
            assert piece == data[entry["offset"]:entry["offset"] + entry["length"]]
            assert json.loads(piece) == original
        assert sorted(p.name for p in result.artifact_paths) == sorted(
            ["index.jsonl", *(e["path"] for e in index)]
        )

    def test_output_is_byte_identical_across_runs_and_platforms(self, tmp_path: Path) -> None:
        first = self._split(tmp_path, _export(), out="one")
        second = self._split(tmp_path, _export(), out="two")
        digests = [_bundle_digest(r.artifact_dir, r.artifact_paths) for r in (first, second)]
        assert digests[0] == digests[1] == SPLIT_DIGEST

    def test_compact_and_empty_exports(self, tmp_path: Path) -> None:
        compact = self._split(tmp_path, b'[{"a":1},{"b":[{}]}]', out="compact")
        assert compact.succeeded, compact.stderr
        assert (compact.artifact_dir / "conversation-000001.json").read_bytes() == b'{"b":[{}]}'
        empty = self._split(tmp_path, b" [ ] \n", out="empty")
        assert empty.succeeded, empty.stderr
        assert empty.stdout == "split 0 conversations\n"
        assert (empty.artifact_dir / "index.jsonl").read_bytes() == b""

    def test_large_export_streams_across_buffer_boundaries(self, tmp_path: Path) -> None:
        conversations = [{"i": i, "text": "x\\\"]}" * (i * 37 % 3000)} for i in range(200)]
        data = json.dumps(conversations).encode()
        assert len(data) > 3 * 65536
        result = self._split(tmp_path, data)
        assert result.succeeded, result.stderr
        index = [json.loads(line) for line in
                 (result.artifact_dir / "index.jsonl").read_text().splitlines()]
        assert len(index) == 200
        for entry, original in zip(index, conversations):
            piece = (result.artifact_dir / entry["path"]).read_bytes()
            assert json.loads(piece) == original

    @pytest.mark.parametrize("data, message", [
        (b'{"a": 1}', "not a JSON array"),
        (b"[1, 2]", "expected a JSON object"),
        (b'[{"a":1},]', "expected a JSON object"),
        (b'[{"a":[1}]]', "mismatched bracket"),
        (b'[{"a":1}', "before the JSON array is closed"),
        (b'[{"a":"unterminated}]', "before the JSON array is closed"),
        (b'[{"a":1} {"b":2}]', "expected ',' or ']'"),
        (b'[{"a":1}] trailing', "unexpected data after"),
        (b"", "before the JSON array is closed"),
        (b"[" + b'{"a":' * 5000 + b"1", "nested more than 4096"),
    ], ids=["object", "scalars", "trailing-comma", "mismatch", "truncated",
            "open-string", "missing-comma", "trailing-data", "empty", "too-deep"])
    def test_malformed_exports_fail(self, tmp_path: Path, data: bytes, message: str) -> None:
        result = self._split(tmp_path, data)
        assert result.exit_code == 2
        assert message in result.stderr

    def test_missing_input_fails(self, tmp_path: Path) -> None:
        result = run_in_sandbox(
            SandboxConfig(command=[str(CHATGPT_EXPORT_SPLIT)], artifact_dir=tmp_path / "out"),
            requirements=WASM_EXTRACTOR_REQUIREMENTS,
        )
        assert result.exit_code == 1
        assert "STELE_INPUT_PATH" in result.stderr


def test_wat_modules_ship_as_text() -> None:
    # Reviewable sources; the recorded identity is the compiled binary's hash.
    for module in (PROBE, CHATGPT_EXPORT_SPLIT):
        assert not module.read_bytes().startswith(b"\0asm")
    assert os.path.getsize(CHATGPT_EXPORT_SPLIT) > 0


class TestHandlesReleased:
    """Preopened directories must be closed when a run ends, however it ends.

    Windows cannot delete a directory that is still open, so a leaked WASI
    handle makes run_in_sandbox fail while removing the staging directory.
    """

    @staticmethod
    def _open_under(root: Path) -> int:
        count = 0
        for fd in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{fd}").startswith(str(root)):
                    count += 1
            except OSError:
                pass
        return count

    @pytest.mark.parametrize("mode,limits,timeout", [
        ("l", {"fuel": 10**18}, 1),   # wall-clock interrupt trap
        ("l", {"fuel": 10**6}, 30),   # out-of-fuel trap
        ("w", {}, 30),                # normal exit
    ])
    def test_no_directory_handle_outlives_the_run(
        self, tmp_path: Path, wasm_backend, mode, limits, timeout
    ) -> None:
        # Fails on Windows (via staging cleanup) if a handle leaks; on Linux
        # the /proc check below catches the same leak directly.
        result = _probe(tmp_path, mode, backend=_limited(wasm_backend, **limits), timeout=timeout)
        assert result.exit_code != 0 or mode == "w"
        if os.path.isdir("/proc/self/fd"):
            assert self._open_under(tmp_path) == 0
