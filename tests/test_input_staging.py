from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from stele.containment.staging import InputStagingError, stage_regular_file

pytestmark = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"), reason="secure staging requires O_NOFOLLOW"
)


def test_regular_file_is_copied_and_hashed_from_staged_bytes(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    payload = b'{"hello":"world"}\n'
    source.write_bytes(payload)

    staged = stage_regular_file(source, tmp_path / "stage")

    assert staged.original_path == source
    assert staged.staged_path != source
    assert staged.staged_path.read_bytes() == payload
    assert staged.sha256 == hashlib.sha256(payload).hexdigest()


def test_symlink_input_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("secret")
    link = tmp_path / "link.json"
    link.symlink_to(real)

    with pytest.raises(InputStagingError, match="not a regular file"):
        stage_regular_file(link, tmp_path / "stage")


def test_directory_input_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "directory"
    source.mkdir()

    with pytest.raises(InputStagingError, match="not a regular file"):
        stage_regular_file(source, tmp_path / "stage")


def test_swap_to_symlink_between_lstat_and_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.json"
    source.write_text("safe")
    target = tmp_path / "target.json"
    target.write_text("different")

    real_open = os.open
    swapped = False

    def racing_open(path, flags, *args):
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(target)
        return real_open(path, flags, *args)

    monkeypatch.setattr("stele.containment.staging.os.open", racing_open)

    with pytest.raises(InputStagingError, match="securely open"):
        stage_regular_file(source, tmp_path / "stage")
