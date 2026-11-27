# file: first-party/applications/aiBuddy/host/frontendBridge.py ; version: 3
"""Connects the AI Buddy loop to the browser frontend channel (frontend.host@0.1.0).

This is the only AI Buddy module that knows the channel API. It turns loop
events into tracks and browser Invocations into loop requests, and resolves
each Invocation when the loop has applied it. It never calls the runtime:
reads and interventions go through the loop's own thread-safe requests, which
complete at the loop's next safe boundary.

Tracks:
    aibuddy.loop          state  loop flags (busy, paused, quiescent, breakpoints), published at once
    aibuddy.status        state  AI Buddy status summary (refreshed at the loop's next boundary)
    aibuddy.breakpoint    state  current pause (pauseId, phase, prompt/rawOutput) or null
    aibuddy.inference     state  running inference partial output, last finished summary
    aibuddy.game          state  Minesweeper state, or null
    aibuddy.inferences    log    summaries of the last INFERENCE_HISTORY finished inferences
                                 (full records stay in AI Buddy state; the browser loads one
                                 on demand through aiBuddy.read and releases it when closed)
    aibuddy.conversation  log    {"from": "user"|"buddy", "text", "at"}
    aibuddy.diagnostics   log    {"text", "at"}
"""
from __future__ import annotations

import threading
from concurrent.futures import Future
from datetime import datetime, timezone

__all__ = ["FrontendBridge", "READ_CAPABILITIES", "INTERVENTION_CAPABILITIES"]

