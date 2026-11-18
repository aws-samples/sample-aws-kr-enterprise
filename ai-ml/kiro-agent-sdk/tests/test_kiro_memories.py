import json
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


class MemoryTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        method = message.get("method", "")
        if method == "initialize":
            self.requests.append(message)
            methods = ["_kiro/memory/" + name for name in
                       ("list", "get", "create", "update", "delete", "getSyncStatus")]
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1, "agentCapabilities": {
                    "loadSession": True, "_meta": {"kiro": {"extensionMethods": methods}}}}})
        elif method.startswith("_kiro/memory/"):
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"],
                                  "result": {"observed": message["params"]}})
        else:
            await super().write(data)


class KiroMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_methods_forward_observed_request_shapes(self):
        transport = MemoryTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            self.assertEqual((await client.list_kiro_memories())["observed"], {})
            self.assertEqual((await client.list_kiro_memories(
                cursor="cursor", limit=2, scopes=["Global"]))["observed"],
                {"cursor": "cursor", "limit": 2, "scopes": ["Global"]})
            self.assertEqual((await client.get_kiro_memory("id"))["observed"], {"id": "id"})
            self.assertEqual((await client.create_kiro_memory(
                memory_type="SEMANTIC", scopes=["Global"], title="Title", summary="Summary",
                content="Content", unresolved_conflict=False))["observed"],
                {"memoryType": "SEMANTIC", "scopes": ["Global"], "title": "Title",
                 "summary": "Summary", "content": "Content", "unresolvedConflict": False})
            self.assertEqual((await client.update_kiro_memory(
                "id", title="New title", content="New content", unresolved_conflict=True))["observed"],
                {"id": "id", "newTitle": "New title", "content": "New content",
                 "unresolvedConflict": True})
            self.assertEqual((await client.delete_kiro_memory("id"))["observed"], {"id": "id"})
            self.assertEqual((await client.get_kiro_memory_sync_status())["observed"], {})

    async def test_invalid_requests_do_not_reach_server(self):
        transport = MemoryTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            calls = [
                lambda: client.list_kiro_memories(cursor=""),
                lambda: client.list_kiro_memories(limit=True),
                lambda: client.list_kiro_memories(scopes=[]),
                lambda: client.get_kiro_memory("\0"),
                lambda: client.create_kiro_memory(memory_type="SEMANTIC", scopes=[], title="T",
                                                  summary="S", content="C"),
                lambda: client.create_kiro_memory(memory_type="SEMANTIC", scopes=["Global"], title="T",
                                                  summary="S", content="C", unresolved_conflict=0),
                lambda: client.update_kiro_memory("id"),
                lambda: client.update_kiro_memory("id", content=" "),
                lambda: client.delete_kiro_memory(""),
                lambda: client.delete_kiro_memory(" id "),
            ]
            for call in calls:
                with self.subTest(call=call):
                    with self.assertRaises(ValueError):
                        await call()
        self.assertFalse(any(r["method"].startswith("_kiro/memory/") for r in transport.requests))

    async def test_unadvertised_memory_extensions_are_not_sent(self):
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            for call in (client.list_kiro_memories, lambda: client.get_kiro_memory("id"),
                         lambda: client.delete_kiro_memory("id"), client.get_kiro_memory_sync_status):
                with self.assertRaises(ProtocolError):
                    await call()
        self.assertFalse(any(r["method"].startswith("_kiro/memory/") for r in transport.requests))


if __name__ == "__main__":
    unittest.main()
