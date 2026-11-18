"""Kiro ACP subprocess client."""
from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import contextlib
import copy
import hashlib
import inspect
import json
import math
import os
import re
import signal
import stat
import tempfile
import time
import uuid
from collections import deque
from collections.abc import AsyncIterable
from contextvars import ContextVar
from datetime import timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Optional, Union

import jsonschema

from .errors import CheckpointRecoveryError, CLIConnectionError, CLIJSONDecodeError, CLINotFoundError, JSONRPCError, ProcessError, PromptBlockedError, ProtocolError, ReportedCreditLimitError, ResultError, SessionArchiveError
from .credit_ledger import _amount as _credit_amount, _scope as _credit_scope
from .timestamps import parse_timestamp
from .loop_lock import make_loop_lock
from .kiro_client_auth import validate_client_token
from .hooks import HOOK_TRIGGERS, NATIVE_HOOK_TRIGGERS, NativeHookBridge, _bounded_hook_group, _bounded_native_stop_group, hook_events
from .native_hook_runtime import NativeHookRuntime, METHOD as NATIVE_HOOK_METHOD, command_result
from .agent_mcp import inline_sdk_servers
from .runtime_policy import VerbatimPrompt
from .runtime_usage import validate_usage_report
from .hook_failure import _HookFailureRunner, _mcp_warning_guard
from .history import tool_call_message
from .hook_output import parse_hook_output
from .hook_messages import append_hook_warning as _append_hook_warning
from .external_mcp import ExternalStdioMcpBridge
from .remote_mcp import RemoteMcpBridge
from .mcp_oauth import McpOAuthConfig
from .mcp import SdkMcpBridge, transform_mcp_arguments, transform_mcp_result
from .mcp_config import load_mcp_config
from .mcp_status import McpStatusTracker
from .process_env import child_process_env
from .session_store import MirrorWriter, project_key_for_directory
from .stream_prefetch import PrefetchedInput
from .transport import Transport
from .types import (PERMISSION_MODES, AgentDefinition, AgentMessage, HookEventMessage, HookMatcher, KiroAgentOptions, KiroEvent, MirrorErrorMessage, PermissionResultAllow, PermissionResultDeny, ResultMessage, StreamEvent, TextBlock, ThoughtBlock,
                    ToolCall, ToolPermissionContext)

if TYPE_CHECKING:
    from .background_tasks import BackgroundTaskManager


_launch_cleanup_tasks: set[asyncio.Task] = set()
_PROCESS_CLEANUP_TIMEOUT = 3.0
_STREAM_INPUT_CLEANUP_TIMEOUT = 1.0
_MAX_PENDING_PROCESS_CLEANUPS = 128
_MAX_REPORTED_SUBAGENT_TYPES = 1024
_FILE_CHANGED_CONTEXT: ContextVar[Any] = ContextVar("kiro_file_changed_client", default=None)
_SDK_HOOK_CONTEXT: ContextVar[Any] = ContextVar("kiro_sdk_hook_client", default=None)
_CLIENT_AUTH_CONTEXT: ContextVar[tuple[Any, ...]] = ContextVar("kiro_client_auth_clients", default=())
_NATIVE_HOOK_MESSAGE_TIMEOUT = 5.0
_MAX_BLOCKED_PROMPT_BYTES = 65536


def _blank_client_api_key(environment: dict[str, str]) -> dict[str, str]:
    if os.name == "nt":
        environment = {key: value for key, value in environment.items()
                       if not isinstance(key, str) or key.upper() != "KIRO_API_KEY"}
    return {**environment, "KIRO_API_KEY": ""}


_KIRO_HOOK_TRIGGERS = frozenset({
    "SessionStart", "SessionEnd", "Stop", "PreToolUse", "PostToolUse", "PreTaskExec",
    "PostTaskExec", "UserPromptSubmit", "PostFileCreate", "PostFileSave", "PostFileDelete", "Manual",
})
_KIRO_HOOK_TRIGGER_ALIASES = {
    "sessionStart": "SessionStart", "agentSpawn": "SessionStart", "agentStop": "Stop", "stop": "Stop",
    "promptSubmit": "UserPromptSubmit", "userPromptSubmit": "UserPromptSubmit",
    "preTaskExecution": "PreTaskExec", "postTaskExecution": "PostTaskExec",
    "preToolUse": "PreToolUse", "postToolUse": "PostToolUse",
    "fileCreated": "PostFileCreate", "fileEdited": "PostFileSave", "AfterFileEdit": "PostFileSave",
    "fileDeleted": "PostFileDelete", "userTriggered": "Manual",
}


def _hook_warning(event: str, message: str, session_id: Optional[str]) -> HookEventMessage:
    return HookEventMessage("hook_warning", {"hook_event_name": event, "message": message, "session_id": session_id},
                            hook_event_name=event, session_id=session_id)



def _snapshot_blocked_prompt(prompt: Any) -> Optional[Union[str, list[dict[str, str]]]]:
    """Copy a bounded, text-only submitted prompt without resolving attachments."""
    try:
        if type(prompt) is str:
            if len(prompt) > _MAX_BLOCKED_PROMPT_BYTES or len(prompt.encode("utf-8")) > _MAX_BLOCKED_PROMPT_BYTES:
                return None
            return prompt
        if type(prompt) is not list or len(prompt) > 128:
            return None
        result: list[dict[str, str]] = []
        text_bytes = 0
        for block in prompt:
            if (type(block) is not dict or set(block) != {"type", "text"} or
                    type(block["type"]) is not str or block["type"] != "text" or
                    type(block["text"]) is not str or len(block["text"]) > _MAX_BLOCKED_PROMPT_BYTES):
                return None
            text_bytes += len(block["text"].encode("utf-8"))
            if text_bytes > _MAX_BLOCKED_PROMPT_BYTES:
                return None
            result.append({"type": "text", "text": block["text"]})
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return result if len(encoded) <= _MAX_BLOCKED_PROMPT_BYTES else None
    except (UnicodeEncodeError, TypeError, ValueError, OverflowError):
        return None


def _snapshot_blocked_prompt_json(
    prompt: Any, max_bytes: int = _MAX_BLOCKED_PROMPT_BYTES,
) -> Optional[Union[str, list[dict[str, Any]]]]:
    """Copy bounded JSON-only content without resolving or interpreting attachments."""
    if type(prompt) is str:
        if max_bytes == _MAX_BLOCKED_PROMPT_BYTES:
            return _snapshot_blocked_prompt(prompt)
        try:
            return prompt if len(prompt) <= max_bytes and len(prompt.encode("utf-8")) <= max_bytes else None
        except UnicodeEncodeError:
            return None
    if type(prompt) is not list or len(prompt) > 128 or any(type(block) is not dict for block in prompt):
        return None
    seen: set[int] = set()
    nodes = 0
    total = 0
    def copy_value(value: Any, depth: int) -> Any:
        nonlocal nodes, total
        nodes += 1
        if nodes > 1024 or depth > 12:
            raise ValueError("Blocked prompt exceeds structural limits")
        kind = type(value)
        if kind is str:
            if len(value) > max_bytes:
                raise ValueError("Blocked prompt exceeds size limit")
            total += len(value.encode("utf-8"))
            if total > max_bytes:
                raise ValueError("Blocked prompt exceeds size limit")
            return value
        if kind is type(None) or kind is bool:
            return value
        if kind is int:
            if value.bit_length() > 256:
                raise ValueError("Blocked prompt number is too large")
            return value
        if kind is float:
            if not math.isfinite(value):
                raise ValueError("Blocked prompt number is not finite")
            return value
        if kind not in (dict, list) or len(value) > 128 or id(value) in seen:
            raise ValueError("Blocked prompt contains unsupported or repeated content")
        seen.add(id(value))
        if kind is list:
            return [copy_value(item, depth + 1) for item in value]
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Blocked prompt contains an invalid key")
            copied_key = copy_value(key, depth + 1)
            result[copied_key] = copy_value(item, depth + 1)
        return result
    try:
        result = copy_value(prompt, 0)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return result if len(encoded) <= max_bytes else None
    except (UnicodeEncodeError, TypeError, ValueError, OverflowError, RecursionError, RuntimeError):
        return None


def _normalize_context_usage(result: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Add the SDK's observed usage fields to a copied Kiro snapshot."""
    if result is None:
        return None
    metadata = result.get("_meta")
    kiro = metadata.get("kiro") if isinstance(metadata, dict) else None
    candidates = []
    if isinstance(kiro, dict):
        context = kiro.get("contextUsage")
        if isinstance(context, dict):
            candidates.append(context.get("usagePercentage"))
        candidates.append(kiro.get("usagePercentage"))
        breakdown = kiro.get("breakdown")
        if isinstance(breakdown, dict):
            categories = []
            for name, item in breakdown.items():
                if not isinstance(name, str) or not name or not isinstance(item, dict):
                    continue
                tokens = item.get("tokens")
                if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                    continue
                category: dict[str, Any] = {"name": name, "tokens": tokens}
                percent = item.get("percent")
                try:
                    valid = (not isinstance(percent, bool) and isinstance(percent, (int, float)) and
                             math.isfinite(percent) and percent >= 0)
                except OverflowError:
                    valid = False
                if valid:
                    category["kiroPercent"] = float(percent)
                categories.append(category)
            if categories:
                result["categories"] = categories
    candidates.append(result.get("contextUsagePercentage"))
    for value in candidates:
        try:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float)) and
                     math.isfinite(value) and value >= 0)
        except OverflowError:
            valid = False
        if valid:
            result["percentage"] = float(value)
            break
    return result


def _available_commands(value: Any) -> Optional[list[dict[str, Any]]]:
    """Bound and normalize a server-advertised ACP slash-command snapshot."""
    if not isinstance(value, list) or len(value) > 256:
        return None
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    total = 0
    for item in value:
        if not isinstance(item, dict) or ("type" in item and item["type"] != "command"):
            continue
        name, description = item.get("name"), item.get("description")
        if (not isinstance(name, str) or not name or len(name) > 256 or
                not isinstance(description, str) or len(description) > 2048 or
                any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                    for char in name) or
                any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) <= 159 or
                    0xD800 <= ord(char) <= 0xDFFF for char in description)):
            continue
        if name in names:
            return None
        names.add(name)
        normalized: dict[str, Any] = {"name": name, "description": description}
        input_spec = item.get("input")
        if (isinstance(input_spec, dict) and
                ("type" not in input_spec or input_spec["type"] == "text")):
            hint = input_spec.get("hint")
            if (isinstance(hint, str) and len(hint) <= 1024 and
                    not any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                            for char in hint)):
                normalized["input"] = {"hint": hint}
        total += len(name.encode("utf-8")) + len(description.encode("utf-8"))
        if "input" in normalized:
            total += len(normalized["input"]["hint"].encode("utf-8"))
        if total > 128 * 1024:
            return None
        result.append(normalized)
    return result


def _track_process_cleanup(task: asyncio.Task) -> None:
    if task in _launch_cleanup_tasks:
        return
    _launch_cleanup_tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        _launch_cleanup_tasks.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)


async def _terminate_owned_process(process: asyncio.subprocess.Process, *,
                                   signal_exited_group: bool = False) -> None:
    """Terminate owned work, without waiting indefinitely for a blocked pipe."""
    stdin = process.stdin
    if stdin is not None:
        stdin.close()

    def send_signal(force: bool) -> None:
        if os.name == "posix":
            if force or signal_exited_group or process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill() if force else process.terminate()

    send_signal(False)

    async def close_input() -> None:
        if stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await stdin.wait_closed()

    waiters = [asyncio.create_task(process.wait())]
    if stdin is not None:
        waiters.append(asyncio.create_task(close_input()))
    forced = False
    try:
        _, pending = await asyncio.wait(waiters, timeout=_PROCESS_CLEANUP_TIMEOUT)
        if pending:
            send_signal(True)
            forced = True
            _, pending = await asyncio.wait(pending, timeout=_PROCESS_CLEANUP_TIMEOUT)
        if pending:
            raise CLIConnectionError("Subprocess cleanup did not finish")
        for task in waiters:
            task.result()
    except BaseException:
        if not forced and any(not task.done() for task in waiters):
            with contextlib.suppress(Exception):
                send_signal(True)
        raise
    finally:
        for task in waiters:
            if not task.done():
                _track_process_cleanup(task)
            elif not task.cancelled():
                task.exception()


def _process_credentials(user: str) -> dict[str, Any]:
    """Give a child of another account that account's groups instead of the parent's."""
    import pwd
    account = pwd.getpwnam(user)
    if os.geteuid() != 0 and account.pw_uid == os.geteuid():
        # An unprivileged process cannot change groups and already runs as this account.
        return {}
    return {"group": account.pw_gid, "extra_groups": os.getgrouplist(account.pw_name, account.pw_gid)}


async def _spawn_cli(*command: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """Keep ownership of a child even if cancellation races subprocess creation."""
    if kwargs.get("user") is not None:
        kwargs.update(_process_credentials(kwargs["user"]))
    for task in tuple(_launch_cleanup_tasks):
        if task.done():
            _launch_cleanup_tasks.discard(task)
            if not task.cancelled():
                task.exception()
    if len(_launch_cleanup_tasks) >= _MAX_PENDING_PROCESS_CLEANUPS:
        raise CLIConnectionError("Pending subprocess cleanup capacity reached")
    launch = asyncio.create_task(asyncio.create_subprocess_exec(*command, **kwargs))
    try:
        return await asyncio.shield(launch)
    except asyncio.CancelledError:
        async def cleanup() -> None:
            try:
                process = await launch
            except Exception:
                return
            await _terminate_owned_process(process)

        task = asyncio.create_task(cleanup())
        _track_process_cleanup(task)
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                continue
            except Exception as exc:
                raise CLIConnectionError("Cancelled subprocess cleanup failed") from exc
        raise


def _strict_json(text: str) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"Invalid JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)


def _reported_turn_credits(metadata: dict[str, Any]) -> Optional[Decimal]:
    summaries = metadata.get("promptTurnSummaries")
    if not isinstance(summaries, list):
        return None
    total = Decimal(0)
    found = False
    with localcontext() as context:
        context.prec = 1000
        for item in summaries:
            if not isinstance(item, dict):
                return None
            if item.get("unit") != "credit":
                continue
            value = item.get("usage")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            try:
                amount = Decimal(str(value))
            except InvalidOperation:
                return None
            if not amount.is_finite() or amount < 0:
                return None
            total += amount
            found = True
    return total if found else None


def _sum_credits(values) -> Decimal:
    with localcontext() as context:
        context.prec = 1000
        return sum(values, Decimal(0))


def _is_cancelled_rpc_error(error: JSONRPCError) -> bool:
    if error.code == -32800:
        return True
    return (error.code == -32603 and isinstance(error.data, dict) and
            error.data.get("details") == "This operation was aborted")


def _tool_kiro_metadata(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    meta = event.get("_meta")
    value = meta.get("kiro") if isinstance(meta, dict) else None
    return copy.deepcopy(value) if isinstance(value, dict) else None


def _reported_kiro_metadata(event: Any) -> Optional[dict[str, Any]]:
    if not isinstance(event, dict):
        return None
    meta = event.get("_meta")
    value = meta.get("kiro") if isinstance(meta, dict) else None
    return value if isinstance(value, dict) else None


def _valid_reported_agent_id(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and value == value.strip() and len(value) <= 256 and
            not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def _normalize_hook_listing_filters(trigger: Any, tool_id: Any, tool_tags: Any,
                                    include_disabled: Any) -> tuple[Optional[str], Optional[str], Optional[list[str]], bool]:
    if trigger is not None:
        if not isinstance(trigger, str):
            raise ValueError("Unsupported Kiro hook trigger")
        trigger = _KIRO_HOOK_TRIGGER_ALIASES.get(trigger, trigger)
        if trigger not in _KIRO_HOOK_TRIGGERS:
            raise ValueError("Unsupported Kiro hook trigger")
    if tool_id is not None and not _valid_reported_agent_id(tool_id):
        raise ValueError("tool_id must be a bounded nonempty tool ID")
    if (tool_tags is not None and
            (tool_id is None or not isinstance(tool_tags, list) or len(tool_tags) > 32 or
             any(not _valid_reported_agent_id(tag) for tag in tool_tags) or
             len(set(tool_tags)) != len(tool_tags))):
        raise ValueError("tool_tags requires a tool_id and distinct bounded tags")
    if not isinstance(include_disabled, bool):
        raise ValueError("include_disabled must be a boolean")
    return trigger, tool_id, list(tool_tags) if tool_tags is not None else None, include_disabled


def _reported_tool_agent_id(*events: Any) -> Optional[str]:
    """Return an unambiguous reported subagent ID, excluding the spawn call itself."""
    reported = None
    for event in events:
        if not isinstance(event, dict):
            continue
        metadata = _reported_kiro_metadata(event)
        if metadata is None:
            continue
        if metadata.get("kind") == "agent-subtask":
            return None
        if "agentSubtaskId" not in metadata:
            continue
        value = metadata["agentSubtaskId"]
        if not _valid_reported_agent_id(value):
            return None
        if reported is not None and value != reported:
            return None
        reported = value
    return reported


def _normalized_tool_hook_identity(payload: dict[str, Any]) -> dict[str, Any]:
    event = copy.deepcopy(payload)
    metadata = _reported_kiro_metadata(payload)
    nested_id = _reported_tool_agent_id(payload)
    if "agent_id" in payload:
        direct = payload["agent_id"]
        has_nested = bool(metadata and ("agentSubtaskId" in metadata or metadata.get("kind") == "agent-subtask"))
        if not _valid_reported_agent_id(direct) or (has_nested and direct != nested_id):
            event.pop("agent_id", None)
    elif nested_id is not None:
        event["agent_id"] = nested_id
    return event


def _valid_concrete_permission_path(path: Any) -> bool:
    return (isinstance(path, str) and bool(path) and len(path) <= 4096 and
            not any(char in path for char in "*?[]") and
            not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in path) and
            (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()))


def _reported_permission_path(params: Any) -> Optional[str]:
    """Return only an explicitly reported concrete filesystem request target."""
    metadata = _reported_kiro_metadata(params)
    consent = metadata.get("consent") if metadata else None
    if not isinstance(consent, dict) or consent.get("capability") not in ("fs_read", "fs_write"):
        return None
    path = consent.get("triggeringResource")
    return path if _valid_concrete_permission_path(path) else None


def _reported_display_field(tool: Any, name: str) -> Optional[str]:
    value = tool.get(name) if isinstance(tool, dict) else None
    return (value if isinstance(value, str) and len(value) <= 4096 and "\0" not in value and
            not any(0xD800 <= ord(char) <= 0xDFFF for char in value) else None)


def _selected_model(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    options = data.get("configOptions")
    if isinstance(options, list):
        for option in options:
            if isinstance(option, dict) and option.get("id") == "model" and isinstance(option.get("currentValue"), str):
                return option["currentValue"]
    models = data.get("models")
    if isinstance(models, dict) and isinstance(models.get("currentModelId"), str):
        return models["currentModelId"]
    return None


def _merge_tool_updates(previous: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    merged = {**previous, **event}
    old_meta, new_meta = previous.get("_meta"), event.get("_meta")
    if isinstance(old_meta, dict) and isinstance(new_meta, dict):
        merged_meta = {**old_meta, **new_meta}
        old_kiro, new_kiro = old_meta.get("kiro"), new_meta.get("kiro")
        if isinstance(old_kiro, dict) and isinstance(new_kiro, dict):
            merged_meta["kiro"] = {**old_kiro, **new_kiro}
        merged["_meta"] = merged_meta
    return merged


def _stream_content(item: Any, *, allow_composed: bool = False, force_composed: bool = False) -> Any:
    """Each streamed item is one prompt: a string, a list of ACP content blocks or a VerbatimPrompt."""
    if isinstance(item, VerbatimPrompt):
        if not allow_composed:
            raise ValueError("VerbatimPrompt requires runtime_compat_mode='v3'")
        return item
    if not isinstance(item, (str, list)):
        raise ValueError("Streaming input items must be prompt strings or lists of ACP content blocks")
    return VerbatimPrompt(item) if force_composed else item


def _snapshot_stream_content(content: Any, max_bytes: int) -> Any:
    if isinstance(content, VerbatimPrompt):
        value = _snapshot_blocked_prompt_json(content.content, max_bytes)
        return VerbatimPrompt(value) if value is not None else None
    return _snapshot_blocked_prompt_json(content, max_bytes)


def _stream_snapshot_value(content: Any) -> Any:
    return content.content if isinstance(content, VerbatimPrompt) else content


def _receive_queued_messages(client: Any) -> AsyncIterator[Any]:
    """Use finite reception for SDK-owned jobs, regardless of client preference."""
    receiver = getattr(client, "_receive_current_messages", None)
    return receiver() if callable(receiver) else client.receive_messages()


async def _buffered_stream_input(contents: list[Any]) -> AsyncIterator[Any]:
    for content in contents:
        yield content


def _prompt_content(blocks: list[Any], server_info: Optional[dict[str, Any]]) -> list[Any]:
    """Check ACP image and embedded resource blocks against Kiro's advertised prompt capabilities."""
    capabilities = (server_info or {}).get("agentCapabilities", {}).get("promptCapabilities", {})
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ValueError("Prompt content blocks must be ACP content dictionaries")
        if block["type"] == "image":
            mime = block.get("mimeType")
            if set(block) - {"type", "mimeType", "data", "uri"} or not isinstance(mime, str) or \
                    not re.fullmatch(r"image/[A-Za-z0-9.+-]+", mime):
                raise ValueError("Image blocks require base64 data and an image MIME type")
            _validate_base64(block.get("data"), "Image")
            if capabilities.get("image") is not True:
                raise ProtocolError("Kiro does not advertise image prompt support")
        elif block["type"] == "resource":
            resource = block.get("resource")
            if (set(block) != {"type", "resource"} or not isinstance(resource, dict) or
                    not isinstance(resource.get("uri"), str) or not resource["uri"] or
                    ("text" in resource) == ("blob" in resource) or
                    ("text" in resource and not isinstance(resource["text"], str))):
                raise ValueError("Resource blocks require a URI and either text or base64 blob content")
            if "blob" in resource:
                _validate_base64(resource["blob"], "Resource")
            if capabilities.get("embeddedContext") is not True:
                raise ProtocolError("Kiro does not advertise embedded context support")
    return blocks


def _validate_base64(data: Any, kind: str) -> None:
    if not isinstance(data, str) or not data:
        raise ValueError(f"{kind} data must be a nonempty base64 string")
    try:
        base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{kind} data must be valid base64") from exc


def _nullable_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError("Expected an ACP object")
    return value


def _kiro_capabilities(info: Any) -> dict[str, Any]:
    caps = _nullable_mapping(_nullable_mapping(info).get("agentCapabilities"))
    return _nullable_mapping(_nullable_mapping(caps.get("_meta")).get("kiro"))


_BOOLEAN_OPTIONS = ("include_thoughts", "include_partial_messages", "continue_conversation", "enable_kiro_hooks",
                    "require_sandbox", "fork_session")


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_option_types(options: KiroAgentOptions, transport: Optional[Transport]) -> None:
    """Reject option values whose type would otherwise be reinterpreted or fail late."""
    for name in _BOOLEAN_OPTIONS:
        if type(getattr(options, name)) is not bool:
            raise ValueError(f"{name} must be a boolean")
    if options.agent_engine not in (None, "v2", "v3"):
        raise ValueError("agent_engine must be None, 'v2' or 'v3'")
    dirs = options.add_dirs
    if isinstance(dirs, (str, bytes, os.PathLike)) or not isinstance(dirs, (list, tuple)):
        raise TypeError("add_dirs must be a list of directory paths, not a single path or mapping")
    for path in dirs:
        if not isinstance(path, (str, Path)) or not str(path) or "\0" in str(path):
            raise ValueError("add_dirs entries must be nonempty str or Path values without NUL")
    for name, limit in (("agent", 256), ("resume", 512), ("resume_session_at", 512)):
        value = getattr(options, name)
        if value is not None and (not isinstance(value, str) or not value or len(value) > limit or
                                  _has_control_characters(value)):
            raise ValueError(f"{name} must be a nonempty string of at most {limit} characters without control characters")
    for name in ("stderr", "can_use_tool"):
        value = getattr(options, name)
        if value is not None and not callable(value):
            raise ValueError(f"{name} must be callable")
    store = options.session_store
    if store is not None and not callable(getattr(store, "append", None)):
        raise ValueError("session_store must provide an async append method")
    if options.resume_from_store and store is not None and not callable(getattr(store, "load", None)):
        raise ValueError("resume_from_store requires a session_store with an async load method")
    source = options.session_store_source_directory
    if source is not None and (not isinstance(source, (str, Path)) or "\0" in str(source) or
                               not Path(source).is_absolute()):
        raise ValueError("session_store_source_directory must be an absolute path")
    if options.output_format is not None and not isinstance(options.output_format, dict):
        raise ValueError("output_format must be {'type': 'json_schema', 'schema': {...}}")
    cli = options.cli_path
    if cli is not None and (not isinstance(cli, (str, Path)) or not str(cli) or "\0" in str(cli)):
        raise ValueError("cli_path must be a nonempty path without NUL")
    if transport is not None and options.runtime_compat_mode == "v3":
        raise ValueError("Runtime policies require local POSIX Kiro V3 and reject or runtime-acp hooks")
    if options.env_inheritance_mode not in ("inherit", "minimal"):
        raise ValueError("env_inheritance_mode must be inherit or minimal")
    environment = options.env
    if not isinstance(environment, dict):
        raise ValueError("env must be a mapping of strings")
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("env keys and values must be strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("env keys must be nonempty without '=' or NUL, and values must not contain NUL")


def _native_hook_option_signature(options: KiroAgentOptions) -> Any:
    def matchers(group: Any) -> Any:
        return tuple((id(matcher), getattr(matcher, "matcher", None), getattr(matcher, "timeout", None),
                      getattr(matcher, "on_failure", None), tuple(id(hook) for hook in getattr(matcher, "hooks", ()) or ()))
                     for matcher in (group or ()))
    hooks = options.native_hooks if isinstance(options.native_hooks, dict) else {}
    return id(options.native_hooks), tuple((event, id(group), matchers(group)) for event, group in hooks.items())


def _connection_option_signature(options: KiroAgentOptions) -> Any:
    """Options consumed once per client whose later change would otherwise be ignored silently."""
    return (id(options.session_store), options.session_store_flush, options.session_store_error_policy,
            _native_hook_option_signature(options), options.enable_file_checkpointing,
            options.agent_engine, options.env_inheritance_mode)


async def _resolve_callback(value: Any) -> Any:
    """Accept synchronous callbacks and callbacks returning awaitables."""
    return await value if inspect.isawaitable(value) else value


class KiroSDKClient:
    """A persistent ACP session. Only one prompt may be active at a time."""
    @property
    def _permission_mode(self) -> Any:
        if ("permission_mode" in self._runtime_option_sources and
                self.options.permission_mode != self._runtime_option_sources["permission_mode"]):
            raise ValueError("Create a new client after changing options.permission_mode; "
                             "use set_permission_mode() to change the mode of this client")
        return self._runtime_option_values.get("permission_mode", self.options.permission_mode)

    @_permission_mode.setter
    def _permission_mode(self, value: Any) -> None:
        self._runtime_option_sources.setdefault("permission_mode", self.options.permission_mode)
        self._runtime_option_values["permission_mode"] = value

    @property
    def _model_option(self) -> Any:
        if "model" in self._runtime_option_sources and self.options.model != self._runtime_option_sources["model"]:
            raise ValueError("Create a new client after changing model")
        return self._runtime_option_values.get("model", self.options.model)

    @_model_option.setter
    def _model_option(self, value: Any) -> None:
        self._runtime_option_sources.setdefault("model", self.options.model)
        self._runtime_option_values["model"] = value

    def __init__(self, options: Optional[KiroAgentOptions] = None, transport: Optional[Transport] = None):
        self.options = options or KiroAgentOptions()
        _validate_option_types(self.options, transport)
        self._connection_option_snapshot = _connection_option_signature(self.options)
        if not isinstance(self.options.native_hooks, dict):
            raise ValueError("native_hooks must be a mapping")
        self._native_hook_bridge_mode = self.options.native_hook_bridge_mode
        self._native_hook_runtime_path = self.options.native_hook_runtime_path
        self._native_hook_runtime = None
        self._native_acp_dispatch_tasks: set[asyncio.Task] = set()
        self._native_callback_slots: Any = None
        self._extension_hook_revision = 0
        self._runtime_policy_snapshot = self._runtime_policy()
        self._runtime_agent_efforts: dict[str, str] = {}
        self._runtime_agent_max_turns: dict[str, int] = {}
        self._runtime_foreground_agents: list[str] = []
        self._agent_inline_sdk_snapshot, self._agent_inline_sdk_configs = inline_sdk_servers(self.options)
        if self._agent_inline_sdk_configs and transport is not None:
            raise ValueError("Inline SDK agent servers require the local subprocess transport")
        self._active_runtime_request_id: Optional[str] = None
        self._runtime_cancelled_hooks: set[tuple[str, str, str]] = set()
        self._runtime_option_values: dict[str, Any] = {}
        self._runtime_option_sources: dict[str, Any] = {"permission_mode": self.options.permission_mode}
        self._client_auth_snapshot = self._client_auth_scope()
        self._client_auth_lock = make_loop_lock()
        self._client_auth_tasks: set[asyncio.Future[Any]] = set()
        self._client_auth_pending = 0
        self._client_auth_request_times: deque[float] = deque()
        self._require_client_auth_authorization = False
        self._client_auth_factory_guard: Optional[Callable[[], bool]] = None
        self._client_auth_ancestors: tuple[KiroSDKClient, ...] = ()
        self._client_auth_control_active = False
        self._client_auth_shutdown_revision = 0
        self._incoming_message_limit: Optional[tuple[int, int]] = None
        self._incoming_message_count = 0
        self._incoming_message_bytes = 0
        self._hook_failure_snapshot = self._capture_hook_failure_snapshot()
        self._hook_failure_runner = _HookFailureRunner()
        self._require_native_failure_authorization = False
        self._preset_generation = 0
        self._connection_generation = 0
        self._validate_unsupported_limits()
        self._resume_from_store_bounded_snapshot = (
            self.options.resume_from_store_bounded_read,
            self.options.session_store if self.options.resume_from_store_bounded_read else None)
        self._receive_messages_mode = self.options.receive_messages_mode
        self._stream_input_policy = (self.options.stream_input_mode, self.options.stream_input_buffer_max_messages,
                                     self.options.stream_input_buffer_max_bytes,
                                     self.options.stream_input_buffer_timeout)
        self._process_user = self.options.user
        if self._process_user is not None and transport is not None:
            raise ValueError("user requires a local Kiro subprocess")
        self._allowed_tools_snapshot = (
            'native', copy.deepcopy([]),
            copy.deepcopy({}), copy.deepcopy(self.options.kiro_permissions),
            copy.deepcopy(self.options.tools), self.options.agent_engine, self._permission_mode,
            copy.deepcopy([]), 'native',
            copy.deepcopy({}))
        self._mapped_tool_permissions = self._compile_tool_permissions()
        self._permission_update_callback = self._effective_can_use_tool()
        self._permission_update_allow_timeout_snapshot = 5.0
        self._accept_edits_additional_roots_snapshot = (
            list([]))
        self._accept_edits_additional_root_identities = self._snapshot_edit_directory_roots(
            self._accept_edits_additional_roots_snapshot)
        self._task_budget_snapshot = ('reject', None,
                                      None, False)
        self._sdk_mcp_output_snapshot = (dict(self.options.sdk_mcp_output_transforms),
                                         self.options.sdk_mcp_output_timeout)
        self._sdk_mcp_input_snapshot = (dict(self.options.sdk_mcp_input_transforms),
                                        self.options.sdk_mcp_input_timeout)
        self._mcp_failure_input_names = frozenset(
            name for name, callback in self._sdk_mcp_input_snapshot[0].items()
            if False)
        self._mcp_failure_output_names = frozenset(
            name for name, callback in self._sdk_mcp_output_snapshot[0].items()
            if False)
        self._require_mcp_failure_authorization = False
        self._precompact_mode_snapshot = 'reject'
        self._prompt_hook_stop_mode = 'reject'
        self._prompt_block_original_mode = 'suppress'
        self._native_hook_system_message_callback = self.options.on_native_hook_system_message
        self._precompact_hook_snapshot = self._precompact_hook_signature()
        self._precompact_hook_refs = [(item, tuple(item.hooks)) for item in []]
        self._notification_mode_snapshot = 'reject'
        self._notification_hook_snapshot = self._notification_hook_signature()
        self._notification_hook_refs = [(item, tuple(item.hooks)) for item in []]
        self._compaction_request_sent = False
        self._native_hooks = {event: list(matchers) for event, matchers in
                              self._native_option_hooks().items()
                              if matchers}
        v3_only_hooks = {"PostFileCreate", "PostFileSave", "PostFileDelete", "PreTaskExec", "PostTaskExec"}
        if set(self._native_hooks) & v3_only_hooks and self.options.agent_engine != "v3":
            raise ValueError("File, task and session-end hooks require Kiro V3")
        self._sdk_pretool_mode = self.options.sdk_pretool_hook_mode
        self._sdk_pretool_snapshot = self._pretool_hook_signature() if self._sdk_pretool_mode == "native" else ()
        if self._sdk_pretool_mode == "native":
            if os.name != "posix" or self.options.agent_engine != "v3" or not self.options.enable_kiro_hooks:
                raise ValueError("Native SDK PreToolUse hooks require POSIX and enabled Kiro V3 hooks")
            if transport is not None:
                raise ValueError("Native SDK PreToolUse hooks require a local CLI")
            if self._sdk_hooks.get("PreToolUse"):
                native = []
                for matcher in self._sdk_hooks["PreToolUse"]:
                    callbacks = [self._adapt_native_sdk_pretool(hook) for hook in matcher.hooks]
                    native.append(HookMatcher(matcher=matcher.matcher, hooks=callbacks, timeout=matcher.timeout,
                                              on_failure=self._effective_hook_failure_policy("PreToolUse", matcher)))
                self._native_hooks.setdefault("PreToolUse", []).extend(native)
        self._sdk_stop_mode = self.options.sdk_stop_hook_mode
        self._sdk_stop_limit_snapshot = self.options.max_native_stop_blocks
        self._sdk_stop_snapshot = self._stop_hook_signature() if self._sdk_stop_mode == "native" else ()
        self._sdk_stop_refs = ([(item, tuple(item.hooks)) for item in self._sdk_hooks.get("Stop", [])]
                               if self._sdk_stop_mode == "native" else [])
        self._native_stop_lock = make_loop_lock()
        self._native_stop_blocks = 0
        self._native_stop_valid_blocks = 0
        self._native_stop_warning_count = 0
        self._native_stop_warning_bytes = 0
        stop_matchers = (self._sdk_hooks.get("Stop", []) if self._sdk_stop_mode == "native" else
                         self._native_option_hooks().get("Stop", []))
        self._native_stop_multi = (sum(len(matcher.hooks) for matcher in stop_matchers) > 1 and
            any((self._effective_hook_failure_policy("Stop", matcher) if self._sdk_stop_mode == "native" else
                 matcher.on_failure) is not None for matcher in stop_matchers))
        if self._sdk_stop_mode == "native":
            if os.name != "posix" or self.options.agent_engine != "v3" or not self.options.enable_kiro_hooks:
                raise ValueError("Native SDK Stop hooks require POSIX and enabled Kiro V3 hooks")
            if transport is not None:
                raise ValueError("Native SDK Stop hooks require a local CLI")
            if self._native_hooks.get("Stop"):
                raise ValueError("Native SDK Stop hooks cannot be combined with native Stop hooks")
            native = []
            for matcher in self._sdk_hooks.get("Stop", []):
                policy = self._effective_hook_failure_policy("Stop", matcher)
                callbacks = [self._adapt_native_sdk_stop(hook, matcher.timeout, policy) for hook in matcher.hooks]
                native.append(HookMatcher(matcher=matcher.matcher, hooks=callbacks, timeout=matcher.timeout,
                                          on_failure=policy))
            if native:
                self._native_hooks.setdefault("Stop", []).extend(native)
        self._sdk_session_start_mode = 'reject'
        self._subagent_start_mode = 'observed'
        self._base_native_hooks = {event: list(matchers) for event, matchers in self._native_hooks.items()}
        if self.options.max_buffer_size is not None and (isinstance(self.options.max_buffer_size, bool) or
                                                         not isinstance(self.options.max_buffer_size, int) or
                                                         self.options.max_buffer_size <= 0):
            raise ValueError("max_buffer_size must be positive")
        if self.options.startup_timeout is not None and (isinstance(self.options.startup_timeout, bool) or
                                                        not isinstance(self.options.startup_timeout, (int, float)) or
                                                        not math.isfinite(self.options.startup_timeout) or
                                                        self.options.startup_timeout <= 0):
            raise ValueError("startup_timeout must be positive")
        if self.options.turn_timeout is not None and (isinstance(self.options.turn_timeout, bool) or
                                                     not isinstance(self.options.turn_timeout, (int, float)) or
                                                     not math.isfinite(self.options.turn_timeout) or
                                                     self.options.turn_timeout <= 0):
            raise ValueError("turn_timeout must be positive")
        credit_limit = self.options.max_reported_credits
        if credit_limit is not None:
            try:
                valid_limit = (not isinstance(credit_limit, bool) and isinstance(credit_limit, (int, float)) and
                               math.isfinite(credit_limit) and credit_limit > 0 and self.options.agent_engine == "v3")
            except OverflowError:
                valid_limit = False
            if not valid_limit:
                raise ValueError("max_reported_credits must be positive and requires Kiro V3")
        self._reported_credit_limit = Decimal(str(credit_limit)) if credit_limit is not None else None
        self._reported_credits = Decimal(0)
        self._credit_pending = False
        self._credit_unknown = False
        self._credit_ledger = self.options.reported_credit_ledger
        self._credit_scope = self.options.reported_credit_scope
        self._credit_reservation = None
        if self._credit_ledger is not None:
            if (self._reported_credit_limit is None or
                    not callable(getattr(self._credit_ledger, "begin", None)) or
                    not callable(getattr(self._credit_ledger, "complete", None))):
                raise ValueError("reported_credit_ledger requires max_reported_credits and begin/complete methods")
            _credit_scope(self._credit_scope)
        elif self._credit_scope is not None:
            raise ValueError("reported_credit_scope requires a reported_credit_ledger")
        if self.options.load_timeout_ms is not None and (isinstance(self.options.load_timeout_ms, bool) or
                                                        not isinstance(self.options.load_timeout_ms, int) or
                                                        self.options.load_timeout_ms <= 0):
            raise ValueError("load_timeout_ms must be a positive integer")
        if self.options.session_store_flush not in ("batched", "eager"):
            raise ValueError("session_store_flush must be batched or eager")
        if self.options.session_store_error_policy not in ("fail", "continue"):
            raise ValueError("session_store_error_policy must be fail or continue")
        if self.options.session_store_error_policy == "continue" and self.options.session_store is None:
            raise ValueError("session_store_error_policy=continue requires a session_store")
        timeout = self.options.session_store_flush_timeout
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                                    not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("session_store_flush_timeout must be positive")
        if not isinstance(self.options.session_store_archive, bool):
            raise ValueError("session_store_archive must be a boolean")
        if self.options.session_store_archive and (self.options.session_store is None or
                                                   self.options.agent_engine != "v3" or transport is not None):
            raise ValueError("session_store_archive requires a local V3 client and session_store")
        if not isinstance(self.options.resume_from_store, bool):
            raise ValueError("resume_from_store must be a boolean")
        if self.options.resume_from_store and (not self.options.resume or self.options.session_store is None or
                                                self.options.agent_engine != "v3" or transport is not None or
                                                self.options.fork_session):
            raise ValueError("resume_from_store requires resume, a session_store, and a local V3 client without fork_session")
        if self.options.session_store_source_directory is not None and not self.options.resume_from_store:
            raise ValueError("session_store_source_directory requires resume_from_store")
        if not isinstance(self.options.forward_subagent_text, bool):
            raise ValueError("forward_subagent_text must be a boolean")
        for name in ("on_user_input", "on_mcp_elicitation"):
            callback = getattr(self.options, name)
            if callback is not None and (not callable(callback) or self.options.agent_engine != "v3"):
                raise ValueError(f"{name} requires a callback and Kiro V3")
        max_attachment = 16777216
        if isinstance(max_attachment, bool) or not isinstance(max_attachment, int) or max_attachment <= 0:
            raise ValueError("max_resolved_attachment_bytes must be a positive integer")
        from .archive import _limits as _archive_limits
        _archive_limits(self.options.session_store_archive_max_bytes,
                        self.options.session_store_archive_max_uncompressed_bytes)
        timeout = self.options.session_store_archive_timeout
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                                    not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("session_store_archive_timeout must be positive")
        if self._permission_mode not in PERMISSION_MODES:
            raise ValueError(f"permission_mode must be one of {', '.join(m for m in PERMISSION_MODES if m)}")
        if not isinstance(self.options.kiro_shell_edit, bool) or (
            self.options.kiro_shell_edit and self.options.agent_engine != "v3"
        ):
            raise ValueError("kiro_shell_edit must be a boolean and requires Kiro V3 when enabled")
        if self.options.mcp_env_mode not in ("literal", "expand"):
            raise ValueError("mcp_env_mode must be literal or expand")
        if self.options.sdk_tool_interrupt_mode not in ("kiro", "cancel-all", "cancel-observed"):
            raise ValueError("sdk_tool_interrupt_mode must be kiro, cancel-all, or cancel-observed")
        if self.options.sdk_mcp_toggle_mode not in ("reject", "sdk-tools", "sdk-and-stdio", "sdk-all"):
            raise ValueError("sdk_mcp_toggle_mode must be reject, sdk-tools, sdk-and-stdio, or sdk-all")
        if (not isinstance(self.options.mcp_oauth, dict) or
                any(not isinstance(name, str) or not isinstance(config, McpOAuthConfig)
                    for name, config in self.options.mcp_oauth.items()) or
                len({id(config) for config in self.options.mcp_oauth.values()}) != len(self.options.mcp_oauth)):
            raise ValueError("mcp_oauth must map server names to McpOAuthConfig")
        if self.options.mcp_oauth and self.options.sdk_mcp_toggle_mode != "sdk-all":
            raise ValueError("mcp_oauth requires sdk_mcp_toggle_mode='sdk-all'")
        if self._permission_mode == "plan":
            if self.options.agent_engine != "v3":
                raise ValueError("plan permission mode requires Kiro V3")
            if self.options.agent or self._has_profile_system_prompt() or self.options.tools is not None or self._native_hooks or self._effective_kiro_permissions() is not None or self.options.kiro_resources:
                raise ValueError("plan mode cannot be combined with a custom agent profile")
        if self.options.agent and (self._has_profile_system_prompt() or self.options.tools is not None or self._native_hooks or self._effective_kiro_permissions() is not None or self.options.kiro_resources):
            raise ValueError("agent cannot be combined with a temporary SDK agent profile")
        self._validate_resources(self.options.kiro_resources)
        if any(not isinstance(name, str) or not name for name in []):
            raise ValueError("disallowed_tools must contain nonempty Kiro selectors")
        if self.options.kiro_permissions is not None:
            if self.options.agent_engine != "v3":
                raise ValueError("kiro_permissions requires Kiro V3")
            self._validate_kiro_permissions(self.options.kiro_permissions)
            if self._effective_can_use_tool() and any(rule["effect"] == "allow" for rule in self.options.kiro_permissions["rules"]):
                raise ValueError("Kiro allow rules can bypass can_use_tool")
        if self._native_hooks and self.options.agent_engine not in (None, "v2", "v3"):
            raise ValueError("Native SDK pre-tool hooks require Kiro V2 or V3")
        if self._native_hooks and os.name != "posix":
            raise ValueError("Native SDK pre-tool hooks currently require a POSIX shell")
        if self._native_hooks and self.options.agent_engine == "v3" and not self.options.enable_kiro_hooks:
            raise ValueError("Native SDK pre-tool hooks require Kiro hooks to be enabled")
        if self.options.add_dirs and self.options.agent_engine != "v3":
            raise ValueError("add_dirs requires the Kiro V3 engine")
        if self.options.require_sandbox and self.options.agent_engine != "v3":
            raise ValueError("require_sandbox requires Kiro V3")
        if self.options.enable_file_checkpointing is not None and not isinstance(self.options.enable_file_checkpointing, bool):
            raise ValueError("enable_file_checkpointing must be a boolean")
        if self.options.enable_file_checkpointing is False:
            raise ValueError("Kiro does not expose a per-session checkpoint disable option")
        if self.options.enable_file_checkpointing is True and self.options.agent_engine != "v3":
            raise ValueError("Verified file checkpointing requires Kiro V3")
        if self._configured_thinking() is not None:
            if self.options.agent_engine != "v3":
                raise ValueError("thinking requires Kiro V3")
        if self.options.continue_conversation and self.options.resume:
            raise ValueError("continue_conversation and resume are mutually exclusive")
        if self.options.session_id is not None:
            if self.options.agent_engine != "v3":
                raise ValueError("session_id requires Kiro V3")
            if self.options.resume or self.options.continue_conversation or self.options.fork_session:
                raise ValueError("session_id cannot be combined with resume, continuation or fork")
            if not isinstance(self.options.session_id, str) or not re.fullmatch(
                r"(?:sess_)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                self.options.session_id,
            ):
                raise ValueError("session_id must be a UUID, optionally prefixed with sess_")
        if self.options.continue_conversation and self.options.agent_engine not in (None, "v2", "v3"):
            raise ValueError("continue_conversation requires Kiro V2 or V3")
        if self.options.fork_session and self.options.agent_engine != "v3":
            raise ValueError("fork_session requires Kiro V3")
        if self.options.fork_session and not (self.options.resume or self.options.continue_conversation):
            raise ValueError("fork_session requires resume or continue_conversation")
        if self.options.resume_session_at is not None and not self.options.fork_session:
            raise ValueError("resume_session_at requires fork_session")
        if self.options.resume_session_at == "":
            raise ValueError("resume_session_at must not be empty")
        if (any(not isinstance(name, str) or not name or "," in name for name in [])):
            raise ValueError("allowed_tools must contain nonempty Kiro tool names without commas")
        self._validate_policy_presets()
        for event, matchers in [*self._sdk_hooks.items(), *self._native_hooks.items()]:
            for matcher in matchers:
                if not isinstance(matcher, HookMatcher):
                    raise TypeError("Hook entries must be HookMatcher instances")
                if matcher.matcher is not None and event != "FileChanged":
                    re.compile(matcher.matcher)
                if matcher.timeout is not None and (isinstance(matcher.timeout, bool) or
                                                    not isinstance(matcher.timeout, (int, float)) or
                                                    not math.isfinite(matcher.timeout) or matcher.timeout <= 0):
                    raise ValueError("Hook timeout must be positive")
        if self.options.output_format is not None:
            if self.options.output_format.get("type") != "json_schema" or not isinstance(self.options.output_format.get("schema"), dict):
                raise ValueError("output_format must be {'type': 'json_schema', 'schema': {...}}")
            jsonschema.Draft202012Validator.check_schema(self.options.output_format["schema"])
        if transport is not None:
            if self.options.sdk_tool_interrupt_mode != "kiro":
                raise ValueError("SDK tool cancellation requires a local CLI")
            for path in ([self.options.cwd] if self.options.cwd is not None else []) + list(self.options.add_dirs):
                self._remote_path(path)
            if self.options.kiro_shell_edit:
                raise ValueError("Kiro shell edits require a locally verified CLI")
            if (self._has_profile_system_prompt() or self.options.tools is not None or self.options.kiro_permissions is not None or self.options.kiro_resources or
                    self._native_hooks or self._configured_env() or self.options.agents or
                    self.options.max_buffer_size is not None or self.options.stderr is not None):
                raise ValueError("Custom transports cannot apply local CLI or agent-profile settings")
            if self.options.agent_engine != "v3" and (self.options.agent or self._configured_model() or self._configured_effort()):
                raise ValueError("Custom transports require v3 session metadata for agent, model, or effort")
            if (isinstance(self.options.mcp_servers, dict) and
                    any(isinstance(server, dict) and server.get("type") == "sdk"
                        for server in self.options.mcp_servers.values())):
                raise ValueError("SDK MCP bridges require the local subprocess transport")
            if self.options.continue_conversation and self.options.agent_engine != "v3":
                raise ValueError("Custom transport continuation requires V3 session listing")
        self._transport = transport
        if self._client_auth_snapshot[0] == "client" and transport is not None:
            raise ValueError("Client-owned Kiro authentication requires a local subprocess")
        self._connected = False
        self._disconnecting = False
        self._process = None
        self._reader = None
        self._stderr_task = None
        self._stderr = bytearray()
        self._pending = {}
        self._handler_tasks = set()
        self._lingering_handler_tasks: set[asyncio.Task] = set()
        self._inbound_requests: dict[Any, dict[str, Any]] = {}
        self._mcp_permission_records: dict[tuple[str, str], dict[str, Any]] = {}
        self._mcp_bridges: list[SdkMcpBridge] = []
        self._named_mcp_bridges: dict[str, list[SdkMcpBridge]] = {}
        self._external_mcp_bridges: list[Union[ExternalStdioMcpBridge, RemoteMcpBridge]] = []
        self._named_external_mcp_bridges: dict[str, list[Union[ExternalStdioMcpBridge, RemoteMcpBridge]]] = {}
        self._mcp_oauth_states: dict[str, tuple[Any, set[tuple[str, str, int]]]] = {}
        self._agent_mcp_bridges: dict[str, dict[str, Any]] = {}
        self._agent_inline_mcp_bridges: dict[tuple[str, str], dict[str, Any]] = {}
        self._plugin_inline_mcp_configs: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._configured_mcp_servers: dict[str, dict[str, Any]] = {}
        self._before_mcp_servers_setup: Optional[Callable[[], Awaitable[None]]] = None
        self._resolved_system_prompt: Optional[str] = None
        self._effective_effort: Optional[str] = None
        self._agent_model_snapshot: Optional[str] = None
        self._agent_mapping_snapshot: Optional[tuple[Any, ...]] = None
        self._mapped_agent_profiles: dict[str, Optional[dict[str, Any]]] = {}
        self._startup_fallback: Optional[tuple[str, str]] = None
        self._shell_edit_verified = False
        self._sdk_tool_interrupt_mode = "kiro"
        self._sdk_mcp_toggle_mode = "reject"
        self._code_intelligence_option = False
        self._code_intelligence_enabled = False
        self._sdk_tools_suspended = False
        self._pending_session_plan: Optional[tuple[str, Optional[asyncio.Task[Any]], str, bool]] = None
        self._mode_updates = 0
        self._before_initial_prompt: Optional[Callable[[str], Awaitable[None]]] = None
        self._prompt_send_guard: Optional[Callable[[], Awaitable[bool]]] = None
        self._bounded_recovery_loader: Optional[Callable[[Any, dict[str, str]], Awaitable[Any]]] = None
        self._compaction_send_guard: Optional[Callable[[], Awaitable[bool]]] = None
        self._interaction_guard: Optional[Callable[[], Awaitable[bool]]] = None
        self._before_initial_turn: Optional[Callable[[str], Awaitable[None]]] = None
        self._plugin_interpolation_task: Optional[asyncio.Task[Any]] = None
        self._policy_presets: list[str] = []
        self._mcp_servers = []
        self._owned_workflows: set[str] = set()
        self._background_task_manager: Optional[BackgroundTaskManager] = None
        self._agent_file = None
        self._agent_file_identity = None
        self._native_hook_bridge = None
        self._created_agents = []
        # Python 3.9 binds these primitives at construction.  A client may be
        # constructed before an event loop exists; connect() initializes them.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._events = None
            self._sandbox_event = None
        else:
            self._events = asyncio.Queue()
            self._sandbox_event = asyncio.Event()
        self._next_id = 0
        self._turn = None
        self._turn_prompt_warnings: tuple[str, ...] = ()
        self._turn_mirror_prompt_id: Optional[str] = None
        self._turn_model = ""
        self._initial_prompt_active = False
        self._initial_prompt_failed = False
        self._queued_after_initial = None
        self._interrupt_requested = False
        self._pending_prompt_ids: set[int] = set()
        self._turn_wire_completed = False
        self._stream_input = None
        self._input_next_task = None
        self._input_cancel_event: Optional[asyncio.Event] = None
        self._input_collecting = False
        self._stream_cleanup_task: Optional[asyncio.Task] = None
        self._message_activity: Optional[asyncio.Event] = None
        self._continuous_messages_active = False
        self._turn_started_at = None
        self._turn_finished_at = None
        self._lock = make_loop_lock()
        self._query_lock = make_loop_lock()
        self._connect_lock = make_loop_lock()
        from .loop_lock import LazyLoopLock
        self._disconnect_lock = LazyLoopLock()
        self._disconnect_tasks: set[asyncio.Task] = set()
        self._sdk_interrupt_lock = make_loop_lock()
        self.session_id = None
        self.server_info = None
        self.session_info = None
        self._session_model = self._configured_model() or ""
        self.history = []
        self._context_metadata_by_session: dict[Optional[str], dict[str, Any]] = {}
        self._available_commands_by_session: dict[str, Optional[list[dict[str, Any]]]] = {}
        self._mcp_status_tracker = McpStatusTracker()
        self._subagent_parents_by_session: dict[str, dict[str, str]] = {}
        self._subagent_types_by_session: dict[str, dict[str, tuple[str, Optional[str]]]] = {}
        self._hook_registry_revision: dict[str, int] = {}
        self._hook_snapshots_by_session: dict[str, Any] = {}
        self._native_hook_names: set[str] = set()
        self._native_hook_command: Optional[str] = None
        self._hook_listing_negotiated = False
        self._sandbox_status = None
        self._reader_error = None
        self._store_key = None
        self._mirror_writer = None
        self._pending_mirror_errors: deque[MirrorErrorMessage] = deque()
        self._last_mirror_error: Optional[MirrorErrorMessage] = None
        self._store_lock = make_loop_lock()
        self._current_mode = None
        self._mode_before_plan = None
        self._initializing = False
        self._protected_mode = None
        self._guards_armed = False
        self._transport_closed = False
        self._sandbox_required = self.options.require_sandbox or None
        self._last_rewind_fork_id: Optional[str] = None
        self._recovery_in_doubt = False
        self._restored_session_id: Optional[str] = None

    @staticmethod
    def _validate_resources(resources: list[str]) -> None:
        if not isinstance(resources, list) or any(not isinstance(item, str) or not item.startswith(("file://", "skill://")) or
                                                  item in ("file://", "skill://") for item in resources):
            raise ValueError("kiro_resources must contain file:// or skill:// URIs")

    @property
    def _sdk_hooks(self) -> dict[str, list[HookMatcher]]:
        return hook_events(self.options.hooks, HOOK_TRIGGERS, "hooks")

    def _native_option_hooks(self) -> dict[str, list[HookMatcher]]:
        return hook_events(self.options.native_hooks, NATIVE_HOOK_TRIGGERS, "native_hooks")

    def _effective_hook_failure_policy(self, event: str, matcher: HookMatcher) -> Any:
        return matcher.on_failure

    def _capture_hook_failure_snapshot(self) -> Any:
        hooks = self._sdk_hooks
        selected = []
        enabled = False
        blocking = {"UserPromptSubmit", "PreToolUse", "PostToolUse"}
        observed = {"PostToolUse", "Stop"}
        ignored_block = {"Stop"}
        for event, matchers in hooks.items():
            if not isinstance(matchers, list):
                raise TypeError("Hook matchers must be lists")
            entries = []
            for matcher in matchers:
                if not isinstance(matcher, HookMatcher) or not isinstance(matcher.hooks, list):
                    raise TypeError("Hook entries must be HookMatcher instances with hook lists")
                if matcher.on_failure not in (None, "continue", "block"):
                    raise ValueError("Hook on_failure must be continue or block")
                policy = self._effective_hook_failure_policy(event, matcher)
                if policy is not None:
                    if (event not in blocking | observed or
                            (policy == "block" and event not in blocking | ignored_block) or
                            (event == "PreToolUse" and self.options.sdk_pretool_hook_mode not in ("permission", "native")) or
                            (event == "Stop" and self.options.sdk_stop_hook_mode not in ("receive", "native")) or
                            not matcher.hooks or any(not callable(hook) for hook in matcher.hooks)):
                        raise ValueError("Hook on_failure requires a supported SDK hook and failure policy")
                    enabled = True
                entries.append((id(matcher), matcher.matcher, matcher.timeout, policy,
                                tuple(id(hook) for hook in matcher.hooks)))
            if event in blocking | observed:
                selected.append((event, tuple(entries)))
        native_groups = [("native:" + event, matchers) for event, matchers in self._native_option_hooks().items()]
        for name, matchers in native_groups:
            entries = []
            for matcher in matchers:
                if not isinstance(matcher, HookMatcher) or not isinstance(matcher.hooks, list):
                    raise TypeError("Native hook entries must contain hook lists")
                policy = matcher.on_failure
                if policy is not None:
                    if (name not in ("native:PreToolUse", "native:Stop") or
                            policy not in ("block", "continue") or
                            not matcher.hooks or any(not callable(hook) for hook in matcher.hooks)):
                        raise ValueError("Native hook on_failure requires PreToolUse")
                    enabled = True
                entries.append((id(matcher), matcher.matcher, matcher.timeout, policy,
                                tuple(id(hook) for hook in matcher.hooks)))
            if name in ("native:PreToolUse", "native:Stop"):
                selected.append((name, tuple(entries)))
        native_pretool = list(self._native_option_hooks().get("PreToolUse") or [])
        if self.options.sdk_pretool_hook_mode == "native":
            native_pretool.extend(hooks.get("PreToolUse", []))
        if any(matcher.on_failure == "continue" for matcher in native_pretool):
            if not _bounded_hook_group(native_pretool):
                raise ValueError("Native PreToolUse on_failure=continue requires bounded hook timeouts")
        native_stop = [(matcher, False) for matcher in (self._native_option_hooks().get("Stop") or [])]
        if self.options.sdk_stop_hook_mode == "native":
            native_stop.extend((matcher, True) for matcher in hooks.get("Stop", []))
        if any((self._effective_hook_failure_policy("Stop", matcher) if is_sdk else matcher.on_failure) is not None
               for matcher, is_sdk in native_stop):
            if not _bounded_native_stop_group([matcher for matcher, _ in native_stop]):
                raise ValueError("Native Stop on_failure requires bounded hook timeouts")
        return ('strict', tuple(sorted(selected))) if enabled else None

    def _validate_hook_failure_snapshot(self) -> None:
        self._hook_failure_runner.check()
        if self._capture_hook_failure_snapshot() != self._hook_failure_snapshot:
            raise ValueError("Create a new client after changing hook on_failure configuration")

    async def _invoke_failure_aware_hook(self, hook: Callable, payload: dict[str, Any],
                                         tool_call_id: Optional[str], timeout: float, *,
                                         _sdk_control_operation: bool = False) -> Any:
        token = _SDK_HOOK_CONTEXT.set(None if _sdk_control_operation else self)
        try:
            return await self._hook_failure_runner.invoke(hook, copy.deepcopy(payload), timeout, tool_call_id)
        finally:
            _SDK_HOOK_CONTEXT.reset(token)

    def _native_failure_guard(self) -> Callable[[], Awaitable[bool]]:
        guard = self._interaction_guard
        turn, session_id = self._turn, self.session_id
        def current() -> bool:
            return (self._interaction_guard is guard and self._turn is turn and self.session_id == session_id and
                    (turn is None or not turn.done()) and self._connected and not self._disconnecting and
                    not self._interrupt_requested and self._reader_error is None and
                    True and True and True)
        async def check() -> bool:
            self._validate_unsupported_limits()
            if not current():
                raise ProtocolError("Native hook authorization changed")
            if guard is None:
                if self._require_native_failure_authorization:
                    raise ProtocolError("Native hook authorization is unavailable")
                return True
            if await guard() is not True or not current():
                raise ProtocolError("Native hook authorization changed")
            self._validate_unsupported_limits()
            return True
        return check

    def _pretool_hook_signature(self) -> tuple[Any, ...]:
        matchers = self._sdk_hooks.get("PreToolUse", [])
        if not isinstance(matchers, list) or any(not isinstance(item, HookMatcher) for item in matchers):
            raise TypeError("PreToolUse hooks must be a list of HookMatcher instances")
        return tuple((id(item), item.matcher, item.timeout, item.on_failure, tuple(id(hook) for hook in item.hooks))
                     for item in matchers)

    def _precompact_hook_signature(self) -> tuple[Any, ...]:
        matchers = []
        if (not isinstance(matchers, list) or
                any(not isinstance(item, HookMatcher) or not isinstance(item.hooks, list) or
                    any(not callable(hook) for hook in item.hooks) for item in matchers)):
            raise TypeError("PreCompact hooks must be lists of callable HookMatcher hooks")
        return tuple((id(item), item.matcher, item.timeout, tuple(id(hook) for hook in item.hooks))
                     for item in matchers)

    def _notification_hook_signature(self) -> tuple[Any, ...]:
        matchers = []
        if (not isinstance(matchers, list) or
                any(not isinstance(item, HookMatcher) or not isinstance(item.hooks, list) or
                    any(not callable(hook) for hook in item.hooks) for item in matchers)):
            raise TypeError("Notification hooks must be lists of callable HookMatcher hooks")
        return tuple((id(item), item.matcher, item.timeout, tuple(id(hook) for hook in item.hooks))
                     for item in matchers)

    def _stop_hook_signature(self) -> tuple[Any, ...]:
        matchers = self._sdk_hooks.get("Stop", [])
        if (not isinstance(matchers, list) or
                any(not isinstance(item, HookMatcher) or not isinstance(item.hooks, list) or
                    any(not callable(hook) for hook in item.hooks) for item in matchers)):
            raise TypeError("Stop hooks must be lists of callable HookMatcher hooks")
        return tuple((id(item), item.matcher, item.timeout, item.on_failure, tuple(id(hook) for hook in item.hooks))
                     for item in matchers)

    def _record_native_stop_block(self, payload: dict[str, Any]) -> None:
        if self._is_active_main_native_stop(payload):
            self._native_stop_valid_blocks += 1

    def _is_active_main_native_stop(self, payload: dict[str, Any]) -> bool:
        return bool(self._turn is not None and not self._turn.done() and
                    not self._interrupt_requested and self.session_id and
                    payload.get("session_id") == self.session_id and not self._notification_is_subagent(payload) and
                    not any(key in payload for key in ("agent_id", "agentSubtaskId")))

    async def _with_observed_native_stop_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        result.pop("kiro_observed_continued", None)
        main_stop = self._is_active_main_native_stop(payload)
        if main_stop and "continued" not in payload:
            result["continued"] = self._native_stop_valid_blocks > 0
            result["kiro_observed_continued"] = True
        return result

    def _adapt_native_sdk_stop(self, callback: Any, timeout: Optional[float], policy: Any = None) -> Any:
        class WarningFailure(Exception):
            pass
        class GuardFailure(Exception):
            pass
        async def adapted(payload: dict[str, Any], tool_id: Optional[str], context: dict[str, Any]) -> Any:
            turn = self._turn
            if (turn is None or turn.done() or self._interrupt_requested or not isinstance(payload.get("session_id"), str) or
                    payload["session_id"] != self.session_id or self._notification_is_subagent(payload) or
                    any(key in payload for key in ("agent_id", "agentSubtaskId"))):
                return {}
            async with self._native_stop_lock:
                if (turn is not self._turn or turn.done() or
                        self._native_stop_blocks >= self._sdk_stop_limit_snapshot):
                    return {}
                event = await self._with_observed_native_stop_input(payload)
                if turn is not self._turn or not self._is_active_main_native_stop(payload):
                    return {}
                event["hook_event_name"] = "Stop"
                event.setdefault("cwd", self._working_directory())
                event["continued"] = self._native_stop_valid_blocks > 0
                event["kiro_observed_continued"] = True
                task = None
                counted = False
                failure_guard = self._native_failure_guard() if policy is not None else None
                async def check_guard() -> None:
                    if failure_guard is not None:
                        async def invoke(_payload: Any, _id: Any, _context: Any) -> bool:
                            return await failure_guard()
                        try:
                            if await self._hook_failure_runner.invoke(
                                    invoke, {}, 1 if self._native_stop_multi else 5) is not True:
                                raise ValueError("Native Stop authorization changed")
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            raise GuardFailure("Native Stop authorization changed") from None
                try:
                    self._validate_unsupported_limits()
                    if policy is None:
                        task = asyncio.ensure_future(callback(event, None, {}))
                        def consume(completed: asyncio.Future) -> None:
                            if not completed.cancelled():
                                completed.exception()
                        task.add_done_callback(consume)
                        done, _ = await asyncio.wait((task,), timeout=timeout or 60)
                        if not done:
                            raise asyncio.TimeoutError("Native Stop hook timed out")
                        if task.cancelled():
                            raise ProtocolError("Native Stop hook was cancelled")
                        value = task.result()
                    else:
                        value = await self._invoke_failure_aware_hook(callback, event, None, timeout or 60)
                    self._validate_unsupported_limits()
                    if policy is not None:
                        await check_guard()
                    if turn is not self._turn or turn.done() or self._interrupt_requested:
                        return {}
                    if policy is not None and isinstance(value, dict):
                        value = {key: item for key, item in value.items() if key in ("continue", "warning")}
                    try:
                        output = parse_hook_output("Stop", value, {"continue", "warning"})
                    except ProtocolError as exc:
                        raise ValueError("Unsupported native Stop hook output") from exc
                    pending_warning = None
                    if policy is not None and "warning" in output:
                        validated_warnings: list[str] = []
                        _append_hook_warning(validated_warnings, "Stop", output["warning"])
                        if validated_warnings:
                            pending_warning = validated_warnings[0]
                    if policy is None and "warning" in output:
                        await self._deliver_native_stop_warning(
                            payload, output["warning"], timeout=1 if self._native_stop_multi else None)
                    feedback = output.get("continue") or ""
                    if pending_warning is not None:
                        try:
                            await check_guard()
                            await self._deliver_native_stop_warning(
                                payload, pending_warning, timeout=1 if self._native_stop_multi else None)
                            await check_guard()
                        except Exception:
                            raise WarningFailure("Native Stop warning failed") from None
                    if "continue" in output:
                        self._native_stop_blocks += 1
                        counted = True
                        return {"continue": feedback or "Continue as requested by the agentStop hook"}
                    return {}
                except asyncio.CancelledError:
                    if turn is not self._turn or turn.done() or self._interrupt_requested or self._disconnecting:
                        return {}
                    if policy is not None:
                        return {}
                    if not counted:
                        self._native_stop_blocks += 1
                    self._events.put_nowait(ProtocolError("Native Stop hook failed"))
                    raise
                except Exception as exc:
                    if policy is not None and not isinstance(exc, (WarningFailure, GuardFailure)):
                        self._validate_unsupported_limits()
                        if turn is not self._turn or turn.done() or self._interrupt_requested or self._disconnecting:
                            return {}
                        await check_guard()
                        return {}
                    if (not counted and turn is self._turn and not turn.done() and
                            not self._interrupt_requested and not self._disconnecting):
                        self._native_stop_blocks += 1
                        error = ProtocolError("Native Stop hook failed")
                        error.__cause__ = exc
                        self._events.put_nowait(error)
                    raise
                finally:
                    if task is not None and not task.done():
                        task.cancel()
        setattr(adapted, "_kiro_native_stop_adapter", True)
        if policy is not None:
            setattr(adapted, "_kiro_native_stop_failure_managed", True)
        return adapted

    def _adapt_native_sdk_pretool(self, callback: Any) -> Any:
        async def adapted(payload: dict[str, Any], tool_id: Optional[str], context: dict[str, Any]) -> Any:
            event = self._with_reported_tool_identity(payload)
            event["hook_event_name"] = "PreToolUse"
            event.setdefault("session_id", self.session_id or "")
            event.setdefault("cwd", self._working_directory())
            if isinstance(tool_id, str) and tool_id:
                event.setdefault("tool_call_id", tool_id)
            return await callback(event, tool_id, context)
        return adapted

    def _remember_subagent_type(self, session_id: Any, agent_id: Any, tool_id: Any, update: Any) -> None:
        if (not self.session_id or session_id != self.session_id or not _valid_reported_agent_id(agent_id) or
                not _valid_reported_agent_id(tool_id) or not isinstance(update, dict)):
            return
        raw = update.get("rawInput")
        if not isinstance(raw, dict) or "name" not in raw:
            return
        name = raw["name"] if _valid_reported_agent_id(raw["name"]) else None
        types = self._subagent_types_by_session.setdefault(session_id, {})
        previous = types.get(agent_id)
        if previous is None:
            if len(types) < _MAX_REPORTED_SUBAGENT_TYPES:
                types[agent_id] = (tool_id, name)
        elif previous[1] is not None and previous != (tool_id, name):
            types[agent_id] = (previous[0], None)

    def _reported_subagent_type(self, session_id: Any, agent_id: Any) -> Optional[str]:
        if not self.session_id or session_id != self.session_id or not _valid_reported_agent_id(agent_id):
            return None
        entry = self._subagent_types_by_session.get(session_id, {}).get(agent_id)
        return entry[1] if entry is not None else None

    def _with_reported_tool_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = _normalized_tool_hook_identity(payload)
        event.pop("kiro_observed_agent_type", None)
        observed = (self._reported_subagent_type(payload.get("session_id"), event.get("agent_id"))
                    if self._turn is not None and not self._turn.done() and not self._interrupt_requested else None)
        if "agent_type" in event:
            direct = event["agent_type"]
            if not _valid_reported_agent_id(direct) or (observed is not None and direct != observed):
                event.pop("agent_type", None)
        elif observed is not None:
            event["agent_type"] = observed
            event["kiro_observed_agent_type"] = True
        return event

    def _validate_pretool_hook_state(self) -> None:
        if self.options.sdk_pretool_hook_mode != self._sdk_pretool_mode:
            raise ValueError("Create a new client after changing sdk_pretool_hook_mode")
        if self._sdk_pretool_mode == "native" and self._pretool_hook_signature() != self._sdk_pretool_snapshot:
            raise ValueError("Create a new client after changing native SDK PreToolUse hooks")

    def _compile_tool_permissions(self) -> Optional[dict[str, Any]]:
        configs = (
            
        )
        for label, mode, mapping, _, _ in configs:
            if mode not in ("native", "v3-permissions") or not isinstance(mapping, dict):
                raise ValueError(f"Invalid {label} mapping mode or map")
            if mode == "native" and mapping:
                raise ValueError(f"{label} permission mapping requires v3-permissions mode")
        if all(config[1] == "native" for config in configs):
            return None
        if (self.options.agent_engine != "v3" or self.options.agent or
                self._permission_mode in ("plan", "trustAll") or
                self.options.kiro_policy_presets or
                not isinstance(self.options.tools, list) or len(self.options.tools) > 128 or
                any(not isinstance(tool, str) or not tool or "\0" in tool or len(tool) > 1024
                    for tool in self.options.tools)):
            raise ValueError("Mapped tools require V3, explicit tools and no conflicting permission controls")
        combined = copy.deepcopy(self.options.kiro_permissions or {"rules": []})
        self._validate_kiro_permissions(combined)
        count = 0
        for label, mode, mapping, names, effect in configs:
            if mode == "native":
                continue
            if (not isinstance(names, list) or not names or len(names) > 128 or
                    any(not isinstance(name, str) or not name.strip() or name != name.strip() or
                        len(name) > 512 or "\0" in name for name in names) or len(set(names)) != len(names)):
                raise ValueError(f"Mapped {label} requires distinct bounded tool names")
            if set(mapping) != set(names):
                raise ValueError(f"Each {label} name requires exactly one explicit permission mapping")
            for name in names:
                rules = mapping[name]
                if not isinstance(rules, list) or not rules:
                    raise ValueError(f"Each {label} name must map to Kiro permission rules")
                self._validate_kiro_permissions({"rules": rules})
                for rule in rules:
                    if rule["effect"] != effect:
                        raise ValueError(f"Mapped {label} must contain only {effect} rules")
                    if any(not value or "\0" in value or len(value) > 4096
                           for key in ("match", "exclude") for value in rule.get(key, [])):
                        raise ValueError("Invalid mapped permission resource")
                count += len(rules)
                if count > 128:
                    raise ValueError("Too many mapped permission rules")
                combined["rules"].extend(copy.deepcopy(rules))
        if len(json.dumps(combined, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
            raise ValueError("Mapped permissions exceed the size limit")
        return combined

    def _effective_kiro_permissions(self) -> Optional[dict[str, Any]]:
        return (self.options.kiro_permissions)

    def _has_profile_system_prompt(self) -> bool:
        return (self.options.system_prompt is not None)

    def _configured_thinking(self) -> Optional[bool]:
        value = self.options.thinking
        if value is not None and type(value) is not bool:
            raise ValueError("thinking must be True, False or None")
        return value

    def _configured_model(self) -> Any:
        return self._model_option

    def _configured_effort(self) -> Any:
        return self.options.effort

    def _configured_env(self) -> dict[str, str]:
        environment = self.options.env
        if self._connection_option_snapshot[-1] == "minimal":
            from .process_env import blank_non_allowlisted_environment
            environment = {**blank_non_allowlisted_environment(), **environment}
        if self._client_auth_snapshot[0] == "client":
            return _blank_client_api_key(environment)
        return environment

    def _effective_can_use_tool(self) -> Any:
        return self.options.can_use_tool

    def _client_auth_scope(self) -> tuple[Any, ...]:
        method, provider, timeout = (self.options.kiro_auth_method, self.options.kiro_access_token_provider,
                                     self.options.kiro_access_token_timeout)
        idle_mode, idle_authorizer = self.options.kiro_client_auth_idle_mode, self.options.kiro_idle_token_authorizer
        if method == "cli":
            if (provider is not None or type(timeout) not in (int, float) or timeout != 10 or
                    idle_mode != "reject" or idle_authorizer is not None):
                raise ValueError("Kiro client authentication requires explicit client mode")
        elif method == "client":
            try:
                valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 60
            except OverflowError:
                valid_timeout = False
            if (self.options.agent_engine != "v3" or not callable(provider) or not valid_timeout or
                    self.options.stderr is not None or type(self.options.env) is not dict or
                    self.options.env.get("KIRO_API_KEY") not in (None, "") or
                    (os.name == "nt" and any(
                        isinstance(key, str) and key.upper() == "KIRO_API_KEY" and value not in (None, "")
                        for key, value in self.options.env.items())) or
                    idle_mode not in ("reject", "authorized") or
                    (idle_mode == "reject" and idle_authorizer is not None) or
                    (idle_mode == "authorized" and not callable(idle_authorizer))):
                raise ValueError("Client-owned Kiro authentication requires a local V3 token provider")
        else:
            raise ValueError("Invalid Kiro authentication method")
        return (method, provider, timeout, idle_mode, idle_authorizer)

    def _validate_native_hook_bridge(self, *, require_trust: bool = False) -> None:
        mode = self.options.native_hook_bridge_mode
        if mode not in ("reject", "same-user", "runtime-acp"):
            raise ValueError("native_hook_bridge_mode must be reject, same-user, or runtime-acp")
        if mode != self._native_hook_bridge_mode:
            raise ValueError("Create a new client after changing native_hook_bridge_mode")
        if self.options.native_hook_runtime_path != self._native_hook_runtime_path:
            raise ValueError("Create a new client after changing native_hook_runtime_path")
        if (self.options.native_hook_runtime_path is not None and mode != "runtime-acp" and
                self.options.runtime_compat_mode != "v3"):
            raise ValueError("native_hook_runtime_path requires runtime-acp")
        if mode == "runtime-acp":
            if os.name != "posix" or self.options.agent_engine != "v3" or not self.options.enable_kiro_hooks:
                raise ValueError("Native ACP hooks require POSIX and enabled Kiro V3 hooks")
            if getattr(self, "_transport", None) is not None:
                raise ValueError("Native ACP hooks require a local Kiro CLI")
            if self.options.native_hook_runtime_path is not None and not isinstance(
                    self.options.native_hook_runtime_path, (str, Path)):
                raise ValueError("native_hook_runtime_path must be a path")
        if require_trust and self._native_hooks and mode == "reject":
            raise ValueError(
                "Native hook bridge disabled: select native_hook_bridge_mode='runtime-acp' for the "
                "verified Kiro V3 adapter, or 'same-user' to trust code running as the Kiro user. "
                "The same-user mode does not isolate host callbacks from model-executed code.")

    def _validate_unsupported_limits(self) -> None:
        self._validate_native_hook_bridge()
        if inline_sdk_servers(self.options)[0] != self._agent_inline_sdk_snapshot:
            raise ValueError("Create a new client after changing inline SDK agent servers")
        if self._runtime_policy() != self._runtime_policy_snapshot:
            raise ValueError("Create a new client after changing runtime_compat_mode, max_turns, verbatim_prompts, "
                             "strict_mcp_config, resume_drops_turn, runtime fallback, runtime_usage_mode or runtime agent controls")
        if self.options.runtime_usage_mode not in ("none", "reported"):
            raise ValueError("runtime_usage_mode must be none or reported")
        if self.options.runtime_usage_mode == "reported" and self.options.runtime_compat_mode != "v3":
            raise ValueError("Reported runtime usage requires verified Kiro V3")
        if self.options.runtime_compat_mode not in ("reject", "v3"):
            raise ValueError("runtime_compat_mode must be reject or v3")
        if self.options.runtime_compat_mode == "v3":
            if (os.name != "posix" or self.options.agent_engine != "v3" or
                    getattr(self, "_transport", None) is not None or
                    self._native_hook_bridge_mode == "same-user"):
                raise ValueError("Runtime policies require local POSIX Kiro V3 and reject or runtime-acp hooks")
            if self.options.native_hook_runtime_path is not None and not isinstance(
                    self.options.native_hook_runtime_path, (str, Path)):
                raise ValueError("native_hook_runtime_path must be a path")
        self._validate_hook_failure_snapshot()
        auth_scope = self._client_auth_scope()
        if (auth_scope[0] != self._client_auth_snapshot[0] or
                auth_scope[1] is not self._client_auth_snapshot[1] or auth_scope[2] != self._client_auth_snapshot[2] or
                auth_scope[3] != self._client_auth_snapshot[3] or auth_scope[4] is not self._client_auth_snapshot[4]):
            raise ValueError("Create a new client after changing Kiro authentication")
        self._compile_tool_permissions()
        bounded_restore = self.options.resume_from_store_bounded_read
        if type(bounded_restore) is not bool:
            raise ValueError("resume_from_store_bounded_read must be a boolean")
        if bounded_restore:
            from .session_store import InMemorySessionStore
            from .sqlite_store import SQLiteSessionStore
            if not self.options.resume_from_store or type(self.options.session_store) not in (
                    InMemorySessionStore, SQLiteSessionStore):
                raise ValueError("resume_from_store_bounded_read requires resume_from_store and a built-in store")
        snapshot = getattr(self, "_resume_from_store_bounded_snapshot", None)
        if snapshot is not None and (bounded_restore != snapshot[0] or
                                     (bounded_restore and self.options.session_store is not snapshot[1])):
            raise ValueError("Create a new client after changing bounded mirror recovery settings")
        receive_mode = self.options.receive_messages_mode
        if receive_mode not in ("current", "continuous"):
            raise ValueError("receive_messages_mode must be current or continuous")
        if getattr(self, "_receive_messages_mode", receive_mode) != receive_mode:
            raise ValueError("Create a new client after changing receive_messages_mode")
        input_mode = self.options.stream_input_mode
        input_count = self.options.stream_input_buffer_max_messages
        input_bytes = self.options.stream_input_buffer_max_bytes
        input_timeout = self.options.stream_input_buffer_timeout
        if input_mode not in ("on-receive", "buffered", "prefetch", "prefetch-first"):
            raise ValueError("stream_input_mode must be on-receive, buffered, prefetch or prefetch-first")
        if type(input_count) is not int or not 1 <= input_count <= 1024:
            raise ValueError("stream_input_buffer_max_messages must be an integer from 1 to 1024")
        if type(input_bytes) is not int or not 1 <= input_bytes <= 64 * 1024 * 1024:
            raise ValueError("stream_input_buffer_max_bytes must be an integer from 1 to 67108864")
        if input_timeout is not None:
            try:
                valid_timeout = (not isinstance(input_timeout, bool) and isinstance(input_timeout, (int, float))
                                 and math.isfinite(input_timeout) and input_timeout > 0)
            except OverflowError:
                valid_timeout = False
            if not valid_timeout:
                raise ValueError("stream_input_buffer_timeout must be a positive finite number or None")
        input_policy = (input_mode, input_count, input_bytes, input_timeout)
        if getattr(self, "_stream_input_policy", input_policy) != input_policy:
            raise ValueError("Create a new client after changing streaming input settings")
        native_message_callback = self.options.on_native_hook_system_message
        if native_message_callback is not None and not callable(native_message_callback):
            raise ValueError("on_native_hook_system_message must be callable")
        if (hasattr(self, "_native_hook_system_message_callback") and
                native_message_callback is not self._native_hook_system_message_callback):
            raise ValueError("Create a new client after changing the native hook message callback")
        input_transforms, input_timeout = self.options.sdk_mcp_input_transforms, self.options.sdk_mcp_input_timeout
        try:
            finite_input_timeout = (not isinstance(input_timeout, bool) and isinstance(input_timeout, (int, float)) and
                                    math.isfinite(input_timeout) and input_timeout > 0)
        except OverflowError:
            finite_input_timeout = False
        if (not isinstance(input_transforms, dict) or len(input_transforms) > 128 or
                any(not isinstance(name, str) or not name or len(name) > 256 or "\0" in name or
                    not callable(callback) for name, callback in input_transforms.items()) or
                not finite_input_timeout):
            raise ValueError("Invalid SDK MCP input transforms or timeout")
        input_snapshot = getattr(self, "_sdk_mcp_input_snapshot", None)
        if (input_snapshot is not None and
                (set(input_transforms) != set(input_snapshot[0]) or
                 any(callback is not input_snapshot[0][name] for name, callback in input_transforms.items()) or
                 input_timeout != input_snapshot[1])):
            raise ValueError("Create a new client after changing SDK MCP input transforms")
        stop_mode, stop_limit = self.options.sdk_stop_hook_mode, self.options.max_native_stop_blocks
        if (stop_mode not in ("receive", "native") or
                isinstance(stop_limit, bool) or not isinstance(stop_limit, int) or not 1 <= stop_limit <= 16 or
                (stop_mode == "native" and self.options.agent_engine != "v3")):
            raise ValueError("Invalid native SDK Stop hook configuration")
        if (hasattr(self, "_sdk_stop_mode") and
                (stop_mode != self._sdk_stop_mode or
                 (stop_mode == "native" and self._stop_hook_signature() != self._sdk_stop_snapshot))):
            raise ValueError("Create a new client after changing Stop hooks")
        if hasattr(self, "_sdk_stop_limit_snapshot") and stop_limit != self._sdk_stop_limit_snapshot:
            raise ValueError("Create a new client after changing native Stop hook settings")
        if (hasattr(self, "_notification_mode_snapshot") and
                (self._notification_hook_signature() != self._notification_hook_snapshot)):
            raise ValueError("Create a new client after changing Notification hooks")
        if (hasattr(self, "_precompact_mode_snapshot") and
                (self._precompact_hook_signature() != self._precompact_hook_snapshot)):
            raise ValueError("Create a new client after changing PreCompact hooks")
        transforms = self.options.sdk_mcp_output_transforms
        timeout = self.options.sdk_mcp_output_timeout
        try:
            finite_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and
                              math.isfinite(timeout) and timeout > 0)
        except OverflowError:
            finite_timeout = False
        if (not isinstance(transforms, dict) or len(transforms) > 128 or
                any(not isinstance(name, str) or not name or len(name) > 256 or "\0" in name or
                    not callable(callback) for name, callback in transforms.items()) or
                not finite_timeout):
            raise ValueError("Invalid SDK MCP output transforms or timeout")
        snapshot = getattr(self, "_sdk_mcp_output_snapshot", None)
        if (snapshot is not None and
                (set(transforms) != set(snapshot[0]) or
                 any(callback is not snapshot[0][name] for name, callback in transforms.items()) or
                 timeout != snapshot[1])):
            raise ValueError("Create a new client after changing SDK MCP output transforms")
        # Reading the mode rejects a changed options.permission_mode; set_permission_mode() is the supported path.
        if self._permission_mode not in PERMISSION_MODES:
            raise ValueError(f"permission_mode must be one of {', '.join(m for m in PERMISSION_MODES if m)}")
        prior_budget = getattr(self, "_task_budget_snapshot", None)
        if prior_budget is not None and (
                'reject' != prior_budget[0] or None != prior_budget[1] or
                None is not prior_budget[2] or False != prior_budget[3]):
            raise ValueError("Create a new client after changing advisory task_budget settings")
        allow_timeout = 5.0
        try:
            valid_allow_timeout = (not isinstance(allow_timeout, bool) and isinstance(allow_timeout, (int, float)) and
                                   math.isfinite(allow_timeout) and 0 < allow_timeout <= 60)
        except OverflowError:
            valid_allow_timeout = False
        if (not valid_allow_timeout):
            raise ValueError("Session allow rules require session deny rules and an explicit matcher without a consent mapper")
        if (getattr(self, "_permission_update_allow_timeout_snapshot", allow_timeout) != allow_timeout):
            raise ValueError("Create a new client after changing session allow-rule support")
        edit_roots = []
        if (type(edit_roots) is not list or len(edit_roots) > 16 or
                any(type(root) is not str or not self._valid_posix_subtree_path(root) or
                    root == "/" or str(PurePosixPath(root)) != root for root in edit_roots) or
                len(set(edit_roots)) != len(edit_roots) or
                (edit_roots)):
            raise ValueError("Additional edit roots require unique absolute paths and session workspace edit and directory modes")
        if getattr(self, "_accept_edits_additional_roots_snapshot", edit_roots) != edit_roots:
            raise ValueError("Create a new client after changing additional edit roots")
        if (getattr(self, "_connected", False) and self._agent_mapping_snapshot is not None and
                self._agent_mapping_state() != self._agent_mapping_snapshot):
            raise ValueError("Reconnect after changing mapped agent tools or permissions")
        snapshot = getattr(self, "_allowed_tools_snapshot", None)
        if snapshot is not None and (
                'native' != snapshot[0] or 'native' != snapshot[8] or
                ((snapshot[0] == "v3-permissions" or snapshot[8] == "v3-permissions") and
                 ([] != snapshot[1] or {} != snapshot[2]
                  or self.options.kiro_permissions != snapshot[3] or self.options.tools != snapshot[4]
                  or self.options.agent_engine != snapshot[5] or self._permission_mode != snapshot[6]
                  or [] != snapshot[7]
                  or {} != snapshot[9]))):
            raise ValueError("Create a new client after changing mapped tool permissions")
        if self.options.sdk_pretool_hook_mode not in ("permission", "native"):
            raise ValueError("sdk_pretool_hook_mode must be permission or native")
        if (not isinstance(self.options.enable_kiro_code_intelligence, bool) or
                (self.options.enable_kiro_code_intelligence and self.options.agent_engine != "v3")):
            raise ValueError("enable_kiro_code_intelligence requires a boolean and Kiro V3 when enabled")
        if (getattr(self, "_connected", False) and
                self.options.enable_kiro_code_intelligence != self._code_intelligence_option):
            raise ValueError("Reconnect after changing enable_kiro_code_intelligence")
        self._configured_thinking()
        if self.options.resume_drops_turn is not None:
            value = self.options.resume_drops_turn
            if (self.options.runtime_compat_mode != "v3" or
                    not isinstance(value, str) or not value or len(value) > 128 or
                    any(ord(c) < 32 for c in value) or
                    not self.options.fork_session or not self.options.resume_session_at or
                    not (self.options.resume or self.options.continue_conversation)):
                raise ValueError("resume_drops_turn requires verified V3, a user message ID, "
                                 "resume, fork_session and resume_session_at")
        user = self.options.user
        if user is not None and (os.name != "posix" or not isinstance(user, str) or
                                 not user.strip() or len(user) > 256 or
                                 any(ord(char) < 32 or 127 <= ord(char) <= 159 or
                                     0xD800 <= ord(char) <= 0xDFFF for char in user)):
            raise ValueError("user must be a nonempty POSIX account name")
        if getattr(self, "_process_user", user) != user:
            raise ValueError("Create a new client after changing user")
        if type(self.options.verbatim_prompts) is not bool:
            raise ValueError("verbatim_prompts must be a boolean")
        if self.options.verbatim_prompts and self.options.runtime_compat_mode != "v3":
            raise ValueError("verbatim_prompts requires runtime_compat_mode='v3'")
        if not isinstance(self.options.strict_mcp_config, bool):
            raise ValueError("strict_mcp_config must be a boolean")
        if self.options.strict_mcp_config and self.options.runtime_compat_mode != "v3":
            raise ValueError("strict_mcp_config requires runtime_compat_mode='v3'")
        turns = self.options.max_turns
        if turns is not None and (type(turns) is not int or turns < 0 or turns > 2**31 - 1):
            raise ValueError("max_turns must be a nonnegative integer or None")
        if turns and self.options.runtime_compat_mode != "v3":
            raise ValueError("Positive max_turns requires runtime_compat_mode='v3'")
        if self.options.fallback_model_mode not in ("reject", "startup", "runtime"):
            raise ValueError("fallback_model_mode must be reject, startup or runtime")
        fallback = self.options.fallback_model
        if self.options.fallback_model_mode == "runtime":
            if (self.options.runtime_compat_mode != "v3" or not isinstance(fallback, str) or
                    not fallback.strip() or len(fallback) > 128 or any(ord(c) < 32 for c in fallback)):
                raise ValueError("Runtime fallback requires verified V3 and an explicit fallback_model ID")
        elif fallback not in (None, ""):
            if (self.options.fallback_model_mode != "startup" or self.options.agent_engine != "v3" or
                    not isinstance(fallback, str) or not fallback.strip() or "\0" in fallback or
                    not isinstance(self._configured_model(), str) or not self._configured_model().strip() or
                    "\0" in self._configured_model()):
                raise ValueError("fallback_model requires a primary model and opt-in V3 startup selection")
            if self.options.resume or self.options.continue_conversation or self.options.fork_session:
                raise ValueError("Startup fallback supports new sessions only")
            agents = self.options.agents if isinstance(self.options.agents, dict) else {}
            if any(
                    isinstance(definition, AgentDefinition) and definition.model == "inherit"
                    for definition in agents.values()):
                raise ValueError("Startup fallback cannot be combined with inherited model snapshots")
        current = _connection_option_signature(self.options)
        if current != self._connection_option_snapshot:
            names = ("session_store", "session_store_flush", "session_store_error_policy", "native_hooks",
                     "enable_file_checkpointing", "agent_engine", "env_inheritance_mode")
            changed = [name for name, old, new in zip(names, self._connection_option_snapshot, current) if old != new]
            raise ValueError(f"Create a new client after changing {', '.join(changed)}")

    async def _prepare_system_prompt(self) -> Optional[str]:
        value = self.options.system_prompt
        if value is not None and (type(value) is not str or not value.strip()):
            raise ValueError("system_prompt must be a non-empty string")
        return value

    def _validate_startup_fallback_state(self) -> None:
        configured = ((self._configured_model(), self.options.fallback_model)
                      if self.options.fallback_model_mode == "startup" and self.options.fallback_model not in (None, "") else None)
        if self._connected and configured != self._startup_fallback:
            raise ValueError("Reconnect after changing startup fallback settings")

    def _validate_policy_presets(self) -> list[str]:
        presets = self.options.kiro_policy_presets
        if (not isinstance(presets, list) or
                any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
                    for name in presets)):
            raise ValueError("kiro_policy_presets must contain valid preset IDs")
        if presets and self.options.agent_engine != "v3":
            raise ValueError("kiro_policy_presets requires Kiro V3")
        if presets and self._permission_mode == "plan":
            raise ValueError("kiro_policy_presets cannot be combined with Plan mode")
        if presets and (self._effective_can_use_tool() or self._sdk_hooks.get("PreToolUse")):
            raise ValueError("Kiro policy presets can bypass permission callbacks and SDK permission hooks")
        return list(dict.fromkeys(presets))

    @staticmethod
    def _validate_kiro_permissions(permissions: dict[str, Any]) -> None:
        capabilities = {"fs_read", "fs_write", "shell", "web_fetch", "web_search", "mcp",
                        "subagent", "skill", "power", "context", "diagnostics", "sandbox_network",
                        "all", "builtin", "filesystem"}
        if not isinstance(permissions, dict) or set(permissions) != {"rules"} or not isinstance(permissions["rules"], list):
            raise ValueError("kiro_permissions must contain a rules list")
        for rule in permissions["rules"]:
            if not isinstance(rule, dict) or set(rule) - {"capability", "effect", "match", "exclude"}:
                raise ValueError("Invalid Kiro permission rule")
            if (not isinstance(rule.get("capability"), str) or rule["capability"] not in capabilities or
                    not isinstance(rule.get("effect"), str) or rule["effect"] not in {"allow", "deny", "ask"}):
                raise ValueError("Invalid Kiro permission capability or effect")
            for key in ("match", "exclude"):
                if key in rule and (not isinstance(rule[key], list) or not rule[key] or any(not isinstance(value, str) for value in rule[key])):
                    raise ValueError(f"Permission {key} must be a list of strings")

    def _validate_kiro_consent(self, value: Any, params: dict[str, Any], choice: Any) -> dict[str, str]:
        if self.options.agent_engine != "v3":
            raise ProtocolError("Kiro consent metadata requires V3")
        if (not isinstance(choice, dict) or choice.get("kind") not in ("allow_always", "reject_always") or
                not isinstance(value, dict) or set(value) - {"scope", "resource", "workspaceRoot"}):
            raise ProtocolError("Kiro consent requires a selected persistent permission option")
        if value.get("scope") not in ("session", "workspace", "user"):
            raise ProtocolError("Kiro consent scope must be session, workspace, or user")
        resource = value.get("resource")
        if not isinstance(resource, str) or not resource.strip() or "\0" in resource:
            raise ProtocolError("Kiro consent resource must be a nonempty string")
        meta = params.get("_meta")
        kiro = meta.get("kiro") if isinstance(meta, dict) else None
        offered = kiro.get("consent") if isinstance(kiro, dict) else None
        if (not isinstance(offered, dict) or offered.get("persistableConsent") is False or
                not isinstance(offered.get("capability"), str) or not offered["capability"]):
            raise ProtocolError("Kiro did not provide persistable consent context")
        root = value.get("workspaceRoot")
        if value["scope"] == "workspace" and root is None:
            raise ProtocolError("Workspace consent requires the reported workspaceRoot")
        if root is not None:
            if (not isinstance(root, str) or "\0" in root or
                    not (PurePosixPath(root).is_absolute() or PureWindowsPath(root).is_absolute()) or
                    root != offered.get("workspaceRoot")):
                raise ProtocolError("Kiro consent workspaceRoot must match the reported absolute root")
        return dict(value)

    @staticmethod
    def _valid_posix_subtree_path(path: Any) -> bool:
        return (type(path) is str and _valid_concrete_permission_path(path) and path.startswith("/") and
                "//" not in path and "\\" not in path and
                not any(part in (".", "..") for part in path.split("/")))

    def _validate_shell_edit(self, params: dict[str, Any], original: Any, updated: Any) -> Optional[str]:
        if not isinstance(updated, dict):
            raise ProtocolError("Updated tool input must be a dictionary")
        if updated == original:
            return None
        meta = params.get("_meta")
        kiro = meta.get("kiro") if isinstance(meta, dict) else None
        consent = kiro.get("consent") if isinstance(kiro, dict) else None
        same_other_fields = (
            isinstance(original, dict) and
            ((not original and set(updated) == {"command"}) or
             ("command" in original and set(updated) == set(original) and
              isinstance(original["command"], str) and
              all(updated[key] == original[key] for key in original if key != "command")))
        )
        if (not self.options.kiro_shell_edit or self.options.agent_engine != "v3" or
                not self._shell_edit_verified or
                not isinstance(consent, dict) or consent.get("capability") != "shell" or
                not same_other_fields or not isinstance(updated.get("command"), str) or
                not updated["command"].strip() or "\0" in updated["command"]):
            raise ProtocolError("Only an explicitly enabled Kiro V3 shell command edit is supported")
        return updated["command"]

    def _selected_agent(self) -> Optional[str]:
        return self.options.agent or None

    async def connect(self, prompt: Optional[Union[str, list[Any], AsyncIterable[dict[str, Any]]]] = None) -> None:
        self._check_elicitation_reentry()
        if any(not task.done() for task in self._native_acp_dispatch_tasks):
            raise CLIConnectionError("A previous native ACP hook is still pending")
        if any(not task.done() for task in self._hook_failure_runner.tasks):
            raise CLIConnectionError("A previous hook callback is still pending")
        if self in _CLIENT_AUTH_CONTEXT.get():
            raise CLIConnectionError("Authentication and model hooks cannot control the client")
        async with self._connect_lock:
            if self._events is None:
                self._events = asyncio.Queue()
            if self._sandbox_event is None:
                self._sandbox_event = asyncio.Event()
            if self._connected:
                raise CLIConnectionError("Already connected")
            self._hook_failure_runner.reopen()
            self._require_settled_request_callbacks()
            if self._client_auth_pending or any(not task.done() for task in self._client_auth_tasks):
                raise CLIConnectionError("A previous Kiro authentication callback is still pending")
            if self._continuous_messages_active:
                raise CLIConnectionError("Previous continuous message receiver is still active")
            if self._input_collecting:
                raise CLIConnectionError("Previous streaming input collection is still active")
            if self._stream_cleanup_task is not None:
                if not self._stream_cleanup_task.done():
                    raise CLIConnectionError("Previous streaming input cleanup is still pending")
                try:
                    self._stream_cleanup_task.result()
                except (Exception, asyncio.CancelledError):
                    raise CLIConnectionError("Previous streaming input cleanup failed") from None
                self._stream_cleanup_task = None
            if self._connection_generation >= 2**63 - 1:
                raise ProtocolError("Connection generation limit reached")
            self._message_activity = asyncio.Event()
            self._connection_generation += 1
            if not self._client_auth_ancestors:
                self._client_auth_request_times.clear()
            if self._transport is None and self.options.cwd is not None and not Path(self.options.cwd).is_dir():
                raise CLIConnectionError(f"Working directory does not exist: {self.options.cwd}")
            if self.options.agents is not None and not isinstance(self.options.agents, dict):
                raise TypeError("agents must be a dictionary or None")
            try:
                self._validate_unsupported_limits()
                self._validate_pretool_hook_state()
                if self.options.kiro_shell_edit:
                    if self._transport is not None or self.options.agent_engine != "v3":
                        raise ValueError("Kiro shell edits require a local V3 CLI")
                    from .store_fork import _require_tested_cli
                    try:
                        await _require_tested_cli(self._cli_path, str(self.options.cwd or os.getcwd()),
                                                  self._configured_env(), self.options.startup_timeout,
                                                  operation="Shell input editing")
                    except SessionArchiveError as exc:
                        raise ProtocolError(str(exc)) from exc
                    self._shell_edit_verified = True
                self._resolved_system_prompt = await self._prepare_system_prompt()
                # Portable plugins and native adapters may add hooks after construction.
                # Reject before staging profiles, opening brokers or starting the CLI.
                self._validate_native_hook_bridge(require_trust=True)
                self._policy_presets = self._validate_policy_presets()
                self._effective_effort = self._resolve_agent_effort()
                self._agent_model_snapshot = self._resolve_inherited_model()
                self._startup_fallback = ((self._configured_model(), self.options.fallback_model)
                                          if self.options.fallback_model_mode == "startup" and
                                          self.options.fallback_model not in (None, "") else None)
                if self.options.mcp_env_mode not in ("literal", "expand"):
                    raise ValueError("mcp_env_mode must be literal or expand")
                if self.options.sdk_tool_interrupt_mode not in ("kiro", "cancel-all", "cancel-observed"):
                    raise ValueError("sdk_tool_interrupt_mode must be kiro, cancel-all, or cancel-observed")
                if self.options.sdk_mcp_toggle_mode not in ("reject", "sdk-tools", "sdk-and-stdio", "sdk-all"):
                    raise ValueError("sdk_mcp_toggle_mode must be reject, sdk-tools, sdk-and-stdio, or sdk-all")
                if self.options.sdk_tool_interrupt_mode != "kiro" and self._transport is not None:
                    raise ValueError("SDK tool cancellation requires a local CLI")
                if self.options.sdk_mcp_toggle_mode != "reject" and self._transport is not None:
                    raise ValueError("SDK MCP toggling requires a local CLI")
                self._sdk_tool_interrupt_mode = self.options.sdk_tool_interrupt_mode
                self._sdk_mcp_toggle_mode = self.options.sdk_mcp_toggle_mode
                self._code_intelligence_option = self.options.enable_kiro_code_intelligence
                self._code_intelligence_enabled = (
                    self._code_intelligence_option or False)
                self._configured_mcp_servers = load_mcp_config(
                    self.options.mcp_servers, self.options.cwd, self._configured_env(),
                    expand_programmatic=self.options.mcp_env_mode == "expand",
                    local_process=self._transport is None)
                self._plugin_inline_mcp_configs.clear()
                for key, entry in self._agent_inline_sdk_configs.items():
                    if (key in self._plugin_inline_mcp_configs or key in self._configured_mcp_servers or
                            entry[1] in self._configured_mcp_servers):
                        raise ValueError("Inline SDK agent server conflicts with a configured server name")
                    self._plugin_inline_mcp_configs[key] = entry
                if self._before_mcp_servers_setup is not None:
                    await self._before_mcp_servers_setup()
                for name in self.options.sdk_mcp_output_transforms:
                    server = self._mcp_config_for_name(name)
                    kind = server.get("type", "stdio")
                    if (not server or
                            (kind != "sdk" and not (kind == "stdio" and
                             self._sdk_mcp_toggle_mode in ("sdk-and-stdio", "sdk-all")) and not
                             (kind in ("http", "sse") and self._sdk_mcp_toggle_mode == "sdk-all"))):
                        raise ValueError("MCP output transforms require an SDK or explicitly mediated server")
                for name in self.options.sdk_mcp_input_transforms:
                    server = self._mcp_config_for_name(name)
                    kind = server.get("type", "stdio")
                    if (not server or
                            (kind != "sdk" and not (kind == "stdio" and
                             self._sdk_mcp_toggle_mode in ("sdk-and-stdio", "sdk-all")) and not
                             (kind in ("http", "sse") and self._sdk_mcp_toggle_mode == "sdk-all"))):
                        raise ValueError("MCP input transforms require an SDK or explicitly mediated server")
                if (not isinstance(self.options.mcp_oauth, dict) or
                        any(not isinstance(name, str) or not isinstance(auth, McpOAuthConfig) or
                            self._mcp_config_for_name(name).get("type") not in ("http", "sse")
                            for name, auth in self.options.mcp_oauth.items()) or
                        len({id(auth) for auth in self.options.mcp_oauth.values()}) != len(self.options.mcp_oauth) or
                        (self.options.mcp_oauth and self._sdk_mcp_toggle_mode != "sdk-all")):
                    raise ValueError("mcp_oauth requires explicitly configured HTTP/SSE servers and sdk-all")
                if self._sdk_tool_interrupt_mode == "cancel-all" and any(
                    server.get("type") == "sdk" and server.get("instance") is not None
                    for server in self._configured_mcp_servers.values()
                ):
                    raise ValueError("SDK tool cancellation does not support MCP Server instances")
                if self._transport is not None and any(
                    server.get("type") == "sdk" for server in self._configured_mcp_servers.values()
                ):
                    raise ValueError("SDK MCP bridges require the local subprocess transport")
                await self._prepare_agent_mcp_bridges()
                self._validate_unsupported_limits()
                self._validate_unsupported_limits()
                self._install_agents()
                await self._connect_locked(prompt)
            except BaseException:
                if ((self._native_hook_bridge_mode == "runtime-acp" or self.options.runtime_compat_mode == "v3")
                        and self._process is None):
                    if self._native_hook_runtime is not None:
                        self._native_hook_runtime.close()
                        self._native_hook_runtime = None
                    if self._native_hook_bridge is not None:
                        await self._native_hook_bridge.close()
                        self._native_hook_bridge = None
                self._pending_session_plan = None
                self._remove_agents()
                for bridge in self._mcp_bridges:
                    with contextlib.suppress(Exception):
                        await bridge.close()
                for bridge in self._external_mcp_bridges:
                    with contextlib.suppress(Exception):
                        await bridge.close()
                self._mcp_bridges.clear()
                self._named_mcp_bridges.clear()
                self._external_mcp_bridges.clear()
                self._named_external_mcp_bridges.clear()
                self._mcp_oauth_states.clear()
                self._agent_mcp_bridges.clear()
                self._agent_inline_mcp_bridges.clear()
                self._plugin_inline_mcp_configs.clear()
                self._configured_mcp_servers.clear()
                self._resolved_system_prompt = None
                self._effective_effort = None
                self._agent_model_snapshot = None
                self._startup_fallback = None
                self._shell_edit_verified = False
                self._sdk_tool_interrupt_mode = "kiro"
                self._sdk_mcp_toggle_mode = "reject"
                self._code_intelligence_option = False
                self._code_intelligence_enabled = False
                self._native_hooks = {event: list(matchers) for event, matchers in self._base_native_hooks.items()}
                self._policy_presets.clear()
                raise

    def _mcp_config_for_name(self, name: str) -> dict[str, Any]:
        entry = self._plugin_inline_mcp_configs.get(name)
        return entry[2] if entry is not None else self._configured_mcp_servers.get(name, {})

    async def _prepare_agent_mcp_bridges(self) -> None:
        """Start duplicate SDK bridges explicitly referenced by generated agents."""
        names = set()
        for name in sorted(names):
            config = self._configured_mcp_servers.get(name)
            if isinstance(config, dict) and config.get("type") == "sdk":
                bridge = self._new_sdk_mcp_bridge(name, config)
                self._mcp_bridges.append(bridge)
                self._named_mcp_bridges.setdefault(name, []).append(bridge)
                self._agent_mcp_bridges[name] = await bridge.start()
            elif (self._sdk_mcp_toggle_mode in ("sdk-and-stdio", "sdk-all") and isinstance(config, dict) and
                  config.get("type", "stdio") == "stdio"):
                bridge = self._new_external_mcp_bridge(name, config)
                self._agent_mcp_bridges[name] = await bridge.start()
            elif (self._sdk_mcp_toggle_mode == "sdk-all" and isinstance(config, dict) and
                  config.get("type") in ("http", "sse")):
                bridge = self._new_remote_mcp_bridge(name, config)
                self._agent_mcp_bridges[name] = await bridge.start()
        for name, (agent, server, config) in self._plugin_inline_mcp_configs.items():
            if config.get("type") == "sdk":
                bridge = self._new_sdk_mcp_bridge(name, config)
                self._mcp_bridges.append(bridge)
                self._named_mcp_bridges.setdefault(name, []).append(bridge)
            elif config.get("type", "stdio") == "stdio":
                bridge = self._new_external_mcp_bridge(name, config)
            else:
                bridge = self._new_remote_mcp_bridge(name, config)
            self._agent_inline_mcp_bridges[(agent, server)] = await bridge.start()

    def _external_mcp_cleanup_failed(self) -> None:
        if self._reader_error is None:
            self._reader_error = CLIConnectionError("External stdio MCP cleanup failed; reconnect the client")
        if self._connected and not self._disconnecting:
            task = asyncio.create_task(self._abort_for_guard(force=True))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_finished)

    def _new_external_mcp_bridge(self, name: str, config: dict[str, Any]) -> ExternalStdioMcpBridge:
        callback = self._mcp_output_callback(name)
        input_callback = self._mcp_input_callback(name)
        async def transform(tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
            return await transform_mcp_result(callback, self._sdk_mcp_output_snapshot[1], tool_name, arguments, result)
        async def transform_input(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await transform_mcp_arguments(input_callback, self._sdk_mcp_input_snapshot[1], tool_name, arguments)
        bridge_options: dict[str, Any] = {"on_cleanup_failure": self._external_mcp_cleanup_failed}
        if callback is not None:
            bridge_options["output_transform"] = transform
        if input_callback is not None:
            bridge_options["input_transform"] = transform_input
        bridge = ExternalStdioMcpBridge(config, self.options.cwd, self._configured_env(), _spawn_cli, **bridge_options)
        self._external_mcp_bridges.append(bridge)
        self._named_external_mcp_bridges.setdefault(name, []).append(bridge)
        return bridge

    def _mcp_output_callback(self, name: str) -> Any:
        callback = self._sdk_mcp_output_snapshot[0].get(name)
        if callback is None:
            return None
        async def transform(tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
            self._validate_unsupported_limits()
            guard = self._interaction_guard if name in self._mcp_failure_output_names else None
            turn, session_id = self._turn, self.session_id
            if name in self._mcp_failure_output_names:
                await self._check_mcp_failure_authorization(guard, turn, session_id)
                self._validate_unsupported_limits()
            async def check_warning() -> None:
                await self._check_mcp_failure_authorization(guard, turn, session_id)
                self._validate_unsupported_limits()
            token = _mcp_warning_guard.set(check_warning) if name in self._mcp_failure_output_names else None
            try:
                replacement = await callback(name, tool_name, arguments, result)
            finally:
                if token is not None:
                    _mcp_warning_guard.reset(token)
            self._validate_unsupported_limits()
            if name in self._mcp_failure_output_names:
                await self._check_mcp_failure_authorization(guard, turn, session_id)
                self._validate_unsupported_limits()
            return replacement
        return transform

    def _mcp_input_callback(self, name: str) -> Any:
        callback = self._sdk_mcp_input_snapshot[0].get(name)
        if callback is None:
            return None
        async def transform(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self._validate_unsupported_limits()
            guard = self._interaction_guard if name in self._mcp_failure_input_names else None
            turn, session_id = self._turn, self.session_id
            if name in self._mcp_failure_input_names:
                await self._check_mcp_failure_authorization(guard, turn, session_id)
                self._validate_unsupported_limits()
            async def check_warning() -> None:
                await self._check_mcp_failure_authorization(guard, turn, session_id)
                self._validate_unsupported_limits()
            token = _mcp_warning_guard.set(check_warning) if name in self._mcp_failure_input_names else None
            try:
                replacement = await callback(name, tool_name, arguments)
            finally:
                if token is not None:
                    _mcp_warning_guard.reset(token)
            self._validate_unsupported_limits()
            if name in self._mcp_failure_input_names:
                await self._check_mcp_failure_authorization(guard, turn, session_id)
                self._validate_unsupported_limits()
            return replacement
        return transform

    async def _check_mcp_failure_authorization(self, guard: Any, turn: Any, session_id: Any) -> None:
        def current() -> bool:
            return (self._interaction_guard is guard and self._turn is turn and self.session_id == session_id and
                    (turn is None or not turn.done()) and self._connected and not self._disconnecting and
                    not self._interrupt_requested and self._reader_error is None)
        if not current():
            raise ProtocolError("MCP hook authorization changed")
        if guard is None:
            if self._require_mcp_failure_authorization:
                raise ProtocolError("MCP hook authorization is unavailable")
            return
        if await guard() is not True or not current():
            raise ProtocolError("MCP hook authorization changed")

    def _new_sdk_mcp_bridge(self, name: str, config: dict[str, Any]) -> SdkMcpBridge:
        output_callback = self._mcp_output_callback(name)
        input_callback = self._mcp_input_callback(name)
        if output_callback is None and input_callback is None:
            return SdkMcpBridge(config)
        return SdkMcpBridge(config, output_transform=output_callback,
                            output_timeout=self._sdk_mcp_output_snapshot[1],
                            input_transform=input_callback,
                            input_timeout=self._sdk_mcp_input_snapshot[1])

    def _new_remote_mcp_bridge(self, name: str, config: dict[str, Any]) -> RemoteMcpBridge:
        callback = self._mcp_output_callback(name)
        input_callback = self._mcp_input_callback(name)
        async def transform(tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
            return await transform_mcp_result(callback, self._sdk_mcp_output_snapshot[1], tool_name, arguments, result)
        async def transform_input(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await transform_mcp_arguments(input_callback, self._sdk_mcp_input_snapshot[1], tool_name, arguments)
        bridge_options: dict[str, Any] = {"connect_timeout": self.options.startup_timeout,
            "oauth": self.options.mcp_oauth.get(name), "_oauth_state": self._mcp_oauth_states.get(name)}
        if callback is not None:
            bridge_options["output_transform"] = transform
        if input_callback is not None:
            bridge_options["input_transform"] = transform_input
        bridge = RemoteMcpBridge(config, **bridge_options)
        if bridge.oauth is not None:
            self._mcp_oauth_states[name] = (bridge.oauth, bridge.oauth_origins)
        self._external_mcp_bridges.append(bridge)
        self._named_external_mcp_bridges.setdefault(name, []).append(bridge)
        return bridge

    def _skill_roots(self) -> list[Path]:
        workspace = Path(self.options.cwd or os.getcwd()).resolve()
        return [workspace / ".kiro" / "skills", self._kiro_home() / "skills"]

    def _agent_skill_paths(self, skills: Optional[list[str]]) -> dict[str, Path]:
        if skills is None:
            return {}
        if not isinstance(skills, list) or any(not isinstance(name, str) or
                                                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name)
                                                for name in skills):
            raise ValueError("Agent skills must be Kiro skill directory names")
        roots = self._skill_roots()
        paths: dict[str, Path] = {}
        for name in dict.fromkeys(skills):
            for root in roots:
                candidate = root / name / "SKILL.md"
                if not candidate.is_file():
                    continue
                resolved_root = root.resolve()
                resolved_file = candidate.resolve()
                try:
                    resolved_file.relative_to(resolved_root)
                except ValueError as exc:
                    raise ValueError(f"Agent skill {name!r} resolves outside its skill directory") from exc
                paths[name] = candidate
                break
            else:
                raise ValueError(f"Agent skill {name!r} was not found in .kiro/skills or the Kiro home skills directory")
        return paths

    def _agent_skill_resources(self, skills: Optional[list[str]]) -> list[str]:
        return [path.resolve().as_uri() for path in self._agent_skill_paths(skills).values()]

    def _runtime_agent_turn_state(self) -> Any:
        definitions = self.options.agents or {}
        if not isinstance(definitions, dict):
            return ("invalid",)
        return {name: (type(definition.max_turns), definition.max_turns)
                for name, definition in definitions.items() if isinstance(definition, AgentDefinition) and
                definition.max_turns is not None}

    def _runtime_policy(self) -> tuple[Any, ...]:
        options = self.options
        return (options.runtime_compat_mode, options.max_turns, options.verbatim_prompts, options.strict_mcp_config,
                options.resume_drops_turn, options.fallback_model_mode,
                options.fallback_model if options.fallback_model_mode == "runtime" else None,
                self._runtime_agent_turn_state(), options.runtime_usage_mode)

    def _runtime_turn_limit(self) -> Optional[int]:
        limits = [value for value in (self.options.max_turns,
                  self._runtime_agent_max_turns.get(self._current_mode or self._selected_agent())) if value]
        return min(limits) if limits else None

    def _resolve_agent_effort(self) -> Optional[str]:
        child_efforts: dict[str, str] = {}
        definitions = list((self.options.agents or {}).items())
        for name, definition in definitions:
            if not isinstance(definition, AgentDefinition) or definition.effort is None:
                continue
            raise ValueError("Agent effort requires agent_effort_mode='main-only' or 'runtime'")
        if len(child_efforts) > 256:
            raise ValueError("Runtime effort supports at most 256 named agents")
        self._runtime_agent_efforts = child_efforts
        return self._configured_effort()

    def _resolve_inherited_model(self) -> Optional[str]:
        definitions = list((self.options.agents or {}).items())
        inherited = [name for name, definition in definitions
                     if isinstance(definition, AgentDefinition) and definition.model == "inherit"]
        if not inherited:
            return None
        if self.options.fallback_model_mode == "startup" and self.options.fallback_model not in (None, ""):
            raise ValueError("Startup fallback cannot be combined with inherited model snapshots")
        raise ValueError("Inherited agent models require agent_model_mode='snapshot'")

    def _compile_agent_tool_settings(self, definition: AgentDefinition) -> tuple[
            Optional[list[str]], Optional[list[str]], Optional[dict[str, Any]], bool]:
        if (definition.tools_mode not in ("native", "selectors") or
                not isinstance(definition.tool_selector_map, dict) or
                definition.disallowed_tools_mode not in ("native", "v3-permissions") or
                not isinstance(definition.disallowed_tool_permission_map, dict)):
            raise ValueError("Invalid agent tool mapping mode or map")
        if definition.tools_mode == "native" and definition.tool_selector_map:
            raise ValueError("Agent tool_selector_map requires selectors mode")
        if definition.disallowed_tools_mode == "native" and definition.disallowed_tool_permission_map:
            raise ValueError("Agent disallowed tool mapping requires v3-permissions mode")
        mapped = (definition.tools_mode == "selectors" or
                  definition.disallowed_tools_mode == "v3-permissions")
        if mapped and (self.options.agent_engine != "v3" or not isinstance(definition.tools, list)):
            raise ValueError("Agent tool mappings require Kiro V3 and explicit tools")
        tools = copy.deepcopy(definition.tools)
        if definition.tools_mode == "selectors":
            names = definition.tools
            if (len(names) > 128 or
                    any(not isinstance(name, str) or not name.strip() or name != name.strip() or
                        len(name) > 512 or "\0" in name for name in names) or len(set(names)) != len(names) or
                    set(definition.tool_selector_map) != set(names)):
                raise ValueError("Agent tools require distinct names and an exact selector map")
            tools = []
            for name in names:
                selectors = definition.tool_selector_map[name]
                if (not isinstance(selectors, list) or not selectors or len(selectors) > 128 or
                        any(not isinstance(value, str) or not value or "\0" in value or len(value) > 1024
                            for value in selectors)):
                    raise ValueError("Each agent tool must map to bounded Kiro selectors")
                for selector in selectors:
                    if selector not in tools:
                        tools.append(selector)
            if len(tools) > 128:
                raise ValueError("Too many mapped agent tool selectors")
        excluded = copy.deepcopy(definition.disallowed_tools)
        permissions = copy.deepcopy(definition.kiro_permissions)
        if definition.disallowed_tools_mode == "v3-permissions":
            if self._permission_mode in ("plan", "trustAll") or self.options.kiro_policy_presets:
                raise ValueError("Agent deny mappings cannot use trustAll, Plan mode or permission presets")
            names = definition.disallowed_tools
            if (not isinstance(names, list) or not names or len(names) > 128 or
                    any(not isinstance(name, str) or not name.strip() or name != name.strip() or
                        len(name) > 512 or "\0" in name for name in names) or len(set(names)) != len(names) or
                    set(definition.disallowed_tool_permission_map) != set(names)):
                raise ValueError("Agent disallowed_tools require distinct names and an exact permission map")
            permissions = permissions if permissions is not None else {"rules": []}
            self._validate_kiro_permissions(permissions)
            count = 0
            for name in names:
                rules = definition.disallowed_tool_permission_map[name]
                if not isinstance(rules, list) or not rules:
                    raise ValueError("Each agent disallowed tool requires deny rules")
                self._validate_kiro_permissions({"rules": rules})
                for rule in rules:
                    if rule["effect"] != "deny":
                        raise ValueError("Agent disallowed tool mappings may only contain deny rules")
                    if any(not value or "\0" in value or len(value) > 4096
                           for key in ("match", "exclude") for value in rule.get(key, [])):
                        raise ValueError("Invalid mapped agent permission resource")
                count += len(rules)
                if count > 128:
                    raise ValueError("Too many mapped agent permission rules")
                permissions["rules"].extend(copy.deepcopy(rules))
            if len(json.dumps(permissions, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
                raise ValueError("Mapped agent permissions exceed the size limit")
            excluded = None
        return tools, excluded, permissions, mapped

    def _agent_mapping_state(self) -> tuple[Any, ...]:
        definitions = self.options.agents
        if definitions is None:
            definitions = {}
        if not isinstance(definitions, dict):
            return ("invalid",)
        state = {}
        for name, definition in definitions.items():
            if not isinstance(definition, AgentDefinition):
                state[name] = ("invalid",)
            elif (definition.tools_mode != "native" or definition.tool_selector_map or
                  definition.disallowed_tools_mode != "native" or definition.disallowed_tool_permission_map):
                state[name] = copy.deepcopy((definition.tools, definition.tools_mode, definition.tool_selector_map,
                    definition.disallowed_tools, definition.disallowed_tools_mode,
                    definition.disallowed_tool_permission_map, definition.kiro_permissions))
        return (self.options.agent if state else None, state,
                self.options.agent_engine if state else None,
                self._permission_mode if state else None,
                copy.deepcopy(self.options.kiro_policy_presets) if state else None)

    def _install_agents(self) -> None:
        definitions = dict(self.options.agents or {})
        agent_max_turns: dict[str, int] = {}
        foreground_agents = []
        for name, definition in definitions.items():
            if not isinstance(definition, AgentDefinition) or definition.max_turns is None:
                continue
            if (self.options.runtime_compat_mode != "v3" or type(definition.max_turns) is not int or
                    not 1 <= definition.max_turns <= 2**31 - 1):
                raise ValueError("Agent max_turns requires a positive integer and runtime_compat_mode='v3'")
            if not isinstance(name, str) or not name or len(name) > 128 or any(ord(c) < 32 for c in name):
                raise ValueError("Agent max_turns requires bounded agent names")
            agent_max_turns[name] = definition.max_turns
        if len(agent_max_turns) > 256 or len(foreground_agents) > 256:
            raise ValueError("Agent runtime controls support at most 256 named agents")
        self._runtime_agent_max_turns = agent_max_turns
        self._runtime_foreground_agents = foreground_agents
        if not definitions:
            self._agent_mapping_snapshot = self._agent_mapping_state()
            return
        agents_dir = self._kiro_home() / "agents"
        workspace = Path(self.options.cwd or os.getcwd()).resolve() / ".kiro" / "agents"
        agents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        agent_mcp: dict[str, dict[str, Any]] = {}
        agent_resources: dict[str, list[str]] = {}
        agent_tools: dict[str, tuple[Optional[list[str]], Optional[list[str]], Optional[dict[str, Any]], bool]] = {}
        for name, definition in definitions.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in {"default", "plan", "vibe", "spec", "kiro_default", "kiro_planner"}:
                raise ValueError(f"Invalid or reserved agent name: {name!r}")
            if not isinstance(definition, AgentDefinition):
                raise TypeError(f"Agent {name!r} must be an AgentDefinition")
            if not isinstance(definition.description, str) or not isinstance(definition.prompt, str):
                raise ValueError("Agent description and prompt must be strings")
            if definition.tools is not None and (not isinstance(definition.tools, list) or
                    any(not isinstance(item, str) or not item for item in definition.tools)):
                raise ValueError("Agent tools must contain nonempty Kiro selectors")
            if definition.disallowed_tools is not None and (not isinstance(definition.disallowed_tools, list) or
                    any(not isinstance(item, str) or not item for item in definition.disallowed_tools)):
                raise ValueError("Agent disallowed_tools must contain nonempty Kiro selectors")
            agent_tools[name] = self._compile_agent_tool_settings(definition)
            if definition.model is not None and (not isinstance(definition.model, str) or not definition.model.strip()):
                raise ValueError("Agent model must be a nonempty Kiro model ID")
            if definition.initial_prompt is not None and not isinstance(definition.initial_prompt, str):
                raise ValueError("Agent initial_prompt must be a string")
            if definition.model == "inherit":
                if self._agent_model_snapshot is None:
                    raise ValueError("Kiro agent model snapshot was not configured")
            self._validate_resources(definition.kiro_resources)
            resources = list(definition.kiro_resources)
            for resource in self._agent_skill_resources(definition.skills):
                if resource not in resources:
                    resources.append(resource)
            agent_resources[name] = resources
            if definition.kiro_permissions is not None:
                if self.options.agent_engine != "v3":
                    raise ValueError("Agent kiro_permissions requires Kiro V3")
                self._validate_kiro_permissions(definition.kiro_permissions)
                if (self._effective_can_use_tool()) and any(
                             rule["effect"] == "allow" for rule in definition.kiro_permissions["rules"]):
                    raise ValueError("Agent Kiro allow rules can bypass permission hooks")
            if definition.disallowed_tools and self.options.agent_engine != "v3":
                raise ValueError("Agent disallowed_tools requires Kiro V3")
            if definition.mcp_servers is not None:
                if self.options.agent_engine != "v3":
                    raise ValueError("Agent mcp_servers requires Kiro V3")
                if not isinstance(definition.mcp_servers, list):
                    raise ValueError("Agent mcp_servers must be a list of inline server definitions")
                servers: dict[str, Any] = {}
                for entry in definition.mcp_servers:
                    if isinstance(entry, str):
                        raise ValueError("Named agent MCP references require agent_mcp_reference_mode='duplicate'")
                    else:
                        if not isinstance(entry, dict) or len(entry) != 1:
                            raise ValueError("Each agent MCP entry must contain one named inline server")
                        server_name, config = next(iter(entry.items()))
                    if (not isinstance(server_name, str) or not server_name.strip() or "\0" in server_name or
                            not isinstance(config, dict) or server_name in servers):
                        raise ValueError("Invalid or duplicate agent MCP server")
                    if config.get("type") == "sdk":
                        if isinstance(entry, str) and server_name in self._agent_mcp_bridges:
                            config = self._agent_mcp_bridges[server_name]
                        else:
                            raise ValueError("Agent MCP SDK servers require a parent reference or agent_inline_mcp_mode='sdk'")
                    elif isinstance(entry, str) and server_name in self._agent_mcp_bridges:
                        config = self._agent_mcp_bridges[server_name]
                    config = copy.deepcopy(config)
                    for field in ("env", "headers"):
                        values = config.get(field)
                        if isinstance(values, list):
                            converted = {}
                            for item in values:
                                if (not isinstance(item, dict) or set(item) != {"name", "value"} or
                                        not isinstance(item["name"], str) or not isinstance(item["value"], str) or
                                        item["name"] in converted):
                                    raise ValueError(f"Invalid agent MCP {field} entries")
                                converted[item["name"]] = item["value"]
                            config[field] = converted
                    try:
                        json.dumps(config, allow_nan=False)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Agent MCP configuration must be JSON-serializable") from exc
                    servers[server_name] = config
                agent_mcp[name] = servers
            if any((directory / (name + ext)).exists()
                   for directory in (agents_dir, workspace) for ext in (".json", ".md", ".yaml", ".yml")):
                raise FileExistsError(f"Kiro agent already exists: {name}")
        try:
            for name, definition in definitions.items():
                config: dict[str, Any] = {"name": name, "description": definition.description,
                                          "prompt": definition.prompt}
                selected_tools, excluded_tools, permissions, mapped = agent_tools[name]
                if selected_tools is not None:
                    config["tools"] = selected_tools
                if definition.model:
                    config["model"] = (self._agent_model_snapshot if definition.model == "inherit"
                                       else definition.model)
                if agent_resources[name]:
                    config["resources"] = agent_resources[name]
                if excluded_tools:
                    config["excludedTools"] = excluded_tools
                if permissions is not None:
                    config["permissions"] = permissions
                if name in agent_mcp:
                    config["mcpServers"] = agent_mcp[name]
                content = json.dumps(config, ensure_ascii=False).encode()
                path = agents_dir / (name + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    stat = os.fstat(output.fileno())
                    self._created_agents.append((path, stat.st_ino, len(content), hashlib.sha256(content).digest()))
                    output.write(content)
                if mapped:
                    self._mapped_agent_profiles[name] = permissions
            self._agent_mapping_snapshot = self._agent_mapping_state()
        except BaseException:
            self._remove_agents()
            raise

    def _remove_agents(self) -> None:
        for path, inode, size, digest in self._created_agents:
            try:
                stat = path.lstat()
                if not path.is_symlink() and stat.st_ino == inode and stat.st_size == size and hashlib.sha256(path.read_bytes()).digest() == digest:
                    path.unlink()
            except FileNotFoundError:
                pass
        self._created_agents.clear()
        self._mapped_agent_profiles.clear()
        self._agent_mapping_snapshot = None

    @property
    def _cli_path(self) -> Union[str, Path]:
        return self.options.cli_path if self.options.cli_path is not None else "kiro-cli"

    def _kiro_home(self) -> Path:
        environment = {**os.environ, **self._configured_env()}
        if environment.get("KIRO_HOME"):
            path = Path(environment["KIRO_HOME"]).expanduser()
            return path if path.is_absolute() else (Path(self.options.cwd or os.getcwd()) / path).resolve()
        return Path(environment.get("HOME", str(Path.home()))) / ".kiro"

    def _remove_temporary_agent(self) -> None:
        path, identity = self._agent_file, self._agent_file_identity
        self._agent_file = None
        self._agent_file_identity = None
        if path is None or identity is None:
            return
        inode, size, digest = identity
        try:
            stat = path.lstat()
            if not path.is_symlink() and stat.st_ino == inode and stat.st_size == size and hashlib.sha256(path.read_bytes()).digest() == digest:
                path.unlink()
        except FileNotFoundError:
            pass

    async def _prepare_verified_runtime(self) -> None:
        if self._native_hook_runtime is not None:
            return
        if self._native_hook_runtime_path is not None:
            source = Path(self._native_hook_runtime_path).absolute()
            if (len(source.parents) < 5 or source.name != "acp-server.js" or source.parent.name != "server" or
                    source.parents[4].name != "node_modules" or not source.is_file()):
                raise ValueError("native_hook_runtime_path must be an existing @kiro/agent/dist/server/acp-server.js")
        from .store_fork import _require_tested_cli
        await _require_tested_cli(self._cli_path, str(self.options.cwd or os.getcwd()),
            self._configured_env(), self.options.startup_timeout or 30, operation="SDK runtime adapter")
        self._validate_unsupported_limits()
        self._native_hook_runtime = NativeHookRuntime()
        self._native_hook_runtime.prepare(
            self._native_hook_runtime_path, child_process_env(self.options.cwd, self._configured_env()),
            strict_mcp=self._runtime_policy_snapshot[3],
            mcp_bootstrap=False)

    def _bind_client_auth(self, child: KiroSDKClient) -> None:
        """Bind an internal client's token and prompt access to this operation."""
        if self._client_auth_snapshot[0] != "client":
            return
        owner = asyncio.current_task()
        generation, session_id, turn = self._connection_generation, self.session_id, self._turn
        connected, initializing = self._connected, self._initializing
        shutdown_revision = self._client_auth_shutdown_revision
        interrupted, suspended = self._interrupt_requested, self._sdk_tools_suspended
        prompt_guard, interaction_guard = self._prompt_send_guard, self._interaction_guard
        factory_guard = self._client_auth_factory_guard
        require_authorization = self._require_client_auth_authorization

        def current() -> bool:
            if (owner is None or owner.done() or self._connection_generation != generation or
                    self.session_id != session_id or self._turn is not turn or
                    self._connected != connected or self._initializing != initializing or
                    self._client_auth_shutdown_revision != shutdown_revision or
                    self._disconnecting or self._reader_error is not None or
                    self._interrupt_requested != interrupted or self._sdk_tools_suspended != suspended or
                    (turn is not None and (turn.done() or self._interrupt_requested)) or
                    (not connected and not self._connect_lock.locked()) or
                    self._prompt_send_guard is not prompt_guard or self._interaction_guard is not interaction_guard or
                    self._client_auth_factory_guard is not factory_guard or
                    self._require_client_auth_authorization != require_authorization or
                    (require_authorization and prompt_guard is None)):
                return False
            self._validate_unsupported_limits()
            return factory_guard is None or factory_guard() is True

        async def authorize() -> bool:
            if not current():
                return False
            for guard in (prompt_guard, interaction_guard if interaction_guard is not prompt_guard else None):
                if guard is not None and (await guard() is not True or not current()):
                    return False
            return current()

        child._client_auth_ancestors = (self, *self._client_auth_ancestors)
        child._client_auth_lock = self._client_auth_lock
        child._client_auth_request_times = self._client_auth_request_times
        child._client_auth_factory_guard = current
        child._require_client_auth_authorization = True
        child._prompt_send_guard = authorize
        child._interaction_guard = authorize


    async def _connect_control(self) -> None:
        """Initialize a local ACP connection without creating a session."""
        if self in _CLIENT_AUTH_CONTEXT.get():
            raise CLIConnectionError("Authentication callbacks cannot control the client")
        async with self._connect_lock:
            if self._connected:
                raise CLIConnectionError("Already connected")
            self._hook_failure_runner.reopen()
            self._require_settled_request_callbacks()
            if self._transport is not None:
                raise ValueError("Control-only connections require the local CLI")
            if self.options.cwd is not None and not Path(self.options.cwd).is_dir():
                raise CLIConnectionError(f"Working directory does not exist: {self.options.cwd}")
            self._client_auth_control_active = self._client_auth_snapshot[0] == "client"
            await self._connect_locked(control_only=True)


    def _require_settled_request_callbacks(self) -> None:
        self._lingering_handler_tasks = {task for task in self._lingering_handler_tasks if not task.done()}
        if self._lingering_handler_tasks:
            raise CLIConnectionError("Request callback cleanup is incomplete; disconnect again after it finishes")

    async def _connect_locked(self, prompt: Optional[str] = None, *, control_only: bool = False) -> None:
        if self._connected:
            raise CLIConnectionError("Already connected")
        if (self._transport is None and os.sep in str(self._cli_path) and
                Path(self._cli_path).expanduser().is_dir()):
            raise CLINotFoundError(f"Kiro CLI is a directory, not an executable: {self._cli_path}")
        self._native_callback_slots = None
        self._validate_native_hook_bridge(require_trust=True)
        self._events = asyncio.Queue()
        self._pending_mirror_errors.clear()
        self._stderr.clear()
        self.server_info = None
        self.session_info = None
        self._session_model = self._configured_model() or ""
        self.history = []
        self._context_metadata_by_session = {}
        self._available_commands_by_session = {}
        self._mcp_status_tracker = McpStatusTracker()
        self._subagent_parents_by_session = {}
        self._subagent_types_by_session = {}
        self._hook_registry_revision = {}
        self._hook_snapshots_by_session = {}
        self._native_hook_names = set()
        self._native_hook_command = None
        self._hook_listing_negotiated = False
        self._sandbox_status = None
        self._sandbox_event = asyncio.Event()
        self._reader_error = None
        self._store_key = None
        self._current_mode = None
        self._mode_before_plan = None
        self._protected_mode = None
        self._guards_armed = False
        self._transport_closed = False
        self._sandbox_required = self.options.require_sandbox or None
        self._recovery_in_doubt = False
        if self._sandbox_required and self.options.agent_engine != "v3":
            raise ValueError("require_sandbox requires Kiro V3")
        agent = self._selected_agent()
        effort = self._effective_effort if self._effective_effort is not None else self._configured_effort()
        hook_command = None
        hook_names = set()
        if self._transport is not None:
            try:
                await self._transport.connect()
            except BaseException:
                with contextlib.suppress(Exception):
                    await self._transport.close()
                raise
        else:
            command = [str(self._cli_path), "acp"]
            if self._client_auth_snapshot[0] == "client":
                command.extend(["--auth-method", "client"])
            agent = self._selected_agent()
            hook_command = None
            hook_env = {}
            if self.options.runtime_compat_mode == "v3":
                await self._prepare_verified_runtime()
                hook_env = {"KIRO_KAS_SERVER_PATH": str(self._native_hook_runtime.path)}
            if self._native_hooks:
                stop_matchers = self._native_hooks.get("Stop", [])
                self._native_stop_multi = (sum(len(matcher.hooks) for matcher in stop_matchers) > 1 and
                                           any(matcher.on_failure is not None for matcher in stop_matchers))
                self._native_hook_bridge = NativeHookBridge(
                    self._native_hooks, supports_ask=self.options.agent_engine == "v3",
                    on_system_message=self._deliver_native_hook_message,
                    failure_guard_factory=self._native_failure_guard,
                    on_stop_block=self._record_native_stop_block)
                if self._native_hook_bridge_mode == "runtime-acp":
                    await self._prepare_verified_runtime()
                    hook_command = self._native_hook_runtime.command
                    hook_env = {"KIRO_KAS_SERVER_PATH": str(self._native_hook_runtime.path)}
                else:
                    hook_command, hook_env = await self._native_hook_bridge.start(
                        private_credentials=True, user=self._process_user)
            if self._has_profile_system_prompt() or self.options.tools is not None or self._effective_kiro_permissions() is not None or self.options.kiro_resources or (hook_command):
                agent_dir = self._kiro_home() / "agents"
                try:
                    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd, filename = tempfile.mkstemp(prefix="kiro-sdk-", suffix=".json", dir=str(agent_dir))
                except BaseException:
                    if self._native_hook_bridge:
                        await self._native_hook_bridge.close()
                        self._native_hook_bridge = None
                    raise
                self._agent_file = Path(filename)
                agent = self._agent_file.stem
                config: dict[str, Any] = {"name": agent, "description": "Temporary Kiro Agent SDK profile"}
                if self._has_profile_system_prompt():
                    config["prompt"] = self._resolved_system_prompt
                if self.options.tools is not None:
                    config["tools"] = self.options.tools
                elif hook_command or self._effective_kiro_permissions() is not None:
                    config["tools"] = ["*"]
                if self._effective_kiro_permissions() is not None:
                    config["permissions"] = self._effective_kiro_permissions()
                resources = list(self.options.kiro_resources)
                for resource in ():
                    if resource not in resources:
                        resources.append(resource)
                if resources:
                    config["resources"] = resources
                if hook_command:
                    if self.options.agent_engine == "v3":
                        config["hooks"] = []
                        for event in self._native_hooks:
                            hook_name = f"{agent}-{event}"
                            hook_names.add(hook_name)
                            config["hooks"].append({"name": hook_name, "trigger": event,
                                                    "action": {"type": "command", "command": hook_command}})
                    else:
                        triggers = {"SessionStart": "agentSpawn", "UserPromptSubmit": "userPromptSubmit",
                                    "PreToolUse": "preToolUse", "PostToolUse": "postToolUse", "Stop": "stop"}
                        config["hooks"] = {triggers[event]: [{"command": hook_command}] for event in self._native_hooks}
                if self.options.agent_engine == "v3" and hook_command:
                    self._native_hook_names = set(hook_names)
                    self._native_hook_command = hook_command
                try:
                    with os.fdopen(fd, "wb") as output:
                        stat = os.fstat(output.fileno())
                        self._agent_file_identity = (stat.st_ino, 0, hashlib.sha256(b"").digest())
                        content = json.dumps(config, ensure_ascii=False).encode()
                        self._agent_file_identity = (stat.st_ino, len(content), hashlib.sha256(content).digest())
                        output.write(content)
                except BaseException:
                    self._remove_temporary_agent()
                    if self._native_hook_bridge:
                        await self._native_hook_bridge.close()
                        self._native_hook_bridge = None
                    raise
            if agent and self.options.agent_engine != "v3":
                command.extend(["--agent", agent])
            if self.options.agent_engine:
                command.extend(["--agent-engine", self.options.agent_engine])
            elif hook_command:
                command.extend(["--agent-engine", "v2"])
            if effort and self.options.agent_engine != "v3":
                command.extend(["--effort", effort])
            if self._configured_model() and self.options.agent_engine != "v3":
                command.extend(["--model", self._configured_model()])
            try:
                self._validate_unsupported_limits()
                if self._native_hook_runtime is not None:
                    self._native_hook_runtime.bind(agent, self._native_hooks)
                    self._native_hook_runtime.verify()
                if self._shell_edit_verified and (
                    not self.options.kiro_shell_edit or self.options.agent_engine != "v3" or
                    "--agent-engine" not in command or command[command.index("--agent-engine") + 1] != "v3"
                ):
                    raise ProtocolError("Kiro shell editing requires the verified V3 process")
                process_env = child_process_env(self.options.cwd, {**self._configured_env(), **hook_env})
                if hook_command:
                    process_env.pop("KIRO_SDK_HOOK_TOKEN", None)
                    process_env.pop("KIRO_SDK_HOOK_PORT", None)
                if self._client_auth_snapshot[0] == "client":
                    process_env = _blank_client_api_key(process_env)
                self._process = await _spawn_cli(
                    *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=str(self.options.cwd) if self.options.cwd else None,
                    env=process_env,
                    limit=self.options.max_buffer_size or 16 * 1024 * 1024,
                    start_new_session=(os.name == "posix"),
                    **({"user": self._process_user} if self._process_user is not None else {}))
            except FileNotFoundError as exc:
                self._remove_temporary_agent()
                if self._native_hook_bridge:
                    await self._native_hook_bridge.close()
                    self._native_hook_bridge = None
                raise CLINotFoundError(f"Kiro CLI not found: {self._cli_path}") from exc
            except BaseException:
                self._remove_temporary_agent()
                if self._native_hook_bridge:
                    await self._native_hook_bridge.close()
                    self._native_hook_bridge = None
                raise
        self._connected = True
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr()) if self._process else None
        self._initializing = True
        hook_verified_revision = 0
        try:
            caps = {}
            self._hook_listing_negotiated = (
                self.options.agent_engine == "v3" and self.options.enable_kiro_hooks is True)
            if self.options.enable_kiro_hooks and self.options.agent_engine == "v3":
                caps = {"_meta": {"kiro": {"hooks": {"enabled": True, "v2": True}}}}
            if self.options.on_user_input is not None:
                caps.setdefault("_meta", {}).setdefault("kiro", {})["userInput"] = True
            if self._code_intelligence_enabled:
                caps.setdefault("_meta", {}).setdefault("kiro", {}).setdefault("settings", {})[
                    "codeIntelligence"] = {"enabled": True}
            session_policies = None
            if self.options.runtime_compat_mode == "v3":
                session_policies = {"protocol": 3, "strictMcp": self.options.strict_mcp_config,
                                    "preCompact": False,
                                    "preCompactScope": 'tree',
                                    "subagentStart": False,
                                    "postToolOutput": (False),
                                    "sdkPostTool": False,
                                    **({})}
                caps.setdefault("_meta", {}).setdefault("kiro", {})["sdkSessionPolicies"] = session_policies
            self.server_info = await self._request("initialize", {"protocolVersion": 1, "clientCapabilities": caps,
                "clientInfo": {"name": "kiro-agent-sdk", "version": "0.1.0"}})
            if self.server_info.get("protocolVersion") != 1:
                raise ProtocolError("Unsupported ACP protocol version")
            if self._native_hook_runtime is not None:
                self._native_hook_runtime.confirm(self.server_info)
                if (self.options.runtime_compat_mode == "v3" and
                        (type(self.server_info.get("agentCapabilities", {}).get("sdkRuntimePolicies")) is not int or
                         self.server_info["agentCapabilities"]["sdkRuntimePolicies"] != 7)):
                    raise ProtocolError("Kiro did not confirm SDK runtime policies")
                if session_policies is not None:
                    confirmed = self.server_info.get("agentCapabilities", {}).get("sdkSessionPolicies")
                    if (not isinstance(confirmed, dict) or type(confirmed.get("protocol")) is not int or
                            confirmed != session_policies or
                            type(confirmed.get("strictMcp")) is not bool or type(confirmed.get("preCompact")) is not bool):
                        raise ProtocolError("Kiro did not confirm SDK session policies")
            if self._code_intelligence_enabled:
                self._require_kiro_extension("_kiro/codeIntelligence")
            if self.options.enable_file_checkpointing:
                capabilities = _kiro_capabilities(self.server_info)
                if capabilities.get("checkpoints") is not True:
                    raise ProtocolError("Kiro does not advertise file checkpoints")
            if control_only:
                return
            servers = []
            for name, server in self._configured_mcp_servers.items():
                if server.get("type") == "sdk":
                    bridge = self._new_sdk_mcp_bridge(name, server)
                    self._mcp_bridges.append(bridge)
                    self._named_mcp_bridges.setdefault(name, []).append(bridge)
                    item = await bridge.start()
                else:
                    item = dict(server)
                transport_type = item.get("type", "stdio")
                if transport_type not in ("stdio", "http", "sse"):
                    raise ValueError(f"Unsupported MCP transport: {transport_type}")
                relay_remote = (server.get("type") != "sdk" and transport_type in ("http", "sse") and
                                self._sdk_mcp_toggle_mode == "sdk-all")
                if (transport_type in ("http", "sse") and not relay_remote and
                        not self.server_info.get("agentCapabilities", {}).get("mcpCapabilities", {}).get(transport_type, False)):
                    raise ProtocolError(f"Kiro does not advertise MCP {transport_type} support")
                item["name"] = name
                if isinstance(item.get("env"), dict):
                    item["env"] = [{"name": k, "value": v} for k, v in item["env"].items()]
                if isinstance(item.get("headers"), dict):
                    item["headers"] = [{"name": k, "value": v} for k, v in item["headers"].items()]
                if transport_type == "stdio":
                    item.setdefault("args", [])
                    item.setdefault("env", [])
                    if (not isinstance(item.get("command"), str) or not item["command"].strip() or
                            "\0" in item["command"] or not isinstance(item["args"], list) or
                            any(not isinstance(arg, str) or "\0" in arg for arg in item["args"])):
                        raise ValueError(f"Invalid stdio MCP server: {name}")
                    entries = item["env"]
                else:
                    item.setdefault("headers", [])
                    if not isinstance(item.get("url"), str) or not item["url"].strip() or "\0" in item["url"]:
                        raise ValueError(f"Invalid {transport_type} MCP server: {name}")
                    entries = item["headers"]
                if (not isinstance(entries, list) or any(
                        not isinstance(entry, dict) or set(entry) != {"name", "value"} or
                        not isinstance(entry["name"], str) or not entry["name"] or "\0" in entry["name"] or
                        not isinstance(entry["value"], str) or "\0" in entry["value"] for entry in entries)):
                    raise ValueError(f"Invalid MCP server environment or headers: {name}")
                if (server.get("type") != "sdk" and transport_type == "stdio" and
                        self._sdk_mcp_toggle_mode in ("sdk-and-stdio", "sdk-all")):
                    bridge = self._new_external_mcp_bridge(name, server)
                    item = await bridge.start()
                    item["name"] = name
                elif relay_remote:
                    bridge = self._new_remote_mcp_bridge(name, server)
                    item = await bridge.start()
                    item["name"] = name
                servers.append(item)
            self._mcp_servers = servers
            requested_option_model = self._model_option
            requested_model = self._configured_model()
            params = {"cwd": self._working_directory(), "mcpServers": servers}
            if self.options.add_dirs:
                params["additionalDirectories"] = self._additional_directories()
            if self.options.agent_engine == "v3":
                meta = {}
                if self._permission_mode == "plan":
                    meta["modeId"] = "plan"
                elif agent:
                    meta["modeId"] = agent
                if requested_model and self._startup_fallback is None:
                    meta["modelId"] = requested_model
                if effort:
                    meta["effortLevel"] = effort
                if self._configured_thinking() is not None:
                    meta["thinkingEnabled"] = self._configured_thinking()
                if self.options.session_id is not None:
                    meta["overrideSessionId"] = self.options.session_id
                if self._policy_presets:
                    meta["policyPreset"] = self._policy_presets
                if meta:
                    params["_meta"] = {"kiro": meta}
            if self.options.resume_from_store and self._restored_session_id is None:
                if not self.server_info.get("agentCapabilities", {}).get("loadSession", False):
                    raise ProtocolError("Kiro does not advertise session/load")
                from .archive import _restore_session_from_store, restore_session_from_store
                source = (self.options.session_store_source_directory
                          if self.options.session_store_source_directory is not None else self.options.cwd)
                recovery_guard = (self._prompt_send_guard if self._resume_from_store_bounded_snapshot[0] else None)
                recovery_loader = self._bounded_recovery_loader if recovery_guard is not None else None
                recovery_env = copy.deepcopy(self._configured_env())
                recovery_state = (self.options.resume, self.options.session_store_source_directory,
                                  self.options.cwd, self.options.session_store_archive_timeout,
                                  self.options.session_store_archive_max_bytes,
                                  self.options.session_store_archive_max_uncompressed_bytes,
                                  self.options.cli_path, self._cli_path, recovery_env, self._connection_generation)

                async def authorize_recovery() -> bool:
                    def current() -> bool:
                        try:
                            self._validate_unsupported_limits()
                            return (self._prompt_send_guard is recovery_guard and
                                    self._bounded_recovery_loader is recovery_loader and self._connected and
                                    not self._disconnecting and self._reader_error is None and
                                    recovery_state == (
                                        self.options.resume, self.options.session_store_source_directory,
                                        self.options.cwd, self.options.session_store_archive_timeout,
                                        self.options.session_store_archive_max_bytes,
                                        self.options.session_store_archive_max_uncompressed_bytes,
                                        self.options.cli_path, self._cli_path, self._configured_env(),
                                        self._connection_generation))
                        except Exception:
                            return False
                    return (current() and await recovery_guard() is True and current())

                if recovery_guard is not None and not await authorize_recovery():
                    raise PermissionError("Session recovery authorization changed")
                restore = _restore_session_from_store if recovery_guard is not None else restore_session_from_store
                self._restored_session_id = await restore(
                    self.options.session_store, self.options.resume, source,
                    target_directory=self.options.cwd or os.getcwd(), cli_path=self._cli_path,
                    env=recovery_env, timeout=self.options.session_store_archive_timeout,
                    max_bytes=self.options.session_store_archive_max_bytes,
                    max_uncompressed_bytes=self.options.session_store_archive_max_uncompressed_bytes,
                    bounded_mirror_read=self._resume_from_store_bounded_snapshot[0],
                    **({"before_import": authorize_recovery, "mirror_loader": recovery_loader}
                       if recovery_guard is not None else {}))
                self._validate_unsupported_limits()
                if recovery_guard is not None and not await authorize_recovery():
                    raise PermissionError("Session recovery authorization changed")
            resume_id = self._restored_session_id if self.options.resume_from_store else self.options.resume
            if self.options.continue_conversation:
                resume_id = (await self._find_latest_session(params["cwd"]))
            if self.options.fork_session and not resume_id:
                raise ProtocolError("No Kiro session is available to fork")
            method = "session/new"
            if resume_id:
                if self.options.fork_session:
                    self._require_session_capability("fork")
                    if not self.server_info.get("agentCapabilities", {}).get("loadSession", False):
                        raise ProtocolError("Kiro cannot load a forked session")
                    method = "session/fork"
                    if self.options.resume_session_at is not None:
                        capability = self.server_info.get("agentCapabilities", {}).get("sessionCapabilities", {}).get("fork", {})
                        if not _nullable_mapping(_nullable_mapping(capability.get("_meta")).get("kiro")).get("messageId"):
                            raise ProtocolError("Kiro does not advertise forking at a message")
                        params.setdefault("_meta", {}).setdefault("kiro", {})["messageId"] = self.options.resume_session_at
                    if self.options.resume_drops_turn is not None:
                        params.setdefault("_meta", {}).setdefault("kiro", {})[
                            "sdkResumeDropsTurn"] = self.options.resume_drops_turn
                else:
                    if not self.server_info.get("agentCapabilities", {}).get("loadSession", False):
                        raise ProtocolError("Kiro does not advertise session/load")
                    method = "session/load"
                params["sessionId"] = resume_id
            self._validate_unsupported_limits()
            load_guard = (self._prompt_send_guard if self._resume_from_store_bounded_snapshot[0] and
                          method == "session/load" else None)
            if load_guard is not None:
                if (await load_guard() is not True or self._prompt_send_guard is not load_guard or
                        not self._connected or self._disconnecting or self._reader_error is not None):
                    raise PermissionError("Session recovery authorization changed")
                self._validate_unsupported_limits()
            session = await self._request(method, params)
            if self.options.resume_drops_turn is not None:
                confirmation = _nullable_mapping(_nullable_mapping(session.get("_meta")).get("kiro"))
                if confirmation.get("sdkResumeDropsTurn") != self.options.resume_drops_turn:
                    raise ProtocolError("Kiro did not confirm resume_drops_turn validation")
            if method == "session/fork":
                fork_id = session.get("sessionId")
                if not fork_id:
                    raise ProtocolError("Kiro did not return a forked session ID")
                self.session_id = fork_id
                load_params = {"sessionId": fork_id, "cwd": params["cwd"], "mcpServers": servers}
                if self._policy_presets:
                    load_params["_meta"] = {"kiro": {"policyPreset": self._policy_presets}}
                if self.options.add_dirs:
                    load_params["additionalDirectories"] = self._additional_directories()
                try:
                    loaded = await self._request("session/load", load_params)
                except Exception as exc:
                    raise ProtocolError(f"Forked session {fork_id} could not be loaded") from exc
                session = {**session, **loaded, "sessionId": fork_id}
            self.session_info = session
            self._session_model = _selected_model(session) or self._configured_model() or ""
            self._current_mode = session.get("modes", {}).get("currentModeId", self._current_mode)
            self.session_id = session.get("sessionId", resume_id if method == "session/load" else None)
            if not self.session_id:
                raise ProtocolError("Kiro did not return a session ID")
            if self.session_id not in self._available_commands_by_session and "availableCommands" in session:
                self._available_commands_by_session[self.session_id] = _available_commands(session["availableCommands"])
            self._available_commands_by_session = {
                key: value for key, value in self._available_commands_by_session.items() if key == self.session_id}
            if self.options.session_id is not None and self.session_id != self.options.session_id:
                raise ProtocolError("Kiro did not use the requested session ID")
            if method == "session/new" and self._before_initial_prompt is not None:
                await self._before_initial_prompt(self.session_id)
            if method == "session/new" and self.options.agent_engine == "v3" and agent and self._current_mode != agent:
                raise ProtocolError("Kiro did not select the requested agent profile")
            if self._code_intelligence_enabled:
                status = await self._request("_kiro/codeIntelligence",
                                             {"sessionId": self.session_id, "subcommand": "status"})
                if not isinstance(status, dict) or status.get("success") is not True:
                    raise ProtocolError("Kiro did not confirm code intelligence is enabled")
            if self.options.session_store is not None:
                self._store_key = {"project_key": project_key_for_directory(params["cwd"]),
                                   "session_id": self.session_id}
                self._mirror_writer = MirrorWriter(self.options.session_store, self._store_key,
                                                   self.options.session_store_flush, self._mirror_failed,
                                                   continue_on_error=self.options.session_store_error_policy == "continue")
            # Kiro restores the saved profile on load, overriding launch/session
            # inputs. Reapply an explicit profile so temporary safety hooks and
            # tool restrictions are not silently lost on a resumed session.
            if method in ("session/load", "session/fork") and agent:
                await self.set_mode(agent)
            if self._permission_mode == "plan":
                if method in ("session/load", "session/fork"):
                    await self.set_mode("plan")
                elif self._current_mode != "plan":
                    raise ProtocolError("Kiro did not select plan mode")
            if self._startup_fallback is not None:
                primary, fallback = self._startup_fallback
                config_options = session.get("configOptions")
                if not isinstance(config_options, list):
                    raise ProtocolError("Kiro did not report model choices for startup fallback")
                model_options = next((item for item in config_options
                                      if isinstance(item, dict) and item.get("id") == "model"), None)
                choices = model_options.get("options") if isinstance(model_options, dict) else None
                if not isinstance(choices, list):
                    raise ProtocolError("Kiro did not report model choices for startup fallback")
                available = {item["value"] for item in choices
                             if isinstance(item, dict) and isinstance(item.get("value"), str)}
                selected = primary if primary in available else fallback if fallback in available else None
                if selected is None:
                    raise ProtocolError("Kiro did not report the primary or fallback model")
                try:
                    configured = await self.set_config_option("model", selected)
                except JSONRPCError:
                    # A server-rejected selection happens before any prompt is
                    # sent. A timeout or broken transport has an uncertain
                    # outcome and is deliberately not retried.
                    if selected != primary or fallback == primary or fallback not in available:
                        raise
                    selected = fallback
                    configured = await self.set_config_option("model", selected)
                if (_selected_model(configured) != selected):
                    raise ProtocolError("Kiro did not confirm the selected startup model")
            elif method in ("session/load", "session/fork") and requested_model:
                await self._apply_model(requested_model, persist_option=requested_option_model is not None)
            elif method == "session/new" and self.options.agent_engine == "v3" and requested_model:
                options = session.get("configOptions", [])
                confirmed = (any(option.get("id") == "model" and option.get("currentValue") == requested_model
                                 for option in options))
                if not confirmed:
                    await self._apply_model(requested_model, persist_option=requested_option_model is not None)
            if self._configured_thinking() is not None:
                expected = "on" if self._configured_thinking() else "off"
                configured = await self.set_config_option("thinking", expected)
                options = configured.get("configOptions", [])
                if not any(option.get("id") == "thinking" and option.get("currentValue") == expected for option in options):
                    raise ProtocolError("Kiro did not confirm the requested thinking setting")
            if self.options.agent_engine == "v3" and effort:
                configured = await self.set_config_option("effortLevel", effort)
                options = configured.get("configOptions", [])
                if (not isinstance(options, list) or not any(
                        isinstance(option, dict) and option.get("id") == "effortLevel" and
                        option.get("currentValue") == effort for option in options)):
                    raise ProtocolError("Kiro did not confirm the requested effort setting")
                if self._configured_thinking() is not None:
                    expected = "on" if self._configured_thinking() else "off"
                    if not any(option.get("id") == "thinking" and option.get("currentValue") == expected for option in options):
                        raise ProtocolError("Kiro changed the requested thinking setting while setting effort")
            self._validate_unsupported_limits()
            if self._effective_kiro_permissions() is not None:
                response = await self._request("_kiro/permissions/list", {"sessionId": self.session_id})
                actual = [r for r in response.get("rules", []) if r.get("scope") == "agent"]
                for expected in self._effective_kiro_permissions()["rules"]:
                    if not any(all(rule.get(key) == value for key, value in expected.items()) for rule in actual):
                        raise ProtocolError("Kiro did not register a requested agent permission rule")
            selected_mapped_agent = self._selected_agent()
            if selected_mapped_agent in self._mapped_agent_profiles:
                mapped_permissions = self._mapped_agent_profiles[selected_mapped_agent]
                if mapped_permissions is not None:
                    response = await self._request("_kiro/permissions/list", {"sessionId": self.session_id})
                    actual = [rule for rule in response.get("rules", []) if rule.get("scope") == "agent"]
                    if any(not any(all(rule.get(key) == value for key, value in expected.items()) for rule in actual)
                           for expected in mapped_permissions["rules"]):
                        raise ProtocolError("Kiro did not register a mapped agent permission rule")
                self._protected_mode = selected_mapped_agent
                if self._current_mode != selected_mapped_agent:
                    raise ProtocolError("Kiro did not confirm the mapped agent profile")
            if self._policy_presets:
                response = await self._request("_kiro/permissions/list", {"sessionId": self.session_id})
                rules = response.get("rules")
                if not isinstance(rules, list) or any(not any(
                    isinstance(rule, dict) and rule.get("scope") == "session" and
                    rule.get("source") == "preset:" + preset and rule.get("effect") == "allow" and
                    isinstance(rule.get("capability"), str) for rule in rules
                ) for preset in self._policy_presets):
                    raise ProtocolError("Kiro did not report every requested policy preset")
            if self.options.agent_engine == "v3" and (hook_command):
                hook_revision = self._hook_registry_revision.get(self.session_id, 0)
                hook_list = await self._request("_kiro/hooks/list", {"sessionId": self.session_id})
                changed = self._hook_registry_revision.get(self.session_id, 0) != hook_revision
                selected_hooks = self._hook_snapshots_by_session.get(self.session_id) if changed else hook_list.get("hooks")
                if hook_command and not self._native_hooks_active(selected_hooks):
                    raise ProtocolError("Kiro did not register the requested native hooks")
                hook_verified_revision = self._hook_registry_revision.get(self.session_id, 0)
            if self._sandbox_required:
                await self._require_sandbox()
            if self._agent_file and self._has_profile_restrictions():
                self._protected_mode = self._agent_file.stem
                if self._current_mode != self._protected_mode:
                    raise ProtocolError("Kiro did not confirm the restricted agent profile")
            if self._policy_presets and (not self._current_mode or self._current_mode == "plan"):
                raise ProtocolError("Kiro did not confirm a non-Plan mode for policy presets")
            if (self._native_hook_names and self._hook_registry_revision.get(self.session_id, 0) != hook_verified_revision and
                    not self._native_hooks_active(self._hook_snapshots_by_session.get(self.session_id))):
                raise ProtocolError("Kiro did not register the requested native hooks")
            self._guards_armed = True
            initial_guard = self._prompt_send_guard
            if initial_guard is not None:
                if (await initial_guard() is not True or self._prompt_send_guard is not initial_guard or
                        not self._connected or self._disconnecting or self._reader_error is not None):
                    raise ProtocolError("Session initialization authorization changed")
                self._validate_unsupported_limits()
            # session/load replays the old conversation before replying. Those
            # chunks must not be mistaken for the next prompt's response.
            if method in ("session/load", "session/fork"):
                while not self._events.empty():
                    self.history.append(self._events.get_nowait())
            selected_agent = self._selected_agent()
            definition = (self.options.agents or {}).get(selected_agent) if selected_agent else None
            agent_initial = (definition.initial_prompt if method == "session/new" and
                             isinstance(definition, AgentDefinition) else None)
            if agent_initial is not None:
                if self._before_initial_turn is not None:
                    await self._before_initial_turn(self.session_id)
                await self.query(agent_initial)
                self._initial_prompt_active = True
            if prompt is not None:
                await self.query(prompt)
        except BaseException:
            with contextlib.suppress(Exception):
                await self.disconnect()
            raise
        finally:
            self._initializing = False

    def _check_elicitation_reentry(self) -> None:
        if _SDK_HOOK_CONTEXT.get() is self:
            raise CLIConnectionError("SDK hooks cannot control their parent client")

    async def _send(self, obj: dict[str, Any]) -> None:
        self._check_elicitation_reentry()
        if (_FILE_CHANGED_CONTEXT.get() is self or self in _CLIENT_AUTH_CONTEXT.get() or
                asyncio.current_task() in self._client_auth_tasks):
            raise CLIConnectionError("FileChanged and authentication callbacks cannot control the client")
        if self._reader_error:
            raise CLIConnectionError("Kiro connection reader failed") from self._reader_error
        if not self._connected:
            raise CLIConnectionError("Kiro CLI is not connected")
        if self._transport is not None:
            if not self._transport.is_ready():
                raise CLIConnectionError("Custom transport is not ready")
            await self._transport.write(json.dumps(obj, ensure_ascii=False) + "\n")
            return
        if self._process is None or self._process.stdin is None or self._process.returncode is not None:
            raise CLIConnectionError("Kiro CLI is not connected")
        self._process.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        await self._process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any], sent: Optional[asyncio.Event] = None) -> Any:
        self._check_elicitation_reentry()
        if (_FILE_CHANGED_CONTEXT.get() is self or self in _CLIENT_AUTH_CONTEXT.get() or
                asyncio.current_task() in self._client_auth_tasks):
            raise CLIConnectionError("FileChanged and authentication callbacks cannot control the client")
        self._next_id += 1
        ident = self._next_id
        if method == "initialize":
            self._turn_wire_completed = False
        if method == "session/prompt":
            self._pending_prompt_ids.add(ident)
            self._turn_wire_completed = False
        future = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            if sent:
                sent.set()
            timeout = self.options.startup_timeout
            if method == "session/load" and self.options.load_timeout_ms is not None:
                timeout = self.options.load_timeout_ms / 1000
            if self._initializing and method != "session/prompt" and timeout is not None:
                try:
                    result = await asyncio.wait_for(future, timeout)
                except asyncio.TimeoutError as exc:
                    raise CLIConnectionError(f"Timed out during Kiro startup: {method}") from exc
            else:
                result = await future
            if method in {"initialize", "session/new", "session/load", "session/prompt",
                          "session/set_mode", "session/set_model", "session/set_config_option",
                          "_kiro/session/compact", "_kiro/permissions/list", "_kiro/hooks/list"} and not isinstance(result, dict):
                raise ProtocolError(f"Invalid object response for {method}")
            return result
        finally:
            if sent:
                sent.set()
            self._pending.pop(ident, None)
            self._pending_prompt_ids.discard(ident)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _drain_stderr(self) -> None:
        if self._client_auth_snapshot[0] == "client":
            while await self._process.stderr.read(4096):
                pass
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        callback = self.options.stderr
        while True:
            data = await self._process.stderr.read(4096)
            if not data:
                text = decoder.decode(b"", final=True)
                if callback and text:
                    try:
                        callback(text)
                    except Exception as exc:
                        await self._events.put(exc)
                return
            self._stderr.extend(data)
            if len(self._stderr) > 65536:
                del self._stderr[:-65536]
            text = decoder.decode(data)
            if callback and text:
                try:
                    callback(text)
                except Exception as exc:
                    callback = None
                    self._reader_error = exc
                    self._signal_message_activity()
                    for future in list(self._pending.values()):
                        if not future.done():
                            future.set_exception(exc)
                    await self._events.put(exc)

    async def _messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._transport is not None:
            async for message in self._transport.read_messages():
                yield message
            return
        process = self._process
        while True:
            read = asyncio.create_task(process.stdout.readline())
            try:
                while not read.done():
                    done, _ = await asyncio.wait((read,), timeout=0.1)
                    if not done and process.returncode is not None:
                        return
                line = read.result()
            except ValueError as exc:
                raise ProtocolError("Kiro ACP message exceeded max_buffer_size") from exc
            finally:
                if not read.done():
                    read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
                elif not read.cancelled():
                    read.exception()
            if not line:
                # EOF may precede the child-watcher's process_exited callback.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), 1)
                return
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                if self._client_auth_snapshot[0] != "client":
                    raise CLIJSONDecodeError(line.decode(errors="replace"), exc) from exc
            else:
                yield message
                continue
            line = b""
            raise ProtocolError("Kiro ACP returned invalid JSON")

    async def _read_loop(self) -> None:
        try:
            async for msg in self._messages():
                if not isinstance(msg, dict):
                    raise ProtocolError("Invalid JSON-RPC message")
                if self._incoming_message_limit is not None:
                    self._incoming_message_count += 1
                    self._incoming_message_bytes += len(json.dumps(msg, ensure_ascii=True).encode("ascii"))
                    if (self._incoming_message_count > self._incoming_message_limit[0] or
                            self._incoming_message_bytes > self._incoming_message_limit[1]):
                        raise ProtocolError("Kiro session operation exceeded its incoming message limit")
                if "method" in msg and "id" in msg:
                    ident = msg["id"]
                    if isinstance(ident, bool) or not isinstance(ident, (str, int)) or ident in self._inbound_requests:
                        raise ProtocolError("Invalid or duplicate inbound request ID")
                    if (msg["method"] in (NATIVE_HOOK_METHOD,) and
                            sum(item["method"] in (NATIVE_HOOK_METHOD,)
                                for item in self._inbound_requests.values()) >= 128):
                        await self._send({"jsonrpc": "2.0", "id": ident,
                            "error": {"code": -32000, "message": "Native ACP hook capacity exhausted"}})
                        continue
                    if (msg["method"] == "_kiro/auth/getAccessToken" and
                            sum(item["method"] == "_kiro/auth/getAccessToken"
                                for item in self._inbound_requests.values()) >= 8):
                        error = ProtocolError("Too many concurrent Kiro authentication requests")
                        self._reader_error = error
                        await self._abort_for_guard(force=True)
                        raise error
                    params = msg.get("params")
                    request_session = params.get("sessionId") if isinstance(params, dict) else None
                    request_turn = self._turn if request_session in (None, self.session_id) else None
                    record = {"id": ident, "method": msg["method"], "turn": request_turn,
                              "session_id": request_session,
                              "cancelled": False, "responded": False}
                    task = asyncio.create_task(self._handle_request(msg, record))
                    record["task"] = task
                    self._inbound_requests[ident] = record
                    self._handler_tasks.add(task)
                    task.add_done_callback(lambda completed, record=record:
                                           self._inbound_finished(record, completed))
                    if self._disconnecting or (
                        self._interrupt_requested and record["turn"] is self._turn and self._turn is not None
                    ) or record.get("runtime_hook_key") in self._runtime_cancelled_hooks:
                        response = self._mark_inbound_cancelled(record)
                        if response is not None:
                            await self._send(response)
                            self._retire_inbound(record)
                elif "method" in msg:
                    if msg["method"] == "_kiro/auth/getAccessToken":
                        raise ProtocolError("Invalid Kiro authentication notification")
                    if msg["method"] == "_kiro/sdk/cancelHook":
                        params = _nullable_mapping(msg.get("params"))
                        key = (params.get("sessionId"), params.get("requestId"), params.get("callId"))
                        if (any(type(value) is not str or not value or len(value) > 256 for value in key) or
                                key[0] != self.session_id):
                            continue
                        if (key[1] != self._active_runtime_request_id and
                                not any(record.get("runtime_hook_key") == key
                                        for record in self._inbound_requests.values())):
                            continue
                        if key not in self._runtime_cancelled_hooks:
                            if len(self._runtime_cancelled_hooks) >= 4096:
                                raise ProtocolError("Runtime hook cancellation capacity exhausted")
                            self._runtime_cancelled_hooks.add(key)
                        for record in tuple(self._inbound_requests.values()):
                            if record.get("runtime_hook_key") != key:
                                continue
                            response = self._mark_inbound_cancelled(record)
                            if response is not None:
                                await self._send(response)
                                self._retire_inbound(record)
                    elif msg["method"] == "$/cancel_request":
                        params = msg.get("params")
                        ident = params.get("id") if isinstance(params, dict) else None
                        record = self._inbound_requests.get(ident) if isinstance(ident, (str, int)) and not isinstance(ident, bool) else None
                        if record is not None:
                            response = self._mark_inbound_cancelled(record)
                            if response is not None:
                                await self._send(response)
                                self._retire_inbound(record)
                    elif msg["method"] == "session/update":
                        params = _nullable_mapping(msg.get("params"))
                        if self.session_id is None or params.get("sessionId") == self.session_id:
                            update = _nullable_mapping(params.get("update"))
                            if update.get("sessionUpdate") == "available_commands_update":
                                scope = params.get("sessionId")
                                if (isinstance(scope, str) and 0 < len(scope) <= 1024 and
                                        (scope in self._available_commands_by_session or
                                         len(self._available_commands_by_session) < 16)):
                                    self._available_commands_by_session[scope] = _available_commands(
                                        update.get("availableCommands"))
                            if update.get("sessionUpdate") == "current_mode_update":
                                self._mode_updates += 1
                                self._current_mode = update.get("currentModeId", self._current_mode)
                                if self._guards_armed and self._policy_presets and self._current_mode == "plan":
                                    error = ProtocolError("Kiro entered Plan mode with active policy presets")
                                    self._reader_error = error
                                    await self._abort_for_guard()
                                    raise error
                                if self._guards_armed and self._protected_mode and self._current_mode != self._protected_mode:
                                    error = ProtocolError("Kiro changed the restricted agent profile")
                                    self._reader_error = error
                                    await self._abort_for_guard()
                                    raise error
                                if (self._guards_armed and isinstance(self._current_mode, str) and
                                        self._current_mode in self._mapped_agent_profiles and
                                        self._current_mode != self._protected_mode):
                                    error = ProtocolError("Kiro switched to an unverified mapped agent profile")
                                    self._reader_error = error
                                    await self._abort_for_guard()
                                    raise error
                            mirror_key = (self._turn_mirror_prompt_id if self._turn is not None and
                                          not self._turn.done() else None)
                            if mirror_key is not None:
                                await self._mirror("update", update, prompt_key=mirror_key)
                            else:
                                await self._mirror("update", update)
                            meta = _nullable_mapping(_nullable_mapping(update.get("_meta")).get("kiro"))
                            if (update.get("sessionUpdate") in ("tool_call", "tool_call_update") and
                                    meta.get("kind") == "agent-subtask"):
                                scope, agent_id, tool_id = params.get("sessionId"), meta.get("agentSubtaskId"), update.get("toolCallId")
                                if all(isinstance(value, str) and value for value in (scope, agent_id, tool_id)):
                                    self._subagent_parents_by_session.setdefault(scope, {})[agent_id] = tool_id
                                    self._remember_subagent_type(scope, agent_id, tool_id, update)
                            if update.get("sessionUpdate") == "session_info_update" and meta.get("kind") == "context_usage":
                                scope = params.get("sessionId")
                                if scope is None or isinstance(scope, str):
                                    self._context_metadata_by_session[scope or None] = copy.deepcopy(update)
                            if (self._turn_mirror_prompt_id is not None and
                                    update.get("sessionUpdate") == "session_info_update" and
                                    meta.get("kind") == "user_message_id_assigned" and
                                    self._turn is not None and not self._turn.done()):
                                update = dict(update)
                                update["_sdk_replay_turn"] = self._turn
                            turn_content = update.get("sessionUpdate") in (
                                "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan")
                            if (not turn_content or
                                    (self._turn is not None and not self._turn.done() and not self._turn_wire_completed) or
                                    (self._initializing and self._turn is None and not self._turn_wire_completed)):
                                await self._events.put(update)
                    else:
                        if msg["method"] == "_kiro.dev/metadata":
                            metadata = msg.get("params", {})
                            if isinstance(metadata, dict):
                                scope = metadata.get("sessionId")
                                if scope is None or isinstance(scope, str):
                                    self._context_metadata_by_session[scope or None] = copy.deepcopy(metadata)
                        if msg["method"] in ("_kiro/mcp/status", "_kiro.dev/mcp/server_initialized",
                                            "_kiro.dev/mcp/server_init_failure", "_kiro.dev/mcp/oauth_request"):
                            self._mcp_status_tracker.update(msg["method"], msg.get("params", {}))
                        if (self._native_hook_names) and msg["method"] == "_kiro/hooks/didChange":
                            params = _nullable_mapping(msg.get("params"))
                            if isinstance(params, dict):
                                scope, hooks = params.get("sessionId"), params.get("hooks")
                                if isinstance(scope, str) and scope:
                                    self._hook_registry_revision[scope] = self._hook_registry_revision.get(scope, 0) + 1
                                    self._hook_snapshots_by_session[scope] = copy.deepcopy(hooks)
                                    if (self._guards_armed and scope == self.session_id and self._native_hook_names
                                            and not self._native_hooks_active(hooks)):
                                        error = ProtocolError("Kiro native hooks stopped being active")
                                        self._reader_error = error
                                        await self._abort_for_guard()
                                        raise error
                        if msg["method"] == "_kiro/sandbox/status":
                            status = _nullable_mapping(msg.get("params"))
                            if status.get("sessionId") in (None, self.session_id):
                                self._sandbox_status = status
                                self._sandbox_event.set()
                                if self._guards_armed and self._sandbox_required and status.get("status") != "active":
                                    error = ProtocolError("Kiro command sandbox stopped being active")
                                    self._reader_error = error
                                    await self._abort_for_guard()
                                    raise error
                        await self._mirror("notification", {"method": msg["method"], "params": msg.get("params", {})})
                        await self._events.put({"sessionUpdate": "notification", "method": msg["method"], "params": msg.get("params", {})})
                elif msg.get("id") in self._pending:
                    future = self._pending[msg["id"]]
                    if msg["id"] in self._pending_prompt_ids:
                        self._turn_wire_completed = True
                    if not future.done():
                        if "error" in msg:
                            error = msg["error"]
                            if not isinstance(error, dict):
                                future.set_exception(ProtocolError("Invalid Kiro error response"))
                                continue
                            if self._client_auth_snapshot[0] == "client":
                                code = error.get("code") if isinstance(error, dict) else None
                                future.set_exception(JSONRPCError({
                                    "code": code if type(code) is int else -32603,
                                    "message": "Kiro request failed"}))
                            else:
                                future.set_exception(JSONRPCError(error) if isinstance(error, dict) else ProtocolError(str(error)))
                        else:
                            future.set_result(msg["result"] if "result" in msg else {})
            stderr = self._stderr.decode(errors="replace")
            code = self._process.returncode if self._process is not None else None
            raise ProcessError("Kiro transport closed", exit_code=code, stderr=stderr)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._reader_error is None:
                self._reader_error = exc
            exc = self._reader_error
            self._signal_message_activity()
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(exc)
            await self._events.put(exc)

    def _handler_finished(self, task: asyncio.Task) -> None:
        self._handler_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _inbound_finished(self, record: dict[str, Any], task: asyncio.Task) -> None:
        key = record.get("mcp_permission_key")
        if key is not None and self._mcp_permission_records.get(key) is record:
            self._mcp_permission_records.pop(key, None)
        self._retire_inbound(record)
        self._handler_finished(task)

    def _retire_inbound(self, record: dict[str, Any]) -> None:
        if self._inbound_requests.get(record["id"]) is record:
            self._inbound_requests.pop(record["id"], None)

    async def _deliver_native_hook_message(self, event: str, payload: dict[str, Any], message: str, *,
                                           timeout: Optional[float] = None) -> None:
        self._validate_unsupported_limits()
        callback = self._native_hook_system_message_callback
        session_id = payload.get("session_id")
        turn = self._turn
        if (callback is None or not isinstance(session_id, str) or not session_id or
                session_id != self.session_id or not self._connected or self._disconnecting or
                self._interrupt_requested or (turn is not None and turn.done())):
            raise ProtocolError("Native hook warning requires a callback and a matching connected session")
        data = {"hook_event_name": event, "session_id": session_id, "message": message}
        if event in ("SubagentStart", "SubagentStop") and _valid_reported_agent_id(payload.get("agent_id")):
            data["agent_id"] = payload["agent_id"]
        async def deliver(_payload: Any, _tool_id: Any, _context: Any) -> Any:
            return await _resolve_callback(callback(data))
        task = asyncio.create_task(self._invoke_failure_aware_hook(
            deliver, {}, None, _NATIVE_HOOK_MESSAGE_TIMEOUT if timeout is None else timeout))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_finished)
        try:
            waiting = (task, turn) if turn is not None else (task,)
            await asyncio.wait(waiting, timeout=_NATIVE_HOOK_MESSAGE_TIMEOUT if timeout is None else timeout,
                               return_when=asyncio.FIRST_COMPLETED)
            if turn is not None and (turn is not self._turn or turn.done()):
                raise ProtocolError("Turn ended during native hook warning callback")
            if not task.done():
                raise asyncio.TimeoutError("Native hook warning callback timed out")
            if task.cancelled():
                raise ProtocolError("Native hook warning callback was cancelled")
            task.result()
            self._validate_unsupported_limits()
            if (not self._connected or self._disconnecting or self._interrupt_requested or
                    self.session_id != session_id or
                    (turn is not None and (turn is not self._turn or turn.done()))):
                raise ProtocolError("Connection changed during native hook warning callback")
        finally:
            if not task.done():
                task.cancel()

    async def _deliver_native_stop_warning(self, payload: dict[str, Any], message: Any, *,
                                           timeout: Optional[float] = None) -> None:
        validated: list[str] = []
        _append_hook_warning(validated, "Stop", message)
        if not validated:
            return
        if not self.session_id or payload.get("session_id") != self.session_id:
            raise ProtocolError("Native Stop warning belongs to a different session")
        size = len(validated[0].encode("utf-8"))
        if self._native_stop_warning_count >= 32 or self._native_stop_warning_bytes + size > 65536:
            raise ProtocolError("Stop system messages exceed the limit")
        self._native_stop_warning_count += 1
        self._native_stop_warning_bytes += size
        await self._deliver_native_hook_message("Stop", payload, validated[0], timeout=timeout)

    def _queue_inbound_hook_warnings(self, record: dict[str, Any], event: str, messages: list[str]) -> None:
        turn = record["turn"]
        if (not messages or record["cancelled"] or turn is None or turn is not self._turn or turn.done() or
                self._interrupt_requested or record["session_id"] != self.session_id):
            return
        for message in messages:
            self._events.put_nowait({"sessionUpdate": "sdk_hook_warning", "turn": turn,
                                     "event": event, "message": message, "session_id": self.session_id})

    @staticmethod
    def _inbound_cancel_response(record: dict[str, Any]) -> dict[str, Any]:
        method = record["method"]
        if method == "session/request_permission":
            result = {"outcome": {"outcome": "cancelled"}}
        elif method == "_kiro/userInput":
            result = {"action": "dismissed"}
        elif method == "_kiro/mcp/elicitation":
            result = {"action": "cancel"}
        else:
            return {"jsonrpc": "2.0", "id": record["id"],
                    "error": {"code": -32800, "message": "Request cancelled"}}
        return {"jsonrpc": "2.0", "id": record["id"], "result": result}

    @classmethod
    def _mark_inbound_cancelled(cls, record: dict[str, Any]) -> Optional[dict[str, Any]]:
        if record["responded"] or record["cancelled"]:
            return None
        record["cancelled"] = True
        record["responded"] = True
        record["task"].cancel()
        return cls._inbound_cancel_response(record)

    def _native_hooks_active(self, hooks: Any) -> bool:
        if self._native_hook_runtime is not None:
            return self._native_hook_runtime.hooks_active(hooks)
        if not isinstance(hooks, list):
            return False
        enabled = set()
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            action, metadata = hook.get("action"), hook.get("_meta")
            if (isinstance(action, dict) and action.get("command") == self._native_hook_command and
                    isinstance(metadata, dict) and metadata.get("enabled") is True):
                name = hook.get("name")
                if isinstance(name, str):
                    enabled.add(name)
        return self._native_hook_names.issubset(enabled)

    async def _abort_for_guard(self, *, force: bool = False) -> None:
        """Stop owned work after a reported guard failure or turn timeout."""
        if self._transport is not None:
            async def attempt(operation: Any, timeout: float) -> bool:
                task = asyncio.create_task(operation)
                task.add_done_callback(self._handler_finished)
                try:
                    done, _ = await asyncio.wait((task,), timeout=timeout)
                    if task not in done:
                        return False
                    await task
                    return True
                finally:
                    if not task.done():
                        task.cancel()

            try:
                if self.session_id:
                    message = {"jsonrpc": "2.0", "method": "session/cancel",
                               "params": {"sessionId": self.session_id}}
                    await attempt(self._transport.write(json.dumps(message) + "\n"), 1)
            except Exception:
                pass
            try:
                self._transport_closed = await attempt(self._transport.close(), 3)
            except Exception:
                pass
        elif self._process is not None and self._process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(self._process.pid, signal.SIGKILL if force else signal.SIGTERM)
                elif force:
                    self._process.kill()
                else:
                    self._process.terminate()
            except ProcessLookupError:
                pass

    def _has_profile_restrictions(self) -> bool:
        return (self._effective_kiro_permissions() is not None or
                self.options.tools is not None or bool(self._native_hooks))

    @staticmethod
    def _snapshot_edit_directory_roots(roots: list[str]) -> tuple[tuple[str, int, int], ...]:
        identities = []
        for root in roots:
            try:
                path = Path(root)
                info = path.stat()
                if path.resolve(strict=True) != path or not path.is_dir():
                    raise ValueError("Additional edit roots must be existing directories without symlink components")
                identities.append((root, info.st_dev, info.st_ino))
            except (OSError, RuntimeError) as exc:
                raise ValueError("Additional edit roots must be existing directories without symlink components") from exc
        return tuple(identities)

    @staticmethod
    def _notification_is_subagent(params: Any) -> bool:
        if not isinstance(params, dict):
            return False
        candidates = [params]
        for key in ("toolCall", "elicitation"):
            if isinstance(params.get(key), dict):
                candidates.append(params[key])
        for candidate in candidates:
            metadata = _tool_kiro_metadata(candidate)
            if metadata is not None and ("agentSubtaskId" in metadata or
                                         metadata.get("kind") == "agent-subtask"):
                return True
        return False

    async def _handle_client_auth_request(self, msg: dict[str, Any], record: dict[str, Any]) -> None:
        async def error() -> None:
            if record["cancelled"] or record["responded"]:
                return
            record["responded"] = True
            await self._send({"jsonrpc": "2.0", "id": record["id"],
                              "error": {"code": -32603, "message": "Kiro client authentication unavailable"}})
            self._retire_inbound(record)

        if (self._client_auth_snapshot[0] != "client" or type(msg.get("params")) is not dict or
                msg["params"] != {} or self._client_auth_pending >= 8):
            await error()
            return
        startup = self._initializing or self._client_auth_control_active
        turn, session_id = record["turn"], self.session_id
        idle = not startup and (turn is None or turn.done())
        if (not startup and (
                not session_id or turn is not self._turn or
                (idle and self._client_auth_snapshot[3] != "authorized") or
                (not idle and self._interrupt_requested))):
            await error()
            return
        now = time.monotonic()
        while self._client_auth_request_times and self._client_auth_request_times[0] <= now - 60:
            self._client_auth_request_times.popleft()
        if len(self._client_auth_request_times) >= 32:
            await error()
            return
        self._client_auth_request_times.append(now)
        self._client_auth_pending += 1
        generation = self._connection_generation
        shutdown_revision = self._client_auth_shutdown_revision
        provider = self._client_auth_snapshot[1]
        guard = (self._prompt_send_guard if startup else
                 self._client_auth_snapshot[4] if idle else self._interaction_guard)
        factory_guard = self._client_auth_factory_guard
        deadline = asyncio.get_running_loop().time() + self._client_auth_snapshot[2]
        acquired = False
        result: Optional[dict[str, str]] = None

        def current() -> bool:
            if (record["cancelled"] or record["responded"] or not self._connected or self._disconnecting or
                    self._reader_error is not None or self._connection_generation != generation or
                    self._client_auth_shutdown_revision != shutdown_revision or
                    self._client_auth_factory_guard is not factory_guard):
                return False
            if startup:
                if not (self._initializing or self._client_auth_control_active) or self._prompt_send_guard is not guard:
                    return False
            elif idle:
                if (self._initializing or not session_id or self.session_id != session_id or
                        self._turn is not turn or (turn is not None and not turn.done()) or
                        self._query_lock.locked() or self.options.kiro_idle_token_authorizer is not guard):
                    return False
            elif (not session_id or self.session_id != session_id or self._turn is not turn or
                  turn is None or turn.done() or self._interrupt_requested or self._interaction_guard is not guard):
                return False
            if guard is None and self._require_client_auth_authorization:
                return False
            try:
                return factory_guard is None or factory_guard() is True
            except Exception:
                return False

        async def run(operation: Callable[[], Awaitable[Any]], limit: float) -> Any:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Kiro client authentication timed out")
            async def invoke() -> Any:
                token = _CLIENT_AUTH_CONTEXT.set((*_CLIENT_AUTH_CONTEXT.get(), self, *self._client_auth_ancestors))
                try:
                    try:
                        return True, await operation()
                    except asyncio.CancelledError:
                        raise
                    except BaseException:
                        return False, None
                finally:
                    _CLIENT_AUTH_CONTEXT.reset(token)
            task = asyncio.ensure_future(invoke())
            owners = (self, *self._client_auth_ancestors)
            for owner in owners:
                owner._client_auth_tasks.add(task)
            def finished(completed: asyncio.Future[Any]) -> None:
                for owner in owners:
                    owner._client_auth_tasks.discard(completed)
                if not completed.cancelled():
                    completed.exception()
            task.add_done_callback(finished)
            try:
                done, _ = await asyncio.wait((task,), timeout=min(limit, remaining))
                if not done or task.cancelled():
                    raise TimeoutError("Kiro client authentication timed out")
                succeeded, value = task.result()
                if not succeeded:
                    raise RuntimeError("Kiro client authentication callback failed")
                return value
            finally:
                if not task.done():
                    task.cancel()

        async def check() -> None:
            self._validate_unsupported_limits()
            if not current():
                raise PermissionError("Kiro client authentication changed")
            if guard is not None:
                if await run(guard, 5) is not True or not current():
                    raise PermissionError("Kiro client authentication changed")
                self._validate_unsupported_limits()

        try:
            await asyncio.wait_for(self._client_auth_lock.acquire(), timeout=self._client_auth_snapshot[2])
            acquired = True
            if any(not task.done() for task in self._client_auth_tasks):
                raise RuntimeError("Kiro client authentication is still pending")
            await check()
            value = await run(provider, self._client_auth_snapshot[2])
            result = validate_client_token(value)
            del value
            await check()
            if not current():
                raise PermissionError("Kiro client authentication changed")
            record["responded"] = True
            await self._send({"jsonrpc": "2.0", "id": record["id"], "result": result})
            self._retire_inbound(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            await error()
        finally:
            if result is not None:
                result.clear()
            if acquired:
                self._client_auth_lock.release()
            self._client_auth_pending -= 1

    def _verify_native_acp_profile(self) -> None:
        if self._agent_file is not None:
            path, identity = self._agent_file, self._agent_file_identity
        else:
            selected = self._selected_agent()
            owned = next((item for item in self._created_agents if item[0].stem == selected), None)
            if owned is None:
                raise ProtocolError("Native ACP hook profile is not SDK-owned")
            path, *identity = owned
        if identity is None:
            raise ProtocolError("Native ACP hook profile identity is missing")
        inode, size, digest = identity
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            content = stream.read(size + 1)
        if (not stat.S_ISREG(info.st_mode) or info.st_ino != inode or info.st_size != size or
                hashlib.sha256(content).digest() != digest):
            raise ProtocolError("Native ACP hook profile changed")

    def _runtime_native_allow_permitted(self) -> bool:
        """A native hook grant must not skip an independent SDK permission policy."""
        mode = self._permission_mode
        return (mode == "ask" and self._current_mode != "plan" and self._effective_can_use_tool() is None and
                not (self._sdk_pretool_mode == "permission" and
                     (self._sdk_hooks.get("PreToolUse"))))

    async def _invoke_native_callback(self, invoke: Callable[[], Awaitable[Any]]) -> Any:
        if self._native_callback_slots is None:
            self._native_callback_slots = asyncio.Semaphore(32)
        async with self._native_callback_slots:
            return await invoke()

    async def _handle_native_acp_hook(self, msg: dict[str, Any], record: dict[str, Any]) -> None:
        runtime, dispatcher = self._native_hook_runtime, self._native_hook_bridge
        task = None
        try:
            if (runtime is None or dispatcher is None or self._native_hook_bridge_mode != "runtime-acp" or
                    len(self._native_acp_dispatch_tasks) >= 128):
                raise ProtocolError("Native ACP hook unavailable")
            payload = runtime.validate(msg.get("params"), self.session_id)
            self._verify_native_acp_profile()
            generation = self._connection_generation
            guard = self._native_failure_guard()

            async def authorize() -> bool:
                if (runtime is not self._native_hook_runtime or generation != self._connection_generation or
                        record["cancelled"] or record["responded"]):
                    raise ProtocolError("Native ACP hook request expired")
                self._verify_native_acp_profile()
                result = await guard()
                if (runtime is not self._native_hook_runtime or generation != self._connection_generation or
                        record["cancelled"] or record["responded"]):
                    raise ProtocolError("Native ACP hook request expired")
                self._verify_native_acp_profile()
                return result

            async def invoke() -> dict[str, Any]:
                await authorize()
                response = await dispatcher.dispatch(payload, authorize=authorize,
                    input_writable=(self.options.runtime_compat_mode == "v3" and
                                    msg["params"].get("toolInputWritable") is True),
                    allow_once_permitted=self._runtime_native_allow_permitted())
                await authorize()
                if response.get("allowOnce") is True and not self._runtime_native_allow_permitted():
                    raise ProtocolError("Native hook approval conflicts with SDK permission policy")
                return command_result(payload, response)

            task = asyncio.create_task(self._invoke_native_callback(invoke))
            self._native_acp_dispatch_tasks.add(task)
            def finished(done: asyncio.Task) -> None:
                self._native_acp_dispatch_tasks.discard(done)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
            # Reply before the runtime's own deadline; do not wait indefinitely
            # for a Python callback that ignores cancellation.
            timeout = max(.05, msg["params"]["timeoutMs"] / 1000 - .25)
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done:
                raise ProtocolError("Native ACP hook timed out")
            result = task.result()
            if not record["cancelled"] and not record["responded"]:
                record["responded"] = True
                await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
                self._retire_inbound(record)
        except (Exception, asyncio.CancelledError):
            if task is not None and not task.done():
                task.cancel()
            if not record["responded"]:
                record["responded"] = True
                with contextlib.suppress(Exception):
                    await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                      "error": {"code": -32800, "message": "Native hook unavailable"}})
                self._retire_inbound(record)
            # Kiro's native executor historically fails open on some errors.
            # Stop this owned harness on invalid origin, deadline or lost authority.
            if not self._disconnecting and not record["cancelled"]:
                self._reader_error = ProtocolError("Native ACP hook failed or authorization changed")
                await self._abort_for_guard(force=True)

    async def _handle_request(self, msg: dict[str, Any], record: dict[str, Any]) -> None:
        if msg.get("method") == NATIVE_HOOK_METHOD:
            await self._handle_native_acp_hook(msg, record)
            return
        if msg.get("method") == "_kiro/auth/getAccessToken":
            await self._handle_client_auth_request(msg, record)
            return
        interaction_guard = self._interaction_guard
        elicitation_deadline = asyncio.get_running_loop().time() + 300.0
        elicitation_generation, elicitation_session, elicitation_turn = (
            self._connection_generation, self.session_id, record["turn"])

        def elicitation_remaining() -> float:
            remaining = elicitation_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProtocolError("MCP elicitation deadline expired")
            return remaining

        def validate_elicitation_scope() -> None:
            self._validate_unsupported_limits()
            elicitation_remaining()
            if (not self._connected or self._disconnecting or self._reader_error is not None or
                    self._connection_generation != elicitation_generation or self.session_id != elicitation_session or
                    not elicitation_session or record["session_id"] != elicitation_session or
                    elicitation_turn is None or self._turn is not elicitation_turn or elicitation_turn.done() or
                    self._interrupt_requested or record["cancelled"] or record["responded"] or
                    self._interaction_guard is not interaction_guard):
                raise ProtocolError("MCP elicitation scope expired")

        async def authorize_interaction() -> bool:
            return interaction_guard is None or await interaction_guard() is True

        async def respond(payload: dict[str, Any]) -> bool:
            if record["cancelled"] or record["responded"]:
                return False
            if "result" in payload:
                if msg["method"] == "session/request_permission":
                    self._validate_hook_failure_snapshot()
                if (interaction_guard is not None and msg["method"] in (
                        "session/request_permission", "_kiro/userInput", "_kiro/mcp/elicitation")):
                    if (self._interaction_guard is not interaction_guard or
                            await authorize_interaction() is not True or self._interaction_guard is not interaction_guard):
                        raise ProtocolError("Interaction authorization changed")
                    if record["cancelled"] or record["responded"]:
                        return False
                    if msg["method"] == "session/request_permission":
                        self._validate_hook_failure_snapshot()
            turn = record["turn"]
            if turn is not None and (turn is not self._turn or turn.done() or self._interrupt_requested):
                record["cancelled"] = True
                record["responded"] = True
                await self._send(self._inbound_cancel_response(record))
                self._retire_inbound(record)
                return False
            record["responded"] = True
            await self._send(payload)
            self._retire_inbound(record)
            return True

        try:
            if (interaction_guard is not None and msg["method"] in (
                    "session/request_permission", "_kiro/userInput", "_kiro/mcp/elicitation") and
                    (not self._connected or self._disconnecting or not self.session_id or
                     record["session_id"] != self.session_id or record["turn"] is None or
                     record["turn"] is not self._turn or record["turn"].done() or self._interrupt_requested)):
                record["responded"] = True
                await self._send(self._inbound_cancel_response(record))
                self._retire_inbound(record)
                return
            if (interaction_guard is not None and msg["method"] in (
                    "session/request_permission", "_kiro/userInput", "_kiro/mcp/elicitation")):
                if (self._interaction_guard is not interaction_guard or
                        await authorize_interaction() is not True or self._interaction_guard is not interaction_guard):
                    raise ProtocolError("Interaction authorization changed")
                if record["cancelled"] or record["responded"]:
                    return
            if msg["method"] == "_kiro/userInput":
                callback = self.options.on_user_input
                if callback is None:
                    result = {"action": "dismissed"}
                else:
                    params = msg.get("params")
                    if (not isinstance(params, dict) or not isinstance(params.get("question"), str) or
                            not isinstance(params.get("options"), list)):
                        raise ProtocolError("Invalid Kiro user input request")
                    answer = await _resolve_callback(callback(copy.deepcopy(params)))
                    if answer is None:
                        result = {"action": "dismissed"}
                    elif isinstance(answer, str) and answer.strip():
                        result = {"action": "answered", "answer": answer}
                    else:
                        raise ProtocolError("User input callback must return a nonempty string or None")
                await respond({"jsonrpc": "2.0", "id": msg["id"], "result": result})
                return
            if msg["method"] == "_kiro/mcp/elicitation":
                callback = self.options.on_mcp_elicitation
                if callback is None:
                    result = {"action": "cancel"}
                else:
                    params = msg.get("params")
                    if not isinstance(params, dict) or not isinstance(params.get("elicitation"), dict):
                        raise ProtocolError("Invalid Kiro MCP elicitation request")
                    result = await _resolve_callback(callback(copy.deepcopy(params)))
                    if (not isinstance(result, dict) or result.get("action") not in ("accept", "decline", "cancel") or
                            ("content" in result and result["content"] is not None and
                             not isinstance(result["content"], dict))):
                        raise ProtocolError("MCP elicitation callback must return an action and optional content")
                    try:
                        json.dumps(result, allow_nan=False)
                    except (TypeError, ValueError) as exc:
                        raise ProtocolError("MCP elicitation response must be JSON-serializable") from exc
                await respond({"jsonrpc": "2.0", "id": msg["id"], "result": result})
                return
            if msg["method"] != "session/request_permission":
                await respond({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not supported"}})
                return
            if (self._mapped_tool_permissions is not None or self._mapped_agent_profiles):
                self._validate_unsupported_limits()
            params = _nullable_mapping(msg.get("params"))
            tool = params.get("toolCall", {})
            if not isinstance(tool, dict):
                raise ProtocolError("Invalid Kiro permission tool call")
            options = params.get("options", [])
            type(tool) is dict and "rawInput" not in tool
            original_input = copy.deepcopy(tool.get("rawInput") or {})
            agent_id = _reported_tool_agent_id(params, tool)
            hook_fields: dict[str, Any] = {"tool_name": tool.get("title", ""), "tool_input": original_input}
            if type(tool.get("rawInput")) is not dict:
                hook_fields["kiro_input_unavailable"] = True
            request_session = params.get("sessionId")
            if isinstance(request_session, str) and request_session:
                hook_fields["session_id"] = request_session
            if agent_id is not None:
                hook_fields["agent_id"] = agent_id
                agent_type = (self._reported_subagent_type(request_session, agent_id)
                              if record["turn"] is not None and record["turn"] is self._turn and
                              not record["turn"].done() and not self._interrupt_requested else None)
                if agent_type is not None:
                    hook_fields["agent_type"] = agent_type
                    hook_fields["kiro_observed_agent_type"] = True
            else:
                agent_type = None
            permission_turn = record["turn"]
            permission_mode = (self._permission_mode)
            allow = permission_mode == "trustAll"
            explicit_option = None
            explicit_consent = None
            tool.get("title")
            callback_deny_reason = ""
            edited_command = None
            interrupt = False
            hook_permissions: list[str] = []
            hook_reasons: list[tuple[str, str]] = []
            hook_inputs: list[dict[str, Any]] = []
            pretool_warnings: list[str] = []
            hook_failure_blocks: list[bool] = []
            blocked = False
            if self._sdk_pretool_mode == "permission":
                blocked = await self._run_hooks("PreToolUse", hook_fields, tool.get("toolCallId"),
                    permission_decisions=hook_permissions, permission_reasons=hook_reasons,
                    permission_inputs=hook_inputs, hook_system_messages=pretool_warnings,
                    failure_blocks=hook_failure_blocks)
            if "deny" in hook_permissions:
                blocked = True
            elif "ask" in hook_permissions:
                allow = False
            elif "allow" in hook_permissions:
                allow = True
            self._queue_inbound_hook_warnings(record, "PreToolUse", pretool_warnings)
            named_server_enabled = True
            if not blocked and hook_inputs:
                edits = [self._validate_shell_edit(params, original_input, value)
                         for value in hook_inputs]
                changes = {value for value in edits if value is not None}
                if len(changes) > 1:
                    raise ProtocolError("Conflicting hook input edits are not supported")
                if changes and self._effective_can_use_tool() and permission_mode != "denyUnlisted":
                    raise ProtocolError("Hook input edits cannot be combined with can_use_tool")
                edited_command = next(iter(changes), None)
            def permission_context() -> ToolPermissionContext:
                return ToolPermissionContext(
                    session_id=params.get("sessionId", ""), options=copy.deepcopy(options),
                    tool_call=copy.deepcopy(tool), tool_call_id=tool.get("toolCallId"), title=tool.get("title"),
                    agent_id=agent_id, blocked_path=_reported_permission_path(params),
                    kiro_agent_type=agent_type,
                    display_name=_reported_display_field(tool, "displayName"),
                    description=_reported_display_field(tool, "description"),
                    decision_reason=next((reason for kind, reason in hook_reasons if kind == "ask"), None),
                    request_metadata=copy.deepcopy(params.get("_meta"))
                    if isinstance(params.get("_meta"), dict) else None,
                    kiro_input_unavailable=type(tool.get("rawInput")) is not dict)

            if (self._effective_can_use_tool() and named_server_enabled and not hook_failure_blocks and
                    permission_mode != "denyUnlisted"):
                decision = await self._effective_can_use_tool()(tool.get("title", ""), copy.deepcopy(original_input),
                                                           permission_context())
                if not isinstance(decision, (PermissionResultAllow, PermissionResultDeny)):
                    raise ProtocolError("Permission callback must return PermissionResultAllow or PermissionResultDeny")
                if isinstance(decision, PermissionResultAllow):
                    if decision.updated_input is not None:
                        edited_command = self._validate_shell_edit(params, original_input,
                                                                   decision.updated_input)
                allow = isinstance(decision, PermissionResultAllow)
                if isinstance(decision, (PermissionResultAllow, PermissionResultDeny)):
                    explicit_option = decision.option_id
                    explicit_consent = decision.kiro_consent
                if isinstance(decision, PermissionResultDeny):
                    if not isinstance(decision.message, str):
                        raise ProtocolError("Permission denial message must be a string")
                    callback_deny_reason = decision.message
                    interrupt = decision.interrupt
            if blocked:
                if explicit_consent is not None:
                    raise ProtocolError("A hook blocked the request; persistent consent was not applied")
                allow = False
                explicit_option = None
            kinds = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
            if explicit_option is not None:
                choice = next((o for o in options if o.get("optionId") == explicit_option and o.get("kind") in kinds), None)
                if choice is None:
                    raise ProtocolError("Permission option does not match the requested decision")
            else:
                choice = next((o for o in options if o.get("kind") == kinds[0]), None)
            outcome = {"outcome": "selected", "optionId": choice["optionId"]} if choice else {"outcome": "cancelled"}
            response: dict[str, Any] = {"outcome": outcome}
            if explicit_consent is not None:
                response["_meta"] = {"kiro": {"consent": self._validate_kiro_consent(explicit_consent, params, choice)}}
            if edited_command is not None and allow:
                if (not choice or choice.get("kind") != "allow_once" or explicit_consent is not None):
                    raise ProtocolError("A shell command edit requires a one-time allow without persistent consent")
                response.setdefault("_meta", {}).setdefault("kiro", {})["editedCommand"] = edited_command
            hook_deny_reason = next((reason for kind, reason in hook_reasons if kind == "deny"), "")
            rejection_reason = hook_deny_reason or callback_deny_reason
            if (self.options.agent_engine == "v3" and choice and choice.get("kind") == "reject_once" and
                    rejection_reason):
                response.setdefault("_meta", {}).setdefault("kiro", {})["rejectionReason"] = rejection_reason
            if record["cancelled"]:
                return
            if (permission_turn is not None and
                    (self._turn is not permission_turn or permission_turn.done() or self._interrupt_requested)):
                await respond({"jsonrpc": "2.0", "id": msg["id"],
                               "result": {"outcome": {"outcome": "cancelled"}}})
                return
            if (choice and choice.get("kind") in ("reject_once", "reject_always") and
                    permission_turn is not None and permission_turn is self._turn and
                    params.get("sessionId") == self.session_id):
                denial = {"tool_name": tool.get("title", ""), "tool_call_id": tool.get("toolCallId"),
                          "tool_input": copy.deepcopy(original_input)}
                if type(tool.get("rawInput")) is not dict:
                    denial["kiro_input_unavailable"] = True
                metadata = _tool_kiro_metadata(tool)
                if metadata is not None:
                    denial["kiro_metadata"] = metadata
                await self._events.put({"sessionUpdate": "sdk_permission_denial",
                                        "turn": permission_turn, "denial": denial})
            sent = await respond({"jsonrpc": "2.0", "id": msg["id"], "result": response})
            if interrupt and sent and permission_turn is not None and self._turn is permission_turn and not permission_turn.done():
                try:
                    await self.interrupt()
                except Exception as exc:
                    await self._events.put(exc)
        except asyncio.CancelledError:
            if record["cancelled"] or self._disconnecting or not self._connected:
                raise
            # A callback may itself raise CancelledError without a peer cancel.
            # Settle that request instead of silently abandoning its response.
            await respond({"jsonrpc": "2.0", "id": msg["id"],
                           "error": {"code": -32603, "message": "Kiro callback was cancelled"}})
        except Exception as exc:
            if record["cancelled"]:
                return
            interaction = ((msg.get("method") == "_kiro/userInput" and self.options.on_user_input is not None) or
                           (msg.get("method") == "_kiro/mcp/elicitation" and
                            (self.options.on_mcp_elicitation is not None)))
            permission = (msg.get("method") == "session/request_permission" and
                          (self._effective_can_use_tool() is not None or bool(self._sdk_hooks.get("PreToolUse"))))
            if interaction or permission:
                error = ProtocolError("Kiro user interaction callback failed" if interaction else
                                      "Kiro permission callback or hook failed")
                error.__cause__ = exc
                await self._events.put(error)
            message = ("Kiro user interaction callback failed" if interaction else
                       "Kiro permission callback or hook failed" if permission else str(exc))
            await respond({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": message}})
        finally:
            pass

    async def _mirror(self, kind: str, data: Any, *, prompt_key: Optional[str] = None) -> None:
        writer = self._mirror_writer
        generation = self._connection_generation
        if writer is not None:
            async with self._store_lock:
                if self._mirror_writer is not writer or self._connection_generation != generation:
                    raise CLIConnectionError("Session mirror changed before the write")
                entry = {"type": kind, "data": data}
                if prompt_key is not None:
                    entry["kiro_sdk_prompt_key"] = prompt_key
                await writer.append(entry)

    async def _flush_mirror(self) -> None:
        if self._mirror_writer is not None:
            async with self._store_lock:
                try:
                    await asyncio.wait_for(self._mirror_writer.flush(), self.options.session_store_flush_timeout)
                except asyncio.TimeoutError as exc:
                    raise CLIConnectionError("Timed out flushing the session mirror") from exc

    async def _mirror_session_info(self) -> None:
        """Copy the title and metadata Kiro reports for this session into the mirror.

        The metadata is advisory: when Kiro cannot list sessions, the mirror keeps the conversation without it.
        """
        capabilities = (self.server_info or {}).get("agentCapabilities", {}).get("sessionCapabilities", {})
        if self._mirror_writer is None or capabilities.get("list") is None or not self.session_id:
            return
        params: dict[str, Any] = {"cwd": self._working_directory()}
        try:
            for _ in range(20):
                page = await asyncio.wait_for(self._request("session/list", params), 10)
                records = page.get("sessions") if isinstance(page, dict) else None
                if not isinstance(records, list):
                    return
                for record in records:
                    if isinstance(record, dict) and record.get("sessionId") == self.session_id:
                        await self._mirror("session_info", copy.deepcopy(record))
                        return
                cursor = page.get("nextCursor")
                if not isinstance(cursor, str) or not cursor or cursor == params.get("cursor"):
                    return
                params["cursor"] = cursor
        except (JSONRPCError, ProtocolError, asyncio.TimeoutError):
            return

    async def _mirror_archive(self) -> None:
        if not self.options.session_store_archive:
            return
        from .archive import _archive_payload, export_session_archive
        try:
            archive = await asyncio.wait_for(
                export_session_archive(self.session_id, cwd=self.options.cwd, cli_path=self._cli_path,
                                       env=self._configured_env(), max_bytes=self.options.session_store_archive_max_bytes,
                                       max_uncompressed_bytes=self.options.session_store_archive_max_uncompressed_bytes,
                                       timeout=self.options.session_store_archive_timeout),
                self.options.session_store_archive_timeout)
        except asyncio.TimeoutError as exc:
            raise SessionArchiveError("Timed out exporting the Kiro session archive") from exc
        await self._mirror("session_archive", _archive_payload(self.session_id, archive))

    async def _mirror_failed(self, exc: Exception) -> None:
        if self.options.session_store_error_policy == "continue":
            key = copy.deepcopy(self._store_key)
            message = MirrorErrorMessage("mirror_error", {"key": key, "error": str(exc)}, key=key, error=str(exc))
            self._last_mirror_error = message
            self._pending_mirror_errors.append(message)
            await self._events.put({"sessionUpdate": "sdk_mirror_error"})
            return
        if self._reader_error is None:
            self._reader_error = exc
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(exc)
            await self._events.put(exc)
            if self._connected:
                await self._abort_for_guard()

    async def _run_hooks(self, event: str, fields: dict[str, Any], tool_call_id: Optional[str] = None,
                         additional_context: Optional[list[str]] = None,
                         permission_decisions: Optional[list[str]] = None,
                         permission_reasons: Optional[list[tuple[str, str]]] = None,
                         permission_inputs: Optional[list[dict[str, Any]]] = None,
                         session_titles: Optional[list[str]] = None,
                         prompt_block_reasons: Optional[list[str]] = None,
                         prompt_system_messages: Optional[list[str]] = None,
                         hook_system_messages: Optional[list[str]] = None,
                         failure_blocks: Optional[list[bool]] = None) -> bool:
        self._validate_hook_failure_snapshot()
        blocked = False
        payload = {"hook_event_name": event, "session_id": self.session_id or "",
                   "cwd": self._working_directory(), **fields}
        if (event in ("PreToolUse", "PostToolUse") and
                isinstance(tool_call_id, str) and tool_call_id):
            payload["tool_call_id"] = tool_call_id
        for matcher in self._sdk_hooks.get(event, []):
            if matcher.matcher and not re.search(matcher.matcher, fields.get("tool_name", "")):
                continue
            policy = self._effective_hook_failure_policy(event, matcher)
            if event == "Stop" and policy == "block":
                # agentStop hooks only observe the finished turn, so a failure cannot block anything.
                policy = "continue"
            for hook in (tuple(matcher.hooks) if policy is not None else matcher.hooks):
                self._validate_hook_failure_snapshot()
                try:
                    result = await self._invoke_failure_aware_hook(hook, payload, tool_call_id, matcher.timeout or 60)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._validate_hook_failure_snapshot()
                    if policy is None:
                        raise
                    if policy == "block":
                        blocked = True
                        if failure_blocks is not None:
                            failure_blocks.append(True)
                    continue
                self._validate_hook_failure_snapshot()
                prior_blocked = blocked
                restrictive = (event in ("UserPromptSubmit", "PreToolUse") and isinstance(result, dict) and
                               any(result.get(key) not in (None, False) for key in ("block", "ask")))
                tracked = (additional_context, permission_decisions, permission_reasons, permission_inputs,
                           session_titles, prompt_block_reasons, prompt_system_messages, hook_system_messages)
                lengths = {id(items): (items, len(items)) for items in tracked if items is not None}
                try:
                    allowed = {"block"} if event in ("UserPromptSubmit", "PreToolUse") else set()
                    if hook_system_messages is not None:
                        allowed.add("warning")
                    if event == "UserPromptSubmit" and additional_context is not None:
                        allowed.update({"context", "title"})
                        if prompt_system_messages is not None:
                            allowed.add("warning")
                    if event == "PreToolUse" and permission_decisions is not None:
                        allowed.update({"ask", "allow", "tool_input"})
                    if event == "PostToolUse" and additional_context is not None:
                        allowed.add("context")
                    if policy is not None and isinstance(result, dict):
                        # Keep restrictive keys so an unsupported block or ask fails closed.
                        result = {key: value for key, value in result.items()
                                  if key in allowed or key in ("block", "ask")}
                    output = parse_hook_output(event, result, allowed)
                    if "warning" in output:
                        messages = prompt_system_messages if event == "UserPromptSubmit" else hook_system_messages
                        assert messages is not None
                        _append_hook_warning(messages, event, output["warning"])
                    if "context" in output:
                        assert additional_context is not None
                        additional_context.append(output["context"])
                    if "title" in output:
                        if session_titles is None:
                            raise ProtocolError("A session title is not available for this prompt")
                        session_titles.append(output["title"])
                    if event == "PreToolUse" and permission_decisions is not None:
                        if "ask" in output:
                            permission_decisions.append("ask")
                            if permission_reasons is not None and output["ask"]:
                                permission_reasons.append(("ask", output["ask"]))
                        elif "allow" in output:
                            permission_decisions.append("allow")
                        if "tool_input" in output:
                            if "allow" not in output or permission_inputs is None:
                                raise ProtocolError("preToolUse tool_input requires allow")
                            permission_inputs.append(copy.deepcopy(output["tool_input"]))
                    if "block" in output:
                        blocked = True
                        reason = output["block"]
                        if event == "UserPromptSubmit" and reason and prompt_block_reasons is not None:
                            if (len(reason) > 8192 or len(prompt_block_reasons) >= 32 or
                                    sum(len(item.encode("utf-8")) for item in prompt_block_reasons) +
                                    len(reason.encode("utf-8")) > 65536):
                                raise ProtocolError("promptSubmit block reasons exceed the limit")
                            prompt_block_reasons.append(reason)
                        if permission_reasons is not None and reason:
                            permission_reasons.append(("deny", reason))
                except (ProtocolError, ValueError, TypeError, RecursionError):
                    if policy is None:
                        raise
                    for items, length in lengths.values():
                        del items[length:]
                    blocked = prior_blocked or policy == "block" or restrictive
                    if (policy == "block" or restrictive) and failure_blocks is not None:
                        failure_blocks.append(True)
        return blocked

    def _posttool_hook_fields(self, merged: dict[str, Any], output: Any) -> dict[str, Any]:
        fields = {"tool_name": merged.get("title", ""), "tool_input": merged.get("rawInput") or {},
                  "tool_response": output}
        agent_id = _reported_tool_agent_id(merged)
        if agent_id is not None:
            fields["agent_id"] = agent_id
            agent_type = self._reported_subagent_type(self.session_id, agent_id)
            if agent_type is not None:
                fields["agent_type"] = agent_type
                fields["kiro_observed_agent_type"] = True
        return fields

    async def _run_posttool_hooks(self, fields: dict[str, Any], tool_call_id: Optional[str], *,
                                  hook_system_messages: Optional[list[str]] = None) -> None:
        if await self._run_hooks("PostToolUse", fields, tool_call_id, hook_system_messages=hook_system_messages):
            await self._abort_for_guard(force=True)
            raise ProtocolError("Post-tool hook failed")

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        self._check_elicitation_reentry()
        if (_FILE_CHANGED_CONTEXT.get() is self or self in _CLIENT_AUTH_CONTEXT.get() or
                asyncio.current_task() in self._client_auth_tasks):
            raise CLIConnectionError("FileChanged, authentication and model hooks cannot control the client")
        async with self._query_lock:
            self._validate_unsupported_limits()
            self._validate_pretool_hook_state()
            self._validate_startup_fallback_state()
            if self._connected and self.options.sdk_tool_interrupt_mode != self._sdk_tool_interrupt_mode:
                raise ValueError("Reconnect after changing sdk_tool_interrupt_mode")
            if self._connected and self.options.sdk_mcp_toggle_mode != self._sdk_mcp_toggle_mode:
                raise ValueError("Reconnect after changing sdk_mcp_toggle_mode")
            if self._sdk_tools_suspended:
                raise CLIConnectionError("Consume the interrupted response before starting another query")
            if self._stream_input is not None or self._queued_after_initial is not None:
                raise CLIConnectionError("Consume or close the current input stream first")
            if self._initial_prompt_active:
                if not self.session_id:
                    raise CLIConnectionError("Call connect() first")
                if session_id not in ("default", self.session_id):
                    raise ValueError("This client is connected to a different session")
                if isinstance(prompt, AsyncIterable):
                    if self._stream_input_policy[0] == "buffered":
                        await self._collect_stream_input(prompt.__aiter__(), after_initial=True)
                    elif self._stream_input_policy[0] in ("prefetch", "prefetch-first"):
                        self._stream_input = self._prefetch_stream_input(prompt.__aiter__())
                    else:
                        self._stream_input = prompt.__aiter__()
                elif isinstance(prompt, (str, list)):
                    if isinstance(prompt, list) and not prompt:
                        raise ValueError("prompt must not be empty")
                    self._queued_after_initial = (copy.deepcopy(prompt), session_id)
                else:
                    raise TypeError("prompt must be a string, a list of ACP content blocks, or an async iterable")
                self._signal_message_activity()
                return
            self._initial_prompt_failed = False
            if isinstance(prompt, AsyncIterable):
                if not self.session_id:
                    raise CLIConnectionError("Call connect() first")
                if session_id not in ("default", self.session_id):
                    raise ValueError("This client is connected to a different session")
                if self._turn is not None:
                    raise CLIConnectionError("Consume the current response before starting another query")
                stream = prompt.__aiter__()
                if self._stream_input_policy[0] == "buffered":
                    await self._collect_stream_input(stream)
                elif self._stream_input_policy[0] in ("prefetch", "prefetch-first"):
                    self._stream_input = self._prefetch_stream_input(stream)
                    if self._stream_input_policy[0] == "prefetch-first":
                        await self._start_prefetched_first(self._stream_input)
                else:
                    self._stream_input = stream
            else:
                await self._query_locked(prompt, session_id)
            self._signal_message_activity()

    def _prefetch_stream_input(self, stream: Any) -> PrefetchedInput:
        """Snapshot a bounded queue while the receiver processes prior turns."""
        _, max_messages, max_bytes, _ = self._stream_input_policy

        def snapshot(item: Any) -> tuple[Any, int]:
            content = _stream_content(item, allow_composed=self.options.runtime_compat_mode == "v3", force_composed=self.options.verbatim_prompts)
            value = _snapshot_stream_content(content, max_bytes)
            if value is None or (isinstance(_stream_snapshot_value(value), list) and not _stream_snapshot_value(value)):
                raise ValueError("Streaming input requires bounded JSON prompt content")
            size = len(json.dumps(_stream_snapshot_value(value), ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False).encode("utf-8"))
            if size > max_bytes:
                raise ValueError("Streaming input exceeds the buffered byte limit")
            return value, size

        return PrefetchedInput(stream, snapshot, max_messages, max_bytes)

    async def _start_prefetched_first(self, stream: PrefetchedInput) -> None:
        """Wait for and submit the first prefetched input before query returns."""
        generation, session_id = self._connection_generation, self.session_id
        timeout = self._stream_input_policy[3]
        self._input_collecting = True
        cancelled = asyncio.Event()
        self._input_cancel_event = cancelled
        pending = asyncio.ensure_future(stream.__anext__())
        self._input_next_task = pending
        cancel_waiter = asyncio.create_task(cancelled.wait())
        try:
            done, _ = await asyncio.wait((pending, cancel_waiter), timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancelled.is_set() or cancel_waiter in done:
                raise asyncio.CancelledError
            if not done:
                raise TimeoutError("Streaming input first message timed out")
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            if (self._stream_input is not stream or self._connection_generation != generation or
                    not self._connected or self._disconnecting or self.session_id != session_id):
                raise asyncio.CancelledError
            self._validate_unsupported_limits()
            await self._query_locked(_stream_content(item, allow_composed=self.options.runtime_compat_mode == "v3", force_composed=self.options.verbatim_prompts), "default")
        except BaseException:
            if (self._connection_generation == generation and self._connected and not self._disconnecting):
                with contextlib.suppress(Exception):
                    await self.disconnect()
            raise
        finally:
            if self._input_next_task is pending:
                self._input_next_task = None
            if self._input_cancel_event is cancelled:
                self._input_cancel_event = None
            if not cancel_waiter.done():
                cancel_waiter.cancel()
            await asyncio.gather(cancel_waiter, return_exceptions=True)
            if self._connection_generation == generation:
                self._input_collecting = False
                self._signal_message_activity()

    async def _collect_stream_input(self, stream: Any, *, after_initial: bool = False) -> None:
        """Collect finite JSON prompts under bounded ownership before sending the first turn."""
        _, max_messages, max_bytes, timeout = self._stream_input_policy
        generation = self._connection_generation
        session_id = self.session_id
        self._stream_input = stream
        self._input_collecting = True
        cancelled = asyncio.Event()
        self._input_cancel_event = cancelled
        cancel_waiter = asyncio.create_task(cancelled.wait())
        pending = None
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        contents: list[Any] = []
        total_bytes = 0
        try:
            while True:
                pending = asyncio.ensure_future(stream.__anext__())
                self._input_next_task = pending
                remaining = None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait((pending, cancel_waiter), timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)
                if cancelled.is_set() or cancel_waiter in done:
                    raise asyncio.CancelledError
                if not done:
                    raise TimeoutError("Streaming input buffering timed out")
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    break
                finally:
                    if self._input_next_task is pending:
                        self._input_next_task = None
                if len(contents) >= max_messages:
                    raise ValueError("Streaming input exceeds the buffered message limit")
                content = _stream_content(item, allow_composed=self.options.runtime_compat_mode == "v3", force_composed=self.options.verbatim_prompts)
                snapshot = _snapshot_stream_content(content, max_bytes - total_bytes)
                if snapshot is None or (isinstance(_stream_snapshot_value(snapshot), list) and not _stream_snapshot_value(snapshot)):
                    raise ValueError("Streaming input requires bounded JSON prompt content")
                encoded = json.dumps(_stream_snapshot_value(snapshot), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                total_bytes += len(encoded)
                if total_bytes > max_bytes:
                    raise ValueError("Streaming input exceeds the buffered byte limit")
                contents.append(snapshot)
            if (self._stream_input is not stream or self._connection_generation != generation or
                    not self._connected or self._disconnecting or self.session_id != session_id):
                raise asyncio.CancelledError
            self._validate_unsupported_limits()
            if contents:
                self._stream_input = _buffered_stream_input(contents[1:]) if len(contents) > 1 else None
                if after_initial:
                    self._queued_after_initial = (contents[0], "default")
                else:
                    await self._query_locked(contents[0], "default")
            else:
                self._stream_input = _buffered_stream_input([])
        except BaseException:
            if (self._connection_generation == generation and self._connected and not self._disconnecting):
                with contextlib.suppress(Exception):
                    await self.disconnect()
            raise
        finally:
            if pending is not None and self._input_next_task is pending:
                self._input_next_task = None
            if self._input_cancel_event is cancelled:
                self._input_cancel_event = None
            if not cancel_waiter.done():
                cancel_waiter.cancel()
            await asyncio.gather(cancel_waiter, return_exceptions=True)
            if self._connection_generation == generation:
                self._input_collecting = False
                self._signal_message_activity()

    async def _query_locked(self, prompt: Any, session_id: str) -> None:
        preparation_revision = self._extension_hook_revision
        literal = self.options.verbatim_prompts or isinstance(prompt, VerbatimPrompt)
        if isinstance(prompt, VerbatimPrompt):
            prompt = prompt.content
        if literal and self.options.runtime_compat_mode != "v3":
            raise ValueError("verbatim_prompts and VerbatimPrompt require runtime_compat_mode='v3'")
        self._validate_unsupported_limits()
        prompt_guard = self._prompt_send_guard
        guard_session = self.session_id
        guard_generation = self._connection_generation
        async def check_prompt_guard() -> None:
            if preparation_revision != self._extension_hook_revision:
                raise CLIConnectionError("Prompt preparation was interrupted")
            self._validate_hook_failure_snapshot()
            def current() -> bool:
                return (self._prompt_send_guard is prompt_guard and self._connected and not self._disconnecting and
                        self._reader_error is None and self.session_id == guard_session and
                        self._connection_generation == guard_generation and self._turn is None)
            if self._reader_error is not None:
                raise CLIConnectionError("Kiro connection reader failed") from self._reader_error
            if not self._connected or self._disconnecting:
                raise CLIConnectionError("Kiro connection closed during prompt preparation")
            if not current():
                raise ProtocolError("Prompt authorization guard changed")
            if prompt_guard is not None:
                if await prompt_guard() is not True:
                    raise ProtocolError("Prompt authorization changed")
                if not current():
                    raise ProtocolError("Prompt authorization changed")
                self._validate_hook_failure_snapshot()
        self._validate_pretool_hook_state()
        self._validate_startup_fallback_state()
        if (self.options.reported_credit_ledger is not self._credit_ledger or
                self.options.reported_credit_scope != self._credit_scope):
            raise ValueError("Create a new client after changing the credit ledger or scope")
        if self._credit_ledger is not None:
            try:
                current_limit = _credit_amount(self.options.max_reported_credits, positive=True)
            except ValueError as exc:
                raise ValueError("Create a new client after changing the reported credit limit") from exc
            if current_limit != self._reported_credit_limit:
                raise ValueError("Create a new client after changing the reported credit limit")
        if self._connected and self.options.sdk_tool_interrupt_mode != self._sdk_tool_interrupt_mode:
            raise ValueError("Reconnect after changing sdk_tool_interrupt_mode")
        if self._connected and self.options.sdk_mcp_toggle_mode != self._sdk_mcp_toggle_mode:
            raise ValueError("Reconnect after changing sdk_mcp_toggle_mode")
        if self._sdk_tools_suspended:
            raise CLIConnectionError("Consume the interrupted response before starting another query")
        if not self.session_id:
            raise CLIConnectionError("Call connect() first")
        if session_id not in ("default", self.session_id):
            raise ValueError("This client is connected to a different session")
        if self._turn is not None:
            raise CLIConnectionError("Consume the current response before starting another query")
        if self._reported_credit_limit is not None:
            if self._credit_pending or self._credit_unknown:
                raise ReportedCreditLimitError("Reported Kiro credit usage is unavailable for a prior turn",
                                               float(self._reported_credits), float(self._reported_credit_limit))
            if self._reported_credits >= self._reported_credit_limit:
                raise ReportedCreditLimitError("Reported Kiro credit limit reached",
                                               float(self._reported_credits), float(self._reported_credit_limit))
        if self._recovery_in_doubt:
            raise CLIConnectionError("Checkpoint outcome is uncertain; disconnect and inspect the workspace before continuing")
        if self._sandbox_required and (self._sandbox_status or {}).get("status") != "active":
            raise ProtocolError("Kiro command sandbox is not active")
        if not isinstance(prompt, (str, list)):
            raise TypeError("prompt must be a string or a list of ACP content blocks")
        if isinstance(prompt, list) and not prompt:
            raise ValueError("prompt must not be empty")
        await check_prompt_guard()
        if self._pending_session_plan is not None:
            session, _, origin, ready = self._pending_session_plan
            if not ready or session != self.session_id:
                raise ProtocolError("Deferred Plan update is not ready for this session")
            await self._set_session_native_plan("plan", expected_origin=origin, allow_stream=True)
            self._pending_session_plan = None
        if isinstance(prompt, str):
            content = [{"type": "text", "text": prompt}]
        elif isinstance(prompt, list):
            content = _prompt_content(prompt, self.server_info)
        else:
            raise TypeError("prompt must be a string or a list of ACP content blocks")
        if not content:
            raise ValueError("prompt must not be empty")
        if self._sandbox_required and (self._sandbox_status or {}).get("status") != "active":
            raise ProtocolError("Kiro command sandbox is not active")
        if self._reader_error:
            raise CLIConnectionError("Kiro connection reader failed") from self._reader_error
        if self._credit_ledger is not None:
            try:
                reservation, previous = await self._credit_ledger.begin(self._credit_scope, self._reported_credit_limit)
            except ReportedCreditLimitError as exc:
                self._reported_credits = _credit_amount(exc.reported_credits)
                raise
            if not isinstance(reservation, str) or not reservation or len(reservation) > 1024 or "\0" in reservation:
                raise ProtocolError("Credit ledger returned an invalid reservation")
            previous = _credit_amount(previous)
            if previous >= self._reported_credit_limit:
                raise ProtocolError("Credit ledger reserved a turn after the limit was reached")
            self._credit_reservation = reservation
            self._reported_credits = previous
        try:
            await check_prompt_guard()
            hook_context: list[str] = []
            session_titles: list[str] = []
            block_reasons: list[str] = []
            system_messages: list[str] = []
            prompt_fields = {"prompt": prompt}
            blocked = await self._run_hooks("UserPromptSubmit", prompt_fields, additional_context=hook_context,
                                            session_titles=session_titles, prompt_block_reasons=block_reasons,
                                            prompt_system_messages=system_messages)
            if blocked:
                raise PromptBlockedError(block_reasons, self.session_id, system_messages)
            await check_prompt_guard()
            if session_titles:
                if self.options.agent_engine != "v3":
                    raise ProtocolError("promptSubmit session titles require Kiro V3")
                title = session_titles[-1]
                renamed = await self.rename_session(title)
                if not isinstance(renamed, dict) or renamed.get("success") is not True:
                    raise ProtocolError("Kiro did not confirm the session title")
            if hook_context:
                content = list(content)
                content.extend({"type": "text", "text": text} for text in hook_context)
            if self.options.output_format is not None:
                content = list(content)
                content.append({"type": "text", "text": "Respond with only valid JSON conforming to this JSON Schema: " +
                                json.dumps(self.options.output_format["schema"], ensure_ascii=False)})
            await check_prompt_guard()
            mirror_prompt_id = uuid.uuid4().hex if self._mirror_writer is not None else None
            if mirror_prompt_id is not None:
                await self._mirror("prompt", content, prompt_key=mirror_prompt_id)
            else:
                await self._mirror("prompt", content)
            await check_prompt_guard()
            if self._sandbox_required and (self._sandbox_status or {}).get("status") != "active":
                raise ProtocolError("Kiro command sandbox is not active")
            if self._reader_error:
                raise CLIConnectionError("Kiro connection reader failed") from self._reader_error
        except BaseException:
            if self._credit_ledger is not None:
                try:
                    usage = Decimal(0)
                    total = _credit_amount(await self._credit_ledger.complete(
                        self._credit_scope, self._credit_reservation, usage))
                    if total < self._reported_credits or (usage is not None and
                            total != _sum_credits((self._reported_credits, usage))):
                        raise ProtocolError("Credit ledger returned an inconsistent preflight total")
                    self._reported_credits = total
                    if usage is None:
                        self._credit_unknown = True
                    self._credit_reservation = None
                except asyncio.CancelledError:
                    self._credit_pending = True
                    raise
                except Exception as exc:
                    self._credit_pending = True
                    raise ProtocolError("Credit reservation could not be released; reconcile the ledger") from exc
            raise
        sent = asyncio.Event()
        self._interrupt_requested = False
        self._native_stop_blocks = 0
        self._native_stop_valid_blocks = 0
        self._native_stop_warning_count = 0
        self._native_stop_warning_bytes = 0
        if self.session_id:
            self._subagent_types_by_session.pop(self.session_id, None)
        self._turn_model = self._session_model
        self._turn_started_at = time.monotonic()
        self._turn_finished_at = None
        if self._reported_credit_limit is not None:
            self._credit_pending = True
        turn = asyncio.create_task(self._prompt_request(content, sent, guard_session, guard_generation,
                                                       literal=literal))
        self._turn = turn
        self._turn_prompt_warnings = tuple(system_messages)
        self._turn_mirror_prompt_id = mirror_prompt_id
        turn.add_done_callback(self._handler_finished)
        # A turn can be cancelled before its coroutine starts, in which case
        # _request's finally block never gets a chance to signal the waiter.
        turn.add_done_callback(lambda _: sent.set())
        try:
            await sent.wait()
            if turn.done():
                if turn.cancelled():
                    raise CLIConnectionError("Kiro turn was closed")
                await turn
        except BaseException:
            if self._connection_generation == guard_generation and self._connected and not self._disconnecting:
                with contextlib.suppress(Exception):
                    await self.disconnect()
            raise

    async def _prompt_request(self, content: list[dict[str, Any]], sent: asyncio.Event,
                              session_id: str, generation: int, *, literal: bool = False) -> Any:
        try:
            self._validate_hook_failure_snapshot()
            if (not self._connected or self._disconnecting or self._reader_error is not None or
                    self.session_id != session_id or self._connection_generation != generation):
                raise CLIConnectionError("Connection changed before sending the prompt")
            params = {"sessionId": session_id, "prompt": content}
            policy_request_id = None
            if self.options.runtime_compat_mode == "v3":
                self._validate_unsupported_limits()
                if self._native_hook_runtime is None:
                    raise ProtocolError("Verified runtime is unavailable")
                policy_request_id = uuid.uuid4().hex
                params["_meta"] = {"kiro": {"sdkRuntime": {"protocol": 7,
                    "usageMode": self.options.runtime_usage_mode,
                    "requestId": policy_request_id, "maxTurns": self._runtime_turn_limit(),
                    "thinkingDisabled": False,
                    "agentEfforts": dict(self._runtime_agent_efforts),
                    "agentMaxTurns": dict(self._runtime_agent_max_turns),
                    "inheritAgents": list(()),
                    "foregroundAgents": list(self._runtime_foreground_agents),
                    "backgroundAgents": [],
                    "backgroundContext": 'none',
                    "backgroundResults": [],
                    "subagentStopLimit": None,
                    "literal": literal, "fallbackModel": self.options.fallback_model
                    if self.options.fallback_model_mode == "runtime" else None}}}
            async def checked_request():
                self._active_runtime_request_id = policy_request_id
                self._runtime_cancelled_hooks.clear()
                try:
                    result = await self._request("session/prompt", params, sent)
                finally:
                    if self._active_runtime_request_id == policy_request_id:
                        self._active_runtime_request_id = None
                        self._runtime_cancelled_hooks.clear()
                cancelled = result.get("stopReason") == "cancelled"
                meta = result.get("_meta")
                kiro_meta = meta.get("kiro") if isinstance(meta, dict) else None
                has_receipt = isinstance(kiro_meta, dict) and "sdkRuntime" in kiro_meta
                if policy_request_id is not None and (not cancelled or has_receipt):
                    receipt = kiro_meta.get("sdkRuntime") if isinstance(kiro_meta, dict) else None
                    expected = params["_meta"]["kiro"]["sdkRuntime"]
                    limit = expected["maxTurns"]
                    fallback = expected["fallbackModel"]
                    if (not isinstance(receipt, dict) or type(receipt.get("protocol")) is not int or receipt.get("protocol") != 7 or
                            receipt.get("usageMode") != expected["usageMode"] or
                            receipt.get("requestId") != policy_request_id or receipt.get("literal") is not literal or
                            receipt.get("fallbackModel") != fallback or type(receipt.get("fallbackApplied")) is not bool or
                            receipt.get("thinkingDisabled") is not expected["thinkingDisabled"] or
                            receipt.get("agentEfforts") != expected["agentEfforts"] or
                            receipt.get("agentMaxTurns") != expected["agentMaxTurns"] or
                            receipt.get("inheritAgents") != expected["inheritAgents"] or
                            receipt.get("foregroundAgents") != expected["foregroundAgents"] or
                            receipt.get("backgroundAgents") != expected["backgroundAgents"] or
                            receipt.get("backgroundContext") != expected["backgroundContext"] or
                            type(receipt.get("backgroundResultsApplied")) is not bool or
                            (receipt.get("backgroundResultsApplied") is False) or
                            receipt.get("backgroundResultIds") != (
                                []) or
                            not isinstance(receipt.get("agentMaxTurns"), dict) or
                            any(type(value) is not int for value in receipt["agentMaxTurns"].values()) or
                            type(receipt.get("subagentStopLimit")) is not type(expected["subagentStopLimit"]) or
                            receipt.get("subagentStopLimit") != expected["subagentStopLimit"] or
                            (receipt.get("fallbackApplied") is True and fallback is None) or
                            type(receipt.get("modelTurns")) is not int or receipt["modelTurns"] < 0 or
                            type(receipt.get("maxTurnsReached")) is not bool or
                            (limit is not None and receipt["modelTurns"] > limit) or
                            (receipt["maxTurnsReached"] and (limit is None or receipt["modelTurns"] != limit))):
                        self._reader_error = ProtocolError("Kiro runtime policy receipt is invalid")
                        await self._abort_for_guard(force=True)
                        raise self._reader_error
                    try:
                        usage = validate_usage_report(receipt.get("modelUsage"), enabled=expected["usageMode"] == "reported")
                    except ProtocolError as exc:
                        self._reader_error = exc
                        await self._abort_for_guard(force=True)
                        raise
                    result = {**result, "_sdk_model_turns": receipt["modelTurns"]}
                    if usage is not None:
                        result["_sdk_model_usage"] = usage
                    if receipt["maxTurnsReached"] and not cancelled:
                        result["stopReason"] = "max_turn_requests"
                return result
            request = checked_request()
            if self.options.turn_timeout is None:
                return await request
            request_task = asyncio.create_task(request)
            request_task.add_done_callback(self._handler_finished)
            try:
                done, _ = await asyncio.wait((request_task,), timeout=self.options.turn_timeout)
                if request_task in done or request_task.done():
                    return await request_task
                request_task.cancel()
                sent.set()
                error = CLIConnectionError("Timed out waiting for the Kiro turn")
                self._reader_error = error
                await self._abort_for_guard(force=True)
                raise error
            finally:
                if not request_task.done():
                    request_task.cancel()
        finally:
            if self._connection_generation == generation and self.session_id == session_id:
                self._turn_finished_at = time.monotonic()

    def _partial_stream_event(self, event: dict[str, Any], *,
                              parent_tool_call_id: Optional[str] = None,
                              subagent_id: Optional[str] = None) -> Optional[StreamEvent]:
        return StreamEvent(copy.deepcopy(event), message_id=event.get("messageId"), session_id=self.session_id,
                           agent_id=subagent_id, parent_tool_call_id=parent_tool_call_id)

    async def receive_response(self) -> AsyncIterator[Any]:
        """Consume one turn of messages."""
        self._check_elicitation_reentry()
        if self._continuous_messages_active:
            raise CLIConnectionError("A continuous message receiver is active")
        if self._lock.locked():
            # Waiting would deadlock a receiver started from the active receiver's own task.
            raise CLIConnectionError("Another message receiver is active")
        response = self._receive_response_raw()
        try:
            async for message in response:
                yield message
        finally:
            await response.aclose()

    async def _receive_response_raw(self) -> AsyncIterator[Any]:
        if self._input_collecting:
            raise CLIConnectionError("Streaming input is still being buffered")
        if self._turn is None and self._stream_input is None and self._queued_after_initial is None:
            raise CLIConnectionError("No active query")
        async with self._lock:
            if self._turn is None:
                if self._initial_prompt_failed:
                    await self.disconnect()
                    raise CLIConnectionError("Initial agent prompt failed; queued prompts were not sent")
                if self._queued_after_initial is not None:
                    async with self._query_lock:
                        prompt, session_id = self._queued_after_initial
                        self._queued_after_initial = None
                        await self._query_locked(prompt, session_id)
                elif self._stream_input is None:
                    raise CLIConnectionError("No active query")
                else:
                    stream = self._stream_input
                    generation = self._connection_generation
                    cancelled = asyncio.Event()
                    self._input_cancel_event = cancelled
                    pending = cancel_waiter = None
                    try:
                        pending = asyncio.ensure_future(stream.__anext__())
                        self._input_next_task = pending
                        cancel_waiter = asyncio.create_task(cancelled.wait())
                        done, _ = await asyncio.wait((pending, cancel_waiter), return_when=asyncio.FIRST_COMPLETED)
                        if cancel_waiter in done or cancelled.is_set():
                            raise asyncio.CancelledError
                        item = pending.result()
                        async with self._query_lock:
                            if (self._stream_input is not stream or self._connection_generation != generation or
                                    not self._connected or self._disconnecting):
                                raise asyncio.CancelledError
                            await self._query_locked(_stream_content(item, allow_composed=self.options.runtime_compat_mode == "v3", force_composed=self.options.verbatim_prompts), "default")
                    except StopAsyncIteration:
                        if self._stream_input is stream and self._connection_generation == generation:
                            self._stream_input = None
                        return
                    except BaseException:
                        if (self._stream_input is stream and self._connection_generation == generation and
                                self._connected and not self._disconnecting):
                            await self.disconnect()
                        raise
                    finally:
                        if pending is not None and self._input_next_task is pending:
                            self._input_next_task = None
                        if self._input_cancel_event is cancelled:
                            self._input_cancel_event = None
                        if cancel_waiter is not None:
                            if not cancel_waiter.done():
                                cancel_waiter.cancel()
                            await asyncio.gather(cancel_waiter, return_exceptions=True)
            parts = []
            kiro_usage = None
            credit_summaries: dict[Any, Decimal] = {}
            invalid_credits = False
            completion_status = None
            user_message_id = None
            replay_ids: set[str] = set()
            replay_id_invalid = False
            permission_denials: list[dict[str, Any]] = []
            tool_calls: dict[str, dict[str, Any]] = {}
            completed_tools: set[str] = set()
            announced_tools: set[str] = set()
            subagent_parents = self._subagent_parents_by_session.setdefault(self.session_id, {})
            subagent_tools: dict[str, str] = {}
            assistant_blocks: list[Any] = []
            assistant_message_id = None
            turn_model = self._turn_model

            def take_assistant() -> Optional[AgentMessage]:
                nonlocal assistant_blocks, assistant_message_id
                if not assistant_blocks:
                    return None
                message = AgentMessage(assistant_blocks, message_id=assistant_message_id,
                                       session_id=self.session_id, model=turn_model)
                assistant_blocks, assistant_message_id = [], None
                return message

            def tool_message(tool_id: Optional[str], merged: dict[str, Any],
                             agent_id: Optional[str] = None) -> ToolCall:
                return tool_call_message(tool_id, merged, self.session_id, agent_id,
                                          subagent_parents.get(agent_id) if agent_id else None)

            turn = self._turn
            completed = False
            turn_success = False
            event_task = None
            # Background progress must not keep a finite parent response open.
            # Any remaining notifications stay available to continuous reception.
            try:
                for warning in ():
                    yield _hook_warning("UserPromptExpansion", warning, self.session_id)
                for warning in self._turn_prompt_warnings:
                    yield _hook_warning("UserPromptSubmit", warning, self.session_id)
                self._turn_prompt_warnings = ()
                for event, warning in ():
                    yield _hook_warning(event, warning, self.session_id)
                while True:
                    if turn.done() and self._events.empty():
                        try:
                            if turn.cancelled():
                                raise CLIConnectionError("Kiro turn was closed")
                            result = await turn
                        except JSONRPCError as exc:
                            if not self._interrupt_requested or not _is_cancelled_rpc_error(exc):
                                raise
                            result = {"stopReason": "cancelled"}
                        if self._reported_credit_limit is not None:
                            usage = None if invalid_credits or not credit_summaries else _sum_credits(
                                credit_summaries.values())
                            if self._credit_ledger is not None:
                                total = await self._credit_ledger.complete(
                                    self._credit_scope, self._credit_reservation, usage)
                                total = _credit_amount(total)
                                if total < self._reported_credits or (usage is not None and
                                        total != _sum_credits((self._reported_credits, usage))):
                                    raise ProtocolError("Credit ledger returned an inconsistent total")
                                self._reported_credits = total
                                self._credit_reservation = None
                            elif usage is not None:
                                self._reported_credits = _sum_credits((self._reported_credits, usage))
                            self._credit_pending = False
                            if usage is None:
                                self._credit_unknown = True
                        reason = result.get("stopReason")
                        is_error = (reason in ("cancelled", "max_turn_requests") or completion_status == "aborted" or
                                    (completion_status == "failed" and reason != "refusal"))
                        turn_success = not is_error and reason != "refusal"
                        if self._initial_prompt_active:
                            self._initial_prompt_failed = is_error
                        assistant = take_assistant()
                        if assistant is not None:
                            yield assistant
                        for pending_id, merged in tool_calls.items():
                            # Calls whose input Kiro never reported are still announced once.
                            if pending_id not in announced_tools and pending_id not in completed_tools:
                                announced_tools.add(pending_id)
                                yield tool_message(pending_id, merged, subagent_tools.get(pending_id))
                        unique_user_id = (next(iter(replay_ids))
                                          if len(replay_ids) == 1 and not replay_id_invalid else None)
                        output = None
                        if self.options.output_format is not None and not is_error and reason != "refusal":
                            try:
                                output = _strict_json("".join(parts))
                                jsonschema.validate(output, self.options.output_format["schema"])
                            except (ValueError, jsonschema.ValidationError) as exc:
                                raise ResultError(f"Kiro did not return valid structured output: {exc}",
                                                  {"kind": "invalid_structured_output",
                                                   "result": "".join(parts), "errors": [str(exc)],
                                                   "session_id": self.session_id}) from exc
                        if self._sdk_stop_mode != "native":
                            stop_fields = {"stop_reason": reason, "last_agent_message": "".join(parts),
                                           "continued": False, "kiro_observed_continued": True}
                            stop_warnings: list[str] = []
                            await self._run_hooks("Stop", stop_fields, hook_system_messages=stop_warnings)
                            for warning in stop_warnings:
                                yield _hook_warning("Stop", warning, self.session_id)
                        mirrored_result = {"stop_reason": reason, "result": "".join(parts),
                                           "kiro_usage": kiro_usage, "is_error": is_error}
                        if result.get("_sdk_model_usage") is not None:
                            mirrored_result["kiro_model_usage"] = copy.deepcopy(result["_sdk_model_usage"])
                        if permission_denials:
                            mirrored_result["permission_denials"] = copy.deepcopy(permission_denials)
                        if self._turn_mirror_prompt_id is not None:
                            if unique_user_id is not None:
                                mirrored_result["kiro_sdk_unique_user_message_id"] = unique_user_id
                            await self._mirror("result", mirrored_result, prompt_key=self._turn_mirror_prompt_id)
                        else:
                            await self._mirror("result", mirrored_result)
                        await self._mirror_session_info()
                        await self._mirror_archive()
                        await self._flush_mirror()
                        while self._pending_mirror_errors:
                            yield self._pending_mirror_errors.popleft()
                        completed = True
                        elapsed = max(0, int(((self._turn_finished_at or time.monotonic()) -
                                              (self._turn_started_at or time.monotonic())) * 1000))
                        yield ResultMessage(self.session_id, reason, is_error, result="".join(parts), duration_ms=elapsed,
                                            model_turns=result.get("_sdk_model_turns"),
                                            structured_output=output, kiro_usage=kiro_usage,
                                            user_message_id=user_message_id,
                                            kiro_model_usage=copy.deepcopy(result.get("_sdk_model_usage")),
                                            permission_denials=permission_denials or None)
                        break
                    event_task = asyncio.create_task(self._events.get())
                    done, _ = await asyncio.wait([event_task, turn], return_when=asyncio.FIRST_COMPLETED)
                    if event_task not in done:
                        event_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await event_task
                        continue
                    event = event_task.result()
                    if isinstance(event, Exception):
                        raise event
                    kind = event.get("sessionUpdate")
                    replay_turn = event.pop("_sdk_replay_turn", None)
                    if kind == "sdk_background_wakeup":
                        continue
                    if kind == "sdk_permission_denial":
                        if event.get("turn") is turn:
                            permission_denials.append(event["denial"])
                        continue
                    if kind == "sdk_hook_warning":
                        if event.get("turn") is turn and event.get("session_id") == self.session_id:
                            yield _hook_warning(event["event"], event["message"], self.session_id)
                        continue
                    if kind == "sdk_mirror_error":
                        while self._pending_mirror_errors:
                            yield self._pending_mirror_errors.popleft()
                        continue
                    metadata = _nullable_mapping(_nullable_mapping(event.get("_meta")).get("kiro"))
                    subagent_id = metadata.get("agentSubtaskId")
                    if not isinstance(subagent_id, str) or not subagent_id:
                        subagent_id = None
                    tool_id = event.get("toolCallId")
                    if (kind in ("tool_call", "tool_call_update") and metadata.get("kind") == "agent-subtask"
                            and subagent_id and isinstance(tool_id, str) and tool_id):
                        subagent_parents[subagent_id] = tool_id
                    if kind == "session_info_update" and metadata.get("kind") == "turn_completion":
                        if subagent_id is None:
                            kiro_usage = metadata
                            if metadata.get("status") in ("success", "failed", "aborted"):
                                completion_status = metadata["status"]
                        if self._reported_credit_limit is not None:
                            credits = _reported_turn_credits(metadata)
                            turn_id = metadata.get("turnId")
                            if credits is None or (turn_id is not None and not isinstance(turn_id, str)):
                                invalid_credits = True
                            else:
                                key = turn_id or ""
                                credit_summaries[key] = max(credits, credit_summaries.get(key, Decimal(0)))
                    elif kind == "session_info_update" and metadata.get("kind") == "user_message_id_assigned":
                        user_message_id = metadata.get("userMessageId")
                        if self._turn_mirror_prompt_id is not None and replay_turn is turn:
                            reported_subagent = metadata.get("agentSubtaskId")
                            if ("agentSubtaskId" in metadata and
                                    (not isinstance(reported_subagent, str) or not reported_subagent)):
                                replay_id_invalid = True
                            elif subagent_id is None:
                                candidate = metadata.get("userMessageId")
                                if (not isinstance(candidate, str) or not candidate.strip() or
                                        len(candidate) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in candidate)):
                                    replay_id_invalid = True
                                elif len(replay_ids) < 2:
                                    replay_ids.add(candidate)
                    elif kind == "notification" and event.get("method") == "_kiro.dev/metadata":
                        params = _nullable_mapping(event.get("params"))
                        if "meteringUsage" in params:
                            kiro_usage = params
                    if subagent_id and kind in ("agent_message_chunk", "agent_thought_chunk"):
                        if (self.options.forward_subagent_text and
                                (kind != "agent_thought_chunk" or self.options.include_thoughts)):
                            parent_id = subagent_parents.get(subagent_id)
                            if self.options.include_partial_messages:
                                stream = self._partial_stream_event(event, parent_tool_call_id=parent_id,
                                                                    subagent_id=subagent_id)
                                if stream is not None:
                                    yield stream
                            content = _nullable_mapping(event.get("content"))
                            if isinstance(content, dict) and content.get("type") == "text":
                                text = content.get("text", "")
                                block = ThoughtBlock(text) if kind == "agent_thought_chunk" else TextBlock(text)
                                yield AgentMessage([block], message_id=event.get("messageId"), session_id=self.session_id,
                                                   agent_id=subagent_id, parent_tool_call_id=parent_id)
                        continue
                    is_subagent_tool = (kind in ("tool_call", "tool_call_update") and subagent_id and
                                        metadata.get("kind") != "agent-subtask")
                    if (not is_subagent_tool and kind == "tool_call_update" and
                            isinstance(tool_id, str) and tool_id in subagent_tools):
                        subagent_id = subagent_tools[tool_id]
                        is_subagent_tool = True
                    if kind in ("tool_call", "tool_call_update"):
                        tool_id = event.get("toolCallId")
                        valid_tool_id = tool_id if isinstance(tool_id, str) and tool_id else None
                        if kind == "tool_call" and not is_subagent_tool:
                            assistant = take_assistant()
                            if assistant is not None:
                                yield assistant
                        previous = tool_calls.get(valid_tool_id, {}) if valid_tool_id else {}
                        merged = dict(event) if kind == "tool_call" else _merge_tool_updates(previous, event)
                        owner = subagent_id if is_subagent_tool else None
                        if valid_tool_id:
                            tool_calls[valid_tool_id] = merged
                            if owner:
                                subagent_tools.setdefault(valid_tool_id, owner)
                        terminal = kind == "tool_call_update" and event.get("status") in ("completed", "failed")
                        if ("rawInput" in merged and not terminal and
                                (not valid_tool_id or valid_tool_id not in announced_tools)):
                            if valid_tool_id:
                                announced_tools.add(valid_tool_id)
                            yield tool_message(valid_tool_id, merged, owner)
                        if terminal and (not valid_tool_id or valid_tool_id not in completed_tools):
                            if valid_tool_id:
                                completed_tools.add(valid_tool_id)
                                announced_tools.add(valid_tool_id)
                            output = merged.get("rawOutput", merged.get("content"))
                            post_warnings: list[str] = []
                            if event["status"] == "completed":
                                await self._run_posttool_hooks(self._posttool_hook_fields(merged, output),
                                    valid_tool_id, hook_system_messages=post_warnings)
                            yield tool_message(valid_tool_id, merged, owner)
                            for warning in post_warnings:
                                yield _hook_warning("PostToolUse", warning, self.session_id)
                        yield KiroEvent(kind, event)
                        continue
                    if self.options.include_partial_messages and (kind == "agent_message_chunk" or
                                                                 (kind == "agent_thought_chunk" and self.options.include_thoughts)):
                        stream = self._partial_stream_event(event)
                        if stream is not None:
                            yield stream
                    if kind == "agent_message_chunk" and _nullable_mapping(event.get("content")).get("type") == "text":
                        text = event["content"].get("text", "")
                        parts.append(text)
                        message_id = event.get("messageId")
                        if message_id and assistant_message_id and message_id != assistant_message_id:
                            assistant = take_assistant()
                            if assistant is not None:
                                yield assistant
                        if message_id:
                            assistant_message_id = message_id
                        if assistant_blocks and isinstance(assistant_blocks[-1], TextBlock):
                            assistant_blocks[-1].text += text
                        else:
                            assistant_blocks.append(TextBlock(text))
                    elif kind == "agent_thought_chunk":
                        if self.options.include_thoughts:
                            text = _nullable_mapping(event.get("content")).get("text", "")
                            if assistant_blocks and isinstance(assistant_blocks[-1], ThoughtBlock):
                                assistant_blocks[-1].text += text
                            else:
                                assistant_blocks.append(ThoughtBlock(text))
                    else:
                        yield KiroEvent(kind or "unknown", event)
            finally:
                if event_task is not None and not event_task.done():
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                was_interrupted = self._interrupt_requested
                if not turn.done():
                    with contextlib.suppress(Exception):
                        await self.interrupt()
                    turn.cancel()
                cleanup_failed = False
                cleanup_cancelled = None
                # Kiro can finish a turn while an inbound callback is still
                # waiting. Retire those requests before a later turn starts;
                # a late approval must never be applied to the new turn.
                responses = []
                for record in list(self._inbound_requests.values()):
                    if record["turn"] is turn:
                        response = self._mark_inbound_cancelled(record)
                        if response is not None:
                            responses.append((record, response))
                if responses:
                    async def cancel_inbounds():
                        for record, response in responses:
                            try:
                                await self._send(response)
                            finally:
                                self._retire_inbound(record)
                    response_task = asyncio.create_task(cancel_inbounds())
                    self._handler_tasks.add(response_task)
                    response_task.add_done_callback(self._handler_finished)
                    try:
                        done, _ = await asyncio.wait((response_task,), timeout=1)
                        if not done:
                            cleanup_failed = True
                            response_task.cancel()
                        else:
                            await response_task
                    except asyncio.CancelledError as exc:
                        cleanup_failed = True
                        cleanup_cancelled = exc
                        response_task.cancel()
                    except Exception:
                        cleanup_failed = True
                if self._pending_session_plan is not None and self._pending_session_plan[1] is turn:
                    session_id, _, origin, _ = self._pending_session_plan
                    self._pending_session_plan = ((session_id, None, origin, True)
                                                  if completed and turn_success and not was_interrupted and
                                                  not cleanup_failed else None)
                self._turn = None
                self._turn_prompt_warnings = ()
                self._turn_mirror_prompt_id = None
                self._turn_model = ""
                self._initial_prompt_active = False
                self._interrupt_requested = False
                self._turn_started_at = None
                self._turn_finished_at = None
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await turn
                if completed and was_interrupted and self._sdk_tool_interrupt_mode != "kiro":
                    async with self._sdk_interrupt_lock:
                        if self._reader_error is None:
                            for bridge in self._mcp_bridges:
                                bridge.resume_tools()
                            if self._sdk_tool_interrupt_mode == "cancel-observed":
                                for bridge in self._external_mcp_bridges:
                                    bridge.resume_tools()
                            self._sdk_tools_suspended = False
                        else:
                            cleanup_failed = True
                if not completed:
                    with contextlib.suppress(Exception):
                        await self.disconnect()
                elif cleanup_failed:
                    await self.disconnect()
                if cleanup_cancelled is not None:
                    raise cleanup_cancelled

    async def receive_messages(self) -> AsyncIterator[Any]:
        """Receive current input, or wait for future turns when explicitly configured."""
        self._check_elicitation_reentry()
        if self.options.receive_messages_mode != self._receive_messages_mode:
            raise ValueError("Create a new client after changing receive_messages_mode")
        receiver = (self.receive_messages_continuously() if self._receive_messages_mode == "continuous"
                    else self._receive_current_messages())
        try:
            async for message in receiver:
                yield message
        finally:
            await receiver.aclose()

    async def _receive_current_messages(self) -> AsyncIterator[Any]:
        if self._continuous_messages_active:
            raise CLIConnectionError("A continuous message receiver is active")
        if self._input_collecting:
            raise CLIConnectionError("Streaming input is still being buffered")
        if self._turn is None and self._stream_input is None and self._queued_after_initial is None:
            raise CLIConnectionError("No active query")
        generation = self._connection_generation
        try:
            while self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
                async for message in self.receive_response():
                    yield message
        finally:
            if (self._connection_generation == generation and self._connected and not self._disconnecting and
                    (self._stream_input is not None or self._queued_after_initial is not None)):
                await self.disconnect()

    def _signal_message_activity(self) -> None:
        if self._message_activity is not None:
            self._message_activity.set()

    async def receive_messages_continuously(self) -> AsyncIterator[Any]:
        """Receive successive turns and enabled owned task messages until disconnect.

        Only one receiver may consume a session at a time. A completed result
        is delivered after turn cleanup, so the caller can submit another turn
        while handling that result. Enabled SDK background lifecycle notifications
        may arrive while idle; they do not start another model turn.
        """
        self._check_elicitation_reentry()
        if not self._connected or self._disconnecting:
            raise CLIConnectionError("Call connect() first")
        if self._continuous_messages_active or self._lock.locked():
            raise CLIConnectionError("Another message receiver is active")
        activity = self._message_activity
        if activity is None:
            raise CLIConnectionError("Call connect() first")
        self._continuous_messages_active = True
        generation = self._connection_generation
        try:
            while self._connected and not self._disconnecting and self._connection_generation == generation:
                if self._input_collecting:
                    activity.clear()
                    if (not self._input_collecting or not self._connected or self._disconnecting or
                            self._connection_generation != generation):
                        continue
                    await activity.wait()
                    continue
                if self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
                    response = self._receive_response_raw()
                    result = None
                    try:
                        async for message in response:
                            if isinstance(message, ResultMessage):
                                result = message
                            else:
                                yield message
                    finally:
                        await response.aclose()
                    if result is not None:
                        yield result
                    continue
                if self._reader_error is not None:
                    raise CLIConnectionError("Kiro connection reader failed") from self._reader_error
                activity.clear()
                if (not self._connected or self._disconnecting or self._connection_generation != generation or
                        self._reader_error is not None or self._turn is not None or
                        self._stream_input is not None or self._queued_after_initial is not None):
                    continue
                await activity.wait()
        finally:
            try:
                if (self._connection_generation == generation and self._connected and not self._disconnecting and
                        (self._stream_input is not None or self._queued_after_initial is not None)):
                    await self.disconnect()
            finally:
                self._continuous_messages_active = False

    @property
    def last_mirror_error(self) -> Optional[MirrorErrorMessage]:
        """Most recent nonfatal mirror failure on this client, including after disconnect."""
        return copy.deepcopy(self._last_mirror_error)

    async def interrupt(self) -> None:
        self._check_elicitation_reentry()
        if (_FILE_CHANGED_CONTEXT.get() is self or self in _CLIENT_AUTH_CONTEXT.get() or
                asyncio.current_task() in self._client_auth_tasks):
            raise CLIConnectionError("FileChanged and authentication callbacks cannot control the client")
        async with self._sdk_interrupt_lock:
            self._extension_hook_revision += 1
            workspace_error = None
            try:
                pass
            except Exception as exc:
                workspace_error = exc
            if not self.session_id:
                if workspace_error is not None:
                    raise workspace_error
                return
            turn = self._turn
            cancelled = []
            active_turn = turn is not None and not turn.done()
            was_suspended = self._sdk_tools_suspended
            if self._sdk_tool_interrupt_mode != "kiro":
                self._sdk_tools_suspended = True
                for bridge in self._mcp_bridges:
                    bridge.suspend_tools()
                if self._sdk_tool_interrupt_mode == "cancel-observed":
                    for bridge in self._external_mcp_bridges:
                        bridge.suspend_tools()
            if active_turn:
                self._interrupt_requested = True
                for record in list(self._inbound_requests.values()):
                    if record["turn"] is turn and record["session_id"] in (None, self.session_id):
                        response = self._mark_inbound_cancelled(record)
                        if response is not None:
                            cancelled.append((record, response))
            try:
                await self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})
                for record, response in cancelled:
                    await self._send(response)
                    self._retire_inbound(record)
                if self._sdk_tools_suspended:
                    if self._sdk_tool_interrupt_mode == "cancel-observed":
                        bridges = [*self._mcp_bridges, *self._external_mcp_bridges]
                        operation = asyncio.gather(
                            *(bridge.request_tool_cancellation() for bridge in bridges),
                            return_exceptions=True)
                        try:
                            done, _ = await asyncio.wait((operation,), timeout=3)
                            if not done:
                                raise asyncio.TimeoutError("MCP cancellation request timed out")
                            failures = [result for result in operation.result()
                                        if isinstance(result, BaseException)]
                            if failures:
                                raise CLIConnectionError("MCP cancellation request could not be delivered") from failures[0]
                        finally:
                            if not operation.done():
                                operation.cancel()
                                def consume(done: asyncio.Future) -> None:
                                    if not done.cancelled():
                                        done.exception()
                                operation.add_done_callback(consume)
                    else:
                        await asyncio.wait_for(
                            asyncio.gather(*(bridge.cancel_tools() for bridge in self._mcp_bridges)), 3)
                    if not active_turn and not was_suspended:
                        for bridge in self._mcp_bridges:
                            bridge.resume_tools()
                        if self._sdk_tool_interrupt_mode == "cancel-observed":
                            for bridge in self._external_mcp_bridges:
                                bridge.resume_tools()
                        self._sdk_tools_suspended = False
            except BaseException as exc:
                if self._reader_error is None:
                    error = CLIConnectionError("Interrupt delivery failed; connection cannot continue")
                    error.__cause__ = exc
                    self._reader_error = error
                await self._abort_for_guard(force=True)
                raise
            if workspace_error is not None:
                raise workspace_error

    async def steer(self, message: str) -> dict[str, Any]:
        """Send a Kiro V3 steering message to an active turn."""
        if self.options.agent_engine != "v3":
            raise ProtocolError("Steering requires Kiro V3")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Steering message must not be empty")
        if self._turn is None or self._turn.done():
            raise CLIConnectionError("No active turn to steer")
        return await self._request("_session/steer", {"sessionId": self.session_id, "message": message})

    async def clear_steering(self) -> dict[str, Any]:
        """Discard queued Kiro V3 steering messages for the current session."""
        if self.options.agent_engine != "v3":
            raise ProtocolError("Clearing steering requires Kiro V3")
        if not self.session_id:
            raise CLIConnectionError("Call connect() first")
        return await self._request("_session/steer/clear", {"sessionId": self.session_id})

    async def set_model(self, model: Optional[str] = None) -> None:
        """Select a Kiro model, or the local CLI's reported default when omitted."""
        await self._apply_model(model, persist_option=True)

    async def _apply_model(self, model: Optional[str], *, persist_option: bool) -> None:
        self._check_elicitation_reentry()
        await self._apply_model_core(model, persist_option=persist_option)

    async def _resolve_model_selection(self, model: Optional[str]) -> str:
        if model is None:
            if self._client_auth_snapshot[0] == "client":
                raise ProtocolError("Client-owned authentication requires an explicit model")
            if not self.session_id:
                raise CLIConnectionError("Call connect() first")
            if self._transport is not None:
                raise ProtocolError("A remote transport must select an explicit model")
            from .sessions import _list_cli
            try:
                listing = await asyncio.wait_for(
                    _list_cli("--list-models", self.options.cwd, self._cli_path, self._configured_env()),
                    self.options.startup_timeout)
            except asyncio.TimeoutError as exc:
                raise CLIConnectionError("Timed out retrieving Kiro's default model") from exc
            model = listing.get("default_model") if isinstance(listing, dict) else None
            models = listing.get("models") if isinstance(listing, dict) else None
            if (not isinstance(model, str) or not model.strip() or "\0" in model or
                    not isinstance(models, list) or
                    not any(isinstance(item, dict) and item.get("model_id") == model for item in models)):
                raise ProtocolError("Kiro did not report an available default model")
        if not isinstance(model, str) or not model.strip() or "\0" in model:
            raise ValueError("model must be a nonempty Kiro model ID")
        return model

    async def _apply_model_core(self, model: Optional[str], *, persist_option: bool) -> None:
        self._check_elicitation_reentry()
        model = await self._resolve_model_selection(model)
        if self.options.agent_engine == "v3":
            result = await self._set_config_option_core("model", model)
            confirmed = (any(option.get("id") == "model" and option.get("currentValue") == model
                             for option in result.get("configOptions", [])))
            if not confirmed:
                raise ProtocolError("Kiro did not confirm the requested model")
        else:
            await self._request("session/set_model", {"sessionId": self.session_id, "modelId": model})
        if persist_option:
            self._model_option = model
        self._session_model = model
        if self._startup_fallback is not None:
            self._startup_fallback = (model, self._startup_fallback[1])

    async def set_mode(self, mode: str) -> None:
        self._check_elicitation_reentry()
        self._check_mode_change(mode)
        try:
            await self._request("session/set_mode", {"sessionId": self.session_id, "modeId": mode})
            self._current_mode = mode
        finally:
            pass

    def _check_mode_change(self, mode: str, *, _session_plan_transition: bool = False) -> None:
        if mode == "plan" and self._policy_presets:
            raise ProtocolError("Plan mode cannot be combined with Kiro policy presets")
        if (isinstance(mode, str) and mode in self._mapped_agent_profiles and mode != self._protected_mode and
                not (self._initializing and mode == self._selected_agent())):
            raise ProtocolError("Reconnect with the mapped agent selected to verify its permissions")
        protected = self._protected_mode
        if protected is None and self._has_profile_restrictions() and self._agent_file:
            protected = self._agent_file.stem
        if protected and mode != protected:
            raise ProtocolError("Changing mode would discard configured agent restrictions")

    async def set_config_option(self, config_id: str, value: str) -> Any:
        self._check_elicitation_reentry()
        return await self._set_config_option_core(config_id, value)

    async def _set_config_option_core(self, config_id: str, value: str) -> Any:
        self._check_elicitation_reentry()
        if config_id == "mode":
            self._check_mode_change(value)
        if config_id == "sandbox":
            if self._sandbox_required and value != "enabled":
                raise ProtocolError("Cannot disable the required command sandbox")
        try:
            result = await self._request("session/set_config_option", {"sessionId": self.session_id, "configId": config_id, "value": value})
            if self.session_info is not None and isinstance(result.get("configOptions"), list):
                self.session_info["configOptions"] = result["configOptions"]
            if config_id == "model":
                selected = _selected_model(result)
                if selected is not None:
                    self._session_model = selected
            return result
        finally:
            pass

    async def _set_session_native_plan(self, mode: str, *, expected_origin: Optional[str] = None,
                                       allow_stream: bool = False,
                                       guard: Optional[Callable[[], bool]] = None) -> None:
        self._check_elicitation_reentry()
        raise ValueError("Deferred Plan support is not enabled")

    async def set_permission_mode(self, mode: str) -> None:
        self._check_elicitation_reentry()
        if mode not in PERMISSION_MODES or mode is None:
            raise ValueError(f"permission_mode must be one of {', '.join(m for m in PERMISSION_MODES if m)}")
        if (self._connected and (self._mapped_tool_permissions is not None or self._mapped_agent_profiles) and
                mode != self._permission_mode):
            raise ProtocolError("Reconnect with the intended mapped tool permission mode")
        if mode == "plan":
            if self.options.agent_engine != "v3":
                raise ValueError("plan permission mode requires Kiro V3")
            self._check_mode_change("plan")
            if self._permission_mode != "plan":
                self._mode_before_plan = self._current_mode
            await self.set_mode("plan")
        elif self._permission_mode == "plan":
            if self._current_mode == "plan":
                if not self._mode_before_plan:
                    raise ProtocolError("Previous mode is unknown; choose a mode with set_mode()")
                await self.set_mode(self._mode_before_plan)
            self._mode_before_plan = None
        self._permission_mode = mode

    @property
    def sdk_permission_mode(self) -> str:
        """Return the SDK's current ACP decision mode, not Kiro's effective policy."""
        return (self._permission_mode or "ask")

    async def get_server_info(self) -> Optional[dict[str, Any]]:
        if self.server_info is None:
            return None
        result = copy.deepcopy(self.server_info)
        commands = await self.get_kiro_available_commands()
        if commands is not None and "commands" not in result:
            result["commands"] = commands
        return result

    async def get_kiro_available_commands(self) -> Optional[list[dict[str, Any]]]:
        """Return the latest bounded ACP command list for the current session, if reported."""
        if not self.session_id:
            return None
        return copy.deepcopy(self._available_commands_by_session.get(self.session_id))

    async def get_context_usage(self) -> Optional[dict[str, Any]]:
        """Return Kiro context metadata with percentage when Kiro reports it."""
        return _normalize_context_usage(await self.get_context_usage_raw())

    async def get_context_usage_raw(self) -> Optional[dict[str, Any]]:
        """Return the latest unmodified Kiro context metadata for this session."""
        scope = self.session_id if self.session_id in self._context_metadata_by_session else None
        result = self._context_metadata_by_session.get(scope)
        return copy.deepcopy(result) if result is not None else None

    @property
    def reported_credits(self) -> float:
        """Credits reported for completed turns while this client tracked a limit."""
        return float(self._reported_credits)

    async def get_mcp_status(self) -> Optional[dict[str, Any]]:
        """Return the MCP server status Kiro reported for this session."""
        return self._mcp_status_tracker.get(self.session_id)

    async def get_mcp_status_raw(self) -> Optional[dict[str, Any]]:
        """Return the last unmodified Kiro MCP status notification."""
        return self._mcp_status_tracker.raw(self.session_id)

    async def _find_latest_session(self, cwd: str) -> Optional[str]:
        if self.options.agent_engine == "v3":
            self._require_session_capability("list")
            sessions = []
            cursor = None
            seen = set()
            pages, size = 0, 0
            guard = self._prompt_send_guard if self._client_auth_control_active else None
            while True:
                if pages >= 100:
                    raise ProtocolError("Kiro session listing exceeds the page limit")
                if guard is not None and (await guard() is not True or self._prompt_send_guard is not guard):
                    raise PermissionError("Snapshot continuation authorization changed")
                params = {"cwd": cwd}
                if cursor is not None:
                    params["cursor"] = cursor
                page = await self._request("session/list", params)
                if guard is not None and (await guard() is not True or self._prompt_send_guard is not guard):
                    raise PermissionError("Snapshot continuation authorization changed")
                if not isinstance(page, dict) or not isinstance(page.get("sessions"), list):
                    raise ProtocolError("Invalid Kiro session listing")
                pages += 1
                try:
                    size += len(json.dumps(page, ensure_ascii=True).encode("ascii"))
                except (TypeError, ValueError):
                    raise ProtocolError("Invalid Kiro session listing") from None
                if len(sessions) + len(page["sessions"]) > 10000 or size > 8 * 1024 * 1024:
                    raise ProtocolError("Kiro session listing exceeds the size limit")
                sessions.extend(page["sessions"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or len(cursor) > 4096:
                    raise ProtocolError("Invalid Kiro session listing cursor")
                if cursor in seen:
                    raise ProtocolError("Kiro session listing repeated a cursor")
                seen.add(cursor)
        else:
            from .sessions import _list_sessions_for_process_user, list_sessions
            try:
                operation = (_list_sessions_for_process_user(cwd, self._cli_path, self._configured_env(),
                                                              self._process_user) if self._process_user is not None else
                             list_sessions(cwd=cwd, cli_path=self._cli_path, env=self._configured_env()))
                sessions = await asyncio.wait_for(operation, self.options.startup_timeout)
            except asyncio.TimeoutError as exc:
                raise CLIConnectionError("Timed out listing Kiro sessions") from exc
            sessions = [s for s in sessions if s.get("source") == "v2"]
        sessions = [s for s in sessions if isinstance(s, dict) and isinstance(s.get("sessionId"), str)]
        if not sessions:
            return None
        if len(sessions) == 1:
            return sessions[0]["sessionId"]
        def updated(session: dict[str, Any]) -> float:
            try:
                value = parse_timestamp(session["updatedAt"])
                return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ProtocolError("Cannot determine the latest Kiro session") from exc
        return max(sessions, key=updated)["sessionId"]

    async def _require_sandbox(self) -> None:
        if (self._sandbox_status or {}).get("status") != "active":
            self._sandbox_event.clear()
            await self._request("_kiro/sandbox/applyConfig", {"configId": "sandbox", "value": "enabled"})
            if (self._sandbox_status or {}).get("status") != "active":
                try:
                    await asyncio.wait_for(self._sandbox_event.wait(), self.options.startup_timeout)
                except asyncio.TimeoutError as exc:
                    raise ProtocolError("Kiro did not report sandbox status") from exc
        if (self._sandbox_status or {}).get("status") != "active":
            raise ProtocolError(f"Kiro sandbox unavailable: {self._sandbox_status}")

    async def get_sandbox_status(self) -> Optional[dict[str, Any]]:
        """Return the latest status reported by Kiro's command sandbox."""
        return self._sandbox_status

    async def list_kiro_permissions(self) -> dict[str, Any]:
        if self.options.agent_engine != "v3":
            raise ProtocolError("Capability permissions require Kiro V3")
        return await self._request("_kiro/permissions/list", {"sessionId": self.session_id})

    async def explain_kiro_permission(self, capability: str, resource: str) -> dict[str, Any]:
        if self.options.agent_engine != "v3":
            raise ProtocolError("Capability permissions require Kiro V3")
        return await self._request("_kiro/permissions/explain", {"sessionId": self.session_id,
                                   "capability": capability, "resource": resource})

    async def get_kiro_account_usage(self) -> dict[str, Any]:
        """Return Kiro's account usage response; it is not a per-turn cost limit."""
        if self.options.agent_engine != "v3":
            raise ProtocolError("Account usage requires Kiro V3")
        return await self._request("_kiro/account/getUsage", {})

    async def list_kiro_powers(self) -> dict[str, Any]:
        """Return installed Kiro powers and any load errors reported by Kiro."""
        if self.options.agent_engine != "v3":
            raise ProtocolError("Powers listing requires Kiro V3")
        return await self._request("_kiro/powers/list", {})

    async def list_kiro_hooks(self, *, trigger: Optional[str] = None, tool_id: Optional[str] = None,
                              tool_tags: Optional[list[str]] = None,
                              include_disabled: bool = False) -> dict[str, Any]:
        """Return Kiro's hook listing for this session without changing its hook guards."""
        if self.options.agent_engine != "v3" or self.options.enable_kiro_hooks is not True:
            raise ProtocolError("Hook listing requires enabled Kiro V3 hooks")
        trigger, tool_id, tool_tags, include_disabled = _normalize_hook_listing_filters(
            trigger, tool_id, tool_tags, include_disabled)
        async with self._disconnect_lock:
            if not self._connected or not self.session_id or self._reader_error is not None:
                raise CLIConnectionError("Call connect() first and use a healthy connection")
            if not self._hook_listing_negotiated:
                raise ProtocolError("Reconnect with Kiro V3 hooks enabled before listing hooks")
            self._validate_unsupported_limits()
            params: dict[str, Any] = {"sessionId": self.session_id, "includeDisabled": include_disabled}
            if trigger is not None:
                params["trigger"] = trigger
            if tool_id is not None:
                params["toolId"] = tool_id
            if tool_tags is not None:
                params["toolTags"] = list(tool_tags)
            result = await self._request("_kiro/hooks/list", params)
            if (not isinstance(result, dict) or not isinstance(result.get("hooks"), list) or
                    any(not isinstance(hook, dict) for hook in result["hooks"])):
                raise ProtocolError("Kiro returned an invalid hook listing")
            return result

    async def get_kiro_spec_task_statuses(self, *, tasks_file_path: str, feature_name: str,
                                          workspace_paths: list[str]) -> dict[str, Any]:
        """Return Kiro spec task statuses from paths on the Kiro host."""
        if self.options.agent_engine != "v3":
            raise ProtocolError("Spec task statuses require Kiro V3")
        def absolute(path: Any) -> bool:
            return (isinstance(path, str) and bool(path) and "\0" not in path and
                    (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()))
        if not absolute(tasks_file_path):
            raise ValueError("tasks_file_path must be a non-empty absolute path")
        if not isinstance(feature_name, str) or not feature_name:
            raise ValueError("feature_name must not be empty")
        if (not isinstance(workspace_paths, list) or not workspace_paths or
                any(not absolute(path) for path in workspace_paths)):
            raise ValueError("workspace_paths must be a non-empty list of absolute paths")
        return await self._request("_kiro/spec/getTaskStatuses",
                                   {"tasksFilePath": tasks_file_path, "featureName": feature_name,
                                    "workspacePaths": workspace_paths})

    async def _kiro_code_intelligence(self, subcommand: str) -> dict[str, Any]:
        if self.options.agent_engine != "v3":
            raise ProtocolError("Code intelligence requires Kiro V3")
        async with self._query_lock, self._disconnect_lock:
            if not self._connected or not self.session_id or self._reader_error is not None:
                raise CLIConnectionError("Call connect() first and use a healthy connection")
            if (not isinstance(self.options.enable_kiro_code_intelligence, bool) or
                    self.options.enable_kiro_code_intelligence != self._code_intelligence_option):
                raise ValueError("Reconnect after changing enable_kiro_code_intelligence")
            if not self._code_intelligence_enabled:
                raise ProtocolError("Enable Kiro code intelligence before connecting")
            if self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
                raise CLIConnectionError("Consume the current response before using code intelligence")
            self._require_kiro_extension("_kiro/codeIntelligence")
            try:
                request = self._request("_kiro/codeIntelligence",
                                        {"sessionId": self.session_id, "subcommand": subcommand})
                if self.options.startup_timeout is None:
                    result = await request
                else:
                    result = await asyncio.wait_for(request, self.options.startup_timeout)
                if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                    raise ProtocolError("Kiro returned invalid code intelligence status")
                return result
            except BaseException as exc:
                if subcommand == "init":
                    error = CLIConnectionError("Code intelligence initialization outcome is uncertain")
                    error.__cause__ = exc
                    self._reader_error = error
                    await self._abort_for_guard(force=True)
                raise

    async def get_kiro_code_intelligence_status(self) -> dict[str, Any]:
        """Return Kiro's workspace code intelligence and language server status."""
        return await self._kiro_code_intelligence("status")

    async def initialize_kiro_code_intelligence(self) -> dict[str, Any]:
        """Ask Kiro to initialize workspace code intelligence and language servers."""
        return await self._kiro_code_intelligence("init")

    async def get_kiro_code_overview(self) -> dict[str, Any]:
        """Ask Kiro for an overview of the current workspace."""
        return await self._kiro_code_intelligence("overview")

    async def _kiro_context(self, subcommand: str, *, path: Optional[str] = None,
                            force: bool = False) -> dict[str, Any]:
        if self.options.agent_engine != "v3":
            raise ProtocolError("Session context management requires Kiro V3")
        if not self.session_id:
            raise CLIConnectionError("Call connect() first")
        params: dict[str, Any] = {"sessionId": self.session_id, "subcommand": subcommand}
        if path is not None:
            if not isinstance(path, str) or not path.strip() or "\0" in path:
                raise ValueError("path must be a non-empty string")
            params["path"] = path
        if force:
            params["force"] = True
        if subcommand == "show":
            return await self._request("_kiro/session/context", params)
        async with self._query_lock:
            if self._turn is not None:
                raise CLIConnectionError("Consume the current response before changing context")
            return await self._request("_kiro/session/context", params)

    async def list_kiro_context(self) -> dict[str, Any]:
        """List Kiro's context files and, when available, its context breakdown."""
        return await self._kiro_context("show")

    async def add_kiro_context(self, path: str, *, force: bool = False) -> dict[str, Any]:
        """Add a path to Kiro's session context; paths refer to the Kiro host."""
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        if path is None:
            raise ValueError("path must be a non-empty string")
        return await self._kiro_context("add", path=path, force=force)

    async def remove_kiro_context(self, path: str) -> dict[str, Any]:
        """Remove a path from Kiro's session context without deleting the file."""
        if path is None:
            raise ValueError("path must be a non-empty string")
        return await self._kiro_context("remove", path=path)

    async def clear_kiro_context(self) -> dict[str, Any]:
        """Remove all context-file references from this Kiro session."""
        return await self._kiro_context("clear")

    async def reconnect_mcp_server(self, server_name: str, *, start_oauth: bool = False) -> dict[str, Any]:
        """Reconnect a server bound to the current Kiro V3 session."""
        self._check_elicitation_reentry()
        if self.options.agent_engine != "v3":
            raise ProtocolError("MCP server reconnection requires Kiro V3")
        if not isinstance(server_name, str) or not server_name.strip() or "\0" in server_name:
            raise ValueError("server_name must be a nonempty string")
        if not isinstance(start_oauth, bool):
            raise ValueError("start_oauth must be a boolean")
        async with self._query_lock, self._disconnect_lock, self._sdk_interrupt_lock:
            if not self._connected or not self.session_id or self._reader_error is not None:
                raise CLIConnectionError("Call connect() first and use a healthy connection")
            if self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
                raise CLIConnectionError("Consume the current response before reconnecting MCP")
            wire_name = server_name
            inline = self._plugin_inline_mcp_configs.get(server_name)
            if inline is not None:
                self._validate_unsupported_limits()
                if self._selected_agent() != inline[0] or self._current_mode != inline[0]:
                    raise ProtocolError("Mediated inline MCP reconnect requires its selected main agent")
                wire_name = inline[1]
            bridges = [bridge for bridge in self._named_external_mcp_bridges.get(server_name, [])
                       if isinstance(bridge, RemoteMcpBridge)]
            if start_oauth and bridges:
                bridge = bridges[0]
                if bridge.oauth is None:
                    raise ProtocolError("SDK-relayed OAuth requires mcp_oauth configuration")
                if not callable(getattr(bridge.oauth.context.storage, "clear_tokens", None)):
                    raise ProtocolError("OAuth token storage must implement async clear_tokens")
                operation = asyncio.create_task(bridge._clear_oauth_tokens())
                try:
                    done, _ = await asyncio.wait((operation,), timeout=3)
                    if not done:
                        raise asyncio.TimeoutError("OAuth token clearing timed out")
                    operation.result()
                    # Kiro sees a stdio bridge; OAuth is handled by the SDK on a 401.
                    return await self._request("_kiro/mcp/resetServer", {"sessionId": self.session_id,
                                               "serverName": wire_name, "startOAuth": False})
                except BaseException as exc:
                    if not operation.done():
                        operation.cancel()
                        operation.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                    if self._reader_error is None:
                        self._reader_error = CLIConnectionError(
                            "OAuth restart outcome is uncertain; connection cannot continue")
                    await self._abort_for_guard(force=True)
                    if isinstance(exc, Exception):
                        raise CLIConnectionError("OAuth restart failed; connection cannot continue") from None
                    raise
            return await self._request("_kiro/mcp/resetServer", {"sessionId": self.session_id,
                                       "serverName": wire_name, "startOAuth": start_oauth})

    async def revoke_mcp_oauth_tokens(self, server_name: str) -> dict[str, bool]:
        """Request advertised OAuth revocation, then disconnect; flags mean HTTP acceptance."""
        from .oauth_errors import sanitized_oauth
        return await sanitized_oauth(self._revoke_mcp_oauth_tokens(server_name),
            "MCP OAuth revocation failed; disconnect again if cleanup is incomplete", CLIConnectionError,
            safe_errors={"Consume the current response before revoking MCP OAuth tokens": CLIConnectionError,
                         "Call connect() first and use a healthy connection": CLIConnectionError,
                         "server_name must be a nonempty string": ValueError,
                         "Reconnect after changing sdk_mcp_toggle_mode": ValueError,
                         "Server must use SDK-relayed MCP OAuth": ProtocolError})

    async def _revoke_mcp_oauth_tokens(self, server_name: str) -> dict[str, bool]:
        """Request server-side OAuth token revocation and disconnect this client.

        The returned flags report which token types received HTTP 200 from the
        advertised revocation endpoint; they do not prove server invalidation.
        """
        self._check_elicitation_reentry()
        if not isinstance(server_name, str) or not server_name.strip() or "\0" in server_name:
            raise ValueError("server_name must be a nonempty string")
        async with self._query_lock, self._disconnect_lock, self._sdk_interrupt_lock:
            if not self._connected or not self.session_id or self._reader_error is not None:
                raise CLIConnectionError("Call connect() first and use a healthy connection")
            if self.options.sdk_mcp_toggle_mode != self._sdk_mcp_toggle_mode:
                raise ValueError("Reconnect after changing sdk_mcp_toggle_mode")
            if self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
                raise CLIConnectionError("Consume the current response before revoking MCP OAuth tokens")
            bridges = [bridge for bridge in self._named_external_mcp_bridges.get(server_name, [])
                       if isinstance(bridge, RemoteMcpBridge)]
            if not bridges or any(bridge.oauth is None for bridge in bridges):
                raise ProtocolError("Server must use SDK-relayed MCP OAuth")
            if len({id(bridge.oauth) for bridge in bridges}) != 1:
                raise ProtocolError("Server OAuth state is inconsistent")
            try:
                try:
                    # Stop live MCP streams before revocation so they cannot
                    # continue using or refresh the token during this operation.
                    await asyncio.wait_for(asyncio.gather(*(bridge.close() for bridge in bridges)), 10)
                    return await asyncio.wait_for(bridges[0]._revoke_oauth_tokens(), 40)
                except Exception:
                    raise CLIConnectionError("MCP OAuth revocation failed; client disconnected") from None
            finally:
                # A partial revocation or a live MCP stream has an uncertain
                # outcome. Close all client connections before returning.
                try:
                    await self._disconnect_locked()
                except Exception:
                    raise CLIConnectionError("MCP OAuth revocation cleanup failed; outcome is uncertain") from None

    async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None:
        """Opt-in tool-call suspension; it does not disconnect or hide the MCP server."""
        self._check_elicitation_reentry()
        if not isinstance(server_name, str) or not server_name.strip() or "\0" in server_name:
            raise ValueError("server_name must be a nonempty string")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        async with self._query_lock, self._disconnect_lock, self._sdk_interrupt_lock:
            if not self._connected or not self.session_id:
                raise CLIConnectionError("Call connect() first")
            if self.options.sdk_mcp_toggle_mode != self._sdk_mcp_toggle_mode:
                raise ValueError("Reconnect after changing sdk_mcp_toggle_mode")
            if enabled and server_name in self._plugin_inline_mcp_configs:
                self._validate_unsupported_limits()
            if self._sdk_mcp_toggle_mode not in ("sdk-tools", "sdk-and-stdio", "sdk-all"):
                raise ProtocolError("Server toggling requires sdk_mcp_toggle_mode='sdk-tools', 'sdk-and-stdio', or 'sdk-all'")
            if (self._turn is not None or self._stream_input is not None or
                    self._queued_after_initial is not None or self._sdk_tools_suspended):
                raise CLIConnectionError("Consume the current response before switching SDK tools")
            bridges = self._named_mcp_bridges.get(server_name)
            external = self._named_external_mcp_bridges.get(server_name, [])
            if ((not bridges and not external) or
                    (self._sdk_mcp_toggle_mode not in ("sdk-and-stdio", "sdk-all") and bridges and
                     any(bridge.instance is not None for bridge in bridges))):
                raise ProtocolError("Only SDK helper-tool servers and opted-in SDK instances, stdio servers, or HTTP/SSE servers can be toggled")
            if enabled and any(isinstance(bridge, ExternalStdioMcpBridge) and bridge._cleanup_failed
                               for bridge in external):
                raise CLIConnectionError("External stdio MCP cleanup failed; reconnect the client")
            try:
                for bridge in bridges or []:
                    bridge.set_tools_enabled(enabled)
                for bridge in external:
                    bridge.set_tools_enabled(enabled)
                cancellable = [bridge for bridge in bridges or [] if bridge.instance is None]
                if not enabled and cancellable:
                    await asyncio.wait_for(asyncio.gather(*(bridge.cancel_tools() for bridge in cancellable)), 3)
            except BaseException as exc:
                if self._reader_error is None:
                    error = CLIConnectionError("SDK tool toggle failed; connection cannot continue")
                    error.__cause__ = exc
                    self._reader_error = error
                await self._abort_for_guard(force=True)
                raise

    async def request_mcp_tool_cancellation(self, server_name: str) -> None:
        """Request cancellation of observed outstanding tool calls for an SDK-managed MCP server."""
        self._check_elicitation_reentry()
        if not isinstance(server_name, str) or not server_name.strip() or "\0" in server_name:
            raise ValueError("server_name must be a nonempty string")
        async with self._disconnect_lock, self._sdk_interrupt_lock:
            if not self._connected or not self.session_id or self._reader_error is not None:
                raise CLIConnectionError("Call connect() first and use a healthy connection")
            if self.options.sdk_mcp_toggle_mode != self._sdk_mcp_toggle_mode:
                raise ValueError("Reconnect after changing sdk_mcp_toggle_mode")
            bridges = [*self._named_mcp_bridges.get(server_name, []),
                       *self._named_external_mcp_bridges.get(server_name, [])]
            if not bridges:
                raise ProtocolError("Cancellation requires an SDK-managed MCP server")
            operation = asyncio.gather(*(bridge.request_tool_cancellation() for bridge in bridges),
                                       return_exceptions=True)
            try:
                done, _ = await asyncio.wait((operation,), timeout=3)
                if not done:
                    raise asyncio.TimeoutError("MCP cancellation request timed out")
                failures = [result for result in operation.result() if isinstance(result, BaseException)]
                if failures:
                    raise CLIConnectionError("MCP cancellation request could not be delivered") from failures[0]
            except BaseException as exc:
                if not operation.done():
                    operation.cancel()
                    def consume(done: asyncio.Future) -> None:
                        if not done.cancelled():
                            done.exception()
                    operation.add_done_callback(consume)
                if self._reader_error is None:
                    error = CLIConnectionError("MCP cancellation outcome is uncertain; connection cannot continue")
                    error.__cause__ = exc
                    self._reader_error = error
                await self._abort_for_guard(force=True)
                raise

    async def request_kiro_mcp_enabled(self, enabled: bool) -> None:
        """Request a process-wide Kiro MCP switch; the notification has no acknowledgement."""
        self._check_elicitation_reentry()
        if self.options.agent_engine != "v3":
            raise ProtocolError("The Kiro MCP switch requires V3")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        async with self._query_lock:
            if not self.session_id or not self._connected:
                raise CLIConnectionError("Call connect() first")
            if self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
                raise CLIConnectionError("Consume the current response before switching MCP")
            await self._send({"jsonrpc": "2.0", "method": "_kiro/mcp/toggle",
                              "params": {"enabled": enabled}})

    async def get_mcp_resource(self, server_name: str, uri: str) -> dict[str, Any]:
        if self.options.agent_engine != "v3":
            raise ProtocolError("MCP resource access requires Kiro V3")
        return await self._request("_kiro/mcp/getResource", {"sessionId": self.session_id,
                                   "serverName": server_name, "uri": uri})

    async def get_mcp_prompt(self, server_name: str, prompt_name: str,
                             arguments: Optional[dict[str, str]] = None) -> dict[str, Any]:
        if self.options.agent_engine != "v3":
            raise ProtocolError("MCP prompt access requires Kiro V3")
        return await self._request("_kiro/mcp/getPrompt", {"sessionId": self.session_id,
                                   "serverName": server_name, "promptName": prompt_name,
                                   "arguments": arguments or {}})

    def _require_session_capability(self, name: str) -> None:
        capabilities = (self.server_info or {}).get("agentCapabilities", {}).get("sessionCapabilities", {})
        if name not in capabilities or capabilities[name] is None:
            raise ProtocolError(f"Kiro does not advertise session/{name}")

    async def list_sessions(self, cwd: Optional[str] = None, cursor: Optional[str] = None) -> dict[str, Any]:
        """List sessions via ACP. Requires a server advertising session/list."""
        self._require_session_capability("list")
        params = {}
        if cwd is not None:
            params["cwd"] = self._remote_path(cwd) if self._transport is not None else str(Path(cwd).resolve())
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("session/list", params)

    async def fork_session(self, session_id: Optional[str] = None, *,
                           message_id: Optional[str] = None, title: Optional[str] = None,
                           resume_drops_turn: Optional[str] = None) -> dict[str, Any]:
        """Fork a session; returns the new session descriptor from Kiro."""
        self._check_elicitation_reentry()
        if resume_drops_turn is not None:
            if self._query_lock.locked():
                raise CLIConnectionError("A query or session operation is already active")
            async with self._query_lock, self._disconnect_lock:
                return await self._fork_session_locked(session_id, message_id=message_id, title=title,
                                                       resume_drops_turn=resume_drops_turn)
        return await self._fork_session_locked(session_id, message_id=message_id, title=title)

    async def _fork_session_locked(self, session_id: Optional[str] = None, *,
                                   message_id: Optional[str] = None, title: Optional[str] = None,
                                   resume_drops_turn: Optional[str] = None) -> dict[str, Any]:
        fork_guard = None
        guarded_session, guarded_generation = self.session_id, self._connection_generation
        if resume_drops_turn is not None:
            self._validate_unsupported_limits()
            if (self.options.runtime_compat_mode != "v3" or self._native_hook_runtime is None or
                    not self._native_hook_runtime.confirmed or not self.session_id or
                    (session_id is not None and session_id != self.session_id)):
                raise ProtocolError("Guarded forking requires this client's verified V3 session")
            if self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
                raise CLIConnectionError("Consume the current response before guarded forking")
            if any(not isinstance(value, str) or not value or len(value) > 128 or
                   any(ord(c) < 32 for c in value) for value in (message_id, resume_drops_turn)):
                raise ValueError("Guarded forking requires message_id and resume_drops_turn IDs")
            fork_guard = self._native_failure_guard()
            await fork_guard()
        self._require_session_capability("fork")
        params = {
            "sessionId": session_id or self.session_id,
            "cwd": self._working_directory(),
            "mcpServers": self._mcp_servers,
        }
        if self.options.add_dirs:
            params["additionalDirectories"] = self._additional_directories()
        if message_id is not None:
            capability = (self.server_info or {}).get("agentCapabilities", {}).get("sessionCapabilities", {}).get("fork", {})
            if not _nullable_mapping(_nullable_mapping(capability.get("_meta")).get("kiro")).get("messageId"):
                raise ProtocolError("Kiro does not advertise forking at a message")
            if not message_id:
                raise ValueError("message_id must not be empty")
            params["_meta"] = {"kiro": {"messageId": message_id}}
        if title is not None:
            params.setdefault("_meta", {}).setdefault("kiro", {})["title"] = title
        if resume_drops_turn is not None:
            params.setdefault("_meta", {}).setdefault("kiro", {})["sdkResumeDropsTurn"] = resume_drops_turn
            await fork_guard()
        result = await self._request("session/fork", params)
        if fork_guard is not None:
            await fork_guard()
            if not isinstance(result, dict):
                raise ProtocolError("Kiro returned an invalid guarded fork response")
            confirmation = _nullable_mapping(_nullable_mapping(result.get("_meta")).get("kiro"))
            fork_id = result.get("sessionId")
            if (self.session_id != guarded_session or self._connection_generation != guarded_generation or
                    confirmation.get("sdkResumeDropsTurn") != resume_drops_turn or
                    type(fork_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,256}", fork_id) is None or
                    fork_id == guarded_session):
                raise ProtocolError("Kiro did not confirm the guarded fork")
        return result

    @staticmethod
    def _remote_path(path: Any) -> str:
        if not isinstance(path, (str, Path)) or not str(path) or "\0" in str(path) or not (
            PurePosixPath(str(path)).is_absolute() or PureWindowsPath(str(path)).is_absolute()
        ):
            raise ValueError("Custom transport paths must be absolute paths on the remote host")
        return str(path)

    def _working_directory(self) -> str:
        if self._transport is not None and self.options.cwd is not None:
            return self._remote_path(self.options.cwd)
        return str(Path(self.options.cwd or os.getcwd()).resolve())

    def _additional_directories(self) -> list[str]:
        if self._transport is not None:
            return [self._remote_path(path) for path in self.options.add_dirs]
        cwd = Path(self.options.cwd or os.getcwd()).resolve()
        return [str((cwd / path).resolve()) if not Path(path).is_absolute() else str(Path(path).resolve())
                for path in self.options.add_dirs]

    async def delete_session(self, session_id: str) -> None:
        """Delete a stored session. This is irreversible."""
        self._require_session_capability("delete")
        if session_id == self.session_id:
            raise ValueError("Disconnect from the active session before deleting it")
        await self._request("session/delete", {"sessionId": session_id})

    async def rename_session(self, title: str, session_id: Optional[str] = None) -> dict[str, Any]:
        """Rename a Kiro V3 session."""
        if self.options.agent_engine != "v3":
            raise ProtocolError("Session renaming requires Kiro V3")
        if not title.strip():
            raise ValueError("title must not be empty")
        result = await self._request("_kiro/session/rename", {"sessionId": session_id or self.session_id, "title": title})
        if (session_id in (None, self.session_id) and isinstance(result, dict) and result.get("success") is True):
            await self._mirror("session_info", {"title": title})
        return result

    async def compact(self) -> dict[str, Any]:
        """Ask Kiro V3 to compact the current conversation."""
        self._check_elicitation_reentry()
        async with self._query_lock, self._disconnect_lock:
            return await self._compact_locked()

    async def get_guarded_fork_options(self, user_message_id: str) -> dict[str, Any]:
        """Find an idle session's safe truncation point; the fork revalidates it."""
        if self._query_lock.locked():
            raise CLIConnectionError("A query or session operation is already active")
        async with self._query_lock:
            if self.options.runtime_compat_mode != "v3":
                raise ProtocolError("Guarded fork lookup requires the verified V3 runtime")
            if not self._connected:
                raise CLIConnectionError("Call connect() first and use a healthy connection")
            self._validate_unsupported_limits()
            if (self._native_hook_runtime is None or not self._native_hook_runtime.confirmed or
                    not self.session_id or not self._connected):
                raise ProtocolError("Guarded fork lookup requires the verified V3 runtime")
            if self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
                raise CLIConnectionError("Consume the current response before looking up a fork point")
            if (not isinstance(user_message_id, str) or not user_message_id or len(user_message_id) > 128 or
                    any(ord(c) < 32 for c in user_message_id)):
                raise ValueError("user_message_id must be a Kiro user message ID")
            generation, session_id = self._connection_generation, self.session_id
            guard = self._native_failure_guard()
            await guard()
            result = await self._request("_kiro/sdk/forkPoint",
                                         {"sessionId": session_id, "userMessageId": user_message_id})
            await guard()
            if (generation != self._connection_generation or session_id != self.session_id or
                    not isinstance(result, dict) or type(result.get("protocol")) is not int or
                    result["protocol"] != 1 or result.get("sessionId") != session_id or
                    result.get("userMessageId") != user_message_id or
                    not isinstance(result.get("messageId"), str) or not result["messageId"] or
                    len(result["messageId"]) > 128 or any(ord(c) < 32 for c in result["messageId"])):
                raise ProtocolError("Kiro did not confirm the guarded fork point")
            return {"resume": session_id, "fork_session": True, "resume_session_at": result["messageId"],
                    "resume_drops_turn": user_message_id}

    async def _compact_locked(self, *, allow_changed_snapshot: bool = False,
                              on_send_start: Optional[Callable[[], None]] = None) -> dict[str, Any]:
        self._compaction_request_sent = False
        send_guard, guarded_session = self._compaction_send_guard, self.session_id
        if self.options.agent_engine != "v3":
            raise ProtocolError("Context compaction requires Kiro V3")
        if self._turn is not None or self._stream_input is not None or self._queued_after_initial is not None:
            raise CLIConnectionError("Consume the current response before compaction")
        methods = _kiro_capabilities(self.server_info).get("extensionMethods", [])
        if "_kiro/session/compact" not in methods:
            raise ProtocolError("Kiro does not advertise context compaction")
        self._validate_unsupported_limits()
        self._validate_hook_failure_snapshot()
        if send_guard is not None:
            if (self._compaction_send_guard is not send_guard or await send_guard() is not True or
                    self._compaction_send_guard is not send_guard or self.session_id != guarded_session or
                    not self._connected or self._disconnecting or self._reader_error is not None):
                raise PermissionError("Compaction authorization changed")
            self._validate_hook_failure_snapshot()
        if on_send_start is not None:
            on_send_start()
        self._compaction_request_sent = True
        result = await self._request("_kiro/session/compact", {"sessionId": self.session_id})
        return result

    def _require_kiro_extension(self, method: str) -> None:
        methods = _kiro_capabilities(self.server_info).get("extensionMethods", [])
        if method not in methods:
            raise ProtocolError(f"Kiro does not advertise {method}")

    @staticmethod
    def _memory_string(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError(f"{name} must be a nonempty string without NUL")
        return value

    @classmethod
    def _memory_id(cls, value: Any) -> str:
        value = cls._memory_string(value, "memory_id")
        if value != value.strip():
            raise ValueError("memory_id must not have surrounding whitespace")
        return value

    async def list_kiro_memories(self, *, cursor: Optional[str] = None, limit: Optional[int] = None,
                                 scopes: Optional[list[str]] = None) -> dict[str, Any]:
        """List one page of persistent Kiro memories."""
        method = "_kiro/memory/list"
        self._require_kiro_extension(method)
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = self._memory_string(cursor, "cursor")
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("limit must be a positive integer")
            params["limit"] = limit
        if scopes is not None:
            if not isinstance(scopes, list) or not scopes:
                raise ValueError("scopes must be a nonempty list")
            params["scopes"] = [self._memory_string(scope, "scope") for scope in scopes]
        return await self._request(method, params)

    async def get_kiro_memory(self, memory_id: str) -> dict[str, Any]:
        """Get a persistent Kiro memory by its Kiro ID."""
        method = "_kiro/memory/get"
        self._require_kiro_extension(method)
        return await self._request(method, {"id": self._memory_id(memory_id)})

    async def create_kiro_memory(self, *, memory_type: str, scopes: list[str], title: str,
                                 summary: str, content: str,
                                 unresolved_conflict: Optional[bool] = None) -> dict[str, Any]:
        """Create a persistent Kiro memory; its scope may extend beyond this session."""
        method = "_kiro/memory/create"
        self._require_kiro_extension(method)
        if not isinstance(scopes, list) or not scopes:
            raise ValueError("scopes must be a nonempty list")
        params: dict[str, Any] = {
            "memoryType": self._memory_string(memory_type, "memory_type"),
            "scopes": [self._memory_string(scope, "scope") for scope in scopes],
            "title": self._memory_string(title, "title"),
            "summary": self._memory_string(summary, "summary"),
            "content": self._memory_string(content, "content"),
        }
        if unresolved_conflict is not None:
            if not isinstance(unresolved_conflict, bool):
                raise ValueError("unresolved_conflict must be a boolean")
            params["unresolvedConflict"] = unresolved_conflict
        return await self._request(method, params)

    async def update_kiro_memory(self, memory_id: str, *, memory_type: Optional[str] = None,
                                 title: Optional[str] = None, summary: Optional[str] = None,
                                 content: Optional[str] = None,
                                 unresolved_conflict: Optional[bool] = None) -> dict[str, Any]:
        """Update a persistent Kiro memory by ID."""
        method = "_kiro/memory/update"
        self._require_kiro_extension(method)
        params: dict[str, Any] = {"id": self._memory_id(memory_id)}
        for key, value in (("memoryType", memory_type), ("newTitle", title),
                           ("summary", summary), ("content", content)):
            if value is not None:
                params[key] = self._memory_string(value, key)
        if unresolved_conflict is not None:
            if not isinstance(unresolved_conflict, bool):
                raise ValueError("unresolved_conflict must be a boolean")
            params["unresolvedConflict"] = unresolved_conflict
        if len(params) == 1:
            raise ValueError("At least one memory update is required")
        return await self._request(method, params)

    async def delete_kiro_memory(self, memory_id: str) -> dict[str, Any]:
        """Delete a persistent Kiro memory by ID."""
        method = "_kiro/memory/delete"
        self._require_kiro_extension(method)
        return await self._request(method, {"id": self._memory_id(memory_id)})

    async def get_kiro_memory_sync_status(self) -> dict[str, Any]:
        """Get Kiro memory synchronization status."""
        method = "_kiro/memory/getSyncStatus"
        self._require_kiro_extension(method)
        return await self._request(method, {})

    async def list_kiro_workflows(self, *, workspace_paths: Optional[list[str]] = None) -> dict[str, Any]:
        """List Kiro workflow runs for this session or specified remote host paths."""
        self._require_kiro_extension("_kiro/workflow/list")
        if workspace_paths is None:
            if not self.session_id:
                raise CLIConnectionError("Call connect() first")
            params = {"sessionId": self.session_id}
        else:
            def absolute(path: Any) -> bool:
                return (isinstance(path, str) and bool(path) and "\0" not in path and
                        (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()))
            if (not isinstance(workspace_paths, list) or not workspace_paths or
                    any(not absolute(path) for path in workspace_paths)):
                raise ValueError("workspace_paths must contain absolute paths")
            params = {"workspacePaths": workspace_paths}
        return await self._request("_kiro/workflow/list", params)

    async def create_kiro_workflow(self, *, workflow: Optional[dict[str, Any]] = None,
                                   workflow_path: Optional[str] = None, inputs: Optional[dict[str, Any]] = None,
                                   model: Optional[str] = None, effort: Optional[str] = None) -> dict[str, Any]:
        """Create a Kiro workflow run without starting it."""
        self._require_kiro_extension("_kiro/workflow/new")
        if not self.session_id:
            raise CLIConnectionError("Call connect() first")
        if (workflow is None) == (workflow_path is None):
            raise ValueError("Specify exactly one of workflow or workflow_path")
        if workflow is not None and not isinstance(workflow, dict):
            raise ValueError("workflow must be a dictionary")
        if workflow_path is not None and (not isinstance(workflow_path, str) or not workflow_path.strip()
                                          or "\0" in workflow_path):
            raise ValueError("workflow_path must be a nonempty Kiro workflow path or URI")
        if inputs is not None and not isinstance(inputs, dict):
            raise ValueError("inputs must be a dictionary")
        params: dict[str, Any] = {"parentSessionId": self.session_id, "inputs": inputs or {}}
        params["workflow" if workflow is not None else "workflowPath"] = workflow if workflow is not None else workflow_path
        for name, value, key in (("model", model, "modelId"), ("effort", effort, "effortLevel")):
            if value is not None:
                if not isinstance(value, str) or not value.strip() or "\0" in value:
                    raise ValueError(f"{name} must be a nonempty string")
                params[key] = value
        try:
            json.dumps(params, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow and inputs must be JSON-serializable") from exc
        result = await self._request("_kiro/workflow/new", params)
        workflow_id = result.get("workflowId") if isinstance(result, dict) else None
        if isinstance(workflow_id, str) and workflow_id.strip() and "\0" not in workflow_id:
            self._owned_workflows.add(workflow_id)
        return result

    async def _kiro_workflow_action(self, action: str, workflow_id: str) -> dict[str, Any]:
        method = "_kiro/workflow/" + action
        self._require_kiro_extension(method)
        if (not isinstance(workflow_id, str) or not workflow_id.strip() or "\0" in workflow_id):
            raise ValueError("workflow_id must be a nonempty Kiro workflow ID")
        return await self._request(method, {"workflowId": workflow_id})

    async def inspect_kiro_workflow(self, workflow_id: str) -> dict[str, Any]:
        return await self._kiro_workflow_action("inspect", workflow_id)

    async def run_kiro_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Start a Kiro workflow; its steps may run asynchronously."""
        return await self._kiro_workflow_action("invoke", workflow_id)

    async def pause_kiro_workflow(self, workflow_id: str) -> dict[str, Any]:
        return await self._kiro_workflow_action("pause", workflow_id)

    async def resume_kiro_workflow(self, workflow_id: str) -> dict[str, Any]:
        return await self._kiro_workflow_action("resume", workflow_id)

    async def cancel_kiro_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Request cancellation of a Kiro workflow."""
        return await self._kiro_workflow_action("cancel", workflow_id)

    def attach_background_tasks(self, manager: Optional[BackgroundTaskManager]) -> None:
        """Attach an explicitly managed task manager, or detach it with None.

        Attaching does not transfer ownership of the manager or close it on
        disconnect. The caller remains responsible for its lifetime.
        """
        from .background_tasks import BackgroundTaskManager
        if manager is not None and not isinstance(manager, BackgroundTaskManager):
            raise TypeError("manager must be a BackgroundTaskManager or None")
        self._background_task_manager = manager

    async def stop_task(self, task_id: str) -> None:
        """Cancel an attached SDK task or a Kiro workflow created by this client."""
        self._check_elicitation_reentry()
        if not isinstance(task_id, str) or not task_id.strip() or "\0" in task_id:
            raise ValueError("task_id must be a nonempty task ID")
        manager = self._background_task_manager
        owned_background = manager is not None and manager.owns(task_id)
        if owned_background and task_id in self._owned_workflows:
            raise ProtocolError("Task ID is ambiguous between the attached manager and a Kiro workflow")
        if owned_background:
            status = await manager.stop(task_id)
            if status.state == "failed":
                raise ProtocolError("SDK background task failed; inspect its status")
            return
        if task_id not in self._owned_workflows:
            raise ProtocolError("stop_task supports only attached SDK tasks and Kiro workflows created by this client")
        result = await self.cancel_kiro_workflow(task_id)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ProtocolError("Kiro did not confirm the workflow cancellation request")

    async def delete_kiro_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Delete a Kiro workflow run; Kiro may cancel it first."""
        result = await self._kiro_workflow_action("delete", workflow_id)
        if isinstance(result, dict) and result.get("ok") is True:
            self._owned_workflows.discard(workflow_id)
        return result

    async def get_history_page(self, *, before_message_id: Optional[str] = None,
                               limit: Optional[int] = None) -> dict[str, Any]:
        """Read a page of V3 history without relying on Kiro's storage format."""
        self._require_kiro_extension("_kiro/session/history")
        params: dict[str, Any] = {"sessionId": self.session_id}
        if before_message_id is not None:
            params["beforeMessageId"] = before_message_id
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("limit must be a positive integer")
            params["limit"] = limit
        return await self._request("_kiro/session/history", params)

    async def export_session(self, session_id: Optional[str] = None) -> dict[str, Any]:
        """Ask Kiro V3 to export a session to a server-local archive."""
        self._require_kiro_extension("_kiro/session/export")
        if self._turn is not None and (session_id is None or session_id == self.session_id):
            raise CLIConnectionError("Consume the current response before exporting")
        return await self._request("_kiro/session/export", {"sessionId": session_id or self.session_id})

    def _check_checkpoint(self, message_id: str) -> None:
        if self.options.agent_engine != "v3":
            raise ProtocolError("Checkpoint restoration requires Kiro V3")
        if not self.session_id:
            raise CLIConnectionError("Call connect() first")
        if self._turn is not None:
            raise CLIConnectionError("Consume the current response before restoring a checkpoint")
        capabilities = _kiro_capabilities(self.server_info)
        if capabilities.get("checkpoints") is not True:
            raise ProtocolError("Kiro does not advertise checkpoints")
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("message_id must not be empty")

    async def restore_checkpoint(self, message_id: str) -> dict[str, Any]:
        """Restore Kiro files and rewind conversation to a user message."""
        self._check_elicitation_reentry()
        async with self._query_lock:
            return await self._restore_checkpoint_locked(message_id)

    async def _restore_checkpoint_locked(self, message_id: str) -> dict[str, Any]:
        self._check_checkpoint(message_id)
        self._recovery_in_doubt = True
        result = await self._request("_kiro/checkpoint/revertMultiple",
                                     {"sessionId": self.session_id, "messageId": message_id})
        self._recovery_in_doubt = not isinstance(result, dict) or result.get("success") is not True
        return result

    async def rewind_files(self, user_message_id: str, *, restore_conversation: bool = False) -> dict[str, Any]:
        """Restore Kiro files only if the caller accepts rewinding conversation too."""
        self._check_elicitation_reentry()
        if not restore_conversation:
            raise ProtocolError("Kiro also rewinds conversation; pass restore_conversation=True")
        return await self.restore_checkpoint(user_message_id)

    @property
    def last_rewind_fork_id(self) -> Optional[str]:
        """The most recent fork created by rewind_files_preserving_conversation."""
        return self._last_rewind_fork_id

    @property
    def restored_session_id(self) -> Optional[str]:
        """The session imported by resume_from_store, including after a failed connection."""
        return self._restored_session_id

    async def rewind_files_preserving_conversation(self, user_message_id: str) -> dict[str, Any]:
        """Fork the latest conversation, then restore files in the original session."""
        self._check_elicitation_reentry()
        async with self._query_lock:
            self._last_rewind_fork_id = None
            self._check_checkpoint(user_message_id)
            self._require_session_capability("fork")
            original_id = self.session_id
            fork = await self.fork_session()
            fork_id = fork.get("sessionId") if isinstance(fork, dict) else None
            if not isinstance(fork_id, str) or not fork_id or fork_id == original_id:
                raise ProtocolError("Kiro did not return a distinct conversation fork ID")
            self._last_rewind_fork_id = fork_id
            self._recovery_in_doubt = True
            try:
                result = await self._restore_checkpoint_locked(user_message_id)
            except Exception as exc:
                raise CheckpointRecoveryError(fork_id) from exc
            if not isinstance(result, dict) or result.get("success") is not True:
                raise CheckpointRecoveryError(fork_id, result)
            self._recovery_in_doubt = False
            return {"original_session_id": original_id, "fork_session_id": fork_id, "checkpoint_result": result}

    async def extension_request(self, method: str, params: dict[str, Any]) -> Any:
        """Call a Kiro-specific ACP extension explicitly."""
        if not method.startswith(("_kiro/", "_kiro.dev/")):
            raise ValueError("Only Kiro extension methods are allowed")
        return await self._request(method, params)

    async def disconnect(self) -> None:
        self._check_elicitation_reentry()
        if (_FILE_CHANGED_CONTEXT.get() is self or self in _CLIENT_AUTH_CONTEXT.get() or
                asyncio.current_task() in self._client_auth_tasks):
            raise CLIConnectionError("FileChanged and authentication callbacks cannot control the client")
        self._client_auth_shutdown_revision += 1
        if self._connected:
            self._disconnecting = True
        current = asyncio.current_task()
        self._disconnect_tasks.add(current)
        try:
            self._disconnect_lock.cancel_others(self._disconnect_tasks)
            async with self._disconnect_lock:
                await self._disconnect_locked()
        finally:
            self._disconnect_tasks.discard(current)
            if not self._connected:
                self._disconnecting = False
            if not self._disconnect_tasks:
                self._disconnect_lock.priority_tasks = None

    @staticmethod
    async def _finish_stream_cleanup(stream: Any, pending: Any) -> None:
        if pending is not None:
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                if not pending.done():
                    raise
            except Exception:
                pass
        if stream is not None:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    async def _disconnect_locked(self) -> None:
        self._extension_hook_revision += 1
        workspace_error = None
        try:
            pass
        except Exception as exc:
            workspace_error = exc
        hook_cleanup_error = workspace_error
        try:
            await self._hook_failure_runner.close()
        except Exception as exc:
            hook_cleanup_error = exc
        async_hook_error = None
        try:
            pass
        except Exception as exc:
            async_hook_error = exc
        model_hook_error = None
        try:
            pass
        except Exception as exc:
            model_hook_error = exc
        self._client_auth_control_active = False
        process = self._process
        if not self._connected:
            if hook_cleanup_error is not None:
                raise hook_cleanup_error
            if async_hook_error is not None:
                raise async_hook_error
            if model_hook_error is not None:
                raise model_hook_error
            if self._client_auth_pending or any(not task.done() for task in self._client_auth_tasks):
                raise CLIConnectionError("Kiro authentication cleanup is still pending")
            return
        self._disconnecting = True
        self._signal_message_activity()
        stream, self._stream_input = self._stream_input, None
        input_task = self._input_next_task
        if self._input_cancel_event is not None:
            self._input_cancel_event.set()
        if input_task is not None and not input_task.done():
            input_task.cancel()
        input_error = None
        if stream is not None or input_task is not None:
            cleanup = asyncio.create_task(self._finish_stream_cleanup(stream, input_task))
            self._stream_cleanup_task = cleanup
            cleanup.add_done_callback(self._handler_finished)
            done, _ = await asyncio.wait((cleanup,), timeout=_STREAM_INPUT_CLEANUP_TIMEOUT)
            if not done:
                input_error = CLIConnectionError("Streaming input cleanup is still pending")
            else:
                try:
                    cleanup.result()
                except (Exception, asyncio.CancelledError):
                    input_error = CLIConnectionError("Streaming input cleanup failed")
        for record in self._inbound_requests.values():
            record["cancelled"] = True
        handler_tasks = list(self._handler_tasks)
        for task in handler_tasks:
            task.cancel()
        if handler_tasks:
            await asyncio.wait(handler_tasks, timeout=3)
        # Application callbacks that ignore cancellation keep running; report and track them so that a
        # reconnect cannot start while a callback from the previous connection can still act.
        self._lingering_handler_tasks = {task for task in (*self._lingering_handler_tasks, *handler_tasks)
                                         if not task.done()}
        self._inbound_requests.clear()
        if self._turn and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn
        close_error = hook_cleanup_error or async_hook_error or model_hook_error or input_error
        if self._transport is not None and not self._transport_closed:
            try:
                await self._transport.close()
            except Exception as exc:
                if close_error is None:
                    close_error = exc
        elif process is not None:
            await _terminate_owned_process(process, signal_exited_group=True)
        for task in (self._reader, self._stderr_task):
            if task:
                try:
                    await asyncio.wait_for(task, 3)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
        if self._mirror_writer is not None:
            try:
                await asyncio.wait_for(self._mirror_writer.close(), self.options.session_store_flush_timeout)
            except asyncio.TimeoutError:
                if close_error is None:
                    close_error = CLIConnectionError("Timed out closing the session mirror")
            except Exception as exc:
                if close_error is None:
                    close_error = exc
            self._mirror_writer = None
        for bridge in self._mcp_bridges:
            await bridge.close()
        self._mcp_bridges.clear()
        self._named_mcp_bridges.clear()
        for bridge in self._external_mcp_bridges:
            try:
                await bridge.close()
            except Exception as exc:
                if close_error is None:
                    close_error = exc
        self._external_mcp_bridges.clear()
        self._named_external_mcp_bridges.clear()
        self._mcp_oauth_states.clear()
        self._agent_mcp_bridges.clear()
        self._agent_inline_mcp_bridges.clear()
        self._plugin_inline_mcp_configs.clear()
        self._configured_mcp_servers.clear()
        self._resolved_system_prompt = None
        self._effective_effort = None
        self._agent_model_snapshot = None
        self._startup_fallback = None
        self._shell_edit_verified = False
        self._sdk_tool_interrupt_mode = "kiro"
        self._sdk_mcp_toggle_mode = "reject"
        self._code_intelligence_option = False
        self._code_intelligence_enabled = False
        self._sdk_tools_suspended = False
        self._pending_session_plan = None
        self._prompt_send_guard = None
        self._bounded_recovery_loader = None
        self._compaction_send_guard = None
        self._interaction_guard = None
        self._native_hooks = {event: list(matchers) for event, matchers in self._base_native_hooks.items()}
        self._policy_presets.clear()
        self._subagent_types_by_session.clear()
        self._mcp_servers = []
        self._owned_workflows.clear()
        self._remove_temporary_agent()
        if self._native_hook_bridge:
            await self._native_hook_bridge.close()
            self._native_hook_bridge = None
        if self._native_hook_runtime is not None:
            self._native_hook_runtime.close()
            self._native_hook_runtime = None
        self._process = None
        self._connected = False
        self._disconnecting = False
        self._remove_agents()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CLIConnectionError("Kiro transport disconnected"))
        self._store_key = None
        self.session_id = None
        self._available_commands_by_session.clear()
        self._turn = None
        self._turn_prompt_warnings = ()
        self._turn_mirror_prompt_id = None
        self._native_stop_warning_count = 0
        self._native_stop_warning_bytes = 0
        self._turn_model = ""
        self._initial_prompt_active = False
        self._initial_prompt_failed = False
        self._queued_after_initial = None
        self._turn_started_at = None
        self._turn_finished_at = None
        if close_error is None and (self._client_auth_pending or any(not task.done() for task in self._client_auth_tasks)):
            close_error = CLIConnectionError("Kiro authentication cleanup is still pending")
        if close_error is None and any(not task.done() for task in self._lingering_handler_tasks):
            close_error = CLIConnectionError("Request callback cleanup is incomplete; disconnect again after it finishes")
        if close_error is not None:
            raise close_error

    async def __aenter__(self) -> KiroSDKClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.disconnect()
