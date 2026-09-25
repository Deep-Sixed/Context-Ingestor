"""Moving run inputs and outputs into the evidence store.

- snapshot_staged_input: the bytes staging copied for the parser become a
  Snapshot whose digest is StagedInput.sha256, the hash of what the parser saw.
- ingest_artifacts: parser outputs are stored by digest, read through the same
  no-follow, stay-beneath-root opens the ledger uses to hash them.
- materialize_snapshot: write a stored Snapshot back out (e.g. for replay),
  from verified bytes only.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from ..containment.staging import StagedInput
from ..ledger.hashing import artifact_relative_path, open_regular_file_beneath
from .records import Snapshot, SnapshotKind
from .store import BlobStore, IntegrityError, validate_tree_path


def snapshot_staged_input(store: BlobStore, staged: StagedInput) -> Snapshot:
    """Store a staged input and record it as a Snapshot.

    A staged file becomes a FILE snapshot addressed by its bytes. A staged
    directory becomes a TREE snapshot: every file is stored as a blob, then the
    canonical tree object over staged.manifest, whose digest is
    sha256_manifest(staged.manifest) == staged.sha256. Each file is re-hashed as
    it is stored and must match what staging recorded; the Snapshot record is
    published last, so it only exists once all of its content does.

    Empty directories are not part of a TREE snapshot, just as they are not
    part of the manifest digest.
    """
    root = Path(staged.staged_path)
    if stat.S_ISDIR(os.lstat(root).st_mode):
        for relative, expected in staged.manifest.items():
            with open_regular_file_beneath(root, Path(relative)) as fd:
                store.put_fd(fd, expected_digest=expected)
        digest = store.put_tree(staged.manifest)
        size = sum(store.size(d) for d in staged.manifest.values())
        snapshot = Snapshot(SnapshotKind.TREE, digest, size, len(staged.manifest))
    else:
        digest = store.put_file(root, expected_digest=staged.sha256)
        snapshot = Snapshot(SnapshotKind.FILE, digest, store.size(digest), 1)

    if snapshot.digest != staged.sha256:
        raise IntegrityError(
            f"snapshot digest {snapshot.digest} differs from staged hash {staged.sha256}"
        )
    return store.put_snapshot(snapshot)


def ingest_artifacts(
    store: BlobStore, artifact_dir: Path, artifact_paths: list[Path]
) -> dict[str, str]:
    """Store collected artifacts; return {relative POSIX path: blob digest}.

    artifact_paths should come from collect_artifact_paths(). Each file is
    opened beneath artifact_dir without following symlinks (refusing '..' and
    symlinked parents), and the digest is computed from the bytes stored. The
    result equals build_manifest(artifact_dir, artifact_paths), so
    store.put_tree(result) yields the ledger's artifact_hash.
    """
    artifact_dir = Path(artifact_dir)
    manifest: dict[str, str] = {}
    for path in artifact_paths:
        relative = artifact_relative_path(artifact_dir, path)
        with open_regular_file_beneath(artifact_dir, relative) as fd:
            manifest[relative.as_posix()] = store.put_fd(fd)
    return manifest


def materialize_snapshot(store: BlobStore, snapshot: Snapshot, destination: Path) -> None:
    """Write a Snapshot's verified bytes to destination, which must not exist.

    FILE: destination becomes the file. TREE: destination becomes a directory
    holding every file at its relative path. Each blob is verified before it
    is renamed into place, so no unverified bytes are ever left behind.
    """
    destination = Path(destination)
    if snapshot.kind is SnapshotKind.FILE:
        store.export(snapshot.digest, destination)
        return

    tree = store.read_tree(snapshot.digest)
    destination.mkdir()
    for relative, digest in tree.items():
        validate_tree_path(relative)
        parts = relative.split("/")
        if os.name == "nt" and any(("\\" in p or ":" in p) for p in parts):
            raise ValueError(f"tree path cannot be represented on Windows: {relative!r}")
        target = destination.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        store.export(digest, target)
