import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (HookMatcher, InMemorySessionStore, KiroAgentOptions, KiroSDKClient,
                            ProtocolError, ReportedCreditLimitError, SQLiteCreditLedger, project_key_for_directory)

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class NestedCreditLedgerTests(unittest.IsolatedAsyncioTestCase):
    pass


class CreditLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "credits.db"
        self.ledger = SQLiteCreditLedger(self.path)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def options(self, **values):
        return KiroAgentOptions(cli_path=FAKE, agent_engine="v3", cwd=self.root, max_reported_credits=0.15,
                                reported_credit_ledger=self.ledger, reported_credit_scope="team-1", **values)

    async def test_limit_survives_new_clients_and_duplicate_usage(self):
        log = self.root / "requests.jsonl"
        options = self.options(env={"FAKE_DUPLICATE_USAGE": "1", "FAKE_LOG_REQUESTS": str(log)})
        for expected in (0.1, 0.2):
            async with KiroSDKClient(options) as client:
                await client.query("hello")
                _ = [message async for message in client.receive_response()]
                self.assertEqual(client.reported_credits, expected)
        async with KiroSDKClient(options) as client:
            with self.assertRaises(ReportedCreditLimitError) as error:
                await client.query("must-not-run")
            self.assertEqual(error.exception.reported_credits, 0.2)
            self.assertEqual(client.reported_credits, 0.2)
        requests = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum(item.get("method") == "session/prompt" for item in requests), 2)
        state = await SQLiteCreditLedger(self.path).inspect("team-1")
        self.assertEqual((state["state"], state["reported_credits"]), ("ready", "0.2"))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    async def test_unknown_usage_blocks_until_explicit_reconciliation(self):
        async with KiroSDKClient(self.options(env={"FAKE_NO_USAGE": "1"})) as client:
            await client.query("hello")
            _ = [message async for message in client.receive_response()]
        state = await self.ledger.inspect("team-1")
        self.assertEqual(state["state"], "unknown")
        async with KiroSDKClient(self.options()) as client:
            with self.assertRaises(ReportedCreditLimitError):
                await client.query("must-not-run")
        with self.assertRaises(ValueError):
            await self.ledger.reconcile("team-1", "wrong", reported_credits="0.1")
        await self.ledger.reconcile("team-1", state["reservation_id"], reported_credits="0.1")
        async with KiroSDKClient(self.options()) as client:
            await client.query("after reconciliation")
            _ = [message async for message in client.receive_response()]
            self.assertEqual(client.reported_credits, 0.2)

    async def test_parallel_and_abandoned_turns_fail_closed(self):
        first = KiroSDKClient(self.options())
        second = KiroSDKClient(self.options())
        await first.connect()
        await second.connect()
        try:
            await first.query("cancel")
            with self.assertRaises(ReportedCreditLimitError):
                await second.query("must-not-run")
            state = await self.ledger.inspect("team-1")
            self.assertEqual(state["state"], "pending")
            await first.disconnect()
            with self.assertRaises(ReportedCreditLimitError):
                await second.query("still-blocked")
            await self.ledger.reconcile("team-1", state["reservation_id"], reported_credits="0.1")
            await second.query("after reconciliation")
            _ = [message async for message in second.receive_response()]
            self.assertEqual(second.reported_credits, 0.2)
        finally:
            await first.disconnect()
            await second.disconnect()

    async def test_scope_limit_mismatch_and_mutation_are_rejected(self):
        opts = self.options()
        async with KiroSDKClient(opts) as client:
            await client.query("hello")
            _ = [message async for message in client.receive_response()]
        opts.max_reported_credits = 1
        async with KiroSDKClient(opts) as client:
            with self.assertRaisesRegex(ValueError, "different limit"):
                await client.query("must-not-run")
            opts.reported_credit_scope = "other"
            with self.assertRaisesRegex(ValueError, "new client"):
                await client.query("must-not-run")
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine="v3", reported_credit_ledger=self.ledger,
                                           reported_credit_scope="team-1"))

    async def test_reconciliation_never_decreases_known_usage(self):
        reservation, total = await self.ledger.begin("team-1", "1")
        await self.ledger.complete("team-1", reservation, "0.2")
        reservation, total = await self.ledger.begin("team-1", "1")
        with self.assertRaisesRegex(ValueError, "cannot decrease"):
            await self.ledger.reconcile("team-1", reservation, reported_credits="0.1")
        self.assertEqual((await self.ledger.inspect("team-1"))["state"], "pending")

    async def test_limit_change_is_atomic_and_requires_settled_scope(self):
        reservation, _ = await self.ledger.begin("team-1", "1")
        with self.assertRaises(ValueError):
            await self.ledger.set_limit("team-1", expected_limit="1", new_limit="2")
        await self.ledger.complete("team-1", reservation, "0.2")
        with self.assertRaises(ValueError):
            await self.ledger.set_limit("team-1", expected_limit="3", new_limit="2")
        await self.ledger.set_limit("team-1", expected_limit="1", new_limit="2")
        reservation, total = await self.ledger.begin("team-1", "2")
        self.assertEqual(str(total), "0.2")
        await self.ledger.complete("team-1", reservation, "0.1")

    async def test_parallel_reservations_allow_only_one_turn(self):
        async def attempt():
            try:
                return await SQLiteCreditLedger(self.path).begin("shared", "1")
            except ReportedCreditLimitError:
                return None
        results = await asyncio.gather(*(attempt() for _ in range(12)))
        accepted = [result for result in results if result is not None]
        self.assertEqual(len(accepted), 1)
        await self.ledger.complete("shared", accepted[0][0], "0.1")

    async def test_rejected_prompt_is_not_written_to_session_mirror(self):
        store = InMemorySessionStore()
        options = self.options(session_store=store, session_store_flush="eager")
        async with KiroSDKClient(options) as client:
            await client.query("first")
            _ = [message async for message in client.receive_response()]
            await client.query("second")
            _ = [message async for message in client.receive_response()]
        async with KiroSDKClient(options) as client:
            with self.assertRaises(ReportedCreditLimitError):
                await client.query("must-not-be-mirrored")
        entries = await store.load({"project_key": project_key_for_directory(self.root), "session_id": "test-session"})
        prompts = [entry["data"] for entry in entries if entry.get("type") == "prompt"]
        self.assertEqual([prompt[0]["text"] for prompt in prompts], ["first", "second"])

    async def test_guard_runs_before_hooks_and_preflight_failures_release_reservation(self):
        seen = []
        async def deny(data, tool_id, context):
            seen.append(data["prompt"])
            return {'block': "test"}
        guarded = self.options(hooks={"promptSubmit": [HookMatcher(hooks=[deny])]})
        async with KiroSDKClient(guarded) as client:
            with self.assertRaises(ProtocolError):
                await client.query("blocked by hook")
        state = await self.ledger.inspect("team-1")
        self.assertEqual((state["state"], state["reported_credits"]), ("ready", "0"))
        async with KiroSDKClient(self.options()) as client:
            for _ in range(2):
                await client.query("ok")
                _ = [message async for message in client.receive_response()]
        async with KiroSDKClient(guarded) as client:
            with self.assertRaises(ReportedCreditLimitError):
                await client.query("over budget")
        self.assertEqual(seen, ["blocked by hook"])

    async def test_cancelled_preflight_releases_its_reservation(self):
        entered = asyncio.Event()
        async def wait_in_hook(data, tool_id, context):
            entered.set()
            await asyncio.Event().wait()
        options = self.options(hooks={"promptSubmit": [HookMatcher(hooks=[wait_in_hook])]})
        async with KiroSDKClient(options) as client:
            task = asyncio.create_task(client.query("cancel before send"))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        state = await self.ledger.inspect("team-1")
        self.assertEqual((state["state"], state["reported_credits"]), ("ready", "0"))


if __name__ == "__main__":
    unittest.main()
