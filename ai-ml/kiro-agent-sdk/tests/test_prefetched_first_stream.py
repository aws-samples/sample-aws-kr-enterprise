import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (AgentDefinition, CLIConnectionError,
                            HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError, ResultMessage, query)
from test_transport import FakeTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


def message(content):
    return content


def prompts(transport):
    return [r["params"]["prompt"] for r in transport.requests if r.get("method") == "session/prompt"]


class PrefetchedFirstStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_prompt_is_sent_before_return_and_later_input_can_wait_for_result(self):
        first_done = asyncio.Event()

        async def source():
            yield message("first")
            await first_done.wait()
            yield message("second")

        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch-first"),
                                 transport=transport) as client:
            await asyncio.wait_for(client.query(source()), 2)
            self.assertEqual(prompts(transport), [[{"type": "text", "text": "first"}]])
            results = []
            async for event in client.receive_messages():
                if isinstance(event, ResultMessage):
                    results.append(event.result)
                    first_done.set()
            self.assertEqual(results, ["first", "second"])

    async def test_first_message_timeout_closes_source_and_sends_nothing(self):
        closed = asyncio.Event()

        async def source():
            try:
                await asyncio.Event().wait()
                yield message("never")
            finally:
                closed.set()

        transport = FakeTransport()
        client = KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch-first",
                                                stream_input_buffer_timeout=0.03), transport=transport)
        await client.connect()
        with self.assertRaisesRegex(TimeoutError, "first message timed out"):
            await client.query(source())
        self.assertTrue(closed.is_set())
        self.assertFalse(client._connected)
        self.assertEqual(prompts(transport), [])

    async def test_bad_first_message_is_rejected_without_sending(self):
        for item in ({"prompt": "bad"}, message("long text"),
                     message([{"type": "text", "text": object()}])):
            with self.subTest(item=item):
                async def source():
                    yield item
                transport = FakeTransport()
                client = KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch-first",
                                                        stream_input_buffer_max_bytes=8), transport=transport)
                await client.connect()
                with self.assertRaises(ValueError):
                    await client.query(source())
                self.assertEqual(prompts(transport), [])
                self.assertFalse(client._connected)

    async def test_empty_stream_and_one_shot_work(self):
        async def empty():
            if False:
                yield message("never")

        async with KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch-first"),
                                 transport=FakeTransport()) as client:
            await client.query(empty())
            self.assertEqual([event async for event in client.receive_messages()], [])

        async def source():
            yield message("one")
            yield message("two")

        options = KiroAgentOptions(cli_path=FAKE, stream_input_mode="prefetch-first")
        results = [event.result async for event in query(prompt=source(), options=options)
                   if isinstance(event, ResultMessage)]
        self.assertEqual(results, ["one", "two"])

    async def test_initial_agent_turn_runs_before_first_stream_message(self):
        async def source():
            yield message("next")

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                stream_input_mode="prefetch-first", agent="selected",
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")})
            async with KiroSDKClient(options) as client:
                await client.query(source())
                results = [event.result async for event in client.receive_messages()
                           if isinstance(event, ResultMessage)]
                self.assertEqual(results, ["initial", "next"])

    async def test_initial_turn_can_finish_before_source_produces_first_message(self):
        initial_done = asyncio.Event()

        async def source():
            await initial_done.wait()
            yield message("next")

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                stream_input_mode="prefetch-first", agent="selected",
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")})
            async with KiroSDKClient(options) as client:
                await asyncio.wait_for(client.query(source()), 2)
                results = []
                async for event in client.receive_messages():
                    if isinstance(event, ResultMessage):
                        results.append(event.result)
                        initial_done.set()
                self.assertEqual(results, ["initial", "next"])

    async def test_cancelled_initial_turn_never_sends_prefetched_prompt(self):
        async def source():
            yield message("must-not-run")

        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                stream_input_mode="prefetch-first", agent="selected",
                env={"FAKE_LOG_REQUESTS": str(log)},
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="cancel")})
            client = KiroSDKClient(options)
            await client.connect()
            await client.query(source())
            await client.interrupt()
            with self.assertRaisesRegex(CLIConnectionError, "queued prompts were not sent"):
                _ = [event async for event in client.receive_messages()]
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            sent = [item["params"]["prompt"][0]["text"] for item in requests
                    if item.get("method") == "session/prompt"]
            self.assertEqual(sent, ["cancel"])

    async def test_continuous_receiver_waits_for_first_message_and_turn(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def source():
            entered.set()
            await release.wait()
            yield message("first")

        options = KiroAgentOptions(stream_input_mode="prefetch-first", receive_messages_mode="continuous")
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            listener = client.receive_messages()
            receiver = asyncio.create_task(listener.__anext__())
            submit = asyncio.create_task(client.query(source()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertFalse(receiver.done())
                release.set()
                await asyncio.wait_for(submit, 2)
                first = await asyncio.wait_for(receiver, 2)
                events = [first]
                while not any(isinstance(event, ResultMessage) for event in events):
                    events.append(await asyncio.wait_for(listener.__anext__(), 2))
                self.assertEqual(next(event.result for event in events if isinstance(event, ResultMessage)), "first")
            finally:
                release.set()
                if not submit.done():
                    submit.cancel()
                    await asyncio.gather(submit, return_exceptions=True)
                if not receiver.done():
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
                await listener.aclose()

    async def test_guard_revoked_during_first_prompt_hook_prevents_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        permitted = True

        async def hook(event, tool_id, context):
            entered.set()
            await release.wait()
            return {}

        async def guard():
            return permitted

        async def source():
            yield message("private prompt")

        options = KiroAgentOptions(stream_input_mode="prefetch-first",
                                   hooks={"promptSubmit": [HookMatcher(hooks=[hook])]})
        transport = FakeTransport()
        client = KiroSDKClient(options, transport=transport)
        await client.connect()
        client._prompt_send_guard = guard
        submit = asyncio.create_task(client.query(source()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            permitted = False
            release.set()
            with self.assertRaises(ProtocolError):
                await asyncio.wait_for(submit, 2)
            self.assertEqual(prompts(transport), [])
        finally:
            release.set()
            if not submit.done():
                submit.cancel()
                await asyncio.gather(submit, return_exceptions=True)
            await client.disconnect()

    async def test_mutation_during_first_input_cannot_send_prompt(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def source():
            entered.set()
            await release.wait()
            yield message("must-not-send")

        options = KiroAgentOptions(stream_input_mode="prefetch-first")
        transport = FakeTransport()
        client = KiroSDKClient(options, transport=transport)
        await client.connect()
        task = asyncio.create_task(client.query(source()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            options.stream_input_mode = "prefetch"
            release.set()
            with self.assertRaisesRegex(ValueError, "streaming input settings"):
                await asyncio.wait_for(task, 2)
            self.assertEqual(prompts(transport), [])
        finally:
            release.set()
            await client.disconnect()

    async def test_disconnect_bounds_uncooperative_first_source_and_blocks_late_send(self):
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def source():
            try:
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                yield message("late")
            finally:
                closed.set()

        transport = FakeTransport()
        client = KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch-first"),
                               transport=transport)
        task = None
        try:
            await client.connect()
            task = asyncio.create_task(client.query(source()))
            await asyncio.wait_for(entered.wait(), 2)
            with patch("kiro_agent_sdk.client._STREAM_INPUT_CLEANUP_TIMEOUT", 0.05):
                with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                    await client.disconnect()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                await client.connect()
            release.set()
            await asyncio.wait_for(closed.wait(), 2)
            await asyncio.wait_for(asyncio.shield(client._stream_cleanup_task), 2)
            self.assertEqual(prompts(transport), [])
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.disconnect()
