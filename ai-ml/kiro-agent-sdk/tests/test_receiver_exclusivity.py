"""A second receive_response() while one is active is refused instead of deadlocking."""
import asyncio
import unittest

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient, ResultMessage
from test_transport import FakeTransport


class ReceiverExclusivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_receiver_in_the_same_task_is_refused(self):
        async with KiroSDKClient(KiroAgentOptions(), transport=FakeTransport()) as client:
            await client.query("first")
            results = 0
            async for message in client.receive_response():
                with self.assertRaisesRegex(CLIConnectionError, "Another message receiver is active"):
                    async for _ in client.receive_response():
                        pass
                results += isinstance(message, ResultMessage)
            self.assertEqual(results, 1)
            await client.query("second")
            messages = [message async for message in client.receive_response()]
            self.assertEqual(sum(isinstance(message, ResultMessage) for message in messages), 1)

    async def test_concurrent_receiver_task_is_refused_and_the_first_completes(self):
        async with KiroSDKClient(KiroAgentOptions(), transport=FakeTransport()) as client:
            await client.query("first")
            stream = client.receive_response()
            first = await stream.__anext__()

            async def second():
                async for _ in client.receive_response():
                    pass
            with self.assertRaisesRegex(CLIConnectionError, "Another message receiver is active"):
                await asyncio.wait_for(second(), 2)
            rest = [first] + [message async for message in stream]
            self.assertEqual(sum(isinstance(message, ResultMessage) for message in rest), 1)


if __name__ == "__main__":
    unittest.main()
