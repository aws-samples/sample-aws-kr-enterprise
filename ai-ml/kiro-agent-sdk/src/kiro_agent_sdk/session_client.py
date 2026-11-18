"""Authenticated, authorized management of local Kiro V3 sessions."""
from __future__ import annotations

import asyncio
import copy
import math
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .archive import _valid_session_id
from .client import KiroSDKClient, _CLIENT_AUTH_CONTEXT
from .errors import CLIConnectionError, ProtocolError
from .loop_lock import make_loop_lock
from .sessions import _page, _replay_messages
from .types import ForkSessionResult, HistoryMessage, KiroAgentOptions, SessionInfo

_ACCESS_CONTEXT: ContextVar[tuple[Any, ...]] = ContextVar("kiro_session_access", default=())


async def _wait_for_task(task: asyncio.Future[Any], timeout: float) -> Any:
    # Python 3.9-3.11 wait_for can return the child's result when cancellation
    # and completion happen together. wait keeps caller cancellation distinct.
    try:
        if timeout <= 0:
            raise asyncio.TimeoutError
        done, _ = await asyncio.wait((task,), timeout=timeout)
        if not done:
            raise asyncio.TimeoutError
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif not task.cancelled():
            task.exception()


@dataclass(frozen=True)
class SessionAccess:
    """Requested operation, local workspace (None for global listing), and target."""

    operation: str
    cwd: Optional[str]
    session_id: Optional[str] = None


