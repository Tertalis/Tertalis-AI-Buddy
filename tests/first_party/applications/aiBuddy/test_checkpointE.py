# file: tests/first_party/applications/aiBuddy/test_checkpointE.py ; version: 2
"""Checkpoint E runtime contracts: rules and procedural learning.

Scripted responses exercise rule establishment, revision, ordering,
disregard, protection and loss investigation. They verify mechanics and
permission enforcement, not whether a learned rule is any good.
"""
from __future__ import annotations

from backend.values import MISSING

START = "aiBuddy.debug.game.start@1"
INITIAL_RULE = "In Minesweeper a hidden cell is unknown, not a mine."


def _inference(harness, number: int) -> dict:
    return harness.state.load(f"aibuddy/inferences/i{number:06d}")


def _rule(harness, number: int) -> dict:
    return harness.state.load(f"aibuddy/rules/r{number:06d}")


def _playableSeed(engine, width, height, mines):
    """First seed whose opening reveal at 1,1 leaves the game in progress."""
    for seed in range(1000):
        game = engine.newGame(gameId="g000001", width=width, height=height, mines=mines, seed=seed,
                              now="t", workId=None)
        game, _outcome = engine.applyMove(game, "reveal", 1, 1, now="t", by="test")
        if game["status"] == "in-progress":
            return seed, game
    raise AssertionError("no playable seed found")


def _startGame(harness, engine, **settings):
    seed, opened = _playableSeed(engine, 5, 5, 5)
    harness.developer(START, {"width": 5, "height": 5, "mines": 5, "seed": seed})
    return opened


def test_initial_rule_is_protected_and_shown_by_scope(makeHarness, engine):
    harness = makeHarness(["<say>hi</say>", "<stop>ok</stop>"])
    rules = harness.read("aiBuddy.debug.rule.list@1")
    assert [(rule["id"], rule["scope"], rule["editable"], rule["origin"]["by"]) for rule in rules] == [
        ("r000001", "minesweeper", False, "developer")]

    harness.run("hello")
    plain = harness.providerPrompts()[0]
    assert "Rules you follow" not in plain
    assert "<rule>text</rule> - establish a rule for future occasions (general rules)." in plain

    _startGame(harness, engine)
    game = harness.providerPrompts()[1]
    assert "Rules you follow, in order (you may disregard one for this occasion):" in game
    assert f"1. {INITIAL_RULE}" in game and "(protected)" in game
    assert "(minesweeper rules)" in game
    assert _inference(harness, 2)["inputs"]["ruleIds"] == ["r000001"]


def test_ai_buddy_establishes_and_revises_its_own_rule(makeHarness, engine):
    harness = makeHarness(["<reveal>1,1</reveal><rule>Start in a corner.</rule>",
                           "<revise>2: Start in a corner, then work along the edges.</revise><stop>enough</stop>"])
    _startGame(harness, engine)

    learned = _rule(harness, 2)
    assert learned["editable"] is True and learned["scope"] == "minesweeper"
    assert learned["origin"]["by"] == "ai-buddy" and learned["origin"]["inferenceId"] == "i000001"
    second = harness.providerPrompts()[1]
    assert "2. Start in a corner.\n" in second
    assert "Rule change (establish): new rule r000002: Start in a corner." in second

    revised = _rule(harness, 2)
    assert revised["text"] == "Start in a corner, then work along the edges."
    assert [(item["by"], item["previousText"]) for item in revised["revisions"]] == [("ai-buddy", "Start in a corner.")]


def test_protected_rule_cannot_be_changed_by_ai_buddy(makeHarness, engine):
    harness = makeHarness(["<revise>1: Hidden cells are mines.</revise><retire>1: wrong</retire>"
                           "<reorder>1: 2</reorder><stop>no</stop>"])
    _startGame(harness, engine)

    rule = _rule(harness, 1)
    assert rule["text"].startswith(INITIAL_RULE) and rule["active"] is True
    assert rule["order"] == 10 and rule["revisions"] == []
    changes = _inference(harness, 1)["ruleChanges"]
    assert [(change["action"], change["applied"]) for change in changes] == [
        ("revise", False), ("reorder", False), ("retire", False)]
    assert "rule-protected-change-rejected" in _inference(harness, 1)["interpretation"]["warnings"]


