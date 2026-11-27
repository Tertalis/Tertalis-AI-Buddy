# file: first-party/frontendHost/host.py ; version: 2
"""Loopback browser frontend host: serves declared browser CodeEntries and one channel per run.

Security posture follows G-01 (literal 127.0.0.1, exact Host, no logging,
no-store, constant-time Bearer) with one recorded deviation: the launch URL
carries the credential in its fragment (#token=...). Fragments are never sent
in requests, so the server still rejects credentials in request URLs.

Unauthenticated (Host-checked): "/", "/boot.mjs", "/boot.css", "/pack/<packId>/<source>".
Browser module imports cannot send Authorization, and these are public code.
Authenticated: "/session", "/snapshot", "/stream", "/invoke".
"""
from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock, Thread
from urllib.parse import parse_qs, urlsplit

from backend.core.runtimeIds import newRuntimeId
from backend.transport.loopbackHttp import (
    QuietHandler,
    ThreadingLoopbackHttpServer,
    bearerMatches,
    exactHost,
    newCredential,
    singleOrigin,
)

from .channel import FrontendChannel
from .protocol import INVOCATIONS_TRACK, MAX_INVOCATION_BODY, ProtocolError, encode, flowError, parseInvocation

__all__ = ["FrontendHost", "ServedSource"]

ROOT = Path(__file__).resolve().parent
JS_MODULE_FORMAT = "js-module-source@1"
MAX_VIEWS_LIMIT = 32
STREAM_KEEPALIVE_SECONDS = 15.0
MAX_STREAM_QUERY = 8 * 1024
MAX_UNREAD_DRAIN = 1024 * 1024
DRAIN_SECONDS = 1.0
_URL_CREDENTIAL_KEYS = frozenset({"token", "credential", "access_token", "authorization", "bearer"})
_PACK_PATH = re.compile(r"/pack/([A-Za-z0-9_.-]{1,128})/([A-Za-z0-9_./-]{1,256}\.mjs)")
_FIXED = {
    "/": ("bootstrap.html", "text/html; charset=utf-8"),
    "/index.html": ("bootstrap.html", "text/html; charset=utf-8"),
    "/boot.mjs": ("boot.mjs", "text/javascript; charset=utf-8"),
    "/boot.css": ("boot.css", "text/css; charset=utf-8"),
}
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


@dataclass(frozen=True, slots=True)
class ServedSource:
    """Exactly the bytes the PackLoader hashed, addressed by Pack and declared source path."""

    packId: str
    codeEntryId: str
    role: str
    body: bytes
    sha256: str


