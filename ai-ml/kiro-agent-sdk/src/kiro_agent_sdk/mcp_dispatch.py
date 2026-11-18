"""Connection-local admission and cancellation before remote HTTP dispatch."""
from __future__ import annotations

from typing import Any, Optional

from .errors import ProtocolError
from .mcp_cancellation import _request_key


class RemoteDispatch:
    """Distinguish queued tool calls from calls handed to the HTTP transport."""

    def __init__(self) -> None:
        self.pending: dict[tuple[type, Any], bool] = {}
        self.cancelled: set[tuple[type, Any]] = set()
        self.closed = False

    def admit(self, message: dict[str, Any]) -> None:
        if message.get("method") != "tools/call":
            return
        key = _request_key(message.get("id"))
        if self.closed or key is None or key in self.pending or key in self.cancelled:
            raise ProtocolError("Remote MCP dispatch ID is unavailable")
        if len(self.pending) >= 128:
            raise ProtocolError("Remote MCP dispatch capacity exceeded")
        self.pending[key] = False

    def begin(self, message: dict[str, Any]) -> bool:
        """Return False to suppress a cancelled POST, including an OAuth retry."""
        if message.get("method") != "tools/call":
            return True
        key = _request_key(message.get("id"))
        if self.closed:
            raise ProtocolError("Remote MCP dispatch connection has closed")
        if key in self.cancelled:
            return False
        if key is None or key not in self.pending:
            raise ProtocolError("Unregistered remote MCP tool dispatch")
        self.pending[key] = True
        return True

    def cancel(self, ident: Any) -> Optional[bool]:
        """Return whether a best-effort upstream notice is needed."""
        key = _request_key(ident)
        if key in self.cancelled:
            return False
        if key is None or key not in self.pending:
            return None
        if len(self.cancelled) >= 4096:
            raise ProtocolError("Remote MCP cancellation history capacity exceeded; reconnect")
        started = self.pending.pop(key)
        self.cancelled.add(key)
        return started

    def response(self, message: dict[str, Any]) -> None:
        if "method" not in message and ("result" in message or "error" in message):
            self.pending.pop(_request_key(message.get("id")), None)

    def close(self) -> None:
        self.closed = True
        self.pending.clear()
        self.cancelled.clear()
