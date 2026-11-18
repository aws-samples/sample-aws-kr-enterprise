import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ProtocolError
from kiro_agent_sdk.runtime_policy import JAVASCRIPT as PROMPT_JS
from kiro_agent_sdk.runtime_session import JAVASCRIPT

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class RuntimeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"fixture")
        self.source.with_name("xhr-sync-worker.js").write_bytes(b"worker")
        mock = patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched fixture")
        mock.start()
        self.addCleanup(mock.stop)
        self.log = self.root / "requests"

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3", runtime_compat_mode="v3",
            native_hook_runtime_path=self.source, tools=[],
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_RUNTIME_POLICIES": "1",
                 "FAKE_LOG_REQUESTS": str(self.log)})
        values.update(changes)
        return KiroAgentOptions(**values)

    async def test_strict_policy_negotiation_and_mutation(self):
        options = self.options(strict_mcp_config=True)
        async with KiroSDKClient(options) as client:
            self.assertIs(client.server_info["agentCapabilities"]["sdkSessionPolicies"]["strictMcp"], True)
            options.strict_mcp_config = False
            with self.assertRaisesRegex(ValueError, "strict_mcp_config"):
                await client.query("no downgrade")
        options = self.options(strict_mcp_config=True)
        options.env["FAKE_NO_SESSION_POLICIES"] = "1"
        with self.assertRaisesRegex(ProtocolError, "session policies"):
            async with KiroSDKClient(options):
                pass

    async def test_strict_mutation_during_runtime_probe_never_launches(self):
        options = self.options(strict_mcp_config=True)
        client = KiroSDKClient(options)
        async def mutate(*_, **__):
            options.strict_mcp_config = False
        with patch("kiro_agent_sdk.store_fork._require_tested_cli", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "strict_mcp_config"):
                await client.connect()
        self.assertIsNone(client._process)
        self.assertFalse(self.log.exists())

    async def test_guarded_fork_passes_guard_before_load_and_requires_receipt(self):
        options = self.options(resume="parent", fork_session=True, resume_session_at="assistant",
                               resume_drops_turn="user")
        async with KiroSDKClient(options) as client:
            self.assertEqual(client.session_id, "forked")
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        fork = next(r for r in requests if r.get("method") == "session/fork")
        self.assertEqual(fork["params"]["_meta"]["kiro"]["sdkResumeDropsTurn"], "user")
        options.env["FAKE_RUNTIME_NO_FORK_RECEIPT"] = "1"
        with self.assertRaisesRegex(ProtocolError, "resume_drops_turn"):
            async with KiroSDKClient(options):
                pass

    async def test_fork_point_helper_returns_options_and_rechecks_authority(self):
        async with KiroSDKClient(self.options()) as client:
            values = await client.get_guarded_fork_options("user")
            self.assertEqual(values, {"resume": "test-session", "fork_session": True,
                                     "resume_session_at": "prior-boundary", "resume_drops_turn": "user"})
            async def deny():
                return False
            client._interaction_guard = deny
            with self.assertRaises(ProtocolError):
                await client.get_guarded_fork_options("user")

    async def test_guarded_fork_can_copy_without_loading_a_new_client(self):
        options = self.options()
        async with KiroSDKClient(options) as client:
            result = await client.fork_session(message_id="prior", resume_drops_turn="user")
            self.assertEqual(result["sessionId"], "forked")
            self.assertEqual(client.session_id, "test-session")
            with self.assertRaises(ProtocolError):
                await client.fork_session("another-session", message_id="prior", resume_drops_turn="user")
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(sum(r.get("method") == "session/fork" for r in requests), 1)
        self.assertFalse(any(r.get("method") == "session/load" for r in requests))

    async def test_busy_lookup_fails_instead_of_waiting_on_the_calling_hook(self):
        async with KiroSDKClient(self.options()) as client:
            async with client._query_lock:
                with self.assertRaisesRegex(Exception, "already active"):
                    await client.get_guarded_fork_options("user")

    def test_runtime_session_controls_js(self):
        node = Path.home() / ".local/share/kiro-cli/node"
        node = str(node) if node.is_file() else shutil.which("node")
        if not node:
            self.skipTest("requires Node")
        script = PROMPT_JS + JAVASCRIPT.replace(
            "const kiroSdkBootStrictMcp=false;", "const kiroSdkBootStrictMcp=true;") + r'''
import assert from "node:assert/strict";
// The filter is active before initialize, including eager startup reconciliation.
assert.deepEqual(kiroSdkMcpInputs({fileServers:["eager"],clientServers:[]}).fileServers,[]);
kiroSdkInitializeSessions({clientCapabilities:{_meta:{kiro:{sdkSessionPolicies:{
    protocol:3,strictMcp:true,preCompact:true,preCompactScope:"tree",subagentStart:true}}}}},{});
assert.throws(()=>kiroSdkInitializeSessions({clientCapabilities:{_meta:{kiro:{sdkSessionPolicies:{
    protocol:3,strictMcp:false,preCompact:false,preCompactScope:"tree",subagentStart:false}}}}},{}));
const inputs={fileServers:["global"],profileServers:["profile"],builtinServers:["builtin"],
    clientServers:["explicit"],dispatchedServers:["inherited"],scopeServers:["shadow"]};
const filtered=kiroSdkMcpInputs(inputs);
assert.deepEqual(filtered.clientServers,["explicit"]);
for(const key of ["fileServers","profileServers","builtinServers","dispatchedServers","scopeServers"])
    assert.deepEqual(filtered[key],[]);
assert.deepEqual(inputs.fileServers,["global"]);
// An RPC rejection blocks before the native summary model is invoked.
let calls=[],starts=[],allow=true;
let sequence=0;
function Lf(){return "start-correlation-"+(++sequence)}
const jt={fromHuman:()=>({withText:text=>({role:"user",text})})};
async function fm(connection,method,params){
    if(method==="_kiro/sdk/subagentStart"){
        starts.push(params);return {protocol:1,additionalContext:"trusted context for "+params.execution.id};
    }
    calls.push(params);assert.equal(method,"_kiro/sdk/preCompact");assert.equal(params.trigger,"auto");
    return {protocol:2,allow};
}
await assert.rejects(()=>kiroSdkRunPrompt({sessionId:"s",_meta:{kiro:{sdkRuntime:{
    protocol:7,usageMode:"none",requestId:"a".repeat(20),maxTurns:null,literal:false,fallbackModel:null,thinkingDisabled:false,
    agentEfforts:{},agentMaxTurns:{},subagentStopLimit:null,inheritAgents:[],foregroundAgents:[],backgroundAgents:[],backgroundContext:"none",backgroundResults:[]}}}},async()=>{
    const definition={};kiroSdkBindDefinition(definition,{sessionId:"s"});
    const execution={definition,executionId:"root",chatSessionId:"s",abortController:new AbortController()};
    const child={definition:{},executionId:"child",chatSessionId:"s",agentName:"research",
        abortController:new AbortController()};
    const grandchild={...child,executionId:"grandchild",abortController:new AbortController()};
    kiroSdkBindChild(execution,child);
    kiroSdkBindChild(child,grandchild);
    await Promise.all([kiroSdkBeginChild(child),kiroSdkBeginChild(child)]);
    assert.equal(starts.length,1);
    assert.equal(starts[0].execution.parentId,"root");
    const messages=[];
    const state={execution:child,context:{withNewMessage(m){messages.push(m);return this}}};
    kiroSdkApplyChildContext(state);
    kiroSdkApplyChildContext(state);
    assert.deepEqual(messages,[{role:"user",text:"trusted context for child"}]);
    await kiroSdkBeginChild(grandchild);
    assert.equal(starts[1].execution.depth,2);
    await kiroSdkPreCompact(execution);
    await kiroSdkPreCompact(child);
    await kiroSdkPreCompact(grandchild,"model-handoff");
    assert.equal(calls[1].execution.parentId,"root");
    assert.equal(calls[2].execution.depth,2);
    assert.equal(calls[2].reason,"model-handoff");
    kiroSdkUnbindChild(child);
    await assert.rejects(()=>kiroSdkBeginChild(grandchild),/ended/);
    await assert.rejects(()=>kiroSdkPreCompact(grandchild),/ended/);
    assert.throws(()=>kiroSdkBindChild(child,{...grandchild,executionId:"late"}),/scope/);
    await kiroSdkPreCompact({definition:{},abortController:new AbortController()});
    allow=false;
    await assert.rejects(()=>kiroSdkPreCompact(execution),/rejected/);
    assert.equal(execution.abortController.signal.aborted,true);
    return {stopReason:"end_turn"};
}),/PreCompact/);
assert.equal(calls.length,4);
// Fork tests use the snapshot contract; separate pinned-vendor tests verify
// the Kiro selection function and patch placement.
function GVn(rows){return rows}
function pbo(rows){
    for(const kind of ["checkpoint_revert","summarization"]){
        const active=[];
        for(const row of rows){
            if(row.payload.type==="tombstone"&&row.payload.kind===kind){
                const at=active.findIndex(r=>r.id===row.payload.effectiveFromMessageId);
                assert(at>=0);active.splice(at);
            }else active.push(row);
        }
        rows=active;
    }
    return rows;
}
function mbo(raw,id){const rows=pbo(GVn(raw)),i=rows.findIndex(r=>r.id===id);assert(i>=0);
    return {messageId:id,index:i,messages:rows.slice(0,i+1)}}
const row=(id,type,fields={})=>({id,payload:{type,...fields}});
const owned={turnId:"turn",executionId:"execution"};
const rows=[row("prior","assistant"),row("user","user",{_meta:{kiro:{userMessageTag:"tag"}}}),
    row("start","turn_start",{...owned,boundarySource:"observed",userMessageTag:"tag"}),
    row("answer","assistant",owned),
    row("metadata","session_metadata",{key:"contextUsage",executionId:"execution"}),
    row("paused","session_event",{category:"session_pause",context:owned}),
    row("end","turn_end",{...owned,boundarySource:"observed"})];
assert.equal(kiroSdkHistoryEnd(rows,undefined),rows.length);
assert.equal(kiroSdkHistoryEnd(rows,"user"),1);
assert.equal(kiroSdkHistoryEnd(rows,"missing"),-1);
const selected=kiroSdkForkSelection(rows,"prior","user");
assert.deepEqual(selected.messages,[rows[0]]);
assert.notEqual(selected.messages[0],rows[0]); // exact independent snapshot
for(const bad of [
    [...rows,row("later","user",{source:"chat",...owned})],
    [...rows,row("notice","session_event",owned)],
    [...rows,row("unknown","assistant")],
    [...rows,row("other","assistant",{turnId:"different",executionId:"execution"})],
    [...rows,row("end","assistant",owned)],
    [...rows,row("tombstone","tombstone",{kind:"checkpoint_revert"})],
    rows.map(r=>r.id==="start"?row("start","turn_start",{...owned,boundarySource:"recovered"}):r),
])assert.throws(()=>kiroSdkForkSelection(bad,"prior","user"),/Resume rejected/);
assert.throws(()=>kiroSdkForkSelection(rows,"end","user"),/Resume rejected/);
const compressed=[row("old-user","user",{source:"chat"}),
    row("compact","tombstone",{kind:"summarization",effectiveFromMessageId:"old-user"}),
    row("summary","assistant",{operationType:"Summary"}),...rows.slice(1)];
assert.equal(kiroSdkForkSelection(compressed,"summary","user").messages[0].id,"summary");
const rewound=[row("kept","assistant"),row("old-user","user",{source:"chat"}),
    row("rewind","tombstone",{kind:"checkpoint_revert",effectiveFromMessageId:"old-user"}),...rows.slice(1)];
assert.equal(kiroSdkForkSelection(rewound,"kept","user").messages[0].id,"kept");
// A later tombstone must never hide another user prompt from the guard.
const masked=[...rows,row("unseen","user",{source:"chat"}),
    row("mask","tombstone",{kind:"checkpoint_revert",effectiveFromMessageId:"unseen"})];
assert.throws(()=>kiroSdkForkSelection(masked,"prior","user"),/Resume rejected/);
class Szt{async loadParentSession(){return {messages:rows}}}
const server={sessionState:()=>({workspacePaths:[],promptPersistenceInFlightCount:0})};
assert.equal((await kiroSdkForkPoint(server,{sessionId:"s",userMessageId:"user"})).messageId,"prior");
// A candidate is only a hint: the actual fork must recheck a changed suffix.
const hint=await kiroSdkForkPoint(server,{sessionId:"s",userMessageId:"user"});
rows.push(row("queued","user",{source:"chat"}));
assert.throws(()=>kiroSdkForkSelection(rows,hint.messageId,hint.userMessageId),/Resume rejected/);
let encoded='{"id":"raw","payload":{"type":"user"},"extra":"retained"}\n';
const To={readFile:async()=>typeof encoded==="string"?new TextEncoder().encode(encoded):encoded};
const ktn={safeParse:row=>({success:!!row.payload?.type,data:{}})};
assert.equal((await kiroSdkReadForkMessages("owned"))[0].extra,"retained");
for(const invalid of [new Uint8Array([0xff]),'{"partial":', '{"id":"invalid-schema"}',
    '{"id":"ok","payload":{"type":"user"}}\nnot-json\n']){
    encoded=invalid;
    await assert.rejects(()=>kiroSdkReadForkMessages("owned"),/Resume rejected/);
}
'''
        result = subprocess.run([node, "--input-type=module", "-e", script],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


from test_agentcore import HAS_STARLETTE


@unittest.skipUnless(HAS_STARLETTE, "requires AgentCore dependencies")
class AgentCoreRuntimeSessionTests(unittest.IsolatedAsyncioTestCase):
    pass
