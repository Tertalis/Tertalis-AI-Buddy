# file: tests/first_party/applications/aiBuddy/test_checkpointD.py ; version: 4
"""Checkpoint D runtime contracts: independent activity and execution state.

Game boards are computed with the AppPack's own deterministic engine, so
scripted responses can make known-legal, known-safe or known-losing moves.
These tests verify mechanics, not whether a model can play.
"""
from __future__ import annotations

START = "aiBuddy.debug.game.start@1"
LIFECYCLE = "aiBuddy.debug.lifecycle.change@1"
GAME = "aiBuddy.game.minesweeper.state@1"


def _inference(harness, number: int) -> dict:
    return harness.state.load(f"aibuddy/inferences/i{number:06d}")


def _game(harness, number: int = 1) -> dict:
    return harness.state.load(f"aibuddy/games/g{number:06d}")


def _playableSeed(engine, width, height, mines, row=1, column=1):
    """First seed whose opening reveal at (row, column) leaves the game in progress."""
    for seed in range(1000):
        game = engine.newGame(gameId="g000001", width=width, height=height, mines=mines, seed=seed,
                              now="2026-11-24T21:00:00+01:00", workId=None)
        game, _outcome = engine.applyMove(game, "reveal", row, column, now="t", by="test")
        if game["status"] == "in-progress":
            return seed, game
    raise AssertionError("no playable seed found")


def _hiddenSafeCell(game):
    mines = {tuple(cell) for cell in game["mines"]}
    for r in range(1, game["height"] + 1):
        for c in range(1, game["width"] + 1):
            if game["visible"][r - 1][c - 1] == "#" and (r, c) not in mines:
                return r, c
    raise AssertionError("no hidden safe cell")


def test_game_operations_are_unavailable_without_a_game(makeHarness):
    harness = makeHarness(["<reveal>1,1</reveal><say>There is no game.</say>"])
    harness.run("reveal the corner")
    prompt = harness.providerPrompts()[0]
    assert "<reveal>row,column</reveal>" not in prompt
    assert "<activity>minesweeper</activity>" in prompt
    assert "game-action-not-available-ignored" in _inference(harness, 1)["interpretation"]["warnings"]
    assert harness.read(GAME) == {"available": False, "lastGameId": None}


def test_moves_after_the_game_ends_are_ignored(makeHarness, engine):
    mines = {tuple(cell) for cell in engine.placeMines(3, 3, 1, 7, 1, 1)}
    opened, _outcome = engine.applyMove(
        engine.newGame(gameId="g000001", width=3, height=3, mines=1, seed=7, now="t", workId=None),
        "reveal", 1, 1, now="t", by="test")
    assert opened["status"] == "won", "fixture assumption: the opening reveal wins this board"
    mine = next(iter(mines))
    harness = makeHarness([f"<reveal>1,1</reveal><reveal>{mine[0]},{mine[1]}</reveal>"])
    harness.developer(START, {"width": 3, "height": 3, "mines": 1, "seed": 7})
    assert _game(harness)["status"] == "won"
    assert len(_game(harness)["moves"]) == 1
    assert "moves-after-game-end-ignored" in _inference(harness, 1)["interpretation"]["warnings"]


def test_game_is_started_on_selection_and_can_be_won(makeHarness, engine):
    mines = {tuple(cell) for cell in engine.placeMines(3, 3, 1, 7, 1, 1)}
    safe = [(r, c) for r in range(1, 4) for c in range(1, 4) if (r, c) not in mines]
    moves = "".join(f"<reveal>{r},{c}</reveal>" for r, c in safe)
    harness = makeHarness([f"Playing. {moves}"], settings={"activity": {"movesPerStep": 10}})
    started = harness.developer(START, {"width": 3, "height": 3, "mines": 1, "seed": 7})

    assert started == {"workId": "w000001", "created": True}
    prompt = harness.providerPrompts()[0]
    assert "You are playing Minesweeper" in prompt
    assert "Minesweeper game g000001 (3x3, 1 mines)" in prompt
    assert "<reveal>row,column</reveal>" in prompt and "<stop>why</stop>" in prompt
    game = _game(harness)
    assert game["status"] == "won" and game["workIds"] == ["w000001"]
    work = harness.work("w000001")
    assert work["state"] == "completed" and work["endedReason"] == "game-won"
    assert harness.read(GAME)["available"] is False


def test_revealing_a_mine_loses_the_game(makeHarness, engine):
    seed, opened = _playableSeed(engine, 5, 5, 5)
    mine = tuple(opened["mines"][0])
    harness = makeHarness([f"<reveal>1,1</reveal><reveal>{mine[0]},{mine[1]}</reveal>"])
    harness.developer(START, {"width": 5, "height": 5, "mines": 5, "seed": seed})
    game = _game(harness)
    assert game["status"] == "lost"
    assert game["visible"][mine[0] - 1][mine[1] - 1] == "*"
    assert harness.work("w000001")["endedReason"] == "game-lost"


