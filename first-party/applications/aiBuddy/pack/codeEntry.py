# file: first-party/applications/aiBuddy/pack/codeEntry.py ; version: 9
"""AI Buddy AppPack CodeEntry (materialization Checkpoints A to F).

All AI Buddy state lives in the Application's committed Value System root
under ``aibuddy/``, so it is saved and restored by the platform SaveBundle
mechanism. Every mutating capability is intended to run as one Tertalis Job:
its writes become authoritative only when the Job commits.

State layout (schema aibuddy.state@5):
    aibuddy/meta                       schema, creation time, migrations
    aibuddy/lifecycle                  active / resting / inactive, notices
    aibuddy/personality                authoritative personality
    aibuddy/experiences/<eNNNNNN>      almost-literal experiences
    aibuddy/memories/<mNNNNNN>         deliberate memories, with origin
    aibuddy/situation/<sNNNNNN>        State / Important State information
    aibuddy/work/open                  ids of non-terminal things to do
    aibuddy/work/<wNNNNNN>             one thing to do and its working information
    aibuddy/inferences/<iNNNNNN>       diagnostic inference record
    aibuddy/outbox/<oNNNNNN>           one deliberate outgoing communication
    aibuddy/games/<gNNNNNN>            one Minesweeper game, independent of inference
    aibuddy/rules/<rNNNNNN>            one rule: an ordered, scoped instruction
    aibuddy/passive/cursor             how far passive memory processing has reviewed
    aibuddy/outbox/delivered           highest delivered outbox number
    (each collection also has a "sequence" counter)

Things to do are undertakings of an activity. Eligibility (may it advance
now?) is decided separately from selection (which eligible one advances?).
Both are deterministic here and their reasons are recorded. Inference may
be asked to choose through a Choice undertaking.

Personal memory material is experiences plus memories. Diagnostic records
are not retrieved as memory but are reachable through provenance.
Capabilities under ``aiBuddy.debug.`` are developer interventions; nothing
produced by inference is ever mapped to them.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
from datetime import datetime, timedelta, timezone

from backend.llm.llmTypes import LlmQuery
from backend.orchestration.cancellation import ExecutionCancelled
from backend.values import MISSING

STATE_SCHEMA = "aibuddy.state@5"
RESULT_CONTRACT = "aibuddy.result.tags@6"
RETRIEVAL_METHOD = "aibuddy.retrieval.lexical@1"
SCHEDULING_POLICY = "aibuddy.scheduling.tiers@1"

_META = "aibuddy/meta"
_PERSONALITY = "aibuddy/personality"
_LIFECYCLE = "aibuddy/lifecycle"
_PASSIVE_CURSOR = "aibuddy/passive/cursor"
_SEQUENCES = {
    "experience": ("aibuddy/experiences/sequence", "aibuddy/experiences", "e"),
    "memory": ("aibuddy/memories/sequence", "aibuddy/memories", "m"),
    "situation": ("aibuddy/situation/sequence", "aibuddy/situation", "s"),
    "work": ("aibuddy/work/sequence", "aibuddy/work", "w"),
    "inference": ("aibuddy/inferences/sequence", "aibuddy/inferences", "i"),
    "outbox": ("aibuddy/outbox/sequence", "aibuddy/outbox", "o"),
    "game": ("aibuddy/games/sequence", "aibuddy/games", "g"),
    "rule": ("aibuddy/rules/sequence", "aibuddy/rules", "r"),
}
_WORK_OPEN = "aibuddy/work/open"
_OUTBOX_DELIVERED = "aibuddy/outbox/delivered"

_MEMORY_DEFAULTS = {"recentLimit": 4, "automaticLimit": 5, "recallLimit": 5, "maxContinuations": 4}
_DEFAULT_TIME_PRESSURE = [
    {"afterSeconds": 3, "text": "You have spent {seconds} seconds thinking about this. If you are ready, conclude."},
    {"afterSeconds": 30, "text": "You have spent {seconds} seconds thinking about this. "
                                 "Unless it genuinely needs more thought, conclude now."},
    {"afterSeconds": 120, "text": "You have spent {seconds} seconds on this. Conclude now with what you have."},
]
_STATE_LIMIT = 3
_ACTIVITY_DEFAULTS = {"maxSteps": 100, "maxIdleSteps": 3, "movesPerStep": 3, "stepsInPrompt": 3,
                      "investigateLosses": 1}
_RULE_SCOPES = ("general", "minesweeper", "user-input", "task")
_PASSIVE_DEFAULTS = {"batchSize": 20, "maxCandidates": 3, "candidateLifetimeSeconds": 86400}
_MINESWEEPER_DEFAULTS = {"width": 9, "height": 9, "mines": 10}
_LIFECYCLE_HISTORY_LIMIT = 50

# Activities: how an undertaking is processed and which category it belongs to.
# "passive" processing may run while AI Buddy is inactive or resting; it must
# never express anything or activate AI Buddy.
ACTIVITIES = {
    "process-user-input": {"strategy": "inference", "category": "user-input", "passive": False},
    "task": {"strategy": "inference", "category": "task", "passive": False},
    "recheck": {"strategy": "inference", "category": "task", "passive": False},
    "choice": {"strategy": "inference", "category": "choice", "passive": False},
    "minesweeper": {"strategy": "inference", "category": "activity", "passive": False},
    "investigation": {"strategy": "inference", "category": "task", "passive": False},
    "reaction": {"strategy": "inference", "category": "task", "passive": False},
    "memory-processing": {"strategy": "inference", "category": "passive", "passive": True},
    "salient-subjects": {"strategy": "inference", "category": "passive", "passive": True},
    "periodic-check": {"strategy": "deterministic", "category": "periodic", "passive": True},
}
_ESTABLISHABLE_ACTIVITIES = ("minesweeper",)  # by AI Buddy with <activity>
_PERIODIC_ACTIVITIES = ("minesweeper", "memory-processing", "salient-subjects")
_PASSIVE_INFERENCE = ("memory-processing", "salient-subjects")
_TERMINAL = {"completed", "abandoned", "expired", "failed"}
_TIER_NAMES = ("interrupt directive", "user input", "important", "normal", "optional")

_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)
_ACTION_TAGS = ("say", "recall", "remember", "continue", "intend", "choose", "stop", "rest", "activity",
                "rule", "revise", "reorder", "retire", "disregard", "react", "candidate")
_GAME_OPS = re.compile(r"<(reveal|flag|unflag)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small state helpers
# ---------------------------------------------------------------------------

def _load(view, address, default=None):
    """Loads one value, mapping an absent value to default."""
    value = view.load(address)
    return default if value is MISSING else value


def _itemAddress(kind: str, number: int) -> tuple[str, str]:
    """Returns (identifier, address) for one sequenced record."""
    _sequence, base, prefix = _SEQUENCES[kind]
    identifier = f"{prefix}{number:06d}"
    return identifier, f"{base}/{identifier}"


def _addressOf(identifier: str) -> str:
    """Maps a record identifier back to its address."""
    if type(identifier) is not str or len(identifier) != 7 or not identifier[1:].isdigit():
        raise ValueError(f"Invalid AI Buddy record identifier: {identifier!r}.")
    for _sequence, base, prefix in _SEQUENCES.values():
        if identifier[0] == prefix:
            return f"{base}/{identifier}"
    raise ValueError(f"Unknown AI Buddy record identifier: {identifier!r}.")


def _allocate(transaction, kind: str) -> tuple[str, str]:
    """Allocates the next identifier of one record kind inside transaction."""
    sequenceAddress = _SEQUENCES[kind][0]
    number = _load(transaction, sequenceAddress, 0) + 1
    transaction.set(sequenceAddress, number)
    return _itemAddress(kind, number)


def _now(payload) -> str:
    """Returns the orchestration-supplied time, or the local time when absent."""
    if isinstance(payload, dict) and type(payload.get("now")) is str:
        return payload["now"]
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parseTime(value: str) -> datetime:
    """Parses an ISO-8601 time; naive values are treated as UTC."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _secondsBetween(earlier: str, later: str) -> float:
    """Returns elapsed seconds from earlier to later."""
    return (_parseTime(later) - _parseTime(earlier)).total_seconds()


