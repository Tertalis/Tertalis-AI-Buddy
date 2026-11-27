# file: backend/packs/runtime.py ; version: 18
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from backend.context.codeEntryContext import CodeEntryIdentity
from backend.llm.executionSessions import lifetimeBoundary
from backend.llm.providerComposition import activateBatch, packUnloadBoundary
from backend.core.runtimeIds import newRuntimeId
from backend.registration import RegistrationScope
from backend.values.committed import CommittedValueLayer, CommittedValueTransaction

if TYPE_CHECKING:
    from backend.application.applicationRuntime import ApplicationRuntime

__all__ = [
    "CODE_ENTRY_FORMATS",
    "CODE_ENTRY_ROLES",
    "DeclaredSourceEntry",
    "JS_MODULE_FORMAT",
    "ManualActivationPlan",
    "PACK_KINDS",
    "PYTHON_SOURCE_FORMAT",
    "PackDefinition",
    "PackLoader",
    "PackResolver",
]

_APPLICATION_HOOK_NAMES = ("onApplicationCreate", "onApplicationLoad", "onApplicationRun")

PACK_KINDS = frozenset({"appPack", "modPack"})
PYTHON_SOURCE_FORMAT = "python-source@1"
JS_MODULE_FORMAT = "js-module-source@1"
CODE_ENTRY_FORMATS = frozenset({PYTHON_SOURCE_FORMAT, JS_MODULE_FORMAT})
CODE_ENTRY_ROLES = frozenset({"main", "library"})
JS_SOURCE_LIMIT = 1024 * 1024
_CODE_ENTRY_KEYS = frozenset({"id", "source", "format", "role"})
_FORMAT_SUFFIXES = {PYTHON_SOURCE_FORMAT: (".py",), JS_MODULE_FORMAT: (".mjs",)}


@dataclass(frozen=True, slots=True)
class CodeEntryDefinition:
    """Names one declared source entry within its owning Pack root."""
    codeEntryId: str
    source: str
    format: str = PYTHON_SOURCE_FORMAT
    role: str = "main"


@dataclass(frozen=True, slots=True)
class DeclaredSourceEntry:
    """Immutable record of a non-executed CodeEntry, holding exactly the hashed bytes.

    Python never executes these entries. They are recorded at activation so
    a frontend host can serve the same bytes whose identity was established.
    """
    packId: str
    packVersion: str
    codeEntryId: str
    format: str
    role: str
    source: str
    sourceBytes: bytes
    sourceSha256: str
    implementationId: str


@dataclass(frozen=True, slots=True)
class PackDefinition:
    """Immutable discovered Pack identity and ordered CodeEntry declarations."""
    packId: str
    kind: str
    version: str
    root: Path
    codeEntries: tuple[CodeEntryDefinition, ...]


@dataclass(frozen=True, slots=True)
class ManualActivationPlan:
    """Explicit ordered Pack list. No dependency semantics are inferred."""

    packIds: tuple[str, ...]

    @classmethod
    def fromJson(cls, value: object) -> "ManualActivationPlan":
        """Validates a manual ordered activation list without dependency resolution."""
        if not isinstance(value, dict) or not isinstance(value.get("packs"), list):
            raise ValueError("Activation plan must contain a 'packs' list.")
        packIds = tuple(value["packs"])
        if not all(type(item) is str and item for item in packIds):
            raise ValueError("Every activation plan Pack identity must be a non-empty string.")
        if len(set(packIds)) != len(packIds):
            raise ValueError("Manual activation plan must not contain the same Pack identity more than once.")
        return cls(packIds=packIds)


