# Frontend kit (`frontend.kit`)

Pack ID `frontend.kit`, version `0.1.0`, kind `modPack`. Every CodeEntry is a
`js-module-source@1` library: Python never executes them. A frontend host (the
first-party one is `frontend.host`) serves them at `/pack/frontend.kit/<source>`
so browser Views and Application UI entries can import them. Relative imports
between kit modules work because sources are served by their declared path.

Design: `design/working/browser-frontend-checkpoint-01.txt`.

| Module | Purpose |
|---|---|
| `http.mjs` | `MAX_BODY`, `ConnectionError`, `BodyError`, `readBoundedJson`, `requestOptions` (no cookies, cache, redirects or referrer), `requireLoopbackOrigin`, `isCredential`. Shared with `tools/observationViewer`. |
| `credential.mjs` | `launchCredential()`: reads `#token=...`, removes it with `history.replaceState`, and remembers it in this tab's `sessionStorage` (key `tertalis.frontend.credential`) so F5 / Ctrl+F5 work; `forgetCredential()` once the host rejects it. The only module that uses storage. |
| `eventStream.mjs` | Bounded `text/event-stream` parser over a `fetch` body (EventSource cannot send `Authorization`). |
| `stream.mjs` | `StreamClient`: one resumable stream per View; resumes with per-track positions; backs off; stops on 401/403/404. |
| `tracks.mjs` | `TrackStore`: state and log tracks, in-order application, gap detection, bounded log tail. |
| `invoke.mjs` | `InvocationClient`, `FlowError`: client-generated stable ids; Results arrive on the `flow.invocations` track; `unknown` when the outcome cannot be known. |
| `session.mjs` | `openSession()`: the `session` object passed to an Application's `mount(element, session)`. |
| `bootstrap.mjs` | `boot()`: generic page start-up used by the frontend host's page. |

The `session` object: `descriptor`, `runId`, `tracks` (a `TrackStore`:
`get(name)`, `subscribe(name, listener)`, `watch(name, listener)` which also
calls the listener immediately with the current track), `invoke(target, args, {expect})`,
`onState(listener)`, `close()`.

Snapshot recovery retries if a request fails or an Emit reveals another gap
while a snapshot is in flight. Closing the session cancels scheduled recovery.
A terminal stream failure (including an old credential rejected after a host
restart) settles pending actions as `unknown`. A fresh launch link is required
after credential rotation; existing tabs do not acquire the new credential.

Rules kept by every module (checked by
`tests/first_party/frontendKit/kit.test.mjs`): only `credential.mjs` may use
tab session storage; no localStorage, IndexedDB, cookies, console logging,
HTML-string sinks or `eval`; every module is declared in the manifest.
