import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (BackgroundTaskManager, CLIConnectionError,
                            KiroAgentOptions, KiroSDKClient, ResultMessage, query, query_with_fallback,
                            query_with_structured_output_retry)

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class ContinuousMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_receive_messages_can_opt_into_continuous_reception(self):
        options = KiroAgentOptions(cli_path=FAKE, receive_messages_mode="continuous")
        async with KiroSDKClient(options) as client:
            listener = client.receive_messages()
            results = []
            async def listen():
                try:
                    async for message in listener:
                        if isinstance(message, ResultMessage):
                            results.append(message.result)
                            if len(results) == 1:
                                await client.query("second")
                            else:
                                break
                finally:
                    await listener.aclose()
            task = asyncio.create_task(listen())
            try:
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                await client.query("first")
                await asyncio.wait_for(task, 5)
                self.assertEqual(results, ["first", "second"])
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_one_shot_and_background_tasks_remain_finite_in_continuous_mode(self):
        async def source():
            for text in ("one", "two"):
                yield text
        options = KiroAgentOptions(cli_path=FAKE, receive_messages_mode="continuous")
        async def one_shot():
            return [m.result async for m in query(prompt=source(), options=options)
                    if isinstance(m, ResultMessage)]
        self.assertEqual(await asyncio.wait_for(one_shot(), 5), ["one", "two"])
        async with BackgroundTaskManager() as manager:
            task_id = await manager.start("background", options=options)
            status = await asyncio.wait_for(manager.wait(task_id), 5)
            self.assertEqual(status.state, "completed")
            events = [m async for m in manager.events(task_id)]
            self.assertEqual([m.result for m in events if isinstance(m, ResultMessage)], ["background"])

    async def test_retry_helpers_remain_finite_in_continuous_mode(self):
        from test_runtime_fallback import RetryTransport
        async def approve_fallback(result, observed_tools):
            return True
        async def fallback():
            return [m async for m in query_with_fallback(
                prompt="first", fallback_model="model-two", approve_retry=approve_fallback,
                options=KiroAgentOptions(agent_engine="v3", receive_messages_mode="continuous"),
                transport=RetryTransport()) if isinstance(m, ResultMessage)]
        fallback_results = await asyncio.wait_for(fallback(), 5)
        self.assertEqual([m.is_error for m in fallback_results], [True, False])

        async def approve_format(error, observed_tools, attempt):
            return True
        async def structured():
            return [m async for m in query_with_structured_output_retry(
                prompt="not-json", retry_prompt='{"ok": true}', approve_retry=approve_format,
                options=KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
                    receive_messages_mode="continuous",
                    output_format={"type": "json_schema", "schema": {"type": "object"}}))
                    if isinstance(m, ResultMessage)]
        structured_results = await asyncio.wait_for(structured(), 5)
        self.assertEqual(structured_results[0].structured_output, {"ok": True})

    async def test_mode_is_validated_and_cannot_change_after_client_creation(self):
        for value in ("unknown", None, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "receive_messages_mode"):
                KiroSDKClient(KiroAgentOptions(cli_path=FAKE, receive_messages_mode=value))
        options = KiroAgentOptions(cli_path=FAKE)
        async with KiroSDKClient(options) as client:
            options.receive_messages_mode = "continuous"
            with self.assertRaisesRegex(ValueError, "receive_messages_mode"):
                await client.query("one")
            with self.assertRaisesRegex(ValueError, "receive_messages_mode"):
                await client.receive_messages().__anext__()
            options.receive_messages_mode = "current"
            await client.query("valid")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "valid")

    async def test_idle_listener_receives_later_turns_and_can_submit_from_result(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        results = []
        done = asyncio.Event()
        async def listen():
            async for message in client.receive_messages_continuously():
                if isinstance(message, ResultMessage):
                    results.append(message.result)
                    if len(results) == 1:
                        await client.query("second")
                    else:
                        done.set()
                        break
        try:
            await client.connect()
            task = asyncio.create_task(listen())
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            await client.query("first")
            await asyncio.wait_for(done.wait(), 5)
            await asyncio.wait_for(task, 5)
            self.assertEqual(results, ["first", "second"])
            self.assertTrue(client._connected)
        finally:
            await client.disconnect()

    async def test_idle_disconnect_ends_listener_and_reconnect_is_independent(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        await client.connect()
        listener = client.receive_messages_continuously()
        waiting = asyncio.create_task(listener.__anext__())
        await asyncio.sleep(0)
        await client.disconnect()
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(waiting, 5)
        await listener.aclose()
        try:
            await client.connect()
            await client.query("fresh")
            result = [m async for m in client.receive_response() if isinstance(m, ResultMessage)]
            self.assertEqual(result[0].result, "fresh")
        finally:
            await client.disconnect()

    async def test_only_one_receiver_can_consume_the_session(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            listener = client.receive_messages_continuously()
            waiting = asyncio.create_task(listener.__anext__())
            await asyncio.sleep(0)
            for other in (client.receive_messages_continuously(), client.receive_response(),
                          client.receive_messages()):
                with self.assertRaisesRegex(CLIConnectionError, "receiver is active"):
                    await other.__anext__()
                await other.aclose()
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            await listener.aclose()
            self.assertTrue(client._connected)
            await client.query("after cancellation")
            messages = [m async for m in client.receive_response()]
            self.assertEqual(messages[-1].result, "after cancellation")

    async def test_continuous_receiver_rejects_an_active_finite_receiver(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            await client.query("one")
            finite = client.receive_response()
            await finite.__anext__()
            continuous = client.receive_messages_continuously()
            with self.assertRaisesRegex(CLIConnectionError, "receiver is active"):
                await continuous.__anext__()
            await continuous.aclose()
            await finite.aclose()

    async def test_streamed_input_is_consumed_and_disconnect_closes_listener(self):
        entered = asyncio.Event()
        async def prompts():
            for text in ("one", "two"):
                yield text
            entered.set()
            await asyncio.Event().wait()
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        results = []
        done = asyncio.Event()
        async def listen():
            async for message in client.receive_messages_continuously():
                if isinstance(message, ResultMessage):
                    results.append(message.result)
                    if len(results) == 2:
                        done.set()
        try:
            await client.connect()
            task = asyncio.create_task(listen())
            await asyncio.sleep(0)
            await client.query(prompts())
            await asyncio.wait_for(done.wait(), 5)
            await asyncio.wait_for(entered.wait(), 5)
            await client.disconnect()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            self.assertEqual(results, ["one", "two"])
        finally:
            await client.disconnect()

    async def test_reconnect_waits_for_suspended_receiver_to_close(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        await client.connect()
        await client.query("one")
        listener = client.receive_messages_continuously()
        await listener.__anext__()
        await client.disconnect()
        with self.assertRaisesRegex(CLIConnectionError, "receiver is still active"):
            await client.connect()
        await listener.aclose()
        try:
            await client.connect()
            await client.query("fresh")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "fresh")
        finally:
            await client.disconnect()

    async def test_reader_failure_wakes_idle_listener(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        try:
            await client.connect()
            listener = client.receive_messages_continuously()
            waiting = asyncio.create_task(listener.__anext__())
            await asyncio.sleep(0)
            client._process.terminate()
            with self.assertRaisesRegex(CLIConnectionError, "reader failed"):
                await asyncio.wait_for(waiting, 5)
            await listener.aclose()
        finally:
            await client.disconnect()

    async def test_uncooperative_input_cannot_reach_a_reconnected_session(self):
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def source():
            try:
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                yield "late"
            finally:
                closed.set()
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        task = None
        try:
            await client.connect()
            listener = client.receive_messages_continuously()
            task = asyncio.create_task(listener.__anext__())
            await asyncio.sleep(0)
            await client.query(source())
            await asyncio.wait_for(entered.wait(), 2)
            with patch("kiro_agent_sdk.client._STREAM_INPUT_CLEANUP_TIMEOUT", 0.05):
                with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                    await client.disconnect()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            await listener.aclose()
            with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                await client.connect()
            release.set()
            await asyncio.wait_for(closed.wait(), 2)
            await asyncio.wait_for(asyncio.shield(client._stream_cleanup_task), 2)
            await client.connect()
            await client.query("fresh")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "fresh")
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.disconnect()


class ContinuousMessagesLoopTests(unittest.TestCase):
    def test_client_created_outside_the_running_loop(self):
        creation_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(creation_loop)
        try:
            client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        finally:
            asyncio.set_event_loop(None)
            creation_loop.close()

        async def run():
            async with client:
                listener = client.receive_messages_continuously()
                waiting = asyncio.create_task(listener.__anext__())
                await asyncio.sleep(0)
                await client.query("first")
                await asyncio.wait_for(waiting, 5)
                await listener.aclose()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
