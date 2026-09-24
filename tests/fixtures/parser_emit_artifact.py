"""
Fake parser: writes a single artifact to the approved output directory.
Expected outcome: exit 0, artifact present in /stele/output/.
"""
import json
import os
import sys

output_dir = os.environ.get("STELE_OUTPUT_DIR", "/stele/output")

artifact = {
    "parser": "fake_parser_v0",
    "chunks": [{"id": "c1", "content": "hello from sandbox"}],
}

out_path = os.path.join(output_dir, "result.json")
with open(out_path, "w") as f:
    json.dump(artifact, f)

print(f"artifact written to {out_path}", flush=True)
sys.exit(0)
