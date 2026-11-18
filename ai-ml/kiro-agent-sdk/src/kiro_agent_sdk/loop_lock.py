"""Lazy lock binding for Python versions that bind asyncio locks at construction."""
from __future__ import annotations

import asyncio
import sys
from typing import Any, Optional
from .errors import CLIConnectionError


class LazyLoopLock:
    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._loop: Any = None
        self.owner: Any = None
        self.acquiring: set[Any] = set()
        self.priority_tasks: Any = None
        self._shutdown_cancelled: dict[Any, int] = {}

    def cancel_others(self, allowed: set[Any]) -> None:
        self.priority_tasks = allowed
        for task in {*self.acquiring, self.owner}:
            if task is not None and task not in allowed and not task.done():
                if task not in self._shutdown_cancelled:
                    self._shutdown_cancelled[task] = getattr(task, "cancelling", lambda: 0)()
                    task.cancel()

    def _translate_shutdown_cancel(self, task: Any) -> None:
        before = self._shutdown_cancelled.pop(task, None)
        if before is None:
            return
        count = getattr(task, "cancelling", lambda: before + 1)()
        if hasattr(task, "uncancel"):
            task.uncancel()
        if before == 0 and count == 1:
            raise CLIConnectionError("Client operation interrupted by disconnect") from None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None:
            self._lock = asyncio.Lock()
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("Client lock belongs to a different event loop")
        return self._lock

    @property
    def _waiters(self) -> Any:
        return None if self._lock is None else self._lock._waiters

    def locked(self) -> bool:
        return self._lock is not None and self._lock.locked()

    async def acquire(self) -> bool:
        task = asyncio.current_task()
        if self.priority_tasks is not None and task not in self.priority_tasks:
            raise CLIConnectionError("Client shutdown is in progress")
        self.acquiring.add(task)
        try:
            result = await self._get_lock().acquire()
            self.owner = task
            return result
        except asyncio.CancelledError:
            self._translate_shutdown_cancel(task)
            raise
        finally:
            self.acquiring.discard(task)

    def release(self) -> None:
        if self._lock is None:
            raise RuntimeError("Lock is not acquired")
        self._lock.release()
        self.owner = None

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *args: Any) -> None:
        self.release()
        task = asyncio.current_task()
        if args and args[0] is asyncio.CancelledError:
            self._translate_shutdown_cancel(task)
        else:
            before = self._shutdown_cancelled.pop(task, None)
            if before is not None and hasattr(task, "uncancel"):
                task.uncancel()


def make_loop_lock() -> Any:
    return LazyLoopLock() if sys.version_info < (3, 10) else asyncio.Lock()
