# file: tests/backend/packs/test_codeEntryFormats.py ; version: 2
"""CodeEntry format/role declarations, closed Pack kinds, and declared (non-executed) sources."""
import hashlib
import json
from pathlib import Path

import pytest

from backend.application.applicationRuntime import ApplicationRuntime
from backend.packs.runtime import (
    JS_MODULE_FORMAT,
    PYTHON_SOURCE_FORMAT,
    DeclaredSourceEntry,
    ManualActivationPlan,
    PackResolver,
)
import backend.packs.runtime as packRuntime


def _write(root: Path, packId: str, entries: list[dict], files: dict[str, str | bytes], *, kind: str = "modPack") -> Path:
    directory = root / packId.replace(".", "_")
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps({"packId": packId, "kind": kind, "version": "0.0.1", "codeEntries": entries}),
        encoding="utf-8",
    )
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")  # hashes are compared exactly
    return directory


def _resolve(root: Path, packId: str):
    return PackResolver(roots=(root,)).requireSingle(packId)


def test_defaults_keep_existing_manifests_python_main(tmp_path):
    _write(tmp_path, "p.one", [{"id": "entry", "source": "codeEntry.py"}], {"codeEntry.py": ""})
    entry = _resolve(tmp_path, "p.one").codeEntries[0]
    assert (entry.format, entry.role) == (PYTHON_SOURCE_FORMAT, "main")


@pytest.mark.parametrize("kind", ["viewPack", "pack", "AppPack"])
def test_pack_kind_set_is_closed(tmp_path, kind):
    _write(tmp_path, "p.kind", [], {}, kind=kind)
    with pytest.raises(ValueError, match="unknown kind"):
        _resolve(tmp_path, "p.kind")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"id": "e", "source": "a.mjs", "adapter": "browser"}, "unknown key"),
        ({"id": "e", "source": "a.mjs", "format": "js@9"}, "unsupported format"),
        ({"id": "e", "source": "a.mjs", "format": JS_MODULE_FORMAT, "role": "default"}, "invalid role"),
        ({"id": "e", "source": "a.py", "role": "library"}, "not supported yet"),
    ],
)
def test_invalid_code_entry_declarations_are_rejected(tmp_path, entry, message):
    _write(tmp_path, "p.bad", [entry], {})
    with pytest.raises(ValueError, match=message):
        _resolve(tmp_path, "p.bad")


def test_at_most_one_main_per_format(tmp_path):
    entries = [
        {"id": "a", "source": "a.mjs", "format": JS_MODULE_FORMAT},
        {"id": "b", "source": "b.mjs", "format": JS_MODULE_FORMAT},
    ]
    _write(tmp_path, "p.mains", entries, {})
    with pytest.raises(ValueError, match="more than one main"):
        _resolve(tmp_path, "p.mains")


def test_python_and_js_mains_may_coexist_with_js_libraries(tmp_path):
    entries = [
        {"id": "py", "source": "codeEntry.py"},
        {"id": "ui", "source": "ui/main.mjs", "format": JS_MODULE_FORMAT},
        {"id": "lib", "source": "lib.mjs", "format": JS_MODULE_FORMAT, "role": "library"},
    ]
    _write(tmp_path, "p.mixed", entries, {})
    pack = _resolve(tmp_path, "p.mixed")
    assert [(e.codeEntryId, e.format, e.role) for e in pack.codeEntries] == [
        ("py", PYTHON_SOURCE_FORMAT, "main"),
        ("ui", JS_MODULE_FORMAT, "main"),
        ("lib", JS_MODULE_FORMAT, "library"),
    ]


def _activate(tmp_path, *packIds):
    runtime = ApplicationRuntime(appPackId="test.app", packResolver=PackResolver(roots=(tmp_path,)))
    runtime.start()
    runtime.packLoader.activate(ManualActivationPlan(packIds=packIds))
    return runtime


def test_js_entries_are_recorded_and_never_executed(tmp_path):
    marker = tmp_path / "executed.txt"
    jsSource = "export function mount() {}\nthis is not python(\n"
    _write(
        tmp_path,
        "p.ui",
        [
            {"id": "py", "source": "codeEntry.py"},
            {"id": "ui", "source": "ui/main.mjs", "format": JS_MODULE_FORMAT},
        ],
        {"codeEntry.py": f"from pathlib import Path\nPath({str(marker)!r}).write_text('py')\n", "ui/main.mjs": jsSource},
    )
    runtime = _activate(tmp_path, "p.ui")
    try:
        assert marker.read_text() == "py"
        (declared,) = runtime.packLoader.declaredEntries(JS_MODULE_FORMAT)
        assert isinstance(declared, DeclaredSourceEntry)
        assert (declared.packId, declared.codeEntryId, declared.role, declared.source) == ("p.ui", "ui", "main", "ui/main.mjs")
        assert declared.sourceBytes == jsSource.encode("utf-8")
        assert declared.sourceSha256 == hashlib.sha256(jsSource.encode("utf-8")).hexdigest()
        assert runtime.packLoader.declaredEntries(PYTHON_SOURCE_FORMAT) == ()
        with pytest.raises(AttributeError):
            declared.sourceBytes = b""  # frozen
    finally:
        runtime.close()
    assert runtime.packLoader.declaredEntries(JS_MODULE_FORMAT) == ()


