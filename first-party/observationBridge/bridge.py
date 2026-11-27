# file: first-party/observationBridge/bridge.py ; version: 4
"""Bounded selected-publication registry with a replaceable loopback HTTP adapter."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, RLock, Thread
from uuid import uuid4

from backend.observation import ObservationGrantRevoked, ObservationReadGrant
from backend.transport.loopbackHttp import (
    LoopbackHttpServer,
    QuietHandler,
    bearerMatches,
    exactHost,
    newCredential,
    singleOrigin,
    validateLocalOrigins,
)

from .schema import MAX_SAMPLES, MAX_WINDOWS, SCHEMA_ID, serialize

MAX_PUBLICATIONS = 8


def validateOrigins(origins: object) -> tuple[str, ...]:
    """Accepts only explicitly named local HTTP viewer origins with concrete ports."""
    return validateLocalOrigins(origins)


@dataclass(slots=True)
class _Publication:
    """One read claim and latest bounded response, never an accumulating history."""

    grant: ObservationReadGrant
    revision: int = 0
    fingerprint: bytes | None = None
    response: bytes | None = None


class ObservationBridge:
    """Pack-owned publication access and endpoint; never owns source sessions/collectors.

    Publication lock precedes grant/service locks. Observation revocation never
    calls back into this object. Withdrawal waits for in-flight response writers;
    source revocation prevents later grant reads, while previously copied responses
    may finish. No runtime/context callback is retained by the endpoint thread.
    """

    def __init__(self, *, port: int = 0, host: str = "127.0.0.1", allowedOrigins: object = ()) -> None:
        """Starts one explicit local endpoint, self-cleaning partial socket/thread startup."""
        if host != "127.0.0.1" or type(host) is not str:
            raise ValueError("Bridge binding must be 127.0.0.1.")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535.")
        self._origins = validateOrigins(allowedOrigins)
        self._lock = RLock()
        self._closeLock = RLock()
        self._stopping = Event()
        self._closed = False
        self._publications: dict[str, _Publication] = {}
        self._credential = newCredential()
        self._server = None
        self._thread = None
        try:
            self._server = _Server((host, port), self)
            self._authority = f"127.0.0.1:{self._server.server_port}"
            self._thread = Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="tertalis-observation-bridge", daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def connection(self) -> dict:
        """Returns explicit connection credentials to the same-app capability caller only."""
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("Observation bridge is closed.")
            return {"endpoint": f"http://{self._authority}", "readCredential": self._credential,
                    "authorization": "Bearer", "schemaId": SCHEMA_ID, "allowedOrigins": list(self._origins)}

    def publish(self, grant: ObservationReadGrant) -> dict:
        """Takes ownership of a separately mediated read claim; failure revokes only that claim."""
        if type(grant) is not ObservationReadGrant:
            raise TypeError("A mediated read grant is required.")
        try:
            with self._lock:
                if self._stopping.is_set():
                    raise RuntimeError("Observation bridge is closed.")
                if len(self._publications) >= MAX_PUBLICATIONS:
                    raise ValueError("Publication capacity reached.")
                evidence = grant.snapshot()
                if evidence.request.historySamples > MAX_SAMPLES or evidence.request.historyWindows > MAX_WINDOWS:
                    raise ValueError("Source retention exceeds bridge limits.")
                publicationId = uuid4().hex
                publication = _Publication(grant)
                self._refresh(publicationId, publication, evidence)
                self._publications[publicationId] = publication
                return {"publicationId": publicationId, "snapshotUrl": f"http://{self._authority}/publications/{publicationId}",
                        "schemaId": SCHEMA_ID, "sessionId": evidence.sessionId, "clockEpochId": evidence.clockEpochId}
        except BaseException:
            grant.revoke()
            raise

    @staticmethod
    def _refresh(publicationId: str, publication: _Publication, evidence) -> None:
        """Changes revision only for changed allowlisted evidence; caches at most one response."""
        fingerprint = hashlib.sha256(serialize(evidence, publicationId, 0)).digest()
        if fingerprint != publication.fingerprint:
            response = serialize(evidence, publicationId, publication.revision + 1)
            publication.revision += 1
            publication.response = response
            publication.fingerprint = fingerprint

    def withdraw(self, publicationId: str) -> bool:
        """Stops future serving and clears cached evidence, waiting for prior sends to finish."""
        with self._lock:
            publication = self._publications.pop(publicationId, None)
            if publication is None:
                return False
            publication.response = publication.fingerprint = None
            publication.grant.revoke()
            return True

    def serve(self, publicationId: str, writer: Callable[[int, bytes], None]) -> None:
        """Revalidates every read and serializes response delivery against withdrawal/close."""
        with self._lock:
            if self._stopping.is_set():
                writer(503, b'{"error":"bridge-closed"}')
                return
            publication = self._publications.get(publicationId)
            if publication is None:
                writer(404, b'{"error":"unknown-publication"}')
                return
            try:
                evidence = publication.grant.snapshot()
                self._refresh(publicationId, publication, evidence)
            except ObservationGrantRevoked:
                publication.response = publication.fingerprint = None
                publication.grant.revoke()
                writer(410, b'{"error":"publication-revoked"}')
                return
            except Exception:
                publication.response = publication.fingerprint = None
                writer(503, b'{"error":"observation-unavailable"}')
                return
            writer(200, publication.response)

    def close(self) -> None:
        """Rejects new requests, withdraws owned readers and closes/joins endpoint resources."""
        with self._closeLock:
            self._stopping.set()
            if self._server is not None:
                self._server.interruptClients()
            with self._lock:
                if self._closed:
                    return
                for publicationId in tuple(self._publications):
                    self.withdraw(publicationId)
            try:
                if self._thread is not None and self._thread.is_alive():
                    self._server.shutdown()
                    self._thread.join(5)
                    if self._thread.is_alive():
                        raise RuntimeError("Observation endpoint did not terminate.")
            finally:
                try:
                    if self._server is not None:
                        self._server.server_close()
                finally:
                    self._credential = ""
                    self._closed = True


class _Server(LoopbackHttpServer):
    """Single request worker, finite connection timeout/backlog; no reader thread fanout."""

    def __init__(self, address, bridge: ObservationBridge) -> None:
        """Binds loopback; the server retains Pack state only, never invocation context."""
        self.bridge = bridge
        host, port = address
        super().__init__(port, _Handler, isStopping=bridge._stopping.is_set, host=host)


class _Handler(QuietHandler):
    """Strict read-only HTTP adapter with exact Host, Origin and Bearer checks."""

    server_version = "TertalisObservationBridge"

    def send_error(self, code, message=None, explain=None) -> None:
        """Keeps parser/unsupported-method errors fixed, non-reflective JSON with no-store headers."""
        self._reply(int(code), b'{"error":"http-request-rejected"}')

    def _origin(self) -> str | None:
        """Returns a validated explicit Origin, with absence allowed for authenticated local tools."""
        return singleOrigin(self.headers, self.server.bridge._origins)
    def _reply(self, status: int, body: bytes, origin: str | None = None, *, preflight: bool = False) -> None:
        """Writes only fixed protocol headers and a bounded allowlisted body; never reflects requests."""
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.send_header("Vary", "Origin")
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _validate(self, *, preflight: bool = False) -> tuple[str, str | None] | None:
        """Rejects ambiguous headers, URL credentials, ambient discovery and unauthorized reads."""
        bridge = self.server.bridge
        if not exactHost(self.headers, bridge._authority):
            self._reply(403, b'{"error":"host-rejected"}')
            return None
        try:
            origin = self._origin()
        except PermissionError:
            self._reply(403, b'{"error":"origin-rejected"}')
            return None
        match = re.fullmatch(r"/publications/([0-9a-f]{32})", self.path)
        if match is None:
            self._reply(400, b'{"error":"invalid-target"}', origin)
            return None
        if preflight:
            requested = self.headers.get_all("Access-Control-Request-Headers", [])
            if (origin is None or self.headers.get_all("Access-Control-Request-Method", []) != ["GET"]
                    or len(requested) != 1 or requested[0].strip().lower() != "authorization"):
                self._reply(403, b'{"error":"preflight-rejected"}')
                return None
        else:
            if not bearerMatches(self.headers, bridge._credential):
                self._reply(401, b'{"error":"read-credential-required"}', origin)
                return None
        return match[1], origin

    def do_GET(self) -> None:
        """Serves only a credential-authorized, explicitly selected publication."""
        validated = self._validate()
        if validated is not None:
            publicationId, origin = validated
            self.server.bridge.serve(publicationId, lambda status, body: self._reply(status, body, origin))

    def do_OPTIONS(self) -> None:
        """Answers an explicitly allowed browser preflight with no observation data."""
        validated = self._validate(preflight=True)
        if validated is not None:
            self._reply(204, b"", validated[1], preflight=True)
