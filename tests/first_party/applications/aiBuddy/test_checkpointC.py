# file: tests/first_party/applications/aiBuddy/test_checkpointC.py ; version: 3
"""Checkpoint C runtime contracts: things to do and coordination.

Responses are scripted; these tests verify eligibility, selection,
directives, amendment, interruption and timing mechanics, not model
behaviour.
"""
from __future__ import annotations

import threading
import time


def _queue(harness, capabilityId, payload):
    """Queues a developer intervention without letting the driver run work yet."""
    return harness.driver.developerJob(capabilityId, payload)


def _task(harness, purpose, **extra):
    return _queue(harness, "aiBuddy.debug.work.add@1", {"kind": "task", "purpose": purpose, **extra})


def _change(harness, workId, action, **extra):
    return _queue(harness, "aiBuddy.debug.work.change@1", {"workId": workId, "action": action, **extra})


def _inference(harness, number: int) -> dict:
    return harness.state.load(f"aibuddy/inferences/i{number:06d}")


def _promptOrder(harness, *markers):
    """Returns, for each provider prompt, the first marker it contains."""
    return [next((marker for marker in markers if marker in prompt), None) for prompt in harness.providerPrompts()]


def test_dependency_blocks_work_until_completed(makeHarness):
    harness = makeHarness(["<say>A done</say>", "<say>C done</say>", "<say>B done</say>"])
    _task(harness, "alpha task")
    _task(harness, "gamma task")
    _task(harness, "beta task", dependsOn=["w000002"])
    harness.driver.pause()
    harness.settle()

    schedule = harness.read("aiBuddy.debug.schedule@1", {"now": harness.clock()})
    byId = {entry["id"]: entry for entry in schedule["candidates"]}
    assert byId["w000003"]["eligible"] is False
    assert byId["w000003"]["reason"] == "waiting for w000002 (pending)"
    assert schedule["selected"] == "w000001"

    harness.continueRun()
    assert _promptOrder(harness, "alpha task", "beta task", "gamma task") == ["alpha task", "gamma task", "beta task"]
    second = _inference(harness, 2)["scheduling"]
    blocked = next(entry for entry in second["candidates"] if entry["id"] == "w000003")
    assert blocked["eligible"] is False and blocked["reason"] == "waiting for w000002 (pending)"
    assert harness.work("w000003")["state"] == "completed"


def test_invalid_developer_command_fails_its_job_and_operation_continues(makeHarness):
    harness = makeHarness(["<say>still here</say>"])
    future = _task(harness, "orphan task", dependsOn=["w000999"])
    harness.settle()
    error = future.exception(timeout=5)
    assert isinstance(error, ValueError) and "w000999" in str(error)
    assert harness.state.load("aibuddy/work/sequence") == 0

    harness.run("hello")
    assert harness.spoken == ["still here"]


def test_developer_requests_use_the_driver_clock(makeHarness):
    harness = makeHarness([])
    _task(harness, "later task", notBeforeSeconds=30)
    harness.settle()
    assert harness.work("w000001")["notBefore"] == "2026-11-24T21:00:30+01:00"
    assert harness.work("w000001")["createdAt"] == "2026-11-24T21:00:00+01:00"


def test_user_input_is_selected_before_older_queued_work(makeHarness):
    harness = makeHarness(["<say>hi</say>", "<say>done</say>"])
    _task(harness, "alpha task")
    harness.driver.submitInput("hello")
    harness.settle()

    assert _promptOrder(harness, "<message>\nhello", "alpha task") == ["<message>\nhello", "alpha task"]
    assert _inference(harness, 1)["scheduling"]["reason"].startswith("user input")
    assert harness.spoken == ["hi", "done"]


def test_input_before_the_inference_runs_amends_and_supersedes_it(makeHarness):
    harness = makeHarness(["<say>Got both.</say>"])
    harness.driver.setBreakBeforeInference(True)
    harness.run("first part")
    assert len(harness.pauses) == 1

    harness.run("second part")
    assert len(harness.pauses) == 2
    prompt = harness.pauses[1]["prompt"]
    assert "You have received 2 messages from the user, in order:" in prompt
    assert "<message>\nfirst part\n</message>" in prompt and "<message>\nsecond part\n</message>" in prompt
    assert "already working on this" not in prompt
    assert _inference(harness, 1)["state"] == "superseded"

    harness.continueRun()
    assert harness.spoken == ["Got both."]
    assert harness.state.load("aibuddy/work/sequence") == 1
    assert len(harness.providerPrompts()) == 1