class FrontendHost:
    """Pack-owned endpoint and channel. Never retains invocation context or calls the runtime."""

    def __init__(self, *, port: int = 0, maxViews: int = 8, title: str = "Tertalis", main: dict | None = None) -> None:
        """Starts the loopback endpoint; the channel and served sources are sealed later by seal()."""
        if type(maxViews) is not int or not 1 <= maxViews <= MAX_VIEWS_LIMIT:
            raise ValueError(f"maxViews must be an integer between 1 and {MAX_VIEWS_LIMIT}.")
        if type(title) is not str or not 1 <= len(title) <= 120:
            raise ValueError("title must be a short string.")
        if main is not None and (type(main) is not dict or set(main) != {"packId", "codeEntryId"}
                                 or not all(type(value) is str and value for value in main.values())):
            raise ValueError("main must be {packId, codeEntryId}.")
        self.runId = newRuntimeId()
        self._title = title
        self._mainSelector = main
        self._maxViews = maxViews
        self._lock = RLock()
        self._stopping = Event()
        self._credential = newCredential()
        self._fixed = {path: ((ROOT / name).read_bytes(), contentType) for path, (name, contentType) in _FIXED.items()}
        self._sources: dict[str, ServedSource] = {}
        self._mainUrl: str | None = None
        self._channel: FrontendChannel | None = None
        self._views = 0
        self._server = None
        self._thread = None
        try:
            self._server = _Server(port, self, maxClients=maxViews + 16)
            self._authority = self._server.authority
            self._origin = f"http://{self._authority}"
            self._thread = Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="tertalis-frontend-host", daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    # ----- capability-facing -------------------------------------------------

    def seal(self, declaredEntries) -> FrontendChannel:
        """Builds the served-source table once from declared js entries and returns the run's channel."""
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("Frontend host is closed.")
            if self._channel is not None:
                return self._channel
            sources: dict[str, ServedSource] = {}
            mains = []
            for entry in declaredEntries:
                if entry.format != JS_MODULE_FORMAT:
                    continue
                url = f"/pack/{entry.packId}/{entry.source}"
                if not _PACK_PATH.fullmatch(url) or ".." in entry.source.split("/"):
                    raise ValueError(f"Declared browser source cannot be served safely: {entry.packId}/{entry.source}.")
                sources[url] = ServedSource(entry.packId, entry.codeEntryId, entry.role, entry.sourceBytes, entry.sourceSha256)
                if entry.role == "main":
                    mains.append((entry.packId, entry.codeEntryId, url))
            if self._mainSelector is not None:
                chosen = [item for item in mains if item[:2] == (self._mainSelector["packId"], self._mainSelector["codeEntryId"])]
                if not chosen:
                    raise LookupError(f"No active browser main CodeEntry {self._mainSelector['packId']}/{self._mainSelector['codeEntryId']}.")
            elif len(mains) == 1:
                chosen = mains
            else:
                raise LookupError("Configure frontendHost.main: there is not exactly one active browser main CodeEntry.")
            self._sources = sources
            self._mainUrl = chosen[0][2]
            self._channel = FrontendChannel(runId=self.runId)
            return self._channel

    def connection(self) -> dict:
        """Launch URL for display by the launcher only (it contains the credential)."""
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("Frontend host is closed.")
            return {"url": f"{self._origin}/#token={self._credential}", "origin": self._origin, "runId": self.runId}

    def close(self) -> None:
        """Ends streams and waiters, closes and joins the endpoint, forgets the credential."""
        with self._lock:
            self._stopping.set()
            channel = self._channel
        if channel is not None:
            channel.close()
        if self._server is not None:
            self._server.interruptClients()
        try:
            if self._thread is not None and self._thread.is_alive():
                self._server.shutdown()
                self._thread.join(5)
                if self._thread.is_alive():
                    raise RuntimeError("Frontend endpoint did not terminate.")
        finally:
            try:
                if self._server is not None:
                    self._server.server_close()
            finally:
                self._credential = ""

    # ----- HTTP-facing (called on handler threads; never touches the runtime) ---

    def _enterView(self) -> bool:
        with self._lock:
            if self._views >= self._maxViews:
                return False
            self._views += 1
            return True

    def _leaveView(self) -> None:
        with self._lock:
            self._views -= 1

    def _descriptor(self) -> dict | None:
        channel = self._channel
        if channel is None:
            return None
        return {"runId": self.runId, "title": self._title, "main": self._mainUrl,
                "tracks": [{"name": name, "style": style} for name, style in channel.tracks()]}


class _Server(ThreadingLoopbackHttpServer):
    """Thread per connection (streams are long-lived); connections are capped."""

    def __init__(self, port: int, host: FrontendHost, *, maxClients: int) -> None:
        self.host = host
        super().__init__(port, _Handler, isStopping=host._stopping.is_set, maxClients=maxClients)


