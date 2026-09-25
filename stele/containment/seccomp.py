"""
Seccomp syscall filter for the bubblewrap backend (roadmap #6).

bwrap installs a classic-BPF program read from ``--seccomp <fd>`` immediately
before it execs the parser, so the filter covers the parser and everything it
starts. The program is generated here in pure Python with ``struct`` rather
than through libseccomp: the syscall numbers for the two supported
architectures are fixed kernel ABI, and a small generator keeps Stele free of
native runtime dependencies.

Policy: a denylist, not an allowlist. Python parser runtimes (CPython, NumPy,
PyTorch, OCR engines) touch a broad and version-dependent set of syscalls; an
allowlist tight enough to matter breaks them unpredictably, and one loose
enough to run them all adds little over blocking the dangerous families
explicitly. The filter instead:

* kills the process (``SECCOMP_RET_KILL_PROCESS``, reported as SIGSYS) on any
  syscall in ``KILLED_SYSCALLS``: debugging/cross-process memory access,
  kernel keyrings, eBPF and perf, kexec and kernel modules, every mount and
  namespace operation, userfaultfd, file-handle opens, and host-administration
  calls;
* kills ``clone`` when it asks for any new namespace (the flags are in a
  register the filter can read), so ``unshare``'s ban cannot be sidestepped;
* makes ``clone3`` and io_uring fail with ENOSYS: clone3 passes its flags in
  memory the filter cannot inspect, and glibc falls back to ``clone``; io_uring
  can issue operations the filter never sees, and runtimes fall back to
  ordinary I/O;
* refuses the ``TIOCSTI``/``TIOCLINUX`` terminal-injection ioctls with EPERM;
* kills anything issued through a non-native ABI (32-bit x86 via ``int 0x80``,
  x32, 32-bit ARM), whose different syscall numbers would otherwise bypass
  every rule above.

Only x86_64 and aarch64 are supported. On any other architecture
``filter_for_host()`` returns None and the bubblewrap backend does not claim
``Capability.SYSCALL_FILTER``.
"""
from __future__ import annotations

import os
import platform
import struct
import sys

# --- Kernel ABI constants (linux/seccomp.h, linux/filter.h, linux/audit.h) ---

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000

_BPF_LD_W_ABS = 0x20     # BPF_LD | BPF_W | BPF_ABS
_BPF_JEQ_K = 0x15        # BPF_JMP | BPF_JEQ | BPF_K
_BPF_JGE_K = 0x35        # BPF_JMP | BPF_JGE | BPF_K
_BPF_JSET_K = 0x45       # BPF_JMP | BPF_JSET | BPF_K
_BPF_RET_K = 0x06        # BPF_RET | BPF_K

# struct seccomp_data offsets. Both supported arches are little-endian, so the
# low 32 bits of a 64-bit argument sit at its start.
_OFF_NR = 0
_OFF_ARCH = 4


def _arg_low(index: int) -> int:
    return 16 + 8 * index


_EPERM = 1
_ENOSYS = 38

# Signal the kernel delivers for SECCOMP_RET_KILL_PROCESS (same on both arches).
SIGSYS = 31

# CLONE_NEWNS | NEWCGROUP | NEWUTS | NEWIPC | NEWUSER | NEWPID | NEWNET.
# CLONE_NEWTIME (0x80) is only accepted by clone3/unshare; in clone() that bit
# is part of the exit signal.
_CLONE_NEW_MASK = 0x7E020000

_X32_SYSCALL_BIT = 0x40000000

_TIOCSTI = 0x5412
_TIOCLINUX = 0x541C

