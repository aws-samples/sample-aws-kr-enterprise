"""Validate the dictionaries that SDK hook callbacks return.

The keys follow what a Kiro command hook can do: add ``context`` (exit 0 with stdout), ``block`` with a
reason (exit 2 with stderr), ``ask`` the user before a tool runs, ``continue`` working after ``agentStop``,
or report a ``warning``. ``allow``, ``tool_input`` and ``title`` are SDK additions for tool approval,
shell command edits and session titles.
"""
from __future__ import annotations

from typing import Any

from .errors import ProtocolError

HOOK_OUTPUT_KEYS = frozenset({"context", "block", "ask", "allow", "tool_input", "title", "continue", "warning"})
_MAX_TEXT = 65536


def _text(event: str, key: str, value: Any) -> str:
    if not isinstance(value, str) or "\0" in value or len(value) > _MAX_TEXT or any(
            0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ProtocolError(f"{event} hook {key} must be a bounded string")
    return value


def parse_hook_output(event: str, value: Any, allowed: frozenset[str] | set[str]) -> dict[str, Any]:
    """Return the validated output of one hook callback.

    ``block``, ``ask`` and ``continue`` accept ``True`` or a reason string and are omitted when false.
    An empty ``context`` is dropped.
    """
    if not isinstance(value, dict):
        raise ProtocolError(f"{event} hook must return a dictionary")
    unsupported = set(value) - set(allowed)
    if unsupported:
        raise ProtocolError(f"Unsupported {event} hook output: {sorted(unsupported)}")
    output: dict[str, Any] = {}
    for key in ("block", "ask", "continue"):
        if key not in value or value[key] is None or value[key] is False:
            continue
        reason = value[key]
        output[key] = None if reason is True else _text(event, key, reason)
    if "context" in value and value["context"] is not None:
        context = _text(event, "context", value["context"])
        if context.strip():
            output["context"] = context
    if "title" in value and value["title"] is not None:
        title = _text(event, "title", value["title"]).strip()
        if not title:
            raise ProtocolError(f"{event} hook title must not be empty")
        output["title"] = title
    if "warning" in value and value["warning"] is not None:
        output["warning"] = value["warning"]
    if "allow" in value and value["allow"] is not None:
        if not isinstance(value["allow"], bool):
            raise ProtocolError(f"{event} hook allow must be a boolean")
        if value["allow"]:
            output["allow"] = True
    if "tool_input" in value and value["tool_input"] is not None:
        if not isinstance(value["tool_input"], dict):
            raise ProtocolError(f"{event} hook tool_input must be a dictionary")
        output["tool_input"] = value["tool_input"]
    if sum(key in output for key in ("block", "ask", "allow")) > 1:
        raise ProtocolError(f"{event} hook may return only one of block, ask and allow")
    if "tool_input" in output and ("block" in output or "ask" in output):
        raise ProtocolError(f"{event} hook tool_input cannot accompany block or ask")
    return output
