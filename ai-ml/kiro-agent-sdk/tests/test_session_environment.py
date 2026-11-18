import unittest
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import delete_session, get_session_history, get_session_messages, list_models


class SessionEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_listing_and_delete_pass_environment(self):
        env = {"KIRO_HOME": "/isolated/kiro"}
        with patch("kiro_agent_sdk.sessions._run_cli", new=AsyncMock(return_value=b"")) as run:
            await delete_session("test-id", cwd="/workspace", cli_path="custom-kiro", env=env)
        run.assert_awaited_once_with(["--delete-session", "test-id"], "/workspace", "custom-kiro", env)
        with patch("kiro_agent_sdk.sessions._list_cli",
                   new=AsyncMock(return_value={"models": [{"model_id": "example"}]})) as listing:
            self.assertEqual((await list_models(cwd="/workspace", cli_path="custom-kiro", env=env))[0]["model_id"],
                             "example")
        listing.assert_awaited_once_with("--list-models", "/workspace", "custom-kiro", env)

    async def test_history_opens_client_in_supplied_environment(self):
        env = {"KIRO_HOME": "/isolated/kiro"}
        with patch("kiro_agent_sdk.client.KiroSDKClient") as factory:
            client = factory.return_value
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client.history = [{"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hello"}}]
            events = await get_session_history("test-id", cwd="/workspace", agent_engine="v3", env=env)
        options = factory.call_args.args[0]
        self.assertEqual((options.cwd, options.agent_engine, options.resume, options.env),
                         ("/workspace", "v3", "test-id", env))
        self.assertEqual(events[0]["content"]["text"], "hello")

    async def test_message_conversion_passes_environment(self):
        env = {"KIRO_HOME": "/isolated/kiro"}
        with patch("kiro_agent_sdk.sessions.get_session_history", new=AsyncMock(return_value=[])) as history:
            self.assertEqual(await get_session_messages("test-id", cwd="/workspace", agent_engine="v3", env=env), [])
        history.assert_awaited_once_with("test-id", cwd="/workspace", cli_path="kiro-cli", agent_engine="v3", env=env)


if __name__ == "__main__":
    unittest.main()
