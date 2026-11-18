import asyncio
import copy
import tempfile
import threading
import unittest
from pathlib import Path

from kiro_agent_sdk import DynamoDBCreditLedger, KiroAgentOptions, KiroSDKClient, ReportedCreditLimitError


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class MemoryDynamo:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.fail_after_write = False

    def get_item(self, *, TableName, Key, ConsistentRead):
        assert ConsistentRead is True
        with self.lock:
            item = self.items.get(Key["scope"]["S"])
            return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, *, TableName, Item, ConditionExpression, ExpressionAttributeNames,
                 ExpressionAttributeValues=None):
        scope = Item["scope"]["S"]
        with self.lock:
            current = self.items.get(scope)
            if ConditionExpression == "attribute_not_exists(#scope)":
                assert ExpressionAttributeNames == {"#scope": "scope"}
                if current is not None:
                    raise ConditionalFailure()
            else:
                assert ConditionExpression == "#revision = :revision"
                assert ExpressionAttributeNames == {"#revision": "revision"}
                if current is None or current["revision"] != ExpressionAttributeValues[":revision"]:
                    raise ConditionalFailure()
            self.items[scope] = copy.deepcopy(Item)
            if self.fail_after_write:
                self.fail_after_write = False
                raise TimeoutError("response lost after write")
        return {}


class DynamoDBCreditLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MemoryDynamo()
        self.ledger = DynamoDBCreditLedger("kiro-credits", client=self.client)

    async def test_concurrent_reservation_allows_only_one_turn(self):
        async def reserve():
            try:
                return await self.ledger.begin("team", "1")
            except ReportedCreditLimitError:
                return None
        outcomes = await asyncio.gather(*(reserve() for _ in range(20)))
        accepted = [item for item in outcomes if item is not None]
        self.assertEqual(len(accepted), 1)
        await self.ledger.complete("team", accepted[0][0], "0.2")
        status = await self.ledger.inspect("team")
        self.assertEqual((status["state"], status["reported_credits"]), ("ready", "0.2"))

    async def test_unknown_usage_reconciliation_and_limit_changes(self):
        reservation, _ = await self.ledger.begin("team", "1")
        self.assertEqual(str(await self.ledger.complete("team", reservation, None)), "0")
        self.assertEqual((await self.ledger.inspect("team"))["state"], "unknown")
        with self.assertRaises(ReportedCreditLimitError):
            await self.ledger.begin("team", "1")
        with self.assertRaises(ValueError):
            await self.ledger.reconcile("team", "0" * 32, reported_credits="0.1")
        await self.ledger.reconcile("team", reservation, reported_credits="0.2")
        await self.ledger.set_limit("team", expected_limit="1", new_limit="2")
        with self.assertRaises(ValueError):
            await self.ledger.set_limit("team", expected_limit="1", new_limit="3")
        reservation, total = await self.ledger.begin("team", "2")
        self.assertEqual(str(total), "0.2")
        with self.assertRaisesRegex(ValueError, "cannot decrease"):
            await self.ledger.reconcile("team", reservation, reported_credits="0.1")
        await self.ledger.complete("team", reservation, "0.1")
        self.assertEqual((await self.ledger.inspect("team"))["reported_credits"], "0.3")

    async def test_uncertain_writes_cannot_duplicate_usage_or_start_another_turn(self):
        self.client.fail_after_write = True
        with self.assertRaises(TimeoutError):
            await self.ledger.begin("team", "1")
        status = await self.ledger.inspect("team")
        self.assertEqual(status["state"], "pending")
        with self.assertRaises(ReportedCreditLimitError):
            await self.ledger.begin("team", "1")
        await self.ledger.reconcile("team", status["reservation_id"], reported_credits="0")
        reservation, _ = await self.ledger.begin("team", "1")
        self.client.fail_after_write = True
        with self.assertRaises(TimeoutError):
            await self.ledger.complete("team", reservation, "0.2")
        status = await self.ledger.inspect("team")
        self.assertEqual((status["state"], status["reported_credits"]), ("ready", "0.2"))
        with self.assertRaises(ValueError):
            await self.ledger.complete("team", reservation, "0.2")
        self.assertEqual((await self.ledger.inspect("team"))["reported_credits"], "0.2")

    async def test_sdk_clients_share_reported_credit_limit(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                env={"FAKE_DUPLICATE_USAGE": "1"}, max_reported_credits=0.15,
                reported_credit_ledger=self.ledger, reported_credit_scope="team")
            for _ in range(2):
                async with KiroSDKClient(options) as client:
                    await client.query("hello")
                    _ = [item async for item in client.receive_response()]
            async with KiroSDKClient(options) as client:
                with self.assertRaises(ReportedCreditLimitError):
                    await client.query("must-not-run")
            self.assertEqual((await self.ledger.inspect("team"))["reported_credits"], "0.2")

    async def test_malformed_state_and_unexpected_errors_are_not_accepted(self):
        self.client.items["team"] = {"scope": {"S": "team"}, "state": {"S": "ready"}}
        with self.assertRaises(ValueError):
            await self.ledger.begin("team", "1")
        with self.assertRaises(ValueError):
            DynamoDBCreditLedger("a", client=self.client)
        with self.assertRaises(ValueError):
            DynamoDBCreditLedger("kiro-credits", client=self.client, max_conflicts=0)
