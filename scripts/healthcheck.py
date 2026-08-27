#!/usr/bin/env python3
"""Container liveness probe.

A file rather than an inline command, because the inline version had to
survive YAML escaping and then a shell, and the CRLF in an HTTP request does
not come through either intact.

Deliberately cheap: a raw socket under `python -S`, so neither urllib nor the
site packages are imported. That matters because the probe is paid out of the
same CPU quota the analysis is saturating — the previous urllib version cost
1.8s at idle, timed out under normal load, and Swarm killed healthy containers
in the middle of an analysis.

Exit 0 when the server answers 200, 1 otherwise.
"""
import socket
import sys

REQUEST = b"GET /api/health HTTP/1.0\r\nHost: localhost\r\n\r\n"


def main() -> int:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=5) as sock:
            sock.sendall(REQUEST)
            head = sock.recv(64)
    except OSError:
        return 1
    return 0 if b" 200 " in head else 1


if __name__ == "__main__":
    sys.exit(main())
