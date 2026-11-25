# file: tests/first_party/applications/aiBuddy/test_checkpointA.py ; version: 6
"""Checkpoint A runtime contracts with a deterministic stub provider.

These tests verify orchestration, state and persistence mechanics. They do
not evaluate model behaviour; responses are scripted.
"""
from __future__ import annotations

import threading
import time

from backend.values import MISSING


def test_fresh_instance_has_authoritative_personality_and_empty_personal_memory(makeHarness):
    harness = makeHarness([])
    try:
        assert harness.state.load("aibuddy/personality")["name"] == "Buddy"
        assert harness.state.load("aibuddy/experiences/sequence") == 0
        assert harness.state.load("aibuddy/experiences/e000001") is MISSING
        assert harness.state.load("aibuddy/work/open") == []
        harness.settle()
        assert harness.providerPrompts() == []
        assert harness.spoken == []
    finally:
        harness.close()


def test_hello_breakpoint_edit_continue_and_exact_submitted_prompt(makeHarness):
    harness = makeHarness(["I should greet back. <say>Hello. Nice to meet you.</say>"])
    try:
        harness.driver.setBreakBeforeInference(True)
        harness.driver.submitInput("Hello")
        harness.settle()

        assert harness.providerPrompts() == []
        assert len(harness.pauses) == 1
        paused = harness.pauses[0]
        built = paused["prompt"]
        assert "You have received a message from the user" in built
        assert "<message>\nHello\n</message>" in built
        assert "You are Buddy" in built
        assert "Current time: 2026-11-24T21:00:00+01:00" in built

        edited = built + "\nBe brief."
        harness.driver.replacePrompt(edited)
        harness.continueRun()

        assert harness.providerPrompts() == [edited]
        record = harness.state.load(f"aibuddy/inferences/{paused['inferenceId']}")
        assert record["state"] == "completed"
        assert record["builtPrompt"] == built
        assert record["submittedPrompt"] == edited
        assert [item["kind"] for item in record["interventions"]] == ["prompt-replaced"]
        assert record["interventions"][0]["by"] == "developer"
        assert record["rawOutput"] == "I should greet back. <say>Hello. Nice to meet you.</say>"
        assert record["timing"]["durationNs"] >= 0
        assert harness.spoken == ["Hello. Nice to meet you."]
    finally:
        harness.close()


def test_only_deliberate_communication_becomes_speech_and_is_delivered_once(makeHarness):
    harness = makeHarness(["Thinking aloud here. <say>First.</say> more thought <say>Second.</say>"])
    try:
        harness.driver.submitInput("Hi")
        harness.settle()
        assert harness.spoken == ["First.", "Second."]
        record = harness.finished[0]
        assert "Thinking aloud here." in record["interpretation"]["thinking"]
        assert harness.state.load("aibuddy/outbox/delivered") == 2
        harness.settle()
        assert harness.spoken == ["First.", "Second."]
        communications = [harness.state.load(f"aibuddy/experiences/e{n:06d}") for n in (2, 3)]
        assert [item["kind"] for item in communications] == ["communication", "communication"]
        assert all(item["inferenceId"] == record["id"] for item in communications)
    finally:
        harness.close()


def test_declining_to_communicate_is_valid(makeHarness):
    harness = makeHarness(["The user only tested the keyboard. Nothing to say.",
                                 "<think><say>not deliberate</say></think>", "<say>unclosed"])
    try:
        harness.run("asdf", "x", "y")
        assert harness.spoken == []
        assert [record["state"] for record in harness.finished] == ["completed"] * 3
        assert harness.finished[2]["interpretation"]["warnings"] == ["unclosed-say-ignored"]
        assert harness.state.load("aibuddy/work/open") == []
    finally:
        harness.close()


def test_each_inference_gets_a_fresh_prompt_with_selected_rather_than_replayed_history(makeHarness):
    harness = makeHarness(["<say>reply-token-alpha</say>", "<say>reply-token-beta</say>"])
    try:
        harness.driver.submitInput("first message")
        harness.settle()
        harness.driver.submitInput("second message")
        harness.settle()
        first, second = harness.providerPrompts()
        assert "first message" in first and "second message" not in first
        assert not second.startswith(first)
        assert second.count("<message>") == 1
        assert "<message>\nsecond message\n</message>" in second
        assert "The user said: first message" in second
        assert "You said: reply-token-alpha" in second
    finally:
        harness.close()


def test_provider_failure_is_recorded_and_operation_continues(makeHarness):
    harness = makeHarness([{"fail": "server unreachable"}, "<say>back</say>"])
    try:
        harness.driver.submitInput("are you there?")
        harness.settle()
        assert harness.spoken == []
        failed = harness.finished[0]
        assert failed["state"] == "failed" and "server unreachable" in failed["error"]
        assert any("failed" in line for line in harness.diagnostics)
        harness.driver.submitInput("again")
        harness.settle()
        assert harness.spoken == ["back"]
    finally:
        harness.close()


