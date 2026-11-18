import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from kiro_agent_sdk.background_request_store import DynamoDBBackgroundRequestStore, SQLiteBackgroundRequestStore


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeDynamoDB:
    def __init__(self):
        self.items = set()
        self.lock = threading.Lock()
        self.calls = []
        self.failure = None

    def put_item(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
            if self.failure:
                raise self.failure
            key = kwargs["Item"]["request_key"]["S"]
            if key in self.items:
                raise ConditionalFailure()
            self.items.add(key)
            return {}

    def get_item(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
            if self.failure:
                raise self.failure
            key = kwargs["Key"]["request_key"]["S"]
            return {"Item": {"request_key": {"S": key}}} if key in self.items else {}


class BackgroundRequestStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_claim_is_atomic_and_does_not_store_raw_identifiers(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "claims.sqlite"
            stores = [SQLiteBackgroundRequestStore(path), SQLiteBackgroundRequestStore(path)]
            self.assertFalse(await stores[0].exists("private-session", "a" * 32))
            self.assertFalse(path.exists())
            claims = await asyncio.gather(*(stores[index % 2].claim("private-session", "a" * 32)
                                            for index in range(32)))
            self.assertEqual(sum(claims), 1)
            self.assertTrue(await stores[0].exists("private-session", "a" * 32))
            self.assertFalse(await stores[0].exists("other-session", "a" * 32))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(path) as db:
                values = db.execute("SELECT request_key FROM kiro_background_requests").fetchall()
            self.assertEqual(len(values), 1)
            self.assertEqual(len(values[0][0]), 64)
            self.assertNotIn("private-session", values[0][0])
            self.assertNotIn("a" * 32, values[0][0])
            other = SQLiteBackgroundRequestStore(path, namespace="other")
            self.assertTrue(await other.claim("private-session", "a" * 32))

    async def test_sqlite_exists_handles_special_characters_in_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteBackgroundRequestStore(Path(root) / "claims #1.sqlite")
            self.assertTrue(await store.claim("session", "a" * 32))
            self.assertTrue(await store.exists("session", "a" * 32))

    async def test_sqlite_rejects_invalid_input_symlinks_and_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "claims.sqlite"
            for invalid in ("", "../bad", "bad\n", "x" * 257):
                with self.subTest(session=invalid), self.assertRaises(ValueError):
                    await SQLiteBackgroundRequestStore(path).claim(invalid, "a" * 32)
            with self.assertRaises(ValueError):
                await SQLiteBackgroundRequestStore(path).claim("session", "bad")
            target = Path(root) / "target"
            target.write_text("")
            link = Path(root) / "link"
            os.symlink(target, link)
            with self.assertRaises(ValueError):
                await SQLiteBackgroundRequestStore(link).claim("session", "a" * 32)
            with self.assertRaises(ValueError):
                await SQLiteBackgroundRequestStore(link).exists("session", "a" * 32)
            store = SQLiteBackgroundRequestStore(path)
            store.namespace = "changed"
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                await store.claim("session", "a" * 32)
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                await store.exists("session", "a" * 32)

    async def test_dynamodb_uses_conditional_writes_and_atomic_claims(self):
        client = FakeDynamoDB()
        stores = [DynamoDBBackgroundRequestStore("background-claims", client=client),
                  DynamoDBBackgroundRequestStore("background-claims", client=client)]
        claims = await asyncio.gather(*(stores[index % 2].claim("private-session", "a" * 32)
                                        for index in range(32)))
        self.assertEqual(sum(claims), 1)
        call = client.calls[0]
        self.assertEqual(call["ConditionExpression"], "attribute_not_exists(#key)")
        self.assertEqual(call["ExpressionAttributeNames"], {"#key": "request_key"})
        self.assertEqual(len(call["Item"]["request_key"]["S"]), 64)
        self.assertNotIn("private-session", repr(call))
        self.assertNotIn("a" * 32, repr(call))
        self.assertTrue(await stores[0].exists("private-session", "a" * 32))
        self.assertFalse(await stores[0].exists("other-session", "a" * 32))
        self.assertTrue(client.calls[-1]["ConsistentRead"])

    async def test_dynamodb_failure_and_mutated_configuration_do_not_claim(self):
        client = FakeDynamoDB()
        store = DynamoDBBackgroundRequestStore("background-claims", client=client)
        client.failure = RuntimeError("private")
        with self.assertRaises(RuntimeError):
            await store.claim("session", "a" * 32)
        client.failure = None
        store.table_name = "different"
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            await store.claim("session", "a" * 32)
        self.assertEqual(len(client.calls), 1)

    def test_configuration_is_validated(self):
        for namespace in ("", "../bad", "a" * 129):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                SQLiteBackgroundRequestStore("/tmp/unused", namespace=namespace)
        for value in (False, 0, -1, float("inf"), float("nan"), 10**1000):
            with self.subTest(timeout=str(value)[:20]), self.assertRaises(ValueError):
                SQLiteBackgroundRequestStore("/tmp/unused", timeout=value)
        for table in ("", "a", "a/b", "a" * 256):
            with self.subTest(table=table), self.assertRaises(ValueError):
                DynamoDBBackgroundRequestStore(table, client=FakeDynamoDB())
