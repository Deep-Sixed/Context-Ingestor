"""
Roadmap #6 — Linux hardening: seccomp, Landlock, exec allowlist.

  - The generated seccomp program is checked instruction by instruction with a
    small classic-BPF interpreter, for x86_64 and aarch64, on every OS.
  - Bubblewrap claims SYSCALL_FILTER only where it installs the filter.
  - Live: a parser making a blocked syscall is killed and the run result
    reports the violation; non-native ABIs are killed too.
  - Live: under Landlock, writes outside /stele/output fail even on the
    writable tmpfs mounts; the mount layout alone also refuses writes.
  - Live: exec outside the allowlist fails.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
from pathlib import Path

import pytest

from stele.containment import landlock, seccomp
from stele.containment import backend as backend_module
from stele.containment.backend import (
    SECCOMP_VIOLATION,
    BubblewrapBackend,
    Capability,
    ExecutionOutcome,
)
from stele.containment.runner import run_in_sandbox
from stele.containment.sandbox import (
    SANDBOX_LANDLOCK,
    BubblewrapSandbox,
    LandlockLaunch,
    SandboxConfig,
)

PYTHON = str(Path(sys.executable).resolve())
LINUX_BWRAP = sys.platform == "linux" and shutil.which("bwrap") is not None
requires_bwrap = pytest.mark.skipif(
    not LINUX_BWRAP, reason="live containment proof requires Linux bubblewrap",
)
# CI must run these: its skip check fails the build on either reason below.
requires_seccomp = pytest.mark.skipif(
    not LINUX_BWRAP or seccomp.filter_for_host() is None,
    reason="live seccomp proof requires Linux bubblewrap on x86_64/aarch64",
)
requires_landlock = pytest.mark.skipif(
    not LINUX_BWRAP or BubblewrapBackend().landlock_launch() is None,
    reason="live Landlock proof requires Linux bubblewrap and a Landlock kernel",
)
requires_x86_64 = pytest.mark.skipif(
    seccomp.host_machine() != "x86_64", reason="x86 ABI bypass proof needs an x86_64 host",
)

AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
AUDIT_ARCH_I386 = 0x40000003
AUDIT_ARCH_ARM = 0x40000028
KILL = seccomp.SECCOMP_RET_KILL_PROCESS
ALLOW = seccomp.SECCOMP_RET_ALLOW
ENOSYS = seccomp.SECCOMP_RET_ERRNO | 38
EPERM = seccomp.SECCOMP_RET_ERRNO | 1

REQUIRED_BLOCKED = [
    "ptrace", "process_vm_readv", "process_vm_writev", "keyctl", "add_key",
    "request_key", "bpf", "perf_event_open", "kexec_load", "kexec_file_load",
    "mount", "umount2", "pivot_root", "unshare", "setns", "init_module",
    "finit_module", "delete_module", "userfaultfd",
]


# ---------------------------------------------------------------------------
# A classic-BPF interpreter for the subset the generator emits
# ---------------------------------------------------------------------------

def run_bpf(program: bytes, *, arch: int, nr: int, args: tuple[int, ...] = ()) -> int:
    args = tuple(args) + (0,) * (6 - len(args))
    data = struct.pack("<iIQ6Q", nr, arch, 0, *args)
    insns = [struct.unpack_from("<HBBI", program, off) for off in range(0, len(program), 8)]
    acc = 0
    pc = 0
    while True:
        code, jt, jf, k = insns[pc]
        if code == 0x20:            # ld [k]
            acc = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == 0x06:          # ret k
            return k
        elif code in (0x15, 0x35, 0x45):
            taken = {0x15: acc == k, 0x35: acc >= k, 0x45: bool(acc & k)}[code]
            pc += 1 + (jt if taken else jf)
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")


@pytest.fixture(params=["x86_64", "aarch64"])
def arch(request):
    return request.param


def _verdict(arch: str, name: str, args: tuple[int, ...] = ()) -> int:
    table = seccomp._ARCHES[arch][1]
    return run_bpf(seccomp.build_filter(arch), arch=AUDIT_ARCH[arch], nr=table[name], args=args)


class TestFilterProgram:

    @pytest.mark.parametrize("name", REQUIRED_BLOCKED)
    def test_required_syscalls_kill(self, arch: str, name: str) -> None:
        assert _verdict(arch, name) == KILL

    def test_every_listed_syscall_kills(self, arch: str) -> None:
        table = seccomp._ARCHES[arch][1]
        for name in seccomp.KILLED_SYSCALLS:
            if name in table:
                assert _verdict(arch, name) == KILL, name

    def test_ordinary_syscalls_allowed(self, arch: str) -> None:
        # read, write, openat, mmap, execve, getpid on each arch.
        ordinary = {"x86_64": [0, 1, 257, 9, 59, 39], "aarch64": [63, 64, 56, 222, 221, 172]}
        program = seccomp.build_filter(arch)
        for nr in ordinary[arch]:
            assert run_bpf(program, arch=AUDIT_ARCH[arch], nr=nr) == ALLOW, nr

    def test_non_native_abi_is_killed(self, arch: str) -> None:
        program = seccomp.build_filter(arch)
        foreign = AUDIT_ARCH_I386 if arch == "x86_64" else AUDIT_ARCH_ARM
        # 20 is getpid on i386 and harmless on ARM: the ABI alone is fatal.
        assert run_bpf(program, arch=foreign, nr=20) == KILL

    def test_x32_numbers_are_killed(self) -> None:
        program = seccomp.build_filter("x86_64")
        getpid_x32 = 0x40000000 | 39
        assert run_bpf(program, arch=AUDIT_ARCH["x86_64"], nr=getpid_x32) == KILL

    def test_clone_into_new_namespace_is_killed(self, arch: str) -> None:
        for flag in (0x10000000, 0x20000000, 0x40000000, 0x00020000, 0x08000000,
                     0x04000000, 0x02000000):
            assert _verdict(arch, "clone", (flag | 17,)) == KILL, hex(flag)

    def test_plain_clone_allowed(self, arch: str) -> None:
        fork_flags = 0x01200011       # CLONE_CHILD_SETTID|CLONE_CHILD_CLEARTID|SIGCHLD
        thread_flags = 0x003D0F00     # what glibc passes for pthread_create
        assert _verdict(arch, "clone", (fork_flags,)) == ALLOW
        assert _verdict(arch, "clone", (thread_flags,)) == ALLOW

    def test_clone3_and_io_uring_report_enosys(self, arch: str) -> None:
        for name in seccomp.ENOSYS_SYSCALLS:
            assert _verdict(arch, name) == ENOSYS, name

    def test_terminal_injection_ioctls_refused(self, arch: str) -> None:
        assert _verdict(arch, "ioctl", (1, 0x5412)) == EPERM      # TIOCSTI
        assert _verdict(arch, "ioctl", (1, 0x541C)) == EPERM      # TIOCLINUX
        assert _verdict(arch, "ioctl", (1, 0x5401)) == ALLOW      # TCGETS

    def test_unsupported_architecture_has_no_filter(self) -> None:
        with pytest.raises(ValueError):
            seccomp.build_filter("riscv64")
        assert seccomp.normalize_machine("AMD64") == "x86_64"
        assert seccomp.normalize_machine("arm64") == "aarch64"


# ---------------------------------------------------------------------------
# Capability claims and argv
# ---------------------------------------------------------------------------

class TestCapabilityClaims:

    def test_no_syscall_filter_claim_on_unsupported_arch(self, monkeypatch) -> None:
        monkeypatch.setattr(seccomp, "host_machine", lambda: None)
        assert Capability.SYSCALL_FILTER not in BubblewrapBackend().capabilities()

    def test_no_syscall_filter_claim_without_kernel_support(self, monkeypatch) -> None:
        monkeypatch.setattr(seccomp, "kernel_supports_filters", lambda: False)
        assert Capability.SYSCALL_FILTER not in BubblewrapBackend().capabilities()

    @requires_seccomp
    def test_linux_host_claims_syscall_filter(self) -> None:
        assert Capability.SYSCALL_FILTER in BubblewrapBackend().capabilities()


class TestArgv:

    def _config(self, tmp_path: Path, **kwargs) -> SandboxConfig:
        return SandboxConfig(command=["/usr/bin/true"], artifact_dir=tmp_path / "o", **kwargs)

    def test_seccomp_fd_is_passed_before_command(self, tmp_path: Path) -> None:
        argv = BubblewrapSandbox().build_argv(self._config(tmp_path), seccomp_fd=7)
        i = argv.index("--seccomp")
        assert argv[i + 1] == "7"
        assert i < argv.index("--")

    def test_no_hardening_args_by_default(self, tmp_path: Path) -> None:
        argv = BubblewrapSandbox().build_argv(self._config(tmp_path))
        assert "--seccomp" not in argv
        assert argv[argv.index("--") + 1:] == ["/usr/bin/true"]

    def test_landlock_launcher_wraps_command(self, tmp_path: Path) -> None:
        launch = LandlockLaunch(python="/usr/bin/python3", script="/host/landlock.py")
        argv = BubblewrapSandbox().build_argv(
            self._config(tmp_path, exec_allowlist=["/usr/bin/tesseract"]), landlock=launch,
        )
        assert argv[argv.index("/host/landlock.py") + 1] == SANDBOX_LANDLOCK
        tail = argv[argv.index("--") + 1:]
        assert tail[:5] == ["/usr/bin/python3", "-I", "-S", "-B", SANDBOX_LANDLOCK]
        policy = tail[5:tail.index("--")]
        assert ["--write", "/stele/output"] == policy[:2]
        assert "/tmp" not in policy
        assert policy[policy.index("--exec") + 1] == "/usr/bin/tesseract"
        assert tail[tail.index("--") + 1:] == ["/usr/bin/true"]

    def test_writable_scratch_adds_tmp(self, tmp_path: Path) -> None:
        launch = LandlockLaunch(python="/usr/bin/python3", script="/host/landlock.py")
        argv = BubblewrapSandbox().build_argv(
            self._config(tmp_path, writable_scratch=True), landlock=launch,
        )
        i = argv.index("/tmp", argv.index(SANDBOX_LANDLOCK, argv.index("--")))
        assert argv[i - 1] == "--write"


class TestLauncherUnits:

    def test_shebang_interpreter_found(self, tmp_path: Path) -> None:
        script = tmp_path / "tool"
        script.write_bytes(b"#!/usr/bin/perl -w\nprint 1;\n")
        assert landlock.interpreters(str(script))[0] == "/usr/bin/perl"

    @pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux ELF interpreter")
    def test_elf_loader_found(self) -> None:
        interps = landlock.interpreters(PYTHON)
        assert interps and "ld-linux" in interps[0]

    def test_bad_arguments_fail_closed(self) -> None:
        assert landlock.main(["--bogus", "x", "--", "/usr/bin/true"]) == 126
        assert landlock.main(["--write", "/stele/output", "--"]) == 126

    def test_write_rights_grow_with_abi(self) -> None:
        assert not landlock.write_access(1) & landlock.TRUNCATE
        assert landlock.write_access(3) & landlock.TRUNCATE
        assert landlock.write_access(3) & landlock.REFER
        assert not landlock.write_access(7) & landlock.EXECUTE


# ---------------------------------------------------------------------------
# Live proofs
# ---------------------------------------------------------------------------

def _run(tmp_path: Path, code: str, **kwargs):
    return run_in_sandbox(
        SandboxConfig(command=[PYTHON, "-c", code], artifact_dir=tmp_path / "out", **kwargs),
        backend=BubblewrapBackend(),
    )


def _syscall_code(nr: int, *args: int) -> str:
    return (
        "import ctypes\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "print('before', flush=True)\n"
        f"r = libc.syscall({nr}, {', '.join(str(a) for a in args) or '0'})\n"
        "print('survived', r, ctypes.get_errno(), flush=True)\n"
    )


@requires_seccomp
class TestSeccompLive:

    @pytest.mark.parametrize("name", [
        "ptrace", "process_vm_readv", "keyctl", "bpf", "perf_event_open",
        "mount", "unshare", "setns", "userfaultfd", "kexec_load", "init_module",
    ])
    def test_blocked_syscall_kills_parser_and_is_reported(self, tmp_path: Path, name: str) -> None:
        nr = seccomp._ARCHES[seccomp.host_machine()][1][name]
        result = _run(tmp_path, _syscall_code(nr))
        assert "before" in result.stdout
        assert "survived" not in result.stdout
        assert result.exit_code == 128 + seccomp.SIGSYS
        assert result.violation == SECCOMP_VIOLATION
        assert not result.succeeded
        assert "seccomp" in result.hardening

    @pytest.mark.skipif(not hasattr(os, "unshare"), reason="os.unshare needs Python 3.12+")
    def test_unshare_via_os_module_is_killed(self, tmp_path: Path) -> None:
        result = _run(tmp_path, "import os; os.unshare(os.CLONE_NEWUSER); print('survived')")
        assert result.violation == SECCOMP_VIOLATION
        assert "survived" not in result.stdout

    def test_clone_into_new_user_namespace_is_killed(self, tmp_path: Path) -> None:
        clone = seccomp._ARCHES[seccomp.host_machine()][1]["clone"]
        # CLONE_NEWUSER | SIGCHLD with no stack: the filter runs before the kernel.
        result = _run(tmp_path, _syscall_code(clone, 0x10000000 | 17, 0))
        assert result.violation == SECCOMP_VIOLATION

    def test_normal_parser_work_is_unaffected(self, tmp_path: Path) -> None:
        # Threads (clone3 -> clone fallback), fork+exec of the allowed
        # interpreter, and ordinary file output all still work.
        code = (
            "import os, subprocess, sys, threading\n"
            "t = threading.Thread(target=lambda: None); t.start(); t.join()\n"
            "r = subprocess.run([sys.executable, '-c', 'print(6*7)'], capture_output=True, text=True)\n"
            "open(os.environ['STELE_OUTPUT_DIR'] + '/ok.txt', 'w').write(r.stdout)\n"
        )
        result = _run(tmp_path, code)
        assert result.succeeded, result.stderr
        assert result.violation is None
        assert (tmp_path / "out" / "ok.txt").read_text().strip() == "42"

    def test_tiocsti_is_refused(self, tmp_path: Path) -> None:
        code = (
            "import fcntl, termios\n"
            "fd = open('/dev/null', 'rb')\n"
            "try:\n"
            "    fcntl.ioctl(fd, termios.TIOCSTI, b'x')\n"
            "except OSError as e:\n"
            "    print('errno', e.errno)\n"
        )
        result = _run(tmp_path, code)
        # Without the filter /dev/null answers ENOTTY (25); the filter says EPERM.
        assert "errno 1" in result.stdout, result.stdout + result.stderr

    @requires_x86_64
    def test_i386_int80_abi_is_killed(self, tmp_path: Path) -> None:
        # mov eax, 20 (i386 getpid); int 0x80; ret — the arch check alone kills it.
        code = (
            "import ctypes, mmap\n"
            "buf = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)\n"
            "buf.write(bytes([0xb8, 20, 0, 0, 0, 0xcd, 0x80, 0xc3]))\n"
            "addr = ctypes.addressof(ctypes.c_char.from_buffer(buf))\n"
            "print('before', flush=True)\n"
            "ctypes.CFUNCTYPE(ctypes.c_int)(addr)()\n"
            "print('survived', flush=True)\n"
        )
        result = _run(tmp_path, code)
        assert "before" in result.stdout
        assert result.violation == SECCOMP_VIOLATION

    @requires_x86_64
    def test_x32_abi_is_killed(self, tmp_path: Path) -> None:
        result = _run(tmp_path, _syscall_code(0x40000000 | 39))
        assert result.violation == SECCOMP_VIOLATION

    def test_ordinary_nonzero_exit_is_not_a_violation(self, tmp_path: Path) -> None:
        result = _run(tmp_path, "raise SystemExit(3)")
        assert result.exit_code == 3
        assert result.violation is None


@requires_landlock
class TestLandlockLive:

    def _write(self, path: str) -> str:
        return (
            "import sys\n"
            "try:\n"
            f"    open({path!r}, 'w').write('x')\n"
            "    print('WROTE')\n"
            "except OSError as e:\n"
            "    print('blocked', e.errno)\n"
        )

    def test_landlock_is_applied(self, tmp_path: Path) -> None:
        result = _run(tmp_path, "print('hi')")
        assert result.succeeded, result.stderr
        assert "landlock" in result.hardening

    @pytest.mark.parametrize("path", ["/tmp/x", "/home/x", "/var/x", "/run/x", "/root/x"])
    def test_writes_to_writable_tmpfs_are_refused(self, tmp_path: Path, path: str) -> None:
        # The mount layout makes these writable tmpfs; only Landlock stops them.
        result = _run(tmp_path, self._write(path))
        assert "blocked 13" in result.stdout, result.stdout + result.stderr

    def test_output_dir_and_dev_null_stay_writable(self, tmp_path: Path) -> None:
        code = (
            "import os\n"
            "os.makedirs('/stele/output/sub/deeper')\n"
            "open('/stele/output/sub/deeper/a.txt', 'w').write('a')\n"
            "os.rename('/stele/output/sub/deeper/a.txt', '/stele/output/b.txt')\n"
            "open(os.devnull, 'w').write('discard')\n"
            "print('ok')\n"
        )
        result = _run(tmp_path, code)
        assert result.succeeded, result.stderr
        assert (tmp_path / "out" / "b.txt").read_text() == "a"

    def test_writable_scratch_allows_tmp(self, tmp_path: Path) -> None:
        result = _run(tmp_path, self._write("/tmp/x"), writable_scratch=True)
        assert "WROTE" in result.stdout, result.stderr

    def test_exec_outside_allowlist_fails(self, tmp_path: Path) -> None:
        code = (
            "import subprocess\n"
            "try:\n"
            "    subprocess.run(['/usr/bin/true'])\n"
            "    print('EXECUTED')\n"
            "except PermissionError:\n"
            "    print('exec blocked')\n"
        )
        result = _run(tmp_path, code)
        assert "exec blocked" in result.stdout, result.stdout + result.stderr

    def test_exec_allowlist_permits_named_program(self, tmp_path: Path) -> None:
        code = "import subprocess; print(subprocess.run(['/usr/bin/true']).returncode)"
        result = _run(tmp_path, code, exec_allowlist=["/usr/bin/true"])
        assert result.stdout.strip() == "0", result.stderr

    def test_binary_dropped_into_output_cannot_run(self, tmp_path: Path) -> None:
        code = (
            "import os, shutil, subprocess, sys\n"
            "shutil.copy(sys.executable, '/stele/output/py')\n"
            "os.chmod('/stele/output/py', 0o755)\n"
            "try:\n"
            "    subprocess.run(['/stele/output/py', '-c', 'pass'])\n"
            "    print('EXECUTED')\n"
            "except PermissionError:\n"
            "    print('exec blocked')\n"
        )
        result = _run(tmp_path, code)
        assert "exec blocked" in result.stdout, result.stdout + result.stderr


@requires_bwrap
class TestMountLayoutWithoutLandlock:
    """The mount namespace refuses writes on its own, without Landlock."""

    @pytest.fixture(autouse=True)
    def _no_landlock(self, monkeypatch):
        monkeypatch.setattr(BubblewrapBackend, "landlock_launch", lambda self: None)

    @pytest.mark.parametrize("path", ["/usr/stele-evil", "/stele-evil", "/stele/evil"])
    def test_writes_outside_writable_mounts_fail(self, tmp_path: Path, path: str) -> None:
        code = (
            "try:\n"
            f"    open({path!r}, 'w').write('x')\n"
            "    print('WROTE')\n"
            "except OSError as e:\n"
            "    print('blocked', e.errno)\n"
        )
        result = _run(tmp_path, code)
        assert "blocked" in result.stdout, result.stdout + result.stderr
        assert "landlock" not in result.hardening
        assert not Path(path).exists()


class TestOutcomeReporting:

    def test_violation_travels_to_run_result(self, tmp_path: Path, monkeypatch) -> None:
        class Killed(BubblewrapBackend):
            def available(self):
                return True

            def capabilities(self):
                return frozenset(Capability)

            def execute(self, config):
                return ExecutionOutcome(
                    exit_code=159, stdout="", stderr="", wall_time_seconds=0.0,
                    hardening=("seccomp",), violation=SECCOMP_VIOLATION,
                )

        result = run_in_sandbox(
            SandboxConfig(command=["/usr/bin/true"], artifact_dir=tmp_path / "o"),
            backend=Killed(),
        )
        assert result.violation == SECCOMP_VIOLATION
        assert result.hardening == ("seccomp",)
        assert not result.succeeded
