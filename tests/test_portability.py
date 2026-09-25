"""
Cross-platform behaviour (Linux, macOS, Windows).

  - Hashes are of the exact bytes on disk (no newline translation on Windows).
  - Manifest keys use POSIX separators, so artifact_hash matches across OSes.
  - The lstat fallback used where O_NOFOLLOW/dir_fd are missing (Windows)
    still refuses symlinked files and parent directories.
  - A host without bubblewrap gets a clear SandboxUnavailableError.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from stele.containment.runner import SandboxUnavailableError, run_in_sandbox
from stele.containment.sandbox import SandboxConfig
from stele.ledger import hashing
from stele.ledger.hashing import UnsafeFileError, build_manifest, sha256_file


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlinks here: {exc}")


def test_hash_is_of_exact_bytes(tmp_path: Path) -> None:
    data = b"line one\r\nline two\n\x00\x1a binary tail"
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    assert sha256_file(p) == hashlib.sha256(data).hexdigest()


def test_manifest_keys_use_posix_separators(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    f = nested / "result.json"
    f.write_text("{}")
    assert list(build_manifest(tmp_path, [f])) == ["a/b/result.json"]


class TestLstatFallback:
    """Runs the Windows code path on every OS."""

    @pytest.fixture(autouse=True)
    def force_fallback(self, monkeypatch):
        monkeypatch.setattr(hashing, "RACE_FREE_NOFOLLOW", False)

    def test_regular_nested_file_hashes(self, tmp_path: Path) -> None:
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "f.txt").write_bytes(b"payload")
        digest = hashing.sha256_file_beneath(tmp_path, Path("d/f.txt"))
        assert digest == hashlib.sha256(b"payload").hexdigest()

    def test_symlinked_parent_directory_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "f.txt").write_text("host secret")
        root = tmp_path / "root"
        root.mkdir()
        _symlink_or_skip(root / "d", outside, directory=True)
        with pytest.raises(UnsafeFileError):
            hashing.sha256_file_beneath(root, Path("d/f.txt"))

    def test_symlinked_file_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "secret.txt"
        outside.write_text("host secret")
        root = tmp_path / "root"
        root.mkdir()
        _symlink_or_skip(root / "f.txt", outside)
        with pytest.raises(UnsafeFileError):
            hashing.sha256_file_beneath(root, Path("f.txt"))

    def test_dotdot_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeFileError):
            hashing.sha256_file_beneath(tmp_path, Path("../x"))


def test_missing_bubblewrap_raises_clear_error(tmp_path: Path, monkeypatch) -> None:
    def no_bwrap(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", no_bwrap)
    with pytest.raises(SandboxUnavailableError, match="Linux bubblewrap"):
        run_in_sandbox(SandboxConfig(command=["/usr/bin/true"], artifact_dir=tmp_path / "o"))


def test_missing_bubblewrap_on_windows_style_error(tmp_path: Path, monkeypatch) -> None:
    # CreateProcess failures carry no filename.
    def no_bwrap(argv, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(subprocess, "run", no_bwrap)
    with pytest.raises(SandboxUnavailableError):
        run_in_sandbox(SandboxConfig(command=["/usr/bin/true"], artifact_dir=tmp_path / "o"))


def test_race_free_path_is_used_on_posix() -> None:
    if os.name == "posix":
        assert hashing.RACE_FREE_NOFOLLOW
