#!/usr/bin/env python3
"""Poll TTS backend /health endpoints until all are ready (or timeout).

Used as a pre-start gate before the UI server begins listening:
    python wait_health.py && exec python -u ui/server.py

Exits 0 either when all backends respond or after the timeout (whichever
comes first) so a single broken backend never blocks startup indefinitely.
"""

import sys
import time
import urllib.request

PORTS = [8095, 8096]
TIMEOUT_S = 120


def _healthy(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


def main() -> int:
    for elapsed in range(1, TIMEOUT_S + 1):
        if all(_healthy(p) for p in PORTS):
            print(f"[wait-health] all backends ready ({elapsed}s)", flush=True)
            return 0
        time.sleep(1)

    print(
        f"[wait-health] WARNING: not all backends ready after {TIMEOUT_S}s, continuing",
        flush=True,
    )
    return 0  # don't block startup forever


if __name__ == "__main__":
    sys.exit(main())
