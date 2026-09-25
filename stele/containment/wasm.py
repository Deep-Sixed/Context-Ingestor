"""
Wasm/WASI sandbox backend on Wasmtime (roadmap #7).

A Wasm parser is specified exactly like a host-process parser, through
SandboxConfig, with ParserRequirements(wasm_module=True):

    SandboxConfig(
        command=["/path/to/parser.wasm", "--flag"],  # module path, then guest args
        artifact_dir=Path("out"),
        input_path=Path("conversations.json"),
    )

command[0] is a host path to a WebAssembly module, either binary (.wasm) or
WebAssembly text (.wat, compiled by Wasmtime). The guest sees argv
[<module file name>, *command[1:]], mirroring a host process.

Inside the guest (WASI preview 1):

  /stele/output         fd 3: the artifact directory, read-write (the only writable path)
  /stele/input/<name>   fd 4: the staged input file or directory, read-only
  <dst>                 next fds: each extra_ro_binds (src, dst) directory, read-only

Nothing else is preopened, so no other host path can be named. WASI preview 1
has no way to create sockets and no socket is preopened, so there is no
network; a module importing anything beyond wasi_snapshot_preview1 fails to
link. The environment is empty apart from STELE_OUTPUT_DIR, STELE_INPUT_PATH
(when there is an input) and config.env. stdin is empty. script_path is not
supported (the module is the parser).

Limits: memory via Wasmtime store limits (memory.grow fails beyond the cap and
a module whose initial memory exceeds it fails to instantiate), CPU via fuel
(a deterministic instruction budget that traps when exhausted) and the
wall-clock timeout via epoch interruption. A trap fails the run with
exit code WASM_TRAP_EXIT_CODE and the reason on stderr; the wall-clock timeout
reports timed_out=True like the bubblewrap backend.

Determinism: every run gets a fresh Engine, Store and WASI context, so no
state carries over. The guest cannot observe the host's clock or entropy:
clock_time_get starts at FIXED_EPOCH_NS and advances a fixed step per read,
random_get is a SHA-256 counter stream from a fixed (configurable) seed, and
poll_oneoff (sleep) is refused. File metadata is normalised too: timestamps
are fixed, inode numbers are replaced by per-run sequence numbers, link counts
are 1, directory sizes are 0, and directory listings are sorted by name. NaNs
are canonicalised and relaxed SIMD and threads are disabled, so arithmetic is
identical across x86 and ARM. Host filesystem naming rules (case sensitivity,
characters Windows forbids) remain the host's; deterministic parsers should
write portable names.

The SHA-256 of the executed module binary (after WAT compilation for text
modules) is returned as ExecutionOutcome.module_sha256 and
SandboxResult.module_sha256, and is available beforehand from
wasm_module_sha256().
"""
from __future__ import annotations

import gc
import hashlib
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .backend import Capability, ExecutionOutcome, SandboxBackend
from .capture import DEFAULT_OUTPUT_LIMIT_BYTES, BoundedCapture
from .telemetry import FailureReason, RunFailure, exit_status_failure
from .sandbox import SANDBOX_INPUT_DIR, SANDBOX_OUTPUT, SandboxConfig

WASMTIME_MISSING = (
    "the Wasm backend requires the wasmtime Python package, which is not "
    "installed (pip install 'stele[wasm]')."
)

# Exit code for a run that trapped (including CPU/memory limit traps), and for
# one whose module could not be loaded, compiled or linked.
WASM_TRAP_EXIT_CODE = 134
WASM_LOAD_EXIT_CODE = 127

# 2000-01-01T00:00:00Z. The guest's realtime clock starts here.
FIXED_EPOCH_NS = 946_684_800 * 1_000_000_000
# Each clock read advances the clock by this much, so elapsed-time loops end.
CLOCK_STEP_NS = 1_000
DEFAULT_ENTROPY_SEED = b"stele-wasm-deterministic-entropy-v1"

DEFAULT_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
# Roughly a minute of straight-line Wasm on current hardware; the wall-clock
# timeout remains the backstop.
DEFAULT_FUEL = 50_000_000_000

_WASI = "wasi_snapshot_preview1"

# WASI preview 1 errno values.
_ESUCCESS = 0
_EFAULT = 21
_EINVAL = 28
_ENAMETOOLONG = 37
_ENOSYS = 52
_ENOTSUP = 58
_EOVERFLOW = 61

