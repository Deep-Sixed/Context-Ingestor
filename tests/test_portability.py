"""
Cross-platform behaviour (Linux, macOS, Windows).

  - Hashes are of the exact bytes on disk (no newline translation on Windows).
  - Manifest keys use POSIX separators, so artifact_hash matches across OSes.
  - The lstat fallback used where O_NOFOLLOW/dir_fd are missing (Windows)
    still refuses symlinked files and parent directories.
  - A host without bubblewrap or a container engine gets a clear
    SandboxUnavailableError.
  - Input staging without O_NOFOLLOW (Windows) refuses links and swaps.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import stele.containment.runner as runner_module
from stele.containment.oci import OciBackend
from stele.containment import staging
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
    monkeypatch.setattr(runner_module, "bwrap_available", lambda: True)
    with pytest.raises(SandboxUnavailableError, match="Linux bubblewrap"):
        run_in_sandbox(SandboxConfig(command=["/usr/bin/true"], artifact_dir=tmp_path / "o"))


def test_missing_bubblewrap_on_windows_style_error(tmp_path: Path, monkeypatch) -> None:
    # CreateProcess failures carry no filename.
    def no_bwrap(argv, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(subprocess, "run", no_bwrap)
    monkeypatch.setattr(runner_module, "bwrap_available", lambda: True)
    with pytest.raises(SandboxUnavailableError):
        run_in_sandbox(SandboxConfig(command=["/usr/bin/true"], artifact_dir=tmp_path / "o"))


def test_race_free_path_is_used_on_posix() -> None:
    if os.name == "posix":
        assert hashing.RACE_FREE_NOFOLLOW


def test_missing_bubblewrap_reported_before_input_staging(tmp_path: Path, monkeypatch) -> None:
    # Staging needs no-follow opens that Windows lacks; the bwrap check must
    # come first so the user sees the real cause.
    source = tmp_path / "input.txt"
    source.write_text("data")
    monkeypatch.setattr(runner_module, "bwrap_available", lambda: False)
    # ...and no container engine either, so no backend can run here.
    monkeypatch.setattr(OciBackend, "_find_engines", lambda self: [])

    def must_not_stage(*args, **kwargs):
        raise AssertionError("input must not be staged when bwrap is missing")

    monkeypatch.setattr(runner_module, "stage_input", must_not_stage)
    with pytest.raises(SandboxUnavailableError, match="Linux bubblewrap"):
        run_in_sandbox(SandboxConfig(
            command=["/usr/bin/true"], artifact_dir=tmp_path / "o", input_path=source,
        ))


def test_reparse_point_attribute_is_treated_as_link() -> None:
    from types import SimpleNamespace
    import stat as stat_mod

    regular = stat_mod.S_IFREG | 0o644
    plain = SimpleNamespace(st_mode=regular, st_file_attributes=0)
    reparse = SimpleNamespace(
        st_mode=regular, st_file_attributes=stat_mod.FILE_ATTRIBUTE_REPARSE_POINT
    )
    assert not hashing._is_link_or_reparse_point(plain)
    assert hashing._is_link_or_reparse_point(reparse)


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")
def test_windows_junction_parent_is_refused(tmp_path: Path) -> None:
    import _winapi  # type: ignore[import-not-found]

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("host secret")
    root = tmp_path / "root"
    root.mkdir()
    _winapi.CreateJunction(str(outside), str(root / "d"))
    with pytest.raises(UnsafeFileError):
        hashing.sha256_file_beneath(root, Path("d/f.txt"))


class TestStagingFallback:
    """Input staging where O_NOFOLLOW is missing (Windows), run on every OS.

    Wasm parsers run on Windows too, so their inputs must be stageable there.
    """

    @pytest.fixture(autouse=True)
    def force_fallback(self, monkeypatch):
        monkeypatch.setattr(staging, "RACE_FREE_NOFOLLOW", False)

    def test_regular_file_is_staged_byte_exact(self, tmp_path: Path) -> None:
        data = b"line one\r\nline two\n\x00\x1a binary tail"
        source = tmp_path / "input.bin"
        source.write_bytes(data)
        staged = staging.stage_regular_file(source, tmp_path / "stage")
        assert staged.staged_path.read_bytes() == data
        assert staged.sha256 == hashlib.sha256(data).hexdigest()

    def test_symlinked_file_is_refused(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("host secret")
        _symlink_or_skip(tmp_path / "link.txt", secret)
        with pytest.raises(staging.InputStagingError):
            staging.stage_regular_file(tmp_path / "link.txt", tmp_path / "stage")

    def test_file_swapped_after_check_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "input.txt"
        source.write_text("checked")
        other = tmp_path / "other.txt"
        other.write_text("swapped in")
        real_open = os.open

        def swapped_open(path, flags, *args, **kwargs):
            if Path(path) == source:
                path = other
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swapped_open)
        with pytest.raises(staging.InputStagingError, match="changed while being opened"):
            staging.stage_regular_file(source, tmp_path / "stage")

    def test_directory_inputs_are_refused(self, tmp_path: Path) -> None:
        (tmp_path / "corpus").mkdir()
        (tmp_path / "corpus" / "a.txt").write_text("a")
        with pytest.raises(staging.InputStagingError, match="O_NOFOLLOW"):
            staging.stage_directory(tmp_path / "corpus", tmp_path / "stage")
