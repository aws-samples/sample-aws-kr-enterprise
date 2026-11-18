"""Bounded asynchronous prefetch for streamed user messages."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Callable


class PrefetchedInput:
    """Read ahead without sending prompts or allowing unbounded queued input."""

    def __init__(self, source: Any, snapshot: Callable[[Any], tuple[Any, int]],
                 max_messages: int, max_bytes: int) -> None:
        self._source = source
        self._snapshot = snapshot
        self._max_messages = max_messages
        self._max_bytes = max_bytes
        self._items: deque[tuple[Any, int]] = deque()
        self._bytes = 0
        self._condition = asyncio.Condition()
        self._closed = False
        self._finished = False
        self._started = False
        self._error: BaseException | None = None
        self._task = asyncio.create_task(self._produce())

    def __aiter__(self) -> "PrefetchedInput":
        return self

    async def _produce(self) -> None:
        self._started = True
        error = None
        try:
            while not self._closed:
                async with self._condition:
                    await self._condition.wait_for(
                        lambda: self._closed or len(self._items) < self._max_messages)
                    if self._closed:
                        break
                try:
                    item = await self._source.__anext__()
                except StopAsyncIteration:
                    break
                if self._closed:
                    break
                content, size = self._snapshot(item)
                async with self._condition:
                    await self._condition.wait_for(
                        lambda: self._closed or self._bytes + size <= self._max_bytes)
                    if self._closed:
                        break
                    self._items.append((content, size))
                    self._bytes += size
                    self._condition.notify_all()
        except asyncio.CancelledError:
            if not self._closed:
                error = RuntimeError("Streaming input prefetch was cancelled")
        except BaseException as exc:
            error = exc
        finally:
            close = getattr(self._source, "aclose", None)
            if close is not None:
                try:
                    await close()
                except asyncio.CancelledError:
                    if not self._closed:
                        error = RuntimeError("Streaming input cleanup was cancelled")
                except BaseException as exc:
                    if error is None:
                        error = exc
            async with self._condition:
                self._error = error
                self._finished = True
                self._condition.notify_all()

    async def __anext__(self) -> Any:
        async with self._condition:
            await self._condition.wait_for(lambda: self._closed or self._items or self._finished)
            if self._closed:
                raise StopAsyncIteration
            if self._items:
                content, size = self._items.popleft()
                self._bytes -= size
                self._condition.notify_all()
                return content
            if self._error is not None:
                raise self._error
            raise StopAsyncIteration

    async def aclose(self) -> None:
        async with self._condition:
            self._closed = True
            self._items.clear()
            self._bytes = 0
            self._condition.notify_all()
        if not self._task.done():
            self._task.cancel()
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise
        if not self._started:
            close = getattr(self._source, "aclose", None)
            if close is not None:
                await close()