class _Handler(QuietHandler):
    """Same-origin browser API with exact Host/Origin checks and Bearer authentication."""

    server_version = "TertalisFrontendHost"
    protocol_version = "HTTP/1.1"

    def send_error(self, code, message=None, explain=None) -> None:
        """Fixed, non-reflective errors."""
        self._json(int(code), {"error": "http-request-rejected"})

    # replies -----------------------------------------------------------------

    def _headers(self, status: int, contentType: str, length: int | None, *, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", contentType)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _body(self, status: int, body: bytes, contentType: str, *, extra: dict | None = None) -> None:
        self.close_connection = True
        self._headers(status, contentType, len(body), extra=extra)
        if body and self.command != "HEAD":
            self.wfile.write(body)
        if self.command == "POST" and not getattr(self, "_bodyConsumed", False):
            self._lingeringClose()

    def _lingeringClose(self) -> None:
        """Rejected before reading the request body: half-close, then drain (bounded) so the peer
        reads the response. Closing with unread received data makes the OS send RST (notably on
        Windows), which would destroy the response before the client reads it."""
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + DRAIN_SECONDS
            drained = 0
            while drained < MAX_UNREAD_DRAIN and time.monotonic() < deadline:
                chunk = self.connection.recv(65536)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass

    def _json(self, status: int, value: object) -> None:
        self._body(status, encode(value), "application/json")

    # validation --------------------------------------------------------------

    def _target(self) -> tuple[str, dict[str, list[str]]] | None:
        """Exact Host, same-origin Origin (required for POST), no credentials in the URL."""
        host = self.server.host
        if not exactHost(self.headers, host._authority):
            self._json(403, {"error": "host-rejected"})
            return None
        try:
            origin = singleOrigin(self.headers, (host._origin,))
        except PermissionError:
            self._json(403, {"error": "origin-rejected"})
            return None
        if self.command == "POST" and origin is None:
            self._json(403, {"error": "origin-required"})
            return None
        if len(self.path) > MAX_STREAM_QUERY:
            self._json(414, {"error": "target-too-long"})
            return None
        parts = urlsplit(self.path)
        if parts.fragment or parts.netloc or parts.scheme:
            self._json(400, {"error": "invalid-target"})
            return None
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=False, max_num_fields=64) if parts.query else {}
        if _URL_CREDENTIAL_KEYS & {key.lower() for key in query}:
            self._json(400, {"error": "credential-in-url-rejected"})
            return None
        return parts.path, query

    def _authorized(self) -> bool:
        if not bearerMatches(self.headers, self.server.host._credential):
            self._json(401, {"error": "credential-required"})
            return False
        return True

    # routes ------------------------------------------------------------------

    def do_GET(self) -> None:
        target = self._target()
        if target is None:
            return
        path, query = target
        host = self.server.host
        if path in host._fixed:
            if query:
                self._json(400, {"error": "unexpected-query"})
                return
            body, contentType = host._fixed[path]
            self._body(200, body, contentType)
            return
        if path.startswith("/pack/"):
            source = host._sources.get(path)
            if source is None or query:
                self._json(404, {"error": "unknown-source"})
                return
            self._body(200, source.body, "text/javascript; charset=utf-8", extra={"ETag": f'"{source.sha256}"'})
            return
        if path not in ("/session", "/snapshot", "/stream"):
            self._json(404, {"error": "unknown-target"})
            return
        if not self._authorized():
            return
        channel = host._channel
        if channel is None:
            self._json(503, {"error": "not-ready"})
            return
        if path == "/session":
            if query:
                self._json(400, {"error": "unexpected-query"})
                return
            self._json(200, host._descriptor())
        elif path == "/snapshot":
            names = query.get("track", [])
            if set(query) != {"track"} or len(names) != 1:
                self._json(400, {"error": "invalid-query"})
                return
            snapshot = channel.snapshot(names[0])
            if snapshot is None:
                self._json(404, {"error": "unknown-track"})
                return
            self._json(200, snapshot)
        else:
            self._stream(channel, query)

    def do_HEAD(self) -> None:
        self._json(405, {"error": "method-not-allowed"})

    def do_POST(self) -> None:
        target = self._target()
        if target is None:
            return
        path, query = target
        if path != "/invoke" or query:
            self._json(404, {"error": "unknown-target"})
            return
        if not self._authorized():
            return
        channel = self.server.host._channel
        if channel is None:
            self._json(503, flowError("not-ready", "The application has not connected its frontend yet."))
            return
        contentTypes = self.headers.get_all("Content-Type", [])
        lengths = self.headers.get_all("Content-Length", [])
        if len(contentTypes) != 1 or not contentTypes[0].lower().startswith("application/json"):
            self._json(415, flowError("invalid-json", "Invocations must be application/json."))
            return
        if len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) > MAX_INVOCATION_BODY:
            self._json(413, flowError("too-large", "The Invocation body is missing or too large."))
            return
        try:
            body = self.rfile.read(int(lengths[0]))
            self._bodyConsumed = True
            invocation = parseInvocation(body)
        except ProtocolError as err:
            self._json(400, flowError(err.code, str(err)))
            return
        except OSError:
            self.close_connection = True
            return
        status, reply = channel.submit(invocation)
        self._json(status, reply)

    def _stream(self, channel: FrontendChannel, query: dict[str, list[str]]) -> None:
        if not set(query) <= {"tracks", "after", "run"} or len(query.get("tracks", [])) != 1 or len(query.get("run", [])) > 1:
            self._json(400, {"error": "invalid-query"})
            return
        names = [name for name in query["tracks"][0].split(",") if name]
        if not names or len(names) > 64 or len(set(names)) != len(names) or not all(channel.hasTrack(name) for name in names):
            self._json(400, {"error": "unknown-track"})
            return
        cursor = channel.positions(names)
        if query.get("run", [None])[0] == channel.runId:
            for item in query.get("after", []):
                name, separator, position = item.rpartition(":")
                if not separator or name not in cursor or not position.isdigit():
                    self._json(400, {"error": "invalid-query"})
                    return
                cursor[name] = min(int(position), cursor[name])
        host = self.server.host
        if not host._enterView():
            self._json(429, {"error": "view-limit"})
            return
        try:
            self.close_connection = True
            self._headers(200, "text/event-stream; charset=utf-8", None)
            self._event("run", encode({"runId": channel.runId}))
            while not host._stopping.is_set():
                events = channel.waitEvents(cursor, STREAM_KEEPALIVE_SECONDS)
                if events is None:
                    break
                if not events:
                    self.wfile.write(b": ping\n\n")
                for name, data in events:
                    self._event(name, data)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            host._leaveView()

    def _event(self, name: str, data: bytes) -> None:
        self.wfile.write(b"event: " + name.encode("ascii") + b"\ndata: " + data + b"\n\n")