def test_input_during_an_undertaking_amends_it_and_keeps_prior_steps(makeHarness):
    harness = makeHarness(["<recall>plans</recall>", "<say>ok</say>"])
    harness.driver.setBreakBeforeInference(True)
    harness.run("first")
    harness.continueRun()
    assert len(harness.pauses) == 2

    harness.run("second")
    assert len(harness.pauses) == 3
    prompt = harness.pauses[2]["prompt"]
    assert "it arrived while you were already working on this" in prompt
    assert "You tried to recall: plans" in prompt
    assert _inference(harness, 2)["state"] == "superseded"

    harness.continueRun()
    assert harness.spoken == ["ok"]
    assert harness.state.load("aibuddy/work/sequence") == 1


def test_cancel_policy_keeps_partial_output_and_rebuilds_prompt(makeHarness):
    harness = makeHarness([{"block": True}, "<say>ok</say>"], interruptPolicy="cancel")
    harness.driver.submitInput("think hard")
    worker = threading.Thread(target=harness.settle)
    worker.start()
    deadline = time.monotonic() + 5
    while harness.driver.partialOutputSoFar != "partial thought":
        assert time.monotonic() < deadline, "inference never produced partial output"
        time.sleep(0.01)
    harness.driver.submitInput("actually, never mind")
    worker.join(timeout=10)
    assert not worker.is_alive()

    interrupted = _inference(harness, 1)
    assert interrupted["state"] == "interrupted"
    assert interrupted["partialOutput"] == "partial thought"
    second = harness.providerPrompts()[1]
    assert "interrupted: a new message arrived" in second
    assert "Your partial output: partial thought" in second
    assert "<message>\nactually, never mind\n</message>" in second
    assert harness.spoken == ["ok"]
    assert harness.pauses == []


def test_periodic_check_gets_due_opportunities_without_inference(makeHarness):
    harness = makeHarness(["<say>hi</say>"], settings={"periodic": [{"name": "heartbeat", "everySeconds": 10}]})
    harness.settle()
    first = harness.work("w000001")
    assert first["state"] == "completed" and first["steps"][0]["result"]["lateSeconds"] == 0.0
    assert harness.work("w000002")["notBefore"] == "2026-11-24T21:00:10+01:00"

    harness.advance(5)
    harness.settle()
    schedule = harness.read("aiBuddy.debug.schedule@1", {"now": harness.clock()})
    assert schedule["selected"] is None
    assert schedule["candidates"][0]["reason"] == "scheduled for 2026-11-24T21:00:10+01:00"

    harness.advance(5)
    harness.settle()
    assert harness.work("w000002")["state"] == "completed"
    assert harness.providerPrompts() == []

    harness.advance(10)
    harness.run("hi there")
    first = _inference(harness, 1)["scheduling"]
    assert first["selected"] == "w000004"
    assert any(entry["id"] == "w000003" and entry["eligible"] for entry in first["candidates"])
    assert harness.work("w000003")["state"] == "completed"


def test_completed_result_can_be_rechecked_as_separate_work(makeHarness):
    harness = makeHarness(["<say>The answer is 42.</say>",
                           "Let me reconsider. <say>Correction: it is 41.</say>"])
    harness.run("What is 6 times 7 minus 1?")
    harness.developer("aiBuddy.debug.work.add@1", {"kind": "recheck", "recheckOf": "w000001"})

    recheck = harness.providerPrompts()[1]
    assert "Reconsider whether what you concluded and said was right and sufficient." in recheck
    assert "The user said: What is 6 times 7 minus 1?" in recheck
    assert "- You said: The answer is 42." in recheck
    assert harness.spoken == ["The answer is 42.", "Correction: it is 41."]
    original = harness.work("w000001")
    assert original["state"] == "completed" and original["endedReason"] == "inference-concluded"
    assert harness.work("w000002")["recheckOf"] == "w000001"


def test_important_state_is_always_included_but_never_schedules(makeHarness):
    harness = makeHarness(["<say>Wet outside.</say>", "<say>Hi.</say>"])
    harness.developer("aiBuddy.debug.situation.change@1",
                      {"action": "add", "text": "The user is in a meeting until 15:00.", "important": True})
    harness.developer("aiBuddy.debug.situation.change@1", {"action": "add", "text": "The weather today is rainy."})
    assert harness.providerPrompts() == []

    harness.run("What's the weather like?", "hello there")
    weather, greeting = harness.providerPrompts()
    for prompt in (weather, greeting):
        assert "Important current information:\n- The user is in a meeting until 15:00." in prompt
    assert "Current information that may be relevant:\n- The weather today is rainy." in weather
    assert "The weather today is rainy." not in greeting


