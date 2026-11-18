"""Build the message objects shared by live responses and session history."""
from __future__ import annotations

import copy
from typing import Any, Optional

from .types import AgentMessage, TextBlock, ToolCall, UserMessage


def content_blocks(blocks: Any) -> list[Any]:
    """Map ACP text blocks to ``TextBlock`` and keep other ACP blocks as dictionaries."""
    if isinstance(blocks, (str, dict)):
        blocks = [blocks]
    result: list[Any] = []
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, str):
            result.append(TextBlock(block))
        elif isinstance(block, dict) and set(block) == {"type", "text"} and block["type"] == "text" and \
                isinstance(block["text"], str):
            result.append(TextBlock(block["text"]))
        elif isinstance(block, dict):
            result.append(copy.deepcopy(block))
    return result


def history_message(role: str, message_id: Optional[str], session_id: Optional[str], blocks: Any, *,
                    agent_id: Optional[str] = None, parent_tool_call_id: Optional[str] = None) -> Any:
    """A ``UserMessage`` or ``AgentMessage`` read back from a Kiro session."""
    cls = UserMessage if role == "user" else AgentMessage
    return cls(content_blocks(blocks), message_id=message_id, session_id=session_id, agent_id=agent_id,
               parent_tool_call_id=parent_tool_call_id)


def tool_call_message(tool_id: Optional[str], merged: dict[str, Any], session_id: Optional[str],
                      agent_id: Optional[str] = None, parent_tool_call_id: Optional[str] = None) -> ToolCall:
    """Snapshot an ACP tool call merged with its updates."""
    def listed(key: str) -> Optional[list[Any]]:
        return copy.deepcopy(merged[key]) if isinstance(merged.get(key), list) else None
    title, status, kind = merged.get("title"), merged.get("status"), merged.get("kind")
    meta = merged.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    return ToolCall(tool_id or "", title if isinstance(title, str) else "",
                    status=status if isinstance(status, str) else None, kind=kind if isinstance(kind, str) else None,
                    raw_input=copy.deepcopy(merged["rawInput"]) if isinstance(merged.get("rawInput"), dict) else None,
                    raw_output=copy.deepcopy(merged.get("rawOutput")), content=listed("content"),
                    locations=listed("locations"), session_id=session_id, agent_id=agent_id,
                    parent_tool_call_id=parent_tool_call_id,
                    kiro_metadata=copy.deepcopy(kiro) if isinstance(kiro, dict) else None)
