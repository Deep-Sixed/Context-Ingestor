"""Immutable, content-addressed blob store (roadmap #16).

Layout under the store root (every key uses POSIX separators on every OS)::

    stele-archive.json                 format marker and layout version
    blobs/ab/cdef...                   blob whose SHA-256 is "abcdef..."
    meta/snapshots/<kind>/ab/cdef...   Snapshot record for (kind, "abcdef...")
    meta/sources/ab/cdef...            Source record whose source_id is "abcdef..."
    tmp/                               in-flight writes; never read as evidence

Guarantees:

- Blobs are addressed by the SHA-256 of their bytes, computed by the store
  while it writes them. Callers never supply a digest that is trusted.
- Nothing is overwritten. Writing content that is already present is a no-op
  (after re-verifying the stored copy); a conflicting metadata record raises.
- Publishing is atomic: bytes go to a temp file in ``tmp/``, are hashed while
  written, fsynced, then renamed into place with os.replace, and the shard
  directory is fsynced where the platform supports it (not on Windows). A
  crash at any point leaves at most an orphaned temp file, never a partial
  object under a final name.
- Every read re-hashes the bytes and raises IntegrityError on mismatch. A read
  returns data only after the whole blob has been verified.
- There is no deletion or garbage collection API. Purging evidence is a later,
  explicit, evidence-recorded operation; until then nothing is ever removed.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator

from ..ledger.hashing import UnsafeFileError, encode_manifest, open_regular_file
from .records import (
    RecordFormatError,
    Snapshot,
    SnapshotKind,
    Source,
    canonical_json,
    is_digest,
)

FORMAT_NAME = "stele.archive"
LAYOUT_VERSION = 1
_MARKER = "stele-archive.json"
_CHUNK = 1 << 16
_LAYOUT_NAMES = {_MARKER, "blobs", "meta", "tmp"}


class ArchiveError(Exception):
    """Base class for evidence store errors."""


class IntegrityError(ArchiveError):
    """Stored bytes do not match their address, or a record conflicts."""


class MissingObjectError(ArchiveError, LookupError):
    """No object is stored under the requested digest."""


class InvalidDigestError(ArchiveError, ValueError):
    """A digest is not a lowercase hex SHA-256."""


def _check_digest(digest: str) -> str:
    # Also the path-traversal guard: only 64 hex chars ever become a path.
    if not is_digest(digest):
        raise InvalidDigestError(f"not a SHA-256 hex digest: {digest!r}")
    return digest


def _fsync_directory(path: Path) -> None:
    """fsync a directory so a rename into it is durable. No-op on Windows,
    which cannot open directories this way (NTFS journals the rename)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        # Some filesystems do not support fsync on directories.
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF):
            raise
    finally:
        os.close(fd)


def validate_tree_path(path: str) -> None:
    """A tree entry is a relative POSIX path of plain, non-empty components."""
    if not isinstance(path, str) or not path or "\x00" in path or path.startswith("/"):
        raise ValueError(f"unsafe tree path: {path!r}")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError(f"unsafe tree path: {path!r}")


def decode_tree(data: bytes) -> dict[str, str]:
    """Parse a canonical tree object back into {relative POSIX path: digest}.

    Rejects anything encode_manifest would not have produced: unsorted or
    duplicate entries, unsafe paths, bad digests, or a path that is both a
    file and a directory.
    """
    if not data:
        return {}
    if not data.endswith(b"\x00"):
        raise RecordFormatError("tree object is truncated")
    fields = data[:-1].split(b"\x00")
    if len(fields) % 2:
        raise RecordFormatError("tree object has an odd number of fields")
    manifest: dict[str, str] = {}
    try:
        for i in range(0, len(fields), 2):
            path = fields[i].decode("utf-8")
            digest = fields[i + 1].decode("ascii")
            validate_tree_path(path)
            if not is_digest(digest):
                raise ValueError(f"bad digest for {path!r}")
            if path in manifest:
                raise ValueError(f"duplicate entry {path!r}")
            manifest[path] = digest
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecordFormatError(f"invalid tree object: {exc}") from exc
    if encode_manifest(manifest) != data:
        raise RecordFormatError("tree object is not in canonical (sorted) form")
    _check_no_file_dir_clash(manifest)
    return manifest


