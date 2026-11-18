"""Persistent local SessionStore backed by SQLite."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Union

from .sqlite_requirements import require_upsert
from .session_store import (_MAX_RETAINED_ARCHIVE_ENTRY_BYTES as _MAX_ARCHIVE_ENTRY_BYTES,
                            _bounded_entry_size, _bounded_load_limits, merge_session_info)

_CANONICAL_ARCHIVE_ENTRY = re.compile(
    r'\{"type": "session_archive", "data": \{"format": "kiro-cli-chat-archive-v1", '
    r'"session_id": "[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", "size": [0-9]{1,12}, '
    r'"sha256": "[0-9a-f]{64}", "base64": "[A-Za-z0-9+/]*={0,2}"\}\}')
_MAX_ARCHIVE_SCAN_BYTES = 64 * 1024 * 1024
_JSON_STRUCTURE = re.compile(r'["\\{}\[\],:]')


def _preflight_json_structure(raw: str, max_nodes: int) -> None:
    """Reject obviously excessive JSON structures before materializing them."""
    limit = min(20000, max_nodes) * 2 + 2
    structural = depth = 0
    inside = False
    escaped_at = -1
    for match in _JSON_STRUCTURE.finditer(raw):
        char, index = match.group(), match.start()
        if inside:
            if index == escaped_at:
                escaped_at = -1
                continue
            escaped_at = -1
            if char == "\\":
                escaped_at = index + 1
            elif char == '"':
                inside = False
            continue
        if char == '"':
            inside = True
        elif char in "{}[],:":
            structural += 1
            if structural > limit:
                raise ValueError("Session mirror entry exceeds structural limits")
            if char in "{[":
                depth += 1
                if depth > 33:
                    raise ValueError("Session mirror entry exceeds structural limits")
            elif char in "}]":
                depth -= 1
    # JSON parsing and the exact node-count validation still run afterwards.


class SQLiteSessionStore:
    """Store ordered mirror entries in a local SQLite database.

    Every operation opens its own connection. A cancelled operation may still
    finish in its worker thread, so callers must not assume cancellation rolls
    back a write. Use a dedicated database and protect its directory.
    """

    def __init__(self, path: Union[str, Path], *, timeout: float = 30.0) -> None:
        require_upsert()
        if not isinstance(path, (str, Path)) or not str(path) or str(path) == ":memory:":
            raise ValueError("path must be a persistent SQLite file")
        try:
            valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and
                             math.isfinite(timeout) and timeout > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout must be positive")
        self.path = Path(path).expanduser().resolve()
        self.timeout = float(timeout)
        self._initialized = False
        self._init_lock = threading.Lock()

    @staticmethod
    def _key(key: dict[str, str]) -> tuple[str, str, str]:
        if not isinstance(key, dict):
            raise ValueError("session key must be a dictionary")
        project, session, subpath = key.get("project_key"), key.get("session_id"), key.get("subpath", "")
        if (not isinstance(project, str) or not project or "\0" in project or
                not isinstance(session, str) or not session or "\0" in session or
                not isinstance(subpath, str) or "\0" in subpath):
            raise ValueError("Invalid session key")
        return project, session, subpath

    def _connect(self) -> sqlite3.Connection:
        with self._init_lock:
            if not self._initialized:
                try:
                    fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
                connection = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
                try:
                    connection.execute("PRAGMA foreign_keys = ON")
                    connection.execute("""
                        CREATE TABLE IF NOT EXISTS kiro_sdk_sessions (
                            project_key TEXT NOT NULL,
                            session_id TEXT NOT NULL,
                            subpath TEXT NOT NULL,
                            mtime INTEGER NOT NULL,
                            PRIMARY KEY (project_key, session_id, subpath)
                        )
                    """)
                    connection.execute("""
                        CREATE TABLE IF NOT EXISTS kiro_sdk_entries (
                            sequence INTEGER PRIMARY KEY,
                            project_key TEXT NOT NULL,
                            session_id TEXT NOT NULL,
                            subpath TEXT NOT NULL,
                            entry_json TEXT NOT NULL,
                            FOREIGN KEY (project_key, session_id, subpath)
                                REFERENCES kiro_sdk_sessions (project_key, session_id, subpath)
                                ON DELETE CASCADE
                        )
                    """)
                    connection.execute("""
                        CREATE INDEX IF NOT EXISTS kiro_sdk_entries_key
                        ON kiro_sdk_entries (project_key, session_id, subpath, sequence)
                    """)
                    connection.execute("""
                        CREATE TABLE IF NOT EXISTS kiro_sdk_session_info (
                            project_key TEXT NOT NULL,
                            session_id TEXT NOT NULL,
                            subpath TEXT NOT NULL CHECK (subpath = ''),
                            mtime INTEGER NOT NULL,
                            info_json TEXT NOT NULL,
                            PRIMARY KEY (project_key, session_id, subpath),
                            FOREIGN KEY (project_key, session_id, subpath)
                                REFERENCES kiro_sdk_sessions (project_key, session_id, subpath)
                                ON DELETE CASCADE
                        )
                    """)
                finally:
                    connection.close()
                self._initialized = True
        connection = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def append(self, key: dict[str, str], entries: list[dict[str, Any]]) -> None:
        target = self._key(key)
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("entries must be a list of dictionaries")
        values = copy.deepcopy(entries)
        await asyncio.to_thread(self._append, target, values, False)

    async def append_if_absent(self, key: dict[str, str], entries: list[dict[str, Any]]) -> bool:
        target = self._key(key)
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("entries must be a list of dictionaries")
        return await asyncio.to_thread(self._append_batches, target, [copy.deepcopy(entries)], True)

    async def append_batches_if_absent(self, key: dict[str, str],
                                       batches: list[list[dict[str, Any]]]) -> bool:
        target = self._key(key)
        if (not isinstance(batches, list) or any(not isinstance(batch, list) for batch in batches) or
                any(not isinstance(entry, dict) for batch in batches for entry in batch)):
            raise ValueError("batches must be lists of dictionaries")
        return await asyncio.to_thread(self._append_batches, target, copy.deepcopy(batches), True)

    def _append(self, target: tuple[str, str, str], entries: list[dict[str, Any]],
                only_if_absent: bool) -> bool:
        return self._append_batches(target, [entries], only_if_absent)

    def _append_batches(self, target: tuple[str, str, str], batches: list[list[dict[str, Any]]],
                        only_if_absent: bool) -> bool:
        encoded_batches = [[json.dumps(entry, ensure_ascii=False, allow_nan=False) for entry in batch]
                           for batch in batches]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = int(time.time() * 1000)
            created = connection.execute("""
                INSERT OR IGNORE INTO kiro_sdk_sessions (project_key, session_id, subpath, mtime)
                VALUES (?, ?, ?, 0)
            """, target).rowcount == 1
            if only_if_absent and not created:
                connection.rollback()
                return False
            info = None
            if not target[2]:
                row = connection.execute("""
                    SELECT info_json FROM kiro_sdk_session_info
                    WHERE project_key = ? AND session_id = ? AND subpath = ?
                """, target).fetchone()
                info = json.loads(row[0]) if row is not None else {}
                for batch in batches:
                    info = merge_session_info(info, batch)
            connection.execute("""
                UPDATE kiro_sdk_sessions SET mtime = MAX(mtime + 1, ?)
                WHERE project_key = ? AND session_id = ? AND subpath = ?
            """, (now, *target))
            for encoded in encoded_batches:
                connection.executemany("""
                    INSERT INTO kiro_sdk_entries (project_key, session_id, subpath, entry_json)
                    VALUES (?, ?, ?, ?)
                """, [(*target, entry) for entry in encoded])
            if info is not None:
                mtime = connection.execute("""
                    SELECT mtime FROM kiro_sdk_sessions
                    WHERE project_key = ? AND session_id = ? AND subpath = ?
                """, target).fetchone()[0]
                connection.execute("""
                    INSERT INTO kiro_sdk_session_info (project_key, session_id, subpath, mtime, info_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(project_key, session_id, subpath)
                    DO UPDATE SET mtime = excluded.mtime, info_json = excluded.info_json
                """, (*target, mtime, json.dumps(info, ensure_ascii=False, allow_nan=False)))
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def load(self, key: dict[str, str]) -> Any:
        return await asyncio.to_thread(self._load, self._key(key))

    async def load_bounded(self, key: dict[str, str], *, max_entries: int, max_bytes: int,
                           skip_archives: bool = False, retain_latest_archive: bool = False) -> Any:
        """Return a bounded snapshot, optionally retaining just the latest archive."""
        _bounded_load_limits(max_entries, max_bytes)
        if type(skip_archives) is not bool or type(retain_latest_archive) is not bool:
            raise ValueError("Archive read settings must be booleans")
        if retain_latest_archive and not skip_archives:
            raise ValueError("retain_latest_archive requires skip_archives")
        return await asyncio.to_thread(self._load_bounded, self._key(key), max_entries, max_bytes,
                                       skip_archives, retain_latest_archive)

    def _load_bounded(self, target: tuple[str, str, str], max_entries: int, max_bytes: int,
                      skip_archives: bool, retain_latest_archive: bool) -> Any:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            if connection.execute("""
                SELECT 1 FROM kiro_sdk_sessions
                WHERE project_key = ? AND session_id = ? AND subpath = ?
            """, target).fetchone() is None:
                return None
            rows = connection.execute("""
                SELECT CASE WHEN length(CAST(entry_json AS BLOB)) <= ? THEN entry_json ELSE NULL END
                FROM kiro_sdk_entries
                WHERE project_key = ? AND session_id = ? AND subpath = ?
                ORDER BY sequence LIMIT ?
            """, (max(max_bytes, _MAX_ARCHIVE_ENTRY_BYTES) if skip_archives else max_bytes,
                  *target, max_entries + 1))
            entries, used, nodes, scanned = [], 0, 0, 0
            latest_archive = None
            for (raw,) in rows:
                if len(entries) >= max_entries or raw is None:
                    raise ValueError("Session mirror exceeds limits")
                raw_bytes = len(raw.encode("utf-8"))
                scanned += raw_bytes
                if skip_archives and scanned > _MAX_ARCHIVE_SCAN_BYTES:
                    raise ValueError("Session mirror exceeds archive scan limit")
                if (skip_archives and raw_bytes > _MAX_ARCHIVE_ENTRY_BYTES and
                        raw.startswith('{"type": "session_archive", "data": {')):
                    raise ValueError("Session archive entry exceeds size limit")
                canonical_archive = (skip_archives and raw.startswith('{"type": "session_archive", "data": {')
                                     and _CANONICAL_ARCHIVE_ENTRY.fullmatch(raw) is not None)
                if canonical_archive:
                    if retain_latest_archive:
                        latest_archive = (len(entries), raw)
                    entry = {"type": "session_archive"}
                else:
                    if raw_bytes > max_bytes or (not skip_archives and used + raw_bytes > max_bytes):
                        raise ValueError("Session mirror exceeds size limit")
                    _preflight_json_structure(raw, 200000 - nodes)
                    try:
                        entry = json.loads(raw)
                    except RecursionError as exc:
                        raise ValueError("Session mirror entry exceeds structural limits") from exc
                    if (skip_archives and type(entry) is dict and
                            entry.get("type") in ("session_archive", "archive_message")):
                        if raw_bytes > _MAX_ARCHIVE_ENTRY_BYTES:
                            raise ValueError("Session archive entry exceeds size limit")
                        if retain_latest_archive and entry["type"] == "session_archive":
                            latest_archive = (len(entries), entry)
                        entry = {"type": entry["type"]}
                    elif used + raw_bytes > max_bytes:
                        raise ValueError("Session mirror exceeds size limit")
                size, count = _bounded_entry_size(entry, max_bytes - used, 200000 - nodes)
                used += size if canonical_archive or skip_archives else raw_bytes
                nodes += count
                entries.append(entry)
            if latest_archive is not None:
                index, archive = latest_archive
                if type(archive) is str:
                    archive = json.loads(archive)
                _bounded_entry_size(archive, _MAX_ARCHIVE_ENTRY_BYTES, 20000)
                entries[index] = archive
            return entries
        finally:
            connection.close()

    def _load(self, target: tuple[str, str, str]) -> Any:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            exists = connection.execute("""
                SELECT 1 FROM kiro_sdk_sessions
                WHERE project_key = ? AND session_id = ? AND subpath = ?
            """, target).fetchone()
            if exists is None:
                return None
            rows = connection.execute("""
                SELECT entry_json FROM kiro_sdk_entries
                WHERE project_key = ? AND session_id = ? AND subpath = ?
                ORDER BY sequence
            """, target).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        if not isinstance(project_key, str) or not project_key or "\0" in project_key:
            raise ValueError("Invalid project key")
        return await asyncio.to_thread(self._list_sessions, project_key)

    def _list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute("""
                SELECT session_id, mtime FROM kiro_sdk_sessions
                WHERE project_key = ? AND subpath = ''
            """, (project_key,)).fetchall()
            return [{"session_id": session, "mtime": mtime} for session, mtime in rows]
        finally:
            connection.close()

    async def list_session_info(self, project_key: str) -> list[dict[str, Any]]:
        if not isinstance(project_key, str) or not project_key or "\0" in project_key:
            raise ValueError("Invalid project key")
        return await asyncio.to_thread(self._list_session_info, project_key)

    def _list_session_info(self, project_key: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute("""
                SELECT session_id, mtime, info_json FROM kiro_sdk_session_info
                WHERE project_key = ? AND subpath = ''
            """, (project_key,)).fetchall()
            return [{"session_id": session, "mtime": mtime, "info": json.loads(info)}
                    for session, mtime, info in rows]
        finally:
            connection.close()

    async def list_subkeys(self, key: dict[str, str]) -> list[str]:
        project, session, _ = self._key(key)
        return await asyncio.to_thread(self._list_subkeys, project, session)

    def _list_subkeys(self, project: str, session: str) -> list[str]:
        connection = self._connect()
        try:
            rows = connection.execute("""
                SELECT subpath FROM kiro_sdk_sessions
                WHERE project_key = ? AND session_id = ? AND subpath != ''
                ORDER BY subpath
            """, (project, session)).fetchall()
            return [row[0] for row in rows]
        finally:
            connection.close()

    async def delete(self, key: dict[str, str]) -> None:
        await asyncio.to_thread(self._delete, self._key(key))

    def _delete(self, target: tuple[str, str, str]) -> None:
        project, session, subpath = target
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if subpath:
                connection.execute("""
                    DELETE FROM kiro_sdk_sessions
                    WHERE project_key = ? AND session_id = ? AND subpath = ?
                """, target)
            else:
                connection.execute("""
                    DELETE FROM kiro_sdk_sessions
                    WHERE project_key = ? AND session_id = ?
                """, (project, session))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
