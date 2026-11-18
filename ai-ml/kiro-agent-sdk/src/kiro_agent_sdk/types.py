"""Public message types and options for Kiro CLI sessions."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional, TypedDict, Union

from .mcp_oauth import McpOAuthConfig

# ask: call can_use_tool (or deny) for each Kiro permission request; denyUnlisted: deny requests that no
# hook or Kiro rule allowed; trustAll: allow every request; plan: Kiro's read-only planning mode.
PermissionMode = Literal["ask", "denyUnlisted", "trustAll", "plan"]
PERMISSION_MODES: tuple[Optional[str], ...] = (None, "ask", "denyUnlisted", "trustAll", "plan")
EffortLevel = Literal["none", "low", "medium", "high", "xhigh", "max"]


class McpStdioServerConfig(TypedDict, total=False):
    type: Literal["stdio"]
    command: str
    args: list[str]
    env: dict[str, str]


class McpHttpServerConfig(TypedDict, total=False):
    type: Literal["http", "sse"]
    url: str
    headers: Union[dict[str, str], list[dict[str, str]]]


class McpSdkServerConfig(TypedDict, total=False):
    type: Literal["sdk"]
    name: str
    version: str
    tools: list[Any]
    instance: Any


class McpToolAnnotations(TypedDict, total=False):
    readOnly: bool
    destructive: bool
    openWorld: bool


McpServerConfig = Union[McpStdioServerConfig, McpHttpServerConfig, McpSdkServerConfig]

McpServerConnectionStatus = Literal["connected", "pending", "failed", "needs-auth", "disabled", "unknown"]


class _McpToolInfoRequired(TypedDict):
    name: str


class McpToolInfo(_McpToolInfoRequired, total=False):
    description: str
    annotations: Any


class McpServerInfo(TypedDict):
    name: str
    version: str


class _McpServerStatusRequired(TypedDict):
    name: str
    status: McpServerConnectionStatus


class McpServerStatus(_McpServerStatusRequired, total=False):
    serverInfo: McpServerInfo
    error: str
    tools: list[McpToolInfo]
    config: dict[str, Any]
    scope: str
    kiroStatus: str


class _McpStatusRequired(TypedDict):
    mcpServers: list[McpServerStatus]


class McpStatusResponse(_McpStatusRequired, total=False):
    sessionId: str
    servers: list[dict[str, Any]]


class _ContextUsageCategoryRequired(TypedDict):
    name: str
    tokens: int


class ContextUsageCategory(_ContextUsageCategoryRequired, total=False):
    color: str
    isDeferred: bool
    kiroPercent: float


class ContextUsageResponse(TypedDict, total=False):
    categories: list[ContextUsageCategory]
    totalTokens: int
    maxTokens: int
    rawMaxTokens: int
    model: str
    isAutoCompactEnabled: bool
    memoryFiles: list[dict[str, Any]]
    mcpTools: list[dict[str, Any]]
    agents: list[dict[str, Any]]
    gridRows: list[list[dict[str, Any]]]
    autoCompactThreshold: int
    deferredBuiltinTools: list[dict[str, Any]]
    systemTools: list[dict[str, Any]]
    systemPromptSections: list[dict[str, Any]]
    slashCommands: dict[str, Any]
    skills: dict[str, Any]
    messageBreakdown: dict[str, Any]
    apiUsage: Optional[dict[str, Any]]
    percentage: float
    contextUsagePercentage: float
    sessionId: str
    sessionUpdate: str
    _meta: dict[str, Any]


class _SessionKeyOptional(TypedDict, total=False):
    subpath: str


class SessionKey(_SessionKeyOptional):
    project_key: str
    session_id: str


class SessionListSubkeysKey(TypedDict):
    project_key: str
    session_id: str


class SessionStoreEntry(TypedDict, total=False):
    type: str
    data: Any
    uuid: str
    kiro_sdk_prompt_key: str


class SessionStoreListEntry(TypedDict):
    session_id: str
    mtime: int


class SessionInfoEntry(TypedDict):
    """A store's latest Kiro session metadata, merged from ``session_info`` mirror entries."""
    session_id: str
    mtime: int
    info: dict[str, Any]


SessionStoreFlushMode = Literal["batched", "eager"]


class HookContext(TypedDict, total=False):
    """Per-call context passed as the third hook callback argument."""


HookInput = dict[str, Any]
HookOutput = dict[str, Any]
HookCallback = Callable[[HookInput, Optional[str], HookContext], Awaitable[HookOutput]]


@dataclass
class TextBlock:
    text: str


@dataclass
class ThoughtBlock:
    """Reasoning text Kiro streamed as ``agent_thought_chunk``."""
    text: str


