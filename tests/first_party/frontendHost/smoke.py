# file: tests/first_party/frontendHost/smoke.py ; version: 2
"""Explicit real-browser verification of frontend.kit + frontend.host with a fixture Application.

Run from the repository root:
    python tests/first_party/frontendHost/smoke.py --playwright <playwright package dir> \
        --browser <chromium/edge executable> --output <evidence dir>
The launch URL (with credential fragment) goes to the browser driver over private stdin only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Event, Thread

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.packs.runtime import ManualActivationPlan, PackResolver  # noqa: E402
from backend.runtime import RuntimeHost  # noqa: E402
from backend.save import ApplicationStore  # noqa: E402

MAIN = """\
// Fixture browser main for the frontend host smoke.
export function mount(root, session) {
  const status = document.createElement('output'); status.id = 'status';
  const result = document.createElement('output'); result.id = 'result';
  const echo = document.createElement('button'); echo.id = 'echo'; echo.textContent = 'Echo';
  const resume = document.createElement('button'); resume.id = 'continue'; resume.textContent = 'Continue';
  const pending = document.createElement('button'); pending.id = 'pending'; pending.textContent = 'Pending';
  const state = document.createElement('output'); state.id = 'connection';
  root.append(status, result, echo, resume, pending, state);
  session.tracks.watch('status', track => { status.textContent = track ? JSON.stringify(track.value) : ''; });
  session.onState(value => { state.textContent = value; });
  const run = async (target, args, options) => {
    try { result.textContent = 'result:' + JSON.stringify(await session.invoke(target, args, options)); }
    catch (error) { result.textContent = 'error:' + error.code; }
  };
  echo.addEventListener('click', () => run('test.echo', {n: 1}));
  resume.addEventListener('click', () => run('test.continue', {}, {expect: {pause: 'p1'}}));
  pending.addEventListener('click', () => run('test.pending', {}));
  root.dataset.ready = '1';
}
"""


def loopOwner(channel, stopped: Event) -> None:
    """Fixture Application loop: echo, and a single-use breakpoint guarded by its pause id."""
    paused = {"pause": "p1"}
    while not stopped.is_set():
        try:
            items = channel.waitInvocations(0.2)
        except RuntimeError:
            return
        for item in items:
            if item.target == "test.echo":
                channel.resolve(item.invocationId, item.arguments)
            elif item.target == "test.continue" and paused is not None and item.expect == paused:
                paused = None
                channel.resolve(item.invocationId, "continued")
            elif item.target == "test.continue":
                channel.fail(item.invocationId, "conflict", "The breakpoint is no longer current.")
            elif item.target == "test.pending":
                pass  # Deliberately accepted without a Result before the restart.
            else:
                channel.fail(item.invocationId, "unknown-target", "Unknown action.")


def run(args) -> dict:
    """Runs the smoke with a private stdin/stdout control protocol."""
    with tempfile.TemporaryDirectory(prefix="tertalis-frontend-") as folder:
        app = Path(folder) / "app"
        (app / "ui").mkdir(parents=True)
        (app / "manifest.json").write_text(json.dumps({"packId": "test.ui", "kind": "appPack", "version": "1.0.0", "codeEntries": [
            {"id": "ui", "source": "ui/main.mjs", "format": "js-module-source@1"}]}), encoding="utf-8")
        (app / "ui" / "main.mjs").write_text(MAIN, encoding="utf-8", newline="\n")
        host = RuntimeHost(applicationStore=ApplicationStore(Path(folder) / "saves"), packResolver=PackResolver(roots=(
            app, ROOT / "first-party" / "frontendKit", ROOT / "first-party" / "frontendHost")))
        host.start()
        stopped = Event()
        process = worker = None
        try:
            runtime = host.createApplication(appPackId="test.ui", plan=ManualActivationPlan(packIds=("test.ui", "frontend.kit", "frontend.host")),
                                             config={"frontendHost": {"title": "Frontend smoke", "maxViews": 5}})
            channel = runtime.invokeCapability("frontend.host.channel@1")
            channel.declareTrack("status", "state", initial={"step": 0})
            connection = runtime.invokeCapability("frontend.host.connection@1")
            worker = Thread(target=loopOwner, args=(channel, stopped), daemon=True)
            worker.start()
            evidence = args.output.resolve()
            evidence.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen([args.node, str(Path(__file__).with_name("browser-smoke.cjs")), str(args.playwright.resolve()),
                                        str(args.browser.resolve()), str(evidence)], cwd=ROOT, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            process.stdin.write(json.dumps({"url": connection["url"], "origin": connection["origin"]}) + "\n")
            process.stdin.flush()
            result = None
            step = 0
            for line in process.stdout:
                message = json.loads(line)
                if message.get("action") == "advance-status":
                    step += 1
                    channel.set("status", {"step": step})
                elif message.get("action") == "close-host":
                    stopped.set()
                    host.closeApplicationRun(runtime.applicationRun.applicationRunId)
                elif message.get("action") == "restart-host":
                    stopped.set()
                    host.closeApplicationRun(runtime.applicationRun.applicationRunId)
                    worker.join(5)
                    port = int(connection["origin"].rsplit(":", 1)[1])
                    runtime = host.createApplication(
                        appPackId="test.ui", plan=ManualActivationPlan(packIds=("test.ui", "frontend.kit", "frontend.host")),
                        config={"frontendHost": {"title": "Frontend smoke", "maxViews": 5, "port": port}})
                    channel = runtime.invokeCapability("frontend.host.channel@1")
                    channel.declareTrack("status", "state", initial={"step": 0})
                    assert runtime.invokeCapability("frontend.host.connection@1")["runId"] != connection["runId"]
                    stopped = Event()
                    worker = Thread(target=loopOwner, args=(channel, stopped), daemon=True)
                    worker.start()
                elif message.get("result") == "passed":
                    result = message
                    continue
                else:
                    raise RuntimeError("Unknown browser test message.")
                process.stdin.write('{"ok":true}\n')
                process.stdin.flush()
            process.wait(timeout=30)
            if process.returncode != 0 or result is None:
                raise RuntimeError("Browser smoke failed; " + process.stderr.read())
            (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            stopped.set()
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            host.stop()
            if worker is not None:
                worker.join(5)


def main() -> None:
    """Requires explicit local runtime/browser paths and an evidence output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    parser.add_argument("--playwright", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args()), indent=2))


if __name__ == "__main__":
    main()
