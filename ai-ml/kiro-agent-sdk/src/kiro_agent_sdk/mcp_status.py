"""Track Kiro MCP status snapshots and V2 per-server notifications."""
from __future__ import annotations

import copy
from typing import Any, Optional


class McpStatusTracker:
    def __init__(self) -> None:
        self._snapshots: dict[Optional[str], dict[str, Any]] = {}
        self._events: dict[Optional[str], dict[str, dict[str, Any]]] = {}
        self._last: dict[Optional[str], dict[str, Any]] = {}

    def update(self, method: str, params: Any) -> None:
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        if session_id is not None and not isinstance(session_id, str):
            return
        scope = session_id or None
        if method == "_kiro/mcp/status":
            if not isinstance(params.get("servers"), list):
                return
            self._snapshots[scope] = copy.deepcopy(params)
            self._last[scope] = copy.deepcopy(params)
            return
        statuses = {"_kiro.dev/mcp/server_initialized": "connected",
                    "_kiro.dev/mcp/server_init_failure": "failed",
                    "_kiro.dev/mcp/oauth_request": "needs-auth"}
        status = statuses.get(method)
        name = params.get("serverName")
        if status is None or not isinstance(name, str) or not name:
            return
        server: dict[str, Any] = {"name": name, "status": status}
        error = params.get("error", params.get("errorMessage"))
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str):
            server["error"] = error
        self._events.setdefault(scope, {})[name] = server
        if scope in self._snapshots:
            snapshot = self._snapshots[scope]
            servers = snapshot["servers"]
            current = next((item for item in servers
                            if isinstance(item, dict) and item.get("name") == name), None)
            if current is None:
                current = {"name": name}
                servers.append(current)
            current.pop("error", None)
            current.pop("errorMessage", None)
            current.pop("failedAuthorization", None)
            current["status"] = "failed" if status == "needs-auth" else status
            if status == "needs-auth":
                current["failedAuthorization"] = True
            if status != "connected":
                current.pop("tools", None)
            if isinstance(error, str):
                current["errorMessage"] = error
        self._last[scope] = copy.deepcopy(params)

    def get(self, session_id: Optional[str]) -> Optional[dict[str, Any]]:
        scope = session_id if session_id in self._last else None
        if scope not in self._last:
            return None
        result = copy.deepcopy(self._last[scope])
        if scope in self._snapshots:
            entries = self._snapshots[scope]["servers"]
            servers = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    continue
                server = copy.deepcopy(entry)
                original = server.get("status")
                if original == "failed" and server.get("failedAuthorization") is True:
                    status = "needs-auth"
                elif original == "connecting":
                    status = "pending"
                elif original in ("connected", "failed", "disabled"):
                    status = original
                else:
                    status = "unknown"
                server["status"] = status
                if isinstance(original, str) and original != status:
                    server["kiroStatus"] = original
                if "tools" in server:
                    tools = server["tools"]
                    if isinstance(tools, list):
                        server["tools"] = [tool for tool in tools
                                           if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
                    else:
                        del server["tools"]
                if "serverInfo" in server:
                    info = server["serverInfo"]
                    if not (isinstance(info, dict) and isinstance(info.get("name"), str) and
                            isinstance(info.get("version"), str)):
                        del server["serverInfo"]
                if isinstance(server.get("errorMessage"), str):
                    server["error"] = server["errorMessage"]
                elif not isinstance(server.get("error"), str):
                    server.pop("error", None)
                servers.append(server)
        else:
            servers = copy.deepcopy(list(self._events.get(scope, {}).values()))
        result["mcpServers"] = servers
        return result

    def raw(self, session_id: Optional[str]) -> Optional[dict[str, Any]]:
        scope = session_id if session_id in self._last else None
        value = self._last.get(scope)
        return copy.deepcopy(value) if value is not None else None
