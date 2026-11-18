"""Each option accepts a valid value in an enabling configuration and rejects invalid values.

Previously accepted invalid values are covered by test_option_validation_fixes.py.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (HookMatcher, InMemorySessionStore, KiroAgentOptions, KiroSDKClient, McpOAuthConfig,
                            SQLiteCreditLedger)

FAKE = str(Path(__file__).with_name("fake_kiro.py"))
NUMBER_BAD = (0, -1, True, float("nan"), "1")
CALLBACK_BAD = ("callback", 1)


async def callback(*args):
    return None


class OptionInvalidContractMatrix(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_bytes(b"fixture")
        self.runtime.with_name("xhr-sync-worker.js").write_bytes(b"worker")
        self.v3 = dict(agent_engine="v3", cli_path=FAKE, cwd=str(self.root))
        self.runtime_v3 = dict(self.v3, runtime_compat_mode="v3", native_hook_runtime_path=self.runtime)
        dict(self.runtime_v3, mcp_elicitation_hook_mode="sdk", mcp_elicitation_runtime_mode="v3",
                        mcp_server_request_allowlist=["policy"])
        store = InMemorySessionStore()
        McpOAuthConfig("http://127.0.0.1:9999/callback", callback)
        self.construction = {
            "kiro_permissions": (self.v3, {"rules": [{"capability": "shell", "effect": "deny"}]},
                                 ("rules", ["rules"], {"rules": "bad"}, {"rules": [{"capability": "shell"}]})),
            "permission_mode": ({}, "denyUnlisted", ("unknown", 1, True, "default")),
            "kiro_shell_edit": (dict(self.v3, can_use_tool=callback), True, (1, "true", object())),
            "hooks": ({}, {"promptSubmit": [HookMatcher(hooks=[callback])]},
                      ("hooks", {"UserPromptSubmit": "x"}, {"promptSubmit": ["x"]},
                       {"Unknown": [HookMatcher(hooks=[callback])]})),
            "native_hooks": (dict(self.v3, native_hook_bridge_mode="same-user"), {"postToolUse": [HookMatcher()]},
                             ("hooks", {"PostToolUse": "x"}, {"Unknown": [HookMatcher()]})),
            "session_store_flush": ({"session_store": store}, "eager", ("unknown", 1, None)),
            "session_store_flush_timeout": ({"session_store": store}, 5, NUMBER_BAD),
            "max_reported_credits": (self.v3, 1.5, NUMBER_BAD),
            "reported_credit_ledger": (dict(self.v3, max_reported_credits=1, reported_credit_scope="team"),
                                       SQLiteCreditLedger(str(self.root / "one.db")), ("ledger", object())),
            "reported_credit_scope": (dict(self.v3, max_reported_credits=1,
                                           reported_credit_ledger=SQLiteCreditLedger(str(self.root / "two.db"))),
                                      "team", ("", 1)),
            "session_store_archive": (dict(self.v3, session_store=store), True, (1, "true")),
            "session_store_archive_max_uncompressed_bytes": (
                dict(self.v3, session_store=store, session_store_archive=True), 2 ** 20, (0, -1, True, "1")),
            "session_store_archive_timeout": (dict(self.v3, session_store=store, session_store_archive=True), 30,
                                              (0, -1, True, float("nan"))),
            "resume_from_store": (dict(self.v3, session_store=store, resume="old"), True, (1, "true")),
            "forward_subagent_text": (self.v3, True, (1, "true", None)),
            "on_user_input": (self.v3, callback, CALLBACK_BAD),
            "on_mcp_elicitation": (self.v3, callback, CALLBACK_BAD),
            "on_native_hook_system_message": (dict(self.v3, native_hook_bridge_mode="same-user",
                                                   native_hooks={"preToolUse": [HookMatcher()]}), callback, CALLBACK_BAD),
            "enable_file_checkpointing": (self.v3, True, (1, "true")),
            "env": ({}, {"A": "b"}, ("env", {"A": 1}, {1: "b"}, {"A\0": "b"}, {"A": "b\0"}, {"": "b"}, {"A=B": "c"})),
            "kiro_idle_token_authorizer": (dict(self.v3, kiro_auth_method="client", kiro_access_token_provider=callback,
                                                kiro_client_auth_idle_mode="authorized"), callback, CALLBACK_BAD)
        }

    async def test_connect_rejects_invalid_runtime_path_before_launch(self):
        log = self.root / "requests.jsonl"
        cases = [("native_hook_runtime_path", value, {"agent_engine": "v3", "runtime_compat_mode": "v3"})
                 for value in ("relative.js", str(self.root / "missing" / "acp-server.js"),
                               str(self.root / "node_modules/@kiro/agent/dist/server/other.js"))]
        for name, value, base in cases:
            with self.subTest(option=name, value=repr(value)[:30]):
                values = dict(cwd=self.root, cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)}, **base)
                values[name] = value
                client = KiroSDKClient(KiroAgentOptions(**values))
                try:
                    with self.assertRaises((ValueError, TypeError)):
                        await asyncio.wait_for(client.connect(), 10)
                    self.assertFalse(log.exists())
                finally:
                    await client.disconnect()
        client = KiroSDKClient(KiroAgentOptions(cwd=self.root, cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)}))
        async with client:
            self.assertTrue(log.exists())


if __name__ == "__main__":
    unittest.main()
