"""
Deterministic hashing for Stele Phase F.

Two concerns:
  sha256_file()     — content hash of a single artifact file
  sha256_manifest() — deterministic hash of the full {path: hash} manifest
  build_manifest()  — walk artifact_dir and produce the manifest dict
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(65_536):
            h.update(chunk)
    return h.hexdigest()


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


def build_manifest(artifact_dir: Path, artifact_paths: list[Path]) -> dict[str, str]:
    """Build {relative_path: sha256} for each artifact file.

    Raises FileNotFoundError if any path does not exist or is not a file.
    Raises ValueError if any path is not under artifact_dir.
    """
    manifest: dict[str, str] = {}
    for path in artifact_paths:
        if not path.exists():
            raise FileNotFoundError(f"artifact not found: {path}")
        if not path.is_file():
            raise ValueError(f"artifact path is not a file: {path}")
        try:
            rel = str(path.relative_to(artifact_dir))
        except ValueError:
            raise ValueError(
                f"artifact {path} is outside artifact_dir {artifact_dir} — "
                "this path did not come through /stele/output"
            )
        manifest[rel] = sha256_file(path)
    return manifest
