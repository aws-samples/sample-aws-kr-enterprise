# Python API 참조

이 문서는 현재 소스의 공개 시그니처에서 생성됩니다. 동작과 제약은 [사용 가이드](guide.md),
AgentCore HTTP 어댑터는 [AgentCore 가이드](agentcore.md)를 참고하세요.
비동기 반복자는 `async for`로, 다른 비동기 함수는 `await`로 호출합니다.

## 최상위 함수

### `query`

```python
async def query(*, prompt: 'Any', options: 'Optional[KiroAgentOptions]' = None, transport: 'Optional[Transport]' = None) -> 'AsyncIterator[Any]': ...
```

### `query_with_fallback`

```python
async def query_with_fallback(*, prompt: 'Any', fallback_model: 'str', approve_retry: 'Callable[[ResultMessage, bool], Awaitable[bool]]', options: 'Optional[KiroAgentOptions]' = None, transport: 'Optional[Transport]' = None) -> 'AsyncIterator[Any]': ...
```

Retry one reported failure only after an explicit caller decision.

### `query_with_structured_output_retry`

```python
async def query_with_structured_output_retry(*, prompt: 'Any', options: 'KiroAgentOptions', approve_retry: 'Callable[[ResultError, bool, int], Awaitable[bool]]', max_retries: 'int' = 1, retry_prompt: 'str' = 'Return the previous answer as valid JSON matching the requested schema. Do not repeat the original task or call tools.') -> 'AsyncIterator[Any]': ...
```

Resume after invalid structured output only when the caller approves.

### `tool`

```python
def tool(name: 'str', description: 'str', input_schema: 'Any', annotations: 'Any' = None): ...
```

### `create_mcp_server`

```python
def create_mcp_server(name: 'str', version: 'str' = '1.0.0', tools: 'Any' = None) -> 'dict[str, Any]': ...
```

### `preauthorize_mcp_oauth`

```python
async def preauthorize_mcp_oauth(server: 'dict[str, Any]', oauth: 'McpOAuthConfig', *, timeout: 'Optional[float]' = 300.0) -> 'None': ...
```

Initialize a remote MCP connection before starting Kiro, retaining OAuth state in ``oauth``.

### `list_sessions`

```python
async def list_sessions(directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, include_worktrees: 'bool' = False, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, all_directories: 'bool' = False) -> 'list[SessionInfo]': ...
```

List sessions in one workspace, Git worktrees, or all local workspaces.

### `get_session_info`

```python
async def get_session_info(session_id: 'str', directory: 'Union[str, Path, None]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, all_directories: 'bool' = False, include_worktrees: 'bool' = False) -> 'Optional[SessionInfo]': ...
```

### `get_session_history`

```python
async def get_session_history(session_id: 'str', *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', agent_engine: 'Optional[str]' = None, env: 'Optional[dict[str, str]]' = None) -> 'list[dict[str, Any]]': ...
```

Load a saved session and return Kiro's replay updates in arrival order.

### `get_session_messages`

```python
async def get_session_messages(session_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', agent_engine: 'Optional[str]' = None, env: 'Optional[dict[str, str]]' = None, coalesce_chunks: 'bool' = False, exclude_observed_subagents: 'bool' = False, kiro_history_source: "Literal['replay', 'archive-text', 'archive-tools']" = 'replay', include_observed_tool_messages: 'bool' = False) -> 'list[HistoryMessage]': ...
```

Read replay chunks or an explicitly selected local V3 archive text preview.

### `delete_session`

```python
async def delete_session(session_id: 'str', directory: 'Union[str, Path, None]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None) -> 'None': ...
```

Delete a saved session using Kiro CLI. Kiro treats an absent ID as success.

### `rename_session`

```python
async def rename_session(session_id: 'str', title: 'str', directory: 'Union[str, Path, None]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, timeout: 'Optional[float]' = 60.0) -> 'None': ...
```

Rename a Kiro V3 session without opening or creating a chat session.

### `fork_session`

```python
async def fork_session(session_id: 'str', directory: 'Union[str, Path, None]' = None, up_to_message_id: 'Optional[str]' = None, title: 'Optional[str]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, timeout: 'Optional[float]' = 60.0) -> 'ForkSessionResult': ...
```

Fork a Kiro V3 session without creating an unrelated chat session.

### `list_models`

```python
async def list_models(*, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None) -> 'list[dict[str, Any]]': ...
```

### `list_subagents`

```python
async def list_subagents(session_id: 'str', directory: 'Union[str, Path, None]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0) -> 'list[str]': ...
```

List subagent IDs recorded in a local Kiro V3 session archive.

### `get_subagent_messages`

```python
async def get_subagent_messages(session_id: 'str', agent_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0) -> 'list[HistoryMessage]': ...
```

Read the text preview of a recorded local Kiro V3 subagent.

### `get_subagent_history`

```python
async def get_subagent_history(session_id: 'str', agent_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0) -> 'list[dict[str, Any]]': ...
```

Read raw native records, including non-text events, from a local subagent.

### `merge_session_info`

```python
def merge_session_info(info: 'Optional[dict[str, Any]]', entries: 'list[dict[str, Any]]') -> 'dict[str, Any]': ...
```

Return Kiro session metadata updated by the ``session_info`` entries in a batch.

### `project_key_for_directory`

```python
def project_key_for_directory(directory: 'str | Path | None' = None) -> 'str': ...
```

Stable opaque key for a workspace path.

### `list_sessions_from_store`

```python
async def list_sessions_from_store(session_store: 'Any', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0) -> 'list[SessionInfo]': ...
```

List mirrored sessions with the Kiro metadata recorded in the store, most recently written first.

### `get_session_info_from_store`

```python
async def get_session_info_from_store(session_store: 'Any', session_id: 'str', directory: 'Union[str, Path, None]' = None) -> 'Optional[SessionInfo]': ...
```

Return the Kiro metadata a store recorded for one mirrored session.

### `get_session_messages_from_store`

```python
async def get_session_messages_from_store(session_store: 'Any', session_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, coalesce_chunks: 'bool' = False, exclude_observed_subagents: 'bool' = False, include_observed_tool_messages: 'bool' = False, bounded_without_archives: 'bool' = False) -> 'list[HistoryMessage]': ...
```

Read mirrored messages, optionally using a bounded snapshot without archive payloads.

### `delete_session_via_store`

```python
async def delete_session_via_store(session_store: 'Any', session_id: 'str', directory: 'Union[str, Path, None]' = None) -> 'None': ...
```

Delete a mirror from the supplied store; the Kiro session is unaffected.

### `rename_session_via_store`

```python
async def rename_session_via_store(session_store: 'Any', session_id: 'str', title: 'str', directory: 'Union[str, Path, None]' = None) -> 'None': ...
```

Record a new title in the mirror only; Kiro's own session title is unchanged.

### `fork_session_via_store`

```python
async def fork_session_via_store(session_store: 'Any', session_id: 'str', directory: 'Union[str, Path, None]' = None, up_to_message_id: 'Optional[str]' = None, title: 'Optional[str]' = None, *, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, batch_size: 'int' = 500, timeout: 'Optional[float]' = 30.0, bounded_mirror_read: 'bool' = False) -> 'ForkSessionResult': ...
```

Fork a mirrored V3 archive without creating a session in the user's Kiro home.

### `list_subagents_from_store`

```python
async def list_subagents_from_store(session_store: 'Any', session_id: 'str', directory: 'Union[str, Path, None]' = None, *, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, bounded_mirror_read: 'bool' = False) -> 'list[str]': ...
```

List subagent IDs observed in a mirror and its latest archive.

### `get_subagent_messages_from_store`

