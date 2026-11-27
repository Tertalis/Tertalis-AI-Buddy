# file: tests/backend/transport/test_loopbackHttp.py ; version: 1
"""Shared loopback HTTP mechanics: binding, header checks, connection cap, quiet close."""
from __future__ import annotations

import socket
import time
from email.message import Message
from threading import Event, Thread

import pytest

from backend.transport.loopbackHttp import (
    LoopbackHttpServer,
    QuietHandler,
    ThreadingLoopbackHttpServer,
    bearerMatches,
    exactHost,
    newCredential,
    singleOrigin,
    validateLocalOrigins,
)


def headers(**values) -> Message:
    message = Message()
    for name, value in values.items():
        for item in value if isinstance(value, list) else [value]:
            message[name.replace("_", "-")] = item
    return message


def test_binding_is_literal_loopback_only():
    for host in ("0.0.0.0", "localhost", "::1"):
        with pytest.raises(ValueError):
            LoopbackHttpServer(0, QuietHandler, isStopping=lambda: False, host=host)
    with pytest.raises(ValueError):
        LoopbackHttpServer(70000, QuietHandler, isStopping=lambda: False)
    server = LoopbackHttpServer(0, QuietHandler, isStopping=lambda: False)
    try:
        assert server.authority == f"127.0.0.1:{server.server_port}"
    finally:
        server.server_close()


def test_header_checks_reject_duplicates_and_mismatches():
    assert exactHost(headers(Host="127.0.0.1:5"), "127.0.0.1:5")
    assert not exactHost(headers(Host=["127.0.0.1:5", "127.0.0.1:5"]), "127.0.0.1:5")
    assert not exactHost(headers(Host="localhost:5"), "127.0.0.1:5")
    assert singleOrigin(headers(), ("http://127.0.0.1:5",)) is None
    assert singleOrigin(headers(Origin="http://127.0.0.1:5"), ("http://127.0.0.1:5",)) == "http://127.0.0.1:5"
    for value in ("http://evil.example", ["http://127.0.0.1:5", "http://127.0.0.1:5"], "null"):
        with pytest.raises(PermissionError):
            singleOrigin(headers(Origin=value), ("http://127.0.0.1:5",))
    credential = newCredential()
    assert len(credential) >= 43 and credential != newCredential()
    assert bearerMatches(headers(Authorization="Bearer " + credential), credential)
    assert not bearerMatches(headers(Authorization=["Bearer " + credential] * 2), credential)
    assert not bearerMatches(headers(Authorization="Bearer x"), credential)
    assert not bearerMatches(headers(Authorization="Bearer é"), credential)
    assert not bearerMatches(headers(Authorization="Bearer "), "")


def test_local_origin_validation_matches_g01_rules():
    assert validateLocalOrigins(["http://127.0.0.1:7777", "http://127.0.0.1:7777"]) == ("http://127.0.0.1:7777",)
    for bad in (["http://127.0.0.1"], ["https://127.0.0.1:1"], ["http://evil.example:1"], ["http://127.0.0.1:1/"], "x", [1]):
        with pytest.raises(ValueError):
            validateLocalOrigins(bad)


class _Hold(QuietHandler):
    """Holds each connection until released, to exercise the connection cap."""

    release = Event()

    def do_GET(self):
        _Hold.release.wait(5)
        self.send_response(204)
        self.end_headers()


def test_connection_cap_and_interrupt_on_close():
    _Hold.release.clear()
    server = ThreadingLoopbackHttpServer(0, _Hold, isStopping=lambda: False, maxClients=1, socketTimeout=5)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    worker.start()
    try:
        first = socket.create_connection(("127.0.0.1", server.server_port))
        first.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
        time.sleep(0.1)
        second = socket.create_connection(("127.0.0.1", server.server_port))
        second.settimeout(2)
        try:
            second.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
            received = second.recv(64)
        except ConnectionResetError:
            received = b""
        assert received == b"", "connections beyond the cap are closed unread"
        second.close()
        server.interruptClients()
        _Hold.release.set()
        first.close()
    finally:
        _Hold.release.set()
        server.shutdown()
        worker.join(5)
        server.server_close()
    assert not worker.is_alive()
