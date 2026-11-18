"""Per-operation lifecycle and input guards for session-control clients."""
import unittest

from kiro_agent_sdk import CLIConnectionError


OPERATIONS = [
    ("list_sessions", (), {}, (), {"cursor": ""}),
    ("get_session_info", ("test-session",), {}, ("",), {}),
    ("rename_session", ("test-session", "title"), {}, ("test-session", ""), {}),
    ("fork_session", ("test-session",), {}, ("",), {}),
    ("delete_session", ("test-session",), {}, ("",), {}),
    ("get_session_history", ("test-session",), {}, ("",), {}),
    ("get_session_messages", ("test-session",), {}, ("test-session",), {"offset": -1}),
]


class SessionClientContractMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_session_client import SessionClientTests
        self.fixture = SessionClientTests("test_configuration_validation")
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_each_control_operation_normal_result_contract(self):
        from kiro_agent_sdk import AgentMessage, ForkSessionResult, SessionInfo, ToolCall, UserMessage
        client = self.fixture.client()
        await client.connect()
        listing = await client.list_sessions()
        self.assertIsInstance(listing, dict)
        self.assertEqual(listing["sessions"][0]["sessionId"], "test-session")
        info = await client.get_session_info("test-session")
        self.assertIsInstance(info, SessionInfo)
        self.assertEqual(info.session_id, "test-session")
        self.assertIsNone(await client.rename_session("test-session", "title"))
        fork = await client.fork_session("test-session")
        self.assertIsInstance(fork, ForkSessionResult)
        self.assertEqual(fork.session_id, "forked")
        self.assertIsNone(await client.delete_session("forked"))
        history = await client.get_session_history("test-session")
        self.assertIsInstance(history, list)
        self.assertTrue(history)
        messages = await client.get_session_messages("test-session")
        self.assertTrue(messages)
        self.assertTrue(all(isinstance(message, (UserMessage, AgentMessage, ToolCall)) for message in messages))
        self.assertFalse(any(row.get("method") in ("session/new", "session/prompt")
                             for row in self.fixture.requests()))

    async def test_each_control_operation_requires_a_connected_client(self):
        for name, args, kwargs, _, _ in OPERATIONS:
            with self.subTest(method=name):
                client = self.fixture.client()
                for phase in ("before", "after"):
                    if phase == "after":
                        await client.connect()
                        await client.disconnect()
                    before = len(self.fixture.requests())
                    with self.assertRaises(CLIConnectionError):
                        await getattr(client, name)(*args, **kwargs)
                    self.assertEqual(len(self.fixture.requests()), before)

    async def test_each_control_operation_rejects_its_invalid_input_before_dispatch(self):
        client = self.fixture.client()
        await client.connect()
        for name, _, _, args, kwargs in OPERATIONS:
            with self.subTest(method=name):
                before = len(self.fixture.requests())
                with self.assertRaises(ValueError):
                    await getattr(client, name)(*args, **kwargs)
                self.assertEqual(len(self.fixture.requests()), before)

    async def test_each_control_operation_rechecks_revoked_authorization(self):
        for name, args, kwargs, _, _ in OPERATIONS:
            with self.subTest(method=name):
                allowed = True
                async def authorize(access):
                    return allowed
                client = self.fixture.client(authorize=authorize)
                await client.connect()
                allowed = False
                before = len(self.fixture.requests())
                supplied = self.fixture.calls
                with self.assertRaises(PermissionError):
                    await getattr(client, name)(*args, **kwargs)
                self.assertEqual(len(self.fixture.requests()), before)
                self.assertEqual(self.fixture.calls, supplied)
                await client.disconnect()

    async def test_connect_and_disconnect_return_no_payload_and_reopen_cleanly(self):
        client = self.fixture.client()
        for _ in range(2):
            self.assertIsNone(await client.connect())
            self.assertTrue(client._connected)
            self.assertIsNone(await client.disconnect())
            self.assertFalse(client._connected)


if __name__ == "__main__":
    unittest.main()