```python
async def get_subagent_messages_from_store(session_store: 'Any', session_id: 'str', agent_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, allow_stale: 'bool' = False, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, bounded_mirror_read: 'bool' = False) -> 'list[HistoryMessage]': ...
```

Read a subagent text preview from the latest mirrored Kiro archive.

### `get_subagent_history_from_store`

```python
async def get_subagent_history_from_store(session_store: 'Any', session_id: 'str', agent_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, allow_stale: 'bool' = False, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, bounded_mirror_read: 'bool' = False) -> 'list[dict[str, Any]]': ...
```

Read raw native subagent records from the latest mirrored archive.

### `export_session_archive`

```python
async def export_session_archive(session_id: 'str', *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, timeout: 'Optional[float]' = 30.0) -> 'bytes': ...
```

Export a local session with the Kiro CLI's chat archive helper.

### `import_session_archive`

```python
async def import_session_archive(archive: 'bytes', *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, timeout: 'Optional[float]' = 30.0) -> 'str': ...
```

Import an archive as a new local Kiro session and return its ID.

### `import_session_to_store`

```python
async def import_session_to_store(session_id: 'str', store: 'Any', *, directory: 'Union[str, Path, None]' = None, include_subagents: 'bool' = True, batch_size: 'int' = 500, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0) -> 'None': ...
```

Import an existing local V3 session into an empty mirror key.

### `restore_session_from_store`

```python
async def restore_session_from_store(session_store: 'Any', session_id: 'str', directory: 'Union[str, Path, None]' = None, *, target_directory: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, allow_stale: 'bool' = False, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, timeout: 'Optional[float]' = 30.0, bounded_mirror_read: 'bool' = False) -> 'str': ...
```

Import the latest stored Kiro archive as a new local session.

### `get_session_archive_history`

```python
async def get_session_archive_history(session_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0) -> 'list[dict[str, Any]]': ...
```

Return raw Kiro V3 archive records, including non-text and historical events.

### `get_session_archive_messages`

```python
async def get_session_archive_messages(session_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0, include_tool_messages: 'bool' = False) -> 'list[HistoryMessage]': ...
```

Return Kiro record previews, optionally including reported tool calls and results.

### `get_session_archive_history_from_store`

```python
async def get_session_archive_history_from_store(store: 'Any', session_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, allow_stale: 'bool' = False, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, bounded_mirror_read: 'bool' = False) -> 'list[dict[str, Any]]': ...
```

Return raw records from the latest archived session mirror.

### `get_session_archive_messages_from_store`

```python
async def get_session_archive_messages_from_store(store: 'Any', session_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, allow_stale: 'bool' = False, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, include_tool_messages: 'bool' = False, bounded_mirror_read: 'bool' = False) -> 'list[HistoryMessage]': ...
```

Return stored Kiro record previews, optionally including reported tool calls and results.

### `apply_private_access`

```python
def apply_private_access(client: 'Any', access: 'AgentCorePrivateAccess', *, replace_existing: 'bool' = False) -> 'tuple[str, ...]': ...
```

Apply runtime first, then endpoint policies; never roll back to weaker access.

## 동기 세션 함수

`kiro_agent_sdk.sync`에서 가져오며 실행 중인 이벤트 루프에서는 사용할 수 없습니다.

```python
def list_sessions(directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, include_worktrees: 'bool' = False, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, all_directories: 'bool' = False) -> 'list[SessionInfo]': ...
def get_session_info(session_id: 'str', directory: 'Union[str, Path, None]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, all_directories: 'bool' = False, include_worktrees: 'bool' = False) -> 'Optional[SessionInfo]': ...
def get_session_history(session_id: 'str', *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', agent_engine: 'Optional[str]' = None, env: 'Optional[dict[str, str]]' = None) -> 'list[dict[str, Any]]': ...
def get_session_messages(session_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', agent_engine: 'Optional[str]' = None, env: 'Optional[dict[str, str]]' = None, coalesce_chunks: 'bool' = False, exclude_observed_subagents: 'bool' = False, kiro_history_source: "Literal['replay', 'archive-text', 'archive-tools']" = 'replay', include_observed_tool_messages: 'bool' = False) -> 'list[HistoryMessage]': ...
def list_models(*, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None) -> 'list[dict[str, Any]]': ...
def delete_session(session_id: 'str', directory: 'Union[str, Path, None]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None) -> 'None': ...
def rename_session(session_id: 'str', title: 'str', directory: 'Union[str, Path, None]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, timeout: 'Optional[float]' = 60.0) -> 'None': ...
def fork_session(session_id: 'str', directory: 'Union[str, Path, None]' = None, up_to_message_id: 'Optional[str]' = None, title: 'Optional[str]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, timeout: 'Optional[float]' = 60.0) -> 'ForkSessionResult': ...
def list_subagents(session_id: 'str', directory: 'Union[str, Path, None]' = None, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0) -> 'list[str]': ...
def get_subagent_messages(session_id: 'str', agent_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0) -> 'list[HistoryMessage]': ...
def get_subagent_history(session_id: 'str', agent_id: 'str', directory: 'Union[str, Path, None]' = None, limit: 'Optional[int]' = None, offset: 'int' = 0, *, cwd: 'Union[str, Path, None]' = None, cli_path: 'Union[str, Path]' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, max_bytes: 'int' = 8388608, max_uncompressed_bytes: 'int' = 67108864, max_messages: 'int' = 10000, timeout: 'Optional[float]' = 30.0) -> 'list[dict[str, Any]]': ...
```

## `KiroSDKClient`

`KiroSDKClient(options=None, transport=None)`로 생성합니다. `async with` 사용을 권장합니다.

### `connect`

```python
async def connect(self, prompt: 'Optional[Union[str, list[Any], AsyncIterable[dict[str, Any]]]]' = None) -> 'None': ...
```

### `query`

```python
async def query(self, prompt: 'Any', session_id: 'str' = 'default') -> 'None': ...
```

### `receive_response`

```python
async def receive_response(self) -> 'AsyncIterator[Any]': ...
```

Consume one turn of messages.

### `receive_messages`

```python
async def receive_messages(self) -> 'AsyncIterator[Any]': ...
```

Receive current input, or wait for future turns when explicitly configured.

### `receive_messages_continuously`

```python
async def receive_messages_continuously(self) -> 'AsyncIterator[Any]': ...
```

Receive successive turns and enabled owned task messages until disconnect.

### `last_mirror_error`

읽기 전용 속성.

Most recent nonfatal mirror failure on this client, including after disconnect.

### `interrupt`

```python
async def interrupt(self) -> 'None': ...
```

### `steer`

```python
async def steer(self, message: 'str') -> 'dict[str, Any]': ...
```

Send a Kiro V3 steering message to an active turn.

### `clear_steering`

```python
async def clear_steering(self) -> 'dict[str, Any]': ...
```

Discard queued Kiro V3 steering messages for the current session.

### `set_model`

```python
async def set_model(self, model: 'Optional[str]' = None) -> 'None': ...
```

Select a Kiro model, or the local CLI's reported default when omitted.

### `set_mode`

```python
async def set_mode(self, mode: 'str') -> 'None': ...
```

### `set_config_option`

```python
async def set_config_option(self, config_id: 'str', value: 'str') -> 'Any': ...
```

### `set_permission_mode`

```python
async def set_permission_mode(self, mode: 'str') -> 'None': ...
```

### `sdk_permission_mode`

읽기 전용 속성.

Return the SDK's current ACP decision mode, not Kiro's effective policy.

### `get_server_info`

```python
async def get_server_info(self) -> 'Optional[dict[str, Any]]': ...
```

### `get_kiro_available_commands`

```python
async def get_kiro_available_commands(self) -> 'Optional[list[dict[str, Any]]]': ...
```

