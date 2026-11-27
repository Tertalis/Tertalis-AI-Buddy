# file: tests/first_party/applications/aiBuddy/test_checkpointG.py ; version: 1
"""Checkpoint G: browser frontend - the bridge maps Invocations to the loop and loop events to tracks.

HTTP and browser behaviour of frontend.host/frontend.kit are platform-tested;
here the channel is driven directly and the loop is settled synchronously.
"""
from __future__ import annotations

import sys
from itertools import count

import pytest

_ids = count(1)


def invoke(harness, target, arguments=None, expect=None):
    """Submits one Invocation as the host would after HTTP intake; returns its id."""
    protocol = sys.modules[type(harness.channel).__module__.rsplit(".", 1)[0] + ".protocol"]
    invocationId = f"t-{next(_ids)}"
    status, _reply = harness.channel.submit(protocol.Invocation(invocationId, target, arguments or {}, expect))
    assert status == 202
    harness.bridge.pump()
    return invocationId


def outcome(harness, invocationId):
    """The Result/FlowError published for an Invocation, or None while pending."""
    items = harness.channel.snapshot("flow.invocations")["items"]
    return next((item for item in items if item["invocationId"] == invocationId), None)


def track(harness, name):
    snapshot = harness.channel.snapshot(name)
    return snapshot["items"] if snapshot["style"] == "log" else snapshot["value"]


def drain(harness):
    """Settles the loop and lets the bridge publish what changed (status refresh included)."""
    for _ in range(3):              # a status refresh issued by one pump is answered by the next settle
        harness.settle()
        harness.bridge.pump()


def test_messages_from_the_browser_reach_ai_buddy_and_the_conversation_track(makeHarness):
    h = makeHarness(["<say>Hello from Buddy</say>"], frontend=True)
    said = invoke(h, "aiBuddy.say", {"text": "Hi there"})
    assert outcome(h, said) == {"role": "result", "invocationId": said, "value": {"queued": True}}
    drain(h)
    conversation = track(h, "aibuddy.conversation")
    assert [(item["from"], item["text"]) for item in conversation] == [("user", "Hi there"), ("buddy", "Hello from Buddy")]
    assert h.spoken == ["Hello from Buddy"], "terminal events still fire"
    assert any("input recorded" in item["text"] for item in track(h, "aibuddy.diagnostics"))
    assert track(h, "aibuddy.inference")["last"]["state"] == "completed"
    assert track(h, "aibuddy.status")["buddy"]["experiences"] >= 1
    assert track(h, "aibuddy.loop") == {"busy": False, "quiescent": False, "paused": False, "breakpoints": False,
                                        "interruptPolicy": "finish"}


def test_breakpoints_from_the_browser_carry_pause_ids_and_reject_stale_actions(makeHarness):
    h = makeHarness(["<say>original</say>"], frontend=True)
    invoke(h, "aiBuddy.breakpoints", {"enabled": True})
    invoke(h, "aiBuddy.say", {"text": "Hi"})
    drain(h)
    pause = track(h, "aibuddy.breakpoint")
    assert pause["phase"] == "prompt" and pause["pauseId"] and "Hi" in pause["text"]

    edited = invoke(h, "aiBuddy.replacePrompt", {"text": "EDITED PROMPT"}, {"pause": pause["pauseId"]})
    drain(h)
    assert outcome(h, edited)["value"] == {"replaced": "prompt"}
    assert track(h, "aibuddy.breakpoint")["text"] == "EDITED PROMPT"

    resumed = invoke(h, "aiBuddy.continue", {}, {"pause": pause["pauseId"]})
    drain(h)
    assert outcome(h, resumed)["role"] == "result"
    assert h.providerPrompts()[-1] == "EDITED PROMPT"
    output = track(h, "aibuddy.breakpoint")
    assert output["phase"] == "output" and output["pauseId"] != pause["pauseId"]

    stale = invoke(h, "aiBuddy.continue", {}, {"pause": pause["pauseId"]})
    drain(h)
    assert outcome(h, stale)["code"] == "conflict"
    assert track(h, "aibuddy.breakpoint")["pauseId"] == output["pauseId"], "a stale action changes nothing"

    replaced = invoke(h, "aiBuddy.replaceOutput", {"text": "<say>replaced</say>"}, {"pause": output["pauseId"]})
    drain(h)
    assert outcome(h, replaced)["value"] == {"replaced": "output"}
    invoke(h, "aiBuddy.continue", {}, {"pause": output["pauseId"]})
    drain(h)
    assert track(h, "aibuddy.breakpoint") is None
    assert track(h, "aibuddy.conversation")[-1]["text"] == "replaced"


