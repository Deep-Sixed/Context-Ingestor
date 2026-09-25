"""Trusted collection of parser-produced artifact paths."""
from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafeArtifactError(ValueError):
    """Raised when parser output contains a symlink or non-regular entry."""


def _lstat(path: Path) -> os.stat_result:
    # A directory that is listable but not searchable (e.g. chmod 0444) lets
    # os.walk see names whose lstat then fails; treat that like unreadable.
    try:
        return os.lstat(path)
    except OSError as exc:
        raise UnsafeArtifactError(
            f"unreadable artifact entry (parser-controlled permissions?): "
            f"{path}: {exc.strerror}"
        ) from exc


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

    def _unreadable(exc: OSError) -> None:
        # os.walk silently skips directories it cannot list; a parser could
        # chmod 000 a subdirectory to get a partial bundle recorded as whole.
        raise UnsafeArtifactError(
            f"unreadable artifact directory (parser-controlled permissions?): "
            f"{exc.filename}: {exc.strerror}"
        ) from exc

    artifacts: list[Path] = []
    for root, dirnames, filenames in os.walk(
        artifact_dir, onerror=_unreadable, followlinks=False
    ):
        root_path = Path(root)

        for name in dirnames:
            path = root_path / name
            entry = _lstat(path)
            if not stat.S_ISDIR(entry.st_mode):
                raise UnsafeArtifactError(
                    f"unsafe artifact directory entry (symlink/non-directory): {path}"
                )

        for name in filenames:
            path = root_path / name
            entry = _lstat(path)
            if not stat.S_ISREG(entry.st_mode):
                raise UnsafeArtifactError(
                    f"unsafe artifact file entry (symlink/non-regular): {path}"
                )
            artifacts.append(path)

    return sorted(artifacts, key=lambda path: str(path.relative_to(artifact_dir)))