_FILETYPE_REGULAR = 4
_FILESTAT_SIZE = 64
_DIRENT_HEADER = 24

# A helper module in the same store calls the real WASI implementation (which
# shares the run's file-descriptor table) with pointers into its own memory.
# Its results are normalised in Python and copied into the guest's memory.
_SHIM_STAT = 0
_SHIM_USED = 64
_SHIM_PATH = 1024
_SHIM_PATH_MAX = 4096
_SHIM_DIRBUF = 8192
_SHIM_DIRBUF_LEN = 65536
_SHIM_WAT = """
(module
  (import "wasi_snapshot_preview1" "fd_filestat_get"
    (func $fd_filestat_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "path_filestat_get"
    (func $path_filestat_get (param i32 i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_readdir"
    (func $fd_readdir (param i32 i32 i32 i64 i32) (result i32)))
  (memory (export "memory") 2)
  (func (export "fd_filestat_get") (param i32 i32) (result i32)
    (call $fd_filestat_get (local.get 0) (local.get 1)))
  (func (export "path_filestat_get") (param i32 i32 i32 i32 i32) (result i32)
    (call $path_filestat_get
      (local.get 0) (local.get 1) (local.get 2) (local.get 3) (local.get 4)))
  (func (export "fd_readdir") (param i32 i32 i32 i64 i32) (result i32)
    (call $fd_readdir
      (local.get 0) (local.get 1) (local.get 2) (local.get 3) (local.get 4)))
)
"""


def _wasmtime() -> Any:
    import wasmtime

    return wasmtime


def load_wasm_module(path: Path) -> bytes:
    """Return the binary module at path, compiling WebAssembly text if needed."""
    raw = Path(path).read_bytes()
    if raw.startswith(b"\0asm"):
        return raw
    return bytes(_wasmtime().wat2wasm(raw.decode("utf-8")))


def wasm_module_sha256(path: Path) -> str:
    """SHA-256 of the module binary the Wasm backend would execute for path."""
    return hashlib.sha256(load_wasm_module(path)).hexdigest()


class WasmtimeBackend(SandboxBackend):
    """WebAssembly/WASI modules on Wasmtime: portable, limited, deterministic."""

    name = "wasmtime"

    def __init__(
        self,
        *,
        memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
        fuel: int = DEFAULT_FUEL,
        entropy_seed: bytes = DEFAULT_ENTROPY_SEED,
        output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    ) -> None:
        if memory_limit_bytes < 1024 * 1024:
            raise ValueError("memory_limit_bytes must be at least 1 MiB")
        if fuel <= 0:
            raise ValueError("fuel must be positive")
        self.memory_limit_bytes = memory_limit_bytes
        self.fuel = fuel
        self.entropy_seed = entropy_seed
        self.output_limit_bytes = output_limit_bytes

    def capabilities(self) -> frozenset[Capability]:
        # No SYSCALL_FILTER: there are no host syscalls to filter, but the
        # capability names seccomp-style enforcement, which this is not. No
        # GPU, NATIVE_LIBS or HOST_PROCESS: only Wasm modules run here.
        return frozenset({
            Capability.FILESYSTEM_ISOLATION,
            Capability.NETWORK_ISOLATION,
            Capability.RESOURCE_LIMITS,
            Capability.DETERMINISTIC,
            Capability.WASM_MODULE,
        })

    def available(self) -> bool:
        try:
            _wasmtime()
        except ImportError:
            return False
        return True

    def unavailable_reason(self) -> str:
        return WASMTIME_MISSING

    def execute(self, config: SandboxConfig) -> ExecutionOutcome:
        if not config.command:
            raise ValueError("Wasm parser needs command=[module_path, *args]")
        if config.script_path is not None:
            raise ValueError(
                "the Wasm backend runs the module in command[0]; script_path is not supported"
            )
        preopens = _preopens(config)

        t0 = time.monotonic()
        module_path = Path(config.command[0])
        try:
            binary = load_wasm_module(module_path)
        except (OSError, UnicodeDecodeError, _wasmtime().WasmtimeError) as exc:
            return _failed(WASM_LOAD_EXIT_CODE, f"cannot load wasm module {module_path}: {exc}", t0)
        module_sha256 = hashlib.sha256(binary).hexdigest()

        run = _Run(self, config, binary, preopens)
        cpu0 = time.thread_time()  # the guest runs on this thread
        try:
            outcome = run.execute()
            cpu = time.thread_time() - cpu0
            peak_memory, fuel_consumed = run.peak_memory_bytes, run.fuel_consumed
        finally:
            # The store owns the WASI context, which holds the preopened
            # directories open. A trap's traceback forms a reference cycle
            # back to the store, so without an explicit collection those
            # handles outlive the run — and Windows then refuses to delete
            # the staging directory.
            del run
            gc.collect()
        failure = outcome.failure
        if (
            failure is not None and failure.reason is FailureReason.WASM_TRAP
            and peak_memory is not None and peak_memory + _WASM_PAGE > self.memory_limit_bytes
        ):
            # The guest trapped with its linear memory at the cap: growth was
            # refused and the module gave up.
            failure = RunFailure(
                FailureReason.OUT_OF_MEMORY,
                f"linear memory reached the {self.memory_limit_bytes}-byte limit; {failure.detail}",
                exit_code=failure.exit_code,
            )
        return replace(
            outcome,
            wall_time_seconds=time.monotonic() - t0,
            module_sha256=module_sha256,
            cpu_time_seconds=cpu,
            peak_memory_bytes=peak_memory,
            runtime=self.runtime_name(),
            limits=self.limits(config),
            counters={} if fuel_consumed is None else {"fuel_consumed": fuel_consumed},
            failure=failure,
        )

    def runtime_name(self) -> str:
        try:
            from importlib.metadata import version

            return f"wasmtime {version('wasmtime')}"
        except Exception:  # noqa: BLE001 - metadata is best effort
            return "wasmtime"

    def limits(self, config: SandboxConfig) -> dict[str, Any]:
        return {
            "timeout_seconds": config.timeout_seconds,
            "memory_bytes": self.memory_limit_bytes,
            "fuel": self.fuel,
            "output_bytes": self.output_limit_bytes,
        }