@dataclass
class UserMessage:
    """A user prompt, as sent to Kiro or replayed from the session history."""
    content: list[Any]
    message_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    parent_tool_call_id: Optional[str] = None


@dataclass(init=False, eq=False, repr=False)
class SessionInfo(dict[str, Any]):
    """Kiro session metadata as Kiro reported it; attributes read Kiro's own keys.

    Records come from ``session/list``, ``kiro-cli chat --list-sessions`` or an archive's
    ``session.json``. A session store may add ``mtime``, its own write time in milliseconds.
    """

    @staticmethod
    def _milliseconds(value: Any) -> Optional[int]:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                from .timestamps import parse_timestamp
                parsed = parse_timestamp(value)
                if parsed.tzinfo is not None:
                    delta = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
                    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
            except (ValueError, OverflowError, OSError):
                pass
        return None

    def _text(self, *keys: str) -> Optional[str]:
        for key in keys:
            value = self.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def session_id(self) -> Optional[str]:
        return self._text("sessionId", "id")

    @property
    def title(self) -> Optional[str]:
        """The title Kiro generated for the session or the one it was renamed to."""
        return self._text("title")

    @property
    def updated_at(self) -> Optional[int]:
        """Last update in Unix milliseconds: Kiro's time, else the store's write time."""
        for key in ("updatedAt", "lastModifiedAt", "mtime"):
            value = self._milliseconds(self.get(key))
            if value is not None:
                return value
        return None

    @property
    def created_at(self) -> Optional[int]:
        return self._milliseconds(self.get("createdAt"))

    @property
    def cwd(self) -> Optional[str]:
        return self._text("cwd")

    @property
    def agent(self) -> Optional[str]:
        """The Kiro agent (mode) the session uses."""
        meta = self.get("_meta")
        kiro = meta.get("kiro") if isinstance(meta, dict) else None
        mode = kiro.get("modeId") if isinstance(kiro, dict) else None
        return mode if isinstance(mode, str) and mode else self._text("agentMode")

    @property
    def model(self) -> Optional[str]:
        return self._text("modelId")

    @property
    def status(self) -> Optional[str]:
        return self._text("status")

    @property
    def source(self) -> Optional[str]:
        """The Kiro engine that owns the session, such as ``v3``."""
        return self._text("source")


@dataclass
class ForkSessionResult:
    session_id: str


@dataclass
class AgentMessage:
    """Agent output joined from ``agent_message_chunk`` and ``agent_thought_chunk`` updates.

    ``content`` holds ``TextBlock`` and ``ThoughtBlock`` items, and ACP content dictionaries for other
    block types. ``agent_id`` is the Kiro subagent that produced the text, and ``parent_tool_call_id``
    the tool call that started that subagent.
    """
    content: list[Any]
    message_id: Optional[str] = None
    session_id: Optional[str] = None
    model: str = ""
    agent_id: Optional[str] = None
    parent_tool_call_id: Optional[str] = None


@dataclass
class ToolCall:
    """A Kiro tool call: ACP ``tool_call`` merged with its ``tool_call_update`` events.

    A response reports a call when its input is known and again when it completes or fails.
    ``status`` is ``pending``, ``in_progress``, ``completed`` or ``failed``.
    """
    tool_call_id: str
    title: str
    status: Optional[str] = None
    kind: Optional[str] = None
    raw_input: Optional[dict[str, Any]] = None
    raw_output: Any = None
    content: Optional[list[Any]] = None
    locations: Optional[list[Any]] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    parent_tool_call_id: Optional[str] = None
    kiro_metadata: Optional[dict[str, Any]] = None


@dataclass
class StreamEvent:
    """A partial ACP update, delivered when ``include_partial_messages=True``."""
    event: dict[str, Any]
    message_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    parent_tool_call_id: Optional[str] = None


@dataclass
class KiroEvent:
    """An ACP session update or SDK notice. ``kind`` is the ACP ``sessionUpdate`` name or the notice name."""
    kind: str
    data: dict[str, Any]


@dataclass
class HookEventMessage(KiroEvent):
    hook_event_name: str = ""
    session_id: Optional[str] = None


@dataclass
class MirrorErrorMessage(KiroEvent):
    key: Optional[SessionKey] = None
    error: str = ""


