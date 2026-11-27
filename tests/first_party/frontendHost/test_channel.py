# file: tests/first_party/frontendHost/test_channel.py ; version: 3
"""Delivery seam: bounded, ordered, idempotent, thread-safe; never calls the runtime."""
from __future__ import annotations

from threading import Thread
import time

import pytest


@pytest.fixture(autouse=True)
def _names(fh):
    """Binds module names for brevity in this file."""
    global FrontendChannel, ChannelClosed, Invocation, INVOCATIONS
    FrontendChannel, ChannelClosed, Invocation, INVOCATIONS = fh.FrontendChannel, fh.ChannelClosed, fh.Invocation, fh.INVOCATIONS


def invocation(invocationId="i1", target="app.do", arguments=None, expect=None):
    return Invocation(invocationId, target, arguments or {}, expect)


def test_state_and_log_tracks_keep_positions_and_snapshots():
    channel = FrontendChannel(runId="r", logTail=2)
    channel.declareTrack("status", "state", initial={"busy": False})
    channel.declareTrack("log", "log")
    assert channel.snapshot("status") == {"runId": "r", "track": "status", "style": "state", "position": 0, "value": {"busy": False}}
    assert channel.set("status", {"busy": True}) == 1
    for value in ("a", "b", "c"):
        channel.append("log", value)
    assert channel.snapshot("log")["items"] == ["b", "c"]
    assert channel.reset("log") == 4 and channel.snapshot("log")["items"] == []
    assert channel.tracks() == (("status", "state"), ("log", "log"))
    with pytest.raises(ValueError):
        channel.append("status", 1)
    with pytest.raises(KeyError):
        channel.append(INVOCATIONS, 1)
    with pytest.raises(ValueError):
        channel.declareTrack(INVOCATIONS, "log")
    with pytest.raises(ValueError):
        channel.declareTrack("status", "state")
    with pytest.raises(ValueError):
        channel.declareTrack("bad:name", "state")
    with pytest.raises((TypeError, ValueError)):
        channel.set("status", {"x": float("nan")})


def test_emits_are_frozen_at_emit_time():
    channel = FrontendChannel(runId="r")
    channel.declareTrack("status", "state")
    value = {"items": [1]}
    channel.set("status", value)
    value["items"].append(2)
    assert channel.snapshot("status")["value"] == {"items": [1]}


def test_wait_events_replays_after_cursor_and_reports_gaps():
    channel = FrontendChannel(runId="r", replay=2)
    channel.declareTrack("log", "log")
    for value in range(4):
        channel.append("log", value)
    cursor = {"log": 2}
    events = channel.waitEvents(cursor, 0)
    assert [name for name, _ in events] == ["emit", "emit"] and cursor == {"log": 4}
    cursor = {"log": 0}
    events = channel.waitEvents(cursor, 0)
    assert events == [("gap", b'{"track":"log","position":4}')] and cursor == {"log": 4}
    assert channel.waitEvents(cursor, 0.01) == []


def test_waiters_wake_on_emit_and_close():
    channel = FrontendChannel(runId="r")
    channel.declareTrack("log", "log")
    results = []
    worker = Thread(target=lambda: results.append(channel.waitEvents({"log": 0}, 5)))
    worker.start()
    time.sleep(0.05)
    channel.append("log", "x")
    worker.join(2)
    assert results and results[0][0][0] == "emit"
    worker = Thread(target=lambda: results.append(channel.waitEvents({"log": 1}, 5)))
    worker.start()
    time.sleep(0.05)
    channel.close()
    worker.join(2)
    assert results[-1] is None


def test_invocations_are_idempotent_bounded_and_resolved_on_the_invocations_track():
    channel = FrontendChannel(runId="r", maxInbox=1)
    assert channel.submit(invocation("i1")) == (202, {"role": "accepted", "invocationId": "i1"})
    assert channel.submit(invocation("i1")) == (202, {"role": "accepted", "invocationId": "i1"})
    status, reply = channel.submit(invocation("i2"))
    assert status == 429 and reply["code"] == "busy"
    (taken,) = channel.takeInvocations()
    assert taken.invocationId == "i1"
    channel.resolve("i1", {"ok": True})
    assert channel.submit(invocation("i1")) == (200, {"role": "result", "invocationId": "i1", "value": {"ok": True}})
    assert channel.snapshot(INVOCATIONS)["items"] == [{"role": "result", "invocationId": "i1", "value": {"ok": True}}]
    with pytest.raises(RuntimeError):
        channel.resolve("i1")
    with pytest.raises(KeyError):
        channel.resolve("never")
    channel.submit(invocation("i3"))
    channel.takeInvocations()
    channel.fail("i3", "conflict", "The breakpoint is no longer current.")
    assert channel.snapshot(INVOCATIONS)["items"][-1] == {"role": "flowError", "code": "conflict", "message": "The breakpoint is no longer current.", "invocationId": "i3"}