_WASM_PAGE = 65536


def _failed(code: int, message: str, t0: float) -> ExecutionOutcome:
    return ExecutionOutcome(
        exit_code=code,
        stdout="",
        stderr=f"stele: {message}\n",
        wall_time_seconds=time.monotonic() - t0,
        failure=RunFailure(FailureReason.ENGINE_ERROR, message, exit_code=code),
    )


def _preopens(config: SandboxConfig) -> list[tuple[Path, str, bool]]:
    """(host dir, guest path, writable) for every directory the guest may use."""
    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    preopens = [(config.artifact_dir, SANDBOX_OUTPUT, True)]
    if config.input_path is not None:
        # WASI preopens are directories. run_in_sandbox stages the input alone
        # in a private directory, which is exposed as /stele/input; refuse
        # anything else so a sibling of an unstaged path cannot leak in.
        staged_parent = config.input_path.parent
        if [p.name for p in staged_parent.iterdir()] != [config.input_path.name]:
            raise ValueError(
                f"Wasm input {config.input_path} must be alone in its directory "
                "(pass inputs through run_in_sandbox, which stages them)"
            )
        preopens.append((staged_parent, SANDBOX_INPUT_DIR, False))
    for src, dst in config.extra_ro_binds:
        if not Path(src).is_dir():
            raise ValueError(f"Wasm read-only mounts must be directories: {src}")
        preopens.append((Path(src), dst, False))
    return preopens


