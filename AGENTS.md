# AI agent entry point — Tertalis AI Buddy

This repository contains the experimental AI Buddy AppPack, terminal launcher,
application tests and a complete pinned Tertalis platform source snapshot.
The adopted platform SHA is recorded in README.md. Do not treat platform
frontend availability as completed AI Buddy browser UI integration.

## Read before working

1. Read [README.md](README.md) for actual implementation status and
   [SETUP.md](SETUP.md) for repository-local Windows setup.
2. Read the canonical
   [coordination index](https://github.com/Tertalis/Tertalis/blob/integration/ai-workflow/index.txt)
   and
   [protocol](https://github.com/Tertalis/Tertalis/blob/integration/ai-workflow/coordination-protocol.txt).
3. On that same canonical ref, read
   `ai-workflow/handoff/ai-buddy-thread.txt`,
   `ai-workflow/state/ai-buddy.txt`, current assignment and inbox
   before making an ownership claim or implementation change.
4. Read relevant current `docs/` and `design/` contracts in
   `Tertalis/Tertalis` `integration`, but recognize that some historical documents and coordination snapshots
   still use the earlier Turnix/Actant terminology. They are not
   necessarily authoritative over newer canonical platform design.

## Ownership and compatibility

- AI Buddy owns first-party/applications/aiBuddy/ and its application tests
  under tests/first_party/applications/aiBuddy/. The AppPack and terminal
  launcher are implemented; the application README documents their behavior.
- Tertalis owns the shared `backend/`, reusable `first-party/`
  facilities and platform-facing guarantees.
- Reusable gaps discovered by application work are routed to the
  shared Integration workstream rather than patched by creating a
  competing local platform.
- A new repository does not establish a new Coordinator or independent
  inbox. The authoritative file-backed coordination system is on
  `Tertalis/Tertalis`, ref `integration`. A future GitHub-native
  coordination mirror remains secondary until an approved cutover.
- Preserve accepted platform revisions until an authorized Integrator
  has verified a replacement. Do not overwrite shared history,
  delete branches or perform destructive tests without authorization.

## Setup and evidence

From the repository root on Windows:

```powershell
.\setup.bat
.\python-embedded\python.exe -X frozen_modules=off -m pytest tests
```

Root setup prepares platform internals only; pytest tests exercises both
platform and AI Buddy tests. Root setup must never discover or initialize
applications. Run AI Buddy with first-party/applications/aiBuddy/run.bat
after starting an external llama-server; see the application README.
AI Buddy requires Python 3.15.
Report the exact refs, actual tests performed and unverified conditions.
Follow the canonical coordination handoff/completion protocol.

## Platform documentation authority

For Tertalis platform contracts and design, consult the CURRENT
`Tertalis/Tertalis` `integration` `design/index.txt` and relevant
authoritative platform design/docs, because this Application does not carry copied root `design/`
or `docs/` trees. Retrieve only the
areas pertinent to the assigned task and its dependencies; do not load
the entire design corpus by default. Current documentation does NOT
authorize upgrading this Application's accepted Tertalis source SHA.
Some existing platform design specifications still reside in `docs/`;
their relocation to `design/` and future creator/user docs are not
yet complete. Release-version applicability annotations are future
work; use exact Git SHAs in current integration reports.

## Shared ai-workflow boundary

Every assistant, implementer, integrator, coordinator and design thread must
read the current canonical ai-workflow instructions at
[Tertalis/Tertalis integration](https://github.com/Tertalis/Tertalis/blob/integration/ai-workflow/index.txt)
before working. This repository's `ai-workflow/index.txt` is only a local
routing stub; its `integration` branch is the application's development
branch, not a coordination authority. All operational inboxes, messages,
workstream state and repository-location registry are maintained only in
`Tertalis/Tertalis` on `integration`. Do not create a local inbox or rely
on copied historical mail. For other applications, consult Tertalis's
canonical registry rather than recording their addresses here.
