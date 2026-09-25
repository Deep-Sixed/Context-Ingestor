"""
Roadmap #16 — snapshot archive and content-addressed evidence store.

  - Blobs round-trip, are addressed by the SHA-256 the store computes, and are
    never overwritten; identical writes are no-ops.
  - Every read re-verifies; corruption is an IntegrityError, never served.
  - An interrupted publish (exception or hard kill) never leaves a partial
    blob under its final digest.
  - Concurrent identical writes are safe.
  - A run's staged input becomes a Snapshot whose digest is the hash the
    parser saw; directory Snapshots are tree objects whose digest is the
    manifest digest staging and the ledger already use.
  - Artifacts from a run are stored and retrievable by digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from stele.archive import (
    ArchiveError,
    BlobStore,
    IntegrityError,
    InvalidDigestError,
    MissingObjectError,
    RecordFormatError,
    Snapshot,
    SnapshotKind,
    Source,
    decode_tree,
    ingest_artifacts,
    materialize_snapshot,
    snapshot_staged_input,
)
from stele.containment.artifacts import collect_artifact_paths
from stele.containment.backend import (
    Capability,
    ExecutionOutcome,
    SandboxBackend,
)
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig
from stele.containment.staging import stage_input
from stele.ledger.hashing import UnsafeFileError, build_manifest, encode_manifest, sha256_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(Path(sys.executable).resolve())
BASE = {Capability.FILESYSTEM_ISOLATION, Capability.NETWORK_ISOLATION}

requires_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="live containment proof requires Linux bubblewrap",
)
requires_posix_staging = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fwalk"),
    reason="secure staging requires O_NOFOLLOW and os.fwalk",
)
requires_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks needs extra privileges on Windows"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_file(store: BlobStore, digest: str) -> Path:
    return store.root / "blobs" / digest[:2] / digest[2:]


def tmp_entries(store: BlobStore) -> list[Path]:
    return list((store.root / "tmp").iterdir())


def overwrite(path: Path, data: bytes) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    path.write_bytes(data)


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "archive")


# ---------------------------------------------------------------------------
# Blob store basics
# ---------------------------------------------------------------------------

class TestBlobStore:

    def test_round_trip_returns_identical_bytes(self, store: BlobStore) -> None:
        data = bytes(range(256)) * 1000
        digest = store.put_bytes(data)
        assert digest == sha(data)
        assert store.has(digest)
        assert store.read(digest) == data
        assert store.size(digest) == len(data)

    def test_layout_is_sharded_by_digest(self, store: BlobStore) -> None:
        digest = store.put_bytes(b"sharded")
        assert blob_file(store, digest).is_file()
        assert (store.root / "stele-archive.json").is_file()
        assert tmp_entries(store) == []

    def test_empty_blob(self, store: BlobStore) -> None:
        digest = store.put_bytes(b"")
        assert digest == sha(b"")
        assert store.read(digest) == b""

    def test_identical_write_is_a_noop(self, store: BlobStore) -> None:
        first = store.put_bytes(b"same")
        mtime = os.stat(blob_file(store, first)).st_mtime_ns
        assert store.put_bytes(b"same") == first
        assert os.stat(blob_file(store, first)).st_mtime_ns == mtime
        assert tmp_entries(store) == []

    def test_put_file_and_stream(self, store: BlobStore, tmp_path: Path) -> None:
        f = tmp_path / "doc.bin"
        f.write_bytes(b"file bytes\r\n")  # CRLF must survive on Windows
        assert store.put_file(f) == sha(b"file bytes\r\n")
        with f.open("rb") as fh:
            assert store.put_stream(fh) == sha(b"file bytes\r\n")

    @requires_symlinks
    def test_put_file_refuses_symlink(self, store: BlobStore, tmp_path: Path) -> None:
        target = tmp_path / "t"
        target.write_bytes(b"x")
        link = tmp_path / "l"
        link.symlink_to(target)
        with pytest.raises(UnsafeFileError):
            store.put_file(link)

    def test_expected_digest_mismatch_publishes_nothing(self, store: BlobStore) -> None:
        wrong = sha(b"something else")
        with pytest.raises(IntegrityError, match="expected"):
            store.put_bytes(b"actual", expected_digest=wrong)
        assert not store.has(sha(b"actual"))
        assert not store.has(wrong)
        assert tmp_entries(store) == []

    def test_digest_is_validated_before_touching_paths(self, store: BlobStore) -> None:
        for bad in ["../../etc/passwd", "ABC", "a" * 63, "A" * 64, "g" * 64, ""]:
            with pytest.raises(InvalidDigestError):
                store.read(bad)
            with pytest.raises(InvalidDigestError):
                store.has(bad)

    def test_missing_blob(self, store: BlobStore) -> None:
        with pytest.raises(MissingObjectError):
            store.read(sha(b"never stored"))
        with pytest.raises(MissingObjectError):
            list(store.iter_verified(sha(b"never stored")))

    def test_no_deletion_api(self) -> None:
        public = {name for name in dir(BlobStore) if not name.startswith("_")}
        assert not public & {"delete", "remove", "purge", "gc", "collect_garbage", "unlink"}

    def test_reopen_existing_store(self, store: BlobStore) -> None:
        digest = store.put_bytes(b"persist")
        assert BlobStore(store.root).read(digest) == b"persist"

    def test_refuses_foreign_directory(self, tmp_path: Path) -> None:
        (tmp_path / "junk.txt").write_text("not an archive")
        with pytest.raises(ArchiveError, match="not a Stele archive"):
            BlobStore(tmp_path)

    def test_refuses_unknown_layout_version(self, store: BlobStore) -> None:
        marker = store.root / "stele-archive.json"
        overwrite(marker, b'{"format":"stele.archive","layout_version":99}')
        with pytest.raises(ArchiveError, match="layout 99"):
            BlobStore(store.root)


# ---------------------------------------------------------------------------
# Integrity on read
# ---------------------------------------------------------------------------

class TestIntegrity:

    def _corrupt(self, store: BlobStore, digest: str) -> Path:
        path = blob_file(store, digest)
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0x01
        overwrite(path, bytes(data))
        return path

    def test_flipped_byte_is_detected_on_read(self, store: BlobStore) -> None:
        digest = store.put_bytes(b"evidence " * 100)
        self._corrupt(store, digest)
        with pytest.raises(IntegrityError, match="corrupt"):
            store.read(digest)
        with pytest.raises(IntegrityError):
            list(store.iter_verified(digest))

    def test_truncation_is_detected(self, store: BlobStore) -> None:
        digest = store.put_bytes(b"evidence " * 100)
        path = blob_file(store, digest)
        overwrite(path, path.read_bytes()[:10])
        with pytest.raises(IntegrityError):
            store.read(digest)

    def test_export_never_leaves_corrupt_bytes(self, store: BlobStore, tmp_path: Path) -> None:
        digest = store.put_bytes(b"x" * 200_000)
        self._corrupt(store, digest)
        dest = tmp_path / "out.bin"
        with pytest.raises(IntegrityError):
            store.export(digest, dest)
        assert not dest.exists()
        assert list(tmp_path.glob(".stele-*")) == []

    def test_export_round_trip_and_refuses_overwrite(self, store: BlobStore, tmp_path: Path) -> None:
        digest = store.put_bytes(b"exported")
        dest = tmp_path / "out.bin"
        store.export(digest, dest)
        assert dest.read_bytes() == b"exported"
        with pytest.raises(FileExistsError):
            store.export(digest, dest)

    def test_rewriting_over_corruption_is_refused(self, store: BlobStore) -> None:
        digest = store.put_bytes(b"original evidence")
        path = self._corrupt(store, digest)
        tampered = path.read_bytes()
        with pytest.raises(IntegrityError, match="refusing to overwrite"):
            store.put_bytes(b"original evidence")
        # The damaged object is left in place as evidence, not silently repaired.
        assert path.read_bytes() == tampered

    @requires_symlinks
    def test_symlink_in_place_of_blob_is_refused(self, store: BlobStore, tmp_path: Path) -> None:
        digest = store.put_bytes(b"real")
        decoy = tmp_path / "decoy"
        decoy.write_bytes(b"real")
        path = blob_file(store, digest)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        path.unlink()
        path.symlink_to(decoy)
        with pytest.raises(IntegrityError, match="not a regular file"):
            store.read(digest)


# ---------------------------------------------------------------------------
# Crash injection and concurrency
# ---------------------------------------------------------------------------

class TestAtomicPublish:

    def test_failed_rename_leaves_no_blob(self, store: BlobStore, monkeypatch) -> None:
        data = b"interrupted"

        def crash(src, dst):
            raise OSError("injected crash during rename")

        monkeypatch.setattr(os, "replace", crash)
        with pytest.raises(OSError, match="injected"):
            store.put_bytes(data)
        monkeypatch.undo()

        assert not blob_file(store, sha(data)).exists()
        assert not store.has(sha(data))
        assert tmp_entries(store) == []
        # A retry publishes normally.
        assert store.read(store.put_bytes(data)) == data

    def test_failure_mid_write_leaves_no_blob(self, store: BlobStore) -> None:
        def chunks():
            yield b"first half "
            raise RuntimeError("injected crash mid-write")

        with pytest.raises(RuntimeError, match="injected"):
            store.put_chunks(chunks())
        assert list((store.root / "blobs").iterdir()) == []
        assert tmp_entries(store) == []

    def test_failed_fsync_leaves_no_blob(self, store: BlobStore, monkeypatch) -> None:
        def crash(fd):
            raise OSError("injected fsync failure")

        monkeypatch.setattr(os, "fsync", crash)
        with pytest.raises(OSError, match="injected"):
            store.put_bytes(b"not durable")
        monkeypatch.undo()
        assert not store.has(sha(b"not durable"))
        assert tmp_entries(store) == []

    @pytest.mark.parametrize("kill_at", ["fsync", "replace"])
    def test_hard_kill_never_exposes_partial_blob(self, store: BlobStore, kill_at: str) -> None:
        """The process dies with no chance to clean up: only tmp/ may hold debris."""
        data = b"A" * 300_000
        script = (
            "import os, sys\n"
            "from stele.archive import BlobStore\n"
            "store = BlobStore(sys.argv[1])\n"
            f"os.{kill_at} = lambda *a, **k: os._exit(17)\n"
            "store.put_bytes(b'A' * 300_000)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        proc = subprocess.run([PYTHON, "-c", script, str(store.root)], env=env)
        assert proc.returncode == 17

        digest = sha(data)
        assert not blob_file(store, digest).exists()
        assert not store.has(digest)
        with pytest.raises(MissingObjectError):
            store.read(digest)
        assert len(tmp_entries(store)) == 1  # the orphan, which is never read
        # The store keeps working and the retry is complete and verified.
        assert store.read(store.put_bytes(data)) == data

    def test_concurrent_identical_writes(self, store: BlobStore) -> None:
        data = os.urandom(1 << 20)
        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(12)

        def writer() -> None:
            try:
                barrier.wait()
                results.append(store.put_bytes(data))
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert results == [sha(data)] * 12
        assert store.read(sha(data)) == data
        shard = blob_file(store, sha(data)).parent
        assert [p.name for p in shard.iterdir()] == [sha(data)[2:]]
        assert tmp_entries(store) == []

    def test_concurrent_writes_from_processes(self, store: BlobStore) -> None:
        script = (
            "import sys\n"
            "from stele.archive import BlobStore\n"
            "print(BlobStore(sys.argv[1]).put_bytes(b'shared' * 50_000))\n"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        procs = [
            subprocess.Popen([PYTHON, "-c", script, str(store.root)], env=env,
                             stdout=subprocess.PIPE, text=True)
            for _ in range(6)
        ]
        outputs = {p.communicate()[0].strip() for p in procs}
        assert all(p.returncode == 0 for p in procs)
        assert outputs == {sha(b"shared" * 50_000)}
        assert store.read(sha(b"shared" * 50_000)) == b"shared" * 50_000


# ---------------------------------------------------------------------------
# Trees and records
# ---------------------------------------------------------------------------

class TestTreesAndRecords:

    def test_tree_digest_is_the_manifest_digest(self, store: BlobStore) -> None:
        manifest = {
            "b/c.txt": store.put_bytes(b"c"),
            "a.txt": store.put_bytes(b"a"),
        }
        digest = store.put_tree(manifest)
        assert digest == sha256_manifest(manifest) == sha(encode_manifest(manifest))
        assert store.read_tree(digest) == manifest

    def test_tree_requires_stored_blobs(self, store: BlobStore) -> None:
        with pytest.raises(MissingObjectError):
            store.put_tree({"a.txt": sha(b"absent")})

    @pytest.mark.parametrize("path", ["../x", "/abs", "a//b", "./a", "a/../b", ""])
    def test_tree_refuses_unsafe_paths(self, store: BlobStore, path: str) -> None:
        with pytest.raises(ValueError):
            store.put_tree({path: store.put_bytes(b"x")})

    def test_decode_tree_is_strict(self) -> None:
        d = sha(b"x")
        good = encode_manifest({"a": d, "b": d})
        assert decode_tree(good) == {"a": d, "b": d}
        unsorted = b"b\x00" + d.encode() + b"\x00a\x00" + d.encode() + b"\x00"
        for bad in [
            unsorted,
            good[:-1],                                   # truncated
            b"a\x00" + b"z" * 64 + b"\x00",              # bad digest
            b"../a\x00" + d.encode() + b"\x00",          # traversal
            encode_manifest({"a": d, "a/b": d}),         # file and directory
        ]:
            with pytest.raises(RecordFormatError):
                decode_tree(bad)

    def test_snapshot_canonical_serialization(self) -> None:
        snap = Snapshot(SnapshotKind.FILE, sha(b"x"), 1, 1)
        data = snap.to_canonical()
        assert data == (
            b'{"digest":"' + sha(b"x").encode() + b'","file_count":1,"kind":"file",'
            b'"schema":"stele.snapshot","schema_version":1,"size":1}'
        )
        assert Snapshot.from_canonical(data) == snap

    def test_snapshot_parsing_is_strict(self) -> None:
        snap = Snapshot(SnapshotKind.TREE, sha(b"x"), 3, 2)
        obj = json.loads(snap.to_canonical())
        for mutate in [
            lambda o: o.update(schema_version=2),
            lambda o: o.update(schema="stele.other"),
            lambda o: o.update(extra=1),
            lambda o: o.update(size=-1),
            lambda o: o.update(kind="symlink"),
            lambda o: o.update(digest="../../x"),
        ]:
            bad = dict(obj)
            mutate(bad)
            with pytest.raises(RecordFormatError):
                Snapshot.from_canonical(json.dumps(bad, sort_keys=True, separators=(",", ":")).encode())
        with pytest.raises(RecordFormatError, match="canonical"):
            Snapshot.from_canonical(json.dumps(obj, indent=1).encode())

    def test_source_identity(self, store: BlobStore) -> None:
        src = Source.from_path("C:/corpus/doc.pdf")
        assert src.to_canonical() == (
            b'{"locator":"C:/corpus/doc.pdf","schema":"stele.source","schema_version":1}'
        )
        source_id = store.put_source(src)
        assert source_id == src.source_id == sha(src.to_canonical())
        assert store.put_source(src) == source_id
        assert store.get_source(source_id) == src

    def test_snapshot_record_requires_content(self, store: BlobStore) -> None:
        with pytest.raises(MissingObjectError):
            store.put_snapshot(Snapshot(SnapshotKind.FILE, sha(b"absent"), 6, 1))
        digest = store.put_bytes(b"sized")
        with pytest.raises(IntegrityError, match="size"):
            store.put_snapshot(Snapshot(SnapshotKind.FILE, digest, 999, 1))

    def test_tampered_snapshot_record_is_detected(self, store: BlobStore) -> None:
        digest = store.put_bytes(b"content")
        store.put_snapshot(Snapshot(SnapshotKind.FILE, digest, 7, 1))
        record = store.root / "meta" / "snapshots" / "file" / digest[:2] / digest[2:]
        other = Snapshot(SnapshotKind.FILE, sha(b"other"), 7, 1)
        overwrite(record, other.to_canonical())
        with pytest.raises(IntegrityError, match="another snapshot"):
            store.get_snapshot(digest, SnapshotKind.FILE)
        # And re-recording the true snapshot does not overwrite the evidence.
        with pytest.raises(IntegrityError, match="conflicting"):
            store.put_snapshot(Snapshot(SnapshotKind.FILE, digest, 7, 1))


# ---------------------------------------------------------------------------
# Staged input -> Snapshot
# ---------------------------------------------------------------------------

def _tree(root: Path) -> dict[str, bytes]:
    files = {"a.txt": b"alpha", "sub/b.json": b"{}", "sub/deeper/c.md": b"# c"}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return files


@requires_posix_staging
class TestSnapshots:

    def test_file_snapshot(self, store: BlobStore, tmp_path: Path) -> None:
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"%PDF-1.7")
        staged = stage_input(src, tmp_path / "stage")
        snap = snapshot_staged_input(store, staged)
        assert snap == Snapshot(SnapshotKind.FILE, staged.sha256, 8, 1)
        assert store.get_snapshot(snap.digest, "file") == snap
        assert store.read(snap.digest) == b"%PDF-1.7"

    def test_directory_snapshot_digest_equals_staged_hash(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "corpus"
        files = _tree(src)
        staged = stage_input(src, tmp_path / "stage")
        snap = snapshot_staged_input(store, staged)

        assert snap.kind is SnapshotKind.TREE
        assert snap.digest == staged.sha256 == sha256_manifest(staged.manifest)
        assert snap.file_count == 3 and snap.size == sum(len(d) for d in files.values())
        tree = store.read_tree(snap.digest)
        assert {rel: store.read(d) for rel, d in tree.items()} == files
        assert store.get_snapshot(snap.digest, SnapshotKind.TREE) == snap

    def test_materialize_round_trip(self, store: BlobStore, tmp_path: Path) -> None:
        src = tmp_path / "corpus"
        files = _tree(src)
        snap = snapshot_staged_input(store, stage_input(src, tmp_path / "stage"))
        out = tmp_path / "replayed"
        materialize_snapshot(store, snap, out)
        assert {p.relative_to(out).as_posix(): p.read_bytes()
                for p in out.rglob("*") if p.is_file()} == files
        # Materializing the tree reproduces the same Snapshot digest.
        assert stage_input(out, tmp_path / "stage2").sha256 == snap.digest

    def test_empty_file_and_empty_directory_do_not_collide(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        (tmp_path / "empty.txt").write_bytes(b"")
        (tmp_path / "emptydir").mkdir()
        f = snapshot_staged_input(store, stage_input(tmp_path / "empty.txt", tmp_path / "s1"))
        d = snapshot_staged_input(store, stage_input(tmp_path / "emptydir", tmp_path / "s2"))
        assert f.digest == d.digest == sha(b"")
        assert store.get_snapshot(f.digest, "file").kind is SnapshotKind.FILE
        assert store.get_snapshot(d.digest, "tree").kind is SnapshotKind.TREE

    def test_snapshot_refuses_bytes_that_differ_from_staging(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "doc.txt"
        src.write_bytes(b"original")
        staged = stage_input(src, tmp_path / "stage")
        staged.staged_path.write_bytes(b"swapped")
        with pytest.raises(IntegrityError):
            snapshot_staged_input(store, staged)
        assert not store.has_snapshot(staged.sha256, "file")


# ---------------------------------------------------------------------------
# Runs: input snapshot + artifacts
# ---------------------------------------------------------------------------

class ProbeBackend(SandboxBackend):
    """Stands in for a parser: hashes the input it is given, emits artifacts."""

    name = "probe"

    def capabilities(self):
        return frozenset(BASE)

    def available(self):
        return True

    def unavailable_reason(self):
        return ""

    def execute(self, config):
        out = config.artifact_dir
        out.mkdir(parents=True, exist_ok=True)
        seen: dict[str, str] = {}
        if config.input_path is not None:
            p = config.input_path
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        seen[f.relative_to(p).as_posix()] = sha(f.read_bytes())
            else:
                seen["file"] = sha(p.read_bytes())
        (out / "seen.json").write_text(json.dumps(seen, sort_keys=True))
        (out / "chunks").mkdir()
        (out / "chunks" / "0001.txt").write_bytes(b"chunk one")
        return ExecutionOutcome(exit_code=0, stdout="", stderr="", wall_time_seconds=0.0)


class TestRunIntegration:

    def test_without_store_nothing_changes(self, tmp_path: Path) -> None:
        result = run_in_sandbox(
            SandboxConfig(command=["x"], artifact_dir=tmp_path / "o"), backend=ProbeBackend()
        )
        assert result.input_snapshot is None
        assert result.artifact_digests == {}
        assert result.artifact_bundle_digest is None

    def test_artifacts_are_stored_and_retrievable(self, store: BlobStore, tmp_path: Path) -> None:
        out = tmp_path / "o"
        result = run_in_sandbox(
            SandboxConfig(command=["x"], artifact_dir=out), backend=ProbeBackend(), store=store
        )
        assert set(result.artifact_digests) == {"seen.json", "chunks/0001.txt"}
        assert store.read(result.artifact_digests["chunks/0001.txt"]) == b"chunk one"
        # Same manifest the ledger builds, and the bundle is the ledger's artifact_hash.
        assert result.artifact_digests == build_manifest(out, result.artifact_paths)
        assert result.artifact_bundle_digest == sha256_manifest(result.artifact_digests)
        assert store.read_tree(result.artifact_bundle_digest) == result.artifact_digests
        # Evidence outlives the working directory.
        shutil.rmtree(out)
        assert store.read(result.artifact_digests["seen.json"]) == b"{}"

    @requires_posix_staging
    def test_file_input_snapshot_is_what_the_parser_saw(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"%PDF-1.7 payload")
        out = tmp_path / "o"
        result = run_in_sandbox(
            SandboxConfig(command=["x"], artifact_dir=out, input_path=src),
            backend=ProbeBackend(), store=store,
        )
        seen = json.loads((out / "seen.json").read_text())["file"]
        assert result.input_snapshot is not None
        assert result.input_snapshot.digest == seen == result.input_sha256
        src.write_bytes(b"edited after the run")
        assert sha(store.read(seen)) == seen
        assert store.read(seen) == b"%PDF-1.7 payload"

    @requires_posix_staging
    def test_directory_input_snapshot_is_what_the_parser_saw(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "corpus"
        files = _tree(src)
        out = tmp_path / "o"
        result = run_in_sandbox(
            SandboxConfig(command=["x"], artifact_dir=out, input_path=src),
            backend=ProbeBackend(), store=store,
        )
        seen = json.loads((out / "seen.json").read_text())
        snap = result.input_snapshot
        assert snap is not None and snap.kind is SnapshotKind.TREE
        assert snap.digest == sha256_manifest(seen) == result.input_sha256
        assert store.read_tree(snap.digest) == seen
        assert {rel: store.read(d) for rel, d in seen.items()} == files

    @requires_symlinks
    def test_symlinked_artifact_is_never_ingested(self, store: BlobStore, tmp_path: Path) -> None:
        out = tmp_path / "o"
        out.mkdir()
        secret = tmp_path / "secret"
        secret.write_bytes(b"host secret")
        (out / "leak").symlink_to(secret)
        with pytest.raises(UnsafeFileError):
            ingest_artifacts(store, out, [out / "leak"])
        assert not store.has(sha(b"host secret"))

    @requires_symlinks
    def test_symlinked_artifact_parent_is_refused(self, store: BlobStore, tmp_path: Path) -> None:
        out = tmp_path / "o"
        out.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "f").write_bytes(b"host file")
        (out / "d").symlink_to(outside, target_is_directory=True)
        with pytest.raises(UnsafeFileError):
            ingest_artifacts(store, out, [out / "d" / "f"])
        assert not store.has(sha(b"host file"))

    def test_ingest_through_lstat_fallback(
        self, store: BlobStore, tmp_path: Path, monkeypatch
    ) -> None:
        """The Windows no-follow path ingests the same bytes and digests."""
        from stele.ledger import hashing

        monkeypatch.setattr(hashing, "RACE_FREE_NOFOLLOW", False)
        out = tmp_path / "o"
        (out / "d").mkdir(parents=True)
        (out / "d" / "f.txt").write_bytes(b"fallback")
        manifest = ingest_artifacts(store, out, collect_artifact_paths(out))
        assert manifest == {"d/f.txt": sha(b"fallback")}
        assert store.read(manifest["d/f.txt"]) == b"fallback"

    def test_artifact_outside_dir_is_refused(self, store: BlobStore, tmp_path: Path) -> None:
        out = tmp_path / "o"
        out.mkdir()
        other = tmp_path / "other.txt"
        other.write_bytes(b"x")
        with pytest.raises(ValueError, match="outside"):
            ingest_artifacts(store, out, [other])


# ---------------------------------------------------------------------------
# Live bubblewrap
# ---------------------------------------------------------------------------

@requires_bwrap
class TestLiveArchive:

    PROBE = (
        "import hashlib, json, os\n"
        "base = os.environ['STELE_INPUT_PATH']\n"
        "out = os.environ['STELE_OUTPUT_DIR']\n"
        "seen = {}\n"
        "if os.path.isdir(base):\n"
        "    for root, _, files in os.walk(base):\n"
        "        for f in files:\n"
        "            p = os.path.join(root, f)\n"
        "            seen[os.path.relpath(p, base)] = hashlib.sha256(open(p, 'rb').read()).hexdigest()\n"
        "else:\n"
        "    seen['file'] = hashlib.sha256(open(base, 'rb').read()).hexdigest()\n"
        "open(os.path.join(out, 'seen.json'), 'w').write(json.dumps(seen))\n"
        "os.makedirs(os.path.join(out, 'chunks'))\n"
        "open(os.path.join(out, 'chunks', '1.txt'), 'wb').write(b'parsed')\n"
    )

    def test_file_input_snapshot_matches_parser(self, store: BlobStore, tmp_path: Path) -> None:
        src = tmp_path / "note.md"
        src.write_bytes(b"# staged bytes")
        out = tmp_path / "out"
        result = run_in_sandbox(
            SandboxConfig(command=[PYTHON, "-c", self.PROBE], artifact_dir=out, input_path=src),
            store=store,
        )
        assert result.succeeded, result.stderr
        assert result.backend == "bubblewrap"
        seen = json.loads((out / "seen.json").read_text())["file"]
        assert result.input_snapshot.digest == seen == sha(b"# staged bytes")
        assert store.read(seen) == b"# staged bytes"
        assert store.read(result.artifact_digests["chunks/1.txt"]) == b"parsed"
        assert result.artifact_digests == build_manifest(out, collect_artifact_paths(out))

    def test_directory_input_snapshot_matches_parser(self, store: BlobStore, tmp_path: Path) -> None:
        src = tmp_path / "corpus"
        files = _tree(src)
        out = tmp_path / "out"
        result = run_in_sandbox(
            SandboxConfig(command=[PYTHON, "-c", self.PROBE], artifact_dir=out, input_path=src),
            store=store,
        )
        assert result.succeeded, result.stderr
        seen = json.loads((out / "seen.json").read_text())
        assert result.input_snapshot.digest == sha256_manifest(seen) == result.input_sha256
        assert {rel: store.read(d) for rel, d in store.read_tree(result.input_snapshot.digest).items()} == files
