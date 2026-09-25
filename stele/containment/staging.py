"""Trusted host-side staging for parser inputs.

Untrusted source paths are never bind-mounted directly into the parser sandbox.
An input is either one regular file or a directory tree of regular files. Every
file is opened without following symlinks (directory entries relative to their
already-open parent), checked to be the object that was inspected, and copied
from that exact open descriptor into a private Stele-owned staging directory.
The SHA-256 digest is computed from the same bytes that are copied, closing the
check/open/hash race at the containment boundary.

Windows has no O_NOFOLLOW. There a single file is lstat-checked (symlinks and
every other reparse point are refused), opened normally, and accepted only if
the opened file has the same volume and file index as the one checked, so a
link swapped in between is still refused. Directory inputs need os.fwalk and
O_NOFOLLOW and are refused on Windows.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..ledger.hashing import _is_link_or_reparse_point, sha256_manifest

# True where files can be opened without following a final symlink (Linux,
# macOS). False on Windows, which uses the identity-checked fallback above.
RACE_FREE_NOFOLLOW = hasattr(os, "O_NOFOLLOW")

# os.open defaults to text mode on Windows, which would translate line endings.
_BINARY = getattr(os, "O_BINARY", 0)


class InputStagingError(ValueError):
    """Raised when an input cannot be safely staged for sandbox execution."""


@dataclass(frozen=True)
class StagedInput:
    original_path: Path
    staged_path: Path
    # File input: SHA-256 of the file bytes.
    # Directory input: sha256_manifest() over {posix relative path: file SHA-256}.
    sha256: str
    # Per-file hashes for directory inputs (empty for a single file).
    manifest: dict[str, str] = field(default_factory=dict)


def _open_flags() -> int:
    # O_NONBLOCK keeps a FIFO swapped in after lstat from blocking the open;
    # the fstat check then rejects it.
    flags = os.O_RDONLY | _BINARY | getattr(os, "O_NONBLOCK", 0)
    if RACE_FREE_NOFOLLOW:
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _copy_verified(fd: int, observed: os.stat_result, label: str, destination: Path) -> str:
    """Copy an already-open file to destination, hashing the copied bytes."""
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise InputStagingError(f"opened input is not a regular file: {label}")
    if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
        raise InputStagingError(f"input changed while being opened: {label}")
    if not RACE_FREE_NOFOLLOW and opened.st_ino == 0:
        # Without O_NOFOLLOW the identity check is the only defence against a
        # swapped-in link; a filesystem without file indexes cannot pass it.
        raise InputStagingError(f"cannot verify input identity (no file index): {label}")

    digest = hashlib.sha256()
    with os.fdopen(fd, "rb", closefd=False) as src, destination.open("xb") as dst:
        os.chmod(destination, 0o600)
        while chunk := src.read(65_536):
            digest.update(chunk)
            dst.write(chunk)
    return digest.hexdigest()


def stage_regular_file(source: Path, staging_dir: Path) -> StagedInput:
    """Copy source into staging_dir without following symlinks.

    The source identity observed by lstat must still match the file opened
    with O_NOFOLLOW. Both hashing and copying read from that one descriptor.
    Only regular files are accepted; directories, devices, FIFOs, sockets, and
    symlinks are rejected.
    """
    source = Path(source)
    staging_dir = Path(staging_dir)

    try:
        observed = os.lstat(source)
    except OSError as exc:
        raise InputStagingError(f"cannot inspect input {source}: {exc}") from exc

    if not stat.S_ISREG(observed.st_mode) or _is_link_or_reparse_point(observed):
        raise InputStagingError(f"input is not a regular file: {source}")

    flags = _open_flags()
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise InputStagingError(f"cannot securely open input {source}: {exc}") from exc

    try:
        staging_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        destination = staging_dir / source.name
        sha = _copy_verified(fd, observed, str(source), destination)
        return StagedInput(original_path=source, staged_path=destination, sha256=sha)
    finally:
        os.close(fd)


def stage_directory(source: Path, staging_dir: Path) -> StagedInput:
    """Copy a directory tree of regular files into staging_dir.

    The tree is walked by file descriptor (os.fwalk) and every entry is
    inspected and opened relative to its already-open parent, so swapping a
    path component for a symlink mid-walk cannot redirect the copy. Symlinks,
    FIFOs, sockets and devices anywhere in the tree are rejected; unreadable
    entries fail the staging rather than being skipped.
    """
    source = Path(source)
    staging_dir = Path(staging_dir)
    flags = _open_flags()
    if not hasattr(os, "fwalk") or not RACE_FREE_NOFOLLOW:
        raise InputStagingError("secure directory staging requires os.fwalk and O_NOFOLLOW")

    try:
        root_stat = os.lstat(source)
    except OSError as exc:
        raise InputStagingError(f"cannot inspect input {source}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise InputStagingError(f"input is not a directory: {source}")

    staging_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination_root = staging_dir / (source.name or "input")
    destination_root.mkdir(mode=0o700)

    def _fail(exc: OSError) -> None:
        raise InputStagingError(f"unreadable input entry: {exc.filename}: {exc.strerror}") from exc

    manifest: dict[str, str] = {}
    try:
        _copy_tree(source, root_stat, destination_root, flags, _fail, manifest)
    except OSError as exc:
        # Some Python versions raise listing errors from fwalk directly instead
        # of calling onerror; either way the staging fails, never skips.
        _fail(exc)

    return StagedInput(
        original_path=source,
        staged_path=destination_root,
        sha256=sha256_manifest(manifest),
        manifest=manifest,
    )


def _copy_tree(
    source: Path,
    root_stat: os.stat_result,
    destination_root: Path,
    flags: int,
    _fail: Callable[[OSError], None],
    manifest: dict[str, str],
) -> None:
    """Walk source by descriptor, copying regular files into destination_root."""
    walked_root = False
    for dirpath, dirnames, filenames, dirfd in os.fwalk(
        source, onerror=_fail, follow_symlinks=False
    ):
        if not walked_root:
            walked_root = True
            top = os.fstat(dirfd)
            if (top.st_dev, top.st_ino) != (root_stat.st_dev, root_stat.st_ino):
                raise InputStagingError(f"input changed while being opened: {source}")

        relative_dir = Path(dirpath).relative_to(source)
        target_dir = destination_root / relative_dir

        for name in dirnames:
            entry = _lstat_at(name, dirfd, Path(dirpath) / name)
            if not stat.S_ISDIR(entry.st_mode):
                raise InputStagingError(
                    f"input contains a symlink or non-directory entry: {Path(dirpath) / name}"
                )
            (target_dir / name).mkdir(mode=0o700)

        for name in filenames:
            label = str(Path(dirpath) / name)
            entry = _lstat_at(name, dirfd, Path(dirpath) / name)
            if not stat.S_ISREG(entry.st_mode):
                raise InputStagingError(
                    f"input contains a symlink or non-regular file: {label}"
                )
            try:
                fd = os.open(name, flags, dir_fd=dirfd)
            except OSError as exc:
                raise InputStagingError(f"cannot securely open input {label}: {exc}") from exc
            try:
                sha = _copy_verified(fd, entry, label, target_dir / name)
            finally:
                os.close(fd)
            manifest[(relative_dir / name).as_posix()] = sha


def _lstat_at(name: str, dirfd: int, label: Path) -> os.stat_result:
    try:
        return os.lstat(name, dir_fd=dirfd)
    except OSError as exc:
        raise InputStagingError(f"cannot inspect input entry {label}: {exc}") from exc


def stage_input(source: Path, staging_dir: Path) -> StagedInput:
    """Stage a regular file or a directory tree; reject everything else."""
    source = Path(source)
    try:
        observed = os.lstat(source)
    except OSError as exc:
        raise InputStagingError(f"cannot inspect input {source}: {exc}") from exc
    if stat.S_ISDIR(observed.st_mode):
        return stage_directory(source, staging_dir)
    return stage_regular_file(source, staging_dir)
