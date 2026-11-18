"""Inspect raw main-session records in Kiro's local V3 archive."""
from __future__ import annotations

import asyncio
import copy
import io
import zipfile
from pathlib import Path
from typing import Any, Optional, Union

from .archive import (DEFAULT_ARCHIVE_MAX_BYTES, DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                      _check_archive, _load_archive_mirror, _stored_archive, export_session_archive)
from .errors import SessionArchiveError
from .sessions import _directory, _page, _store_key
from .subagents import _read_records, _validate
from .history import history_message
from .types import HistoryMessage, ToolCall


def _records(data: bytes, max_bytes: int, max_uncompressed_bytes: int,
             max_messages: int) -> list[dict[str, Any]]:
    _check_archive(data, max_bytes, max_uncompressed_bytes)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return _read_records(archive, "messages.jsonl", max_messages)


def _valid_tool_label(value: Any, max_length: int) -> bool:
    if (type(value) is not str or not value.strip() or len(value) > max_length or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _tool_identity(payload: dict[str, Any]) -> Optional[str]:
    tool_id = payload.get("toolCallId")
    if not _valid_tool_label(tool_id, 512):
        return None
    return tool_id


def _unambiguous_tool_origin(payload: dict[str, Any]) -> bool:
    if "agentSubtaskId" in payload:
        return False
    if "_meta" in payload:
        metadata = payload["_meta"]
        if not isinstance(metadata, dict) or "agentSubtaskId" in metadata:
            return False
        kiro = metadata.get("kiro")
        if ("kiro" in metadata and not isinstance(kiro, dict)) or (
                isinstance(kiro, dict) and "agentSubtaskId" in kiro):
            return False
    return True


def _matching_tool_context(call: dict[str, Any], result: dict[str, Any]) -> bool:
    for key in ("executionId", "turnId"):
        if key not in call and key not in result:
            continue
        left, right = call.get(key), result.get(key)
        if not _valid_tool_label(left, 512) or left != right:
            return False
    return True


def _tool_records(records: list[dict[str, Any]], session_id: str) -> dict[int, ToolCall]:
    """Build a ToolCall for each unique reported call, with its uniquely paired result when there is one."""
    calls: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    results: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, record in enumerate(records):
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") not in ("tool_call", "tool_result"):
            continue
        tool_id = _tool_identity(payload)
        if tool_id is None:
            continue
        target = calls if payload["type"] == "tool_call" else results
        matches = target.setdefault(tool_id, [])
        if len(matches) < 2:
            matches.append((index, payload))
    converted: dict[int, ToolCall] = {}
    for tool_id, matched_calls in calls.items():
        if len(matched_calls) != 1:
            continue
        index, call = matched_calls[0]
        name, args = call.get("toolName"), call.get("args")
        if (not _unambiguous_tool_origin(call) or not _valid_tool_label(name, 512) or
                not isinstance(args, dict)):
            continue
        title = call.get("title")
        message = ToolCall(tool_id, title if _valid_tool_label(title, 1024) else name, raw_input=copy.deepcopy(args),
                           session_id=session_id, kiro_metadata={"toolName": name})
        converted[index] = message
        matched_results = results.get(tool_id, [])
        if len(matched_results) != 1:
            continue
        result_index, result = matched_results[0]
        success, content = result.get("success"), result.get("content")
        if (result_index <= index or not _unambiguous_tool_origin(result) or
                not _matching_tool_context(call, result) or type(success) is not bool):
            continue
        message.status = "completed" if success else "failed"
        message.raw_output = copy.deepcopy(content)
    return converted


def _preview(records: list[dict[str, Any]], session_id: str,
             include_tool_messages: bool = False) -> list[HistoryMessage]:
    messages: list[HistoryMessage] = []
    tools = _tool_records(records, session_id) if include_tool_messages else {}
    for index, record in enumerate(records):
        if index in tools:
            messages.append(tools[index])
            continue
        payload = record["payload"]
        role = payload.get("type")
        if role not in ("user", "assistant") or not isinstance(payload.get("content"), str):
            continue
        if role == "assistant" and payload.get("operationType") not in (None, "Say"):
            continue
        message_id = record.get("id") if isinstance(record.get("id"), str) else None
        messages.append(history_message(role, message_id, session_id, payload["content"]))
    return messages


async def _local_records(session_id: str, directory: Union[str, Path, None],
                         cwd: Union[str, Path, None], cli_path: Union[str, Path],
                         env: Optional[dict[str, str]], max_bytes: int,
                         max_uncompressed_bytes: int, max_messages: int,
                         timeout: Optional[float]) -> list[dict[str, Any]]:
    _validate(session_id, None, max_bytes, max_uncompressed_bytes, max_messages)
    data = await export_session_archive(session_id, cwd=_directory(directory, cwd), cli_path=cli_path, env=env,
                                        max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
                                        timeout=timeout)
    return await asyncio.to_thread(_records, data, max_bytes, max_uncompressed_bytes, max_messages)


async def _store_records_snapshot(store: Any, session_id: str, directory: Union[str, Path, None],
                                  allow_stale: bool, max_bytes: int, max_uncompressed_bytes: int,
                                  max_messages: int, bounded_mirror_read: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    _validate(session_id, None, max_bytes, max_uncompressed_bytes, max_messages)
    if not isinstance(allow_stale, bool):
        raise ValueError("allow_stale must be a boolean")
    entries = await _load_archive_mirror(store, _store_key(session_id, directory), bounded_mirror_read)
    if entries is None:
        return [], [], 0
    if not isinstance(entries, list):
        raise SessionArchiveError("Invalid session mirror")
    data = await asyncio.to_thread(_stored_archive, entries, session_id, allow_stale=allow_stale, max_bytes=max_bytes)
    if data is None:
        raise SessionArchiveError("Session archive records require a mirrored Kiro session archive")
    records = await asyncio.to_thread(_records, data, max_bytes, max_uncompressed_bytes, max_messages)
    archive_index = next(i for i in range(len(entries) - 1, -1, -1)
                         if isinstance(entries[i], dict) and entries[i].get("type") == "session_archive")
    return records, entries, archive_index


async def _store_records(store: Any, session_id: str, directory: Union[str, Path, None],
                         allow_stale: bool, max_bytes: int, max_uncompressed_bytes: int,
                         max_messages: int, bounded_mirror_read: bool) -> list[dict[str, Any]]:
    records, _, _ = await _store_records_snapshot(store, session_id, directory, allow_stale,
                                                   max_bytes, max_uncompressed_bytes, max_messages, bounded_mirror_read)
    return records


async def get_session_archive_history(session_id: str, directory: Union[str, Path, None] = None,
                                      limit: Optional[int] = None, offset: int = 0, *,
                                      cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = "kiro-cli",
                                      env: Optional[dict[str, str]] = None,
                                      max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                      max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                      max_messages: int = 10000, timeout: Optional[float] = 30.0) -> list[dict[str, Any]]:
    """Return raw Kiro V3 archive records, including non-text and historical events."""
    _page(offset, limit)
    records = await _local_records(session_id, directory, cwd, cli_path, env, max_bytes,
                                   max_uncompressed_bytes, max_messages, timeout)
    return records[offset:None if limit is None else offset + limit]


async def get_session_archive_messages(session_id: str, directory: Union[str, Path, None] = None,
                                       limit: Optional[int] = None, offset: int = 0, *,
                                       cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = "kiro-cli",
                                       env: Optional[dict[str, str]] = None,
                                       max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                       max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                       max_messages: int = 10000, timeout: Optional[float] = 30.0,
                                       include_tool_messages: bool = False) -> list[HistoryMessage]:
    """Return Kiro record previews, optionally including reported tool calls and results."""
    _page(offset, limit)
    if not isinstance(include_tool_messages, bool):
        raise ValueError("include_tool_messages must be a boolean")
    records = await _local_records(session_id, directory, cwd, cli_path, env, max_bytes,
                                   max_uncompressed_bytes, max_messages, timeout)
    return _preview(records, session_id, include_tool_messages)[offset:None if limit is None else offset + limit]


async def get_session_archive_history_from_store(store: Any, session_id: str,
                                                 directory: Union[str, Path, None] = None,
                                                 limit: Optional[int] = None, offset: int = 0, *,
                                                 allow_stale: bool = False, max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                                 max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                                 max_messages: int = 10000,
                                                 bounded_mirror_read: bool = False) -> list[dict[str, Any]]:
    """Return raw records from the latest archived session mirror."""
    _page(offset, limit)
    records = await _store_records(store, session_id, directory, allow_stale, max_bytes,
                                   max_uncompressed_bytes, max_messages, bounded_mirror_read)
    return records[offset:None if limit is None else offset + limit]


async def get_session_archive_messages_from_store(store: Any, session_id: str,
                                                  directory: Union[str, Path, None] = None,
                                                  limit: Optional[int] = None, offset: int = 0, *,
                                                  allow_stale: bool = False, max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                                  max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                                  max_messages: int = 10000,
                                                  include_tool_messages: bool = False,
                                                  bounded_mirror_read: bool = False) -> list[HistoryMessage]:
    """Return stored Kiro record previews, optionally including reported tool calls and results."""
    _page(offset, limit)
    if not isinstance(include_tool_messages, bool):
        raise ValueError("include_tool_messages must be a boolean")
    records, _, _ = await _store_records_snapshot(store, session_id, directory, allow_stale,
                                                                     max_bytes, max_uncompressed_bytes, max_messages,
                                                                     bounded_mirror_read)
    return _preview(records, session_id, include_tool_messages)[offset:None if limit is None else offset + limit]
