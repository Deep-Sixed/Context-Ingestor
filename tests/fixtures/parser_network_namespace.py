from __future__ import annotations

import socket
import sys

interfaces = {name for _, name in socket.if_nameindex()}
unexpected = interfaces - {"lo"}
if unexpected:
    print(f"unexpected network interfaces: {sorted(unexpected)}")
    sys.exit(1)

print("network namespace isolated")
