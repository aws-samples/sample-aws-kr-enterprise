"""Validate hook messages before exposing them to application code."""
from __future__ import annotations

from typing import Any

from .errors import ProtocolError


def append_hook_warning(messages: list[str], event: str, message: Any) -> None:
    if (not isinstance(message, str) or len(message) > 8192 or "\0" in message or
            any(0xD800 <= ord(char) <= 0xDFFF for char in message)):
        raise ProtocolError(f"{event} systemMessage must be a bounded string")
    if not message:
        return
    if (len(messages) >= 32 or sum(len(item.encode("utf-8")) for item in messages) +
            len(message.encode("utf-8")) > 65536):
        raise ProtocolError(f"{event} system messages exceed the limit")
    messages.append(message)
