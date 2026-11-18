"""Opt-in durable snapshots and event checkpoints for SDK background tasks."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from .sqlite_requirements import require_upsert
from pathlib import Path
from typing import Any, Optional, Union

from .background_request_store import _namespace, _SESSION, _TABLE

_TASK = re.compile(r"^sdk-task-[0-9a-f]{32}$")
MAX_BACKGROUND_SNAPSHOT_BYTES = 256 * 1024


def _key(namespace: str, runtime_id: str, task_id: str, *, events: bool = False,
         checkpoint: bool = False) -> str:
    if type(runtime_id) is not str or not _SESSION.fullmatch(runtime_id):
        raise ValueError("Invalid Runtime session ID")
    if type(task_id) is not str or not _TASK.fullmatch(task_id):
        raise ValueError("Invalid background task ID")
    domain = ("kiro-background-event-checkpoint-v1" if checkpoint else
              "kiro-background-event-snapshot-v1" if events else "kiro-background-snapshot-v1")
    data = json.dumps([domain, namespace, runtime_id, task_id],
                      separators=(",", ":")).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def _payload(value: bytes) -> bytes:
    if type(value) is not bytes or not 0 < len(value) <= MAX_BACKGROUND_SNAPSHOT_BYTES:
        raise ValueError("Invalid background snapshot")
    return value


def _sequence(value: int) -> str:
    if type(value) is not int or not 0 <= value <= 2**64 - 1:
        raise ValueError("Invalid background event sequence")
    return f"{value:020d}"


def _stored_sequence(value: Any) -> int:
    if type(value) is not str or not re.fullmatch(r"[0-9]{20}", value):
        raise ValueError("Invalid background event sequence")
    number = int(value)
    if _sequence(number) != value:
        raise ValueError("Invalid background event sequence")
    return number


class SQLiteBackgroundResultStore:
    """Store immutable task snapshots in a persistent, trusted SQLite file."""

    def __init__(self, path: Union[str, Path], *, namespace: str = "default", timeout: float = 10.0):
        require_upsert()
        if not isinstance(path, (str, Path)) or not str(path) or str(path) == ":memory:" or "\0" in str(path):
            raise ValueError("path must be a persistent SQLite file")
        try:
            valid = (not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and
                     math.isfinite(timeout) and timeout > 0)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("timeout must be a positive finite number")
        self.path = Path(path).expanduser().absolute()
        self.namespace = _namespace(namespace)
        self.timeout = float(timeout)
        self._settings = (self.path, self.namespace, self.timeout)

    @staticmethod
    def _connect(path: Path, timeout: float) -> sqlite3.Connection:
        require_upsert()
        if path.is_symlink():
            raise ValueError("Background result store path must not be a symlink")
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        db = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS kiro_background_results "
                       "(snapshot_key TEXT PRIMARY KEY, payload BLOB NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS kiro_background_event_checkpoints "
                       "(snapshot_key TEXT PRIMARY KEY, sequence TEXT NOT NULL, payload BLOB NOT NULL)")
        except BaseException:
            db.close()
            raise
        return db

    @classmethod
    def _save(cls, path: Path, timeout: float, key: str, payload: bytes) -> bool:
        db = cls._connect(path, timeout)
        try:
            result = db.execute(
                "INSERT INTO kiro_background_results (snapshot_key, payload) VALUES (?, ?) "
                "ON CONFLICT(snapshot_key) DO UPDATE SET payload=excluded.payload "
                "WHERE payload=excluded.payload", (key, payload))
            return result.rowcount == 1
        finally:
            db.close()

    @classmethod
    def _load(cls, path: Path, timeout: float, key: str) -> Optional[bytes]:
        db = cls._connect(path, timeout)
        try:
            row = db.execute(
                "SELECT CASE WHEN typeof(payload)='blob' AND length(payload) BETWEEN 0 AND ? "
                "THEN payload ELSE NULL END FROM kiro_background_results WHERE snapshot_key=?",
                (MAX_BACKGROUND_SNAPSHOT_BYTES, key)).fetchone()
            if row is None or row[0] == b"":
                return None
            return _payload(row[0])
        finally:
            db.close()

    @classmethod
    def _delete(cls, path: Path, timeout: float, key: str) -> bool:
        db = cls._connect(path, timeout)
        try:
            result = db.execute("UPDATE kiro_background_results SET payload=? WHERE snapshot_key=?",
                                (b"", key))
            return result.rowcount == 1
        finally:
            db.close()

    def _snapshot_settings(self) -> tuple[Path, str, float]:
        if (self.path, self.namespace, self.timeout) != self._settings:
            raise ValueError("Background result store configuration changed")
        return self._settings

    async def save(self, runtime_id: str, task_id: str, payload: bytes) -> bool:
        """Save once, or return True when the existing bytes are identical."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._save, path, timeout, _key(namespace, runtime_id, task_id),
                                         _payload(payload))
        self._snapshot_settings()
        return result

    async def load(self, runtime_id: str, task_id: str) -> Optional[bytes]:
        """Load a snapshot for this Runtime session and task."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._load, path, timeout, _key(namespace, runtime_id, task_id))
        self._snapshot_settings()
        return result

    async def delete(self, runtime_id: str, task_id: str) -> bool:
        """Replace an existing snapshot with a tombstone that prevents re-creation."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._delete, path, timeout, _key(namespace, runtime_id, task_id))
        self._snapshot_settings()
        return result

    async def save_events(self, runtime_id: str, task_id: str, payload: bytes) -> bool:
        """Save a separate immutable event snapshot."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._save, path, timeout, _key(namespace, runtime_id, task_id, events=True),
                                         _payload(payload))
        self._snapshot_settings()
        return result

    async def load_events(self, runtime_id: str, task_id: str) -> Optional[bytes]:
        """Load a separate event snapshot."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._load, path, timeout, _key(namespace, runtime_id, task_id, events=True))
        self._snapshot_settings()
        return result

    async def delete_events(self, runtime_id: str, task_id: str) -> bool:
        """Tombstone an event snapshot independently of the result snapshot."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._delete, path, timeout, _key(namespace, runtime_id, task_id, events=True))
        self._snapshot_settings()
        return result

    @classmethod
    def _save_checkpoint(cls, path: Path, timeout: float, key: str, sequence: str, payload: bytes) -> bool:
        db = cls._connect(path, timeout)
        try:
            result = db.execute(
                "INSERT INTO kiro_background_event_checkpoints (snapshot_key, sequence, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(snapshot_key) DO UPDATE SET sequence=excluded.sequence, payload=excluded.payload "
                "WHERE kiro_background_event_checkpoints.payload != X'' AND "
                "(kiro_background_event_checkpoints.sequence < excluded.sequence OR "
                "(kiro_background_event_checkpoints.sequence = excluded.sequence AND "
                "kiro_background_event_checkpoints.payload = excluded.payload))", (key, sequence, payload))
            return result.rowcount == 1
        finally:
            db.close()

    @classmethod
    def _load_checkpoint(cls, path: Path, timeout: float, key: str) -> Optional[tuple[int, bytes]]:
        db = cls._connect(path, timeout)
        try:
            row = db.execute(
                "SELECT CASE WHEN typeof(sequence)='text' AND length(sequence)=20 THEN sequence ELSE NULL END, "
                "CASE WHEN typeof(payload)='blob' AND length(payload) BETWEEN 0 AND ? "
                "THEN payload ELSE NULL END FROM kiro_background_event_checkpoints WHERE snapshot_key=?",
                (MAX_BACKGROUND_SNAPSHOT_BYTES, key)).fetchone()
            if row is None or row[1] == b"":
                return None
            return _stored_sequence(row[0]), _payload(row[1])
        finally:
            db.close()

    @classmethod
    def _delete_checkpoint(cls, path: Path, timeout: float, key: str) -> bool:
        db = cls._connect(path, timeout)
        try:
            result = db.execute(
                "UPDATE kiro_background_event_checkpoints SET payload=? WHERE snapshot_key=?", (b"", key))
            return result.rowcount == 1
        finally:
            db.close()

    async def save_event_checkpoint(self, runtime_id: str, task_id: str, sequence: int, payload: bytes) -> bool:
        """Atomically save a newer checkpoint, or accept identical bytes at the same sequence."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._save_checkpoint, path, timeout,
                                         _key(namespace, runtime_id, task_id, checkpoint=True),
                                         _sequence(sequence), _payload(payload))
        self._snapshot_settings()
        return result

    async def load_event_checkpoint(self, runtime_id: str, task_id: str) -> Optional[tuple[int, bytes]]:
        """Load the latest checkpoint and its sequence."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._load_checkpoint, path, timeout,
                                         _key(namespace, runtime_id, task_id, checkpoint=True))
        self._snapshot_settings()
        return result

    async def delete_event_checkpoint(self, runtime_id: str, task_id: str) -> bool:
        """Tombstone an existing checkpoint and prevent future writes for this task."""
        path, namespace, timeout = self._snapshot_settings()
        result = await asyncio.to_thread(self._delete_checkpoint, path, timeout,
                                         _key(namespace, runtime_id, task_id, checkpoint=True))
        self._snapshot_settings()
        return result


class DynamoDBBackgroundResultStore:
    """Store immutable snapshots in a table keyed by string ``snapshot_key``."""

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
        self.table_name, self.namespace, self.client = table_name, namespace, client
        self._settings = (table_name, namespace, client)

    @staticmethod
    def _save(client: Any, table: str, key: str, payload: bytes) -> bool:
        try:
            client.put_item(TableName=table, Item={"snapshot_key": {"S": key}, "payload": {"B": payload}},
                ConditionExpression="attribute_not_exists(#key) OR #payload = :payload",
                ExpressionAttributeNames={"#key": "snapshot_key", "#payload": "payload"},
                ExpressionAttributeValues={":payload": {"B": payload}})
            return True
        except Exception as exc:
            response = getattr(exc, "response", None)
            if (isinstance(response, dict) and isinstance(response.get("Error"), dict) and
                    response["Error"].get("Code") == "ConditionalCheckFailedException"):
                return False
            raise

    @staticmethod
    def _load(client: Any, table: str, key: str) -> Optional[bytes]:
        response = client.get_item(TableName=table, Key={"snapshot_key": {"S": key}}, ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return None
        if (type(item) is not dict or set(item) != {"snapshot_key", "payload"} or
                item["snapshot_key"] != {"S": key} or type(item["payload"]) is not dict or
                set(item["payload"]) != {"B"}):
            raise ValueError("Invalid background snapshot item")
        payload = item["payload"]["B"]
        return None if type(payload) is bytes and payload == b"" else _payload(payload)

    @staticmethod
    def _delete(client: Any, table: str, key: str) -> bool:
        try:
            client.update_item(
                TableName=table, Key={"snapshot_key": {"S": key}},
                UpdateExpression="SET #payload = :empty", ConditionExpression="attribute_exists(#key)",
                ExpressionAttributeNames={"#key": "snapshot_key", "#payload": "payload"},
                ExpressionAttributeValues={":empty": {"B": b""}})
            return True
        except Exception as exc:
            response = getattr(exc, "response", None)
            if (isinstance(response, dict) and isinstance(response.get("Error"), dict) and
                    response["Error"].get("Code") == "ConditionalCheckFailedException"):
                return False
            raise

    def _snapshot_settings(self) -> tuple[str, str, Any]:
        table, namespace, client = self._settings
        if self.table_name != table or self.namespace != namespace or self.client is not client:
            raise ValueError("Background result store configuration changed")
        return table, namespace, client

    async def save(self, runtime_id: str, task_id: str, payload: bytes) -> bool:
        """Save once, or return True when the existing bytes are identical."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._save, client, table, _key(namespace, runtime_id, task_id),
                                         _payload(payload))
        self._snapshot_settings()
        return result

    async def load(self, runtime_id: str, task_id: str) -> Optional[bytes]:
        """Load a snapshot using a strongly consistent read."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._load, client, table, _key(namespace, runtime_id, task_id))
        self._snapshot_settings()
        return result

    async def delete(self, runtime_id: str, task_id: str) -> bool:
        """Replace an existing snapshot with a tombstone that prevents re-creation."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._delete, client, table, _key(namespace, runtime_id, task_id))
        self._snapshot_settings()
        return result

    async def save_events(self, runtime_id: str, task_id: str, payload: bytes) -> bool:
        """Save a separate immutable event snapshot."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._save, client, table, _key(namespace, runtime_id, task_id, events=True),
                                         _payload(payload))
        self._snapshot_settings()
        return result

    async def load_events(self, runtime_id: str, task_id: str) -> Optional[bytes]:
        """Load a separate event snapshot using a strongly consistent read."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._load, client, table, _key(namespace, runtime_id, task_id, events=True))
        self._snapshot_settings()
        return result

    async def delete_events(self, runtime_id: str, task_id: str) -> bool:
        """Tombstone an event snapshot independently of the result snapshot."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._delete, client, table, _key(namespace, runtime_id, task_id, events=True))
        self._snapshot_settings()
        return result

    @staticmethod
    def _save_checkpoint(client: Any, table: str, key: str, sequence: str, payload: bytes) -> bool:
        try:
            client.put_item(
                TableName=table,
                Item={"snapshot_key": {"S": key}, "sequence": {"S": sequence}, "payload": {"B": payload}},
                ConditionExpression=("attribute_not_exists(#key) OR (#payload <> :empty AND "
                                     "(#sequence < :sequence OR "
                                     "(#sequence = :sequence AND #payload = :payload)))"),
                ExpressionAttributeNames={"#key": "snapshot_key", "#sequence": "sequence", "#payload": "payload"},
                ExpressionAttributeValues={":empty": {"B": b""}, ":sequence": {"S": sequence},
                                           ":payload": {"B": payload}})
            return True
        except Exception as exc:
            response = getattr(exc, "response", None)
            if (isinstance(response, dict) and isinstance(response.get("Error"), dict) and
                    response["Error"].get("Code") == "ConditionalCheckFailedException"):
                return False
            raise

    @staticmethod
    def _load_checkpoint(client: Any, table: str, key: str) -> Optional[tuple[int, bytes]]:
        response = client.get_item(TableName=table, Key={"snapshot_key": {"S": key}}, ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return None
        if (type(item) is not dict or set(item) != {"snapshot_key", "sequence", "payload"} or
                item["snapshot_key"] != {"S": key} or type(item["sequence"]) is not dict or
                set(item["sequence"]) != {"S"} or type(item["payload"]) is not dict or
                set(item["payload"]) != {"B"}):
            raise ValueError("Invalid background checkpoint item")
        payload = item["payload"]["B"]
        if type(payload) is bytes and payload == b"":
            return None
        return _stored_sequence(item["sequence"]["S"]), _payload(payload)

    async def save_event_checkpoint(self, runtime_id: str, task_id: str, sequence: int, payload: bytes) -> bool:
        """Atomically save a newer checkpoint, or accept identical bytes at the same sequence."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._save_checkpoint, client, table,
                                         _key(namespace, runtime_id, task_id, checkpoint=True),
                                         _sequence(sequence), _payload(payload))
        self._snapshot_settings()
        return result

    async def load_event_checkpoint(self, runtime_id: str, task_id: str) -> Optional[tuple[int, bytes]]:
        """Load the latest checkpoint using a strongly consistent read."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._load_checkpoint, client, table,
                                         _key(namespace, runtime_id, task_id, checkpoint=True))
        self._snapshot_settings()
        return result

    async def delete_event_checkpoint(self, runtime_id: str, task_id: str) -> bool:
        """Tombstone an existing checkpoint and prevent future writes for this task."""
        table, namespace, client = self._snapshot_settings()
        result = await asyncio.to_thread(self._delete, client, table,
                                         _key(namespace, runtime_id, task_id, checkpoint=True))
        self._snapshot_settings()
        return result
