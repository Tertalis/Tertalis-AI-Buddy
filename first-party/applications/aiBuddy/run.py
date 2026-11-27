# file: first-party/applications/aiBuddy/run.py ; version: 11
"""Launcher for AI Buddy: terminal plus browser UI (materialization Checkpoints A-H).

Starts a Tertalis RuntimeHost, creates or loads one AI Buddy Application,
runs the host-side loop on a background thread, and reads terminal
input. Plain lines are messages to AI Buddy; lines starting with "/" are
developer commands (see /help).

Unless --no-ui is given, the browser UI (frontend.kit + frontend.host) is
activated too and its launch link is printed. The link carries a private
access token in its #fragment; open it in any local browser, in as many
tabs as you like. Python stays authoritative; the terminal keeps working.

Inference uses llama.cpp in one of two modes:
- managed (when llama.local.json5 exists next to this file, or --llama-config
  names a file): Tertalis starts llama-server itself and AI Buddy can switch
  between the configured models (/model, or the browser UI) without restarting;
- external (otherwise, or with --llama-url): start llama-server yourself and
  point --llama-url at it.
Copy llama.example.json5 to llama.local.json5 and fill it in for managed mode.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.packs.runtime import ManualActivationPlan, PackResolver  # noqa: E402
from backend.runtime import RuntimeHost  # noqa: E402
from backend.save import ApplicationStore  # noqa: E402

APP_PACK_ID = "aiBuddy"
PLAN = ManualActivationPlan(packIds=("llmDrivers.llamaCpp", APP_PACK_ID))
UI_PLAN = ManualActivationPlan(packIds=("llmDrivers.llamaCpp", APP_PACK_ID, "frontend.kit", "frontend.host"))

HELP = """\
Messages: type any line that does not start with "/".
Developer commands:
  /break on|off     breakpoint mode: pause before each inference (prompt) and
                    after each response, before anything in it is acted on
  /show             show the paused prompt, or the paused response
  /edit             edit the paused prompt or response in an editor, then /continue
  /continue  (/c)   continue from the breakpoint or a pause
  /pause            stop starting new work at the next boundary
  /interrupt        cancel the running inference (it stays prepared and paused);
                    with the managed server use /interrupt! (see the limitation in README)
  /model            list the configured models (managed server); /model <name> switches
  /raw              raw output, interpretation and timing of the last inference
  /record <id>      show a stored record (e.g. i000001, w000001, e000001)
  /memories <text>  run memory retrieval for <text> and show what is found
  /why <id>         provenance of a memory or experience (e.g. m000001)
  /inject <text>    developer intervention: add a memory marked as developer-provided
  /schedule         eligibility and selection as decided now
  /task <purpose>   Queue: establish a task (/task! for Important)
  /later <s> <purpose>       task that becomes eligible after <s> seconds
  /after <wId> <purpose>     task that depends on <wId>
  /recheck <wId> [focus]     reconsider a completed thing to do as new work
  /choose <wId> <wId> ...    Choice: let AI Buddy choose what to do next
  /now <wId>        Interrupt directive: make it current at the next boundary
  /important <wId>  Important directive
  /suspend <wId>, /resume <wId>, /abandon <wId>, /amend <wId> <purpose>
                    (<wId> is an id such as w000003 from /status; "game" or a game id
                    such as g000001 means the undertaking playing that game)
  /info <text>      State information (/info! for Important State)
  /drop-info <sId>  deactivate state information
  /game [WxH] [mines] [seed]  start or resume Minesweeper (/game! for Important)
  /board            show the current game (/board! also shows mines)
  /activate, /deactivate      explicit AI Buddy activation and inactivity
  /rest, /wake      put AI Buddy to rest, or wake it
  /quiesce, /unquiesce        application quiescence (no processing at all)
  /rules            list rules in order (/rules! includes retired ones)
  /rule <scope> <text>        developer rule, protected (/rule+ for editable);
                    scopes: general, minesweeper, user-input, task
  /revise-rule <rId> <text>, /retire-rule <rId>, /order-rule <rId> <number>
  /protect <rId>, /unprotect <rId>   change whether AI Buddy may edit a rule
  /reflect          passive memory processing now (may run while inactive)
  /salience         passive salient-subject generation now (spontaneous candidates)
  /status           AI Buddy state summary
  /personality      authoritative personality
  /save             save now
  /ui               print the browser UI link again
  /help             this help
  /quit             save and exit
