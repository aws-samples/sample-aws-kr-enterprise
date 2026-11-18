"""Opt-in, process-local background sessions managed by the SDK."""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Optional

from .client import KiroSDKClient, _receive_queued_messages
from .background_events import encode_background_event, encode_structured_result
from .runtime_usage import validate_normalized_usage_report
from .errors import KiroSDKError
from .types import KiroAgentOptions, ResultMessage


class BackgroundTaskError(KiroSDKError):
    """An SDK-managed background task failed or exceeded its event buffer."""


@dataclass(frozen=True)
class BackgroundTaskStatus:
    """A snapshot of a process-local SDK-managed background session."""

    task_id: str
    state: Literal["pending", "running", "cancelling", "completed", "failed", "cancelled"]
    session_id: Optional[str]
    events_received: int
    error_type: Optional[str] = None
    events_dropped: int = 0
    cleanup_pending: bool = False


@dataclass(frozen=True)
class BackgroundTaskResult:
    """An optionally retained final result."""

    task_id: str
    result: Optional[str] = field(repr=False)
    is_error: bool
    structured_output: Any = field(default=None, repr=False)
    kiro_model_usage: Any = field(default=None, repr=False)
    usage_omitted: bool = False


@dataclass(frozen=True)
class BackgroundTaskEventPage:
    """A bounded, non-consuming snapshot of retained task events."""

    events: list[dict[str, Any]] = field(repr=False)
    next_cursor: int
    latest_sequence: int
    history_dropped: int
    gap: bool
    has_more: bool


@dataclass
class _Task:
    task_id: str
    queue: asyncio.Queue[Any]
    done: asyncio.Event = field(default_factory=asyncio.Event)
    available: asyncio.Event = field(default_factory=asyncio.Event)
    reader_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: Any = "pending"
    session_id: Optional[str] = None
    events_received: int = 0
    events_dropped: int = 0
    error: Optional[BaseException] = None
    retained_result: Optional[BackgroundTaskResult] = None
    retained_structured: Optional[bytes] = field(default=None, repr=False)
    retained_usage: Optional[bytes] = field(default=None, repr=False)
    usage_omitted: bool = False
    result_retention_error: bool = False
    history: deque[tuple[int, bytes]] = field(default_factory=deque)
    history_bytes: int = 0
    history_dropped: int = 0
    history_signal: asyncio.Event = field(default_factory=asyncio.Event)
    worker: Optional[asyncio.Task[Any]] = None
    cancel_requested: bool = False
    client: Optional[KiroSDKClient] = field(default=None, repr=False)
    cleanup_started: bool = False
    cleanup: Optional[asyncio.Task[None]] = field(default=None, repr=False)


