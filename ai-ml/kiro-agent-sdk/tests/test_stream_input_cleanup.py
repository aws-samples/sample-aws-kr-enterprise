import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import CLIConnectionError, HookMatcher, KiroAgentOptions, KiroSDKClient, ResultMessage

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class StreamInputCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_accepts_a_stream_of_user_messages(self):
        async def inputs():
            for text in ("first", "second"):
                yield text
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        try:
            await client.connect(inputs())
            results = [message.result async for message in client.receive_messages()
                       if isinstance(message, ResultMessage)]
            self.assertEqual(results, ["first", "second"])
        finally:
            await client.disconnect()

    async def test_uncooperative_input_is_bounded_and_cannot_reach_reconnected_session(self):
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def inputs():
            try:
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                yield "late input"
            finally:
                closed.set()
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)}))
            receiver = None
            try:
                await client.connect()
                await client.query(inputs())
                receiver = asyncio.create_task(client.receive_messages().__anext__())
                await asyncio.wait_for(entered.wait(), 2)
                with patch("kiro_agent_sdk.client._STREAM_INPUT_CLEANUP_TIMEOUT", 0.05):
                    with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                        await asyncio.wait_for(client.disconnect(), 4)
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(receiver, 2)
                self.assertFalse(client._connected)
                with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                    await client.connect()
                release.set()
                await asyncio.wait_for(closed.wait(), 2)
                await asyncio.wait_for(asyncio.shield(client._stream_cleanup_task), 2)
                await client.connect()
                await client.query("fresh")
                self.assertEqual([m async for m in client.receive_response()][-1].result, "fresh")
                requests = [json.loads(line) for line in log.read_text().splitlines()]
                prompts = [item for item in requests if item.get("method") == "session/prompt"]
                self.assertEqual(len(prompts), 1)
                self.assertNotIn("late input", json.dumps(requests))
            finally:
                release.set()
                if receiver is not None and not receiver.done():
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
                await client.disconnect()

    async def test_uncooperative_aclose_is_bounded_and_reconnect_waits(self):
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Source:
            def __aiter__(self):
                return self
            async def __anext__(self):
                entered.set()
                await asyncio.Event().wait()
            async def aclose(self):
                await release.wait()
                closed.set()
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        receiver = None
        try:
            await client.connect()
            await client.query(Source())
            receiver = asyncio.create_task(client.receive_response().__anext__())
            await asyncio.wait_for(entered.wait(), 2)
            with patch("kiro_agent_sdk.client._STREAM_INPUT_CLEANUP_TIMEOUT", 0.05):
                with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                    await asyncio.wait_for(client.disconnect(), 4)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(receiver, 2)
            with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                await client.connect()
            release.set()
            await asyncio.wait_for(closed.wait(), 2)
            await asyncio.wait_for(asyncio.shield(client._stream_cleanup_task), 2)
            await client.connect()
        finally:
            release.set()
            if receiver is not None and not receiver.done():
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            await client.disconnect()

    async def test_failed_aclose_fails_closed_on_reconnect(self):
        class Source:
            def __aiter__(self):
                return self
            async def __anext__(self):
                await asyncio.Event().wait()
            async def aclose(self):
                raise RuntimeError("private cleanup failure")
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        await client.connect()
        await client.query(Source())
        with self.assertRaisesRegex(CLIConnectionError, "cleanup failed") as error:
            await client.disconnect()
        self.assertNotIn("private", str(error.exception))
        with self.assertRaisesRegex(CLIConnectionError, "cleanup failed"):
            await client.connect()

    async def test_synchronous_next_failure_closes_source_and_disconnects(self):
        class Source:
            closed = False
            def __aiter__(self):
                return self
            def __anext__(self):
                raise RuntimeError("input failed")
            async def aclose(self):
                self.closed = True
        source = Source()
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        await client.connect()
        await client.query(source)
        with self.assertRaisesRegex(RuntimeError, "input failed"):
            _ = [message async for message in client.receive_response()]
        self.assertTrue(source.closed)
        self.assertFalse(client._connected)

    async def test_old_prompt_hook_cannot_send_into_reconnected_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def prompt_hook(event, tool_id, context):
            if event["prompt"] == "old":
                entered.set()
                await release.wait()
            return {}
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)},
                                       hooks={"promptSubmit": [HookMatcher(hooks=[prompt_hook])]})
            client = KiroSDKClient(options)
            pending = None
            try:
                await client.connect()
                pending = asyncio.create_task(client.query("old"))
                await asyncio.wait_for(entered.wait(), 2)
                await client.disconnect()
                await client.connect()
                release.set()
                with self.assertRaises(CLIConnectionError):
                    await asyncio.wait_for(pending, 2)
                await client.query("fresh")
                self.assertIsInstance([m async for m in client.receive_response()][-1], ResultMessage)
                requests = [json.loads(line) for line in log.read_text().splitlines()]
                prompts = [item for item in requests if item.get("method") == "session/prompt"]
                self.assertEqual(len(prompts), 1)
                self.assertNotIn('"text": "old"', json.dumps(prompts))
            finally:
                release.set()
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await client.disconnect()

    async def test_old_scheduled_turn_cannot_use_reconnected_session(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)}))
            try:
                await client.connect()
                old_session, old_generation = client.session_id, client._connection_generation
                await client.disconnect()
                await client.connect()
                self.assertEqual(client.session_id, old_session)
                sent = asyncio.Event()
                with self.assertRaisesRegex(CLIConnectionError, "Connection changed"):
                    await client._prompt_request([{"type": "text", "text": "old"}], sent,
                                                 old_session, old_generation)
                self.assertIsNone(client._turn_finished_at)
                await client.query("fresh")
                self.assertEqual([m async for m in client.receive_response()][-1].result, "fresh")
                requests = [json.loads(line) for line in log.read_text().splitlines()]
                prompts = [item for item in requests if item.get("method") == "session/prompt"]
                self.assertEqual(len(prompts), 1)
                self.assertNotIn('"text": "old"', json.dumps(prompts))
            finally:
                await client.disconnect()

    async def test_old_credit_reservation_cannot_run_hooks_in_new_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        hooks, completions = [], []
        class Ledger:
            async def begin(self, scope, limit):
                entered.set()
                await release.wait()
                return "reservation", 0
            async def complete(self, scope, reservation, credits):
                completions.append((scope, reservation, credits))
                return 0
        async def hook(event, tool_id, context):
            hooks.append(event["prompt"])
            return {}
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", max_reported_credits=1,
                reported_credit_ledger=Ledger(), reported_credit_scope="fixture",
                env={"FAKE_LOG_REQUESTS": str(log)},
                hooks={"promptSubmit": [HookMatcher(hooks=[hook])]})
            client = KiroSDKClient(options)
            pending = None
            try:
                await client.connect()
                pending = asyncio.create_task(client.query("old"))
                await asyncio.wait_for(entered.wait(), 2)
                await client.disconnect()
                await client.connect()
                release.set()
                with self.assertRaisesRegex(CLIConnectionError, "preparation was interrupted"):
                    await asyncio.wait_for(pending, 2)
                self.assertEqual(hooks, [])
                self.assertEqual([(scope, reservation) for scope, reservation, _ in completions],
                                 [("fixture", "reservation")])
                requests = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertFalse(any(item.get("method") == "session/prompt" for item in requests))
            finally:
                release.set()
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await client.disconnect()

    async def test_old_mirror_write_cannot_enter_new_session(self):
        class Writer:
            def __init__(self):
                self.entries = []
            async def append(self, entry):
                self.entries.append(entry)
        for replace_writer in (False, True):
            with self.subTest(replace_writer=replace_writer):
                client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
                first, second = Writer(), Writer()
                client._mirror_writer = first
                await client._store_lock.acquire()
                pending = asyncio.create_task(client._mirror("prompt", "private"))
                try:
                    await asyncio.sleep(0)
                    client._connection_generation += 1
                    if replace_writer:
                        client._mirror_writer = second
                    client._store_lock.release()
                    with self.assertRaisesRegex(CLIConnectionError, "mirror changed"):
                        await asyncio.wait_for(pending, 2)
                    self.assertEqual(first.entries, [])
                    self.assertEqual(second.entries, [])
                finally:
                    if client._store_lock.locked():
                        client._store_lock.release()
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

