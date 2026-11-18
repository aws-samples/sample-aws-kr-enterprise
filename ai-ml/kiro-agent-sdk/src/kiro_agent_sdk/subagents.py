"""Inspect Kiro V3 subagent records without treating all sub-executions as agents."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Optional, Union

from .archive import (DEFAULT_ARCHIVE_MAX_BYTES, DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                      _check_archive, _limits, _load_archive_mirror, _stored_archive, _valid_session_id,
                      export_session_archive)
from .errors import SessionArchiveError
from .sessions import _directory, _page, _store_key
from .history import history_message
from .types import HistoryMessage


def _read_records(archive: zipfile.ZipFile, member: str, max_messages: int) -> list[dict[str, Any]]:
    if member not in archive.namelist():
        return []
    records = []
    try:
        with archive.open(member) as stream:
            for count, line in enumerate(stream, 1):
                if count > max_messages:
                    raise SessionArchiveError("Kiro archive exceeds the message count limit")
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                    raise SessionArchiveError("Unexpected Kiro archive message")
                records.append(item)
    except (zipfile.BadZipFile, UnicodeDecodeError, ValueError) as exc:
        raise SessionArchiveError("Invalid Kiro archive messages") from exc
    return records


def _scan_archive(data: bytes, target: Optional[str], max_bytes: int,
                  max_uncompressed_bytes: int, max_messages: int
                  ) -> tuple[dict[str, str], set[str], list[dict[str, Any]]]:
    _check_archive(data, max_bytes, max_uncompressed_bytes)
    by_tool: dict[str, str] = {}
    unkeyed: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for entry in _read_records(archive, "messages.jsonl", max_messages):
            payload = entry["payload"]
            kind = payload.get("type")
            agent_id = payload.get("subSessionId")
            if kind not in ("sub_agent_start", "sub_agent_complete") or not _valid_session_id(agent_id):
                continue
            suffix = "-sub-agent-start" if kind == "sub_agent_start" else "-sub-agent-complete"
            record_id = entry.get("id")
            if isinstance(record_id, str) and record_id.endswith(suffix) and len(record_id) > len(suffix):
                by_tool[record_id[:-len(suffix)]] = agent_id
            else:
                unkeyed.add(agent_id)
        records = []
        if target is not None and (target in by_tool.values() or target in unkeyed):
            records = _read_records(archive, f"sub-executions/{target}.jsonl", max_messages)
    return by_tool, unkeyed, records


def _validate(session_id: str, agent_id: Optional[str], max_bytes: int,
              max_uncompressed_bytes: int, max_messages: int) -> None:
    if not _valid_session_id(session_id):
        raise ValueError("Invalid session ID")
    if agent_id is not None and not _valid_session_id(agent_id):
        raise ValueError("Invalid subagent ID")
    _limits(max_bytes, max_uncompressed_bytes)
    if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages <= 0:
        raise ValueError("max_messages must be a positive integer")


def _observed_agents(entries: list[dict[str, Any]], by_tool: dict[str, str]) -> None:
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "update":
            continue
        update = entry.get("data")
        if not isinstance(update, dict) or update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            continue
        tool_id = update.get("toolCallId")
        meta = update.get("_meta")
        kiro = meta.get("kiro") if isinstance(meta, dict) else None
        if not isinstance(tool_id, str) or not tool_id or not isinstance(kiro, dict):
            continue
        if kiro.get("kind") != "agent-subtask" and tool_id not in by_tool:
            continue
        agent_id = kiro.get("agentSubtaskId")
        if _valid_session_id(agent_id):
            by_tool[tool_id] = agent_id


def _messages(records: list[dict[str, Any]], session_id: str, agent_id: str,
              by_tool: dict[str, str], limit: Optional[int], offset: int) -> list[HistoryMessage]:
    parent = next((tool_id for tool_id, value in by_tool.items() if value == agent_id), None)
    messages = []
    for entry in records:
        payload = entry["payload"]
        role = payload.get("type")
        if role not in ("user", "assistant") or not isinstance(payload.get("content"), str):
            continue
        if role == "assistant" and payload.get("operationType") not in (None, "Say"):
            continue
        message_id = entry.get("id") if isinstance(entry.get("id"), str) else None
        messages.append(history_message(role, message_id, session_id, payload["content"], agent_id=agent_id,
                                        parent_tool_call_id=parent))
    return messages[offset:None if limit is None else offset + limit]


async def list_subagents(session_id: str, directory: Union[str, Path, None] = None, *,
                         cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = "kiro-cli",
                         env: Optional[dict[str, str]] = None, max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                         max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                         max_messages: int = 10000, timeout: Optional[float] = 30.0) -> list[str]:
    """List subagent IDs recorded in a local Kiro V3 session archive."""
    _validate(session_id, None, max_bytes, max_uncompressed_bytes, max_messages)
    archive = await export_session_archive(session_id, cwd=_directory(directory, cwd), cli_path=cli_path, env=env,
                                           max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
                                           timeout=timeout)
    by_tool, unkeyed, _ = await asyncio.to_thread(_scan_archive, archive, None, max_bytes,
                                                  max_uncompressed_bytes, max_messages)
    return list(dict.fromkeys([*by_tool.values(), *sorted(unkeyed)]))


async def get_subagent_messages(session_id: str, agent_id: str,
                                directory: Union[str, Path, None] = None,
                                limit: Optional[int] = None, offset: int = 0, *,
                                cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = "kiro-cli",
                                env: Optional[dict[str, str]] = None, max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                max_messages: int = 10000, timeout: Optional[float] = 30.0) -> list[HistoryMessage]:
    """Read the text preview of a recorded local Kiro V3 subagent."""
    _page(offset, limit)
    _validate(session_id, agent_id, max_bytes, max_uncompressed_bytes, max_messages)
    if not _valid_session_id(agent_id):
        raise ValueError("Invalid subagent ID")
    archive = await export_session_archive(session_id, cwd=_directory(directory, cwd), cli_path=cli_path, env=env,
                                           max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
                                           timeout=timeout)
    by_tool, _, records = await asyncio.to_thread(_scan_archive, archive, agent_id, max_bytes,
                                                   max_uncompressed_bytes, max_messages)
    return _messages(records, session_id, agent_id, by_tool, limit, offset)


async def get_subagent_history(session_id: str, agent_id: str,
                               directory: Union[str, Path, None] = None,
                               limit: Optional[int] = None, offset: int = 0, *,
                               cwd: Union[str, Path, None] = None, cli_path: Union[str, Path] = "kiro-cli",
                               env: Optional[dict[str, str]] = None, max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                               max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                               max_messages: int = 10000, timeout: Optional[float] = 30.0) -> list[dict[str, Any]]:
    """Read raw native records, including non-text events, from a local subagent."""
    _page(offset, limit)
    _validate(session_id, agent_id, max_bytes, max_uncompressed_bytes, max_messages)
    if not _valid_session_id(agent_id):
        raise ValueError("Invalid subagent ID")
    archive = await export_session_archive(session_id, cwd=_directory(directory, cwd), cli_path=cli_path, env=env,
                                           max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
                                           timeout=timeout)
    _, _, records = await asyncio.to_thread(_scan_archive, archive, agent_id, max_bytes,
                                             max_uncompressed_bytes, max_messages)
    return records[offset:None if limit is None else offset + limit]


async def list_subagents_from_store(session_store: Any, session_id: str,
                                    directory: Union[str, Path, None] = None, *,
                                    max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                    max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                    max_messages: int = 10000,
                                    bounded_mirror_read: bool = False) -> list[str]:
    """List subagent IDs observed in a mirror and its latest archive."""
    _validate(session_id, None, max_bytes, max_uncompressed_bytes, max_messages)
    entries = await _load_archive_mirror(session_store, _store_key(session_id, directory), bounded_mirror_read)
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise SessionArchiveError("Invalid session mirror")
    archive = await asyncio.to_thread(_stored_archive, entries, session_id, allow_stale=True, max_bytes=max_bytes)
    by_tool: dict[str, str] = {}
    unkeyed: set[str] = set()
    if archive is not None:
        by_tool, unkeyed, _ = await asyncio.to_thread(_scan_archive, archive, None, max_bytes,
                                                     max_uncompressed_bytes, max_messages)
        archive_index = next(i for i in range(len(entries) - 1, -1, -1)
                             if isinstance(entries[i], dict) and entries[i].get("type") == "session_archive")
        _observed_agents(entries[archive_index + 1:], by_tool)
    else:
        _observed_agents(entries, by_tool)
    return list(dict.fromkeys([*by_tool.values(), *sorted(unkeyed)]))


async def get_subagent_messages_from_store(session_store: Any, session_id: str, agent_id: str,
                                           directory: Union[str, Path, None] = None,
                                           limit: Optional[int] = None, offset: int = 0, *,
                                           allow_stale: bool = False, max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                           max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                           max_messages: int = 10000,
                                           bounded_mirror_read: bool = False) -> list[HistoryMessage]:
    """Read a subagent text preview from the latest mirrored Kiro archive."""
    _page(offset, limit)
    _validate(session_id, agent_id, max_bytes, max_uncompressed_bytes, max_messages)
    if not _valid_session_id(agent_id):
        raise ValueError("Invalid subagent ID")
    if not isinstance(allow_stale, bool):
        raise ValueError("allow_stale must be a boolean")
    entries = await _load_archive_mirror(session_store, _store_key(session_id, directory), bounded_mirror_read)
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise SessionArchiveError("Invalid session mirror")
    archive = await asyncio.to_thread(_stored_archive, entries, session_id,
                                      allow_stale=allow_stale, max_bytes=max_bytes)
    if archive is None:
        raise SessionArchiveError("Subagent messages require a mirrored Kiro session archive")
    by_tool, _, records = await asyncio.to_thread(_scan_archive, archive, agent_id, max_bytes,
                                                   max_uncompressed_bytes, max_messages)
    return _messages(records, session_id, agent_id, by_tool, limit, offset)


async def get_subagent_history_from_store(session_store: Any, session_id: str, agent_id: str,
                                          directory: Union[str, Path, None] = None,
                                          limit: Optional[int] = None, offset: int = 0, *,
                                          allow_stale: bool = False, max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                          max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                          max_messages: int = 10000,
                                          bounded_mirror_read: bool = False) -> list[dict[str, Any]]:
    """Read raw native subagent records from the latest mirrored archive."""
    _page(offset, limit)
    _validate(session_id, agent_id, max_bytes, max_uncompressed_bytes, max_messages)
    if not _valid_session_id(agent_id):
        raise ValueError("Invalid subagent ID")
    if not isinstance(allow_stale, bool):
        raise ValueError("allow_stale must be a boolean")
    entries = await _load_archive_mirror(session_store, _store_key(session_id, directory), bounded_mirror_read)
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise SessionArchiveError("Invalid session mirror")
    archive = await asyncio.to_thread(_stored_archive, entries, session_id,
                                      allow_stale=allow_stale, max_bytes=max_bytes)
    if archive is None:
        raise SessionArchiveError("Subagent history requires a mirrored Kiro session archive")
    _, _, records = await asyncio.to_thread(_scan_archive, archive, agent_id, max_bytes,
                                             max_uncompressed_bytes, max_messages)
    return records[offset:None if limit is None else offset + limit]