Return the latest bounded ACP command list for the current session, if reported.

### `get_context_usage`

```python
async def get_context_usage(self) -> 'Optional[dict[str, Any]]': ...
```

Return Kiro context metadata with percentage when Kiro reports it.

### `get_context_usage_raw`

```python
async def get_context_usage_raw(self) -> 'Optional[dict[str, Any]]': ...
```

Return the latest unmodified Kiro context metadata for this session.

### `reported_credits`

읽기 전용 속성.

Credits reported for completed turns while this client tracked a limit.

### `get_mcp_status`

```python
async def get_mcp_status(self) -> 'Optional[dict[str, Any]]': ...
```

Return the MCP server status Kiro reported for this session.

### `get_mcp_status_raw`

```python
async def get_mcp_status_raw(self) -> 'Optional[dict[str, Any]]': ...
```

Return the last unmodified Kiro MCP status notification.

### `get_sandbox_status`

```python
async def get_sandbox_status(self) -> 'Optional[dict[str, Any]]': ...
```

Return the latest status reported by Kiro's command sandbox.

### `list_kiro_permissions`

```python
async def list_kiro_permissions(self) -> 'dict[str, Any]': ...
```

### `explain_kiro_permission`

```python
async def explain_kiro_permission(self, capability: 'str', resource: 'str') -> 'dict[str, Any]': ...
```

### `get_kiro_account_usage`

```python
async def get_kiro_account_usage(self) -> 'dict[str, Any]': ...
```

Return Kiro's account usage response; it is not a per-turn cost limit.

### `list_kiro_powers`

```python
async def list_kiro_powers(self) -> 'dict[str, Any]': ...
```

Return installed Kiro powers and any load errors reported by Kiro.

### `list_kiro_hooks`

```python
async def list_kiro_hooks(self, *, trigger: 'Optional[str]' = None, tool_id: 'Optional[str]' = None, tool_tags: 'Optional[list[str]]' = None, include_disabled: 'bool' = False) -> 'dict[str, Any]': ...
```

Return Kiro's hook listing for this session without changing its hook guards.

### `get_kiro_spec_task_statuses`

```python
async def get_kiro_spec_task_statuses(self, *, tasks_file_path: 'str', feature_name: 'str', workspace_paths: 'list[str]') -> 'dict[str, Any]': ...
```

Return Kiro spec task statuses from paths on the Kiro host.

### `get_kiro_code_intelligence_status`

```python
async def get_kiro_code_intelligence_status(self) -> 'dict[str, Any]': ...
```

Return Kiro's workspace code intelligence and language server status.

### `initialize_kiro_code_intelligence`

```python
async def initialize_kiro_code_intelligence(self) -> 'dict[str, Any]': ...
```

Ask Kiro to initialize workspace code intelligence and language servers.

### `get_kiro_code_overview`

```python
async def get_kiro_code_overview(self) -> 'dict[str, Any]': ...
```

Ask Kiro for an overview of the current workspace.

### `list_kiro_context`

```python
async def list_kiro_context(self) -> 'dict[str, Any]': ...
```

List Kiro's context files and, when available, its context breakdown.

### `add_kiro_context`

```python
async def add_kiro_context(self, path: 'str', *, force: 'bool' = False) -> 'dict[str, Any]': ...
```

Add a path to Kiro's session context; paths refer to the Kiro host.

### `remove_kiro_context`

```python
async def remove_kiro_context(self, path: 'str') -> 'dict[str, Any]': ...
```

Remove a path from Kiro's session context without deleting the file.

### `clear_kiro_context`

```python
async def clear_kiro_context(self) -> 'dict[str, Any]': ...
```

Remove all context-file references from this Kiro session.

### `reconnect_mcp_server`

```python
async def reconnect_mcp_server(self, server_name: 'str', *, start_oauth: 'bool' = False) -> 'dict[str, Any]': ...
```

Reconnect a server bound to the current Kiro V3 session.

### `revoke_mcp_oauth_tokens`

```python
async def revoke_mcp_oauth_tokens(self, server_name: 'str') -> 'dict[str, bool]': ...
```

Request advertised OAuth revocation, then disconnect; flags mean HTTP acceptance.

### `toggle_mcp_server`

```python
async def toggle_mcp_server(self, server_name: 'str', enabled: 'bool') -> 'None': ...
```

Opt-in tool-call suspension; it does not disconnect or hide the MCP server.

### `request_mcp_tool_cancellation`

```python
async def request_mcp_tool_cancellation(self, server_name: 'str') -> 'None': ...
```

Request cancellation of observed outstanding tool calls for an SDK-managed MCP server.

### `request_kiro_mcp_enabled`

```python
async def request_kiro_mcp_enabled(self, enabled: 'bool') -> 'None': ...
```

Request a process-wide Kiro MCP switch; the notification has no acknowledgement.

### `get_mcp_resource`

```python
async def get_mcp_resource(self, server_name: 'str', uri: 'str') -> 'dict[str, Any]': ...
```

### `get_mcp_prompt`

```python
async def get_mcp_prompt(self, server_name: 'str', prompt_name: 'str', arguments: 'Optional[dict[str, str]]' = None) -> 'dict[str, Any]': ...
```

### `list_sessions`

```python
async def list_sessions(self, cwd: 'Optional[str]' = None, cursor: 'Optional[str]' = None) -> 'dict[str, Any]': ...
```

List sessions via ACP. Requires a server advertising session/list.

### `fork_session`

```python
async def fork_session(self, session_id: 'Optional[str]' = None, *, message_id: 'Optional[str]' = None, title: 'Optional[str]' = None, resume_drops_turn: 'Optional[str]' = None) -> 'dict[str, Any]': ...
```

Fork a session; returns the new session descriptor from Kiro.

### `delete_session`

```python
async def delete_session(self, session_id: 'str') -> 'None': ...
```

Delete a stored session. This is irreversible.

### `rename_session`

```python
async def rename_session(self, title: 'str', session_id: 'Optional[str]' = None) -> 'dict[str, Any]': ...
```

Rename a Kiro V3 session.

### `compact`

```python
async def compact(self) -> 'dict[str, Any]': ...
```

Ask Kiro V3 to compact the current conversation.

### `get_guarded_fork_options`

```python
async def get_guarded_fork_options(self, user_message_id: 'str') -> 'dict[str, Any]': ...
```

Find an idle session's safe truncation point; the fork revalidates it.

### `list_kiro_memories`

```python
async def list_kiro_memories(self, *, cursor: 'Optional[str]' = None, limit: 'Optional[int]' = None, scopes: 'Optional[list[str]]' = None) -> 'dict[str, Any]': ...
```

List one page of persistent Kiro memories.

### `get_kiro_memory`

```python
async def get_kiro_memory(self, memory_id: 'str') -> 'dict[str, Any]': ...
```

Get a persistent Kiro memory by its Kiro ID.

### `create_kiro_memory`

```python
async def create_kiro_memory(self, *, memory_type: 'str', scopes: 'list[str]', title: 'str', summary: 'str', content: 'str', unresolved_conflict: 'Optional[bool]' = None) -> 'dict[str, Any]': ...
```

Create a persistent Kiro memory; its scope may extend beyond this session.

### `update_kiro_memory`

```python
async def update_kiro_memory(self, memory_id: 'str', *, memory_type: 'Optional[str]' = None, title: 'Optional[str]' = None, summary: 'Optional[str]' = None, content: 'Optional[str]' = None, unresolved_conflict: 'Optional[bool]' = None) -> 'dict[str, Any]': ...
```

Update a persistent Kiro memory by ID.

### `delete_kiro_memory`

```python
async def delete_kiro_memory(self, memory_id: 'str') -> 'dict[str, Any]': ...
```