def test_disregarding_a_rule_is_recorded_and_leaves_it_unchanged(makeHarness, engine):
    harness = makeHarness(["On a hunch. <disregard>1: testing a hunch</disregard><reveal>1,1</reveal>",
                           "<stop>done</stop>"])
    _startGame(harness, engine)

    assert _inference(harness, 1)["disregarded"] == [
        {"ruleId": "r000001", "text": _rule(harness, 1)["text"], "reason": "testing a hunch"}]
    assert _rule(harness, 1)["revisions"] == []
    assert "You disregarded a rule for that step:" in harness.providerPrompts()[1]
    assert "(testing a hunch)" in harness.providerPrompts()[1]


def test_reordering_own_rules_never_touches_protected_ones(makeHarness, engine):
    harness = makeHarness(["<rule>Alpha rule.</rule><rule>Beta rule.</rule><reveal>1,1</reveal>",
                           "<reorder>3: 1</reorder><stop>x</stop>"])
    _startGame(harness, engine)

    ordered = [rule["id"] for rule in harness.read("aiBuddy.debug.rule.list@1")]
    assert ordered == ["r000003", "r000001", "r000002"]
    assert _rule(harness, 1)["order"] == 10 and _rule(harness, 1)["revisions"] == []
    assert _rule(harness, 3)["revisions"][0]["previousOrder"] == 30


def test_a_loss_establishes_an_investigation_and_the_learned_rule_is_used_later(makeHarness, engine):
    seed, opened = _playableSeed(engine, 5, 5, 5)
    mine = opened["mines"][0]
    harness = makeHarness([
        f"<reveal>1,1</reveal><reveal>{mine[0]},{mine[1]}</reveal>",
        "I guessed. <rule>Reveal a cell only when the numbers prove it is safe.</rule>",
        "<stop>later</stop>",
    ])
    harness.developer(START, {"width": 5, "height": 5, "mines": 5, "seed": seed})

    investigation = harness.work("w000002")
    assert investigation["kind"] == "investigation" and investigation["investigates"] == "g000001"
    assert investigation["origin"]["by"] == "orchestration" and investigation["state"] == "completed"
    prompt = harness.providerPrompts()[1]
    assert f"You lost Minesweeper game g000001 when you revealed {mine[0]},{mine[1]}." in prompt
    assert " M" in prompt and " *" in prompt
    assert f"1. {INITIAL_RULE}" in prompt and "(minesweeper rules)" in prompt
    learned = _rule(harness, 2)
    assert learned["scope"] == "minesweeper" and learned["origin"]["investigates"] == "g000001"

    harness.developer(START, {"width": 5, "height": 5, "mines": 3, "seed": 1})
    assert harness.state.load("aibuddy/games/g000002") is not MISSING
    assert "2. Reveal a cell only when the numbers prove it is safe." in harness.providerPrompts()[2]


def test_developer_authority_and_editability(makeHarness):
    harness = makeHarness(["<say>a</say>", "<revise>1: Be brief and kind.</revise><say>b</say>"])
    harness.developer("aiBuddy.debug.rule.add@1", {"scope": "general", "text": "Be brief."})
    harness.run("hello")
    assert "1. Be brief. (protected)" in harness.providerPrompts()[0]

    harness.developer("aiBuddy.debug.rule.change@1", {"ruleId": "r000001", "action": "revise",
                                                      "text": "Hidden cells are unknown until proven."})
    assert _rule(harness, 1)["revisions"][0]["by"] == "developer"

    harness.developer("aiBuddy.debug.rule.change@1", {"ruleId": "r000002", "action": "editable", "value": True})
    harness.run("hi")
    rule = _rule(harness, 2)
    assert rule["text"] == "Be brief and kind."
    assert [(item["by"], item["action"]) for item in rule["revisions"]] == [
        ("developer", "editable"), ("ai-buddy", "revise")]


def test_migration_seeds_initial_rules_into_existing_instances(makeHarness):
    first = makeHarness([])
    applicationId = first.runtime.applicationRun.application.applicationId
    meta = first.state.load("aibuddy/meta")
    transaction = first.state.openTransaction()
    transaction.set("aibuddy/meta", {**meta, "schema": "aibuddy.state@4", "migrations": []})
    transaction.setAbsent("aibuddy/rules/sequence")
    transaction.setAbsent("aibuddy/rules/r000001")
    transaction.commit()
    first.runtime.saveApplication()
    first.close()

    second = makeHarness([], applicationId=applicationId)
    assert second.state.load("aibuddy/meta")["schema"] == "aibuddy.state@5"
    assert second.state.load("aibuddy/rules/sequence") == 1
    assert _rule(second, 1)["origin"] == {"by": "developer", "source": "pack rules.json"}