def test_illegal_moves_are_outcomes_and_idle_steps_end_the_activity(makeHarness, engine):
    seed, _opened = _playableSeed(engine, 5, 5, 5)
    harness = makeHarness([
        "<reveal>1,1</reveal><reveal>1,1</reveal><flag>99,99</flag>",
        "<unflag>1,1</unflag>",
        "I am not sure what to do.",
    ], settings={"activity": {"maxIdleSteps": 2}})
    harness.developer(START, {"width": 5, "height": 5, "mines": 5, "seed": seed})

    outcomes = [move["outcome"] for move in _game(harness)["moves"]]
    assert outcomes[0].startswith("revealed")
    assert outcomes[1:] == ["illegal: the cell is already revealed",
                            "illegal: row 99, column 99 is outside the 5x5 board",
                            "illegal: the cell is not flagged"]
    work = harness.work("w000001")
    assert work["endedReason"] == "no-progress" and work["idleSteps"] == 2
    assert _game(harness)["status"] == "in-progress"
    assert "You played reveal 1,1:" in harness.providerPrompts()[1]


def test_activity_yields_to_input_and_related_input_can_act_on_the_game(makeHarness, engine):
    seed, opened = _playableSeed(engine, 5, 5, 3)
    row, column = _hiddenSafeCell(opened)
    harness = makeHarness([
        "<reveal>1,1</reveal>",
        f"<say>Flagging that one.</say><flag>{row},{column}</flag>",
        "<stop>taking a break</stop>",
        "<say>It is evening.</say><activity>minesweeper</activity>",
        "<stop>later</stop>",
    ], settings={"activity": {"maxIdleSteps": 5}})
    harness.driver.developerJob(START, {"width": 5, "height": 5, "mines": 3, "seed": seed})
    harness.driver.runOnce()
    assert len(harness.providerPrompts()) == 1

    harness.run(f"flag {row},{column} for me")
    second = _inference(harness, 2)
    assert second["scheduling"]["selected"] == "w000002"
    assert any(entry["id"] == "w000001" and entry["eligible"] for entry in second["scheduling"]["candidates"])
    inputPrompt = harness.providerPrompts()[1]
    assert "Current activity: Minesweeper game g000001" in inputPrompt
    assert "<flag>row,column</flag>" in inputPrompt
    flagged = _game(harness)["moves"][-1]
    assert (flagged["op"], flagged["outcome"], flagged["inferenceId"]) == ("flag", "flagged", "i000002")

    gamePrompt = harness.providerPrompts()[2]
    assert f"- flag {row},{column} (you): flagged" in gamePrompt
    assert f"The user said: flag {row},{column} for me" in gamePrompt
    assert harness.work("w000001")["endedReason"] == "stopped-by-ai-buddy"

    harness.run("what time is it?")
    assert harness.state.load("aibuddy/games/sequence") == 1
    assert _game(harness)["workIds"] == ["w000001", "w000004"]
    assert harness.work("w000004")["origin"]["by"] == "ai-buddy"
    assert harness.spoken == ["Flagging that one.", "It is evening."]


def test_game_and_suspended_undertaking_survive_restart(makeHarness, engine):
    seed, _opened = _playableSeed(engine, 5, 5, 3)
    first = makeHarness(["<reveal>1,1</reveal>"], settings={"activity": {"maxIdleSteps": 5}})
    applicationId = first.runtime.applicationRun.application.applicationId
    first.driver.developerJob(START, {"width": 5, "height": 5, "mines": 3, "seed": seed})
    first.driver.runOnce()
    first.developer("aiBuddy.debug.work.change@1", {"workId": "w000001", "action": "suspend"})
    board = first.read(GAME)["board"]
    first.close()

    second = makeHarness(["<stop>resting my eyes</stop>"], applicationId=applicationId,
                         settings={"activity": {"maxIdleSteps": 5}})
    assert second.read(GAME)["board"] == board
    second.settle()
    assert len(second.providerPrompts()) == 1
    second.developer("aiBuddy.debug.work.change@1", {"workId": "w000001", "action": "resume"})
    resumed = second.providerPrompts()[-1]
    assert "\n".join(board) in resumed
    assert "The application was not running from about" in resumed
    assert second.work("w000001")["endedReason"] == "stopped-by-ai-buddy"


