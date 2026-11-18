"""Opt-in OAuth authorization code flow for SDK-relayed remote MCP servers."""
from __future__ import annotations

import asyncio
import ipaddress
import hmac
import json
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qs, urljoin, urlsplit


def _origin(url: str, *, origin_only: bool = False) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("Invalid OAuth URL")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None or
                parsed.password is not None or parsed.fragment or
                (origin_only and (parsed.path not in ("", "/") or parsed.query))):
            raise ValueError("Invalid OAuth URL")
        host = host.encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.port == 0:
            raise ValueError("Invalid OAuth URL")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid OAuth URL") from exc
    if parsed.scheme == "http":
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise ValueError("OAuth requires HTTPS except for loopback HTTP")
    return parsed.scheme, host, port


class InMemoryMcpOAuthStorage:
    """Process-local MCP OAuth credentials; use a protected custom store for persistence."""

    def __init__(self) -> None:
        self._tokens: Any = None
        self._client_info: Any = None

    async def get_tokens(self) -> Any:
        return self._tokens

    async def set_tokens(self, tokens: Any) -> None:
        self._tokens = tokens

    async def clear_tokens(self) -> None:
        self._tokens = None

    async def get_client_info(self) -> Any:
        return self._client_info

    async def set_client_info(self, client_info: Any) -> None:
        self._client_info = client_info