def _shift(value: str, seconds: float) -> str:
    """Returns value shifted by seconds, keeping its offset."""
    return (_parseTime(value) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _sha256(text: str) -> str:
    """Returns the hex SHA-256 digest of UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _requirePayload(payload, *keys):
    """Requires a dict payload containing the named keys."""
    if not isinstance(payload, dict):
        raise TypeError("AI Buddy capability payload must be an object.")
    missing = [key for key in keys if key not in payload]
    if missing:
        raise ValueError(f"AI Buddy capability payload is missing: {', '.join(missing)}.")
    return payload


def _settings(ctx) -> dict:
    """Returns AI Buddy runtime configuration with defaults."""
    raw = ctx.config.get("aiBuddy", {})
    if not isinstance(raw, dict):
        raise TypeError("aiBuddy configuration must be an object.")
    providerName = raw.get("providerName", "llama.cpp")
    if type(providerName) is not str or not providerName:
        raise ValueError("aiBuddy.providerName must be a non-empty string.")
    model = raw.get("model")
    if model is not None and (type(model) is not str or not model):
        raise ValueError("aiBuddy.model must be a non-empty string or null.")
    providerOptions = raw.get("providerOptions", {})
    if not isinstance(providerOptions, dict):
        raise TypeError("aiBuddy.providerOptions must be an object.")
    memory = dict(_MEMORY_DEFAULTS)
    rawMemory = raw.get("memory", {})
    if not isinstance(rawMemory, dict):
        raise TypeError("aiBuddy.memory must be an object.")
    for key, value in rawMemory.items():
        if key not in _MEMORY_DEFAULTS:
            raise ValueError(f"Unknown aiBuddy.memory setting {key!r}.")
        if type(value) is not int or value < 0:
            raise ValueError(f"aiBuddy.memory.{key} must be a non-negative integer.")
        memory[key] = value
    pressure = raw.get("timePressure", _DEFAULT_TIME_PRESSURE)
    if not isinstance(pressure, list) or not all(
            isinstance(item, dict) and isinstance(item.get("afterSeconds"), (int, float))
            and type(item.get("text")) is str for item in pressure):
        raise ValueError("aiBuddy.timePressure must be a list of {afterSeconds, text}.")
    pressureCategories = raw.get("timePressureCategories", ["user-input"])
    periodic = raw.get("periodic", [])
    if not isinstance(periodic, list) or not all(
            isinstance(item, dict) and type(item.get("name")) is str and item["name"]
            and isinstance(item.get("everySeconds"), (int, float)) and item["everySeconds"] > 0
            for item in periodic):
        raise ValueError("aiBuddy.periodic must be a list of {name, everySeconds}.")
    for entry in periodic:
        if entry.get("establishes") not in (None, *_PERIODIC_ACTIVITIES):
            raise ValueError(f"aiBuddy.periodic {entry['name']!r} establishes an unknown activity.")
    activity = dict(_ACTIVITY_DEFAULTS)
    for key, value in raw.get("activity", {}).items():
        if key not in _ACTIVITY_DEFAULTS or type(value) is not int or value < (0 if key == "investigateLosses" else 1):
            raise ValueError(f"Invalid aiBuddy.activity setting {key!r}.")
        activity[key] = value
    minesweeper = dict(_MINESWEEPER_DEFAULTS)
    for key, value in raw.get("minesweeper", {}).items():
        if key not in _MINESWEEPER_DEFAULTS or type(value) is not int or value < 1:
            raise ValueError(f"Invalid aiBuddy.minesweeper setting {key!r}.")
        minesweeper[key] = value
    passive = dict(_PASSIVE_DEFAULTS)
    for key, value in raw.get("passive", {}).items():
        if key not in _PASSIVE_DEFAULTS or type(value) is not int or value < (0 if key == "candidateLifetimeSeconds" else 1):
            raise ValueError(f"Invalid aiBuddy.passive setting {key!r}.")
        passive[key] = value
    lifetime = raw.get("intentionLifetimeSeconds")
    if lifetime is not None and (not isinstance(lifetime, (int, float)) or lifetime <= 0):
        raise ValueError("aiBuddy.intentionLifetimeSeconds must be positive or null.")
    return {"providerName": providerName, "model": model, "providerOptions": providerOptions,
            "memory": memory, "timePressure": sorted(pressure, key=lambda item: item["afterSeconds"]),
            "timePressureCategories": list(pressureCategories), "periodic": periodic,
            "intentionLifetimeSeconds": lifetime, "activity": activity, "minesweeper": minesweeper,
            "passive": passive}


# ---------------------------------------------------------------------------
# Things to do
# ---------------------------------------------------------------------------

def _workDefaults(work: dict) -> dict:
    """Fills fields absent from records written by earlier schema versions."""
    kind = work["kind"]
    defaults = {
        "category": ACTIVITIES.get(kind, {}).get("category", "task"),
        "purpose": None,
        "origin": {"by": "user-input"} if kind == "process-user-input" else {"by": "unknown"},
        "important": False,
        "optional": False,
        "interrupt": False,
        "dependsOn": [],
        "notBefore": None,
        "expiresAt": None,
        "inputExperienceIds": [],
        "inferenceIds": [],
        "steps": [],
        "shownMemoryIds": [],
        "continuations": 0,
        "activeProcessingNs": 0,
        "startedAt": None,
        "lastAdvancedAt": None,
        "amendments": [],
    }
    return {**defaults, **work}


def _work(view, workId: str) -> dict:
    """Loads one thing to do with defaults applied."""
    return _workDefaults(_load(view, _addressOf(workId)))


def _openWork(view) -> list[dict]:
    """Returns open things to do in establishment order."""
    return [_work(view, workId) for workId in _load(view, _WORK_OPEN, [])]


def _lastInference(view, work):
    """Returns the latest inference record of work, or None."""
    return _load(view, _addressOf(work["inferenceIds"][-1])) if work["inferenceIds"] else None


def _newWork(transaction, *, kind: str, now: str, origin: dict, purpose: str | None = None,
             important: bool = False, optional: bool = False, dependsOn=(), notBefore: str | None = None,
             expiresAt: str | None = None, inputExperienceIds=(), extra: dict | None = None) -> str:
    """Establishes one pending thing to do (the Queue directive)."""
    if kind not in ACTIVITIES:
        raise ValueError(f"Unknown activity {kind!r}.")
    for dependency in dependsOn:
        if _load(transaction, _addressOf(dependency)) is None:
            raise ValueError(f"Unknown dependency {dependency!r}.")
    workId, address = _allocate(transaction, "work")
    transaction.set(address, _workDefaults({
        "id": workId, "kind": kind, "state": "pending", "createdAt": now, "origin": origin,
        "purpose": purpose, "important": important, "optional": optional,
        "dependsOn": list(dependsOn), "notBefore": notBefore, "expiresAt": expiresAt,
        "inputExperienceIds": list(inputExperienceIds), **(extra or {}),
    }))
    transaction.set(_WORK_OPEN, [*_load(transaction, _WORK_OPEN, []), workId])
    return workId


def _closeWork(transaction, work, *, state, reason, at=None):
    """Ends one thing to do and removes it from the open index."""
    transaction.set(_addressOf(work["id"]), {**work, "state": state, "endedReason": reason,
                                             "endedAt": at or _now({})})
    transaction.set(_WORK_OPEN, [item for item in _load(transaction, _WORK_OPEN, []) if item != work["id"]])


def evaluateSchedule(view, now: str) -> dict:
    """Decides eligibility and selection for all open things to do.

    Returns {"policy", "now", "lifecycle", "candidates", "selected", "reason",
    "expire"}. Each candidate records whether it is eligible and why not, and
    eligible candidates record their tier. Selection: lowest tier first;
    within a tier the least recently advanced (never advanced first), then
    oldest. While AI Buddy is inactive only passive work is eligible; while
    it rests, optional non-passive work waits.
    """
    lifecycle = _lifecycle(view)["state"]
    candidates = []
    expire = []
    for work in _openWork(view):
        entry = {"id": work["id"], "kind": work["kind"], "purpose": work["purpose"], "state": work["state"]}
        reason = None
        if work["state"] == "suspended":
            reason = "suspended"
        elif work["startedAt"] is None and work["expiresAt"] and _secondsBetween(work["expiresAt"], now) >= 0:
            reason = "expired"
            expire.append(work["id"])
        else:
            for dependency in work["dependsOn"]:
                state = _load(view, _addressOf(dependency))["state"]
                if state != "completed":
                    reason = f"waiting for {dependency} ({state})"
                    break
            if reason is None and work["notBefore"] and _secondsBetween(now, work["notBefore"]) > 0:
                reason = f"scheduled for {work['notBefore']}"
        if reason is None:
            if work["interrupt"]:
                tier = 0
            elif work["category"] == "user-input":
                tier = 1
            elif work["important"]:
                tier = 2
            elif work["optional"]:
                tier = 4
            else:
                tier = 3
            passive = ACTIVITIES[work["kind"]]["passive"]
            if lifecycle == "inactive" and not passive:
                reason = "AI Buddy is inactive"
            elif lifecycle == "resting" and not passive and tier == 4:
                reason = "AI Buddy is resting; optional work waits"
        if reason is None:
            entry.update({"eligible": True, "tier": tier, "tierName": _TIER_NAMES[tier],
                          "lastAdvancedAt": work["lastAdvancedAt"],
                          "passive": ACTIVITIES[work["kind"]]["passive"]})
        else:
            entry.update({"eligible": False, "reason": reason})
        candidates.append(entry)
    eligible = [entry for entry in candidates if entry["eligible"]]
    eligible.sort(key=lambda entry: (entry["tier"], entry["lastAdvancedAt"] is not None,
                                     entry["lastAdvancedAt"] or "", entry["id"]))
    selected = eligible[0]["id"] if eligible else None
    reason = None
    if selected is not None:
        first = eligible[0]
        others = [entry["id"] for entry in eligible[1:]]
        reason = f"{first['tierName']}; " + (
            "least recently advanced of its tier" if any(entry["tier"] == first["tier"] for entry in eligible[1:])
            else "highest eligible tier") + (f"; also eligible: {', '.join(others)}" if others else "")
    return {"policy": SCHEDULING_POLICY, "now": now, "lifecycle": lifecycle, "candidates": candidates,
            "selected": selected, "reason": reason, "expire": expire}


# ---------------------------------------------------------------------------
# Minesweeper engine (deterministic; game state is independent of inference)
# ---------------------------------------------------------------------------

def _neighbours(row: int, column: int, height: int, width: int):
    """Yields the in-bounds neighbours of a 1-based cell."""
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if (dr or dc) and 1 <= row + dr <= height and 1 <= column + dc <= width:
                yield row + dr, column + dc


def placeMines(width: int, height: int, mines: int, seed: int, row: int, column: int) -> list[list[int]]:
    """Places mines deterministically, keeping the first revealed cell safe.

    The first cell and its neighbours are kept free when the board allows;
    otherwise only the first cell is. Returns sorted 1-based [row, column].
    """
    cells = [(r, c) for r in range(1, height + 1) for c in range(1, width + 1)]
    protected = {(row, column), *_neighbours(row, column, height, width)}
    candidates = [cell for cell in cells if cell not in protected]
    if len(candidates) < mines:
        candidates = [cell for cell in cells if cell != (row, column)]
    chosen = random.Random(f"{seed}:{row}:{column}").sample(candidates, mines)
    return sorted([r, c] for r, c in chosen)


def newGame(*, gameId: str, width: int, height: int, mines: int, seed: int, now: str, workId: str | None) -> dict:
    """Returns a new game record with every cell hidden."""
    if mines >= width * height:
        raise ValueError("A Minesweeper game needs fewer mines than cells.")
    return {"id": gameId, "width": width, "height": height, "mineCount": mines, "seed": seed,
            "mines": None, "visible": ["#" * width for _ in range(height)], "status": "in-progress",
            "moves": [], "startedAt": now, "endedAt": None, "workIds": [workId] if workId else []}


def _count(game: dict, row: int, column: int, mines: set) -> int:
    return sum((r, c) in mines for r, c in _neighbours(row, column, game["height"], game["width"]))


def applyMove(game: dict, op: str, row: int, column: int, *, now: str, by: str) -> tuple[dict, str]:
    """Applies one operation and returns (updated game, outcome).

    Illegal operations change nothing except the move history; their outcome
    says why. Operations on an ended game are illegal.
    """
    def finish(updated, outcome):
        move = {"op": op, "row": row, "column": column, "outcome": outcome, "at": now, "by": by}
        return {**updated, "moves": [*game["moves"], move]}, outcome

    if game["status"] != "in-progress":
        return finish(game, f"illegal: the game is already {game['status']}")
    if not (1 <= row <= game["height"] and 1 <= column <= game["width"]):
        return finish(game, f"illegal: row {row}, column {column} is outside the {game['height']}x{game['width']} board")
    grid = [list(line) for line in game["visible"]]
    cell = grid[row - 1][column - 1]
    if op == "flag":
        if cell != "#":
            return finish(game, "illegal: only a hidden cell can be flagged")
        grid[row - 1][column - 1] = "F"
        return finish({**game, "visible": ["".join(line) for line in grid]}, "flagged")
    if op == "unflag":
        if cell != "F":
            return finish(game, "illegal: the cell is not flagged")
        grid[row - 1][column - 1] = "#"
        return finish({**game, "visible": ["".join(line) for line in grid]}, "unflagged")
    if op != "reveal":
        return finish(game, f"illegal: unknown operation {op!r}")
    if cell == "F":
        return finish(game, "illegal: the cell is flagged; unflag it first")
    if cell != "#":
        return finish(game, "illegal: the cell is already revealed")

    minesList = game["mines"] or placeMines(game["width"], game["height"], game["mineCount"], game["seed"], row, column)
    mines = {(r, c) for r, c in minesList}
    updated = {**game, "mines": minesList}
    if (row, column) in mines:
        grid[row - 1][column - 1] = "*"
        updated.update(visible=["".join(line) for line in grid], status="lost", endedAt=now)
        return finish(updated, "hit a mine: the game is lost")
    revealed = 0
    pending = [(row, column)]
    while pending:
        r, c = pending.pop()
        if grid[r - 1][c - 1] != "#":
            continue
        count = _count(game, r, c, mines)
        grid[r - 1][c - 1] = "." if count == 0 else str(count)
        revealed += 1
        if count == 0:
            pending.extend(cell for cell in _neighbours(r, c, game["height"], game["width"])
                           if grid[cell[0] - 1][cell[1] - 1] == "#")
    updated["visible"] = ["".join(line) for line in grid]
    hiddenSafe = sum(1 for r in range(1, game["height"] + 1) for c in range(1, game["width"] + 1)
                     if updated["visible"][r - 1][c - 1] in "#F" and (r, c) not in mines)
    shown = updated["visible"][row - 1][column - 1]
    outcome = f"revealed {revealed} cell{'s' if revealed != 1 else ''}" + (
        f" (this cell shows {shown})" if shown != "." else " (this cell has no neighbouring mines)")
    if hiddenSafe == 0:
        updated.update(status="won", endedAt=now)
        outcome += "; every safe cell is revealed: the game is won"
    return finish(updated, outcome)


def renderBoard(game: dict, *, showMines: bool = False) -> list[str]:
    """Renders the visible board with 1-based row and column numbers."""
    width = game["width"]
    mines = {(r, c) for r, c in (game["mines"] or [])} if showMines else set()
    header = "     " + " ".join(f"{c:>2}" for c in range(1, width + 1))
    lines = [header]
    for r in range(1, game["height"] + 1):
        cells = []
        for c in range(1, width + 1):
            value = game["visible"][r - 1][c - 1]
            if showMines and value in "#F" and (r, c) in mines:
                value = "M" if value == "#" else "F!"
            cells.append(f"{value:>2}")
        lines.append(f"{r:>3}  " + " ".join(cells))
    return lines


def _parseCell(text: str) -> tuple[int, int] | None:
    """Parses "row,column" (or "row column") into 1-based integers."""
    numbers = re.findall(r"\d+", text)
    return (int(numbers[0]), int(numbers[1])) if len(numbers) == 2 else None


def _currentGame(view) -> dict | None:
    """Returns the game in progress, if any (one ongoing game at a time)."""
    last = _load(view, _SEQUENCES["game"][0], 0)
    game = _load(view, _itemAddress("game", last)[1]) if last else None
    return game if game is not None and game["status"] == "in-progress" else None


def _gameSummary(game: dict) -> str:
    flags = sum(line.count("F") for line in game["visible"])
    return (f"Minesweeper game {game['id']} ({game['height']}x{game['width']}, {game['mineCount']} mines), "
            f"{len(game['moves'])} moves so far, {flags} flags placed, started {game['startedAt']}")


# ---------------------------------------------------------------------------
# Rules (ordered, scoped instructions; protected unless editable)
# ---------------------------------------------------------------------------

def _rules(view, *, includeRetired: bool = False) -> list[dict]:
    """Returns rules in order (then identifier)."""
    rules = _collection(view, "rule")
    if not includeRetired:
        rules = [rule for rule in rules if rule["active"]]
    return sorted(rules, key=lambda rule: (rule["order"], rule["id"]))


def _ruleScopes(work: dict, gameAvailable: bool) -> list[str]:
    """Returns the rule scopes that apply to one step of an undertaking."""
    scopes = ["general", work["category"]]
    if work["kind"] in ("minesweeper", "investigation") or gameAvailable:
        scopes.append("minesweeper")
    return scopes


def _ruleScopeFor(work: dict) -> str:
    """Returns the scope given to a rule AI Buddy establishes in this undertaking."""
    return "minesweeper" if work["kind"] in ("minesweeper", "investigation") else "general"


def _newRule(transaction, *, text: str, scope: str, editable: bool, origin: dict, now: str,
             order: float | None = None) -> str:
    """Establishes one rule; without an order it goes after the rules of its scope."""
    if scope not in _RULE_SCOPES:
        raise ValueError(f"Unknown rule scope {scope!r}; expected one of {', '.join(_RULE_SCOPES)}.")
    if type(text) is not str or not text.strip():
        raise ValueError("A rule needs text.")
    if order is None:
        orders = [rule["order"] for rule in _rules(transaction) if rule["scope"] == scope]
        order = (max(orders) if orders else 0) + 10
    ruleId, address = _allocate(transaction, "rule")
    transaction.set(address, {"id": ruleId, "text": text.strip(), "scope": scope, "order": order,
                              "editable": editable, "active": True, "createdAt": now, "origin": origin,
                              "revisions": []})
    return ruleId


def _orderBetween(displayed: list[dict], moving: dict, position: int) -> float:
    """Returns an order value placing moving at 1-based position among displayed.

    Only the moved rule gets a new value, so no other rule (protected or
    not) is modified by a reorder.
    """
    others = [rule for rule in displayed if rule["id"] != moving["id"]]
    position = max(1, min(position, len(others) + 1))
    before = others[position - 2]["order"] if position >= 2 else None
    after = others[position - 1]["order"] if position - 1 < len(others) else None
    if before is None and after is None:
        return moving["order"]
    if before is None:
        return after - 10
    if after is None:
        return before + 10
    return (before + after) / 2


def _numbered(text: str) -> tuple[int, str] | None:
    """Parses "N: rest" (or "N rest") into (N, rest)."""
    match = re.match(r"\s*(\d+)\s*[:.)-]?\s*(.*)\Z", text, re.DOTALL)
    return (int(match.group(1)), match.group(2).strip()) if match else None


def _seedRules(ctx, transaction, now: str) -> list[str]:
    """Establishes the AppPack's initial developer rules (protected unless marked editable)."""
    path = ctx.packRoot / "rules.json"
    if not path.exists():
        return []
    entries = ctx.io.readJson(path).get("rules")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError('pack rules.json must be {"rules": [{"text": ..., "scope": ..., "editable": ...}, ...]}.')
    created = []
    for entry in entries:
        created.append(_newRule(transaction, text=entry["text"], scope=entry.get("scope", "general"),
                                editable=bool(entry.get("editable", False)),
                                origin={"by": "developer", "source": "pack rules.json"}, now=now))
    return created


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _lifecycle(view) -> dict:
    """Returns the lifecycle record, defaulting to active."""
    return _load(view, _LIFECYCLE) or {"state": "active", "since": None, "reason": None, "history": [],
                                        "notices": [], "lastSeenAt": None}


def _setLifecycle(transaction, record: dict, state: str, *, now: str, by: str, reason: str) -> dict:
    """Records one lifecycle transition and returns the updated record."""
    entry = {"from": record["state"], "to": state, "at": now, "by": by, "reason": reason}
    updated = {**record, "state": state, "since": now, "reason": reason, "changedBy": by,
               "history": [*record.get("history", []), entry][-_LIFECYCLE_HISTORY_LIMIT:]}
    transaction.set(_LIFECYCLE, updated)
    return updated


def _duration(seconds: float) -> str:
    """Renders an approximate duration for a prompt."""
    seconds = max(0.0, seconds)
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            value = round(seconds / size, 1)
            value = int(value) if value == int(value) else value
            return f"{value} {unit}{'s' if value != 1 else ''}"
    return f"{int(seconds)} second{'s' if int(seconds) != 1 else ''}"


def _noticeLines(notices: list[dict], now: str) -> list[str]:
    """Renders pending lifecycle notices (activation, restart) for one prompt."""
    lines = []
    for notice in notices:
        if notice["kind"] == "activation":
            since = notice.get("inactiveSince")
            if since:
                lines.append(f"You became active at {notice['at']} after being inactive since {since} "
                             f"({_duration(_secondsBetween(since, notice['at']))}).")
            else:
                lines.append(f"You became active at {notice['at']}.")
        elif notice["kind"] == "restart":
            last = notice.get("lastSeenAt")
            if last:
                lines.append(f"The application was not running from about {last} until it restarted "
                             f"(about {_duration(_secondsBetween(last, now))}).")
            else:
                lines.append("The application has restarted.")
    return lines


# ---------------------------------------------------------------------------
# Memory material and retrieval
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset("""
a about above after again all am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers herself
him himself his how i if in into is it its itself just me more most my myself no nor not now of off on once only
or other our ours ourselves out over own same she should so some such than that the their theirs them themselves
then there these they this those through to too under until up very was we were what when where which while who
whom why will with would you your yours yourself yourselves
""".split())


def _terms(text: str) -> set[str]:
    """Returns the lowercase content terms of text."""
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in _STOPWORDS and len(word) > 1}