Delete a persistent Kiro memory by ID.

### `get_kiro_memory_sync_status`

```python
async def get_kiro_memory_sync_status(self) -> 'dict[str, Any]': ...
```

Get Kiro memory synchronization status.

### `list_kiro_workflows`

```python
async def list_kiro_workflows(self, *, workspace_paths: 'Optional[list[str]]' = None) -> 'dict[str, Any]': ...
```

List Kiro workflow runs for this session or specified remote host paths.

### `create_kiro_workflow`

```python
async def create_kiro_workflow(self, *, workflow: 'Optional[dict[str, Any]]' = None, workflow_path: 'Optional[str]' = None, inputs: 'Optional[dict[str, Any]]' = None, model: 'Optional[str]' = None, effort: 'Optional[str]' = None) -> 'dict[str, Any]': ...
```

Create a Kiro workflow run without starting it.

### `inspect_kiro_workflow`

```python
async def inspect_kiro_workflow(self, workflow_id: 'str') -> 'dict[str, Any]': ...
```

### `run_kiro_workflow`

```python
async def run_kiro_workflow(self, workflow_id: 'str') -> 'dict[str, Any]': ...
```

Start a Kiro workflow; its steps may run asynchronously.

### `pause_kiro_workflow`

```python
async def pause_kiro_workflow(self, workflow_id: 'str') -> 'dict[str, Any]': ...
```

### `resume_kiro_workflow`

```python
async def resume_kiro_workflow(self, workflow_id: 'str') -> 'dict[str, Any]': ...
```

### `cancel_kiro_workflow`

```python
async def cancel_kiro_workflow(self, workflow_id: 'str') -> 'dict[str, Any]': ...
```

Request cancellation of a Kiro workflow.

### `attach_background_tasks`

```python
def attach_background_tasks(self, manager: 'Optional[BackgroundTaskManager]') -> 'None': ...
```

Attach an explicitly managed task manager, or detach it with None.

### `stop_task`

```python
async def stop_task(self, task_id: 'str') -> 'None': ...
```

Cancel an attached SDK task or a Kiro workflow created by this client.

### `delete_kiro_workflow`

```python
async def delete_kiro_workflow(self, workflow_id: 'str') -> 'dict[str, Any]': ...
```

Delete a Kiro workflow run; Kiro may cancel it first.

### `get_history_page`

```python
async def get_history_page(self, *, before_message_id: 'Optional[str]' = None, limit: 'Optional[int]' = None) -> 'dict[str, Any]': ...
```

Read a page of V3 history without relying on Kiro's storage format.

### `export_session`

```python
async def export_session(self, session_id: 'Optional[str]' = None) -> 'dict[str, Any]': ...
```

Ask Kiro V3 to export a session to a server-local archive.

### `restore_checkpoint`

```python
async def restore_checkpoint(self, message_id: 'str') -> 'dict[str, Any]': ...
```

Restore Kiro files and rewind conversation to a user message.

### `rewind_files`

```python
async def rewind_files(self, user_message_id: 'str', *, restore_conversation: 'bool' = False) -> 'dict[str, Any]': ...
```

Restore Kiro files only if the caller accepts rewinding conversation too.

### `last_rewind_fork_id`

읽기 전용 속성.

The most recent fork created by rewind_files_preserving_conversation.

### `restored_session_id`

읽기 전용 속성.

The session imported by resume_from_store, including after a failed connection.

### `rewind_files_preserving_conversation`

```python
async def rewind_files_preserving_conversation(self, user_message_id: 'str') -> 'dict[str, Any]': ...
```

Fork the latest conversation, then restore files in the original session.

### `extension_request`

```python
async def extension_request(self, method: 'str', params: 'dict[str, Any]') -> 'Any': ...
```

Call a Kiro-specific ACP extension explicitly.

### `disconnect`

```python
async def disconnect(self) -> 'None': ...
```

## `KiroSessionClient`

```python
def KiroSessionClient(*, cwd: 'str | Path | None' = None, cli_path: 'str | Path' = 'kiro-cli', env: 'Optional[dict[str, str]]' = None, access_token_provider: 'Optional[Callable[[], Awaitable[dict[str, str]]]]' = None, authorize: 'Optional[Callable[[SessionAccess], Awaitable[bool]]]' = None, timeout: 'float' = 30, token_timeout: 'float' = 10) -> 'None': ...
```

### `connect`

```python
async def connect(self) -> 'None': ...
```

### `disconnect`

```python
async def disconnect(self) -> 'None': ...
```

### `list_sessions`

```python
async def list_sessions(self, *, cursor: 'Optional[str]' = None, all_directories: 'bool' = False) -> 'dict[str, Any]': ...
```

Return one bounded ACP page. Global listing requires explicit selection.

### `get_session_info`

```python
async def get_session_info(self, session_id: 'str') -> 'Optional[SessionInfo]': ...
```

Find one session in this workspace, with bounded pagination.

### `rename_session`

```python
async def rename_session(self, session_id: 'str', title: 'str') -> 'None': ...
```

### `fork_session`

```python
async def fork_session(self, session_id: 'str', *, up_to_message_id: 'Optional[str]' = None, title: 'Optional[str]' = None) -> 'ForkSessionResult': ...
```

### `delete_session`

```python
async def delete_session(self, session_id: 'str') -> 'None': ...
```

### `get_session_history`

```python
async def get_session_history(self, session_id: 'str') -> 'list[dict[str, Any]]': ...
```

Resume a saved session to read replay events without submitting a prompt.

### `get_session_messages`

```python
async def get_session_messages(self, session_id: 'str', *, limit: 'Optional[int]' = None, offset: 'int' = 0, coalesce_chunks: 'bool' = False, exclude_observed_subagents: 'bool' = False, include_observed_tool_messages: 'bool' = False) -> 'list[HistoryMessage]': ...
```

## `AgentCorePrivateAccess`

```python
def AgentCorePrivateAccess(runtime_arn: 'str', caller_role_arns: 'tuple[str, ...]', vpce_ids: 'tuple[str, ...]', endpoint_names: 'tuple[str, ...]' = ('DEFAULT',)) -> None: ...
```

### `resource_policies`

```python
def resource_policies(self) -> 'dict[str, dict[str, Any]]': ...
```

### `caller_policy`

```python
def caller_policy(self) -> 'dict[str, Any]': ...
```

### `vpc_endpoint_policy`

```python
def vpc_endpoint_policy(self) -> 'dict[str, Any]': ...
```

Allow invocation of this runtime on a dedicated AgentCore VPC endpoint.

### `plan`

```python
def plan(self) -> 'dict[str, Any]': ...
```

Return JSON-serializable documents without calling AWS.

## 메시지와 데이터 클래스

다음은 공개 필드입니다. `<factory>`는 인스턴스별 기본값을 뜻합니다. Kiro가 제공하지 않는 값은 비어 있거나 `None`일 수 있습니다.

### `SessionAccess`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `operation` | `str` | `필수` |
| `cwd` | `Optional[str]` | `필수` |
| `session_id` | `Optional[str]` | `None` |

