"""Trusted collection of parser-produced artifact paths."""
from __future__ import annotations

import os
import shutil
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


def discard_artifact_dir_contents(artifact_dir: Path) -> None:
    """Delete everything a parser wrote below artifact_dir, keeping the directory.

    Never follows symlinks: a symlink entry is unlinked, not its target, and
    rmtree does not descend through links. A directory the parser made
    unreadable is made accessible (it is ours again once the sandbox exits)
    before removal.
    """
    artifact_dir = Path(artifact_dir)
    try:
        root_stat = os.lstat(artifact_dir)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        raise UnsafeArtifactError(f"artifact_dir is not a directory: {artifact_dir}")

    # The parser has exited, so everything below is ours again, but it may
    # have made directories unreadable or unsearchable (chmod 000). Open each
    # one up before descending into it, never following symlinks, so the
    # removal below cannot be blocked by permissions.
    os.chmod(artifact_dir, stat.S_IMODE(root_stat.st_mode) | 0o700)
    for dirpath, dirnames, _files in os.walk(artifact_dir, topdown=True, followlinks=False):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
    for entry in os.scandir(artifact_dir):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)
