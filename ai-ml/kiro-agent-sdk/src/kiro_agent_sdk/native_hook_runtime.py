"""Version-pinned Kiro V3 native hooks over the existing ACP connection."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Optional

from .errors import ProtocolError
from .runtime_policy import JAVASCRIPT, runtime_policy_patches
from .runtime_session import JAVASCRIPT as SESSION_JAVASCRIPT, runtime_session_patches
from .runtime_model import JAVASCRIPT as MODEL_JAVASCRIPT, runtime_model_patches
from .runtime_permission import JAVASCRIPT as PERMISSION_JAVASCRIPT, runtime_permission_patches
from .runtime_posttool import JAVASCRIPT as POST_JAVASCRIPT, runtime_posttool_patches
from .runtime_background import JAVASCRIPT as BACKGROUND_JAVASCRIPT, runtime_background_patches
from .runtime_usage import JAVASCRIPT as USAGE_JAVASCRIPT, runtime_usage_patches
from .runtime_elicitation import JAVASCRIPT as ELICITATION_JAVASCRIPT, runtime_elicitation_patches
from .runtime_mcp_connection import JAVASCRIPT as MCP_CONNECTION_JAVASCRIPT, runtime_mcp_connection_patches
from .runtime_mcp_bootstrap import JAVASCRIPT as MCP_BOOTSTRAP_JAVASCRIPT, runtime_mcp_bootstrap_patches
from .runtime_mcp_roots import JAVASCRIPT as MCP_ROOTS_JAVASCRIPT, runtime_mcp_roots_patches

METHOD = "_kiro/sdk/hooks/callback"
PROTOCOL = 1
SOURCE_SHA256 = "bf79b2b0fa7ae68653abdf117b4a4dbf064fec6fa7ca49500115f3f2f67581b1"
MAX_PAYLOAD = 4 * 1024 * 1024

# Install before importing the engine, including its static ESM dependencies.
# Optional packages must never fall back to ancestor node_modules directories.
DEPENDENCY_GUARD = r'''
import kiroSdkModule from "node:module";
import {realpathSync as kiroSdkRealpath} from "node:fs";
import {dirname as kiroSdkDirname,relative as kiroSdkRelative,isAbsolute as kiroSdkAbsolute} from "node:path";
import {fileURLToPath as kiroSdkFileURL} from "node:url";
const kiroSdkStage=kiroSdkRealpath(kiroSdkDirname(kiroSdkFileURL(import.meta.url)));
const kiroSdkDeps=kiroSdkRealpath(kiroSdkStage+"/node_modules");
const kiroSdkResolve=kiroSdkModule._resolveFilename;
const kiroSdkBuiltins=new Set(kiroSdkModule.builtinModules.flatMap(n=>[n,"node:"+n]));
function kiroSdkCheckResolved(resolved){
    if(kiroSdkBuiltins.has(resolved))return resolved;
    const actual=kiroSdkRealpath(resolved);
    if([kiroSdkStage,kiroSdkDeps].some(root=>{
        const rel=kiroSdkRelative(root,actual);
        return rel===""||(!kiroSdkAbsolute(rel)&&rel!==".."&&!rel.startsWith("../")&&!rel.startsWith("..\\"));
    }))return resolved;
    const error=new Error("Module outside the SDK runtime dependency roots");
    error.code="MODULE_NOT_FOUND";
    throw error;
}
if(typeof kiroSdkModule.registerHooks!=="function")
    throw new Error("SDK runtime requires Node module.registerHooks (22.15+ or 23.5+)");
kiroSdkModule.registerHooks({resolve(specifier,context,nextResolve){
    const resolved=nextResolve(specifier,context);
    if(resolved.url.startsWith("node:"))return resolved;
    if(!resolved.url.startsWith("file:"))throw new Error("Unsupported SDK runtime module URL");
    kiroSdkCheckResolved(kiroSdkFileURL(resolved.url));
    return resolved;
}});
kiroSdkModule._resolveFilename=function(specifier,...args){
    const resolved=Reflect.apply(kiroSdkResolve,this,[specifier,...args]);
    kiroSdkCheckResolved(resolved);
    return resolved;
};
'''

# Only the native hook executor receives sdkHookId. Model shell tools use a
# different runner. Do not replace child_process.spawn globally.
RUNNER = r'''processRunner:((base,connection)=>({spawn:async request=>{
    if(!request.command.startsWith("kiro-sdk-acp-hook:"))return base.spawn(request);
    const failure={exitCode:2,stdout:"",stderr:"SDK hook unavailable",timedOut:false,cancelled:false};
    let timer,abort;
    try{
        const input=JSON.parse(request.stdin);
        const deadline=Math.min(request.timeoutMs??60000,60000);
        if(request.signal.aborted)return {...failure,cancelled:true};
        const stopped=new Promise(resolve=>{
            timer=setTimeout(()=>resolve({...failure,timedOut:true}),deadline);
            abort=()=>resolve({...failure,cancelled:true});
            request.signal.addEventListener("abort",abort,{once:true});
        });
        const reply=fm(connection,"_kiro/sdk/hooks/callback",{
            protocol:1,command:request.command,hookId:request.sdkHookId,
            sessionId:input.session_id,input,timeoutMs:deadline,toolInputWritable:kiroSdkGateWritable(input),
            toolOutputWritable:kiroSdkPostWritable(input)
        }).then(value=>{
            if(value===null||typeof value!=="object"||
               !Number.isInteger(value.exitCode)||![0,1,2].includes(value.exitCode)||
               typeof value.stdout!=="string"||typeof value.stderr!=="string"||
               typeof value.timedOut!=="boolean"||typeof value.cancelled!=="boolean"||
               value.stdout.length+value.stderr.length>4194304)return failure;
            return value;
        },()=>failure);
        const value=await Promise.race([reply,stopped]);
        if(input.hook_event_name==="PostToolUse"&&(value===failure||value.cancelled||value.timedOut))
            kiroSdkFailCurrentPost(new Error("SDK PostToolUse callback did not complete"));
        if(value.sdkUpdatedToolOutput!==undefined||value.sdkPostToolContext!==undefined||
           value.sdkPostToolBlockReason!==undefined||value.sdkPostToolFailure===true){
            if(value.cancelled||value.timedOut)return failure;
            kiroSdkPostUpdate(input,value);
        }
        if(value.sdkAllowOnce!==undefined){
            if(value.sdkAllowOnce!==true||value.exitCode!==0||value.cancelled||value.timedOut)return failure;
        }
        if(value.sdkAdditionalContext!==undefined){
            if(value.exitCode!==0||value.cancelled||value.timedOut)return failure;
            kiroSdkAppendToolContext(input,value.sdkAdditionalContext);
        }
        if(value.sdkUpdatedInput!==undefined){
            if(value.exitCode!==0||value.cancelled||value.timedOut)return failure;
            kiroSdkApplyInputEdit(input,value.sdkUpdatedInput);
        }
        if(value.sdkAllowOnce===true)
            kiroSdkApproveInput(value.sdkUpdatedInput===undefined?input:{...input,tool_input:value.sdkUpdatedInput});
        // Stop errors must finish, rather than request another model turn.
        if(input.hook_event_name==="Stop"&&(value.cancelled||value.timedOut||
           value.exitCode===2))return {...value,exitCode:0,stdout:""};
        return value;
    }catch(error){kiroSdkFailCurrentPost(error);return failure}
    finally{clearTimeout(timer);if(abort)request.signal.removeEventListener("abort",abort)}
}}))(new tje(p),this.connection),clock:new eje'''


def patch_runtime(encoded: bytes, *, strict_mcp: bool = False, mcp_bootstrap: bool = False) -> bytes:
    if type(strict_mcp) is not bool:
        raise ValueError("strict_mcp must be a boolean")
    if type(mcp_bootstrap) is not bool:
        raise ValueError("mcp_bootstrap must be a boolean")
    if hashlib.sha256(encoded).hexdigest() != SOURCE_SHA256:
        raise ValueError("Untested Kiro hook runtime; use the supported Kiro CLI 2.35.1 bundle")
    source = encoded.decode("utf-8")
    patches = (
        ("processRunner:new tje(p),clock:new eje", RUNNER),
        ("this.deps.processRunner.spawn({command:a,cwd:r.cwd,env:qoe(r.rootConversationId),stdin:o",
         "this.deps.processRunner.spawn({sdkHookId:e.id,command:a,cwd:r.cwd,env:qoe(r.rootConversationId),stdin:o"),
        ('agentCapabilities:{loadSession:!0,promptCapabilities:{image:!0,embeddedContext:!0}',
         'agentCapabilities:{sdkMcpBootstrap:{protocol:1,deferred:kiroSdkBootMcpPolicy},sdkElicitation:kiroSdkElicitationSettings,sdkSessionPolicies:kiroSdkSessionSettings,sdkRuntimePolicies:7,sdkNativeHooks:{protocol:1,sourceSha256:"' + SOURCE_SHA256 +
         '"},loadSession:!0,promptCapabilities:{image:!0,embeddedContext:!0}'),
    ) + runtime_policy_patches() + runtime_session_patches() + runtime_model_patches() + runtime_permission_patches() + runtime_posttool_patches() + runtime_background_patches() + runtime_usage_patches() + runtime_elicitation_patches() + runtime_mcp_connection_patches() + runtime_mcp_bootstrap_patches() + runtime_mcp_roots_patches()
    for original, replacement in patches:
        if source.count(original) != 1:
            raise ValueError("Kiro hook runtime patch does not match the verified bundle")
        source = source.replace(original, replacement)
    session_javascript = SESSION_JAVASCRIPT.replace(
        "const kiroSdkBootStrictMcp=false;", "const kiroSdkBootStrictMcp=" + str(strict_mcp).lower() + ";")
    bootstrap_javascript = MCP_BOOTSTRAP_JAVASCRIPT.replace(
        "const kiroSdkBootMcpPolicy=false;", "const kiroSdkBootMcpPolicy=" + str(mcp_bootstrap).lower() + ";")
    if source.startswith("#!"):
        header, _, source = source.partition("\n")
        source = header + "\n" + JAVASCRIPT + session_javascript + MODEL_JAVASCRIPT + PERMISSION_JAVASCRIPT + POST_JAVASCRIPT + BACKGROUND_JAVASCRIPT + USAGE_JAVASCRIPT + ELICITATION_JAVASCRIPT + MCP_CONNECTION_JAVASCRIPT + bootstrap_javascript + MCP_ROOTS_JAVASCRIPT + source
    else:
        source = JAVASCRIPT + session_javascript + MODEL_JAVASCRIPT + PERMISSION_JAVASCRIPT + POST_JAVASCRIPT + BACKGROUND_JAVASCRIPT + USAGE_JAVASCRIPT + ELICITATION_JAVASCRIPT + MCP_CONNECTION_JAVASCRIPT + bootstrap_javascript + MCP_ROOTS_JAVASCRIPT + source
    return source.encode("utf-8")


def _read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid Kiro runtime file")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Kiro runtime file exceeds its size limit")
    return content


class NativeHookRuntime:
    def __init__(self) -> None:
        # This marker routes a registered hook; it is not a bearer credential.
        self.command = "kiro-sdk-acp-hook:" + secrets.token_hex(24)
        self.directory: Optional[Path] = None
        self.path: Optional[Path] = None
        self._files: dict[str, bytes] = {}
        self._dependencies: Optional[Path] = None
        self._worker_source: Optional[Path] = None
        self._worker_digest: Optional[bytes] = None
        self.bindings: dict[str, str] = {}
        self.confirmed = False

    def prepare(self, runtime_path: Any, environment: dict[str, str], *, strict_mcp: bool = False,
                mcp_bootstrap: bool = False) -> None:
        if self.directory is not None:
            raise ValueError("Native hook runtime already prepared")
        if runtime_path is None:
            home = Path(environment.get("HOME", str(Path.home())))
            data = Path(environment.get("XDG_DATA_HOME", str(home / ".local/share")))
            caches = [data / "kiro-cli"]
            if environment.get("KIRO_AGENTCORE_RUNTIME_CACHE"):
                caches.append(Path(environment["KIRO_AGENTCORE_RUNTIME_CACHE"]))
            candidates = []
            for cache in caches:
                candidates = sorted((cache / "kas").glob(
                    "2.35.1-*/node_modules/@kiro/agent/dist/server/acp-server.js"))
                if candidates:
                    break
            if len(candidates) != 1:
                raise ValueError("Set native_hook_runtime_path to the installed Kiro 2.35.1 acp-server.js")
            source = candidates[0]
        else:
            source = Path(runtime_path).absolute()
        if (len(source.parents) < 5 or source.parent.name != "server" or
                source.parents[4].name != "node_modules"):
            raise ValueError("Expected @kiro/agent/dist/server/acp-server.js")
        patched = patch_runtime(_read_regular(source, 32 * 1024 * 1024),
                                strict_mcp=strict_mcp, mcp_bootstrap=mcp_bootstrap)
        worker = _read_regular(source.parent / "xhr-sync-worker.js", 1024 * 1024)
        dependencies = source.parents[4].resolve(strict=True)
        self.directory = Path(tempfile.mkdtemp(prefix="kiro-acp-hooks-"))
        try:
            # Public code, no credentials. Also readable when options.user differs;
            # callers must provide dependencies readable by that CLI user.
            self.directory.chmod(0o755)
            bootstrap = (DEPENDENCY_GUARD + '\nawait import("./acp-server-implementation.js");\n').encode("utf-8")
            worker_bootstrap = (DEPENDENCY_GUARD +
                '\nawait import("./node_modules/@kiro/agent/dist/server/xhr-sync-worker.js");\n').encode("utf-8")
            files = {"acp-server.js": bootstrap, "acp-server-implementation.js": patched,
                     "xhr-sync-worker.js": worker_bootstrap,
                     "package.json": b'{"type":"module"}'}
            for name, content in files.items():
                path = self.directory / name
                with path.open("xb") as stream:
                    stream.write(content)
                path.chmod(0o444)
                self._files[name] = hashlib.sha256(content).digest()
            (self.directory / "node_modules").symlink_to(dependencies, target_is_directory=True)
            self._dependencies = dependencies
            self._worker_source = source.parent / "xhr-sync-worker.js"
            self._worker_digest = hashlib.sha256(worker).digest()
            self.path = self.directory / "acp-server.js"
        except BaseException:
            self.close()
            raise

    def verify(self) -> None:
        if self.directory is None or self.path is None:
            raise ProtocolError("Native hook runtime is not prepared")
        for name, digest in self._files.items():
            try:
                content = _read_regular(self.directory / name, 32 * 1024 * 1024)
            except OSError:
                raise ProtocolError("Native hook runtime changed") from None
            if hashlib.sha256(content).digest() != digest:
                raise ProtocolError("Native hook runtime changed")
        if ((self.directory / "node_modules").resolve(strict=True) != self._dependencies):
            raise ProtocolError("Native hook runtime dependencies changed")
        if (self._worker_source is None or hashlib.sha256(
                _read_regular(self._worker_source, 1024 * 1024)).digest() != self._worker_digest):
            raise ProtocolError("Native hook runtime worker changed")

    def bind(self, agent: str, events: dict[str, Any]) -> None:
        self.bindings = {f"{agent}#hook-{index}": event for index, event in enumerate(events)}

    def confirm(self, server_info: dict[str, Any]) -> None:
        capability = server_info.get("agentCapabilities", {}).get("sdkNativeHooks")
        if capability != {"protocol": PROTOCOL, "sourceSha256": SOURCE_SHA256}:
            raise ProtocolError("Kiro did not confirm the native ACP hook adapter")
        self.confirmed = True

    def hooks_active(self, hooks: Any) -> bool:
        if not isinstance(hooks, list):
            return False
        found = {}
        for hook in hooks:
            if not isinstance(hook, dict) or not isinstance(hook.get("action"), dict):
                continue
            if hook["action"].get("command") != self.command:
                continue
            ident, meta = hook.get("id"), hook.get("_meta")
            if (not isinstance(ident, str) or ident not in self.bindings or ident in found or
                    not isinstance(meta, dict) or meta.get("enabled") is not True or
                    meta.get("trigger") != self.bindings[ident]):
                return False
            found[ident] = meta["trigger"]
        return found == self.bindings

    def validate(self, params: Any, session_id: Any) -> dict[str, Any]:
        if (not self.confirmed or not isinstance(params, dict) or
                params.get("protocol") != PROTOCOL or type(params.get("protocol")) is not int or
                params.get("command") != self.command or
                not session_id or params.get("sessionId") != session_id):
            raise ProtocolError("Unregistered native ACP hook request")
        ident = params.get("hookId")
        event = self.bindings.get(ident) if isinstance(ident, str) else None
        payload = params.get("input")
        timeout = params.get("timeoutMs")
        if (event is None or not isinstance(payload, dict) or
                payload.get("hook_event_name") != event or payload.get("session_id") != session_id or
                type(timeout) not in (int, float) or not 0 < timeout <= 60000 or
                len(json.dumps(payload, ensure_ascii=True).encode()) > MAX_PAYLOAD):
            raise ProtocolError("Invalid native ACP hook input")
        return payload

    def close(self) -> None:
        if self.directory is not None:
            shutil.rmtree(self.directory)
            self.directory = None
            self.path = None
        self.confirmed = False
        self.bindings.clear()


def command_result(payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    result = {"exitCode": 0, "stdout": "", "stderr": "", "timedOut": False, "cancelled": False}
    if response.get("block"):
        if payload.get("hook_event_name") == "Stop":
            result["stdout"] = json.dumps({"decision": "block", "reason": response.get("reason", "Continue working")})
        else:
            result.update(exitCode=2, stderr=response.get("reason", "Blocked by SDK hook"))
            if payload.get("hook_event_name") == "PostToolUse":
                result["sdkPostToolBlockReason"] = response.get("reason", "Blocked by SDK hook")
    elif response.get("ask"):
        result["stdout"] = json.dumps({"hookSpecificOutput": {
            "permissionDecision": "ask", "permissionDecisionReason": response["ask"]}})
    if not response.get("block") and response.get("context"):
        if payload.get("hook_event_name") == "PreToolUse":
            result["sdkAdditionalContext"] = response["context"]
        elif not response.get("ask"):
            result["stdout"] = response["context"]
    if not response.get("block") and "updatedInput" in response:
        result["sdkUpdatedInput"] = response["updatedInput"]
    if not response.get("block") and response.get("allowOnce") is True:
        result["sdkAllowOnce"] = True
    if response.get("warning"):
        result["stderr"] = "SDK hook failed"
    if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_PAYLOAD:
        raise ProtocolError("Native hook output exceeds its size limit")
    return result
