from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Fixed paths inside the sandbox — these never change regardless of host layout.
SANDBOX_OUTPUT = "/stele/output"
SANDBOX_INPUT_DIR = "/stele/input"
# Legacy alias — input file is mounted at SANDBOX_INPUT_DIR/<original_filename>
SANDBOX_INPUT = SANDBOX_INPUT_DIR
SANDBOX_SCRIPT = "/stele/parser"
# Landlock launcher (stele/containment/landlock.py), bound read-only.
SANDBOX_LANDLOCK = "/stele/landlock"

# Device sinks that stay writable under Landlock: libraries routinely open
# /dev/null for writing, and nothing written to these persists.
_LANDLOCK_WRITE_DEVICES = ["/dev/null", "/dev/zero", "/dev/full"]

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

    # Extra programs (sandbox paths, or names looked up on PATH) the parser may
    # exec. Where Landlock is enforced, command[0] and the interpreters it
    # needs are the only other executables; everything else fails with EACCES.
    exec_allowlist: list[str] = field(default_factory=list)

    # Let the parser write to the ephemeral /tmp tmpfs as well as
    # /stele/output. Off by default: where Landlock is enforced, writes are
    # possible only under /stele/output. /tmp is never persisted either way.
    writable_scratch: bool = False


@dataclass(frozen=True)
class LandlockLaunch:
    """How to start the Landlock launcher inside the sandbox."""

    # Interpreter visible inside the sandbox that runs the launcher script.
    python: str
    # Host path of stele/containment/landlock.py, bound at SANDBOX_LANDLOCK.
    script: str


class BubblewrapSandbox:
    """Builds the bwrap(1) argv for a SandboxConfig."""

    def build_argv(
        self,
        config: SandboxConfig,
        *,
        seccomp_fd: int | None = None,
        landlock: LandlockLaunch | None = None,
    ) -> list[str]:
        """bwrap argv for config.

        seccomp_fd: an fd (passed to bwrap via pass_fds) holding the compiled
        BPF program; bwrap installs it just before exec.
        landlock: when set, the command runs through the Landlock launcher,
        which confines writes and exec and then execs the command.
        """
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

        if landlock is not None:
            argv += ["--ro-bind", landlock.script, SANDBOX_LANDLOCK]

        # bwrap builds the layout on a tmpfs root, which is writable unless
        # remounted. Make it read-only so stray writes (/, /stele, /etc) fail
        # instead of landing in ephemeral memory; the tmpfs and bind mounts
        # above keep their own flags.
        argv += ["--remount-ro", "/"]

        # --- Namespace isolation ---
        argv += [
            "--unshare-user",  # gain capabilities only inside a private user namespace
            "--uid", "0",
            "--gid", "0",
            "--unshare-net",   # no network
            "--unshare-pid",   # isolated PID namespace
            "--unshare-uts",   # isolated hostname
            "--unshare-ipc",   # isolated IPC
            "--unshare-cgroup-try",  # isolated cgroup view, where supported
            "--new-session",   # own session: no TIOCSTI keystroke injection into the caller's terminal
            "--die-with-parent",  # sandbox dies if the runner process dies
        ]

        # --- Syscall filter ---
        if seccomp_fd is not None:
            argv += ["--seccomp", str(seccomp_fd)]

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
        argv += ["--"] + self._landlock_prefix(config, landlock) + config.command

        return argv

    @staticmethod
    def _landlock_prefix(config: SandboxConfig, landlock: LandlockLaunch | None) -> list[str]:
        if landlock is None:
            return []
        # -I: ignore PYTHON* env and user site; -S: skip site; -B: no .pyc writes.
        argv = [landlock.python, "-I", "-S", "-B", SANDBOX_LANDLOCK, "--write", SANDBOX_OUTPUT]
        if config.writable_scratch:
            argv += ["--write", "/tmp"]
        for device in _LANDLOCK_WRITE_DEVICES:
            argv += ["--write-dev", device]
        for program in config.exec_allowlist:
            argv += ["--exec", program]
        return argv + ["--"]