@dataclass
class ResultMessage:
    """The end of one turn.

    ``stop_reason`` is Kiro's ACP stop reason (``end_turn``, ``max_tokens``, ``max_turn_requests``,
    ``refusal`` or ``cancelled``). ``kiro_usage`` and ``kiro_model_usage`` carry the usage Kiro reported.
    """
    session_id: str
    stop_reason: Optional[str]
    is_error: bool
    result: Optional[str] = None
    duration_ms: int = 0
    model_turns: Optional[int] = None
    structured_output: Any = None
    permission_denials: Optional[list[dict[str, Any]]] = None
    kiro_usage: Optional[dict[str, Any]] = None
    user_message_id: Optional[str] = None
    kiro_model_usage: Optional[dict[str, Any]] = None


class _KiroPermissionConsentRequired(TypedDict):
    scope: Literal["session", "workspace", "user"]
    resource: str


class KiroPermissionConsent(_KiroPermissionConsentRequired, total=False):
    workspaceRoot: str


@dataclass
class PermissionResultAllow:
    behavior: str = "allow"
    updated_input: Optional[dict[str, Any]] = None
    option_id: Optional[str] = None
    kiro_consent: Optional[KiroPermissionConsent] = None


@dataclass
class PermissionResultDeny:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False
    option_id: Optional[str] = None
    kiro_consent: Optional[KiroPermissionConsent] = None


@dataclass
class ToolPermissionContext:
    tool_call_id: Optional[str] = None
    agent_id: Optional[str] = None
    blocked_path: Optional[str] = None
    decision_reason: Optional[str] = None
    title: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    session_id: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)
    tool_call: dict[str, Any] = field(default_factory=dict)
    request_metadata: Optional[dict[str, Any]] = None
    kiro_agent_type: Optional[str] = None
    kiro_input_unavailable: bool = False

    @property
    def kiro_metadata(self) -> Optional[dict[str, Any]]:
        meta = self.tool_call.get("_meta")
        value = meta.get("kiro") if isinstance(meta, dict) else None
        return copy.deepcopy(value) if isinstance(value, dict) else None

    @property
    def kiro_consent(self) -> Optional[dict[str, Any]]:
        kiro = self.request_metadata.get("kiro") if isinstance(self.request_metadata, dict) else None
        value = kiro.get("consent") if isinstance(kiro, dict) else None
        return copy.deepcopy(value) if isinstance(value, dict) else None


@dataclass
class HookMatcher:
    matcher: Optional[str] = None
    hooks: list[Callable] = field(default_factory=list)
    timeout: Optional[float] = None
    on_failure: Optional[Literal["continue", "block"]] = None