def _collection(view, kind: str) -> list[dict]:
    """Returns all records of one sequenced collection, oldest first."""
    return [_load(view, _itemAddress(kind, number)[1]) for number in range(1, _load(view, _SEQUENCES[kind][0], 0) + 1)]


def _memoryMaterial(view) -> list[dict]:
    """Returns all retrievable memory material: experiences, then memories."""
    return _collection(view, "experience") + _collection(view, "memory")


def _workOf(item: dict) -> str | None:
    """Returns the thing to do whose processing produced item, if any."""
    return item.get("workId") or item.get("origin", {}).get("workId")


def _orderKey(item: dict) -> tuple[str, str]:
    """Orders material by its own time, then identifier."""
    return (item.get("receivedAt") or item.get("at") or item.get("establishedAt") or "", item["id"])


def retrieve(material: list[dict], query: str, *, limit: int, exclude=()) -> list[dict]:
    """Ranks material by shared rare terms with query; recency breaks ties.

    Returns at most limit results as {"id", "score"} with score > 0. This is
    deliberately simple and replaceable; relevance is not guaranteed.
    """
    queryTerms = _terms(query)
    if not queryTerms or limit <= 0:
        return []
    documents = [(item, _terms(item["text"])) for item in material]
    count = len(documents)
    frequency: dict[str, int] = {}
    for _item, terms in documents:
        for term in terms & queryTerms:
            frequency[term] = frequency.get(term, 0) + 1
    excluded = set(exclude)
    scored = []
    for item, terms in documents:
        if item["id"] in excluded:
            continue
        shared = terms & queryTerms
        if not shared:
            continue
        score = sum(math.log(1 + count / frequency[term]) for term in shared)
        scored.append((score, _orderKey(item), item["id"]))
    scored.sort(reverse=True)
    return [{"id": identifier, "score": round(score, 4)} for score, _order, identifier in scored[:limit]]


def describeMaterial(item: dict) -> str:
    """Renders one memory item with its orientation for a prompt."""
    if item["kind"] == "user-message":
        return f"({item['receivedAt']}) The user said: {item['text']}"
    if item["kind"] == "communication":
        return f"({item['at']}) You said: {item['text']}"
    if item.get("origin", {}).get("by") == "developer":
        return f"({item['establishedAt']}) Noted for you by the developer: {item['text']}"
    if item["kind"] == "processed":
        return f"({item['establishedAt']}) A conclusion you reached while reflecting: {item['text']}"
    return f"({item['establishedAt']}) You decided to remember: {item['text']}"


# ---------------------------------------------------------------------------
# Prompt construction and result interpretation
# ---------------------------------------------------------------------------

def _originPhrase(work: dict) -> str:
    """Describes who established an undertaking."""
    by = work["origin"].get("by")
    if by == "ai-buddy":
        return "You decided earlier to do this"
    if by == "developer":
        return "The developer asked you to do this"
    return "This was established for you"


def _situationLines(work: dict, byId: dict, related: dict) -> list[str]:
    """Describes what this undertaking is about."""
    kind = work["kind"]
    lines = ["Situation:"]
    if kind == "process-user-input":
        messages = [byId[identifier] for identifier in work["inputExperienceIds"]]
        lateIds = {item["experienceId"] for item in work["amendments"] if item.get("afterStart")}
        if len(messages) == 1:
            lines.append(f"You have received a message from the user (received {messages[0]['receivedAt']}):")
        else:
            lines.append(f"You have received {len(messages)} messages from the user, in order:")
        for message in messages:
            if len(messages) > 1:
                note = "; it arrived while you were already working on this" if message["id"] in lateIds else ""
                lines.append(f"(received {message['receivedAt']}{note})")
            lines += ["<message>", message["text"], "</message>"]
    elif kind == "recheck":
        original = related["original"]
        lines.append(f"Earlier (finished {original.get('endedAt', 'earlier')}) you handled something. "
                     "Reconsider whether what you concluded and said was right and sufficient.")
        for identifier in original["inputExperienceIds"]:
            lines.append(f"- {describeMaterial(byId[identifier])}")
        for step in original["steps"]:
            for text in step.get("communications", []):
                lines.append(f"- You said: {text}")
        finalThinking = next((step.get("thinking") for step in reversed(original["steps"]) if step.get("thinking")), None)
        if finalThinking:
            lines.append(f"- Your final thinking then: {finalThinking}")
        if work["purpose"]:
            lines.append(f"Focus: {work['purpose']}")
        lines.append("If you find a problem, you may tell the user. Finding nothing wrong is also a valid outcome.")
    elif kind == "choice":
        lines.append("Choose which of these undertakings to do next.")
        for number, candidate in enumerate(related["candidates"], start=1):
            lines.append(f"{number}. {candidate['purpose'] or candidate['kind']} ({_originPhrase(candidate).lower()})")
        if work["purpose"]:
            lines.append(f"Consideration: {work['purpose']}")
        lines.append("Write <choose>number</choose> for your choice, or choose none if none should be done next.")
    elif kind == "minesweeper":
        game = related["game"]
        lines += [
            f"You are playing Minesweeper ({_originPhrase(work).lower()}).",
            _gameSummary(game) + ".",
            "Rules: revealing a cell uncovers it; a number shows how many of its up to 8 neighbouring cells hold "
            "mines; revealing a mine loses the game; revealing every safe cell wins. Flags mark cells you believe "
            "are mines. Rows and columns are numbered from 1.",
            "Board (# hidden, F flagged, . no neighbouring mines, digits count neighbouring mines):",
            *renderBoard(game),
        ]
        moves = game["moves"][-8:]
        if moves:
            lines.append("Most recent moves, oldest first:")
            lines.extend(f"- {move['op']} {move['row']},{move['column']} ({'you' if move['by'] == 'ai-buddy' else move['by']}):"
                         f" {move['outcome']}" for move in moves)
        if work["purpose"]:
            lines.append(f"Note: {work['purpose']}")
    elif kind == "reaction":
        lines.append(f"While reflecting in the background ({work['createdAt']}), you noted this to consider "
                     "when you are active:")
        lines.append(work["purpose"] or "(nothing recorded)")
        lines.append("Decide whether and how to act on it; doing nothing is also fine.")
    elif kind == "investigation":
        game = related["investigatedGame"]
        losing = next((move for move in reversed(game["moves"]) if move["outcome"].startswith("hit a mine")), None)
        lines.append(f"You lost Minesweeper game {game['id']}"
                     + (f" when you revealed {losing['row']},{losing['column']}." if losing else "."))
        lines.append("Investigate the reasoning or rule that led to it. The game is over, so the final board "
                     "shows every mine (M a mine, * the mine you revealed, F! a correct flag):")
        lines.extend(renderBoard(game, showMines=True))
        lines.append("The last moves, oldest first:")
        lines.extend(f"- {move['op']} {move['row']},{move['column']}: {move['outcome']}" for move in game["moves"][-8:])
        lines.append("If you find a reliable lesson, establish it as a rule or revise one of your rules. "
                     "Concluding that the loss was bad luck is also valid.")
    else:
        lines.append(f"{_originPhrase(work)}:")
        lines.append(work["purpose"] or "(no purpose recorded)")
    return lines


def _stepLines(steps: list[dict], byId: dict, limit: int | None = None) -> list[str]:
    """Renders the working information of an undertaking.

    With a limit, only the most recent steps are shown and the omission is
    stated; long activities keep their state in the game record instead.
    """
    if not steps:
        return []
    lines = ["", "You are already working on this. What happened so far, in order:"]
    first = 1
    if limit is not None and len(steps) > limit:
        first = len(steps) - limit + 1
        lines.append(f"({first - 1} earlier steps are not shown.)")
    for number, step in enumerate(steps[first - 1:], start=first):
        if step.get("kind") == "interrupted":
            lines.append(f"Step {number} (interrupted: {step['reason']}; incomplete, nothing in it was acted on):")
            if step.get("partialOutput"):
                lines.append(f"  Your partial output: {step['partialOutput']}")
            continue
        if step.get("kind") == "amended":
            lines.append(f"Step {number}: the purpose was changed by {step['by']} to: {step['purpose']}")
            continue
        lines.append(f"Step {number}:")
        if step.get("thinking"):
            lines.append(f"  Your thinking: {step['thinking']}")
        for text in step.get("communications", []):
            lines.append(f"  You said to the user: {text}")
        for memoryId in step.get("rememberedIds", []):
            lines.append(f"  You stored a memory: {byId[memoryId]['text']}")
        for intended in step.get("intended", []):
            lines.append(f"  You set yourself something to do later: {intended['purpose']}")
        for move in step.get("gameMoves", []):
            where = f"{move['row']},{move['column']}" if move.get("row") else repr(move.get("cell"))
            lines.append(f"  You played {move['op']} {where}: {move['outcome']}")
        if step.get("rested"):
            lines.append("  You chose to rest.")
        for change in step.get("ruleChanges", []):
            lines.append(f"  Rule change ({change['action']}): {change['outcome']}")
        for item in step.get("disregarded", []):
            lines.append(f"  You disregarded a rule for that step: {item['text']} ({item['reason'] or 'no reason given'})")
        for reason in step.get("continueReasons", []):
            lines.append(f"  You chose to keep thinking{': ' + reason if reason else ''}")
        for recall in step.get("recalls", []):
            lines.append(f"  You tried to recall: {recall['query']}")
            if recall["outcome"] == "found":
                lines.extend(f"    - {describeMaterial(byId[item['id']])}" for item in recall["results"])
            elif recall["outcome"] == "no-further":
                lines.append("    No further memories were found beyond those already shown.")
            elif recall["outcome"] == "not-available":
                lines.append("    Searching was not available at that point.")
            else:
                lines.append("    No memories were found for this.")
    return lines


def _timeLines(work: dict, now: str, settings: dict, byId: dict) -> tuple[list[str], str | None]:
    """Describes elapsed and active processing time, with configured pressure."""
    if work["startedAt"] is None or not work["steps"]:
        return [], None
    activeSeconds = round(work["activeProcessingNs"] / 1e9, 1)
    lines = ["", f"You started on this at {work['startedAt']}. Time spent thinking about it so far: {activeSeconds} seconds."]
    if work["inputExperienceIds"]:
        first = byId[work["inputExperienceIds"][0]]["receivedAt"]
        lines.append(f"The first message arrived {round(max(0.0, _secondsBetween(first, now)))} seconds ago.")
    pressure = None
    if work["category"] in settings["timePressureCategories"]:
        for level in settings["timePressure"]:
            if activeSeconds >= level["afterSeconds"]:
                pressure = level["text"].replace("{seconds}", str(activeSeconds))
    if pressure:
        lines.append(pressure)
    return lines, pressure