#: Syscalls that kill the parser outright. Nothing a document parser does
#: legitimately needs these; reaching one means an escape or probe attempt.
KILLED_SYSCALLS = (
    # cross-process inspection and memory access
    "ptrace", "process_vm_readv", "process_vm_writev", "kcmp", "pidfd_getfd",
    "process_madvise",
    # kernel keyrings
    "keyctl", "add_key", "request_key",
    # eBPF, perf, userfaultfd: large kernel attack surface
    "bpf", "perf_event_open", "userfaultfd",
    # kexec and kernel modules
    "kexec_load", "kexec_file_load", "init_module", "finit_module", "delete_module",
    # mounts and namespaces
    "mount", "umount2", "pivot_root", "chroot", "unshare", "setns",
    "open_tree", "move_mount", "fsopen", "fsconfig", "fsmount", "fspick",
    "mount_setattr",
    # file-handle opens bypass path-based confinement (the "shocker" escape)
    "open_by_handle_at", "name_to_handle_at",
    # host administration
    "acct", "swapon", "swapoff", "reboot", "syslog", "quotactl", "quotactl_fd",
    "lookup_dcookie",
    # x86-only legacy/raw-hardware calls
    "iopl", "ioperm", "uselib", "_sysctl",
)

#: Syscalls refused with ENOSYS so runtimes take their fallback path.
ENOSYS_SYSCALLS = ("clone3", "io_uring_setup", "io_uring_enter", "io_uring_register")

_COMMON_NEW = {  # numbers shared by every architecture since Linux 5.1
    "open_tree": 428, "move_mount": 429, "fsopen": 430, "fsconfig": 431,
    "fsmount": 432, "fspick": 433, "clone3": 435, "pidfd_getfd": 438,
    "process_madvise": 440, "mount_setattr": 442, "quotactl_fd": 443,
    "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
}

_X86_64 = {
    "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311, "kcmp": 312,
    "keyctl": 250, "add_key": 248, "request_key": 249,
    "bpf": 321, "perf_event_open": 298, "userfaultfd": 323,
    "kexec_load": 246, "kexec_file_load": 320, "init_module": 175,
    "finit_module": 313, "delete_module": 176,
    "mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161,
    "unshare": 272, "setns": 308,
    "open_by_handle_at": 304, "name_to_handle_at": 303,
    "acct": 163, "swapon": 167, "swapoff": 168, "reboot": 169, "syslog": 103,
    "quotactl": 179, "lookup_dcookie": 212,
    "iopl": 172, "ioperm": 173, "uselib": 134, "_sysctl": 156,
    "clone": 56, "ioctl": 16,
    **_COMMON_NEW,
}

_AARCH64 = {  # asm-generic numbering
    "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271, "kcmp": 272,
    "keyctl": 219, "add_key": 217, "request_key": 218,
    "bpf": 280, "perf_event_open": 241, "userfaultfd": 282,
    "kexec_load": 104, "kexec_file_load": 294, "init_module": 105,
    "finit_module": 273, "delete_module": 106,
    "mount": 40, "umount2": 39, "pivot_root": 41, "chroot": 51,
    "unshare": 97, "setns": 268,
    "open_by_handle_at": 265, "name_to_handle_at": 264,
    "acct": 89, "swapon": 224, "swapoff": 225, "reboot": 142, "syslog": 116,
    "quotactl": 60, "lookup_dcookie": 18,
    "clone": 220, "ioctl": 29,
    **_COMMON_NEW,
}

# machine name -> (AUDIT_ARCH_*, syscall table, reject x32 numbers)
_ARCHES = {
    "x86_64": (0xC000003E, _X86_64, True),
    "aarch64": (0xC00000B7, _AARCH64, False),
}
_MACHINE_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}


def normalize_machine(machine: str) -> str | None:
    """Supported architecture name for a platform.machine() value, else None."""
    m = machine.lower()
    m = _MACHINE_ALIASES.get(m, m)
    return m if m in _ARCHES else None


def _stmt(code: int, k: int) -> tuple[int, int, int, int]:
    return (code, 0, 0, k)


def _jump(code: int, k: int, jt: int, jf: int) -> tuple[int, int, int, int]:
    return (code, jt, jf, k)


