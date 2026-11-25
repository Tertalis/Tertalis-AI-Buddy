# AI Buddy

**Status: materialization Checkpoint F (last of the initial scope) — experimental. Checkpoints A to E verified by the user on Python 3.15; Checkpoint F not yet run.**

AI Buddy is a continuously running AI companion built as a Tertalis AppPack.
Requirements and design: `ai-buddy-conceptual-design.txt`,
`ai-buddy-initial-materialization-requirements.txt` and `ai-buddy-handoff.txt`.
How it is implemented and why: `ai-buddy-implementation-design.txt`.

## What it does (Checkpoints A to F)

- Creates an instance with an authoritative personality (`pack/personality.json`,
  copied into Application state at creation) and empty personal memory.
- Records each user message almost literally and establishes a thing to do.
- Builds a fresh, situation-first prompt for every inference. No chat history.
- Breakpoint mode (`/break on` or `--break`) pauses twice per inference:
  before it (`/show` and `/edit` the prompt) and after the model answers but
  before anything in the answer is acted on (the response is printed; `/edit`
  it if you like). `/continue` goes on from either pause. `/raw` shows the
  last inference record with raw output and timing.
- Treats only text inside `<say>...</say>` as speech (provisional contract).
  Declining to speak is valid.
- Saves after changes and restores on the next start, including an inference
  that was prepared but not yet run.
- Memory (Checkpoint B): user messages and AI Buddy's own communication are
  kept as almost-literal experiences. AI Buddy can store deliberate memories
  with `<remember>`, each linked to the processing and messages it came from.
- Each prompt includes a few recent experiences and automatically selected
  memories that may be relevant, or says plainly that none were found.
- `<recall>` lets AI Buddy search its memory mid-thought; the undertaking
  continues with a fresh prompt containing its earlier steps and the results
  (or "no further memories"). Continuations are capped.

- Things to do (Checkpoint C): user input, tasks, rechecks, choices and
  deterministic periodic checks, with dependencies, scheduled times, expiry,
  suspend/resume/abandon/amend. Eligibility and selection are decided
  separately and the reasons are recorded (`/schedule`).
- Directives: State and Important State information (`/info`, `/info!`),
  Queue (`/task`), Important (`/task!`, `/important`), Interrupt (`/now`) and
  Choice (`/choose`). AI Buddy can set itself later work with `<intend>` and
  keep thinking with `<continue>`.
- New user input amends an open input undertaking. `--interrupt-policy cancel`
  stops a running inference for new input and keeps its partial output as an
  incomplete step; the default `finish` lets it complete first.
- Prompts show elapsed and active thinking time, with configurable pressure
  to conclude based on active time only (debugger pauses are not counted).

- Minesweeper (Checkpoint D): a deterministic game whose state lives in AI
  Buddy's state, independent of inference. It is an optional activity that
  uses spare time and always yields to input and other work. Moves
  (`<reveal>`, `<flag>`, `<unflag>`) exist only while a game is in progress;
  a related message can act on the game. Start with `/game`, view with
  `/board`; AI Buddy can start one with `<activity>minesweeper</activity>`
  and stop with `<stop>`.
- Lifecycle: active; resting (`<rest>` or `/rest`: optional work waits, other
  work wakes it); explicitly inactive (`/deactivate`: input is recorded but
  not processed, only passive periodic checks run, nothing wakes it except
  `/activate`); application quiescence (`/quiesce`: no processing at all).
  The first prompt after activation or a restart says how long AI Buddy was
  inactive or the application was not running.

- Rules (Checkpoint E): ordered, scoped instructions shown in prompts where
  they apply (`general`, `minesweeper`, `user-input`, `task`). Initial rules
  come from `pack/rules.json` and are protected. AI Buddy can establish rules
  (`<rule>`), change its own editable ones (`<revise>`, `<reorder>`,
  `<retire>`) and skip one for an occasion (`<disregard>`); changes to
  protected rules are rejected and recorded. Losing a game establishes an
  investigation that shows the final board with all mines. Developer: `/rules`,
  `/rule`, `/rule+`, `/revise-rule`, `/retire-rule`, `/order-rule`, `/protect`,
  `/unprotect`.

- Passive processing (Checkpoint F): memory processing (reflections on
  memories not yet reviewed, and scheduled reactions) and salient-subject
  generation (up to N optional candidate tasks that expire unstarted). Both
  may run while AI Buddy is inactive or resting, never speak or wake it, and
  reach active processing only through ordinary retrieval and reactions.
  Start them with `/reflect` and `/salience`, or periodically, e.g.
  `--settings '{"periodic": [{"name": "reflect", "everySeconds": 1800, "establishes": "memory-processing"}, {"name": "ideas", "everySeconds": 86400, "establishes": "salient-subjects"}], "passive": {"maxCandidates": 3, "candidateLifetimeSeconds": 86400}}'`.
  Nothing passive runs by default.

## Layout

```
pack/          AppPack (manifest, codeEntry.py, personality.json, rules.json)
host/driver.py host-side driver: runs capabilities as Jobs, debugger controls
run.py         terminal launcher
run.bat        Windows launcher using the root python-embedded
local/         saves (ignored by Git)

Tests live with the repository's other tests, in
tests/first_party/applications/aiBuddy/ (runtime-contract tests with a
scripted stub provider; conftest.py holds the shared harness).
```

## Running

1. Run the root Tertalis `setup.bat`.
2. Start a llama-server yourself, for example:
   `llama-server -m C:\models\your-model.gguf --port 8080 -c 16384`
3. Run `run.bat` (add `--break` to start with the inference breakpoint on).

Useful options: `--llama-url`, `--context-window`, `--temperature`,
`--max-tokens`, `--timeout`, `--new`, `--load <applicationId>`, `--list`,
`--memory '{"recentLimit": 4, "automaticLimit": 5, "recallLimit": 5, "maxContinuations": 4}'`
(the defaults shown), `--interrupt-policy finish|cancel`, and `--settings` for
further options, for example
`--settings '{"periodic": [{"name": "heartbeat", "everySeconds": 10}], "intentionLifetimeSeconds": 86400, "timePressure": [{"afterSeconds": 3, "text": "..."}]}'`.
By default the most recently used instance is loaded; `--new` creates another.

Plain lines are messages to AI Buddy. `/help` lists developer commands.
Lines prefixed with `[diag]` are diagnostics, never AI Buddy speech.

## Verification

From the repository root, after setup:

```powershell
.\python-embedded\python.exe -X frozen_modules=off -m pytest tests\first_party\applications\aiBuddy
```

Instances saved by earlier checkpoints are migrated to the current state
schema when loaded.

These tests use a scripted provider: they check orchestration, state and
persistence, not model behaviour or retrieval quality.
