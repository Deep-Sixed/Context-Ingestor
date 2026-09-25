;; ChatGPT export splitter: the first Stele Wasm extractor (roadmap #7).
;;
;; A ChatGPT data export's conversations.json is one JSON array holding every
;; conversation as an object. This module streams that array and writes each
;; element, byte for byte, to its own file in the output directory:
;;
;;   /stele/output/conversation-000000.json   exact bytes of element 0
;;   /stele/output/conversation-000001.json   ...
;;   /stele/output/index.jsonl                one line per element:
;;     {"index":0,"path":"conversation-000000.json","offset":1,"length":123}
;;
;; offset/length locate each element in the input, so every split file can be
;; traced back to the exact source bytes. The split is structural: elements
;; must be JSON objects, and brackets and strings (with escapes) must balance.
;; Element contents are not otherwise validated; downstream parsers do that.
;;
;; The input path comes from STELE_INPUT_PATH (/stele/input/<name>); the
;; /stele/input and /stele/output directories are found by name among the
;; WASI preopens. The module reads 64 KiB at a time, so input size is bounded
;; only by the fuel and time limits.
;;
;; Exit status: 0 success, 1 missing input or environment, 2 malformed input,
;; 3 cannot write output. Errors are reported on stderr.
;;
;; Memory layout (two 64 KiB pages):
;;       0  scratch: iovec 0, count 8, new fd 16, prestat 24, environ sizes 32/36
;;     256  preopen name buffer
;;     512  current file name (24 bytes); 600-640 digit scratch
;;    1024  index line buffer
;;    2048  string constants
;;    4096  bracket stack (4096 levels)
;;    8192  environ pointers (2048 entries)
;;   16384  environ strings (48 KiB)
;;   65536  input buffer (64 KiB)
(module
  (import "wasi_snapshot_preview1" "environ_sizes_get" (func $environ_sizes_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "environ_get" (func $environ_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_prestat_get" (func $fd_prestat_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_prestat_dir_name" (func $fd_prestat_dir_name (param i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "path_open" (func $path_open (param i32 i32 i32 i32 i32 i64 i64 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_read" (func $fd_read (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_write" (func $fd_write (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_close" (func $fd_close (param i32) (result i32)))
  (import "wasi_snapshot_preview1" "proc_exit" (func $proc_exit (param i32)))

  (memory (export "memory") 2)

  ;; Parser states outside an element.
  ;;   0 before '['   1 first element or ']'   2 inside an element
  ;;   3 ',' or ']'   4 element after ','      5 after ']'
  (global $state (mut i32) (i32.const 0))
  (global $depth (mut i32) (i32.const 0))
  (global $in_string (mut i32) (i32.const 0))
  (global $escaped (mut i32) (i32.const 0))
  (global $count (mut i32) (i32.const 0))
  (global $chunk_offset (mut i64) (i64.const 0))  ;; input offset of the buffer start
  (global $element_start (mut i64) (i64.const 0))
  (global $run_start (mut i32) (i32.const 0))    ;; first unwritten buffer byte of the element
  (global $line_len (mut i32) (i32.const 0))
  (global $out_fd (mut i32) (i32.const -1))
  (global $index_fd (mut i32) (i32.const -1))
  (global $input_dir (mut i32) (i32.const -1))
  (global $output_dir (mut i32) (i32.const -1))
  (global $path_ptr (mut i32) (i32.const 0))
  (global $path_len (mut i32) (i32.const 0))

  (data (i32.const 2048) "/stele/input")  ;; input_dir
  (data (i32.const 2060) "/stele/output")  ;; output_dir
  (data (i32.const 2076) "STELE_INPUT_PATH=")  ;; env_key
  (data (i32.const 2096) "/stele/input/")  ;; input_prefix
  (data (i32.const 2112) "index.jsonl")  ;; index_name
  (data (i32.const 2124) "conversation-")  ;; file_prefix
  (data (i32.const 2140) ".json")  ;; file_suffix
  (data (i32.const 2148) "{\22index\22:")  ;; j_index
  (data (i32.const 2160) ",\22path\22:\22")  ;; j_path
  (data (i32.const 2172) "\22,\22offset\22:")  ;; j_offset
  (data (i32.const 2184) ",\22length\22:")  ;; j_length
  (data (i32.const 2196) "}\0a")  ;; j_end
  (data (i32.const 2200) "split ")  ;; done_a
  (data (i32.const 2208) " conversations\0a")  ;; done_b
  (data (i32.const 2224) "chatgpt-split: ")  ;; err_prefix
  (data (i32.const 2240) "STELE_INPUT_PATH is not set or not under /stele/input\0a")  ;; e_env
  (data (i32.const 2296) "cannot find the /stele/input and /stele/output directories\0a")  ;; e_preopen
  (data (i32.const 2356) "cannot open the input file\0a")  ;; e_open
  (data (i32.const 2384) "cannot read the input file\0a")  ;; e_read
  (data (i32.const 2412) "cannot write to the output directory\0a")  ;; e_write
  (data (i32.const 2452) "input is not a JSON array\0a")  ;; e_array
  (data (i32.const 2480) "expected a JSON object as an array element\0a")  ;; e_object
  (data (i32.const 2524) "expected ',' or ']' after an element\0a")  ;; e_after
  (data (i32.const 2564) "unexpected data after the JSON array\0a")  ;; e_trailing
  (data (i32.const 2604) "mismatched bracket inside an element\0a")  ;; e_mismatch
  (data (i32.const 2644) "input ends before the JSON array is closed\0a")  ;; e_truncated
  (data (i32.const 2688) "elements are nested more than 4096 levels deep\0a")  ;; e_deep
  (data (i32.const 2736) "more than 999999 conversations\0a")  ;; e_many

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

  (func $fail (param $msg i32) (param $len i32) (param $code i32)
    (drop (call $write_all (i32.const 2) (i32.const 2224) (i32.const 15) (; err_prefix ;)))
    (drop (call $write_all (i32.const 2) (local.get $msg) (local.get $len)))
    (call $proc_exit (local.get $code))
    unreachable)

  (func $bytes_eq (param $a i32) (param $b i32) (param $n i32) (result i32)
    (block $differ
      (loop $next
        (if (i32.eqz (local.get $n)) (then (return (i32.const 1))))
        (br_if $differ (i32.ne (i32.load8_u (local.get $a)) (i32.load8_u (local.get $b))))
        (local.set $a (i32.add (local.get $a) (i32.const 1)))
        (local.set $b (i32.add (local.get $b) (i32.const 1)))
        (local.set $n (i32.sub (local.get $n) (i32.const 1)))
        (br $next)))
    (i32.const 0))

  (func $strlen (param $p i32) (result i32)
    (local $q i32)
    (local.set $q (local.get $p))
    (block $done
      (loop $next
        (br_if $done (i32.eqz (i32.load8_u (local.get $q))))
        (local.set $q (i32.add (local.get $q) (i32.const 1)))
        (br $next)))
    (i32.sub (local.get $q) (local.get $p)))

  ;; The preopened directory whose guest path is exactly name, or -1.
  (func $find_preopen (param $name i32) (param $len i32) (result i32)
    (local $fd i32)
    (local.set $fd (i32.const 3))
    (block $none
      (loop $next
        (br_if $none (i32.ge_u (local.get $fd) (i32.const 64)))
        (br_if $none (call $fd_prestat_get (local.get $fd) (i32.const 24)))
        (if (i32.and (i32.eqz (i32.load8_u (i32.const 24)))
                     (i32.eq (i32.load (i32.const 28)) (local.get $len)))
          (then
            (if (i32.eqz (call $fd_prestat_dir_name (local.get $fd) (i32.const 256) (local.get $len)))
              (then
                (if (call $bytes_eq (i32.const 256) (local.get $name) (local.get $len))
                  (then (return (local.get $fd))))))))
        (local.set $fd (i32.add (local.get $fd) (i32.const 1)))
        (br $next)))
    (i32.const -1))

  ;; Set $path_ptr/$path_len to STELE_INPUT_PATH relative to /stele/input.
  (func $locate_input
    (local $i i32) (local $entry i32)
    (if (call $environ_sizes_get (i32.const 32) (i32.const 36))
      (then (call $fail (i32.const 2240) (i32.const 54) (; e_env ;) (i32.const 1))))
    (if (i32.or (i32.gt_u (i32.load (i32.const 32)) (i32.const 2048))
                (i32.gt_u (i32.load (i32.const 36)) (i32.const 49152)))
      (then (call $fail (i32.const 2240) (i32.const 54) (; e_env ;) (i32.const 1))))
    (if (call $environ_get (i32.const 8192) (i32.const 16384))
      (then (call $fail (i32.const 2240) (i32.const 54) (; e_env ;) (i32.const 1))))
    (block $found
      (loop $next
        (if (i32.ge_u (local.get $i) (i32.load (i32.const 32)))
          (then (call $fail (i32.const 2240) (i32.const 54) (; e_env ;) (i32.const 1))))
        (local.set $entry (i32.load (i32.add (i32.const 8192) (i32.shl (local.get $i) (i32.const 2)))))
        (br_if $found (call $bytes_eq (local.get $entry) (i32.const 2076) (i32.const 17) (; env_key ;)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $next)))
    ;; entry = "STELE_INPUT_PATH=/stele/input/<relative path>"
    (local.set $entry (i32.add (local.get $entry) (i32.const 17)))
    (if (i32.eqz (call $bytes_eq (local.get $entry) (i32.const 2096) (i32.const 13) (; input_prefix ;)))
      (then (call $fail (i32.const 2240) (i32.const 54) (; e_env ;) (i32.const 1))))
    (global.set $path_ptr (i32.add (local.get $entry) (i32.const 13)))
    (global.set $path_len (call $strlen (global.get $path_ptr)))
    (if (i32.eqz (global.get $path_len))
      (then (call $fail (i32.const 2240) (i32.const 54) (; e_env ;) (i32.const 1)))))

  ;; Decimal digits of v at dst; returns their count.
  (func $decimal (param $v i64) (param $dst i32) (result i32)
    (local $q i32) (local $len i32)
    (local.set $q (i32.const 640))
    (loop $next
      (local.set $q (i32.sub (local.get $q) (i32.const 1)))
      (i64.store8 (local.get $q) (i64.add (i64.const 48) (i64.rem_u (local.get $v) (i64.const 10))))
      (local.set $v (i64.div_u (local.get $v) (i64.const 10)))
      (br_if $next (i64.ne (local.get $v) (i64.const 0))))
    (local.set $len (i32.sub (i32.const 640) (local.get $q)))
    (memory.copy (local.get $dst) (local.get $q) (local.get $len))
    (local.get $len))

  (func $append (param $p i32) (param $n i32)
    (memory.copy (i32.add (i32.const 1024) (global.get $line_len)) (local.get $p) (local.get $n))
    (global.set $line_len (i32.add (global.get $line_len) (local.get $n))))

  (func $append_decimal (param $v i64)
    (global.set $line_len (i32.add (global.get $line_len)
      (call $decimal (local.get $v) (i32.add (i32.const 1024) (global.get $line_len))))))

  ;; Open conversation-NNNNNN.json for the element starting at buffer index i.
  (func $start_element (param $i i32)
    (local $n i32) (local $k i32)
    (if (i32.gt_u (global.get $count) (i32.const 999999))
      (then (call $fail (i32.const 2736) (i32.const 31) (; e_many ;) (i32.const 2))))
    (memory.copy (i32.const 512) (i32.const 2124) (i32.const 13) (; file_prefix ;))
    (local.set $n (global.get $count))
    (local.set $k (i32.const 6))
    (loop $digit
      (local.set $k (i32.sub (local.get $k) (i32.const 1)))
      (i32.store8 (i32.add (i32.const 525) (local.get $k))
        (i32.add (i32.const 48) (i32.rem_u (local.get $n) (i32.const 10))))
      (local.set $n (i32.div_u (local.get $n) (i32.const 10)))
      (br_if $digit (local.get $k)))
    (memory.copy (i32.const 531) (i32.const 2140) (i32.const 5) (; file_suffix ;))
    ;; O_CREAT | O_EXCL, write rights only
    (if (call $path_open (global.get $output_dir) (i32.const 0) (i32.const 512) (i32.const 24)
          (i32.const 5) (i64.const 64) (i64.const 0) (i32.const 0) (i32.const 16))
      (then (call $fail (i32.const 2412) (i32.const 37) (; e_write ;) (i32.const 3))))
    (global.set $out_fd (i32.load (i32.const 16)))
    (global.set $element_start (i64.add (global.get $chunk_offset) (i64.extend_i32_u (local.get $i))))
    (global.set $run_start (local.get $i))
    (global.set $depth (i32.const 0))
    (global.set $in_string (i32.const 0))
    (global.set $escaped (i32.const 0))
    (call $push (i32.const 123))
    (global.set $state (i32.const 2)))

  ;; Write buffer bytes [run_start, end) of the current element.
  (func $flush_run (param $end i32)
    (if (i32.gt_u (local.get $end) (global.get $run_start))
      (then
        (if (call $write_all (global.get $out_fd)
              (i32.add (i32.const 65536) (global.get $run_start))
              (i32.sub (local.get $end) (global.get $run_start)))
          (then (call $fail (i32.const 2412) (i32.const 37) (; e_write ;) (i32.const 3)))))))

  ;; The element ended at buffer index i (inclusive): close it, index it.
  (func $finish_element (param $i i32)
    (local $end i64)
    (call $flush_run (i32.add (local.get $i) (i32.const 1)))
    (if (call $fd_close (global.get $out_fd))
      (then (call $fail (i32.const 2412) (i32.const 37) (; e_write ;) (i32.const 3))))
    (local.set $end (i64.add (global.get $chunk_offset) (i64.extend_i32_u (i32.add (local.get $i) (i32.const 1)))))
    (global.set $line_len (i32.const 0))
    (call $append (i32.const 2148) (i32.const 9) (; j_index ;))
    (call $append_decimal (i64.extend_i32_u (global.get $count)))
    (call $append (i32.const 2160) (i32.const 9) (; j_path ;))
    (call $append (i32.const 512) (i32.const 24))
    (call $append (i32.const 2172) (i32.const 11) (; j_offset ;))
    (call $append_decimal (global.get $element_start))
    (call $append (i32.const 2184) (i32.const 10) (; j_length ;))
    (call $append_decimal (i64.sub (local.get $end) (global.get $element_start)))
    (call $append (i32.const 2196) (i32.const 2) (; j_end ;))
    (if (call $write_all (global.get $index_fd) (i32.const 1024) (global.get $line_len))
      (then (call $fail (i32.const 2412) (i32.const 37) (; e_write ;) (i32.const 3))))
    (global.set $count (i32.add (global.get $count) (i32.const 1)))
    (global.set $state (i32.const 3)))

  (func $push (param $c i32)
    (if (i32.ge_u (global.get $depth) (i32.const 4096))
      (then (call $fail (i32.const 2688) (i32.const 47) (; e_deep ;) (i32.const 2))))
    (i32.store8 (i32.add (i32.const 4096) (global.get $depth)) (local.get $c))
    (global.set $depth (i32.add (global.get $depth) (i32.const 1))))

  ;; Pop the bracket that c closes; '}' must close '{' and ']' must close '['.
  (func $pop (param $c i32)
    (local $open i32)
    (global.set $depth (i32.sub (global.get $depth) (i32.const 1)))
    (local.set $open (i32.load8_u (i32.add (i32.const 4096) (global.get $depth))))
    (if (i32.ne (local.get $open) (select (i32.const 123) (i32.const 91) (i32.eq (local.get $c) (i32.const 125))))
      (then (call $fail (i32.const 2604) (i32.const 37) (; e_mismatch ;) (i32.const 2)))))

  (func $is_space (param $c i32) (result i32)
    (i32.or
      (i32.or (i32.eq (local.get $c) (i32.const 32)) (i32.eq (local.get $c) (i32.const 10)))
      (i32.or (i32.eq (local.get $c) (i32.const 13)) (i32.eq (local.get $c) (i32.const 9)))))

  ;; One byte inside an element.
  (func $element_byte (param $c i32) (param $i i32)
    (if (global.get $in_string)
      (then
        (if (global.get $escaped)
          (then (global.set $escaped (i32.const 0)))
          (else
            (if (i32.eq (local.get $c) (i32.const 92))
              (then (global.set $escaped (i32.const 1)))
              (else
                (if (i32.eq (local.get $c) (i32.const 34))
                  (then (global.set $in_string (i32.const 0))))))))
        (return)))
    (if (i32.eq (local.get $c) (i32.const 34))
      (then (global.set $in_string (i32.const 1)) (return)))
    (if (i32.or (i32.eq (local.get $c) (i32.const 123)) (i32.eq (local.get $c) (i32.const 91)))
      (then (call $push (local.get $c)) (return)))
    (if (i32.or (i32.eq (local.get $c) (i32.const 125)) (i32.eq (local.get $c) (i32.const 93)))
      (then
        (call $pop (local.get $c))
        (if (i32.eqz (global.get $depth))
          (then (call $finish_element (local.get $i)))))))

  ;; One byte of array structure between elements.
  (func $structure_byte (param $c i32) (param $i i32)
    (local $s i32)
    (if (call $is_space (local.get $c)) (then (return)))
    (local.set $s (global.get $state))
    (if (i32.eqz (local.get $s))
      (then
        (if (i32.ne (local.get $c) (i32.const 91))
          (then (call $fail (i32.const 2452) (i32.const 26) (; e_array ;) (i32.const 2))))
        (global.set $state (i32.const 1))
        (return)))
    (if (i32.eq (local.get $s) (i32.const 1))
      (then
        (if (i32.eq (local.get $c) (i32.const 93))
          (then (global.set $state (i32.const 5)) (return)))))
    (if (i32.or (i32.eq (local.get $s) (i32.const 1)) (i32.eq (local.get $s) (i32.const 4)))
      (then
        (if (i32.ne (local.get $c) (i32.const 123))
          (then (call $fail (i32.const 2480) (i32.const 43) (; e_object ;) (i32.const 2))))
        (call $start_element (local.get $i))
        (return)))
    (if (i32.eq (local.get $s) (i32.const 3))
      (then
        (if (i32.eq (local.get $c) (i32.const 44))
          (then (global.set $state (i32.const 4)) (return)))
        (if (i32.eq (local.get $c) (i32.const 93))
          (then (global.set $state (i32.const 5)) (return)))
        (call $fail (i32.const 2524) (i32.const 37) (; e_after ;) (i32.const 2))))
    (call $fail (i32.const 2564) (i32.const 37) (; e_trailing ;) (i32.const 2)))

  (func (export "_start")
    (local $in i32) (local $n i32) (local $i i32) (local $c i32)
    (call $locate_input)
    (global.set $input_dir (call $find_preopen (i32.const 2048) (i32.const 12) (; input_dir ;)))
    (global.set $output_dir (call $find_preopen (i32.const 2060) (i32.const 13) (; output_dir ;)))
    (if (i32.or (i32.lt_s (global.get $input_dir) (i32.const 0))
                (i32.lt_s (global.get $output_dir) (i32.const 0)))
      (then (call $fail (i32.const 2296) (i32.const 59) (; e_preopen ;) (i32.const 1))))
    ;; read rights only
    (if (call $path_open (global.get $input_dir) (i32.const 0) (global.get $path_ptr) (global.get $path_len)
          (i32.const 0) (i64.const 2) (i64.const 0) (i32.const 0) (i32.const 16))
      (then (call $fail (i32.const 2356) (i32.const 27) (; e_open ;) (i32.const 1))))
    (local.set $in (i32.load (i32.const 16)))
    (if (call $path_open (global.get $output_dir) (i32.const 0) (i32.const 2112) (i32.const 11) (; index_name ;)
          (i32.const 5) (i64.const 64) (i64.const 0) (i32.const 0) (i32.const 16))
      (then (call $fail (i32.const 2412) (i32.const 37) (; e_write ;) (i32.const 3))))
    (global.set $index_fd (i32.load (i32.const 16)))

    (block $eof
      (loop $chunk
        (i32.store (i32.const 0) (i32.const 65536))
        (i32.store (i32.const 4) (i32.const 65536))
        (if (call $fd_read (local.get $in) (i32.const 0) (i32.const 1) (i32.const 8))
          (then (call $fail (i32.const 2384) (i32.const 27) (; e_read ;) (i32.const 1))))
        (local.set $n (i32.load (i32.const 8)))
        (br_if $eof (i32.eqz (local.get $n)))
        (global.set $run_start (i32.const 0))
        (local.set $i (i32.const 0))
        (block $end
          (loop $byte
            (br_if $end (i32.ge_u (local.get $i) (local.get $n)))
            (local.set $c (i32.load8_u (i32.add (i32.const 65536) (local.get $i))))
            (if (i32.eq (global.get $state) (i32.const 2))
              (then (call $element_byte (local.get $c) (local.get $i)))
              (else (call $structure_byte (local.get $c) (local.get $i))))
            (local.set $i (i32.add (local.get $i) (i32.const 1)))
            (br $byte)))
        (if (i32.eq (global.get $state) (i32.const 2))
          (then (call $flush_run (local.get $n))))
        (global.set $chunk_offset (i64.add (global.get $chunk_offset) (i64.extend_i32_u (local.get $n))))
        (br $chunk)))

    (if (i32.ne (global.get $state) (i32.const 5))
      (then (call $fail (i32.const 2644) (i32.const 43) (; e_truncated ;) (i32.const 2))))
    (drop (call $fd_close (local.get $in)))
    (if (call $fd_close (global.get $index_fd))
      (then (call $fail (i32.const 2412) (i32.const 37) (; e_write ;) (i32.const 3))))
    (drop (call $write_all (i32.const 1) (i32.const 2200) (i32.const 6) (; done_a ;)))
    (drop (call $write_all (i32.const 1) (i32.const 1024)
      (call $decimal (i64.extend_i32_u (global.get $count)) (i32.const 1024))))
    (drop (call $write_all (i32.const 1) (i32.const 2208) (i32.const 15) (; done_b ;))))
)
