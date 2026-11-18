import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient


FAKE = Path(__file__).with_name("fake_kiro.py").resolve()


class SpawnCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_cancelled_before_coroutine_starts_unblocks_query(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        await client.connect()
        process = client._process
        create_task = asyncio.create_task

        def cancel_before_start(coroutine, *args, **kwargs):
            task = create_task(coroutine, *args, **kwargs)
            if getattr(getattr(coroutine, "cr_code", None), "co_name", None) == "_prompt_request":
                task.cancel()
            return task

        with patch("kiro_agent_sdk.client.asyncio.create_task", new=cancel_before_start):
            with self.assertRaisesRegex(CLIConnectionError, "turn was closed"):
                await asyncio.wait_for(client.query("hello"), 5)
        self.assertFalse(client._connected)
        self.assertIsNotNone(process.returncode)

    async def test_cancel_during_subprocess_creation_reaps_child(self):
        entered, release, spawned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec
        children = []

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            process = await real_spawn(*args, **kwargs)
            children.append(process)
            spawned.set()
            return process

        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        with patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", new=delayed):
            task = asyncio.create_task(client.connect())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(spawned.wait(), 5)
            await asyncio.wait_for(children[0].wait(), 5)
            self.assertIsNotNone(children[0].returncode)
            self.assertFalse(client._connected)


if __name__ == "__main__":
    unittest.main()