def test_declared_entries_follow_activation_order(tmp_path):
    for packId in ("p.b", "p.a"):
        _write(tmp_path, packId, [{"id": "lib", "source": "lib.mjs", "format": JS_MODULE_FORMAT, "role": "library"}], {"lib.mjs": packId})
    runtime = _activate(tmp_path, "p.b", "p.a")
    try:
        assert [item.packId for item in runtime.packLoader.declaredEntries(JS_MODULE_FORMAT)] == ["p.b", "p.a"]
    finally:
        runtime.close()


def test_python_implementation_identity_is_unchanged(tmp_path):
    code = "def onLoad(ctx):\n    return ctx.identity.implementationId\n"
    _write(tmp_path, "p.id", [{"id": "entry", "source": "codeEntry.py"}], {"codeEntry.py": code})
    runtime = _activate(tmp_path, "p.id")
    try:
        sha = hashlib.sha256(code.encode()).hexdigest()
        expected = hashlib.sha256(f"python-source@1\0p.id\x000.0.1\0entry\0codeEntry.py\0{sha}".encode()).hexdigest()
        loadedEntry = runtime.packLoader._loadedPacks[0].entries[0]
        assert loadedEntry.state == expected
        assert loadedEntry.identity.implementationFormat == PYTHON_SOURCE_FORMAT
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("source", "files", "error", "message"),
    [
        ("../outside.mjs", {}, ValueError, "escapes Pack root"),
        ("missing.mjs", {}, FileNotFoundError, "does not exist"),
        ("ui.js", {"ui.js": ""}, ValueError, "must use a .mjs file"),
    ],
)
def test_js_source_path_rules(tmp_path, source, files, error, message):
    (tmp_path / "outside.mjs").write_text("", encoding="utf-8")
    _write(tmp_path, "p.path", [{"id": "ui", "source": source, "format": JS_MODULE_FORMAT}], files)
    runtime = ApplicationRuntime(appPackId="test.app", packResolver=PackResolver(roots=(tmp_path / "p_path",)))
    runtime.start()
    try:
        with pytest.raises(error, match=message):
            runtime.packLoader.activate(ManualActivationPlan(packIds=("p.path",)))
        assert runtime.packLoader.declaredEntries(JS_MODULE_FORMAT) == ()
    finally:
        runtime.close()


def test_js_source_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(packRuntime, "JS_SOURCE_LIMIT", 8)
    _write(tmp_path, "p.big", [{"id": "ui", "source": "ui.mjs", "format": JS_MODULE_FORMAT}], {"ui.mjs": "x" * 9})
    runtime = ApplicationRuntime(appPackId="test.app", packResolver=PackResolver(roots=(tmp_path,)))
    runtime.start()
    try:
        with pytest.raises(ValueError, match="exceeds"):
            runtime.packLoader.activate(ManualActivationPlan(packIds=("p.big",)))
    finally:
        runtime.close()


def test_failed_activation_leaves_no_declared_entries(tmp_path):
    _write(
        tmp_path,
        "p.fail",
        [
            {"id": "ui", "source": "ui.mjs", "format": JS_MODULE_FORMAT},
            {"id": "py", "source": "codeEntry.py"},
        ],
        {"ui.mjs": "", "codeEntry.py": "raise RuntimeError('boom')\n"},
    )
    runtime = ApplicationRuntime(appPackId="test.app", packResolver=PackResolver(roots=(tmp_path,)))
    runtime.start()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            runtime.packLoader.activate(ManualActivationPlan(packIds=("p.fail",)))
        assert runtime.packLoader.declaredEntries(JS_MODULE_FORMAT) == ()
    finally:
        runtime.close()


def test_context_exposes_declared_entries_only_while_valid(tmp_path):
    _write(tmp_path, "p.lib", [{"id": "lib", "source": "lib.mjs", "format": JS_MODULE_FORMAT, "role": "library"}], {"lib.mjs": "//"})
    _write(
        tmp_path,
        "p.reader",
        [{"id": "entry", "source": "codeEntry.py"}],
        {"codeEntry.py": (
            "def onLoad(ctx):\n"
            "    found = [e.packId for e in ctx.packs.declaredEntries('js-module-source@1')]\n"
            "    return {'found': found, 'packs': ctx.packs}\n"
        )},
    )
    runtime = _activate(tmp_path, "p.lib", "p.reader")
    try:
        state = runtime.packLoader._loadedPacks[1].entries[0].state
        assert state["found"] == ["p.lib"]
        with pytest.raises(RuntimeError, match="no longer valid"):
            state["packs"].declaredEntries(JS_MODULE_FORMAT)
    finally:
        runtime.close()
