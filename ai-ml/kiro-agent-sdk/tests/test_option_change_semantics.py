"""Observed semantics of changing options on a connected client.

Snapshot options keep the connect-time value for the current Kiro session and take effect on the
next connection. Live options apply to the next turn. Rejected options block the next prompt.
"""
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (AgentDefinition, CLIConnectionError, CLINotFoundError, HookMatcher, KiroAgentOptions,
                            KiroSDKClient, PermissionResultAllow, ProtocolError, StreamEvent)

FAKE = str(Path(__file__).with_name("fake_kiro.py"))
SESSION_METHODS = ("session/new", "session/load", "session/fork", "session/list")


async def allow(name, data, context):
    return PermissionResultAllow()


class OptionChangeSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3",
                      env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log)})
        values.update(changes)
        return KiroAgentOptions(**values)

    def requests(self, *methods):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if not methods or row.get("method") in methods]

    async def turn(self, client, prompt="hello"):
        await client.query(prompt)
        return [message async for message in client.receive_response()]

    async def change_then_reconnect(self, options, mutate):
        """Mutate after connect; return (session requests before, during, after reconnect)."""
        client = KiroSDKClient(options)
        try:
            await client.connect()
            first = self.requests(*SESSION_METHODS)
            session = client.session_id
            mutate(options)
            await self.turn(client)
            self.assertEqual(self.requests(*SESSION_METHODS), first)
            prompt = self.requests("session/prompt")[-1]["params"]
            self.assertEqual(prompt["sessionId"], session)
            await client.disconnect()
            await client.connect()
            second = self.requests(*SESSION_METHODS)[len(first):]
            await self.turn(client)
            return first, second, client
        finally:
            await client.disconnect()

    async def test_snapshot_session_parameters_apply_only_after_reconnect(self):
        (self.root / "one").mkdir()
        (self.root / "two").mkdir()
        cases = [
            ("cwd", {}, lambda o: setattr(o, "cwd", self.root / "two"),
             lambda r: r["params"]["cwd"], str(self.root), str(self.root / "two")),
            ("add_dirs", {"add_dirs": [str(self.root / "one")]}, lambda o: setattr(o, "add_dirs", [str(self.root / "two")]),
             lambda r: r["params"]["additionalDirectories"], [str(self.root / "one")], [str(self.root / "two")]),
            ("mcp_servers", {"mcp_servers": {"one": {"type": "stdio", "command": "x"}}},
             lambda o: setattr(o, "mcp_servers", {"two": {"type": "stdio", "command": "x"}}),
             lambda r: [server["name"] for server in r["params"]["mcpServers"]], ["one"], ["two"]),
            ("thinking", {"thinking": True}, lambda o: setattr(o, "thinking", None),
             lambda r: r["params"].get("_meta", {}).get("kiro", {}).get("thinkingEnabled"), True, None),
            ("agent", {"agent": "worker", "agents": {"worker": AgentDefinition("W", "Work"),
                                                     "other": AgentDefinition("O", "Other")}},
             lambda o: setattr(o, "agent", "other"),
             lambda r: r["params"]["_meta"]["kiro"]["modeId"], "worker", "other"),
            ("session_id", {"session_id": "11111111-1111-4111-8111-111111111111"},
             lambda o: setattr(o, "session_id", "22222222-2222-4222-8222-222222222222"),
             lambda r: r["params"]["_meta"]["kiro"]["overrideSessionId"],
             "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
            ("resume", {"resume": "old"}, lambda o: setattr(o, "resume", "other"),
             lambda r: (r["method"], r["params"]["sessionId"]), ("session/load", "old"), ("session/load", "other")),
        ]
        for name, initial, mutate, read, before, after in cases:
            with self.subTest(option=name):
                self.log.unlink(missing_ok=True)
                first, second, _ = await self.change_then_reconnect(self.options(**initial), mutate)
                self.assertEqual(read(first[-1]), before)
                self.assertEqual(read(second[-1]), after)

    async def test_snapshot_session_selection_applies_only_after_reconnect(self):
        _, second, _ = await self.change_then_reconnect(
            self.options(), lambda o: setattr(o, "continue_conversation", True))
        self.assertEqual([row["method"] for row in second], ["session/list", "session/load"])
        self.log.unlink()
        first, second, _ = await self.change_then_reconnect(
            self.options(resume="old", fork_session=True), lambda o: setattr(o, "fork_session", False))
        self.assertEqual(first[0]["method"], "session/fork")
        self.assertEqual([(row["method"], row["params"]["sessionId"]) for row in second], [("session/load", "old")])
        self.log.unlink()
        env = {"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log),
               "FAKE_DEFAULT_SESSION_ID": "first-session"}
        _, _, client = await self.change_then_reconnect(
            self.options(env=env), lambda o: o.env.update(FAKE_DEFAULT_SESSION_ID="second-session"))
        self.assertEqual([row["params"]["sessionId"] for row in self.requests("session/prompt")],
                         ["first-session", "second-session"])

    async def test_snapshot_profile_and_sandbox_apply_only_after_reconnect(self):
        def profile(client):
            return json.loads((self.root / "kiro" / "agents" / (client._agent_file.name)).read_text())["prompt"]
        options = self.options(system_prompt="FIRST")
        client = KiroSDKClient(options)
        try:
            await client.connect()
            options.system_prompt = "SECOND"
            await self.turn(client)
            self.assertIn("FIRST", profile(client))
            await client.disconnect()
            await client.connect()
            self.assertIn("SECOND", profile(client))
        finally:
            await client.disconnect()
        self.log.unlink()
        await self.change_then_reconnect(self.options(require_sandbox=True),
                                         lambda o: setattr(o, "require_sandbox", False))
        self.assertEqual(len(self.requests("_kiro/sandbox/applyConfig")), 1)

    async def test_snapshot_process_settings_apply_only_after_reconnect(self):
        received = []
        env = {"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log), "FAKE_STDERR": "diagnostic"}
        options = self.options(env=env, stderr=lambda line: received.append(("old", line)))
        client = KiroSDKClient(options)
        try:
            await client.connect()
            options.stderr = lambda line: received.append(("new", line))
            await self.turn(client)
            await client.disconnect()
            await client.connect()
        finally:
            await client.disconnect()
        self.assertTrue(any(owner == "old" for owner, _ in received))
        self.assertTrue(any(owner == "new" for owner, _ in received))
        for name, value, error in (("max_buffer_size", 10, ProtocolError),
                                   ("startup_timeout", 0.0001, CLIConnectionError),
                                   ("cli_path", str(self.root / "missing-kiro"), CLINotFoundError)):
            with self.subTest(option=name):
                options = self.options()
                client = KiroSDKClient(options)
                try:
                    await client.connect()
                    setattr(options, name, value)
                    self.assertEqual((await self.turn(client))[-1].result, "hello")
                    await client.disconnect()
                    with self.assertRaises(error):
                        await client.connect()
                finally:
                    await client.disconnect()

    async def test_live_options_apply_to_the_next_turn(self):
        seen = []

        async def hook(event, tool_id, context):
            seen.append(event["prompt"])
            return {}
        options = self.options(can_use_tool=allow)
        async with KiroSDKClient(options) as client:
            self.assertFalse(any(isinstance(m, StreamEvent) for m in await self.turn(client)))
            options.include_partial_messages = True
            self.assertTrue(any(isinstance(m, StreamEvent) for m in await self.turn(client)))
            options.hooks = {"promptSubmit": [HookMatcher(hooks=[hook])]}
            await self.turn(client, "hooked")
            self.assertEqual(seen, ["hooked"])
            self.assertEqual((await self.turn(client, "permission"))[-1].result, "yes")
            options.can_use_tool = None
            self.assertEqual((await self.turn(client, "permission"))[-1].result, "no")
            options.output_format = {"type": "json_schema", "schema": {"type": "object"}}
            self.assertEqual((await self.turn(client, '{"ok": true}'))[-1].structured_output, {"ok": True})
            options.turn_timeout = 0.0001
            with self.assertRaisesRegex(CLIConnectionError, "Timed out"):
                await self.turn(client)


if __name__ == "__main__":
    unittest.main()
