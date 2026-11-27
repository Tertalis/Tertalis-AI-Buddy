# file: tests/first_party/frontendHost/test_host.py ; version: 3
"""Actual Pack activation and loopback HTTP: sealing, sources, authentication, invocations, streams."""
from __future__ import annotations

import hashlib
import json
import socket
from threading import Thread

import pytest


def connect(fh, runtime):
    """The loop owner's two calls: channel first (seals sources), then the launch URL."""
    channel = runtime.invokeCapability("frontend.host.channel@1")
    connection = runtime.invokeCapability("frontend.host.connection@1")
    return channel, fh.Client(connection)


def test_activation_serves_declared_browser_sources_by_path(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    channel, client = connect(fh, runtime)
    assert runtime.invokeCapability("frontend.host.channel@1") is channel
    status, headers, body = client.get("/pack/test.app/ui/main.mjs", auth=False)
    assert status == 200 and body == fh.MAIN_SOURCE.encode()
    assert headers["Content-Type"].startswith("text/javascript")
    assert headers["ETag"] == '"' + hashlib.sha256(fh.MAIN_SOURCE.encode()).hexdigest() + '"'
    assert headers["Cache-Control"] == "no-store" and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    for path in ("/pack/test.app/ui/panel.mjs", "/pack/frontend.kit/bootstrap.mjs", "/pack/frontend.kit/http.mjs"):
        assert client.get(path, auth=False)[0] == 200, path
    for path in ("/pack/test.app/manifest.json", "/pack/test.app/ui/missing.mjs", "/pack/frontend.host/codeEntry.py",
                 "/pack/test.app/../frontend.kit/http.mjs", "/pack/test.app/ui/main.mjs?x=1"):
        assert client.get(path, auth=False)[0] in (400, 404), path
    status, _headers, body = client.get("/", auth=False)
    assert status == 200 and b'src="/boot.mjs"' in body
    assert client.get("/boot.mjs", auth=False)[2].count(b"/pack/frontend.kit/bootstrap.mjs") == 1


def test_session_descriptor_names_main_and_tracks(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    channel, client = connect(fh, runtime)
    channel.declareTrack("status", "state", initial={"busy": False})
    channel.declareTrack("history", "log", maxItems=200)
    status, _headers, body = client.get("/session")
    assert status == 200
    assert json.loads(body) == {"runId": channel.runId, "title": "Test", "main": "/pack/test.app/ui/main.mjs",
                                "tracks": [{"name": "status", "style": "state"},
                                           {"name": "history", "style": "log", "maxItems": 200}]}
    assert json.loads(client.get("/snapshot?track=status")[2])["value"] == {"busy": False}
    assert client.get("/snapshot?track=nope")[0] == 404
    assert client.get("/snapshot?track=status&x=1")[0] == 400


def test_data_endpoints_require_the_credential_and_exact_host(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    _channel, client = connect(fh, runtime)
    for path in ("/session", "/snapshot?track=flow.invocations", "/stream?tracks=flow.invocations"):
        assert client.get(path, auth=False)[0] == 401, path
        assert client.get(path, auth=False, headers={"Authorization": "Bearer wrong"})[0] == 401
    assert client.get("/session", headers={"Host": "attacker.example:80"})[0] == 403
    assert client.get("/", auth=False, headers={"Host": "localhost:1"})[0] == 403
    assert client.get("/session", headers={"Origin": "http://127.0.0.1:1"})[0] == 403


def test_credentials_in_urls_are_rejected(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    _channel, client = connect(fh, runtime)
    for path in ("/?token=" + client.token, "/session?token=x", "/stream?tracks=flow.invocations&access_token=x"):
        status, _headers, body = client.get(path)
        assert status == 400 and client.token.encode() not in body


def test_sources_and_session_wait_for_the_loop_owner(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    client = fh.Client(runtime.invokeCapability("frontend.host.connection@1"))
    assert client.get("/pack/test.app/ui/main.mjs", auth=False)[0] == 404
    assert client.get("/session")[0] == 503
    status, reply = client.invoke({"invocationId": "i1", "target": "app.do", "arguments": {}})
    assert status == 503 and reply["code"] == "not-ready"


def test_main_selection_requires_configuration_when_ambiguous(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost, config={"frontendHost": {"main": {"packId": "test.app", "codeEntryId": "panel"}}})
    with pytest.raises(LookupError):
        runtime.invokeCapability("frontend.host.channel@1")
    with pytest.raises(ValueError):
        fh.createApplication(runtimeHost, config={"frontendHost": {"unknown": 1}})


def test_invocations_round_trip_through_the_loop_owner(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    channel, client = connect(fh, runtime)
    request = {"invocationId": "a-1", "target": "app.say", "arguments": {"text": "hi"}, "expect": {"pause": "p1"}}
    assert client.invoke(request) == (202, {"role": "accepted", "invocationId": "a-1"})
    assert client.invoke(request) == (202, {"role": "accepted", "invocationId": "a-1"})
    (item,) = channel.waitInvocations(1)
    assert (item.target, item.arguments, item.expect) == ("app.say", {"text": "hi"}, {"pause": "p1"})
    channel.fail("a-1", "conflict", "stale")
    assert client.invoke(request)[1]["code"] == "conflict"
    assert json.loads(client.get("/snapshot?track=flow.invocations")[2])["items"][-1]["invocationId"] == "a-1"


@pytest.mark.parametrize(
    ("body", "headers", "status"),
    [
        (b"{", {}, 400),
        (b'{"invocationId":"x","target":"t"}', {}, 400),
        (b'{"invocationId":"bad id","target":"t","arguments":{}}', {}, 400),
        (b'{"invocationId":"x","target":"t","arguments":[]}', {}, 400),
        (b'{"invocationId":"x","target":"t","arguments":{}}', {"Content-Type": "text/plain"}, 415),
        (b'{"invocationId":"x","target":"t","arguments":{}}', {"Origin": "http://evil.example"}, 403),
    ],
)
def test_malformed_invocations_are_rejected_at_intake(fh, runtimeHost, body, headers, status):
    runtime = fh.createApplication(runtimeHost)
    channel, client = connect(fh, runtime)
    sent = {"Content-Type": "application/json", "Origin": client.origin}
    sent.update(headers)
    connection, response = client.request("POST", "/invoke", body=body, headers=sent)
    assert response.status == status
    connection.close()
    assert channel.takeInvocations() == ()


def test_post_requires_same_origin_and_bounded_body(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    _channel, client = connect(fh, runtime)
    connection, response = client.request("POST", "/invoke", body=b"{}", headers={"Content-Type": "application/json"})
    assert response.status == 403
    connection.close()
    big = json.dumps({"invocationId": "x", "target": "t", "arguments": {"a": "x" * 70_000}}).encode()
    connection, response = client.request("POST", "/invoke", body=big, headers={"Content-Type": "application/json", "Origin": client.origin})
    assert response.status == 413
    connection.close()


def test_stream_starts_with_run_then_emits_and_resumes_from_positions(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    channel, client = connect(fh, runtime)
    channel.declareTrack("log", "log")
    channel.append("log", "before")
    connection, response, events = client.stream("tracks=log")
    assert response.status == 200 and response.getheader("Content-Type").startswith("text/event-stream")
    assert next(events) == ("run", {"runId": channel.runId})
    channel.append("log", "after")
    assert next(events) == ("emit", {"track": "log", "position": 2, "kind": "append", "value": "after"})
    connection.close()
    connection, _response, events = client.stream(f"tracks=log&run={channel.runId}&after=log:0")
    assert next(events)[0] == "run"
    assert [next(events)[1]["value"], next(events)[1]["value"]] == ["before", "after"]
    connection.close()
    connection, _response, events = client.stream("tracks=log&run=old-run&after=log:0")
    assert next(events)[0] == "run"
    channel.append("log", "third")
    assert next(events)[1]["position"] == 3, "positions from another run are ignored"
    connection.close()


def test_view_limit_rejects_extra_streams(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost, config={"frontendHost": {"maxViews": 2}})
    _channel, client = connect(fh, runtime)
    opened = [client.stream("tracks=flow.invocations") for _ in range(2)]
    for _connection, _response, events in opened:
        assert next(events)[0] == "run"
    assert client.get("/stream?tracks=flow.invocations")[0] == 429
    for connection, _response, _events in opened:
        connection.close()


def test_two_views_receive_the_same_emits(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    channel, client = connect(fh, runtime)
    channel.declareTrack("status", "state")
    views = [client.stream("tracks=status") for _ in range(2)]
    for _connection, _response, events in views:
        assert next(events)[0] == "run"
    channel.set("status", {"n": 1})
    for connection, _response, events in views:
        assert next(events)[1]["value"] == {"n": 1}
        connection.close()


def test_unload_closes_streams_and_endpoint(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    channel, client = connect(fh, runtime)
    host = fh.hostState(runtime)
    connection, _response, events = client.stream("tracks=flow.invocations")
    assert next(events)[0] == "run"
    finished = []
    reader = Thread(target=lambda: finished.append(list(events)))
    reader.start()
    runtimeHost.closeApplicationRun(runtime.applicationRun.applicationRunId)
    reader.join(5)
    assert finished == [[]]
    assert channel.closed and host._credential == ""
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", int(client.authority.split(":")[1])), timeout=1).close()
    connection.close()


def test_connection_url_carries_credential_only_in_the_fragment(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    connection = runtime.invokeCapability("frontend.host.connection@1")
    origin, fragment = connection["url"].split("/#token=")
    assert origin == connection["origin"] and origin.startswith("http://127.0.0.1:")
    assert len(fragment) >= 43 and "?" not in connection["url"]



def test_large_log_snapshot_fits_the_browser_body_limit(fh, runtimeHost):
    runtime = fh.createApplication(runtimeHost)
    channel, client = connect(fh, runtime)
    channel.declareTrack("log", "log")
    value = "x" * (fh.protocol.MAX_VALUE_BYTES - 2)
    for _ in range(9):
        channel.append("log", value)
    status, _headers, body = client.get("/snapshot?track=log")
    assert status == 200 and len(body) < 2 * 1024 * 1024
    snapshot = json.loads(body)
    assert snapshot["position"] == 9 and snapshot["items"][-1] == value
    assert 0 < len(snapshot["items"]) < 9
    channel.reset("log")
    channel.append("log", "fresh")
    assert channel.snapshot("log")["items"] == ["fresh"]