@dataclass
class McpOAuthConfig:
    """Browser authorization for one remote MCP server.

    The callback must return the complete redirect URL received by the application.
    """

    redirect_uri: str
    authorize: Optional[Callable[[str], Awaitable[str]]] = field(repr=False)
    allowed_origins: list[str] = field(default_factory=list)
    client_name: str = "kiro-agent-sdk"
    scope: Optional[str] = None
    storage: Any = field(default_factory=InMemoryMcpOAuthStorage, repr=False)
    sse_cancel_token_provider: Optional[Callable[[], Awaitable[str]]] = field(default=None, repr=False)
    sse_cancel_retry_with_stored_token: bool = False

    def _create_provider(self, server_url: str) -> tuple[Any, set[tuple[str, str, int]]]:
        server_origin = _origin(server_url)
        if not isinstance(self.allowed_origins, list):
            raise ValueError("OAuth allowed_origins must be a list")
        origins = {server_origin, *(_origin(url, origin_only=True) for url in self.allowed_origins)}
        redirect = urlsplit(self.redirect_uri)
        _origin(self.redirect_uri)
        if redirect.query or redirect.fragment:
            raise ValueError("OAuth redirect_uri must not have a query or fragment")
        if not callable(self.authorize):
            raise ValueError("OAuth authorize must be an async callback")
        if self.sse_cancel_token_provider is not None and not callable(self.sse_cancel_token_provider):
            raise ValueError("sse_cancel_token_provider must be an async callback")
        if type(self.sse_cancel_retry_with_stored_token) is not bool:
            raise ValueError("sse_cancel_retry_with_stored_token must be a boolean")
        if self.sse_cancel_retry_with_stored_token and self.sse_cancel_token_provider is not None:
            raise ValueError("Choose one SSE cancellation token retry source")
        if not isinstance(self.client_name, str) or not self.client_name:
            raise ValueError("OAuth client_name must be nonempty")
        if self.scope is not None and not isinstance(self.scope, str):
            raise ValueError("OAuth scope must be a string")
        if any(not callable(getattr(self.storage, name, None)) for name in
               ("get_tokens", "set_tokens", "get_client_info", "set_client_info")):
            raise ValueError("OAuth storage does not implement the MCP TokenStorage protocol")

        from mcp.client.auth import OAuthClientProvider, oauth2
        from mcp.shared import auth
        from mcp.shared._httpx_utils import _AUTH_REDIRECT_LIMIT, next_request_within_origin
        from pydantic import ValidationError
        if type(_AUTH_REDIRECT_LIMIT) is not int or not 0 <= _AUTH_REDIRECT_LIMIT <= 32:
            raise ValueError("Unsupported MCP OAuth redirect limit")

        class RedactedOAuthClientProvider(OAuthClientProvider):
            async def async_auth_flow(self, request: Any) -> Any:
                from .oauth_errors import sanitized_oauth
                flow = self._raw_auth_flow(request)
                response = outgoing = None
                try:
                    while True:
                        try:
                            outgoing = await sanitized_oauth(flow.asend(response), "OAuth authentication failed")
                        except StopAsyncIteration:
                            return
                        response = yield outgoing
                finally:
                    request = response = outgoing = None
                    try:
                        await sanitized_oauth(flow.aclose(), "OAuth authentication cleanup failed")
                    finally:
                        flow = None

            async def _raw_auth_flow(self, request: Any) -> Any:
                flow = super().async_auth_flow(request)
                registration = False
                redirects = 0
                try:
                    outgoing = await flow.__anext__()
                    while True:
                        metadata = self.context.oauth_metadata
                        endpoint = (str(metadata.registration_endpoint)
                                    if metadata is not None and metadata.registration_endpoint else
                                    urljoin(self.context.get_authorization_base_url(self.context.server_url),
                                            "/register"))
                        if (not registration and outgoing is not request and outgoing.method == "POST" and
                                str(outgoing.url) == str(type(outgoing.url)(endpoint))):
                            registration = True
                            redirects = 0
                        response = yield outgoing
                        if registration:
                            follow = next_request_within_origin(response)
                            if follow is not None and redirects < _AUTH_REDIRECT_LIMIT:
                                redirects += 1
                            else:
                                if response.status_code not in (200, 201):
                                    raise ValueError("OAuth client registration failed")
                                try:
                                    body = json.loads(await response.aread())
                                    if isinstance(body, dict):
                                        body.pop("issuer", None)
                                    client_info = auth.OAuthClientInformationFull.model_validate(body)
                                    check_usable = getattr(oauth2, "check_registration_usable", None)
                                    if check_usable is not None:
                                        check_usable(client_info)
                                    elif (client_info.token_endpoint_auth_method not in
                                          (None, "none", "client_secret_basic", "client_secret_post") or
                                          (client_info.token_endpoint_auth_method in
                                           ("client_secret_basic", "client_secret_post") and
                                           not client_info.client_secret)):
                                        raise ValueError("Unsupported client registration")
                                except Exception:
                                    raise ValueError("OAuth client registration failed") from None
                                registration = False
                        try:
                            outgoing = await flow.asend(response)
                        except StopAsyncIteration:
                            return
                finally:
                    await flow.aclose()

            async def _perform_authorization(self) -> Any:
                from .oauth_errors import sanitized_oauth
                return await sanitized_oauth(super()._perform_authorization(), "OAuth authorization failed")

            async def _handle_token_response(self, response: Any) -> None:
                from .oauth_errors import sanitized_oauth
                try:
                    await sanitized_oauth(super()._handle_token_response(response), "OAuth token exchange failed")
                finally:
                    response = None

            async def _handle_refresh_response(self, response: Any) -> bool:
                from .oauth_errors import sanitized_oauth
                try:
                    return await sanitized_oauth(self._refresh_response(response), "OAuth token refresh failed")
                finally:
                    response = None

            async def _refresh_response(self, response: Any) -> bool:
                if response.status_code != 200:
                    # The library's warning can include an untrusted redirect URL.
                    self.context.clear_tokens()
                    return False
                try:
                    body = await response.aread()
                    auth.OAuthToken.model_validate_json(body)
                except ValidationError:
                    # The library logs validation inputs, which may contain tokens.
                    self.context.clear_tokens()
                    return False
                except Exception:
                    raise ValueError("OAuth token refresh failed") from None
                try:
                    return await super()._handle_refresh_response(response)
                except Exception:
                    raise ValueError("OAuth token refresh failed") from None

        authorize = self.authorize
        redirect_uri = self.redirect_uri
        result: Any = None

        async def redirect_handler(url: str) -> None:
            from .oauth_errors import sanitized_oauth
            try:
                await sanitized_oauth(raw_redirect_handler(url), "OAuth authorization callback failed")
            finally:
                url = ""

        async def raw_redirect_handler(url: str) -> None:
            nonlocal result
            if _origin(url) not in origins:
                raise ValueError("OAuth authorization endpoint origin is not allowed")
            try:
                requested = parse_qs(urlsplit(url).query, keep_blank_values=True, max_num_fields=32)
            except ValueError:
                raise ValueError("Invalid OAuth authorization URL") from None
            if len(requested.get("state", [])) != 1 or not requested["state"][0]:
                raise ValueError("Invalid OAuth authorization state")
            try:
                returned = await authorize(url)
            except Exception:
                raise ValueError("OAuth authorization callback failed") from None
            if not isinstance(returned, str) or len(returned) > 16384:
                raise ValueError("Invalid OAuth callback URL")
            try:
                parsed = urlsplit(returned)
                matching = (_origin(returned) == _origin(redirect_uri) and
                            parsed.path == redirect.path and not parsed.fragment)
                fields = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
            except ValueError as exc:
                raise ValueError("Invalid OAuth callback URL") from exc
            if not matching or "error" in fields:
                raise ValueError("OAuth authorization failed or callback URI does not match")
            if (len(fields.get("code", [])) != 1 or not fields["code"][0] or
                    len(fields.get("state", [])) != 1 or not fields["state"][0] or
                    len(fields.get("iss", [])) > 1):
                raise ValueError("Invalid OAuth authorization response")
            try:
                if not hmac.compare_digest(fields["state"][0].encode(), requested["state"][0].encode()):
                    raise ValueError("OAuth authorization state does not match")
                issuer = fields.get("iss", [None])[0]
                metadata = provider.context.oauth_metadata
                if issuer is not None and (metadata is None or
                        not hmac.compare_digest(issuer.encode(), str(metadata.issuer).encode())):
                    raise ValueError("OAuth authorization issuer does not match")
                if (issuer is None and metadata is not None and
                        getattr(metadata, "authorization_response_iss_parameter_supported", False) is True):
                    raise ValueError("OAuth authorization issuer is required")
            except UnicodeError:
                raise ValueError("Invalid OAuth authorization response") from None
            if hasattr(auth, "AuthorizationCodeResult"):
                result = auth.AuthorizationCodeResult(code=fields["code"][0], state=fields["state"][0],
                                                      iss=issuer)
            else:
                result = (fields["code"][0], fields["state"][0])

        async def callback_handler() -> Any:
            nonlocal result
            if result is None:
                raise ValueError("OAuth callback was not received")
            value, result = result, None
            return value

        provider = RedactedOAuthClientProvider(
            server_url=server_url,
            client_metadata=auth.OAuthClientMetadata(
                redirect_uris=[redirect_uri], client_name=self.client_name,
                scope=self.scope, token_endpoint_auth_method="none"),
            storage=self.storage, redirect_handler=redirect_handler, callback_handler=callback_handler)
        return provider, origins