def build_filter(machine: str) -> bytes:
    """Compile the Stele policy into a raw ``struct sock_filter`` array.

    Raises ValueError for an unsupported architecture: there is no safe
    default program to fall back to.
    """
    arch = normalize_machine(machine)
    if arch is None:
        raise ValueError(f"no seccomp policy for architecture {machine!r}")
    audit_arch, table, reject_x32 = _ARCHES[arch]

    kill = SECCOMP_RET_KILL_PROCESS
    prog: list[tuple[int, int, int, int]] = [
        # Wrong ABI: its syscall numbers mean different calls, so kill.
        _stmt(_BPF_LD_W_ABS, _OFF_ARCH),
        _jump(_BPF_JEQ_K, audit_arch, 1, 0),
        _stmt(_BPF_RET_K, kill),
        _stmt(_BPF_LD_W_ABS, _OFF_NR),
    ]
    if reject_x32:
        prog += [
            _jump(_BPF_JGE_K, _X32_SYSCALL_BIT, 0, 1),
            _stmt(_BPF_RET_K, kill),
        ]

    # Each rule is "jeq nr -> next instruction is its verdict, else skip it".
    # Short relative jumps keep every offset well under the 8-bit limit.
    rules = [(n, kill) for n in KILLED_SYSCALLS if n in table]
    rules += [(n, SECCOMP_RET_ERRNO | _ENOSYS) for n in ENOSYS_SYSCALLS]
    for name, verdict in rules:
        prog += [
            _jump(_BPF_JEQ_K, table[name], 0, 1),
            _stmt(_BPF_RET_K, verdict),
        ]

    # clone(flags, ...): kill on any CLONE_NEW* flag, otherwise allow.
    prog += [
        _jump(_BPF_JEQ_K, table["clone"], 0, 4),
        _stmt(_BPF_LD_W_ABS, _arg_low(0)),
        _jump(_BPF_JSET_K, _CLONE_NEW_MASK, 0, 1),
        _stmt(_BPF_RET_K, kill),
        _stmt(_BPF_RET_K, SECCOMP_RET_ALLOW),
    ]

    # ioctl(fd, cmd, ...): the kernel truncates cmd to 32 bits, so the low
    # word is the whole command.
    injection = (_TIOCSTI, _TIOCLINUX)
    prog += [
        _jump(_BPF_JEQ_K, table["ioctl"], 0, 2 * len(injection) + 2),
        _stmt(_BPF_LD_W_ABS, _arg_low(1)),
    ]
    for cmd in injection:
        prog += [
            _jump(_BPF_JEQ_K, cmd, 0, 1),
            _stmt(_BPF_RET_K, SECCOMP_RET_ERRNO | _EPERM),
        ]
    prog += [_stmt(_BPF_RET_K, SECCOMP_RET_ALLOW)]

    prog += [_stmt(_BPF_RET_K, SECCOMP_RET_ALLOW)]
    return b"".join(struct.pack("<HBBI", *ins) for ins in prog)


def host_machine() -> str | None:
    """This host's supported architecture name, or None."""
    return normalize_machine(platform.machine())


def kernel_supports_filters() -> bool:
    """True when the running kernel has seccomp filter mode (CONFIG_SECCOMP_FILTER)."""
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            status = fh.read()
    except OSError:
        return False
    # "Seccomp_filters:" only appears when filter mode is compiled in.
    return "\nSeccomp_filters:" in status


def filter_for_host() -> bytes | None:
    """The compiled filter for this host, or None when it cannot be applied."""
    machine = host_machine()
    if machine is None or not kernel_supports_filters():
        return None
    return build_filter(machine)


def filter_fd(program: bytes) -> int:
    """An fd bwrap can read the program from; the caller closes it.

    A memfd leaves nothing on disk. It is created close-on-exec so no other
    child inherits it; the caller lists it in ``pass_fds`` for bwrap only.
    """
    fd = os.memfd_create("stele-seccomp", os.MFD_CLOEXEC)
    try:
        os.write(fd, program)
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd
