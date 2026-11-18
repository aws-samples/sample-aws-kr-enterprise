import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path

from kiro_agent_sdk import DynamoDBSessionOwnership, SQLiteSessionOwnership


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class MemoryDynamo:
    """Simulate DynamoDB's conditional writes and strongly consistent reads."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def put_item(self, *, TableName, Item, ConditionExpression, ExpressionAttributeNames,
                 ExpressionAttributeValues):
        assert ConditionExpression == "attribute_not_exists(#session) OR #owner = :owner"
        assert ExpressionAttributeNames == {"#session": "session_id", "#owner": "owner"}
        session, owner = Item["session_id"]["S"], Item["owner"]["S"]
        assert ExpressionAttributeValues == {":owner": {"S": owner}}
        with self.lock:
            if session in self.items and self.items[session] != owner:
                raise ConditionalFailure()
            self.items[session] = owner
        return {}

    def get_item(self, *, TableName, Key, ConsistentRead):
        assert ConsistentRead is True
        with self.lock:
            owner = self.items.get(Key["session_id"]["S"])
        return {"Item": {"owner": {"S": owner}}} if owner is not None else {}

    def delete_item(self, *, TableName, Key, ConditionExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues):
        assert ConditionExpression == "#owner = :owner"
        assert ExpressionAttributeNames == {"#owner": "owner"}
        session, owner = Key["session_id"]["S"], ExpressionAttributeValues[":owner"]["S"]
        with self.lock:
            if self.items.get(session) != owner:
                raise ConditionalFailure()
            del self.items[session]
        return {}


class SessionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "owners.sqlite"
        self.store = SQLiteSessionOwnership(self.path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_registration_is_required_and_revoke_is_owner_scoped(self):
        self.assertFalse(await self.store.is_owner("session-a", "alice"))
        self.assertTrue(await self.store.register("session-a", "alice"))
        self.assertTrue(await self.store.register("session-a", "alice"))
        self.assertFalse(await self.store.register("session-a", "bob"))
        self.assertTrue(await self.store.is_owner("session-a", "alice"))
        self.assertFalse(await self.store.is_owner("session-a", "bob"))
        self.assertFalse(await self.store.revoke("session-a", "bob"))
        self.assertTrue(await self.store.revoke("session-a", "alice"))
        self.assertFalse(await self.store.is_owner("session-a", "alice"))
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_concurrent_competing_registrations_choose_one_owner(self):
        stores = [SQLiteSessionOwnership(self.path) for _ in range(12)]
        results = await asyncio.gather(*(store.register("session-b", f"owner-{index}")
                                         for index, store in enumerate(stores)))
        self.assertEqual(sum(results), 1)
        winner = results.index(True)
        self.assertTrue(await self.store.is_owner("session-b", f"owner-{winner}"))
        self.assertFalse(await self.store.is_owner("session-b", f"owner-{(winner + 1) % len(stores)}"))

    async def test_rejects_invalid_ids_owners_and_unprotected_stores(self):
        for session_id, owner in (("../session", "alice"), ("", "alice"), ("valid", ""),
                                  ("valid", "x\0y"), ("valid", "x" * 1025)):
            with self.subTest(session_id=session_id, owner=owner):
                with self.assertRaises(ValueError):
                    await self.store.register(session_id, owner)
        with self.assertRaises(ValueError):
            SQLiteSessionOwnership(":memory:")
        if os.name == "posix":
            self.root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                await self.store.is_owner("valid", "alice")
            self.root.chmod(0o700)
            victim = self.root / "victim"
            victim.write_text("unchanged")
            self.path.symlink_to(victim)
            with self.assertRaisesRegex(ValueError, "private"):
                await self.store.is_owner("valid", "alice")
            self.assertEqual(victim.read_text(), "unchanged")


class DynamoDBSessionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_concurrency_owner_checks_and_revocation(self):
        client = MemoryDynamo()
        stores = [DynamoDBSessionOwnership("test-owners", client=client) for _ in range(12)]
        self.assertFalse(await stores[0].is_owner("session-c", "owner-0"))
        results = await asyncio.gather(*(store.register("session-c", f"owner-{index}")
                                         for index, store in enumerate(stores)))
        self.assertEqual(sum(results), 1)
        winner = results.index(True)
        self.assertTrue(await stores[0].is_owner("session-c", f"owner-{winner}"))
        self.assertTrue(await stores[0].register("session-c", f"owner-{winner}"))
        self.assertFalse(await stores[0].revoke("session-c", "wrong"))
        self.assertTrue(await stores[0].revoke("session-c", f"owner-{winner}"))
        self.assertFalse(await stores[0].is_owner("session-c", f"owner-{winner}"))

    async def test_invalid_items_and_other_errors_are_not_silently_allowed(self):
        class Broken:
            def get_item(self, **kwargs):
                return {"Item": {"owner": {"N": "42"}}}
            def put_item(self, **kwargs):
                raise RuntimeError("unavailable")
        store = DynamoDBSessionOwnership("test-owners", client=Broken())
        with self.assertRaises(ValueError):
            await store.is_owner("session-c", "alice")
        with self.assertRaises(RuntimeError):
            await store.register("session-c", "alice")
        with self.assertRaises(ValueError):
            DynamoDBSessionOwnership("a", client=Broken())
        with self.assertRaises(ValueError):
            DynamoDBSessionOwnership("test-owners", client=Broken(), region_name="test")
