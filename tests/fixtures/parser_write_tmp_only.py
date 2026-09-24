"""
Fake parser: writes to /tmp inside the sandbox but NOT to /stele/output.
Used to prove that /tmp writes are ephemeral — they do not appear as artifacts
on the host after the sandbox exits.
"""
import sys

tmp_path = "/tmp/stele_secret_tmp.txt"
with open(tmp_path, "w") as f:
    f.write("this lives only inside the sandbox tmpfs")

print(f"wrote to {tmp_path} — this must not appear on host", flush=True)
sys.exit(0)