def test_suspend_resume_abandon_and_expiry(makeHarness):
    harness = makeHarness(["<say>alpha done</say>"])
    _task(harness, "alpha task")
    _task(harness, "beta task")
    _task(harness, "gamma task", notBeforeSeconds=120, expiresInSeconds=60)
    _change(harness, "w000001", "suspend")
    _change(harness, "w000002", "abandon")
    harness.settle()
    assert harness.providerPrompts() == []
    schedule = harness.read("aiBuddy.debug.schedule@1", {"now": harness.clock()})
    assert {entry["id"]: entry.get("reason") for entry in schedule["candidates"]} == {
        "w000001": "suspended", "w000003": "scheduled for 2026-11-24T21:02:00+01:00"}

    harness.advance(61)
    harness.settle()
    assert harness.work("w000003")["state"] == "expired"
    assert harness.work("w000003")["endedReason"] == "expired-unstarted"

    harness.developer("aiBuddy.debug.work.change@1", {"workId": "w000001", "action": "resume"})
    assert _promptOrder(harness, "alpha task", "beta task", "gamma task") == ["alpha task"]
    assert harness.work("w000002")["state"] == "abandoned"
    assert [item["action"] for item in harness.work("w000001")["interventions"]] == ["suspend", "resume"]


def test_intention_establishes_separate_later_work(makeHarness):
    harness = makeHarness([
        "<say>Sure, later.</say><intend>Think about a name for the garden.</intend>",
        "Garden name thoughts. <say>How about Greenhaven?</say>",
    ])
    harness.run("Can you think of a garden name sometime?")

    intended = harness.work("w000002")
    assert intended["origin"] == {"by": "ai-buddy", "inferenceId": "i000001", "workId": "w000001"}
    assert intended["important"] is False and intended["state"] == "completed"
    assert harness.work("w000001")["state"] == "completed"
    assert "You decided earlier to do this:\nThink about a name for the garden." in harness.providerPrompts()[1]
    assert harness.spoken == ["Sure, later.", "How about Greenhaven?"]


def test_choice_lets_inference_select_the_next_undertaking(makeHarness):
    harness = makeHarness(["I prefer the second. <choose>2</choose>", "<say>beta</say>", "<say>alpha</say>"])
    _task(harness, "alpha task")
    _task(harness, "beta task")
    _queue(harness, "aiBuddy.debug.work.choose@1", {"candidateIds": ["w000001", "w000002"]})
    harness.settle()

    choice = harness.providerPrompts()[0]
    assert "1. alpha task" in choice and "2. beta task" in choice and "<choose>number</choose>" in choice
    assert "<recall>" not in choice
    assert _inference(harness, 1)["chosenWorkId"] == "w000002"
    assert _inference(harness, 2)["scheduling"]["reason"].startswith("interrupt directive")
    assert harness.spoken == ["beta", "alpha"]


def test_interrupt_and_important_directives_change_selection(makeHarness):
    harness = makeHarness(["<say>3</say>", "<say>2</say>", "<say>1</say>"])
    _task(harness, "first task")
    _task(harness, "second task")
    _task(harness, "third task")
    _change(harness, "w000002", "important")
    _change(harness, "w000003", "interrupt")
    harness.settle()
    assert _promptOrder(harness, "first task", "second task", "third task") == [
        "third task", "second task", "first task"]


def test_continuation_carries_time_and_configured_pressure(makeHarness):
    harness = makeHarness(["<continue>need more</continue>", "<say>done</say>"],
                          settings={"timePressure": [{"afterSeconds": 0, "text": "PRESSURE after {seconds}s"}]})
    harness.run("question")
    first, second = harness.providerPrompts()
    assert "PRESSURE" not in first
    assert "You chose to keep thinking: need more" in second
    assert "Time spent thinking about it so far:" in second
    assert "PRESSURE after" in second
    assert _inference(harness, 2)["inputs"]["timePressure"].startswith("PRESSURE after")


def test_debugger_pause_is_not_counted_as_thinking_time(makeHarness):
    harness = makeHarness(["<continue>more</continue>", "<say>done</say>"])
    harness.driver.setBreakBeforeInference(True)
    harness.run("question")
    harness.advance(100)
    harness.continueRun()

    prompt = harness.pauses[1]["prompt"]
    assert "The first message arrived 100 seconds ago." in prompt
    assert "If you are ready, conclude." not in prompt
    assert _inference(harness, 2)["inputs"]["activeProcessingSeconds"] < 3