Output prefixed with "[diag]" is diagnostics, never AI Buddy speech."""


def _loadHostModule(name: str, fileName: str):
    """Imports one host module from this application directory."""
    spec = importlib.util.spec_from_file_location(name, APP_ROOT / "host" / fileName)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parseArgs(argv):
    parser = argparse.ArgumentParser(description="Run AI Buddy in a terminal.")
    parser.add_argument("--saves", type=Path, default=APP_ROOT / "local" / "saves",
                        help="SaveBundle root (default: local/saves in this application).")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--new", action="store_true", help="Create a new AI Buddy instance.")
    choice.add_argument("--load", metavar="APPLICATION_ID", help="Load a specific instance.")
    choice.add_argument("--list", action="store_true", help="List saved instances and exit.")
    parser.add_argument("--llama-config", type=Path, default=None, metavar="FILE",
                        help="Managed llama.cpp host file (default: llama.local.json5 here, if it exists).")
    parser.add_argument("--llama-url", default=None,
                        help="External mode: running llama-server base URL (default http://127.0.0.1:18761).")
    parser.add_argument("--context-window", type=int, default=None, help="Context window of the served model.")
    parser.add_argument("--model", default=None,
                        help="Managed: select this configured model. External: model name to request, if the server routes models.")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=300.0, help="Per-inference timeout in seconds.")
    parser.add_argument("--memory", type=json.loads, default=None, metavar="JSON",
                        help='Memory settings, e.g. \'{"recentLimit": 4, "automaticLimit": 5, '
                             '"recallLimit": 5, "maxContinuations": 4}\'.')
    parser.add_argument("--settings", type=json.loads, default=None, metavar="JSON",
                        help='Further aiBuddy settings, e.g. \'{"periodic": [{"name": "heartbeat", '
                             '"everySeconds": 10}], "intentionLifetimeSeconds": 86400}\'.')
    parser.add_argument("--interrupt-policy", choices=("finish", "cancel"), default="finish",
                        help="What new input does to a running inference (default: finish).")
    parser.add_argument("--break", dest="breakAtStart", action="store_true",
                        help="Start in breakpoint mode (pause before each inference and after each response).")
    parser.add_argument("--no-ui", dest="ui", action="store_false", help="Terminal only; no browser UI.")
    parser.add_argument("--ui-port", type=int, default=0, help="Browser UI port on 127.0.0.1 (default: any free port).")
    parser.add_argument("--open", dest="openBrowser", action="store_true",
                        help="Open the browser UI link in the default browser.")
    return parser.parse_args(argv)


def _savedInstances(saves: Path) -> list[Path]:
    """Returns saved AI Buddy instance directories, most recently changed first."""
    root = saves / APP_PACK_ID
    if not root.is_dir():
        return []
    return sorted((path for path in root.iterdir() if path.is_dir()),
                  key=lambda path: path.stat().st_mtime, reverse=True)


def _llamaConfigPath(args) -> Path | None:
    """The managed llama.cpp host file to use, or None for external mode."""
    if args.llama_config is not None:
        if args.llama_url is not None:
            raise SystemExit("Use either --llama-config (managed) or --llama-url (external), not both.")
        return args.llama_config
    if args.llama_url is None and managedModule.DEFAULT_CONFIG.is_file():
        return managedModule.DEFAULT_CONFIG
    return None


def _config(args, managedLlama=None) -> dict:
    if managedLlama is not None:
        llama = managedLlama.config        # must be identical to the approved catalog
    else:
        llama = {"baseUrl": args.llama_url or "http://127.0.0.1:18761"}
        if args.context_window is not None:
            llama["contextWindowTokens"] = args.context_window
    options = {"timeoutSeconds": args.timeout}
    if args.temperature is not None:
        options["temperature"] = args.temperature
    if args.max_tokens is not None:
        options["maxTokens"] = args.max_tokens
    aiBuddy = {"providerName": "llama.cpp", "model": None if managedLlama is not None else args.model,
               "providerOptions": options}
    if args.settings is not None:
        aiBuddy.update(args.settings)
    if args.memory is not None:
        aiBuddy["memory"] = args.memory
    config = {"llamaCpp": llama, "aiBuddy": aiBuddy}
    if args.ui:
        config["frontendHost"] = {"title": "AI Buddy", "port": args.ui_port,
                                  "main": {"packId": APP_PACK_ID, "codeEntryId": "ui"}}
    return config


def _editText(text: str) -> str:
    """Opens text in an editor and returns the saved result."""
    handle, name = tempfile.mkstemp(prefix="aibuddy-prompt-", suffix=".txt")
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        editor = os.environ.get("EDITOR") or ("notepad.exe" if os.name == "nt" else None)
        if editor:
            subprocess.run([editor, str(path)], check=False)
            return path.read_text(encoding="utf-8")
        print("Enter the replacement prompt; finish with a line containing only '.':")
        lines = []
        while (line := input()) != ".":
            lines.append(line)
        return "\n".join(lines)
    finally:
        path.unlink(missing_ok=True)


class Console:
    """Terminal presentation of AI Buddy speech, diagnostics and debugger state."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.pausedInference: dict | None = None
        self.lastRecord: dict | None = None
        self._print = threading.Lock()

    def out(self, text: str) -> None:
        with self._print:
            print(text, flush=True)

    def speak(self, text: str) -> None:
        self.out(f"{self.name}> {text}")

    def diagnostic(self, text: str) -> None:
        self.out(f"  [diag] {text}")

    def paused(self, inference: dict) -> None:
        self.pausedInference = inference
        if inference.get("phase") == "output":
            self.diagnostic(f"breakpoint: response of {inference['inferenceId']} received, not yet acted on. "
                            "/edit, /continue")
            self.out("----- response -----\n" + inference["rawOutput"] + "\n--------------------")
        else:
            self.diagnostic(f"breakpoint: inference {inference['inferenceId']} is about to start. "
                            "/show, /edit, /continue")

    def finished(self, record: dict) -> None:
        self.pausedInference = None
        self.lastRecord = record
        timing = record.get("timing", {})
        if "durationNs" in timing:
            self.diagnostic(f"inference {record['id']} {record['state']} in {timing['durationNs'] / 1e9:.2f}s")

    def showRecord(self, record: dict | None) -> None:
        if record is None:
            self.diagnostic("no such record")
            return
        self.out(json.dumps(record, indent=2, ensure_ascii=False))


