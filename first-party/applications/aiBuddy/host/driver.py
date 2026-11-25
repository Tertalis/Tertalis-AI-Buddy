# file: first-party/applications/aiBuddy/host/driver.py ; version: 10
"""Host-side AI Buddy driver (materialization Checkpoint A).

Tertalis currently has no long-lived application scheduler: lifecycle hook
Contexts end when the hook returns, and Jobs are run by a caller. This
driver is the application-owned caller. It keeps no AI Buddy semantics of
its own: every decision and state change happens inside AppPack
capabilities executed as Jobs, so state is transactional and persisted.

Threading: exactly one thread (the one calling runOnce/run) touches the
ApplicationRuntime. Other threads (a terminal, tests) only enqueue commands
or request cancellation of the current Job, which Job supports from any
thread.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.orchestration import Job, JobState

__all__ = ["BuddyDriver", "DriverEvents"]


def _localNow() -> str:
    """Returns local wall-clock time as an ISO-8601 string."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class DriverEvents:
    """Output callbacks. speak receives deliberate communication only."""

    speak: callable = lambda text: None
    diagnostic: callable = lambda text: None
    paused: callable = lambda inference: None
    inferenceFinished: callable = lambda record: None


@dataclass
class _DebugState:
    breakBeforeInference: bool = False
    paused: bool = False
    pausedInference: dict | None = None
    continueRequested: bool = False