### `KiroAgentOptions`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `cwd` | `Union[str, Path, None]` | `None` |
| `add_dirs` | `list[Union[str, Path]]` | `<factory>` |
| `cli_path` | `Union[str, Path, None]` | `'kiro-cli'` |
| `max_buffer_size` | `Optional[int]` | `None` |
| `startup_timeout` | `Optional[float]` | `60.0` |
| `turn_timeout` | `Optional[float]` | `None` |
| `load_timeout_ms` | `Optional[int]` | `None` |
| `stderr` | `Optional[Callable[[str], None]]` | `None` |
| `agent` | `Optional[str]` | `None` |
| `agents` | `Optional[dict[str, AgentDefinition]]` | `<factory>` |
| `system_prompt` | `Optional[str]` | `None` |
| `tools` | `Optional[list[str]]` | `None` |
| `kiro_permissions` | `Optional[dict[str, Any]]` | `None` |
| `kiro_policy_presets` | `list[str]` | `<factory>` |
| `kiro_resources` | `list[str]` | `<factory>` |
| `require_sandbox` | `bool` | `False` |
| `enable_file_checkpointing` | `Optional[bool]` | `None` |
| `agent_engine` | `Optional[str]` | `None` |
| `effort` | `Optional[str]` | `None` |
| `thinking` | `Optional[bool]` | `None` |
| `resume` | `Optional[str]` | `None` |
| `session_id` | `Optional[str]` | `None` |
| `continue_conversation` | `bool` | `False` |
| `fork_session` | `bool` | `False` |
| `resume_session_at` | `Optional[str]` | `None` |
| `model` | `Optional[str]` | `None` |
| `mcp_servers` | `Union[dict[str, dict[str, Any]], str, Path]` | `<factory>` |
| `mcp_env_mode` | `Literal['literal', 'expand']` | `'literal'` |
| `sdk_tool_interrupt_mode` | `Literal['kiro', 'cancel-all', 'cancel-observed']` | `'kiro'` |
| `strict_mcp_config` | `bool` | `False` |
| `max_turns` | `Optional[int]` | `None` |
| `fallback_model` | `Optional[str]` | `None` |
| `fallback_model_mode` | `Literal['reject', 'startup', 'runtime']` | `'reject'` |
| `permission_mode` | `Optional[PermissionMode]` | `'ask'` |
| `can_use_tool` | `Optional[Callable[[str, dict[str, Any], ToolPermissionContext], Awaitable[Any]]]` | `None` |
| `kiro_shell_edit` | `bool` | `False` |
| `hooks` | `Optional[dict[str, list[HookMatcher]]]` | `<factory>` |
| `native_hooks` | `dict[str, list[HookMatcher]]` | `<factory>` |
| `enable_kiro_hooks` | `bool` | `True` |
| `output_format` | `Optional[dict[str, Any]]` | `None` |
| `session_store` | `Any` | `None` |
| `session_store_flush` | `SessionStoreFlushMode` | `'batched'` |
| `session_store_flush_timeout` | `Optional[float]` | `30.0` |
| `session_store_error_policy` | `Literal['fail', 'continue']` | `'fail'` |
| `env` | `dict[str, str]` | `<factory>` |
| `env_inheritance_mode` | `Literal['inherit', 'minimal']` | `'inherit'` |
| `include_thoughts` | `bool` | `False` |
| `include_partial_messages` | `bool` | `False` |
| `max_reported_credits` | `Optional[float]` | `None` |
| `reported_credit_ledger` | `Any` | `None` |
| `reported_credit_scope` | `Optional[str]` | `None` |
| `session_store_archive` | `bool` | `False` |
| `session_store_archive_max_bytes` | `int` | `8388608` |
| `session_store_archive_max_uncompressed_bytes` | `int` | `67108864` |
| `session_store_archive_timeout` | `Optional[float]` | `30.0` |
| `resume_from_store` | `bool` | `False` |
| `session_store_source_directory` | `Union[str, Path, None]` | `None` |
| `forward_subagent_text` | `bool` | `False` |
| `on_user_input` | `Optional[Callable[[dict[str, Any]], Awaitable[Optional[str]]]]` | `None` |
| `on_mcp_elicitation` | `Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]` | `None` |
| `user` | `Optional[str]` | `None` |
| `verbatim_prompts` | `bool` | `False` |
| `resume_drops_turn` | `Optional[str]` | `None` |
| `sdk_mcp_toggle_mode` | `Literal['reject', 'sdk-tools', 'sdk-and-stdio', 'sdk-all']` | `'reject'` |
| `sdk_pretool_hook_mode` | `Literal['permission', 'native']` | `'permission'` |
| `mcp_oauth` | `dict[str, McpOAuthConfig]` | `<factory>` |
| `enable_kiro_code_intelligence` | `bool` | `False` |
| `sdk_mcp_output_transforms` | `dict[str, Callable[[str, str, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]]` | `<factory>` |
| `sdk_mcp_output_timeout` | `float` | `30.0` |
| `sdk_stop_hook_mode` | `Literal['receive', 'native']` | `'receive'` |
| `max_native_stop_blocks` | `int` | `1` |
| `sdk_mcp_input_transforms` | `dict[str, Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]]` | `<factory>` |
| `sdk_mcp_input_timeout` | `float` | `30.0` |
| `on_native_hook_system_message` | `Optional[Callable[[dict[str, str]], Awaitable[None]]]` | `None` |
| `receive_messages_mode` | `Literal['current', 'continuous']` | `'current'` |
| `stream_input_mode` | `Literal['on-receive', 'buffered', 'prefetch', 'prefetch-first']` | `'on-receive'` |
| `stream_input_buffer_max_messages` | `int` | `128` |
| `stream_input_buffer_max_bytes` | `int` | `8388608` |
| `stream_input_buffer_timeout` | `Optional[float]` | `30.0` |
| `resume_from_store_bounded_read` | `bool` | `False` |
| `kiro_auth_method` | `Literal['cli', 'client']` | `'cli'` |
| `kiro_access_token_provider` | `Optional[Callable[[], Awaitable[dict[str, str]]]]` | `None` |
| `kiro_access_token_timeout` | `float` | `10.0` |
| `kiro_client_auth_idle_mode` | `Literal['reject', 'authorized']` | `'reject'` |
| `kiro_idle_token_authorizer` | `Optional[Callable[[], Awaitable[bool]]]` | `None` |
| `native_hook_bridge_mode` | `Literal['reject', 'same-user', 'runtime-acp']` | `'reject'` |
| `native_hook_runtime_path` | `Union[str, Path, None]` | `None` |
| `runtime_compat_mode` | `Literal['reject', 'v3']` | `'reject'` |
| `runtime_usage_mode` | `Literal['none', 'reported']` | `'none'` |

### `AgentDefinition`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `description` | `str` | `필수` |
| `prompt` | `str` | `필수` |
| `tools` | `Optional[list[str]]` | `None` |
| `disallowed_tools` | `Optional[list[str]]` | `None` |
| `model` | `Optional[str]` | `None` |
| `skills` | `Optional[list[str]]` | `None` |
| `mcp_servers` | `Optional[list[Union[str, dict[str, Any]]]]` | `None` |
| `initial_prompt` | `Optional[str]` | `None` |
| `max_turns` | `Optional[int]` | `None` |
| `effort` | `Union[str, int, None]` | `None` |
| `kiro_resources` | `list[str]` | `<factory>` |
| `kiro_permissions` | `Optional[dict[str, Any]]` | `None` |
| `tools_mode` | `Literal['native', 'selectors']` | `'native'` |
| `tool_selector_map` | `dict[str, list[str]]` | `<factory>` |
| `disallowed_tools_mode` | `Literal['native', 'v3-permissions']` | `'native'` |
| `disallowed_tool_permission_map` | `dict[str, list[dict[str, Any]]]` | `<factory>` |

### `UserMessage`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `content` | `list[Any]` | `필수` |
| `message_id` | `Optional[str]` | `None` |
| `session_id` | `Optional[str]` | `None` |
| `agent_id` | `Optional[str]` | `None` |
| `parent_tool_call_id` | `Optional[str]` | `None` |