@dataclass
class AgentDefinition:
    """A Kiro agent profile the SDK writes for the session; field names follow Kiro agent configuration."""
    description: str
    prompt: str
    tools: Optional[list[str]] = None
    disallowed_tools: Optional[list[str]] = None
    model: Optional[str] = None
    skills: Optional[list[str]] = None
    mcp_servers: Optional[list[Union[str, dict[str, Any]]]] = None
    initial_prompt: Optional[str] = None
    max_turns: Optional[int] = None
    effort: Union[str, int, None] = None
    kiro_resources: list[str] = field(default_factory=list)
    kiro_permissions: Optional[dict[str, Any]] = None
    tools_mode: Literal["native", "selectors"] = "native"
    tool_selector_map: dict[str, list[str]] = field(default_factory=dict)
    disallowed_tools_mode: Literal["native", "v3-permissions"] = "native"
    disallowed_tool_permission_map: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class KiroAgentOptions:
    cwd: Union[str, Path, None] = None
    add_dirs: list[Union[str, Path]] = field(default_factory=list)
    cli_path: Union[str, Path, None] = "kiro-cli"
    max_buffer_size: Optional[int] = None
    startup_timeout: Optional[float] = 60.0
    turn_timeout: Optional[float] = None
    load_timeout_ms: Optional[int] = None
    stderr: Optional[Callable[[str], None]] = None
    agent: Optional[str] = None
    agents: Optional[dict[str, AgentDefinition]] = field(default_factory=dict)
    system_prompt: Optional[str] = field(default=None, repr=False)
    tools: Optional[list[str]] = None
    kiro_permissions: Optional[dict[str, Any]] = None
    kiro_policy_presets: list[str] = field(default_factory=list)
    kiro_resources: list[str] = field(default_factory=list)
    require_sandbox: bool = False
    enable_file_checkpointing: Optional[bool] = None
    agent_engine: Optional[str] = None
    effort: Optional[str] = None
    thinking: Optional[bool] = None
    resume: Optional[str] = None
    session_id: Optional[str] = None
    continue_conversation: bool = False
    fork_session: bool = False
    resume_session_at: Optional[str] = None
    model: Optional[str] = None
    mcp_servers: Union[dict[str, dict[str, Any]], str, Path] = field(default_factory=dict, repr=False)
    mcp_env_mode: Literal["literal", "expand"] = "literal"
    sdk_tool_interrupt_mode: Literal["kiro", "cancel-all", "cancel-observed"] = "kiro"
    strict_mcp_config: bool = False
    max_turns: Optional[int] = None
    fallback_model: Optional[str] = None
    fallback_model_mode: Literal["reject", "startup", "runtime"] = "reject"
    permission_mode: Optional[PermissionMode] = "ask"
    can_use_tool: Optional[Callable[[str, dict[str, Any], ToolPermissionContext], Awaitable[Any]]] = None
    kiro_shell_edit: bool = False
    hooks: Optional[dict[str, list[HookMatcher]]] = field(default_factory=dict)
    native_hooks: dict[str, list[HookMatcher]] = field(default_factory=dict)
    enable_kiro_hooks: bool = True
    output_format: Optional[dict[str, Any]] = None
    session_store: Any = None
    session_store_flush: SessionStoreFlushMode = "batched"
    session_store_flush_timeout: Optional[float] = 30.0
    session_store_error_policy: Literal["fail", "continue"] = "fail"
    env: dict[str, str] = field(default_factory=dict, repr=False)
    env_inheritance_mode: Literal["inherit", "minimal"] = "inherit"
    include_thoughts: bool = False
    include_partial_messages: bool = False
    max_reported_credits: Optional[float] = None
    reported_credit_ledger: Any = None
    reported_credit_scope: Optional[str] = None
    session_store_archive: bool = False
    session_store_archive_max_bytes: int = 8 * 1024 * 1024
    session_store_archive_max_uncompressed_bytes: int = 64 * 1024 * 1024
    session_store_archive_timeout: Optional[float] = 30.0
    resume_from_store: bool = False
    session_store_source_directory: Union[str, Path, None] = None
    forward_subagent_text: bool = False
    on_user_input: Optional[Callable[[dict[str, Any]], Awaitable[Optional[str]]]] = None
    on_mcp_elicitation: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None
    user: Optional[str] = None
    verbatim_prompts: bool = False
    resume_drops_turn: Optional[str] = None
    sdk_mcp_toggle_mode: Literal["reject", "sdk-tools", "sdk-and-stdio", "sdk-all"] = "reject"
    sdk_pretool_hook_mode: Literal["permission", "native"] = "permission"
    mcp_oauth: dict[str, McpOAuthConfig] = field(default_factory=dict, repr=False)
    enable_kiro_code_intelligence: bool = False
    sdk_mcp_output_transforms: dict[
        str, Callable[[str, str, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
    ] = field(default_factory=dict, repr=False)
    sdk_mcp_output_timeout: float = 30.0
    sdk_stop_hook_mode: Literal["receive", "native"] = "receive"
    max_native_stop_blocks: int = 1
    sdk_mcp_input_transforms: dict[
        str, Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
    ] = field(default_factory=dict, repr=False)
    sdk_mcp_input_timeout: float = 30.0
    on_native_hook_system_message: Optional[Callable[[dict[str, str]], Awaitable[None]]] = field(
        default=None, repr=False)
    receive_messages_mode: Literal["current", "continuous"] = "current"
    stream_input_mode: Literal["on-receive", "buffered", "prefetch", "prefetch-first"] = "on-receive"
    stream_input_buffer_max_messages: int = 128
    stream_input_buffer_max_bytes: int = 8 * 1024 * 1024
    stream_input_buffer_timeout: Optional[float] = 30.0
    resume_from_store_bounded_read: bool = False
    kiro_auth_method: Literal["cli", "client"] = "cli"
    kiro_access_token_provider: Optional[Callable[[], Awaitable[dict[str, str]]]] = field(default=None, repr=False)
    kiro_access_token_timeout: float = 10.0
    kiro_client_auth_idle_mode: Literal["reject", "authorized"] = "reject"
    kiro_idle_token_authorizer: Optional[Callable[[], Awaitable[bool]]] = field(default=None, repr=False)
    # Command hooks cannot authenticate their origin against arbitrary same-UID code.
    native_hook_bridge_mode: Literal["reject", "same-user", "runtime-acp"] = "reject"
    native_hook_runtime_path: Union[str, Path, None] = None
    runtime_compat_mode: Literal["reject", "v3"] = "reject"
    runtime_usage_mode: Literal["none", "reported"] = "none"


PermissionResult = Union[PermissionResultAllow, PermissionResultDeny]
CanUseTool = Callable[[str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]]

ContentBlock = Union[TextBlock, ThoughtBlock, dict[str, Any]]
Message = Union[UserMessage, AgentMessage, ToolCall, KiroEvent, ResultMessage, StreamEvent]
HistoryMessage = Union[UserMessage, AgentMessage, ToolCall]
