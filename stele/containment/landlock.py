"""
Landlock launcher for the bubblewrap backend (roadmap #6).

This file has two roles:

* Host side, ``abi_version()`` reports whether the running kernel offers
  Landlock, so the backend knows whether to use the launcher at all.
* Sandbox side, it runs as a script (bind-mounted read-only at
  ``/stele/landlock``) under bwrap's seccomp filter. It restricts itself with
  Landlock and then execs the parser command, so the parser starts already
  confined and cannot undo it.

The ruleset is defense in depth under the mount namespace:

* **Writes** (create, write, truncate, remove, rename, link, mknod) are
  allowed only beneath the ``--write`` directories, normally just
  ``/stele/output``. The tmpfs mounts (``/tmp``, ``/home``, ...) are still
  there but writing to them fails with EACCES. Writing to the device sinks
  named by ``--write-dev`` (``/dev/null`` and friends) stays allowed so
  ordinary libraries keep working; those writes go nowhere.
* **Exec** is allowed only for the ``--exec`` files, the command itself, and
  the ELF interpreters and ``#!`` interpreters they need. Any other
  ``execve`` fails with EACCES, including binaries the parser wrote.
* **Reads** are not restricted by Landlock; the mount layout already limits
  what exists to read.

The launcher fails closed: if the kernel refuses any step it exits 126
without running the parser.

Because this file runs under whatever system Python the sandbox exposes, it
uses only the standard library and syntax available since Python 3.8.
"""
from __future__ import annotations

import os
import struct
import sys

# Syscall numbers are identical on every architecture (added in Linux 5.13).
_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446

_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# LANDLOCK_ACCESS_FS_* (linux/landlock.h)
EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13      # ABI 2
TRUNCATE = 1 << 14   # ABI 3

LAUNCHER_FAILURE_EXIT = 126
_PREFIX = "stele-landlock: "


def _libc():
    import ctypes

    return ctypes, ctypes.CDLL(None, use_errno=True)


