"""Optional session event mirrors; Kiro remains the session authority."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import time
import json
import math
import threading
from pathlib import Path
from typing import Any, Optional, Protocol


_MAX_RETAINED_ARCHIVE_ENTRY_BYTES = 16 * 1024 * 1024


class SessionStore(Protocol):
    async def append(self, key: dict[str, str], entries: list[dict[str, Any]]) -> None: ...
    async def load(self, key: dict[str, str]) -> Any: ...


def merge_session_info(info: Optional[dict[str, Any]], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Return Kiro session metadata updated by the ``session_info`` entries in a batch.

    Each entry carries fields Kiro reported (or a renamed ``title``); later values win field by field.
    Stores that keep an index call this in ``append()`` for main-session keys only.
    """
    merged = dict(info or {})
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == "session_info" and isinstance(entry.get("data"), dict):
            merged.update(copy.deepcopy(entry["data"]))
    return merged


def project_key_for_directory(directory: str | Path | None = None) -> str:
    """Stable opaque key for a workspace path."""
    return hashlib.sha256(os.path.realpath(directory if directory is not None else os.getcwd()).encode('utf-8')).hexdigest()


class _MemoryLock:
    """No awaits occur inside memory-store critical sections."""
    def __init__(self) -> None:
        self.lock = threading.RLock()

    async def __aenter__(self) -> None:
        self.lock.acquire()

    async def __aexit__(self, *args: Any) -> None:
        self.lock.release()


