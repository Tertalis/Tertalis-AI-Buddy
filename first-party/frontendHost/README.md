# Frontend host (`frontend.host`)

Pack ID `frontend.host`, version `0.1.0`, kind `modPack`. One loopback browser
frontend per ApplicationRun. Python stays authoritative; browser tabs (Views)
only render tracks and send Invocations. Design:
`design/working/browser-frontend-checkpoint-01.txt`.

## Use

Add `first-party/frontendKit` and `first-party/frontendHost` to the
PackResolver roots and include both explicitly in the ManualActivationPlan,
after the AppPack:

```python
plan = ManualActivationPlan(packIds=("myApp", "frontend.kit", "frontend.host"))
config = {"frontendHost": {"title": "My app", "port": 0, "maxViews": 8,
                           "main": {"packId": "myApp", "codeEntryId": "ui"}}}
```

`main` may be omitted when exactly one active browser main CodeEntry exists.
The AppPack declares its browser entry (every imported module must be
declared; sources are served at `/pack/<packId>/<declared source path>`):

```json
{"id": "ui", "source": "ui/main.mjs", "format": "js-module-source@1"}
```

and exports `mount(element, session)` (see `first-party/frontendKit/README.md`).

The loop owner (host code that drives the Application) then calls, once:

```python
channel = runtime.invokeCapability("frontend.host.channel@1")   # seals served sources
print(runtime.invokeCapability("frontend.host.connection@1")["url"])
channel.declareTrack("status", "state", initial={...})
channel.declareTrack("history", "log", maxItems=200)   # older items are released
channel.set("status", {...})            # any thread; values frozen as JSON
for item in channel.waitInvocations(timeout):   # Invocation(invocationId, target, arguments, expect)
    channel.resolve(item.invocationId, value)   # or channel.fail(item.invocationId, "conflict", "...")
```

`channel.wake()` returns a blocked `waitInvocations` early (for other input
sources). The channel never calls the runtime; the loop owner runs Jobs.

## HTTP surface

| Path | Auth | Purpose |
|---|---|---|
| `/`, `/boot.mjs`, `/boot.css` | none | generic bootstrap page |
| `/pack/<packId>/<source>` | none | declared `js-module-source@1` bytes (exactly what was hashed; `ETag` = sha256) |
| `GET /session` | Bearer | run id, title, main entry URL, declared tracks |
| `GET /snapshot?track=` | Bearer | authoritative cached track state |
| `GET /stream?tracks=&run=&after=name:pos` | Bearer | `text/event-stream`: `run`, `emit`, `gap`, keep-alive |
| `POST /invoke` | Bearer + same Origin | Invocation; `202` Accepted, `200` known outcome, `4xx` FlowError |

Results and FlowErrors arrive on the reserved `flow.invocations` log track.
Duplicate invocation ids are idempotent. Positions from another run are
ignored. At most `maxViews` streams; extra ones get `429`.

The channel retains at most 1024 known invocation ids by default, including
actions already drained by the loop owner but still pending. Pending ids are
never evicted; admission returns `busy` when they fill the limit. Settled ids
may be evicted, so deduplication is limited to retained ids. Log snapshots
retain a tail bounded by both item count and 1 MiB of encoded items, keeping
responses within the frontend kit's 2 MiB body limit. Results are frozen when
resolved, including the cached reply used for duplicate requests.

## Security

G-01 posture via `backend/transport/loopbackHttp.py`: literal `127.0.0.1`,
exact Host, exact same Origin, constant-time Bearer, no logging, `no-store`,
restrictive CSP, bounded bodies, connection cap. Recorded deviation: the launch
URL carries the credential in its fragment (`#token=`); fragments are never
sent to the server, the page removes it with `history.replaceState` and keeps
it in this tab's `sessionStorage` so reloading works (per tab, gone when the tab
closes, forgotten when rejected, e.g. after a restart rotated it). Query-string credentials are rejected. Bootstrap and Pack
sources need no credential because browser module imports cannot send one;
they are public code and still Host-checked.

## Tests

`tests/first_party/frontendHost/` (pytest). Optional real-browser smoke:
`python tests/first_party/frontendHost/smoke.py --playwright <dir> --browser <exe> --output <dir>`
(needs Node.js and Playwright; not part of the regular suite).
The smoke checks five simultaneous Views, a sixth View exceeding the cap,
conflicting actions, and a real host restart while an accepted action is pending.
