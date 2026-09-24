"""
Deterministic hashing for Stele Phase F.

  collect_artifacts() — walk artifact_dir without following symlinks
  sha256_file()       — content hash of a single artifact file
  sha256_manifest()   — deterministic hash of the full {path: hash} manifest
  build_manifest()    — hash each artifact and produce the manifest dict

Everything under artifact_dir was written by an untrusted parser, so no path
in it is trusted: symlinks are never followed and only regular files are
accepted.  A parser that plants a symlink to a host file must not be able to
get that host file hashed, ledgered, or read by an adapter.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


class UnsafeArtifactError(ValueError):
    """Raised when an artifact path is a symlink, escapes artifact_dir, or is not a regular file."""


def collect_artifacts(artifact_dir: Path) -> tuple[list[Path], list[Path]]:
    """Walk artifact_dir without following symlinks.

    Returns (accepted, rejected), both sorted.  accepted holds regular files
    only.  rejected holds every other entry a parser left behind: symlinks
    (to files or directories, dangling or not), FIFOs, sockets and devices.
    Symlinked directories are never descended into.
    """
    accepted: list[Path] = []
    rejected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(artifact_dir, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            if os.path.islink(base / name):
                rejected.append(base / name)
                dirnames.remove(name)
        for name in filenames:
            path = base / name
            if stat.S_ISREG(os.lstat(path).st_mode):
                accepted.append(path)
            else:
                rejected.append(path)
    return sorted(accepted), sorted(rejected)


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of a regular file's contents.

    The file is opened with O_NOFOLLOW and checked with fstat, so the bytes
    hashed are the bytes of the regular file at that path — never the target
    of a symlink, and never a FIFO that could block the reader.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if os.path.islink(path):
            raise UnsafeArtifactError(f"artifact is a symlink: {path}") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafeArtifactError(f"artifact is not a regular file: {path}")
        h = hashlib.sha256()
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # now owned by fh
            while chunk := fh.read(65_536):
                h.update(chunk)
        return h.hexdigest()
    finally:
        if fd != -1:
            os.close(fd)


def sha256_manifest(manifest: dict[str, str]) -> str:
    """Return a deterministic hex sha256 over a {relative_path: file_hash} manifest.

    Paths are sorted lexicographically before hashing so that two manifests
    with identical content but different insertion order produce the same hash.
    """
    h = hashlib.sha256()
    for rel_path in sorted(manifest):
        h.update(rel_path.encode())
        h.update(b"\x00")  # null-separate key from value to prevent collisions
        h.update(manifest[rel_path].encode())
        h.update(b"\x00")
    return h.hexdigest()


def resolve_artifact(artifact_dir: Path, rel_path: str) -> Path:
    """Return artifact_dir / rel_path, refusing any path that leaves artifact_dir.

    Every component is checked: a symlinked parent directory, an absolute
    rel_path, or '..' traversal all raise UnsafeArtifactError.  The final
    component is checked by sha256_file (O_NOFOLLOW).
    """
    root = os.path.realpath(artifact_dir)
    candidate = os.path.normpath(os.path.join(root, rel_path))
    parent = os.path.dirname(candidate)
    if (
        os.path.isabs(rel_path)
        or os.path.commonpath([root, candidate]) != root
        or candidate == root
        or os.path.realpath(parent) != parent
    ):
        raise UnsafeArtifactError(f"artifact {rel_path!r} escapes artifact_dir {artifact_dir}")
    return Path(candidate)


def build_manifest(artifact_dir: Path, artifact_paths: list[Path]) -> dict[str, str]:
    """Build {relative_path: sha256} for each artifact file.

    Raises FileNotFoundError if any path does not exist.
    Raises ValueError if any path is not under artifact_dir.
    Raises UnsafeArtifactError (a ValueError) if any path is a symlink, sits
    under a symlinked directory, or is not a regular file.
    """
    manifest: dict[str, str] = {}
    for path in artifact_paths:
        if not os.path.lexists(path):
            raise FileNotFoundError(f"artifact not found: {path}")
        try:
            rel = str(path.relative_to(artifact_dir))
        except ValueError:
            raise ValueError(
                f"artifact {path} is outside artifact_dir {artifact_dir} — "
                "this path did not come through /stele/output"
            )
        manifest[rel] = sha256_file(resolve_artifact(artifact_dir, rel))
    return manifest
