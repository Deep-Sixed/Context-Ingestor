"""Trusted collection of parser-produced artifact paths."""
from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafeArtifactError(ValueError):
    """Raised when parser output contains a symlink or non-regular entry."""


def collect_artifact_paths(artifact_dir: Path) -> list[Path]:
    """Return regular files under artifact_dir without following symlinks.

    The parser controls everything created below artifact_dir, so every entry is
    checked with lstat after the sandbox exits. Symlinks, devices, FIFOs,
    sockets, and any non-directory/non-regular entry fail closed.
    """
    artifact_dir = Path(artifact_dir)

    try:
        root_stat = os.lstat(artifact_dir)
    except FileNotFoundError:
        return []

    if not stat.S_ISDIR(root_stat.st_mode):
        raise UnsafeArtifactError(f"artifact_dir is not a directory: {artifact_dir}")

    artifacts: list[Path] = []
    for root, dirnames, filenames in os.walk(artifact_dir, followlinks=False):
        root_path = Path(root)

        for name in dirnames:
            path = root_path / name
            entry = os.lstat(path)
            if not stat.S_ISDIR(entry.st_mode):
                raise UnsafeArtifactError(
                    f"unsafe artifact directory entry (symlink/non-directory): {path}"
                )

        for name in filenames:
            path = root_path / name
            entry = os.lstat(path)
            if not stat.S_ISREG(entry.st_mode):
                raise UnsafeArtifactError(
                    f"unsafe artifact file entry (symlink/non-regular): {path}"
                )
            artifacts.append(path)

    return sorted(artifacts, key=lambda path: str(path.relative_to(artifact_dir)))