def _bounded_entry_size(entry: Any, max_bytes: int, max_nodes: int) -> tuple[int, int]:
    """Validate an entry before copying or serializing untrusted mirror data."""
    pending, seen, nodes, text_bytes = [(entry, 0)], set(), 0, 0
    node_limit = min(20000, max_nodes)
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > node_limit or depth > 32:
            raise ValueError("Session mirror entry exceeds structural limits")
        if type(value) is str:
            if len(value) > max_bytes:
                raise ValueError("Session mirror entry exceeds size limit")
            text_bytes += len(value.encode("utf-8"))
            if text_bytes > max_bytes:
                raise ValueError("Session mirror entry exceeds size limit")
        elif type(value) is int:
            if value.bit_length() > 256:
                raise ValueError("Session mirror entry contains an oversized number")
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("Session mirror entry contains a non-finite number")
        elif value is None or type(value) is bool:
            pass
        elif type(value) in (dict, list):
            if id(value) in seen or len(value) > 1024:
                raise ValueError("Session mirror entry contains repeated or oversized containers")
            seen.add(id(value))
            if type(value) is dict:
                if any(type(key) is not str for key in value):
                    raise ValueError("Session mirror entry contains an invalid key")
                pending.extend((key, depth + 1) for key in value)
                pending.extend((child, depth + 1) for child in value.values())
            else:
                pending.extend((child, depth + 1) for child in value)
        else:
            raise ValueError("Session mirror entry contains a non-JSON value")
    if type(entry) is not dict:
        raise ValueError("Invalid session mirror entry")
    size = len(json.dumps(entry, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if size > max_bytes:
        raise ValueError("Session mirror entry exceeds size limit")
    return size, nodes


def _bounded_load_limits(max_entries: int, max_bytes: int) -> None:
    if (type(max_entries) is not int or not 1 <= max_entries <= 50000 or
            type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024):
        raise ValueError("Invalid bounded session mirror limits")


class InMemorySessionStore:
    """An isolated in-memory event mirror for tests and short-lived processes."""
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self._mtimes: dict[tuple[str, str, str], int] = {}
        self._info: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._lock = _MemoryLock()

    def get_entries(self, key: dict[str, str]) -> list[dict[str, Any]]:
        with self._lock.lock:
            return copy.deepcopy(self._entries.get(self._key(key), []))

    @property
    def size(self) -> int:
        with self._lock.lock:
            return sum(not key[2] for key in self._entries)

    def clear(self) -> None:
        with self._lock.lock:
            self._entries.clear()
            self._mtimes.clear()
            self._info.clear()

    @staticmethod
    def _key(key: dict[str, str]) -> tuple[str, str, str]:
        return key['project_key'], key['session_id'], key.get('subpath', '')

    def _append_locked(self, k: tuple[str, str, str], entries: list[dict[str, Any]]) -> None:
        values = copy.deepcopy(entries)
        mtime = max(int(time.time() * 1000), self._mtimes.get(k, 0) + 1)
        if not k[2]:
            self._info[k] = merge_session_info(self._info.get(k), values)
        self._entries.setdefault(k, []).extend(values)
        self._mtimes[k] = mtime

    async def append(self, key: dict[str, str], entries: list[dict[str, Any]]) -> None:
        async with self._lock:
            self._append_locked(self._key(key), entries)

    async def append_if_absent(self, key: dict[str, str], entries: list[dict[str, Any]]) -> bool:
        async with self._lock:
            k = self._key(key)
            if k in self._entries:
                return False
            self._append_locked(k, entries)
            return True

    async def append_batches_if_absent(self, key: dict[str, str],
                                       batches: list[list[dict[str, Any]]]) -> bool:
        async with self._lock:
            k = self._key(key)
            if k in self._entries:
                return False
            self._append_locked(k, [entry for batch in batches for entry in batch])
            return True

    async def load(self, key: dict[str, str]) -> Any:
        async with self._lock:
            value = self._entries.get(self._key(key))
            return copy.deepcopy(value) if value is not None else None

    async def load_bounded(self, key: dict[str, str], *, max_entries: int, max_bytes: int,
                           skip_archives: bool = False, retain_latest_archive: bool = False) -> Any:
        """Return a bounded snapshot, optionally retaining just the latest archive."""
        _bounded_load_limits(max_entries, max_bytes)
        if type(skip_archives) is not bool or type(retain_latest_archive) is not bool:
            raise ValueError("Archive read settings must be booleans")
        if retain_latest_archive and not skip_archives:
            raise ValueError("retain_latest_archive requires skip_archives")
        async with self._lock:
            entries = self._entries.get(self._key(key))
            if entries is None:
                return None
            if len(entries) > max_entries:
                raise ValueError("Session mirror exceeds entry limit")
            used = nodes = 0
            selected = []
            latest_archive = None
            for entry in entries:
                kind = entry.get("type") if type(entry) is dict else None
                if retain_latest_archive and type(kind) is str and kind == "session_archive":
                    latest_archive = (len(selected), entry)
                value = ({"type": kind} if skip_archives and type(kind) is str and
                         kind in ("session_archive", "archive_message") else entry)
                size, count = _bounded_entry_size(value, max_bytes - used, 200000 - nodes)
                used += size
                nodes += count
                selected.append(value)
            if latest_archive is not None:
                index, archive = latest_archive
                _bounded_entry_size(archive, _MAX_RETAINED_ARCHIVE_ENTRY_BYTES, 20000)
                selected[index] = archive
            return copy.deepcopy(selected)

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        async with self._lock:
            return [{'session_id': sid, 'mtime': self._mtimes[(project, sid, sub)]}
                    for project, sid, sub in self._entries if project == project_key and not sub]

    async def list_session_info(self, project_key: str) -> list[dict[str, Any]]:
        async with self._lock:
            return [{'session_id': sid, 'mtime': self._mtimes[k], 'info': copy.deepcopy(info)}
                    for k, info in self._info.items() for project, sid, _ in (k,) if project == project_key]

    async def list_subkeys(self, key: dict[str, str]) -> list[str]:
        async with self._lock:
            return [sub for project, sid, sub in self._entries
                    if project == key['project_key'] and sid == key['session_id'] and sub]

    async def delete(self, key: dict[str, str]) -> None:
        async with self._lock:
            target = self._key(key)
            keys = [k for k in self._entries if k == target or (not target[2] and k[:2] == target[:2])]
            for k in keys:
                self._entries.pop(k, None)
                self._mtimes.pop(k, None)
                self._info.pop(k, None)


class MirrorWriter:
    """Serialize bounded mirror writes without blocking the ACP reader on each entry."""
    def __init__(self, store: SessionStore, key: dict[str, str], mode: str, on_error: Any,
                 *, continue_on_error: bool = False) -> None:
        self._store = store
        self._key = key
        self._mode = mode
        self._on_error = on_error
        self._continue_on_error = continue_on_error
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._failed = asyncio.Event()
        self._error: Optional[Exception] = None
        self._pending_bytes = 0
        self._closed = False
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(self._task_finished)

    @staticmethod
    def _task_finished(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    def _check_error(self) -> None:
        if self._error is not None:
            raise self._error

    async def _put(self, item: Any, accepted: Optional[list[bool]] = None) -> None:
        self._check_error()
        async def enqueue() -> None:
            await self._queue.put(item)
            if accepted is not None:
                accepted[0] = True
        put_task = asyncio.create_task(enqueue())
        failed_task = asyncio.create_task(self._failed.wait())
        try:
            done, _ = await asyncio.wait((put_task, failed_task), return_when=asyncio.FIRST_COMPLETED)
            if failed_task in done:
                self._check_error()
            await put_task
            self._check_error()
        finally:
            for task in (put_task, failed_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put_task, failed_task, return_exceptions=True)

    async def append(self, entry: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("Session mirror is closed")
        entry = copy.deepcopy(entry)
        size = len(json.dumps(entry, ensure_ascii=False).encode())
        if self._pending_bytes and self._pending_bytes + size > 1024 * 1024:
            await self.flush()
        self._pending_bytes += size
        accepted = [False]
        try:
            await self._put(("entry", entry, size), accepted)
        except BaseException:
            if not accepted[0]:
                self._pending_bytes -= size
            raise

    async def _barrier(self, kind: str) -> None:
        self._check_error()
        future = asyncio.get_running_loop().create_future()
        await self._put((kind, future, 0))
        failed_task = asyncio.create_task(self._failed.wait())
        try:
            done, _ = await asyncio.wait((future, failed_task), return_when=asyncio.FIRST_COMPLETED)
            if failed_task in done:
                self._check_error()
            await future
        finally:
            failed_task.cancel()
            await asyncio.gather(failed_task, return_exceptions=True)

    async def flush(self) -> None:
        await self._barrier("flush")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._barrier("close")
            await self._task
        finally:
            if not self._task.done():
                self._task.cancel()
                await asyncio.wait((self._task,), timeout=1)

    async def _run(self) -> None:
        entries: list[dict[str, Any]] = []
        size = 0

        async def commit() -> None:
            nonlocal entries, size
            if entries:
                try:
                    await self._store.append(self._key, entries)
                except Exception as exc:
                    if not self._continue_on_error:
                        raise
                    await self._on_error(exc)
                self._pending_bytes -= size
                entries, size = [], 0

        try:
            while True:
                kind, payload, entry_size = await self._queue.get()
                if kind == "entry":
                    entries.append(payload)
                    size += entry_size
                    if self._mode == "eager" or len(entries) >= 500 or size >= 1024 * 1024:
                        await commit()
                else:
                    await commit()
                    if not payload.done():
                        payload.set_result(None)
                    if kind == "close":
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc
            self._failed.set()
            await self._on_error(exc)
