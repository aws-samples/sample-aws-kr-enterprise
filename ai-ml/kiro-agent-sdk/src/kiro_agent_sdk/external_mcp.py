"""Opt-in local stdio MCP relay for per-server tool-call suspension."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

from .process_env import child_process_env
from .mcp_cancellation import ObservedToolCalls, cancellation_notice, parse_message
from .mcp_output_tracker import McpOutputTracker
from .mcp_input_gate import McpInputGate
from .errors import CLIConnectionError, ProtocolError

_MCP_CLEANUP_TIMEOUT = 3.0
_MCP_PROCESS_CLEANUP_TIMEOUT = 7.0
_MCP_BRIDGE_CLOSE_TIMEOUT = 12.0


class _ToolFilterBridge:
    def __init__(self, on_cleanup_failure: Optional[Callable[[], None]] = None) -> None:
        self.token = secrets.token_urlsafe(32)
        self.server: Optional[asyncio.AbstractServer] = None
        self.connections: set[asyncio.Task[Any]] = set()
        self._cleaning: set[asyncio.Task[Any]] = set()
        self._cancelling: set[asyncio.Task[Any]] = set()
        self._tools_enabled = True
        self._tools_suspended = False
        self._closed = False
        self._cleanup_failed = False
        self._on_cleanup_failure = on_cleanup_failure
        self._tool_cancellers: set[Callable[[], Awaitable[None]]] = set()

    async def start(self) -> dict[str, Any]:
        self.server = await asyncio.start_server(self._connected, "127.0.0.1", 0, limit=16 * 1024 * 1024)
        port = self.server.sockets[0].getsockname()[1]
        return {"command": sys.executable, "args": [str(Path(__file__).with_name("_mcp_bridge.py"))],
                "env": [{"name": "KIRO_SDK_MCP_PORT", "value": str(port)},
                        {"name": "KIRO_SDK_MCP_TOKEN", "value": self.token}]}

    def set_tools_enabled(self, enabled: bool) -> None:
        if enabled and self._cleanup_failed:
            raise CLIConnectionError("External stdio MCP cleanup failed; reconnect the client")
        self._tools_enabled = enabled

    def suspend_tools(self) -> None:
        self._tools_suspended = True

    def resume_tools(self) -> None:
        self._tools_suspended = False

    def _mark_cleanup_failed(self) -> None:
        first_failure = not self._cleanup_failed
        self._cleanup_failed = True
        self._tools_enabled = False
        self._closed = True
        if self.server is not None:
            self.server.close()
        if first_failure and self._on_cleanup_failure is not None:
            with contextlib.suppress(Exception):
                self._on_cleanup_failure()

    async def _settle_cleanup(self, tasks: list[asyncio.Task], *, timeout: float,
                              cancel_pending: bool, fail_on_exception: bool) -> None:
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        failed = False
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                failed = True
        if fail_on_exception and failed:
            self._mark_cleanup_failed()
        if pending:
            self._mark_cleanup_failed()
            from .client import _track_process_cleanup
            for task in pending:
                if cancel_pending:
                    task.cancel()
                _track_process_cleanup(task)

    async def request_tool_cancellation(self) -> None:
        """Send best-effort cancellation notices for observed outstanding tool calls."""
        for cancel in list(self._tool_cancellers):
            await cancel()

    @staticmethod
    def _classify(line: bytes) -> tuple[bool, bool, Any]:
        """Fail closed on malformed or ambiguous JSON while tools are disabled."""
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        try:
            message = json.loads(line, object_pairs_hook=unique,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return True, True, None
        if not isinstance(message, dict):
            # Batch requests are not forwarded while disabled: one could contain a tool call.
            return True, True, None
        if message.get("method") != "tools/call":
            return False, False, None
        if "id" not in message:
            return True, False, None
        ident = message.get("id")
        if not isinstance(ident, (str, int)) or isinstance(ident, bool):
            return True, True, None
        return True, True, ident

    async def close(self) -> None:
        self._closed = True
        if self.server is not None:
            self.server.close()
        connections = list(self.connections)
        for task in connections:
            if task not in self._cleaning and task not in self._cancelling:
                self._cancelling.add(task)
                task.cancel()
        if connections:
            try:
                done, pending = await asyncio.wait(connections, timeout=_MCP_BRIDGE_CLOSE_TIMEOUT)
            except BaseException:
                self._mark_cleanup_failed()
                from .client import _track_process_cleanup
                for task in connections:
                    if not task.done():
                        _track_process_cleanup(task)
                raise
            failed = False
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    failed = True
            if failed:
                self._mark_cleanup_failed()
            if pending:
                self._mark_cleanup_failed()
                from .client import _track_process_cleanup
                for task in pending:
                    _track_process_cleanup(task)
        if self.server is not None:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 3)
            except asyncio.TimeoutError:
                self._cleanup_failed = True
        if self._cleanup_failed:
            raise CLIConnectionError("MCP bridge cleanup failed")


class ExternalStdioMcpBridge(_ToolFilterBridge):
    """Relay a local stdio server and block new tool calls while disabled."""

    def __init__(self, config: dict[str, Any], cwd: Union[str, Path, None], env: dict[str, str],
                 spawn: Callable[..., Awaitable[asyncio.subprocess.Process]], *,
                 output_transform: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
                 input_transform: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
                 on_cleanup_failure: Optional[Callable[[], None]] = None) -> None:
        if on_cleanup_failure is not None and not callable(on_cleanup_failure):
            raise ValueError("on_cleanup_failure must be callable")
        super().__init__(on_cleanup_failure)
        if set(config) - {"type", "command", "args", "env"} or config.get("type", "stdio") != "stdio":
            raise ValueError("Unsupported external stdio MCP bridge configuration")
        command = config.get("command")
        args = config.get("args", [])
        overrides = config.get("env", {})
        if (not isinstance(command, str) or not command.strip() or "\0" in command or
                not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args)):
            raise ValueError("Invalid external stdio MCP command")
        if isinstance(overrides, list):
            converted: dict[str, str] = {}
            for item in overrides:
                if not isinstance(item, dict) or set(item) != {"name", "value"}:
                    raise ValueError("Invalid external stdio MCP environment")
                key, value = item["name"], item["value"]
                if not isinstance(key, str) or key in converted:
                    raise ValueError("Invalid external stdio MCP environment")
                converted[key] = value
            overrides = converted
        if (not isinstance(overrides, dict) or any(
            not isinstance(key, str) or not key or "=" in key or "\0" in key or
            not isinstance(value, str) or "\0" in value for key, value in overrides.items()
        )):
            raise ValueError("Invalid external stdio MCP environment")
        self.command, self.args = command, list(args)
        self.cwd = str(cwd) if cwd is not None else None
        self.env = {**child_process_env(cwd, env), **overrides}
        self._spawn = spawn
        self.output_transform = output_transform
        self.input_transform = input_transform

    async def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        owner = asyncio.current_task()
        assert owner is not None
        self.connections.add(owner)
        process: Optional[asyncio.subprocess.Process] = None
        connection_open = True
        pumps: list[asyncio.Task[Any]] = []
        cancel_tools = None
        tracker = None
        gate = None
        try:
            if self._closed:
                return
            token = await asyncio.wait_for(reader.readline(), 5)
            if self._closed or not secrets.compare_digest(token.decode().strip(), self.token):
                return
            process = await self._spawn(self.command, *self.args, cwd=self.cwd, env=self.env,
                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024,
                                        start_new_session=(os.name == "posix"))
            assert process.stdin is not None and process.stdout is not None
            write_lock = asyncio.Lock()
            state_lock = asyncio.Lock()
            backend_lock = asyncio.Lock()
            observed = ObservedToolCalls()

            async def send_client(data: bytes) -> None:
                async with write_lock:
                    if not connection_open or self._closed:
                        raise CLIConnectionError("External MCP connection is closing")
                    writer.write(data)
                    await writer.drain()

            async def send_wire(wire: dict[str, Any]) -> None:
                await send_client((json.dumps(wire, ensure_ascii=False, allow_nan=False) + "\n").encode())

            async def send_backend(wire: dict[str, Any]) -> None:
                if not connection_open or self._closed:
                    raise CLIConnectionError("External MCP connection is closing")
                process.stdin.write((json.dumps(wire, ensure_ascii=False, allow_nan=False) + "\n").encode())
                await process.stdin.drain()

            def fail() -> None:
                if owner not in self._cancelling and owner not in self._cleaning:
                    self._cancelling.add(owner)
                    owner.cancel()

            if self.output_transform is not None or self.input_transform is not None:
                tracker = McpOutputTracker(self.output_transform, send_wire, fail)
            if self.input_transform is not None:
                gate = McpInputGate(self.input_transform, tracker, send_wire, send_backend, backend_lock,
                                    lambda: connection_open and not self._closed and self._tools_enabled
                                            and not self._tools_suspended, fail)

            async def from_client() -> None:
                while line := await reader.readline():
                    async with backend_lock:
                        if not self._tools_enabled or self._tools_suspended:
                            blocked, reply, ident = self._classify(line)
                            if blocked:
                                if reply:
                                    response = {"jsonrpc": "2.0", "id": ident,
                                                "error": {"code": -32800, "message": "MCP server tools disabled"}}
                                    await send_client((json.dumps(response, ensure_ascii=False) + "\n").encode())
                                continue
                        async with state_lock:
                            message = parse_message(line)
                            if tracker is not None:
                                if message is None:
                                    raise ProtocolError("Invalid MCP request")
                                if await tracker.reject_cancelled_id(message):
                                    continue
                                if gate is not None:
                                    if message.get("method") == "notifications/cancelled":
                                        if await gate.cancel_notification(message):
                                            continue
                                    await gate.check_available(message)
                                    if message.get("method") == "tools/call":
                                        await gate.submit(message)
                                        continue
                                if not await tracker.request(message):
                                    continue
                            elif message is None:
                                observed.invalidate()
                            else:
                                if not observed.request(message):
                                    await send_client((json.dumps({"jsonrpc": "2.0", "id": message.get("id"),
                                        "error": {"code": -32600, "message": "MCP request ID cannot be reused"}}) + "\n").encode())
                                    continue
                        if not connection_open or self._closed:
                            return
                        process.stdin.write(line)
                        await process.stdin.drain()

            async def from_server() -> None:
                while line := await process.stdout.readline():
                    async with state_lock:
                        message = parse_message(line)
                        if tracker is not None:
                            if message is None:
                                raise ProtocolError("Invalid MCP response")
                            await tracker.response(message)
                        elif message is None:
                            observed.invalidate()
                        else:
                            if not observed.response(message):
                                continue
                    if tracker is None:
                        await send_client(line)
                if tracker is not None:
                    await tracker.drain()

            async def cancel_observed_tools() -> None:
                async with backend_lock:
                    if gate is not None:
                        for ident in await gate.tool_ids():
                            await gate.cancel(ident)
                    async with state_lock:
                        identifiers = await tracker.tool_ids() if tracker is not None else observed.ids()
                    for ident in identifiers:
                        if tracker is not None:
                            await tracker.cancel(ident)
                        else:
                            async with state_lock:
                                suppressed = observed.suppress(ident)
                            if suppressed:
                                await send_wire({"jsonrpc": "2.0", "id": ident,
                                    "error": {"code": -32800, "message": "MCP request cancelled locally"}})
                        if not connection_open or self._closed:
                            return
                        process.stdin.write((json.dumps(cancellation_notice(ident), ensure_ascii=False) + "\n").encode())
                    await process.stdin.drain()

            cancel_tools = cancel_observed_tools
            self._tool_cancellers.add(cancel_tools)
            pumps = [asyncio.create_task(from_client()), asyncio.create_task(from_server())]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionError, OSError, UnicodeError, asyncio.TimeoutError, asyncio.CancelledError, ValueError, ProtocolError):
            pass
        finally:
            connection_open = False
            self._cleaning.add(owner)
            if cancel_tools is not None:
                self._tool_cancellers.discard(cancel_tools)
            if gate is not None:
                gate.discard_pending()
            if tracker is not None:
                tracker.discard_pending()
            cleanup_tasks: list[asyncio.Task] = list(pumps)
            try:
                termination = None
                if process is not None:
                    # Loaded at cleanup time to avoid the client/bridge import cycle.
                    from .client import _terminate_owned_process, _track_process_cleanup
                    termination = asyncio.create_task(_terminate_owned_process(process, signal_exited_group=True))
                    cleanup_tasks.append(termination)
                    _track_process_cleanup(termination)
                for task in pumps:
                    if not task.done():
                        task.cancel()
                await self._settle_cleanup(pumps, timeout=_MCP_CLEANUP_TIMEOUT,
                                           cancel_pending=False, fail_on_exception=False)
                auxiliary = []
                if gate is not None:
                    auxiliary.append(asyncio.create_task(gate.close()))
                if tracker is not None:
                    auxiliary.append(asyncio.create_task(tracker.close()))
                cleanup_tasks.extend(auxiliary)
                await self._settle_cleanup(auxiliary, timeout=_MCP_CLEANUP_TIMEOUT,
                                           cancel_pending=True, fail_on_exception=True)
                if termination is not None:
                    await self._settle_cleanup([termination], timeout=_MCP_PROCESS_CLEANUP_TIMEOUT,
                                               cancel_pending=True, fail_on_exception=True)
            except asyncio.CancelledError:
                self._mark_cleanup_failed()
                raise
            except Exception:
                self._mark_cleanup_failed()
            finally:
                from .client import _track_process_cleanup
                for task in cleanup_tasks:
                    if not task.done():
                        _track_process_cleanup(task)
                try:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), _MCP_CLEANUP_TIMEOUT)
                    except asyncio.TimeoutError:
                        writer.transport.abort()
                        self._mark_cleanup_failed()
                    except Exception:
                        pass
                finally:
                    self.connections.discard(owner)
                    self._cleaning.discard(owner)
                    self._cancelling.discard(owner)
