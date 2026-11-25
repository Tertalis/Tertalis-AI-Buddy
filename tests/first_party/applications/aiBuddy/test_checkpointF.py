# file: tests/first_party/applications/aiBuddy/test_checkpointF.py ; version: 2
"""Checkpoint F runtime contracts: passive processing and scheduled reactions.

Scripted responses verify that passive processing can run while AI Buddy is
inactive or resting without expressing anything or waking it, that its
results reach active processing only through ordinary retrieval and
scheduled reactions, and that spontaneous candidates expire unstarted.
"""
from __future__ import annotations

LIFECYCLE = "aiBuddy.debug.lifecycle.change@1"
PASSIVE = "aiBuddy.debug.passive.start@1"


def _inference(harness, number: int) -> dict:
    return harness.state.load(f"aibuddy/inferences/i{number:06d}")


def test_passive_processing_runs_while_inactive_and_its_reaction_waits_for_activation(makeHarness):
    harness = makeHarness([
        "<say>Nice name.</say>",
        "Reflecting. <remember>The user has a cat named Miso.</remember><react>Ask how Miso is doing.</react>"
        "<say>Hello!</say><intend>something</intend>",
        "<say>How is Miso?</say>",
    ])
    harness.run("My cat is called Miso.")
    harness.developer(LIFECYCLE, {"action": "deactivate"})
    harness.developer(PASSIVE, {"activity": "memory-processing"})

    passivePrompt = harness.providerPrompts()[1]
    assert "This is background processing" in passivePrompt
    assert "The user said: My cat is called Miso." in passivePrompt and "You said: Nice name." in passivePrompt
    assert "<react>text</react>" in passivePrompt and "<say>" not in passivePrompt

    reflection = harness.state.load("aibuddy/memories/m000001")
    assert reflection["kind"] == "processed"
    assert reflection["origin"]["by"] == "passive-processing" and reflection["origin"]["sourceIds"] == ["e000001", "e000002"]
    warnings = _inference(harness, 2)["interpretation"]["warnings"]
    assert "passive-communications-ignored" in warnings and "passive-intend-ignored" in warnings
    assert harness.spoken == ["Nice name."]
    assert harness.state.load("aibuddy/experiences/sequence") == 2
    assert harness.state.load("aibuddy/lifecycle")["state"] == "inactive"
    reaction = harness.work("w000003")
    assert reaction["kind"] == "reaction" and reaction["state"] == "pending"
    schedule = harness.read("aiBuddy.debug.schedule@1", {"now": harness.clock()})
    assert {entry["id"]: entry.get("reason") for entry in schedule["candidates"]}["w000003"] == "AI Buddy is inactive"

    harness.developer(LIFECYCLE, {"action": "activate"})
    reactionPrompt = harness.providerPrompts()[2]
    assert "While reflecting in the background" in reactionPrompt and "Ask how Miso is doing." in reactionPrompt
    assert "You became active at" in reactionPrompt
    assert "Hello!" not in reactionPrompt
    assert harness.spoken == ["Nice name.", "How is Miso?"]


def test_reflection_is_found_by_ordinary_retrieval_and_is_not_reviewed_twice(makeHarness):
    harness = makeHarness(["<say>ok</say>", "<remember>The user's cat is named Miso.</remember>", "<say>Miso.</say>"],
                          memory={"recentLimit": 0})
    harness.run("My cat is called Miso.")
    harness.developer(PASSIVE, {"activity": "memory-processing"})
    assert harness.state.load("aibuddy/passive/cursor") == {"experience": 2, "memory": 1}

    harness.developer(PASSIVE, {"activity": "memory-processing"})
    assert len(harness.providerPrompts()) == 2
    assert harness.work("w000003")["endedReason"] == "nothing-to-process"

    harness.run("What is my cat called?")
    assert "A conclusion you reached while reflecting: The user's cat is named Miso." in harness.providerPrompts()[2]


def test_salient_subjects_establish_limited_candidates_that_expire_unstarted(makeHarness):
    harness = makeHarness([
        "<say>Nice.</say>",
        "<candidate>Practise Minesweeper openings.</candidate><candidate>Review garden notes.</candidate>"
        "<candidate>A third idea.</candidate>",
    ], settings={"passive": {"maxCandidates": 2, "candidateLifetimeSeconds": 60}})
    harness.run("I like gardening.")
    harness.developer(LIFECYCLE, {"action": "deactivate"})
    harness.developer(PASSIVE, {"activity": "salient-subjects"})

    assert "Choose up to 2 subjects" in harness.providerPrompts()[1]
    candidates = [harness.work("w000003"), harness.work("w000004")]
    assert [item["purpose"] for item in candidates] == ["Practise Minesweeper openings.", "Review garden notes."]
    assert all(item["optional"] and item["spontaneous"] and item["state"] == "pending" for item in candidates)
    assert all(item["expiresAt"] == "2026-11-24T21:01:00+01:00" for item in candidates)
    assert "candidates-beyond-limit-ignored" in _inference(harness, 2)["interpretation"]["warnings"]
    assert harness.state.load("aibuddy/work/sequence") == 4

    harness.advance(61)
    harness.settle()
    assert [harness.work(identifier)["state"] for identifier in ("w000003", "w000004")] == ["expired", "expired"]
    assert len(harness.providerPrompts()) == 2


def test_passive_work_does_not_end_rest_or_consume_notices(makeHarness):
    first = makeHarness(["<say>hi</say>"])
    applicationId = first.runtime.applicationRun.application.applicationId
    first.run("hello")
    first.developer(LIFECYCLE, {"action": "rest"})
    first.close()

    second = makeHarness(["<remember>A quiet day.</remember>", "<say>back</say>"], applicationId=applicationId)
    second.developer(PASSIVE, {"activity": "memory-processing"})
    assert second.state.load("aibuddy/lifecycle")["state"] == "resting"
    assert "was not running" not in second.providerPrompts()[1]
    assert [notice["kind"] for notice in second.state.load("aibuddy/lifecycle")["notices"]] == ["restart"]

    second.run("anyone there?")
    assert "The application was not running from about" in second.providerPrompts()[2]
    assert second.state.load("aibuddy/lifecycle")["state"] == "active"


def test_periodic_memory_processing_is_opportunistic(makeHarness):
    harness = makeHarness(["<say>a</say>", "<remember>The user greeted me.</remember>"], settings={
        "periodic": [{"name": "reflect", "everySeconds": 100, "establishes": "memory-processing"}]})
    harness.settle()
    assert harness.work("w000002")["endedReason"] == "nothing-to-process"
    assert harness.providerPrompts() == []

    harness.advance(100)
    harness.run("hello")
    first, second = harness.providerPrompts()
    assert "<message>\nhello\n</message>" in first
    assert "This is background processing" in second
    assert harness.work("w000005")["origin"]["by"] == "periodic"
    assert harness.state.load("aibuddy/memories/m000001")["kind"] == "processed"


def test_active_prompts_cannot_use_passive_actions(makeHarness):
    harness = makeHarness(["<react>later</react><candidate>an idea</candidate><say>ok</say>"])
    harness.run("hi")
    warnings = _inference(harness, 1)["interpretation"]["warnings"]
    assert "react-only-in-passive-processing-ignored" in warnings
    assert "candidate-only-in-passive-processing-ignored" in warnings
    assert harness.state.load("aibuddy/work/sequence") == 1
    assert harness.spoken == ["ok"]
