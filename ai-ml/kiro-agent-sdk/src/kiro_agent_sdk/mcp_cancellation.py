"""Track unambiguous MCP tool calls for best-effort cancellation requests."""
from __future__ import annotations

import json
from typing import Any, Optional
from .errors import ProtocolError

_MAX_TRACKED = 4096
_MAX_ID_CHARS = 512


def _request_key(value: Any) -> Optional[tuple[type, Any]]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str) and len(value) > _MAX_ID_CHARS:
        return None
    return type(value), value


def parse_message(line: bytes) -> Optional[dict[str, Any]]:
    """Parse only unambiguous individual JSON-RPC messages for tracking."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(line, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


class ObservedToolCalls:
    """Store request IDs only; ambiguous or excess IDs are never cancelled."""

    def __init__(self) -> None:
        self._pending: dict[tuple[type, Any], bool] = {}
        self._untracked: set[tuple[type, Any]] = set()
        self._suppressed: set[tuple[type, Any]] = set()
        self._overflowed = False

    def request(self, message: Any) -> bool:
        if not isinstance(message, dict):
            return True
        key = _request_key(message.get("id"))
        if message.get("method") == "notifications/cancelled":
            params = message.get("params")
            if isinstance(params, dict):
                self.suppress(params.get("requestId"))
            return True
        if "method" in message and key in self._suppressed:
            return False
        if "method" not in message or "id" not in message:
            return True
        raw_id = message.get("id")
        if isinstance(raw_id, (bool, float, list, dict)):
            self.invalidate()
            return True
        if key is None or key in self._untracked:
            return True
        if key in self._pending:
            # A repeated in-flight ID cannot be correlated safely.
            del self._pending[key]
            self._untracked.add(key)
        elif not self._overflowed:
            if len(self._pending) + len(self._untracked) < _MAX_TRACKED:
                self._pending[key] = message.get("method") == "tools/call"
            else:
                # Once an ID was missed, later reuse cannot be correlated safely.
                self._overflowed = True
        return True

    def response(self, message: Any) -> bool:
        if (not isinstance(message, dict) or "method" in message or
                not ("result" in message or "error" in message)):
            return True
        raw_id = message.get("id")
        if isinstance(raw_id, (bool, float, list, dict)):
            self.invalidate()
            return True
        key = _request_key(raw_id)
        if key in self._suppressed:
            return False
        if key is not None:
            self._pending.pop(key, None)
        return True

    def ids(self) -> list[Any]:
        return [key[1] for key, is_tool in self._pending.items() if is_tool]

    def suppress(self, ident: Any) -> bool:
        key = _request_key(ident)
        if key is None or not self._pending.get(key, False):
            return False
        if len(self._suppressed) >= _MAX_TRACKED:
            raise ProtocolError("MCP cancellation history capacity exceeded; reconnect")
        del self._pending[key]
        self._suppressed.add(key)
        return True

    def invalidate(self) -> None:
        """Stop tracking after a message whose request IDs cannot be disambiguated."""
        self._pending.clear()
        self._untracked.clear()
        self._overflowed = True


def cancellation_notice(ident: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": ident, "reason": "Requested by SDK client"}}
