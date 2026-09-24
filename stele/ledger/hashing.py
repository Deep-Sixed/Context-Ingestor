"""
Deterministic, no-follow hashing for Stele artifacts.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


class UnsafeFileError(ValueError):
    """Raised when a path is not the regular file Stele expected to hash."""


def _nofollow_flags(*, directory: bool = False) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise UnsafeFileError("secure artifact hashing requires O_NOFOLLOW")

    # O_NONBLOCK: opening a FIFO for reading otherwise blocks until a writer
    # appears, so a parser-planted FIFO would hang hashing forever. It has no
    # effect on reads from regular files, and the fstat check rejects FIFOs.
    flags = os.O_RDONLY | nofollow | os.O_NONBLOCK
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


def sha256_file(path: Path) -> str:
    """Return SHA-256 for one regular file without following the final symlink."""
    path = Path(path)

    observed = os.lstat(path)
    if not stat.S_ISREG(observed.st_mode):
        raise UnsafeFileError(f"path is not a regular file: {path}")

    try:
        fd = os.open(path, _nofollow_flags())
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
        return _hash_fd(fd)
    finally:
        os.close(fd)


def sha256_file_beneath(root: Path, relative_path: Path) -> str:
    """Hash a regular file while proving every path component stays under root.

    Every directory component is opened relative to the previously opened
    directory with O_DIRECTORY and O_NOFOLLOW. The final file is opened with
    O_NOFOLLOW. Dot-dot traversal and symlinked parent directories are refused.
    """
    root = Path(root)
    relative_path = Path(relative_path)

    if relative_path.is_absolute() or not relative_path.parts:
        raise UnsafeFileError(f"artifact path must be relative: {relative_path}")
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        raise UnsafeFileError(
            f"artifact path contains unsafe component: {relative_path}"
        )

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
        return _hash_fd(file_fd)
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


def sha256_manifest(manifest: dict[str, str]) -> str:
    """Return a deterministic SHA-256 over a relative-path to file-hash manifest."""
    h = hashlib.sha256()
    for rel_path in sorted(manifest):
        h.update(rel_path.encode())
        h.update(b"\x00")
        h.update(manifest[rel_path].encode())
        h.update(b"\x00")
    return h.hexdigest()


def build_manifest(artifact_dir: Path, artifact_paths: list[Path]) -> dict[str, str]:
    """Build a manifest while refusing any escape from artifact_dir."""
    artifact_dir = Path(artifact_dir)
    manifest: dict[str, str] = {}

    for path in artifact_paths:
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

        manifest[str(relative)] = sha256_file_beneath(
            artifact_dir,
            relative,
        )

    return manifest
