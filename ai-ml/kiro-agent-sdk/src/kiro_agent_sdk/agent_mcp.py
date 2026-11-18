"""Validate and snapshot SDK servers declared inside agent definitions."""
from __future__ import annotations

import json
import re
from typing import Any

from .mcp import SdkMcpTool, _annotations, _schema
from .types import AgentDefinition


def inline_sdk_servers(options: Any) -> tuple[Any, dict[str, tuple[str, str, dict[str, Any]]]]:
    mode = 'reject'
    if mode not in ("reject", "sdk"):
        raise ValueError("agent_inline_mcp_mode must be reject or sdk")
    if mode == "reject":
        return ("reject",), {}
    if options.agent_engine != "v3":
        raise ValueError("Inline SDK agent servers require Kiro V3")
    definitions = options.agents or {}
    if not isinstance(definitions, dict):
        raise ValueError("Inline SDK agent servers require agent definitions")
    signatures = []
    servers: dict[str, tuple[str, str, dict[str, Any]]] = {}
    total_bytes = 0
    for agent, definition in definitions.items():
        if (not isinstance(agent, str) or len(agent) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", agent) or
                agent in {"default", "plan", "vibe", "spec", "kiro_default", "kiro_planner"} or
                not isinstance(definition, AgentDefinition)):
            raise ValueError("Invalid inline SDK agent definition")
        entries = definition.mcp_servers
        if entries is None:
            continue
        if not isinstance(entries, list) or len(entries) > 128:
            raise ValueError("Agent MCP servers must be a bounded list")
        names = set()
        for entry in entries:
            if isinstance(entry, str):
                server, config = entry, None
            elif isinstance(entry, dict) and len(entry) == 1:
                server, config = next(iter(entry.items()))
            else:
                raise ValueError("Agent MCP entries must name one server")
            if (not isinstance(server, str) or not server.strip() or len(server) > 128 or
                    any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in server) or server in names):
                raise ValueError("Invalid or duplicate inline SDK server name")
            names.add(server)
            if not isinstance(config, dict) or config.get("type") != "sdk":
                continue
            if options.strict_mcp_config:
                raise ValueError("strict_mcp_config excludes agent profiles; configure this SDK server in mcp_servers instead")
            key = f"agent:{agent}:{server}"
            if len(key) > 256 or len(servers) >= 128:
                raise ValueError("Inline SDK agent servers exceed limits")
            if set(config) - {"type", "name", "version", "tools", "instance"}:
                raise ValueError("Unsupported inline SDK server field")
            name, version = config.get("name", server), config.get("version", "1.0.0")
            if any(not isinstance(value, str) or not value.strip() or len(value) > 128 or
                   any(ord(c) < 32 for c in value) for value in (name, version)):
                raise ValueError("Invalid inline SDK server name or version")
            instance = config.get("instance")
            tools = config.get("tools", [])
            if not isinstance(tools, list) or len(tools) > 128:
                raise ValueError("Inline SDK tools must be a bounded list")
            if instance is not None and (tools or not callable(getattr(instance, "run", None)) or
                                         not callable(getattr(instance, "create_initialization_options", None))):
                raise ValueError("Inline SDK instance must be an MCP Server without a separate tools list")
            if instance is not None and options.sdk_tool_interrupt_mode == "cancel-all":
                raise ValueError("SDK tool cancellation does not support MCP Server instances")
            tool_names = set()
            frozen_tools = []
            tool_signatures = []
            for tool in tools:
                if (not isinstance(tool, SdkMcpTool) or not isinstance(tool.name, str) or not tool.name.strip() or
                        len(tool.name) > 128 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in tool.name) or
                        tool.name in tool_names or not callable(tool.handler) or
                        not isinstance(tool.description, str)):
                    raise ValueError("Invalid or duplicate inline SDK tool")
                tool_names.add(tool.name)
                encoded = json.dumps({"schema": _schema(tool.input_schema),
                                      "annotations": _annotations(tool.annotations) if tool.annotations is not None else None,
                                      "description": tool.description}, sort_keys=True, allow_nan=False)
                total_bytes += len(encoded.encode())
                if total_bytes > 1024 * 1024:
                    raise ValueError("Inline SDK tool definitions exceed 1 MiB")
                data = json.loads(encoded)
                # Freeze callable identity/schema without copying a Server's
                # locks, streams, lifespan manager or application state.
                frozen_tools.append(SdkMcpTool(tool.name, data["description"], data["schema"],
                                              tool.handler, data["annotations"]))
                tool_signatures.append((tool.name, id(tool.handler), encoded))
            signatures.append((agent, server, name, version, id(instance), tuple(tool_signatures)))
            servers[key] = (agent, server, {"type": "sdk", "name": name, "version": version,
                                          "tools": frozen_tools, **({"instance": instance} if instance is not None else {})})
    return ("sdk", tuple(signatures)), servers
