"""
Deterministic, no-follow hashing for Stele artifacts.
"""
from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class UnsafeFileError(ValueError):
    """Raised when a path is not the regular file Stele expected to hash."""


# Linux and macOS open every path component relative to its parent with
# O_NOFOLLOW, which is race-free. Windows has neither O_NOFOLLOW nor dir_fd
# support, so it falls back to lstat-checking each component and confirming the
# opened file is the one that was checked. That fallback still refuses symlinks
# and junctions but cannot exclude a swap between check and open.
RACE_FREE_NOFOLLOW = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
)

# O_BINARY matters on Windows: os.open defaults to text mode there, which
# would translate line endings and change the hash.
_BINARY = getattr(os, "O_BINARY", 0)


def _nofollow_flags(*, directory: bool = False) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise UnsafeFileError("secure artifact hashing requires O_NOFOLLOW")

    # O_NONBLOCK: opening a FIFO for reading otherwise blocks until a writer
    # appears, so a parser-planted FIFO would hang hashing forever. It has no
    # effect on reads from regular files, and the fstat check rejects FIFOs.
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | _BINARY
    if directory:
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is None:
            raise UnsafeFileError("secure artifact hashing requires O_DIRECTORY")
        flags |= directory_flag
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb", closefd=False) as fh:
        while chunk := fh.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def open_regular_file(path: Path) -> Iterator[int]:
    """Open one regular file without following the final symlink; yield its fd.

    The file identity observed by lstat must match the opened descriptor.
    Callers that need the bytes (not just a hash) read from this descriptor so
    what they hash and what they copy are the same bytes.
    """
    path = Path(path)

    observed = os.lstat(path)
    if not stat.S_ISREG(observed.st_mode) or _is_link_or_reparse_point(observed):
        raise UnsafeFileError(f"path is not a regular file: {path}")

    flags = _nofollow_flags() if hasattr(os, "O_NOFOLLOW") else os.O_RDONLY | _BINARY
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UnsafeFileError(f"cannot securely open artifact {path}: {exc}") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafeFileError(f"opened artifact is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
            raise UnsafeFileError(f"artifact changed while being opened: {path}")
        yield fd
    finally:
        os.close(fd)


def sha256_file(path: Path) -> str:
    """Return SHA-256 for one regular file without following the final symlink."""
    with open_regular_file(path) as fd:
        return _hash_fd(fd)


def _check_relative(relative_path: Path) -> None:
    if relative_path.is_absolute() or not relative_path.parts:
        raise UnsafeFileError(f"artifact path must be relative: {relative_path}")
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        raise UnsafeFileError(
            f"artifact path contains unsafe component: {relative_path}"
        )


@contextmanager
def open_regular_file_beneath(root: Path, relative_path: Path) -> Iterator[int]:
    """Open a regular file while proving every path component stays under root.

    Every directory component is opened relative to the previously opened
    directory with O_DIRECTORY and O_NOFOLLOW. The final file is opened with
    O_NOFOLLOW. Dot-dot traversal and symlinked parent directories are refused.
    Yields the open file descriptor, which is closed on exit.
    """
    root = Path(root)
    relative_path = Path(relative_path)
    _check_relative(relative_path)

    if not RACE_FREE_NOFOLLOW:
        with _open_beneath_by_lstat(root, relative_path) as fd:
            yield fd
        return

    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        try:
            root_fd = os.open(root, _nofollow_flags(directory=True))
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise UnsafeFileError(
                f"cannot securely open artifact root {root}: {exc}"
            ) from exc

        directory_fds.append(root_fd)

        for component in relative_path.parts[:-1]:
            try:
                next_fd = os.open(
                    component,
                    _nofollow_flags(directory=True),
                    dir_fd=directory_fds[-1],
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise UnsafeFileError(
                    f"unsafe artifact directory component {component!r}: {exc}"
                ) from exc
            directory_fds.append(next_fd)

        try:
            file_fd = os.open(
                relative_path.parts[-1],
                _nofollow_flags(),
                dir_fd=directory_fds[-1],
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise UnsafeFileError(
                f"cannot securely open artifact {relative_path}: {exc}"
            ) from exc

        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafeFileError(
                f"artifact is not a regular file: {relative_path}"
            )
        yield file_fd
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def sha256_file_beneath(root: Path, relative_path: Path) -> str:
    """Hash a regular file while proving every path component stays under root.

    See open_regular_file_beneath for the no-follow guarantees.
    """
    with open_regular_file_beneath(root, relative_path) as fd:
        return _hash_fd(fd)


def _is_link_or_reparse_point(st: os.stat_result) -> bool:
    """Symlink, or (on Windows) any reparse point: junctions, mount points,
    app-exec links, cloud placeholders. All of them can redirect an open."""
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


@contextmanager
def _open_beneath_by_lstat(root: Path, relative_path: Path) -> Iterator[int]:
    """Fallback for platforms without O_NOFOLLOW/dir_fd (Windows)."""
    current = root
    for component in (None, *relative_path.parts[:-1]):
        if component is not None:
            current = current / component
        st = os.lstat(current)
        if _is_link_or_reparse_point(st) or not stat.S_ISDIR(st.st_mode):
            raise UnsafeFileError(
                f"unsafe artifact directory component (link/non-directory): {current}"
            )
    with open_regular_file(root / relative_path) as fd:
        yield fd


def encode_manifest(manifest: dict[str, str]) -> bytes:
    """Canonical byte encoding of a relative-path to file-hash manifest.

    Entries are sorted by path; each is ``path NUL digest NUL`` in UTF-8. No
    filesystem path may contain NUL, so the encoding is unambiguous. The
    archive stores directory snapshots and artifact bundles as exactly these
    bytes, so a tree object's blob digest equals sha256_manifest().
    """
    parts: list[bytes] = []
    for rel_path in sorted(manifest):
        parts += [rel_path.encode(), b"\x00", manifest[rel_path].encode(), b"\x00"]
    return b"".join(parts)


def sha256_manifest(manifest: dict[str, str]) -> str:
    """Return a deterministic SHA-256 over a relative-path to file-hash manifest."""
    return hashlib.sha256(encode_manifest(manifest)).hexdigest()


def artifact_relative_path(artifact_dir: Path, path: Path) -> Path:
    """Return path relative to artifact_dir, refusing escapes and unsafe parts."""
    artifact_dir = Path(artifact_dir)
    path = Path(path)
    try:
        relative = path.relative_to(artifact_dir)
    except ValueError:
        raise ValueError(
            f"artifact {path} is outside artifact_dir {artifact_dir} — "
            "this path did not come through /stele/output"
        )

    if not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError(
            f"artifact {path} contains an unsafe path relative to {artifact_dir}"
        )
    return relative


def build_manifest(artifact_dir: Path, artifact_paths: list[Path]) -> dict[str, str]:
    """Build a manifest while refusing any escape from artifact_dir."""
    artifact_dir = Path(artifact_dir)
    manifest: dict[str, str] = {}

    for path in artifact_paths:
        relative = artifact_relative_path(artifact_dir, path)

        # POSIX separators keep manifests (and artifact_hash) identical across
        # operating systems; on Linux/macOS this is the same as str(relative).
        manifest[relative.as_posix()] = sha256_file_beneath(
            artifact_dir,
            relative,
        )

    return manifest