managedModule = _loadHostModule("aiBuddyLlamaManaged", "llamaManaged.py")


def main(argv=None) -> int:
    args = _parseArgs(argv)
    loopModule = _loadHostModule("aiBuddyHostLoop", "buddyLoop.py")
    bridgeModule = _loadHostModule("aiBuddyFrontendBridge", "frontendBridge.py") if args.ui else None
    args.saves.mkdir(parents=True, exist_ok=True)

    if args.list:
        for path in _savedInstances(args.saves):
            print(path.name)
        return 0

    llamaPath = _llamaConfigPath(args)
    managedLlama = None
    if llamaPath is not None:
        try:
            managedLlama = managedModule.ManagedLlama(managedModule.readConfig(llamaPath))
            if args.model is not None and args.model not in managedLlama.models:
                raise ValueError(f"Unknown model {args.model!r}; configured: {', '.join(managedLlama.models)}.")
        except Exception as error:  # noqa: BLE001 - shown to the user before anything starts.
            print(f"Cannot use {llamaPath}: {type(error).__name__}: {error}")
            return 2
    host = RuntimeHost(
        applicationStore=ApplicationStore(args.saves),
        packResolver=PackResolver(roots=(REPO_ROOT / "first-party" / "llmDrivers", APP_ROOT / "pack",
                                         REPO_ROOT / "first-party" / "frontendKit", REPO_ROOT / "first-party" / "frontendHost")),
        **({"providerComposers": (managedLlama.composer,)} if managedLlama is not None else {}),
    )
    host.start()
    try:
        config = _config(args, managedLlama)
        plan = UI_PLAN if args.ui else PLAN
        existing = _savedInstances(args.saves)
        if args.load or (existing and not args.new):
            applicationId = args.load or existing[0].name
            runtime = host.loadApplication(appPackId=APP_PACK_ID, applicationId=applicationId,
                                           plan=plan, config=config)
            print(f"Loaded AI Buddy instance {applicationId}")
        else:
            runtime = host.createApplication(appPackId=APP_PACK_ID, plan=plan, config=config)
            print(f"Created AI Buddy instance {runtime.applicationRun.application.applicationId}")
        if managedLlama is not None:
            selected = runtime.invokeCapability("aiBuddy.debug.llm.describe@1")["selected"]
            print(f"Inference: managed llama.cpp from {llamaPath.name}; models: {', '.join(managedLlama.models)}; "
                  f"selected: {args.model or selected} (loads on first use). Type /help for commands.")
            if args.interrupt_policy == "cancel":
                print("Note: --interrupt-policy cancel with the managed server can leave it unusable until restart "
                      "(known limitation, see README).")
        else:
            print(f"Inference: llama.cpp at {args.llama_url or 'http://127.0.0.1:18761'}. Type /help for commands.")

        personality = runtime.invokeCapability("aiBuddy.debug.personality.describe@1")
        console = Console(personality["name"])
        driver = loopModule.BuddyLoop(runtime, events=loopModule.DriverEvents(
            speak=console.speak, diagnostic=console.diagnostic,
            paused=console.paused, inferenceFinished=console.finished), interruptPolicy=args.interrupt_policy)
        if args.breakAtStart:
            driver.setBreakBeforeInference(True)
        if managedLlama is not None and args.model:
            # Recorded now, loaded by the first inference.
            driver.developerJob("aiBuddy.debug.llm.select@1", {"model": args.model, "load": False})

        stop = threading.Event()
        uiLink = None
        if args.ui:
            # Before the loop thread starts: these calls use the ApplicationRun lane from this thread.
            channel = runtime.invokeCapability("frontend.host.channel@1")
            bridge = bridgeModule.FrontendBridge(driver, channel)
            driver.events = bridge.wrap(driver.events)
            uiLink = runtime.invokeCapability("frontend.host.connection@1")["url"]
            threading.Thread(target=bridge.run, args=(stop,), name="aiBuddyFrontend", daemon=True).start()
            print(f"Browser UI: {uiLink}\n  (private link with an access token; do not share it)")
            if args.openBrowser:
                webbrowser.open(uiLink)
        worker = threading.Thread(target=driver.run, args=(stop,), name="aiBuddyLoop", daemon=True)
        worker.start()

        def intervene(capabilityId, payload):
            future = driver.developerJob(capabilityId, payload)
            if driver.quiescent:
                console.diagnostic("the application is quiescent; this runs after /unquiesce")
                return
            try:
                console.showRecord(future.result())
            except Exception as error:  # noqa: BLE001 - shown to the developer.
                console.diagnostic(f"{type(error).__name__}: {error}")

        def resolveWork(token, usage):
            """Resolves a thing-to-do argument; "game" means the open Minesweeper undertaking.

            Returns None (after telling the developer why) when it cannot.
            """
            if token.lower() == "game" or re.fullmatch(r"g\d{6}", token):
                payload = {} if token.lower() == "game" else {"gameId": token}
                try:
                    game = ask("aiBuddy.game.minesweeper.state@1", payload)
                except Exception as error:  # noqa: BLE001 - shown to the developer.
                    console.diagnostic(f"{type(error).__name__}: {error}")
                    return None
                if not game.get("id"):
                    console.diagnostic("there is no Minesweeper game in progress (/game starts one)")
                    return None
                openIds = {work["id"] for work in ask("aiBuddy.debug.status@1")["openWork"]}
                playing = [workId for workId in game["workIds"] if workId in openIds]
                if playing:
                    return playing[-1]
                if game["status"] != "in-progress":
                    console.diagnostic(f"game {game['id']} has ended ({game['status']}); nothing is playing it")
                    return None
                last = ask("aiBuddy.debug.record.describe@1", {"id": game["workIds"][-1]}) if game["workIds"] else None
                ended = f"; its last undertaking {last['id']} ended: {last.get('endedReason')}" if last else ""
                console.diagnostic(f"game {game['id']} is saved in progress but no undertaking is playing it"
                                   f"{ended}. /game resumes it.")
                return None
            if re.fullmatch(r"w\d{6}", token):
                return token
            console.diagnostic(f"usage: {usage}")
            openWork = ask("aiBuddy.debug.status@1")["openWork"]
            if openWork:
                console.out("Open things to do:\n" + "\n".join(
                    f"  {work['id']}  {work['kind']:<18} {work['state']:<11} {work.get('purpose') or ''}"
                    for work in openWork))
            else:
                console.out("There are no open things to do.")
            return None

        def ask(capabilityId, payload=None):
            if driver.busy:
                console.diagnostic("waiting for the current step to finish...")
            return driver.query(capabilityId, payload).result()

        try:
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if not line.strip():
                    continue
                if not line.startswith("/"):
                    driver.submitInput(line)
                    continue
                command, _, argument = line[1:].strip().partition(" ")
                argument = argument.strip()
                if command in ("quit", "exit", "q"):
                    break
                elif command == "help":
                    console.out(HELP)
                elif command == "ui":
                    console.out(f"Browser UI: {uiLink}" if uiLink else "The browser UI is off (--no-ui).")
                elif command == "break":
                    driver.setBreakBeforeInference(argument.lower() in ("on", "1", "yes", ""))
                elif command == "show":
                    paused = console.pausedInference
                    if paused is None:
                        console.diagnostic("no inference is paused at the breakpoint")
                    else:
                        console.out(paused["rawOutput"] if paused.get("phase") == "output" else paused["prompt"])
                elif command == "edit":
                    paused = console.pausedInference
                    if paused is None:
                        console.diagnostic("no inference is paused at the breakpoint")
                    else:
                        field = "rawOutput" if paused.get("phase") == "output" else "prompt"
                        edited = _editText(paused[field])
                        if edited != paused[field] and (edited or field == "rawOutput"):
                            if field == "rawOutput":
                                driver.replaceOutput(edited)
                            else:
                                driver.replacePrompt(edited)
                            paused[field] = edited
                        else:
                            console.diagnostic(f"{'response' if field == 'rawOutput' else 'prompt'} unchanged")
                elif command in ("continue", "c"):
                    driver.continueExecution()
                elif command == "pause":
                    driver.pause()
                elif command in ("interrupt", "interrupt!"):
                    if managedLlama is not None and command == "interrupt" and driver.busy:
                        console.diagnostic("with the managed server, cancelling mid-answer can leave it unusable until "
                                           "AI Buddy restarts (known limitation). Use /interrupt! to cancel anyway.")
                    elif not driver.interrupt():
                        console.diagnostic("nothing is running")
                elif command == "model":
                    if not argument:
                        info = ask("aiBuddy.debug.llm.describe@1")
                        if info["mode"] != "managed":
                            console.diagnostic("model selection needs the managed server (llama.local.json5)")
                        else:
                            for model in info["models"]:
                                mark = "*" if model["name"] == info["selected"] else " "
                                size = f" ({model['contextWindowTokens']} tokens)" if model.get("contextWindowTokens") else ""
                                console.out(f" {mark} {model['name']}{size}")
                    else:
                        console.diagnostic(f"switching to {argument} (loading can take a while)...")
                        intervene("aiBuddy.debug.llm.select@1", {"model": argument})
                elif command == "raw":
                    console.showRecord(console.lastRecord)
                elif command == "record":
                    try:
                        console.showRecord(ask("aiBuddy.debug.record.describe@1", {"id": argument}))
                    except Exception as error:  # noqa: BLE001 - shown to the developer.
                        console.diagnostic(f"{type(error).__name__}: {error}")
                elif command == "memories":
                    console.showRecord(ask("aiBuddy.debug.memory.search@1", {"query": argument}))
                elif command == "why":
                    try:
                        console.showRecord(ask("aiBuddy.debug.memory.provenance@1", {"id": argument}))
                    except Exception as error:  # noqa: BLE001 - shown to the developer.
                        console.diagnostic(f"{type(error).__name__}: {error}")
                elif command == "inject":
                    if not argument:
                        console.diagnostic("usage: /inject <text>")
                    else:
                        try:
                            result = driver.developerJob("aiBuddy.debug.memory.inject@1", {"text": argument}).result()
                            console.diagnostic(f"memory {result['memoryId']} injected")
                        except Exception as error:  # noqa: BLE001 - shown to the developer.
                            console.diagnostic(f"{type(error).__name__}: {error}")
                elif command == "schedule":
                    console.showRecord(ask("aiBuddy.debug.schedule@1"))
                elif command in ("task", "task!"):
                    intervene("aiBuddy.debug.work.add@1",
                              {"kind": "task", "purpose": argument, "important": command == "task!"})
                elif command == "later":
                    seconds, _, purpose = argument.partition(" ")
                    try:
                        delay = float(seconds)
                    except ValueError:
                        console.diagnostic("usage: /later <seconds> <purpose>")
                    else:
                        intervene("aiBuddy.debug.work.add@1",
                                  {"kind": "task", "purpose": purpose, "notBeforeSeconds": delay})
                elif command == "after":
                    dependency, _, purpose = argument.partition(" ")
                    dependency = resolveWork(dependency, "/after <wId> <purpose>")
                    if dependency:
                        intervene("aiBuddy.debug.work.add@1",
                                  {"kind": "task", "purpose": purpose, "dependsOn": [dependency]})
                elif command == "recheck":
                    original, _, focus = argument.partition(" ")
                    original = resolveWork(original, "/recheck <wId of a completed thing to do> [focus]")
                    if original:
                        intervene("aiBuddy.debug.work.add@1",
                                  {"kind": "recheck", "recheckOf": original, "purpose": focus or None})
                elif command == "choose":
                    candidates = [resolveWork(token, "/choose <wId> <wId> ...") for token in argument.split()]
                    if len(candidates) < 2:
                        resolveWork("", "/choose <wId> <wId> ...")
                    elif all(candidates):
                        intervene("aiBuddy.debug.work.choose@1", {"candidateIds": candidates})
                elif command in ("now", "important", "suspend", "resume", "abandon"):
                    action = "interrupt" if command == "now" else command
                    workId = resolveWork(argument, f"/{command} <wId>")
                    if workId:
                        intervene("aiBuddy.debug.work.change@1", {"workId": workId, "action": action})
                elif command == "amend":
                    workId, _, purpose = argument.partition(" ")
                    workId = resolveWork(workId, "/amend <wId> <new purpose>")
                    if workId and not purpose.strip():
                        console.diagnostic("usage: /amend <wId> <new purpose>")
                    elif workId:
                        intervene("aiBuddy.debug.work.change@1", {"workId": workId, "action": "amend", "purpose": purpose})
                elif command in ("info", "info!"):
                    intervene("aiBuddy.debug.situation.change@1",
                              {"action": "add", "text": argument, "important": command == "info!"})
                elif command == "drop-info":
                    intervene("aiBuddy.debug.situation.change@1", {"action": "remove", "situationId": argument})
                elif command in ("game", "game!"):
                    payload = {"important": command == "game!"}
                    parts = argument.split()
                    try:
                        if parts and "x" in parts[0].lower():
                            width, height = parts.pop(0).lower().split("x")
                            payload.update(width=int(width), height=int(height))
                        if parts:
                            payload["mines"] = int(parts.pop(0))
                        if parts:
                            payload["seed"] = int(parts.pop(0))
                    except ValueError:
                        console.diagnostic("usage: /game [WxH] [mines] [seed]")
                    else:
                        intervene("aiBuddy.debug.game.start@1", payload)
                elif command in ("board", "board!"):
                    state = ask("aiBuddy.game.minesweeper.state@1", {"showMines": command == "board!"})
                    if state.get("board"):
                        console.out(state["summary"] + f" [{state['status']}]")
                        console.out("\n".join(state["board"]))
                    else:
                        console.diagnostic("no game in progress")
                elif command in ("activate", "deactivate", "rest", "wake"):
                    intervene("aiBuddy.debug.lifecycle.change@1", {"action": command})
                elif command == "quiesce":
                    driver.quiesce()
                elif command == "unquiesce":
                    driver.unquiesce()
                elif command in ("rules", "rules!"):
                    rules = ask("aiBuddy.debug.rule.list@1", {"includeRetired": command == "rules!"})
                    if not rules:
                        console.out("There are no rules.")
                    for rule in rules:
                        flags = ("" if rule["editable"] else " protected") + ("" if rule["active"] else " retired")
                        console.out(f"  {rule['id']}  [{rule['scope']}, order {rule['order']}{flags}, "
                                    f"by {rule['origin'].get('by')}] {rule['text']}")
                elif command in ("rule", "rule+"):
                    scope, _, text = argument.partition(" ")
                    if not text.strip():
                        console.diagnostic("usage: /rule <scope> <text>")
                    else:
                        intervene("aiBuddy.debug.rule.add@1",
                                  {"scope": scope, "text": text, "editable": command == "rule+"})
                elif command in ("revise-rule", "retire-rule", "order-rule", "protect", "unprotect"):
                    ruleId, _, rest = argument.partition(" ")
                    if not re.fullmatch(r"r\d{6}", ruleId):
                        console.diagnostic(f"usage: /{command} <rId> ...   (ids from /rules)")
                    elif command == "revise-rule":
                        intervene("aiBuddy.debug.rule.change@1", {"ruleId": ruleId, "action": "revise", "text": rest})
                    elif command == "retire-rule":
                        intervene("aiBuddy.debug.rule.change@1", {"ruleId": ruleId, "action": "retire"})
                    elif command == "order-rule":
                        try:
                            order = float(rest)
                        except ValueError:
                            console.diagnostic("usage: /order-rule <rId> <number>")
                        else:
                            intervene("aiBuddy.debug.rule.change@1", {"ruleId": ruleId, "action": "reorder", "order": order})
                    else:
                        intervene("aiBuddy.debug.rule.change@1",
                                  {"ruleId": ruleId, "action": "editable", "value": command == "unprotect"})
                elif command in ("reflect", "salience"):
                    activity = "memory-processing" if command == "reflect" else "salient-subjects"
                    intervene("aiBuddy.debug.passive.start@1", {"activity": activity})
                elif command == "status":
                    console.showRecord(ask("aiBuddy.debug.status@1"))
                elif command == "personality":
                    console.showRecord(ask("aiBuddy.debug.personality.describe@1"))
                elif command == "save":
                    driver.requestSave()
                else:
                    console.diagnostic(f"unknown command /{command}; /help lists commands")
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            if managedLlama is None or not driver.busy:
                driver.interrupt()
            else:
                # Cancelling mid-answer can leave the managed server unusable (known limitation):
                # let the running step finish first, then cancel only if it takes too long.
                console.diagnostic("waiting up to 30s for the current step to finish...")
                worker.join(timeout=30)
                driver.interrupt()
            worker.join(timeout=30)
            if worker.is_alive():
                console.diagnostic("loop did not stop within 30s; exiting without final save")
            else:
                bundle = runtime.saveApplication()
                console.diagnostic(f"saved generation {bundle.generation}")
    finally:
        host.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