def buildPrompt(*, personality: dict, now: str, work: dict, byId: dict, related: dict, recent=(),
                relevant=(), storedCount: int = 0, importantState=(), relevantState=(),
                allowContinue: bool = True, timeLines=(), notices=(), movesPerStep: int = 3,
                stepsInPrompt: int | None = None, canStartActivity: bool = False, rules=(),
                ruleScope: str = "general") -> str:
    """Builds one fresh situation-first prompt for one step of an undertaking.

    The prompt is constructed only from authoritative personality, the
    current time, current information, selected memory material, what the
    undertaking is about and its working information. No accumulated
    conversation is replayed as an execution model.
    """
    lines = [
        f"You are {personality['name']}, an AI companion that runs continuously on this computer.",
        "This is not a chat transcript. Each time you think, you are given a fresh description of your current situation.",
        "",
        "About you:",
        personality["summary"],
    ]
    characteristics = personality.get("stableCharacteristics") or []
    if characteristics:
        lines.append("Stable characteristics:")
        lines.extend(f"- {item}" for item in characteristics)
    style = personality.get("communicationStyle")
    if style:
        lines.append(f"Communication style: {style}")
    lines += ["", f"Current time: {now}", ""]
    noticeLines = _noticeLines(list(notices), now)
    if noticeLines:
        lines += [*noticeLines, ""]

    game = related.get("game")
    isActivity = work["kind"] == "minesweeper"
    gameAvailable = game is not None and (isActivity or work["kind"] == "process-user-input")
    if game is not None and not isActivity:
        lines.append(f"Current activity: {_gameSummary(game)}.")
        if gameAvailable:
            lines.append("Board (# hidden, F flagged, . no neighbouring mines, digits count neighbouring mines):")
            lines.extend(renderBoard(game))
        lines.append("")

    if importantState:
        lines.append("Important current information:")
        lines.extend(f"- {item['text']}" for item in importantState)
        lines.append("")
    if relevantState:
        lines.append("Current information that may be relevant:")
        lines.extend(f"- {item['text']}" for item in relevantState)
        lines.append("")
    if recent:
        lines.append("Your most recent experiences before this, oldest first:")
        lines.extend(f"- {describeMaterial(item)}" for item in recent)
        lines.append("")
    if storedCount == 0:
        lines += ["You have no personal memories yet.", ""]
    elif relevant:
        lines.append("Memories that may be relevant (found automatically; may be incomplete or inaccurate):")
        lines.extend(f"- {describeMaterial(item)}" for item in relevant)
        lines.append("")
    else:
        lines += ["No other memories were found that seem relevant.", ""]

    if rules:
        lines.append("Rules you follow, in order (you may disregard one for this occasion):")
        lines.extend(f"{number}. {rule['text']}{'' if rule['editable'] else ' (protected)'}"
                     for number, rule in enumerate(rules, start=1))
        lines.append("")

    lines += _situationLines(work, byId, related)
    lines += _stepLines(work["steps"], byId, stepsInPrompt if isActivity else None)
    lines += list(timeLines)

    lines += [
        "",
        "What to do:",
        "Decide what, if anything, to say or do. You may think in plain text first; your thinking is not shown to the user.",
        "You can use these actions, each inside its own tag:",
        "<say>text</say> - something the user receives.",
        "<remember>text</remember> - store something you consider worth remembering, in your own words.",
        "<intend>text</intend> - set yourself something to do later; it does not start now.",
    ]
    if work["kind"] == "choice":
        lines.append("<choose>number</choose> - your choice from the list above.")
    if gameAvailable:
        lines += [
            f"<reveal>row,column</reveal>, <flag>row,column</flag>, <unflag>row,column</unflag> - Minesweeper moves, "
            f"applied in order; at most {movesPerStep} in this step.",
        ]
    if isActivity:
        lines.append("<stop>why</stop> - stop playing for now; the game stays saved and can be resumed.")
    elif work["kind"] != "choice":
        lines.append("<rest>why</rest> - rest if you have nothing you want to do; new work will wake you.")
    if canStartActivity:
        lines.append("<activity>minesweeper</activity> - start or resume a Minesweeper game later.")
    lines.append(f"<rule>text</rule> - establish a rule for future occasions ({ruleScope} rules).")
    if rules:
        lines += [
            "<disregard>N: why</disregard> - do not follow rule N this time; the rule stays unchanged.",
            "<revise>N: new text</revise>, <reorder>N: new position</reorder>, <retire>N: why</retire> - "
            "change rule N for the future; protected rules cannot be changed.",
        ]
    if isActivity:
        lines.append("The game continues in further steps until it ends or you stop.")
    elif not allowContinue and work["kind"] == "choice":
        lines.append("This needs only your choice; searching memory or further steps are not available.")
    elif not allowContinue:
        if work["steps"]:
            lines.append("You have used the steps available for this. Conclude now: say what is useful, or nothing.")
        lines.append("Searching your memory is not available for this.")
    else:
        lines += [
            "<recall>what to search for</recall> - search your memory; you will think again with the results.",
            "<continue>why</continue> - keep thinking about this in another step.",
            "If you use <recall>, you may wait to <say> anything until you have the results.",
        ]
    lines.append("If you decide not to say anything, do not use <say> at all.")
    return "\n".join(lines)


