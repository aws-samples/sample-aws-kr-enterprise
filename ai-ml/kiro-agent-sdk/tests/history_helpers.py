"""Helpers for asserting on session history messages in tests."""
from kiro_agent_sdk import AgentMessage, TextBlock, ToolCall, UserMessage


def role(message):
    if isinstance(message, UserMessage):
        return "user"
    if isinstance(message, AgentMessage):
        return "agent"
    if isinstance(message, ToolCall):
        return "tool"
    raise TypeError(type(message).__name__)


def blocks(message):
    """Message content as ACP dictionaries, for comparing with recorded updates."""
    return [{"type": "text", "text": block.text} if isinstance(block, TextBlock) else block
            for block in message.content]