### `AgentMessage`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `content` | `list[Any]` | `필수` |
| `message_id` | `Optional[str]` | `None` |
| `session_id` | `Optional[str]` | `None` |
| `model` | `str` | `''` |
| `agent_id` | `Optional[str]` | `None` |
| `parent_tool_call_id` | `Optional[str]` | `None` |

### `ToolCall`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `tool_call_id` | `str` | `필수` |
| `title` | `str` | `필수` |
| `status` | `Optional[str]` | `None` |
| `kind` | `Optional[str]` | `None` |
| `raw_input` | `Optional[dict[str, Any]]` | `None` |
| `raw_output` | `Any` | `None` |
| `content` | `Optional[list[Any]]` | `None` |
| `locations` | `Optional[list[Any]]` | `None` |
| `session_id` | `Optional[str]` | `None` |
| `agent_id` | `Optional[str]` | `None` |
| `parent_tool_call_id` | `Optional[str]` | `None` |
| `kiro_metadata` | `Optional[dict[str, Any]]` | `None` |

### `KiroEvent`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `kind` | `str` | `필수` |
| `data` | `dict[str, Any]` | `필수` |

### `ResultMessage`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `session_id` | `str` | `필수` |
| `stop_reason` | `Optional[str]` | `필수` |
| `is_error` | `bool` | `필수` |
| `result` | `Optional[str]` | `None` |
| `duration_ms` | `int` | `0` |
| `model_turns` | `Optional[int]` | `None` |
| `structured_output` | `Any` | `None` |
| `permission_denials` | `Optional[list[dict[str, Any]]]` | `None` |
| `kiro_usage` | `Optional[dict[str, Any]]` | `None` |
| `user_message_id` | `Optional[str]` | `None` |
| `kiro_model_usage` | `Optional[dict[str, Any]]` | `None` |

### `StreamEvent`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `event` | `dict[str, Any]` | `필수` |
| `message_id` | `Optional[str]` | `None` |
| `session_id` | `Optional[str]` | `None` |
| `agent_id` | `Optional[str]` | `None` |
| `parent_tool_call_id` | `Optional[str]` | `None` |

### `HookEventMessage`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `kind` | `str` | `필수` |
| `data` | `dict[str, Any]` | `필수` |
| `hook_event_name` | `str` | `''` |
| `session_id` | `Optional[str]` | `None` |

### `MirrorErrorMessage`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `kind` | `str` | `필수` |
| `data` | `dict[str, Any]` | `필수` |
| `key` | `Optional[SessionKey]` | `None` |
| `error` | `str` | `''` |

### `TextBlock`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `text` | `str` | `필수` |

### `ThoughtBlock`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `text` | `str` | `필수` |

### `VerbatimPrompt`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `content` | `Any` | `필수` |

### `PermissionResultAllow`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `behavior` | `str` | `'allow'` |
| `updated_input` | `Optional[dict[str, Any]]` | `None` |
| `option_id` | `Optional[str]` | `None` |
| `kiro_consent` | `Optional[KiroPermissionConsent]` | `None` |

### `PermissionResultDeny`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `behavior` | `str` | `'deny'` |
| `message` | `str` | `''` |
| `interrupt` | `bool` | `False` |
| `option_id` | `Optional[str]` | `None` |
| `kiro_consent` | `Optional[KiroPermissionConsent]` | `None` |

### `ToolPermissionContext`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `tool_call_id` | `Optional[str]` | `None` |
| `agent_id` | `Optional[str]` | `None` |
| `blocked_path` | `Optional[str]` | `None` |
| `decision_reason` | `Optional[str]` | `None` |
| `title` | `Optional[str]` | `None` |
| `display_name` | `Optional[str]` | `None` |
| `description` | `Optional[str]` | `None` |
| `session_id` | `str` | `''` |
| `options` | `list[dict[str, Any]]` | `<factory>` |
| `tool_call` | `dict[str, Any]` | `<factory>` |
| `request_metadata` | `Optional[dict[str, Any]]` | `None` |
| `kiro_agent_type` | `Optional[str]` | `None` |
| `kiro_input_unavailable` | `bool` | `False` |

추가 속성: `kiro_metadata`, `kiro_consent`

### `HookMatcher`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `matcher` | `Optional[str]` | `None` |
| `hooks` | `list[Callable]` | `<factory>` |
| `timeout` | `Optional[float]` | `None` |
| `on_failure` | `Optional[Literal['continue', 'block']]` | `None` |

### `SdkMcpTool`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `name` | `str` | `필수` |
| `description` | `str` | `필수` |
| `input_schema` | `Any` | `필수` |
| `handler` | `Callable` | `필수` |
| `annotations` | `Any` | `None` |

### `McpOAuthConfig`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `redirect_uri` | `str` | `필수` |
| `authorize` | `Optional[Callable[[str], Awaitable[str]]]` | `필수` |
| `allowed_origins` | `list[str]` | `<factory>` |
| `client_name` | `str` | `'kiro-agent-sdk'` |
| `scope` | `Optional[str]` | `None` |
| `storage` | `Any` | `<factory>` |
| `sse_cancel_token_provider` | `Optional[Callable[[], Awaitable[str]]]` | `None` |
| `sse_cancel_retry_with_stored_token` | `bool` | `False` |

### `ForkSessionResult`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `session_id` | `str` | `필수` |

### `SessionInfo`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |

추가 속성: `session_id`, `title`, `updated_at`, `created_at`, `cwd`, `agent`, `model`, `status`, `source`

### `BackgroundTaskStatus`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `task_id` | `str` | `필수` |
| `state` | `Literal['pending', 'running', 'cancelling', 'completed', 'failed', 'cancelled']` | `필수` |
| `session_id` | `Optional[str]` | `필수` |
| `events_received` | `int` | `필수` |
| `error_type` | `Optional[str]` | `None` |
| `events_dropped` | `int` | `0` |
| `cleanup_pending` | `bool` | `False` |

### `BackgroundTaskResult`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `task_id` | `str` | `필수` |
| `result` | `Optional[str]` | `필수` |
| `is_error` | `bool` | `필수` |
| `structured_output` | `Any` | `None` |
| `kiro_model_usage` | `Any` | `None` |
| `usage_omitted` | `bool` | `False` |

### `BackgroundTaskEventPage`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `events` | `list[dict[str, Any]]` | `필수` |
| `next_cursor` | `int` | `필수` |
| `latest_sequence` | `int` | `필수` |
| `history_dropped` | `int` | `필수` |
| `gap` | `bool` | `필수` |
| `has_more` | `bool` | `필수` |

### `AgentCorePrivateAccess`

| 필드 | 타입 | 기본값 |
| --- | --- | --- |
| `runtime_arn` | `str` | `필수` |
| `caller_role_arns` | `tuple[str, ...]` | `필수` |
| `vpce_ids` | `tuple[str, ...]` | `필수` |
| `endpoint_names` | `tuple[str, ...]` | `('DEFAULT',)` |

추가 속성: `region`, `resource_arns`

## 저장소와 전송

