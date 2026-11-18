"""HTTP adapter for Amazon Bedrock AgentCore Runtime and compatible containers."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import inspect
import json
import logging
import lzma
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .client import (KiroSDKClient, _normalize_context_usage, _normalize_hook_listing_filters,
                     _prompt_content)
from .background_tasks import (BackgroundTaskError, BackgroundTaskEventPage, BackgroundTaskManager,
                               BackgroundTaskResult, BackgroundTaskStatus)
from .background_events import _MESSAGES, encode_structured_result
from .runtime_usage import validate_normalized_usage_report
from .background_result_store import MAX_BACKGROUND_SNAPSHOT_BYTES
from .device_auth import DeviceLogin, cli_auth_command
from .errors import PromptBlockedError, ProtocolError
from .types import (KiroAgentOptions, PermissionResultAllow, PermissionResultDeny,
                    ResultMessage, StreamEvent)
from .mcp_oauth import InMemoryMcpOAuthStorage, McpOAuthConfig, _origin, preauthorize_mcp_oauth
from .mcp_status import McpStatusTracker
from .remote_mcp import RemoteMcpBridge
from .session_store import InMemorySessionStore
from .sqlite_store import SQLiteSessionStore
from .sessions import _messages_from_mirror_entries

logger = logging.getLogger(__name__)
_SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
_KIRO_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_INTERACTION_RE = re.compile(r"^[0-9a-f]{32}$")
_SDK_BACKGROUND_TASK_RE = re.compile(r"^sdk-task-[0-9a-f]{32}$")
_MAX_BACKGROUND_START_REQUESTS = 4096
_MAX_BACKGROUND_EVENT_STREAMS = 4
_MAX_PENDING_INTERACTIONS = 16
_MAX_INTERACTION_BYTES = 256 * 1024
_MAX_RUNTIME_NODE_BYTES = 256 * 1024 * 1024
_MAX_RUNTIME_NODE_DECODER_MEMORY = 64 * 1024 * 1024
_MAX_INTROSPECTION_BYTES = 1024 * 1024
_MAX_INTROSPECTION_NODES = 20000
OptionsFactory = Callable[[Path, Optional[str]], KiroAgentOptions]
SessionAuthorizer = Callable[[Request, str], Awaitable[bool]]
KiroTokenProvider = Callable[[], Awaitable[dict[str, str]]]
KiroTokenProviderFactory = Callable[[Path, str], KiroTokenProvider]
KiroIdleTokenAuthorizer = Callable[[str], Awaitable[bool]]
BackgroundTasksFactory = Callable[[Path, str], BackgroundTaskManager]
BackgroundTaskOptionsFactory = Callable[[Path, str], KiroAgentOptions]
_MAX_BACKGROUND_CLAIMS_IN_FLIGHT = 16
_MAX_BACKGROUND_ARCHIVE_OPERATIONS = 16
_MAX_SESSION_HISTORY_OPERATIONS = 8
_MAX_SESSION_RECOVERY_OPERATIONS = 8
_MAX_BACKGROUND_ARCHIVE_WATCHERS = 128
_BACKGROUND_ARCHIVE_TIMEOUT = object()
_SESSION_HISTORY_TIMEOUT = object()
_SESSION_RECOVERY_TIMEOUT = object()
_BACKGROUND_EVENT_TYPES = frozenset(item.__name__ for item in _MESSAGES)
_BACKGROUND_AUTH_ENV = ("KIRO_API_KEY", "HOME", "KIRO_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                        "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "TMPDIR", "KIRO_CHAT_LOG_FILE",
                        "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
                        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                        "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
                        "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE")
_PROMPT_CAPABILITIES = {"agentCapabilities": {"promptCapabilities": {
    "image": True, "embeddedContext": True}}}


class _RequestBodyTooLarge(Exception):
    pass


@contextlib.asynccontextmanager
async def _aclosing(iterator):
    try:
        yield iterator
    finally:
        await iterator.aclose()


def _preauthorization_overrides(servers: Any, enabled: bool) -> dict[str, dict[str, str]]:
    if (not isinstance(servers, dict) or len(servers) > 32 or
            (servers and not enabled) or any(
                not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 256 or
                any(ord(char) < 32 or ord(char) == 127 for char in name) or
                not isinstance(server, dict) or set(server) != {"type", "url"} or
                server.get("type") not in ("http", "sse") or
                not isinstance(server.get("url"), str) or not server["url"]
                for name, server in servers.items())):
        raise ValueError("Invalid MCP OAuth preauthorization server overrides")
    for server in servers.values():
        _origin(server["url"])
    return copy.deepcopy(servers)


@dataclass(frozen=True)
class AgentCoreConfig:
    """Server settings. Credentials are supplied by the host, never by requests."""

    state_dir: Path = Path("/tmp/kiro-agentcore")
    cli_path: str = "kiro-cli"
    agent_engine: Literal["v2", "v3"] = "v3"
    auth_mode: Literal["api-key", "cli", "device"] = "api-key"
    api_key: Optional[str] = field(default=None, repr=False)
    secret_id: Optional[str] = None
    secret_json_key: Optional[str] = None
    device_license: Literal["free", "pro"] = "free"
    device_start_url: Optional[str] = None
    device_region: Optional[str] = None
    device_timeout: float = 300.0
    model: Optional[str] = None
    runtime_cache_dir: Optional[Path] = None
    startup_timeout: float = 120.0
    turn_timeout: float = 300.0
    max_sessions: int = 32
    max_prompt_bytes: int = 1024 * 1024
    max_body_bytes: int = 2 * 1024 * 1024
    # Serve without session_authorizer: every caller that can invoke the runtime may use every session.
    allow_unauthorized_sessions: bool = False
    allow_sdk_events: bool = False
    allow_interactions: bool = False
    interaction_timeout: float = 120.0
    session_authorization_timeout: float = 10.0
    allow_mcp_oauth_interactions: bool = False
    allow_command_introspection: bool = False
    allow_hook_introspection: bool = False
    allow_session_introspection: bool = False
    allow_session_history: bool = False
    allow_session_history_tools: bool = False
    allow_active_introspection: bool = False
    allow_prompt_block_details: bool = False
    reauthorize_output: bool = False
    allow_background_task_results: bool = False
    allow_background_task_events: bool = False
    allow_background_task_event_stream: bool = False
    background_task_event_stream_timeout: float = 60.0
    background_request_claim_timeout: float = 5.0
    background_result_store_timeout: float = 5.0
    auto_archive_background_results: bool = False
    archive_background_task_events: bool = False
    allow_background_task_retry: bool = False
    allow_background_event_checkpoints: bool = False
    background_event_checkpoint_interval: Optional[float] = None
    preauthorize_mcp_oauth: bool = False
    mcp_oauth_preauthorize_servers: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    child_env_mode: str = "minimal"

    def __post_init__(self) -> None:
        if self.child_env_mode not in ("minimal", "inherit"):
            raise ValueError("child_env_mode must be minimal or inherit")
        for name in ("state_dir", "runtime_cache_dir"):
            value = getattr(self, name)
            if name == "state_dir" and value is None:
                raise ValueError("state_dir must be a filesystem path")
            if value is not None:
                try:
                    path = Path(value)
                except TypeError as exc:
                    raise ValueError(f"{name} must be a filesystem path") from exc
                if "\0" in str(path):
                    raise ValueError(f"{name} must not contain NUL")
                object.__setattr__(self, name, path)
        if self.agent_engine not in ("v2", "v3"):
            raise ValueError("agent_engine must be v2 or v3")
        if self.auth_mode not in ("api-key", "cli", "device"):
            raise ValueError("auth_mode must be api-key, cli or device")
        if self.auth_mode == "device" and os.name != "posix":
            raise ValueError("device authentication requires a POSIX terminal")
        for name in ("allow_unauthorized_sessions",
                     "allow_sdk_events", "allow_interactions", "allow_mcp_oauth_interactions", "preauthorize_mcp_oauth",
                     "allow_background_task_results",
                     "allow_background_task_events",
                     "allow_background_task_event_stream",
                     "auto_archive_background_results",
                     "archive_background_task_events",
                     "allow_background_event_checkpoints",
                     "allow_background_task_retry",
                     "allow_command_introspection", "allow_hook_introspection", "allow_session_introspection",
                     "allow_session_history", "allow_session_history_tools",
                     "allow_active_introspection", "allow_prompt_block_details", "reauthorize_output"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.allow_session_history_tools and not self.allow_session_history:
            raise ValueError("Session history tools require allow_session_history")
        if self.allow_background_task_events and not self.allow_sdk_events:
            raise ValueError("Background task events require allow_sdk_events")
        if self.allow_background_task_event_stream and not self.allow_background_task_events:
            raise ValueError("Background task event streams require allow_background_task_events")
        if self.archive_background_task_events and not self.allow_background_task_events:
            raise ValueError("Background event archive requires allow_background_task_events")
        if self.allow_background_event_checkpoints and not self.archive_background_task_events:
            raise ValueError("Background event checkpoints require archive_background_task_events")
        if self.background_event_checkpoint_interval is not None:
            interval = self.background_event_checkpoint_interval
            if (isinstance(interval, bool) or not isinstance(interval, (int, float)) or
                    not 0.1 <= interval <= 300):
                raise ValueError("background_event_checkpoint_interval must be between 0.1 and 300 seconds")
            if not self.allow_background_event_checkpoints or not self.auto_archive_background_results:
                raise ValueError("Automatic event checkpoints require checkpoints and automatic background archive")
        if (isinstance(self.background_task_event_stream_timeout, bool) or
                not isinstance(self.background_task_event_stream_timeout, (int, float)) or
                not 0 < self.background_task_event_stream_timeout <= 300):
            raise ValueError("background_task_event_stream_timeout must be positive and at most 300 seconds")
        if (isinstance(self.background_request_claim_timeout, bool) or
                not isinstance(self.background_request_claim_timeout, (int, float)) or
                not 0 < self.background_request_claim_timeout <= 60):
            raise ValueError("background_request_claim_timeout must be positive and at most 60 seconds")
        if (isinstance(self.background_result_store_timeout, bool) or
                not isinstance(self.background_result_store_timeout, (int, float)) or
                not 0 < self.background_result_store_timeout <= 60):
            raise ValueError("background_result_store_timeout must be positive and at most 60 seconds")
        if self.allow_mcp_oauth_interactions and not self.allow_interactions:
            raise ValueError("MCP OAuth interactions require allow_interactions")
        if self.preauthorize_mcp_oauth and not self.allow_mcp_oauth_interactions:
            raise ValueError("MCP OAuth preauthorization requires MCP OAuth interactions")
        object.__setattr__(self, "mcp_oauth_preauthorize_servers",
                           _preauthorization_overrides(self.mcp_oauth_preauthorize_servers,
                                                       self.preauthorize_mcp_oauth))
        if self.allow_mcp_oauth_interactions and self.agent_engine != "v3":
            raise ValueError("MCP OAuth interactions require Kiro V3")
        if self.api_key is not None and (not isinstance(self.api_key, str) or not self.api_key.strip() or "\0" in self.api_key):
            raise ValueError("api_key must be a nonempty string")
        if self.secret_id is not None and (not isinstance(self.secret_id, str) or not self.secret_id.strip() or "\0" in self.secret_id):
            raise ValueError("secret_id must be a nonempty string")
        if self.secret_json_key is not None:
            if not self.secret_id:
                raise ValueError("secret_json_key requires secret_id")
            if (not isinstance(self.secret_json_key, str) or not self.secret_json_key.strip() or
                    len(self.secret_json_key) > 256 or
                    any(ord(character) < 32 or ord(character) == 127 for character in self.secret_json_key)):
                raise ValueError("secret_json_key must be a nonempty string of at most 256 characters "
                                 "without control characters")
        if self.auth_mode == "api-key" and bool(self.api_key) == bool(self.secret_id):
            raise ValueError("api-key authentication requires exactly one API key or secret ID")
        if self.auth_mode in ("cli", "device") and (self.api_key is not None or self.secret_id is not None):
            raise ValueError("CLI and device authentication cannot be combined with API key settings")
        if self.device_license not in ("free", "pro"):
            raise ValueError("device_license must be free or pro")
        if self.auth_mode != "device" and (self.device_license != "free" or self.device_start_url or self.device_region):
            raise ValueError("device login settings require device authentication")
        if self.device_license == "pro":
            try:
                value = self.device_start_url
                url = urlsplit(value) if isinstance(value, str) else None
                valid_url = bool(isinstance(value, str) and value and value == value.strip() and len(value) <= 2048 and
                                 not any(ord(char) < 32 for char in value) and url.scheme == "https" and
                                 url.hostname and url.port in (None, 443) and url.username is None and
                                 url.password is None and not url.query and not url.fragment)
            except ValueError:
                valid_url = False
            if not valid_url or not isinstance(self.device_region, str) or not re.fullmatch(
                    r"[a-z]{2}(?:-[a-z0-9]+)+-\d+", self.device_region):
                raise ValueError("pro device login requires an HTTPS Identity Center start URL and region")
        elif self.device_start_url or self.device_region:
            raise ValueError("Identity Center settings require the pro license")
        for name in ("max_sessions", "max_prompt_bytes", "max_body_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("startup_timeout", "turn_timeout", "interaction_timeout", "session_authorization_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 < value < float("inf")):
                raise ValueError(f"{name} must be a positive finite number")
        if (isinstance(self.device_timeout, bool) or not isinstance(self.device_timeout, (int, float)) or
                not 0 < self.device_timeout <= 900):
            raise ValueError("device_timeout must be positive and at most 900 seconds")
        if not isinstance(self.cli_path, str) or not self.cli_path.strip() or "\0" in self.cli_path:
            raise ValueError("cli_path must be a nonempty string")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip() or
                                       len(self.model) > 256 or
                                       any(ord(character) < 32 or ord(character) == 127 for character in self.model)):
            raise ValueError("model must be None or a nonempty string of at most 256 characters "
                             "without control characters")

    @classmethod
    def from_env(cls) -> "AgentCoreConfig":
        """Read the documented KIRO_AGENTCORE_* settings from the environment."""
        def optional(name: str) -> Optional[str]:
            return os.environ.get(name) or None
        def flag(name: str) -> bool:
            value = os.environ.get(name, "false").lower()
            if value not in ("true", "false"):
                raise ValueError(f"{name} must be true or false")
            return value == "true"

        return cls(
            state_dir=Path(os.environ.get("KIRO_AGENTCORE_STATE_DIR", "/tmp/kiro-agentcore")),
            cli_path=os.environ.get("KIRO_AGENTCORE_CLI_PATH", "kiro-cli"),
            agent_engine=os.environ.get("KIRO_AGENTCORE_ENGINE", "v3"),
            auth_mode=os.environ.get("KIRO_AGENTCORE_AUTH_MODE", "api-key"),
            api_key=optional("KIRO_API_KEY"),
            secret_id=optional("KIRO_AGENTCORE_SECRET_ID"),
            secret_json_key=optional("KIRO_AGENTCORE_SECRET_JSON_KEY"),
            device_license=os.environ.get("KIRO_AGENTCORE_DEVICE_LICENSE", "free"),
            device_start_url=optional("KIRO_AGENTCORE_DEVICE_START_URL"),
            device_region=optional("KIRO_AGENTCORE_DEVICE_REGION"),
            device_timeout=float(os.environ.get("KIRO_AGENTCORE_DEVICE_TIMEOUT", "300")),
            model=optional("KIRO_AGENTCORE_MODEL"),
            runtime_cache_dir=(Path(value) if (value := optional("KIRO_AGENTCORE_RUNTIME_CACHE")) else None),
            startup_timeout=float(os.environ.get("KIRO_AGENTCORE_STARTUP_TIMEOUT", "120")),
            turn_timeout=float(os.environ.get("KIRO_AGENTCORE_TURN_TIMEOUT", "300")),
            max_sessions=int(os.environ.get("KIRO_AGENTCORE_MAX_SESSIONS", "32")),
            max_prompt_bytes=int(os.environ.get("KIRO_AGENTCORE_MAX_PROMPT_BYTES", str(1024 * 1024))),
            max_body_bytes=int(os.environ.get("KIRO_AGENTCORE_MAX_BODY_BYTES", str(2 * 1024 * 1024))),
            allow_unauthorized_sessions=flag("KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS"),
            allow_sdk_events=flag("KIRO_AGENTCORE_ALLOW_SDK_EVENTS"),
            allow_interactions=flag("KIRO_AGENTCORE_ALLOW_INTERACTIONS"),
            interaction_timeout=float(os.environ.get("KIRO_AGENTCORE_INTERACTION_TIMEOUT", "120")),
            session_authorization_timeout=float(os.environ.get("KIRO_AGENTCORE_SESSION_AUTHORIZATION_TIMEOUT", "10")),
            allow_mcp_oauth_interactions=flag("KIRO_AGENTCORE_ALLOW_MCP_OAUTH_INTERACTIONS"),
            preauthorize_mcp_oauth=flag("KIRO_AGENTCORE_PREAUTHORIZE_MCP_OAUTH"),
            allow_background_task_results=flag("KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_RESULTS"),
            allow_background_task_events=flag("KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_EVENTS"),
            allow_background_task_event_stream=flag("KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_EVENT_STREAM"),
            background_task_event_stream_timeout=float(os.environ.get("KIRO_AGENTCORE_BACKGROUND_TASK_EVENT_STREAM_TIMEOUT", "60")),
            background_request_claim_timeout=float(os.environ.get("KIRO_AGENTCORE_BACKGROUND_REQUEST_CLAIM_TIMEOUT", "5")),
            background_result_store_timeout=float(os.environ.get("KIRO_AGENTCORE_BACKGROUND_RESULT_STORE_TIMEOUT", "5")),
            auto_archive_background_results=flag("KIRO_AGENTCORE_AUTO_ARCHIVE_BACKGROUND_RESULTS"),
            archive_background_task_events=flag("KIRO_AGENTCORE_ARCHIVE_BACKGROUND_TASK_EVENTS"),
            allow_background_event_checkpoints=flag("KIRO_AGENTCORE_ALLOW_BACKGROUND_EVENT_CHECKPOINTS"),
            background_event_checkpoint_interval=(
                float(value) if (value := optional("KIRO_AGENTCORE_BACKGROUND_EVENT_CHECKPOINT_INTERVAL")) else None),
            allow_background_task_retry=flag("KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_RETRY"),
            child_env_mode=os.environ.get("KIRO_AGENTCORE_CHILD_ENV", "minimal"),
            allow_command_introspection=flag("KIRO_AGENTCORE_ALLOW_COMMAND_INTROSPECTION"),
            allow_hook_introspection=flag("KIRO_AGENTCORE_ALLOW_HOOK_INTROSPECTION"),
            allow_session_introspection=flag("KIRO_AGENTCORE_ALLOW_SESSION_INTROSPECTION"),
            allow_session_history=flag("KIRO_AGENTCORE_ALLOW_SESSION_HISTORY"),
            allow_session_history_tools=flag("KIRO_AGENTCORE_ALLOW_SESSION_HISTORY_TOOLS"),
            allow_active_introspection=flag("KIRO_AGENTCORE_ALLOW_ACTIVE_INTROSPECTION"),
            allow_prompt_block_details=flag("KIRO_AGENTCORE_ALLOW_PROMPT_BLOCK_DETAILS"),
            reauthorize_output=flag("KIRO_AGENTCORE_REAUTHORIZE_OUTPUT"),
        )


@dataclass
class _Interaction:
    interaction_id: str
    kind: str
    request: dict[str, Any]
    future: asyncio.Future
    size: int


@dataclass(frozen=True)
class _PermissionOAuthResponse:
    redirect_url: str = field(repr=False)
    config: AgentCoreConfig
    authorizer: SessionAuthorizer
    request: Request = field(repr=False)


class _DeviceAuthRequired(Exception):
    pass


class _InvocationPromptBlocked(Exception):
    """A user request, rather than an initial agent turn, was blocked."""
    def __init__(self, error: PromptBlockedError, session_id: str, client: KiroSDKClient, preset_generation: tuple[int, int]):
        super().__init__("Prompt blocked")
        self.error = error
        self.session_id = session_id
        self.client = client
        self.preset_generation = preset_generation


@dataclass
class _Session:
    runtime_id: str
    directory: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: Optional[KiroSDKClient] = None
    broken: bool = False
    last_used: float = field(default_factory=time.monotonic)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fork_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_ready: bool = False
    initializing: bool = False
    oauth_pins: int = 0
    control_failed: bool = False
    preprocessing_interrupted: bool = False
    clear_steering: bool = False
    interactions: dict[str, _Interaction] = field(default_factory=dict)
    device_login: Optional[DeviceLogin] = None
    background_tasks: Optional[BackgroundTaskManager] = None
    background_tasks_factory: Optional[BackgroundTasksFactory] = None
    background_start_requests: dict[str, tuple[str, str, BackgroundTaskOptionsFactory, Any, Optional[str]]] = field(default_factory=dict)
    background_event_streams: int = 0
    # Initial-prompt guard returned when a prewarm connected the client; the next turn adopts it.
    prewarm_guard: Optional[Callable[[], Awaitable[bool]]] = None


def _read_marker(path: Path, engine: str) -> Optional[tuple[str, bool]]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as source:
            data = source.read(4097)
    except BaseException:
        raise
    if len(data) > 4096:
        raise ValueError("Invalid persisted Kiro session marker")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid persisted Kiro session marker") from exc
    if (not isinstance(value, dict) or value.get("engine") != engine or
            not isinstance(value.get("session_id"), str) or
            not _KIRO_SESSION_RE.fullmatch(value["session_id"]) or
            not isinstance(value.get("steering_pending", False), bool)):
        raise ValueError("Invalid persisted Kiro session marker")
    return value["session_id"], value.get("steering_pending", False)


def _write_marker(path: Path, engine: str, session_id: str, steering_pending: bool = False) -> None:
    if not isinstance(session_id, str) or not _KIRO_SESSION_RE.fullmatch(session_id):
        raise ValueError("Kiro returned an invalid session ID")
    fd, name = tempfile.mkstemp(prefix=".session-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"engine": engine, "session_id": session_id, "steering_pending": steering_pending}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _copy_runtime_cache(source: Path, home: Path) -> None:
    """Copy only public executable cache files; never copy a credential store."""
    node, compressed = source / "node", source / "node.xz"
    if (not source.is_dir() or not (node.is_file() or compressed.is_file()) or
            not (source / "kas").is_dir()):
        raise ValueError("Runtime cache must contain node and kas")
    if node.is_file() and compressed.exists():
        raise ValueError("Runtime cache cannot contain both node and node.xz")
    target = home / ".local" / "share" / "kiro-cli"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("node", "node.sha256"):
        original, destination = source / name, target / name
        if (original.is_file() or name == "node" and compressed.is_file()) and not destination.exists():
            fd, temporary = tempfile.mkstemp(prefix=".runtime-", dir=str(target))
            os.close(fd)
            try:
                if name == "node" and not original.is_file():
                    if compressed.is_symlink() or (source / "node.sha256").is_symlink():
                        raise ValueError("Compressed runtime cache must use regular files")
                    with (source / "node.sha256").open("rb") as checksum:
                        checksum_bytes = checksum.read(129)
                    if len(checksum_bytes) > 128:
                        raise ValueError("Invalid compressed runtime node checksum")
                    expected = checksum_bytes.decode("ascii").strip()
                    if not re.fullmatch(r"[0-9a-f]{64}", expected):
                        raise ValueError("Invalid compressed runtime node checksum")
                    digest, size = hashlib.sha256(), 0
                    decoder = lzma.LZMADecompressor(
                        format=lzma.FORMAT_XZ, memlimit=_MAX_RUNTIME_NODE_DECODER_MEMORY)
                    with compressed.open("rb") as inp, open(temporary, "wb") as out:
                        while not decoder.eof:
                            data = inp.read(1024 * 1024) if decoder.needs_input else b""
                            if decoder.needs_input and not data:
                                raise ValueError("Compressed runtime node is truncated")
                            chunk = decoder.decompress(data, max_length=1024 * 1024)
                            size += len(chunk)
                            if size > _MAX_RUNTIME_NODE_BYTES:
                                raise ValueError("Compressed runtime node exceeds size limit")
                            digest.update(chunk)
                            out.write(chunk)
                        if decoder.unused_data or inp.read(1):
                            raise ValueError("Compressed runtime node has trailing data")
                    if not size or digest.hexdigest() != expected:
                        raise ValueError("Compressed runtime node failed integrity validation")
                    os.chmod(temporary, 0o755)
                else:
                    shutil.copy2(original, temporary)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    if not (target / "kas").exists():
        temporary_dir = Path(tempfile.mkdtemp(prefix=".kas-", dir=str(target)))
        try:
            shutil.copytree(source / "kas", temporary_dir / "kas", symlinks=True)
            os.replace(temporary_dir / "kas", target / "kas")
        finally:
            shutil.rmtree(temporary_dir, ignore_errors=True)


def _validate_prompt(prompt: Any, max_bytes: int) -> bool:
    """Validate the public HTTP subset before creating or resuming a session."""
    if isinstance(prompt, str):
        if not prompt.strip() or "\0" in prompt:
            raise ValueError("Invalid prompt")
        encoded = prompt.encode("utf-8")
    elif isinstance(prompt, list) and prompt:
        # Account for attachment bytes (including base64 expansion) and JSON
        # syntax, not only the text sent alongside an attachment.
        encoded = json.dumps(prompt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > max_bytes:
            return False
        meaningful = False
        for block in prompt:
            if not isinstance(block, dict):
                raise ValueError("Invalid content block")
            kind = block.get("type")
            if kind == "text":
                if (set(block) != {"type", "text"} or not isinstance(block["text"], str)
                        or "\0" in block["text"]):
                    raise ValueError("Invalid text block")
                meaningful = meaningful or bool(block["text"].strip())
            elif kind == "image" and "uri" not in block:
                meaningful = True
            elif (kind == "resource" and isinstance(block.get("resource"), dict) and
                  isinstance(block["resource"].get("uri"), str) and
                  block["resource"]["uri"].startswith("memory://")):
                meaningful = True
            else:
                # Resource links and resources that name local or remote URIs would let
                # HTTP callers point Kiro at files, so only inline content is accepted.
                raise ValueError("Unsupported content block")
        if not meaningful:
            raise ValueError("Empty prompt")
        # Actual server capabilities are checked again when the Kiro prompt is sent.
        _prompt_content(prompt, _PROMPT_CAPABILITIES)
    else:
        raise ValueError("Invalid prompt")
    return len(encoded) <= max_bytes


def _validate_introspection_result(value: Any, *, strict_builtins: bool = False) -> None:
    """Bound untrusted server metadata before returning it to a session owner."""
    pending = [(value, 0)]
    seen: set[int] = set()
    nodes = 0
    text_bytes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_INTROSPECTION_NODES or depth > 32:
            raise ValueError("Introspection response exceeds structural limits")
        if strict_builtins and type(item) not in (type(None), bool, str, int, float, dict, list):
            raise ValueError("Introspection response contains a non-JSON value")
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, str):
            if len(item) > _MAX_INTROSPECTION_BYTES:
                raise ValueError("Introspection response exceeds size limit")
            text_bytes += len(item.encode("utf-8"))
            if text_bytes > _MAX_INTROSPECTION_BYTES:
                raise ValueError("Introspection response exceeds size limit")
        elif isinstance(item, int):
            if item.bit_length() > 256:
                raise ValueError("Introspection response contains an oversized number")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("Introspection response contains a non-finite number")
        elif isinstance(item, (dict, list)):
            if id(item) in seen or len(item) > 1024:
                raise ValueError("Introspection response contains repeated or oversized containers")
            seen.add(id(item))
            if isinstance(item, dict):
                if any(not isinstance(key, str) or (strict_builtins and type(key) is not str) for key in item):
                    raise ValueError("Introspection response contains an invalid key")
                pending.extend((key, depth + 1) for key in item)
                pending.extend((child, depth + 1) for child in item.values())
            else:
                pending.extend((child, depth + 1) for child in item)
        else:
            raise ValueError("Introspection response contains a non-JSON value")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_INTROSPECTION_BYTES - 1024:
        raise ValueError("Introspection response exceeds size limit")


def _sdk_permission_mode_snapshot(client: KiroSDKClient) -> dict[str, Optional[str]]:
    mode = client.sdk_permission_mode
    if type(mode) is not str or mode not in ("ask", "denyUnlisted", "trustAll", "plan"):
        raise ValueError("Invalid SDK permission mode")
    return {"mode": mode}


def _requires_session_authorizer(client: Any) -> bool:
    return (getattr(getattr(client, "options", None), "runtime_compat_mode", "reject") == "v3" or
            getattr(client, "_native_hook_bridge_mode", "reject") == "runtime-acp" or
            getattr(client, "_resume_from_store_bounded_snapshot", (False, None))[0] is True or
            getattr(client, "_require_agentcore_background_authorization", False) is True or
            getattr(client, "_require_oauth_interaction_authorization", False) is True or
            getattr(client, "_client_auth_snapshot", ("cli",))[0] == "client" or
            getattr(client, "_hook_failure_snapshot", None) is not None or
            bool(getattr(client, "_mcp_failure_input_names", ())) or
            bool(getattr(client, "_mcp_failure_output_names", ())))


def _background_task_status(status: BackgroundTaskStatus) -> dict[str, Any]:
    if (type(status) is not BackgroundTaskStatus or type(status.task_id) is not str or
            not _SDK_BACKGROUND_TASK_RE.fullmatch(status.task_id) or
            status.state not in ("pending", "running", "cancelling", "completed", "failed", "cancelled") or
            (status.session_id is not None and
             (type(status.session_id) is not str or not _KIRO_SESSION_RE.fullmatch(status.session_id))) or
            (status.error_type is not None and
             (type(status.error_type) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", status.error_type))) or
            type(status.cleanup_pending) is not bool or
            type(status.events_received) is not int or type(status.events_dropped) is not int or
            not 0 <= status.events_dropped <= status.events_received or status.events_received.bit_length() > 64):
        raise ValueError("Invalid background task status")
    return asdict(status)


def _background_retained_data(retained: Any, result_limit: int, structured_retention: bool) -> Any:
    if retained is None:
        return None
    expected = {"result", "is_error", "structured_output"} if structured_retention else {"result", "is_error"}
    optional = {"kiro_model_usage", "usage_omitted"}
    if (type(retained) is not dict or not expected <= set(retained) or set(retained) - expected - optional or
            ("usage_omitted" in retained and type(retained["usage_omitted"]) is not bool) or
            (retained.get("usage_omitted") is True and "kiro_model_usage" in retained)):
        raise ValueError("Invalid archived result")
    text = retained["result"]
    if (not result_limit or type(retained["is_error"]) is not bool or
            (text is not None and type(text) is not str)):
        raise ValueError("Invalid archived result")
    remaining = result_limit - (len(text.encode("utf-8")) if text is not None else 0)
    if remaining < 0:
        raise ValueError("Invalid archived result")
    result = dict(retained)
    if structured_retention and retained["structured_output"] is not None:
        encoded = encode_structured_result(retained["structured_output"], max_bytes=remaining)
        remaining -= len(encoded)
        result["structured_output"] = json.loads(encoded)
    if "kiro_model_usage" in retained:
        usage = validate_normalized_usage_report(retained["kiro_model_usage"])
        try:
            encoded = encode_structured_result(usage, max_bytes=remaining)
        except ValueError:
            result.pop("kiro_model_usage")
            result["usage_omitted"] = True
        else:
            result["kiro_model_usage"] = json.loads(encoded)
    return result


def _background_archive_data(status: Any, retained: Any, task_id: str,
                             result_limit: int, structured_retention: bool) -> dict[str, Any]:
    required = {"task_id", "state", "session_id", "events_received", "error_type", "events_dropped"}
    if type(status) is not dict or set(status) not in (required, required | {"cleanup_pending"}):
        raise ValueError("Invalid archived task")
    checked = _background_task_status(BackgroundTaskStatus(**status))
    if (checked["task_id"] != task_id or checked["state"] not in ("completed", "failed", "cancelled") or
            checked["cleanup_pending"]):
        raise ValueError("Invalid archived task")
    # Only fully cleaned tasks can be archived. Keep the existing durable
    # schema so an idempotent save also matches snapshots from older SDKs.
    checked.pop("cleanup_pending")
    return {"task": checked, "retained_result": _background_retained_data(retained, result_limit, structured_retention)}


def _background_archive_json(encoded: bytes, max_bytes: int) -> Any:
    if type(encoded) is not bytes or not 0 < len(encoded) <= max_bytes:
        raise ValueError("Invalid background snapshot")

    def integer(value: str) -> int:
        if len(value.lstrip("-")) > 78:
            raise ValueError("Invalid background snapshot number")
        return int(value)

    def floating(value: str) -> float:
        if len(value) > 128:
            raise ValueError("Invalid background snapshot number")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Invalid background snapshot number")
        return result

    def constant(value: str) -> None:
        raise ValueError("Invalid background snapshot number")

    data = json.loads(encoded, parse_int=integer, parse_float=floating, parse_constant=constant)
    if encode_structured_result(data, max_bytes=max_bytes) != encoded:
        raise ValueError("Invalid background snapshot")
    return data


def _background_archived_events(data: Any, task_id: str, history_limits: tuple[int, int]) -> list[tuple[int, dict]]:
    if (type(data) is not dict or set(data) != {"task_id", "latest_sequence", "history_dropped", "events"} or
            data["task_id"] != task_id or type(data["latest_sequence"]) is not int or
            not 0 <= data["latest_sequence"] <= 2**64 - 1 or type(data["history_dropped"]) is not int or
            type(data["events"]) is not list or not history_limits[0] or
            len(data["events"]) > history_limits[0] or
            data["history_dropped"] != data["latest_sequence"] - len(data["events"])):
        raise ValueError("Invalid archived events")
    events: list[tuple[int, dict]] = []
    previous = total = 0
    for item in data["events"]:
        if (type(item) is not list or len(item) != 2 or type(item[0]) is not int or
                not previous < item[0] <= data["latest_sequence"] or type(item[1]) is not str):
            raise ValueError("Invalid archived event")
        raw = base64.b64decode(item[1], validate=True)
        if base64.b64encode(raw).decode("ascii") != item[1]:
            raise ValueError("Invalid archived event")
        total += len(raw)
        if total > history_limits[1]:
            raise ValueError("Archived events exceed configured limits")
        event = _background_archive_json(raw, 128 * 1024)
        if (type(event) is not dict or set(event) != {"type", "message"} or
                type(event["type"]) is not str or event["type"] not in _BACKGROUND_EVENT_TYPES or
                type(event["message"]) is not dict):
            raise ValueError("Invalid archived event")
        events.append((item[0], event))
        previous = item[0]
    return events


def _background_event_page(page: BackgroundTaskEventPage) -> dict[str, Any]:
    if type(page) is not BackgroundTaskEventPage:
        raise ValueError("Invalid background event page")
    return {"events": page.events, "next_cursor": page.next_cursor, "latest_sequence": page.latest_sequence,
            "history_dropped": page.history_dropped, "gap": page.gap, "has_more": page.has_more}


async def _sdk_permission_mode_result(client: KiroSDKClient) -> dict[str, Optional[str]]:
    return _sdk_permission_mode_snapshot(client)


def _active_introspection_snapshot(client: KiroSDKClient, action: str, *, include_commands: bool) -> Any:
    """Copy only SDK-maintained JSON data without awaiting or running application callbacks."""
    session_id = client.session_id
    commands = client._available_commands_by_session.get(session_id)
    if action == "sdk_permission_mode":
        return _sdk_permission_mode_snapshot(client)
    if action == "available_commands":
        _validate_introspection_result(commands, strict_builtins=True)
        if commands is not None and (type(commands) is not list or
                                     any(type(item) is not dict for item in commands)):
            raise ValueError("Invalid command snapshot")
        return copy.deepcopy(commands)
    if action == "server_info":
        info = client.server_info
        _validate_introspection_result(info, strict_builtins=True)
        if info is None:
            return None
        if type(info) is not dict:
            raise ValueError("Invalid server snapshot")
        result = copy.deepcopy(info)
        if not include_commands:
            result.pop("commands", None)
        elif "commands" not in result and commands is not None:
            _validate_introspection_result(commands, strict_builtins=True)
            if type(commands) is not list or any(type(item) is not dict for item in commands):
                raise ValueError("Invalid command snapshot")
            result["commands"] = copy.deepcopy(commands)
        return result
    if action == "context_usage":
        scope = session_id if session_id in client._context_metadata_by_session else None
        raw = client._context_metadata_by_session.get(scope)
        _validate_introspection_result(raw, strict_builtins=True)
        if raw is not None and type(raw) is not dict:
            raise ValueError("Invalid context snapshot")
        return _normalize_context_usage(copy.deepcopy(raw))
    if action == "mcp_status":
        tracker = client._mcp_status_tracker
        if type(tracker) is not McpStatusTracker:
            raise ValueError("Invalid MCP status tracker")
        scope = session_id if session_id in tracker._last else None
        if scope not in tracker._last:
            return None
        _validate_introspection_result(tracker._last[scope], strict_builtins=True)
        if scope in tracker._snapshots:
            _validate_introspection_result(tracker._snapshots[scope], strict_builtins=True)
        else:
            _validate_introspection_result(tracker._events.get(scope, {}), strict_builtins=True)
        return McpStatusTracker.get(tracker, session_id)
    raise ValueError("Unsupported active introspection action")


def _load_secret(secret_id: str, json_key: Optional[str]) -> str:
    # Boto3 uses the runtime execution role; no key is written to disk or logs.
    import boto3

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    # A fresh boto3 Session avoids reusing credentials captured before an
    # AgentCore runtime session was provisioned.
    response = boto3.Session().client("secretsmanager", region_name=region).get_secret_value(SecretId=secret_id)
    value = response.get("SecretString")
    if not isinstance(value, str) or not value:
        raise ValueError("The Kiro secret must contain a SecretString")
    if json_key is not None:
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError("The Kiro secret is not JSON") from exc
        value = parsed.get(json_key) if isinstance(parsed, dict) else None
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("The Kiro API key is missing from the secret")
    return value


class _Lease:
    def __init__(self, service: "_AgentCoreService", session: _Session):
        self.service, self.session, self.released = service, session, False

    async def release(self) -> None:
        if not self.released:
            self.released = True
            self.session.last_used = time.monotonic()
            self.service.active -= 1
            self.session.lock.release()


class _LeaseStreamingResponse(StreamingResponse):
    def __init__(self, *args: Any, lease: _Lease, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._lease = lease

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        await close()
            finally:
                await self._lease.release()


class _GuardedIntrospectionResponse(JSONResponse):
    """Reauthorize after headers and release an optional idle-session lease."""

    def __init__(self, content: dict[str, Any], guard: Callable[[], Awaitable[bool]],
                 lease: Optional[_Lease] = None):
        super().__init__(content, headers={"Cache-Control": "no-store"})
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key.lower() != b"content-length"]
        self._guard, self._lease = guard, lease

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def guarded_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                try:
                    authorized = await self._guard() is True
                except Exception as exc:
                    logger.warning("Introspection output authorization failed (%s)", type(exc).__name__)
                    authorized = False
                if not authorized:
                    message = {**message, "body": b'{"error":"Session access denied","is_error":true}'}
            await send(message)
        try:
            await super().__call__(scope, receive, guarded_send)
        finally:
            if self._lease is not None:
                await self._lease.release()


class _GuardedBackgroundEventStream(StreamingResponse):
    """Reauthorize each body chunk and release its session pin on disconnect."""

    def __init__(self, iterator: Any, guard: Callable[[], Awaitable[bool]],
                 denied: asyncio.Event, release: Callable[[], None], send_timeout: float):
        super().__init__(iterator, media_type="text/event-stream",
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self._guard, self._denied, self._release = guard, denied, release
        self._send_timeout = send_timeout

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def bounded_send(message: dict[str, Any]) -> None:
            operation = asyncio.create_task(send(message))
            try:
                done, _ = await asyncio.wait((operation,), timeout=self._send_timeout)
                if not done:
                    raise TimeoutError("Background event stream send timed out")
                operation.result()
            finally:
                if not operation.done():
                    operation.cancel()
                    operation.add_done_callback(_AgentCoreService._consume_authorizer_result)

        async def guarded_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                authorized = False
                if not self._denied.is_set():
                    try:
                        authorized = await self._guard() is True
                    except Exception as exc:
                        logger.warning("Background stream authorization failed (%s)", type(exc).__name__)
                if not authorized:
                    if self._denied.is_set():
                        message = {**message, "body": b""}
                    else:
                        self._denied.set()
                        message = {**message, "body": b'data: {"type":"error","error":"Session access denied"}\n\n'}
            await bounded_send(message)
        try:
            await super().__call__(scope, receive, guarded_send)
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        await close()
            finally:
                self._release()


class _AgentCoreService:
    def __init__(self, config: AgentCoreConfig, options_factory: Optional[OptionsFactory],
                 session_authorizer: Optional[SessionAuthorizer],
                 background_tasks_factory: Optional[BackgroundTasksFactory] = None,
                 background_task_options_factory: Optional[BackgroundTaskOptionsFactory] = None,
                 background_request_store: Any = None, background_result_store: Any = None,
                 kiro_token_provider_factory: Optional[KiroTokenProviderFactory] = None,
                 kiro_idle_token_authorizer: Optional[KiroIdleTokenAuthorizer] = None):
        self.config, self.options_factory = config, options_factory
        self.session_authorizer = session_authorizer
        self.kiro_token_provider_factory = kiro_token_provider_factory
        self.kiro_idle_token_authorizer = kiro_idle_token_authorizer
        self.background_tasks_factory = background_tasks_factory
        self.background_task_options_factory = background_task_options_factory
        self.background_request_store = background_request_store
        self.background_result_store = background_result_store
        self._background_claims: set[asyncio.Future[Any]] = set()
        self._background_archive_operations: set[asyncio.Future[Any]] = set()
        self._session_history_operations: set[asyncio.Future[Any]] = set()
        self._session_recovery_operations: set[asyncio.Future[Any]] = set()
        self._background_archive_watchers: set[asyncio.Task[Any]] = set()
        self.sessions: dict[str, _Session] = {}
        # Python 3.9 binds locks to the current loop at construction time.
        self.pool_lock: Optional[asyncio.Lock] = None
        self._cleanup_operations: set[asyncio.Task[Any]] = set()
        self._state_io_operations: set[asyncio.Task[Any]] = set()
        self.active = 0
        self._closing = False

    @staticmethod
    def _consume_authorizer_result(task: asyncio.Future) -> None:
        if not task.cancelled():
            task.exception()

    async def _claim_background_request(self, store: Any, runtime_id: str, request_id: str,
                                        timeout: float, *, check_existing: bool = False) -> Optional[bool]:
        if len(self._background_claims) >= _MAX_BACKGROUND_CLAIMS_IN_FLIGHT:
            return None
        operation = asyncio.ensure_future((store.exists if check_existing else store.claim)(runtime_id, request_id))
        self._background_claims.add(operation)

        def finished(task: asyncio.Future[Any]) -> None:
            self._background_claims.discard(task)
            if not task.cancelled():
                task.exception()

        operation.add_done_callback(finished)
        done, _ = await asyncio.wait((operation,), timeout=timeout)
        if not done:
            # A timed-out thread or callback may still be running or writing.
            # Keep it counted until it finishes and never start on uncertainty.
            return None
        result = operation.result()
        if type(result) is not bool:
            raise ValueError("Background request store returned an invalid claim")
        return result

    async def _background_archive_operation(self, operation: Awaitable[Any], timeout: float) -> Any:
        if len(self._background_archive_operations) >= _MAX_BACKGROUND_ARCHIVE_OPERATIONS:
            if inspect.iscoroutine(operation):
                operation.close()
            return _BACKGROUND_ARCHIVE_TIMEOUT
        task = asyncio.ensure_future(operation)
        self._background_archive_operations.add(task)

        def finished(item: asyncio.Future[Any]) -> None:
            self._background_archive_operations.discard(item)
            if not item.cancelled():
                item.exception()

        task.add_done_callback(finished)
        done, _ = await asyncio.wait((task,), timeout=timeout)
        return task.result() if done else _BACKGROUND_ARCHIVE_TIMEOUT

    async def _session_history_operation(self, operation: Awaitable[Any], timeout: float) -> Any:
        if len(self._session_history_operations) >= _MAX_SESSION_HISTORY_OPERATIONS:
            if inspect.iscoroutine(operation):
                operation.close()
            return _SESSION_HISTORY_TIMEOUT
        task = asyncio.ensure_future(operation)
        self._session_history_operations.add(task)

        def finished(item: asyncio.Future[Any]) -> None:
            self._session_history_operations.discard(item)
            if not item.cancelled():
                item.exception()

        task.add_done_callback(finished)
        done, _ = await asyncio.wait((task,), timeout=timeout)
        return task.result() if done else _SESSION_HISTORY_TIMEOUT

    async def _session_recovery_operation(self, operation: Awaitable[Any], timeout: float) -> Any:
        if len(self._session_recovery_operations) >= _MAX_SESSION_RECOVERY_OPERATIONS:
            if inspect.iscoroutine(operation):
                operation.close()
            return _SESSION_RECOVERY_TIMEOUT
        task = asyncio.ensure_future(operation)
        self._session_recovery_operations.add(task)

        def finished(item: asyncio.Future[Any]) -> None:
            self._session_recovery_operations.discard(item)
            if not item.cancelled():
                item.exception()

        task.add_done_callback(finished)
        done, _ = await asyncio.wait((task,), timeout=timeout)
        return task.result() if done else _SESSION_RECOVERY_TIMEOUT

    async def _auto_archive_background(self, runtime_id: str, task_id: str, manager: BackgroundTaskManager,
                                       store: Any, request: Request, config: AgentCoreConfig,
                                       authorizer: SessionAuthorizer) -> None:
        try:
            interval = config.background_event_checkpoint_interval
            if interval is None or not manager._max_history_events:
                await manager.wait(task_id)
            else:
                waiter = asyncio.create_task(manager.wait(task_id))
                try:
                    while not waiter.done():
                        done, _ = await asyncio.wait((waiter,), timeout=interval)
                        if done:
                            break
                        if (self._closing or self.config is not config or self.background_result_store is not store or
                                self.session_authorizer is not authorizer):
                            return
                        result = await self._background_task_archive(
                            runtime_id, {"action": "background_task_checkpoint_events", "task_id": task_id},
                            request, config, authorizer)
                        if result.status_code in (403, 404):
                            return
                    await waiter
                finally:
                    if not waiter.done():
                        waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            actions = ["background_task_archive"]
            if config.archive_background_task_events and manager._max_history_events:
                actions.append("background_task_archive_events")
            if interval is not None and manager._max_history_events:
                actions.append("background_task_checkpoint_events")
            for action in actions:
                deadline = time.monotonic() + config.background_result_store_timeout
                while (not self._closing and self.config is config and self.background_result_store is store and
                       self.session_authorizer is authorizer and time.monotonic() < deadline):
                    result = await self._background_task_archive(
                        runtime_id, {"action": action, "task_id": task_id}, request, config, authorizer)
                    if result.status_code not in (409, 503):
                        break
                    await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Background automatic archive failed (%s)", type(exc).__name__)

    def _watch_background_archive(self, runtime_id: str, task_id: str, manager: BackgroundTaskManager,
                                  store: Any, request: Request, config: AgentCoreConfig,
                                  authorizer: SessionAuthorizer) -> None:
        if len(self._background_archive_watchers) >= _MAX_BACKGROUND_ARCHIVE_WATCHERS:
            raise RuntimeError("Background automatic archive capacity reached")
        task = asyncio.create_task(self._auto_archive_background(
            runtime_id, task_id, manager, store, request, config, authorizer))
        self._background_archive_watchers.add(task)
        task.add_done_callback(self._background_archive_watchers.discard)

    async def _authorize(self, request: Request, runtime_id: str) -> bool:
        if self._closing:
            return False
        authorizer = self.session_authorizer
        if authorizer is None:
            return True
        task = None
        try:
            task = asyncio.ensure_future(authorizer(request, runtime_id))
            done, _ = await asyncio.wait((task,), timeout=self.config.session_authorization_timeout)
            if not done:
                logger.warning("Session authorization timed out")
                return False
            if task.cancelled():
                return False
            return task.result() is True and self.session_authorizer is authorizer and not self._closing
        except Exception as exc:
            logger.warning("Session authorization failed (%s)", type(exc).__name__)
            return False
        finally:
            if task is not None and not task.done():
                task.cancel()
                task.add_done_callback(self._consume_authorizer_result)

    async def _state_io(self, function: Callable, *args: Any, **kwargs: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._state_io_operations.add(task)
        task.add_done_callback(self._state_io_operations.discard)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                if not cancelled:
                    raise
        if cancelled:
            task.exception()
            raise asyncio.CancelledError
        return task.result()

    async def ping(self, request: Request) -> JSONResponse:
        busy = (self.active or self._closing or
                any(not task.done() for task in (*self._background_claims,
                    *self._background_archive_watchers, *self._background_archive_operations, *self._cleanup_operations)) or
                any(session.background_tasks is not None and
                    session.background_tasks.active_statuses(limit=1)[0]
                    for session in self.sessions.values()))
        return JSONResponse({"status": "HealthyBusy" if busy else "Healthy"})

    async def _lease(self, runtime_id: str) -> tuple[Optional[_Lease], Optional[str]]:
        if self.pool_lock is None:
            self.pool_lock = asyncio.Lock()
        async with self.pool_lock:
            if self._closing:
                return None, "busy"
            session = self.sessions.get(runtime_id)
            if session is None:
                if len(self.sessions) >= self.config.max_sessions:
                    idle = [item for item in self.sessions.values()
                            if not item.lock.locked() and not item.control_lock.locked() and
                            not item.oauth_pins and not item.background_event_streams and
                            (item.device_login is None or item.device_login.task.done())]
                    if not idle:
                        return None, "capacity"
                    victim = min(idle, key=lambda item: item.last_used)
                    # Keep the pool locked until cleanup completes so another request
                    # cannot start a second client for the same Kiro session.
                    await victim.lock.acquire()
                    try:
                        if victim.oauth_pins:
                            return None, "capacity"
                        await self._discard_client(victim)
                        await self._close_background_tasks(victim)
                        if self.sessions.get(victim.runtime_id) is victim:
                            del self.sessions[victim.runtime_id]
                    finally:
                        victim.lock.release()
                if self._closing:
                    return None, "busy"
                root = self.config.state_dir.resolve()
                directory = root / hashlib.sha256(runtime_id.encode()).hexdigest()
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if directory.is_symlink() or directory.resolve().parent != root:
                    raise ValueError("Invalid session directory")
                session = _Session(runtime_id, directory)
                self.sessions[runtime_id] = session
            if session.lock.locked() or session.control_lock.locked() or session.oauth_pins:
                return None, "busy"
            await session.lock.acquire()
            if session.oauth_pins or self._closing:
                session.lock.release()
                return None, "busy"
            self.active += 1
            return _Lease(self, session), None

    async def _control(self, runtime_id: str, action: str, message: Optional[str],
                       authorizer: Optional[SessionAuthorizer]) -> JSONResponse:
        # Pin the current client while the control request is in flight, even
        # if its prompt ends. This prevents eviction or a new turn from
        # accidentally receiving a late cancel/steer intended for the old turn.
        if self.pool_lock is None:
            return JSONResponse({"error": "No active turn"}, status_code=409)
        async with self.pool_lock:
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            if (self.session_authorizer is not authorizer or
                    (client is not None and _requires_session_authorizer(client) and authorizer is None)):
                return JSONResponse({"error": "Session access denied"}, status_code=403)
            active_turn = (client is not None and session is not None and session.turn_ready and
                           client._turn is not None and not client._turn.done())
            preprocessing = (client is not None and action == "interrupt" and
                             client._plugin_interpolation_task is not None and
                             not client._plugin_interpolation_task.done())
            if (session is None or not session.lock.locked() or session.control_lock.locked() or
                    session.broken or client is None or not (active_turn or preprocessing)):
                return JSONResponse({"error": "No controllable active turn"}, status_code=409)
            if (action == "mcp_cancel_tools" and
                    message not in client._named_mcp_bridges and
                    message not in client._named_external_mcp_bridges):
                return JSONResponse({"error": "MCP server is not SDK-managed"}, status_code=400)
            await session.control_lock.acquire()
            if preprocessing:
                session.preprocessing_interrupted = True
            if action == "steer":
                session.clear_steering = True
            self.active += 1
        try:
            if (self.session_authorizer is not authorizer or
                    (_requires_session_authorizer(client) and authorizer is None)):
                return JSONResponse({"error": "Session access denied"}, status_code=403)
            if action == "steer":
                await self._state_io(_write_marker, session.directory / "session.json", self.config.agent_engine,
                              client.session_id, steering_pending=True)
            if action == "interrupt":
                operation = client.interrupt()
            elif action == "mcp_cancel_tools":
                operation = client.request_mcp_tool_cancellation(message)
            else:
                operation = client.steer(message)
            result = await asyncio.wait_for(operation, min(self.config.startup_timeout, 10.0))
            if action == "steer":
                if isinstance(result, dict) and result.get("queued") is False:
                    return JSONResponse({"error": "Kiro did not queue steering"}, status_code=409)
                if not isinstance(result, dict) or result.get("queued") is not True:
                    raise RuntimeError("Kiro did not confirm steering")
            return JSONResponse({"action": action, "status": "requested"})
        except BaseException as exc:
            # A timed-out or cancelled control may still arrive at Kiro.
            # Discard the connection before accepting another turn so it
            # cannot act on that later turn.
            session.broken = True
            session.control_failed = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._discard_client(session), 10.0)
            if not isinstance(exc, Exception):
                raise
            logger.warning("Kiro control request failed (%s)", type(exc).__name__)
            return JSONResponse({"error": "Agent control request failed", "is_error": True})
        finally:
            self.active -= 1
            session.control_lock.release()

    async def _reconnect_mcp(self, runtime_id: str, server_name: str, start_oauth: bool) -> JSONResponse:
        if self.config.agent_engine != "v3":
            return JSONResponse({"error": "MCP reconnection requires Kiro V3"}, status_code=400)
        if self.session_authorizer is None:
            return JSONResponse({"error": "MCP reconnection requires a session authorizer"}, status_code=403)
        if self.pool_lock is None:
            return JSONResponse({"error": "No connected session"}, status_code=409)
        async with self.pool_lock:
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            if (session is None or client is None or session.broken or session.lock.locked() or
                    session.control_lock.locked() or session.oauth_pins):
                return JSONResponse({"error": "Session is unavailable or busy"}, status_code=409)
            bridges = [bridge for bridge in client._named_external_mcp_bridges.get(server_name, [])
                       if isinstance(bridge, RemoteMcpBridge)]
            if not bridges or (start_oauth and bridges[0].oauth is None):
                return JSONResponse({"error": "MCP server is not an eligible SDK-managed remote server"},
                                    status_code=400)
            inline = client._plugin_inline_mcp_configs.get(server_name)
            if inline is not None and (inline[0] != client._selected_agent() or inline[0] != client._current_mode):
                return JSONResponse({"error": "Inline MCP server is not in the selected main agent"}, status_code=400)
            await session.lock.acquire()
            if session.oauth_pins or session.broken or session.client is not client:
                session.lock.release()
                return JSONResponse({"error": "Session is unavailable or busy"}, status_code=409)
            self.active += 1
            lease = _Lease(self, session)
        operation = asyncio.create_task(client.reconnect_mcp_server(server_name, start_oauth=start_oauth))
        try:
            done, _ = await asyncio.wait((operation,), timeout=self.config.startup_timeout)
            if not done:
                raise asyncio.TimeoutError("MCP reconnection timed out")
            result = operation.result()
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError("MCP reconnection was not accepted")
            return JSONResponse({"action": "mcp_reconnect", "status": "requested"})
        except BaseException as exc:
            if not operation.done():
                operation.cancel()
                operation.add_done_callback(self._consume_authorizer_result)
            session.broken = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._discard_client(session), 10.0)
            if not isinstance(exc, Exception):
                raise
            logger.warning("MCP reconnection failed (%s)", type(exc).__name__)
            return JSONResponse({"error": "MCP reconnection failed", "is_error": True})
        finally:
            await lease.release()

    async def _revoke_mcp_oauth(self, runtime_id: str, server_name: str) -> JSONResponse:
        if self.config.agent_engine != "v3":
            return JSONResponse({"error": "MCP OAuth revocation requires Kiro V3"}, status_code=400)
        if self.session_authorizer is None:
            return JSONResponse({"error": "MCP OAuth revocation requires a session authorizer"}, status_code=403)
        if self.pool_lock is None:
            return JSONResponse({"error": "No connected session"}, status_code=409)
        async with self.pool_lock:
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            if (session is None or client is None or session.broken or session.lock.locked() or
                    session.control_lock.locked() or session.oauth_pins):
                return JSONResponse({"error": "Session is unavailable or busy"}, status_code=409)
            bridges = [bridge for bridge in client._named_external_mcp_bridges.get(server_name, [])
                       if isinstance(bridge, RemoteMcpBridge)]
            if not bridges or any(bridge.oauth is None for bridge in bridges):
                return JSONResponse({"error": "MCP server has no SDK-managed OAuth"}, status_code=400)
            await session.lock.acquire()
            if session.oauth_pins or session.broken or session.client is not client:
                session.lock.release()
                return JSONResponse({"error": "Session is unavailable or busy"}, status_code=409)
            self.active += 1
            lease = _Lease(self, session)
        operation = None
        try:
            operation = asyncio.create_task(
                client.revoke_mcp_oauth_tokens(server_name))
            done, _ = await asyncio.wait((operation,), timeout=min(self.config.startup_timeout, 75))
            if not done:
                raise asyncio.TimeoutError("MCP OAuth revocation timed out")
            result = operation.result()
            if (not isinstance(result, dict) or set(result) != {"access_token", "refresh_token"} or
                    any(not isinstance(value, bool) for value in result.values())):
                raise RuntimeError("MCP OAuth revocation returned an invalid result")
            await self._discard_client(session)
            action = "mcp_revoke_oauth"
            return JSONResponse({"action": action, "status": "requested", "tokens": result})
        except BaseException as exc:
            if operation is not None and not operation.done():
                operation.cancel()
                operation.add_done_callback(self._consume_authorizer_result)
            session.broken = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._discard_client(session), 10.0)
            if not isinstance(exc, Exception):
                raise
            logger.warning("MCP OAuth revocation failed (%s)", type(exc).__name__)
            return JSONResponse({"error": "MCP OAuth revocation failed", "is_error": True})
        finally:
            await lease.release()

    async def _compact_control(self, runtime_id: str, include_events: bool = False, *,
                               request: Optional[Request] = None, config: Optional[AgentCoreConfig] = None,
                               authorizer: Optional[SessionAuthorizer] = None) -> JSONResponse:
        def response(value: dict[str, Any], status: int = 200) -> JSONResponse:
            return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

        if self.config.agent_engine != "v3":
            return response({"error": "Compaction requires Kiro V3"}, 400)
        if self.session_authorizer is None:
            return response({"error": "Compaction requires a session authorizer"}, 403)
        if include_events and not self.config.allow_sdk_events:
            return response({"error": "SDK events are not enabled"}, 403)
        if self.pool_lock is None:
            return response({"error": "No connected session"}, 409)
        async with self.pool_lock:
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            if (session is None or client is None or session.broken or session.lock.locked() or
                    session.control_lock.locked() or session.oauth_pins):
                return response({"error": "Session is unavailable or busy"}, 409)
            await session.lock.acquire()
            if session.oauth_pins or session.broken or session.client is not client:
                session.lock.release()
                return response({"error": "Session is unavailable or busy"}, 409)
            self.active += 1
            session.control_failed = False
            session.preprocessing_interrupted = False
            lease = _Lease(self, session)
        guard = None
        if client._hook_failure_snapshot is not None:
            if request is None or config is None or authorizer is None or client._compaction_send_guard is not None:
                await lease.release()
                return response({"error": "Session access denied"}, 403)
            guard = self._session_policy_guard(session, client, request, config, authorizer)
            client._compaction_send_guard = guard
        async def output_allowed() -> bool:
            return guard is None or (client._compaction_send_guard is guard and await guard() is True and
                                     client._compaction_send_guard is guard)
        operation = None
        try:
            operation = asyncio.create_task(client.compact())
            done, _ = await asyncio.wait((operation,), timeout=self.config.startup_timeout)
            if not done:
                raise asyncio.TimeoutError("Compaction timed out")
            result = operation.result()
            if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                raise RuntimeError("Kiro returned an invalid compaction result")
            if not await output_allowed():
                await self._discard_client(session)
                return response({"error": "Session access denied"}, 403)
            return response({"action": "compact", "result": result, "is_error": not result["success"]})
        except PermissionError:
            await self._discard_client(session)
            return response({"error": "Session access denied"}, 403)
        except BaseException as exc:
            if operation is not None and not operation.done():
                operation.cancel()
                done, _ = await asyncio.wait((operation,), timeout=3)
                if done:
                    self._consume_authorizer_result(operation)
                else:
                    operation.add_done_callback(self._consume_authorizer_result)
            if (isinstance(exc, asyncio.TimeoutError) and operation is not None and operation.done() and
                    not client._compaction_request_sent and not session.control_failed):
                return response({"error": "Compaction preparation timed out", "is_error": True}, 422)
            if (isinstance(exc, asyncio.CancelledError) and session.preprocessing_interrupted and
                    not client._compaction_request_sent):
                async with session.control_lock:
                    pass
                session.preprocessing_interrupted = False
                if session.control_failed or session.broken:
                    return response({"error": "Compaction control failed", "is_error": True})
                return response({"error": "Compaction interrupted", "is_error": True}, 409)
            session.broken = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._discard_client(session), 10.0)
            if not isinstance(exc, Exception):
                raise
            logger.warning("Compaction control failed (%s)", type(exc).__name__)
            return response({"error": "Compaction failed", "is_error": True})
        finally:
            if guard is not None and client._compaction_send_guard is guard:
                client._compaction_send_guard = None
            await lease.release()

    async def _code_intelligence_control(self, runtime_id: str, action: str) -> JSONResponse:
        def response(value: dict[str, Any], status: int = 200) -> JSONResponse:
            return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

        if self.config.agent_engine != "v3":
            return response({"error": "Code intelligence requires Kiro V3"}, 400)
        if self.session_authorizer is None:
            return response({"error": "Code intelligence requires a session authorizer"}, 403)
        if self.pool_lock is None:
            return response({"error": "No connected session"}, 409)
        async with self.pool_lock:
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            if (session is None or client is None or session.broken or session.lock.locked() or
                    session.control_lock.locked() or session.oauth_pins):
                return response({"error": "Session is unavailable or busy"}, 409)
            if not client._code_intelligence_enabled:
                return response({"error": "Code intelligence is not enabled"}, 400)
            await session.lock.acquire()
            if session.oauth_pins or session.broken or session.client is not client:
                session.lock.release()
                return response({"error": "Session is unavailable or busy"}, 409)
            self.active += 1
            lease = _Lease(self, session)
        operation = None
        try:
            method = {"code_status": client.get_kiro_code_intelligence_status,
                      "code_init": client.initialize_kiro_code_intelligence,
                      "code_overview": client.get_kiro_code_overview}[action]
            operation = asyncio.create_task(method())
            done, _ = await asyncio.wait((operation,), timeout=self.config.startup_timeout)
            if not done:
                raise asyncio.TimeoutError("Code intelligence control timed out")
            result = operation.result()
            if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                raise RuntimeError("Code intelligence returned an invalid result")
            return response({"action": action, "result": result, "is_error": not result["success"]})
        except BaseException as exc:
            if operation is not None and not operation.done():
                operation.cancel()
                operation.add_done_callback(self._consume_authorizer_result)
            session.broken = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._discard_client(session), 10.0)
            if not isinstance(exc, Exception):
                raise
            logger.warning("Code intelligence control failed (%s)", type(exc).__name__)
            return response({"error": "Code intelligence control failed", "is_error": True})
        finally:
            await lease.release()

    async def _introspection_control(self, runtime_id: str, action: str, payload: dict[str, Any],
                                     request: Request, config: AgentCoreConfig,
                                     authorizer: Optional[SessionAuthorizer]) -> JSONResponse:
        def response(value: dict[str, Any], status: int = 200) -> JSONResponse:
            return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

        if self.config is not config or self.session_authorizer is not authorizer:
            return response({"error": "Session access denied"}, 403)
        if action == "list_kiro_hooks":
            enabled = config.allow_hook_introspection
        elif action in ("available_commands",):
            enabled = config.allow_command_introspection
        elif action in ("server_info", "context_usage", "mcp_status", "sdk_permission_mode"):
            enabled = config.allow_session_introspection
        elif action == "session_messages":
            enabled = config.allow_session_history
        else:
            return response({"error": "Invalid introspection request"}, 400)
        if not enabled or authorizer is None:
            return response({"error": "Introspection requires explicit enablement and a session authorizer"}, 403)
        if action == "list_kiro_hooks":
            if set(payload) - {"action", "trigger", "tool_id", "tool_tags", "include_disabled"}:
                return response({"error": "Invalid hook listing request"}, 400)
            if any(key in payload and payload[key] is None for key in ("trigger", "tool_id", "tool_tags")):
                return response({"error": "Invalid hook listing request"}, 400)
            try:
                trigger, tool_id, tool_tags, include_disabled = _normalize_hook_listing_filters(
                    payload.get("trigger"), payload.get("tool_id"), payload.get("tool_tags"),
                    payload.get("include_disabled", False))
            except (TypeError, ValueError):
                return response({"error": "Invalid hook listing request"}, 400)
            if config.agent_engine != "v3":
                return response({"error": "Hook listing requires Kiro V3"}, 400)
        elif action == "session_messages":
            allowed = {"action", "limit", "offset", "coalesce_chunks", "exclude_observed_subagents",
                       "include_observed_tool_messages"}
            limit, offset = payload.get("limit", 20), payload.get("offset", 0)
            flags = ("coalesce_chunks", "exclude_observed_subagents", "include_observed_tool_messages")
            if (set(payload) - allowed or type(limit) is not int or not 1 <= limit <= 50 or
                    type(offset) is not int or not 0 <= offset <= 20000 or
                    any(key in payload and type(payload[key]) is not bool for key in flags)):
                return response({"error": "Invalid session history request"}, 400)
            if payload.get("include_observed_tool_messages", False) and not config.allow_session_history_tools:
                return response({"error": "Session history tool messages are not enabled"}, 403)
        elif set(payload) != {"action"}:
            return response({"error": "Invalid introspection request"}, 400)
        if self.pool_lock is None:
            return response({"error": "No connected session"}, 409)
        active = False
        lease: Optional[_Lease] = None
        async with self.pool_lock:
            if self.config is not config or self.session_authorizer is not authorizer:
                return response({"error": "Session access denied"}, 403)
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            if (config.allow_active_introspection and action in (
                    "available_commands", "server_info", "context_usage", "mcp_status", "sdk_permission_mode") and
                    session is not None and session.lock.locked()):
                if (client is None or session.broken or session.initializing or not session.turn_ready or
                        session.control_lock.locked() or session.oauth_pins or not client._connected or
                        not client.session_id or client._reader_error is not None or client._turn is None or
                        client._turn.done()):
                    return response({"error": "Session is unavailable or busy"}, 409)
                active = True
                original_turn = client._turn
            elif (session is None or client is None or session.broken or session.lock.locked() or
                    session.control_lock.locked() or session.oauth_pins or not client._connected or
                    not client.session_id or client._reader_error is not None or client._turn is not None or
                    client._stream_input is not None or client._queued_after_initial is not None or
                    client._initial_prompt_active):
                return response({"error": "Session is unavailable or busy"}, 409)
            if not active and (action == "list_kiro_hooks" and
                    (client.options.enable_kiro_hooks is not True or not client._hook_listing_negotiated)):
                return response({"error": "Kiro hooks are not enabled for this session"}, 400)
            if not active:
                if session.lock._waiters and any(not waiter.cancelled() for waiter in session.lock._waiters):
                    return response({"error": "Session is unavailable or busy"}, 409)
                await session.lock.acquire()
                if session.oauth_pins or session.broken or session.client is not client:
                    session.lock.release()
                    return response({"error": "Session is unavailable or busy"}, 409)
                self.active += 1
                lease = _Lease(self, session)
        original_session = client.session_id
        store = client.options.session_store if action == "session_messages" else None
        original_store_key = client._store_key if action == "session_messages" else None
        store_key = (dict(original_store_key) if type(original_store_key) is dict and len(original_store_key) == 2 and
                     all(type(key) is str and type(value) is str for key, value in original_store_key.items())
                     else None)
        if action == "session_messages" and (
                type(store) not in (InMemorySessionStore, SQLiteSessionStore) or
                type(store_key) is not dict or set(store_key) != {"project_key", "session_id"} or
                type(store_key.get("project_key")) is not str or
                re.fullmatch(r"[0-9a-f]{64}", store_key["project_key"]) is None or
                store_key.get("session_id") != original_session):
            await lease.release()
            return response({"error": "A supported session mirror is required"}, 400)
        generation = (client._preset_generation, client._connection_generation)
        def same_client() -> bool:
            if (self.config is not config or self.session_authorizer is not authorizer or
                    self.sessions.get(runtime_id) is not session or session.client is not client or
                    session.broken or session.initializing or
                    not client._connected or client._disconnecting or client._reader_error is not None or
                    client.session_id != original_session or
                    (client._preset_generation, client._connection_generation) != generation or
                    (action == "session_messages" and
                     (client.options.session_store is not store or client._store_key is not original_store_key or
                      client._store_key != store_key))):
                return False
            return True
        def current() -> bool:
            if not same_client() or session.control_lock.locked() or session.oauth_pins:
                return False
            if active:
                return (session.lock.locked() and session.turn_ready and client._turn is original_turn and
                        not original_turn.done())
            return (session.lock.locked() and client._turn is None and client._stream_input is None and
                    client._queued_after_initial is None and not client._initial_prompt_active)
        def output_current() -> bool:
            # A copied active snapshot can remain valid after the observed turn
            # ends, provided it is still the same authorized client/session.
            return same_client() if active else current()
        def denied_response() -> JSONResponse:
            if self.config is not config or self.session_authorizer is not authorizer:
                return response({"error": "Session access denied"}, 403)
            if not current():
                return response({"error": "Session is unavailable or busy"}, 409)
            return response({"error": "Session access denied"}, 403)
        async def authorize_output() -> bool:
            return output_current() and await self._authorize(request, runtime_id) and output_current()
        if active:
            if not current() or not await self._authorize(request, runtime_id) or not current():
                return denied_response()
            try:
                result = _active_introspection_snapshot(
                    client, action, include_commands=config.allow_command_introspection)
                _validate_introspection_result(result, strict_builtins=True)
                guarded = _GuardedIntrospectionResponse({"action": action, "result": result, "is_error": False},
                                                         authorize_output)
            except Exception as exc:
                logger.warning("Active introspection failed (%s)", type(exc).__name__)
                return response({"error": "Introspection failed", "is_error": True})
            if not await authorize_output():
                return denied_response()
            return guarded
        operation = None
        transferred = False
        try:
            if not await authorize_output():
                return denied_response()
            if action == "list_kiro_hooks":
                operation = asyncio.create_task(client.list_kiro_hooks(
                    trigger=trigger, tool_id=tool_id, tool_tags=tool_tags, include_disabled=include_disabled))
            elif action == "available_commands":
                operation = asyncio.create_task(client.get_kiro_available_commands())
            elif action == "server_info":
                operation = asyncio.create_task(client.get_server_info())
            elif action == "context_usage":
                operation = asyncio.create_task(client.get_context_usage())
            elif action == "mcp_status":
                operation = asyncio.create_task(client.get_mcp_status())
            elif action == "sdk_permission_mode":
                operation = asyncio.create_task(_sdk_permission_mode_result(client))
            elif action == "session_messages":
                async def read_history() -> dict[str, Any]:
                    entries = await self._session_history_operation(
                        store.load_bounded(store_key, max_entries=20000, max_bytes=8 * 1024 * 1024,
                                           skip_archives=True),
                        min(config.startup_timeout, 9))
                    if entries is _SESSION_HISTORY_TIMEOUT:
                        raise TimeoutError("Session mirror read timed out")
                    if entries is None:
                        raise LookupError("Session mirror is unavailable")
                    if type(entries) is not list:
                        raise ValueError("Invalid session mirror")
                    messages = _messages_from_mirror_entries(
                        entries, original_session, limit=limit + 1, offset=offset,
                        coalesce_chunks=payload.get("coalesce_chunks", False),
                        exclude_observed_subagents=payload.get("exclude_observed_subagents", False),
                        include_observed_tool_messages=payload.get("include_observed_tool_messages", False))
                    return {"messages": [{"type": type(item).__name__, **asdict(item)} for item in messages[:limit]],
                            "next_offset": offset + limit if len(messages) > limit else None}
                operation = asyncio.create_task(read_history())
            else:
                raise ValueError("Invalid introspection action")
            done, _ = await asyncio.wait((operation,), timeout=min(config.startup_timeout, 10))
            if not done:
                raise asyncio.TimeoutError("Introspection timed out")
            result = operation.result()
            if action == "server_info" and isinstance(result, dict) and not config.allow_command_introspection:
                # The SDK combines the command snapshot into this copied result.
                # Preserve the separate HTTP opt-in for that top-level list.
                result.pop("commands", None)
            if action == "list_kiro_hooks":
                if (not isinstance(result, dict) or not isinstance(result.get("hooks"), list) or
                        len(result["hooks"]) > 1024 or any(not isinstance(hook, dict) for hook in result["hooks"])):
                    raise ValueError("Invalid hook listing")
            elif action in ("available_commands",) and (
                    result is not None) and (
                    not isinstance(result, list) or len(result) > 1024 or
                    any(not isinstance(item, dict) for item in result)):
                raise ValueError("Invalid command listing")
            elif action in ("server_info", "context_usage", "mcp_status", "sdk_permission_mode", "session_messages") and (
                    result is not None and not isinstance(result, dict)):
                raise ValueError("Invalid session information")
            _validate_introspection_result(result)
            if (self.config is not config or self.session_authorizer is not authorizer or
                    session.client is not client or session.broken or not client._connected or
                    client.session_id != original_session or client._reader_error is not None or
                    client._turn is not None or client._stream_input is not None or
                    client._queued_after_initial is not None or client._initial_prompt_active):
                raise RuntimeError("Session changed during introspection")
            guarded = _GuardedIntrospectionResponse({"action": action, "result": result, "is_error": False},
                                                     authorize_output, lease)
            if not await authorize_output():
                return denied_response()
            transferred = True
            return guarded
        except BaseException as exc:
            if operation is not None and not operation.done():
                operation.cancel()
                operation.add_done_callback(self._consume_authorizer_result)
            session.broken = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._discard_client(session), 10)
            if not isinstance(exc, Exception):
                raise
            logger.warning("Introspection failed (%s)", type(exc).__name__)
            return response({"error": "Introspection failed", "is_error": True})
        finally:
            if not transferred:
                await lease.release()

    async def _background_task_archive(self, runtime_id: str, payload: dict[str, Any],
                                       request: Request, config: AgentCoreConfig,
                                       authorizer: Optional[SessionAuthorizer]) -> JSONResponse:
        def response(value: dict[str, Any], status: int) -> JSONResponse:
            return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

        action, task_id = payload["action"], payload.get("task_id")
        checkpoint = action in ("background_task_checkpoint_events", "background_task_checkpoint_events_get",
                                "background_task_checkpoint_events_delete")
        event_action = action in ("background_task_archive_events", "background_task_archive_events_get",
                                  "background_task_archive_events_delete") or checkpoint
        event_get = action in ("background_task_archive_events_get", "background_task_checkpoint_events_get")
        if ((set(payload) - {"action", "task_id", "after", "limit"} if event_get else
             set(payload) != {"action", "task_id"}) or type(task_id) is not str or
                not _SDK_BACKGROUND_TASK_RE.fullmatch(task_id) or
                (event_get and (type(payload.get("after", 0)) is not int or
                                not 0 <= payload.get("after", 0) <= 2**64 - 1 or
                                type(payload.get("limit", 32)) is not int or
                                not 1 <= payload.get("limit", 32) <= 64))):
            return response({"error": "Invalid background archive request"}, 400)
        factory, store = self.background_tasks_factory, self.background_result_store
        if (not config.allow_background_task_results or authorizer is None or factory is None or store is None or
                self.session_authorizer is not authorizer or self.config is not config):
            return response({"error": "Background task archive is not enabled"}, 403)
        if (event_action and action not in ("background_task_archive_events_delete",
                                            "background_task_checkpoint_events_delete") and
                (not config.allow_background_task_events or not config.archive_background_task_events)):
            return response({"error": "Background event archive is not enabled"}, 403)
        if (checkpoint and action != "background_task_checkpoint_events_delete" and
                not config.allow_background_event_checkpoints):
            return response({"error": "Background event checkpoints are not enabled"}, 403)
        if self.pool_lock is None:
            return response({"error": "Session is unavailable"}, 409)
        async with self.pool_lock:
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            manager = session.background_tasks if session is not None else None
            if (session is None or client is None or manager is None or session.broken or session.initializing or
                    session.control_lock.locked() or session.oauth_pins or not client._connected or
                    client._disconnecting or client._reader_error is not None or manager._closed or
                    self.config is not config or self.session_authorizer is not authorizer or
                    self.background_tasks_factory is not factory or self.background_result_store is not store or
                    session.background_tasks_factory is not factory or client._background_task_manager is not manager):
                return response({"error": "Session is unavailable"}, 409)
            await session.control_lock.acquire()
            self.active += 1

        limit, structured = manager._max_retained_result_bytes, manager._retain_structured_output
        settings = manager._retention_settings
        history_limits = (manager._max_history_events, manager._max_history_bytes)

        def current() -> bool:
            return (not self._closing and self.config is config and self.session_authorizer is authorizer and
                    self.background_tasks_factory is factory and self.background_result_store is store and
                    self.sessions.get(runtime_id) is session and session.client is client and
                    session.background_tasks is manager and session.background_tasks_factory is factory and
                    client._background_task_manager is manager and not session.broken and not session.initializing and
                    not session.oauth_pins and client._connected and not client._disconnecting and
                    client._reader_error is None and not manager._closed and
                    manager._retention_settings == settings == (limit, structured) and
                    manager._max_retained_result_bytes == limit and manager._retain_structured_output is structured and
                    (not event_action or
                     (manager._max_history_events, manager._max_history_bytes) == history_limits))

        async def authorized() -> bool:
            return current() and await self._authorize(request, runtime_id) and current()

        try:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            if action in ("background_task_archive_delete", "background_task_archive_events_delete",
                          "background_task_checkpoint_events_delete"):
                delete_name = "delete_event_checkpoint" if checkpoint else "delete_events" if event_action else "delete"
                delete = getattr(store, delete_name, None)
                if not inspect.iscoroutinefunction(delete):
                    return response({"error": "Background snapshot deletion is not enabled"}, 403)
                outcome = await self._background_archive_operation(
                    delete(runtime_id, task_id), config.background_result_store_timeout)
                if not await authorized():
                    return response({"error": "Session access denied"}, 403)
                if outcome is _BACKGROUND_ARCHIVE_TIMEOUT:
                    return response({"error": "Background snapshot store is unavailable"}, 503)
                if type(outcome) is not bool:
                    raise ValueError("Invalid background snapshot store response")
                if not outcome:
                    return response({"error": "Unknown background snapshot"}, 404)
                return response({"action": action, "task_id": task_id}, 200)
            if action in ("background_task_archive_events", "background_task_checkpoint_events"):
                if not history_limits[0]:
                    return response({"error": "Background event history is not configured"}, 409)
                if not manager.owns(task_id):
                    return response({"error": "Unknown background task"}, 404)
                record = manager._get(task_id)
                if not checkpoint and not record.done.is_set():
                    return response({"error": "Background task has not finished"}, 409)
                status = _background_task_status(manager.status(task_id))
                if not checkpoint and status["state"] not in ("completed", "failed", "cancelled"):
                    raise ValueError("Invalid background task status")
                if (type(history_limits[0]) is not int or not 1 <= history_limits[0] <= 1024 or
                        type(history_limits[1]) is not int or not 1 <= history_limits[1] <= 512 * 1024 or
                        len(record.history) > history_limits[0]):
                    raise ValueError("Invalid background event history settings")
                history = list(record.history)
                if (any(type(item) is not tuple or len(item) != 2 or type(item[1]) is not bytes for item in history) or
                        type(record.history_bytes) is not int or
                        record.history_bytes != sum(len(item[1]) for item in history)):
                    raise ValueError("Invalid background event history")
                data = {"task_id": task_id, "latest_sequence": status["events_received"],
                        "history_dropped": record.history_dropped,
                        "events": [[sequence, base64.b64encode(encoded).decode("ascii")]
                                   for sequence, encoded in history]}
                _background_archived_events(data, task_id, history_limits)
                try:
                    encoded = encode_structured_result(data, max_bytes=MAX_BACKGROUND_SNAPSHOT_BYTES)
                except ValueError:
                    return response({"error": "Background snapshot exceeds storage limits"}, 413)
                if not await authorized() or not manager.owns(task_id) or manager._get(task_id) is not record:
                    return response({"error": "Session access denied"}, 403)
                operation = (store.save_event_checkpoint(runtime_id, task_id, data["latest_sequence"], encoded)
                             if checkpoint else store.save_events(runtime_id, task_id, encoded))
                outcome = await self._background_archive_operation(operation, config.background_result_store_timeout)
                if not await authorized() or not manager.owns(task_id) or manager._get(task_id) is not record:
                    return response({"error": "Session access denied"}, 403)
                if outcome is _BACKGROUND_ARCHIVE_TIMEOUT:
                    return response({"error": "Background snapshot store is unavailable"}, 503)
                if type(outcome) is not bool:
                    raise ValueError("Invalid background snapshot store response")
                if not outcome:
                    return response({"error": "Background snapshot conflicts with stored data"}, 409)
                return response({"action": action, "task_id": task_id}, 200)
            if action == "background_task_archive":
                if not manager.owns(task_id):
                    return response({"error": "Unknown background task"}, 404)
                record = manager._get(task_id)
                if not record.done.is_set():
                    return response({"error": "Background task has not finished"}, 409)
                status = _background_task_status(manager.status(task_id))
                if status["cleanup_pending"]:
                    return response({"error": "Background task cleanup is incomplete"}, 409)
                retained = manager.retained_result(task_id) if limit else None
                result = None
                if retained is not None:
                    if (type(retained) is not BackgroundTaskResult or retained.task_id != task_id or
                            type(retained.usage_omitted) is not bool):
                        raise ValueError("Invalid retained result")
                    result = {"result": retained.result, "is_error": retained.is_error}
                    if structured:
                        result["structured_output"] = retained.structured_output
                    elif retained.structured_output is not None:
                        raise ValueError("Unexpected structured result")
                    if retained.kiro_model_usage is not None:
                        result["kiro_model_usage"] = retained.kiro_model_usage
                    if retained.usage_omitted:
                        result["usage_omitted"] = retained.usage_omitted
                data = _background_archive_data(status, result, task_id, limit, structured)
                try:
                    encoded = encode_structured_result(data, max_bytes=MAX_BACKGROUND_SNAPSHOT_BYTES)
                except ValueError:
                    return response({"error": "Background snapshot exceeds storage limits"}, 413)
                if not await authorized() or not manager.owns(task_id) or manager._get(task_id) is not record:
                    return response({"error": "Session access denied"}, 403)
                outcome = await self._background_archive_operation(
                    store.save(runtime_id, task_id, encoded), config.background_result_store_timeout)
                if not await authorized() or not manager.owns(task_id) or manager._get(task_id) is not record:
                    return response({"error": "Session access denied"}, 403)
                if outcome is _BACKGROUND_ARCHIVE_TIMEOUT:
                    return response({"error": "Background snapshot store is unavailable"}, 503)
                if type(outcome) is not bool:
                    raise ValueError("Invalid background snapshot store response")
                if not outcome:
                    return response({"error": "Background snapshot conflicts with stored data"}, 409)
                return response({"action": action, "task_id": task_id}, 200)

            if event_get and not history_limits[0]:
                return response({"error": "Background event history is not configured"}, 409)
            if event_get and (type(history_limits[0]) is not int or not 1 <= history_limits[0] <= 1024 or
                              type(history_limits[1]) is not int or not 1 <= history_limits[1] <= 512 * 1024):
                raise ValueError("Invalid background event history settings")
            operation = (store.load_event_checkpoint(runtime_id, task_id) if checkpoint else
                         store.load_events(runtime_id, task_id) if event_get else store.load(runtime_id, task_id))
            loaded = await self._background_archive_operation(operation, config.background_result_store_timeout)
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            if loaded is _BACKGROUND_ARCHIVE_TIMEOUT:
                return response({"error": "Background snapshot store is unavailable"}, 503)
            if loaded is None:
                return response({"error": "Unknown background snapshot"}, 404)
            checkpoint_sequence = None
            if checkpoint:
                if (type(loaded) is not tuple or len(loaded) != 2 or type(loaded[0]) is not int or
                        not 0 <= loaded[0] <= 2**64 - 1 or type(loaded[1]) is not bytes):
                    raise ValueError("Invalid background checkpoint")
                checkpoint_sequence, loaded = loaded
            data = _background_archive_json(loaded, MAX_BACKGROUND_SNAPSHOT_BYTES)
            if event_get:
                events = _background_archived_events(data, task_id, history_limits)
                if checkpoint and checkpoint_sequence != data["latest_sequence"]:
                    raise ValueError("Invalid background checkpoint sequence")
                after, page_limit = payload.get("after", 0), payload.get("limit", 32)
                if after > data["latest_sequence"]:
                    return response({"error": "Invalid background event cursor"}, 400)
                remaining = [(sequence, event) for sequence, event in events if sequence > after]
                selected, has_more = remaining[:page_limit], len(remaining) > page_limit
                next_cursor = selected[-1][0] if has_more else data["latest_sequence"]
                page = {"action": action,
                        "events": [{"sequence": sequence, **event} for sequence, event in selected],
                        "next_cursor": next_cursor, "latest_sequence": data["latest_sequence"],
                        "history_dropped": data["history_dropped"], "gap": next_cursor - after != len(selected),
                        "has_more": has_more}
                try:
                    _validate_introspection_result(page, strict_builtins=True)
                except ValueError:
                    return response({"error": "Background event page exceeds the response limit"}, 413)
                data = page
            else:
                if type(data) is not dict or set(data) != {"task", "retained_result"}:
                    raise ValueError("Invalid background snapshot")
                data = {"action": action, **_background_archive_data(
                    data["task"], data["retained_result"], task_id, limit, structured)}
            if not await authorized():
                return response({"error": "Session access denied"}, 403)

            async def output_guard() -> bool:
                return await authorized()

            return _GuardedIntrospectionResponse(data, output_guard)
        except BackgroundTaskError:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            return response({"error": "Background result was not retained"}, 413)
        except Exception as exc:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            logger.warning("Background archive failed (%s)", type(exc).__name__)
            return response({"error": "Background archive failed", "is_error": True}, 500)
        finally:
            self.active -= 1
            session.control_lock.release()

    async def _background_tasks_control(self, runtime_id: str, payload: dict[str, Any],
                                        request: Request, config: AgentCoreConfig,
                                        authorizer: Optional[SessionAuthorizer]) -> JSONResponse:
        def response(value: dict[str, Any], status: int = 200) -> JSONResponse:
            return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

        action = payload["action"]
        is_start = action in ("background_task_start", "background_task_retry")
        is_retry = action == "background_task_retry"
        task_id = payload.get("task_id")
        if (action == "background_tasks" and
                (set(payload) - {"action", "include_completed"} or
                 type(payload.get("include_completed", False)) is not bool)) or (
                action == "background_task_start" and (
                    set(payload) != {"action", "prompt", "request_id"} or
                    type(payload.get("request_id")) is not str or
                    not _INTERACTION_RE.fullmatch(payload["request_id"]))) or (
                is_retry and (
                    set(payload) != {"action", "prompt", "request_id", "previous_request_id",
                                     "acknowledge_side_effects"} or
                    type(payload.get("request_id")) is not str or
                    not _INTERACTION_RE.fullmatch(payload["request_id"]) or
                    type(payload.get("previous_request_id")) is not str or
                    not _INTERACTION_RE.fullmatch(payload["previous_request_id"]) or
                    payload["request_id"] == payload["previous_request_id"] or
                    payload.get("acknowledge_side_effects") is not True)) or (
                action == "background_task_events" and (
                    set(payload) - {"action", "task_id", "after", "limit"} or
                    type(task_id) is not str or not _SDK_BACKGROUND_TASK_RE.fullmatch(task_id) or
                    type(payload.get("after", 0)) is not int or not 0 <= payload.get("after", 0) <= 2**64 - 1 or
                    type(payload.get("limit", 32)) is not int or not 1 <= payload.get("limit", 32) <= 64)) or (
                action not in ("background_tasks", "background_task_start", "background_task_retry",
                               "background_task_events") and
                (set(payload) != {"action", "task_id"} or type(task_id) is not str or
                 not _SDK_BACKGROUND_TASK_RE.fullmatch(task_id))):
            return response({"error": "Invalid background task request"}, 400)
        if is_start:
            try:
                if not _validate_prompt(payload.get("prompt"), config.max_prompt_bytes):
                    return response({"error": "Background prompt is too large"}, 413)
                prompt_hash = hashlib.sha256(json.dumps(
                    payload["prompt"], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
                return response({"error": "Invalid background prompt"}, 400)
        factory = self.background_tasks_factory
        if (authorizer is None or factory is None or self.session_authorizer is not authorizer or
                self.config is not config):
            return response({"error": "Background task control is not enabled"}, 403)
        if action == "background_task_result" and not config.allow_background_task_results:
            return response({"error": "Background task results are not enabled"}, 403)
        if action == "background_task_events" and not config.allow_background_task_events:
            return response({"error": "Background task events are not enabled"}, 403)
        options_factory = self.background_task_options_factory
        request_store = self.background_request_store
        archive_store = self.background_result_store if config.auto_archive_background_results else None
        if is_start and options_factory is None:
            return response({"error": "Background task start is not enabled"}, 403)
        if is_retry and (not config.allow_background_task_retry or request_store is None or
                         not inspect.iscoroutinefunction(getattr(request_store, "exists", None))):
            return response({"error": "Background task retry is not enabled"}, 403)
        if is_start and config.auto_archive_background_results and archive_store is None:
            return response({"error": "Background automatic archive is not enabled"}, 403)
        if self.pool_lock is None:
            return response({"error": "Session is unavailable"}, 409)
        async with self.pool_lock:
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            manager = session.background_tasks if session is not None else None
            if (self.config is not config or self.session_authorizer is not authorizer or
                    self.background_tasks_factory is not factory):
                return response({"error": "Session access denied"}, 403)
            if (session is None or client is None or manager is None or session.broken or session.initializing or
                    session.control_lock.locked() or session.oauth_pins or not client._connected or
                    client._disconnecting or client._reader_error is not None or manager._closed or
                    session.background_tasks_factory is not factory or client._background_task_manager is not manager):
                return response({"error": "Session is unavailable"}, 409)
            await session.control_lock.acquire()
            self.active += 1

        result_limit = manager._max_retained_result_bytes
        structured_retention = manager._retain_structured_output
        retention_settings = manager._retention_settings
        history_limits = (manager._max_history_events, manager._max_history_bytes)
        def current() -> bool:
            return (not self._closing and self.config is config and self.session_authorizer is authorizer and
                    self.background_tasks_factory is factory and self.sessions.get(runtime_id) is session and
                    session.client is client and session.background_tasks is manager and
                    session.background_tasks_factory is factory and client._background_task_manager is manager and
                    session.control_lock.locked() and not session.broken and not session.initializing and
                    not session.oauth_pins and client._connected and not client._disconnecting and
                    client._reader_error is None and not manager._closed and
                    (not is_start or (
                        self.background_task_options_factory is options_factory and
                        self.background_request_store is request_store and
                        (not config.auto_archive_background_results or self.background_result_store is archive_store))) and
                    (action != "background_task_result" or (
                        manager._max_retained_result_bytes == result_limit and
                        manager._retain_structured_output is structured_retention and
                        manager._retention_settings == retention_settings == (result_limit, structured_retention))) and
                    (action != "background_task_events" or
                     (manager._max_history_events, manager._max_history_bytes) == history_limits))

        async def authorized() -> bool:
            return current() and await self._authorize(request, runtime_id) and current()

        async def unavailable_result(task_record: Any, message: str, code: int) -> JSONResponse:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            if not manager.owns(task_id) or manager._get(task_id) is not task_record:
                return response({"error": "Background task is no longer available"}, 409)
            return response({"error": message}, code)

        launch_ready = asyncio.Event()
        launched_id: Optional[str] = None
        launched = False
        try:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            if is_start:
                if self.background_task_options_factory is not options_factory:
                    return response({"error": "Session access denied"}, 403)
                request_id = payload["request_id"]
                retry_source = payload["previous_request_id"] if is_retry else None
                previous = session.background_start_requests.get(request_id)
                if previous is not None:
                    previous_hash, previous_task, previous_factory, previous_store, previous_source = previous
                    if (previous_hash != prompt_hash or previous_factory is not options_factory or
                            previous_store is not request_store or previous_source != retry_source):
                        return response({"error": "Background request ID conflict"}, 409)
                    if not await authorized() or self.background_task_options_factory is not options_factory:
                        return response({"error": "Session access denied"}, 403)
                    if not manager.owns(previous_task):
                        return response({"error": "Background request ID has already been used"}, 409)
                    return response({"action": action, "task_id": previous_task})
                if manager._has_failed_cleanup():
                    return response({"error": "Background task cleanup is incomplete"}, 409)
                if is_retry:
                    original = session.background_start_requests.get(retry_source)
                    if original is not None and manager.owns(original[1]):
                        original_status = manager.status(original[1])
                        if original_status.cleanup_pending or original_status.state not in ("failed", "cancelled"):
                            return response({"error": "Original background task is active or completed"}, 409)
                    exists = await self._claim_background_request(
                        request_store, runtime_id, retry_source, config.background_request_claim_timeout,
                        check_existing=True)
                    if not await authorized():
                        return response({"error": "Session access denied"}, 403)
                    if exists is None:
                        return response({"error": "Background request claim is unavailable"}, 503)
                    if not exists:
                        return response({"error": "Unknown original background request"}, 404)
                if len(session.background_start_requests) >= _MAX_BACKGROUND_START_REQUESTS:
                    return response({"error": "Background request ID capacity reached"}, 429)
                workspace = session.directory / "workspace"
                child_options = options_factory(workspace, runtime_id)
                if not isinstance(child_options, KiroAgentOptions) or not isinstance(child_options.env, dict):
                    if asyncio.iscoroutine(child_options):
                        child_options.close()
                    raise ValueError("Background task options are invalid")
                parent_env = client.options.env
                if not isinstance(parent_env, dict):
                    raise ValueError("Session environment is invalid")
                auth_snapshot = {key: parent_env[key] for key in _BACKGROUND_AUTH_ENV if key in parent_env}
                if any(type(key) is not str or type(value) is not str for key, value in auth_snapshot.items()):
                    raise ValueError("Session authentication environment is invalid")
                child_env = dict(child_options.env)
                if any(key in child_env and key not in auth_snapshot for key in _BACKGROUND_AUTH_ENV):
                    raise ValueError("Background task authentication cannot be overridden")
                child_env.update(auth_snapshot)
                child_options = replace(child_options, cwd=workspace, cli_path=config.cli_path,
                                        agent_engine=config.agent_engine, startup_timeout=config.startup_timeout,
                                        turn_timeout=config.turn_timeout, env=child_env)
                original_session_id = client.session_id

                def task_current() -> bool:
                    return (not self._closing and self.config is config and self.session_authorizer is authorizer and
                            self.background_tasks_factory is factory and
                            self.background_task_options_factory is options_factory and
                            self.background_request_store is request_store and
                            (not config.auto_archive_background_results or self.background_result_store is archive_store) and
                            self.sessions.get(runtime_id) is session and session.client is client and
                            session.background_tasks is manager and session.background_tasks_factory is factory and
                            client._background_task_manager is manager and not session.broken and
                            not session.initializing and client._connected and not client._disconnecting and
                            client._reader_error is None and client.session_id == original_session_id and
                            not manager._closed and isinstance(client.options.env, dict) and
                            {key: client.options.env[key] for key in _BACKGROUND_AUTH_ENV
                             if key in client.options.env} == auth_snapshot)

                async def task_guard() -> bool:
                    await launch_ready.wait()
                    return task_current() and await self._authorize(request, runtime_id) and task_current()

                if not await authorized() or not task_current():
                    return response({"error": "Session access denied"}, 403)
                if len(manager._tasks) >= manager._max_tasks:
                    return response({"error": "Background task capacity reached"}, 429)
                if (config.auto_archive_background_results and
                        len(self._background_archive_watchers) >= _MAX_BACKGROUND_ARCHIVE_WATCHERS):
                    return response({"error": "Background automatic archive capacity reached"}, 429)
                if request_store is not None:
                    claimed = await self._claim_background_request(
                        request_store, runtime_id, request_id, config.background_request_claim_timeout)
                    if not await authorized() or not task_current():
                        return response({"error": "Session access denied"}, 403)
                    if claimed is None:
                        return response({"error": "Background request claim is unavailable"}, 503)
                    if not claimed:
                        return response({"error": "Background request ID was already claimed"}, 409)
                launched_id = await BackgroundTaskManager._start_guarded(
                    manager, payload["prompt"], child_options, task_guard)
                if not await authorized() or not task_current():
                    return response({"error": "Session access denied"}, 403)
                if config.auto_archive_background_results:
                    if len(self._background_archive_watchers) >= _MAX_BACKGROUND_ARCHIVE_WATCHERS:
                        return response({"error": "Background automatic archive capacity reached"}, 429)
                    self._watch_background_archive(runtime_id, launched_id, manager, archive_store,
                                                   request, config, authorizer)
                result = {"action": action, "task_id": launched_id}
                session.background_start_requests[request_id] = (
                    prompt_hash, launched_id, options_factory, request_store, retry_source)
                launch_ready.set()
                launched = True
                return response(result)
            if action == "background_tasks":
                statuses, truncated = (manager.statuses(limit=128) if payload.get("include_completed", False)
                                       else manager.active_statuses(limit=128))
                if type(statuses) is not list or len(statuses) > 128 or type(truncated) is not bool:
                    raise ValueError("Invalid background task listing")
                result = {"action": action, "tasks": [_background_task_status(status) for status in statuses],
                          "truncated": truncated}
            else:
                if not manager.owns(task_id):
                    return response({"error": "Unknown background task"}, 404)
                task_record = manager._get(task_id)
                if action == "background_task_forget":
                    if not await authorized():
                        return response({"error": "Session access denied"}, 403)
                    try:
                        manager.forget(task_id)
                    except RuntimeError:
                        if not await authorized():
                            return response({"error": "Session access denied"}, 403)
                        return response({"error": "Background task is active, being read, or awaiting cleanup"}, 409)
                    if not await authorized():
                        return response({"error": "Session access denied"}, 403)
                    return response({"action": action, "task_id": task_id, "status": "forgotten"})
                if action == "background_task_stop":
                    if not await authorized():
                        return response({"error": "Session access denied"}, 403)
                    status = await manager.stop(task_id)
                else:
                    status = manager.status(task_id)
                result = {"action": action, "task": _background_task_status(status)}
                if action == "background_task_events":
                    if not manager._max_history_events:
                        return await unavailable_result(task_record, "Background event history is not configured", 409)
                    try:
                        page = manager.event_history(task_id, after=payload.get("after", 0),
                                                     limit=payload.get("limit", 32))
                    except ValueError:
                        return await unavailable_result(task_record, "Invalid background event cursor", 400)
                    result.update(_background_event_page(page))
                    try:
                        _validate_introspection_result(result, strict_builtins=True)
                    except ValueError:
                        return await unavailable_result(task_record, "Background event page exceeds the response limit", 413)
                if action == "background_task_result":
                    if not manager._max_retained_result_bytes:
                        return await unavailable_result(task_record, "Background result retention is not configured", 409)
                    if not task_record.done.is_set():
                        return await unavailable_result(task_record, "Background task has not finished", 409)
                    if status.cleanup_pending:
                        return await unavailable_result(task_record, "Background task cleanup is incomplete", 409)
                    retained = manager.retained_result(task_id)
                    if retained is None:
                        return await unavailable_result(task_record, "No background result was retained", 404)
                    if (type(retained) is not BackgroundTaskResult or retained.task_id != task_id or
                            type(retained.is_error) is not bool or
                            type(retained.usage_omitted) is not bool or
                            (retained.result is not None and
                             (type(retained.result) is not str or
                              len(retained.result) > manager._max_retained_result_bytes))):
                        raise ValueError("Invalid background task result")
                    retained_data = {"result": retained.result, "is_error": retained.is_error}
                    if structured_retention:
                        retained_data["structured_output"] = retained.structured_output
                    elif retained.structured_output is not None:
                        raise ValueError("Unexpected structured background result")
                    if retained.kiro_model_usage is not None:
                        retained_data["kiro_model_usage"] = retained.kiro_model_usage
                    if retained.usage_omitted:
                        retained_data["usage_omitted"] = retained.usage_omitted
                    result.update(_background_retained_data(retained_data, result_limit, structured_retention))
                    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > _MAX_INTROSPECTION_BYTES:
                        return await unavailable_result(task_record, "Background result exceeds the response limit", 413)
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            if action in ("background_task_result", "background_task_events") and (
                    not manager.owns(task_id) or manager._get(task_id) is not task_record):
                return response({"error": "Background task is no longer available"}, 409)
            if action in ("background_task_result", "background_task_events"):
                def output_current() -> bool:
                    return (not self._closing and self.config is config and self.session_authorizer is authorizer and
                            self.background_tasks_factory is factory and self.sessions.get(runtime_id) is session and
                            session.client is client and session.background_tasks is manager and
                            session.background_tasks_factory is factory and client._background_task_manager is manager and
                            not session.broken and not session.initializing and client._connected and
                            not client._disconnecting and client._reader_error is None and not manager._closed and
                            (action != "background_task_result" or (
                                manager._max_retained_result_bytes == result_limit and
                                manager._retain_structured_output is structured_retention and
                                manager._retention_settings == retention_settings == (result_limit, structured_retention))) and
                            (action != "background_task_events" or
                             (manager._max_history_events, manager._max_history_bytes) == history_limits) and
                            manager.owns(task_id) and manager._get(task_id) is task_record)
                async def authorize_output() -> bool:
                    return output_current() and await self._authorize(request, runtime_id) and output_current()
                return _GuardedIntrospectionResponse(result, authorize_output)
            return response(result)
        except KeyError:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            return response({"error": "Unknown background task"}, 404)
        except TimeoutError:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            return response({"error": "Background task cleanup timed out"}, 504)
        except BackgroundTaskError:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            return response({"error": "Background result was not retained"}, 413)
        except Exception as exc:
            if not await authorized():
                return response({"error": "Session access denied"}, 403)
            logger.warning("Background task control failed (%s)", type(exc).__name__)
            return response({"error": "Background task control failed", "is_error": True}, 500)
        finally:
            if launched_id is not None and not launched:
                with contextlib.suppress(Exception):
                    await manager.stop(launched_id)
                    manager.forget(launched_id)
            self.active -= 1
            session.control_lock.release()

    async def _background_task_event_stream(self, runtime_id: str, payload: dict[str, Any], request: Request,
                                            config: AgentCoreConfig,
                                            authorizer: Optional[SessionAuthorizer]) -> Any:
        def response(value: dict[str, Any], status: int) -> JSONResponse:
            return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

        task_id, after, limit = payload.get("task_id"), payload.get("after", 0), payload.get("limit", 32)
        if (set(payload) - {"action", "task_id", "after", "limit"} or
                type(task_id) is not str or not _SDK_BACKGROUND_TASK_RE.fullmatch(task_id) or
                type(after) is not int or not 0 <= after <= 2**64 - 1 or
                type(limit) is not int or not 1 <= limit <= 64):
            return response({"error": "Invalid background event stream request"}, 400)
        factory = self.background_tasks_factory
        if (authorizer is None or factory is None or self.session_authorizer is not authorizer or
                self.config is not config or not config.allow_background_task_event_stream):
            return response({"error": "Background event streams are not enabled"}, 403)
        if self.pool_lock is None:
            return response({"error": "Session is unavailable"}, 409)
        pending_error: Optional[tuple[str, int]] = None
        async with self.pool_lock:
            session = self.sessions.get(runtime_id)
            client = session.client if session is not None else None
            manager = session.background_tasks if session is not None else None
            if (self._closing or self.config is not config or self.session_authorizer is not authorizer or
                    self.background_tasks_factory is not factory):
                return response({"error": "Session access denied"}, 403)
            if (session is None or client is None or manager is None or session.broken or session.initializing or
                    not client._connected or client._disconnecting or client._reader_error is not None or
                    manager._closed or session.background_tasks_factory is not factory or
                    client._background_task_manager is not manager):
                pending_error = ("Session is unavailable", 409)
            elif not manager._max_history_events:
                pending_error = ("Background event history is not configured", 409)
            elif not manager.owns(task_id):
                pending_error = ("Unknown background task", 404)
            else:
                task_record = manager._get(task_id)
                if after > task_record.events_received:
                    pending_error = ("Invalid background event cursor", 400)
                elif session.background_event_streams >= _MAX_BACKGROUND_EVENT_STREAMS:
                    pending_error = ("Background event stream capacity reached", 429)
                else:
                    history_limits = (manager._max_history_events, manager._max_history_bytes)
                    session.background_event_streams += 1
                    self.active += 1
        if pending_error is not None:
            if (self._closing or self.config is not config or self.session_authorizer is not authorizer or
                    self.background_tasks_factory is not factory or not await self._authorize(request, runtime_id) or
                    self._closing or self.config is not config or self.session_authorizer is not authorizer or
                    self.background_tasks_factory is not factory):
                return response({"error": "Session access denied"}, 403)
            return response({"error": pending_error[0]}, pending_error[1])

        released = False
        denied = asyncio.Event()

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                session.background_event_streams -= 1
                session.last_used = time.monotonic()
                self.active -= 1

        def current() -> bool:
            return (not released and not self._closing and self.config is config and
                    self.session_authorizer is authorizer and self.background_tasks_factory is factory and
                    self.sessions.get(runtime_id) is session and session.client is client and
                    session.background_tasks is manager and session.background_tasks_factory is factory and
                    client._background_task_manager is manager and not session.broken and not session.initializing and
                    client._connected and not client._disconnecting and client._reader_error is None and
                    not manager._closed and
                    (manager._max_history_events, manager._max_history_bytes) == history_limits and
                    manager.owns(task_id) and manager._get(task_id) is task_record)

        async def authorized() -> bool:
            return current() and await self._authorize(request, runtime_id) and current()

        async def stream():
            cursor = after
            deadline = time.monotonic() + config.background_task_event_stream_timeout
            try:
                while not denied.is_set():
                    if not await authorized():
                        yield 'data: {"type":"error","error":"Session access denied"}\n\n'
                        return
                    if time.monotonic() >= deadline:
                        yield "data: " + json.dumps({"type": "timeout", "next_cursor": cursor}) + "\n\n"
                        return
                    page_limit = limit
                    while True:
                        page = manager.event_history(task_id, after=cursor, limit=page_limit)
                        value = {"type": "events", "task": _background_task_status(manager.status(task_id)),
                                 **_background_event_page(page)}
                        try:
                            _validate_introspection_result(value, strict_builtins=True)
                            break
                        except ValueError:
                            if page_limit == 1:
                                yield 'data: {"type":"error","error":"Background event page exceeds the response limit"}\n\n'
                                return
                            page_limit = max(1, page_limit // 2)
                    if time.monotonic() >= deadline:
                        yield "data: " + json.dumps({"type": "timeout", "next_cursor": cursor}) + "\n\n"
                        return
                    if page.events or page.gap:
                        cursor = page.next_cursor
                        yield "data: " + json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n\n"
                        continue
                    if task_record.done.is_set():
                        value = {"type": "complete", "task": _background_task_status(manager.status(task_id)),
                                 "next_cursor": cursor}
                        yield "data: " + json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n\n"
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        yield "data: " + json.dumps({"type": "timeout", "next_cursor": cursor}) + "\n\n"
                        return
                    signal = task_record.history_signal
                    try:
                        await asyncio.wait_for(signal.wait(), timeout=min(5.0, remaining))
                    except asyncio.TimeoutError:
                        if time.monotonic() < deadline:
                            yield ": keepalive\n\n"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Background event stream failed (%s)", type(exc).__name__)
                yield 'data: {"type":"error","error":"Background event stream failed"}\n\n'

        try:
            if not await authorized():
                release()
                return response({"error": "Session access denied"}, 403)
            return _GuardedBackgroundEventStream(
                stream(), authorized, denied, release,
                min(10.0, max(0.1, config.background_task_event_stream_timeout)))
        except BaseException:
            release()
            raise

    async def _await_interaction(self, session: _Session, kind: str, request: dict[str, Any], *,
                                 oauth_client: Optional[KiroSDKClient] = None, pin_owned: bool = False) -> Any:
        pinned = False
        if oauth_client is not None:
            # Check and pin without an await. Eviction rechecks after acquiring
            # the session lock and marks the client broken before disconnect.
            if (self.pool_lock is None or self.sessions.get(session.runtime_id) is not session or
                    session.client is not oauth_client or
                    (session.broken and not (session.initializing and session.lock.locked())) or
                    (pin_owned and session.oauth_pins <= 0)):
                return None
            if not pin_owned:
                session.oauth_pins += 1
                pinned = True
        else:
            turn = session.client._turn if session.client is not None else None
            if turn is None or turn.done():
                return None
        try:
            try:
                size = len(json.dumps(request, allow_nan=False, ensure_ascii=False).encode("utf-8"))
            except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
                return None
            if (len(session.interactions) >= _MAX_PENDING_INTERACTIONS or
                    size + sum(item.size for item in session.interactions.values()) > _MAX_INTERACTION_BYTES):
                return None
            ident = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            item = _Interaction(ident, kind, request, future, size)
            session.interactions[ident] = item
            try:
                try:
                    return await asyncio.wait_for(future, self.config.interaction_timeout)
                except asyncio.TimeoutError:
                    return None
            finally:
                session.interactions.pop(ident, None)
        finally:
            if pinned:
                session.oauth_pins -= 1
                session.last_used = time.monotonic()

    async def _interactions(self, runtime_id: str, payload: dict[str, Any],
                            authorizer: Optional[SessionAuthorizer], request: Request) -> JSONResponse:
        if not (self.config.allow_interactions):
            return JSONResponse({"error": "Remote interactions are not enabled"}, status_code=403)
        if (self.session_authorizer is not authorizer or
                ((self.config.allow_mcp_oauth_interactions)
                 and authorizer is None)):
            return JSONResponse({"error": "Session access denied"}, status_code=403)
        if (self.config.allow_mcp_oauth_interactions):
            config = self.config
            if (not await self._authorize(request, runtime_id) or self.config is not config or
                    self.session_authorizer is not authorizer):
                return JSONResponse({"error": "Session access denied"}, status_code=403)
        action = payload.get("action")
        if action == "interactions" and set(payload) != {"action"}:
            return JSONResponse({"error": "Invalid interaction request"}, status_code=400)
        if action == "respond":
            ident = payload.get("interaction_id")
            if not isinstance(ident, str) or not _INTERACTION_RE.fullmatch(ident):
                return JSONResponse({"error": "Invalid interaction ID"}, status_code=400)
        if self.pool_lock is None:
            return JSONResponse({"pending": []}) if action == "interactions" else JSONResponse(
                {"error": "Interaction is no longer pending"}, status_code=409)
        async with self.pool_lock:
            if (not (self.config.allow_interactions) or
                    self.session_authorizer is not authorizer or
                    ((self.config.allow_mcp_oauth_interactions)
                     and authorizer is None)):
                return JSONResponse({"error": "Session access denied"}, status_code=403)
            session = self.sessions.get(runtime_id)
            if (session is not None and session.client is not None and
                    _requires_session_authorizer(session.client) and authorizer is None):
                return JSONResponse({"error": "Session access denied"}, status_code=403)
            if action == "interactions":
                pending = ([] if session is None else [
                    {"interaction_id": item.interaction_id, "kind": item.kind, "request": item.request}
                    for item in session.interactions.values() if not item.future.done() and (
                        self.config.allow_interactions)])
                return JSONResponse({"pending": pending}, headers={"Cache-Control": "no-store"})
            item = session.interactions.get(ident) if session is not None else None
            if item is None or item.future.done():
                return JSONResponse({"error": "Interaction is no longer pending"}, status_code=409)
            if not (self.config.allow_interactions):
                return JSONResponse({"error": "Interaction is no longer enabled"}, status_code=403)
            keys = set(payload)
            if item.kind == "permission":
                if keys != {"action", "interaction_id", "decision"} or payload.get("decision") not in ("allow", "deny"):
                    return JSONResponse({"error": "Invalid permission decision"}, status_code=400)
                value = payload["decision"]
            elif item.kind == "user_input":
                value = payload.get("answer")
                if (keys != {"action", "interaction_id", "answer"} or
                        (value is not None and (not isinstance(value, str) or not value.strip() or "\0" in value))):
                    return JSONResponse({"error": "Invalid answer"}, status_code=400)
            elif item.kind in ("mcp_oauth",):
                value = payload.get("redirect_url")
                if (keys != {"action", "interaction_id", "redirect_url"} or
                        (value is not None and (not isinstance(value, str) or not value or len(value) > 16384 or
                         any(ord(char) < 32 or ord(char) == 127 for char in value)))):
                    return JSONResponse({"error": "Invalid OAuth response"}, status_code=400)
            else:
                value = payload.get("response")
                if (keys != {"action", "interaction_id", "response"} or not isinstance(value, dict) or
                        set(value) - {"action", "content"} or value.get("action") not in ("accept", "decline", "cancel") or
                        ("content" in value and (value["action"] != "accept" or
                                                 not isinstance(value["content"], dict)))):
                    return JSONResponse({"error": "Invalid elicitation response"}, status_code=400)
            try:
                size = len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8"))
            except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
                return JSONResponse({"error": "Invalid interaction response"}, status_code=400)
            if size > _MAX_INTERACTION_BYTES:
                return JSONResponse({"error": "Interaction response is too large"}, status_code=413)
            if self.config.allow_mcp_oauth_interactions and item.kind == "mcp_oauth" and value is not None:
                if authorizer is None:
                    return JSONResponse({"error": "Session access denied"}, status_code=403)
                value = _PermissionOAuthResponse(value, self.config, authorizer, request)
            del session.interactions[ident]
            item.future.set_result(value)
            return JSONResponse({"status": "accepted"})

    @staticmethod
    def _session_home(session: _Session, *, isolate_runtime: bool = False) -> tuple[Path, dict[str, str]]:
        home = session.directory / "home"
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if home.is_symlink() or home.resolve().parent != session.directory.resolve():
            raise ValueError("Invalid session home")
        home.chmod(0o700)
        env = {"HOME": str(home), "KIRO_HOME": str(home / ".kiro"),
               "XDG_DATA_HOME": str(home / ".local" / "share"),
               "XDG_CONFIG_HOME": str(home / ".config"),
               "XDG_CACHE_HOME": str(home / ".cache"),
               "XDG_STATE_HOME": str(home / ".local" / "state"), "KIRO_API_KEY": ""}
        if isolate_runtime:
            runtime, temporary = home / ".runtime", home / "tmp"
            for directory in (runtime, temporary):
                directory.mkdir(exist_ok=True, mode=0o700)
                if directory.is_symlink() or directory.resolve().parent != home.resolve():
                    raise ValueError("Invalid session runtime directory")
                directory.chmod(0o700)
            env.update({"XDG_RUNTIME_DIR": str(runtime), "TMPDIR": str(temporary),
                        "KIRO_CHAT_LOG_FILE": str(runtime / "kiro-chat.log")})
        return home, env

    async def _device_action(self, runtime_id: str, action: str, payload: dict[str, Any]) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        def response(value, status=200):
            return JSONResponse(value, status_code=status, headers=headers)
        if self.config.auth_mode != "device":
            return response({"error": "Device authentication is not enabled"}, 403)
        if set(payload) != {"action"}:
            return response({"error": "Invalid authentication request"}, 400)
        try:
            lease, unavailable = await self._lease(runtime_id)
        except Exception as exc:
            logger.warning("Device session preparation failed (%s)", type(exc).__name__)
            return response({"error": "Session unavailable", "is_error": True})
        if unavailable:
            return response({"error": "Session is " + unavailable}, 429 if unavailable == "capacity" else 409)
        assert lease is not None
        session = lease.session
        try:
            home, env = self._session_home(session, isolate_runtime=True)
            login = session.device_login
            if action == "auth_cancel":
                if login is not None and not login.task.done():
                    await login.cancel()
                session.device_login = None
                return response({"action": action, "status": "cancelled"})
            if action == "auth_logout":
                if login is not None:
                    await login.cancel()
                    session.device_login = None
                await self._discard_client(session)
                if not await cli_auth_command(self.config.cli_path, home, env, "logout"):
                    return response({"error": "Device logout failed", "is_error": True})
                return response({"action": action, "status": "signed_out"})
            if login is not None and not login.task.done():
                if login.details:
                    return response({"action": action, "status": "pending", **login.details})
                if action == "auth_status":
                    return response({"action": action, "status": "starting"})
            elif action == "auth_status":
                if await cli_auth_command(self.config.cli_path, home, env, "whoami"):
                    return response({"action": action, "status": "authenticated"})
                return response({"action": action, "status": "failed" if login and login.status == "failed"
                                 else "not_authenticated"})
            else:
                if await cli_auth_command(self.config.cli_path, home, env, "whoami"):
                    return response({"action": action, "status": "authenticated"})
                await self._discard_client(session)
                login = DeviceLogin(self.config.cli_path, home, env, license=self.config.device_license,
                    identity_provider=self.config.device_start_url, region=self.config.device_region,
                    timeout=self.config.device_timeout)
                session.device_login = login
            assert login is not None
            try:
                await asyncio.wait_for(asyncio.shield(login.challenge), min(30.0, self.config.device_timeout))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await login.cancel()
                raise
            if login.details:
                return response({"action": action, "status": "pending", **login.details})
            if login.status == "authenticated":
                return response({"action": action, "status": "authenticated"})
            return response({"error": "Device login could not be started", "is_error": True})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Device authentication failed (%s)", type(exc).__name__)
            return response({"error": "Device authentication failed", "is_error": True})
        finally:
            await lease.release()

    def _session_policy_guard(self, session: _Session, client: KiroSDKClient, request: Request,
                              turn_config: AgentCoreConfig, authorizer: Optional[SessionAuthorizer]) -> Callable[[], Awaitable[bool]]:
        def current() -> bool:
            return (authorizer is not None and self.config is turn_config and self.session_authorizer is authorizer and
                    self.sessions.get(session.runtime_id) is session and session.client is client and
                    session.lock.locked() and (session.initializing or not session.broken) and
                    client._connected and not client._disconnecting and client._reader_error is None)
        async def guard() -> bool:
            return current() and await self._authorize(request, session.runtime_id) and current()
        return guard

    async def _ensure_output_authorized(self, session: _Session, request: Request,
                                        config: AgentCoreConfig, authorizer: Optional[SessionAuthorizer],
                                        client: Optional[KiroSDKClient], client_session_id: str, preset_generation: tuple[int, int], *,
                                        detached: bool = False) -> None:
        def current() -> bool:
            valid = (authorizer is not None and self.config is config and self.session_authorizer is authorizer and
                     self.sessions.get(session.runtime_id) is session and session.lock.locked() and not session.broken and
                     isinstance(client_session_id, str) and bool(client_session_id) and
                     client is not None and type(preset_generation) is tuple and len(preset_generation) == 2 and
                     all(type(value) is int for value in preset_generation) and
                     (client._preset_generation, client._connection_generation) == preset_generation and
                     ((session.client is None) if detached else
                      (client is not None and session.client is client and client._connected and
                       client.session_id == client_session_id and not client._disconnecting and
                       client._reader_error is None)))
            if valid and not detached:
                try:
                    KiroSDKClient._validate_unsupported_limits(client)
                except Exception:
                    return False
            return bool(valid)
        if not current() or not await self._authorize(request, session.runtime_id) or not current():
            raise PermissionError("Session output authorization changed")

    async def _stop_replaced_client(self, session: _Session, client: Optional[KiroSDKClient]) -> None:
        if client is not None and session.client is not client and client._connected:
            with contextlib.suppress(Exception):
                await KiroSDKClient._abort_for_guard(client, force=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 10)

    async def _reauthorized_json_body(self, body: bytes, session: _Session, request: Request,
                                      config: AgentCoreConfig, authorizer: Optional[SessionAuthorizer],
                                      client: Optional[KiroSDKClient], client_session_id: str, preset_generation: tuple[int, int],
                                      lease: _Lease, *, detached: bool):
        try:
            try:
                await self._ensure_output_authorized(session, request, config, authorizer, client,
                                                     client_session_id, preset_generation, detached=detached)
            except PermissionError:
                await self._stop_replaced_client(session, client)
                with contextlib.suppress(Exception):
                    await self._discard_client(session)
                body = b'{"error":"Agent invocation failed","is_error":true}'
            yield body
        finally:
            await lease.release()

    async def _client(self, session: _Session, request: Request, turn_config: AgentCoreConfig,
                      authorizer: Optional[SessionAuthorizer]) -> tuple[KiroSDKClient, Optional[Callable[[], Awaitable[bool]]]]:
        if self._closing:
            raise RuntimeError("AgentCore service is closing")
        if session.broken:
            await self._discard_client(session)
        if self._closing:
            raise RuntimeError("AgentCore service is closing")
        if session.client is not None:
            return session.client, None
        workspace = session.directory / "workspace"
        await self._state_io(workspace.mkdir, parents=True, exist_ok=True, mode=0o700)
        marker = await self._state_io(_read_marker, session.directory / "session.json", self.config.agent_engine)
        if self._closing:
            raise RuntimeError("AgentCore service is closing")
        resume = marker[0] if marker else None
        if marker and marker[1]:
            session.clear_steering = True
        auth_env: dict[str, str] = {}
        if self.config.auth_mode in ("api-key", "device"):
            home, auth_env = self._session_home(session, isolate_runtime=self.config.auth_mode == "device")
            if self.config.auth_mode == "device":
                if ((session.device_login is not None and not session.device_login.task.done()) or
                        not await cli_auth_command(self.config.cli_path, home, auth_env, "whoami")):
                    raise _DeviceAuthRequired()
            else:
                key = (self.config.api_key if self.config.api_key is not None else
                       await asyncio.to_thread(_load_secret, self.config.secret_id, self.config.secret_json_key))
                auth_env["KIRO_API_KEY"] = key
            if self.config.runtime_cache_dir is not None and self.config.agent_engine == "v3":
                await asyncio.to_thread(_copy_runtime_cache, self.config.runtime_cache_dir, home)
        else:
            # Avoid accidentally falling back to an inherited API key.
            auth_env = {"KIRO_API_KEY": ""}
        if self._closing:
            raise RuntimeError("AgentCore service is closing")
        options = (self.options_factory(workspace, resume) if self.options_factory else
                   KiroAgentOptions(tools=[], model=self.config.model))
        if not isinstance(options, KiroAgentOptions):
            raise TypeError("options_factory must return KiroAgentOptions")
        if self.options_factory and self.config.model is not None and options.model is None:
            # The configured model is the default; a model chosen by options_factory takes precedence.
            options = replace(options, model=self.config.model)
        # The container environment (cloud credentials, service configuration) is not inherited by Kiro
        # unless the deployment explicitly selects child_env_mode="inherit".
        options = replace(options, env_inheritance_mode="minimal" if self.config.child_env_mode == "minimal"
                          else options.env_inheritance_mode)
        if ((options.native_hook_bridge_mode == "runtime-acp" or options.runtime_compat_mode == "v3") and
                options.native_hook_runtime_path is None and
                turn_config.runtime_cache_dir is not None):
            candidates = sorted((turn_config.runtime_cache_dir / "kas").glob(
                "2.35.1-*/node_modules/@kiro/agent/dist/server/acp-server.js"))
            if len(candidates) != 1:
                raise ValueError("AgentCore native ACP hooks require one supported cached Kiro runtime")
            options = replace(options, native_hook_runtime_path=candidates[0])
        if ((options.native_hook_bridge_mode == "runtime-acp" or options.runtime_compat_mode == "v3" or
             options.kiro_auth_method == "client") and self.session_authorizer is None):
            raise ValueError("AgentCore session policy mappings require session_authorizer")
        token_factory = self.kiro_token_provider_factory
        idle_token_authorizer = self.kiro_idle_token_authorizer
        if options.kiro_auth_method == "client":
            if (turn_config.auth_mode != "cli" or token_factory is None or
                    options.kiro_access_token_provider is not None or authorizer is None or
                    self.session_authorizer is not authorizer or self.config is not turn_config or
                    not session.lock.locked() or not await self._authorize(request, session.runtime_id) or
                    self.session_authorizer is not authorizer or self.config is not turn_config or
                    self.kiro_token_provider_factory is not token_factory):
                raise PermissionError("AgentCore client authentication is unavailable")
            factory_failed = False
            try:
                token_provider = token_factory(workspace, session.runtime_id)
            except asyncio.CancelledError:
                raise
            except BaseException:
                factory_failed, token_provider = True, None
            if factory_failed:
                raise ValueError("Token provider factory failed")
            if not callable(token_provider) or self.kiro_token_provider_factory is not token_factory:
                if inspect.iscoroutine(token_provider):
                    token_provider.close()
                raise ValueError("Token provider factory must return an async callable")
            options = replace(options, kiro_access_token_provider=token_provider)
            if options.kiro_client_auth_idle_mode == "authorized":
                if idle_token_authorizer is None or options.kiro_idle_token_authorizer is not None:
                    raise ValueError("AgentCore idle Kiro authentication requires a session authorizer")
                def idle_token_current() -> bool:
                    return (self.kiro_idle_token_authorizer is idle_token_authorizer and
                            self.kiro_token_provider_factory is token_factory and
                            self.session_authorizer is authorizer and self.config is turn_config and
                            not self._closing and self.sessions.get(session.runtime_id) is session and
                            session.client is client and not session.broken and not session.initializing and
                            client._connected and not client._disconnecting and client._reader_error is None)
                async def authorize_idle_token() -> bool:
                    return (idle_token_current() and await idle_token_authorizer(session.runtime_id) is True and
                            idle_token_current())
                options = replace(options, kiro_idle_token_authorizer=authorize_idle_token)
        if options.session_id or options.continue_conversation or options.fork_session:
            raise ValueError("AgentCore manages session creation and resume")
        if options.resume not in (None, resume):
            raise ValueError("options_factory cannot select another session")
        if not isinstance(options.env, dict):
            raise ValueError("options.env must be a dictionary")
        if self.config.allow_interactions:
            if (options.can_use_tool is not None or options.on_user_input is not None or
                    options.on_mcp_elicitation is not None or
                    options.permission_mode not in (None, "ask")):
                raise ValueError("Remote interactions cannot replace application callbacks or permission modes")

            async def approve(name, data, context):
                request = {
                    "tool_name": name, "tool_input": data, "tool_call_id": context.tool_call_id,
                    "tool_call": context.tool_call, "options": context.options,
                    "request_metadata": context.request_metadata}
                if context.kiro_input_unavailable:
                    request["kiro_input_unavailable"] = True
                decision = await self._await_interaction(session, "permission", request)
                return PermissionResultAllow() if decision == "allow" else PermissionResultDeny()

            async def policy_input(kind: str, request_data: dict[str, Any]) -> Any:
                client._validate_unsupported_limits()
                return None

            async def ask(request):
                return (await self._await_interaction(session, "user_input", request))

            async def elicit(request):
                response = (await self._await_interaction(session, "mcp_elicitation", request))
                return response if response is not None else {"action": "cancel"}

            options = replace(options, can_use_tool=approve,
                              on_user_input=ask if self.config.agent_engine == "v3" else None,
                              on_mcp_elicitation=elicit if self.config.agent_engine == "v3" else None)
        preauthorization_guard: Optional[Callable[[], bool]] = None
        if self.config.allow_mcp_oauth_interactions:
            oauth_configs = {}
            if not isinstance(options.mcp_oauth, dict):
                raise ValueError("mcp_oauth must be a dictionary")
            for name, config in options.mcp_oauth.items():
                if (not isinstance(name, str) or not isinstance(config, McpOAuthConfig) or
                        config.authorize is not None or type(config.storage) is not InMemoryMcpOAuthStorage):
                    raise ValueError("AgentCore MCP OAuth interactions require authorize=None and default storage")
                async def authorize(url: str, server_name: str = name) -> str:
                    active_config = self.config
                    turn_guard = client._interaction_guard
                    turn = client._turn
                    def current() -> bool:
                        return (self.config is active_config and self.config is turn_config and
                                self.config.allow_mcp_oauth_interactions and self.config.allow_interactions and
                                self.session_authorizer is authorizer and authorizer is not None and
                                self.sessions.get(session.runtime_id) is session and session.client is client and
                                (session.initializing or (not session.broken and client._connected)) and
                                not client._disconnecting and client._reader_error is None and
                                (not turn_config.preauthorize_mcp_oauth or not session.initializing or
                                 (preauthorization_guard is not None and preauthorization_guard())))
                    async def authorize_turn() -> bool:
                        if session.initializing:
                            return await self._authorize(request, session.runtime_id)
                        if turn is None or turn.done():
                            return client._turn is None or client._turn is turn or client._turn.done()
                        return (client._turn is turn and turn_guard is not None and
                                client._interaction_guard is turn_guard and await turn_guard() is True and
                                client._interaction_guard is turn_guard)
                    if not current():
                        raise ValueError("MCP OAuth authorization changed")
                    session.oauth_pins += 1
                    try:
                        if not await authorize_turn() or not current():
                            raise ValueError("MCP OAuth authorization changed")
                        client._validate_unsupported_limits()
                        response = await self._await_interaction(
                            session, "mcp_oauth", {"server_name": server_name, "authorization_url": url},
                            oauth_client=client, pin_owned=True)
                        if (not isinstance(response, _PermissionOAuthResponse) or not current() or
                                response.config is not active_config or response.authorizer is not authorizer or
                                not await self._authorize(response.request, session.runtime_id) or not current() or
                                not await authorize_turn() or not current() or
                                not await self._authorize(response.request, session.runtime_id) or not current() or
                                not await authorize_turn() or not current()):
                            raise ValueError("MCP OAuth authorization changed")
                        client._validate_unsupported_limits()
                        return response.redirect_url
                    finally:
                        session.oauth_pins -= 1
                        session.last_used = time.monotonic()
                oauth_configs[name] = replace(config, authorize=authorize, storage=InMemoryMcpOAuthStorage())
            options = replace(options, mcp_oauth=oauth_configs)
        options = replace(options, cwd=workspace, cli_path=self.config.cli_path,
                          agent_engine=self.config.agent_engine, resume=resume,
                          startup_timeout=self.config.startup_timeout, turn_timeout=self.config.turn_timeout,
                          include_partial_messages=True, include_thoughts=False,
                          env={**options.env, **auth_env})
        client = KiroSDKClient(options)
        if options.kiro_auth_method == "client":
            client._require_client_auth_authorization = True
        if options.resume_from_store_bounded_read and authorizer is None:
            raise PermissionError("Bounded mirror recovery requires session authorization")
        if options.resume_from_store_bounded_read:
            async def load_bounded_recovery(store: Any, key: dict[str, str]) -> Any:
                if store is not client.options.session_store:
                    raise PermissionError("Session recovery store changed")
                timeout = min(turn_config.startup_timeout, options.session_store_archive_timeout or
                              turn_config.startup_timeout, 30.0)
                result = await self._session_recovery_operation(
                    store.load_bounded(key, max_entries=20000, max_bytes=8 * 1024 * 1024,
                                       skip_archives=True, retain_latest_archive=True), timeout)
                if result is _SESSION_RECOVERY_TIMEOUT:
                    raise TimeoutError("Bounded session recovery read timed out")
                return result
            client._bounded_recovery_loader = load_bounded_recovery
        tasks_factory = self.background_tasks_factory
        created_tasks = False
        if tasks_factory is not None:
            if authorizer is None or self.session_authorizer is not authorizer or self.config is not turn_config:
                raise PermissionError("Background tasks require session authorization")
            if session.background_tasks is None:
                manager = tasks_factory(workspace, session.runtime_id)
                if type(manager) is not BackgroundTaskManager:
                    if asyncio.iscoroutine(manager):
                        manager.close()
                    raise ValueError("Background task factory must return a new BackgroundTaskManager")
                if (manager._closed or manager._tasks or any(
                        item is not session and item.background_tasks is manager for item in self.sessions.values())):
                    raise ValueError("Background task managers must be new and session-specific")
                if self.background_tasks_factory is not tasks_factory:
                    raise PermissionError("Background task factory changed")
                session.background_tasks = manager
                session.background_tasks_factory = tasks_factory
                created_tasks = True
            if session.background_tasks_factory is not tasks_factory or session.background_tasks._closed:
                raise PermissionError("Background task manager changed")
            client.attach_background_tasks(session.background_tasks)
            client._require_agentcore_background_authorization = True
        elif session.background_tasks is not None:
            raise PermissionError("Background task factory changed")
        client._require_oauth_interaction_authorization = self.config.allow_mcp_oauth_interactions
        client._require_native_failure_authorization = (
            getattr(client, "_hook_failure_snapshot", None) is not None or
            options.native_hook_bridge_mode == "runtime-acp")
        client._require_mcp_failure_authorization = bool(
            getattr(client, "_mcp_failure_input_names", ()) or getattr(client, "_mcp_failure_output_names", ()))
        initial_guard = (self._session_policy_guard(session, client, request, turn_config, authorizer)
                         if (_requires_session_authorizer(client) or turn_config.reauthorize_output) else None)
        client._prompt_send_guard = initial_guard
        client._interaction_guard = initial_guard
        async def prepare_initial_prompt(session_id: str) -> None:
            if initial_guard is not None and await initial_guard() is not True:
                raise PermissionError("Session policy authorization changed")
            await self._state_io(_write_marker, session.directory / "session.json", self.config.agent_engine, session_id,
                          steering_pending=session.clear_steering)
        client._before_initial_prompt = prepare_initial_prompt
        async def prepare_initial_turn(session_id: str) -> None:
            if initial_guard is not None and await initial_guard() is not True:
                raise PermissionError("Session policy authorization changed")
            if session.clear_steering:
                result = await client.clear_steering()
                if not isinstance(result, dict) or result.get("cleared") is not True:
                    raise RuntimeError("Kiro did not clear pending steering")
                session.clear_steering = False
            await self._state_io(_write_marker, session.directory / "session.json", self.config.agent_engine, session_id,
                          steering_pending=session.clear_steering)
        client._before_initial_turn = prepare_initial_turn
        session.client = client
        session.broken = True
        session.initializing = True
        if options.kiro_auth_method == "client":
            def client_auth_factory_current() -> bool:
                return (token_factory is not None and self.kiro_token_provider_factory is token_factory and
                        authorizer is not None and self.session_authorizer is authorizer and
                        self.config is turn_config and not self._closing and
                        self.sessions.get(session.runtime_id) is session and session.client is client and
                        (session.initializing or not session.broken))
            client._client_auth_factory_guard = client_auth_factory_current
        try:
            if turn_config.mcp_oauth_preauthorize_servers and not options.mcp_oauth:
                raise ValueError("MCP preauthorization server overrides require matching OAuth configurations")
            if turn_config.preauthorize_mcp_oauth and options.mcp_oauth:
                server_source = options.mcp_servers
                if not isinstance(server_source, (dict, str, Path)):
                    raise ValueError("Invalid MCP preauthorization server configuration")
                servers = server_source if isinstance(server_source, dict) else {}
                overrides = _preauthorization_overrides(turn_config.mcp_oauth_preauthorize_servers,
                                                         turn_config.preauthorize_mcp_oauth)
                if (len(options.mcp_oauth) > 32 or set(overrides) - set(options.mcp_oauth) or
                        any((name in servers) == (name in overrides) for name in options.mcp_oauth)):
                    raise ValueError("MCP preauthorization requires unambiguous HTTP or SSE servers")
                override_snapshot = copy.deepcopy(overrides)
                server_snapshot = {name: copy.deepcopy(servers[name] if name in servers else overrides[name])
                                   for name in options.mcp_oauth}
                if any(not isinstance(server, dict) or server.get("type") not in ("http", "sse")
                       for server in server_snapshot.values()):
                    raise ValueError("MCP preauthorization requires HTTP or SSE servers")
                oauth_snapshot = dict(options.mcp_oauth)
                oauth_settings = {
                    name: (auth.redirect_uri, auth.authorize, copy.deepcopy(auth.allowed_origins),
                           auth.client_name, auth.scope, auth.storage, auth.sse_cancel_token_provider,
                           auth.sse_cancel_retry_with_stored_token)
                    for name, auth in oauth_snapshot.items()
                }

                def preauthorization_current() -> bool:
                    try:
                        return (self.config is turn_config and self.session_authorizer is authorizer and
                                authorizer is not None and not self._closing and
                                self.sessions.get(session.runtime_id) is session and session.client is client and
                                session.initializing and session.lock.locked() and
                                options.mcp_servers is server_source and isinstance(options.mcp_oauth, dict) and
                                turn_config.mcp_oauth_preauthorize_servers == override_snapshot and
                                options.mcp_oauth.keys() == oauth_snapshot.keys() and all(
                                    options.mcp_oauth[name] is oauth_snapshot[name] and
                                    (servers.get(name) == server if name not in override_snapshot
                                     else name not in servers and override_snapshot[name] == server) and
                                    oauth_snapshot[name].redirect_uri == oauth_settings[name][0] and
                                    oauth_snapshot[name].authorize is oauth_settings[name][1] and
                                    oauth_snapshot[name].allowed_origins == oauth_settings[name][2] and
                                    oauth_snapshot[name].client_name == oauth_settings[name][3] and
                                    oauth_snapshot[name].scope == oauth_settings[name][4] and
                                    oauth_snapshot[name].storage is oauth_settings[name][5] and
                                    oauth_snapshot[name].sse_cancel_token_provider is oauth_settings[name][6] and
                                    oauth_snapshot[name].sse_cancel_retry_with_stored_token == oauth_settings[name][7]
                                    for name, server in server_snapshot.items()))
                    except Exception:
                        return False

                async def authorize_preauthorization() -> bool:
                    return (preauthorization_current() and await self._authorize(request, session.runtime_id) and
                            preauthorization_current())

                async def check_connected_mcp_configuration() -> None:
                    if not await authorize_preauthorization():
                        raise PermissionError("MCP preauthorization is not authorized")
                    for name, expected in server_snapshot.items():
                        actual = client._mcp_config_for_name(name)
                        if (not isinstance(actual, dict) or set(actual) - {"type", "url", "headers"} or
                                actual.get("type") != expected.get("type") or
                                actual.get("url") != expected.get("url") or
                                actual.get("headers") not in (None, {}, [])):
                            raise ValueError("Preauthorized MCP server configuration changed")
                    if not await authorize_preauthorization():
                        raise PermissionError("MCP preauthorization authorization changed")

                preauthorization_guard = preauthorization_current
                client._before_mcp_servers_setup = check_connected_mcp_configuration
                for name, server in server_snapshot.items():
                    if not await authorize_preauthorization():
                        raise PermissionError("MCP preauthorization is not authorized")
                    await preauthorize_mcp_oauth(server, oauth_snapshot[name], timeout=turn_config.startup_timeout)
                    if not await authorize_preauthorization():
                        raise PermissionError("MCP preauthorization authorization changed")
            await client.connect()
            if self.config.allow_mcp_oauth_interactions and options.mcp_oauth:
                async def wait_for_oauth_servers() -> None:
                    names = set()
                    for name in options.mcp_oauth:
                        inline = client._plugin_inline_mcp_configs.get(name)
                        if inline is None:
                            names.add(name)
                        elif inline[0] == client._selected_agent() and inline[0] == client._current_mode:
                            names.add(inline[1])
                    while True:
                        status = await client.get_mcp_status()
                        servers = {item.get("name"): item.get("status")
                                   for item in (status or {}).get("mcpServers", [])
                                   if isinstance(item, dict)}
                        if any(servers.get(name) in ("failed", "needs-auth", "disabled") for name in names):
                            raise RuntimeError("MCP OAuth server could not connect")
                        if all(servers.get(name) == "connected" for name in names):
                            return
                        await asyncio.sleep(0.1)
                await asyncio.wait_for(wait_for_oauth_servers(), self.config.startup_timeout)
            await self._state_io(_write_marker, session.directory / "session.json", self.config.agent_engine, client.session_id,
                          steering_pending=session.clear_steering)
        except BaseException:
            session.initializing = False
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            if created_tasks:
                with contextlib.suppress(Exception):
                    await self._close_background_tasks(session)
            raise
        finally:
            client._before_initial_prompt = None
            client._before_initial_turn = None
            client._before_mcp_servers_setup = None
        session.broken = False
        return client, initial_guard

    async def _discard_client(self, session: _Session) -> None:
        client = session.client
        session.prewarm_guard = None
        session.initializing = False
        session.broken = True
        for item in session.interactions.values():
            if item.kind in ("mcp_oauth",) and not item.future.done():
                item.future.set_result(None)
        if client is None:
            session.broken = False
            return
        try:
            await client.disconnect()
        except Exception as exc:
            logger.warning("Kiro cleanup failed (%s)", type(exc).__name__)
            raise RuntimeError("Kiro cleanup failed") from None
        session.client = None
        session.broken = False

    async def _close_background_tasks(self, session: _Session) -> None:
        manager = session.background_tasks
        if manager is None:
            return
        await manager.aclose()
        if session.background_tasks is manager:
            session.background_tasks = None
            session.background_tasks_factory = None

    async def _turn(self, session: _Session, prompt: Any, request: Request,
                    turn_config: AgentCoreConfig, authorizer: Optional[SessionAuthorizer]):
        client, guard = await self._client(session, request, turn_config, authorizer)
        if guard is None and session.prewarm_guard is not None and session.client is client:
            guard = session.prewarm_guard
        session.prewarm_guard = None
        client_session_id = client.session_id
        preset_generation = (client._preset_generation, client._connection_generation)
        try:
            if not isinstance(client_session_id, str) or not client_session_id:
                raise ProtocolError("Kiro session identity is unavailable")
            if ((_requires_session_authorizer(client) or turn_config.reauthorize_output) and
                    (authorizer is None or self.session_authorizer is not authorizer or self.config is not turn_config)):
                raise PermissionError("Session policy mapping requires session authorization")
            if _requires_session_authorizer(client) or turn_config.reauthorize_output:
                if guard is None and client._prompt_send_guard is None and client._interaction_guard is None:
                    guard = self._session_policy_guard(session, client, request, turn_config, authorizer)
                    client._prompt_send_guard = guard
                    client._interaction_guard = guard
                elif client._prompt_send_guard is not guard or client._interaction_guard is not guard:
                    raise ProtocolError("Session policy authorization guard is inconsistent")
            session.control_failed = False
            session.preprocessing_interrupted = False
            if client._initial_prompt_active:
                session.initializing = False
                session.turn_ready = True
                initial_result = None
                async with _aclosing(client.receive_response()) as initial_messages:
                    async for message in initial_messages:
                        if isinstance(message, ResultMessage):
                            initial_result = message
                session.turn_ready = False
                if initial_result is None or initial_result.is_error:
                    raise RuntimeError("Initial agent prompt failed")
            if session.clear_steering:
                result = await asyncio.wait_for(client.clear_steering(), min(self.config.startup_timeout, 10.0))
                if not isinstance(result, dict) or result.get("cleared") is not True:
                    raise RuntimeError("Kiro did not clear pending steering")
                await self._state_io(_write_marker, session.directory / "session.json", self.config.agent_engine, client.session_id)
                session.clear_steering = False
            try:
                await client.query(prompt)
            except PromptBlockedError as exc:
                if client.session_id == client_session_id and exc.session_id == client_session_id:
                    raise _InvocationPromptBlocked(exc, client_session_id, client, preset_generation) from None
                raise
            except asyncio.CancelledError:
                if session.preprocessing_interrupted:
                    # Let the interrupt finish before the invocation discards
                    # the client and releases the session to another request.
                    async with session.control_lock:
                        pass
                    raise RuntimeError("Agent prompt preparation interrupted") from None
                raise
            if (client._preset_generation, client._connection_generation) != preset_generation:
                raise ProtocolError("Prompt context or connection changed during invocation")
            session.initializing = False
            session.turn_ready = True
            try:
                async with _aclosing(client.receive_response()) as messages:
                    async for message in messages:
                        yield client, client_session_id, preset_generation, message
            except asyncio.CancelledError:
                if session.control_failed:
                    raise RuntimeError("Agent control request failed") from None
                raise
        finally:
            if ((turn_config.reauthorize_output or _requires_session_authorizer(client)) and
                    session.client is not client):
                await self._stop_replaced_client(session, client)
            if guard is not None:
                if client._prompt_send_guard is guard:
                    client._prompt_send_guard = None
                if client._interaction_guard is guard:
                    client._interaction_guard = None
            session.initializing = False
            session.turn_ready = False
            for ident, item in list(session.interactions.items()):
                if item.kind != "mcp_oauth":
                    if not item.future.done():
                        item.future.set_result(None)
                    session.interactions.pop(ident, None)

    @staticmethod
    def _result(runtime_id: str, message: ResultMessage) -> dict[str, Any]:
        return {"response": message.result, "is_error": message.is_error,
                "stop_reason": message.stop_reason, "session_id": runtime_id,
                "kiro_session_id": message.session_id}

    def _prompt_block_details(self, blocked: _InvocationPromptBlocked, prompt: Any,
                              config: AgentCoreConfig, authorizer: Optional[SessionAuthorizer]) -> Optional[dict[str, Any]]:
        if (self.config is not config or self.session_authorizer is not authorizer or authorizer is None or
                not config.allow_prompt_block_details):
            return None
        error = blocked.error
        def messages(value: Any) -> list[str]:
            if (type(value) is not tuple or len(value) > 32 or
                    any(type(item) is not str or len(item) > 8192 or "\0" in item or
                        any(0xD800 <= ord(char) <= 0xDFFF for char in item) for item in value)):
                raise ValueError("Invalid prompt block metadata")
            result = list(value)
            if sum(len(item.encode("utf-8")) for item in result) > 65536:
                raise ValueError("Prompt block metadata exceeds limits")
            return result
        try:
            if (type(error) is not PromptBlockedError or
                    error.session_id != blocked.session_id):
                return None
            details: dict[str, Any] = {
                "kind": "blocked",
                "reasons": messages(error.reasons),
                "warnings": messages(error.warnings),
            }
            _validate_introspection_result(details, strict_builtins=True)
            return details
        except (ValueError, TypeError, UnicodeError, OverflowError, AttributeError):
            return None

    async def _stream(self, session: _Session, prompt: Any, lease: _Lease, include_events: bool,
                      request: Request, turn_config: AgentCoreConfig, authorizer: Optional[SessionAuthorizer],
                      details_config: Optional[AgentCoreConfig] = None,
                      details_authorizer: Optional[SessionAuthorizer] = None):
        complete = False
        output_client = None
        output_session_id = None
        output_preset_generation = None
        output_required = turn_config.reauthorize_output
        try:
            result = None
            async with _aclosing(self._turn(session, prompt, request, turn_config, authorizer)) as messages:
                async for client, client_session_id, preset_generation, message in messages:
                    if output_client is None:
                        output_client = client
                        output_session_id = client_session_id
                        output_preset_generation = preset_generation
                        output_required = output_required or _requires_session_authorizer(client)
                    elif (output_client is not client or output_session_id != client_session_id or
                          output_preset_generation != preset_generation):
                        raise ProtocolError("Session output client changed")
                    if include_events and is_dataclass(message):
                        if output_required:
                            await self._ensure_output_authorized(session, request, turn_config, authorizer, client,
                                                                 client_session_id, preset_generation)
                        event = {"type": "sdk_event", "event_type": type(message).__name__, "message": asdict(message)}
                        yield "data: " + json.dumps(event, allow_nan=False) + "\n\n"
                    if isinstance(message, StreamEvent) and message.agent_id is None:
                        content = message.event.get("content")
                        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                            if output_required:
                                await self._ensure_output_authorized(session, request, turn_config, authorizer, client,
                                                                     client_session_id, preset_generation)
                            yield "data: " + json.dumps({"type": "text_delta", "text": content["text"]}) + "\n\n"
                        else:
                            delta = message.event.get("delta")
                            if (isinstance(delta, dict) and delta.get("type") == "text_delta" and
                                    isinstance(delta.get("text"), str)):
                                if output_required:
                                    await self._ensure_output_authorized(session, request, turn_config, authorizer, client,
                                                                         client_session_id, preset_generation)
                                yield "data: " + json.dumps({"type": "text_delta", "text": delta["text"]}) + "\n\n"
                    elif isinstance(message, ResultMessage):
                        result = message
            if result is None:
                raise RuntimeError("Kiro returned no terminal result")
            if result.is_error:
                if output_required:
                    await self._ensure_output_authorized(session, request, turn_config, authorizer, output_client,
                                                         output_session_id, output_preset_generation)
                with contextlib.suppress(Exception):
                    await self._discard_client(session)
            if output_required:
                await self._ensure_output_authorized(session, request, turn_config, authorizer, output_client,
                                                     output_session_id, output_preset_generation, detached=result.is_error)
            complete = True
            yield "data: " + json.dumps({"type": "result", **self._result(session.runtime_id, result)}) + "\n\n"
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            raise
        except _DeviceAuthRequired:
            yield 'data: {"type":"error","message":"Device authentication required"}\n\n'
        except _InvocationPromptBlocked as blocked:
            output_required = (details_config is not None or turn_config.reauthorize_output or
                               _requires_session_authorizer(blocked.client))
            allowed = True
            if details_config is not None and output_required:
                try:
                    await self._ensure_output_authorized(session, request, turn_config, authorizer, blocked.client,
                                                         blocked.session_id, blocked.preset_generation)
                except PermissionError:
                    allowed = False
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            if details_config is not None and output_required and allowed:
                try:
                    await self._ensure_output_authorized(session, request, turn_config, authorizer, blocked.client,
                                                         blocked.session_id, blocked.preset_generation, detached=True)
                except PermissionError:
                    allowed = False
            details = (self._prompt_block_details(blocked, prompt, details_config, details_authorizer)
                       if allowed and details_config is not None and session.client is None else None)
            if details is not None:
                yield "data: " + json.dumps({"type": "error", "message": "Prompt blocked",
                                             "prompt_block": details}, allow_nan=False) + "\n\n"
            else:
                yield 'data: {"type":"error","message":"Agent invocation failed"}\n\n'
        except Exception as exc:
            logger.warning("Kiro invocation failed (%s)", type(exc).__name__)
            if output_required:
                await self._stop_replaced_client(session, output_client)
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            yield 'data: {"type":"error","message":"Agent invocation failed"}\n\n'
        finally:
            try:
                async def finish_stream() -> None:
                    try:
                        if not complete:
                            await self._discard_client(session)
                    finally:
                        await lease.release()
                cleanup = asyncio.create_task(finish_stream())
                self._cleanup_operations.add(cleanup)
                def finished(task: asyncio.Task) -> None:
                    self._cleanup_operations.discard(task)
                    self._consume_authorizer_result(task)
                cleanup.add_done_callback(finished)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(cleanup)
            finally:
                pass

    async def invoke(self, request: Request):
        if self._closing:
            return JSONResponse({"error": "Service is closing"}, status_code=503)
        runtime_id = request.headers.get(_SESSION_HEADER, "")
        if not _SESSION_RE.fullmatch(runtime_id):
            return JSONResponse({"error": "A valid AgentCore runtime session header is required"}, status_code=400)
        request_config = self.config
        body_limit_exceeded = False
        cached = getattr(request, "_body", None)
        if cached is not None:
            if len(cached) > request_config.max_body_bytes:
                return JSONResponse({"error": "Request is too large"}, status_code=413)
        else:
            original_receive = request.receive
            received = 0
            async def bounded_receive():
                nonlocal received, body_limit_exceeded
                if body_limit_exceeded:
                    raise _RequestBodyTooLarge()
                message = await original_receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > min(request_config.max_body_bytes, self.config.max_body_bytes):
                        body_limit_exceeded = True
                        raise _RequestBodyTooLarge()
                return message
            request = Request(request.scope, receive=bounded_receive)
        authorizer = self.session_authorizer
        if request_config.reauthorize_output and authorizer is None:
            return JSONResponse({"error": "Session access denied"}, status_code=403)
        authorized = await self._authorize(request, runtime_id)
        if body_limit_exceeded:
            return JSONResponse({"error": "Request is too large"}, status_code=413)
        if not authorized or self.session_authorizer is not authorizer:
            return JSONResponse({"error": "Session access denied"}, status_code=403)
        if (self.config is not request_config and
                (request_config.reauthorize_output or self.config.reauthorize_output)):
            return JSONResponse({"error": "Session access denied"}, status_code=403)
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > self.config.max_body_bytes:
                    return JSONResponse({"error": "Request is too large"}, status_code=413)
                body.extend(chunk)
            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError("Duplicate JSON key")
                    obj[key] = value
                return obj
            def constant(value):
                raise ValueError("Invalid JSON constant")
            def integer(value):
                if len(value) > 65:
                    raise ValueError("JSON integer is too large")
                return int(value)
            def floating(value):
                if len(value) > 128:
                    raise ValueError("JSON number is too large")
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("JSON number is not finite")
                return number
            payload = json.loads(body, object_pairs_hook=unique, parse_constant=constant,
                                 parse_int=integer, parse_float=floating)
        except _RequestBodyTooLarge:
            return JSONResponse({"error": "Request is too large"}, status_code=413)
        except (ClientDisconnect, ValueError, UnicodeDecodeError, RecursionError):
            return JSONResponse({"error": "Invalid JSON request"}, status_code=400)
        if self.session_authorizer is not authorizer:
            return JSONResponse({"error": "Session access denied"}, status_code=403)
        if self._closing:
            return JSONResponse({"error": "Service is closing"}, status_code=503)
        if (self.config is not request_config and
                (request_config.reauthorize_output or self.config.reauthorize_output)):
            return JSONResponse({"error": "Session access denied"}, status_code=403)
        if authorizer is None and self.pool_lock is not None:
            async with self.pool_lock:
                existing = self.sessions.get(runtime_id)
                if (existing is not None and existing.client is not None and
                        _requires_session_authorizer(existing.client)):
                    return JSONResponse({"error": "Session access denied"}, status_code=403)
        if not isinstance(payload, dict) or set(payload) - {
                "prompt", "stream", "include_events", "include_prompt_block_details"}:
            if not isinstance(payload, dict) or "action" not in payload:
                return JSONResponse({"error": "Expected prompt and optional streaming flags"}, status_code=400)
            action, message = payload.get("action"), payload.get("message")
            if action in ("auth_start", "auth_status", "auth_cancel", "auth_logout"):
                return await self._device_action(runtime_id, action, payload)
            if action in ("interactions", "respond"):
                return await self._interactions(runtime_id, payload, authorizer, request)
            if action == "background_task_events_stream":
                return await self._background_task_event_stream(runtime_id, payload, request, request_config, authorizer)
            if action in ("background_task_archive", "background_task_archive_get", "background_task_archive_delete",
                          "background_task_archive_events", "background_task_archive_events_get",
                          "background_task_archive_events_delete", "background_task_checkpoint_events",
                          "background_task_checkpoint_events_get", "background_task_checkpoint_events_delete"):
                return await self._background_task_archive(runtime_id, payload, request, request_config, authorizer)
            if action in ("background_tasks", "background_task_status", "background_task_stop", "background_task_result",
                          "background_task_start", "background_task_retry", "background_task_forget", "background_task_events"):
                return await self._background_tasks_control(runtime_id, payload, request, request_config, authorizer)
            if action in ("code_status", "code_init", "code_overview") and set(payload) == {"action"}:
                return await self._code_intelligence_control(runtime_id, action)
            if action in ("list_kiro_hooks", "available_commands", "server_info", "context_usage", "mcp_status", "sdk_permission_mode", "session_messages"):
                return await self._introspection_control(runtime_id, action, payload, request, request_config, authorizer)
            if action == "compact" and set(payload) <= {"action", "include_events"}:
                include_compact_events = payload.get("include_events", False)
                if not isinstance(include_compact_events, bool):
                    return JSONResponse({"error": "Invalid compact event flag"}, status_code=400)
                return await self._compact_control(
                    runtime_id, include_compact_events, request=request, config=request_config, authorizer=authorizer)
            if action == "mcp_reconnect" and set(payload) == {"action", "server_name", "start_oauth"}:
                server_name, start_oauth = payload.get("server_name"), payload.get("start_oauth")
                try:
                    if (not isinstance(server_name, str) or not _validate_prompt(server_name, 256) or
                            not isinstance(start_oauth, bool)):
                        return JSONResponse({"error": "Invalid MCP reconnection request"}, status_code=400)
                except (ValueError, UnicodeEncodeError):
                    return JSONResponse({"error": "Invalid MCP reconnection request"}, status_code=400)
                return await self._reconnect_mcp(runtime_id, server_name, start_oauth)
            if action == "mcp_revoke_oauth" and set(payload) == {"action", "server_name"}:
                server_name = payload.get("server_name")
                try:
                    if not isinstance(server_name, str) or not _validate_prompt(server_name, 256):
                        return JSONResponse({"error": "Invalid MCP server name"}, status_code=400)
                except (ValueError, UnicodeEncodeError):
                    return JSONResponse({"error": "Invalid MCP server name"}, status_code=400)
                return await self._revoke_mcp_oauth(runtime_id, server_name)
            if action == "interrupt" and set(payload) == {"action"}:
                return await self._control(runtime_id, action, None, authorizer)
            if action == "mcp_cancel_tools" and set(payload) == {"action", "server_name"}:
                server_name = payload.get("server_name")
                try:
                    if not isinstance(server_name, str) or not _validate_prompt(server_name, 256):
                        return JSONResponse({"error": "Invalid MCP server name"}, status_code=400)
                except (ValueError, UnicodeEncodeError):
                    return JSONResponse({"error": "Invalid MCP server name"}, status_code=400)
                return await self._control(runtime_id, action, server_name, authorizer)
            if action == "steer" and set(payload) == {"action", "message"}:
                if self.config.agent_engine != "v3":
                    return JSONResponse({"error": "Steering requires Kiro V3"}, status_code=400)
                try:
                    if not isinstance(message, str) or not _validate_prompt(message, self.config.max_prompt_bytes):
                        return JSONResponse({"error": "Invalid steering message"}, status_code=400)
                except (ValueError, UnicodeEncodeError):
                    return JSONResponse({"error": "Invalid steering message"}, status_code=400)
                return await self._control(runtime_id, action, message, authorizer)
            if action == "prewarm" and set(payload) == {"action"}:
                return await self._prewarm(runtime_id, request, request_config, authorizer)
            return JSONResponse({"error": "Invalid control request"}, status_code=400)
        prompt = payload.get("prompt")
        stream = payload.get("stream", False)
        include_events = payload.get("include_events", False)
        include_block_details = payload.get("include_prompt_block_details", False)
        if (not isinstance(stream, bool) or not isinstance(include_events, bool) or
                not isinstance(include_block_details, bool)):
            return JSONResponse({"error": "Invalid prompt or stream flag"}, status_code=400)
        wants_stream = stream or "text/event-stream" in request.headers.get("accept", "")
        details_config = self.config
        if include_block_details and (not details_config.allow_prompt_block_details or authorizer is None):
            return JSONResponse({"error": "Prompt block details require explicit enablement and a session authorizer"},
                                status_code=403)
        if include_events and not self.config.allow_sdk_events:
            return JSONResponse({"error": "SDK events are not enabled"}, status_code=403)
        if include_events and not wants_stream:
            return JSONResponse({"error": "SDK events require streaming"}, status_code=400)
        try:
            within_limit = _validate_prompt(prompt, self.config.max_prompt_bytes)
        except (ValueError, UnicodeEncodeError, RecursionError):
            return JSONResponse({"error": "Invalid prompt content"}, status_code=400)
        if not within_limit:
            return JSONResponse({"error": "Prompt is too large"}, status_code=413)
        try:
            lease, unavailable = await self._lease(runtime_id)
        except Exception as exc:
            logger.warning("Session preparation failed (%s)", type(exc).__name__)
            return JSONResponse({"error": "Session unavailable", "is_error": True})
        if unavailable == "capacity":
            return JSONResponse({"error": "Session capacity reached"}, status_code=429)
        if unavailable == "busy":
            return JSONResponse({"error": "Session is busy"}, status_code=409)
        assert lease is not None
        session = lease.session
        if (self.config is not request_config and
                (request_config.reauthorize_output or self.config.reauthorize_output)):
            await lease.release()
            return JSONResponse({"error": "Session access denied"}, status_code=403)
        if include_block_details and (
                self.config is not details_config or self.session_authorizer is not authorizer):
            await lease.release()
            return JSONResponse({"error": "Session access denied"}, status_code=403)
        if wants_stream:
            return _LeaseStreamingResponse(
                self._stream(session, prompt, lease, include_events, request, request_config, authorizer,
                             details_config if include_block_details else None, authorizer),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store"},
                background=BackgroundTask(lease.release), lease=lease)
        turn_config = request_config
        complete = False
        response_owns_lease = False
        output_client = None
        output_session_id = None
        output_preset_generation = None
        output_required = turn_config.reauthorize_output
        try:
            result = None
            async with _aclosing(self._turn(session, prompt, request, turn_config, authorizer)) as messages:
                async for client, client_session_id, preset_generation, message in messages:
                    if output_client is None:
                        output_client = client
                        output_session_id = client_session_id
                        output_preset_generation = preset_generation
                        output_required = output_required or _requires_session_authorizer(client)
                    elif (output_client is not client or output_session_id != client_session_id or
                          output_preset_generation != preset_generation):
                        raise ProtocolError("Session output client changed")
                    if isinstance(message, ResultMessage):
                        result = message
            if result is None:
                raise RuntimeError("Kiro returned no terminal result")
            if result.is_error:
                if output_required:
                    await self._ensure_output_authorized(session, request, turn_config, authorizer, output_client,
                                                         output_session_id, output_preset_generation)
                with contextlib.suppress(Exception):
                    await self._discard_client(session)
            if output_required:
                await self._ensure_output_authorized(session, request, turn_config, authorizer, output_client,
                                                     output_session_id, output_preset_generation, detached=result.is_error)
                payload = json.dumps(self._result(runtime_id, result), allow_nan=False,
                                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                response = _LeaseStreamingResponse(
                    self._reauthorized_json_body(payload, session, request, turn_config, authorizer,
                                                 output_client, output_session_id, output_preset_generation, lease,
                                                 detached=result.is_error),
                    media_type="application/json", headers={"Cache-Control": "no-store"},
                    background=BackgroundTask(lease.release), lease=lease)
                response_owns_lease = True
                complete = True
                return response
            complete = True
            return JSONResponse(self._result(runtime_id, result))
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            raise
        except _DeviceAuthRequired:
            return JSONResponse({"error": "Device authentication required", "is_error": True})
        except _InvocationPromptBlocked as blocked:
            output_required = (include_block_details or turn_config.reauthorize_output or
                               _requires_session_authorizer(blocked.client))
            allowed = True
            if include_block_details and output_required:
                try:
                    await self._ensure_output_authorized(session, request, turn_config, authorizer, blocked.client,
                                                         blocked.session_id, blocked.preset_generation)
                except PermissionError:
                    allowed = False
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            if include_block_details and output_required and allowed:
                try:
                    await self._ensure_output_authorized(session, request, turn_config, authorizer, blocked.client,
                                                         blocked.session_id, blocked.preset_generation, detached=True)
                except PermissionError:
                    allowed = False
            details = (self._prompt_block_details(blocked, prompt, details_config, authorizer)
                       if allowed and include_block_details and session.client is None else None)
            if details is not None:
                if output_required:
                    payload = json.dumps({"error": "Prompt blocked", "is_error": True, "prompt_block": details},
                                         allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    response = _LeaseStreamingResponse(
                        self._reauthorized_json_body(payload, session, request, turn_config, authorizer,
                                                     blocked.client, blocked.session_id, blocked.preset_generation,
                                                     lease, detached=True),
                        media_type="application/json", headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(lease.release), lease=lease)
                    response_owns_lease = True
                    complete = True
                    return response
                return JSONResponse({"error": "Prompt blocked", "is_error": True, "prompt_block": details},
                                    headers={"Cache-Control": "no-store"})
            return JSONResponse({"error": "Agent invocation failed", "is_error": True})
        except Exception as exc:
            logger.warning("Kiro invocation failed (%s)", type(exc).__name__)
            if output_required:
                await self._stop_replaced_client(session, output_client)
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            # AgentCore may replay 5xx responses even when the caller disables
            # SDK retries. A failed turn can already have run tools, so return
            # a structured error without triggering a service-level retry.
            return JSONResponse({"error": "Agent invocation failed", "is_error": True})
        finally:
            try:
                if not complete:
                    with contextlib.suppress(Exception):
                        await self._discard_client(session)
            finally:
                if not response_owns_lease:
                    await lease.release()

    async def close(self) -> None:
        self._closing = True
        operations = set(self._cleanup_operations | self._state_io_operations)
        watchers = tuple(self._background_archive_watchers)
        for watcher in watchers:
            watcher.cancel()
        sessions = list(self.sessions.values())
        workers = getattr(self, "_shutdown_workers", None)
        if workers is None:
            workers = self._shutdown_workers = {}
        owned = {}
        for session in sessions:
            actions = {"client": lambda session=session: self._discard_client(session),
                       "background": lambda session=session: self._close_background_tasks(session)}
            if session.device_login is not None:
                actions["login"] = session.device_login.cancel
            for kind, action in actions.items():
                key = session.runtime_id, kind
                task = workers.get(key)
                if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
                    task = workers[key] = asyncio.create_task(action())
                    self._cleanup_operations.add(task)
                    task.add_done_callback(self._cleanup_operations.discard)
                    task.add_done_callback(self._consume_authorizer_result)
                owned[key] = task
        all_tasks = operations | set(watchers) | set(owned.values())
        if all_tasks:
            _, pending = await asyncio.wait(all_tasks, timeout=15)
        else:
            pending = set()
        prior_failed = bool(operations & pending) or any(
            task.done() and (task.cancelled() or task.exception() is not None) for task in operations)
        retained = {}
        for session in sessions:
            tasks = [task for (runtime_id, _), task in owned.items() if runtime_id == session.runtime_id]
            if prior_failed or any(not task.done() or task.cancelled() or task.exception() is not None for task in tasks):
                retained[session.runtime_id] = session
            else:
                for key in tuple(workers):
                    if key[0] == session.runtime_id:
                        workers.pop(key, None)
        self.sessions = retained
        if pending or retained or prior_failed:
            failures = tuple((runtime_id, kind,
                              "pending" if not task.done() else "cancelled" if task.cancelled() else "failed")
                             for (runtime_id, kind), task in owned.items()
                             if not task.done() or task.cancelled() or task.exception() is not None)
            error = RuntimeError("Session cleanup failed or is incomplete; close again")
            # Safe action/status identifiers, never exception messages, causes,
            # task objects or transport frames containing credentials.
            error.cleanup_failures = failures
            error.cleanup_failure_types = tuple(
                (runtime_id, kind, type(task.exception()).__name__)
                for (runtime_id, kind), task in owned.items()
                if task.done() and not task.cancelled() and task.exception() is not None)
            error.operation_failure_types = tuple(sorted(
                type(task.exception()).__name__ for task in operations
                if task.done() and not task.cancelled() and task.exception() is not None))
            error.pending_operations = len(operations & pending)
            raise error


    async def _prewarm(self, runtime_id: str, request: Request, config: AgentCoreConfig,
                       authorizer: Optional[SessionAuthorizer]) -> JSONResponse:
        """Start the session's Kiro client without a model prompt; the next prompt reuses it."""
        def response(value: dict[str, Any], status: int = 200) -> JSONResponse:
            return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

        try:
            lease, unavailable = await self._lease(runtime_id)
        except Exception as exc:
            logger.warning("Session preparation failed (%s)", type(exc).__name__)
            return response({"error": "Session unavailable", "is_error": True})
        if unavailable == "capacity":
            return response({"error": "Session capacity reached"}, 429)
        if unavailable == "busy":
            return response({"error": "Session is busy"}, 409)
        assert lease is not None
        session = lease.session
        try:
            if self.config is not config and (config.reauthorize_output or self.config.reauthorize_output):
                return response({"error": "Session access denied"}, 403)
            already_warm = session.client is not None and not session.broken
            client, guard = await self._client(session, request, config, authorizer)
            if guard is not None:
                session.prewarm_guard = guard
            if ((_requires_session_authorizer(client) or config.reauthorize_output) and
                    (authorizer is None or self.session_authorizer is not authorizer or self.config is not config)):
                raise PermissionError("Session policy mapping requires session authorization")
            if not await self._authorize(request, runtime_id) or self.session_authorizer is not authorizer:
                raise PermissionError("Session authorization changed during prewarm")
            if not isinstance(client.session_id, str) or not client.session_id:
                raise ProtocolError("Kiro session identity is unavailable")
            return response({"action": "prewarm", "status": "ready", "already_warm": already_warm, "is_error": False})
        except PermissionError:
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            return response({"error": "Session access denied"}, 403)
        except Exception as exc:
            logger.warning("Session prewarm failed (%s)", type(exc).__name__)
            with contextlib.suppress(Exception):
                await self._discard_client(session)
            return response({"error": "Session unavailable", "is_error": True})
        finally:
            await lease.release()


