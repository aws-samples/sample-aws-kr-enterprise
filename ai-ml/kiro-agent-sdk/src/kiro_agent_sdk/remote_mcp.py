"""Optional HTTP/SSE MCP relay using authenticated local stdio."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from importlib import metadata
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from .external_mcp import _ToolFilterBridge
from .mcp_cancellation import ObservedToolCalls, _request_key, cancellation_notice, parse_message
from .mcp_output_tracker import McpOutputTracker
from .mcp_input_gate import McpInputGate
from .mcp_oauth import McpOAuthConfig, _origin
from .mcp_content_encoding import McpContentDecoder, ACCEPT_ENCODING
from .mcp_dispatch import RemoteDispatch

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_HEADERS = {"accept", "content-type", "content-length", "host", "connection",
                      "transfer-encoding", "upgrade", "trailer", "te", "proxy-authorization",
                      "proxy-connection", "mcp-session-id", "mcp-protocol-version", "accept-encoding"}
_MAX_REMOTE_MESSAGE = 16 * 1024 * 1024
_SSE_INITIALIZATION_SHUTDOWN_TIMEOUT = 3.0
_SSE_CANCEL_TOKEN_TIMEOUT = 1.0
_RESPONSE_CLOSE_TASKS: set[asyncio.Task] = set()
_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9._~+/-]+={0,}$", re.ASCII)


class _ResponseLimiter:
    """Bound JSON bodies and individual SSE events without buffering them."""

    def __init__(self, *, sse: bool) -> None:
        self.sse = sse
        self.size = 0
        self.line_size = 0
        self.after_cr = False
        self.cr_was_blank = False

    def _add(self, count: int) -> None:
        self.size += count
        if self.size > _MAX_REMOTE_MESSAGE:
            raise ValueError("Remote MCP response exceeds the message limit")

    def feed(self, data: bytes) -> None:
        if not self.sse:
            self._add(len(data))
            return
        position = 0
        while position < len(data):
            if self.after_cr:
                self.after_cr = False
                if data[position:position + 1] == b"\n":
                    if not self.cr_was_blank:
                        self._add(1)
                    position += 1
                    continue
            cr = data.find(b"\r", position)
            lf = data.find(b"\n", position)
            end = min((item for item in (cr, lf) if item >= 0), default=len(data))
            count = end - position
            self._add(count)
            self.line_size += count
            if end == len(data):
                return
            self._add(1)
            blank = self.line_size == 0
            if blank:
                self.size = 0
            self.line_size = 0
            self.after_cr = data[end] == 13
            self.cr_was_blank = blank and self.after_cr
            position = end + 1


def _require_supported_mcp() -> None:
    """Require the transport versions with same-origin redirect enforcement."""
    try:
        version = metadata.version("mcp")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("Remote MCP relaying requires the optional mcp package") from exc
    match = re.fullmatch(r"([12])\.(\d+)\.(\d+)(?:\.post\d+)?", version)
    if match is None:
        raise RuntimeError("Remote MCP relaying requires mcp >=1.30,<2 or >=2.3,<3")
    major, minor = int(match.group(1)), int(match.group(2))
    if (major == 1 and minor < 30) or (major == 2 and minor < 3):
        raise RuntimeError("Remote MCP relaying requires mcp >=1.30,<2 or >=2.3,<3")


def _loopback_host(host: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return (host or "").lower() == "localhost"


def _same_callable(left: Any, right: Any) -> bool:
    """Compare bound methods by identity without invoking custom equality."""
    return (getattr(left, "__self__", None) is getattr(right, "__self__", None) and
            getattr(left, "__func__", left) is getattr(right, "__func__", right))


class RemoteMcpBridge(_ToolFilterBridge):
    """Relay explicit MCP HTTP/SSE endpoints; block new tool calls while disabled."""

    def __init__(self, config: dict[str, Any], *, connect_timeout: Optional[float] = 60.0,
                 oauth: Optional[McpOAuthConfig] = None,
                 _oauth_state: Optional[tuple[Any, set[tuple[str, str, int]]]] = None,
                 output_transform: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
                 input_transform: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
                 request_guard: Optional[Callable[[], Awaitable[bool]]] = None) -> None:
        super().__init__()
        self.output_transform = output_transform
        self.input_transform = input_transform
        if request_guard is not None and not callable(request_guard):
            raise ValueError("Remote MCP request guard must be callable")
        self._request_guard = request_guard
        if set(config) - {"type", "url", "headers"} or config.get("type") not in ("http", "sse"):
            raise ValueError("Unsupported remote MCP bridge configuration")
        url = config.get("url")
        if not isinstance(url, str) or not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError("Invalid remote MCP URL")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and bool(parsed.hostname) and
                     parsed.username is None and parsed.password is None and not parsed.fragment)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid remote MCP URL") from exc
        if not valid:
            raise ValueError("Invalid remote MCP URL")
        headers = config.get("headers", {})
        if isinstance(headers, list):
            converted: dict[str, str] = {}
            for item in headers:
                if not isinstance(item, dict) or set(item) != {"name", "value"}:
                    raise ValueError("Invalid remote MCP headers")
                key = item["name"]
                if not isinstance(key, str) or key.lower() in converted:
                    raise ValueError("Duplicate or invalid remote MCP header")
                converted[key.lower()] = item["value"]
            headers = converted
        if not isinstance(headers, dict):
            raise ValueError("Invalid remote MCP headers")
        normalized: dict[str, str] = {}
        for key, value in headers.items():
            if (not isinstance(key, str) or not _HEADER_NAME.fullmatch(key) or key.lower() in normalized or
                    key.lower() in _FORBIDDEN_HEADERS or not isinstance(value, str) or
                    any(ord(char) < 32 or ord(char) > 126 for char in value)):
                raise ValueError("Invalid or transport-managed remote MCP header")
            normalized[key.lower()] = value
        if normalized and parsed.scheme == "http" and not _loopback_host(parsed.hostname):
            raise ValueError("Remote MCP headers require HTTPS except for loopback HTTP")
        _require_supported_mcp()
        self.kind, self.url, self.headers = config["type"], url, normalized
        self.connect_timeout = connect_timeout
        if oauth is not None and not isinstance(oauth, McpOAuthConfig):
            raise ValueError("Invalid remote MCP OAuth configuration")
        if oauth is not None and normalized:
            raise ValueError("Remote MCP OAuth cannot be combined with static headers")
        if oauth is not None:
            if oauth.sse_cancel_token_provider is not None and not callable(oauth.sse_cancel_token_provider):
                raise ValueError("sse_cancel_token_provider must be an async callback")
            if type(oauth.sse_cancel_retry_with_stored_token) is not bool:
                raise ValueError("sse_cancel_retry_with_stored_token must be a boolean")
            if oauth.sse_cancel_retry_with_stored_token and oauth.sse_cancel_token_provider is not None:
                raise ValueError("Choose one SSE cancellation token retry source")
            if (oauth.sse_cancel_token_provider is not None or oauth.sse_cancel_retry_with_stored_token) and self.kind != "sse":
                raise ValueError("SSE cancellation token retry requires an SSE MCP server")
        self._oauth_config = oauth
        self._sse_cancel_token_provider = oauth.sse_cancel_token_provider if oauth is not None else None
        self._sse_cancel_stored_mode = oauth.sse_cancel_retry_with_stored_token if oauth is not None else False
        self._sse_cancel_token_tasks: set[asyncio.Future[Any]] = set()
        self.oauth, self.oauth_origins = (
            (_oauth_state or oauth._create_provider(url)) if oauth is not None else (None, set())
        )
        self._sse_cancel_storage = self.oauth.context.storage if self._sse_cancel_stored_mode else None
        self._sse_cancel_storage_reader = (getattr(self._sse_cancel_storage, "get_tokens", None)
                                           if self._sse_cancel_stored_mode else None)
        if self._sse_cancel_stored_mode and (
                self._sse_cancel_storage is not oauth.storage or not callable(self._sse_cancel_storage_reader)):
            raise ValueError("SSE cancellation retry requires the connected OAuth token storage")

    async def _clear_oauth_tokens(self) -> None:
        """Forget local tokens under the provider lock; this does not revoke them."""
        if self.oauth is None:
            raise ValueError("Remote MCP server has no SDK OAuth configuration")
        clear = getattr(self.oauth.context.storage, "clear_tokens", None)
        if not callable(clear):
            raise ValueError("OAuth token storage must implement async clear_tokens")
        async with self.oauth.context.lock:
            await clear()
            self.oauth.context.clear_tokens()
            self.oauth._initialized = False

    async def _revoke_oauth_tokens(self) -> dict[str, bool]:
        from .oauth_errors import sanitized_oauth
        return await sanitized_oauth(self._revoke_oauth_tokens_raw(), "OAuth token revocation failed", RuntimeError,
            safe_errors={message: ValueError for message in (
                "Remote MCP server has no SDK OAuth configuration",
                "OAuth token storage must implement async clear_tokens",
                "Invalid OAuth tokens", "OAuth server does not advertise a revocation endpoint",
                "OAuth revocation endpoint origin is not allowed",
                "OAuth revocation requires an unsupported client authentication method",
                "OAuth revocation requires a public client registration")})

    async def _revoke_oauth_tokens_raw(self) -> dict[str, bool]:
        """Request RFC 7009 revocation and forget local tokens on success."""
        if self.oauth is None:
            raise ValueError("Remote MCP server has no SDK OAuth configuration")
        provider = self.oauth
        context = provider.context
        storage = context.storage
        clear = getattr(storage, "clear_tokens", None)
        if not callable(clear):
            raise ValueError("OAuth token storage must implement async clear_tokens")

        async with context.lock:
            tokens = await storage.get_tokens()
            if tokens is None:
                tokens = context.current_tokens
            access = getattr(tokens, "access_token", None)
            refresh = getattr(tokens, "refresh_token", None)
            if any(value is not None and (not isinstance(value, str) or not value or len(value) > 16384)
                   for value in (access, refresh)):
                raise ValueError("Invalid OAuth tokens")
            requested = {"access_token": bool(access), "refresh_token": bool(refresh)}
            if access or refresh:
                metadata = context.oauth_metadata
                endpoint = getattr(metadata, "revocation_endpoint", None)
                if endpoint is None:
                    raise ValueError("OAuth server does not advertise a revocation endpoint")
                endpoint = str(endpoint)
                if _origin(endpoint) not in self.oauth_origins:
                    raise ValueError("OAuth revocation endpoint origin is not allowed")
                methods = getattr(metadata, "revocation_endpoint_auth_methods_supported", None)
                if methods is not None and "none" not in methods:
                    raise ValueError("OAuth revocation requires an unsupported client authentication method")
                info = await storage.get_client_info()
                if info is None:
                    info = context.client_info
                client_id = getattr(info, "client_id", None)
                if (not isinstance(client_id, str) or not client_id or len(client_id) > 16384 or
                        getattr(info, "client_secret", None) or
                        getattr(info, "token_endpoint_auth_method", "none") not in (None, "none")):
                    raise ValueError("OAuth revocation requires a public client registration")

                from mcp.client import streamable_http
                http = getattr(streamable_http, "httpx2", None) or getattr(streamable_http, "httpx", None)
                if http is None:
                    raise RuntimeError("Unsupported MCP HTTP implementation")
                try:
                    async with http.AsyncClient(follow_redirects=False, trust_env=False, timeout=15.0,
                                                headers={"Accept-Encoding": "identity"}) as client:
                        for hint, value in (("refresh_token", refresh), ("access_token", access)):
                            if value:
                                async with client.stream(
                                    "POST", endpoint,
                                    data={"token": value, "token_type_hint": hint, "client_id": client_id},
                                ) as response:
                                    if response.status_code != 200:
                                        raise RuntimeError("OAuth token revocation was not accepted")
                except Exception:
                    # Do not expose token-bearing request details from HTTP exceptions.
                    raise RuntimeError("OAuth token revocation failed") from None
            await clear()
            context.clear_tokens()
            provider._initialized = False
            return requested

    @asynccontextmanager
    async def _remote_streams(
        self, *,
        sse_cancel_setter: Optional[Callable[[Callable[[dict[str, Any]], Awaitable[None]]], None]] = None,
        dispatch: Optional[RemoteDispatch] = None,
    ) -> AsyncIterator[tuple[Any, Any]]:
        # Use the HTTP implementation shipped with the installed MCP version.
        from mcp.client import streamable_http
        http = getattr(streamable_http, "httpx2", None) or getattr(streamable_http, "httpx", None)
        if http is None:
            raise RuntimeError("Unsupported MCP HTTP implementation")
        oauth = self.oauth
        oauth_origins = self.oauth_origins
        request_guard = self._request_guard
        bridge_kind = self.kind
        sse_endpoint: Optional[str] = None
        sse_authorization: Optional[str] = None

        def remember_sse_endpoint(request: Any) -> None:
            nonlocal sse_endpoint, sse_authorization
            if bridge_kind != "sse" or request.method != "POST":
                return
            try:
                source, target = urlsplit(self.url), urlsplit(str(request.url))
                def origin(value: Any) -> tuple[str, str, int]:
                    if (value.scheme not in ("http", "https") or not value.hostname or
                            value.username is not None or value.password is not None or value.fragment):
                        raise ValueError("Invalid SSE endpoint")
                    port = value.port
                    if port == 0:
                        raise ValueError("Invalid SSE endpoint")
                    return (value.scheme, value.hostname.encode("idna").decode("ascii").lower(),
                            port if port is not None else (443 if value.scheme == "https" else 80))
                if origin(source) != origin(target):
                    return
                content = request.content
                if len(content) > _MAX_REMOTE_MESSAGE:
                    return
                message = json.loads(content)
                if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or
                        not isinstance(message.get("method"), str)):
                    return
                sse_endpoint = str(request.url)
                sse_authorization = request.headers.get("authorization")
            except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError, http.RequestNotRead):
                return

        async def send_sse_cancellation(message: dict[str, Any]) -> None:
            # The MCP SSE writer serializes POSTs, so it cannot send a
            # cancellation while a tool POST is waiting for its response.
            if sse_endpoint is None:
                raise RuntimeError("MCP SSE cancellation endpoint is unavailable")
            def retry_configuration_current() -> bool:
                config = self._oauth_config
                if config is None:
                    return not self._sse_cancel_stored_mode and self._sse_cancel_token_provider is None
                if (config.sse_cancel_token_provider is not self._sse_cancel_token_provider or
                        config.sse_cancel_retry_with_stored_token is not self._sse_cancel_stored_mode):
                    return False
                return (not self._sse_cancel_stored_mode or (
                    config.storage is self._sse_cancel_storage and
                    self.oauth.context.storage is self._sse_cancel_storage and
                    _same_callable(getattr(self._sse_cancel_storage, "get_tokens", None),
                                   self._sse_cancel_storage_reader)))
            if not retry_configuration_current():
                raise RuntimeError("MCP SSE cancellation configuration changed; reconnect")
            endpoint, previous_authorization = sse_endpoint, sse_authorization
            headers = {**self.headers, "Accept-Encoding": ACCEPT_ENCODING}
            if oauth is not None and sse_authorization is not None:
                # OAuth providers may hold their lock until the in-flight POST
                # returns. Reuse its bearer header without starting a new flow.
                if not sse_authorization.startswith("Bearer ") or len(sse_authorization) <= 7:
                    raise RuntimeError("MCP SSE cancellation authorization is unavailable")
                headers["Authorization"] = sse_authorization
            try:
                from mcp.client import sse
                async with http.AsyncClient(
                    headers=headers, follow_redirects=False, trust_env=False,
                    timeout=3.0, transport=LimitedTransport(capture_sse=False),
                ) as cancel_client:
                    response = await sse.request_within_origin(
                        cancel_client, "POST", endpoint, json=message)
                    if response.status_code == 401 and (
                            self._sse_cancel_token_provider is not None or self._sse_cancel_stored_mode):
                        async def retry_token() -> str:
                            if not self._sse_cancel_stored_mode:
                                return await self._sse_cancel_token_provider()
                            from mcp.shared.auth import OAuthToken
                            stored = await self._sse_cancel_storage_reader()
                            if (not isinstance(stored, OAuthToken) or stored.token_type != "Bearer" or
                                    (stored.expires_in is not None and
                                     (type(stored.expires_in) is not int or stored.expires_in <= 0))):
                                raise ValueError("Invalid stored OAuth token")
                            return stored.access_token
                        task = asyncio.ensure_future(retry_token())
                        self._sse_cancel_token_tasks.add(task)
                        def consume(done: asyncio.Future) -> None:
                            self._sse_cancel_token_tasks.discard(done)
                            if not done.cancelled():
                                done.exception()
                        task.add_done_callback(consume)
                        try:
                            done, _ = await asyncio.wait((task,), timeout=_SSE_CANCEL_TOKEN_TIMEOUT)
                            if not done or task.cancelled():
                                raise RuntimeError("MCP SSE cancellation token unavailable")
                            token = task.result()
                        finally:
                            if not task.done():
                                task.cancel()
                        if (not isinstance(token, str) or len(token) > 16384 or
                                _BEARER_TOKEN.fullmatch(token) is None):
                            raise ValueError("Invalid MCP SSE cancellation token")
                        if (self._closed or not retry_configuration_current() or sse_endpoint != endpoint):
                            raise RuntimeError("MCP SSE cancellation configuration changed")
                        if (self._sse_cancel_stored_mode and previous_authorization is not None and
                                secrets.compare_digest("Bearer " + token, previous_authorization)):
                            raise RuntimeError("Stored MCP SSE cancellation token has not changed")
                        response = await sse.request_within_origin(
                            cancel_client, "POST", endpoint, json=message,
                            headers={"Authorization": "Bearer " + token})
                    response.raise_for_status()
            except asyncio.CancelledError:
                response = token = task = done = _ = cancel_client = headers = previous_authorization = endpoint = retry_token = None
                raise
            except Exception:
                pass
            else:
                return
            response = token = task = done = _ = cancel_client = headers = previous_authorization = endpoint = retry_token = None
            # Raise outside the exception handler so even explicit inspection
            # of __context__ cannot recover a token-bearing HTTP request.
            raise RuntimeError("MCP SSE cancellation request failed") from None

        def initialization_notification(request: Any) -> bool:
            if bridge_kind != "sse" or request.method != "POST":
                return False
            try:
                content = request.content
                if len(content) > _MAX_REMOTE_MESSAGE or b"notifications/initialized" not in content:
                    return False
                message = json.loads(content)
            except (AttributeError, TypeError, ValueError, RecursionError, http.RequestNotRead):
                return False
            return (isinstance(message, dict) and message.get("jsonrpc") == "2.0" and
                    message.get("method") == "notifications/initialized" and "id" not in message)

        async def close_initialization_on_cancel(operation: asyncio.Task[Any]) -> None:
            # MCP's SSE writer cancels pending POSTs during shutdown. If that
            # happens while AnyIO is opening a socket, the socket can be lost.
            # Finish this side-effect-free notification briefly, then cancel.
            import anyio
            def dispose_completed(task: asyncio.Task[Any]) -> None:
                if task.cancelled():
                    return
                try:
                    response = task.result()
                except BaseException:
                    return
                async def close_response() -> None:
                    with contextlib.suppress(Exception):
                        await response.aclose()
                closing = asyncio.create_task(close_response())
                _RESPONSE_CLOSE_TASKS.add(closing)
                closing.add_done_callback(_RESPONSE_CLOSE_TASKS.discard)

            disposed = False
            try:
                with anyio.CancelScope(shield=True):
                    with anyio.move_on_after(_SSE_INITIALIZATION_SHUTDOWN_TIMEOUT):
                        try:
                            await asyncio.shield(operation)
                        except Exception:
                            pass
                    if not operation.done():
                        operation.cancel()
                        await asyncio.wait({operation}, timeout=1.0)
                    if operation.done():
                        close_timed_out = False
                        if not operation.cancelled():
                            try:
                                response = operation.result()
                            except BaseException:
                                pass
                            else:
                                with anyio.move_on_after(1.0) as close_scope:
                                    with contextlib.suppress(Exception):
                                        await response.aclose()
                                close_timed_out = close_scope.cancel_called
                        disposed = not close_timed_out
            finally:
                if not disposed:
                    if not operation.done():
                        operation.cancel()
                    operation.add_done_callback(dispose_completed)

        class LimitedStream(http.AsyncByteStream):
            def __init__(self, stream: Any, *, sse: bool, decoder: McpContentDecoder) -> None:
                self.stream = stream
                self.limiter = _ResponseLimiter(sse=sse)
                self.decoder = decoder
                self.encoded_size = 0
            async def __aiter__(self) -> AsyncIterator[bytes]:
                async for chunk in self.stream:
                    if not self.limiter.sse:
                        self.encoded_size += len(chunk)
                        if self.encoded_size > _MAX_REMOTE_MESSAGE:
                            raise ValueError("Encoded remote MCP response exceeds the message limit")
                    for decoded in self.decoder.feed(chunk):
                        # An expanding SSE stream can contain many tiny events.
                        # Yield to cancellation even if the parser keeps skipping
                        # empty events and no additional network read is needed.
                        await asyncio.sleep(0)
                        if request_guard is not None and await request_guard() is not True:
                            raise PermissionError("Remote MCP response is not authorized")
                        self.limiter.feed(decoded)
                        if decoded:
                            yield decoded
                if request_guard is not None and await request_guard() is not True:
                    raise PermissionError("Remote MCP response is not authorized")
                self.decoder.finish()
            async def aclose(self) -> None:
                await self.stream.aclose()

        class LimitedTransport(http.AsyncBaseTransport):
            def __init__(self, *, capture_sse: bool = True) -> None:
                self.transport = http.AsyncHTTPTransport(trust_env=False)
                self.capture_sse = capture_sse
            async def handle_async_request(self, request: Any) -> Any:
                if request_guard is not None and await request_guard() is not True:
                    raise PermissionError("Remote MCP request is not authorized")
                if oauth is not None and _origin(str(request.url)) not in oauth_origins:
                    raise ValueError("OAuth request origin is not allowed")
                # Auth flows can create fresh Request instances instead of using
                # AsyncClient's defaults. Negotiate only our bounded decoders.
                request.headers["Accept-Encoding"] = ACCEPT_ENCODING
                # OAuth libraries can yield fresh HTTP requests that bypass the
                # client's normal timeout injection.
                request.extensions.setdefault("timeout", {
                    "connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0})
                if dispatch is not None and request.method == "POST":
                    try:
                        content = request.content
                    except (AttributeError, http.RequestNotRead):
                        # Redirects and OAuth retries may rebuild a buffered POST
                        # as a stream. Buffer incrementally, with the same cap and
                        # authorization checks as the original dispatch.
                        buffered = bytearray()
                        async for chunk in request.stream:
                            if len(chunk) > _MAX_REMOTE_MESSAGE - len(buffered):
                                raise ValueError("Remote MCP POST exceeds the message limit")
                            buffered.extend(chunk)
                            if request_guard is not None and await request_guard() is not True:
                                raise PermissionError("Remote MCP request is not authorized")
                            await asyncio.sleep(0)
                        request.stream = http.ByteStream(bytes(buffered))
                        content = await request.aread()
                    if len(content) > _MAX_REMOTE_MESSAGE:
                        raise ValueError("Remote MCP POST exceeds the message limit")
                    message = parse_message(content)
                    if (message is None and
                            request.headers.get("content-type", "").split(";", 1)[0].lower() == "application/json"):
                        raise ValueError("Cannot correlate remote MCP JSON POST")
                    if message is not None and not dispatch.begin(message):
                        # The library may have queued this call or reconstructed
                        # it after OAuth. Complete locally without a tool POST.
                        if bridge_kind == "sse":
                            return http.Response(202, content=b"")
                        return http.Response(200, json={"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32800, "message": "MCP request cancelled before dispatch"}})
                if self.capture_sse:
                    remember_sse_endpoint(request)
                if initialization_notification(request):
                    operation = asyncio.create_task(self.transport.handle_async_request(request))
                    try:
                        response = await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        await close_initialization_on_cancel(operation)
                        raise
                else:
                    response = await self.transport.handle_async_request(request)
                encoding = response.headers.get("content-encoding", "").strip().lower()
                try:
                    # HEAD/204/304 have no HTTP response body even when their
                    # headers describe an encoded representation.
                    no_body = request.method == "HEAD" or response.status_code in (204, 304)
                    decoder = McpContentDecoder("" if no_body else encoding)
                except ValueError:
                    await response.aclose()
                    raise
                response_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                accepted = {part.split(";", 1)[0].strip().lower()
                            for part in request.headers.get("accept", "").split(",")}
                # OAuth/discovery clients buffer their bodies. A server cannot
                # switch those requests to unbounded event-wise accounting just
                # by labeling a JSON-like response as an SSE stream.
                sse = response_type == "text/event-stream" and "text/event-stream" in accepted
                response.stream = LimitedStream(response.stream, sse=sse, decoder=decoder)
                if decoder.compressed or no_body:
                    # httpx/httpx2 must not decompress our already decoded bytes.
                    # The encoded Content-Length is no longer a decoded length.
                    response.headers.pop("content-encoding", None)
                    response.headers.pop("content-length", None)
                return response
            async def aclose(self) -> None:
                await self.transport.aclose()

        async def limit_session_termination(request: Any) -> None:
            if request.method == "DELETE":
                request.extensions["timeout"] = {"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}
        async def reject_termination_redirect(response: Any) -> None:
            if response.request.method == "DELETE" and 300 <= response.status_code < 400:
                raise RuntimeError("MCP session termination redirect refused")
        def client_factory(*, headers: Any = None, timeout: Any = None, auth: Any = None) -> Any:
            if timeout is None:
                timeout = http.Timeout(30, read=300)
            safe_headers = {**(headers or {}), "Accept-Encoding": ACCEPT_ENCODING}
            return http.AsyncClient(headers=safe_headers, timeout=timeout, auth=auth,
                                    follow_redirects=False, trust_env=False,
                                    transport=LimitedTransport(),
                                    event_hooks={"request": [limit_session_termination],
                                                 "response": [reject_termination_redirect]})
        async with AsyncExitStack() as stack:
            owned_streams: list[Any] = []
            async def close_streams() -> None:
                for stream in owned_streams:
                    with contextlib.suppress(Exception):
                        await stream.aclose()
            # Register before the transport so it exits before its receive ends.
            stack.push_async_callback(close_streams)
            if self.kind == "http":
                client = await stack.enter_async_context(client_factory(headers=self.headers, auth=oauth))
                transport_options: dict[str, Any] = {"http_client": client}
                if hasattr(streamable_http, "DEFAULT_MAX_SSE_EVENT_SIZE"):
                    transport_options["max_sse_event_size"] = _MAX_REMOTE_MESSAGE
                streams = await stack.enter_async_context(
                    streamable_http.streamable_http_client(self.url, **transport_options))
            else:
                from mcp.client.sse import sse_client
                owner = asyncio.current_task()
                assert owner is not None
                def expire() -> None:
                    if owner not in self._cancelling:
                        self._cancelling.add(owner)
                        owner.cancel()
                timer = (asyncio.get_running_loop().call_later(self.connect_timeout, expire)
                         if self.connect_timeout is not None else None)
                try:
                    streams = await stack.enter_async_context(
                        sse_client(self.url, headers=self.headers, auth=oauth, httpx_client_factory=client_factory))
                finally:
                    if timer is not None:
                        timer.cancel()
                if sse_cancel_setter is not None:
                    sse_cancel_setter(send_sse_cancellation)
            owned_streams.extend(streams[:2])
            yield streams[0], streams[1]

    async def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        owner = asyncio.current_task()
        assert owner is not None
        self.connections.add(owner)
        cancel_tools = None
        dispatch = RemoteDispatch()
        try:
            if self._closed:
                return
            token = await asyncio.wait_for(reader.readline(), 5)
            if self._closed or not secrets.compare_digest(token.decode().strip(), self.token):
                return
            import mcp.types
            from mcp.shared.message import SessionMessage
            sse_cancel: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None

            def set_sse_cancel(sender: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
                nonlocal sse_cancel
                sse_cancel = sender

            async with self._remote_streams(sse_cancel_setter=set_sse_cancel, dispatch=dispatch) as (from_server, to_server):
                write_lock = asyncio.Lock()
                state_lock = asyncio.Lock()
                backend_lock = asyncio.Lock()
                observed = ObservedToolCalls()

                def parse_wire(wire: Any) -> Any:
                    if hasattr(mcp.types, "jsonrpc_message_adapter"):
                        return mcp.types.jsonrpc_message_adapter.validate_python(wire, by_name=False)
                    return mcp.types.JSONRPCMessage.model_validate(wire)

                async def send_client(data: bytes) -> None:
                    async with write_lock:
                        writer.write(data)
                        await writer.drain()

                async def send_wire(wire: dict[str, Any]) -> None:
                    await send_client((json.dumps(wire, ensure_ascii=False, allow_nan=False) + "\n").encode())

                async def send_backend(wire: dict[str, Any]) -> None:
                    dispatch.admit(wire)
                    await to_server.send(SessionMessage(parse_wire(wire)))

                def fail() -> None:
                    if owner not in self._cancelling:
                        self._cancelling.add(owner)
                        owner.cancel()

                tracker = (McpOutputTracker(self.output_transform, send_wire, fail)
                           if self.output_transform is not None or self.input_transform is not None else None)
                gate = (McpInputGate(self.input_transform, tracker, send_wire, send_backend, backend_lock,
                                     lambda: self._tools_enabled and not self._tools_suspended, fail)
                        if self.input_transform is not None else None)

                async def from_client() -> None:
                    while line := await reader.readline():
                        async with backend_lock:
                            if not self._tools_enabled or self._tools_suspended:
                                blocked, reply, ident = self._classify(line)
                                if blocked:
                                    if reply:
                                        response = {"jsonrpc": "2.0", "id": ident,
                                                    "error": {"code": -32800,
                                                              "message": "MCP server tools disabled"}}
                                        await send_client((json.dumps(response, ensure_ascii=False) + "\n").encode())
                                    continue
                            wire = parse_message(line) if tracker is not None else json.loads(line)
                            if wire is None or (tracker is not None and "id" in wire and _request_key(wire["id"]) is None):
                                raise ValueError("Invalid MCP request")
                            parsed = parse_wire(wire)
                            cancel_upstream = None
                            cancel_locally = None
                            async with state_lock:
                                # Track the object the MCP adapter actually forwards.
                                normalized = parsed.model_dump(by_alias=True, mode="json", exclude_none=True)
                                if (tracker is not None and "id" in wire and
                                        _request_key(normalized.get("id")) != _request_key(wire["id"])):
                                    raise ValueError("Cannot correlate MCP request")
                                if normalized.get("method") == "notifications/cancelled":
                                    params = normalized.get("params")
                                    if isinstance(params, dict):
                                        ident = params.get("requestId")
                                        cancel_upstream = dispatch.cancel(ident)
                                        if tracker is None and _request_key(ident) is not None and ident in observed.ids():
                                            cancel_locally = ident
                                if tracker is not None:
                                    if await tracker.reject_cancelled_id(normalized):
                                        continue
                                    if gate is not None:
                                        if normalized.get("method") == "notifications/cancelled":
                                            if await gate.cancel_notification(normalized):
                                                continue
                                        await gate.check_available(normalized)
                                        if normalized.get("method") == "tools/call":
                                            await gate.submit(normalized)
                                            continue
                                    if not await tracker.request(normalized):
                                        continue
                                    accepted = True
                                else:
                                    accepted = observed.request(normalized)
                            if not accepted:
                                response = {"jsonrpc": "2.0", "id": normalized.get("id"),
                                            "error": {"code": -32600,
                                                      "message": "MCP request ID cannot be reused after cancellation"}}
                                await send_client((json.dumps(response, ensure_ascii=False) + "\n").encode())
                                continue
                            if cancel_locally is not None:
                                await send_wire({"jsonrpc": "2.0", "id": cancel_locally,
                                    "error": {"code": -32800, "message": "MCP request cancelled locally"}})
                            if cancel_upstream is False:
                                continue
                            dispatch.admit(normalized)
                            await to_server.send(SessionMessage(parsed))

                async def from_remote() -> None:
                    async for item in from_server:
                        if isinstance(item, Exception):
                            raise RuntimeError("Remote MCP transport failed")
                        wire = item.message.model_dump(by_alias=True, mode="json", exclude_none=True)
                        async with state_lock:
                            dispatch.response(wire)
                            if tracker is not None:
                                await tracker.response(wire)
                                accepted = False
                            else:
                                accepted = observed.response(wire)
                        if not accepted:
                            continue
                        await send_client((json.dumps(wire, ensure_ascii=False) + "\n").encode())
                    if tracker is not None:
                        await tracker.drain()

                async def cancel_observed_tools() -> None:
                    # Local completion and the dedicated SSE cancellation HTTP
                    # request must not wait for the ordinary POST writer lock.
                    if gate is not None:
                        for ident in await gate.tool_ids():
                            await gate.cancel(ident)
                    async with state_lock:
                        identifiers = await tracker.tool_ids() if tracker is not None else observed.ids()
                    for ident in identifiers:
                        upstream = dispatch.cancel(ident)
                        if tracker is not None:
                            await tracker.cancel(ident)
                        else:
                            async with state_lock:
                                suppressed = observed.suppress(ident)
                            if suppressed:
                                await send_wire({"jsonrpc": "2.0", "id": ident,
                                    "error": {"code": -32800, "message": "MCP request cancelled locally"}})
                        if not upstream:
                            continue
                        notice = cancellation_notice(ident)
                        if self.kind == "sse":
                            if sse_cancel is None:
                                raise RuntimeError("MCP SSE cancellation endpoint is unavailable")
                            await sse_cancel(notice)
                        else:
                            await to_server.send(SessionMessage(parse_wire(notice)))

                cancel_tools = cancel_observed_tools
                self._tool_cancellers.add(cancel_tools)
                pumps = [asyncio.create_task(from_client()), asyncio.create_task(from_remote())]
                try:
                    done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                    self._cleaning.add(owner)
                    for task in done:
                        await task
                finally:
                    self._cleaning.add(owner)
                    for task in pumps:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
                    if gate is not None:
                        await gate.close()
                    if tracker is not None:
                        await tracker.close()
        except (Exception, asyncio.CancelledError):
            # The Kiro side observes a closed server; never log upstream headers or URLs here.
            pass
        finally:
            dispatch.close()
            self._cleaning.add(owner)
            if cancel_tools is not None:
                self._tool_cancellers.discard(cancel_tools)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.connections.discard(owner)
            self._cleaning.discard(owner)
            self._cancelling.discard(owner)
