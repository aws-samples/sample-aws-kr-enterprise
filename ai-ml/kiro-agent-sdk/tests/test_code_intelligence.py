import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient, ProtocolError

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class CodeIntelligenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"

    async def asyncTearDown(self):
        self.temp.cleanup()

    def options(self, **kwargs):
        return KiroAgentOptions(
            cwd=self.root, cli_path=FAKE, agent_engine="v3",
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log)},
            **kwargs,
        )

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    async def test_opt_in_negotiates_and_calls_code_intelligence(self):
        async with KiroSDKClient(self.options(enable_kiro_code_intelligence=True)) as client:
            self.assertEqual((await client.get_kiro_code_intelligence_status())["subcommand"], "status")
            self.assertEqual((await client.initialize_kiro_code_intelligence())["subcommand"], "init")
            self.assertEqual((await client.get_kiro_code_overview())["subcommand"], "overview")
            requests = self.requests()
            init = next(item for item in requests if item["method"] == "initialize")
            kiro = init["params"]["clientCapabilities"]["_meta"]["kiro"]
            self.assertEqual(kiro["settings"]["codeIntelligence"], {"enabled": True})
            self.assertEqual(kiro["hooks"]["enabled"], True)
            commands = [item["params"] for item in requests if item["method"] == "_kiro/codeIntelligence"]
            self.assertEqual([item["subcommand"] for item in commands], ["status", "status", "init", "overview"])
            self.assertTrue(all(item["sessionId"] == client.session_id for item in commands))

    async def test_custom_transport_negotiates_code_intelligence(self):
        from test_transport import FakeTransport

        class CodeTransport(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message.get("method") == "initialize":
                    self.requests.append(message)
                    await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "protocolVersion": 1, "agentCapabilities": {
                            "_meta": {"kiro": {"extensionMethods": ["_kiro/codeIntelligence"]}}}}})
                elif message.get("method") == "_kiro/codeIntelligence":
                    self.requests.append(message)
                    await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "success": True, "subcommand": message["params"]["subcommand"]}})
                else:
                    await super().write(data)

        transport = CodeTransport()
        options = KiroAgentOptions(agent_engine="v3", cwd="/remote/workspace",
                                   enable_kiro_code_intelligence=True)
        async with KiroSDKClient(options, transport=transport) as client:
            self.assertEqual((await client.get_kiro_code_overview())["subcommand"], "overview")
        init = next(item for item in transport.requests if item["method"] == "initialize")
        self.assertEqual(init["params"]["clientCapabilities"]["_meta"]["kiro"]["settings"]["codeIntelligence"],
                         {"enabled": True})

    async def test_disabled_by_default_and_requires_advertised_extension(self):
        async with KiroSDKClient(self.options()) as client:
            with self.assertRaisesRegex(ProtocolError, "Enable"):
                await client.get_kiro_code_intelligence_status()
            init = next(item for item in self.requests() if item["method"] == "initialize")
            kiro = init["params"]["clientCapabilities"].get("_meta", {}).get("kiro", {})
            self.assertNotIn("codeIntelligence", kiro.get("settings", {}))
            self.assertFalse(any(item["method"] == "_kiro/codeIntelligence" for item in self.requests()))
        options = self.options(enable_kiro_code_intelligence=True)
        options.env["FAKE_NO_CODE_INTELLIGENCE"] = "1"
        with self.assertRaisesRegex(ProtocolError, "_kiro/codeIntelligence"):
            await KiroSDKClient(options).connect()
        options.env.pop("FAKE_NO_CODE_INTELLIGENCE")
        options.env["FAKE_CODE_DISABLED"] = "1"
        with self.assertRaisesRegex(ProtocolError, "did not confirm"):
            await KiroSDKClient(options).connect()

    async def test_requires_v3_boolean_and_reconnect_after_mutation(self):
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                await KiroSDKClient(self.options(enable_kiro_code_intelligence=value)).connect()
        options = self.options(enable_kiro_code_intelligence=True)
        options.agent_engine = "v2"
        with self.assertRaisesRegex(ValueError, "Kiro V3"):
            await KiroSDKClient(options).connect()
        options.agent_engine = "v3"
        async with KiroSDKClient(options) as client:
            options.enable_kiro_code_intelligence = False
            with self.assertRaisesRegex(ValueError, "Reconnect"):
                await client.get_kiro_code_intelligence_status()
            with self.assertRaisesRegex(ValueError, "Reconnect"):
                await client.query("hello")
            options.enable_kiro_code_intelligence = 1
            with self.assertRaisesRegex(ValueError, "Reconnect"):
                await client.get_kiro_code_intelligence_status()
            options.enable_kiro_code_intelligence = True
            self.assertTrue((await client.get_kiro_code_intelligence_status())["success"])

    async def test_control_requires_an_idle_connected_session(self):
        client = KiroSDKClient(self.options(enable_kiro_code_intelligence=True))
        with self.assertRaises(CLIConnectionError):
            await client.get_kiro_code_intelligence_status()
        async with client:
            await client.query("hello")
            with self.assertRaisesRegex(CLIConnectionError, "Consume"):
                await client.initialize_kiro_code_intelligence()
            _ = [message async for message in client.receive_response()]
            self.assertTrue((await client.initialize_kiro_code_intelligence())["success"])

    async def test_interrupted_initialization_invalidates_connection(self):
        options = self.options(enable_kiro_code_intelligence=True, startup_timeout=1.5)
        options.env["FAKE_CODE_INIT_HANG"] = "1"
        async with KiroSDKClient(options) as client:
            with self.assertRaises(asyncio.TimeoutError):
                await client.initialize_kiro_code_intelligence()
            self.assertIsNotNone(client._reader_error)
            with self.assertRaises(CLIConnectionError):
                await client.query("hello")

    async def test_invalid_initialization_response_invalidates_connection(self):
        options = self.options(enable_kiro_code_intelligence=True)
        options.env["FAKE_CODE_INVALID_RESPONSE"] = "1"
        async with KiroSDKClient(options) as client:
            with self.assertRaisesRegex(ProtocolError, "invalid code intelligence"):
                await client.initialize_kiro_code_intelligence()
            self.assertIsNotNone(client._reader_error)
            with self.assertRaises(CLIConnectionError):
                await client.query("hello")

    async def test_canceled_initialization_invalidates_connection(self):
        options = self.options(enable_kiro_code_intelligence=True)
        options.env["FAKE_CODE_INIT_HANG"] = "1"
        async with KiroSDKClient(options) as client:
            task = asyncio.create_task(client.initialize_kiro_code_intelligence())
            try:
                for _ in range(100):
                    if any(item["method"] == "_kiro/codeIntelligence" and
                           item["params"].get("subcommand") == "init" for item in self.requests()):
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("Initialization request was not sent")
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNotNone(client._reader_error)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