def create_agentcore_app(config: Optional[AgentCoreConfig] = None, *,
                         options_factory: Optional[OptionsFactory] = None,
                         session_authorizer: Optional[SessionAuthorizer] = None,
                         background_tasks_factory: Optional[BackgroundTasksFactory] = None,
                         background_task_options_factory: Optional[BackgroundTaskOptionsFactory] = None,
                         background_request_store: Any = None,
                         background_result_store: Any = None,
                         kiro_token_provider_factory: Optional[KiroTokenProviderFactory] = None,
                         kiro_idle_token_authorizer: Optional[KiroIdleTokenAuthorizer] = None) -> Starlette:
    """Create the AgentCore Runtime HTTP app. Use one worker per runtime instance.

    The asynchronous session authorizer must return True for each authorized
    invocation and must validate the caller's identity itself. It is required
    unless the config sets ``allow_unauthorized_sessions``.
    Client-owned Kiro authentication additionally needs a per-session
    ``kiro_token_provider_factory(workspace, session_id)``.
    Authorized idle refresh additionally needs an independent asynchronous
    ``kiro_idle_token_authorizer(session_id)``.
    A background task factory must return a new, empty manager per Runtime
    session. The app owns its lifetime and closes it on eviction or shutdown.
    """
    if session_authorizer is not None and not callable(session_authorizer):
        raise ValueError("session_authorizer must be callable")
    if kiro_token_provider_factory is not None and not callable(kiro_token_provider_factory):
        raise ValueError("kiro_token_provider_factory must be callable")
    if kiro_token_provider_factory is not None and session_authorizer is None:
        raise ValueError("Client-owned Kiro authentication requires session_authorizer")
    if kiro_idle_token_authorizer is not None and not callable(kiro_idle_token_authorizer):
        raise ValueError("kiro_idle_token_authorizer must be callable")
    if kiro_idle_token_authorizer is not None and (session_authorizer is None or kiro_token_provider_factory is None):
        raise ValueError("Idle Kiro authentication requires session authorization and a token provider factory")
    if background_tasks_factory is not None and not callable(background_tasks_factory):
        raise ValueError("background_tasks_factory must be callable")
    if background_tasks_factory is not None and session_authorizer is None:
        raise ValueError("Background task control requires session_authorizer")
    if background_task_options_factory is not None and not callable(background_task_options_factory):
        raise ValueError("background_task_options_factory must be callable")
    if background_task_options_factory is not None and background_tasks_factory is None:
        raise ValueError("Background task start requires background_tasks_factory")
    if background_request_store is not None and not inspect.iscoroutinefunction(getattr(background_request_store, "claim", None)):
        raise ValueError("background_request_store must provide an async claim method")
    if background_request_store is not None and background_task_options_factory is None:
        raise ValueError("background_request_store requires background task start")
    if background_result_store is not None and (
            not inspect.iscoroutinefunction(getattr(background_result_store, "save", None)) or
            not inspect.iscoroutinefunction(getattr(background_result_store, "load", None))):
        raise ValueError("background_result_store must provide async save and load methods")
    if (background_result_store is not None and getattr(background_result_store, "delete", None) is not None and
            not inspect.iscoroutinefunction(getattr(background_result_store, "delete", None))):
        raise ValueError("background_result_store delete must be async")
    resolved_config = config or AgentCoreConfig.from_env()
    if resolved_config.allow_background_task_retry and (
            background_request_store is None or
            not inspect.iscoroutinefunction(getattr(background_request_store, "exists", None))):
        raise ValueError("Background task retry requires a request store with async exists")
    if background_result_store is not None and (
            background_tasks_factory is None or not resolved_config.allow_background_task_results):
        raise ValueError("background_result_store requires authorized background task results")
    if resolved_config.auto_archive_background_results and (
            background_result_store is None or background_task_options_factory is None):
        raise ValueError("Automatic background archive requires a result store and background task start")
    if resolved_config.archive_background_task_events and (
            background_result_store is None or
            any(not inspect.iscoroutinefunction(getattr(background_result_store, name, None))
                for name in ("save_events", "load_events", "delete_events"))):
        raise ValueError("Background event archive requires a store with async event methods")
    if resolved_config.allow_background_event_checkpoints and (
            background_result_store is None or
            any(not inspect.iscoroutinefunction(getattr(background_result_store, name, None))
                for name in ("save_event_checkpoint", "load_event_checkpoint", "delete_event_checkpoint"))):
        raise ValueError("Background event checkpoints require a store with async checkpoint methods")
    if (resolved_config.allow_background_task_results or resolved_config.allow_background_task_events) and background_tasks_factory is None:
        raise ValueError("Background task output requires background_tasks_factory")
    if resolved_config.allow_mcp_oauth_interactions and session_authorizer is None:
        raise ValueError("MCP OAuth interactions require session_authorizer")
    if resolved_config.reauthorize_output and session_authorizer is None:
        raise ValueError("Output reauthorization requires session_authorizer")
    if session_authorizer is None:
        if not resolved_config.allow_unauthorized_sessions:
            raise ValueError("create_agentcore_app requires session_authorizer; set allow_unauthorized_sessions "
                             "only if every caller that can invoke the runtime may use every session")
        logger.warning("allow_unauthorized_sessions is set: any caller that can invoke the runtime can use "
                       "any session ID")
    service = _AgentCoreService(resolved_config, options_factory, session_authorizer, background_tasks_factory,
                                background_task_options_factory, background_request_store, background_result_store,
                                kiro_token_provider_factory, kiro_idle_token_authorizer)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await service.close()
    app = Starlette(routes=[Route("/ping", service.ping, methods=["GET"]),
                            Route("/invocations", service.invoke, methods=["POST"])],
                    lifespan=lifespan)
    app.state.kiro_agentcore = service
    return app


def main() -> None:
    """Run the AgentCore-compatible HTTP server."""
    import uvicorn

    uvicorn.run(create_agentcore_app(), host="0.0.0.0", port=8080, workers=1)


if __name__ == "__main__":
    main()
