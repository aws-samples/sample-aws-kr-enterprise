import asyncio
import unittest

from kiro_agent_sdk import (
    CLIConnectionError,
    InMemorySessionStore,
    KiroAgentOptions,
    KiroSDKClient,
    MirrorErrorMessage,
    ResultMessage,
)
from test_transport import FakeTransport


class FlakyStore(InMemorySessionStore):
    def __init__(self):
        super().__init__()
        self.failures = 1
        self.attempts = 0

    async def append(self, key, entries):
        self.attempts += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("mirror unavailable")
        await super().append(key, entries)


class MirrorErrorPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonfatal_failure_emits_once_before_result_and_continues(self):
        for mode in ("batched", "eager"):
            with self.subTest(mode=mode):
                store = FlakyStore()
                options = KiroAgentOptions(agent_engine="v3", session_store=store,
                                           session_store_flush=mode, session_store_error_policy="continue")
                client = KiroSDKClient(options, transport=FakeTransport())
                async with client:
                    await client.query("first")
                    messages = [m async for m in client.receive_response()]
                    errors = [m for m in messages if isinstance(m, MirrorErrorMessage)]
                    self.assertEqual(len(errors), 1)
                    self.assertEqual(errors[0].error, "mirror unavailable")
                    self.assertEqual(errors[0].key["session_id"], "remote")
                    self.assertIsInstance(messages[-1], ResultMessage)
                    self.assertFalse(messages[-1].is_error)
                    self.assertEqual(client.last_mirror_error.error, "mirror unavailable")
                    await client.query("second")
                    later = [m async for m in client.receive_response()]
                    self.assertFalse(any(isinstance(m, MirrorErrorMessage) for m in later))
                    self.assertEqual(later[-1].result, "second")
                self.assertEqual(client.last_mirror_error.error, "mirror unavailable")
                self.assertGreaterEqual(store.attempts, 2)

    async def test_failure_policy_remains_fatal_by_default(self):
        options = KiroAgentOptions(agent_engine="v3", session_store=FlakyStore())
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            with self.assertRaisesRegex(RuntimeError, "mirror unavailable"):
                await client.query("first")
                _ = [m async for m in client.receive_response()]

    async def test_continue_does_not_hide_flush_timeout(self):
        class SlowStore:
            async def append(self, key, entries):
                await asyncio.sleep(0.5)
        options = KiroAgentOptions(agent_engine="v3", session_store=SlowStore(),
                                   session_store_error_policy="continue", session_store_flush_timeout=0.05)
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            with self.assertRaisesRegex(CLIConnectionError, "Timed out"):
                await client.query("first")
                _ = [m async for m in client.receive_response()]

    def test_invalid_policies_fail_early(self):
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(session_store_error_policy="continue"))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(session_store_error_policy="ignore"))


if __name__ == "__main__":
    unittest.main()