async def preauthorize_mcp_oauth(
    server: dict[str, Any], oauth: McpOAuthConfig, *, timeout: Optional[float] = 300.0,
) -> None:
    """Initialize a remote MCP connection before starting Kiro, retaining OAuth state in ``oauth``.

    The server may permit anonymous access, so a successful initialization does not
    guarantee that an OAuth grant was issued. Use the same configuration and storage
    for the subsequent client, without concurrent authorization on that storage.
    """
    if not isinstance(server, dict) or not isinstance(oauth, McpOAuthConfig):
        raise ValueError("A remote MCP server configuration and McpOAuthConfig are required")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                                not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("OAuth preauthorization timeout must be positive and finite, or None")

    from .errors import CLIConnectionError
    from .remote_mcp import RemoteMcpBridge
    from mcp import ClientSession

    bridge = RemoteMcpBridge(server, oauth=oauth, connect_timeout=None)

    async def initialize() -> None:
        async with bridge._remote_streams() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

    try:
        if timeout is None:
            await initialize()
        else:
            await asyncio.wait_for(initialize(), timeout)
    except asyncio.TimeoutError:
        raise
    except Exception:
        pass
    else:
        return
    bridge = None
    initialize = None
    # Underlying transport errors can include authorization URLs or responses.
    raise CLIConnectionError("MCP OAuth preauthorization failed")
