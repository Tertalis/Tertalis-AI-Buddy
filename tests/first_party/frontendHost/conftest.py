# file: tests/first_party/frontendHost/conftest.py ; version: 2
"""Real RuntimeHost activation of frontend.kit + frontend.host with a fixture AppPack declaring a browser main."""
from __future__ import annotations

import importlib.util
import json
import sys
from http.client import HTTPConnection
from pathlib import Path

from types import SimpleNamespace

import pytest

from backend.packs.runtime import ManualActivationPlan, PackResolver
from backend.runtime import RuntimeHost
from backend.save import ApplicationStore

REPO_ROOT = Path(__file__).resolve().parents[3]
HOST_ROOT = REPO_ROOT / "first-party" / "frontendHost"
KIT_ROOT = REPO_ROOT / "first-party" / "frontendKit"
_spec = importlib.util.spec_from_file_location("frontendHostTestEntry", HOST_ROOT / "codeEntry.py")
_entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_entry)
hostModule = _entry._implementation
channelModule = sys.modules[hostModule.__package__ + ".channel"]
protocolModule = sys.modules[hostModule.__package__ + ".protocol"]

MAIN_SOURCE = "export function mount(root, session) { root.dataset.mounted = session.runId; }\n"


def writeAppPack(root: Path) -> Path:
    """Fixture AppPack with a python main and a browser main plus one browser library."""
    app = root / "app"
    (app / "ui").mkdir(parents=True)
    (app / "manifest.json").write_text(json.dumps({
        "packId": "test.app", "kind": "appPack", "version": "1.0.0",
        "codeEntries": [
            {"id": "ui", "source": "ui/main.mjs", "format": "js-module-source@1"},
            {"id": "panel", "source": "ui/panel.mjs", "format": "js-module-source@1", "role": "library"},
        ],
    }), encoding="utf-8")
    # newline="\n": served bytes are compared exactly (text mode would write CRLF on Windows).
    (app / "ui" / "main.mjs").write_text(MAIN_SOURCE, encoding="utf-8", newline="\n")
    (app / "ui" / "panel.mjs").write_text("export const panel = 1;\n", encoding="utf-8", newline="\n")
    return app


@pytest.fixture
def runtimeHost(tmp_path):
    """A started RuntimeHost resolving the fixture AppPack and both first-party frontend Packs."""
    app = writeAppPack(tmp_path)
    host = RuntimeHost(applicationStore=ApplicationStore(tmp_path / "saves"), packResolver=PackResolver(roots=(app, KIT_ROOT, HOST_ROOT)))
    host.start()
    yield host
    host.stop()


def createApplication(runtimeHost, *, config=None, packs=("test.app", "frontend.kit", "frontend.host")):
    """Creates the Application with an explicit manual plan."""
    return runtimeHost.createApplication(appPackId="test.app", plan=ManualActivationPlan(packIds=packs),
                                         config={"frontendHost": {"title": "Test"}} if config is None else config)


def hostState(runtime):
    """Test-only inspection of the frontend host Pack state."""
    return next(pack.entries[0].state for pack in runtime.packLoader._loadedPacks if pack.pack.packId == "frontend.host")


class Client:
    """Minimal HTTP client that sends only what each test states."""

    def __init__(self, connection: dict) -> None:
        self.origin = connection["origin"]
        self.authority = self.origin[len("http://"):]
        self.token = connection["url"].split("#token=", 1)[1]

    def request(self, method: str, path: str, *, body: bytes | None = None, headers: dict | None = None, auth: bool = True, timeout: float = 3):
        host, port = self.authority.split(":")
        connection = HTTPConnection(host, int(port), timeout=timeout)
        sent = {"Host": self.authority}
        if auth:
            sent["Authorization"] = "Bearer " + self.token
        sent.update(headers or {})
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in sent.items():
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return connection, response

    def get(self, path: str, **kwargs):
        connection, response = self.request("GET", path, **kwargs)
        try:
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def invoke(self, value: dict, **kwargs):
        headers = {"Content-Type": "application/json", "Origin": self.origin}
        headers.update(kwargs.pop("headers", {}))
        connection, response = self.request("POST", "/invoke", body=json.dumps(value).encode(), headers=headers, **kwargs)
        try:
            return response.status, json.loads(response.read() or b"null")
        finally:
            connection.close()

    def stream(self, query: str):
        """Opens a stream and returns (connection, response, events iterator)."""
        connection, response = self.request("GET", "/stream?" + query, headers={"Accept": "text/event-stream"}, timeout=5)

        def events():
            name, data = None, None
            while True:
                line = response.fp.readline()
                if not line:
                    return
                line = line.rstrip(b"\r\n")
                if line.startswith(b"event: "):
                    name = line[7:].decode()
                elif line.startswith(b"data: "):
                    data = json.loads(line[6:])
                elif line == b"" and name is not None:
                    yield name, data
                    name, data = None, None
        return connection, response, events()


@pytest.fixture
def fh():
    """Shared modules and helpers (tests run with --import-mode=importlib, so no relative imports)."""
    return SimpleNamespace(
        host=hostModule, channel=channelModule, protocol=protocolModule,
        FrontendChannel=channelModule.FrontendChannel, ChannelClosed=channelModule.ChannelClosed,
        Invocation=protocolModule.Invocation, INVOCATIONS=protocolModule.INVOCATIONS_TRACK,
        Client=Client, createApplication=createApplication, hostState=hostState, MAIN_SOURCE=MAIN_SOURCE,
    )
