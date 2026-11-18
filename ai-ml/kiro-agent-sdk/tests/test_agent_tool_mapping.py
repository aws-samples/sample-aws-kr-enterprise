import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import AgentDefinition, KiroAgentOptions, KiroSDKClient, ProtocolError


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class AgentToolMappingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def definition(self, **changes):
        values = dict(
            description="Review", prompt="Inspect input.", tools=["Read", "Search"],
            tools_mode="selectors", tool_selector_map={"Read": ["read"], "Search": ["grep", "read"]},
            disallowed_tools=["Read(secret)"], disallowed_tools_mode="v3-permissions",
            disallowed_tool_permission_map={"Read(secret)": [
                {"capability": "fs_read", "effect": "deny", "match": [str(self.root / "secret")]}]},
            kiro_permissions={"rules": [{"capability": "shell", "effect": "deny"}]})
        values.update(changes)
        return AgentDefinition(**values)

    def options(self, definition=None, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3",
                      env={"KIRO_HOME": str(self.root / "kiro")}, agent="worker",
                      agents={"worker": definition or self.definition()})
        values.update(changes)
        return KiroAgentOptions(**values)

    async def test_selected_agent_mapping_is_registered_and_mode_is_protected(self):
        definition = self.definition()
        before = copy.deepcopy(definition)
        async with KiroSDKClient(self.options(definition)) as client:
            profile = self.root / "kiro" / "agents" / "worker.json"
            config = json.loads(profile.read_text())
            self.assertEqual(config["tools"], ["read", "grep"])
            self.assertNotIn("excludedTools", config)
            self.assertEqual([rule["effect"] for rule in config["permissions"]["rules"]], ["deny", "deny"])
            self.assertEqual(config["permissions"]["rules"][1]["match"], [str(self.root / "secret")])
            with self.assertRaises(ProtocolError):
                await client.set_mode("vibe")
            with self.assertRaisesRegex(ProtocolError, "permission mode"):
                await client.set_permission_mode("trustAll")
            await client.query("hello")
            self.assertEqual([item async for item in client.receive_response()][-1].result, "hello")
        self.assertEqual(definition, before)
        self.assertFalse(profile.exists())

    async def test_unselected_agent_gets_its_own_mapped_profile(self):
        options = self.options(agent=None, tools=[])
        async with KiroSDKClient(options) as client:
            profile = self.root / "kiro" / "agents" / "worker.json"
            config = json.loads(profile.read_text())
            self.assertEqual(config["tools"], ["read", "grep"])
            self.assertEqual(config["permissions"]["rules"][1]["effect"], "deny")
            await client.query("hello")
            self.assertEqual([item async for item in client.receive_response()][-1].result, "hello")

    async def test_mapped_agent_cannot_be_selected_without_verification(self):
        options = self.options(agent=None)
        async with KiroSDKClient(options) as client:
            with self.assertRaisesRegex(ProtocolError, "Reconnect"):
                await client.set_mode("worker")
            with self.assertRaisesRegex(ProtocolError, "Reconnect"):
                await client.set_config_option("mode", "worker")

    async def test_unsolicited_mapped_agent_mode_fails_closed(self):
        options = self.options(agent=None)
        async with KiroSDKClient(options) as client:
            process = client._process
            await client.query("guard_mapped")
            await client._send({"jsonrpc": "2.0", "method": "_test/trigger", "params": {}})
            await asyncio.wait_for(process.wait(), 5)
            with self.assertRaisesRegex(ProtocolError, "unverified mapped agent"):
                _ = [item async for item in client.receive_response()]

    async def test_invalid_mappings_are_rejected_before_starting_cli(self):
        cases = [
            {"tools_mode": "native"},
            {"tool_selector_map": {"Read": ["read"]}},
            {"tool_selector_map": {"Read": [], "Search": ["grep"]}},
            {"tool_selector_map": {"Read": ["read\0"], "Search": ["grep"]}},
            {"tools": None},
            {"disallowed_tools_mode": "native"},
            {"disallowed_tool_permission_map": {}},
            {"disallowed_tool_permission_map": {"Read(secret)": [
                {"capability": "fs_read", "effect": "allow"}]}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                client = KiroSDKClient(self.options(self.definition(**changes)))
                with self.assertRaises(ValueError):
                    await client.connect()
                self.assertIsNone(client._process)
        for changes in ({"agent_engine": "v2"}, {"permission_mode": "trustAll"},
                        {"kiro_policy_presets": ["example"]}):
            with self.subTest(changes=changes):
                client = KiroSDKClient(self.options(**changes))
                with self.assertRaises(ValueError):
                    await client.connect()

    async def test_selected_policy_must_be_reported_and_changes_require_reconnect(self):
        options = self.options(env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_NO_POLICY": "1"})
        client = KiroSDKClient(options)
        with self.assertRaisesRegex(ProtocolError, "mapped agent permission"):
            await client.connect()
        self.assertFalse(client._connected)
        options = self.options()
        async with KiroSDKClient(options) as client:
            options.agents["worker"].tool_selector_map["Read"] = ["write"]
            with self.assertRaisesRegex(ValueError, "mapped agent"):
                await client.query("must-not-run")

