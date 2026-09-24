from __future__ import annotations

from pathlib import Path

import pytest

from stele.containment.artifacts import UnsafeArtifactError, collect_artifact_paths


def test_collect_artifacts_accepts_nested_regular_files(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    nested = artifact_dir / "nested"
    nested.mkdir(parents=True)
    first = artifact_dir / "a.json"
    second = nested / "b.txt"
    first.write_text("{}")
    second.write_text("ok")

    assert collect_artifact_paths(artifact_dir) == [first, second]


def test_collect_artifacts_rejects_file_symlink(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("host data")
    (artifact_dir / "result.json").symlink_to(outside)

    with pytest.raises(UnsafeArtifactError, match="symlink/non-regular"):
        collect_artifact_paths(artifact_dir)


def test_collect_artifacts_rejects_directory_symlink(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifact_dir / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeArtifactError, match="symlink/non-directory"):
        collect_artifact_paths(artifact_dir)
