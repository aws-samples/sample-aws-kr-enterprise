"""Local Kiro CLI session archives and optional session-store recovery."""
from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import io
import json
import math
import os
import re
import stat
import struct
import tempfile
import zipfile
import zlib
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Optional, Union

from .errors import CLIConnectionError, ProcessError, SessionArchiveError
from .sessions import _run_cli, _store_key

# Kiro session.json fields copied into the mirror as session metadata.
_ARCHIVE_SESSION_FIELDS = ("title", "createdAt", "lastModifiedAt", "agentMode", "modelId", "status")

DEFAULT_ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
ARCHIVE_FORMAT = "kiro-cli-chat-archive-v1"


def _archive_payload(session_id: str, data: bytes) -> dict[str, Any]:
    return {"format": ARCHIVE_FORMAT, "session_id": session_id, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "base64": base64.b64encode(data).decode("ascii")}


def _valid_session_id(session_id: Any) -> bool:
    return (isinstance(session_id, str) and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", session_id) is not None)


def _limits(max_bytes: int, max_uncompressed_bytes: int) -> None:
    for name, value in (("max_bytes", max_bytes), ("max_uncompressed_bytes", max_uncompressed_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")


def _validate_timeout(timeout: Optional[float]) -> None:
    if timeout is None:
        return
    try:
        valid = (not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and
                 math.isfinite(timeout) and timeout > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("timeout must be positive")


def _check_archive(data: bytes, max_bytes: int, max_uncompressed_bytes: int) -> None:
    _limits(max_bytes, max_uncompressed_bytes)
    if not isinstance(data, bytes):
        raise TypeError("archive must be bytes")
    if not data or len(data) > max_bytes:
        raise SessionArchiveError("Session archive is empty or exceeds the size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            items = archive.infolist()
            if not items or len(items) > 10000:
                raise SessionArchiveError("Invalid session archive entry count")
            names = set()
            expanded = 0
            for item in items:
                name = item.filename
                parts = PurePosixPath(name).parts
                normalized = "/".join(parts) + ("/" if name.endswith("/") else "")
                file_type = stat.S_IFMT(item.external_attr >> 16)
                if (not name or name.startswith("/") or "\\" in name or "\0" in name or
                        any(part in (".", "..") or ":" in part for part in parts) or
                        name != normalized or name in names or
                        file_type not in (0, stat.S_IFREG, stat.S_IFDIR) or
                        (file_type == stat.S_IFDIR and not item.is_dir()) or
                        (file_type == stat.S_IFREG and item.is_dir()) or item.flag_bits & 1):
                    raise SessionArchiveError("Unsafe session archive entry")
                names.add(name)
                expanded += item.file_size
                if expanded > max_uncompressed_bytes:
                    raise SessionArchiveError("Session archive exceeds the expanded size limit")
                if not item.is_dir():
                    with archive.open(item) as member:
                        while member.read(65536):
                            pass
            if "session.json" not in names:
                raise SessionArchiveError("Kiro session archive is missing session.json")
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, RuntimeError, NotImplementedError,
            struct.error, zlib.error) as exc:
        raise SessionArchiveError("Invalid Kiro session archive") from exc


async def _helper(arguments: list[str], cwd: str, cli_path: Union[str, Path],
                  env: Optional[dict[str, str]], timeout: Optional[float]) -> dict[str, Any]:
    _validate_timeout(timeout)
    try:
        raw = await asyncio.wait_for(_run_cli(["_", *arguments], cwd, cli_path, env), timeout)
    except asyncio.TimeoutError as exc:
        raise SessionArchiveError("Timed out waiting for the Kiro archive helper") from exc
    except ProcessError as exc:
        raise SessionArchiveError("Kiro archive helper failed") from exc
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionArchiveError("Kiro archive helper returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        raise SessionArchiveError("Kiro archive helper returned an invalid response")
    if result.get("kind") == "error":
        raise SessionArchiveError(f"Kiro archive helper failed: {result['data'].get('message', 'unknown error')}")
    return result


async def export_session_archive(session_id: str, *, cwd: Union[str, Path, None] = None,
                                 cli_path: Union[str, Path] = "kiro-cli",
                                 env: Optional[dict[str, str]] = None,
                                 max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                 max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                 timeout: Optional[float] = 30.0) -> bytes:
    """Export a local session with the Kiro CLI's chat archive helper."""
    _limits(max_bytes, max_uncompressed_bytes)
    if not _valid_session_id(session_id):
        raise ValueError("Invalid session ID")
    workspace = str(Path(cwd or os.getcwd()).resolve())
    with tempfile.TemporaryDirectory(prefix="kiro-sdk-archive-") as temp:
        archive_path = Path(temp) / "session.zip"
        result = await _helper(["export-session", "--id", session_id, "--cwd", workspace,
                                "--out", str(archive_path)], workspace, cli_path, env, timeout)
        if result.get("kind") != "exportSession" or not archive_path.is_file():
            raise SessionArchiveError("Kiro did not produce the expected session archive")
        size = archive_path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise SessionArchiveError("Session archive is empty or exceeds the size limit")
        data = await asyncio.to_thread(archive_path.read_bytes)
        await asyncio.to_thread(_check_archive, data, max_bytes, max_uncompressed_bytes)
        return data


async def _import_session_archive(archive: bytes, *, cwd: Union[str, Path, None] = None,
                                  cli_path: Union[str, Path] = "kiro-cli",
                                  env: Optional[dict[str, str]] = None,
                                  max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                  max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                  timeout: Optional[float] = 30.0,
                                  before_helper: Optional[Callable[[], Awaitable[bool]]] = None) -> str:
    await asyncio.to_thread(_check_archive, archive, max_bytes, max_uncompressed_bytes)
    if before_helper is not None and await before_helper() is not True:
        raise PermissionError("Session recovery authorization changed")
    workspace = str(Path(cwd or os.getcwd()).resolve())
    with tempfile.TemporaryDirectory(prefix="kiro-sdk-archive-") as temp:
        archive_path = Path(temp) / "session.zip"
        await asyncio.to_thread(archive_path.write_bytes, archive)
        if before_helper is not None and await before_helper() is not True:
            raise PermissionError("Session recovery authorization changed")
        result = await _helper(["import-session", "--archive", str(archive_path),
                                "--cwd", workspace], workspace, cli_path, env, timeout)
    if result.get("kind") != "importSession":
        raise SessionArchiveError("Kiro did not confirm session archive import")
    path = result["data"].get("path")
    if not isinstance(path, str) or not path or "\0" in path:
        raise SessionArchiveError("Kiro did not return an imported session path")
    session_id = Path(path).name
    if not _valid_session_id(session_id):
        raise SessionArchiveError("Kiro did not return an imported session ID")
    return session_id


async def import_session_archive(archive: bytes, *, cwd: Union[str, Path, None] = None,
                                 cli_path: Union[str, Path] = "kiro-cli",
                                 env: Optional[dict[str, str]] = None,
                                 max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                 max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                 timeout: Optional[float] = 30.0) -> str:
    """Import an archive as a new local Kiro session and return its ID."""
    return await _import_session_archive(
        archive, cwd=cwd, cli_path=cli_path, env=env, max_bytes=max_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes, timeout=timeout)


def _archive_mirror_entries(archive_data: bytes, session_id: str,
                            max_messages: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            if "session.json" in archive.namelist():
                session = json.loads(archive.read("session.json"))
                if isinstance(session, dict):
                    info = {key: session[key] for key in _ARCHIVE_SESSION_FIELDS
                            if isinstance(session.get(key), str) and session[key]}
                    entries.append({"type": "session_info", "data": {**info, "sessionId": session_id}})
            if "messages.jsonl" in archive.namelist():
                with archive.open("messages.jsonl") as messages:
                    for count, raw_line in enumerate(messages, 1):
                        if count > max_messages:
                            raise SessionArchiveError("Session archive exceeds the message count limit")
                        if not raw_line.strip():
                            continue
                        item = json.loads(raw_line)
                        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                            raise SessionArchiveError("Unexpected Kiro archive message")
                        entries.append({"type": "archive_message", "data": item})
                        payload = item["payload"]
                        content = payload.get("content")
                        message_id = item.get("id") if isinstance(item.get("id"), str) else None
                        if payload.get("type") == "user" and isinstance(content, str):
                            entries.append({"type": "prompt", "uuid": message_id,
                                            "data": [{"type": "text", "text": content}]})
                        elif (payload.get("type") == "assistant" and isinstance(content, str) and
                              payload.get("operationType") in (None, "Say")):
                            entries.append({"type": "update", "data": {
                                "sessionUpdate": "agent_message_chunk", "messageId": message_id,
                                "content": {"type": "text", "text": content}}})
    except (zipfile.BadZipFile, UnicodeDecodeError, ValueError) as exc:
        raise SessionArchiveError("Invalid Kiro archive messages") from exc
    entries.append({"type": "session_archive", "data": _archive_payload(session_id, archive_data)})
    return entries


def _without_subexecutions(data: bytes, max_bytes: int, max_uncompressed_bytes: int) -> bytes:
    _check_archive(data, max_bytes, max_uncompressed_bytes)
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
            target.comment = source.comment
            for item in source.infolist():
                if PurePosixPath(item.filename).parts[0] == "sub-executions":
                    continue
                if item.is_dir():
                    target.writestr(copy.copy(item), b"")
                else:
                    with source.open(item) as reader, target.open(copy.copy(item), "w") as writer:
                        shutil.copyfileobj(reader, writer, length=65536)
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, RuntimeError, NotImplementedError,
            struct.error, zlib.error) as exc:
        raise SessionArchiveError("Unable to remove subagent transcripts from the Kiro archive") from exc
    result = output.getvalue()
    _check_archive(result, max_bytes, max_uncompressed_bytes)
    return result


async def _write_archive_to_store(session_id: str, store: Any, key: dict[str, str], archive: bytes, *,
                                  batch_size: int, max_messages: int, title: Optional[str] = None) -> None:
    entries = await asyncio.to_thread(_archive_mirror_entries, archive, session_id, max_messages)
    if title is not None:
        entries.insert(-1, {"type": "session_info", "data": {"title": title}})
    batches = [entries[i:i + batch_size] for i in range(0, len(entries), batch_size)]
    create_batches = getattr(store, "append_batches_if_absent", None)
    if callable(create_batches):
        try:
            created = await create_batches(key, batches)
        except NotImplementedError:
            pass
        else:
            if not isinstance(created, bool):
                raise SessionArchiveError("Session store returned an invalid append_batches_if_absent result")
            if not created:
                raise FileExistsError("Session mirror already exists")
            return
    create = getattr(store, "append_if_absent", None)
    if callable(create):
        try:
            created = await create(key, batches[0])
        except NotImplementedError:
            pass
        else:
            if not isinstance(created, bool):
                raise SessionArchiveError("Session store returned an invalid append_if_absent result")
            if not created:
                raise FileExistsError("Session mirror already exists")
            for batch in batches[1:]:
                await store.append(key, batch)
            return
    if await store.load(key) is not None:
        raise FileExistsError("Session mirror already exists")
    for batch in batches:
        await store.append(key, batch)


async def import_session_to_store(session_id: str, store: Any, *,
                                  directory: Union[str, Path, None] = None,
                                  include_subagents: bool = True,
                                  batch_size: int = 500,
                                  cli_path: Union[str, Path] = "kiro-cli",
                                  env: Optional[dict[str, str]] = None,
                                  max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                  max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                  max_messages: int = 10000,
                                  timeout: Optional[float] = 30.0) -> None:
    """Import an existing local V3 session into an empty mirror key.

    The archive retains Kiro's data; text messages are also mapped for mirror
    inspection. Stores with append_batches_if_absent can atomically create all
    batches. Coordinate other writers when an adapter lacks that capability.
    """
    if not _valid_session_id(session_id):
        raise ValueError("Invalid session ID")
    if not isinstance(include_subagents, bool):
        raise ValueError("include_subagents must be a boolean")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages <= 0:
        raise ValueError("max_messages must be a positive integer")
    _limits(max_bytes, max_uncompressed_bytes)
    _validate_timeout(timeout)
    key = _store_key(session_id, directory)
    if await store.load(key) is not None:
        raise FileExistsError("Session mirror already exists")
    if not include_subagents:
        from .store_fork import _require_tested_cli
        workspace = str(Path(directory or os.getcwd()).resolve())
        if not Path(workspace).is_dir():
            raise CLIConnectionError(f"Working directory does not exist: {workspace}")
        try:
            await _require_tested_cli(cli_path, workspace, env, timeout, operation="Excluding subagent transcripts")
        except asyncio.TimeoutError as exc:
            raise SessionArchiveError("Timed out checking the Kiro CLI version") from exc
    archive = await export_session_archive(session_id, cwd=directory, cli_path=cli_path, env=env,
                                           max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
                                           timeout=timeout)
    if not include_subagents:
        archive = await asyncio.to_thread(_without_subexecutions, archive, max_bytes, max_uncompressed_bytes)
    await _write_archive_to_store(session_id, store, key, archive, batch_size=batch_size, max_messages=max_messages)


def _stored_archive(entries: list[dict[str, Any]], session_id: str, *,
                    allow_stale: bool, max_bytes: int) -> Optional[bytes]:
    index = next((i for i in range(len(entries) - 1, -1, -1)
                  if isinstance(entries[i], dict) and entries[i].get("type") == "session_archive"), None)
    if index is None:
        return None
    data = entries[index].get("data") if index is not None else None
    if not isinstance(data, dict) or data.get("format") != ARCHIVE_FORMAT:
        raise SessionArchiveError("Session mirror has no supported Kiro archive")
    if data.get("session_id") != session_id:
        raise SessionArchiveError("Stored session archive belongs to a different session")
    if not allow_stale and any(isinstance(entry, dict) and entry.get("type") in ("prompt", "update", "result")
                               for entry in entries[index + 1:]):
        raise SessionArchiveError("Session mirror contains activity after its latest archive; pass allow_stale=True to restore it")
    encoded = data.get("base64")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((max_bytes + 2) // 3):
        raise SessionArchiveError("Stored session archive exceeds the size limit")
    try:
        archive = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SessionArchiveError("Stored session archive has invalid encoding") from exc
    if data.get("size") != len(archive) or data.get("sha256") != hashlib.sha256(archive).hexdigest():
        raise SessionArchiveError("Stored session archive integrity check failed")
    return archive


async def _load_archive_mirror(store: Any, key: dict[str, str], bounded: bool, *,
                               loader: Optional[Callable[[Any, dict[str, str]], Awaitable[Any]]] = None) -> Any:
    """Load a consistent mirror with at most one archive payload when requested."""
    if type(bounded) is not bool:
        raise ValueError("bounded_mirror_read must be a boolean")
    if not bounded:
        return await store.load(key)
    from .session_store import InMemorySessionStore
    from .sqlite_store import SQLiteSessionStore
    if type(store) not in (InMemorySessionStore, SQLiteSessionStore):
        raise ValueError("bounded_mirror_read requires a built-in session store")
    try:
        if loader is not None:
            return await loader(store, key)
        return await store.load_bounded(key, max_entries=20000, max_bytes=8 * 1024 * 1024,
                                        skip_archives=True, retain_latest_archive=True)
    except ValueError as exc:
        raise SessionArchiveError("Bounded session mirror is invalid or exceeds limits") from exc


async def _restore_session_from_store(session_store: Any, session_id: str,
                                      directory: Union[str, Path, None] = None, *,
                                      target_directory: Union[str, Path, None] = None,
                                      cli_path: Union[str, Path] = "kiro-cli",
                                      env: Optional[dict[str, str]] = None,
                                      allow_stale: bool = False,
                                      max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                      max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                      timeout: Optional[float] = 30.0,
                                      bounded_mirror_read: bool = False,
                                      before_import: Optional[Callable[[], Awaitable[bool]]] = None,
                                      mirror_loader: Optional[Callable[[Any, dict[str, str]], Awaitable[Any]]] = None) -> str:
    _limits(max_bytes, max_uncompressed_bytes)
    if not isinstance(allow_stale, bool):
        raise ValueError("allow_stale must be a boolean")
    if not _valid_session_id(session_id):
        raise ValueError("Invalid session ID")
    entries = await _load_archive_mirror(session_store, _store_key(session_id, directory), bounded_mirror_read,
                                         loader=mirror_loader)
    if not isinstance(entries, list):
        raise SessionArchiveError("Session mirror was not found")
    archive = await asyncio.to_thread(_stored_archive, entries, session_id,
                                      allow_stale=allow_stale, max_bytes=max_bytes)
    if archive is None:
        raise SessionArchiveError("Session mirror has no supported Kiro archive")
    if before_import is not None and await before_import() is not True:
        raise PermissionError("Session recovery authorization changed")
    destination = target_directory if target_directory is not None else directory
    if before_import is not None:
        return await _import_session_archive(
            archive, cwd=destination, cli_path=cli_path, env=env, max_bytes=max_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes, timeout=timeout, before_helper=before_import)
    return await import_session_archive(archive, cwd=destination, cli_path=cli_path, env=env,
                                        max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
                                        timeout=timeout)


async def restore_session_from_store(session_store: Any, session_id: str,
                                     directory: Union[str, Path, None] = None, *,
                                     target_directory: Union[str, Path, None] = None,
                                     cli_path: Union[str, Path] = "kiro-cli",
                                     env: Optional[dict[str, str]] = None,
                                     allow_stale: bool = False,
                                     max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
                                     max_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_UNCOMPRESSED_BYTES,
                                     timeout: Optional[float] = 30.0,
                                     bounded_mirror_read: bool = False) -> str:
    """Import the latest stored Kiro archive as a new local session."""
    return await _restore_session_from_store(
        session_store, session_id, directory, target_directory=target_directory, cli_path=cli_path, env=env,
        allow_stale=allow_stale, max_bytes=max_bytes, max_uncompressed_bytes=max_uncompressed_bytes,
        timeout=timeout, bounded_mirror_read=bounded_mirror_read)
