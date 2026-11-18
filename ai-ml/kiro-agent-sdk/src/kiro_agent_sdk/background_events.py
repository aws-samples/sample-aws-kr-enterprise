"""Bounded snapshots for optional background task event history."""
from __future__ import annotations

import json
import math
from dataclasses import fields
from typing import Any

from .types import (AgentMessage, HookEventMessage, KiroEvent, MirrorErrorMessage, ResultMessage, StreamEvent,
                    TextBlock, ThoughtBlock, ToolCall, UserMessage)

_MESSAGES = frozenset((AgentMessage, HookEventMessage, KiroEvent, MirrorErrorMessage, ResultMessage, StreamEvent,
                       ToolCall, UserMessage))
_DATA = _MESSAGES | frozenset((TextBlock, ThoughtBlock))


def _copy_bounded_json(value: Any, *, max_bytes: int, allow_messages: bool) -> Any:
    seen: set[int] = set()
    nodes = 0
    text_bytes = 0

    def copy_value(value: Any, depth: int) -> Any:
        nonlocal nodes, text_bytes
        nodes += 1
        if nodes > 4096 or depth > 24:
            raise ValueError("Background event exceeds structural limits")
        kind = type(value)
        if kind in (type(None), bool):
            return value
        if kind is str:
            if len(value) > max_bytes:
                raise ValueError("Background event exceeds size limit")
            text_bytes += len(value.encode("utf-8"))
            if text_bytes > max_bytes:
                raise ValueError("Background event exceeds size limit")
            return value
        if kind is int:
            if value.bit_length() > 256:
                raise ValueError("Background event integer is too large")
            return value
        if kind is float:
            if not math.isfinite(value):
                raise ValueError("Background event number is invalid")
            return value
        if not (allow_messages and kind in _DATA) and kind not in ((dict, list, tuple) if allow_messages else (dict, list)):
            raise ValueError("Unsupported background event data")
        if id(value) in seen:
            raise ValueError("Background event contains a cycle")
        seen.add(id(value))
        try:
            if allow_messages and kind in _DATA:
                return {item.name: copy_value(getattr(value, item.name), depth + 1) for item in fields(value)}
            if kind is dict:
                if len(value) > 1024 or any(type(key) is not str for key in value):
                    raise ValueError("Invalid background event mapping")
                return {copy_value(key, depth + 1): copy_value(item, depth + 1) for key, item in value.items()}
            if len(value) > 1024:
                raise ValueError("Background event list is too large")
            return [copy_value(item, depth + 1) for item in value]
        finally:
            seen.remove(id(value))

    return copy_value(value, 0)


def encode_background_event(event: Any, *, max_bytes: int) -> bytes:
    """Copy supported message data under structural and UTF-8 limits."""
    if type(event) not in _MESSAGES:
        raise ValueError("Unsupported background event")
    payload = {"type": type(event).__name__,
               "message": _copy_bounded_json(event, max_bytes=max_bytes, allow_messages=True)}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError("Background event exceeds size limit")
    return encoded


def encode_structured_result(value: Any, *, max_bytes: int) -> bytes:
    """Snapshot bounded JSON without invoking custom encoders."""
    copied = _copy_bounded_json(value, max_bytes=max_bytes, allow_messages=False)
    encoded = json.dumps(copied, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError("Structured result exceeds size limit")
    return encoded
