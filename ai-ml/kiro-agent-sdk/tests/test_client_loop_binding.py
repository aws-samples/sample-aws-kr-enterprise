import asyncio
import unittest
from pathlib import Path

from kiro_agent_sdk import InMemorySessionStore, KiroAgentOptions, KiroSDKClient, ResultMessage

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class ClientLoopBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_constructed_in_plain_thread_connects_and_handles_lock_contention(self):
        client = await asyncio.to_thread(
            KiroSDKClient, KiroAgentOptions(cli_path=FAKE, stream_input_mode="prefetch"))
        try:
            await client.connect()
            await client._query_lock.acquire()
            waiting = asyncio.create_task(client.query("hello"))
            for _ in range(100):
                if client._query_lock._waiters:
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(client._query_lock._waiters)
            client._query_lock.release()
            await asyncio.wait_for(waiting, 2)
            results = [event.result async for event in client.receive_response()
                       if isinstance(event, ResultMessage)]
            self.assertEqual(results, ["hello"])
        finally:
            await client.disconnect()

    async def test_memory_store_constructed_in_thread_serializes_writes(self):
        store = await asyncio.to_thread(InMemorySessionStore)
        key = {"project_key": "project", "session_id": "session"}
        def append(index):
            asyncio.run(store.append(key, [{"type": "event", "index": index}]))
        await asyncio.gather(*(asyncio.to_thread(append, index) for index in range(20)))
        entries = await store.load(key)
        self.assertEqual({entry["index"] for entry in entries}, set(range(20)))
