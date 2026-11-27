# Tertalis AI Buddy

**Status: experimental terminal application; initial checkpoints A-F implemented.**

AI Buddy is a continuously running, locally oriented Tertalis AppPack with
personality, memory, work scheduling, developer breakpoints, Minesweeper,
rules and passive processing. See the
[application README](first-party/applications/aiBuddy/README.md) for behavior,
configuration, developer commands and experimental limits.

The AppPack and terminal launcher live under `first-party/applications/aiBuddy/`;
application tests live under `tests/first_party/applications/aiBuddy/`.
Browser UI integration and voice features remain future work.

## Adopted platform

This combined checkout adopts Tertalis/Tertalis `integration` commit
`82772a2ff6cc48a9fb8e776654b2a14bb119b6d7` (2026-11-27), replacing the
historical synchronized baseline `f30c393f7d723f9f8a897ff74a054aed1a1e4cd4`.
All 282 shared platform paths match that target. Platform `docs/`, `design/`
and operational `ai-workflow/` files are excluded; the application routing
stub and application-owned orientation are preserved.

Independent Windows/Python 3.15 adoption check: 1,876 tests passed, including
AI Buddy's 63 scripted-provider tests. Native Ruff/ty tool verification and
the imported frontend host's Chromium five-View/restart fixture smoke passed.
This does not establish live model behavior. The new `frontend.kit` and
`frontend.host` Packs are available; AI Buddy's launcher remains terminal-based.

## Getting the development environment ready

On Windows, clone the repository and run the platform's root setup script:

```powershell
git clone https://github.com/Tertalis/Tertalis-AI-Buddy.git
cd Tertalis-AI-Buddy
.\setup.bat
```

This prepares the platform's repository-local Python runtime and developer
tools. It does **not** create an AI Buddy application or start a model server.
Start an external llama-server, then run
`first-party\applications\aiBuddy\run.bat`; see the application README for
`--llama-url`, `--break` and other options. AI Buddy requires Python 3.15. See
[SETUP.md](SETUP.md) for prerequisites, Python versions and the explicit
fallback to Python 3.14.

For the combined platform and AI Buddy tests, after setup:

```powershell
.\python-embedded\python.exe -X frozen_modules=off -m pytest tests
```

This is the repository's general test entry point; it is not evidence that
the test suite passed during migration. Individual test modules may require
their documented environment or external prerequisites.

## Development direction and boundaries

AI Buddy currently uses the first-party llama.cpp provider in external mode,
with an application-owned loop driving Jobs and terminal controls. Browser UI,
managed provider adoption and voice capabilities remain separate work.

The AI Buddy workstream owns application-specific behavior. Reusable
runtime, hosting, communication and service facilities belong to the
Tertalis platform and should be proposed to its integration workstream
when genuine gaps are encountered. AI Buddy should be able to coexist
with other Tertalis applications without setting up an independent,
uncoordinated model/resource authority.

## Shared AI development coordination

This repository participates in Tertalis's single cooperative
cross-repository coordination system. The canonical file-based inbox
and state currently reside on the `integration` branch of
[Tertalis/Tertalis](https://github.com/Tertalis/Tertalis/tree/integration).
Only the local `ai-workflow/index.txt` routing stub is tracked here;
operational coordination remains in the platform repository. Consult
the canonical AI Buddy handoff and workstream state there before taking
ownership of any work.

Read [AGENTS.md](AGENTS.md) for AI development orientation and protocol.
Platform and application revisions are adopted through explicit,
verified integration, not by automatically tracking new platform commits.

## Releases

No usable AI Buddy release exists yet. A future Tertalis PackageManager,
AppPack distribution model and `tertalis` CLI are architectural direction,
not currently available installation or run commands for AI Buddy.
