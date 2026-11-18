import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import AgentDefinition, CLIConnectionError, KiroAgentOptions, KiroSDKClient, ResultMessage, query
from kiro_agent_sdk.stream_prefetch import PrefetchedInput
from test_transport import FakeTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


def message(content):
    return content


def prompts(transport):
    return [r["params"]["prompt"] for r in transport.requests if r.get("method") == "session/prompt"]


class PrefetchedStreamInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefetches_before_reception_without_sending_and_snapshots_content(self):
        prepared, finish = asyncio.Event(), asyncio.Event()
        content = [{"type": "text", "text": "original"}]

        async def source():
            yield message(content)
            content[0]["text"] = "changed"
            yield message(content)
            prepared.set()
            await finish.wait()

        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch"), transport=transport) as client:
            await client.query(source())
            await asyncio.wait_for(prepared.wait(), 2)
            self.assertEqual(prompts(transport), [])
            finish.set()
            results = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
            self.assertEqual(results, ["original", "changed"])

    async def test_accepts_live_stream_waiting_for_previous_response(self):
        response_seen = asyncio.Event()

        async def source():
            yield message("first")
            await response_seen.wait()
            yield message("second")

        async with KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch"),
                                 transport=FakeTransport()) as client:
            await client.query(source())
            results = []
            async for event in client.receive_messages():
                if isinstance(event, ResultMessage):
                    results.append(event.result)
                    response_seen.set()
            self.assertEqual(results, ["first", "second"])

    async def test_one_shot_and_initial_agent_turn_keep_order(self):
        async def source():
            yield message("next")
            yield message("last")

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                env={"KIRO_HOME": str(Path(root) / "kiro")},
                stream_input_mode="prefetch", agent="selected",
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")})
            async with KiroSDKClient(options) as client:
                await client.query(source())
                results = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
                self.assertEqual(results, ["initial", "next", "last"])
            results = [m.result async for m in query(prompt=source(), options=options) if isinstance(m, ResultMessage)]
            self.assertEqual(results, ["initial", "next", "last"])

    async def test_count_and_byte_backpressure_do_not_drop_or_reorder(self):
        seen = []
        two_consumed = asyncio.Event()

        async def source():
            for value in ("one", "two", "three"):
                seen.append(value)
                if len(seen) == 2:
                    two_consumed.set()
                yield message(value)

        options = KiroAgentOptions(stream_input_mode="prefetch", stream_input_buffer_max_messages=2,
                                   stream_input_buffer_max_bytes=7)
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            await client.query(source())
            await asyncio.wait_for(two_consumed.wait(), 2)
            await asyncio.sleep(0.01)
            # The second item is snapshotted but cannot fit alongside the first.
            self.assertEqual(seen, ["one", "two"])
            results = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
            self.assertEqual(results, ["one", "two", "three"])

    async def test_count_backpressure_stops_reading_until_queue_is_drained(self):
        seen = []
        started = asyncio.Event()

        async def source():
            for value in ("one", "two"):
                seen.append(value)
                started.set()
                yield message(value)

        options = KiroAgentOptions(stream_input_mode="prefetch", stream_input_buffer_max_messages=1)
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            await client.query(source())
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.sleep(0.01)
            self.assertEqual(seen, ["one"])
            results = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
            self.assertEqual(results, ["one", "two"])

    async def test_invalid_later_input_fails_after_prior_turn_and_disconnects(self):
        async def source():
            yield message("first")
            yield {"prompt": "bad"}

        transport = FakeTransport()
        client = KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch"), transport=transport)
        await client.connect()
        await client.query(source())
        results = []
        with self.assertRaisesRegex(ValueError, "prompt strings"):
            async for event in client.receive_messages():
                if isinstance(event, ResultMessage):
                    results.append(event.result)
        self.assertEqual(results, ["first"])
        self.assertEqual(len(prompts(transport)), 1)
        self.assertFalse(client._connected)

    async def test_oversized_message_does_not_send_and_empty_source_finishes(self):
        async def source():
            yield message("too long")

        transport = FakeTransport()
        client = KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch",
                                                stream_input_buffer_max_bytes=4), transport=transport)
        await client.connect()
        await client.query(source())
        with self.assertRaises(ValueError):
            _ = [m async for m in client.receive_messages()]
        self.assertEqual(prompts(transport), [])

        async def empty():
            if False:
                yield message("never")

        async with KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch"),
                                 transport=FakeTransport()) as client:
            await client.query(empty())
            self.assertEqual([m async for m in client.receive_messages()], [])

    async def test_disconnect_prevents_late_uncooperative_source_from_sending(self):
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
        client = KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch"), transport=transport)
        try:
            await client.connect()
            await client.query(source())
            await asyncio.wait_for(entered.wait(), 2)
            with patch("kiro_agent_sdk.client._STREAM_INPUT_CLEANUP_TIMEOUT", 0.05):
                with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                    await client.disconnect()
            with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                await client.connect()
            release.set()
            await asyncio.wait_for(closed.wait(), 2)
            await asyncio.wait_for(asyncio.shield(client._stream_cleanup_task), 2)
            self.assertEqual(prompts(transport), [])
        finally:
            release.set()
            await client.disconnect()

    async def test_closing_receiver_stops_prefetch_and_closes_source(self):
        closed = asyncio.Event()

        async def source():
            try:
                yield message("first")
                await asyncio.Event().wait()
            finally:
                closed.set()

        client = KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch"), transport=FakeTransport())
        await client.connect()
        await client.query(source())
        receiver = client.receive_messages()
        try:
            while not isinstance(await receiver.__anext__(), ResultMessage):
                pass
        finally:
            await receiver.aclose()
        await asyncio.wait_for(closed.wait(), 2)
        self.assertFalse(client._connected)

    async def test_source_close_error_is_reported_and_closes_connection(self):
        class Source:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def aclose(self):
                raise RuntimeError("source close failed")

        client = KiroSDKClient(KiroAgentOptions(stream_input_mode="prefetch"), transport=FakeTransport())
        await client.connect()
        await client.query(Source())
        with self.assertRaisesRegex(RuntimeError, "source close failed"):
            _ = [m async for m in client.receive_messages()]
        self.assertFalse(client._connected)

    async def test_immediate_disconnect_closes_source_before_producer_starts(self):
        class Source:
            def __init__(self):
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise AssertionError("prefetch must not start")

            async def aclose(self):
                self.closed = True

        source = Source()
        stream = PrefetchedInput(source, lambda item: (item, 1), 1, 1)
        await stream.aclose()
        self.assertTrue(source.closed)

    async def test_changed_mode_is_rejected_before_prompt(self):
        async def source():
            yield message("one")

        options = KiroAgentOptions(stream_input_mode="prefetch")
        transport = FakeTransport()
        client = KiroSDKClient(options, transport=transport)
        await client.connect()
        await client.query(source())
        options.stream_input_mode = "on-receive"
        with self.assertRaisesRegex(ValueError, "streaming input settings"):
            _ = [m async for m in client.receive_messages()]
        self.assertEqual(prompts(transport), [])
        await client.disconnect()
