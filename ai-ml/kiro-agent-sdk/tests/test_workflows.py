import json
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


class WorkflowTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        method = message.get("method", "")
        if method == "initialize":
            self.requests.append(message)
            methods = ["_kiro/workflow/" + name for name in
                       ("list", "new", "inspect", "invoke", "pause", "resume", "cancel", "delete")]
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1, "agentCapabilities": {
                    "loadSession": True, "_meta": {"kiro": {"extensionMethods": methods}}}}})
        elif method.startswith("_kiro/workflow/"):
            self.requests.append(message)
            result = ({"runs": [{"workflowId": "w1"}]} if method.endswith("/list") else
                      {"workflowId": "w1"} if method.endswith("/new") else {"ok": True})
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": result})
        else:
            await super().write(data)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_list_and_control_workflow(self):
        transport = WorkflowTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            listing = await client.list_kiro_workflows()
            self.assertEqual(listing["runs"][0]["workflowId"], "w1")
            await client.list_kiro_workflows(workspace_paths=["/workspace"])
            created = await client.create_kiro_workflow(workflow={"name": "test"}, inputs={"task": "inspect"})
            self.assertEqual(created["workflowId"], "w1")
            await client.create_kiro_workflow(workflow_path="bundled://test", model="model", effort="low")
            for operation in (client.inspect_kiro_workflow, client.run_kiro_workflow, client.pause_kiro_workflow,
                              client.resume_kiro_workflow, client.cancel_kiro_workflow, client.delete_kiro_workflow):
                self.assertTrue((await operation("w1"))["ok"])
        requests = {r["method"]: r["params"] for r in transport.requests if r["method"].startswith("_kiro/workflow/")}
        self.assertEqual(requests["_kiro/workflow/list"], {"workspacePaths": ["/workspace"]})
        self.assertEqual(requests["_kiro/workflow/new"], {"parentSessionId": "remote", "inputs": {},
                                                         "workflowPath": "bundled://test", "modelId": "model",
                                                         "effortLevel": "low"})
        self.assertEqual(requests["_kiro/workflow/cancel"], {"workflowId": "w1"})

    async def test_reject_invalid_workflow_arguments(self):
        transport = WorkflowTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            with self.assertRaises(ValueError):
                await client.create_kiro_workflow()
            with self.assertRaises(ValueError):
                await client.create_kiro_workflow(workflow={}, workflow_path="another")
            with self.assertRaises(ValueError):
                await client.create_kiro_workflow(workflow={"value": float("nan")})
            with self.assertRaises(ValueError):
                await client.list_kiro_workflows(workspace_paths=["relative"])
            with self.assertRaises(ValueError):
                await client.cancel_kiro_workflow("")
        self.assertFalse(any(r["method"].startswith("_kiro/workflow/") for r in transport.requests))

    async def test_unadvertised_extension_is_not_sent(self):
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            with self.assertRaises(ProtocolError):
                await client.list_kiro_workflows()
        self.assertFalse(any(r["method"].startswith("_kiro/workflow/") for r in transport.requests))

    async def test_stop_task_accepts_only_workflows_created_by_this_client(self):
        transport = WorkflowTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            with self.assertRaisesRegex(ProtocolError, "only attached SDK tasks"):
                await client.stop_task("unknown")
            created = await client.create_kiro_workflow(workflow={"name": "test"})
            self.assertIsNone(await client.stop_task(created["workflowId"]))
            self.assertTrue((await client.delete_kiro_workflow(created["workflowId"]))["ok"])
            with self.assertRaises(ProtocolError):
                await client.stop_task(created["workflowId"])
        cancellations = [r for r in transport.requests if r.get("method") == "_kiro/workflow/cancel"]
        self.assertEqual(len(cancellations), 1)
        self.assertEqual(cancellations[0]["params"], {"workflowId": "w1"})


if __name__ == "__main__":
    unittest.main()
