"""Explicit request/result/lifetime matrices for advertised Kiro extensions."""
import json
import unittest

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


CASES = [
    ("list_kiro_memories", (), {"limit": 2, "scopes": ["Global"]}, "_kiro/memory/list",
     {"limit": 2, "scopes": ["Global"]}, (), {"limit": 0}),
    ("get_kiro_memory", ("memory",), {}, "_kiro/memory/get", {"id": "memory"}, ("",), {}),
    ("create_kiro_memory", (), {"memory_type": "SEMANTIC", "scopes": ["Global"], "title": "Title",
                               "summary": "Summary", "content": "Content"}, "_kiro/memory/create",
     {"memoryType": "SEMANTIC", "scopes": ["Global"], "title": "Title", "summary": "Summary", "content": "Content"},
     (), {"memory_type": "SEMANTIC", "scopes": [], "title": "Title", "summary": "Summary", "content": "Content"}),
    ("update_kiro_memory", ("memory",), {"title": "Updated"}, "_kiro/memory/update",
     {"id": "memory", "newTitle": "Updated"}, ("memory",), {}),
    ("delete_kiro_memory", ("memory",), {}, "_kiro/memory/delete", {"id": "memory"}, ("",), {}),
    ("get_kiro_memory_sync_status", (), {}, "_kiro/memory/getSyncStatus", {}, (), {"unknown": True}),
    ("list_kiro_workflows", (), {}, "_kiro/workflow/list", {"sessionId": "remote"},
     (), {"workspace_paths": ["relative"]}),
    ("create_kiro_workflow", (), {"workflow": {"name": "fixture"}, "inputs": {"task": "inspect"}},
     "_kiro/workflow/new", {"parentSessionId": "remote", "workflow": {"name": "fixture"}, "inputs": {"task": "inspect"}},
     (), {"workflow": {}, "workflow_path": "bundled://fixture"}),
    *[(name, ("workflow",), {}, "_kiro/workflow/" + action, {"workflowId": "workflow"}, ("",), {})
      for name, action in [
          ("inspect_kiro_workflow", "inspect"), ("run_kiro_workflow", "invoke"),
          ("pause_kiro_workflow", "pause"), ("resume_kiro_workflow", "resume"),
          ("cancel_kiro_workflow", "cancel"), ("delete_kiro_workflow", "delete")]],
]


class ExtensionMatrixTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        method = message.get("method", "")
        if method == "initialize":
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1, "agentCapabilities": {"loadSession": True, "_meta": {
                    "kiro": {"extensionMethods": [case[3] for case in CASES]}}}}})
        elif method in {case[3] for case in CASES}:
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                "echo": message["params"], "futureField": {"preserved": True},
                "workflowId": "workflow", "ok": True}})
        else:
            await super().write(data)


class PublicExtensionMethodContracts(unittest.IsolatedAsyncioTestCase):
    async def test_normal_request_and_result_for_each_declared_method(self):
        transport = ExtensionMatrixTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            for name, args, kwargs, rpc, parameters, _, _ in CASES:
                with self.subTest(method=name):
                    result = await getattr(client, name)(*args, **kwargs)
                    self.assertEqual(transport.requests[-1]["method"], rpc)
                    self.assertEqual(transport.requests[-1]["params"], parameters)
                    self.assertEqual(result["echo"], parameters)
                    self.assertEqual(result["futureField"], {"preserved": True})

    async def test_invalid_arguments_do_not_reach_the_transport_for_each_method(self):
        transport = ExtensionMatrixTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            for name, _, _, _, _, args, kwargs in CASES:
                with self.subTest(method=name):
                    before = len(transport.requests)
                    with self.assertRaises((TypeError, ValueError)):
                        await getattr(client, name)(*args, **kwargs)
                    self.assertEqual(len(transport.requests), before)

    async def test_before_connect_and_after_close_are_guarded_for_each_method(self):
        transport = ExtensionMatrixTransport()
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport)
        for phase in ("before", "after"):
            if phase == "after":
                await client.connect()
                await client.disconnect()
            for name, args, kwargs, _, _, _, _ in CASES:
                with self.subTest(method=name, phase=phase):
                    before = len(transport.requests)
                    with self.assertRaises((CLIConnectionError, ProtocolError)):
                        await getattr(client, name)(*args, **kwargs)
                    self.assertEqual(len(transport.requests), before)

    async def test_unadvertised_methods_are_denied_without_dispatch(self):
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            for name, args, kwargs, _, _, _, _ in CASES:
                with self.subTest(method=name):
                    before = len(transport.requests)
                    with self.assertRaises(ProtocolError):
                        await getattr(client, name)(*args, **kwargs)
                    self.assertEqual(len(transport.requests), before)


if __name__ == "__main__":
    unittest.main()
