"""
Malicious parser: plants symlinks in /stele/output pointing at host paths,
alongside one legitimate artifact.
Expected outcome: the run is not succeeded, the symlinks are reported in
rejected_paths, and nothing they point at is ever hashed or ledgered.
"""
import os
import sys

output_dir = os.environ.get("STELE_OUTPUT_DIR", "/stele/output")

with open(os.path.join(output_dir, "result.json"), "w") as f:
    f.write('{"ok": true}')

os.symlink(os.environ["STELE_TEST_SYMLINK_TARGET"], os.path.join(output_dir, "leak.txt"))
os.symlink("/usr", os.path.join(output_dir, "leakdir"))
os.symlink("/does/not/exist", os.path.join(output_dir, "dangling"))

print("symlinks planted", flush=True)
sys.exit(0)