class BackgroundTaskManager:
    """Manage independent Kiro sessions within the current Python process.

    These are SDK-owned tasks that run in separate Kiro sessions, not Kiro's own subagent tasks.
    Close the manager to stop outstanding jobs. By default, callers must consume
    events before the bounded buffer fills. An explicit overflow policy can retain
    recent events instead; inspect the dropped count before relying on that output.
    Do not mutate options while a job is running.
    """

    def __init__(self, *, max_tasks: int = 16, max_events: int = 256,
                 shutdown_timeout: float = 30.0, event_buffer_timeout: float = 5.0,
                 overflow_policy: Literal["fail", "drop-oldest"] = "fail",
                 max_retained_result_bytes: int = 0, max_history_events: int = 0,
                 max_history_bytes: int = 0, retain_structured_output: bool = False) -> None:
        if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 1:
            raise ValueError("max_tasks must be a positive integer")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        if isinstance(shutdown_timeout, bool) or not isinstance(shutdown_timeout, (int, float)):
            raise ValueError("shutdown_timeout must be a positive finite number")
        import math
        if not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be a positive finite number")
        if (isinstance(event_buffer_timeout, bool) or not isinstance(event_buffer_timeout, (int, float))
                or not math.isfinite(event_buffer_timeout) or event_buffer_timeout <= 0):
            raise ValueError("event_buffer_timeout must be a positive finite number")
        if type(overflow_policy) is not str or overflow_policy not in ("fail", "drop-oldest"):
            raise ValueError("overflow_policy must be 'fail' or 'drop-oldest'")
        if (type(max_retained_result_bytes) is not int or
                not 0 <= max_retained_result_bytes <= 1024 * 1024):
            raise ValueError("max_retained_result_bytes must be an integer from 0 to 1048576")
        if type(retain_structured_output) is not bool or (retain_structured_output and not max_retained_result_bytes):
            raise ValueError("retain_structured_output requires a positive result byte limit")
        if type(max_history_events) is not int or not 0 <= max_history_events <= 1024:
            raise ValueError("max_history_events must be an integer from 0 to 1024")
        if type(max_history_bytes) is not int or not 0 <= max_history_bytes <= 512 * 1024:
            raise ValueError("max_history_bytes must be an integer from 0 to 524288")
        if bool(max_history_events) != bool(max_history_bytes):
            raise ValueError("Event history requires both a count and byte limit")
        self._max_tasks = max_tasks
        self._max_events = max_events
        self._shutdown_timeout = float(shutdown_timeout)
        self._event_buffer_timeout = float(event_buffer_timeout)
        self._overflow_policy = overflow_policy
        self._max_retained_result_bytes = max_retained_result_bytes
        self._retain_structured_output = retain_structured_output
        self._retention_settings = (max_retained_result_bytes, retain_structured_output)
        self._max_history_events = max_history_events
        self._max_history_bytes = max_history_bytes
        self._tasks: dict[str, _Task] = {}
        self._closed = False

    async def start(self, prompt: Any, *, options: Optional[KiroAgentOptions] = None) -> str:
        """Start a new, independent Kiro session and return its SDK task ID."""
        return await self._start(prompt, options, None)

    async def _start_guarded(self, prompt: Any, options: KiroAgentOptions,
                             guard: Callable[[], Awaitable[bool]]) -> str:
        if not callable(guard):
            raise TypeError("guard must be callable")
        return await self._start(prompt, options, guard)

    async def _start(self, prompt: Any, options: Optional[KiroAgentOptions],
                     guard: Optional[Callable[[], Awaitable[bool]]]) -> str:
        if self._closed:
            raise RuntimeError("BackgroundTaskManager is closed")
        if self._has_failed_cleanup():
            raise BackgroundTaskError("Previous background task cleanup is incomplete")
        if len(self._tasks) >= self._max_tasks:
            raise RuntimeError("Background task capacity reached; forget completed tasks first")
        if not isinstance(prompt, (str, list)):
            raise TypeError("prompt must be a string or a list of content blocks")
        if options is not None and not isinstance(options, KiroAgentOptions):
            raise TypeError("options must be KiroAgentOptions")
        opts = copy.copy(options) if options is not None else KiroAgentOptions()
        if (opts.resume or opts.session_id or opts.continue_conversation or opts.fork_session
                or opts.resume_session_at or opts.resume_from_store):
            raise ValueError("Background tasks require independent new sessions")
        prompt = copy.deepcopy(prompt)
        task_id = "sdk-task-" + uuid.uuid4().hex
        record = _Task(task_id, asyncio.Queue(maxsize=self._max_events))
        self._tasks[task_id] = record
        worker = asyncio.create_task(self._run(record, prompt, opts, guard))
        record.worker = worker
        worker.add_done_callback(lambda _: self._worker_done(record))
        return task_id

    @staticmethod
    def _worker_done(record: _Task) -> None:
        # A task cancelled before its coroutine starts cannot run its finally block.
        error = (record.worker.exception() if record.worker is not None and
                 record.worker.done() and not record.worker.cancelled() else None)
        if not record.done.is_set():
            record.error = error
            record.state = "failed" if error is not None or record.client is not None else "cancelled"
            record.cleanup_started = record.client is not None
            record.done.set()
            record.available.set()
            BackgroundTaskManager._notify_history(record)

    def _has_failed_cleanup(self) -> bool:
        return any(task.done.is_set() and task.client is not None for task in self._tasks.values())

    @staticmethod
    def _begin_cleanup(record: _Task, *, retry: bool = False) -> Optional[asyncio.Task[None]]:
        if record.client is None:
            return None
        record.cleanup_started = True
        if record.cleanup is None or (retry and record.cleanup.done()):
            client = record.client
            async def finish() -> None:
                await client.disconnect()
                record.client = None
            record.cleanup = asyncio.create_task(finish())
            def finished(done):
                if not done.cancelled():
                    done.exception()
            record.cleanup.add_done_callback(finished)
        return record.cleanup

    @staticmethod
    async def _finish_cleanup(record: _Task) -> None:
        cleanup = BackgroundTaskManager._begin_cleanup(record)
        if cleanup is None:
            return
        # Worker cancellation must not end ownership of the client. stop() and
        # aclose() have bounded waits and leave this worker tracked on timeout.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    raise BackgroundTaskError("Background cleanup was cancelled") from None
        if cleanup.cancelled():
            raise BackgroundTaskError("Background cleanup was cancelled")
        cleanup.result()

    @staticmethod
    def _notify_history(record: _Task) -> None:
        previous = record.history_signal
        record.history_signal = asyncio.Event()
        previous.set()

    async def _run(self, record: _Task, prompt: Any, options: KiroAgentOptions,
                   guard: Optional[Callable[[], Awaitable[bool]]] = None) -> None:
        client: Optional[KiroSDKClient] = None
        terminal_state = "completed"
        def current() -> bool:
            return (not self._closed and not record.cancel_requested and not record.cleanup_started and
                    not record.done.is_set() and self._tasks.get(record.task_id) is record)

        async def authorized() -> bool:
            return current() and (guard is None or await guard() is True) and current()

        try:
            record.state = "running"
            if not await authorized():
                raise PermissionError("Background task authorization changed")
            client = KiroSDKClient(options=options)
            record.client = client
            client._prompt_send_guard = authorized
            client._interaction_guard = authorized
            client._client_auth_factory_guard = current
            client._require_client_auth_authorization = guard is not None
            await client.connect()
            record.session_id = client.session_id
            if not await authorized():
                raise PermissionError("Background task authorization changed")
            await client.query(prompt)
            async for event in _receive_queued_messages(client):
                if not await authorized():
                    raise PermissionError("Background task authorization changed")
                initial_result = (isinstance(event, ResultMessage) and
                                  getattr(client, "_initial_prompt_active", False) and
                                  getattr(client, "_queued_after_initial", None) is not None)
                if isinstance(event, ResultMessage) and self._max_retained_result_bytes:
                    # Never return an earlier result if a later result cannot
                    # be queued or retained.
                    record.retained_result = None
                    record.retained_structured = None
                    record.retained_usage = None
                    record.usage_omitted = False
                    record.result_retention_error = False
                try:
                    record.queue.put_nowait(event)
                except asyncio.QueueFull as exc:
                    if self._overflow_policy == "drop-oldest":
                        record.queue.get_nowait()
                        record.events_dropped += 1
                        record.queue.put_nowait(event)
                    elif not record.reader_lock.locked():
                        raise BackgroundTaskError("Background event buffer is full") from exc
                    else:
                        try:
                            await asyncio.wait_for(record.queue.put(event), self._event_buffer_timeout)
                        except asyncio.TimeoutError as timeout:
                            raise BackgroundTaskError("Background event buffer is full") from timeout
                record.events_received += 1
                if self._max_history_events:
                    try:
                        encoded = encode_background_event(event, max_bytes=min(self._max_history_bytes, 128 * 1024))
                        while record.history and (len(record.history) >= self._max_history_events or
                                record.history_bytes + len(encoded) > self._max_history_bytes):
                            _, removed = record.history.popleft()
                            record.history_bytes -= len(removed)
                            record.history_dropped += 1
                        record.history.append((record.events_received, encoded))
                        record.history_bytes += len(encoded)
                    except Exception:
                        # A malformed or oversized event is omitted; the
                        # sequence gap remains visible to history readers.
                        record.history_dropped += 1
                    self._notify_history(record)
                record.available.set()
                if isinstance(event, ResultMessage):
                    if self._max_retained_result_bytes and not initial_result:
                        try:
                            if ((self._max_retained_result_bytes, self._retain_structured_output) != self._retention_settings or
                                    type(event.is_error) is not bool or
                                    (event.result is not None and type(event.result) is not str) or
                                    (event.result is not None and
                                     (len(event.result) > self._max_retained_result_bytes or
                                      len(event.result.encode("utf-8")) > self._max_retained_result_bytes))):
                                raise ValueError("Invalid or oversized result")
                            text_bytes = len(event.result.encode("utf-8")) if event.result is not None else 0
                            structured = None
                            if self._retain_structured_output and event.structured_output is not None:
                                structured = encode_structured_result(
                                    event.structured_output, max_bytes=self._max_retained_result_bytes - text_bytes)
                            usage = None
                            record.usage_omitted = False
                            if event.kiro_model_usage is not None:
                                normalized_usage = validate_normalized_usage_report(event.kiro_model_usage)
                                try:
                                    usage = encode_structured_result(
                                        normalized_usage,
                                        max_bytes=self._max_retained_result_bytes - text_bytes - len(structured or b""))
                                except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
                                    record.usage_omitted = True
                            record.retained_result = BackgroundTaskResult(record.task_id, event.result, event.is_error)
                            record.retained_structured = structured
                            record.retained_usage = usage
                        except (ValueError, TypeError, UnicodeEncodeError, OverflowError, RecursionError):
                            record.result_retention_error = True
                    if event.is_error:
                        record.error = BackgroundTaskError("Kiro reported an unsuccessful turn")
                        terminal_state = "failed"
        except asyncio.CancelledError:
            terminal_state = "cancelled"
        except Exception as exc:
            record.error = exc
            terminal_state = "failed"
        finally:
            record.cleanup_started = True
            if client is not None:
                try:
                    await self._finish_cleanup(record)
                except Exception as exc:
                    record.error = exc
                    terminal_state = "failed"
            record.state = terminal_state
            record.done.set()
            record.available.set()
            self._notify_history(record)

    def _get(self, task_id: str) -> _Task:
        if not isinstance(task_id, str) or task_id not in self._tasks:
            raise KeyError("Unknown SDK background task")
        return self._tasks[task_id]

    def owns(self, task_id: str) -> bool:
        """Whether this manager currently holds the SDK task ID."""
        return isinstance(task_id, str) and task_id in self._tasks

    def status(self, task_id: str) -> BackgroundTaskStatus:
        """Return a snapshot; exception messages and prompts are not included."""
        task = self._get(task_id)
        return BackgroundTaskStatus(task.task_id, task.state, task.session_id,
                                    task.events_received,
                                    type(task.error).__name__ if task.error else None,
                                    task.events_dropped, task.cleanup_started and task.client is not None)

    def active_statuses(self, *, limit: int = 128) -> tuple[list[BackgroundTaskStatus], bool]:
        """Return bounded active SDK task snapshots and whether more were omitted."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise ValueError("limit must be an integer from 1 to 1024")
        result: list[BackgroundTaskStatus] = []
        for task in self._tasks.values():
            if (task.state not in ("pending", "running", "cancelling") and
                    not (task.cleanup_started and task.client is not None)):
                continue
            if len(result) == limit:
                return result, True
            result.append(self.status(task.task_id))
        return result, False

    def statuses(self, *, limit: int = 128) -> tuple[list[BackgroundTaskStatus], bool]:
        """Return bounded snapshots of managed tasks, including finished tasks."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise ValueError("limit must be an integer from 1 to 1024")
        result: list[BackgroundTaskStatus] = []
        for task in self._tasks.values():
            if len(result) == limit:
                return result, True
            result.append(self.status(task.task_id))
        return result, False

    async def wait(self, task_id: str) -> BackgroundTaskStatus:
        """Wait for the worker; inspect cleanup_pending if its cleanup failed."""
        task = self._get(task_id)
        await task.done.wait()
        return self.status(task_id)

    def retained_result(self, task_id: str) -> Optional[BackgroundTaskResult]:
        """Return a copied retained terminal result after cleanup, without consuming events."""
        task = self._get(task_id)
        if (self._max_retained_result_bytes, self._retain_structured_output) != self._retention_settings:
            raise RuntimeError("Background result retention settings changed")
        if not self._max_retained_result_bytes:
            raise RuntimeError("Background result retention is disabled")
        if not task.done.is_set():
            raise RuntimeError("Background task has not finished")
        if task.client is not None:
            raise BackgroundTaskError("Background task cleanup is incomplete")
        if task.result_retention_error:
            raise BackgroundTaskError("Background result was invalid or exceeded the retention limit")
        if task.retained_result is None:
            return None
        retained = task.retained_result
        structured = json.loads(task.retained_structured) if task.retained_structured is not None else None
        usage = json.loads(task.retained_usage) if task.retained_usage is not None else None
        return BackgroundTaskResult(retained.task_id, retained.result, retained.is_error, structured, usage,
                                    task.usage_omitted)

    def event_history(self, task_id: str, *, after: int = 0, limit: int = 32) -> BackgroundTaskEventPage:
        """Copy retained events after a cursor without consuming the event queue."""
        task = self._get(task_id)
        if not self._max_history_events:
            raise RuntimeError("Background event history is disabled")
        if type(after) is not int or after < 0 or after > task.events_received:
            raise ValueError("after must be a valid observed event cursor")
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("limit must be an integer from 1 to 64")
        selected: list[tuple[int, bytes]] = []
        has_more = False
        for sequence, encoded in task.history:
            if sequence <= after:
                continue
            if len(selected) == limit:
                has_more = True
                break
            selected.append((sequence, encoded))
        next_cursor = selected[-1][0] if has_more else task.events_received
        events = [{"sequence": sequence, **json.loads(encoded)} for sequence, encoded in selected]
        return BackgroundTaskEventPage(events, next_cursor, task.events_received, task.history_dropped,
                                       next_cursor - after != len(events), has_more)

    async def events(self, task_id: str) -> AsyncIterator[Any]:
        """Consume buffered events once; raise BackgroundTaskError after a failure."""
        task = self._get(task_id)
        if task.reader_lock.locked():
            raise RuntimeError("Only one event consumer is allowed per background task")
        async with task.reader_lock:
            while True:
                if not task.queue.empty():
                    yield task.queue.get_nowait()
                    continue
                if task.done.is_set():
                    if task.error is not None:
                        raise BackgroundTaskError("Background task failed") from task.error
                    return
                # A separate queue.get() waiter can consume an event just as the
                # reader is cancelled. Wait for a notification instead, then
                # dequeue synchronously when this consumer is ready to yield.
                task.available.clear()
                if not task.queue.empty() or task.done.is_set():
                    continue
                await task.available.wait()

    async def stop(self, task_id: str) -> BackgroundTaskStatus:
        """Cancel work or retry failed cleanup, without resubmitting the prompt."""
        task = self._get(task_id)
        cleanup = self._begin_cleanup(task, retry=True) if task.done.is_set() else None
        if task.worker and not task.worker.done() and not task.cancel_requested:
            task.cancel_requested = True
            task.state = "cancelling"
            if not task.cleanup_started:
                task.worker.cancel()
        pending_work = [item for item in (task.worker, cleanup) if item is not None and not item.done()]
        if pending_work:
            _, pending = await asyncio.wait(pending_work, timeout=self._shutdown_timeout)
            if pending:
                raise TimeoutError("Background task cleanup did not finish before the timeout")
        if cleanup is not None and cleanup.done() and task.client is not None:
            task.error = BackgroundTaskError("Background task cleanup is incomplete")
            task.state = "failed"
        # The done callback for a never-started worker can run on the next loop turn.
        if task.worker and task.worker.done():
            self._worker_done(task)
        return self.status(task_id)

    def forget(self, task_id: str) -> None:
        """Discard a finished task and any unread events."""
        task = self._get(task_id)
        if not task.done.is_set():
            raise RuntimeError("Cannot forget a running background task")
        if task.client is not None:
            raise RuntimeError("Cannot forget a background task with incomplete cleanup")
        if task.reader_lock.locked():
            raise RuntimeError("Cannot forget a task with an active event consumer")
        del self._tasks[task_id]

    async def aclose(self) -> None:
        """Stop all jobs and retry failed cleanup; retain ownership on failure."""
        self._closed = True
        workers = [t.worker for t in self._tasks.values() if t.worker and not t.worker.done()]
        for task in self._tasks.values():
            if task.done.is_set():
                cleanup = self._begin_cleanup(task, retry=True)
                if cleanup is not None and not cleanup.done():
                    workers.append(cleanup)
            if task.worker and not task.worker.done() and not task.cancel_requested:
                task.cancel_requested = True
                task.state = "cancelling"
                if not task.cleanup_started:
                    task.worker.cancel()
        if workers:
            _, pending = await asyncio.wait(workers, timeout=self._shutdown_timeout)
            if pending:
                raise TimeoutError("Background task cleanup did not finish before the timeout")
        for task in self._tasks.values():
            if task.worker and task.worker.done():
                self._worker_done(task)
        if self._has_failed_cleanup():
            raise BackgroundTaskError("Background task cleanup is incomplete")

    async def __aenter__(self) -> BackgroundTaskManager:
        if self._closed:
            raise RuntimeError("BackgroundTaskManager is closed")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()
