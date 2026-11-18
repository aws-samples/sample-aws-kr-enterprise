"""Explicit session-owner bindings for deployments sharing a trusted SQLite file."""
from __future__ import annotations

import asyncio
import math
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any, Optional, Union

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class SQLiteSessionOwnership:
    """Persist pre-registered ownership; never claim an unregistered session.

    Protect the containing directory and use a filesystem that supports SQLite
    locking. Cancellation cannot stop a write already running in a thread.
    """

    def __init__(self, path: Union[str, Path], *, timeout: float = 10.0) -> None:
        if not isinstance(path, (str, Path)) or not str(path) or str(path) == ":memory:":
            raise ValueError("path must be a persistent SQLite file")
        try:
            valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and
                             math.isfinite(timeout) and timeout > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout must be a positive finite number")
        self.path = Path(path).expanduser().absolute()
        self.timeout = float(timeout)

    @staticmethod
    def _validate(session_id: str, owner: str) -> None:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid runtime session ID")
        if (not isinstance(owner, str) or not owner or "\0" in owner or
                len(owner.encode("utf-8")) > 1024):
            raise ValueError("Invalid session owner")

    @staticmethod
    def _private(path: Path, *, directory: bool) -> None:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or
                (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)) or
                (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077)):
            raise ValueError("Session ownership store must be private and must not be a symlink")

    def _connect(self) -> sqlite3.Connection:
        self._private(self.path.parent, directory=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self._private(self.path, directory=False)
        connection = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS session_owners
                (session_id TEXT PRIMARY KEY, owner TEXT NOT NULL)""")
        except BaseException:
            connection.close()
            raise
        return connection

    async def register(self, session_id: str, owner: str) -> bool:
        """Register a session, or return False if it belongs to a different owner."""
        self._validate(session_id, owner)
        return await asyncio.to_thread(self._register, session_id, owner)

    def _register(self, session_id: str, owner: str) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR IGNORE INTO session_owners (session_id, owner) VALUES (?, ?)",
                               (session_id, owner))
            row = connection.execute("SELECT owner FROM session_owners WHERE session_id = ?",
                                     (session_id,)).fetchone()
            connection.commit()
            return row is not None and row[0] == owner
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def is_owner(self, session_id: str, owner: str) -> bool:
        """Check a binding. Missing sessions are denied."""
        self._validate(session_id, owner)
        return await asyncio.to_thread(self._is_owner, session_id, owner)

    def _is_owner(self, session_id: str, owner: str) -> bool:
        connection = self._connect()
        try:
            row = connection.execute("SELECT owner FROM session_owners WHERE session_id = ?",
                                     (session_id,)).fetchone()
            return row is not None and row[0] == owner
        finally:
            connection.close()

    async def revoke(self, session_id: str, owner: str) -> bool:
        """Remove only a binding for the specified owner."""
        self._validate(session_id, owner)
        return await asyncio.to_thread(self._revoke, session_id, owner)

    def _revoke(self, session_id: str, owner: str) -> bool:
        connection = self._connect()
        try:
            return connection.execute("DELETE FROM session_owners WHERE session_id = ? AND owner = ?",
                                      (session_id, owner)).rowcount == 1
        finally:
            connection.close()


class DynamoDBSessionOwnership:
    """Pre-registered owners in one DynamoDB region using conditional writes.

    The dedicated table must have a string partition key named ``session_id``
    and no sort key. Global-table cross-region replication is not a lock.
    """

    def __init__(self, table_name: str, *, region_name: Optional[str] = None,
                 client: Optional[Any] = None) -> None:
        if not isinstance(table_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table_name):
            raise ValueError("Invalid DynamoDB table name")
        if client is None:
            import boto3
            client = boto3.client("dynamodb", region_name=region_name)
        elif region_name is not None:
            raise ValueError("Specify region_name or client, not both")
        self.table_name = table_name
        self.client = client

    @staticmethod
    def _condition_failed(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        return (isinstance(response, dict) and isinstance(response.get("Error"), dict) and
                response["Error"].get("Code") == "ConditionalCheckFailedException")

    async def register(self, session_id: str, owner: str) -> bool:
        """Register only if the session is unowned or already has this owner."""
        SQLiteSessionOwnership._validate(session_id, owner)
        return await asyncio.to_thread(self._register, session_id, owner)

    def _register(self, session_id: str, owner: str) -> bool:
        try:
            self.client.put_item(
                TableName=self.table_name, Item={"session_id": {"S": session_id}, "owner": {"S": owner}},
                ConditionExpression="attribute_not_exists(#session) OR #owner = :owner",
                ExpressionAttributeNames={"#session": "session_id", "#owner": "owner"},
                ExpressionAttributeValues={":owner": {"S": owner}})
            return True
        except Exception as exc:
            if self._condition_failed(exc):
                return False
            raise

    async def is_owner(self, session_id: str, owner: str) -> bool:
        """Use a strongly consistent read; a missing session is denied."""
        SQLiteSessionOwnership._validate(session_id, owner)
        return await asyncio.to_thread(self._is_owner, session_id, owner)

    def _is_owner(self, session_id: str, owner: str) -> bool:
        result = self.client.get_item(TableName=self.table_name, Key={"session_id": {"S": session_id}},
                                      ConsistentRead=True)
        item = result.get("Item")
        if item is None:
            return False
        value = item.get("owner") if isinstance(item, dict) else None
        if not isinstance(value, dict) or set(value) != {"S"} or not isinstance(value["S"], str):
            raise ValueError("Invalid DynamoDB session ownership item")
        return value["S"] == owner

    async def revoke(self, session_id: str, owner: str) -> bool:
        """Remove only a binding for the specified owner."""
        SQLiteSessionOwnership._validate(session_id, owner)
        return await asyncio.to_thread(self._revoke, session_id, owner)

    def _revoke(self, session_id: str, owner: str) -> bool:
        try:
            self.client.delete_item(TableName=self.table_name, Key={"session_id": {"S": session_id}},
                                    ConditionExpression="#owner = :owner",
                                    ExpressionAttributeNames={"#owner": "owner"},
                                    ExpressionAttributeValues={":owner": {"S": owner}})
            return True
        except Exception as exc:
            if self._condition_failed(exc):
                return False
            raise