def abi_version() -> int:
    """The kernel's Landlock ABI version, or 0 when Landlock is unavailable."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        ctypes, libc = _libc()
        libc.syscall.restype = ctypes.c_long
        version = libc.syscall(
            ctypes.c_long(_SYS_CREATE_RULESET), None, ctypes.c_size_t(0),
            ctypes.c_uint32(_CREATE_RULESET_VERSION),
        )
    except (OSError, AttributeError):
        return 0
    return max(int(version), 0)


def write_access(abi: int) -> int:
    """Every write-type right the given ABI can restrict."""
    rights = (WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR
              | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM)
    if abi >= 2:
        rights |= REFER
    if abi >= 3:
        rights |= TRUNCATE
    return rights


def _which(name: str) -> str | None:
    if "/" in name:
        return name
    for directory in os.environ.get("PATH", "").split(":"):
        candidate = os.path.join(directory or ".", name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def interpreters(path: str) -> list[str]:
    """Programs the kernel opens for exec when running path.

    That is the ELF PT_INTERP (dynamic loader) or the ``#!`` interpreter, and
    recursively theirs. Landlock checks the exec right on each of them.
    """
    found: list[str] = []
    todo = [path]
    while todo and len(found) < 8:
        current = todo.pop()
        try:
            with open(current, "rb") as fh:
                head = fh.read(4096)
                interp = _shebang(head) or _elf_interp(fh, head)
        except OSError:
            continue
        if interp and interp not in found:
            found.append(interp)
            todo.append(interp)
    return found


def _shebang(head: bytes) -> str | None:
    if not head.startswith(b"#!"):
        return None
    words = head[2:].split(b"\n", 1)[0].split()
    return os.fsdecode(words[0]) if words else None


def _elf_interp(fh, head: bytes) -> str | None:
    if head[:4] != b"\x7fELF" or len(head) < 64:
        return None
    is64 = head[4] == 2
    endian = "<" if head[5] == 1 else ">"
    if is64:
        phoff, = struct.unpack_from(endian + "Q", head, 32)
        phentsize, phnum = struct.unpack_from(endian + "HH", head, 54)
    else:
        phoff, = struct.unpack_from(endian + "I", head, 28)
        phentsize, phnum = struct.unpack_from(endian + "HH", head, 42)
    for i in range(min(phnum, 64)):
        fh.seek(phoff + i * phentsize)
        entry = fh.read(phentsize)
        if len(entry) < phentsize:
            return None
        if struct.unpack_from(endian + "I", entry, 0)[0] != 3:  # PT_INTERP
            continue
        if is64:
            offset, = struct.unpack_from(endian + "Q", entry, 8)
            size, = struct.unpack_from(endian + "Q", entry, 32)
        else:
            offset, = struct.unpack_from(endian + "I", entry, 4)
            size, = struct.unpack_from(endian + "I", entry, 16)
        fh.seek(offset)
        return os.fsdecode(fh.read(min(size, 4096)).split(b"\0", 1)[0])
    return None


class LandlockError(OSError):
    pass


def restrict(write_dirs, write_devices, exec_files) -> int:
    """Confine this process (and its future children). Returns the ABI used."""
    ctypes, libc = _libc()
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int

    def check(result, what):
        if result < 0:
            err = ctypes.get_errno()
            raise LandlockError(err, f"{what}: {os.strerror(err)}")
        return result

    abi = abi_version()
    if abi < 1:
        raise LandlockError(0, "Landlock is not available in the sandbox")

    writes = write_access(abi)
    handled = writes | EXECUTE
    # struct landlock_ruleset_attr, first field only: older kernels know no
    # more, and newer ones treat the missing fields as zero.
    attr_bytes = struct.pack("=Q", handled)
    attr = ctypes.create_string_buffer(attr_bytes, len(attr_bytes))
    ruleset = check(libc.syscall(
        ctypes.c_long(_SYS_CREATE_RULESET), attr, ctypes.c_size_t(len(attr_bytes)),
        ctypes.c_uint32(0),
    ), "landlock_create_ruleset")

    def allow(path, access):
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            # struct landlock_path_beneath_attr is packed: u64 + s32, 12 bytes.
            rule = ctypes.create_string_buffer(struct.pack("=Qi", access, fd), 12)
            check(libc.syscall(
                ctypes.c_long(_SYS_ADD_RULE), ctypes.c_int(ruleset),
                ctypes.c_int(_RULE_PATH_BENEATH), rule, ctypes.c_uint32(0),
            ), f"landlock_add_rule({path})")
        finally:
            os.close(fd)

    try:
        for path in write_dirs:
            allow(path, writes)
        # Rules on files may only carry file rights.
        for path in write_devices:
            if os.path.exists(path):
                allow(path, WRITE_FILE | (TRUNCATE if abi >= 3 else 0))
        for path in exec_files:
            allow(path, EXECUTE)
        check(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(NO_NEW_PRIVS)")
        check(libc.syscall(
            ctypes.c_long(_SYS_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0),
        ), "landlock_restrict_self")
    finally:
        os.close(ruleset)
    return abi


def main(argv: list[str]) -> int:
    write_dirs: list[str] = []
    write_devices: list[str] = []
    exec_files: list[str] = []
    options = {"--write": write_dirs, "--write-dev": write_devices, "--exec": exec_files}
    i = 0
    while i < len(argv) and argv[i] != "--":
        if argv[i] not in options or i + 1 >= len(argv):
            print(_PREFIX + f"bad argument {argv[i]!r}", file=sys.stderr)
            return LAUNCHER_FAILURE_EXIT
        options[argv[i]].append(argv[i + 1])
        i += 2
    command = argv[i + 1:]
    if not command:
        print(_PREFIX + "no command given", file=sys.stderr)
        return LAUNCHER_FAILURE_EXIT

    program = _which(command[0])
    if program is None:
        print(_PREFIX + f"{command[0]}: command not found", file=sys.stderr)
        return 127

    allowed: list[str] = []
    for entry in [program] + exec_files:
        resolved = _which(entry)
        if resolved is None or not os.path.exists(resolved):
            continue
        for path in [resolved] + interpreters(resolved):
            if path not in allowed and os.path.exists(path):
                allowed.append(path)

    try:
        restrict(write_dirs, write_devices, allowed)
    except OSError as exc:
        print(_PREFIX + f"refusing to run parser: {exc}", file=sys.stderr)
        return LAUNCHER_FAILURE_EXIT

    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(program, command)
    except OSError as exc:
        print(_PREFIX + f"{command[0]}: {exc}", file=sys.stderr)
        return 126
    return LAUNCHER_FAILURE_EXIT  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
