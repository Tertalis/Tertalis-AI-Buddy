# file: first-party/frontendHost/channel.py ; version: 3
"""Thread-safe delivery seam between HTTP threads and the Application's loop owner.

HTTP threads only submit Invocations and read Emits/snapshots. The loop owner
declares tracks, pushes state, takes Invocations and resolves them. Nothing
here calls the runtime, so no HTTP thread can enter the ApplicationRun lane.
All storage is bounded; values are frozen as encoded JSON when emitted.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import Condition

from .protocol import INVOCATIONS_TRACK, MAX_VALUE_BYTES, Invocation, encode, flowError, requireTrackName

__all__ = ["ChannelClosed", "FrontendChannel"]

_STYLES = ("state", "log")
MAX_LOG_BYTES = 1024 * 1024  # Leaves room for metadata within the kit's 2 MiB response limit.


class ChannelClosed(RuntimeError):
    """The channel was closed with its host."""


@dataclass(slots=True)
class _Track:
    """One ordered Emit sequence with a cached snapshot and a bounded replay buffer."""

    style: str
    position: int = 0
    value: bytes = b"null"                                  # state: latest value (encoded)
    items: deque = field(default_factory=deque)             # log: bounded tail of encoded items
    itemBytes: int = 0                                      # encoded log tail, including separators
    events: deque = field(default_factory=deque)            # (position, encoded event JSON)


class FrontendChannel:
    """One per ApplicationRun. Positions start at 1 per track; a snapshot at p includes every Emit <= p."""

    def __init__(self, *, runId: str, maxInbox: int = 256, replay: int = 512, logTail: int = 512, idempotency: int = 1024) -> None:
        """Creates the channel with the reserved invocations log track."""
        if any(type(value) is not int or value < 1 for value in (maxInbox, replay, logTail, idempotency)):
            raise ValueError("Channel limits must be positive integers.")
        self.runId = runId
        self._maxInbox = maxInbox
        self._replay = replay
        self._logTail = logTail
        self._idempotency = idempotency
        self._condition = Condition()
        self._tracks: dict[str, _Track] = {}
        self._inbox: deque[Invocation] = deque()
        self._known: OrderedDict[str, dict | None] = OrderedDict()   # invocationId -> outcome item or None while pending
        self._woken = False
        self._closed = False
        self._declare(INVOCATIONS_TRACK, "log")

    # ----- loop owner side -------------------------------------------------

    def declareTrack(self, name: str, style: str, *, initial: object = None, maxItems: int | None = None) -> None:
        """Declares a 'state' (latest value) or 'log' (append) track; declaring twice is an error.

        maxItems bounds a log track's retained tail below the channel-wide logTail
        (older items are released; Views learn the bound from the session descriptor).
        """
        name = requireTrackName(name)
        if name == INVOCATIONS_TRACK:
            raise ValueError("The invocations track is reserved.")
        if maxItems is not None and (style != "log" or type(maxItems) is not int or not 1 <= maxItems <= self._logTail):
            raise ValueError(f"maxItems applies to log tracks and must be 1..{self._logTail}.")
        with self._condition:
            self._requireOpen()
            self._declare(name, style, maxItems=maxItems)
            if style == "state":
                self._tracks[name].value = self._encodeValue(initial)

    def describeTracks(self) -> list[dict]:
        """Declared tracks for the session descriptor: name, style and, for logs, maxItems."""
        with self._condition:
            return [{"name": name, "style": track.style, **({"maxItems": track.items.maxlen} if track.style == "log" else {})}
                    for name, track in self._tracks.items() if name != INVOCATIONS_TRACK]

    def tracks(self) -> tuple[tuple[str, str], ...]:
        """Declared (name, style) pairs, reserved track excluded, in declaration order."""
        with self._condition:
            return tuple((name, track.style) for name, track in self._tracks.items() if name != INVOCATIONS_TRACK)

    def set(self, name: str, value: object) -> int:
        """Replaces a state track's value; returns the new position."""
        return self._emit(name, "state", "set", value)

    def append(self, name: str, value: object) -> int:
        """Appends to a log track; returns the new position."""
        return self._emit(name, "log", "append", value)

    def reset(self, name: str) -> int:
        """Clears a log track; returns the new position."""
        return self._emit(name, "log", "reset", None)

    def waitInvocations(self, timeout: float | None = None) -> tuple[Invocation, ...]:
        """Blocks until Invocations are queued, wake() is called, the timeout elapses, or close().

        Returns the drained Invocations (possibly empty). Raises ChannelClosed after close.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._inbox and not self._woken and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._condition.wait(remaining)
            self._requireOpen()
            self._woken = False
            return self._drain()

    def takeInvocations(self) -> tuple[Invocation, ...]:
        """Non-blocking drain."""
        with self._condition:
            self._requireOpen()
            return self._drain()

    def wake(self) -> None:
        """Wakes a waitInvocations caller without an Invocation (e.g. terminal input arrived)."""
        with self._condition:
            self._woken = True
            self._condition.notify_all()

    def resolve(self, invocationId: str, value: object = None) -> None:
        """Publishes a Result for an accepted Invocation."""
        self._settle(invocationId, {"role": "result", "invocationId": invocationId, "value": value})

    def fail(self, invocationId: str, code: str, message: str) -> None:
        """Publishes a FlowError for an accepted Invocation (e.g. code 'conflict')."""
        self._settle(invocationId, flowError(code, message, invocationId))

    # ----- HTTP side ---------------------------------------------------------

    def submit(self, invocation: Invocation) -> tuple[int, dict]:
        """Accepts or deduplicates one Invocation; returns (HTTP status, reply)."""
        with self._condition:
            if self._closed:
                return 503, flowError("unavailable", "The host is closing.", invocation.invocationId)
            if invocation.invocationId in self._known:
                outcome = self._known[invocation.invocationId]
                return (200, outcome) if outcome is not None else (202, {"role": "accepted", "invocationId": invocation.invocationId})
            if len(self._inbox) >= self._maxInbox:
                return 429, flowError("busy", "Too many pending actions; try again shortly.", invocation.invocationId)
            if len(self._known) >= self._idempotency:
                settled = next((key for key, value in self._known.items() if value is not None), None)
                if settled is None:
                    return 429, flowError("busy", "Too many pending actions; try again shortly.", invocation.invocationId)
                del self._known[settled]
            self._known[invocation.invocationId] = None
            self._inbox.append(invocation)
            self._condition.notify_all()
            return 202, {"role": "accepted", "invocationId": invocation.invocationId}

    def snapshot(self, name: str) -> dict | None:
        """Authoritative cached state of one track, or None if unknown."""
        with self._condition:
            track = self._tracks.get(name)
            if track is None:
                return None
            result = {"runId": self.runId, "track": name, "style": track.style, "position": track.position}
            if track.style == "state":
                result["value"] = json.loads(track.value)
            else:
                result["items"] = [json.loads(item) for item in track.items]
            return result

    def hasTrack(self, name: str) -> bool:
        """True for declared tracks, including the reserved invocations track."""
        with self._condition:
            return name in self._tracks

    def positions(self, names) -> dict[str, int]:
        """Current positions, used as a new stream's starting cursor."""
        with self._condition:
            return {name: self._tracks[name].position for name in names}

    def waitEvents(self, cursor: dict[str, int], timeout: float) -> list[tuple[str, bytes]] | None:
        """Waits for Emits after cursor; returns [(eventName, data bytes)], advancing cursor in place.

        Returns an empty list on timeout and None once the channel is closed. A
        position older than the replay buffer yields a 'gap' event and moves the
        cursor to the current position (the client fetches the snapshot).
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._closed:
                    return None
                ready = self._collect(cursor)
                if ready:
                    return ready
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)

    def close(self) -> None:
        """Ends every waiter; later loop-owner calls raise ChannelClosed."""
        with self._condition:
            self._closed = True
            self._inbox.clear()
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        """True after close()."""
        return self._closed

    # ----- internals ---------------------------------------------------------

    def _declare(self, name: str, style: str, *, maxItems: int | None = None) -> None:
        if style not in _STYLES:
            raise ValueError(f"Track style must be 'state' or 'log', not {style!r}.")
        if name in self._tracks:
            raise ValueError(f"Track already declared: {name}.")
        self._tracks[name] = _Track(style=style, items=deque(maxlen=maxItems or self._logTail), events=deque(maxlen=self._replay))

    def _requireOpen(self) -> None:
        if self._closed:
            raise ChannelClosed("The frontend channel is closed.")

    @staticmethod
    def _encodeValue(value: object) -> bytes:
        data = encode(value)
        if len(data) > MAX_VALUE_BYTES:
            raise ValueError(f"Emitted value exceeds {MAX_VALUE_BYTES} bytes.")
        return data

    def _emit(self, name: str, style: str, kind: str, value: object, *, internal: bool = False) -> int:
        data = None if kind == "reset" else self._encodeValue(value)
        with self._condition:
            self._requireOpen()
            track = self._tracks.get(name)
            if track is None or (name == INVOCATIONS_TRACK and not internal):
                raise KeyError(f"Unknown or reserved track: {name}.")
            if track.style != style:
                raise ValueError(f"Track {name} is a {track.style} track.")
            track.position += 1
            if kind == "set":
                track.value = data
            elif kind == "append":
                if len(track.items) == track.items.maxlen:
                    track.itemBytes -= len(track.items[0]) + 1
                track.items.append(data)
                track.itemBytes += len(data) + 1
                while track.itemBytes > MAX_LOG_BYTES:
                    track.itemBytes -= len(track.items.popleft()) + 1
            else:
                track.items.clear()
                track.itemBytes = 0
            event = b'{"track":' + encode(name) + b',"position":' + str(track.position).encode() + b',"kind":"' + kind.encode() + b'"'
            event += (b',"value":' + data + b"}") if data is not None else b"}"
            track.events.append((track.position, event))
            self._condition.notify_all()
            return track.position

    def _settle(self, invocationId: str, item: dict) -> None:
        with self._condition:
            self._requireOpen()
            if invocationId not in self._known:
                raise KeyError(f"Unknown Invocation: {invocationId}.")
            if self._known[invocationId] is not None:
                raise RuntimeError(f"Invocation already settled: {invocationId}.")
            try:
                item = json.loads(self._encodeValue(item))
                self._emit(INVOCATIONS_TRACK, "log", "append", item, internal=True)
            except (TypeError, ValueError):
                item = flowError("invalid-result", "The result could not be represented.", invocationId)
                self._emit(INVOCATIONS_TRACK, "log", "append", item, internal=True)
            self._known[invocationId] = item

    def _drain(self) -> tuple[Invocation, ...]:
        drained = tuple(self._inbox)
        self._inbox.clear()
        return drained

    def _collect(self, cursor: dict[str, int]) -> list[tuple[str, bytes]]:
        ready: list[tuple[str, bytes]] = []
        for name, after in cursor.items():
            track = self._tracks[name]
            if after >= track.position:
                continue
            oldest = track.events[0][0] if track.events else track.position + 1
            if after + 1 < oldest:
                ready.append(("gap", b'{"track":' + encode(name) + b',"position":' + str(track.position).encode() + b"}"))
            else:
                ready.extend(("emit", event) for position, event in track.events if position > after)
            cursor[name] = track.position
        return ready
