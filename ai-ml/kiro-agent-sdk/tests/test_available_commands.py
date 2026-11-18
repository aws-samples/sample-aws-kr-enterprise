import json
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient
from test_transport import FakeTransport


def update(session_id, commands):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session_id, "update": {
            "sessionUpdate": "available_commands_update", "availableCommands": commands}}}


class CommandTransport(FakeTransport):
    def __init__(self, before=None, initial=None):
        super().__init__()
        self.before = before or []
        self.initial = initial

    async def write(self, data):
        message = json.loads(data)
        if message["method"] != "session/new":
            await super().write(data)
            return
        self.requests.append(message)
        for notification in self.before:
            await self.queue.put(notification)
        result = {"sessionId": "remote", "modes": {"currentModeId": "vibe"}}
        if self.initial is not None:
            result["availableCommands"] = self.initial
        await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": result})


class AvailableCommandTests(unittest.IsolatedAsyncioTestCase):
    async def begin(self, transport):
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport)
        self.addAsyncCleanup(client.disconnect)
        await client.connect()
        return client

    async def deliver(self, client, transport, notification):
        await transport.queue.put(notification)
        await client.query("ack")
        _ = [message async for message in client.receive_response()]

    async def test_startup_update_is_scoped_and_copied(self):
        commands = [{"name": "review", "description": "Review changes",
                     "input": {"hint": "[path]"}, "privateExtension": "not returned"}]
        transport = CommandTransport(before=[update("other", [{"name": "secret", "description": "Other"}]),
                                              update("remote", commands)])
        client = await self.begin(transport)
        expected = [{"name": "review", "description": "Review changes", "input": {"hint": "[path]"}}]
        self.assertEqual(await client.get_kiro_available_commands(), expected)
        info = await client.get_server_info()
        self.assertEqual(info["commands"], expected)
        info["commands"][0]["input"]["hint"] = "changed"
        info["agentCapabilities"]["loadSession"] = False
        self.assertEqual(await client.get_kiro_available_commands(), expected)
        self.assertTrue((await client.get_server_info())["agentCapabilities"]["loadSession"])
        await client.disconnect()
        self.assertIsNone(await client.get_kiro_available_commands())
        self.assertNotIn("commands", await client.get_server_info())

    async def test_setup_response_and_newer_notification_precedence(self):
        initial = [{"name": "old", "description": "Old"}]
        new = [{"name": "new", "description": "New"}]
        first = await self.begin(CommandTransport(initial=initial))
        self.assertEqual(await first.get_kiro_available_commands(), initial)
        second = await self.begin(CommandTransport(before=[update("remote", new)], initial=initial))
        self.assertEqual(await second.get_kiro_available_commands(), new)

    async def test_updates_replace_clear_and_reject_malformed_snapshots(self):
        transport = CommandTransport()
        client = await self.begin(transport)
        self.assertIsNone(await client.get_kiro_available_commands())
        self.assertNotIn("commands", await client.get_server_info())
        command = [{"name": "one", "description": "One"}]
        await self.deliver(client, transport, update("remote", command))
        self.assertEqual(await client.get_kiro_available_commands(), command)
        await self.deliver(client, transport, update("other", [{"name": "other", "description": "Other"}]))
        self.assertEqual(await client.get_kiro_available_commands(), command)
        await self.deliver(client, transport, update("remote", []))
        self.assertEqual(await client.get_kiro_available_commands(), [])
        self.assertEqual((await client.get_server_info())["commands"], [])
        await self.deliver(client, transport, update("remote", "invalid"))
        self.assertIsNone(await client.get_kiro_available_commands())
        self.assertNotIn("commands", await client.get_server_info())
        await self.deliver(client, transport, update("remote", command * 2))
        self.assertIsNone(await client.get_kiro_available_commands())
        await self.deliver(client, transport, update("remote", command * 257))
        self.assertIsNone(await client.get_kiro_available_commands())

    async def test_invalid_entries_are_skipped_and_input_is_bounded(self):
        transport = CommandTransport()
        client = await self.begin(transport)
        values = [{"name": "bad\nname", "description": "Hidden"},
                  {"name": "bad-description", "description": "bad\x1b"},
                  {"type": "_custom", "name": "custom", "description": "Custom"},
                  {"name": "review", "description": "Review", "input": {"hint": "x" * 1025}},
                  {"type": "command", "name": "help", "description": "Help",
                   "input": {"type": "text", "hint": "[topic]"}},
                  {"name": "other", "description": "Other", "input": {"type": "_custom", "hint": "ignored"}}]
        await self.deliver(client, transport, update("remote", values))
        self.assertEqual(await client.get_kiro_available_commands(),
                         [{"name": "review", "description": "Review"},
                          {"name": "help", "description": "Help", "input": {"hint": "[topic]"}},
                          {"name": "other", "description": "Other"}])

    async def test_setup_notification_count_is_bounded(self):
        before = [update("other-" + str(index), []) for index in range(16)]
        before.append(update("remote", [{"name": "late", "description": "Late"}]))
        client = await self.begin(CommandTransport(before=before))
        self.assertIsNone(await client.get_kiro_available_commands())
        self.assertEqual(client._available_commands_by_session, {})

    async def test_resumed_session_captures_its_own_update(self):
        class ResumeTransport(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message["method"] != "session/load":
                    await super().write(data)
                    return
                self.requests.append(message)
                await self.queue.put(update("resumed", [{"name": "continue", "description": "Continue"}]))
                await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3", resume="resumed"), transport=ResumeTransport())
        self.addAsyncCleanup(client.disconnect)
        await client.connect()
        self.assertEqual(await client.get_kiro_available_commands(),
                         [{"name": "continue", "description": "Continue"}])

    async def test_snapshot_total_size_is_bounded(self):
        transport = CommandTransport()
        client = await self.begin(transport)
        commands = [{"name": "command" + str(index), "description": "x" * 2048} for index in range(65)]
        await self.deliver(client, transport, update("remote", commands))
        self.assertIsNone(await client.get_kiro_available_commands())