def test_explicit_inactivity_is_not_ended_by_queued_work_or_interrupts(makeHarness):
    harness = makeHarness(["<say>task</say>", "<say>hello</say>"],
                          settings={"periodic": [{"name": "heartbeat", "everySeconds": 10}]})
    harness.developer(LIFECYCLE, {"action": "deactivate"})
    harness.driver.submitInput("hello")
    harness.driver.developerJob("aiBuddy.debug.work.add@1", {"kind": "task", "purpose": "urgent task"})
    harness.settle()
    harness.developer("aiBuddy.debug.work.change@1", {"workId": "w000004", "action": "interrupt"})

    assert harness.providerPrompts() == []
    assert harness.state.load("aibuddy/experiences/e000001")["text"] == "hello"
    assert harness.work("w000001")["state"] == "completed"
    schedule = harness.read("aiBuddy.debug.schedule@1", {"now": harness.clock()})
    reasons = {entry["id"]: entry.get("reason") for entry in schedule["candidates"]}
    assert reasons["w000003"] == reasons["w000004"] == "AI Buddy is inactive"
    assert harness.read("aiBuddy.debug.status@1")["lifecycle"]["state"] == "inactive"

    harness.advance(600)
    harness.developer(LIFECYCLE, {"action": "activate"})
    first, second = harness.providerPrompts()
    assert "after being inactive since 2026-11-24T21:00:00+01:00 (10 minutes)" in first
    assert "after being inactive" not in second
    assert "urgent task" in first
    assert harness.spoken == ["task", "hello"]


def test_resting_lets_optional_work_wait_but_ends_for_queued_work(makeHarness):
    harness = makeHarness(["Nothing needs doing. <rest>nothing to do</rest>", "<say>done</say>",
                           "<stop>later</stop>"])
    harness.run("anything to do?")
    assert harness.read("aiBuddy.debug.status@1")["lifecycle"]["state"] == "resting"

    harness.developer(START, {})
    assert len(harness.providerPrompts()) == 1
    assert harness.state.load("aibuddy/games/sequence") == 0
    schedule = harness.read("aiBuddy.debug.schedule@1", {"now": harness.clock()})
    assert schedule["candidates"][0]["reason"] == "AI Buddy is resting; optional work waits"

    harness.developer("aiBuddy.debug.work.add@1", {"kind": "task", "purpose": "tidy notes"})
    prompts = harness.providerPrompts()
    assert "tidy notes" in prompts[1] and "You are playing Minesweeper" in prompts[2]
    lifecycle = harness.state.load("aibuddy/lifecycle")
    assert [(entry["to"], entry["by"]) for entry in lifecycle["history"]] == [
        ("resting", "ai-buddy"), ("active", "orchestration")]
    assert lifecycle["history"][-1]["reason"] == "work w000003 selected"


def test_periodic_requirement_escalates_a_waiting_game(makeHarness):
    harness = makeHarness(["<stop>enough for now</stop>"], settings={
        "periodic": [{"name": "daily-game", "everySeconds": 100, "establishes": "minesweeper"}]})
    harness.developer(LIFECYCLE, {"action": "rest"})
    assert harness.work("w000002")["kind"] == "minesweeper"
    assert harness.work("w000002")["important"] is False
    assert harness.providerPrompts() == []

    harness.advance(100)
    harness.settle()
    game = harness.work("w000002")
    assert game["important"] is True and game["escalations"][0]["by"] == "w000003"
    assert len(harness.providerPrompts()) == 1
    assert "You are playing Minesweeper" in harness.providerPrompts()[0]
    assert harness.state.load("aibuddy/lifecycle")["state"] == "active"


def test_quiescence_runs_nothing_and_restores_previous_activity(makeHarness):
    harness = makeHarness(["<say>back</say>"])
    harness.driver.quiesce()
    harness.settle()
    assert harness.read("aiBuddy.debug.status@1")["lifecycle"]["deactivatedBy"] == "quiescence"

    harness.driver.submitInput("are you there?")
    harness.settle()
    assert harness.state.load("aibuddy/experiences/sequence") == 0

    harness.driver.unquiesce()
    harness.settle()
    assert harness.read("aiBuddy.debug.status@1")["lifecycle"]["state"] == "active"
    assert harness.spoken == ["back"]

    harness.developer(LIFECYCLE, {"action": "deactivate"})
    harness.driver.quiesce()
    harness.settle()
    harness.driver.unquiesce()
    harness.settle()
    assert harness.read("aiBuddy.debug.status@1")["lifecycle"]["state"] == "inactive"


def test_game_moves_in_a_paused_response_are_applied_only_after_continue(makeHarness, engine):
    seed, _opened = _playableSeed(engine, 5, 5, 3)
    harness = makeHarness(["Corner first. <reveal>1,1</reveal>", "<stop>enough</stop>"],
                          settings={"activity": {"maxIdleSteps": 5}})
    harness.driver.setBreakBeforeInference(True)
    harness.driver.developerJob(START, {"width": 5, "height": 5, "mines": 3, "seed": seed})
    harness.settle()
    assert len(harness.pauses) == 1

    harness.driver.continueExecution()
    harness.settle()
    assert harness.outputPauses[0]["rawOutput"] == "Corner first. <reveal>1,1</reveal>"
    assert _game(harness)["moves"] == []

    harness.driver.continueExecution()
    harness.settle()
    moves = _game(harness)["moves"]
    assert len(moves) == 1 and moves[0]["op"] == "reveal" and moves[0]["outcome"].startswith("revealed")
    assert len(harness.pauses) == 2