class _Run:
    """One execution: fresh engine, store, WASI context and host shims."""

    def __init__(
        self,
        backend: WasmtimeBackend,
        config: SandboxConfig,
        binary: bytes,
        preopens: list[tuple[Path, str, bool]],
    ) -> None:
        self.backend = backend
        self.config = config
        self.binary = binary
        self.preopens = preopens
        self.stdout = BoundedCapture(backend.output_limit_bytes)
        self.stderr = BoundedCapture(backend.output_limit_bytes)
        self.clock_ns = 0
        self.entropy = _Entropy(backend.entropy_seed)
        self.inodes: dict[int, int] = {}
        self.listings: dict[int, list[tuple[bytes, int, int]]] = {}
        # Telemetry, read from the store when the run ends.
        self.guest_memory: Any = None
        self.peak_memory_bytes: int | None = None
        self.fuel_consumed: int | None = None

    # -- setup ---------------------------------------------------------------

    def _engine(self) -> Any:
        wt = _wasmtime()
        cfg = wt.Config()
        cfg.consume_fuel = True
        cfg.epoch_interruption = True
        cfg.cranelift_nan_canonicalization = True
        cfg.wasm_threads = False
        cfg.wasm_relaxed_simd = False
        cfg.wasm_multi_memory = False
        cfg.wasm_memory64 = False
        return wt.Engine(cfg)

    def _wasi(self) -> Any:
        wt = _wasmtime()
        wasi = wt.WasiConfig()
        wasi.argv = [Path(self.config.command[0]).name, *self.config.command[1:]]
        env = {"STELE_OUTPUT_DIR": SANDBOX_OUTPUT}
        if self.config.input_path is not None:
            env["STELE_INPUT_PATH"] = f"{SANDBOX_INPUT_DIR}/{self.config.input_path.name}"
        env.update(self.config.env)
        wasi.env = list(env.items())
        wasi.stdout_custom = self.stdout
        wasi.stderr_custom = self.stderr
        for host, guest, writable in self.preopens:
            wasi.preopen_dir(str(host), guest, writable)
        return wasi

    def execute(self) -> ExecutionOutcome:
        wt = _wasmtime()
        engine = self._engine()
        store = wt.Store(engine)
        store.set_limits(
            memory_size=self.backend.memory_limit_bytes,
            table_elements=1_000_000,
            instances=2,   # the guest and the metadata shim
            tables=4,
            memories=2,
        )
        store.set_fuel(self.backend.fuel)
        store.set_epoch_deadline(1)
        store.set_wasi(self._wasi())

        timer = threading.Timer(self.config.timeout_seconds, engine.increment_epoch)
        timer.daemon = True
        timer.start()
        try:
            return self._instantiate_and_run(wt, engine, store)
        finally:
            timer.cancel()
            self._measure(store)
            self.shim = None  # exports bound to the store
            self.guest_memory = None

    def _measure(self, store: Any) -> None:
        try:
            self.fuel_consumed = self.backend.fuel - store.get_fuel()
        except Exception:  # noqa: BLE001 - telemetry must never fail a run
            self.fuel_consumed = None
        if self.guest_memory is not None:
            try:
                # Linear memory never shrinks, so its size now is its peak.
                self.peak_memory_bytes = self.guest_memory.data_len(store)
            except Exception:  # noqa: BLE001
                self.peak_memory_bytes = None

    def _instantiate_and_run(self, wt: Any, engine: Any, store: Any) -> ExecutionOutcome:
        try:
            module = wt.Module(engine, self.binary)
            shim_linker = wt.Linker(engine)
            shim_linker.define_wasi()
            self.shim = shim_linker.instantiate(store, wt.Module(engine, _SHIM_WAT)).exports(store)
            linker = wt.Linker(engine)
            linker.define_wasi()
            linker.allow_shadowing = True
            self._define_deterministic_imports(wt, linker)
        except wt.WasmtimeError as exc:
            return self._outcome(
                WASM_LOAD_EXIT_CODE, f"invalid wasm module: {exc}", reason=FailureReason.ENGINE_ERROR,
            )

        try:
            instance = linker.instantiate(store, module)
        except wt.Trap as trap:
            return self._trapped(trap)
        except wt.ExitTrap as exc:
            return self._outcome(exc.code)
        except wt.WasmtimeError as exc:
            message = str(exc)
            if "memory" in message and ("limit" in message or "exceed" in message):
                return self._outcome(
                    WASM_TRAP_EXIT_CODE,
                    f"memory limit of {self.backend.memory_limit_bytes} bytes exceeded "
                    f"at instantiation: {message}",
                    reason=FailureReason.OUT_OF_MEMORY,
                )
            return self._outcome(
                WASM_LOAD_EXIT_CODE, f"cannot link wasm module: {message}",
                reason=FailureReason.ENGINE_ERROR,
            )

        exports = instance.exports(store)
        memory = exports.get("memory")
        if isinstance(memory, wt.Memory):
            self.guest_memory = memory
        start = exports.get("_start")
        if start is None:
            return self._outcome(
                WASM_LOAD_EXIT_CODE, "wasm module exports no _start function",
                reason=FailureReason.ENGINE_ERROR,
            )
        try:
            start(store)
        except wt.ExitTrap as exc:
            return self._outcome(exc.code)
        except wt.Trap as trap:
            return self._trapped(trap)
        except wt.WasmtimeError as exc:
            return self._outcome(
                WASM_TRAP_EXIT_CODE, f"wasm execution failed: {exc}", reason=FailureReason.WASM_TRAP,
            )
        return self._outcome(0)

    def _trapped(self, trap: Any) -> ExecutionOutcome:
        wt = _wasmtime()
        code = trap.trap_code
        if code == wt.TrapCode.INTERRUPT:
            return self._outcome(
                -1, f"wall-clock timeout of {self.config.timeout_seconds}s exceeded",
                timed_out=True, reason=FailureReason.TIMEOUT,
            )
        if code == wt.TrapCode.OUT_OF_FUEL:
            return self._outcome(
                WASM_TRAP_EXIT_CODE,
                f"CPU limit exceeded: fuel budget of {self.backend.fuel} exhausted",
                reason=FailureReason.CPU_LIMIT,
            )
        return self._outcome(
            WASM_TRAP_EXIT_CODE, f"wasm trap: {trap.message}", reason=FailureReason.WASM_TRAP,
        )

    def _outcome(
        self,
        code: int,
        message: str | None = None,
        *,
        timed_out: bool = False,
        reason: FailureReason | None = None,
    ) -> ExecutionOutcome:
        stderr = self.stderr.text("stderr")
        if message is not None:
            stderr += f"stele: {message}\n"
        failure = None
        if reason is not None:
            failure = RunFailure(reason, message or reason.value, exit_code=code)
        elif code != 0:
            failure = exit_status_failure(code)
        return ExecutionOutcome(
            exit_code=code,
            stdout=self.stdout.text("stdout"),
            stderr=stderr,
            wall_time_seconds=0.0,
            timed_out=timed_out,
            failure=failure,
        )

    # -- deterministic WASI overrides -----------------------------------------

    def _define_deterministic_imports(self, wt: Any, linker: Any) -> None:
        i32, i64 = wt.ValType.i32(), wt.ValType.i64()
        overrides: dict[str, tuple[list[Any], Callable[..., int]]] = {
            "clock_time_get": ([i32, i64, i32], self._clock_time_get),
            "clock_res_get": ([i32, i32], self._clock_res_get),
            "random_get": ([i32, i32], self._random_get),
            "poll_oneoff": ([i32, i32, i32, i32], _refuse),
            "fd_filestat_get": ([i32, i32], self._fd_filestat_get),
            "path_filestat_get": ([i32, i32, i32, i32, i32], self._path_filestat_get),
            "fd_readdir": ([i32, i32, i32, i64, i32], self._fd_readdir),
            # Symlink and hard-link support differs by host OS (and artifact
            # collection refuses symlinks anyway); refuse both everywhere.
            "path_symlink": ([i32, i32, i32, i32, i32], _refuse),
            "path_link": ([i32, i32, i32, i32, i32, i32, i32], _refuse),
        }
        for name, (params, func) in overrides.items():
            linker.define_func(
                _WASI, name, wt.FuncType(params, [i32]), _guarded(func), access_caller=True
            )

    def _clock_time_get(self, caller: Any, clock_id: int, _precision: int, out: int) -> int:
        if clock_id not in (0, 1, 2, 3):
            return _EINVAL
        self.clock_ns += CLOCK_STEP_NS
        value = self.clock_ns + (FIXED_EPOCH_NS if clock_id == 0 else 0)
        _memory(caller).write(caller, struct.pack("<Q", value), out)
        return _ESUCCESS

    def _clock_res_get(self, caller: Any, clock_id: int, out: int) -> int:
        if clock_id not in (0, 1, 2, 3):
            return _EINVAL
        _memory(caller).write(caller, struct.pack("<Q", CLOCK_STEP_NS), out)
        return _ESUCCESS

    def _random_get(self, caller: Any, buf: int, length: int) -> int:
        _memory(caller).write(caller, self.entropy.read(length), buf)
        return _ESUCCESS

    def _fd_filestat_get(self, caller: Any, fd: int, buf: int) -> int:
        errno = self.shim["fd_filestat_get"](caller, fd, _SHIM_STAT)
        if errno == _ESUCCESS:
            _memory(caller).write(caller, self._normalised_filestat(caller), buf)
        return errno

    def _path_filestat_get(
        self, caller: Any, fd: int, flags: int, path: int, path_len: int, buf: int
    ) -> int:
        if path_len > _SHIM_PATH_MAX:
            return _ENAMETOOLONG
        name = _memory(caller).read(caller, path, path + path_len)
        if len(name) != path_len:
            return _EFAULT
        shim_memory = self.shim["memory"]
        if path_len:
            shim_memory.write(caller, name, _SHIM_PATH)
        errno = self.shim["path_filestat_get"](caller, fd, flags, _SHIM_PATH, path_len, _SHIM_STAT)
        if errno == _ESUCCESS:
            _memory(caller).write(caller, self._normalised_filestat(caller), buf)
        return errno

    def _normalised_filestat(self, caller: Any) -> bytes:
        raw = bytes(self.shim["memory"].read(caller, _SHIM_STAT, _SHIM_STAT + _FILESTAT_SIZE))
        _dev, ino, filetype, _nlink, size = struct.unpack_from("<QQB7xQQ", raw)
        if filetype != _FILETYPE_REGULAR:
            size = 0
        return struct.pack(
            "<QQB7xQQQQQ",
            1, self._inode(ino), filetype, 1, size,
            FIXED_EPOCH_NS, FIXED_EPOCH_NS, FIXED_EPOCH_NS,
        )

    def _inode(self, real: int) -> int:
        """Per-run inode numbers in order of first observation (never 0)."""
        return self.inodes.setdefault(real, len(self.inodes) + 1)

    def _fd_readdir(
        self, caller: Any, fd: int, buf: int, buf_len: int, cookie: int, bufused: int
    ) -> int:
        if cookie == 0 or fd not in self.listings:
            listing = self._list_directory(caller, fd)
            if isinstance(listing, int):
                return listing
            self.listings[fd] = listing

        out = bytearray()
        entries = self.listings[fd]
        for index in range(cookie, len(entries)):
            if len(out) >= buf_len:
                break
            name, filetype, real_ino = entries[index]
            out += struct.pack("<QQIB3x", index + 1, self._inode(real_ino), len(name), filetype)
            out += name
        out = out[:buf_len]
        memory = _memory(caller)
        if out:
            memory.write(caller, bytes(out), buf)
        memory.write(caller, struct.pack("<I", len(out)), bufused)
        return _ESUCCESS

    def _list_directory(self, caller: Any, fd: int) -> list[tuple[bytes, int, int]] | int:
        """Every entry of fd's directory, sorted by name."""
        shim_memory = self.shim["memory"]
        entries: list[tuple[bytes, int, int]] = []
        cookie = 0
        while True:
            errno = self.shim["fd_readdir"](
                caller, fd, _SHIM_DIRBUF, _SHIM_DIRBUF_LEN, cookie, _SHIM_USED
            )
            if errno != _ESUCCESS:
                return errno
            (used,) = struct.unpack("<I", bytes(shim_memory.read(caller, _SHIM_USED, _SHIM_USED + 4)))
            raw = bytes(shim_memory.read(caller, _SHIM_DIRBUF, _SHIM_DIRBUF + used))
            pos, progressed = 0, False
            while pos + _DIRENT_HEADER <= used:
                d_next, d_ino, namlen, d_type = struct.unpack_from("<QQIB", raw, pos)
                end = pos + _DIRENT_HEADER + namlen
                if end > used:
                    break
                entries.append((raw[pos + _DIRENT_HEADER:end], d_type, d_ino))
                cookie, pos, progressed = d_next, end, True
            if used < _SHIM_DIRBUF_LEN:
                break
            if not progressed:
                return _EOVERFLOW
        entries.sort(key=lambda entry: entry[0])
        return entries


class _Entropy:
    """Deterministic byte stream: SHA-256(seed || counter) blocks."""

    def __init__(self, seed: bytes) -> None:
        self.seed = seed
        self.counter = 0
        self.pending = b""

    def read(self, n: int) -> bytes:
        chunks = [self.pending]
        have = len(self.pending)
        while have < n:
            block = hashlib.sha256(self.seed + self.counter.to_bytes(8, "little")).digest()
            self.counter += 1
            chunks.append(block)
            have += len(block)
        data = b"".join(chunks)
        self.pending = data[n:]
        return data[:n]


def _memory(caller: Any) -> Any:
    memory = caller.get("memory")
    if memory is None:
        raise IndexError("guest exports no memory")
    return memory


def _refuse(caller: Any, *args: int) -> int:
    return _ENOTSUP


def _guarded(func: Callable[..., int]) -> Callable[..., int]:
    """Guest pointers out of range become EFAULT rather than host exceptions."""

    def call(caller: Any, *args: int) -> int:
        try:
            return func(caller, *args)
        except IndexError:
            return _EFAULT

    return call