`SessionStore`의 필수 메서드는 `append`와 `load`입니다. 선택 메서드의 계약은 [세션 저장소](guide.md#세션-저장소)를 참고하세요.

### `SessionStore`

```python
async def append(self, key: 'dict[str, str]', entries: 'list[dict[str, Any]]') -> 'None': ...
async def load(self, key: 'dict[str, str]') -> 'Any': ...
```

### `InMemorySessionStore`

```python
def get_entries(self, key: 'dict[str, str]') -> 'list[dict[str, Any]]': ...
def clear(self) -> 'None': ...
async def append(self, key: 'dict[str, str]', entries: 'list[dict[str, Any]]') -> 'None': ...
async def append_if_absent(self, key: 'dict[str, str]', entries: 'list[dict[str, Any]]') -> 'bool': ...
async def append_batches_if_absent(self, key: 'dict[str, str]', batches: 'list[list[dict[str, Any]]]') -> 'bool': ...
async def load(self, key: 'dict[str, str]') -> 'Any': ...
async def load_bounded(self, key: 'dict[str, str]', *, max_entries: 'int', max_bytes: 'int', skip_archives: 'bool' = False, retain_latest_archive: 'bool' = False) -> 'Any': ...
async def list_sessions(self, project_key: 'str') -> 'list[dict[str, Any]]': ...
async def list_session_info(self, project_key: 'str') -> 'list[dict[str, Any]]': ...
async def list_subkeys(self, key: 'dict[str, str]') -> 'list[str]': ...
async def delete(self, key: 'dict[str, str]') -> 'None': ...
```

### `SQLiteSessionStore`

```python
async def append(self, key: 'dict[str, str]', entries: 'list[dict[str, Any]]') -> 'None': ...
async def append_if_absent(self, key: 'dict[str, str]', entries: 'list[dict[str, Any]]') -> 'bool': ...
async def append_batches_if_absent(self, key: 'dict[str, str]', batches: 'list[list[dict[str, Any]]]') -> 'bool': ...
async def load(self, key: 'dict[str, str]') -> 'Any': ...
async def load_bounded(self, key: 'dict[str, str]', *, max_entries: 'int', max_bytes: 'int', skip_archives: 'bool' = False, retain_latest_archive: 'bool' = False) -> 'Any': ...
async def list_sessions(self, project_key: 'str') -> 'list[dict[str, Any]]': ...
async def list_session_info(self, project_key: 'str') -> 'list[dict[str, Any]]': ...
async def list_subkeys(self, key: 'dict[str, str]') -> 'list[str]': ...
async def delete(self, key: 'dict[str, str]') -> 'None': ...
```

### `SQLiteSessionOwnership`

```python
async def register(self, session_id: 'str', owner: 'str') -> 'bool': ...
async def is_owner(self, session_id: 'str', owner: 'str') -> 'bool': ...
async def revoke(self, session_id: 'str', owner: 'str') -> 'bool': ...
```

### `DynamoDBSessionOwnership`

```python
async def register(self, session_id: 'str', owner: 'str') -> 'bool': ...
async def is_owner(self, session_id: 'str', owner: 'str') -> 'bool': ...
async def revoke(self, session_id: 'str', owner: 'str') -> 'bool': ...
```

### `SQLiteCreditLedger`

```python
async def begin(self, scope: 'str', limit: 'Decimal') -> 'tuple[str, Decimal]': ...
async def complete(self, scope: 'str', reservation_id: 'str', usage: 'Optional[Decimal]') -> 'Decimal': ...
async def inspect(self, scope: 'str') -> 'Optional[dict[str, Any]]': ...
async def set_limit(self, scope: 'str', *, expected_limit: 'Union[int, float, str, Decimal]', new_limit: 'Union[int, float, str, Decimal]') -> 'None': ...
async def reconcile(self, scope: 'str', reservation_id: 'str', *, reported_credits: 'Union[int, float, str, Decimal]', limit: 'Optional[Union[int, float, str, Decimal]]' = None) -> 'None': ...
```

### `DynamoDBCreditLedger`

```python
async def begin(self, scope: 'str', limit: 'Decimal') -> 'tuple[str, Decimal]': ...
async def complete(self, scope: 'str', reservation_id: 'str', usage: 'Optional[Decimal]') -> 'Decimal': ...
async def inspect(self, scope: 'str') -> 'Optional[dict[str, Any]]': ...
async def set_limit(self, scope: 'str', *, expected_limit: 'Union[int, float, str, Decimal]', new_limit: 'Union[int, float, str, Decimal]') -> 'None': ...
async def reconcile(self, scope: 'str', reservation_id: 'str', *, reported_credits: 'Union[int, float, str, Decimal]', limit: 'Optional[Union[int, float, str, Decimal]]' = None) -> 'None': ...
```

### `BackgroundTaskManager`

```python
def BackgroundTaskManager(*, max_tasks: 'int' = 16, max_events: 'int' = 256, shutdown_timeout: 'float' = 30.0, event_buffer_timeout: 'float' = 5.0, overflow_policy: "Literal['fail', 'drop-oldest']" = 'fail', max_retained_result_bytes: 'int' = 0, max_history_events: 'int' = 0, max_history_bytes: 'int' = 0, retain_structured_output: 'bool' = False) -> 'None': ...
async def start(self, prompt: 'Any', *, options: 'Optional[KiroAgentOptions]' = None) -> 'str': ...
def owns(self, task_id: 'str') -> 'bool': ...
def status(self, task_id: 'str') -> 'BackgroundTaskStatus': ...
def active_statuses(self, *, limit: 'int' = 128) -> 'tuple[list[BackgroundTaskStatus], bool]': ...
def statuses(self, *, limit: 'int' = 128) -> 'tuple[list[BackgroundTaskStatus], bool]': ...
async def wait(self, task_id: 'str') -> 'BackgroundTaskStatus': ...
def retained_result(self, task_id: 'str') -> 'Optional[BackgroundTaskResult]': ...
def event_history(self, task_id: 'str', *, after: 'int' = 0, limit: 'int' = 32) -> 'BackgroundTaskEventPage': ...
async def events(self, task_id: 'str') -> 'AsyncIterator[Any]': ...
async def stop(self, task_id: 'str') -> 'BackgroundTaskStatus': ...
def forget(self, task_id: 'str') -> 'None': ...
async def aclose(self) -> 'None': ...
```

### `SQLiteBackgroundRequestStore`

```python
def SQLiteBackgroundRequestStore(path: 'Union[str, Path]', *, namespace: 'str' = 'default', timeout: 'float' = 10.0): ...
async def claim(self, runtime_id: 'str', request_id: 'str') -> 'bool': ...
async def exists(self, runtime_id: 'str', request_id: 'str') -> 'bool': ...
```

### `DynamoDBBackgroundRequestStore`

```python
def DynamoDBBackgroundRequestStore(table_name: 'str', *, namespace: 'str' = 'default', region_name: 'Optional[str]' = None, client: 'Any' = None): ...
async def claim(self, runtime_id: 'str', request_id: 'str') -> 'bool': ...
async def exists(self, runtime_id: 'str', request_id: 'str') -> 'bool': ...
```

### `SQLiteBackgroundResultStore`

```python
def SQLiteBackgroundResultStore(path: 'Union[str, Path]', *, namespace: 'str' = 'default', timeout: 'float' = 10.0): ...
async def save(self, runtime_id: 'str', task_id: 'str', payload: 'bytes') -> 'bool': ...
async def load(self, runtime_id: 'str', task_id: 'str') -> 'Optional[bytes]': ...
async def delete(self, runtime_id: 'str', task_id: 'str') -> 'bool': ...
async def save_events(self, runtime_id: 'str', task_id: 'str', payload: 'bytes') -> 'bool': ...
async def load_events(self, runtime_id: 'str', task_id: 'str') -> 'Optional[bytes]': ...
async def delete_events(self, runtime_id: 'str', task_id: 'str') -> 'bool': ...
async def save_event_checkpoint(self, runtime_id: 'str', task_id: 'str', sequence: 'int', payload: 'bytes') -> 'bool': ...
async def load_event_checkpoint(self, runtime_id: 'str', task_id: 'str') -> 'Optional[tuple[int, bytes]]': ...
async def delete_event_checkpoint(self, runtime_id: 'str', task_id: 'str') -> 'bool': ...
```

### `DynamoDBBackgroundResultStore`

```python
def DynamoDBBackgroundResultStore(table_name: 'str', *, namespace: 'str' = 'default', region_name: 'Optional[str]' = None, client: 'Any' = None): ...
async def save(self, runtime_id: 'str', task_id: 'str', payload: 'bytes') -> 'bool': ...
async def load(self, runtime_id: 'str', task_id: 'str') -> 'Optional[bytes]': ...
async def delete(self, runtime_id: 'str', task_id: 'str') -> 'bool': ...
async def save_events(self, runtime_id: 'str', task_id: 'str', payload: 'bytes') -> 'bool': ...
async def load_events(self, runtime_id: 'str', task_id: 'str') -> 'Optional[bytes]': ...
async def delete_events(self, runtime_id: 'str', task_id: 'str') -> 'bool': ...
async def save_event_checkpoint(self, runtime_id: 'str', task_id: 'str', sequence: 'int', payload: 'bytes') -> 'bool': ...
async def load_event_checkpoint(self, runtime_id: 'str', task_id: 'str') -> 'Optional[tuple[int, bytes]]': ...
async def delete_event_checkpoint(self, runtime_id: 'str', task_id: 'str') -> 'bool': ...
```

### `InMemoryMcpOAuthStorage`

```python
async def get_tokens(self) -> 'Any': ...
async def set_tokens(self, tokens: 'Any') -> 'None': ...
async def clear_tokens(self) -> 'None': ...
async def get_client_info(self) -> 'Any': ...
async def set_client_info(self, client_info: 'Any') -> 'None': ...
```

### `Transport`

```python
async def connect(self) -> 'None': ...
async def write(self, data: 'str') -> 'None': ...
def read_messages(self) -> 'AsyncIterator[dict[str, Any]]': ...
async def close(self) -> 'None': ...
def is_ready(self) -> 'bool': ...
async def end_input(self) -> 'None': ...
```

## 오류

`BackgroundTaskError`, `AgentCorePolicyApplyError`, `KiroSDKError`, `CLIConnectionError`, `CLINotFoundError`, `CLIJSONDecodeError`, `ProcessError`, `ProtocolError`, `JSONRPCError`, `ResultError`, `PromptBlockedError`, `McpInputDeniedError`, `ReportedCreditLimitError`, `SessionArchiveError`, `StoreForkError`, `CheckpointRecoveryError`

의미와 확인할 사항은 [오류 처리](guide.md#오류-처리)를 참고하세요.

### `AgentCorePolicyApplyError`

```python
def AgentCorePolicyApplyError(applied: 'tuple[str, ...]', failed_resource: 'str', *, verified_resources: 'tuple[str, ...]' = (), uncertain_resources: 'tuple[str, ...]' = (), phase: 'str' = 'apply') -> 'None': ...
```

`applied_resources`, `failed_resource`, `verified_resources`, `uncertain_resources`, `phase` 속성으로 중단 시점의 적용 상태를 확인합니다. 확인된 정책과 불확실한 쓰기의 구분은 [AgentCore 가이드](agentcore.md)를 참고하세요.

## `ToolAnnotations`

```python
ToolAnnotations(**values: 'Any')
```

MCP 도구 힌트이며 권한 정책이 아닙니다.

## 구조화된 딕셔너리 타입

타입 정의의 필드이며, 서버가 선택 필드를 실제로 보고한다는 보장은 없습니다.

### `ContextUsageResponse`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `categories` | `list[ContextUsageCategory]` | 선택 |
| `totalTokens` | `int` | 선택 |
| `maxTokens` | `int` | 선택 |
| `rawMaxTokens` | `int` | 선택 |
| `model` | `str` | 선택 |
| `isAutoCompactEnabled` | `bool` | 선택 |
| `memoryFiles` | `list[dict[str, Any]]` | 선택 |
| `mcpTools` | `list[dict[str, Any]]` | 선택 |
| `agents` | `list[dict[str, Any]]` | 선택 |
| `gridRows` | `list[list[dict[str, Any]]]` | 선택 |
| `autoCompactThreshold` | `int` | 선택 |
| `deferredBuiltinTools` | `list[dict[str, Any]]` | 선택 |
| `systemTools` | `list[dict[str, Any]]` | 선택 |
| `systemPromptSections` | `list[dict[str, Any]]` | 선택 |
| `slashCommands` | `dict[str, Any]` | 선택 |
| `skills` | `dict[str, Any]` | 선택 |
| `messageBreakdown` | `dict[str, Any]` | 선택 |
| `apiUsage` | `Optional[dict[str, Any]]` | 선택 |
| `percentage` | `float` | 선택 |
| `contextUsagePercentage` | `float` | 선택 |
| `sessionId` | `str` | 선택 |
| `sessionUpdate` | `str` | 선택 |
| `_meta` | `dict[str, Any]` | 선택 |

### `ContextUsageCategory`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `name` | `str` | 필수 |
| `tokens` | `int` | 필수 |
| `color` | `str` | 선택 |
| `isDeferred` | `bool` | 선택 |
| `kiroPercent` | `float` | 선택 |

### `KiroPermissionConsent`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `scope` | `Literal['session', 'workspace', 'user']` | 필수 |
| `resource` | `str` | 필수 |
| `workspaceRoot` | `str` | 선택 |

### `HookContext`

| 필드 | 타입 | 선언 |
| --- | --- | --- |

### `McpSdkServerConfig`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `type` | `Literal['sdk']` | 선택 |
| `name` | `str` | 선택 |
| `version` | `str` | 선택 |
| `tools` | `list[Any]` | 선택 |
| `instance` | `Any` | 선택 |

### `McpToolAnnotations`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `readOnly` | `bool` | 선택 |
| `destructive` | `bool` | 선택 |
| `openWorld` | `bool` | 선택 |

### `McpToolInfo`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `name` | `str` | 필수 |
| `description` | `str` | 선택 |
| `annotations` | `Any` | 선택 |

### `McpServerInfo`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `name` | `str` | 필수 |
| `version` | `str` | 필수 |

### `McpServerStatus`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `name` | `str` | 필수 |
| `status` | `McpServerConnectionStatus` | 필수 |
| `serverInfo` | `McpServerInfo` | 선택 |
| `error` | `str` | 선택 |
| `tools` | `list[McpToolInfo]` | 선택 |
| `config` | `dict[str, Any]` | 선택 |
| `scope` | `str` | 선택 |
| `kiroStatus` | `str` | 선택 |

### `McpStatusResponse`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `mcpServers` | `list[McpServerStatus]` | 필수 |
| `sessionId` | `str` | 선택 |
| `servers` | `list[dict[str, Any]]` | 선택 |

### `SessionKey`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `subpath` | `str` | 선택 |
| `project_key` | `str` | 필수 |
| `session_id` | `str` | 필수 |

### `SessionListSubkeysKey`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `project_key` | `str` | 필수 |
| `session_id` | `str` | 필수 |

### `SessionStoreEntry`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `type` | `str` | 선택 |
| `data` | `Any` | 선택 |
| `uuid` | `str` | 선택 |
| `kiro_sdk_prompt_key` | `str` | 선택 |

### `SessionStoreListEntry`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `session_id` | `str` | 필수 |
| `mtime` | `int` | 필수 |

### `SessionInfoEntry`

| 필드 | 타입 | 선언 |
| --- | --- | --- |
| `session_id` | `str` | 필수 |
| `mtime` | `int` | 필수 |
| `info` | `dict[str, Any]` | 필수 |

## 타입과 기타 이름

`Message`, `HistoryMessage`, `ContentBlock`, `EffortLevel`, `PermissionMode`, `PermissionResult`, `CanUseTool`, `HookCallback`, `HookInput`, `HookOutput`, `McpServerConfig`, `McpServerConnectionStatus`, `SessionStoreFlushMode`

재생성: `python scripts/generate_api_reference.py`