def interpretResult(rawText: str) -> dict:
    """Interprets raw inference text under the provisional action-tag contract.

    Only closed action tags outside <think> reasoning are actions. Everything
    else is retained as thinking. Unclosed tags are never acted on; they are
    reported as warnings. <continue>, <stop> and <rest> may be empty. Game
    moves keep their textual order across <reveal>, <flag> and <unflag>.
    """
    remainder = _THINK.sub("", rawText)
    actions = {}
    warnings = []
    gameMoves = [{"op": match.group(1).lower(), "cell": match.group(2).strip()}
                 for match in _GAME_OPS.finditer(remainder)]
    remainder = _GAME_OPS.sub("", remainder)
    for tag in ("reveal", "flag", "unflag"):
        if re.search(rf"<{tag}>", remainder, re.IGNORECASE):
            warnings.append(f"unclosed-{tag}-ignored")
    for tag in _ACTION_TAGS:
        pattern = re.compile(rf"<{tag}>(.*?)</{tag}>|<{tag}\s*/>", re.DOTALL | re.IGNORECASE)
        found = [(match.group(1) or "").strip() for match in pattern.finditer(remainder)]
        actions[tag] = found if tag in ("continue", "stop", "rest") else [text for text in found if text]
        remainder = pattern.sub("", remainder)
        if re.search(rf"<{tag}>", remainder, re.IGNORECASE):
            warnings.append(f"unclosed-{tag}-ignored")
    return {
        "contract": RESULT_CONTRACT,
        "communications": actions["say"],
        "recalls": actions["recall"],
        "remember": actions["remember"],
        "continue": actions["continue"],
        "intend": actions["intend"],
        "choose": actions["choose"],
        "stop": actions["stop"],
        "rest": actions["rest"],
        "activity": actions["activity"],
        "rule": actions["rule"],
        "revise": actions["revise"],
        "reorder": actions["reorder"],
        "retire": actions["retire"],
        "disregard": actions["disregard"],
        "react": actions["react"],
        "candidate": actions["candidate"],
        "gameMoves": gameMoves,
        "thinking": remainder.strip(),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Passive processing (may run while inactive; never expresses or activates)
# ---------------------------------------------------------------------------

def _itemNumber(item: dict) -> int:
    return int(item["id"][1:])


def buildPassivePrompt(*, personality: dict, now: str, kind: str, items: list[dict], associated: list[dict],
                       openPurposes: list[str], settings: dict) -> str:
    """Builds a background-processing prompt. Only passive actions are offered."""
    lines = [
        f"You are {personality['name']}. This is background processing: you are reviewing your memories on your own.",
        "Nothing you write here is said to anyone, and it does not wake you or change what you are doing.",
        "",
        "About you:",
        personality["summary"],
        "",
        f"Current time: {now}",
        "",
    ]
    if kind == "memory-processing":
        lines.append("Memories you have not reflected on yet, oldest first:")
        lines.extend(f"- {describeMaterial(item)}" for item in items)
        if associated:
            lines += ["", "Older memories that may be related:"]
            lines.extend(f"- {describeMaterial(item)}" for item in associated)
        lines += [
            "",
            "What to do:",
            "Look for conclusions, associations or generalizations worth keeping, and for anything you should",
            "consider later when you are active. You can use these actions, each inside its own tag:",
            "<remember>text</remember> - record a conclusion or association; it is kept as a reflection, not as "
            "something that happened.",
            "<react>text</react> - note something to consider when you are next active; it becomes something to do.",
            "Writing nothing is fine.",
        ]
    else:
        lines.append("Your recent memories, oldest first:")
        lines.extend(f"- {describeMaterial(item)}" for item in items)
        if openPurposes:
            lines += ["", "Things you already have to do:"]
            lines.extend(f"- {purpose}" for purpose in openPurposes)
        maximum = settings["passive"]["maxCandidates"]
        lines += [
            "",
            "What to do:",
            f"Choose up to {maximum} subjects that would be worth spending spare time on, and write each as",
            "<candidate>what to do</candidate>. They wait for spare time and may expire unstarted.",
            "<remember>text</remember> - record a conclusion or association, kept as a reflection.",
            "Choosing nothing is fine.",
        ]
    return "\n".join(lines)


def _preparePassive(transaction, work: dict, settings: dict, now: str, schedulingRecord: dict, lifecycle: dict):
    """Prepares a passive step. Lifecycle notices are left for the next active prompt."""
    material = _memoryMaterial(transaction)
    batchSize = settings["passive"]["batchSize"]
    inputs = {"passive": True, "currentTime": now}
    associated = []
    openPurposes = []
    if work["kind"] == "memory-processing":
        cursor = _load(transaction, _PASSIVE_CURSOR) or {"experience": 0, "memory": 0}
        items = [item for item in material
                 if _itemNumber(item) > cursor["experience" if item["id"][0] == "e" else "memory"]][:batchSize]
        if not items:
            _closeWork(transaction, {**work, "startedAt": work["startedAt"] or now, "lastAdvancedAt": now},
                       state="completed", reason="nothing-to-process", at=now)
            transaction.set(_LIFECYCLE, {**lifecycle, "lastSeenAt": now})
            transaction.commit()
            return {"deterministic": True, "workId": work["id"], "result": {"nothingToProcess": True},
                    "scheduling": schedulingRecord, "expired": []}
        cursorTo = dict(cursor)
        for item in items:
            key = "experience" if item["id"][0] == "e" else "memory"
            cursorTo[key] = max(cursorTo[key], _itemNumber(item))
        associatedHits = retrieve(material, " ".join(item["text"] for item in items),
                                  limit=settings["memory"]["automaticLimit"], exclude={item["id"] for item in items})
        byId = {item["id"]: item for item in material}
        associated = [byId[hit["id"]] for hit in associatedHits]
        inputs.update(cursorFrom=cursor, cursorTo=cursorTo, associatedIds=[item["id"] for item in associated])
    else:
        items = sorted(material, key=_orderKey)[-batchSize:]
        openPurposes = [item["purpose"] for item in _openWork(transaction)
                        if item["purpose"] and not ACTIVITIES[item["kind"]]["passive"]]
    inputs["materialIds"] = [item["id"] for item in items]
    prompt = buildPassivePrompt(personality=_load(transaction, _PERSONALITY), now=now, kind=work["kind"],
                                items=items, associated=associated, openPurposes=openPurposes, settings=settings)
    inferenceId, inferenceAddress = _allocate(transaction, "inference")
    transaction.set(inferenceAddress, {
        "id": inferenceId, "workId": work["id"], "purpose": work["kind"], "continuation": 0, "state": "prepared",
        "preparedAt": now, "builtPrompt": prompt, "prompt": prompt, "scheduling": schedulingRecord,
        "inputs": inputs, "interventions": [],
    })
    transaction.set(_addressOf(work["id"]), {
        **work, "state": "in-progress", "interrupt": False, "startedAt": work["startedAt"] or now,
        "lastAdvancedAt": now, "inferenceIds": [*work["inferenceIds"], inferenceId],
    })
    transaction.set(_LIFECYCLE, {**lifecycle, "lastSeenAt": now})
    transaction.commit()
    return {"inferenceId": inferenceId, "workId": work["id"], "prompt": prompt, "reoffered": False,
            "scheduling": schedulingRecord, "passive": True}


# ---------------------------------------------------------------------------
# Capabilities: input and the processing cycle
# ---------------------------------------------------------------------------

def receiveInput(ctx, payload):
    """Records one user message and establishes or amends work to process it.

    If a user-input undertaking is open (pending or in progress), the message
    amends it instead of creating a separate obligation. A prepared but not
    yet executed inference of that undertaking is superseded, so the next
    prompt includes the new message.
    """
    payload = _requirePayload(payload, "text")
    text = payload["text"]
    if type(text) is not str:
        raise TypeError("User input text must be a string.")
    receivedAt = payload.get("receivedAt") or _now(payload)
    transaction = ctx.memory.openTransaction()
    experienceId, experienceAddress = _allocate(transaction, "experience")
    transaction.set(experienceAddress, {
        "id": experienceId, "kind": "user-message", "text": text,
        "receivedAt": receivedAt, "source": payload.get("source", "terminal"),
    })
    target = next((work for work in _openWork(transaction)
                   if work["category"] == "user-input" and work["state"] in ("pending", "in-progress")), None)
    if target is None:
        workId = _newWork(transaction, kind="process-user-input", now=receivedAt, origin={"by": "user-input"},
                          inputExperienceIds=[experienceId])
        transaction.commit()
        return {"experienceId": experienceId, "workId": workId, "amended": False, "supersededInferenceId": None}

    superseded = None
    last = _lastInference(transaction, target)
    if last is not None and last["state"] == "prepared":
        transaction.set(_addressOf(last["id"]), {**last, "state": "superseded", "supersededBy": experienceId,
                                                 "supersededAt": receivedAt})
        superseded = last["id"]
    transaction.set(_addressOf(target["id"]), {
        **target,
        "inputExperienceIds": [*target["inputExperienceIds"], experienceId],
        "amendments": [*target["amendments"], {"experienceId": experienceId, "at": receivedAt,
                                               "afterStart": bool(target["steps"])}],
    })
    transaction.commit()
    return {"experienceId": experienceId, "workId": target["id"], "amended": True, "supersededInferenceId": superseded}


def _ensurePeriodic(transaction, settings, now: str) -> list[str]:
    """Establishes a due instance of each configured periodic check that has none."""
    created = []
    openNames = {work.get("periodicName") for work in _openWork(transaction) if work["kind"] == "periodic-check"}
    for definition in settings["periodic"]:
        if definition["name"] not in openNames:
            created.append(_newWork(
                transaction, kind="periodic-check", now=now, origin={"by": "periodic"},
                purpose=f"periodic check {definition['name']}", important=bool(definition.get("important")),
                notBefore=now, extra={"periodicName": definition["name"], "everySeconds": definition["everySeconds"],
                                      "establishes": definition.get("establishes")}))
    return created


def _establishActivity(transaction, kind: str, *, now: str, origin: dict, important: bool = False,
                       params: dict | None = None) -> tuple[str, bool]:
    """Establishes an activity undertaking unless one is already open.

    Returns (workId, created). Multiplicity is limited to one open
    undertaking per activity; asking again for an important one marks the
    existing undertaking important.
    """
    existing = next((work for work in _openWork(transaction) if work["kind"] == kind), None)
    if existing is not None:
        if important and not existing["important"]:
            transaction.set(_addressOf(existing["id"]), {**existing, "important": True})
        return existing["id"], False
    workId = _newWork(transaction, kind=kind, now=now, origin=origin, important=important,
                      optional=not important, extra={"gameParams": params or {}})
    return workId, True


def _runDeterministic(transaction, work: dict, now: str) -> dict:
    """Advances one deterministic undertaking. Returns its step result."""
    if work["kind"] != "periodic-check":
        raise RuntimeError(f"No deterministic handler for {work['kind']!r}.")
    lateSeconds = round(max(0.0, _secondsBetween(work["notBefore"], now)), 1) if work["notBefore"] else 0.0
    result = {"check": work.get("periodicName"), "dueAt": work["notBefore"], "ranAt": now, "lateSeconds": lateSeconds}
    establishes = work.get("establishes")
    if establishes:
        existing = next((item for item in _openWork(transaction) if item["kind"] == establishes), None)
        if existing is None:
            workId, _created = _establishActivity(transaction, establishes, now=now,
                                                  origin={"by": "periodic", "periodicWorkId": work["id"]})
            result["established"] = workId
        elif ACTIVITIES[establishes]["passive"]:
            result["alreadyOpen"] = existing["id"]
        elif not existing["important"]:
            transaction.set(_addressOf(existing["id"]), {
                **existing, "important": True,
                "escalations": [*existing.get("escalations", []), {"at": now, "by": work["id"]}]})
            result["escalated"] = existing["id"]
        else:
            result["alreadyImportant"] = existing["id"]
    step = {"kind": "deterministic", "at": now, "result": result}
    finished = {**work, "startedAt": work["startedAt"] or now, "lastAdvancedAt": now, "steps": [*work["steps"], step]}
    _closeWork(transaction, finished, state="completed", reason="deterministic-completion", at=now)
    nextId = _newWork(
        transaction, kind="periodic-check", now=now, origin={"by": "periodic"}, purpose=work["purpose"],
        important=work["important"], notBefore=_shift(now, work["everySeconds"]),
        extra={"periodicName": work.get("periodicName"), "everySeconds": work["everySeconds"],
               "establishes": work.get("establishes"), "previousId": work["id"]})
    return {**result, "nextId": nextId}


def prepareCycle(ctx, payload):
    """Applies scheduling and prepares the next step of the selected undertaking.

    An inference already prepared but not executed, or generated but not
    processed, is offered again unchanged. Expired unstarted work is closed. A deterministic undertaking
    is advanced immediately within this Job; an inference undertaking gets a
    freshly built prompt and a prepared inference record, which is not run.
    """
    now = _now(payload)
    for work in _openWork(ctx.memory):
        last = _lastInference(ctx.memory, work)
        if last is not None and last["state"] == "prepared":
            return {"inferenceId": last["id"], "workId": work["id"], "prompt": last["prompt"], "reoffered": True}
        if last is not None and last["state"] == "generated":
            return {"inferenceId": last["id"], "workId": work["id"], "rawOutput": last["rawOutput"],
                    "generated": True, "reoffered": True}

    settings = _settings(ctx)
    transaction = ctx.memory.openTransaction()
    lifecycle = _lifecycle(transaction)
    established = _ensurePeriodic(transaction, settings, now)
    decision = evaluateSchedule(transaction, now)
    for workId in decision["expire"]:
        _closeWork(transaction, _work(transaction, workId), state="expired", reason="expired-unstarted", at=now)
    if decision["selected"] is None:
        transaction.set(_LIFECYCLE, {**lifecycle, "lastSeenAt": now})
        transaction.commit()
        return {"idle": True, "expired": decision["expire"], "established": established,
                "lifecycle": lifecycle["state"]}

    work = _work(transaction, decision["selected"])
    schedulingRecord = {"policy": decision["policy"], "selected": decision["selected"], "reason": decision["reason"],
                        "lifecycle": decision["lifecycle"], "candidates": decision["candidates"]}
    if ACTIVITIES[work["kind"]]["strategy"] == "deterministic":
        result = _runDeterministic(transaction, work, now)
        transaction.set(_LIFECYCLE, {**lifecycle, "lastSeenAt": now})
        transaction.commit()
        return {"deterministic": True, "workId": work["id"], "result": result, "scheduling": schedulingRecord,
                "expired": decision["expire"]}

    if work["kind"] in _PASSIVE_INFERENCE:
        return _preparePassive(transaction, work, settings, now, schedulingRecord, lifecycle)

    lifecycleChange = None
    if lifecycle["state"] == "resting" and not ACTIVITIES[work["kind"]]["passive"]:
        lifecycle = _setLifecycle(transaction, lifecycle, "active", now=now, by="orchestration",
                                  reason=f"work {work['id']} selected")
        lifecycleChange = {"from": "resting", "to": "active", "reason": lifecycle["reason"]}

    game = _currentGame(transaction)
    if work["kind"] == "minesweeper":
        if game is None:
            params = {**settings["minesweeper"], **{key: value for key, value in work.get("gameParams", {}).items()
                                                    if key in ("width", "height", "mines")}}
            gameId, gameAddress = _allocate(transaction, "game")
            seed = work.get("gameParams", {}).get("seed")
            if seed is None:
                seed = int(_sha256(f"{gameId}:{now}")[:8], 16)
            game = newGame(gameId=gameId, width=params["width"], height=params["height"], mines=params["mines"],
                           seed=seed, now=now, workId=work["id"])
            transaction.set(gameAddress, game)
        elif work["id"] not in game["workIds"]:
            game = {**game, "workIds": [*game["workIds"], work["id"]]}
            transaction.set(_addressOf(game["id"]), game)
        work = {**work, "gameId": game["id"]}

    memorySettings = settings["memory"]
    personality = _load(transaction, _PERSONALITY)
    material = _memoryMaterial(transaction)
    byId = {item["id"]: item for item in material}
    related = {}
    queryParts = []
    if work["kind"] == "process-user-input":
        queryParts = [byId[identifier]["text"] for identifier in work["inputExperienceIds"]]
    elif work["kind"] == "recheck":
        related["original"] = _work(transaction, work["recheckOf"])
        queryParts = [byId[identifier]["text"] for identifier in related["original"]["inputExperienceIds"]]
    elif work["kind"] == "choice":
        related["candidates"] = [_work(transaction, identifier) for identifier in work["candidateIds"]]
        queryParts = [candidate["purpose"] or "" for candidate in related["candidates"]]
    elif work["kind"] == "minesweeper":
        queryParts = ["minesweeper game"]
    elif work["kind"] == "investigation":
        related["investigatedGame"] = _load(transaction, _addressOf(work["investigates"]))
        queryParts = ["minesweeper game lost"]
    if game is not None:
        related["game"] = game
    if work["purpose"]:
        queryParts.append(work["purpose"])
    query = " ".join(queryParts)

    ownOutput = {item["id"] for item in material if _workOf(item) == work["id"]}
    experiences = [item for item in material if item["id"][0] == "e"]
    if work["inputExperienceIds"]:
        earliest = min(work["inputExperienceIds"])
        experiences = [item for item in experiences if item["id"] < earliest]
    experiences = [item for item in experiences if item["id"] not in ownOutput]
    recent = experiences[-memorySettings["recentLimit"]:] if memorySettings["recentLimit"] else []
    excluded = set(work["inputExperienceIds"]) | ownOutput | {item["id"] for item in recent}
    relevantHits = retrieve(material, query, limit=memorySettings["automaticLimit"], exclude=excluded)
    storedCount = len(material) - len(work["inputExperienceIds"]) - len(ownOutput)

    situation = [item for item in _collection(transaction, "situation") if item["active"]]
    importantState = [item for item in situation if item["important"]]
    stateHits = retrieve([item for item in situation if not item["important"]], query, limit=_STATE_LIMIT)
    situationById = {item["id"]: item for item in situation}
    relevantState = [situationById[hit["id"]] for hit in stateHits]

    allowContinue = work["continuations"] < memorySettings["maxContinuations"] and work["kind"] != "choice"
    timeLines, pressure = _timeLines(work, now, settings, byId)
    notices = list(lifecycle.get("notices", []))
    canStartActivity = work["kind"] != "minesweeper" and not any(
        item["kind"] == "minesweeper" for item in _openWork(transaction))
    gameMovesAvailable = game is not None and work["kind"] in ("minesweeper", "process-user-input")
    scopes = _ruleScopes(work, gameMovesAvailable)
    rules = [rule for rule in _rules(transaction) if rule["scope"] in scopes]
    ruleScope = _ruleScopeFor(work)
    prompt = buildPrompt(
        personality=personality, now=now, work=work, byId=byId, related=related, recent=recent,
        relevant=[byId[hit["id"]] for hit in relevantHits], storedCount=storedCount,
        importantState=importantState, relevantState=relevantState, allowContinue=allowContinue,
        timeLines=timeLines, notices=notices, movesPerStep=settings["activity"]["movesPerStep"],
        stepsInPrompt=settings["activity"]["stepsInPrompt"], canStartActivity=canStartActivity,
        rules=rules, ruleScope=ruleScope)
    transaction.set(_LIFECYCLE, {**lifecycle, "notices": [], "lastSeenAt": now})

    inferenceId, inferenceAddress = _allocate(transaction, "inference")
    transaction.set(inferenceAddress, {
        "id": inferenceId,
        "workId": work["id"],
        "purpose": work["kind"] if not work["steps"] else "continuation",
        "continuation": len(work["steps"]),
        "state": "prepared",
        "preparedAt": now,
        "builtPrompt": prompt,
        "prompt": prompt,
        "scheduling": schedulingRecord,
        "inputs": {
            "personality": True,
            "currentTime": now,
            "experienceIds": list(work["inputExperienceIds"]),
            "recentIds": [item["id"] for item in recent],
            "automaticRetrieval": {"method": RETRIEVAL_METHOD, "results": relevantHits},
            "storedMemoryCount": storedCount,
            "importantStateIds": [item["id"] for item in importantState],
            "stateRetrieval": stateHits,
            "stepCount": len(work["steps"]),
            "recallOffered": allowContinue,
            "activeProcessingSeconds": round(work["activeProcessingNs"] / 1e9, 3),
            "timePressure": pressure,
            "notices": notices,
            "gameId": game["id"] if game is not None else None,
            "gameMovesAvailable": gameMovesAvailable,
            "ruleIds": [rule["id"] for rule in rules],
            "ruleScopes": scopes,
            "ruleScope": ruleScope,
        },
        "interventions": [],
    })
    transaction.set(_addressOf(work["id"]), {
        **work, "state": "in-progress", "interrupt": False, "startedAt": work["startedAt"] or now,
        "lastAdvancedAt": now, "inferenceIds": [*work["inferenceIds"], inferenceId],
    })
    transaction.commit()
    return {"inferenceId": inferenceId, "workId": work["id"], "prompt": prompt, "reoffered": False,
            "scheduling": schedulingRecord, "lifecycleChange": lifecycleChange}


def replacePrompt(ctx, payload):
    """Developer intervention: replaces the prompt of a prepared inference."""
    payload = _requirePayload(payload, "inferenceId", "prompt")
    prompt = payload["prompt"]
    if type(prompt) is not str or not prompt:
        raise ValueError("Replacement prompt must be a non-empty string.")
    address = _addressOf(payload["inferenceId"])
    inference = _load(ctx.memory, address)
    if inference is None or inference["state"] != "prepared":
        raise RuntimeError("Only a prepared inference prompt can be replaced.")
    transaction = ctx.memory.openTransaction()
    transaction.set(address, {
        **inference,
        "prompt": prompt,
        "interventions": [*inference["interventions"], {
            "kind": "prompt-replaced", "by": "developer", "at": _now(payload),
            "previousSha256": _sha256(inference["prompt"]), "newSha256": _sha256(prompt),
        }],
    })
    transaction.commit()
    return {"inferenceId": inference["id"], "promptSha256": _sha256(prompt)}


def replaceOutput(ctx, payload):
    """Developer intervention: replaces the raw output of a generated inference before processing."""
    payload = _requirePayload(payload, "inferenceId", "rawOutput")
    rawOutput = payload["rawOutput"]
    if type(rawOutput) is not str:
        raise TypeError("Replacement output must be a string.")
    address = _addressOf(payload["inferenceId"])
    inference = _load(ctx.memory, address)
    if inference is None or inference["state"] != "generated":
        raise RuntimeError("Only the output of a generated, unprocessed inference can be replaced.")
    transaction = ctx.memory.openTransaction()
    transaction.set(address, {
        **inference,
        "rawOutput": rawOutput,
        "interventions": [*inference["interventions"], {
            "kind": "output-replaced", "by": "developer", "at": _now(payload),
            "previousSha256": _sha256(inference["rawOutput"]), "newSha256": _sha256(rawOutput),
        }],
    })
    transaction.commit()
    return {"inferenceId": inference["id"], "outputSha256": _sha256(rawOutput)}


def recordInterrupted(ctx, payload):
    """Records an inference cancelled to rebuild the prompt, keeping partial output.

    The inference becomes "interrupted" and its partial output is added to
    the undertaking's working information as an incomplete step. Nothing in
    partial output is acted on. The undertaking stays in progress.
    """
    payload = _requirePayload(payload, "inferenceId", "reason")
    address = _addressOf(payload["inferenceId"])
    inference = _load(ctx.memory, address)
    if inference is None or inference["state"] != "prepared":
        raise RuntimeError("Only a prepared (cancelled) inference can be recorded as interrupted.")
    partial = payload.get("partialOutput") or ""
    if type(partial) is not str:
        raise TypeError("partialOutput must be a string.")
    now = _now(payload)
    transaction = ctx.memory.openTransaction()
    transaction.set(address, {**inference, "state": "interrupted", "interruptedAt": now,
                              "interruptReason": payload["reason"], "partialOutput": partial,
                              "submittedPrompt": inference["prompt"]})
    work = _work(transaction, inference["workId"])
    step = {"kind": "interrupted", "inferenceId": inference["id"], "at": now, "reason": payload["reason"],
            "partialOutput": _THINK.sub("", partial).strip()}
    transaction.set(_addressOf(work["id"]), {**work, "steps": [*work["steps"], step]})
    transaction.commit()
    return {"inferenceId": inference["id"], "workId": work["id"], "partialCharacters": len(partial)}


def executeInference(ctx, payload):
    """Runs one prepared inference and records its raw output, unprocessed.

    The output is committed in state "generated"; nothing in it is acted on
    until aiBuddy.cycle.process@1 runs, so a developer can inspect or edit it
    in between, and a restart in between resumes processing without asking
    the model again.

    Cancellation propagates, so the Job aborts and the inference stays
    prepared. Other provider failures are recorded as a failed inference
    and failed thing to do instead of failing the Job, so the record of
    what was attempted survives.

    When payload carries a list under "partialOutput", streamed text
    fragments are appended to it as they arrive. That list belongs to the
    caller and survives a cancelled Job.
    """
    payload = _requirePayload(payload, "inferenceId")
    address = _addressOf(payload["inferenceId"])
    inference = _load(ctx.memory, address)
    if inference is None or inference["state"] != "prepared":
        raise RuntimeError("Only a prepared inference can be executed.")
    settings = _settings(ctx)
    prompt = inference["prompt"]
    partial = payload.get("partialOutput")

    def observe(event):
        """Copies streamed fragments to the caller-owned partial-output sink."""
        if isinstance(partial, list) and event.eventType == "delta":
            partial.append(event.text)

    startedAt = _now(payload)
    try:
        result = ctx.llm.run(
            providerName=settings["providerName"],
            model=settings["model"],
            providerOptions=settings["providerOptions"],
            query=LlmQuery(formatId="text/plain", payload=prompt),
            streamObserver=observe,
        )
    except ExecutionCancelled:
        raise
    except Exception as error:  # noqa: BLE001 - provider failure is information, not a crash.
        message = f"{type(error).__qualname__}: {error}"
        transaction = ctx.memory.openTransaction()
        transaction.set(address, {
            **inference, "state": "failed", "startedAt": startedAt,
            "submittedPrompt": prompt, "provider": settings["providerName"], "model": settings["model"],
            "error": message,
        })
        _closeWork(transaction, _work(transaction, inference["workId"]), state="failed", reason="inference-failed")
        transaction.commit()
        return {"inferenceId": inference["id"], "state": "failed", "workState": "failed", "error": message}

    generatedAt = _now({})
    transaction = ctx.memory.openTransaction()
    transaction.set(address, {
        **inference,
        "state": "generated",
        "startedAt": startedAt,
        "generatedAt": generatedAt,
        "submittedPrompt": prompt,
        "submittedPromptSha256": _sha256(prompt),
        "provider": settings["providerName"],
        "model": settings["model"],
        "generatedOutput": result.rawText,
        "rawOutput": result.rawText,
        "timing": result.timingSnapshot(),
    })
    transaction.commit()
    return {"inferenceId": inference["id"], "state": "generated", "rawOutput": result.rawText,
            "durationNs": result.durationNs}


def processInference(ctx, payload):
    """Processes the raw output of a generated inference: the actions in it.

    Requested recalls run here and are stored as working information; a
    recall or <continue> keeps the undertaking going with a fresh prompt.
    <intend> establishes separate later work without switching to it.
    The output processed is rawOutput, which a developer may have replaced;
    the model's own text stays in generatedOutput.
    """
    payload = _requirePayload(payload, "inferenceId")
    address = _addressOf(payload["inferenceId"])
    inference = _load(ctx.memory, address)
    if inference is None or inference["state"] != "generated":
        raise RuntimeError("Only a generated inference can be processed.")
    settings = _settings(ctx)
    rawText = inference["rawOutput"]
    durationNs = inference["timing"].get("durationNs", 0)
    interpretation = interpretResult(rawText)
    endedAt = _now(payload)
    memorySettings = settings["memory"]
    transaction = ctx.memory.openTransaction()
    work = _work(transaction, inference["workId"])
    material = _memoryMaterial(transaction)
    warnings = list(interpretation["warnings"])
    if work["kind"] in _PASSIVE_INFERENCE:
        return _processPassive(transaction, address, inference, work, interpretation, warnings, settings,
                               endedAt, durationNs)
    for key in ("react", "candidate"):
        if interpretation[key]:
            warnings.append(f"{key}-only-in-passive-processing-ignored")

    continueAvailable = inference["inputs"].get("recallOffered", True) \
        and work["continuations"] < memorySettings["maxContinuations"]
    recalls = []
    shown = list(work["shownMemoryIds"])
    inPrompt = set(inference["inputs"].get("recentIds", [])) | {
        hit["id"] for hit in inference["inputs"].get("automaticRetrieval", {}).get("results", [])}
    excluded = set(work["inputExperienceIds"]) | {item["id"] for item in material if _workOf(item) == work["id"]}
    for query in interpretation["recalls"]:
        if not continueAvailable:
            recalls.append({"query": query, "method": RETRIEVAL_METHOD, "results": [], "outcome": "not-available"})
            continue
        everything = retrieve(material, query, limit=len(material), exclude=excluded)
        results = retrieve(material, query, limit=memorySettings["recallLimit"],
                           exclude=excluded | set(shown) | inPrompt)
        outcome = "found" if results else ("no-further" if everything else "no-match")
        recalls.append({"query": query, "method": RETRIEVAL_METHOD, "results": results, "outcome": outcome})
        shown.extend(item["id"] for item in results)

    sourceIds = list(work["inputExperienceIds"])
    rememberedIds = []
    for text in interpretation["remember"]:
        memoryId, memoryAddress = _allocate(transaction, "memory")
        transaction.set(memoryAddress, {
            "id": memoryId, "kind": "deliberate", "text": text, "establishedAt": endedAt,
            "origin": {"by": "ai-buddy", "inferenceId": inference["id"], "workId": work["id"],
                       "sourceExperienceIds": sourceIds},
        })
        rememberedIds.append(memoryId)

    intended = []
    lifetime = settings["intentionLifetimeSeconds"]
    for text in interpretation["intend"]:
        intendedId = _newWork(
            transaction, kind="task", now=endedAt, purpose=text,
            origin={"by": "ai-buddy", "inferenceId": inference["id"], "workId": work["id"]},
            expiresAt=_shift(endedAt, lifetime) if lifetime else None)
        intended.append({"workId": intendedId, "purpose": text})

    chosen = None
    if interpretation["choose"]:
        if work["kind"] != "choice":
            warnings.append("choose-not-applicable-ignored")
        else:
            selection = interpretation["choose"][0].strip()
            candidates = work["candidateIds"]
            if selection.isdigit() and 1 <= int(selection) <= len(candidates):
                chosen = candidates[int(selection) - 1]
                target = _work(transaction, chosen)
                if target["state"] in _TERMINAL:
                    warnings.append("choice-target-already-ended")
                    chosen = None
                else:
                    transaction.set(_addressOf(chosen), {**target, "interrupt": True, "chosenBy": work["id"]})
            else:
                warnings.append("choice-invalid-ignored")

    gameMoves = []
    game = _currentGame(transaction)
    movesAvailable = bool(inference["inputs"].get("gameMovesAvailable")) and game is not None \
        and game["id"] == inference["inputs"].get("gameId")
    if interpretation["gameMoves"]:
        if not movesAvailable:
            warnings.append("game-action-not-available-ignored")
        else:
            limit = settings["activity"]["movesPerStep"]
            for index, requested in enumerate(interpretation["gameMoves"]):
                if index >= limit:
                    warnings.append("moves-beyond-limit-ignored")
                    break
                if game["status"] != "in-progress":
                    warnings.append("moves-after-game-end-ignored")
                    break
                cell = _parseCell(requested["cell"])
                if cell is None:
                    gameMoves.append({"op": requested["op"], "cell": requested["cell"], "row": None, "column": None,
                                      "outcome": "illegal: no row,column could be read"})
                    continue
                game, outcome = applyMove(game, requested["op"], cell[0], cell[1], now=endedAt, by="ai-buddy")
                game["moves"][-1]["inferenceId"] = inference["id"]
                gameMoves.append({"op": requested["op"], "cell": requested["cell"], "row": cell[0],
                                  "column": cell[1], "outcome": outcome})
            transaction.set(_addressOf(game["id"]), game)

    investigationId = None
    if game is not None and game["status"] == "lost" and settings["activity"]["investigateLosses"] \
            and any(move["outcome"].startswith("hit a mine") for move in gameMoves):
        investigationId = _newWork(
            transaction, kind="investigation", now=endedAt, purpose=f"investigate the loss of game {game['id']}",
            origin={"by": "orchestration", "gameId": game["id"], "inferenceId": inference["id"]},
            extra={"investigates": game["id"]})

    ruleChanges = []
    disregarded = []
    displayed = [_load(transaction, _addressOf(ruleId)) for ruleId in inference["inputs"].get("ruleIds", [])]

    def resolveRule(text, action):
        """Maps "N: rest" to the Nth rule shown in this inference's prompt."""
        parsed = _numbered(text)
        if parsed is None or not 1 <= parsed[0] <= len(displayed):
            warnings.append("rule-reference-invalid-ignored")
            ruleChanges.append({"action": action, "ruleId": None, "applied": False,
                                "outcome": f"no rule matches {text!r}"})
            return None, None
        return displayed[parsed[0] - 1], parsed[1]

    for text in interpretation["disregard"]:
        rule, reason = resolveRule(text, "disregard")
        if rule is not None:
            disregarded.append({"ruleId": rule["id"], "text": rule["text"], "reason": reason})
    for action in ("revise", "reorder", "retire"):
        for text in interpretation[action]:
            rule, rest = resolveRule(text, action)
            if rule is None:
                continue
            current = _load(transaction, _addressOf(rule["id"]))
            if not current["editable"]:
                warnings.append("rule-protected-change-rejected")
                ruleChanges.append({"action": action, "ruleId": current["id"], "applied": False,
                                    "outcome": f"rule {current['id']} is protected; {action} rejected"})
                continue
            revision = {"at": endedAt, "by": "ai-buddy", "inferenceId": inference["id"], "action": action}
            if action == "revise":
                if not rest:
                    warnings.append("rule-revision-empty-ignored")
                    continue
                updated = {**current, "text": rest,
                           "revisions": [*current["revisions"], {**revision, "previousText": current["text"]}]}
                outcome = f"rule {current['id']} now reads: {rest}"
            elif action == "reorder":
                target = _numbered(rest)
                if target is None:
                    warnings.append("rule-reorder-invalid-ignored")
                    continue
                updated = {**current, "order": _orderBetween(displayed, current, target[0]),
                           "revisions": [*current["revisions"], {**revision, "previousOrder": current["order"]}]}
                outcome = f"rule {current['id']} moved to position {target[0]}"
            else:
                updated = {**current, "active": False, "retiredAt": endedAt, "retiredReason": rest,
                           "revisions": [*current["revisions"], {**revision, "reason": rest}]}
                outcome = f"rule {current['id']} retired"
            transaction.set(_addressOf(current["id"]), updated)
            ruleChanges.append({"action": action, "ruleId": current["id"], "applied": True, "outcome": outcome})
    for text in interpretation["rule"]:
        origin = {"by": "ai-buddy", "inferenceId": inference["id"], "workId": work["id"],
                  "sourceExperienceIds": list(work["inputExperienceIds"])}
        if work.get("investigates"):
            origin["investigates"] = work["investigates"]
        ruleId = _newRule(transaction, text=text, scope=inference["inputs"].get("ruleScope", "general"),
                          editable=True, origin=origin, now=endedAt)
        ruleChanges.append({"action": "establish", "ruleId": ruleId, "applied": True,
                            "outcome": f"new rule {ruleId}: {text}"})

    lifecycleChange = None
    if interpretation["rest"]:
        if work["kind"] in ("minesweeper", "choice"):
            warnings.append("rest-not-applicable-ignored")
        else:
            current = _lifecycle(transaction)
            if current["state"] == "active":
                reason = interpretation["rest"][0] or "nothing to do"
                _setLifecycle(transaction, current, "resting", now=endedAt, by="ai-buddy", reason=reason)
                lifecycleChange = {"from": "active", "to": "resting", "reason": reason}

    startedActivities = []
    for name in interpretation["activity"]:
        name = name.strip().lower()
        if name not in _ESTABLISHABLE_ACTIVITIES:
            warnings.append("activity-unknown-ignored")
            continue
        activityId, created = _establishActivity(
            transaction, name, now=endedAt,
            origin={"by": "ai-buddy", "inferenceId": inference["id"], "workId": work["id"]})
        startedActivities.append({"activity": name, "workId": activityId, "created": created})

    stopped = bool(interpretation["stop"])
    if stopped and work["kind"] != "minesweeper":
        warnings.append("stop-not-applicable-ignored")
        stopped = False

    deliveries = []
    for text in interpretation["communications"]:
        experienceId, experienceAddress = _allocate(transaction, "experience")
        transaction.set(experienceAddress, {
            "id": experienceId, "kind": "communication", "text": text, "at": endedAt,
            "inferenceId": inference["id"], "workId": work["id"],
        })
        outboxId, outboxAddress = _allocate(transaction, "outbox")
        transaction.set(outboxAddress, {
            "id": outboxId, "text": text, "at": endedAt,
            "inferenceId": inference["id"], "experienceId": experienceId,
        })
        deliveries.append(outboxId)

    step = {
        "inferenceId": inference["id"], "at": endedAt, "thinking": interpretation["thinking"],
        "communications": interpretation["communications"], "rememberedIds": rememberedIds,
        "recalls": recalls, "continueReasons": interpretation["continue"], "intended": intended,
        "chosen": chosen, "gameMoves": gameMoves, "rested": lifecycleChange is not None,
        "startedActivities": startedActivities, "stopped": stopped,
        "ruleChanges": ruleChanges, "disregarded": disregarded, "investigationId": investigationId,
    }
    updated = {**work, "steps": [*work["steps"], step], "shownMemoryIds": shown,
               "activeProcessingNs": work["activeProcessingNs"] + durationNs}
    gameStatus = None
    if work["kind"] == "minesweeper":
        activitySettings = settings["activity"]
        progressed = any(not move["outcome"].startswith("illegal") for move in gameMoves)
        idleSteps = 0 if progressed else work.get("idleSteps", 0) + 1
        gameStatus = _load(transaction, _addressOf(work["gameId"]))["status"]
        if gameStatus in ("won", "lost"):
            reason = f"game-{gameStatus}"
        elif stopped:
            reason = "stopped-by-ai-buddy"
        elif idleSteps >= activitySettings["maxIdleSteps"]:
            reason = "no-progress"
        elif len(updated["steps"]) >= activitySettings["maxSteps"]:
            reason = "step-limit"
        else:
            reason = None
        updated["idleSteps"] = idleSteps
        if reason is None:
            transaction.set(_addressOf(work["id"]), updated)
            workState = "continuing"
        else:
            _closeWork(transaction, updated, state="completed", reason=reason, at=endedAt)
            workState = "completed"
    else:
        wantsMore = bool(recalls) or bool(interpretation["continue"])
        continuing = wantsMore and continueAvailable and work["kind"] != "choice"
        if wantsMore and not continuing:
            warnings.append("continuation-not-available-ignored")
        if continuing:
            transaction.set(_addressOf(work["id"]), {**updated, "continuations": work["continuations"] + 1})
            workState = "continuing"
        else:
            reason = "continuation-limit" if wantsMore else "inference-concluded"
            _closeWork(transaction, updated, state="completed", reason=reason, at=endedAt)
            workState = "completed"

    transaction.set(address, {
        **inference,
        "state": "completed",
        "endedAt": endedAt,
        "interpretation": {**interpretation, "warnings": warnings},
        "recalls": recalls,
        "rememberedIds": rememberedIds,
        "intendedWorkIds": [item["workId"] for item in intended],
        "chosenWorkId": chosen,
        "gameMoves": gameMoves,
        "startedActivities": [item["workId"] for item in startedActivities],
        "lifecycleChange": lifecycleChange,
        "ruleChanges": ruleChanges,
        "disregarded": disregarded,
        "investigationId": investigationId,
        "outboxIds": deliveries,
    })
    transaction.commit()
    return {
        "inferenceId": inference["id"],
        "state": "completed",
        "workId": work["id"],
        "workState": workState,
        "communications": interpretation["communications"],
        "recalls": [recall["query"] for recall in recalls],
        "rememberedIds": rememberedIds,
        "intendedWorkIds": [item["workId"] for item in intended],
        "chosenWorkId": chosen,
        "gameMoves": gameMoves,
        "gameStatus": gameStatus,
        "startedActivities": startedActivities,
        "lifecycleChange": lifecycleChange,
        "ruleChanges": ruleChanges,
        "disregarded": disregarded,
        "investigationId": investigationId,
        "durationNs": durationNs,
    }


_PASSIVE_ALLOWED = {"memory-processing": ("remember", "react"), "salient-subjects": ("remember", "candidate")}
_PASSIVE_IGNORED = ("communications", "recalls", "continue", "intend", "choose", "stop", "rest", "activity",
                    "rule", "revise", "reorder", "retire", "disregard", "gameMoves", "react", "candidate", "remember")


def _processPassive(transaction, address, inference, work, interpretation, warnings, settings, endedAt, durationNs):
    """Applies the allowed results of a passive step; everything else is ignored and warned.

    Passive results never become experiences or speech. Processed memories
    are retrievable like other memories; reactions are the only way a
    passive step asks for active attention, and they wait while AI Buddy is
    inactive.
    """
    allowed = _PASSIVE_ALLOWED[work["kind"]]
    for key in _PASSIVE_IGNORED:
        if interpretation[key] and key not in allowed:
            warnings.append(f"passive-{key}-ignored")
    sourceIds = list(inference["inputs"].get("materialIds", []))
    origin = {"by": "passive-processing", "activity": work["kind"], "inferenceId": inference["id"],
              "workId": work["id"], "sourceIds": sourceIds}

    processedIds = []
    for text in interpretation["remember"]:
        memoryId, memoryAddress = _allocate(transaction, "memory")
        transaction.set(memoryAddress, {"id": memoryId, "kind": "processed", "text": text, "establishedAt": endedAt,
                                        "origin": origin})
        processedIds.append(memoryId)
    reactionIds = []
    if "react" in allowed:
        for text in interpretation["react"]:
            reactionIds.append(_newWork(transaction, kind="reaction", now=endedAt, purpose=text, origin=origin))
    candidateIds = []
    if "candidate" in allowed:
        maximum = settings["passive"]["maxCandidates"]
        lifetime = settings["passive"]["candidateLifetimeSeconds"]
        if len(interpretation["candidate"]) > maximum:
            warnings.append("candidates-beyond-limit-ignored")
        for text in interpretation["candidate"][:maximum]:
            candidateIds.append(_newWork(
                transaction, kind="task", now=endedAt, purpose=text, origin=origin, optional=True,
                expiresAt=_shift(endedAt, lifetime) if lifetime else None, extra={"spontaneous": True}))
    if work["kind"] == "memory-processing" and inference["inputs"].get("cursorTo"):
        cursor = dict(inference["inputs"]["cursorTo"])
        if processedIds:
            cursor["memory"] = max(cursor["memory"], *(int(memoryId[1:]) for memoryId in processedIds))
        transaction.set(_PASSIVE_CURSOR, cursor)

    step = {"inferenceId": inference["id"], "at": endedAt, "thinking": interpretation["thinking"],
            "processedMemoryIds": processedIds, "reactionIds": reactionIds, "candidateIds": candidateIds}
    updated = {**work, "steps": [*work["steps"], step],
               "activeProcessingNs": work["activeProcessingNs"] + durationNs}
    _closeWork(transaction, updated, state="completed", reason="processed", at=endedAt)
    transaction.set(address, {
        **inference, "state": "completed", "endedAt": endedAt,
        "interpretation": {**interpretation, "warnings": warnings},
        "processedMemoryIds": processedIds, "reactionIds": reactionIds, "candidateIds": candidateIds,
    })
    transaction.commit()
    return {"inferenceId": inference["id"], "state": "completed", "workId": work["id"], "workState": "completed",
            "passive": True, "communications": [], "processedMemoryIds": processedIds,
            "reactionIds": reactionIds, "candidateIds": candidateIds, "warnings": warnings,
            "durationNs": durationNs}


def pendingOutbox(ctx, payload):
    """Read-only: returns deliberate communications not yet delivered."""
    delivered = _load(ctx.memory, _OUTBOX_DELIVERED, 0)
    last = _load(ctx.memory, _SEQUENCES["outbox"][0], 0)
    return [_load(ctx.memory, _itemAddress("outbox", number)[1]) for number in range(delivered + 1, last + 1)]


def acknowledgeOutbox(ctx, payload):
    """Records delivery of outgoing communications up to and including one id."""
    payload = _requirePayload(payload, "throughId")
    number = int(_addressOf(payload["throughId"]).rsplit("/o", 1)[1])
    transaction = ctx.memory.openTransaction()
    if number > _load(transaction, _OUTBOX_DELIVERED, 0):
        transaction.set(_OUTBOX_DELIVERED, number)
    transaction.commit()
    return {"delivered": number}


# ---------------------------------------------------------------------------
# Capabilities: developer inspection and intervention
# ---------------------------------------------------------------------------

def describeRecord(ctx, payload):
    """Read-only: returns one stored record by identifier."""
    payload = _requirePayload(payload, "id")
    record = _load(ctx.memory, _addressOf(payload["id"]))
    return _workDefaults(record) if record is not None and payload["id"][0] == "w" else record


def describeStatus(ctx, payload):
    """Read-only: returns a compact summary of current AI Buddy state."""
    return {
        "schema": _load(ctx.memory, _META, {}).get("schema"),
        "personalityName": _load(ctx.memory, _PERSONALITY, {}).get("name"),
        "experiences": _load(ctx.memory, _SEQUENCES["experience"][0], 0),
        "memories": _load(ctx.memory, _SEQUENCES["memory"][0], 0),
        "inferences": _load(ctx.memory, _SEQUENCES["inference"][0], 0),
        "openWork": [{"id": work["id"], "kind": work["kind"], "state": work["state"],
                      "purpose": work["purpose"], "steps": len(work["steps"])} for work in _openWork(ctx.memory)],
        "situation": [{"id": item["id"], "important": item["important"], "text": item["text"]}
                      for item in _collection(ctx.memory, "situation") if item["active"]],
        "outboxDelivered": _load(ctx.memory, _OUTBOX_DELIVERED, 0),
        "outboxTotal": _load(ctx.memory, _SEQUENCES["outbox"][0], 0),
        "lifecycle": {key: value for key, value in _lifecycle(ctx.memory).items() if key != "history"},
        "rules": len(_rules(ctx.memory)),
        "game": None if _currentGame(ctx.memory) is None else _gameSummary(_currentGame(ctx.memory)),
    }


def describeGame(ctx, payload):
    """Read-only Minesweeper state: the game in progress, or a given game id.

    {"showMines": true} is a developer view that also marks mine positions.
    """
    payload = payload if isinstance(payload, dict) else {}
    game = _load(ctx.memory, _addressOf(payload["gameId"])) if payload.get("gameId") else _currentGame(ctx.memory)
    if game is None:
        last = _load(ctx.memory, _SEQUENCES["game"][0], 0)
        return {"available": False, "lastGameId": _itemAddress("game", last)[0] if last else None}
    return {"available": game["status"] == "in-progress", "id": game["id"], "status": game["status"],
            "summary": _gameSummary(game), "board": renderBoard(game, showMines=bool(payload.get("showMines"))),
            "moves": game["moves"][-10:], "workIds": game["workIds"]}


def startPassive(ctx, payload):
    """Developer: establish a passive processing undertaking now (memory-processing or salient-subjects)."""
    payload = _requirePayload(payload, "activity")
    if payload["activity"] not in _PASSIVE_INFERENCE:
        raise ValueError(f"Passive activity must be one of {', '.join(_PASSIVE_INFERENCE)}.")
    now = _now(payload)
    transaction = ctx.memory.openTransaction()
    workId, created = _establishActivity(transaction, payload["activity"], now=now,
                                         origin={"by": "developer", "at": now})
    transaction.commit()
    return {"workId": workId, "created": created}


def startGame(ctx, payload):
    """Developer Queue directive for Minesweeper: establish (or reuse) the game undertaking.

    Optional width, height, mines and seed apply when a new game is created.
    """
    payload = payload if isinstance(payload, dict) else {}
    params = {}
    for key in ("width", "height", "mines", "seed"):
        if payload.get(key) is not None:
            if type(payload[key]) is not int or payload[key] < (0 if key == "seed" else 1):
                raise ValueError(f"Invalid Minesweeper {key}.")
            params[key] = payload[key]
    now = _now(payload)
    transaction = ctx.memory.openTransaction()
    workId, created = _establishActivity(transaction, "minesweeper", now=now, origin={"by": "developer", "at": now},
                                         important=bool(payload.get("important")), params=params)
    transaction.commit()
    return {"workId": workId, "created": created}


def changeLifecycle(ctx, payload):
    """Developer lifecycle operations: activate, deactivate, rest, wake.

    Activation is an explicit ordered operation; it records a notice so the
    next prompt states how long AI Buddy was inactive. Deactivation records
    who caused it ("by"), so quiescence can restore activity afterwards.
    """
    payload = _requirePayload(payload, "action")
    action = payload["action"]
    now = _now(payload)
    by = payload.get("by", "developer")
    transaction = ctx.memory.openTransaction()
    current = _lifecycle(transaction)
    state = current["state"]
    if action == "activate":
        if state == "active":
            transaction.commit()
            return {"state": state, "changed": False}
        updated = _setLifecycle(transaction, current, "active", now=now, by=by,
                                reason=payload.get("reason") or "activated")
        if state == "inactive":
            notice = {"kind": "activation", "at": now, "inactiveSince": current.get("since")}
            transaction.set(_LIFECYCLE, {**updated, "notices": [*updated.get("notices", []), notice],
                                         "deactivatedBy": None})
    elif action == "deactivate":
        if state == "inactive":
            transaction.commit()
            return {"state": state, "changed": False}
        updated = _setLifecycle(transaction, current, "inactive", now=now, by=by,
                                reason=payload.get("reason") or "deactivated")
        transaction.set(_LIFECYCLE, {**updated, "deactivatedBy": by, "stateBeforeDeactivation": state})
    elif action == "rest":
        if state != "active":
            raise RuntimeError(f"AI Buddy can rest only when active (it is {state}).")
        _setLifecycle(transaction, current, "resting", now=now, by=by, reason=payload.get("reason") or "rest")
    elif action == "wake":
        if state != "resting":
            raise RuntimeError(f"AI Buddy is not resting (it is {state}).")
        _setLifecycle(transaction, current, "active", now=now, by=by, reason=payload.get("reason") or "woken")
    else:
        raise ValueError(f"Unknown lifecycle action {action!r}.")
    transaction.commit()
    return {"state": _lifecycle(ctx.memory)["state"], "changed": True, "previous": state}


def describeRules(ctx, payload):
    """Read-only: rules in order, optionally including retired ones."""
    payload = payload if isinstance(payload, dict) else {}
    rules = _rules(ctx.memory, includeRetired=bool(payload.get("includeRetired")))
    scope = payload.get("scope")
    return [rule for rule in rules if scope is None or rule["scope"] == scope]


def addRule(ctx, payload):
    """Developer rule: protected unless "editable" is true."""
    payload = _requirePayload(payload, "text")
    now = _now(payload)
    transaction = ctx.memory.openTransaction()
    ruleId = _newRule(transaction, text=payload["text"], scope=payload.get("scope", "general"),
                      editable=bool(payload.get("editable", False)), origin={"by": "developer", "at": now},
                      now=now, order=payload.get("order"))
    transaction.commit()
    return {"ruleId": ruleId}


def changeRule(ctx, payload):
    """Developer authority over any rule: revise, retire, reorder (order value), editable (true/false)."""
    payload = _requirePayload(payload, "ruleId", "action")
    now = _now(payload)
    address = _addressOf(payload["ruleId"])
    transaction = ctx.memory.openTransaction()
    rule = _load(transaction, address)
    if rule is None or payload["ruleId"][0] != "r":
        raise ValueError(f"Unknown rule {payload['ruleId']!r}.")
    action = payload["action"]
    revision = {"at": now, "by": "developer", "action": action}
    if action == "revise":
        text = payload.get("text")
        if type(text) is not str or not text.strip():
            raise ValueError("Revising a rule needs text.")
        updated = {**rule, "text": text.strip(), "revisions": [*rule["revisions"], {**revision, "previousText": rule["text"]}]}
    elif action == "retire":
        updated = {**rule, "active": False, "retiredAt": now, "revisions": [*rule["revisions"], revision]}
    elif action == "reorder":
        order = payload.get("order")
        if not isinstance(order, (int, float)):
            raise ValueError("Reordering a rule needs a numeric order.")
        updated = {**rule, "order": order, "revisions": [*rule["revisions"], {**revision, "previousOrder": rule["order"]}]}
    elif action == "editable":
        updated = {**rule, "editable": bool(payload.get("value")),
                   "revisions": [*rule["revisions"], {**revision, "editable": bool(payload.get("value"))}]}
    else:
        raise ValueError(f"Unknown rule action {action!r}.")
    transaction.set(address, updated)
    transaction.commit()
    return {"ruleId": rule["id"], "action": action}


def describeSchedule(ctx, payload):
    """Read-only: eligibility and selection as they would be decided now."""
    decision = evaluateSchedule(ctx.memory, _now(payload))
    return {key: decision[key] for key in ("policy", "now", "selected", "reason", "candidates")}


def describePersonality(ctx, payload):
    """Read-only: returns the authoritative personality."""
    return _load(ctx.memory, _PERSONALITY)


def searchMemory(ctx, payload):
    """Read-only: runs retrieval for a developer query and shows what is stored."""
    payload = _requirePayload(payload, "query")
    limit = payload.get("limit", 10)
    material = _memoryMaterial(ctx.memory)
    byId = {item["id"]: item for item in material}
    hits = retrieve(material, payload["query"], limit=limit)
    return {
        "method": RETRIEVAL_METHOD,
        "storedCount": len(material),
        "results": [{**hit, "text": describeMaterial(byId[hit["id"]])} for hit in hits],
    }


def injectMemory(ctx, payload):
    """Developer intervention: establishes a memory marked as developer-provided."""
    payload = _requirePayload(payload, "text")
    text = payload["text"]
    if type(text) is not str or not text.strip():
        raise ValueError("Injected memory text must be a non-empty string.")
    at = _now(payload)
    transaction = ctx.memory.openTransaction()
    memoryId, memoryAddress = _allocate(transaction, "memory")
    transaction.set(memoryAddress, {
        "id": memoryId, "kind": "deliberate", "text": text.strip(), "establishedAt": at,
        "origin": {"by": "developer", "at": at},
    })
    transaction.commit()
    return {"memoryId": memoryId}


def describeProvenance(ctx, payload):
    """Read-only: follows one memory or experience to the processing that produced it."""
    payload = _requirePayload(payload, "id")
    item = _load(ctx.memory, _addressOf(payload["id"]))
    if item is None:
        return None
    origin = item.get("origin", {})
    inferenceId = origin.get("inferenceId") or item.get("inferenceId")
    inference = None if inferenceId is None else _load(ctx.memory, _addressOf(inferenceId))
    sources = origin.get("sourceExperienceIds") or []
    if not sources and inference is not None:
        sources = _work(ctx.memory, inference["workId"])["inputExperienceIds"]
    return {
        "item": item,
        "producedBy": origin.get("by", "ai-buddy" if inference else "observation"),
        "inference": None if inference is None else {
            "id": inference["id"],
            "thinking": inference.get("interpretation", {}).get("thinking"),
            "developerInterventions": inference.get("interventions", []),
        },
        "sourceExperiences": [_load(ctx.memory, _addressOf(source)) for source in sources],
    }


def addWork(ctx, payload):
    """Developer Queue directive: establishes a task or recheck, optionally Important."""
    payload = _requirePayload(payload, "kind")
    kind = payload["kind"]
    if kind not in ("task", "recheck"):
        raise ValueError("Developers can establish 'task' or 'recheck' work.")
    now = _now(payload)
    purpose = payload.get("purpose")
    extra = {}
    if kind == "task" and (type(purpose) is not str or not purpose.strip()):
        raise ValueError("A task requires a purpose.")
    transaction = ctx.memory.openTransaction()
    if kind == "recheck":
        original = _load(transaction, _addressOf(payload.get("recheckOf", "")))
        if original is None or original["state"] != "completed":
            raise ValueError("A recheck requires a completed thing to do.")
        extra["recheckOf"] = original["id"]
    workId = _newWork(
        transaction, kind=kind, now=now, origin={"by": "developer", "at": now},
        purpose=purpose.strip() if isinstance(purpose, str) and purpose.strip() else None,
        important=bool(payload.get("important")), optional=bool(payload.get("optional")),
        dependsOn=payload.get("dependsOn", []),
        notBefore=_shift(now, payload["notBeforeSeconds"]) if payload.get("notBeforeSeconds") else None,
        expiresAt=_shift(now, payload["expiresInSeconds"]) if payload.get("expiresInSeconds") else None,
        extra=extra)
    transaction.commit()
    return {"workId": workId}


def requestChoice(ctx, payload):
    """Developer Choice directive: asks inference to choose among open undertakings."""
    payload = _requirePayload(payload, "candidateIds")
    candidateIds = payload["candidateIds"]
    if not isinstance(candidateIds, list) or len(candidateIds) < 2:
        raise ValueError("A choice requires at least two candidate ids.")
    now = _now(payload)
    transaction = ctx.memory.openTransaction()
    for candidateId in candidateIds:
        candidate = _load(transaction, _addressOf(candidateId))
        if candidate is None or candidate["state"] in _TERMINAL:
            raise ValueError(f"Choice candidate {candidateId!r} is not an open thing to do.")
    workId = _newWork(transaction, kind="choice", now=now, origin={"by": "developer", "at": now},
                      purpose=payload.get("purpose"), important=True, extra={"candidateIds": list(candidateIds)})
    transaction.commit()
    return {"workId": workId}


def changeWork(ctx, payload):
    """Developer intervention on one thing to do.

    actions: suspend, resume, abandon, amend (purpose), important (value),
    interrupt (the Interrupt directive: make it current at the next boundary).
    """
    payload = _requirePayload(payload, "workId", "action")
    action = payload["action"]
    now = _now(payload)
    transaction = ctx.memory.openTransaction()
    work = _work(transaction, payload["workId"])
    if work["state"] in _TERMINAL:
        raise RuntimeError(f"{work['id']} has already ended ({work['state']}).")
    last = _lastInference(transaction, work)
    if last is not None and last["state"] == "prepared" and action in ("suspend", "abandon", "amend"):
        transaction.set(_addressOf(last["id"]), {**last, "state": "superseded", "supersededBy": f"developer-{action}",
                                                 "supersededAt": now})
    intervention = {"by": "developer", "action": action, "at": now}
    if action == "suspend":
        updated = {**work, "state": "suspended", "stateBeforeSuspend": work["state"]}
    elif action == "resume":
        if work["state"] != "suspended":
            raise RuntimeError(f"{work['id']} is not suspended.")
        updated = {**work, "state": work.get("stateBeforeSuspend") or "pending"}
    elif action == "abandon":
        _closeWork(transaction, {**work, "interventions": [*work.get("interventions", []), intervention]},
                   state="abandoned", reason="abandoned-by-developer", at=now)
        transaction.commit()
        return {"workId": work["id"], "state": "abandoned"}
    elif action == "amend":
        purpose = payload.get("purpose")
        if type(purpose) is not str or not purpose.strip():
            raise ValueError("Amending requires a new purpose.")
        step = {"kind": "amended", "at": now, "by": "the developer", "purpose": purpose.strip()}
        updated = {**work, "purpose": purpose.strip(), "steps": [*work["steps"], step]}
    elif action == "important":
        updated = {**work, "important": bool(payload.get("value", True))}
    elif action == "interrupt":
        updated = {**work, "interrupt": True}
    else:
        raise ValueError(f"Unknown work action {action!r}.")
    transaction.set(_addressOf(work["id"]), {**updated, "interventions": [*work.get("interventions", []), intervention]})
    transaction.commit()
    return {"workId": work["id"], "state": updated["state"]}


def changeSituation(ctx, payload):
    """Developer State / Important State: add information, or deactivate it."""
    payload = _requirePayload(payload, "action")
    now = _now(payload)
    transaction = ctx.memory.openTransaction()
    if payload["action"] == "add":
        text = payload.get("text")
        if type(text) is not str or not text.strip():
            raise ValueError("State information requires text.")
        itemId, address = _allocate(transaction, "situation")
        transaction.set(address, {"id": itemId, "text": text.strip(), "important": bool(payload.get("important")),
                                  "active": True, "establishedAt": now, "by": "developer"})
        result = {"situationId": itemId}
    elif payload["action"] == "remove":
        address = _addressOf(payload.get("situationId", ""))
        item = _load(transaction, address)
        if item is None:
            raise ValueError("Unknown state information.")
        transaction.set(address, {**item, "active": False, "removedAt": now})
        result = {"situationId": item["id"]}
    else:
        raise ValueError(f"Unknown state action {payload['action']!r}.")
    transaction.commit()
    return result


CAPABILITIES = {
    "aiBuddy.input.receive@1": receiveInput,
    "aiBuddy.cycle.prepare@1": prepareCycle,
    "aiBuddy.cycle.execute@1": executeInference,
    "aiBuddy.cycle.process@1": processInference,
    "aiBuddy.cycle.recordInterrupted@1": recordInterrupted,
    "aiBuddy.outbox.pending@1": pendingOutbox,
    "aiBuddy.outbox.acknowledge@1": acknowledgeOutbox,
    "aiBuddy.debug.prompt.replace@1": replacePrompt,
    "aiBuddy.debug.output.replace@1": replaceOutput,
    "aiBuddy.debug.record.describe@1": describeRecord,
    "aiBuddy.debug.status@1": describeStatus,
    "aiBuddy.debug.schedule@1": describeSchedule,
    "aiBuddy.debug.personality.describe@1": describePersonality,
    "aiBuddy.debug.memory.search@1": searchMemory,
    "aiBuddy.debug.memory.inject@1": injectMemory,
    "aiBuddy.debug.memory.provenance@1": describeProvenance,
    "aiBuddy.debug.work.add@1": addWork,
    "aiBuddy.debug.work.choose@1": requestChoice,
    "aiBuddy.debug.work.change@1": changeWork,
    "aiBuddy.debug.situation.change@1": changeSituation,
    "aiBuddy.debug.game.start@1": startGame,
    "aiBuddy.debug.lifecycle.change@1": changeLifecycle,
    "aiBuddy.game.minesweeper.state@1": describeGame,
    "aiBuddy.debug.rule.list@1": describeRules,
    "aiBuddy.debug.rule.add@1": addRule,
    "aiBuddy.debug.rule.change@1": changeRule,
    "aiBuddy.debug.passive.start@1": startPassive,
}


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------

def _validatePersonality(value) -> dict:
    """Requires the minimal authoritative personality shape."""
    if not isinstance(value, dict):
        raise TypeError("Personality must be a JSON object.")
    for key in ("name", "summary"):
        if type(value.get(key)) is not str or not value[key].strip():
            raise ValueError(f"Personality requires a non-empty string {key!r}.")
    return value


class _TrackedMemory:
    """Forwards to a memory facade, remembering every transaction opened."""

    def __init__(self, memory) -> None:
        self._memory = memory
        self.opened = []

    def openTransaction(self):
        transaction = self._memory.openTransaction()
        self.opened.append(transaction)
        return transaction

    def __getattr__(self, name):
        return getattr(self._memory, name)


class _GuardedContext:
    """Forwards to a CodeEntryContext with transaction tracking on memory."""

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self.memory = _TrackedMemory(ctx.memory)

    def __getattr__(self, name):
        return getattr(self._ctx, name)


def _guarded(handler):
    """Wraps a capability so a raised exception never leaves a transaction open.

    Without this, a capability that raises after opening a transaction (for
    example on invalid developer input) leaves an active child transaction,
    and the platform cannot abort the Job's own transaction.
    """
    def invoke(ctx, payload):
        guarded = _GuardedContext(ctx)
        try:
            return handler(guarded, payload)
        except BaseException:
            for transaction in reversed(guarded.memory.opened):
                try:
                    transaction.abort()
                except Exception:  # noqa: BLE001 - already committed or aborted.
                    pass
            raise

    invoke.__name__ = handler.__name__
    invoke.__doc__ = handler.__doc__
    return invoke


def onLoad(ctx):
    """Registers AI Buddy capabilities, each guarded against leaked transactions."""
    for capabilityId, handler in CAPABILITIES.items():
        ctx.capabilities.register(capabilityId, _guarded(handler))


def onApplicationCreate(ctx, state):
    """Establishes a fresh AI Buddy: authoritative personality, empty personal memory."""
    personality = _validatePersonality(ctx.io.readJson(ctx.packRoot / "personality.json"))
    transaction = ctx.memory.openTransaction()
    transaction.set(_META, {"schema": STATE_SCHEMA, "createdAt": _now({}), "migrations": []})
    transaction.set(_LIFECYCLE, _lifecycle(transaction))
    transaction.set(_PERSONALITY, personality)
    for sequenceAddress, _base, _prefix in _SEQUENCES.values():
        transaction.set(sequenceAddress, 0)
    transaction.set(_WORK_OPEN, [])
    transaction.set(_OUTBOX_DELIVERED, 0)
    _seedRules(ctx, transaction, _now({}))
    transaction.commit()


_MIGRATIONS = {
    # from schema: (to schema, sequences to add)
    "aibuddy.state@1": ("aibuddy.state@2", ("memory",)),
    "aibuddy.state@2": ("aibuddy.state@3", ("situation",)),
    "aibuddy.state@3": ("aibuddy.state@4", ("game",)),
    "aibuddy.state@4": ("aibuddy.state@5", ("rule",)),
}


def onApplicationLoad(ctx, state):
    """Migrates restored state step by step to the current schema.

    Migrations add empty collections and record themselves in aibuddy/meta.
    Existing records are not rewritten; readers default fields that earlier
    schemas did not have.
    """
    meta = _load(ctx.memory, _META)
    if not isinstance(meta, dict):
        raise RuntimeError(f"Unsupported AI Buddy state: {meta!r}.")
    schema = meta.get("schema")
    if schema == STATE_SCHEMA:
        _recordRestart(ctx)
        return
    if schema not in _MIGRATIONS:
        raise RuntimeError(f"Unsupported AI Buddy state schema: {schema!r}.")
    transaction = ctx.memory.openTransaction()
    migrations = list(meta.get("migrations", []))
    while schema in _MIGRATIONS:
        target, sequences = _MIGRATIONS[schema]
        for kind in sequences:
            transaction.set(_SEQUENCES[kind][0], 0)
        if "rule" in sequences:
            _seedRules(ctx, transaction, _now({}))
        migrations.append({"from": schema, "to": target, "at": _now({})})
        schema = target
    transaction.set(_META, {**meta, "schema": schema, "migrations": migrations})
    transaction.commit()
    _recordRestart(ctx)


def _recordRestart(ctx) -> None:
    """Records that the application restarted, for the next prompt to state."""
    transaction = ctx.memory.openTransaction()
    lifecycle = _lifecycle(transaction)
    notice = {"kind": "restart", "lastSeenAt": lifecycle.get("lastSeenAt")}
    transaction.set(_LIFECYCLE, {**lifecycle, "notices": [*lifecycle.get("notices", []), notice],
                                 "restarts": lifecycle.get("restarts", 0) + 1})
    transaction.commit()
