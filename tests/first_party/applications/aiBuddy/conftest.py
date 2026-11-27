# file: tests/first_party/applications/aiBuddy/conftest.py ; version: 7
"""Shared AI Buddy test harness: RuntimeHost, AI Buddy and a scripted stub provider."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.packs.runtime import ManualActivationPlan, PackResolver
from backend.runtime import RuntimeHost
from backend.save import ApplicationStore

REPO_ROOT = Path(__file__).resolve().parents[4]
APP_ROOT = REPO_ROOT / "first-party" / "applications" / "aiBuddy"
PLAN = ManualActivationPlan(packIds=("test.stubProvider", "aiBuddy"))
FRONTEND_PLAN = ManualActivationPlan(packIds=("test.stubProvider", "aiBuddy", "frontend.kit", "frontend.host"))
FRONTEND_ROOTS = (REPO_ROOT / "first-party" / "frontendKit", REPO_ROOT / "first-party" / "frontendHost")
DRIVER_ROOT = REPO_ROOT / "first-party" / "llmDrivers" / "llamaCpp"

_STUB_PROVIDER = '''
import json, time
from backend.llm.llmTypes import LlmExecutionProfile, LlmStreamEvent

class StubProvider:
    def __init__(self, config):
        self.responses = list(config.get("responses", []))
        self.log = config["log"]

    def getExecutionProfile(self, *, model, providerOptions):
        return LlmExecutionProfile(contextWindowTokens=8192)

    def stream(self, request):
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"formatId": request.query.formatId, "prompt": request.query.payload}) + "\\n")
        item = self.responses.pop(0) if self.responses else "<say>default</say>"
        if isinstance(item, dict) and item.get("fail"):
            raise LookupError(item["fail"])
        if isinstance(item, dict) and item.get("block"):
            yield LlmStreamEvent(eventType="delta", text="partial thought")
            deadline = time.monotonic() + 5
            while not request.cancellationSignal.requested and time.monotonic() < deadline:
                time.sleep(0.01)
            raise OSError("stub transport stopped")
        for piece in (item[: len(item) // 2], item[len(item) // 2:]):
            if piece:
                yield LlmStreamEvent(eventType="delta", text=piece)
        yield LlmStreamEvent(eventType="completed")

def onLoad(ctx):
    ctx.llm.registerProvider("stub", StubProvider(ctx.config["stubProvider"]))
'''


def _loadDriverModule():
    """Imports host/buddyLoop.py the way run.py does."""
    path = APP_ROOT / "host" / "buddyLoop.py"
    spec = importlib.util.spec_from_file_location("aiBuddyHostLoop", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driverModule = _loadDriverModule()


def _loadBridgeModule():
    """Imports host/frontendBridge.py the way run.py does."""
    path = APP_ROOT / "host" / "frontendBridge.py"
    spec = importlib.util.spec_from_file_location("aiBuddyFrontendBridge", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridgeModule = _loadBridgeModule()


def _loadManagedModule():
    """Imports host/llamaManaged.py the way run.py does."""
    path = APP_ROOT / "host" / "llamaManaged.py"
    spec = importlib.util.spec_from_file_location("aiBuddyLlamaManaged", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


managedModule = _loadManagedModule()


def _loadPackModule():
    """Imports the AppPack CodeEntry for its pure functions (e.g. the game engine)."""
    path = APP_ROOT / "pack" / "codeEntry.py"
    spec = importlib.util.spec_from_file_location("aiBuddyPackForTests", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


packModule = _loadPackModule()


class Harness:
    """One RuntimeHost with AI Buddy and a scripted stub provider."""

    def __init__(self, tmp_path: Path, responses, *, applicationId: str | None = None,
                 memory: dict | None = None, settings: dict | None = None,
                 interruptPolicy: str = "finish", frontend: bool = False, llama: dict | None = None) -> None:
        """llama: a managed llama.cpp catalog; the platform-managed server replaces the stub provider
        (process and HTTP must then be faked by the test, see test_checkpointH.py)."""
        stubRoot = tmp_path / "stub"
        packDir = stubRoot / "stubProvider"
        if not packDir.exists():
            packDir.mkdir(parents=True)
            (packDir / "manifest.json").write_text(json.dumps({
                "packId": "test.stubProvider", "kind": "modPack", "version": "0.0.0",
                "codeEntries": [{"id": "provider", "source": "codeEntry.py"}]}), encoding="utf-8")
            (packDir / "codeEntry.py").write_text(_STUB_PROVIDER, encoding="utf-8", newline="\n")
        self.log = tmp_path / "provider-log.jsonl"
        self.store = ApplicationStore(tmp_path / "saves")
        self.managed = None if llama is None else managedModule.ManagedLlama(llama)
        self.host = RuntimeHost(applicationStore=self.store,
                                packResolver=PackResolver(roots=(stubRoot, APP_ROOT / "pack", *FRONTEND_ROOTS, DRIVER_ROOT)),
                                **({} if self.managed is None else {"providerComposers": (self.managed.composer,)}))
        self.host.start()
        self.closed = False
        aiBuddy = {"providerName": "stub" if llama is None else "llama.cpp"}
        if memory is not None:
            aiBuddy["memory"] = memory
        aiBuddy.update(settings or {})
        self.now = datetime(2026, 11, 24, 21, 0, 0, tzinfo=timezone(timedelta(hours=1)))
        config = {"aiBuddy": aiBuddy,
                  "stubProvider": {"responses": list(responses), "log": str(self.log)}}
        plan = FRONTEND_PLAN if frontend else PLAN
        if llama is not None:
            plan = ManualActivationPlan(packIds=("llmDrivers.llamaCpp", *plan.packIds[1:]))
            config["llamaCpp"] = llama
        if frontend:
            config["frontendHost"] = {"title": "AI Buddy", "main": {"packId": "aiBuddy", "codeEntryId": "ui"}}
        if applicationId is None:
            self.runtime = self.host.createApplication(appPackId="aiBuddy", plan=plan, config=config)
        else:
            self.runtime = self.host.loadApplication(appPackId="aiBuddy", applicationId=applicationId,
                                                     plan=plan, config=config)
        self.spoken: list[str] = []
        self.diagnostics: list[str] = []
        self.pauses: list[dict] = []
        self.outputPauses: list[dict] = []
        self.finished: list[dict] = []
        events = driverModule.DriverEvents(
            speak=self.spoken.append, diagnostic=self.diagnostics.append,
            paused=self._paused, inferenceFinished=self.finished.append)
        self.driver = driverModule.BuddyLoop(self.runtime, events=events, clock=self.clock,
                                               interruptPolicy=interruptPolicy)
        self.channel = self.bridge = None
        if frontend:
            self.channel = self.runtime.invokeCapability("frontend.host.channel@1")
            self.bridge = bridgeModule.FrontendBridge(self.driver, self.channel)
            self.driver.events = self.bridge.wrap(self.driver.events)

    def _paused(self, info: dict) -> None:
        """Records breakpoint pauses: prompt phase in pauses, response phase in outputPauses."""
        (self.outputPauses if info.get("phase") == "output" else self.pauses).append(info)

    def continueRun(self) -> None:
        """Continues and settles, passing through response pauses, up to the next prompt pause or idle."""
        self.driver.continueExecution()
        self.settle()
        while (self.driver.debug.pausedInference or {}).get("phase") == "output":
            self.driver.continueExecution()
            self.settle()

    def clock(self) -> str:
        """Test-controlled wall clock; advance() moves it."""
        return self.now.isoformat(timespec="seconds")

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    @property
    def state(self):
        return self.runtime.applicationRun.application.committedState

    def settle(self, limit: int = 20) -> None:
        """Runs the driver until it reports idle or paused."""
        for _ in range(limit):
            if not self.driver.runOnce():
                return
        raise AssertionError("driver did not settle")

    def providerPrompts(self) -> list[str]:
        if not self.log.exists():
            return []
        return [json.loads(line)["prompt"] for line in self.log.read_text(encoding="utf-8").splitlines()]

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.host.stop()

    def run(self, *messages: str) -> None:
        """Submits messages one at a time, settling after each."""
        for message in messages:
            self.driver.submitInput(message)
            self.settle()

    def developer(self, capabilityId: str, payload=None):
        """Runs a developer intervention through the driver and returns its result."""
        future = self.driver.developerJob(capabilityId, payload)
        self.settle()
        return future.result(timeout=5)

    def work(self, workId: str) -> dict:
        return self.read("aiBuddy.debug.record.describe@1", {"id": workId})

    def read(self, capabilityId: str, payload=None):
        """Reads through a capability directly (tests only; no driver thread running)."""
        return self.runtime.invokeCapability(capabilityId, payload)




@pytest.fixture
def makeHarness(tmp_path: Path):
    """Returns a factory for harnesses sharing one tmp_path (and save root)."""
    created: list[Harness] = []

    def factory(responses, **kwargs) -> Harness:
        harness = Harness(tmp_path, responses, **kwargs)
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        harness.close()


@pytest.fixture
def engine():
    """The AppPack module, for deterministic Minesweeper computations in tests."""
    return packModule
