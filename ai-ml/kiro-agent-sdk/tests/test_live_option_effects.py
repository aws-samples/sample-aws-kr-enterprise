"""Opt-in real-Kiro checks that non-default KiroAgentOptions change what the real Kiro CLI does; these consume credits.

Every case sets the option to a non-default value and asserts an effect observed through the real
Kiro CLI (a file written or refused, a callback skipped or consulted, a value Kiro confirmed).
A case whose required Kiro behaviour is not reached is skipped (BLOCKED_PREREQUISITE), never PASS.
"""
import asyncio
import contextlib
import glob
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

from kiro_agent_sdk import (KiroSDKError, CLIConnectionError, InMemorySessionStore,
                            KiroAgentOptions, KiroSDKClient, PermissionResultAllow, PermissionResultDeny,
                            ProtocolError,
                            ResultMessage, SessionArchiveError, create_mcp_server, delete_session, list_sessions, tool)

WRITE_TITLE = "Write File"
MODEL = "claude-sonnet-5.5"
RUNTIMES = glob.glob(os.path.expanduser(
    "~/.local/share/kiro-cli/kas/2.35.1-*/node_modules/@kiro/agent/dist/server/acp-server.js"))


def isolated_env(root: Path) -> dict:
    return {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}


def config_value(client, identifier):
    return next((item.get("currentValue") for item in client.session_info.get("configOptions", [])
                 if item.get("id") == identifier), None)


def allow_all():
    async def decide(name, data, context):
        return PermissionResultAllow()
    return decide


def write_consent(context):
    consent = context.kiro_consent or {}
    return consent if consent.get("capability") == "fs_write" else None


@unittest.skipUnless(os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli"),
                     "set KIRO_SDK_INTEGRATION=1 with an authenticated Kiro CLI")
class LiveOptionEffectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-live-options-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.env = isolated_env(self.root)
        self.sessions = []

    async def asyncTearDown(self):
        for session_id, cwd in self.sessions:
            with contextlib.suppress(Exception):
                await delete_session(session_id, cwd=cwd, env=self.env)

    def options(self, **values):
        base = dict(cwd=self.cwd, env=self.env, agent_engine="v3", turn_timeout=150)
        base.update(values)
        return KiroAgentOptions(**base)

    def write_options(self, ask_paths=None, **values):
        rule = {"capability": "fs_write", "effect": "ask"}
        if ask_paths:
            rule["match"] = [str(path) for path in ask_paths]
        return self.options(tools=["write"], kiro_permissions={"rules": [rule]}, **values)

    async def turn(self, client, prompt):
        if client.session_id and (client.session_id, self.cwd) not in self.sessions:
            self.sessions.append((client.session_id, self.cwd))
        await client.query(prompt)
        messages = [message async for message in client.receive_response()]
        return next(message for message in messages if isinstance(message, ResultMessage))

    async def write(self, client, path, text):
        return await self.turn(client, f"Use the write tool to write exactly {text} to {path}. "
                                       "Do not use any other tool or file. If the write is refused, stop.")

    async def test_add_dirs_makes_kiro_treat_an_outside_directory_as_a_workspace_root(self):
        extra = self.root / "extra"
        extra.mkdir()
        marker = "ADDDIR" + uuid.uuid4().hex[:10].upper()
        (extra / "note.txt").write_text(marker + "\n")
        roots = []

        async def decide(name, data, context):
            consent = context.kiro_consent or {}
            roots.append(consent.get("workspaceRoot"))
            # Approve only reads that Kiro attributes to the added directory.
            return (PermissionResultAllow() if consent.get("workspaceRoot") == str(extra)
                    else PermissionResultDeny(message="not approved by the test"))

        prompt = (f"Use the read tool to read {extra / 'note.txt'} and reply with its exact contents. "
                  "Do not use any other tool. If reading is refused, reply REFUSED.")
        async with KiroSDKClient(self.options(tools=["read"], can_use_tool=decide)) as client:
            refused = await self.turn(client, prompt)
        if not roots:
            self.skipTest("Kiro read the outside file without a permission request")
        self.assertNotIn(str(extra), roots)
        self.assertNotIn(marker, refused.result)
        roots.clear()
        async with KiroSDKClient(self.options(tools=["read"], can_use_tool=decide, add_dirs=[str(extra)])) as client:
            result = await self.turn(client, prompt)
        self.assertIn(str(extra), roots)
        self.assertIn(marker, result.result)

    def runtime(self):
        if len(RUNTIMES) != 1:
            self.skipTest("the installed Kiro 2.35.1 runtime was not found exactly once")
        return RUNTIMES[0]

    async def test_verbatim_prompts_skip_kiro_steering_context_for_new_turns(self):
        marker = "STEERING" + uuid.uuid4().hex[:10].upper()
        steering = self.cwd / ".kiro" / "steering" / "code.md"
        steering.parent.mkdir(parents=True)
        steering.write_text(f"---\ninclusion: always\n---\nThe steering code word is {marker}.\n")
        prompt = "What is the steering code word? Reply with the code word only, or NONE. Use no tools."
        runtime = self.runtime()
        async with KiroSDKClient(self.options(runtime_compat_mode="v3", native_hook_runtime_path=runtime,
                                              tools=[])) as client:
            composed = await self.turn(client, prompt)
        if marker not in composed.result:
            self.skipTest("Kiro did not add the workspace steering context without verbatim mode")
        async with KiroSDKClient(self.options(runtime_compat_mode="v3", native_hook_runtime_path=runtime,
                                              tools=[], verbatim_prompts=True)) as client:
            literal = await self.turn(client, prompt)
        self.assertNotIn(marker, literal.result)

    async def test_session_id_option_creates_the_requested_kiro_session(self):
        requested = str(uuid.uuid4())
        async with KiroSDKClient(self.options(session_id=requested, tools=[])) as client:
            self.assertEqual(client.session_id, requested)
            await self.turn(client, "Reply with exactly OK. Use no tools.")
        listed = await list_sessions(cwd=self.cwd, env=self.env)
        self.assertIn(requested, [getattr(item, "session_id", None) or item.get("session_id") for item in listed])

    async def test_kiro_file_resource_reaches_the_model_context(self):
        marker = "RESOURCE" + uuid.uuid4().hex[:10].upper()
        resource = self.root / "resources" / "context.md"
        resource.parent.mkdir()
        resource.write_text(f"The project code word is {marker}.\n")
        options = self.options(tools=[], kiro_resources=[resource.as_uri()])
        async with KiroSDKClient(options) as client:
            result = await self.turn(client, "What is the project code word from your context resources? "
                                             "Reply with the code word only. Use no tools.")
        self.assertIn(marker, result.result)

    async def test_small_max_buffer_size_rejects_real_kiro_messages(self):
        with self.assertRaisesRegex(ProtocolError, "max_buffer_size"):
            async with KiroSDKClient(self.options(tools=[], max_buffer_size=1024)):
                pass
        async with KiroSDKClient(self.options(tools=[], max_buffer_size=1024 * 1024)) as client:
            self.sessions.append((client.session_id, self.cwd))
            self.assertTrue(client.session_id)

    def archives(self, store):
        return [entry for rows in store._entries.values() for entry in rows if entry["type"] == "session_archive"]

    async def test_archive_size_limits_bound_a_real_kiro_session_archive(self):
        store = InMemorySessionStore()
        prompt = "Reply with exactly OK. Use no tools."
        async with KiroSDKClient(self.options(tools=[], session_store=store, session_store_archive=True,
                                              session_store_archive_max_bytes=1024)) as client:
            self.sessions.append((client.session_id, self.cwd))
            await client.query(prompt)
            with self.assertRaisesRegex(SessionArchiveError, "size limit"):
                _ = [message async for message in client.receive_response()]
        self.assertEqual(self.archives(store), [])
        async with KiroSDKClient(self.options(tools=[], session_store=store, session_store_archive=True,
                                              session_store_archive_max_bytes=4 * 1024 * 1024)) as client:
            source_id = client.session_id
            self.sessions.append((source_id, self.cwd))
            await self.turn(client, prompt)
        self.assertEqual(len(self.archives(store)), 1)
        target = self.root / "target"
        target.mkdir()
        values = dict(cwd=target, tools=[], session_store=store, resume=source_id, resume_from_store=True,
                      session_store_source_directory=str(self.cwd))
        client = KiroSDKClient(self.options(**values, session_store_archive_max_uncompressed_bytes=1))
        try:
            with self.assertRaises(SessionArchiveError):
                await client.connect()
            self.assertIsNone(client.restored_session_id)
        finally:
            await client.disconnect()
        async with KiroSDKClient(self.options(**values, session_store_archive_max_uncompressed_bytes=32 * 1024 * 1024)) as client:
            self.assertTrue(client.restored_session_id)
            self.sessions.append((client.restored_session_id, target))

    async def test_buffered_stream_input_limits_apply_to_a_real_kiro_connection(self):
        def message(text):
            return text

        async def source(words):
            for word in words:
                yield message(f"Reply with exactly {word}. Use no tools.")

        words = ("ONE", "TWO", "THREE")
        limited = self.options(tools=[], stream_input_mode="buffered", stream_input_buffer_max_messages=2,
                               stream_input_buffer_max_bytes=4096, stream_input_buffer_timeout=20.0)
        client = KiroSDKClient(limited)
        await client.connect()
        try:
            self.sessions.append((client.session_id, self.cwd))
            with self.assertRaisesRegex(ValueError, "message limit"):
                await client.query(source(words))
            self.assertFalse(client._connected)
        finally:
            await client.disconnect()
        options = self.options(tools=[], stream_input_mode="buffered", stream_input_buffer_max_messages=3,
                               stream_input_buffer_max_bytes=4096, stream_input_buffer_timeout=20.0)
        async with KiroSDKClient(options) as client:
            self.sessions.append((client.session_id, self.cwd))
            await client.query(source(words))
            results = []
            async for item in client.receive_messages():
                if isinstance(item, ResultMessage):
                    results.append(item.result)
                    if len(results) == len(words):
                        break
        self.assertEqual([word for word, text in zip(words, results) if word in text], list(words))

    async def test_sdk_mcp_transform_timeouts_bound_real_kiro_tool_calls(self):
        executed, secret = [], "OUTPUT" + uuid.uuid4().hex[:10].upper()

        @tool("guarded", "Return a value", {})
        async def guarded(args):
            executed.append("guarded")
            return {"content": [{"type": "text", "text": "INPUT-EXECUTED"}]}

        @tool("filtered", "Return a value", {})
        async def filtered(args):
            executed.append("filtered")
            return {"content": [{"type": "text", "text": secret}]}

        async def slow_input(server, name, arguments):
            await asyncio.sleep(10)
            return arguments

        async def slow_output(server, name, arguments, result):
            await asyncio.sleep(10)
            return result

        options = self.options(tools=["@mcp"], permission_mode="trustAll",
                               mcp_servers={"inbound": create_mcp_server("inbound", tools=[guarded]),
                                            "outbound": create_mcp_server("outbound", tools=[filtered])},
                               sdk_mcp_input_transforms={"inbound": slow_input}, sdk_mcp_input_timeout=0.5,
                               sdk_mcp_output_transforms={"outbound": slow_output}, sdk_mcp_output_timeout=0.5)
        async with KiroSDKClient(options) as client:
            result = await self.turn(client, "Call the inbound guarded tool once, then the outbound filtered tool once. "
                                             "Report each tool's exact output or error. Use no other tools.")
        if "filtered" not in executed:
            self.skipTest("model did not call the outbound tool")
        self.assertNotIn("guarded", executed)
        self.assertNotIn("INPUT-EXECUTED", result.result)
        self.assertNotIn(secret, result.result)

    async def test_session_store_flush_and_archive_timeouts_bound_real_kiro_turns(self):
        class SlowStore(InMemorySessionStore):
            async def append(self, key, entries):
                await asyncio.sleep(10)
                return await super().append(key, entries)

        prompt = "Reply with exactly OK. Use no tools."
        async with KiroSDKClient(self.options(tools=[], session_store=SlowStore(),
                                              session_store_flush_timeout=0.5)) as client:
            self.sessions.append((client.session_id, self.cwd))
            started = asyncio.get_running_loop().time()
            with self.assertRaises((KiroSDKError, asyncio.TimeoutError)):
                await self.turn(client, prompt)
            self.assertLess(asyncio.get_running_loop().time() - started, 120)
        store = InMemorySessionStore()
        async with KiroSDKClient(self.options(tools=[], session_store=store, session_store_archive=True,
                                              session_store_archive_timeout=0.001)) as client:
            self.sessions.append((client.session_id, self.cwd))
            with self.assertRaises((KiroSDKError, asyncio.TimeoutError)):
                await self.turn(client, prompt)
        self.assertEqual(self.archives(store), [])

    async def test_load_timeout_bounds_a_real_kiro_session_load(self):
        async with KiroSDKClient(self.options(tools=[])) as client:
            session_id = client.session_id
            self.sessions.append((session_id, self.cwd))
            await self.turn(client, "Reply with exactly OK. Use no tools.")
        client = KiroSDKClient(self.options(tools=[], resume=session_id, load_timeout_ms=1))
        try:
            with self.assertRaises((CLIConnectionError, asyncio.TimeoutError)):
                await client.connect()
        finally:
            await client.disconnect()
        async with KiroSDKClient(self.options(tools=[], resume=session_id, load_timeout_ms=60000)) as client:
            self.assertEqual(client.session_id, session_id)

    # Process account ---------------------------------------------------------------------------------

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires an unprivileged POSIX account")
    async def test_process_user_runs_kiro_as_the_selected_account_and_refuses_another(self):
        import pwd
        account = pwd.getpwuid(os.geteuid()).pw_name
        options = self.options(tools=["shell"], can_use_tool=allow_all(), user=account)
        async with KiroSDKClient(options) as client:
            result = await self.turn(client, "Run exactly this shell command once and reply with its raw output "
                                             "only: id -u")
        self.assertIn(str(os.geteuid()), result.result)
        try:
            other = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no nobody account to verify refusal")
        client = KiroSDKClient(self.options(tools=[], user=other.pw_name))
        try:
            with self.assertRaises(PermissionError):
                await client.connect()
            self.assertIsNone(client._process)
        finally:
            await client.disconnect()


if __name__ == "__main__":
    unittest.main()
