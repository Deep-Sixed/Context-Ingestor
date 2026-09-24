"""
Phase E containment proof tests.

These four tests prove the four required properties:

  PASS 1 — Parser runs inside bubblewrap and produces an allowed artifact.
  PASS 2 — Parser cannot write outside approved directories (home, mnt).
  PASS 3 — Exit code, stdout, stderr are faithfully captured.
  PASS 4 — Sandbox failure / ephemeral writes do not produce committed artifacts.

Run with:
    cd EVECOR/services/stele
    uv run pytest tests/test_phase_e_containment.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import SandboxConfig

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON = "/usr/bin/python3.14"


def _config(script: str, artifact_dir: Path, **kwargs) -> SandboxConfig:
    return SandboxConfig(
        command=[PYTHON, SANDBOX_SCRIPT := "/stele/parser"],
        artifact_dir=artifact_dir,
        script_path=FIXTURES / script,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# PASS 1 — Allowed artifact emitted
# ---------------------------------------------------------------------------

class TestAllowedArtifact:
    """Parser runs inside bubblewrap and its output reaches the host via artifact_dir."""

    def test_artifact_written_to_output_dir(self, tmp_path: Path) -> None:
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / "artifacts",
            script_path=FIXTURES / "parser_emit_artifact.py",
        )
        result = run_in_sandbox(config)

        assert result.exit_code == 0, f"unexpected exit: {result.stderr}"
        assert result.succeeded
        assert result.produced_artifacts, "no artifacts found — containment may have blocked the write"

        artifact_file = result.artifact_dir / "result.json"
        assert artifact_file.exists(), f"expected {artifact_file} on host"

        data = json.loads(artifact_file.read_text())
        assert data["parser"] == "fake_parser_v0"
        assert len(data["chunks"]) == 1

    def test_artifact_paths_listed_in_result(self, tmp_path: Path) -> None:
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / "artifacts",
            script_path=FIXTURES / "parser_emit_artifact.py",
        )
        result = run_in_sandbox(config)

        assert len(result.artifact_paths) == 1
        assert result.artifact_paths[0].name == "result.json"


# ---------------------------------------------------------------------------
# PASS 2 — Forbidden writes blocked
# ---------------------------------------------------------------------------

class TestForbiddenWrites:
    """Parser cannot write outside /stele/output — attempts raise OSError inside sandbox."""

    def test_write_to_home_blocked(self, tmp_path: Path) -> None:
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / "artifacts",
            script_path=FIXTURES / "parser_write_home.py",
        )
        result = run_in_sandbox(config)

        # The fake parser exits 0 only if the write SUCCEEDS (containment failure).
        # Containment pass: exit_code == 1 and no evil file on host.
        assert result.exit_code == 1, (
            "write to /home/jarvis/ succeeded inside sandbox — containment FAILED"
        )
        assert not Path("/home/jarvis/stele_evil_write.txt").exists(), (
            "evil file appeared on host — containment FAILED"
        )
        assert "blocked" in result.stdout

    def test_write_to_mnt_production_path_blocked(self, tmp_path: Path) -> None:
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / "artifacts",
            script_path=FIXTURES / "parser_write_mnt.py",
        )
        evil_path = Path(
            "/mnt/jarvis-data/projects/EVECOR/services/stele/stele_evil_write.txt"
        )
        result = run_in_sandbox(config)

        assert result.exit_code == 1, (
            "write to production /mnt path succeeded inside sandbox — containment FAILED"
        )
        assert not evil_path.exists(), "evil file appeared on host — containment FAILED"
        assert "blocked" in result.stdout

    def test_no_artifacts_from_forbidden_writer(self, tmp_path: Path) -> None:
        """A parser that only attempts forbidden writes must produce zero artifacts."""
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / "artifacts",
            script_path=FIXTURES / "parser_write_home.py",
        )
        result = run_in_sandbox(config)
        assert not result.produced_artifacts


# ---------------------------------------------------------------------------
# PASS 3 — Exit status, stdout, stderr captured
# ---------------------------------------------------------------------------

class TestExitStatusCapture:
    """Exit code, stdout, and stderr are faithfully captured regardless of value."""

    @pytest.mark.parametrize("code", [0, 1, 2, 42, 127])
    def test_exit_code_captured(self, tmp_path: Path, code: int) -> None:
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / f"artifacts_{code}",
            script_path=FIXTURES / "parser_exit_code.py",
            env={"STELE_TEST_EXIT_CODE": str(code)},
        )
        result = run_in_sandbox(config)
        assert result.exit_code == code, f"expected exit {code}, got {result.exit_code}"

    def test_stdout_captured(self, tmp_path: Path) -> None:
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / "artifacts",
            script_path=FIXTURES / "parser_exit_code.py",
            env={"STELE_TEST_EXIT_CODE": "0"},
        )
        result = run_in_sandbox(config)
        assert "exiting with code 0" in result.stdout

    def test_stderr_captured_on_syntax_error(self, tmp_path: Path) -> None:
        """A parser that crashes produces a non-zero exit and stderr."""
        config = SandboxConfig(
            command=[PYTHON, "-c", "raise RuntimeError('deliberate crash')"],
            artifact_dir=tmp_path / "artifacts",
        )
        result = run_in_sandbox(config)
        assert result.exit_code != 0
        assert "RuntimeError" in result.stderr


# ---------------------------------------------------------------------------
# PASS 4 — No durable write path reachable from parser
# ---------------------------------------------------------------------------

class TestNoDurableWriteOutsideArtifactDir:
    """
    A parser that writes ONLY to /tmp (ephemeral sandbox tmpfs) must leave
    zero files on the host — including in artifact_dir.
    """

    def test_tmp_write_is_ephemeral(self, tmp_path: Path) -> None:
        config = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=tmp_path / "artifacts",
            script_path=FIXTURES / "parser_write_tmp_only.py",
        )
        result = run_in_sandbox(config)

        assert result.exit_code == 0, f"parser failed unexpectedly: {result.stderr}"

        # The write to /tmp inside the sandbox must NOT appear on the host.
        assert not result.produced_artifacts, (
            "file written to /tmp inside sandbox leaked into artifact_dir — "
            "this would mean /tmp was incorrectly mapped to artifact_dir"
        )
        host_tmp = Path("/tmp/stele_secret_tmp.txt")
        assert not host_tmp.exists(), (
            "file written to /tmp inside sandbox appeared on the host — "
            "sandbox /tmp isolation FAILED"
        )

    def test_failed_parser_produces_no_artifacts(self, tmp_path: Path) -> None:
        """A crashing parser must not leave partial artifacts that could be committed."""
        config = SandboxConfig(
            command=[PYTHON, "-c", "raise RuntimeError('crash before any write')"],
            artifact_dir=tmp_path / "artifacts",
        )
        result = run_in_sandbox(config)
        assert not result.succeeded
        assert not result.produced_artifacts, (
            "a crashing parser left artifacts — Phase F must never commit these"
        )


# ---------------------------------------------------------------------------
# Regression — input filename/extension preserved inside sandbox
# ---------------------------------------------------------------------------

class TestInputExtensionPreserved:

    def test_sandbox_input_mount_includes_original_filename(self, tmp_path: Path) -> None:
        from stele.containment.sandbox import BubblewrapSandbox

        inp = tmp_path / "live-ingest-sample.md"
        inp.write_text("sample")
        art = tmp_path / "artifacts"
        art.mkdir()
        script = tmp_path / "parser.py"
        script.write_text("# stub")

        cfg = SandboxConfig(
            command=[PYTHON, "/stele/parser"],
            artifact_dir=art,
            input_path=inp,
            script_path=script,
        )
        argv = BubblewrapSandbox().build_argv(cfg)

        # Input must be mounted with original extension so parsers can route by suffix.
        assert any(
            a == "--ro-bind" and argv[i + 1] == str(inp) and argv[i + 2].endswith("/live-ingest-sample.md")
            for i, a in enumerate(argv)
            if a == "--ro-bind"
        )

        env_idx = argv.index("STELE_INPUT_PATH")
        assert argv[env_idx + 1].endswith("/live-ingest-sample.md")