class BuddyDriver:
    """Runs AI Buddy capabilities as Jobs and exposes debugger controls."""

    INTERRUPT_POLICIES = ("finish", "cancel")

    def __init__(self, runtime, *, events: DriverEvents | None = None, clock=_localNow,
                 saveAfterChanges: bool = True, interruptPolicy: str = "finish") -> None:
        """interruptPolicy decides what new user input does to a running inference:
        "finish" lets it complete before the input is processed; "cancel" stops it,
        records its partial output as an interrupted step and rebuilds the prompt.
        """
        if interruptPolicy not in self.INTERRUPT_POLICIES:
            raise ValueError(f"interruptPolicy must be one of {self.INTERRUPT_POLICIES}.")
        self.interruptPolicy = interruptPolicy
        self._inputInterruptPending = False
        self.quiescent = False
        self._deferred: list = []
        self.runtime = runtime
        self.events = events or DriverEvents()
        self.clock = clock
        self.saveAfterChanges = saveAfterChanges
        self.debug = _DebugState()
        self._commands: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._currentJob: Job | None = None
        self._currentJobIsInference = False
        self._currentPartial: list[str] | None = None
        self._jobLock = threading.Lock()
        self.lastPartialOutput: str = ""

    # -- thread-safe requests -------------------------------------------------

    def submitInput(self, text: str) -> None:
        """Queues one user message; its receipt time is taken now.

        Under the "cancel" policy a running inference is cancelled so the
        message can be incorporated promptly.
        """
        self._commands.put(("input", text, self.clock()))
        if self.interruptPolicy == "cancel":
            with self._jobLock:
                job = self._currentJob if self._currentJobIsInference else None
                if job is not None:
                    self._inputInterruptPending = True
            if job is not None:
                job.requestCancellation()
        self._wake.set()

    def setBreakBeforeInference(self, enabled: bool) -> None:
        """Enables or disables breakpoint mode.

        In breakpoint mode every inference pauses twice: before it starts
        (phase "prompt": the prompt can be shown and replaced) and after the
        model has answered but before anything is acted on (phase "output":
        the raw output can be shown and replaced). continueExecution goes on.
        """
        self._commands.put(("break", bool(enabled)))
        self._wake.set()

    def replacePrompt(self, prompt: str) -> None:
        """Developer edit of the prompt of the inference paused at the breakpoint."""
        self._commands.put(("replace-prompt", prompt))
        self._wake.set()

    def replaceOutput(self, rawOutput: str) -> None:
        """Developer edit of the raw output of the inference paused after generation."""
        self._commands.put(("replace-output", rawOutput))
        self._wake.set()

    def continueExecution(self) -> None:
        """Continues from the breakpoint or a global pause."""
        self._commands.put(("continue",))
        self._wake.set()

    def pause(self) -> None:
        """Stops starting new work at the next boundary."""
        self._commands.put(("pause",))
        self._wake.set()

    def quiesce(self) -> None:
        """Orchestration quiescence: deactivate AI Buddy, then run no Jobs at all.

        Commands other than inspection and unquiesce wait until resumed.
        """
        self._commands.put(("quiesce",))
        self._wake.set()

    def unquiesce(self) -> None:
        """Ends quiescence; reactivates AI Buddy only if quiescence deactivated it."""
        self._commands.put(("unquiesce",))
        self._wake.set()

    def requestSave(self) -> None:
        """Requests an explicit save at the next boundary."""
        self._commands.put(("save",))
        self._wake.set()

    def query(self, capabilityId: str, payload=None) -> Future:
        """Requests a read-only capability result from the runtime thread.

        The Future completes at the next safe boundary, so a query made while
        an inference is running is answered after that inference ends.
        """
        future: Future = Future()
        self._commands.put(("read", capabilityId, self._stamped(payload), future))
        self._wake.set()
        return future

    def developerJob(self, capabilityId: str, payload=None) -> Future:
        """Runs a developer intervention capability as a Job at the next boundary."""
        future: Future = Future()
        self._commands.put(("job", capabilityId, self._stamped(payload), future))
        self._wake.set()
        return future

    @property
    def partialOutputSoFar(self) -> str:
        """Returns text streamed so far by the running inference, if any."""
        with self._jobLock:
            partial = self._currentPartial
        return "" if partial is None else "".join(partial)

    @property
    def busy(self) -> bool:
        """Reports whether a Job is currently running."""
        with self._jobLock:
            return self._currentJob is not None

    def _stamped(self, payload):
        """Adds the driver's time to a request: orchestration supplies time."""
        if payload is None:
            return {"now": self.clock()}
        if isinstance(payload, dict) and "now" not in payload:
            return {**payload, "now": self.clock()}
        return payload

    def interrupt(self) -> bool:
        """Requests cancellation of the running inference execution, if any.

        Bookkeeping Jobs (recording input, preparing, acknowledging delivery)
        are never cancelled: cancelling them could lose a user message.
        """
        with self._jobLock:
            job = self._currentJob if self._currentJobIsInference else None
        return False if job is None else job.requestCancellation()

    # -- runtime-thread operations -----------------------------------------------

    def _job(self, capabilityId: str, payload=None, *, inference: bool = False,
             partial: list[str] | None = None) -> Job:
        """Runs one capability as a Job, retaining its handle for interruption."""
        job = Job.new()
        with self._jobLock:
            self._currentJob = job
            self._currentJobIsInference = inference
            self._currentPartial = partial
        try:
            return self.runtime.runJob(capabilityId, payload, job=job)
        finally:
            with self._jobLock:
                self._currentJob = None
                self._currentJobIsInference = False
                self._currentPartial = None

    def _read(self, capabilityId: str, payload=None):
        """Invokes a read-only capability outside any Job."""
        return self.runtime.invokeCapability(capabilityId, payload)

    def _save(self) -> None:
        """Publishes one SaveBundle generation of the committed state."""
        if self.saveAfterChanges:
            bundle = self.runtime.saveApplication()
            self.events.diagnostic(f"saved generation {bundle.generation}")

    def _requireSucceeded(self, job: Job, action: str) -> bool:
        """Reports a non-successful Job and pauses so it cannot hot-loop."""
        if job.state is JobState.SUCCEEDED:
            return True
        detail = "" if job.error is None else f": {type(job.error).__qualname__}: {job.error}"
        self.events.diagnostic(f"{action} ended {job.state.value}{detail}; driver paused")
        self.debug.paused = True
        return False

    _ALLOWED_WHILE_QUIESCENT = ("quiesce", "unquiesce", "read", "break", "pause", "continue")

    def _handleCommands(self) -> bool:
        """Applies queued commands at a safe boundary.

        While quiescent, commands that would run Jobs are deferred, in order,
        until quiescence ends.
        """
        batch = []
        if not self.quiescent and self._deferred:
            batch, self._deferred = self._deferred, []
        while True:
            try:
                batch.append(self._commands.get_nowait())
            except queue.Empty:
                break
        for command in batch:
            kind = command[0]
            if self.quiescent and kind not in self._ALLOWED_WHILE_QUIESCENT:
                self._deferred.append(command)
                continue
            self._handleCommand(command)
        return bool(batch)

    def _handleCommand(self, command) -> None:
        """Applies one command."""
        kind = command[0]
        if kind == "quiesce":
            if self.quiescent:
                return
            job = self._job("aiBuddy.debug.lifecycle.change@1",
                            {"action": "deactivate", "by": "quiescence", "reason": "application quiescence",
                             "now": self.clock()})
            if self._requireSucceeded(job, "deactivate for quiescence"):
                self._save()
                self.quiescent = True
                self.events.diagnostic("application quiescent: AI Buddy inactive, no processing")
            return
        if kind == "unquiesce":
            if not self.quiescent:
                return
            self.quiescent = False
            lifecycle = self._read("aiBuddy.debug.status@1")["lifecycle"]
            if lifecycle["state"] == "inactive" and lifecycle.get("deactivatedBy") == "quiescence":
                job = self._job("aiBuddy.debug.lifecycle.change@1",
                                {"action": "activate", "by": "quiescence", "reason": "quiescence ended",
                                 "now": self.clock()})
                self._requireSucceeded(job, "reactivate after quiescence")
            self.events.diagnostic("quiescence ended")
            self._save()
            return
        if kind == "input":
            job = self._job("aiBuddy.input.receive@1", {"text": command[1], "receivedAt": command[2]})
            if self._requireSucceeded(job, "receive input"):
                result = job.result
                how = "amends" if result["amended"] else "establishes"
                self.events.diagnostic(f"input recorded as {result['experienceId']}; {how} {result['workId']}")
                superseded = result["supersededInferenceId"]
                if superseded is not None:
                    self.events.diagnostic(f"prepared inference {superseded} superseded by the new input")
                    if self.debug.pausedInference and self.debug.pausedInference["inferenceId"] == superseded:
                        self.debug.pausedInference = None
                        self.debug.continueRequested = False
                self._save()
        elif kind == "break":
            self.debug.breakBeforeInference = command[1]
            self.events.diagnostic(f"breakpoint mode {'on' if command[1] else 'off'} "
                                   "(pause before each inference and after each response)")
        elif kind == "replace-output":
            paused = self.debug.pausedInference
            if paused is None or paused.get("phase") != "output":
                self.events.diagnostic("no inference is paused after generation; output not replaced")
                return
            job = self._job("aiBuddy.debug.output.replace@1", {
                "inferenceId": paused["inferenceId"], "rawOutput": command[1], "now": self.clock()})
            if self._requireSucceeded(job, "replace output"):
                paused["rawOutput"] = command[1]
                self.events.diagnostic(f"output of {paused['inferenceId']} replaced by developer")
        elif kind == "replace-prompt":
            paused = self.debug.pausedInference
            if paused is not None and paused.get("phase") == "output":
                self.events.diagnostic("the paused inference has already run; its prompt cannot be replaced")
                return
            if paused is None:
                self.events.diagnostic("no paused inference; prompt not replaced")
                return
            job = self._job("aiBuddy.debug.prompt.replace@1", {
                "inferenceId": paused["inferenceId"], "prompt": command[1], "now": self.clock()})
            if self._requireSucceeded(job, "replace prompt"):
                paused["prompt"] = command[1]
                self.events.diagnostic(f"prompt of {paused['inferenceId']} replaced by developer")
        elif kind == "continue":
            self.debug.paused = False
            self.debug.continueRequested = True
        elif kind == "pause":
            self.debug.paused = True
            self.events.diagnostic("paused")
        elif kind == "save":
            bundle = self.runtime.saveApplication()
            self.events.diagnostic(f"saved generation {bundle.generation}")
        elif kind == "job":
            _kind, capabilityId, payload, future = command
            job = self._job(capabilityId, payload)
            if job.state is JobState.SUCCEEDED:
                future.set_result(job.result)
                self.events.diagnostic(f"developer intervention {capabilityId} applied")
                self._save()
            else:
                future.set_exception(job.error or RuntimeError(f"{capabilityId} ended {job.state.value}"))
        elif kind == "read":
            _kind, capabilityId, payload, future = command
            try:
                future.set_result(self._read(capabilityId, payload))
            except Exception as error:  # noqa: BLE001 - reported to the requester.
                future.set_exception(error)

    def _deliverOutbox(self) -> None:
        """Delivers committed deliberate communication, then acknowledges it."""
        pending = self._read("aiBuddy.outbox.pending@1")
        if not pending:
            return
        for entry in pending:
            self.events.speak(entry["text"])
        job = self._job("aiBuddy.outbox.acknowledge@1", {"throughId": pending[-1]["id"]})
        self._requireSucceeded(job, "acknowledge delivery")

    def _pauseAt(self, info: dict) -> None:
        """Pauses at a breakpoint phase and reports it."""
        self.debug.pausedInference = info
        self.debug.continueRequested = False
        where = "before inference" if info["phase"] == "prompt" else "after generation of"
        self.events.diagnostic(f"paused {where} {info['inferenceId']}")
        self.events.paused(info)

    def _execute(self, inference: dict) -> None:
        """Executes one prepared inference; its output is then processed or paused."""
        partial: list[str] = []
        job = self._job("aiBuddy.cycle.execute@1", {
            "inferenceId": inference["inferenceId"], "now": self.clock(), "partialOutput": partial},
            inference=True, partial=partial)
        self.lastPartialOutput = "".join(partial)
        with self._jobLock:
            byInput, self._inputInterruptPending = self._inputInterruptPending, False
        if job.state is JobState.CANCELLED and byInput:
            record = self._job("aiBuddy.cycle.recordInterrupted@1", {
                "inferenceId": inference["inferenceId"], "partialOutput": self.lastPartialOutput,
                "reason": "a new message arrived", "now": self.clock()})
            if self._requireSucceeded(record, "record interrupted inference"):
                self.debug.pausedInference = None
                self.debug.continueRequested = False
                self.events.diagnostic(
                    f"inference {inference['inferenceId']} cancelled for new input; "
                    f"{len(self.lastPartialOutput)} characters of partial output kept as an interrupted step")
                self._save()
            return
        if job.state is JobState.CANCELLED:
            self.debug.pausedInference = {**inference, "phase": "prompt"}
            self.debug.continueRequested = False
            self.events.diagnostic(
                f"inference {inference['inferenceId']} interrupted; it remains prepared "
                f"({len(self.lastPartialOutput)} characters of partial output not retained in state)")
            self.events.paused(self.debug.pausedInference)
            return
        if not self._requireSucceeded(job, "execute inference"):
            return
        self.debug.pausedInference = None
        self.debug.continueRequested = False
        result = job.result
        identifier = inference["inferenceId"]
        if result["state"] == "failed":
            record = self._read("aiBuddy.debug.record.describe@1", {"id": identifier})
            self.events.inferenceFinished(record)
            self.events.diagnostic(f"inference {identifier} failed: {result['error']}")
            self._save()
            return
        generated = {"inferenceId": identifier, "workId": inference.get("workId"), "phase": "output",
                     "rawOutput": result["rawOutput"]}
        if self.debug.breakBeforeInference:
            self._save()
            self._pauseAt(generated)
            return
        self._process(generated)

    def _process(self, generated: dict) -> None:
        """Processes the (possibly developer-replaced) output of a generated inference."""
        identifier = generated["inferenceId"]
        job = self._job("aiBuddy.cycle.process@1", {"inferenceId": identifier, "now": self.clock()})
        if not self._requireSucceeded(job, "process inference output"):
            return
        self.debug.pausedInference = None
        self.debug.continueRequested = False
        record = self._read("aiBuddy.debug.record.describe@1", {"id": identifier})
        self.events.inferenceFinished(record)
        result = job.result
        if result.get("passive"):
            self.events.diagnostic(
                f"passive step {identifier}: {len(result['processedMemoryIds'])} reflections "
                f"{result['processedMemoryIds']}, reactions {result['reactionIds']}, candidates {result['candidateIds']}")
            ignored = [warning for warning in result.get("warnings", []) if warning.startswith("passive-")]
            if ignored:
                self.events.diagnostic(f"passive step {identifier} ignored: {', '.join(ignored)}")
            self._save()
            return
        for memoryId in result.get("rememberedIds", []):
            self.events.diagnostic(f"inference {identifier} established memory {memoryId}")
        for move in result.get("gameMoves", []):
            self.events.diagnostic(f"game move {move['op']} {move['cell']}: {move['outcome']}")
        if result.get("gameStatus") in ("won", "lost"):
            self.events.diagnostic(f"Minesweeper game {result['gameStatus']}")
        for started in result.get("startedActivities", []):
            verb = "established" if started["created"] else "already open as"
            self.events.diagnostic(f"activity {started['activity']} {verb} {started['workId']}")
        if result.get("lifecycleChange"):
            change = result["lifecycleChange"]
            self.events.diagnostic(f"AI Buddy {change['from']} -> {change['to']} ({change['reason']})")
        for change in result.get("ruleChanges", []):
            self.events.diagnostic(f"rule {change['action']}{'' if change['applied'] else ' rejected'}: {change['outcome']}")
        for item in result.get("disregarded", []):
            self.events.diagnostic(f"rule {item['ruleId']} disregarded for this step: {item['reason'] or 'no reason'}")
        if result.get("investigationId"):
            self.events.diagnostic(f"game lost; investigation established as {result['investigationId']}")
        if result.get("workState") == "continuing":
            queries = "; ".join(result.get("recalls", []))
            detail = f"requested recall ({queries}); " if queries else ""
            self.events.diagnostic(f"inference {identifier} {detail}undertaking continues")
        elif not result["communications"]:
            self.events.diagnostic(f"inference {identifier} concluded without communication")
        self._deliverOutbox()
        self._save()

    def runOnce(self) -> bool:
        """Performs at most one unit of work. Returns False when idle or paused."""
        handled = self._handleCommands()
        if self.quiescent or self.debug.paused:
            return handled
        paused = self.debug.pausedInference
        if paused is not None:
            if not self.debug.continueRequested:
                return handled
            if paused.get("phase") == "output":
                self._process(paused)
            else:
                self._execute(paused)
            return True
        self._deliverOutbox()
        job = self._job("aiBuddy.cycle.prepare@1", {"now": self.clock()})
        if not self._requireSucceeded(job, "prepare cycle"):
            return True
        prepared = job.result
        for workId in prepared.get("expired", []):
            self.events.diagnostic(f"{workId} expired before it was started")
        if prepared.get("lifecycleChange"):
            change = prepared["lifecycleChange"]
            self.events.diagnostic(f"AI Buddy {change['from']} -> {change['to']} ({change['reason']})")
        if prepared.get("deterministic"):
            result = prepared["result"]
            self.events.diagnostic(f"{prepared['workId']} advanced deterministically "
                                   f"({prepared['scheduling']['reason']}): {result}")
            self._save()
            return True
        if prepared.get("idle"):
            if prepared.get("expired"):
                self._save()
            return handled
        if prepared.get("generated"):
            generated = {"inferenceId": prepared["inferenceId"], "workId": prepared["workId"],
                         "phase": "output", "rawOutput": prepared["rawOutput"]}
            if self.debug.breakBeforeInference:
                self._pauseAt(generated)
            else:
                self._process(generated)
            return True
        if not prepared.get("reoffered"):
            self.events.diagnostic(f"selected {prepared['workId']}: {prepared['scheduling']['reason']}")
        if self.debug.breakBeforeInference:
            self._pauseAt({**prepared, "phase": "prompt"})
            return True
        self._execute(prepared)
        return True

    def run(self, stop: threading.Event, *, idleSeconds: float = 0.25) -> None:
        """Runs continuously until stop is set, sleeping while idle."""
        while not stop.is_set():
            if not self.runOnce():
                self._wake.wait(timeout=idleSeconds)
                self._wake.clear()
