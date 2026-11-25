# file: tests/first_party/applications/aiBuddy/test_checkpointB.py ; version: 5
"""Checkpoint B runtime contracts: memory and inference continuation.

Responses are scripted; these tests verify mechanics (what is stored,
selected, shown and continued), not retrieval quality or model behaviour.
"""
from __future__ import annotations

from backend.values import MISSING


def _inference(harness, number: int) -> dict:
    return harness.state.load(f"aibuddy/inferences/i{number:06d}")


def test_empty_memory_is_stated_and_recall_returns_usable_absence(makeHarness):
    harness = makeHarness(["<recall>my favourite colour</recall>", "<say>I have nothing on that.</say>"])
    harness.run("What is my favourite colour?")

    first, second = harness.providerPrompts()
    assert "You have no personal memories yet." in first
    assert "<recall>what to search for</recall>" in first
    assert "You tried to recall: my favourite colour" in second
    assert "No memories were found for this." in second
    assert harness.spoken == ["I have nothing on that."]

    assert _inference(harness, 1)["recalls"][0]["outcome"] == "no-match"
    work = harness.state.load("aibuddy/work/w000001")
    assert work["state"] == "completed" and work["endedReason"] == "inference-concluded"
    assert len(work["steps"]) == 2
    assert harness.state.load("aibuddy/work/open") == []


def test_memory_is_established_with_provenance_and_selected_later(makeHarness):
    harness = makeHarness([
        "The user introduced themselves. <remember>The user's name is Ada.</remember><say>Nice to meet you, Ada.</say>",
        "<say>Your name is Ada.</say>",
    ], memory={"recentLimit": 0})
    harness.run("Hi, I am Ada.", "What is my name?")

    memory = harness.state.load("aibuddy/memories/m000001")
    assert memory["text"] == "The user's name is Ada."
    assert memory["origin"] == {"by": "ai-buddy", "inferenceId": "i000001", "workId": "w000001",
                                "sourceExperienceIds": ["e000001"]}

    second = harness.providerPrompts()[1]
    assert "You decided to remember: The user's name is Ada." in second
    selected = _inference(harness, 2)["inputs"]
    assert [hit["id"] for hit in selected["automaticRetrieval"]["results"]] == ["m000001"]
    assert selected["storedMemoryCount"] == 3
    assert selected["recentIds"] == []

    stored = harness.read("aiBuddy.debug.memory.search@1", {"query": "user's name"})
    assert stored["storedCount"] == 5
    assert [hit["id"] for hit in stored["results"]] == ["m000001", "e000004", "e000003"]

    provenance = harness.read("aiBuddy.debug.memory.provenance@1", {"id": "m000001"})
    assert provenance["producedBy"] == "ai-buddy"
    assert provenance["inference"]["thinking"] == "The user introduced themselves."
    assert [item["text"] for item in provenance["sourceExperiences"]] == ["Hi, I am Ada."]

    reply = harness.read("aiBuddy.debug.memory.provenance@1", {"id": "e000002"})
    assert reply["producedBy"] == "ai-buddy"
    assert [item["text"] for item in reply["sourceExperiences"]] == ["Hi, I am Ada."]


def test_recall_returns_additional_memories_then_no_further(makeHarness):
    harness = makeHarness(["<recall>garden</recall>"] * 4 + ["<say>done</say>"], memory={
        "recentLimit": 0, "automaticLimit": 0, "recallLimit": 1, "maxContinuations": 4})
    for text in ("The garden has roses.", "The garden gate squeaks.", "The garden is north of the house."):
        harness.developer("aiBuddy.debug.memory.inject@1", {"text": text})
    harness.run("Tell me about the garden.")

    prompts = harness.providerPrompts()
    assert len(prompts) == 5
    recalls = [_inference(harness, number)["recalls"][0] for number in range(1, 5)]
    assert [recall["outcome"] for recall in recalls] == ["found", "found", "found", "no-further"]
    found = [recall["results"][0]["id"] for recall in recalls[:3]]
    assert sorted(found) == ["m000001", "m000002", "m000003"]

    final = prompts[4]
    assert "Noted for you by the developer: The garden has roses." in final
    assert "No further memories were found beyond those already shown." in final
    assert "Conclude now" in final and "Searching your memory is not available" in final
    assert "<recall>what to search for</recall>" not in final
    assert harness.spoken == ["done"]