def test_interrupting_a_running_inference_leaves_it_prepared_and_uncommitted(makeHarness):
    harness = makeHarness([{"block": True}, "<say>done</say>"])
    try:
        harness.driver.submitInput("think hard")
        worker = threading.Thread(target=harness.settle)
        worker.start()
        deadline = time.monotonic() + 5
        while harness.driver.partialOutputSoFar != "partial thought":
            assert time.monotonic() < deadline, "inference never produced partial output"
            time.sleep(0.01)
        assert harness.driver.interrupt() is True
        worker.join(timeout=5)
        assert not worker.is_alive()

        paused = harness.pauses[-1]
        record = harness.state.load(f"aibuddy/inferences/{paused['inferenceId']}")
        assert record["state"] == "prepared"
        assert harness.spoken == []
        assert harness.driver.lastPartialOutput == "partial thought"

        harness.continueRun()
        assert harness.spoken == ["done"]
        assert len(harness.providerPrompts()) == 2
    finally:
        harness.close()


def test_interrupt_never_cancels_bookkeeping_jobs(makeHarness):
    harness = makeHarness(["<say>ok</say>"])
    try:
        assert harness.driver.interrupt() is False
        harness.driver.submitInput("keep me")
        original = harness.runtime.runJob

        def runJobInterrupting(capabilityId, payload=None, *, job=None):
            if capabilityId == "aiBuddy.input.receive@1":
                assert harness.driver.interrupt() is False
            return original(capabilityId, payload, job=job)

        harness.runtime.runJob = runJobInterrupting
        harness.settle()
        assert harness.state.load("aibuddy/experiences/e000001")["text"] == "keep me"
        assert harness.spoken == ["ok"]
    finally:
        harness.close()


def test_save_and_restore_preserve_state_and_reoffer_prepared_inference(makeHarness):
    first = makeHarness(["<say>Hello.</say>"])
    applicationId = first.runtime.applicationRun.application.applicationId
    firstRunId = first.runtime.applicationRun.applicationRunId
    try:
        first.driver.submitInput("Hello")
        first.settle()
        first.driver.setBreakBeforeInference(True)
        first.driver.submitInput("Are you still there?")
        first.settle()
        pendingId = first.pauses[-1]["inferenceId"]
        first.driver.requestSave()
        first.settle()
    finally:
        first.close()

    second = makeHarness(["<say>Yes.</say>"], applicationId=applicationId)
    try:
        assert second.runtime.applicationRun.applicationRunId != firstRunId
        assert second.state.load("aibuddy/experiences/e000001")["text"] == "Hello"
        assert second.state.load("aibuddy/outbox/delivered") == 1
        second.settle()
        assert second.spoken == ["Yes."]
        assert second.finished[0]["id"] == pendingId
        assert len(second.providerPrompts()) == 2
    finally:
        second.close()


def test_breakpoint_mode_pauses_after_the_response_before_acting_on_it(makeHarness):
    harness = makeHarness(["Greeting back. <say>Hi.</say>"])
    harness.driver.setBreakBeforeInference(True)
    harness.run("Hello")
    assert len(harness.pauses) == 1 and harness.outputPauses == []

    harness.driver.continueExecution()
    harness.settle()
    assert len(harness.outputPauses) == 1
    paused = harness.outputPauses[0]
    assert paused["rawOutput"] == "Greeting back. <say>Hi.</say>"
    assert harness.spoken == []
    assert harness.state.load(f"aibuddy/inferences/{paused['inferenceId']}")["state"] == "generated"

    harness.driver.replaceOutput("Greeting back. <say>Edited hi.</say>")
    harness.driver.continueExecution()
    harness.settle()
    assert harness.spoken == ["Edited hi."]
    record = harness.state.load(f"aibuddy/inferences/{paused['inferenceId']}")
    assert record["state"] == "completed"
    assert record["generatedOutput"] == "Greeting back. <say>Hi.</say>"
    assert record["rawOutput"] == "Greeting back. <say>Edited hi.</say>"
    assert [(item["kind"], item["by"]) for item in record["interventions"]] == [("output-replaced", "developer")]


def test_generated_output_survives_restart_and_is_processed_without_a_new_inference(makeHarness):
    first = makeHarness(["<say>Still me.</say>"])
    applicationId = first.runtime.applicationRun.application.applicationId
    first.driver.setBreakBeforeInference(True)
    first.run("Hello")
    first.driver.continueExecution()
    first.settle()
    assert len(first.outputPauses) == 1 and first.spoken == []
    first.close()

    second = makeHarness([], applicationId=applicationId)
    second.settle()
    assert second.spoken == ["Still me."]
    assert len(second.providerPrompts()) == 1
