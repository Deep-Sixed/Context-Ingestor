#!/bin/bash
# SessionStart hook for Claude Code on the web: installs bubblewrap (the Linux
# parser containment backend) and a dev venv so the suite runs as it does in CI.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

if ! command -v bwrap >/dev/null || ! command -v socat >/dev/null; then
  apt-get install -y bubblewrap socat >/dev/null 2>&1 \
    || { apt-get update >/dev/null 2>&1 || true; apt-get install -y bubblewrap socat; }
fi

# Fail loudly if this container cannot create the namespaces Stele relies on.
bwrap --ro-bind / / --unshare-user --uid 0 --gid 0 --unshare-net -- /usr/bin/true

# The bubblewrap sandbox only exposes /usr, so the venv must come from a system
# Python under /usr/bin (Stele needs 3.12+), matching the CI containment job.
if [ ! -x .venv/bin/python ]; then
  /usr/bin/python3.12 -m venv .venv
fi
.venv/bin/pip install --quiet -e '.[dev]'

echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
