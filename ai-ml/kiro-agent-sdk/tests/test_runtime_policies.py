import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ResultMessage, VerbatimPrompt
from kiro_agent_sdk.runtime_policy import JAVASCRIPT
from kiro_agent_sdk.runtime_session import JAVASCRIPT as SESSION

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


@unittest.skipUnless(os.name == "posix", "requires POSIX runtime")
class RuntimePolicyTests(unittest.IsolatedAsyncioTestCase):
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

    def prompts(self):
        return [r["params"] for r in map(json.loads, self.log.read_text().splitlines())
                if r.get("method") == "session/prompt"]

    async def test_budget_result_and_next_request_get_separate_receipts(self):
        options = self.options(max_turns=2)
        options.env["FAKE_RUNTIME_MODEL_TURNS"] = "3"
        async with KiroSDKClient(options) as client:
            for text in ("first", "second"):
                await client.query(text)
                results = [m async for m in client.receive_response() if isinstance(m, ResultMessage)]
                self.assertEqual((results[-1].stop_reason, results[-1].model_turns, results[-1].is_error),
                                 ("max_turn_requests", 2, True))
            self.assertIsNone(client._native_hook_bridge)
        policies = [p["_meta"]["kiro"]["sdkRuntime"] for p in self.prompts()]
        self.assertNotEqual(policies[0]["requestId"], policies[1]["requestId"])
        self.assertTrue(all(p["maxTurns"] == 2 for p in policies))

    async def test_global_and_per_message_literal_flags_survive_all_stream_modes(self):
        for mode in ("on-receive", "buffered", "prefetch", "prefetch-first"):
            for force in (False, True):
                with self.subTest(mode=mode, force=force):
                    options = self.options(verbatim_prompts=force, stream_input_mode=mode)
                    async def inputs():
                        for composed in (True, False):
                            yield VerbatimPrompt("/literal @file") if composed else "/literal @file"
                    async with KiroSDKClient(options) as client:
                        await client.query(inputs())
                        results = [m async for m in client.receive_messages() if isinstance(m, ResultMessage)]
                        self.assertEqual(len(results), 2)
                    prompts = self.prompts()[-2:]
                    self.assertEqual([p["_meta"]["kiro"]["sdkRuntime"]["literal"] for p in prompts],
                                     [True, force])
                    self.assertTrue(all(p["prompt"] == [{"type": "text", "text": "/literal @file"}] for p in prompts))

    async def test_missing_receipt_is_not_reported_as_success(self):
        options = self.options(max_turns=1)
        options.env["FAKE_RUNTIME_NO_RECEIPT"] = "1"
        async with KiroSDKClient(options) as client:
            with self.assertRaises(Exception):
                await client.query("hello")
                _ = [m async for m in client.receive_response()]
            self.assertIsNotNone(client._reader_error)

    async def test_options_are_immutable_and_unsupported_values_still_fail(self):
        for value in (-1, True, "2", 2**31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                KiroSDKClient(self.options(max_turns=value))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(max_turns=1))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(verbatim_prompts=True))
        options = self.options(max_turns=1)
        async with KiroSDKClient(options) as client:
            options.max_turns = 3
            with self.assertRaisesRegex(ValueError, "new client"):
                await client.query("blocked")

    def test_runtime_counter_does_not_reset_on_steering_or_definition_replacement(self):
        node = str(Path.home() / ".local/share/kiro-cli/node")
        if not Path(node).is_file():
            node = shutil.which("node")
        if not node:
            self.skipTest("requires Node")
        script = JAVASCRIPT + SESSION + r'''
import assert from "node:assert/strict";
const request=(id,maxTurns,literal=false)=>({sessionId:"s",_meta:{kiro:{sdkRuntime:{
    protocol:7,usageMode:"none",requestId:id.repeat(20),maxTurns,literal,fallbackModel:null,thinkingDisabled:false,
    agentEfforts:{},agentMaxTurns:{},subagentStopLimit:null,inheritAgents:[],foregroundAgents:[],backgroundAgents:[],backgroundContext:"none",backgroundResults:[]}}}});
const session={sessionId:"s"};
let late;
const a=await kiroSdkRunPrompt(request("a",2),async()=>{
    const first={},second={};kiroSdkBindDefinition(first,session);
    const execution={definition:first};kiroSdkBeforeModel(execution);
    execution.agentIterationNumber=0;kiroSdkBeforeModel(execution);
    kiroSdkBindDefinition(second,session);late={definition:second};
    assert.throws(()=>kiroSdkBeforeModel(late),/max_turns/);
    return {stopReason:"end_turn"};
});
assert.equal(a._meta.kiro.sdkRuntime.modelTurns,2);
assert.equal(a._meta.kiro.sdkRuntime.maxTurnsReached,true);
assert.throws(()=>kiroSdkBeforeModel(late),/ended/);
const b=await kiroSdkRunPrompt(request("b",1,true),async()=>{
    const definition={};kiroSdkBindDefinition(definition,session);
    kiroSdkBeforeModel({definition});
    assert.equal(kiroSdkLiteralFirstModel({definition}),true);
    kiroSdkBeforeModel({definition:{}}); // independent child is not a main round
    return {stopReason:"end_turn"};
});
assert.equal(b._meta.kiro.sdkRuntime.modelTurns,1);
assert.equal(b._meta.kiro.sdkRuntime.maxTurnsReached,false);
assert.equal(kiroSdkLiteral(),false);
// Literal construction must not call the normal context/resource builders.
const Zfe="plan";function L1(){return "continuation"}function xT(){return {}}
class K7{constructor(options){Object.assign(this,options)}}
class Message{
    constructor(){this.entries=[]}
    withText(text){this.entries.push({type:"text",text});return this}
    withTexts(texts){texts.forEach(text=>this.withText(text));return this}
    withForcedRole(){this.forcedRole=true;return this}
    withUserAuthored(){this.userAuthored=true;return this}
}
const jt={fromHuman:()=>new Message()};
const hh={empty:()=>({messages:[],withNewMessage(message){this.messages.push(message);return this}})};
function Pte(message,images,documents){message.images=images;message.documents=documents;return message}
const literal=kiroSdkLiteralDefinition({sessionAgentContext:()=>({})},
    {modeId:"custom",customAgent:{prompt:"SYSTEM",agentId:"owned"},workspace:{}},
    "s","/command @/secret",[],[],["explicit resource"]);
const built=await literal.build({});
assert.equal(built.context.messages.length,2);
assert.deepEqual(built.context.messages[1].entries,[
    {type:"text",text:"/command @/secret"},{type:"text",text:"explicit resource"}]);
'''
        result = subprocess.run([node, "--input-type=module", "-e", script],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


from test_agentcore import HAS_STARLETTE, HEADER, request


@unittest.skipUnless(HAS_STARLETTE, "requires AgentCore dependencies")
class AgentCoreRuntimePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_runtime_enforces_options_and_rejects_other_callers(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "node_modules/@kiro/agent/dist/server/acp-server.js"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"fixture")
            source.with_name("xhr-sync-worker.js").write_bytes(b"worker")
            async def owner(req, _):
                return req.headers.get("x-owner") == "owner"
            def options(*_):
                return KiroAgentOptions(runtime_compat_mode="v3", native_hook_runtime_path=source,
                    max_turns=1, verbatim_prompts=True, tools=[],
                    env={"FAKE_RUNTIME_POLICIES": "1", "FAKE_RUNTIME_MODEL_TURNS": "2"})
            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "state", cli_path=FAKE, api_key="synthetic"),
                options_factory=options, session_authorizer=owner)
            with patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched"):
                try:
                    headers = [(HEADER, b"runtime-policy-owned-session")]
                    self.assertEqual((await request(app, "POST", "/invocations", b'{"prompt":"/literal @file"}',
                        headers))[0], 403)
                    status, body, _ = await request(app, "POST", "/invocations", b'{"prompt":"/literal @file"}',
                        [*headers, (b"x-owner", b"owner")])
                    self.assertEqual(status, 200)
                    self.assertTrue(json.loads(body)["is_error"])
                finally:
                    await app.state.kiro_agentcore.close()
