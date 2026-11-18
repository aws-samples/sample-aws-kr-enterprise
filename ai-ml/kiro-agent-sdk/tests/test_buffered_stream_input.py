import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (AgentDefinition, CLIConnectionError, KiroAgentOptions,
                            KiroSDKClient, ProtocolError, ResultMessage, query)
from test_image_input import ImageTransport
from test_transport import FakeTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


def message(content):
    return content


def prompts(transport):
    return [item["params"]["prompt"] for item in transport.requests if item.get("method") == "session/prompt"]


class BufferedStreamInputTests(unittest.IsolatedAsyncioTestCase):
    def options(self, **values):
        return KiroAgentOptions(**{"agent_engine": "v3", "stream_input_mode": "buffered", **values})

    async def test_consumes_finite_input_and_sends_first_turn_before_returning(self):
        seen = []
        async def source():
            for text in ("first", "second"):
                seen.append(text)
                yield message(text)
        transport = FakeTransport()
        async with KiroSDKClient(self.options(), transport=transport) as client:
            await client.query(source())
            self.assertEqual(seen, ["first", "second"])
            self.assertEqual(prompts(transport), [[{"type": "text", "text": "first"}]])
            results = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
            self.assertEqual(results, ["first", "second"])
        self.assertEqual(len(prompts(transport)), 2)

    async def test_mutable_input_is_snapshotted_before_next_yield(self):
        content = [{"type": "text", "text": "original"}]
        async def source():
            yield message(content)
            content[0]["text"] = "changed"
            yield message(content)
            content[0]["text"] = "later"
        transport = FakeTransport()
        async with KiroSDKClient(self.options(), transport=transport) as client:
            await client.query(source())
            results = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
        self.assertEqual(results, ["original", "changed"])

    async def test_image_input_is_copied_before_sending(self):
        content = [{"type": "text", "text": "describe"}, {"type": "image", "mimeType": "image/png", "data": "AA=="}]
        async def source():
            yield message(content)
            content[1]["data"] = "AQ=="
        transport = ImageTransport()
        async with KiroSDKClient(self.options(), transport=transport) as client:
            await client.query(source())
            _ = [m async for m in client.receive_response()]
        self.assertEqual(prompts(transport)[0][1]["data"], "AA==")

    async def test_empty_stream_remains_a_finite_empty_response(self):
        async def source():
            if False:
                yield message("never")
        transport = FakeTransport()
        async with KiroSDKClient(self.options(), transport=transport) as client:
            await client.query(source())
            self.assertEqual([m async for m in client.receive_messages()], [])
        self.assertEqual(prompts(transport), [])

    async def test_one_shot_query_and_connect_prompt_preserve_turn_order(self):
        async def source():
            for text in ("one", "two"):
                yield message(text)
        options = self.options(cli_path=FAKE)
        output = [m.result async for m in query(prompt=source(), options=options) if isinstance(m, ResultMessage)]
        self.assertEqual(output, ["one", "two"])
        client = KiroSDKClient(options)
        try:
            await client.connect(prompt=source())
            output = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
            self.assertEqual(output, ["one", "two"])
        finally:
            await client.disconnect()

    async def test_limits_and_bad_messages_fail_before_sending(self):
        cycle = {"type": "text", "text": "one"}
        cycle["cycle"] = cycle
        shared = {"value": "same"}
        variants = [
            ({"stream_input_buffer_max_messages": 1}, [message("one"), message("two")]),
            ({"stream_input_buffer_max_bytes": 4}, [message("long")]),
            ({"stream_input_buffer_max_bytes": 4}, [message("한")]),
            ({}, [message("one"), {"prompt": "two"}]),
            ({}, [message("one"), 2]),
            ({}, [message("one"), message([{"type": "text", "text": object()}])]),
            ({}, [message("one"), message("\ud800")]),
            ({}, [message("one"), message([cycle])]),
            ({}, [message("one"), message([{"type": "text", "text": "x", "a": shared, "b": shared}])]),
        ]
        for options, items in variants:
            with self.subTest(options=options, item=items[-1]):
                async def source():
                    for item in items:
                        yield item
                transport = FakeTransport()
                client = KiroSDKClient(self.options(**options), transport=transport)
                await client.connect()
                try:
                    with self.assertRaises(ValueError):
                        await client.query(source())
                    self.assertEqual(prompts(transport), [])
                    self.assertFalse(client._connected)
                finally:
                    await client.disconnect()

    async def test_count_limit_may_consume_one_additional_item(self):
        seen = []
        async def source():
            for text in ("one", "two", "three"):
                seen.append(text)
                yield message(text)
        transport = FakeTransport()
        client = KiroSDKClient(self.options(stream_input_buffer_max_messages=1), transport=transport)
        await client.connect()
        with self.assertRaisesRegex(ValueError, "message limit"):
            await client.query(source())
        self.assertEqual(seen, ["one", "two"])
        self.assertEqual(prompts(transport), [])

    async def test_finite_receiver_rejects_input_while_it_is_being_buffered(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def source():
            entered.set()
            await release.wait()
            yield message("one")
        client = KiroSDKClient(self.options(cli_path=FAKE))
        try:
            await client.connect()
            task = asyncio.create_task(client.query(source()))
            await asyncio.wait_for(entered.wait(), 2)
            for receiver in (client.receive_response(), client.receive_messages()):
                with self.assertRaisesRegex(CLIConnectionError, "still being buffered"):
                    await receiver.__anext__()
                await receiver.aclose()
            self.assertTrue(client._connected)
            release.set()
            await asyncio.wait_for(task, 2)
            self.assertEqual([m async for m in client.receive_response()][-1].result, "one")
        finally:
            release.set()
            await client.disconnect()

    async def test_timed_out_input_is_cancelled_without_sending(self):
        closed = asyncio.Event()
        async def source():
            try:
                await asyncio.Event().wait()
                yield message("never")
            finally:
                closed.set()
        transport = FakeTransport()
        client = KiroSDKClient(self.options(stream_input_buffer_timeout=0.03), transport=transport)
        await client.connect()
        with self.assertRaisesRegex(TimeoutError, "buffering timed out"):
            await client.query(source())
        self.assertTrue(closed.is_set())
        self.assertFalse(client._connected)
        self.assertEqual(prompts(transport), [])

    async def test_later_capability_failure_does_not_undo_first_turn(self):
        async def source():
            yield message("first")
            yield message([{"type": "text", "text": "describe"},
                           {"type": "image", "mimeType": "image/png", "data": "AA=="}])
        transport = FakeTransport()
        client = KiroSDKClient(self.options(), transport=transport)
        await client.connect()
        await client.query(source())
        self.assertEqual([m async for m in client.receive_response()][-1].result, "first")
        with self.assertRaisesRegex(ProtocolError, "image prompt support"):
            _ = [m async for m in client.receive_response()]
        self.assertEqual(len(prompts(transport)), 1)
        self.assertFalse(client._connected)

    async def test_concurrent_receiver_waits_until_buffering_is_finished(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def source():
            yield message("one")
            entered.set()
            await release.wait()
            yield message("two")
        transport = FakeTransport()
        options = self.options(receive_messages_mode="continuous")
        async with KiroSDKClient(options, transport=transport) as client:
            listener = client.receive_messages()
            waiting = asyncio.create_task(listener.__anext__())
            await asyncio.sleep(0)
            submit = asyncio.create_task(client.query(source()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertFalse(waiting.done())
                self.assertEqual(prompts(transport), [])
                with self.assertRaisesRegex(CLIConnectionError, "receiver is active"):
                    await client.receive_response().__anext__()
                release.set()
                await asyncio.wait_for(submit, 2)
                received = [await asyncio.wait_for(waiting, 2)]
                while len([m for m in received if isinstance(m, ResultMessage)]) < 2:
                    received.append(await asyncio.wait_for(listener.__anext__(), 2))
                self.assertEqual([m.result for m in received if isinstance(m, ResultMessage)], ["one", "two"])
            finally:
                release.set()
                if not submit.done():
                    submit.cancel()
                    await asyncio.gather(submit, return_exceptions=True)
                await listener.aclose()

    async def test_disconnect_bounds_uncooperative_source_and_prevents_late_input(self):
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
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            client = KiroSDKClient(self.options(cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)}))
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
                await client.connect()
                await client.query("fresh")
                self.assertEqual([m async for m in client.receive_response()][-1].result, "fresh")
                requests = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertNotIn("late", json.dumps(requests))
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await client.disconnect()

    async def test_cancelling_buffered_query_closes_source_and_allows_reconnect(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def source():
            try:
                entered.set()
                await asyncio.Event().wait()
                yield message("never")
            finally:
                closed.set()
        client = KiroSDKClient(self.options(cli_path=FAKE))
        try:
            await client.connect()
            task = asyncio.create_task(client.query(source()))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())
            self.assertFalse(client._connected)
            await client.connect()
            await client.query("fresh")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "fresh")
        finally:
            await client.disconnect()

    async def test_mutated_policy_is_rejected_before_sending(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def source():
            entered.set()
            await release.wait()
            yield message("secret")
        options = self.options()
        transport = FakeTransport()
        client = KiroSDKClient(options, transport=transport)
        await client.connect()
        pending = asyncio.create_task(client.query(source()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            options.stream_input_buffer_max_messages = 1
            release.set()
            with self.assertRaisesRegex(ValueError, "streaming input settings"):
                await pending
            self.assertEqual(prompts(transport), [])
        finally:
            release.set()
            await client.disconnect()

    async def test_initial_agent_prompt_precedes_buffered_input(self):
        async def source():
            yield message("next")
            yield message("last")
        with tempfile.TemporaryDirectory() as root:
            options = self.options(cwd=root, cli_path=FAKE, agent="selected",
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")})
            async with KiroSDKClient(options) as client:
                await client.query(source())
                values = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
                self.assertEqual(values, ["initial", "next", "last"])
            output = [m.result async for m in query(prompt=source(), options=options) if isinstance(m, ResultMessage)]
            self.assertEqual(output, ["initial", "next", "last"])

    async def test_cancelled_initial_turn_never_sends_buffered_input(self):
        async def source():
            yield message("must-not-run")
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = self.options(cwd=root, cli_path=FAKE, agent="selected",
                env={"FAKE_LOG_REQUESTS": str(log)},
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="cancel")})
            client = KiroSDKClient(options)
            await client.connect()
            await client.query(source())
            await client.interrupt()
            with self.assertRaisesRegex(CLIConnectionError, "queued prompts were not sent"):
                _ = [m async for m in client.receive_messages()]
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            sent = [item["params"]["prompt"][0]["text"] for item in requests
                    if item.get("method") == "session/prompt"]
            self.assertEqual(sent, ["cancel"])

    def test_invalid_policy_is_rejected(self):
        for values in ({"stream_input_mode": "other"}, {"stream_input_buffer_max_messages": True},
                       {"stream_input_buffer_max_messages": 0}, {"stream_input_buffer_max_messages": 1025},
                       {"stream_input_buffer_max_bytes": 0}, {"stream_input_buffer_max_bytes": 64 * 1024 * 1024 + 1},
                       {"stream_input_buffer_timeout": 0}, {"stream_input_buffer_timeout": float("inf")},
                       {"stream_input_buffer_timeout": True}, {"stream_input_buffer_timeout": 10**1000}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                KiroSDKClient(self.options(**values))


if __name__ == "__main__":
    unittest.main()
