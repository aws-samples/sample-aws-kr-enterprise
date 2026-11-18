"""MCP startup policy regression specifications; execution is deferred."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions
from kiro_agent_sdk.runtime_elicitation import JAVASCRIPT as ELICITATION
from kiro_agent_sdk.runtime_mcp_connection import JAVASCRIPT as CONNECTION
from kiro_agent_sdk.runtime_mcp_bootstrap import JAVASCRIPT as BOOTSTRAP

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


@unittest.skipUnless(os.name == "posix", "requires local POSIX runtime")
class McpBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"fixture")
        self.source.with_name("xhr-sync-worker.js").write_bytes(b"worker")

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3", tools=[],
            runtime_compat_mode="v3", native_hook_runtime_path=self.source,
            mcp_elicitation_hook_mode="sdk", mcp_elicitation_runtime_mode="v3",
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_RUNTIME_POLICIES": "1"})
        values.update(changes)
        return KiroAgentOptions(**values)

    def test_early_clients_get_policy_before_transport_without_reconnection_or_replay(self):
        node = Path.home() / ".local/share/kiro-cli/node"
        executable = str(node) if node.is_file() else shutil.which("node")
        if not executable:
            self.skipTest("requires Node")
        script = r'''
import assert from "node:assert/strict";
import {AsyncLocalStorage as KiroSdkPolicyStorage} from "node:async_hooks";
const kiroSdkExecutionContext=new KiroSdkPolicyStorage();
const frames=[];
const fm=async(_connection,method,params)=>{frames.push({method,params});return {roots:[]}};
const kiroSdkSessionConnection={extNotification:()=>{}};
''' + ELICITATION + CONNECTION + BOOTSTRAP.replace(
            "const kiroSdkBootMcpPolicy=false;", "const kiroSdkBootMcpPolicy=true;") + r'''
const order=[],handlers=new Map();
const makeClient=()=>({
    transport:undefined,
    registerCapabilities(capabilities){
        assert.equal(this.transport,undefined);
        this.capabilities=capabilities;order.push("capabilities");
    },
    setRequestHandler(method,handler){handlers.set(method,handler);order.push("handler:"+method)}
});
const owner={serverName:"policy",mcpClient:makeClient(),disposed:false,isConnected:false,connectPromise:null};
const denied={serverName:"other",mcpClient:makeClient(),disposed:false,isConnected:false,connectPromise:null};
kiroSdkRegisterMcpServerRequests(owner.mcpClient,owner.serverName,owner);
assert.equal(handlers.size,0);
const controller=new AbortController();
const opening=(async()=>{
    await kiroSdkAwaitMcpPolicy(controller.signal);
    kiroSdkPrepareMcpTransport(owner,controller.signal);
    order.push("transport");owner.mcpClient.transport={};owner.isConnected=true;
})();
await Promise.resolve();
assert.deepEqual(order,[]);
assert.equal(kiroSdkMcpPolicyWaiters,1);
const cancelled=new AbortController();
const waiting=kiroSdkAwaitMcpPolicy(cancelled.signal);
const rejection=assert.rejects(waiting,/cancelled/);
cancelled.abort();await rejection;
assert.equal(kiroSdkMcpPolicyWaiting.size,1);
for(let index=0;index<100;index++){
    const cancel=new AbortController(),task=kiroSdkAwaitMcpPolicy(cancel.signal);
    const rejected=assert.rejects(task,/cancelled/);
    cancel.abort();await rejected;
}
assert.equal(kiroSdkMcpPolicyWaiting.size,1);
kiroSdkMcpPolicyWaiters=64;
await assert.rejects(kiroSdkAwaitMcpPolicy(new AbortController().signal),/unavailable/);
kiroSdkMcpPolicyWaiters=1;
const originalTimer=globalThis.setTimeout;
globalThis.setTimeout=callback=>originalTimer(callback,0);
try{
    await assert.rejects(kiroSdkAwaitMcpPolicy(new AbortController().signal),/not received/);
}finally{globalThis.setTimeout=originalTimer}
assert.equal(kiroSdkMcpPolicyWaiters,1);
kiroSdkInitializeElicitation({clientCapabilities:{_meta:{kiro:{sdkElicitation:{
    protocol:1,timeoutMs:5000,serverRequests:{servers:["policy"],roots:true,sampling:true,
        samplingMode:"tools",samplingMaxBytes:262144,connection:{id:"a".repeat(32),protocol:1}}
}}}}});
await opening;
assert.equal(kiroSdkMcpPolicyWaiters,0);
assert.equal(kiroSdkMcpPolicyWaiting.size,0);
assert.deepEqual(order,["capabilities","handler:roots/list","handler:sampling/createMessage","transport"]);
assert.deepEqual(owner.mcpClient.capabilities,{roots:{listChanged:false},sampling:{tools:{}}});
const result=await handlers.get("roots/list")({method:"roots/list",params:{}},{
    mcpReq:{signal:controller.signal}});
assert.deepEqual(result,{roots:[]});
assert.equal(frames.length,1);
kiroSdkPrepareMcpTransport(owner,controller.signal);
assert.equal(order.length,4);
kiroSdkPrepareMcpTransport(denied,controller.signal);
assert.equal(denied.mcpClient.capabilities,undefined);
assert.equal(order.length,4);
const alreadyConnected={serverName:"policy",mcpClient:makeClient(),disposed:false,isConnected:true,connectPromise:null};
alreadyConnected.mcpClient.transport={};
assert.throws(()=>kiroSdkPrepareMcpTransport(alreadyConnected,controller.signal),/retrofitted/);
assert.throws(()=>kiroSdkInitializeElicitation({clientCapabilities:{}}),/twice/);
const replaced=makeClient();
kiroSdkCloseMcpConnection(owner.mcpClient);
owner.mcpClient=replaced;owner.isConnected=false;
kiroSdkPrepareMcpTransport(owner,controller.signal);
assert.equal(order.filter(item=>item==="capabilities").length,2);
const disposed={serverName:"policy",mcpClient:makeClient(),disposed:true,isConnected:false};
assert.throws(()=>kiroSdkPrepareMcpTransport(disposed,controller.signal),/unavailable/);
assert.equal(frames.length,1);
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp-bootstrap.mjs"
            path.write_text(script)
            subprocess.run([executable, str(path)], check=True, capture_output=True, timeout=10)