class PackResolver:
    """Uniqueness-only resolver over one immutable discovery snapshot.

    Content roots are expected to be stable for the lifetime of a resolver.
    The first lookup discovers and validates every Pack manifest once. Later
    lookups reuse that snapshot, avoiding repeated recursive filesystem scans
    while preserving the resolver's 0/1/>1 candidate semantics.
    """

    def __init__(self, *, roots: tuple[Path, ...]) -> None:
        """Binds configured discovery roots without loading or activating any Pack."""
        self._roots = tuple(root.resolve() for root in roots)
        self._candidatesByPackId: dict[str, tuple[PackDefinition, ...]] | None = None

    @staticmethod
    def _definition(manifestPath: Path, manifest: object) -> PackDefinition:
        """Validates one manifest into immutable Pack/source declarations."""
        if not isinstance(manifest, dict):
            raise ValueError(f"Pack manifest must contain a JSON object: {manifestPath}.")
        packId = manifest.get("packId")
        if type(packId) is not str or not packId:
            raise ValueError(f"Pack manifest requires a non-empty string packId: {manifestPath}.")
        kind = manifest.get("kind")
        if type(kind) is not str or not kind:
            raise ValueError(f"Pack {packId!r} requires a non-empty string kind.")
        if kind not in PACK_KINDS:
            raise ValueError(f"Pack {packId!r} has unknown kind {kind!r}; expected 'appPack' or 'modPack'.")
        version = manifest.get("version")
        if type(version) is not str or not version:
            raise ValueError(f"Pack {packId!r} requires a non-empty string version.")

        entriesSource = manifest.get("codeEntries", [])
        if not isinstance(entriesSource, list):
            raise ValueError(f"Pack {packId!r} has invalid codeEntries.")

        entries: list[CodeEntryDefinition] = []
        seenEntryIds: set[str] = set()
        mainFormats: set[str] = set()
        for item in entriesSource:
            if not isinstance(item, dict) or type(item.get("id")) is not str or type(item.get("source")) is not str:
                raise ValueError(f"Pack {packId!r} has an invalid CodeEntry declaration.")
            unknownKeys = set(item) - _CODE_ENTRY_KEYS
            if unknownKeys:
                names = ", ".join(sorted(map(str, unknownKeys)))
                raise ValueError(f"Pack {packId!r} CodeEntry declares unknown key(s): {names}.")
            entryId = item["id"]
            source = item["source"]
            if not entryId or not source:
                raise ValueError(f"Pack {packId!r} CodeEntry id/source must not be blank.")
            if entryId in seenEntryIds:
                raise ValueError(f"Pack {packId!r} declares duplicate CodeEntry id {entryId!r}.")
            entryFormat = item.get("format", PYTHON_SOURCE_FORMAT)
            if type(entryFormat) is not str or entryFormat not in CODE_ENTRY_FORMATS:
                raise ValueError(f"Pack {packId!r} CodeEntry {entryId!r} has unsupported format {entryFormat!r}.")
            role = item.get("role", "main")
            if type(role) is not str or role not in CODE_ENTRY_ROLES:
                raise ValueError(f"Pack {packId!r} CodeEntry {entryId!r} has invalid role {role!r}.")
            if role == "library" and entryFormat == PYTHON_SOURCE_FORMAT:
                raise ValueError(
                    f"Pack {packId!r} CodeEntry {entryId!r}: python-source@1 library entries "
                    "are not supported yet.",
                )
            if role == "main":
                if entryFormat in mainFormats:
                    raise ValueError(f"Pack {packId!r} declares more than one main {entryFormat} CodeEntry.")
                mainFormats.add(entryFormat)
            seenEntryIds.add(entryId)
            entries.append(CodeEntryDefinition(codeEntryId=entryId, source=source, format=entryFormat, role=role))

        return PackDefinition(
            packId=packId,
            kind=kind,
            version=version,
            root=manifestPath.parent.resolve(),
            codeEntries=tuple(entries),
        )

    def _discover(self) -> dict[str, tuple[PackDefinition, ...]]:
        """Snapshots manifest discovery once, preserving distinct version candidates."""
        cached = self._candidatesByPackId
        if cached is not None:
            return cached

        discovered: dict[str, list[PackDefinition]] = {}
        for root in self._roots:
            if not root.is_dir():
                continue
            for manifestPath in sorted(root.rglob("manifest.json")):
                try:
                    manifest = json.loads(manifestPath.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as err:
                    raise ValueError(f"Could not read Pack manifest: {manifestPath}.") from err
                definition = self._definition(manifestPath, manifest)
                discovered.setdefault(definition.packId, []).append(definition)

        snapshot = {packId: tuple(candidates) for packId, candidates in discovered.items()}
        self._candidatesByPackId = snapshot
        return snapshot

    def requireSingle(self, packId: str) -> PackDefinition:
        """Requires an unambiguous configured Pack candidate; never chooses a version silently."""
        if type(packId) is not str or not packId:
            raise ValueError("Pack identity must be a non-empty exact string.")
        matches = self._discover().get(packId, ())
        if not matches:
            raise LookupError(f"Pack was not found: {packId}.")
        if len(matches) != 1:
            roots = ", ".join(str(item.root) for item in matches)
            raise RuntimeError(f"Pack resolution is ambiguous for {packId!r}: {roots}.")
        return matches[0]


@dataclass(slots=True)
class _LoadedCodeEntry:
    """Owns an executed module and its state until exactly scoped unload."""
    identity: CodeEntryIdentity
    pack: PackDefinition
    module: ModuleType
    state: object | None


@dataclass(slots=True)
class _LoadedPack:
    """Keeps activation entries and registration scope together for rollback/unload."""
    pack: PackDefinition
    entries: tuple[_LoadedCodeEntry, ...]
    registrationScope: RegistrationScope
    declared: tuple[DeclaredSourceEntry, ...] = ()


class PackLoader:
    """Mechanical loader/activator for an already ordered manual Pack plan.

    The loader performs no dependency solving or reordering. Its intelligence
    is limited to enforcing lifecycle integrity for the exact Pack sequence it
    was given.
    """

    def __init__(self, *, runtime: ApplicationRuntime, resolver: PackResolver) -> None:
        """Binds one runtime/resolver and begins with no activated Pack ownership."""
        self._runtime = runtime
        self._resolver = resolver
        self._loadedPacks: list[_LoadedPack] = []
        self._activationBarrierReached = False

    @lifetimeBoundary
    def activate(self, plan: ManualActivationPlan) -> None:
        """Stage the full plan and compose outside lanes before its barrier."""
        return activateBatch(self, lambda: self._activatePlan(plan))

    def _activatePlan(self, plan: ManualActivationPlan) -> None:
        """Activates the ordered plan and rolls back only entries prepared by this attempt."""
        self._runtime.requireOperational()
        checkpoint = len(self._loadedPacks)
        previousBarrier = self._activationBarrierReached
        self._activationBarrierReached = False
        self._runtime.trace("activation-plan-started", attributes={"packIds": list(plan.packIds)})
        try:
            for packId in plan.packIds:
                self._activatePack(self._resolver.requireSingle(packId))
        except Exception as activationError:
            cleanupErrors = self._closeLoadedPacks(self._loadedPacks[checkpoint:])
            del self._loadedPacks[checkpoint:]
            self._activationBarrierReached = previousBarrier
            self._runtime.trace(
                "activation-plan-failed",
                message=str(activationError),
                attributes={"packIds": list(plan.packIds)},
                level="error",
            )
            if cleanupErrors:
                raise ExceptionGroup(
                    "Activation plan failed and cleanup also reported errors.",
                    [activationError, *cleanupErrors],
                ) from None
            raise
        self._activationBarrierReached = True
        self._runtime.trace("activation-plan-completed", attributes={"packIds": list(plan.packIds)})

    @lifetimeBoundary
    def activatePack(self, pack: PackDefinition) -> None:
        """Compose one new Pack batch without enlarging existing binding grants."""
        return activateBatch(self, lambda: self._activatePack(pack))

    def _activatePack(self, pack: PackDefinition) -> None:
        """Executes source entries provisionally, then atomically publishes their registrations."""
        self._runtime.requireOperational()
        self._activationBarrierReached = False
        if pack.kind == "appPack" and pack.packId != self._runtime.applicationRun.application.appPackId:
            raise ValueError(
                f"Cannot activate appPack {pack.packId!r} for Application owned by "
                f"{self._runtime.applicationRun.application.appPackId!r}.",
            )
        if any(item.pack.packId == pack.packId for item in self._loadedPacks):
            raise RuntimeError(f"Pack is already active in this ApplicationRun: {pack.packId}.")

        self._runtime.trace("pack-activation-started", attributes={"packId": pack.packId})
        scope = RegistrationScope()
        entries: list[_LoadedCodeEntry] = []
        loadedModules: list[ModuleType] = []
        registeredOwners: list[str] = []
        declared: list[DeclaredSourceEntry] = []

        try:
            for definition in pack.codeEntries:
                if definition.format != PYTHON_SOURCE_FORMAT:
                    declared.append(self._declareSource(pack=pack, definition=definition))
                    continue
                instanceId = newRuntimeId()
                module, sourceSha256, implementationId = self._loadModule(
                    pack=pack,
                    definition=definition,
                    instanceId=instanceId,
                )
                loadedModules.append(module)
                if pack.kind != "appPack":
                    applicationHooks = [
                        name for name in _APPLICATION_HOOK_NAMES
                        if getattr(module, name, None) is not None
                    ]
                    if applicationHooks:
                        names = ", ".join(applicationHooks)
                        raise ValueError(
                            f"Non-appPack {pack.packId!r} CodeEntry "
                            f"{definition.codeEntryId!r} declares Application lifecycle hook(s): {names}.",
                        )
                identity = CodeEntryIdentity(
                    applicationId=self._runtime.applicationRun.application.applicationId,
                    applicationRunId=self._runtime.applicationRun.applicationRunId,
                    packId=pack.packId,
                    packVersion=pack.version,
                    codeEntryId=definition.codeEntryId,
                    codeEntryInstanceId=instanceId,
                    sourceSha256=sourceSha256,
                    implementationFormat=PYTHON_SOURCE_FORMAT,
                    implementationId=implementationId,
                )

                # Once module code has executed, the CodeEntry may already own
                # resources. Make it cleanup-eligible before invoking onLoad so
                # a failing callback still receives a best-effort onUnload with
                # state=None rather than relying on every Pack to self-clean.
                loadedEntry = _LoadedCodeEntry(identity=identity, pack=pack, module=module, state=None)
                entries.append(loadedEntry)
                context = self._runtime.createContext(
                    identity=identity,
                    packRoot=pack.root,
                    registrationScope=scope,
                    allowRegistration=True,
                )
                try:
                    callback = getattr(module, "onLoad", None)
                    loadedEntry.state = None if callback is None else callback(context)
                finally:
                    context.invalidate()

            for item in entries:
                self._runtime.registerCodeEntry(item.identity, item.pack.root)
                registeredOwners.append(item.identity.codeEntryInstanceId)
            scope.publish()

        except Exception as activationError:
            scope.withdraw()
            for ownerId in reversed(registeredOwners):
                self._runtime.unregisterCodeEntry(ownerId)

            cleanupErrors = self._unloadEntries(entries)
            for module in reversed(loadedModules):
                sys.modules.pop(module.__name__, None)

            self._runtime.trace(
                "pack-activation-failed",
                message=str(activationError),
                attributes={"packId": pack.packId},
                level="error",
            )
            if cleanupErrors:
                raise ExceptionGroup(
                    f"Pack {pack.packId!r} activation failed and cleanup also reported errors.",
                    [activationError, *cleanupErrors],
                ) from None
            raise

        self._loadedPacks.append(
            _LoadedPack(pack=pack, entries=tuple(entries), registrationScope=scope, declared=tuple(declared)),
        )
        self._runtime.trace(
            "pack-activation-completed",
            attributes={
                "packId": pack.packId,
                "codeEntryInstanceIds": [item.identity.codeEntryInstanceId for item in entries],
                "declaredEntryIds": [item.codeEntryId for item in declared],
            },
        )

    def declaredEntries(self, entryFormat: str) -> tuple[DeclaredSourceEntry, ...]:
        """Returns recorded non-executed entries of one format over active Packs, in activation order."""
        if type(entryFormat) is not str or entryFormat not in CODE_ENTRY_FORMATS:
            raise ValueError(f"Unsupported CodeEntry format: {entryFormat!r}.")
        return tuple(
            item
            for loadedPack in self._loadedPacks
            for item in loadedPack.declared
            if item.format == entryFormat
        )

    def invokeApplicationCreate(self, *, memoryView: CommittedValueTransaction) -> None:
        """Invokes the app creation hook within its supplied uncommitted memory view."""
        if not isinstance(memoryView, CommittedValueTransaction):
            raise TypeError("onApplicationCreate requires a CommittedValueTransaction memoryView.")
        self._invokeApplicationHook("onApplicationCreate", memoryView=memoryView)

    def invokeApplicationLoad(self) -> None:
        """Invokes the app load hook with a fresh, expiring invocation Context."""
        self._invokeApplicationHook("onApplicationLoad")

    def invokeApplicationRun(self) -> None:
        """Invokes the app run hook after activation without extending its Context lifetime."""
        self._invokeApplicationHook("onApplicationRun")

    def _invokeApplicationHook(
        self,
        hookName: str,
        *,
        memoryView: CommittedValueLayer | CommittedValueTransaction | None = None,
    ) -> None:
        """Dispatches only the defining AppPack hook and always invalidates its fresh Context."""
        self._runtime.requireCompositionReady()
        self._runtime.requireOperational()
        if not self._activationBarrierReached:
            raise RuntimeError("Application lifecycle cannot run before the activation-plan barrier.")

        application = self._runtime.applicationRun.application
        matches = [
            loadedPack
            for loadedPack in self._loadedPacks
            if loadedPack.pack.packId == application.appPackId
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Application lifecycle requires exactly one active defining Pack "
                f"{application.appPackId!r}.",
            )
        loadedPack = matches[0]
        if loadedPack.pack.kind != "appPack":
            raise RuntimeError(
                f"Defining Pack {loadedPack.pack.packId!r} must have kind 'appPack'.",
            )

        self._runtime.trace(
            "application-lifecycle-hook-started",
            attributes={"hook": hookName, "appPackId": loadedPack.pack.packId},
        )
        for item in loadedPack.entries:
            callback = getattr(item.module, hookName, None)
            if callback is None:
                continue
            scope = RegistrationScope()
            context = self._runtime.createContext(
                identity=item.identity,
                packRoot=item.pack.root,
                registrationScope=scope,
                allowRegistration=False,
                memoryView=memoryView,
            )
            try:
                callback(context, item.state)
            finally:
                context.invalidate()
                scope.withdraw()
        self._runtime.trace(
            "application-lifecycle-hook-completed",
            attributes={"hook": hookName, "appPackId": loadedPack.pack.packId},
        )

    @packUnloadBoundary
    @lifetimeBoundary
    def close(self) -> None:
        """Revokes new controlled work before legacy unload, without waiting across lanes."""
        if not self._runtime._llmClosePrepared:
            self._runtime.prepareLlmClose(permanent=False)
        self._activationBarrierReached = False
        cleanupErrors = self._closeLoadedPacks(tuple(self._loadedPacks))
        self._loadedPacks.clear()
        if cleanupErrors:
            raise ExceptionGroup("PackLoader cleanup reported errors.", cleanupErrors)

    def _closeLoadedPacks(self, packs: tuple[_LoadedPack, ...] | list[_LoadedPack]) -> list[Exception]:
        """Revokes exact controlled bindings before withdrawing registrations and unloading entries."""
        errors: list[Exception] = []
        for loadedPack in reversed(tuple(packs)):
            self._runtime.trace("pack-unload-started", attributes={"packId": loadedPack.pack.packId})
            for item in loadedPack.entries:
                self._runtime.revokeLlmCodeEntry(item.identity.codeEntryInstanceId)
            loadedPack.registrationScope.withdraw()
            packErrors = self._unloadEntries(list(loadedPack.entries))
            errors.extend(packErrors)
            for item in reversed(loadedPack.entries):
                self._runtime.unregisterCodeEntry(item.identity.codeEntryInstanceId)
                sys.modules.pop(item.module.__name__, None)
            self._runtime.trace(
                "pack-unload-completed" if not packErrors else "pack-unload-failed",
                attributes={"packId": loadedPack.pack.packId, "errorCount": len(packErrors)},
                level="info" if not packErrors else "error",
            )
        return errors

    def _unloadEntries(self, entries: list[_LoadedCodeEntry]) -> list[Exception]:
        """Attempts each eligible onUnload once with fresh cleanup Context and aggregates failures."""
        errors: list[Exception] = []
        for item in reversed(entries):
            callback = getattr(item.module, "onUnload", None)
            if callback is None:
                continue
            cleanupScope = RegistrationScope()
            context = self._runtime.createContext(
                identity=item.identity,
                packRoot=item.pack.root,
                registrationScope=cleanupScope,
                allowRegistration=False,
            )
            try:
                callback(context, item.state)
            except Exception as err:
                errors.append(err)
            finally:
                context.invalidate()
                cleanupScope.withdraw()
        return errors

    @staticmethod
    def _readSource(*, pack: PackDefinition, definition: CodeEntryDefinition) -> tuple[bytes, str, str]:
        """Reads one contained source and derives its format-qualified implementation identity."""
        packRoot = pack.root.resolve()
        sourcePath = (packRoot / definition.source).resolve()
        if not sourcePath.is_relative_to(packRoot):
            raise ValueError(
                f"CodeEntry source escapes Pack root for {pack.packId!r}/{definition.codeEntryId!r}: {definition.source!r}.",
            )
        if not sourcePath.is_file():
            raise FileNotFoundError(f"CodeEntry source does not exist: {sourcePath}.")
        suffixes = _FORMAT_SUFFIXES[definition.format]
        if sourcePath.suffix not in suffixes:
            label = "Python" if definition.format == PYTHON_SOURCE_FORMAT else definition.format
            raise ValueError(f"{label} CodeEntry source must use a {'/'.join(suffixes)} file: {sourcePath}.")
        if definition.format != PYTHON_SOURCE_FORMAT and sourcePath.stat().st_size > JS_SOURCE_LIMIT:
            raise ValueError(f"CodeEntry source exceeds {JS_SOURCE_LIMIT} bytes: {sourcePath}.")

        try:
            sourceBytes = sourcePath.read_bytes()
        except OSError as err:
            raise RuntimeError(f"Could not read CodeEntry source: {sourcePath}.") from err
        if definition.format != PYTHON_SOURCE_FORMAT and len(sourceBytes) > JS_SOURCE_LIMIT:
            raise ValueError(f"CodeEntry source exceeds {JS_SOURCE_LIMIT} bytes: {sourcePath}.")
        sourceSha256 = hashlib.sha256(sourceBytes).hexdigest()
        identityMaterial = (
            f"{definition.format}\0{pack.packId}\0{pack.version}\0"
            f"{definition.codeEntryId}\0{definition.source}\0{sourceSha256}"
        ).encode("utf-8")
        implementationId = hashlib.sha256(identityMaterial).hexdigest()
        return sourceBytes, sourceSha256, implementationId

    @staticmethod
    def _declareSource(*, pack: PackDefinition, definition: CodeEntryDefinition) -> DeclaredSourceEntry:
        """Records a non-Python CodeEntry without executing it."""
        sourceBytes, sourceSha256, implementationId = PackLoader._readSource(pack=pack, definition=definition)
        return DeclaredSourceEntry(
            packId=pack.packId,
            packVersion=pack.version,
            codeEntryId=definition.codeEntryId,
            format=definition.format,
            role=definition.role,
            source=definition.source,
            sourceBytes=sourceBytes,
            sourceSha256=sourceSha256,
            implementationId=implementationId,
        )

    @staticmethod
    def _loadModule(
        *,
        pack: PackDefinition,
        definition: CodeEntryDefinition,
        instanceId: str,
    ) -> tuple[ModuleType, str, str]:
        """Loads exactly the bytes used to establish CodeEntry implementation identity.

        The Python implementation identity currently covers the declared
        CodeEntry source bytes plus Pack/version/entry identity. It deliberately
        does not pretend to capture arbitrary transitive imports; future Pack
        materialization can widen the implementation closure without changing
        the producer-metadata contract consumed by persistent values.
        """
        sourceBytes, sourceSha256, implementationId = PackLoader._readSource(pack=pack, definition=definition)
        sourcePath = (pack.root.resolve() / definition.source).resolve()

        moduleName = (
            f"_tertalis_{pack.packId.replace('.', '_')}_"
            f"{definition.codeEntryId.replace('.', '_')}_{instanceId.replace('-', '_')}"
        )
        spec = importlib.util.spec_from_file_location(moduleName, sourcePath)
        if spec is None:
            raise RuntimeError(f"Could not create Python module spec for {sourcePath}.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[moduleName] = module
        try:
            code = compile(sourceBytes, str(sourcePath), "exec")
            exec(code, module.__dict__)
        except BaseException:
            sys.modules.pop(moduleName, None)
            raise
        return module, sourceSha256, implementationId
