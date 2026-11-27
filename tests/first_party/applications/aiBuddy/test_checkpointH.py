# file: tests/first_party/applications/aiBuddy/test_checkpointH.py ; version: 1
"""Checkpoint H: platform-managed llama.cpp (ER-03 host composition) with model selection.

The actual llama.cpp Pack, execution adapter and ER-01/02 resources run; only
process creation, readiness and HTTP are faked, as in the platform's own
test_llamaCppExecutionResources.py.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[4] / "first-party" / "applications" / "aiBuddy"


def _managed():
    """host/llamaManaged.py as loaded by conftest (tests run with --import-mode=importlib)."""
    return sys.modules["aiBuddyLlamaManaged"]

MESSAGE_STREAM_ID = "chat-1"


class _Process:
    """Fake owned llama-server child; records its arguments and stop."""

    def __init__(self, args, **kwargs):
        self.args, self.returncode, self.terminated = list(args), None, False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, **kwargs):
        self.returncode = 0
        return 0

    def kill(self):
        self.terminated = True


class _Response:
    def __init__(self, data):
        self.data, self.closed = data, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def read(self, limit=-1):
        return self.data if limit < 0 else self.data[:limit]

    def read1(self, limit):
        result, self.data = self.data[:limit], self.data[limit:]
        return result

    def __iter__(self):
        return iter(self.data)


def _stream(text, *, complete=True):
    """A native-terminal llama.cpp SSE stream; complete=False omits the proof of completion."""
    def line(value):
        return ("data: " + json.dumps(value) + "\n").encode()
    lines = [line({"id": MESSAGE_STREAM_ID, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})]
    if complete:
        lines += [line({"id": MESSAGE_STREAM_ID, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                  line({"id": MESSAGE_STREAM_ID, "choices": [], "usage": {"completion_tokens": 1}}), b"data: [DONE]\n"]
    return lines


@pytest.fixture
def fakeLlama(tmp_path, monkeypatch):
    """Fakes llama-server process, readiness and HTTP below the real adapter; returns (config, server)."""
    adapter = _managed().loadAdapter()
    executable = tmp_path / "llama-server.exe"
    executable.touch()
    paths = {name: tmp_path / f"{name}.gguf" for name in ("A", "B")}
    for path in paths.values():
        path.touch()
    config = {"manageServer": True, "executable": str(executable), "defaultModel": "A",
              "extraArgs": ["--slots"], "models": {
                  "A": {"modelPath": str(paths["A"]), "contextWindowTokens": 4096},
                  "B": {"modelPath": str(paths["B"]), "contextWindowTokens": 8192}}}
    server = {"processes": [], "replies": [], "complete": True, "requests": [], "paths": paths}

    def popen(args, **kwargs):
        process = _Process(args, **kwargs)
        server["processes"].append(process)
        return process

    def urlopen(request, **kwargs):
        url = request.full_url
        server["requests"].append(url)
        if url.endswith("/slots"):
            return _Response(json.dumps([{"id": 0, "is_processing": False}]).encode())
        if url.endswith("/apply-template"):
            return _Response(b'{"prompt":"rendered"}')
        if url.endswith("/tokenize"):
            return _Response(b'{"tokens":[1,2,3]}')
        text = server["replies"].pop(0) if server["replies"] else "<say>default</say>"
        return _Response(_stream(text, complete=server["complete"]))

    class Transport:
        def __init__(self):
            self.mayHaveSent, self.response, self.stopped = False, None, False

        def open(self, request, **kwargs):
            if self.stopped:
                raise InterruptedError("Stopped before fake send.")
            self.mayHaveSent = True
            self.response = adapter.http.urlopen(request, **kwargs)
            return self.response

        def stop(self):
            self.stopped = True
            if self.response is not None:
                self.response.close()

        def close(self):
            if self.response is not None:
                self.response.close()

        def setReadTimeout(self, seconds):
            self.readTimeout = seconds

    monkeypatch.setattr(adapter._impl.subprocess, "Popen", popen)
    monkeypatch.setattr(adapter._impl.LlamaCppDriver, "_waitReady", lambda *args: None)
    monkeypatch.setattr(adapter.http, "urlopen", urlopen)
    monkeypatch.setattr(adapter._transport, "OwnedHttpTransport", Transport)
    return config, server


def _modelOf(process, server):
    """Which configured model file a fake server process was started with."""
    return next(name for name, path in server["paths"].items() if str(path) in process.args)


def _stopHost(harness):
    """Unknown-state tests keep fail-closed tombstones; host stop may then report them."""
    try:
        harness.close()
    except Exception:  # noqa: BLE001
        harness.closed = True


def test_managed_inference_starts_the_server_lazily_with_the_default_model(makeHarness, fakeLlama):
    config, server = fakeLlama
    server["replies"].append("<say>Hello from the managed server</say>")
    h = makeHarness([], llama=config)
    assert server["processes"] == [], "loading is lazy"
    info = h.read("aiBuddy.debug.llm.describe@1")
    assert info == {"mode": "managed", "selected": "A", "default": "A",
                    "models": [{"name": "A", "contextWindowTokens": 4096}, {"name": "B", "contextWindowTokens": 8192}]}
    assert not any("modelPath" in json.dumps(model) for model in info["models"]), "no paths leave the host"
    h.run("Hi")
    assert h.spoken == ["Hello from the managed server"]
    (process,) = server["processes"]
    assert _modelOf(process, server) == "A"
    record = h.finished[-1]
    assert record["model"] == "A" and record["state"] == "completed"
    assert h.read("aiBuddy.debug.status@1")["llm"] == {"mode": "managed", "model": "A"}
    h.run("Again")
    assert len(server["processes"]) == 1, "the loaded model is reused"
    _stopHost(h)


def test_selecting_another_model_switches_the_server_without_restarting_ai_buddy(makeHarness, fakeLlama):
    config, server = fakeLlama
    server["replies"] += ["<say>from A</say>", "<say>from B</say>"]
    h = makeHarness([], llama=config)
    h.run("first")
    result = h.developer("aiBuddy.debug.llm.select@1", {"model": "B"})
    assert result == {"model": "B", "previous": "A", "loaded": True}
    first, second = server["processes"]
    assert first.terminated and _modelOf(second, server) == "B"
    h.run("second")
    assert h.finished[-1]["model"] == "B" and h.spoken[-1] == "from B"
    assert h.read("aiBuddy.debug.llm.describe@1")["selected"] == "B"
    unchanged = h.developer("aiBuddy.debug.llm.select@1", {"model": "B"})
    assert unchanged["loaded"] is True and len(server["processes"]) == 2, "selecting the loaded model is a no-op"
    _stopHost(h)


def test_selection_is_kept_in_state_across_restarts(makeHarness, fakeLlama):
    config, server = fakeLlama
    first = makeHarness([], llama=config)
    first.developer("aiBuddy.debug.llm.select@1", {"model": "B", "load": False})
    assert server["processes"] == [], "load=false only records the choice"
    applicationId = first.runtime.applicationRun.application.applicationId
    _stopHost(first)
    second = makeHarness([], llama=config, applicationId=applicationId)
    assert second.read("aiBuddy.debug.llm.describe@1")["selected"] == "B"
    second.run("hello")
    assert _modelOf(server["processes"][-1], server) == "B"
    _stopHost(second)


def test_unknown_or_invalid_selection_fails_without_stopping_the_loop(makeHarness, fakeLlama):
    config, server = fakeLlama
    h = makeHarness([], llama=config)
    with pytest.raises(ValueError, match="Unknown model"):
        h.developer("aiBuddy.debug.llm.select@1", {"model": "C"})
    with pytest.raises(ValueError, match="load must be"):
        h.developer("aiBuddy.debug.llm.select@1", {"model": "B", "load": "yes"})
    assert not h.driver.debug.paused
    _stopHost(h)


def test_unknown_server_state_pauses_the_loop_and_keeps_the_inference(makeHarness, fakeLlama):
    """Known limitation: without proof of completion the platform refuses further work until restart."""
    config, server = fakeLlama
    server["complete"] = False
    h = makeHarness([], llama=config)
    h.run("first")
    assert h.finished[-1]["state"] == "failed", "the unproven request itself is a provider failure"
    server["complete"] = True
    h.run("second")
    assert h.driver.debug.paused
    assert any("cannot provide 'A'" in text for text in h.diagnostics)
    prepared = [i for i in range(1, 10)
                if (h.read("aiBuddy.debug.record.describe@1", {"id": f"i{i:06d}"}) or {}).get("state") == "prepared"]
    assert prepared, "the inference stays prepared for after a restart"
    _stopHost(h)


def test_external_mode_has_nothing_to_select(makeHarness):
    h = makeHarness(["<say>external</say>"])
    assert h.read("aiBuddy.debug.llm.describe@1") == {"mode": "external", "models": [], "selected": None, "default": None}
    with pytest.raises(ValueError, match="managed llama.cpp"):
        h.developer("aiBuddy.debug.llm.select@1", {"model": "A"})
    h.run("hi")
    assert h.spoken == ["external"] and h.read("aiBuddy.debug.status@1")["llm"] == {"mode": "external", "model": None}


def test_the_example_file_parses_and_only_llamacpp_is_accepted(tmp_path):
    pytest.importorskip("json5")
    managedModule = _managed()
    example = managedModule.readConfig(managedModule.EXAMPLE_CONFIG)
    assert example["manageServer"] is True and set(example["models"]) == {"main", "small"}
    assert "--slots" in example["extraArgs"] and example["defaultModel"] in example["models"]
    bad = tmp_path / "bad.json5"
    for text, message in (("{llamaCpp: {manageServer: false, models: {a: {}}}}", "manageServer"),
                          ("{llamaCpp: {manageServer: true, models: {}}}", "at least one model"),
                          ("{llamaCpp: {manageServer: true, models: {a: {}}}, other: 1}", "exactly one")):
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            managedModule.readConfig(bad)


def test_the_local_file_is_ignored_by_git():
    assert "llama.local.json5" in (APP_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def _launcher():
    spec = importlib.util.spec_from_file_location("aiBuddyManagedLauncherTest", APP_ROOT / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_rejects_unknown_model_before_creating_an_application(fakeLlama, tmp_path, capsys):
    config, server = fakeLlama
    path = tmp_path / "llama.json5"
    path.write_text(json.dumps({"llamaCpp": config}), encoding="utf-8")
    launcher = _launcher()
    saves = tmp_path / "launcher-saves"
    result = launcher.main(["--no-ui", "--llama-config", str(path), "--model", "missing", "--saves", str(saves)])
    assert result == 2
    assert "Unknown model 'missing'" in capsys.readouterr().out
    assert list(saves.iterdir()) == [] and server["processes"] == []


def test_launcher_records_requested_model_without_loading_it(fakeLlama, tmp_path, monkeypatch, capsys):
    config, server = fakeLlama
    path = tmp_path / "llama.json5"
    path.write_text(json.dumps({"llamaCpp": config}), encoding="utf-8")
    launcher = _launcher()
    commands = iter(["/model", "/quit"])
    monkeypatch.setattr("builtins.input", lambda: next(commands))
    result = launcher.main(["--no-ui", "--new", "--llama-config", str(path), "--model", "B",
                            "--saves", str(tmp_path / "launcher-saves")])
    assert result == 0
    assert "* B (8192 tokens)" in capsys.readouterr().out
    assert server["processes"] == [], "startup selection should stay lazy"
