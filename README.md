# Tertalis AI Buddy

**Status: planned application; AI Buddy itself has not yet been implemented.**

AI Buddy is a proposed long-running, locally oriented AI application built on
Tertalis. Its workstream will explore continuous orchestration, concurrent
applications, shared model and runtime facilities, cancellation, and eventually
specialist voice capabilities such as speech recognition and synthesis.

**There is currently no AI Buddy AppPack, chat interface, voice assistant, or
application launcher in this repository.** This is a prepared development
workstream with a pinned snapshot of the Tertalis platform, shared reusable
facilities, tests and historical coordination documents. Please do not mistake
platform capabilities for implemented AI Buddy features.

## Getting the development environment ready

On Windows, clone the repository and run the platform's root setup script:

```powershell
git clone https://github.com/Tertalis/Tertalis-AI-Buddy.git
cd Tertalis-AI-Buddy
.\setup.bat
```

This prepares the platform's repository-local Python runtime and developer
tools. It does **not** create an AI Buddy application or start a model server.
There is no application-specific setup script to run yet. See
[SETUP.md](SETUP.md) for prerequisites, Python versions and the explicit
fallback to Python 3.14.

For existing platform tests (not AI Buddy acceptance tests), after setup:

```powershell
.\python-embedded\python.exe -X frozen_modules=off -m pytest tests
```

This is the repository's general test entry point; it is not evidence that
the test suite passed during migration. Individual test modules may require
their documented environment or external prerequisites.

## Development direction and boundaries

The earliest intended AI Buddy experience may be deliberately simple:
a terminal interaction history, local model execution and potentially
speech output, providing genuine pressure on long-running orchestration.
These are **goals, not existing features**. Scheduling, shared provider
ownership and capability boundaries must be worked out from concrete
implementation evidence rather than inventing a complete product in advance.

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
[EnterTheNameHere/Turnix](https://github.com/Tertalis/Tertalis/tree/integration).
The `ai-workflow/` files inherited into this repository are historical
snapshots, **not** a separately active coordination service. Consult
the canonical AI Buddy handoff and workstream state there before taking
ownership of any work.

Read [AGENTS.md](AGENTS.md) for AI development orientation and protocol.
Platform and application revisions are adopted through explicit,
verified integration, not by automatically tracking new platform commits.

## Releases

No usable AI Buddy release exists yet. A future Tertalis PackageManager,
AppPack distribution model and `tertalis` CLI are architectural direction,
not currently available installation or run commands for AI Buddy.
