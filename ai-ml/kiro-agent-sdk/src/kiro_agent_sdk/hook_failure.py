"""Bounded hook execution for explicit failure policies."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any, Callable
from .errors import CLIConnectionError, ProtocolError


_mcp_warning_guard: ContextVar[Any] = ContextVar("kiro_mcp_warning_guard", default=None)


class _HookFailureRunner:
    def __init__(self) -> None:
        self.tasks: set[asyncio.Future[Any]] = set()
        self.cancel_requested: set[asyncio.Future[Any]] = set()
        self.exhausted = False
        self.closing = False
        self.cleanup_failed = False

    def reopen(self) -> None:
        if self.tasks or self.cleanup_failed:
            raise CLIConnectionError("Hook cleanup is incomplete; disconnect again")
        if self.exhausted:
            raise ProtocolError("Hook execution capacity exhausted; create a new client")
        self.closing = False

    def check(self) -> None:
        if self.cleanup_failed:
            raise CLIConnectionError("Hook cleanup is incomplete; disconnect again")
        if self.closing:
            raise ProtocolError("Hook execution is closed")
        if self.exhausted:
            raise ProtocolError("Hook execution capacity exhausted; create a new client")

    async def invoke(self, hook: Callable, payload: dict[str, Any], timeout: float,
                     tool_call_id: Any = None) -> Any:
        if len(self.tasks) >= 64:
            self.exhausted = True
        self.check()
        task = asyncio.ensure_future(hook(payload, tool_call_id, {}))
        self.tasks.add(task)
        def consume(done: asyncio.Future[Any]) -> None:
            self.tasks.discard(done)
            self.cancel_requested.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(consume)
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done:
                raise asyncio.TimeoutError("Hook timed out")
            if task.cancelled():
                raise RuntimeError("Hook was cancelled")
            return task.result()
        finally:
            if not task.done() and task not in self.cancel_requested:
                self.cancel_requested.add(task)
                task.cancel()

    async def close(self) -> None:
        self.closing = True
        self.cleanup_failed = True
        tasks = tuple(self.tasks)
        for task in tasks:
            if task not in self.cancel_requested:
                self.cancel_requested.add(task)
                task.cancel()
        pending = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=0.05)
        if any(not task.done() for task in pending):
            raise CLIConnectionError("Hook callbacks are still cleaning up; disconnect again")
        self.cleanup_failed = False
