"""Synchronous wrappers for local Kiro session APIs.

Call these from ordinary synchronous code. Async applications should use the
corresponding top-level async functions instead.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Awaitable, Callable, TypeVar

from . import sessions, subagents


T = TypeVar("T")


def _synchronous(function: Callable[..., Awaitable[T]]) -> Callable[..., T]:
    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(function(*args, **kwargs))
        raise RuntimeError("Synchronous Kiro session functions cannot run inside an event loop; use the async API")

    return wrapped


list_sessions = _synchronous(sessions.list_sessions)
get_session_info = _synchronous(sessions.get_session_info)
get_session_history = _synchronous(sessions.get_session_history)
get_session_messages = _synchronous(sessions.get_session_messages)
list_models = _synchronous(sessions.list_models)
delete_session = _synchronous(sessions.delete_session)
rename_session = _synchronous(sessions.rename_session)
fork_session = _synchronous(sessions.fork_session)
list_subagents = _synchronous(subagents.list_subagents)
get_subagent_messages = _synchronous(subagents.get_subagent_messages)
get_subagent_history = _synchronous(subagents.get_subagent_history)

__all__ = [
    "list_sessions", "get_session_info", "get_session_history", "get_session_messages",
    "list_models", "delete_session", "rename_session", "fork_session",
    "list_subagents", "get_subagent_messages", "get_subagent_history",
]