def test_unrepresentable_result_becomes_a_flow_error():
    channel = FrontendChannel(runId="r")
    channel.submit(invocation("i1"))
    channel.takeInvocations()
    channel.resolve("i1", object())
    assert channel.snapshot(INVOCATIONS)["items"][-1]["code"] == "invalid-result"


def test_wait_invocations_blocks_until_submit_wake_or_timeout():
    channel = FrontendChannel(runId="r")
    started = time.monotonic()
    assert channel.waitInvocations(0.05) == ()
    assert time.monotonic() - started >= 0.04
    Thread(target=lambda: (time.sleep(0.05), channel.submit(invocation("i9")))).start()
    assert [item.invocationId for item in channel.waitInvocations(2)] == ["i9"]
    Thread(target=lambda: (time.sleep(0.05), channel.wake())).start()
    assert channel.waitInvocations(2) == ()
    channel.close()
    with pytest.raises(ChannelClosed):
        channel.waitInvocations(0)
    assert channel.submit(invocation("i10"))[0] == 503


def test_concurrent_producers_keep_positions_dense():
    channel = FrontendChannel(runId="r", replay=10_000, logTail=10_000)
    channel.declareTrack("log", "log")
    workers = [Thread(target=lambda: [channel.append("log", n) for n in range(200)]) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    cursor = {"log": 0}
    events = channel.waitEvents(cursor, 0)
    assert len(events) == 1600 and cursor == {"log": 1600}



def test_drained_pending_actions_cannot_bypass_the_retention_bound():
    channel = FrontendChannel(runId="r", maxInbox=2, idempotency=4)
    for n in range(4):
        assert channel.submit(invocation(f"i{n}"))[0] == 202
        channel.takeInvocations()
    assert channel.submit(invocation("overflow"))[0] == 429
    # A settled entry can be evicted even when an older action remains pending.
    channel.resolve("i1", "done")
    assert channel.submit(invocation("next"))[0] == 202
    channel.resolve("i0", "still remembered")
    assert channel.submit(invocation("i0"))[1]["value"] == "still remembered"


def test_cached_outcome_is_frozen_when_resolved():
    channel = FrontendChannel(runId="r")
    channel.submit(invocation())
    channel.takeInvocations()
    value = {"items": [1]}
    channel.resolve("i1", value)
    value["items"].append(2)
    assert channel.submit(invocation())[1]["value"] == {"items": [1]}
    assert channel.snapshot(INVOCATIONS)["items"][0]["value"] == {"items": [1]}


@pytest.mark.parametrize("limit", [0, -1, True])
def test_channel_limits_must_be_positive_integers(limit):
    for name in ("maxInbox", "replay", "logTail", "idempotency"):
        with pytest.raises(ValueError):
            FrontendChannel(runId="r", **{name: limit})


def test_log_tracks_may_retain_fewer_items_than_the_channel():
    channel = FrontendChannel(runId="r", logTail=5)
    channel.declareTrack("recent", "log", maxItems=2)
    channel.declareTrack("all", "log")
    for value in range(4):
        channel.append("recent", value)
        channel.append("all", value)
    assert channel.snapshot("recent")["items"] == [2, 3]
    assert channel.snapshot("all")["items"] == [0, 1, 2, 3]
    assert channel.describeTracks() == [{"name": "recent", "style": "log", "maxItems": 2},
                                        {"name": "all", "style": "log", "maxItems": 5}]
    for bad in ({"maxItems": 0}, {"maxItems": 6}, {"maxItems": "2"}):
        with pytest.raises(ValueError):
            channel.declareTrack("x" + str(len(str(bad))), "log", **bad)
    with pytest.raises(ValueError):
        channel.declareTrack("st", "state", maxItems=2)