READ_CAPABILITIES = frozenset({
    "aiBuddy.debug.status@1", "aiBuddy.debug.schedule@1", "aiBuddy.debug.record.describe@1",
    "aiBuddy.debug.memory.search@1", "aiBuddy.debug.memory.provenance@1", "aiBuddy.debug.rule.list@1",
    "aiBuddy.debug.personality.describe@1", "aiBuddy.game.minesweeper.state@1", "aiBuddy.debug.llm.describe@1",
})
INTERVENTION_CAPABILITIES = frozenset({
    "aiBuddy.debug.work.add@1", "aiBuddy.debug.work.choose@1", "aiBuddy.debug.work.change@1",
    "aiBuddy.debug.situation.change@1", "aiBuddy.debug.game.start@1", "aiBuddy.debug.lifecycle.change@1",
    "aiBuddy.debug.rule.add@1", "aiBuddy.debug.rule.change@1", "aiBuddy.debug.passive.start@1",
    "aiBuddy.debug.memory.inject@1", "aiBuddy.debug.llm.select@1",
})
MAX_TEXT = 64 * 1024
INFERENCE_HISTORY = 200
MAX_PARTIAL = 128 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class _Refused(Exception):
    """An Invocation the bridge rejects before reaching the loop (FlowError code, message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FrontendBridge:
    """Owns the AI Buddy side of the channel. Call wrap() on the loop's events before the loop starts."""

    def __init__(self, loop, channel, *, refreshSeconds: float = 0.3) -> None:
        self.loop = loop
        self.channel = channel
        self.refreshSeconds = refreshSeconds
        self._lock = threading.Lock()
        self._dirty = True
        self._statusPending = False
        self._lastPartial: tuple | None = None
        self._lastRecord: dict | None = None
        self._lastLoopFlags: dict | None = None
        channel.declareTrack("aibuddy.loop", "state", initial=None)
        channel.declareTrack("aibuddy.status", "state", initial=None)
        channel.declareTrack("aibuddy.breakpoint", "state", initial=None)
        channel.declareTrack("aibuddy.inference", "state", initial={"running": False, "partial": "", "last": None})
        channel.declareTrack("aibuddy.game", "state", initial=None)
        channel.declareTrack("aibuddy.inferences", "log", maxItems=INFERENCE_HISTORY)
        channel.declareTrack("aibuddy.conversation", "log")
        channel.declareTrack("aibuddy.diagnostics", "log")

    # ----- events ----------------------------------------------------------------

    def wrap(self, events):
        """Returns DriverEvents that call the given ones and also feed the frontend."""
        from dataclasses import replace

        def both(original, extra):
            def call(*args):
                original(*args)
                try:
                    extra(*args)
                except Exception:  # noqa: BLE001 - the UI must never break the loop.
                    pass
            return call

        return replace(
            events,
            speak=both(events.speak, self._spoke),
            diagnostic=both(events.diagnostic, self._diagnostic),
            paused=both(events.paused, self._paused),
            pauseChanged=both(events.pauseChanged, self._paused),
            breakpointCleared=both(events.breakpointCleared, self._cleared),
            inferenceFinished=both(events.inferenceFinished, self._finished),
            received=both(events.received, self._received),
        )

    def _spoke(self, text: str) -> None:
        self.channel.append("aibuddy.conversation", {"from": "buddy", "text": text, "at": _now()})
        self._markDirty()

    def _received(self, text: str, receivedAt: str) -> None:
        self.channel.append("aibuddy.conversation", {"from": "user", "text": text, "at": receivedAt})
        self._markDirty()

    def _diagnostic(self, text: str) -> None:
        self.channel.append("aibuddy.diagnostics", {"text": text, "at": _now()})
        self._markDirty()

    def _paused(self, info: dict) -> None:
        self.channel.set("aibuddy.breakpoint", self._pauseView(info))
        self._markDirty()

    def _cleared(self) -> None:
        self.channel.set("aibuddy.breakpoint", None)
        self._markDirty()

    def _finished(self, record: dict) -> None:
        summary = self._inferenceSummary(record)
        self._lastRecord = summary          # summaries only: prompts/outputs are loaded on demand
        self._lastPartial = None
        self.channel.set("aibuddy.inference", {"running": False, "partial": "", "last": summary})
        self.channel.append("aibuddy.inferences", summary)
        self._markDirty()

    @staticmethod
    def _inferenceSummary(record: dict) -> dict:
        """Small list entry; the full record (prompt, output) is read on demand."""
        record = record if isinstance(record, dict) else {}
        timing = record.get("timing") if isinstance(record.get("timing"), dict) else {}
        sent = record.get("submittedPrompt", record.get("prompt"))
        return {
            "id": record.get("id"), "workId": record.get("workId"), "purpose": record.get("purpose"),
            "state": record.get("state"), "at": record.get("generatedAt") or _now(),
            "durationNs": timing.get("durationNs"),
            "promptEdited": sent is not None and sent != record.get("builtPrompt", sent),
            "outputEdited": record.get("rawOutput") != record.get("generatedOutput", record.get("rawOutput")),
            "error": record.get("error"),
        }

    @staticmethod
    def _pauseView(info: dict) -> dict:
        view = {key: info.get(key) for key in ("pauseId", "phase", "inferenceId", "workId")}
        view["text"] = info.get("rawOutput") if info.get("phase") == "output" else info.get("prompt")
        return view

    def _markDirty(self) -> None:
        with self._lock:
            self._dirty = True

    # ----- refresh (called on the bridge thread, or from tests via pump) ----------

    def tick(self) -> None:
        """Publishes loop flags, live partial output, and (when dirty) a status/game refresh."""
        debug = self.loop.debug
        flags = {"busy": self.loop.busy, "quiescent": self.loop.quiescent, "paused": debug.paused,
                 "breakpoints": debug.breakBeforeInference, "interruptPolicy": self.loop.interruptPolicy}
        if flags != self._lastLoopFlags:
            self._lastLoopFlags = flags
            self.channel.set("aibuddy.loop", flags)
            self._markDirty()
        partial = self.loop.partialOutputSoFar if self.loop.busy else ""
        key = (self.loop.busy, len(partial))
        if self.loop.busy and key != self._lastPartial:
            self._lastPartial = key
            self.channel.set("aibuddy.inference", {"running": True, "partial": partial[-MAX_PARTIAL:], "last": self._lastRecord})
        with self._lock:
            refresh = self._dirty and not self._statusPending
            if refresh:
                self._dirty = False
                self._statusPending = True
        if refresh:
            status = self.loop.query("aiBuddy.debug.status@1")
            game = self.loop.query("aiBuddy.game.minesweeper.state@1", {})
            status.add_done_callback(lambda future: self._statusDone(future, game))

    def _statusDone(self, future: Future, game: Future) -> None:
        try:
            summary = future.result()
        except Exception as error:  # noqa: BLE001 - shown in the UI instead.
            summary = {"error": f"{type(error).__name__}: {error}"}
        try:
            self.channel.set("aibuddy.status", {"buddy": summary, "at": _now()})
        except Exception:  # noqa: BLE001 - channel closed during shutdown.
            pass

        def gameDone(done: Future) -> None:
            try:
                value = done.result()
                self.channel.set("aibuddy.game", value if value.get("id") else None)
            except Exception:  # noqa: BLE001 - no game or channel closed.
                pass
            finally:
                with self._lock:
                    self._statusPending = False
        game.add_done_callback(gameDone)

    # ----- invocations --------------------------------------------------------------

    def pump(self, timeout: float = 0.0) -> int:
        """Takes and dispatches pending Invocations, then ticks. Returns how many were taken."""
        items = self.channel.waitInvocations(timeout) if timeout > 0 else self.channel.takeInvocations()
        for item in items:
            self.dispatch(item)
        self.tick()
        return len(items)

    def run(self, stop: threading.Event) -> None:
        """Bridge thread: dispatches Invocations and refreshes tracks until stop or channel close."""
        while not stop.is_set():
            try:
                self.pump(self.refreshSeconds)
            except RuntimeError:          # ChannelClosed (host unloaded)
                return

    def dispatch(self, item) -> None:
        """Maps one Invocation to the loop; resolves now or when the loop's Future completes."""
        try:
            outcome = self._apply(item.target, item.arguments, item.expect)
        except _Refused as refusal:
            self.channel.fail(item.invocationId, refusal.code, str(refusal))
            return
        if isinstance(outcome, Future):
            outcome.add_done_callback(lambda future: self._settle(item.invocationId, future))
        else:
            self.channel.resolve(item.invocationId, outcome)

    def _settle(self, invocationId: str, future: Future) -> None:
        try:
            value = future.result()
        except Exception as error:  # noqa: BLE001 - reported as a FlowError.
            code = "conflict" if type(error).__name__ == "StaleBreakpoint" else "failed"
            try:
                self.channel.fail(invocationId, code, f"{error}"[:500] or type(error).__name__)
            except Exception:  # noqa: BLE001 - channel closed during shutdown.
                pass
            return
        try:
            self.channel.resolve(invocationId, value)
        except Exception:  # noqa: BLE001 - channel closed during shutdown.
            pass
        self._markDirty()

    def _apply(self, target: str, arguments: dict, expect: dict | None):
        loop = self.loop
        pauseId = None
        if expect is not None:
            if set(expect) != {"pause"} or type(expect["pause"]) is not str:
                raise _Refused("invalid-expectation", "Only {\"pause\": <pauseId>} is understood.")
            pauseId = expect["pause"]
        if target == "aiBuddy.say":
            loop.submitInput(self._text(arguments, "text"))
            return {"queued": True}
        if target == "aiBuddy.continue":
            return loop.continueExecution(pauseId=pauseId)
        if target == "aiBuddy.replacePrompt":
            return loop.replacePrompt(self._text(arguments, "text"), pauseId=pauseId)
        if target == "aiBuddy.replaceOutput":
            return loop.replaceOutput(self._text(arguments, "text", allowEmpty=True), pauseId=pauseId)
        if target == "aiBuddy.breakpoints":
            enabled = arguments.get("enabled")
            if type(enabled) is not bool:
                raise _Refused("invalid-arguments", "enabled must be true or false.")
            loop.setBreakBeforeInference(enabled)
            return {"breakpoints": enabled}
        if target == "aiBuddy.pause":
            loop.pause()
            return {"pausing": True}
        if target == "aiBuddy.interrupt":
            return {"interrupted": loop.interrupt()}
        if target == "aiBuddy.quiesce":
            loop.quiesce()
            return {"quiescing": True}
        if target == "aiBuddy.unquiesce":
            loop.unquiesce()
            return {"unquiescing": True}
        if target == "aiBuddy.save":
            loop.requestSave()
            return {"saving": True}
        if target in ("aiBuddy.read", "aiBuddy.intervene"):
            capability = arguments.get("capability")
            allowed = READ_CAPABILITIES if target == "aiBuddy.read" else INTERVENTION_CAPABILITIES
            if capability not in allowed:
                raise _Refused("forbidden-target", "That capability is not available from the browser.")
            payload = arguments.get("payload")
            if payload is not None and type(payload) is not dict:
                raise _Refused("invalid-arguments", "payload must be an object.")
            if target == "aiBuddy.read":
                return loop.query(capability, payload)
            return loop.developerJob(capability, payload)
        raise _Refused("unknown-target", "Unknown AI Buddy action.")

    @staticmethod
    def _text(arguments: dict, name: str, *, allowEmpty: bool = False) -> str:
        value = arguments.get(name)
        if type(value) is not str or (not allowEmpty and not value.strip()) or len(value) > MAX_TEXT:
            raise _Refused("invalid-arguments", f"{name} must be a non-empty string of at most {MAX_TEXT} characters.")
        return value
