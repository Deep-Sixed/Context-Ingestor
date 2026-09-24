"""
Fake parser: attempts to write outside the sandbox to $HOME.
Expected outcome: write fails (path does not exist), exit non-zero.
"""
import sys

target = "/home/jarvis/stele_evil_write.txt"
try:
    with open(target, "w") as f:
        f.write("this should never reach the host")
    print(f"ERROR: write to {target} succeeded — containment FAILED", flush=True)
    sys.exit(0)  # unexpected success → test will treat exit 0 as containment failure
except OSError as e:
    print(f"blocked: {e}", flush=True)
    sys.exit(1)  # expected: write blocked