def _check_no_file_dir_clash(manifest: dict[str, str]) -> None:
    directories = {
        "/".join(parts[:i])
        for parts in (p.split("/") for p in manifest)
        for i in range(1, len(parts))
    }
    clash = directories & manifest.keys()
    if clash:
        raise RecordFormatError(f"tree paths are both files and directories: {sorted(clash)}")


class BlobStore:
    """Content-addressed, append-only evidence store rooted at a directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._blobs = self.root / "blobs"
        self._snapshots = self.root / "meta" / "snapshots"
        self._sources = self.root / "meta" / "sources"
        self._tmp = self.root / "tmp"
        self._open_or_initialize()

    # -- layout -------------------------------------------------------------

    def _open_or_initialize(self) -> None:
        marker = self.root / _MARKER
        expected = canonical_json({"format": FORMAT_NAME, "layout_version": LAYOUT_VERSION})
        self.root.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            # Tolerate a concurrent initializer's entries, nothing else.
            if any(p.name not in _LAYOUT_NAMES for p in self.root.iterdir()):
                raise ArchiveError(f"{self.root} is not empty and is not a Stele archive")
            self._tmp.mkdir(exist_ok=True)
            self._install_record(marker, expected)
        with open_regular_file(marker) as fd, os.fdopen(fd, "rb", closefd=False) as fh:
            found = fh.read()
        if found != expected:
            try:
                version = json.loads(found).get("layout_version")
            except (ValueError, AttributeError):
                version = None
            raise ArchiveError(
                f"{self.root} has archive layout {version!r}; "
                f"this Stele supports layout {LAYOUT_VERSION}"
            )
        for directory in (self._blobs, self._snapshots, self._sources, self._tmp):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sharded(base: Path, digest: str) -> Path:
        return base / digest[:2] / digest[2:]

    def _blob_path(self, digest: str) -> Path:
        return self._sharded(self._blobs, _check_digest(digest))

    # -- publishing ---------------------------------------------------------

    def _write_temp(self, chunks: Iterable[bytes]) -> tuple[Path, str, int]:
        """Write chunks to a new temp file, hashing as it goes; fsync it."""
        self._tmp.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._tmp, prefix="put-", suffix=".tmp")
        tmp = Path(name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            _discard(tmp)
            raise
        return tmp, digest.hexdigest(), size

    def _install(self, tmp: Path, final: Path, on_exists: Callable[[Path], None]) -> None:
        """Rename tmp into place unless final already exists; never overwrite.

        on_exists(final) verifies an existing object and raises if it
        conflicts. tmp is always gone when this returns or raises.
        """
        try:
            shard = final.parent
            if not shard.is_dir():
                shard.mkdir(parents=True, exist_ok=True)
                _fsync_directory(shard.parent)
            if final.exists():
                on_exists(final)
                return
            # Blobs and records are read-only once published.
            os.chmod(tmp, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            # Publish with a hard link, which fails if final exists: two
            # writers that both passed the check above must not both rename
            # into place, or the second would swap the inode under a reader
            # that is already verifying the first.
            try:
                os.link(tmp, final)
            except FileExistsError:
                on_exists(final)
                return
            except PermissionError:
                # Windows can refuse while another writer's copy is being
                # published or verified.
                if not final.exists():
                    raise
                on_exists(final)
                return
            _fsync_directory(shard)
        finally:
            # The temp name is always ours (mkstemp); once linked, final holds
            # its own reference to the published inode.
            _discard(tmp)

    def _install_record(self, final: Path, data: bytes) -> None:
        tmp, _, _ = self._write_temp([data])

        def same_record(existing: Path) -> None:
            found = self._read_file(existing)
            if found != data:
                raise IntegrityError(f"conflicting record already stored at {existing}")

        self._install(tmp, final, same_record)

    # -- blobs --------------------------------------------------------------

    def put_chunks(self, chunks: Iterable[bytes], *, expected_digest: str | None = None) -> str:
        """Store bytes from an iterable of chunks; return their SHA-256.

        If expected_digest is given and the bytes hash differently, nothing is
        published and IntegrityError is raised.
        """
        if expected_digest is not None:
            _check_digest(expected_digest)
        tmp, digest, _ = self._write_temp(chunks)
        if expected_digest is not None and digest != expected_digest:
            _discard(tmp)
            raise IntegrityError(f"content hashes to {digest}, expected {expected_digest}")
        self._install(tmp, self._blob_path(digest), lambda existing: self._verify(digest, existing))
        return digest

    def put_bytes(self, data: bytes, *, expected_digest: str | None = None) -> str:
        return self.put_chunks([bytes(data)], expected_digest=expected_digest)

    def put_stream(self, stream: BinaryIO, *, expected_digest: str | None = None) -> str:
        return self.put_chunks(iter(lambda: stream.read(_CHUNK), b""), expected_digest=expected_digest)

    def put_fd(self, fd: int, *, expected_digest: str | None = None) -> str:
        """Store the bytes read from an already-open descriptor (left open)."""
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return self.put_stream(fh, expected_digest=expected_digest)

    def put_file(self, path: Path, *, expected_digest: str | None = None) -> str:
        """Store a regular file, opened without following symlinks."""
        with open_regular_file(path) as fd:
            return self.put_fd(fd, expected_digest=expected_digest)

    def has(self, digest: str) -> bool:
        """True if a blob is published under digest (its bytes are not checked)."""
        return self._blob_path(digest).is_file()

    def size(self, digest: str) -> int:
        try:
            return os.lstat(self._blob_path(digest)).st_size
        except FileNotFoundError:
            raise MissingObjectError(f"no blob {digest}") from None

    def read(self, digest: str) -> bytes:
        """Return a blob's bytes after verifying they hash to digest."""
        path = self._blob_path(digest)
        data = self._read_file(path, missing=f"no blob {digest}")
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise IntegrityError(f"blob {digest} is corrupt (its bytes hash to {actual})")
        return data

    def iter_verified(self, digest: str) -> Iterator[bytes]:
        """Yield a blob in chunks, raising IntegrityError at the end on mismatch.

        Chunks are unverified until iteration completes; use export() when a
        consumer must never observe unverified bytes.
        """
        return self._iter_verified(digest, self._blob_path(digest))

    def _iter_verified(self, digest: str, path: Path) -> Iterator[bytes]:
        h = hashlib.sha256()
        with self._open(path, missing=f"no blob {digest}") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
                yield chunk
        if h.hexdigest() != digest:
            raise IntegrityError(f"blob {digest} is corrupt (its bytes hash to {h.hexdigest()})")

    def export(self, digest: str, destination: Path) -> None:
        """Copy a verified blob to a new file at destination.

        The copy is written beside destination, verified, then renamed into
        place, so destination either does not exist or holds exactly the blob.
        Refuses to replace an existing destination.
        """
        destination = Path(destination)
        if os.path.lexists(destination):
            raise FileExistsError(f"refusing to overwrite {destination}")
        fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".stele-", suffix=".tmp")
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh:
                for chunk in self.iter_verified(digest):
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            if os.path.lexists(destination):
                raise FileExistsError(f"refusing to overwrite {destination}")
            os.replace(tmp, destination)
        except BaseException:
            _discard(tmp)
            raise

    def _verify(self, digest: str, path: Path) -> None:
        h = hashlib.sha256()
        with self._open(path, missing=f"no blob {digest}") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        if h.hexdigest() != digest:
            raise IntegrityError(
                f"blob {digest} is corrupt (its bytes hash to {h.hexdigest()}); "
                "refusing to overwrite stored evidence"
            )

    def _open(self, path: Path, *, missing: str = "") -> BinaryIO:
        try:
            with open_regular_file(path) as fd:
                return os.fdopen(os.dup(fd), "rb")
        except FileNotFoundError:
            raise MissingObjectError(missing or f"missing {path}") from None
        except UnsafeFileError as exc:
            raise IntegrityError(f"stored object is not a regular file: {path}") from exc

    def _read_file(self, path: Path, *, missing: str = "") -> bytes:
        with self._open(path, missing=missing) as fh:
            return fh.read()

    # -- trees --------------------------------------------------------------

    def put_tree(self, manifest: dict[str, str]) -> str:
        """Store a tree object for {relative POSIX path: blob digest}.

        Every referenced blob must already be stored. The returned digest is
        sha256_manifest(manifest), the same digest staging and the ledger use.
        """
        for path, digest in manifest.items():
            validate_tree_path(path)
            if not self.has(_check_digest(digest)):
                raise MissingObjectError(f"tree entry {path!r} references missing blob {digest}")
        _check_no_file_dir_clash(manifest)
        return self.put_bytes(encode_manifest(manifest))

    def read_tree(self, digest: str) -> dict[str, str]:
        """Return a verified tree object's {relative POSIX path: blob digest}."""
        return decode_tree(self.read(digest))

    # -- metadata records ---------------------------------------------------

    def put_snapshot(self, snapshot: Snapshot) -> Snapshot:
        """Record a Snapshot once all of its blobs are stored and verified.

        Every blob the snapshot covers (the file, or the tree object and each
        file it lists) is re-hashed first, and the record is published last,
        so its presence means the snapshot's content was complete and intact
        in the store when it was recorded. Recording it again is a no-op.
        """
        self._check_snapshot_content(snapshot)
        return self._record_snapshot(snapshot)

    def _record_snapshot(self, snapshot: Snapshot) -> Snapshot:
        """Publish a Snapshot record whose content the caller has just verified.

        For stele.archive.ingest only: it stores every blob with
        expected_digest (which hashes new bytes as they are written and
        re-verifies a copy already stored), so hashing them all again in
        put_snapshot would only double the cost of ingesting an input.
        """
        final = self._sharded(self._snapshots / snapshot.kind.value, snapshot.digest)
        self._install_record(final, snapshot.to_canonical())
        return snapshot

    def get_snapshot(self, digest: str, kind: SnapshotKind | str) -> Snapshot:
        kind = SnapshotKind(kind)
        path = self._sharded(self._snapshots / kind.value, _check_digest(digest))
        data = self._read_file(path, missing=f"no {kind.value} snapshot {digest}")
        try:
            snapshot = Snapshot.from_canonical(data)
        except RecordFormatError as exc:
            raise IntegrityError(f"snapshot record {digest} is corrupt: {exc}") from exc
        if (snapshot.kind, snapshot.digest) != (kind, digest):
            raise IntegrityError(f"snapshot record at {kind.value}/{digest} describes another snapshot")
        return snapshot

    def has_snapshot(self, digest: str, kind: SnapshotKind | str) -> bool:
        kind = SnapshotKind(kind)
        return self._sharded(self._snapshots / kind.value, _check_digest(digest)).is_file()

    def verify_snapshot(self, snapshot: Snapshot) -> None:
        """Re-hash every blob a recorded snapshot covers (IntegrityError if any changed)."""
        self._check_snapshot_content(snapshot)

    def _check_snapshot_content(self, snapshot: Snapshot) -> None:
        """Re-hash every blob the snapshot covers; a size match is not enough."""
        if snapshot.kind is SnapshotKind.FILE:
            if self._verified_size(snapshot.digest) != snapshot.size:
                raise IntegrityError(f"snapshot {snapshot.digest} size does not match its blob")
            return
        tree = self.read_tree(snapshot.digest)
        total = sum(self._verified_size(d) for d in tree.values())
        if (total, len(tree)) != (snapshot.size, snapshot.file_count):
            raise IntegrityError(f"snapshot {snapshot.digest} totals do not match its tree")

    def _verified_size(self, digest: str) -> int:
        """Size of a blob whose bytes were just verified against digest."""
        size = 0
        for chunk in self.iter_verified(digest):
            size += len(chunk)
        return size

    def put_source(self, source: Source) -> str:
        """Record a Source; return its source_id."""
        source_id = source.source_id
        self._install_record(self._sharded(self._sources, source_id), source.to_canonical())
        return source_id

    def get_source(self, source_id: str) -> Source:
        path = self._sharded(self._sources, _check_digest(source_id))
        data = self._read_file(path, missing=f"no source {source_id}")
        if hashlib.sha256(data).hexdigest() != source_id:
            raise IntegrityError(f"source record {source_id} is corrupt")
        try:
            return Source.from_canonical(data)
        except RecordFormatError as exc:
            raise IntegrityError(f"source record {source_id} is corrupt: {exc}") from exc


def _discard(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        # An orphaned temp file is harmless: nothing reads tmp/ as evidence.
        pass
