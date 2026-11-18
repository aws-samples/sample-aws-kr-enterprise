"""Tool call messages rebuilt from observed Kiro ACP updates."""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from .history import tool_call_message
from .types import ToolCall

_MAX_TOOL_IDS = 8192
_MAX_EVENTS_PER_TOOL = 256
_MAX_TOOL_INPUT_JSON_BYTES = 1024 * 1024
_MAX_TOOL_INPUT_NODES = 8192
_MAX_TOOL_INPUT_DEPTH = 20


def _identifier(value: Any) -> Optional[str]:
    if (type(value) is not str or not value.strip() or len(value) > 512 or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        return None
    return value


def _metadata(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return reported main-session metadata, rejecting malformed or identified child events."""
    if "agentSubtaskId" in event:
        return None
    metadata = event.get("_meta", {})
    if not isinstance(metadata, dict) or "agentSubtaskId" in metadata:
        return None
    if "kiro" not in metadata:
        return {}
    kiro = metadata["kiro"]
    if not isinstance(kiro, dict) or "agentSubtaskId" in kiro:
        return None
    return kiro


def _context_matches(call: dict[str, Any], update: dict[str, Any]) -> bool:
    for key in ("executionId", "turnId"):
        if key not in call and key not in update:
            continue
        left, right = _identifier(call.get(key)), _identifier(update.get(key))
        if left is None or right is None or left != right:
            return False
    for key in ("serverName", "toolOrigin"):
        if key in call and key in update and (
                type(call[key]) is not type(update[key]) or call[key] != update[key]):
            return False
    return True


def _canonical_input(value: Any) -> Optional[str]:
    if type(value) is not dict:
        return None
    pending = [(value, 0)]
    seen: set[int] = set()
    nodes = 0
    text_bytes = 0
    try:
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if nodes > _MAX_TOOL_INPUT_NODES or depth > _MAX_TOOL_INPUT_DEPTH:
                return None
            kind = type(item)
            if kind is str:
                if len(item) > _MAX_TOOL_INPUT_JSON_BYTES:
                    return None
                text_bytes += len(item.encode("utf-8"))
                if text_bytes > _MAX_TOOL_INPUT_JSON_BYTES:
                    return None
            elif kind in (bool, type(None)):
                continue
            elif kind is int:
                if item.bit_length() > 256:
                    return None
            elif kind is float:
                if not math.isfinite(item):
                    return None
            elif kind in (dict, list):
                if id(item) in seen or len(item) > 1024:
                    return None
                seen.add(id(item))
                if kind is dict:
                    if any(type(key) is not str for key in item):
                        return None
                    children = [child for key, child_value in item.items() for child in (key, child_value)]
                else:
                    children = item
                if len(pending) + len(children) > _MAX_TOOL_INPUT_NODES:
                    return None
                pending.extend((child, depth + 1) for child in children)
            else:
                return None
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return encoded if len(encoded) <= _MAX_TOOL_INPUT_JSON_BYTES else None
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, RuntimeError):
        return None


def observed_tool_messages(events: list[dict[str, Any]], session_id: str) -> dict[int, ToolCall]:
    """Merge each unambiguous main-session tool call with its updates, placed at the call's position."""
    groups: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            continue
        tool_id = _identifier(event.get("toolCallId"))
        if tool_id is None:
            continue
        if tool_id not in groups:
            if len(groups) >= _MAX_TOOL_IDS:
                continue
            groups[tool_id] = {"calls": [], "updates": [], "invalid": False}
        group = groups[tool_id]
        metadata = _metadata(event)
        if metadata is None:
            group["invalid"] = True
            continue
        bucket = group["calls"] if event["sessionUpdate"] == "tool_call" else group["updates"]
        if len(bucket) >= _MAX_EVENTS_PER_TOOL:
            group["invalid"] = True
            continue
        bucket.append((index, event, metadata))

    output: dict[int, ToolCall] = {}
    for tool_id, group in groups.items():
        calls, updates = group["calls"], group["updates"]
        if group["invalid"] or len(calls) != 1:
            continue
        call_index, call, call_meta = calls[0]
        if any(index <= call_index or not _context_matches(call_meta, metadata)
               for index, _, metadata in updates):
            continue
        merged: dict[str, Any] = {}
        for _, event, _ in (calls[0], *updates):
            merged.update({key: value for key, value in event.items() if key != "sessionUpdate"})
        if "rawInput" in merged and merged["rawInput"] is not None and _canonical_input(merged["rawInput"]) is None:
            continue
        if not isinstance(merged.get("title"), str) or _identifier(merged["title"]) is None:
            continue
        output[call_index] = tool_call_message(tool_id, merged, session_id)
    return output
