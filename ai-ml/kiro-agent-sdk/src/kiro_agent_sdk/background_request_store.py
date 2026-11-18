"""Optional durable, fail-closed claims for AgentCore background start requests."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional, Union

_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
_REQUEST = re.compile(r"^[0-9a-f]{32}$")
_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TABLE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")


def _namespace(value: str) -> str:
    if type(value) is not str or not _NAMESPACE.fullmatch(value):
        raise ValueError("Invalid background request namespace")
    return value


def _key(namespace: str, runtime_id: str, request_id: str) -> str:
    if type(runtime_id) is not str or not _SESSION.fullmatch(runtime_id):
        raise ValueError("Invalid Runtime session ID")
    if type(request_id) is not str or not _REQUEST.fullmatch(request_id):
        raise ValueError("Invalid background request ID")
    data = json.dumps(["kiro-background-request-v1", namespace, runtime_id, request_id],
                      ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(data).hexdigest()


class SQLiteBackgroundRequestStore:
    """Atomically claim request IDs in a shared, persistent SQLite file.

    Claims are not automatically released or expired. A claimed request whose
    outcome is unknown must be reconciled by the application.
    """

    def __init__(self, path: Union[str, Path], *, namespace: str = "default", timeout: float = 10.0):
        if not isinstance(path, (str, Path)) or not str(path) or str(path) == ":memory:" or "\0" in str(path):
            raise ValueError("path must be a persistent SQLite file")
        try:
            valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and
                             math.isfinite(timeout) and timeout > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout must be a positive finite number")
        self.path = Path(path).expanduser().absolute()
        self.namespace = _namespace(namespace)
        self.timeout = float(timeout)
        self._settings = (self.path, self.namespace, self.timeout)

    @staticmethod
    def _claim(path: Path, timeout: float, key: str) -> bool:
        if path.is_symlink():
            raise ValueError("Background request store path must not be a symlink")
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        db = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS kiro_background_requests (request_key TEXT PRIMARY KEY)")
            result = db.execute("INSERT OR IGNORE INTO kiro_background_requests (request_key) VALUES (?)", (key,))
            return result.rowcount == 1
        finally:
            db.close()

    @staticmethod
    def _exists(path: Path, timeout: float, key: str) -> bool:
        if path.is_symlink():
            raise ValueError("Background request store path must not be a symlink")
        if not path.exists():
            return False
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          ("kiro_background_requests",)).fetchone() is None:
                return False
            return db.execute("SELECT 1 FROM kiro_background_requests WHERE request_key=?", (key,)).fetchone() is not None
        finally:
            db.close()

    async def claim(self, runtime_id: str, request_id: str) -> bool:
        """Return True only for the first successful claim of this request."""
        path, namespace, timeout = self._settings
        if (self.path, self.namespace, self.timeout) != self._settings:
            raise ValueError("Background request store configuration changed")
        result = await asyncio.to_thread(self._claim, path, timeout, _key(namespace, runtime_id, request_id))
        if (self.path, self.namespace, self.timeout) != self._settings:
            raise ValueError("Background request store configuration changed")
        return result

    async def exists(self, runtime_id: str, request_id: str) -> bool:
        """Check whether this session's request ID has already been claimed."""
        path, namespace, timeout = self._settings
        if (self.path, self.namespace, self.timeout) != self._settings:
            raise ValueError("Background request store configuration changed")
        result = await asyncio.to_thread(self._exists, path, timeout, _key(namespace, runtime_id, request_id))
        if (self.path, self.namespace, self.timeout) != self._settings:
            raise ValueError("Background request store configuration changed")
        return result


class DynamoDBBackgroundRequestStore:
    """Atomically claim request IDs in a DynamoDB table keyed by ``request_key``."""

    def __init__(self, table_name: str, *, namespace: str = "default",
                 region_name: Optional[str] = None, client: Any = None):
        if type(table_name) is not str or not _TABLE.fullmatch(table_name):
            raise ValueError("Invalid DynamoDB table name")
        namespace = _namespace(namespace)
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.Session().client(
                "dynamodb", region_name=region_name, config=Config(retries={"total_max_attempts": 1}))
        elif region_name is not None:
            raise ValueError("Specify region_name or client, not both")
        self.table_name = table_name
        self.namespace = namespace
        self.client = client
        self._settings = (table_name, self.namespace, client)

    @staticmethod
    def _claim(client: Any, table_name: str, key: str) -> bool:
        try:
            client.put_item(
                TableName=table_name, Item={"request_key": {"S": key}},
                ConditionExpression="attribute_not_exists(#key)", ExpressionAttributeNames={"#key": "request_key"})
            return True
        except Exception as exc:
            response = getattr(exc, "response", None)
            if (isinstance(response, dict) and isinstance(response.get("Error"), dict) and
                    response["Error"].get("Code") == "ConditionalCheckFailedException"):
                return False
            raise

    @staticmethod
    def _exists(client: Any, table_name: str, key: str) -> bool:
        response = client.get_item(TableName=table_name, Key={"request_key": {"S": key}}, ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return False
        if type(item) is not dict or item.get("request_key") != {"S": key}:
            raise ValueError("Invalid background request claim")
        return True

    async def claim(self, runtime_id: str, request_id: str) -> bool:
        """Return True only for the first successful claim of this request."""
        table, namespace, client = self._settings
        if self.table_name != table or self.namespace != namespace or self.client is not client:
            raise ValueError("Background request store configuration changed")
        result = await asyncio.to_thread(self._claim, client, table, _key(namespace, runtime_id, request_id))
        if self.table_name != table or self.namespace != namespace or self.client is not client:
            raise ValueError("Background request store configuration changed")
        return result

    async def exists(self, runtime_id: str, request_id: str) -> bool:
        """Check a request claim using a strongly consistent read."""
        table, namespace, client = self._settings
        if self.table_name != table or self.namespace != namespace or self.client is not client:
            raise ValueError("Background request store configuration changed")
        result = await asyncio.to_thread(self._exists, client, table, _key(namespace, runtime_id, request_id))
        if self.table_name != table or self.namespace != namespace or self.client is not client:
            raise ValueError("Background request store configuration changed")
        return result
