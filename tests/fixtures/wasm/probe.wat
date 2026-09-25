;; Containment probe for the Wasm backend proofs (tests/test_wasm_backend.py).
;;
;; argv[1][0] selects a mode; argv[2] is the input file name where a mode
;; needs one. The backend preopens /stele/output as fd 3 and /stele/input as
;; fd 4. Results are accumulated in memory and written to a file in the output
;; directory; 'Y'/'N' flags record whether a WASI call succeeded.
;;
;;   a  write result.json to the output directory
;;   x  print to stdout/stderr and exit with the code in argv[1][1]
;;   f  try to reach paths outside the preopens      -> attempts.txt
;;   n  try every socket call on fds 0-9              -> network.txt
;;   g  grow memory until refused, record the pages   -> pages.bin, then trap
;;   l  loop forever
;;   d  clock, entropy, file metadata, dir listing    -> probe.bin
;;   s  bump a global and a memory cell               -> state.bin
;;   v  dump environment and argv                     -> env.bin
;;   r  copy the input file argv[2]                   -> copy.bin
(module
  (import "wasi_snapshot_preview1" "args_sizes_get" (func $args_sizes_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "args_get" (func $args_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "environ_sizes_get" (func $environ_sizes_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "environ_get" (func $environ_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_write" (func $fd_write (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_read" (func $fd_read (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_close" (func $fd_close (param i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_prestat_get" (func $fd_prestat_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_filestat_get" (func $fd_filestat_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_readdir" (func $fd_readdir (param i32 i32 i32 i64 i32) (result i32)))
  (import "wasi_snapshot_preview1" "path_open" (func $path_open (param i32 i32 i32 i32 i32 i64 i64 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "path_filestat_get" (func $path_filestat_get (param i32 i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "path_symlink" (func $path_symlink (param i32 i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "clock_time_get" (func $clock_time_get (param i32 i64 i32) (result i32)))
  (import "wasi_snapshot_preview1" "clock_res_get" (func $clock_res_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "random_get" (func $random_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "poll_oneoff" (func $poll_oneoff (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "sock_accept" (func $sock_accept (param i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "sock_recv" (func $sock_recv (param i32 i32 i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "sock_send" (func $sock_send (param i32 i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "sock_shutdown" (func $sock_shutdown (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "proc_exit" (func $proc_exit (param i32)))

  ;; Layout: 0-255 scratch (iovec 0, count 8, fd 16, argc 20, argv size 24,
  ;; prestat 32, env count 40, env size 44, misc 48, stat/clock buffer 64-191),
  ;; argv pointers 256, env pointers 512, argv strings 1024, env strings 2048,
  ;; constants 4096, state cell 7000, result buffer 8192, I/O buffer 16384.
  (memory (export "memory") 2)
  (global $outlen (mut i32) (i32.const 0))
  (global $counter (mut i32) (i32.const 0))

  (data (i32.const 4096) "result.json")  ;; $result_name len 11
  (data (i32.const 4112) "{\22parser\22: \22wasm_probe_v0\22, \22chunks\22: [{\22text\22: \22hello from wasm\22}]}")  ;; $result_body len 68
  (data (i32.const 4184) "wrote result.json\0a")  ;; $wrote len 18
  (data (i32.const 4208) "exiting with code ")  ;; $exiting len 18
  (data (i32.const 4232) "diagnostic on stderr\0a")  ;; $diag len 21
  (data (i32.const 4256) "attempts.txt")  ;; $attempts len 12
  (data (i32.const 4272) "ok.txt")  ;; $ok len 6
  (data (i32.const 4280) "../escape.txt")  ;; $dotdot len 13
  (data (i32.const 4296) "/etc/passwd")  ;; $abs len 11
  (data (i32.const 4312) "../../../../../../etc/passwd")  ;; $deep len 28
  (data (i32.const 4344) "evil.txt")  ;; $evil len 8
  (data (i32.const 4352) "link.txt")  ;; $link len 8
  (data (i32.const 4360) "network.txt")  ;; $network len 11
  (data (i32.const 4376) "pages.bin")  ;; $pages len 9
  (data (i32.const 4392) "probe.bin")  ;; $probe len 9
  (data (i32.const 4408) "b.txt")  ;; $b len 5
  (data (i32.const 4416) "a.txt")  ;; $a len 5
  (data (i32.const 4424) "c")  ;; $c len 1
  (data (i32.const 4432) "state.bin")  ;; $state len 9
  (data (i32.const 4448) "env.bin")  ;; $env len 7
  (data (i32.const 4456) "copy.bin")  ;; $copy len 8

  (func $emit (param $p i32) (param $n i32)
    (memory.copy (i32.add (i32.const 8192) (global.get $outlen)) (local.get $p) (local.get $n))
    (global.set $outlen (i32.add (global.get $outlen) (local.get $n))))

  (func $emit_byte (param $b i32)
    (i32.store8 (i32.add (i32.const 8192) (global.get $outlen)) (local.get $b))
    (global.set $outlen (i32.add (global.get $outlen) (i32.const 1))))

  ;; 'Y' when the call succeeded, 'N' when it returned an error.
  (func $flag (param $errno i32)
    (call $emit_byte (select (i32.const 89) (i32.const 78) (i32.eqz (local.get $errno)))))

  (func $write_all (param $fd i32) (param $p i32) (param $n i32) (result i32)
    (local $e i32)
    (block $done
      (loop $more
        (br_if $done (i32.eqz (local.get $n)))
        (i32.store (i32.const 0) (local.get $p))
        (i32.store (i32.const 4) (local.get $n))
        (local.set $e (call $fd_write (local.get $fd) (i32.const 0) (i32.const 1) (i32.const 8)))
        (if (local.get $e) (then (return (local.get $e))))
        (local.set $p (i32.add (local.get $p) (i32.load (i32.const 8))))
        (local.set $n (i32.sub (local.get $n) (i32.load (i32.const 8))))
        (br $more)))
    (i32.const 0))

  ;; Create (or truncate) name in dir for writing; the new fd is stored at 16.
  (func $create (param $dir i32) (param $name i32) (param $len i32) (result i32)
    (call $path_open (local.get $dir) (i32.const 0) (local.get $name) (local.get $len)
      (i32.const 9) (i64.const 64) (i64.const 0) (i32.const 0) (i32.const 16)))

  (func $write_file (param $dir i32) (param $name i32) (param $len i32) (param $p i32) (param $n i32) (result i32)
    (local $e i32) (local $fd i32)
    (local.set $e (call $create (local.get $dir) (local.get $name) (local.get $len)))
    (if (local.get $e) (then (return (local.get $e))))
    (local.set $fd (i32.load (i32.const 16)))
    (local.set $e (call $write_all (local.get $fd) (local.get $p) (local.get $n)))
    (drop (call $fd_close (local.get $fd)))
    (local.get $e))

  (func $flush (param $name i32) (param $len i32)
    (if (call $write_file (i32.const 3) (local.get $name) (local.get $len) (i32.const 8192) (global.get $outlen))
      (then (call $proc_exit (i32.const 97)))))

  (func $strlen (param $p i32) (result i32)
    (local $q i32)
    (local.set $q (local.get $p))
    (block $done
      (loop $next
        (br_if $done (i32.eqz (i32.load8_u (local.get $q))))
        (local.set $q (i32.add (local.get $q) (i32.const 1)))
        (br $next)))
    (i32.sub (local.get $q) (local.get $p)))

  ;; Pointer to argv[i], or to an empty string when there is no such argument.
  (func $arg (param $i i32) (result i32)
    (if (result i32) (i32.lt_u (local.get $i) (i32.load (i32.const 20)))
      (then (i32.load (i32.add (i32.const 256) (i32.shl (local.get $i) (i32.const 2)))))
      (else (i32.const 4095))))

  (func $mode_artifact
    (if (call $write_file (i32.const 3) (i32.const 4096) (i32.const 11) (i32.const 4112) (i32.const 68))
      (then (call $proc_exit (i32.const 1))))
    (drop (call $write_all (i32.const 1) (i32.const 4184) (i32.const 18))))

  (func $mode_exit
    (local $code i32)
    (local.set $code (i32.sub (i32.load8_u (i32.add (call $arg (i32.const 1)) (i32.const 1))) (i32.const 48)))
    (drop (call $write_all (i32.const 1) (i32.const 4208) (i32.const 18)))
    (i32.store8 (i32.const 48) (i32.add (local.get $code) (i32.const 48)))
    (i32.store8 (i32.const 49) (i32.const 10))
    (drop (call $write_all (i32.const 1) (i32.const 48) (i32.const 2)))
    (drop (call $write_all (i32.const 2) (i32.const 4232) (i32.const 21)))
    (call $proc_exit (local.get $code)))

  (func $mode_filesystem
    (local $e i32) (local $name i32)
    (local.set $name (call $arg (i32.const 2)))
    ;; control: creating a file in the output directory works
    (local.set $e (call $create (i32.const 3) (i32.const 4272) (i32.const 6)))
    (call $flag (local.get $e))
    (if (i32.eqz (local.get $e)) (then (drop (call $fd_close (i32.load (i32.const 16))))))
    ;; dot-dot out of the output directory
    (call $flag (call $create (i32.const 3) (i32.const 4280) (i32.const 13)))
    ;; absolute host path
    (call $flag (call $path_open (i32.const 3) (i32.const 0) (i32.const 4296) (i32.const 11)
      (i32.const 0) (i64.const 2) (i64.const 0) (i32.const 0) (i32.const 16)))
    ;; deep dot-dot out of the input directory
    (call $flag (call $path_open (i32.const 4) (i32.const 0) (i32.const 4312) (i32.const 28)
      (i32.const 0) (i64.const 2) (i64.const 0) (i32.const 0) (i32.const 16)))
    ;; create a file in the read-only input directory
    (call $flag (call $create (i32.const 4) (i32.const 4344) (i32.const 8)))
    ;; symlink to a host path
    (call $flag (call $path_symlink (i32.const 4296) (i32.const 11) (i32.const 3) (i32.const 4352) (i32.const 8)))
    ;; exactly two preopens
    (call $flag (call $fd_prestat_get (i32.const 3) (i32.const 32)))
    (call $flag (call $fd_prestat_get (i32.const 4) (i32.const 32)))
    (call $flag (call $fd_prestat_get (i32.const 5) (i32.const 32)))
    (call $flag (call $path_open (i32.const 5) (i32.const 0) (i32.const 4272) (i32.const 6)
      (i32.const 0) (i64.const 2) (i64.const 0) (i32.const 0) (i32.const 16)))
    ;; truncate the staged input for writing
    (call $flag (call $path_open (i32.const 4) (i32.const 0) (local.get $name) (call $strlen (local.get $name))
      (i32.const 8) (i64.const 64) (i64.const 0) (i32.const 0) (i32.const 16)))
    (call $flush (i32.const 4256) (i32.const 12)))

  (func $mode_network
    (local $fd i32)
    (i32.store (i32.const 0) (i32.const 48))
    (i32.store (i32.const 4) (i32.const 1))
    (block $done
      (loop $next
        (br_if $done (i32.ge_u (local.get $fd) (i32.const 10)))
        (call $flag (call $sock_accept (local.get $fd) (i32.const 0) (i32.const 16)))
        (call $flag (call $sock_recv (local.get $fd) (i32.const 0) (i32.const 1) (i32.const 0) (i32.const 8) (i32.const 12)))
        (call $flag (call $sock_send (local.get $fd) (i32.const 0) (i32.const 1) (i32.const 0) (i32.const 8)))
        (call $flag (call $sock_shutdown (local.get $fd) (i32.const 3)))
        (local.set $fd (i32.add (local.get $fd) (i32.const 1)))
        (br $next)))
    (call $flush (i32.const 4360) (i32.const 11)))

  (func $mode_grow
    (block $refused
      (loop $more
        (br_if $refused (i32.eq (memory.grow (i32.const 1)) (i32.const -1)))
        (br $more)))
    (i32.store (i32.const 48) (memory.size))
    (drop (call $write_file (i32.const 3) (i32.const 4376) (i32.const 9) (i32.const 48) (i32.const 4)))
    unreachable)

  (func $mode_loop
    (loop $forever (br $forever)))

  (func $mode_determinism
    (local $name i32) (local $len i32)
    (local.set $name (call $arg (i32.const 2)))
    (local.set $len (call $strlen (local.get $name)))
    (call $emit_byte (call $clock_time_get (i32.const 0) (i64.const 0) (i32.const 64)))
    (call $emit (i32.const 64) (i32.const 8))
    (call $emit_byte (call $clock_time_get (i32.const 0) (i64.const 0) (i32.const 64)))
    (call $emit (i32.const 64) (i32.const 8))
    (call $emit_byte (call $clock_time_get (i32.const 1) (i64.const 0) (i32.const 64)))
    (call $emit (i32.const 64) (i32.const 8))
    (call $emit_byte (call $clock_res_get (i32.const 0) (i32.const 64)))
    (call $emit (i32.const 64) (i32.const 8))
    (call $emit_byte (call $random_get (i32.const 64) (i32.const 32)))
    (call $emit (i32.const 64) (i32.const 32))
    ;; metadata of the input file and the input directory
    (call $emit_byte (call $path_filestat_get (i32.const 4) (i32.const 0) (local.get $name) (local.get $len) (i32.const 64)))
    (call $emit (i32.const 64) (i32.const 64))
    (call $emit_byte (call $fd_filestat_get (i32.const 4) (i32.const 64)))
    (call $emit (i32.const 64) (i32.const 64))
    ;; files created out of name order, then listed
    (call $emit_byte (call $write_file (i32.const 3) (i32.const 4408) (i32.const 5) (i32.const 4424) (i32.const 1)))
    (call $emit_byte (call $write_file (i32.const 3) (i32.const 4416) (i32.const 5) (i32.const 4424) (i32.const 1)))
    (call $emit_byte (call $write_file (i32.const 3) (i32.const 4424) (i32.const 1) (i32.const 4424) (i32.const 1)))
    (call $emit_byte (call $fd_readdir (i32.const 3) (i32.const 16384) (i32.const 4096) (i64.const 0) (i32.const 8)))
    (call $emit (i32.const 8) (i32.const 4))
    (call $emit (i32.const 16384) (i32.load (i32.const 8)))
    ;; a freshly written file carries the fixed timestamps too
    (call $emit_byte (call $path_filestat_get (i32.const 3) (i32.const 0) (i32.const 4408) (i32.const 5) (i32.const 64)))
    (call $emit (i32.const 64) (i32.const 64))
    ;; sleeping is refused
    (call $emit_byte (call $poll_oneoff (i32.const 128) (i32.const 192) (i32.const 1) (i32.const 8)))
    (call $flush (i32.const 4392) (i32.const 9)))

  (func $mode_state
    (global.set $counter (i32.add (global.get $counter) (i32.const 1)))
    (i32.store8 (i32.const 7000) (i32.add (i32.load8_u (i32.const 7000)) (i32.const 1)))
    (i32.store (i32.const 48) (global.get $counter))
    (call $emit (i32.const 48) (i32.const 4))
    (call $emit_byte (i32.load8_u (i32.const 7000)))
    (drop (call $random_get (i32.const 64) (i32.const 8)))
    (call $emit (i32.const 64) (i32.const 8))
    (drop (call $clock_time_get (i32.const 0) (i64.const 0) (i32.const 64)))
    (call $emit (i32.const 64) (i32.const 8))
    (call $flush (i32.const 4432) (i32.const 9)))

  (func $mode_environment
    (drop (call $environ_sizes_get (i32.const 40) (i32.const 44)))
    (if (i32.or (i32.gt_u (i32.load (i32.const 40)) (i32.const 128))
                (i32.gt_u (i32.load (i32.const 44)) (i32.const 2048)))
      (then (call $proc_exit (i32.const 98))))
    (drop (call $environ_get (i32.const 512) (i32.const 2048)))
    (call $emit (i32.const 2048) (i32.load (i32.const 44)))
    (call $emit_byte (i32.const 124))
    (call $emit (i32.const 1024) (i32.load (i32.const 24)))
    (call $flush (i32.const 4448) (i32.const 7)))

  (func $mode_copy
    (local $name i32) (local $in i32) (local $out i32) (local $n i32)
    (local.set $name (call $arg (i32.const 2)))
    (if (call $path_open (i32.const 4) (i32.const 0) (local.get $name) (call $strlen (local.get $name))
          (i32.const 0) (i64.const 2) (i64.const 0) (i32.const 0) (i32.const 16))
      (then (call $proc_exit (i32.const 2))))
    (local.set $in (i32.load (i32.const 16)))
    (if (call $create (i32.const 3) (i32.const 4456) (i32.const 8))
      (then (call $proc_exit (i32.const 3))))
    (local.set $out (i32.load (i32.const 16)))
    (block $eof
      (loop $more
        (i32.store (i32.const 0) (i32.const 16384))
        (i32.store (i32.const 4) (i32.const 65536))
        (if (call $fd_read (local.get $in) (i32.const 0) (i32.const 1) (i32.const 8))
          (then (call $proc_exit (i32.const 4))))
        (local.set $n (i32.load (i32.const 8)))
        (br_if $eof (i32.eqz (local.get $n)))
        (if (call $write_all (local.get $out) (i32.const 16384) (local.get $n))
          (then (call $proc_exit (i32.const 5))))
        (br $more)))
    (drop (call $fd_close (local.get $in)))
    (drop (call $fd_close (local.get $out))))

  (func (export "_start")
    (local $mode i32)
    (drop (call $args_sizes_get (i32.const 20) (i32.const 24)))
    (if (i32.or (i32.gt_u (i32.load (i32.const 20)) (i32.const 64))
                (i32.gt_u (i32.load (i32.const 24)) (i32.const 1024)))
      (then (call $proc_exit (i32.const 98))))
    (drop (call $args_get (i32.const 256) (i32.const 1024)))
    (local.set $mode (i32.load8_u (call $arg (i32.const 1))))
    (if (i32.eq (local.get $mode) (i32.const 97)) (then (call $mode_artifact) (return)))
    (if (i32.eq (local.get $mode) (i32.const 120)) (then (call $mode_exit) (return)))
    (if (i32.eq (local.get $mode) (i32.const 102)) (then (call $mode_filesystem) (return)))
    (if (i32.eq (local.get $mode) (i32.const 110)) (then (call $mode_network) (return)))
    (if (i32.eq (local.get $mode) (i32.const 103)) (then (call $mode_grow) (return)))
    (if (i32.eq (local.get $mode) (i32.const 108)) (then (call $mode_loop) (return)))
    (if (i32.eq (local.get $mode) (i32.const 100)) (then (call $mode_determinism) (return)))
    (if (i32.eq (local.get $mode) (i32.const 115)) (then (call $mode_state) (return)))
    (if (i32.eq (local.get $mode) (i32.const 118)) (then (call $mode_environment) (return)))
    (if (i32.eq (local.get $mode) (i32.const 114)) (then (call $mode_copy) (return)))
    (call $proc_exit (i32.const 99)))
)