def test_continuation_limit_stops_runaway_recall(makeHarness):
    harness = makeHarness(["<recall>anything</recall>"] * 3, memory={"maxContinuations": 1})
    harness.run("hello")

    assert len(harness.providerPrompts()) == 2
    work = harness.state.load("aibuddy/work/w000001")
    assert work["state"] == "completed" and work["endedReason"] == "continuation-limit"
    second = _inference(harness, 2)
    assert second["inputs"]["recallOffered"] is False
    assert second["recalls"][0]["outcome"] == "not-available"
    assert "continuation-not-available-ignored" in second["interpretation"]["warnings"]


def test_breakpoint_applies_to_continuation_prompts(makeHarness):
    harness = makeHarness(["<recall>x</recall>", "<say>ok</say>"])
    harness.driver.setBreakBeforeInference(True)
    harness.run("question")
    assert len(harness.pauses) == 1 and harness.providerPrompts() == []

    harness.continueRun()
    assert len(harness.pauses) == 2
    assert "You tried to recall: x" in harness.pauses[1]["prompt"]

    harness.continueRun()
    assert harness.spoken == ["ok"]


def test_communication_during_an_undertaking_does_not_finish_it(makeHarness):
    harness = makeHarness(["<say>Let me think.</say><recall>plans</recall>", "<say>Nothing planned.</say>"])
    harness.run("Any plans?")
    assert harness.spoken == ["Let me think.", "Nothing planned."]
    assert "You said to the user: Let me think." in harness.providerPrompts()[1]


def test_own_output_of_the_undertaking_is_not_returned_as_memory(makeHarness):
    harness = makeHarness(["<say>alpha beta</say><recall>alpha</recall>", "<say>ok</say>"])
    harness.run("go")
    assert _inference(harness, 1)["recalls"][0]["outcome"] == "no-match"
    second = harness.providerPrompts()[1]
    assert second.count("alpha beta") == 1


def test_tags_inside_think_and_unclosed_tags_are_not_actions(makeHarness):
    harness = makeHarness(["<think><remember>secret</remember><recall>a</recall></think><remember>unclosed"])
    harness.run("x")
    assert harness.state.load("aibuddy/memories/sequence") == 0
    assert len(harness.providerPrompts()) == 1
    assert _inference(harness, 1)["interpretation"]["warnings"] == ["unclosed-remember-ignored"]


def test_memories_survive_restart(makeHarness):
    first = makeHarness(["<remember>The user's name is Ada.</remember>"])
    applicationId = first.runtime.applicationRun.application.applicationId
    first.run("I am Ada.")
    first.close()

    second = makeHarness(["<say>Ada.</say>"], applicationId=applicationId, memory={"recentLimit": 0})
    second.run("What is my name?")
    assert "You decided to remember: The user's name is Ada." in second.providerPrompts()[-1]


def test_checkpoint_a_state_is_migrated_on_load(makeHarness):
    first = makeHarness([])
    applicationId = first.runtime.applicationRun.application.applicationId
    transaction = first.state.openTransaction()
    meta = first.state.load("aibuddy/meta")
    transaction.set("aibuddy/meta", {"schema": "aibuddy.state@1", "createdAt": meta["createdAt"]})
    transaction.setAbsent("aibuddy/memories/sequence")
    transaction.commit()
    first.runtime.saveApplication()
    first.close()

    second = makeHarness(["<remember>migrated fine</remember>"], applicationId=applicationId)
    meta = second.state.load("aibuddy/meta")
    assert meta["schema"] == "aibuddy.state@5"
    assert [item["from"] for item in meta["migrations"]] == [
        "aibuddy.state@1", "aibuddy.state@2", "aibuddy.state@3", "aibuddy.state@4"]
    assert second.state.load("aibuddy/situation/sequence") == 0
    second.run("hello")
    assert second.state.load("aibuddy/memories/m000001")["text"] == "migrated fine"
    assert second.state.load("aibuddy/memories/m000002") is MISSING
