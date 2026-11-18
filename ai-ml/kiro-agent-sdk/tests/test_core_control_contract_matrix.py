"""Exact ACP request, result and connection guards for core control wrappers."""
import json
import unittest

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


CONTROLS = [
    ("list_kiro_permissions", (), {}, "_kiro/permissions/list", {"sessionId": "remote"}),
    ("explain_kiro_permission", ("fs.read", "/workspace/file"), {}, "_kiro/permissions/explain",
     {"sessionId": "remote", "capability": "fs.read", "resource": "/workspace/file"}),
    ("get_kiro_account_usage", (), {}, "_kiro/account/getUsage", {}),
    ("list_kiro_powers", (), {}, "_kiro/powers/list", {}),
    ("get_kiro_spec_task_statuses", (), {"tasks_file_path": "/workspace/tasks.md",
      "feature_name": "fixture", "workspace_paths": ["/workspace"]}, "_kiro/spec/getTaskStatuses",
     {"tasksFilePath": "/workspace/tasks.md", "featureName": "fixture", "workspacePaths": ["/workspace"]}),
    ("list_kiro_context", (), {}, "_kiro/session/context", {"sessionId": "remote", "subcommand": "show"}),
    ("add_kiro_context", ("/workspace/file",), {"force": True}, "_kiro/session/context",
     {"sessionId": "remote", "subcommand": "add", "path": "/workspace/file", "force": True}),
    ("remove_kiro_context", ("/workspace/file",), {}, "_kiro/session/context",
     {"sessionId": "remote", "subcommand": "remove", "path": "/workspace/file"}),
    ("clear_kiro_context", (), {}, "_kiro/session/context", {"sessionId": "remote", "subcommand": "clear"}),
    ("clear_steering", (), {}, "_session/steer/clear", {"sessionId": "remote"}),
    ("rename_session", ("new title",), {}, "_kiro/session/rename", {"sessionId": "remote", "title": "new title"}),
    ("extension_request", ("_kiro/example", {"explicit": True}), {}, "_kiro/example",
     {"explicit": True}),
]


class CoreControlTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        if "id" not in message:
            self.requests.append(message)
        elif message.get("method") in {case[3] for case in CONTROLS}:
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"],
                                 "result": {"received": message["params"], "futureField": ["preserved"]}})
        else:
            await super().write(data)


class CoreControlContractMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_request_and_returned_object_for_each_control(self):
        transport = CoreControlTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            for name, args, kwargs, method, parameters in CONTROLS:
                with self.subTest(method=name):
                    result = await getattr(client, name)(*args, **kwargs)
                    self.assertEqual(transport.requests[-1]["method"], method)
                    self.assertEqual(transport.requests[-1]["params"], parameters)
                    self.assertEqual(result, {"received": parameters, "futureField": ["preserved"]})

    async def test_each_control_rejects_before_connect_and_after_close_without_dispatch(self):
        transport = CoreControlTransport()
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport)
        for phase in ("before", "after"):
            if phase == "after":
                await client.connect()
                await client.disconnect()
            for name, args, kwargs, _, _ in CONTROLS:
                with self.subTest(method=name, phase=phase):
                    before = len(transport.requests)
                    with self.assertRaises((CLIConnectionError, ProtocolError)):
                        await getattr(client, name)(*args, **kwargs)
                    self.assertEqual(len(transport.requests), before)

    async def test_each_control_rejects_unsupported_engine_or_extension_namespace(self):
        transport = CoreControlTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v2"), transport=transport) as client:
            for name, args, kwargs, _, _ in CONTROLS:
                with self.subTest(method=name):
                    before = len(transport.requests)
                    if name == "extension_request":
                        with self.assertRaises(ValueError):
                            await client.extension_request("session/prompt", {})
                    else:
                        with self.assertRaises(ProtocolError):
                            await getattr(client, name)(*args, **kwargs)
                    self.assertEqual(len(transport.requests), before)

    async def test_void_operations_return_none_and_disconnect_is_idempotent(self):
        for _ in range(2):
            # A fresh peer is required after FakeTransport.close sends EOF.
            transport = CoreControlTransport()
            client = KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport)
            self.assertIsNone(await client.connect())
            self.assertIsNone(await client.query("protocol echo"))
            self.assertEqual([message async for message in client.receive_response()][-1].result,
                             "protocol echo")
            self.assertIsNone(await client.interrupt())
            self.assertIsNone(await client.disconnect())
            self.assertIsNone(await client.disconnect())


if __name__ == "__main__":
    unittest.main()
