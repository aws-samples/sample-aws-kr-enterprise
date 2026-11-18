"""Load MCP server configuration given as a mapping, a JSON file or a JSON string with an mcpServers object."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Union

from .process_env import child_process_env


_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^{}]*))?\}")


def _expand(value: str, environment: dict[str, str]) -> str:
    pieces = []
    position = 0
    for match in _VARIABLE.finditer(value):
        prefix = value[position:match.start()]
        if "${" in prefix:
            raise ValueError("Unsupported MCP environment variable expression")
        pieces.append(prefix)
        name, default = match.groups()
        resolved = environment.get(name)
        if resolved is None or (resolved == "" and default is not None):
            if default is None:
                raise ValueError(f"MCP configuration environment variable {name!r} is not set")
            resolved = default
        if not isinstance(resolved, str):
            raise ValueError(f"MCP configuration environment variable {name!r} must be a string")
        pieces.append(resolved)
        position = match.end()
    suffix = value[position:]
    if "${" in suffix:
        raise ValueError("Unsupported MCP environment variable expression")
    pieces.append(suffix)
    return "".join(pieces)


def _parse_json(text: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate MCP configuration key: {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"Invalid MCP JSON constant: {value}")

    return json.loads(text, object_pairs_hook=unique, parse_constant=reject_constant)


def load_mcp_config(
    config: Union[dict[str, dict[str, Any]], str, Path],
    cwd: Union[str, Path, None],
    extra_env: dict[str, str],
    *,
    expand_programmatic: bool = False,
    local_process: bool = True,
) -> dict[str, dict[str, Any]]:
    """Snapshot explicit configs; JSON files contain external servers only."""
    if isinstance(config, dict):
        servers = config
        from_json = False
    elif isinstance(config, (str, Path)):
        if config == "":
            return {}
        if isinstance(config, str) and config.lstrip().startswith(("{", "[")):
            text = config
        else:
            path = Path(config)
            if not path.is_absolute():
                path = Path(cwd or os.getcwd()) / path
            text = path.read_text(encoding="utf-8")
        parsed = _parse_json(text)
        if not isinstance(parsed, dict) or set(parsed) != {"mcpServers"} or not isinstance(parsed["mcpServers"], dict):
            raise ValueError("MCP JSON must contain only an mcpServers object")
        servers = parsed["mcpServers"]
        from_json = True
    else:
        raise TypeError("mcp_servers must be a server dictionary, JSON string, or path")

    result = {}
    environment = child_process_env(cwd if local_process else None, extra_env)
    for name, raw in servers.items():
        if not isinstance(name, str) or not name.strip() or "\0" in name or not isinstance(raw, dict):
            raise ValueError("MCP servers must have nonempty names and configuration objects")
        server = dict(raw)
        if from_json:
            transport = server.get("type", "stdio")
            if transport == "streamable-http":
                transport = server["type"] = "http"
            if transport not in ("stdio", "http", "sse"):
                raise ValueError(f"Unsupported MCP JSON transport for server {name!r}")
            allowed = {"type", "command", "args", "env"} if transport == "stdio" else {"type", "url", "headers"}
            if set(server) - allowed:
                raise ValueError(f"Unsupported MCP JSON fields for server {name!r}: {sorted(set(server) - allowed)}")
            required = "command" if transport == "stdio" else "url"
            if not isinstance(server.get(required), str) or not server[required]:
                raise ValueError(f"MCP server {name!r} requires a nonempty {required}")
        if from_json or (expand_programmatic and server.get("type") != "sdk"):
            for key in ("command", "url"):
                if key in server:
                    if not isinstance(server[key], str):
                        raise ValueError(f"MCP server {name!r} {key} must be a string")
                    server[key] = _expand(server[key], environment)
            if "args" in server:
                if not isinstance(server["args"], list) or any(not isinstance(item, str) for item in server["args"]):
                    raise ValueError(f"MCP server {name!r} args must be a string list")
                server["args"] = [_expand(item, environment) for item in server["args"]]
            for key in ("env", "headers"):
                if key in server:
                    values = server[key]
                    if isinstance(values, dict) and all(isinstance(k, str) and isinstance(v, str)
                                                        for k, v in values.items()):
                        server[key] = {k: _expand(value, environment) for k, value in values.items()}
                    elif (not from_json and isinstance(values, list) and all(
                        isinstance(item, dict) and set(item) == {"name", "value"} and
                        isinstance(item["name"], str) and isinstance(item["value"], str) for item in values
                    )):
                        server[key] = [{"name": item["name"], "value": _expand(item["value"], environment)}
                                       for item in values]
                    else:
                        raise ValueError(f"MCP server {name!r} {key} must be a string map or ACP entries")
        result[name] = server
    return result
