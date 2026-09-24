from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Fixed paths inside the sandbox — these never change regardless of host layout.
SANDBOX_OUTPUT = "/stele/output"
SANDBOX_INPUT_DIR = "/stele/input"
# Legacy alias — input file is mounted at SANDBOX_INPUT_DIR/<original_filename>
SANDBOX_INPUT = SANDBOX_INPUT_DIR
SANDBOX_SCRIPT = "/stele/parser"

# Read-only bind of the full /usr tree gives parsers access to system Python,
# standard libs, and common binaries without exposing writable host paths.
_SYS_RO_REQUIRED = ["/usr"]

# Lib directories may or may not exist depending on the host OS layout.
# --ro-bind-try silently skips missing sources.
_SYS_RO_TRY = ["/lib", "/lib64", "/lib32", "/lib/x86_64-linux-gnu"]

# These host directories are replaced with empty tmpfs mounts inside the sandbox.
# This makes them writable (so parsers don't crash on missing paths), but any
# writes are ephemeral — they vanish when the sandbox exits and never reach the host.
_EPHEMERAL_TMPFS = ["/home", "/mnt", "/root", "/run", "/var"]


@dataclass
class SandboxConfig:
    """Everything needed to construct and run one sandboxed parser invocation."""

    # The command to execute inside the sandbox, e.g. ["/usr/bin/python3", "/stele/parser"].
    command: list[str]

    # Host-side directory that will be bind-mounted read-write at /stele/output.
    # Must exist before calling run_in_sandbox; runner creates it automatically.
    artifact_dir: Path

    # Optional read-only regular input file exposed at /stele/input.
    # run_in_sandbox stages it into a private Stele-owned directory first.
    input_path: Path | None = None

    # Optional parser script exposed at /stele/parser (read-only).
    # Useful when the command is ["/usr/bin/python3", "/stele/parser"].
    script_path: Path | None = None

    # Additional (src, sandbox_dest) read-only bind mounts, e.g. for venvs or
    # model weight directories. src must be an absolute host path.
    extra_ro_binds: list[tuple[str, str]] = field(default_factory=list)

    # Environment variables to set inside the sandbox (all others are cleared).
    env: dict[str, str] = field(default_factory=dict)

    timeout_seconds: int = 300


class BubblewrapSandbox:
    """Builds the bwrap(1) argv for a SandboxConfig."""

    def build_argv(self, config: SandboxConfig) -> list[str]:
        argv: list[str] = ["bwrap"]

        # --- Filesystem layout ---

        for path in _SYS_RO_REQUIRED:
            argv += ["--ro-bind", path, path]

        for path in _SYS_RO_TRY:
            argv += ["--ro-bind-try", path, path]

        # Standard virtual filesystems
        argv += ["--proc", "/proc"]
        argv += ["--dev", "/dev"]

        # Isolated /tmp: writable but ephemeral — disappears on sandbox exit.
        argv += ["--tmpfs", "/tmp"]

        # Block production paths: replace with empty, ephemeral tmpfs.
        # Parsers can still call open('/home/x', 'w') without crashing, but
        # the write goes nowhere durable.
        for path in _EPHEMERAL_TMPFS:
            argv += ["--tmpfs", path]

        # Writable artifact output: the ONLY path that survives sandbox exit.
        config.artifact_dir.mkdir(parents=True, exist_ok=True)
        argv += ["--bind", str(config.artifact_dir), SANDBOX_OUTPUT]

        # Optional read-only input (preserves original filename/extension inside sandbox)
        if config.input_path is not None:
            input_dest = f"{SANDBOX_INPUT_DIR}/{config.input_path.name}"
            argv += ["--ro-bind", str(config.input_path), input_dest]

        # Optional parser script
        if config.script_path is not None:
            argv += ["--ro-bind", str(config.script_path), SANDBOX_SCRIPT]

        # Caller-supplied extra read-only binds (venvs, weights, etc.)
        for src, dst in config.extra_ro_binds:
            argv += ["--ro-bind", src, dst]

        # --- Namespace isolation ---
        argv += [
            "--unshare-user",  # gain capabilities only inside a private user namespace
            "--uid", "0",
            "--gid", "0",
            "--unshare-net",   # no network
            "--unshare-pid",   # isolated PID namespace
            "--unshare-uts",   # isolated hostname
            "--unshare-ipc",   # isolated IPC
            "--die-with-parent",  # sandbox dies if the runner process dies
        ]

        # --- Environment ---
        # Clear everything, then inject a minimal known-good environment.
        argv += ["--clearenv"]
        argv += ["--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin"]
        argv += ["--setenv", "STELE_OUTPUT_DIR", SANDBOX_OUTPUT]
        if config.input_path is not None:
            input_dest = f"{SANDBOX_INPUT_DIR}/{config.input_path.name}"
            argv += ["--setenv", "STELE_INPUT_PATH", input_dest]
        for key, val in config.env.items():
            argv += ["--setenv", key, val]

        # --- Working directory and command ---
        argv += ["--chdir", SANDBOX_OUTPUT]
        argv += ["--"] + config.command

        return argv