class KiroSessionClient:
    """Manage V3 sessions using the same client-owned token provider throughout.

    No chat session is created for listing, renaming, forking, or deleting.
    Reading history resumes the selected session in a temporary SDK connection.
    Application authorization is optional for trusted local use; network services
    must supply an authorizer and bind one client to one authenticated owner.
    """

    def __init__(self, *, cwd: str | Path | None = None, cli_path: str | Path = "kiro-cli",
                 env: Optional[dict[str, str]] = None,
                 access_token_provider: Optional[Callable[[], Awaitable[dict[str, str]]]] = None,
                 authorize: Optional[Callable[[SessionAccess], Awaitable[bool]]] = None,
                 timeout: float = 30, token_timeout: float = 10) -> None:
        try:
            valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 300
        except OverflowError:
            valid_timeout = False
        if (not valid_timeout or (authorize is not None and not callable(authorize)) or
                (env is not None and (type(env) is not dict or
                 any(type(key) is not str or type(value) is not str for key, value in env.items())))):
            raise ValueError("Session control requires a finite timeout and an optional callable authorizer")
        self._cwd = str(Path(cwd or Path.cwd()).resolve())
        self._authorize = authorize
        self._authorizer_identity = authorize
        self._timeout = timeout
        self._client = KiroSDKClient(KiroAgentOptions(
            cwd=self._cwd, cli_path=cli_path, env=dict(env or {}), agent_engine="v3",
            enable_kiro_hooks=False, startup_timeout=timeout,
            kiro_auth_method="client" if access_token_provider is not None else "cli",
            kiro_access_token_provider=access_token_provider, kiro_access_token_timeout=token_timeout))
        self._client._incoming_message_limit = (20000, 8 * 1024 * 1024)
        self._lock = make_loop_lock()
        self._active: Optional[SessionAccess] = None
        self._connected = False
        self._closing = False
        self._generation = 0
        self._deadline: Optional[float] = None
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self._disconnect_task: Optional[asyncio.Task[None]] = None
        self._history_clients: set[KiroSDKClient] = set()
        self._history_cleanup: dict[KiroSDKClient, asyncio.Task[None]] = {}

    @staticmethod
    def _consume_cleanup_error(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _close_history(self, child: KiroSDKClient, *, retry: bool = False) -> None:
        task = self._history_cleanup.get(child)
        if task is None or (retry and task.done()):
            async def finish():
                await child.disconnect()
                self._history_clients.discard(child)
                self._history_cleanup.pop(child, None)
            task = asyncio.create_task(finish())
            self._history_cleanup[child] = task
            task.add_done_callback(self._consume_cleanup_error)
        await asyncio.shield(task)

    async def _disconnect_owned_connections(self) -> None:
        failed = False
        for child in tuple(self._history_clients):
            try:
                await self._close_history(child, retry=True)
            except Exception:
                failed = True
        try:
            await self._client.disconnect()
        except Exception:
            failed = True
        if failed:
            raise CLIConnectionError("Session control cleanup failed; disconnect again after pending callbacks finish")

    async def _close_control(self, *, retry: bool = False) -> None:
        # Invalidate authorization before awaiting cleanup. A second caller
        # cancellation must not abandon shutdown of an already submitted RPC.
        self._active = None
        self._connected = False
        self._generation += 1
        task = self._cleanup_task
        if task is None or (retry and task.done()):
            task = asyncio.create_task(self._disconnect_owned_connections())
            self._cleanup_task = task
            task.add_done_callback(self._consume_cleanup_error)
        await asyncio.shield(task)

    def _check_reentry(self) -> None:
        if self in _ACCESS_CONTEXT.get() or self._client in _CLIENT_AUTH_CONTEXT.get():
            raise CLIConnectionError("Session authorization and token callbacks cannot control their client")

    async def _authorize_active(self) -> bool:
        access, generation, authorizer = self._active, self._generation, self._authorize
        if access is None or self._closing or authorizer is not self._authorizer_identity:
            return False
        context = _ACCESS_CONTEXT.set((*_ACCESS_CONTEXT.get(), self))
        sdk_context = _CLIENT_AUTH_CONTEXT.set((*_CLIENT_AUTH_CONTEXT.get(), self._client))
        try:
            try:
                allowed = True if authorizer is None else await authorizer(access)
            except asyncio.CancelledError:
                raise
            except BaseException:
                allowed = False
        finally:
            _CLIENT_AUTH_CONTEXT.reset(sdk_context)
            _ACCESS_CONTEXT.reset(context)
        return (allowed is True and self._active is access and self._generation == generation and
                self._authorize is authorizer and not self._closing)

    async def _check_access(self) -> None:
        remaining = (self._deadline - asyncio.get_running_loop().time()
                     if self._deadline is not None else self._timeout)
        if remaining <= 0:
            raise CLIConnectionError("Kiro session operation timed out")
        # The CLI may send initialize/list results while it is still asking for
        # credentials. Serialize our standalone policy check with the token
        # handler, whose task registry also detects abandoned callbacks.
        acquire = asyncio.ensure_future(self._client._client_auth_lock.acquire())
        try:
            try:
                await _wait_for_task(acquire, remaining)
            except asyncio.TimeoutError:
                raise CLIConnectionError("Kiro session operation timed out") from None
            await self._check_access_locked()
        finally:
            # The child may have acquired the lock just as our caller was
            # cancelled. Its result still determines whether we must release.
            if (acquire.done() and not acquire.cancelled() and
                    acquire.exception() is None and acquire.result()):
                self._client._client_auth_lock.release()

    async def _check_access_locked(self) -> None:
        remaining = (self._deadline - asyncio.get_running_loop().time()
                     if self._deadline is not None else self._timeout)
        if remaining <= 0:
            raise CLIConnectionError("Kiro session operation timed out")
        if any(not task.done() for task in self._client._client_auth_tasks):
            raise CLIConnectionError("Previous session authentication or authorization is still pending")
        task = asyncio.ensure_future(self._authorize_active())
        self._client._client_auth_tasks.add(task)
        def finished(done):
            self._client._client_auth_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait((task,), timeout=min(remaining, 5))
            if not done or task.cancelled() or task.result() is not True:
                raise PermissionError("Kiro session access denied")
        finally:
            if not task.done():
                task.cancel()

    async def _settle_authentication(self) -> None:
        # Retain this operation's authorization until requests already received
        # by the CLI reader finish. Truly idle requests remain unauthorized.
        while True:
            pending = [record["task"] for record in self._client._inbound_requests.values()
                       if record["method"] == "_kiro/auth/getAccessToken" and not record["task"].done()]
            if not pending:
                return
            remaining = self._deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CLIConnectionError("Kiro session operation timed out")
            _, unfinished = await asyncio.wait(pending, timeout=remaining)
            if unfinished:
                raise CLIConnectionError("Kiro session operation timed out")

    @asynccontextmanager
    async def _operation(self, operation: str, session_id: Optional[str] = None, *, global_listing: bool = False):
        self._check_reentry()
        if self._closing:
            raise CLIConnectionError("Session control cleanup is still pending")
        if session_id is not None and not _valid_session_id(session_id):
            raise ValueError("Invalid Kiro session ID")
        async with self._lock:
            if self._cleanup_task is not None:
                if not self._cleanup_task.done():
                    raise CLIConnectionError("Session control cleanup is still pending")
                if self._cleanup_task.cancelled() or self._cleanup_task.exception() is not None:
                    raise CLIConnectionError("Disconnect after a session control cleanup failure")
                self._cleanup_task = None
            if self._history_clients:
                raise CLIConnectionError("Disconnect after a session history cleanup failure")
            if self._closing or (operation != "connect" and not self._connected):
                raise CLIConnectionError("Connect before managing sessions")
            if operation == "connect" and self._connected:
                raise CLIConnectionError("Already connected")
            if (self._client._client_auth_pending or
                    any(not task.done() for task in self._client._client_auth_tasks)):
                raise CLIConnectionError("Previous session authentication or authorization is still pending")
            if operation == "connect":
                self._client._hook_failure_runner.reopen()
            self._client._validate_unsupported_limits()
            if operation != "connect" and self._client._reader_error is not None:
                raise CLIConnectionError("Disconnect after a session control transport failure")
            self._client._incoming_message_count = self._client._incoming_message_bytes = 0
            self._active = SessionAccess(operation, None if global_listing else self._cwd, session_id)
            self._deadline = asyncio.get_running_loop().time() + self._timeout
            try:
                await self._check_access()
                yield
                await self._settle_authentication()
                await self._check_access()
                await self._settle_authentication()
            except BaseException as error:
                if (operation == "connect" or isinstance(error, asyncio.CancelledError) or
                        asyncio.get_running_loop().time() >= self._deadline):
                    try:
                        await self._close_control()
                    except Exception:
                        # Preserve the operation error. The failed cleanup task
                        # remains tracked and blocks reuse until disconnect().
                        pass
                raise
            finally:
                self._active = None
                self._deadline = None

    async def _rpc(self, operation: Awaitable[Any]) -> Any:
        try:
            remaining = (self._deadline - asyncio.get_running_loop().time()
                         if self._deadline is not None else self._timeout)
            return await _wait_for_task(asyncio.ensure_future(operation), max(0, remaining))
        except asyncio.TimeoutError:
            await self._close_control()
            raise CLIConnectionError("Kiro session operation timed out") from None

    async def connect(self) -> None:
        async with self._operation("connect"):
            self._generation += 1
            self._client._prompt_send_guard = self._authorize_active
            self._client._interaction_guard = self._client._prompt_send_guard
            self._client._client_auth_factory_guard = lambda: self._active is not None and not self._closing
            self._client._require_client_auth_authorization = True
            await self._rpc(self._client._connect_control())
            self._connected = True

    async def disconnect(self) -> None:
        self._check_reentry()
        if self._disconnect_task is None or self._disconnect_task.done():
            self._closing = True
            self._generation += 1
            self._disconnect_task = asyncio.create_task(self._disconnect_serialized())
            self._disconnect_task.add_done_callback(self._consume_cleanup_error)
        await asyncio.shield(self._disconnect_task)

    async def _disconnect_serialized(self) -> None:
        try:
            async with self._lock:
                await self._close_control(retry=True)
        finally:
            self._connected = False
            self._closing = False

    @staticmethod
    def _bounded(value: Any) -> None:
        import json
        try:
            valid = len(json.dumps(value, ensure_ascii=True).encode("ascii")) <= 8 * 1024 * 1024
        except (TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            raise ProtocolError("Kiro session response is invalid or exceeds 8 MiB")

    async def _list_page(self, cursor: Optional[str], global_listing: bool) -> dict[str, Any]:
        if cursor is not None and (type(cursor) is not str or not cursor or len(cursor) > 4096):
            raise ValueError("Invalid session listing cursor")
        await self._check_access()
        result = await self._rpc(self._client.list_sessions(
            cwd=None if global_listing else self._cwd, cursor=cursor))
        await self._check_access()
        self._bounded(result)
        if (type(result) is not dict or type(result.get("sessions")) is not list or
                len(result["sessions"]) > 10000 or any(type(item) is not dict for item in result["sessions"])):
            raise ProtocolError("Invalid Kiro session list")
        return result

    async def list_sessions(self, *, cursor: Optional[str] = None, all_directories: bool = False) -> dict[str, Any]:
        """Return one bounded ACP page. Global listing requires explicit selection."""
        if type(all_directories) is not bool:
            raise ValueError("all_directories must be a boolean")
        async with self._operation("list_sessions", global_listing=all_directories):
            return await self._list_page(cursor, all_directories)

    async def get_session_info(self, session_id: str) -> Optional[SessionInfo]:
        """Find one session in this workspace, with bounded pagination."""
        async with self._operation("get_session_info", session_id):
            cursor, seen, total, size = None, set(), 0, 0
            import json
            for _ in range(100):
                page = await self._list_page(cursor, False)
                total += len(page["sessions"])
                size += len(json.dumps(page, ensure_ascii=True).encode("ascii"))
                if total > 10000 or size > 8 * 1024 * 1024:
                    raise ProtocolError("Kiro session lookup exceeds the size limit")
                matches = [item for item in page["sessions"] if item.get("sessionId") == session_id]
                if len(matches) > 1:
                    raise ProtocolError("Ambiguous Kiro session listing")
                if matches:
                    result = SessionInfo(matches[0])
                    result.setdefault("cwd", self._cwd)
                    result.setdefault("source", "v3")
                    return result
                cursor = page.get("nextCursor")
                if cursor is None:
                    return None
                if type(cursor) is not str or not cursor or len(cursor) > 4096 or cursor in seen:
                    raise ProtocolError("Invalid Kiro session listing cursor")
                seen.add(cursor)
            raise ProtocolError("Kiro session lookup exceeds the page limit")

    async def rename_session(self, session_id: str, title: str) -> None:
        if type(title) is not str or not title.strip() or "\0" in title or len(title) > 1024:
            raise ValueError("A session title must contain between one and 1024 characters")
        async with self._operation("rename_session", session_id):
            result = await self._rpc(self._client.rename_session(title.strip(), session_id))
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ProtocolError("Kiro did not confirm the session rename")

    async def fork_session(self, session_id: str, *, up_to_message_id: Optional[str] = None,
                           title: Optional[str] = None) -> ForkSessionResult:
        if title is not None and (type(title) is not str or not title.strip() or "\0" in title or len(title) > 1024):
            raise ValueError("Invalid fork title")
        if up_to_message_id is not None and (
                type(up_to_message_id) is not str or not up_to_message_id or
                "\0" in up_to_message_id or len(up_to_message_id) > 4096):
            raise ValueError("Invalid fork message ID")
        async with self._operation("fork_session", session_id):
            result = await self._rpc(self._client.fork_session(
                session_id, message_id=up_to_message_id, title=title.strip() if title else None))
            target = result.get("sessionId") if isinstance(result, dict) else None
            if not _valid_session_id(target) or target == session_id:
                raise ProtocolError("Kiro did not return a distinct forked session ID")
            return ForkSessionResult(session_id=target)

    async def delete_session(self, session_id: str) -> None:
        async with self._operation("delete_session", session_id):
            await self._rpc(self._client.delete_session(session_id))

    async def _history(self, session_id: str) -> list[dict[str, Any]]:
        options = copy.copy(self._client.options)
        options.env = dict(options.env)
        options.resume = session_id
        child = KiroSDKClient(options)
        child._incoming_message_limit = (20000, 8 * 1024 * 1024)
        child._prompt_send_guard = self._authorize_active
        child._interaction_guard = child._prompt_send_guard
        self._client._bind_client_auth(child)
        self._history_clients.add(child)
        try:
            await self._rpc(child.connect())
            events = list(child.history)
            if len(events) > 20000:
                raise ProtocolError("Kiro history exceeds the entry limit")
            self._bounded(events)
            return copy.deepcopy(events)
        finally:
            await self._close_history(child)

    async def get_session_history(self, session_id: str) -> list[dict[str, Any]]:
        """Resume a saved session to read replay events without submitting a prompt."""
        async with self._operation("get_session_history", session_id):
            return await self._history(session_id)

    async def get_session_messages(self, session_id: str, *, limit: Optional[int] = None, offset: int = 0,
                                   coalesce_chunks: bool = False, exclude_observed_subagents: bool = False,
                                   include_observed_tool_messages: bool = False) -> list[HistoryMessage]:
        _page(offset, limit)
        if any(type(value) is not bool for value in (
                coalesce_chunks, exclude_observed_subagents, include_observed_tool_messages)):
            raise ValueError("Replay options must be booleans")
        async with self._operation("get_session_messages", session_id):
            if limit == 0:
                return []
            events = await self._history(session_id)
            messages = _replay_messages(events, session_id, coalesce_chunks, exclude_observed_subagents,
                                       include_observed_tool_messages)
            return messages[offset:None if limit is None else offset + limit]

    async def __aenter__(self) -> KiroSessionClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()
