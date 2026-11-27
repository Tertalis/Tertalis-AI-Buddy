# file: first-party/frontendHost/codeEntry.py ; version: 1
"""Explicit optional Pack activation: one loopback frontend host and channel per ApplicationRun."""
import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path

_CONFIG_KEYS = {"port", "maxViews", "title", "main"}


def _loadImplementation():
    """Loads this Pack's sibling package without depending on repository layout or cwd."""
    root = Path(__file__).resolve().parent
    packageName = "_tertalis_frontend_host_" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    if packageName not in sys.modules:
        spec = importlib.util.spec_from_file_location(packageName, root / "__init__.py", submodule_search_locations=[str(root)])
        if spec is None or spec.loader is None:
            raise RuntimeError("Frontend host package could not be loaded.")
        package = importlib.util.module_from_spec(spec)
        sys.modules[packageName] = package
        try:
            spec.loader.exec_module(package)
        except BaseException:
            sys.modules.pop(packageName, None)
            raise
    return importlib.import_module(packageName + ".host")


_implementation = _loadImplementation()


def onLoad(ctx):
    """Starts the endpoint and registers same-app capabilities; serves nothing until channel@1."""
    configuration = ctx.config.get("frontendHost", {})
    if type(configuration) is not dict or set(configuration) - _CONFIG_KEYS:
        raise ValueError("Invalid frontendHost configuration.")
    host = _implementation.FrontendHost(
        port=configuration.get("port", 0),
        maxViews=configuration.get("maxViews", 8),
        title=configuration.get("title", "Tertalis"),
        main=configuration.get("main"),
    )

    def channel(invocation, payload):
        """Returns the run's thread-safe channel; first call seals the served browser sources."""
        if payload is not None:
            raise ValueError("channel takes no payload.")
        return host.seal(invocation.packs.declaredEntries("js-module-source@1"))

    def connection(_invocation, payload):
        """Returns the launch URL (with credential fragment) for the launcher to display only."""
        if payload is not None:
            raise ValueError("connection takes no payload.")
        return host.connection()

    try:
        ctx.capabilities.register("frontend.host.channel@1", channel)
        ctx.capabilities.register("frontend.host.connection@1", connection)
    except BaseException:
        host.close()
        raise
    return host


def onUnload(_ctx, state):
    """Closes the endpoint, streams and channel."""
    if state is not None:
        state.close()
