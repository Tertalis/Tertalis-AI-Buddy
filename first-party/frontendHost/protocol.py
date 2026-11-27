# file: first-party/frontendHost/protocol.py ; version: 1
"""Representation of the frontend host's Communication roles (JSON over HTTP).

Invocation request:  {"invocationId", "target", "arguments", "expect"?}
Accepted:            {"role": "accepted", "invocationId"}
Result:              {"role": "result", "invocationId", "value"}
FlowError:           {"role": "flowError", "invocationId"?, "code", "message"}
Emit (stream):       {"track", "position", "kind": "set"|"append"|"reset", "value"?}
Gap (stream):        {"track", "position"}  -> client fetches the snapshot
Run (stream):        {"runId"}               -> first event of every stream
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

__all__ = [
    "INVOCATIONS_TRACK",
    "MAX_INVOCATION_BODY",
    "MAX_VALUE_BYTES",
    "Invocation",
    "ProtocolError",
    "encode",
    "flowError",
    "parseInvocation",
    "requireTrackName",
]

INVOCATIONS_TRACK = "flow.invocations"
MAX_INVOCATION_BODY = 64 * 1024
MAX_VALUE_BYTES = 256 * 1024
_TRACK = re.compile(r"[a-z][A-Za-z0-9_.-]{0,63}")
_INVOCATION_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_TARGET = re.compile(r"[A-Za-z][A-Za-z0-9_.@-]{0,127}")
_CODE = re.compile(r"[a-z][a-z0-9-]{0,63}")


class ProtocolError(ValueError):
    """A request that does not match the Representation; carries a fixed public code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Invocation:
    """One accepted browser Invocation, as handed to the loop owner."""

    invocationId: str
    target: str
    arguments: dict
    expect: dict | None


def encode(value: object) -> bytes:
    """Strict compact JSON (no NaN/Infinity); raises TypeError/ValueError for non-JSON values."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def requireTrackName(name: object) -> str:
    """Track names are lowercase-initial dotted identifiers; no ':' or ',' (used by stream queries)."""
    if type(name) is not str or not _TRACK.fullmatch(name):
        raise ValueError(f"Invalid track name: {name!r}.")
    return name


def requireCode(code: object) -> str:
    """FlowError codes are short lowercase kebab identifiers."""
    if type(code) is not str or not _CODE.fullmatch(code):
        raise ValueError(f"Invalid FlowError code: {code!r}.")
    return code


def flowError(code: str, message: str, invocationId: str | None = None) -> dict:
    """Builds a FlowError reply/item; message must be a short, non-reflective, public string."""
    item = {"role": "flowError", "code": requireCode(code), "message": str(message)[:500]}
    if invocationId is not None:
        item["invocationId"] = invocationId
    return item


def parseInvocation(body: bytes) -> Invocation:
    """Validates one Invocation request body."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as err:
        raise ProtocolError("invalid-json", "The request body must be a JSON object.") from err
    if type(value) is not dict or not {"invocationId", "target", "arguments"} <= set(value) <= {"invocationId", "target", "arguments", "expect"}:
        raise ProtocolError("invalid-invocation", "An Invocation needs invocationId, target and arguments only.")
    invocationId, target, arguments = value["invocationId"], value["target"], value["arguments"]
    expect = value.get("expect")
    if type(invocationId) is not str or not _INVOCATION_ID.fullmatch(invocationId):
        raise ProtocolError("invalid-invocation", "invocationId must be 1-64 letters, digits or dashes.")
    if type(target) is not str or not _TARGET.fullmatch(target):
        raise ProtocolError("invalid-invocation", "target is not a valid identifier.")
    if type(arguments) is not dict:
        raise ProtocolError("invalid-invocation", "arguments must be a JSON object.")
    if expect is not None and type(expect) is not dict:
        raise ProtocolError("invalid-invocation", "expect must be a JSON object.")
    return Invocation(invocationId=invocationId, target=target, arguments=arguments, expect=expect)
