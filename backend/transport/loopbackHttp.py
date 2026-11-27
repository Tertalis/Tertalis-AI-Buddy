# file: backend/transport/loopbackHttp.py ; version: 1
"""Shared IPv4-loopback HTTP mechanics for first-party local endpoints.

Moved out of the G-01 observation bridge so every local endpoint uses one
reviewed implementation of: literal 127.0.0.1 binding, bounded socket I/O,
client interruption on close, silent error handling (nothing about requests,
paths or credentials is logged), exact Host and Origin checks, and
constant-time Bearer comparison. Transport context is not identity: these
helpers authenticate a single per-endpoint credential and nothing more.
"""
from __future__ import annotations

import secrets
import socket
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from threading import RLock
from urllib.parse import urlsplit

__all__ = [
    "LOOPBACK_HOST",
    "LoopbackHttpServer",
    "QuietHandler",
    "ThreadingLoopbackHttpServer",
    "bearerMatches",
    "exactHost",
    "newCredential",
    "singleOrigin",
    "validateLocalOrigins",
]

LOOPBACK_HOST = "127.0.0.1"


def newCredential() -> str:
    """Returns a fresh 256-bit urlsafe credential."""
    return secrets.token_urlsafe(32)


def validateLocalOrigins(origins: object, *, limit: int = 8) -> tuple[str, ...]:
    """Accepts only explicitly named local HTTP origins with concrete ports."""
    if type(origins) not in (list, tuple) or len(origins) > limit:
        raise ValueError("allowedOrigins must be a bounded list.")
    result = []
    for origin in origins:
        if type(origin) is not str:
            raise ValueError("Invalid viewer origin.")
        parsed = urlsplit(origin)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or parsed.port is None or not 1 <= parsed.port <= 65535 or parsed.netloc != origin[len("http://"):]):
            raise ValueError("Viewer origin must be an exact local HTTP origin with a port.")
        result.append(origin)
    return tuple(dict.fromkeys(result))


def exactHost(headers, authority: str) -> bool:
    """True only for exactly one Host header equal to the endpoint's literal authority."""
    return headers.get_all("Host", []) == [authority]


def singleOrigin(headers, allowed: Iterable[str]) -> str | None:
    """Returns the single allowed Origin, None when absent; raises PermissionError otherwise."""
    origins = headers.get_all("Origin", [])
    if not origins:
        return None
    if len(origins) != 1 or origins[0] not in tuple(allowed):
        raise PermissionError("Origin rejected.")
    return origins[0]


def bearerMatches(headers, credential: str) -> bool:
    """Constant-time check of exactly one Authorization header against the credential."""
    if not credential:
        return False
    values = headers.get_all("Authorization", [])
    try:
        return len(values) == 1 and secrets.compare_digest(values[0].encode("ascii"), ("Bearer " + credential).encode("ascii"))
    except UnicodeError:
        return False


class LoopbackHttpServer(HTTPServer):
    """Literal-loopback server with bounded socket I/O and interruptible clients."""

    request_queue_size = 8
    allow_reuse_address = False

    def __init__(self, port: int, handlerClass, *, isStopping: Callable[[], bool], socketTimeout: float = 1.0, host: str = LOOPBACK_HOST, maxClients: int | None = None) -> None:
        """Binds 127.0.0.1 only; isStopping lets the owner refuse new connections during close.

        maxClients bounds simultaneously accepted connections (and so handler
        threads in the threading variant); excess connections are closed unread.
        """
        if type(host) is not str or host != LOOPBACK_HOST:
            raise ValueError("Local endpoint binding must be 127.0.0.1.")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535.")
        self._isStopping = isStopping
        self._socketTimeout = socketTimeout
        self._maxClients = maxClients
        self._clientsLock = RLock()
        self._clients: set[socket.socket] = set()
        super().__init__((host, port), handlerClass)
        self.authority = f"{LOOPBACK_HOST}:{self.server_port}"

    def get_request(self):
        """Applies a bounded socket I/O timeout before processing any HTTP headers."""
        connection, address = super().get_request()
        connection.settimeout(self._socketTimeout)
        with self._clientsLock:
            if self._isStopping():
                connection.close()
                raise OSError("Local endpoint is stopping.")
            if self._maxClients is not None and len(self._clients) >= self._maxClients:
                connection.close()
                raise OSError("Local endpoint connection limit reached.")
            self._clients.add(connection)
        return connection, address

    def interruptClients(self) -> None:
        """Unblocks partial headers, pending writes and open streams before shutdown waits."""
        with self._clientsLock:
            for connection in self._clients:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close_request(self, request) -> None:
        """Drops the accepted socket from the finite active-connection set on every exit."""
        with self._clientsLock:
            self._clients.discard(request)
        super().close_request(request)

    def handle_error(self, request, client_address) -> None:
        """Isolates broken local connections without logging requests, credentials or internals."""


class ThreadingLoopbackHttpServer(ThreadingMixIn, LoopbackHttpServer):
    """Thread-per-connection variant for long-lived streams; threads are joined on close."""

    daemon_threads = True
    block_on_close = True


class QuietHandler(BaseHTTPRequestHandler):
    """Handler base that never logs and never reflects request syntax in errors."""

    sys_version = ""

    def log_message(self, format, *args) -> None:
        """Suppresses access/error logging so headers, paths and credentials are never emitted."""
