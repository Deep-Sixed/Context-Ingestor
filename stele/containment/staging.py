"""Trusted host-side staging for parser inputs.

Untrusted source paths are never bind-mounted directly into the parser sandbox.
A source must be a regular file, is opened without following the final symlink,
and is copied from that exact open file descriptor into a private Stele-owned
staging directory. The SHA-256 digest is computed from the same bytes that are
copied, closing the check/open/hash race at the containment boundary.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


class InputStagingError(ValueError):
    """Raised when an input cannot be safely staged for sandbox execution."""


@dataclass(frozen=True)
class StagedInput:
    original_path: Path
    staged_path: Path
    sha256: str


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

    if not stat.S_ISREG(observed.st_mode):
        raise InputStagingError(f"input is not a regular file: {source}")

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise InputStagingError("secure input staging requires O_NOFOLLOW")

    # O_NONBLOCK keeps a FIFO swapped in after lstat from blocking the open;
    # the fstat check below then rejects it.
    flags = os.O_RDONLY | nofollow | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise InputStagingError(f"cannot securely open input {source}: {exc}") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise InputStagingError(f"opened input is not a regular file: {source}")
        if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
            raise InputStagingError(f"input changed while being opened: {source}")

        staging_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        destination = staging_dir / source.name
        digest = hashlib.sha256()

        with os.fdopen(fd, "rb", closefd=False) as src, destination.open("xb") as dst:
            os.chmod(destination, 0o600)
            while True:
                chunk = src.read(65_536)
                if not chunk:
                    break
                digest.update(chunk)
                dst.write(chunk)

        return StagedInput(
            original_path=source,
            staged_path=destination,
            sha256=digest.hexdigest(),
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
