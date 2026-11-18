"""Observed semantics of changing options on a connected client, per option.

Guarded options reject the next prompt before it is sent until the connect-time value is restored.
Connect-time options keep the current Kiro session unchanged and apply on reconnect. Turn options
apply to the next turn.
"""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (AgentMessage, CLIConnectionError, HookMatcher, InMemorySessionStore,
                            KiroAgentOptions, KiroSDKClient, SessionArchiveError, SQLiteCreditLedger, TextBlock,
                            ThoughtBlock, create_mcp_server, tool)
from test_transport import FakeTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


async def callback(*args):
    return None


async def other(*args):
    return True


async def estimate(*args):
    return 10


@tool("probe", "Probe", {"value": str})
async def probe(args):
    return {"content": [{"type": "text", "text": args["value"]}]}


def archive_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("session.json", "{}")
        archive.writestr("messages.jsonl", '{"message":"hello"}\n' * 20)
    return output.getvalue()


class OptionChangeContractMatrix(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.log = self.root / "requests.jsonl"
        self.runtime = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_bytes(b"fixture")
        self.runtime.with_name("xhr-sync-worker.js").write_bytes(b"worker")
        patcher = patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched fixture")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.root / "source"
        self.source.mkdir()

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3", tools=[],
                      env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log),
                           "FAKE_RUNTIME_POLICIES": "1"})
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

    def guarded_cases(self):
        runtime = dict(runtime_compat_mode="v3", native_hook_runtime_path=self.runtime)
        auth = dict(kiro_auth_method="client", kiro_access_token_provider=callback,
                    kiro_client_auth_idle_mode="authorized", kiro_idle_token_authorizer=callback)
        sdk = {"probe": create_mcp_server("probe", tools=[probe])}
        store = InMemorySessionStore()
        return [
            ("kiro_access_token_timeout", auth, 3, "Kiro authentication"),
            ("kiro_idle_token_authorizer", auth, other, "Kiro authentication"),
            ("native_hook_runtime_path", runtime, self.root / "other.js", "native_hook_runtime_path"),
            ("native_hooks", dict(native_hook_bridge_mode="same-user",
                                  native_hooks={"postToolUse": [HookMatcher(hooks=[callback])]}),
             {"agentStop": [HookMatcher(hooks=[callback])]}, "native_hooks"),
            ("permission_mode", dict(can_use_tool=callback), "denyUnlisted", "set_permission_mode"),
            ("enable_file_checkpointing", dict(enable_file_checkpointing=True), False, "enable_file_checkpointing"),
            ("agent_engine", {}, "v2", "agent_engine"),
            ("session_store", dict(session_store=store), InMemorySessionStore(), "session_store"),
            ("session_store_flush", dict(session_store=store, session_store_flush="eager"), "batched",
             "session_store_flush"),
            ("session_store_error_policy", dict(session_store=store, session_store_error_policy="fail"), "continue",
             "session_store_error_policy"),
            ("reported_credit_ledger", dict(max_reported_credits=1, reported_credit_scope="team",
                                            reported_credit_ledger=SQLiteCreditLedger(str(self.root / "a.db"))),
             SQLiteCreditLedger(str(self.root / "b.db")), "credit ledger or scope"),
            ("resume_from_store_bounded_read", dict(session_store=store, resume="old", resume_from_store=True,
                                                    resume_from_store_bounded_read=True), False,
             "bounded mirror recovery"),
            ("sdk_mcp_input_timeout", dict(mcp_servers=sdk, sdk_mcp_input_transforms={"probe": callback}), 5,
             "SDK MCP input transforms"),
            ("sdk_mcp_output_timeout", dict(mcp_servers=sdk, sdk_mcp_output_transforms={"probe": callback}), 5,
             "SDK MCP output transforms"),
            ("stream_input_buffer_max_bytes", {}, 1024, "streaming input settings"),
            ("stream_input_buffer_timeout", {}, 1, "streaming input settings")
        ]

    async def test_guarded_options_reject_the_next_prompt_until_restored(self):
        restore = patch("kiro_agent_sdk.archive._restore_session_from_store", return_value="restored-id")
        restore.start()
        self.addCleanup(restore.stop)
        for name, base, value, message in self.guarded_cases():
            with self.subTest(option=name):
                options = self.options(**base)
                original = getattr(options, name)
                async with KiroSDKClient(options) as client:
                    sent = len(self.requests("session/prompt"))
                    setattr(options, name, value)
                    with self.assertRaisesRegex(ValueError, message):
                        await client.query("must not run")
                    self.assertEqual(len(self.requests("session/prompt")), sent)
                    setattr(options, name, original)
                    self.assertEqual((await self.turn(client))[-1].result, "hello")

    async def test_connect_time_options_keep_the_session_and_apply_after_reconnect(self):
        async def hook(*args):
            return {}
        cases = [
            ("kiro_policy_presets", dict(kiro_policy_presets=["read-workspace"]), [],
             lambda c: [r["params"].get("_meta", {}).get("kiro", {}).get("policyPreset")
                        for r in self.requests("session/new")][-1], ["read-workspace"], None),
            ("resume_session_at", dict(resume="old", fork_session=True, resume_session_at="m1"), "m2",
             lambda c: self.requests("session/fork")[-1]["params"]["_meta"]["kiro"]["messageId"], "m1", "m2"),
            ("mcp_env_mode", dict(env={**self.options().env, "MCP_EXEC": "/bin/example"}, mcp_env_mode="literal",
                                  mcp_servers={"local": {"command": "${MCP_EXEC}"}}), "expand",
             lambda c: self.requests("session/new")[-1]["params"]["mcpServers"][0]["command"],
             "${MCP_EXEC}", "/bin/example"),
            ("kiro_resources", dict(kiro_resources=["file:///tmp/a"]), ["file:///tmp/b"],
             lambda c: json.loads(c._agent_file.read_text())["resources"], ["file:///tmp/a"], ["file:///tmp/b"]),
        ]
        for name, base, value, read, before, after in cases:
            with self.subTest(option=name):
                self.log.unlink(missing_ok=True)
                options = self.options(**base)
                client = KiroSDKClient(options)
                try:
                    await client.connect()
                    self.assertEqual(read(client), before)
                    setattr(options, name, value)
                    self.assertEqual((await self.turn(client))[-1].result, "hello")
                    self.assertEqual(read(client), before)
                    await client.disconnect()
                    await client.connect()
                    self.assertEqual(read(client), after)
                finally:
                    await client.disconnect()

    async def test_connect_time_session_recovery_and_load_settings(self):
        options = self.options(resume="old", load_timeout_ms=5000)
        client = KiroSDKClient(options)
        try:
            await client.connect()
            options.load_timeout_ms = 100
            options.env["FAKE_LOAD_HANG"] = "1"
            self.assertEqual((await self.turn(client))[-1].result, "hello")
            await client.disconnect()
            with self.assertRaisesRegex(CLIConnectionError, "session/load"):
                await client.connect()
        finally:
            await client.disconnect()
        with patch("kiro_agent_sdk.archive.restore_session_from_store", return_value="restored-id") as restore:
            options = self.options(resume="source-id", session_store=InMemorySessionStore(), resume_from_store=True,
                                   session_store_source_directory=str(self.root))
            client = KiroSDKClient(options)
            try:
                await client.connect()
                self.assertEqual(restore.await_args.args[2], str(self.root))
                options.session_store_source_directory = str(self.source)
                await client.disconnect()
                await client.connect()
                self.assertEqual((client.session_id, restore.await_count), ("restored-id", 1))
                options.resume_from_store = False
                await client.disconnect()
                await client.connect()
                self.assertEqual((client.session_id, restore.await_count), ("source-id", 1))
            finally:
                await client.disconnect()

    async def test_turn_options_apply_to_the_next_turn(self):
        def thoughts(messages):
            return any(isinstance(block, ThoughtBlock) for message in messages
                       if isinstance(message, AgentMessage) for block in message.content)

        def child_text(messages):
            return any(isinstance(message, AgentMessage) and message.agent_id == "child-1" and
                       any(isinstance(block, TextBlock) and block.text == "CHILD_TEXT" for block in message.content)
                       for message in messages)
        options = self.options(env={**self.options().env, "FAKE_HOOK_EVENTS": "1"})
        async with KiroSDKClient(options) as client:
            self.assertFalse(thoughts(await self.turn(client, "thought")))
            options.include_thoughts = True
            self.assertTrue(thoughts(await self.turn(client, "thought")))
            self.assertFalse(child_text(await self.turn(client, "nested")))
            options.forward_subagent_text = True
            self.assertTrue(child_text(await self.turn(client, "nested")))

    async def test_session_store_flush_timeout_applies_to_the_next_turn(self):
        class Slow(InMemorySessionStore):
            async def append(self, key, entries):
                import asyncio
                await asyncio.sleep(0.5)
                await super().append(key, entries)
        options = KiroAgentOptions(agent_engine="v3", session_store=Slow(), session_store_flush_timeout=5)
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            await self.turn(client, "first")
            options.session_store_flush_timeout = 0.05
            with self.assertRaisesRegex(CLIConnectionError, "flushing"):
                await self.turn(client, "second")


    async def test_archive_settings_apply_to_the_next_mirror(self):
        data = archive_bytes()

        async def helper(arguments, cwd, cli_path, env):
            Path(arguments[arguments.index("--out") + 1]).write_bytes(data)
            return json.dumps({"kind": "exportSession", "data": {"path": "unused"}}).encode()

        async def hang(*args):
            import asyncio
            await asyncio.Event().wait()
        for name, value, effect, error in (
            ("session_store_archive_max_bytes", len(data) - 1, helper, "size limit"),
            ("session_store_archive_max_uncompressed_bytes", 10, helper, "expanded size limit"),
            ("session_store_archive_timeout", 0.05, hang, "Timed out"),
            ("session_store_archive", False, hang, None),
        ):
            with self.subTest(option=name):
                store = InMemorySessionStore()
                options = self.options(session_store=store, session_store_archive=True)
                client = KiroSDKClient(options)
                try:
                    with patch("kiro_agent_sdk.archive._run_cli", side_effect=helper):
                        await client.connect()
                        await self.turn(client, "first")
                    setattr(options, name, value)
                    with patch("kiro_agent_sdk.archive._run_cli", side_effect=effect):
                        if error is None:
                            self.assertEqual((await self.turn(client, "second"))[-1].result, "second")
                        else:
                            with self.assertRaisesRegex(SessionArchiveError, error):
                                await self.turn(client, "second")
                finally:
                    await client.disconnect()
                archives = [entry for rows in store._entries.values() for entry in rows
                            if entry["type"] == "session_archive"]
                self.assertEqual(len(archives), 1)


if __name__ == "__main__":
    unittest.main()
