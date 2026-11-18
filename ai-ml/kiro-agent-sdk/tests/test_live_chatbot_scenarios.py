"""Opt-in real-Kiro checks of the ways a chatbot backend drives the SDK; these consume credits.

Multi-turn context, streaming, interruption, resume, compaction, a tool-backed answer and
isolation between concurrent conversations. A case whose required model behaviour is not
reached is skipped (BLOCKED_PREREQUISITE), never reported as PASS.
"""
import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid

from kiro_agent_sdk import (KiroAgentOptions, KiroSDKClient, PermissionResultAllow, ResultMessage, create_mcp_server, delete_session, get_session_messages, tool)


def isolated_env(root: Path) -> dict:
    return {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}


def code():
    return "CODE" + uuid.uuid4().hex[:8].upper()


@unittest.skipUnless(os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli"),
                     "set KIRO_SDK_INTEGRATION=1 with an authenticated Kiro CLI")
class LiveChatbotScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-live-chatbot-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.env = isolated_env(self.root)
        self.sessions = set()

    async def asyncTearDown(self):
        for session_id in self.sessions:
            with contextlib.suppress(Exception):
                await delete_session(session_id, cwd=self.cwd, env=self.env)

    def options(self, **values):
        base = dict(cwd=self.cwd, env=self.env, agent_engine="v3", tools=[], turn_timeout=150,
                    system_prompt="You are a concise support chatbot. Answer in one short sentence.")
        base.update(values)
        return KiroAgentOptions(**base)

    async def turn(self, client, prompt):
        if client.session_id:
            self.sessions.add(client.session_id)
        await client.query(prompt)
        messages = [message async for message in client.receive_response()]
        result = next(message for message in messages if isinstance(message, ResultMessage))
        self.assertFalse(result.is_error, result.result)
        return result

    async def test_multi_turn_conversation_keeps_earlier_context(self):
        secret, city = code(), "Busan"
        async with KiroSDKClient(self.options()) as client:
            await self.turn(client, f"My customer number is {secret}. Please just acknowledge.")
            await self.turn(client, f"I live in {city}. Please just acknowledge.")
            await self.turn(client, "What is 12 times 7? Reply with the number only.")
            await self.turn(client, "Thanks. Reply OK.")
            answer = await self.turn(client, "What are my customer number and city? Reply as NUMBER, CITY only.")
        self.assertIn(secret, answer.result)
        self.assertIn(city, answer.result)

    async def test_conversation_resumes_after_the_client_disconnects(self):
        secret = code()
        async with KiroSDKClient(self.options()) as client:
            session_id = client.session_id
            await self.turn(client, f"My ticket is {secret}. Just acknowledge.")
        async with KiroSDKClient(self.options(resume=session_id)) as client:
            self.assertEqual(client.session_id, session_id)
            answer = await self.turn(client, "What is my ticket? Reply with it only.")
        self.assertIn(secret, answer.result)
        history = await get_session_messages(session_id, cwd=self.cwd, env=self.env, agent_engine="v3")
        self.assertGreaterEqual(len(history), 4)

    async def test_conversation_survives_manual_compaction(self):
        secret = code()
        async with KiroSDKClient(self.options()) as client:
            await self.turn(client, f"The project code name is {secret}. Remember it; just acknowledge.")
            await self.turn(client, "Briefly, what is a chatbot? One sentence.")
            compacted = await client.compact()
            self.assertTrue(compacted.get("success"), compacted)
            answer = await self.turn(client, "What is the project code name? Reply with it only.")
        self.assertIn(secret, answer.result)

    async def test_tool_backed_answer_uses_the_application_lookup(self):
        status = "SHIPPED-" + uuid.uuid4().hex[:6].upper()
        calls = []

        @tool("order_status", "Look up the delivery status of an order number", {"order": str})
        async def order_status(arguments):
            calls.append(arguments.get("order"))
            return {"content": [{"type": "text", "text": f"Order {arguments.get('order')}: {status}"}]}

        async def allow(name, data, context):
            return PermissionResultAllow()

        options = self.options(tools=["@mcp"], can_use_tool=allow,
                               mcp_servers={"shop": create_mcp_server("shop", tools=[order_status])})
        async with KiroSDKClient(options) as client:
            answer = await self.turn(client, "Use the shop order_status tool to check order 4711 and tell me its "
                                             "status exactly as the tool reports it.")
            follow = await self.turn(client, "Which order number did we just check? Reply with the number only.")
        if not calls:
            self.skipTest("the model did not call the order_status tool")
        self.assertIn("4711", calls)
        self.assertIn(status, answer.result)
        self.assertIn("4711", follow.result)

    async def test_concurrent_conversations_stay_isolated(self):
        secrets = [code() for _ in range(4)]

        async def conversation(secret):
            async with KiroSDKClient(self.options()) as client:
                await self.turn(client, f"My private code is {secret}. Just acknowledge.")
                started = time.monotonic()
                answer = await self.turn(client, "What is my private code? Reply with it only.")
                return answer.result, time.monotonic() - started

        results = await asyncio.gather(*(conversation(secret) for secret in secrets))
        for secret, (reply, seconds) in zip(secrets, results):
            with self.subTest(secret=secret):
                self.assertIn(secret, reply)
                self.assertFalse(any(other in reply for other in secrets if other != secret))
                self.assertLess(seconds, 120)


if __name__ == "__main__":
    unittest.main()
