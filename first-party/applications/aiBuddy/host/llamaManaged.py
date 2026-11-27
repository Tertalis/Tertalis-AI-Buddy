# file: first-party/applications/aiBuddy/host/llamaManaged.py ; version: 1
"""Trusted host composition for platform-managed llama.cpp (ER-03), as Experiment Runner does it.

The launcher reads a host-authored llama.cpp catalog (llama.local.json5, from
llama.example.json5) and grants exactly AI Buddy's loaded `buddy` CodeEntry use
of every catalog model, including idle switches between them. The same
`llamaCpp` object must be passed to the Application config unchanged: the
platform rejects managed activation whose runtime JSON differs from the
approved catalog.

Known limitation (deliberate platform design, ER-01): if the platform cannot
prove that the server finished a request cleanly, the server's state becomes
unknown and all further requests and model switches are refused until AI Buddy
is restarted; the llama-server process may then have to be ended manually.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

__all__ = ["DEFAULT_CONFIG", "EXAMPLE_CONFIG", "ManagedLlama", "loadAdapter", "readConfig"]

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[2]
DRIVER_ROOT = REPO_ROOT / "first-party" / "llmDrivers" / "llamaCpp"
DEFAULT_CONFIG = APP_ROOT / "llama.local.json5"
EXAMPLE_CONFIG = APP_ROOT / "llama.example.json5"
DOMAIN_ID = "ai-buddy"
_ADAPTER_MODULE = "aiBuddyTrustedLlamaCpp"
_CONFIG_LIMIT = 1024 * 1024


def loadAdapter():
    """Loads only the repository's fixed first-party trusted composition module."""
    if _ADAPTER_MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_ADAPTER_MODULE, DRIVER_ROOT / "executionAdapter.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_ADAPTER_MODULE] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_ADAPTER_MODULE, None)
            raise
    return sys.modules[_ADAPTER_MODULE]


def readConfig(path: Path) -> dict:
    """Reads a JSON5 host file {"llamaCpp": {...}} and returns its llamaCpp object."""
    try:
        import json5
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise RuntimeError("Reading llama.local.json5 needs the json5 package "
                           "(pip install -r first-party/applications/aiBuddy/requirements.txt).") from error
    raw = Path(path).read_bytes()
    if len(raw) > _CONFIG_LIMIT:
        raise ValueError(f"{path} is larger than {_CONFIG_LIMIT} bytes.")
    value = json5.loads(raw.decode("utf-8"), allow_duplicate_keys=False)
    if type(value) is not dict or set(value) != {"llamaCpp"} or type(value["llamaCpp"]) is not dict:
        raise ValueError(f"{path} must contain exactly one top-level object: llamaCpp.")
    llama = value["llamaCpp"]
    if llama.get("manageServer") is not True:
        raise ValueError(f"{path}: llamaCpp.manageServer must be true (external servers use --llama-url).")
    if type(llama.get("models")) is not dict or not llama["models"]:
        raise ValueError(f"{path}: llamaCpp.models must name at least one model.")
    # Round-trip through JSON so the Application config is plain JSON data, identical to the catalog input.
    return json.loads(json.dumps(llama))


def _manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _entry(manifest: dict, codeEntryId: str) -> dict:
    return next(entry for entry in manifest["codeEntries"] if entry["id"] == codeEntryId)


class ManagedLlama:
    """Catalog, grant and composer for one RuntimeHost (pass composer in providerComposers)."""

    def __init__(self, llamaConfig: dict, *, appPackRoot: Path = APP_ROOT / "pack", appPackId: str = "aiBuddy",
                 codeEntryId: str = "buddy") -> None:
        adapter = loadAdapter()
        self.config = llamaConfig
        self.catalog = adapter.LlamaCppCatalog.fromConfig(llamaConfig)
        self.models = tuple(name for name in self.catalog.profiles if name is not None)
        appManifest = _manifest(appPackRoot / "manifest.json")
        driverManifest = _manifest(DRIVER_ROOT / "manifest.json")
        caller = adapter.EntrySelector.fromSource(appPackId, appManifest["version"], codeEntryId,
                                                  appPackRoot / _entry(appManifest, codeEntryId)["source"])
        driverEntry = driverManifest["codeEntries"][0]
        provider = adapter.EntrySelector.fromSource(driverManifest["packId"], driverManifest["version"], driverEntry["id"],
                                                    DRIVER_ROOT / driverEntry["source"])
        self.composer = adapter.LlamaCppComposition(
            domainId=DOMAIN_ID, catalog=self.catalog, providerEntries=(provider,),
            grants=(adapter.LlamaCppGrant(caller, self.models, allowTransition=True),))

    @property
    def defaultModel(self) -> str:
        return self.config.get("defaultModel") or self.models[0]
