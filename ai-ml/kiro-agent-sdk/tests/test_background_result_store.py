import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from kiro_agent_sdk import DynamoDBBackgroundResultStore, SQLiteBackgroundResultStore
from kiro_agent_sdk.background_result_store import MAX_BACKGROUND_SNAPSHOT_BYTES

TASK = "sdk-task-" + "a" * 32


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeDynamoDB:
    def __init__(self):
        self.items, self.checkpoints, self.calls = {}, {}, []
        self.lock = threading.Lock()
        self.failure = None

    def put_item(self, **kwargs):
        with self.lock:
            self.calls.append(("put", kwargs))
            if self.failure:
                raise self.failure
            key, payload = kwargs["Item"]["snapshot_key"]["S"], kwargs["Item"]["payload"]["B"]
            if "sequence" in kwargs["Item"]:
                sequence = kwargs["Item"]["sequence"]["S"]
                if key in self.checkpoints:
                    previous, existing = self.checkpoints[key]
                    if existing == b"" or sequence < previous or (sequence == previous and existing != payload):
                        raise ConditionalFailure()
                self.checkpoints[key] = (sequence, payload)
                return {}
            if key in self.items and self.items[key] != payload:
                raise ConditionalFailure()
            self.items[key] = payload
            return {}

    def get_item(self, **kwargs):
        with self.lock:
            self.calls.append(("get", kwargs))
            if self.failure:
                raise self.failure
            key = kwargs["Key"]["snapshot_key"]["S"]
            if key in self.checkpoints:
                sequence, payload = self.checkpoints[key]
                return {"Item": {"snapshot_key": {"S": key}, "sequence": {"S": sequence},
                                 "payload": {"B": payload}}}
            if key not in self.items:
                return {}
            return {"Item": {"snapshot_key": {"S": key}, "payload": {"B": self.items[key]}}}

    def update_item(self, **kwargs):
        with self.lock:
            self.calls.append(("update", kwargs))
            if self.failure:
                raise self.failure
            key = kwargs["Key"]["snapshot_key"]["S"]
            if key in self.checkpoints:
                self.checkpoints[key] = (self.checkpoints[key][0], b"")
                return {}
            if key not in self.items:
                raise ConditionalFailure()
            self.items[key] = b""
            return {}


class BackgroundResultStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_monotonicity_and_tombstones_for_both_stores(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakeDynamoDB()
            for store in (SQLiteBackgroundResultStore(Path(root) / "checkpoints.sqlite"),
                          DynamoDBBackgroundResultStore("results-table", client=fake)):
                with self.subTest(store=type(store).__name__):
                    self.assertFalse(await store.delete_event_checkpoint("session-a", TASK))
                    self.assertTrue(await store.save_event_checkpoint("session-a", TASK, 0, b"zero"))
                    self.assertTrue(await store.save_event_checkpoint("session-a", TASK, 0, b"zero"))
                    self.assertFalse(await store.save_event_checkpoint("session-a", TASK, 0, b"other"))
                    await asyncio.gather(*(store.save_event_checkpoint("session-a", TASK, n, str(n).encode())
                                           for n in range(1, 31)))
                    self.assertEqual(await store.load_event_checkpoint("session-a", TASK), (30, b"30"))
                    self.assertFalse(await store.save_event_checkpoint("session-a", TASK, 29, b"29"))
                    self.assertIsNone(await store.load_event_checkpoint("session-b", TASK))
                    self.assertTrue(await store.save_event_checkpoint("session-a", TASK, 2**64 - 1, b"last"))
                    self.assertEqual(await store.load_event_checkpoint("session-a", TASK), (2**64 - 1, b"last"))
                    for invalid in (-1, 2**64, True, "1"):
                        with self.assertRaises(ValueError):
                            await store.save_event_checkpoint("session-a", TASK, invalid, b"x")
                    self.assertTrue(await store.delete_event_checkpoint("session-a", TASK))
                    self.assertTrue(await store.delete_event_checkpoint("session-a", TASK))
                    self.assertIsNone(await store.load_event_checkpoint("session-a", TASK))
                    self.assertFalse(await store.save_event_checkpoint("session-a", TASK, 2**64 - 1, b"last"))
            put = next(args for operation, args in fake.calls if operation == "put")
            self.assertIn("#sequence < :sequence", put["ConditionExpression"])
            self.assertIn("#payload <> :empty", put["ConditionExpression"])
            self.assertTrue(next(args for operation, args in fake.calls if operation == "get")["ConsistentRead"])

    async def test_sqlite_checkpoint_rejects_corrupt_fields(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "checkpoints.sqlite"
            store = SQLiteBackgroundResultStore(path)
            self.assertTrue(await store.save_event_checkpoint("session-a", TASK, 1, b"event"))
            with sqlite3.connect(path) as db:
                db.execute("UPDATE kiro_background_event_checkpoints SET sequence=?", ("x" * 1000,))
            with self.assertRaises(ValueError):
                await store.load_event_checkpoint("session-a", TASK)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE kiro_background_event_checkpoints SET sequence=?, payload=?",
                           ("00000000000000000001", b"x" * (MAX_BACKGROUND_SNAPSHOT_BYTES + 1)))
            with self.assertRaises(ValueError):
                await store.load_event_checkpoint("session-a", TASK)

    async def test_sqlite_atomic_idempotency_and_session_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "snapshots.sqlite"
            first, second = SQLiteBackgroundResultStore(path), SQLiteBackgroundResultStore(path)
            outcomes = await asyncio.gather(*(store.save("session-a", TASK, payload) for store, payload in
                [(first if index % 2 else second, b"first" if index % 2 else b"second") for index in range(20)]))
            stored = await first.load("session-a", TASK)
            self.assertIn(stored, (b"first", b"second"))
            self.assertEqual(sum(outcomes), 10)
            self.assertTrue(await second.save("session-a", TASK, stored))
            self.assertFalse(await second.save("session-a", TASK, b"other"))
            self.assertIsNone(await first.load("session-b", TASK))
            self.assertIsNone(await SQLiteBackgroundResultStore(path, namespace="other").load("session-a", TASK))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(path) as db:
                key, value = db.execute("SELECT snapshot_key, payload FROM kiro_background_results").fetchone()
            self.assertEqual(len(key), 64)
            self.assertNotIn("session-a", key)
            self.assertNotIn(TASK, key)
            self.assertEqual(value, stored)
            self.assertFalse(await first.delete("session-b", TASK))
            self.assertTrue(await first.delete("session-a", TASK))
            self.assertTrue(await second.delete("session-a", TASK))
            self.assertIsNone(await first.load("session-a", TASK))
            self.assertFalse(await second.save("session-a", TASK, stored))
            self.assertTrue(await first.save_events("session-a", TASK, b"events"))
            self.assertEqual(await second.load_events("session-a", TASK), b"events")
            self.assertTrue(await first.delete_events("session-a", TASK))
            self.assertIsNone(await second.load_events("session-a", TASK))
            self.assertFalse(await first.save_events("session-a", TASK, b"events"))

    async def test_sqlite_invalid_input_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "snapshots.sqlite"
            store = SQLiteBackgroundResultStore(path)
            for session, task, payload in (("../bad", TASK, b"x"), ("session", "bad", b"x"),
                                           ("session", TASK, b""), ("session", TASK, "text"),
                                           ("session", TASK, b"x" * (MAX_BACKGROUND_SNAPSHOT_BYTES + 1))):
                with self.subTest(session=session, task=task), self.assertRaises(ValueError):
                    await store.save(session, task, payload)
            target = Path(root) / "target"
            target.write_text("")
            link = Path(root) / "link"
            os.symlink(target, link)
            with self.assertRaises(ValueError):
                await SQLiteBackgroundResultStore(link).load("session", TASK)
            store.namespace = "changed"
            with self.assertRaises(ValueError):
                await store.save("session", TASK, b"x")

    async def test_dynamodb_conditional_save_and_consistent_read(self):
        client = FakeDynamoDB()
        first = DynamoDBBackgroundResultStore("results-table", client=client)
        second = DynamoDBBackgroundResultStore("results-table", client=client)
        self.assertTrue(await first.save("session-a", TASK, b"private"))
        self.assertTrue(await second.save("session-a", TASK, b"private"))
        self.assertFalse(await second.save("session-a", TASK, b"different"))
        self.assertEqual(await second.load("session-a", TASK), b"private")
        self.assertIsNone(await second.load("session-b", TASK))
        write = client.calls[0][1]
        read = next(call for method, call in client.calls if method == "get")
        self.assertEqual(write["ConditionExpression"], "attribute_not_exists(#key) OR #payload = :payload")
        self.assertTrue(read["ConsistentRead"])
        self.assertNotIn(TASK, repr(read))
        self.assertNotIn("session-a", repr(read))
        self.assertFalse(await second.delete("session-b", TASK))
        self.assertTrue(await second.delete("session-a", TASK))
        self.assertTrue(await second.delete("session-a", TASK))
        self.assertIsNone(await first.load("session-a", TASK))
        self.assertFalse(await first.save("session-a", TASK, b"private"))
        self.assertTrue(await first.save_events("session-a", TASK, b"events"))
        self.assertEqual(await second.load_events("session-a", TASK), b"events")
        self.assertTrue(await second.delete_events("session-a", TASK))
        self.assertIsNone(await first.load_events("session-a", TASK))
        self.assertFalse(await first.save_events("session-a", TASK, b"events"))
        delete = next(call for method, call in client.calls if method == "update")
        self.assertEqual(delete["ConditionExpression"], "attribute_exists(#key)")
        client.failure = RuntimeError("private backend failure")
        with self.assertRaises(RuntimeError):
            await first.load("session-a", TASK)
        client.failure = None
        first.table_name = "changed-table"
        with self.assertRaises(ValueError):
            await first.load("session-a", TASK)

    def test_configuration_is_validated(self):
        for value in (False, 0, -1, float("inf"), float("nan"), 10**1000):
            with self.subTest(timeout=str(value)[:20]), self.assertRaises(ValueError):
                SQLiteBackgroundResultStore("/tmp/unused", timeout=value)
        for table in ("", "a", "a/b", "a" * 256):
            with self.subTest(table=table), self.assertRaises(ValueError):
                DynamoDBBackgroundResultStore(table, client=FakeDynamoDB())
