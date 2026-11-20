# AI agent entry point — Tertalis AI Buddy

This is an unimplemented application workstream that contains a complete
pinned Tertalis platform snapshot. Do **not** describe the repository as a
working AI buddy, invent a startup command, or treat copied coordination
files as independent authority.

## Read before working

1. Read [README.md](README.md) for actual implementation status and
   [SETUP.md](SETUP.md) for repository-local Windows setup.
2. Read the canonical
   [coordination index](https://github.com/EnterTheNameHere/Turnix/blob/actant-integration/ai-workflow/index.txt)
   and
   [protocol](https://github.com/EnterTheNameHere/Turnix/blob/actant-integration/ai-workflow/coordination-protocol.txt).
3. On that same canonical ref, read
   `ai-workflow/handoff/ai-buddy-thread.txt`,
   `ai-workflow/state/ai-buddy.txt`, current assignment and inbox
   before making an ownership claim or implementation change.
4. Read relevant existing `docs/` and `design/` contracts, but
   recognize that some historical documents and coordination snapshots
   still use the earlier Turnix/Actant terminology. They are not
   necessarily authoritative over newer canonical platform design.

## Ownership and compatibility

- AI Buddy owns its own prospective application behavior; no AI Buddy
  AppPack has yet been materialized.
- Tertalis owns the shared `backend/`, reusable `first-party/`
  facilities and platform-facing guarantees.
- Reusable gaps discovered by application work are routed to the
  shared Integration workstream rather than patched by creating a
  competing local platform.
- A new repository does not establish a new Coordinator or independent
  inbox. The authoritative file-based coordination system remains on
  `EnterTheNameHere/Turnix`, ref `actant-integration`, until an
  explicitly approved cutover.
- Preserve accepted platform revisions until an authorized Integrator
  has verified a replacement. Do not overwrite shared history,
  delete branches or perform destructive tests without authorization.

## Setup and evidence

From the repository root on Windows:

```powershell
.\setup.bat
.\python-embedded\python.exe -X frozen_modules=off -m pytest tests
```

These commands prepare and test the **platform checkout**, not an AI Buddy
implementation. Root setup must never be modified to discover and initialize
applications. There is no separate AI Buddy setup/launcher yet.
Report the exact refs, actual tests performed and unverified conditions.
Follow the canonical coordination handoff/completion protocol.