def test_a_breakpoint_made_stale_by_a_developer_change_is_cleared_not_failed(makeHarness):
    """Recorded defect: continuing a paused inference that was superseded stopped the loop."""
    h = makeHarness(["<say>never used</say>", "<say>later</say>"], frontend=True)
    h.driver.setBreakBeforeInference(True)
    h.run("Start something")
    pause = h.driver.pausedSnapshot
    assert pause and pause["phase"] == "prompt"
    abandoned = invoke(h, "aiBuddy.intervene", {"capability": "aiBuddy.debug.work.change@1",
                                                "payload": {"workId": pause["workId"], "action": "abandon"}})
    drain(h)
    assert outcome(h, abandoned)["role"] == "result"
    resumed = invoke(h, "aiBuddy.continue", {}, {"pause": pause["pauseId"]})
    drain(h)
    assert outcome(h, resumed)["value"] == {"continued": True, "stale": True}
    assert not h.driver.debug.paused and h.driver.pausedSnapshot is None
    assert not any("driver paused" in text for text in h.diagnostics)
    assert any("is stale" in text for text in h.diagnostics)


def test_terminal_continue_on_a_stale_breakpoint_no_longer_stops_the_loop(makeHarness):
    h = makeHarness(["<say>never used</say>"])
    h.driver.setBreakBeforeInference(True)
    h.run("Start something")
    pause = h.driver.pausedSnapshot
    h.developer("aiBuddy.debug.work.change@1", {"workId": pause["workId"], "action": "abandon"})
    h.driver.continueExecution()
    h.settle()
    assert not h.driver.debug.paused
    assert not any("driver paused" in text for text in h.diagnostics)


def test_reads_and_interventions_are_limited_to_the_allow_lists(makeHarness):
    h = makeHarness([], frontend=True)
    read = invoke(h, "aiBuddy.read", {"capability": "aiBuddy.debug.personality.describe@1"})
    refused = invoke(h, "aiBuddy.read", {"capability": "aiBuddy.cycle.execute@1"})
    refusedJob = invoke(h, "aiBuddy.intervene", {"capability": "aiBuddy.outbox.acknowledge@1", "payload": {}})
    unknown = invoke(h, "aiBuddy.format", {})
    badExpect = invoke(h, "aiBuddy.continue", {}, {"other": 1})
    badText = invoke(h, "aiBuddy.say", {"text": "   "})
    drain(h)
    assert outcome(h, read)["value"]["name"] == "Buddy"
    assert outcome(h, refused)["code"] == outcome(h, refusedJob)["code"] == "forbidden-target"
    assert outcome(h, unknown)["code"] == "unknown-target"
    assert outcome(h, badExpect)["code"] == "invalid-expectation"
    assert outcome(h, badText)["code"] == "invalid-arguments"


def test_interventions_run_as_jobs_and_failures_become_flow_errors(makeHarness):
    h = makeHarness([], frontend=True)
    added = invoke(h, "aiBuddy.intervene", {"capability": "aiBuddy.debug.work.add@1",
                                            "payload": {"kind": "task", "purpose": "Tidy up"}})
    broken = invoke(h, "aiBuddy.intervene", {"capability": "aiBuddy.debug.rule.change@1",
                                             "payload": {"ruleId": "r999999", "action": "retire"}})
    drain(h)
    assert outcome(h, added)["role"] == "result"
    failure = outcome(h, broken)
    assert failure["role"] == "flowError" and failure["code"] == "failed"
    assert not h.driver.debug.paused, "a failed developer intervention does not stop the loop"


def test_game_track_follows_minesweeper(makeHarness):
    h = makeHarness(["<say>Let me look at the board.</say>"], frontend=True)
    assert track(h, "aibuddy.game") is None
    invoke(h, "aiBuddy.intervene", {"capability": "aiBuddy.debug.game.start@1",
                                    "payload": {"width": 4, "height": 4, "mines": 2, "seed": 7}})
    drain(h)
    game = track(h, "aibuddy.game")
    assert game["status"] == "in-progress" and len(game["board"]) == 5  # header row + 4 rows


def test_loop_controls_publish_flags_immediately(makeHarness):
    h = makeHarness([], frontend=True)
    invoke(h, "aiBuddy.pause")
    h.settle()
    h.bridge.pump()
    assert track(h, "aibuddy.loop")["paused"] is True
    interrupted = invoke(h, "aiBuddy.interrupt")
    assert outcome(h, interrupted)["value"] == {"interrupted": False}


@pytest.mark.parametrize("frontend", [False, True])
def test_without_a_browser_the_buddy_behaves_the_same(makeHarness, frontend):
    h = makeHarness(["<say>same</say>"], frontend=frontend)
    h.run("Hello")
    assert h.spoken == ["same"]
