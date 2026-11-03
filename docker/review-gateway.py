"""Tiny TCP allowlist gateway for isolated reviewer containers.

The reviewer runs on an internal Docker network with no default route. This
gateway is attached to both that network and the normal bridge. It forwards:

- 11434 -> host.docker.internal:11434 (the model endpoint)
- 443   -> mcp.context7.com:443 (Context7 only)

Everything else is unreachable from the reviewer container.
"""

from __future__ import annotations

import socket
import socketserver
import sys
import threading

_BUFFER = 65536
_CONNECT_TIMEOUT = 15


class _Proxy(socketserver.BaseRequestHandler):
    """Forward one accepted TCP connection to a fixed upstream."""

    def handle(self) -> None:
        upstream = socket.create_connection(
            (self.server.upstream_host, self.server.upstream_port),
            timeout=_CONNECT_TIMEOUT,
        )
        client = self.request
        client.settimeout(None)
        upstream.settimeout(None)
        try:
            reverse = threading.Thread(
                target=_pipe,
                args=(client, upstream),
                daemon=True,
            )
            reverse.start()
            _pipe(upstream, client)
            reverse.join(timeout=1)
        finally:
            _close(upstream)
            _close(client)


class _ProxyServer(socketserver.ThreadingTCPServer):
    """Thread-per-connection TCP server with an upstream address."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], upstream: tuple[str, int]) -> None:
        self.upstream_host, self.upstream_port = upstream
        super().__init__(address, _Proxy)


def _pipe(source: socket.socket, destination: socket.socket) -> None:
    try:
        while True:
            data = source.recv(_BUFFER)
            if not data:
                break
            destination.sendall(data)
    except OSError:
        return


def _close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        return


def _serve(port: int, upstream: tuple[str, int]) -> None:
    server = _ProxyServer(("0.0.0.0", port), upstream)
    print(
        f"orch-review-gateway: {port} -> {upstream[0]}:{upstream[1]}",
        flush=True,
    )
    server.serve_forever(poll_interval=0.5)


def main() -> int:
    """Start both allowlisted ports and block forever."""
    try:
        model = threading.Thread(
            target=_serve,
            args=(11434, ("host.docker.internal", 11434)),
            daemon=True,
        )
        context7 = threading.Thread(
            target=_serve,
            args=(443, ("mcp.context7.com", 443)),
            daemon=True,
        )
        model.start()
        context7.start()
        model.join()
        context7.join()
    except OSError as ex:
        print(f"orch-review-gateway: {ex}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
