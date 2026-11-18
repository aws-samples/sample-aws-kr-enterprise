"""SDK-defined MCP tools and local authenticated stdio bridge."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import secrets
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal, Optional, Union, get_args, get_origin, get_type_hints

import jsonschema
from typing_extensions import Required, NotRequired
import typing

from .errors import McpInputDeniedError, ProtocolError
from .mcp_cancellation import ObservedToolCalls, _request_key, cancellation_notice, parse_message


class ToolAnnotations(dict):
    """MCP tool behavior hints, accepting snake_case and wire names."""
    _aliases = {
        "read_only_hint": "readOnlyHint",
        "destructive_hint": "destructiveHint",
        "idempotent_hint": "idempotentHint",
        "open_world_hint": "openWorldHint",
    }

    def __init__(self, **values: Any):
        for source, target in self._aliases.items():
            if source in values:
                value = values.pop(source)
                values.setdefault(target, value)
        for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            if key in values and values[key] is not None and not isinstance(values[key], bool):
                raise ValueError(f"{key} must be a boolean")
        if "title" in values and values["title"] is not None and not isinstance(values["title"], str):
            raise ValueError("title must be a string")
        super().__init__((key, value) for key, value in values.items() if value is not None)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self.get(self._aliases.get(name, name))


@dataclass
class SdkMcpTool:
    name: str
    description: str
    input_schema: Any
    handler: Callable
    annotations: Any = None


def tool(name: str, description: str, input_schema: Any, annotations: Any = None):
    def decorate(handler: Callable) -> SdkMcpTool:
        return SdkMcpTool(name, description, input_schema, handler, annotations)
    return decorate


def create_mcp_server(name: str, version: str = "1.0.0", tools: Any = None) -> dict[str, Any]:
    definitions = list(tools or [])
    if len({t.name for t in definitions}) != len(definitions):
        raise ValueError("Duplicate SDK MCP tool name")
    return {"type": "sdk", "name": name, "version": version, "tools": definitions}


def _schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and (isinstance(value.get("type"), (str, list)) or
                                    isinstance(value.get("$schema"), str) or
                                    isinstance(value.get("$ref"), str) or
                                    isinstance(value.get("properties"), dict) or
                                    any(isinstance(value.get(key), list) for key in ("anyOf", "allOf", "oneOf", "enum"))):
        return value
    if isinstance(value, type) and hasattr(value, "__annotations__"):
        fields = get_type_hints(value, include_extras=True)
        required = set(getattr(value, "__required_keys__", fields.keys()))
        for name, kind in fields.items():
            while get_origin(kind) is Annotated:
                kind = get_args(kind)[0]
            origin = get_origin(kind)
            if origin in _REQUIRED:
                required.add(name)
            elif origin in _NOT_REQUIRED:
                required.discard(name)
        required = [name for name in fields if name in required]
    elif isinstance(value, dict):
        fields, required = value, list(value)
    else:
        raise TypeError("Tool input schema must be a JSON schema, type mapping, or TypedDict")
    properties = {name: _type_schema(kind) for name, kind in fields.items()}
    return {"type": "object", "properties": properties, "required": required}


_REQUIRED = {Required, getattr(typing, "Required", Required)}
_NOT_REQUIRED = {NotRequired, getattr(typing, "NotRequired", NotRequired)}


def _type_schema(kind: Any) -> dict[str, Any]:
    mapping = {str: "string", int: "integer", float: "number", bool: "boolean",
               list: "array", dict: "object", type(None): "null"}
    if kind is Any:
        return {}
    if kind in mapping:
        return {"type": mapping[kind]}
    origin, args = get_origin(kind), get_args(kind)
    if origin in _REQUIRED | _NOT_REQUIRED and args:
        return _type_schema(args[0])
    if origin is Annotated:
        result = _type_schema(args[0])
        if len(args) > 1 and isinstance(args[1], str):
            result["description"] = args[1]
        return result
    if origin is Literal:
        return {"enum": list(args)}
    if origin is Union or origin is getattr(types, "UnionType", object()):
        return {"anyOf": [_type_schema(arg) for arg in args]}
    if origin is list:
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}
    if origin is dict:
        return {"type": "object", "additionalProperties": _type_schema(args[1]) if len(args) > 1 else {}}
    if isinstance(kind, type) and hasattr(kind, "__annotations__"):
        return _schema(kind)
    raise TypeError(f"Unsupported type {kind!r}; use JSON Schema")


def _annotations(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(ToolAnnotations(**copy.deepcopy(dict(value))))
    if callable(getattr(value, "model_dump", None)):
        result = value.model_dump(by_alias=True, mode="json", exclude_none=True)
        if isinstance(result, dict):
            return dict(ToolAnnotations(**result))
    raise TypeError("Tool annotations must be a dictionary or MCP ToolAnnotations object")


async def transform_mcp_result(callback: Optional[Callable], timeout: float, name: str,
                               arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if callback is None:
        return result
    task = None
    try:
        task = asyncio.ensure_future(callback(name, arguments, copy.deepcopy(result)))
        def consume(completed: asyncio.Future) -> None:
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(consume)
        done, _ = await asyncio.wait((task,), timeout=timeout)
        if not done:
            raise asyncio.TimeoutError("MCP output transform timed out")
        replacement = copy.deepcopy(task.result())
        if (not isinstance(replacement, dict) or not isinstance(replacement.get("content"), list) or
                any(not isinstance(item, dict) or not isinstance(item.get("type"), str)
                    for item in replacement["content"]) or "is_error" in replacement or
                ("isError" in replacement and not isinstance(replacement["isError"], bool)) or
                ("structuredContent" in replacement and not isinstance(replacement["structuredContent"], dict)) or
                ("_meta" in replacement and not isinstance(replacement["_meta"], dict)) or
                len(json.dumps(replacement, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8 * 1024 * 1024):
            raise ValueError("Invalid MCP output transform result")
        if result.get("isError"):
            # A transform may rewrite an error, for example to remove secrets, but not turn it into a success.
            replacement["isError"] = True
        return replacement
    except asyncio.CancelledError:
        raise
    except Exception:
        return {"content": [{"type": "text", "text": "SDK MCP output transform failed"}], "isError": True}
    finally:
        if task is not None and not task.done():
            task.cancel()


async def transform_mcp_arguments(callback: Optional[Callable], timeout: float, name: str,
                                  arguments: dict[str, Any]) -> dict[str, Any]:
    if callback is None:
        return arguments
    task = None
    try:
        task = asyncio.ensure_future(callback(name, copy.deepcopy(arguments)))
        def consume(completed: asyncio.Future) -> None:
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(consume)
        done, _ = await asyncio.wait((task,), timeout=timeout)
        if not done:
            raise asyncio.TimeoutError("MCP input transform timed out")
        if task.cancelled():
            raise ProtocolError("MCP input transform was cancelled")
        result = copy.deepcopy(task.result())
        def json_value(value: Any) -> bool:
            if isinstance(value, dict):
                return all(isinstance(key, str) and json_value(item) for key, item in value.items())
            if isinstance(value, list):
                return all(json_value(item) for item in value)
            return value is None or isinstance(value, (str, bool, int, float))
        if (not isinstance(result, dict) or not json_value(result) or
                len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8 * 1024 * 1024):
            raise ValueError("Invalid MCP input transform result")
        return result
    finally:
        if task is not None and not task.done():
            task.cancel()


class SdkMcpBridge:
    def __init__(self, config: dict[str, Any], *, output_transform: Optional[Callable] = None,
                 output_timeout: float = 30.0, input_transform: Optional[Callable] = None,
                 input_timeout: float = 30.0):
        self.config = config
        self.output_transform = output_transform
        self.output_timeout = output_timeout
        self.input_transform = input_transform
        self.input_timeout = input_timeout
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.connections: set[asyncio.Task] = set()
        self._tools_suspended = False
        self._tools_enabled = True
        self._tool_cancellers: set[Callable[[], Awaitable[None]]] = set()
        self._instance_tool_cancellers: set[Callable[[], Awaitable[None]]] = set()
        self._closing = False
        self.instance = config.get("instance")
        if self.instance is not None:
            try:
                import mcp  # noqa: F401
            except ImportError as exc:
                raise RuntimeError("An MCP server instance requires the optional mcp dependency") from exc
        self.tools = {t.name: (t, _schema(t.input_schema)) for t in config.get("tools", [])}
        self._tool_annotations: dict[str, dict[str, Any]] = {}
        for definition, schema in self.tools.values():
            jsonschema.Draft202012Validator.check_schema(schema)
            if definition.annotations is not None:
                self._tool_annotations[definition.name] = _annotations(definition.annotations)

    async def start(self) -> dict[str, Any]:
        self.server = await asyncio.start_server(self._connected, "127.0.0.1", 0, limit=16 * 1024 * 1024)
        port = self.server.sockets[0].getsockname()[1]
        return {"command": sys.executable, "args": [str(Path(__file__).with_name("_mcp_bridge.py"))],
                "env": [{"name": "KIRO_SDK_MCP_PORT", "value": str(port)},
                        {"name": "KIRO_SDK_MCP_TOKEN", "value": self.token}]}

    async def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            token = await asyncio.wait_for(reader.readline(), 5)
            if not secrets.compare_digest(token.decode().strip(), self.token):
                return
            if self.instance is not None:
                await self._serve_instance(reader, writer)
                return
            await self._serve_tools(reader, writer)
        except (ConnectionError, asyncio.TimeoutError, UnicodeError, asyncio.CancelledError, ProtocolError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.connections.discard(task)

    async def _serve_tools(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        pending: dict[Any, asyncio.Task] = {}
        all_tasks: set[asyncio.Task] = set()
        cancelled: set[asyncio.Task] = set()
        committed: set[asyncio.Task] = set()
        tool_tasks: set[asyncio.Task] = set()
        write_lock = asyncio.Lock()

        async def write(response: dict[str, Any], owner: Optional[asyncio.Task] = None) -> None:
            async with write_lock:
                if owner is not None:
                    if owner in cancelled:
                        return
                    committed.add(owner)
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                await writer.drain()

        async def respond(ident: Any, message: dict[str, Any]) -> None:
            task = asyncio.current_task()
            try:
                response = await self._dispatch(message, cancelled=lambda: task in cancelled)
            except asyncio.CancelledError:
                response = {"jsonrpc": "2.0", "id": ident,
                            "error": {"code": -32800, "message": "Request cancelled"}}
            except Exception as exc:
                response = {"jsonrpc": "2.0", "id": ident,
                            "error": {"code": -32603, "message": str(exc)}}
            try:
                json.dumps(response, allow_nan=False)
            except (TypeError, ValueError):
                response = {"jsonrpc": "2.0", "id": ident,
                            "error": {"code": -32603, "message": "Tool returned a non-JSON result"}}
            try:
                await write(response, task)
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                if pending.get(ident) is task:
                    pending.pop(ident, None)

        def finished(task: asyncio.Task) -> None:
            all_tasks.discard(task)
            cancelled.discard(task)
            committed.discard(task)
            tool_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        async def cancel_tools() -> None:
            for ident, task in list(pending.items()):
                if task in tool_tasks and task not in committed and pending.get(ident) is task:
                    pending.pop(ident, None)
                    cancelled.add(task)
                    task.cancel()
                    try:
                        await write({"jsonrpc": "2.0", "id": ident,
                                     "error": {"code": -32800, "message": "Request cancelled"}})
                    except ConnectionError:
                        pass

        self._tool_cancellers.add(cancel_tools)
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    await write({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "Invalid JSON"}})
                    continue
                if not isinstance(message, dict):
                    await write({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "Invalid request"}})
                    continue
                if "id" not in message:
                    if message.get("method") == "notifications/cancelled":
                        params = message.get("params")
                        ident = params.get("requestId") if isinstance(params, dict) else None
                        if isinstance(ident, (str, int)) and not isinstance(ident, bool) and ident in pending:
                            task = pending[ident]
                            if task not in committed:
                                pending.pop(ident, None)
                                cancelled.add(task)
                                task.cancel()
                                await write({"jsonrpc": "2.0", "id": ident,
                                             "error": {"code": -32800, "message": "Request cancelled"}})
                    continue
                ident = message["id"]
                if not isinstance(ident, (str, int)) or isinstance(ident, bool):
                    await write({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "Invalid request ID"}})
                    continue
                if ident in pending:
                    raise ProtocolError("Duplicate in-flight MCP request ID")
                if message.get("method") == "tools/call" and (self._tools_suspended or not self._tools_enabled):
                    await write({"jsonrpc": "2.0", "id": ident,
                                 "error": {"code": -32800, "message": "Tools are suspended"}})
                    continue
                task = asyncio.create_task(respond(ident, message))
                pending[ident] = task
                all_tasks.add(task)
                if message.get("method") == "tools/call":
                    tool_tasks.add(task)
                task.add_done_callback(finished)
        finally:
            self._tool_cancellers.discard(cancel_tools)
            tasks = list(all_tasks)
            cancelled.update(tasks)
            for pending_task in tasks:
                pending_task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=3)

    def suspend_tools(self) -> None:
        self._tools_suspended = True

    def resume_tools(self) -> None:
        self._tools_suspended = False

    def set_tools_enabled(self, enabled: bool) -> None:
        self._tools_enabled = enabled

    async def cancel_tools(self) -> None:
        if self.instance is not None:
            raise RuntimeError("Cancelling all tools is not supported for MCP Server instances")
        for cancel in list(self._tool_cancellers):
            await cancel()

    async def request_tool_cancellation(self) -> None:
        """Cancel SDK handlers or ask official MCP instances to cancel observed calls."""
        if self.instance is None:
            await self.cancel_tools()
        else:
            for cancel in list(self._instance_tool_cancellers):
                await cancel()

    async def _serve_instance(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve an official low-level MCP Server over its memory transport."""
        if self.output_transform is not None or self.input_transform is not None:
            await self._serve_transformed_instance(reader, writer)
            return
        import mcp.types
        from mcp.shared.memory import create_client_server_memory_streams
        from mcp.shared.message import SessionMessage

        async with create_client_server_memory_streams() as (client, server):
            from_server, to_server = client
            server_read, server_write = server
            state_lock = asyncio.Lock()
            backend_lock = asyncio.Lock()
            observed = ObservedToolCalls()

            def parse_wire(wire: Any) -> Any:
                if hasattr(mcp.types, "jsonrpc_message_adapter"):
                    return mcp.types.jsonrpc_message_adapter.validate_python(wire, by_name=False)
                return mcp.types.JSONRPCMessage.model_validate(wire)

            async def output() -> None:
                async for item in from_server:
                    wire = item.message.model_dump(by_alias=True, mode="json", exclude_none=True)
                    async with state_lock:
                        accepted = observed.response(wire)
                    if not accepted:
                        continue
                    writer.write((json.dumps(wire, ensure_ascii=False) + "\n").encode())
                    await writer.drain()

            async def cancel_observed_tools() -> None:
                async with backend_lock:
                    async with state_lock:
                        identifiers = observed.ids()
                    for ident in identifiers:
                        async with state_lock:
                            suppressed = observed.suppress(ident)
                        if suppressed:
                            writer.write((json.dumps({"jsonrpc": "2.0", "id": ident,
                                "error": {"code": -32800, "message": "MCP request cancelled locally"}}) + "\n").encode())
                            await writer.drain()
                        await to_server.send(SessionMessage(parse_wire(cancellation_notice(ident))))

            self._instance_tool_cancellers.add(cancel_observed_tools)
            serve = asyncio.create_task(self.instance.run(
                server_read, server_write, self.instance.create_initialization_options()))
            pump = asyncio.create_task(output())
            read = None
            try:
                while True:
                    read = asyncio.create_task(reader.readline())
                    done, _ = await asyncio.wait((read, serve, pump), return_when=asyncio.FIRST_COMPLETED)
                    if read not in done:
                        read.cancel()
                        await asyncio.gather(read, return_exceptions=True)
                        for task in (serve, pump):
                            if task.done():
                                await task
                        break
                    line = read.result()
                    if not line:
                        break
                    try:
                        wire = parse_message(line)
                        if (wire is None or
                                ("method" in wire and "id" in wire and _request_key(wire["id"]) is None)):
                            raise ValueError("Invalid MCP request")
                        parsed = parse_wire(wire)
                        normalized = parsed.model_dump(by_alias=True, mode="json", exclude_none=True)
                        if ("method" in wire and "id" in wire and
                                (not isinstance(normalized, dict) or
                                 _request_key(normalized.get("id")) != _request_key(wire["id"]))):
                            raise ValueError("Cannot correlate MCP request")
                    except (TypeError, ValueError, RecursionError, UnicodeError):
                        raise ProtocolError("Invalid MCP request") from None
                    async with backend_lock:
                        method = normalized.get("method") if isinstance(normalized, dict) else None
                        if isinstance(method, str) and not hasattr(mcp.types, "jsonrpc_message_adapter"):
                            try:
                                kind = mcp.types.ClientRequest if "id" in normalized else mcp.types.ClientNotification
                                kind.model_validate(normalized)
                            except (TypeError, ValueError):
                                if "id" in normalized:
                                    response = {"jsonrpc": "2.0", "id": normalized["id"],
                                                "error": {"code": -32601, "message": "Unsupported MCP request"}}
                                    writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                                    await writer.drain()
                                continue
                        if (method == "tools/call" and
                                (not self._tools_enabled or self._tools_suspended)):
                            ident = normalized.get("id")
                            if "id" in normalized:
                                if not isinstance(ident, (str, int)) or isinstance(ident, bool):
                                    ident = None
                                response = {"jsonrpc": "2.0", "id": ident,
                                            "error": {"code": -32800, "message": "MCP server tools disabled"}}
                                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                                await writer.drain()
                            continue
                        async with state_lock:
                            accepted = observed.request(normalized)
                        if not accepted:
                            writer.write((json.dumps({"jsonrpc": "2.0", "id": normalized.get("id"),
                                "error": {"code": -32600, "message": "MCP request ID cannot be reused"}}) + "\n").encode())
                            await writer.drain()
                            continue
                        await to_server.send(SessionMessage(parsed))
            finally:
                self._instance_tool_cancellers.discard(cancel_observed_tools)
                if read is not None and not read.done():
                    read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
                await to_server.aclose()
                for task in (serve, pump):
                    task.cancel()
                await asyncio.gather(serve, pump, return_exceptions=True)

    async def _serve_transformed_instance(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Mediate calls and correlate responses for an official MCP server."""
        import mcp.types
        from mcp.shared.memory import create_client_server_memory_streams
        from mcp.shared.message import SessionMessage
        from .mcp_input_gate import McpInputGate
        from .mcp_output_tracker import McpOutputTracker

        async with create_client_server_memory_streams() as (client, server):
            from_server, to_server = client
            server_read, server_write = server
            backend_lock, write_lock = asyncio.Lock(), asyncio.Lock()

            def parse_wire(wire: Any) -> Any:
                if hasattr(mcp.types, "jsonrpc_message_adapter"):
                    return mcp.types.jsonrpc_message_adapter.validate_python(wire, by_name=False)
                return mcp.types.JSONRPCMessage.model_validate(wire)

            async def send(wire: dict[str, Any]) -> None:
                async with write_lock:
                    writer.write((json.dumps(wire, ensure_ascii=False, allow_nan=False) + "\n").encode())
                    await writer.drain()

            async def send_backend(wire: dict[str, Any]) -> None:
                await to_server.send(SessionMessage(parse_wire(wire)))

            async def output_transform(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
                return await self._transform_output(name, arguments, result)

            async def input_transform(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return await transform_mcp_arguments(self.input_transform, self.input_timeout, name, arguments)

            def fail() -> None:
                pump.cancel()

            tracker = McpOutputTracker(output_transform if self.output_transform is not None else None, send, fail)
            gate = (McpInputGate(input_transform, tracker, send, send_backend, backend_lock,
                                 lambda: self._tools_enabled and not self._tools_suspended, fail)
                    if self.input_transform is not None else None)

            async def cancel_observed_tools() -> None:
                async with backend_lock:
                    if gate is not None:
                        for ident in await gate.tool_ids():
                            await gate.cancel(ident)
                    for ident in await tracker.tool_ids():
                        await tracker.cancel(ident)
                        await send_backend(cancellation_notice(ident))

            async def output() -> None:
                async for item in from_server:
                    wire = item.message.model_dump(by_alias=True, mode="json", exclude_none=True)
                    await tracker.response(wire)
                await tracker.drain()

            self._instance_tool_cancellers.add(cancel_observed_tools)
            serve = asyncio.create_task(self.instance.run(
                server_read, server_write, self.instance.create_initialization_options()))
            pump = asyncio.create_task(output())
            read = None
            try:
                while True:
                    read = asyncio.create_task(reader.readline())
                    done_tasks, _ = await asyncio.wait((read, serve, pump), return_when=asyncio.FIRST_COMPLETED)
                    if read not in done_tasks:
                        read.cancel()
                        await asyncio.gather(read, return_exceptions=True)
                        for task in (serve, pump):
                            if task.done():
                                await task
                        break
                    line = read.result()
                    if not line:
                        break
                    try:
                        raw = parse_message(line)
                        if raw is None or ("id" in raw and _request_key(raw["id"]) is None):
                            raise ValueError("Invalid MCP request")
                        parsed = parse_wire(raw)
                    except (TypeError, ValueError, RecursionError, UnicodeError):
                        raise ProtocolError("Invalid MCP request") from None
                    normalized = parsed.model_dump(by_alias=True, mode="json", exclude_none=True)
                    if ("id" in raw and (not isinstance(normalized, dict) or
                                          _request_key(normalized.get("id")) != _request_key(raw["id"]))):
                        raise ProtocolError("Cannot correlate MCP request")
                    async with backend_lock:
                        method = normalized.get("method") if isinstance(normalized, dict) else None
                        if isinstance(method, str) and not hasattr(mcp.types, "jsonrpc_message_adapter"):
                            try:
                                kind = mcp.types.ClientRequest if "id" in normalized else mcp.types.ClientNotification
                                kind.model_validate(normalized)
                            except (TypeError, ValueError):
                                if "id" in normalized:
                                    await send({"jsonrpc": "2.0", "id": normalized["id"],
                                                "error": {"code": -32601, "message": "Unsupported MCP request"}})
                                continue
                        if method == "tools/call" and (not self._tools_enabled or self._tools_suspended):
                            await send({"jsonrpc": "2.0", "id": normalized.get("id"),
                                        "error": {"code": -32800, "message": "MCP server tools disabled"}})
                            continue
                        if await tracker.reject_cancelled_id(normalized):
                            continue
                        if gate is not None:
                            if method == "notifications/cancelled":
                                if await gate.cancel_notification(normalized):
                                    continue
                            await gate.check_available(normalized)
                            if method == "tools/call":
                                await gate.submit(normalized)
                                continue
                        if not await tracker.request(normalized):
                            continue
                        await to_server.send(SessionMessage(parsed))
            finally:
                self._instance_tool_cancellers.discard(cancel_observed_tools)
                if read is not None and not read.done():
                    read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
                if gate is not None:
                    await gate.close()
                await tracker.close()
                await to_server.aclose()
                for task in (serve, pump):
                    task.cancel()
                await asyncio.gather(serve, pump, return_exceptions=True)

    async def _transform_output(self, name: str, arguments: dict[str, Any],
                                result: dict[str, Any]) -> dict[str, Any]:
        return await transform_mcp_result(self.output_transform, self.output_timeout, name, arguments, result)

    async def _dispatch(self, message: dict[str, Any], *,
                        cancelled: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
        ident, method = message["id"], message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": self.config["name"], "version": self.config["version"]}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": schema,
                                  **({"annotations": copy.deepcopy(self._tool_annotations[t.name])}
                                     if t.name in self._tool_annotations else {})}
                                 for t, schema in self.tools.values()]}
        elif method == "tools/call":
            if self._closing or (cancelled is not None and cancelled()):
                raise asyncio.CancelledError
            pair = self.tools.get(params.get("name"))
            if pair is None:
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Unknown tool"}}
            definition, schema = pair
            args = params.get("arguments")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return {"jsonrpc": "2.0", "id": ident,
                        "error": {"code": -32602, "message": "Invalid MCP tool arguments"}}
            if self.input_transform is not None:
                try:
                    args = await transform_mcp_arguments(self.input_transform, self.input_timeout,
                                                         definition.name, args)
                except asyncio.CancelledError:
                    raise
                except McpInputDeniedError as exc:
                    return {"jsonrpc": "2.0", "id": ident,
                            "error": {"code": -32000, "message": exc.reason or "MCP tool call denied"}}
                except Exception:
                    return {"jsonrpc": "2.0", "id": ident,
                            "error": {"code": -32603, "message": "SDK MCP input transform failed"}}
                if not self._tools_enabled or self._tools_suspended:
                    return {"jsonrpc": "2.0", "id": ident,
                            "error": {"code": -32800, "message": "Tools are suspended"}}
            try:
                try:
                    jsonschema.validate(args, schema)
                except jsonschema.ValidationError:
                    return {"jsonrpc": "2.0", "id": ident, "result": {
                        "content": [{"type": "text", "text": "Tool arguments do not match the input schema"}],
                        "isError": True}}
                original_args = copy.deepcopy(args) if self.output_transform is not None else None
                result = await definition.handler(args)
                if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                    raise ValueError("Tool result must contain a content list")
                result = dict(result)
                if "is_error" in result:
                    result["isError"] = result.pop("is_error")
            except Exception as exc:
                text = "SDK MCP tool handler failed" if self.input_transform is not None else str(exc)
                result = {"content": [{"type": "text", "text": text}], "isError": True}
            if self._closing or (cancelled is not None and cancelled()):
                raise asyncio.CancelledError
            if self.output_transform is not None:
                result = await self._transform_output(definition.name, original_args, result)
            if self._closing or (cancelled is not None and cancelled()):
                raise asyncio.CancelledError
        else:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    async def close(self) -> None:
        self._closing = True
        if self.server:
            self.server.close()
        connections = list(self.connections)
        for task in connections:
            task.cancel()
        if connections:
            await asyncio.wait(connections, timeout=3)
        if self.server:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.server.wait_closed(), 3)
