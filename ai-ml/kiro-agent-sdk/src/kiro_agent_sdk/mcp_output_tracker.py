"""Bounded request correlation for SDK-mediated external MCP result transforms."""
from __future__ import annotations

import asyncio
import copy
from typing import Any, Awaitable, Callable, Optional

from .errors import ProtocolError
from .mcp_cancellation import _request_key


class McpOutputTracker:
    def __init__(self, transform: Optional[Callable[..., Awaitable[dict[str, Any]]]],
                 send: Callable[[dict[str, Any]], Awaitable[None]], fail: Callable[[], None]):
        # transform is None when only input review is configured; results then pass unchanged.
        self._transform, self._send, self._fail = transform, send, fail
        self._pending: dict[tuple[type, Any], dict[str, Any]] = {}
        self._cancelled_ids: set[tuple[type, Any]] = set()
        self._lock = asyncio.Lock()
        self._jobs: set[asyncio.Task] = set()

    async def reject_cancelled_id(self, wire: dict[str, Any]) -> bool:
        if "method" in wire and "id" in wire and _request_key(wire["id"]) in self._cancelled_ids:
            await self._send({"jsonrpc": "2.0", "id": wire["id"],
                              "error": {"code": -32600, "message": "MCP request ID cannot be reused after cancellation"}})
            return True
        return False

    async def request(self, wire: dict[str, Any]) -> bool:
        if wire.get("jsonrpc") != "2.0" or ("id" in wire and _request_key(wire["id"]) is None):
            raise ProtocolError("Invalid MCP request")
        method = wire.get("method")
        if method is not None and not isinstance(method, str):
            raise ProtocolError("Invalid MCP request")
        if ((method is not None and ("result" in wire or "error" in wire)) or
                (method is None and ("result" in wire) == ("error" in wire))):
            raise ProtocolError("Invalid MCP request")
        if method == "notifications/cancelled":
            params = wire.get("params") or {}
            if not isinstance(params, dict):
                raise ProtocolError("Invalid MCP cancellation")
            await self.cancel(params.get("requestId"))
        if method is None or "id" not in wire:
            return True
        key = _request_key(wire["id"])
        params = wire.get("params") or {}
        if not isinstance(method, str) or key is None or not isinstance(params, dict):
            raise ProtocolError("Cannot correlate MCP request")
        name, arguments = params.get("name"), params.get("arguments")
        if arguments is None:
            arguments = {}
        if method == "tools/call" and (not isinstance(name, str) or not isinstance(arguments, dict)):
            raise ProtocolError("Invalid MCP tool request")
        async with self._lock:
            if await self.reject_cancelled_id(wire):
                return False
            if key in self._pending or len(self._pending) >= 128:
                raise ProtocolError("MCP request ID is ambiguous or capacity exceeded")
            self._pending[key] = {"method": method, "name": name, "arguments": copy.deepcopy(arguments),
                                  "cancelled": False, "response_received": False, "job": None}
            return True

    async def response(self, wire: dict[str, Any]) -> None:
        if wire.get("jsonrpc") != "2.0" or ("id" in wire and _request_key(wire["id"]) is None):
            raise ProtocolError("Invalid MCP response")
        if "method" in wire:
            if not isinstance(wire["method"], str) or "result" in wire or "error" in wire:
                raise ProtocolError("Invalid MCP response")
            await self._send(wire)
            return
        if ("result" in wire) == ("error" in wire):
            raise ProtocolError("Invalid MCP response")
        key = _request_key(wire.get("id"))
        async with self._lock:
            if key in self._cancelled_ids:
                return
            state = self._pending.get(key)
            if state is None or state["response_received"]:
                raise ProtocolError("Cannot correlate MCP response")
            state["response_received"] = True
            if state["cancelled"]:
                self._pending.pop(key, None)
                return
            if state["method"] != "tools/call":
                await self._send(wire)
                self._pending.pop(key, None)
                return
            job = asyncio.create_task(self._deliver(wire, key, state))
            state["job"] = job
            self._jobs.add(job)
            job.add_done_callback(self._done)

    async def _deliver(self, wire: dict[str, Any], key: tuple[type, Any], state: dict[str, Any]) -> None:
        if state["method"] == "tools/call" and "result" in wire:
            if not isinstance(wire["result"], dict):
                wire["result"] = {"content": [{"type": "text", "text": "Invalid MCP tool result"}], "isError": True}
            elif self._transform is not None and (
                    wire["result"].get("resultType") != "input_required" or
                    "content" in wire["result"] or "structuredContent" in wire["result"]):
                original_type = wire["result"].get("resultType")
                replacement = await self._transform(state["name"], state["arguments"], wire["result"])
                if original_type is not None:
                    if replacement.get("resultType", original_type) != original_type:
                        replacement = {"content": [{"type": "text", "text": "SDK MCP output transform failed"}],
                                       "isError": True}
                    replacement["resultType"] = original_type
                wire["result"] = replacement
        elif state["method"] == "tools/call" and self._transform is not None:
            # Review a JSON-RPC tool error like any other tool output and send only the reviewed
            # result, so error.data and other unreviewed fields never pass the output transform.
            error = wire["error"] if isinstance(wire["error"], dict) else {}
            message = error.get("message") if isinstance(error.get("message"), str) else "MCP tool call failed"
            original = {"content": [{"type": "text", "text": message}], "isError": True}
            replacement = await self._transform(state["name"], state["arguments"], original)
            wire = {"jsonrpc": "2.0", "id": wire.get("id"), "result": {**replacement, "isError": True}}
        async with self._lock:
            if self._pending.get(key) is not state or state["cancelled"]:
                return
            await self._send(wire)
            self._pending.pop(key, None)

    def _done(self, task: asyncio.Task) -> None:
        self._jobs.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._fail()

    async def cancel(self, ident: Any) -> None:
        key = _request_key(ident)
        async with self._lock:
            state = self._pending.get(key)
            if state is None or state["cancelled"]:
                return
            if len(self._cancelled_ids) >= 4096:
                raise ProtocolError("MCP cancellation history capacity exceeded; reconnect")
            self._cancelled_ids.add(key)
            self._pending.pop(key, None)
            state["cancelled"] = True
            if state["job"] is not None:
                state["job"].cancel()
            await self._send({"jsonrpc": "2.0", "id": ident,
                              "error": {"code": -32800, "message": "MCP request cancelled locally"}})
            if state["response_received"]:
                self._pending.pop(key, None)

    async def discard(self, ident: Any) -> None:
        """Forget a request that was never forwarded to the backend."""
        key = _request_key(ident)
        async with self._lock:
            state = self._pending.pop(key, None)
            if state is not None:
                state["cancelled"] = True
                if state["job"] is not None:
                    state["job"].cancel()

    async def tool_ids(self) -> list[Any]:
        async with self._lock:
            return [key[1] for key, state in self._pending.items()
                    if state["method"] == "tools/call" and not state["cancelled"]]

    async def has_id(self, ident: Any) -> bool:
        async with self._lock:
            return _request_key(ident) in self._pending or _request_key(ident) in self._cancelled_ids

    async def drain(self) -> None:
        jobs = list(self._jobs)
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    def discard_pending(self) -> None:
        for state in self._pending.values():
            state["cancelled"] = True
        for job in self._jobs:
            job.cancel()

    async def close(self) -> None:
        self.discard_pending()
        jobs = list(self._jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._pending.clear()
