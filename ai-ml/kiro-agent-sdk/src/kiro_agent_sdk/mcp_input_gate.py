"""Cancellable input transformation before sending a mediated MCP tool call."""
from __future__ import annotations

import asyncio
import copy
from typing import Any, Awaitable, Callable

from .errors import McpInputDeniedError, ProtocolError
from .mcp_cancellation import _request_key
from .mcp_output_tracker import McpOutputTracker


class McpInputGate:
    def __init__(self, transform: Callable[..., Awaitable[dict[str, Any]]], tracker: McpOutputTracker,
                 send: Callable[[dict[str, Any]], Awaitable[None]],
                 backend: Callable[[dict[str, Any]], Awaitable[None]], backend_lock: asyncio.Lock,
                 enabled: Callable[[], bool], fail: Callable[[], None]):
        self._transform, self._tracker, self._send, self._backend = transform, tracker, send, backend
        self._backend_lock, self._enabled, self._fail = backend_lock, enabled, fail
        self._lock = asyncio.Lock()
        self._pending: dict[tuple[type, Any], asyncio.Task] = {}
        self._jobs: set[asyncio.Task] = set()

    async def check_available(self, wire: dict[str, Any]) -> None:
        if "method" not in wire or "id" not in wire:
            return
        key = _request_key(wire["id"])
        if key is None:
            raise ProtocolError("Cannot correlate MCP request")
        async with self._lock:
            if key in self._pending:
                raise ProtocolError("MCP request ID is ambiguous")

    async def submit(self, wire: dict[str, Any]) -> None:
        if await self._tracker.reject_cancelled_id(wire):
            return
        key = _request_key(wire.get("id"))
        params = wire.get("params")
        if (wire.get("jsonrpc") != "2.0" or wire.get("method") != "tools/call" or
                "result" in wire or "error" in wire or key is None or
                not isinstance(params, dict) or not isinstance(params.get("name"), str)):
            raise ProtocolError("Invalid MCP tool request")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError("Invalid MCP tool arguments")
        async with self._lock:
            if key in self._pending or len(self._pending) >= 128 or await self._tracker.has_id(wire["id"]):
                raise ProtocolError("MCP request ID is ambiguous or capacity exceeded")
            job = asyncio.create_task(self._process(copy.deepcopy(wire), key, copy.deepcopy(arguments)))
            self._pending[key] = job
            self._jobs.add(job)
            job.add_done_callback(self._done)

    async def _process(self, wire: dict[str, Any], key: tuple[type, Any], arguments: dict[str, Any]) -> None:
        try:
            replacement = await self._transform(wire["params"]["name"], arguments)
        except asyncio.CancelledError:
            raise
        except McpInputDeniedError as exc:
            await self._reject(key, wire["id"], exc.reason or "MCP tool call denied", -32000)
            return
        except Exception:
            await self._reject(key, wire["id"], "SDK MCP input transform failed", -32603)
            return
        async with self._backend_lock:
            async with self._lock:
                if self._pending.get(key) is not asyncio.current_task():
                    return
                if not self._enabled():
                    self._pending.pop(key, None)
                    await self._send({"jsonrpc": "2.0", "id": wire["id"],
                                      "error": {"code": -32800, "message": "MCP server tools disabled"}})
                    return
                wire["params"]["arguments"] = replacement
                if not await self._tracker.request(wire):
                    self._pending.pop(key, None)
                    return
                if self._pending.get(key) is not asyncio.current_task():
                    await self._tracker.discard(wire["id"])
                    return
                if not self._enabled():
                    self._pending.pop(key, None)
                    await self._tracker.discard(wire["id"])
                    await self._send({"jsonrpc": "2.0", "id": wire["id"],
                                      "error": {"code": -32800, "message": "MCP server tools disabled"}})
                    return
                # The output tracker owns cancellation from this point. Do not
                # hold the input-gate lock while a transport queue is blocked:
                # SDK cancellation must still reach already admitted calls.
                self._pending.pop(key, None)
            await self._backend(wire)

    async def _reject(self, key: tuple[type, Any], ident: Any, message: str, code: int) -> None:
        async with self._lock:
            if self._pending.get(key) is not asyncio.current_task():
                return
            await self._send({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}})
            self._pending.pop(key, None)

    def _done(self, task: asyncio.Task) -> None:
        self._jobs.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._fail()

    async def cancel(self, ident: Any) -> bool:
        key = _request_key(ident)
        async with self._lock:
            task = self._pending.pop(key, None)
            if task is None:
                return False
            task.cancel()
            await self._send({"jsonrpc": "2.0", "id": ident,
                              "error": {"code": -32800, "message": "MCP request cancelled locally"}})
            return True

    async def cancel_notification(self, wire: dict[str, Any]) -> bool:
        params = wire.get("params")
        if (wire.get("jsonrpc") != "2.0" or "id" in wire or
                "result" in wire or "error" in wire or not isinstance(params, dict) or
                _request_key(params.get("requestId")) is None):
            raise ProtocolError("Invalid MCP cancellation")
        return await self.cancel(params["requestId"])

    async def tool_ids(self) -> list[Any]:
        async with self._lock:
            return [key[1] for key in self._pending]

    def discard_pending(self) -> None:
        self._pending.clear()
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
