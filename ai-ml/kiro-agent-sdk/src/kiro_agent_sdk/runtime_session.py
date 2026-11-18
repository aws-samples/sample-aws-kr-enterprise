"""Session controls inside the hash-pinned Kiro runtime."""

PRECOMPACT_METHOD = "_kiro/sdk/preCompact"

JAVASCRIPT = r'''
const kiroSdkBootStrictMcp=false;
let kiroSdkSessionSettings;
let kiroSdkSessionConnection;
const kiroSdkChildScopes=new WeakMap();
const kiroSdkExecutionContext=new KiroSdkPolicyStorage();
const kiroSdkForkReadStorage=new KiroSdkPolicyStorage();
function kiroSdkHistoryEnd(rows,before){
    return before===undefined?rows.length:rows.findIndex(row=>row.id===before);
}
async function kiroSdkReadForkMessages(path){
    const bytes=await To.readFile(path);
    if(bytes.byteLength>33554432)
        throw new Error("Resume rejected by --resume-drops-turn: history exceeds 32 MiB");
    let encoded;
    try{encoded=new TextDecoder("utf-8",{fatal:true}).decode(bytes)}catch{
        throw new Error("Resume rejected by --resume-drops-turn: invalid UTF-8 history");
    }
    const rows=[];
    for(const line of encoded.split("\n")){
        if(!line.trim())continue;
        let row;
        try{row=JSON.parse(line)}catch{
            throw new Error("Resume rejected by --resume-drops-turn: malformed history");
        }
        if(!ktn.safeParse(row).success||rows.length>=100000)
            throw new Error("Resume rejected by --resume-drops-turn: invalid history");
        // Retain the original row, including fields the normal schema parser
        // would strip/default. A safety decision must not silently lose data.
        rows.push(row);
    }
    return rows;
}
function kiroSdkInitializeSessions(request,connection){
    const value=request.clientCapabilities?._meta?.kiro?.sdkSessionPolicies;
    if(value===undefined)return;
    if(kiroSdkSessionSettings!==undefined||value===null||value.protocol!==3||
       typeof value.strictMcp!=="boolean"||typeof value.preCompact!=="boolean"||
       typeof value.subagentStart!=="boolean"||
       (value.postToolOutput!==undefined&&typeof value.postToolOutput!=="boolean")||
       (value.sdkPostTool!==undefined&&typeof value.sdkPostTool!=="boolean")||
       (value.postToolBatch!==undefined&&typeof value.postToolBatch!=="boolean")||
       (value.sdkPostTool===true&&value.postToolOutput!==true)||
       !["main","tree"].includes(value.preCompactScope)||
       value.strictMcp!==kiroSdkBootStrictMcp)
        throw new Error("Invalid SDK session policies");
    kiroSdkSessionSettings=Object.freeze({protocol:3,strictMcp:value.strictMcp,
        preCompact:value.preCompact,preCompactScope:value.preCompactScope,subagentStart:value.subagentStart,
        postToolOutput:value.postToolOutput===true,sdkPostTool:value.sdkPostTool===true,
        ...(value.postToolBatch===true?{postToolBatch:true}:{})});
    kiroSdkSessionConnection=connection;
}
function kiroSdkMcpInputs(inputs){
    if(!kiroSdkBootStrictMcp)return inputs;
    // Filter every resolution, including child scopes and configuration reloads.
    // Keep governance checks on explicitly supplied ACP client servers.
    return {...inputs,fileServers:[],profileServers:[],builtinServers:[],
        dispatchedServers:[],scopeServers:[],mandatoryServers:[]};
}
function kiroSdkExecutionScope(execution){
    const child=kiroSdkChildScopes.get(execution);
    if(child)return child;
    const policy=kiroSdkDefinitionPolicies.get(execution.definition);
    return policy?{policy,parent:null,depth:0,revoked:false}:undefined;
}
function kiroSdkScopeActive(scope){
    return !scope.revoked&&!scope.policy.closed&&(!scope.parent||kiroSdkScopeActive(scope.parent));
}
function kiroSdkBindChild(parent,child){
    const scope=kiroSdkExecutionScope(parent);
    if(!scope)return;
    if(!kiroSdkScopeActive(scope)||parent.chatSessionId!==scope.policy.sessionId||
       child.chatSessionId!==scope.policy.sessionId||scope.depth>=64||
       parent.abortController?.signal.aborted||parent.isCompleted?.()||kiroSdkChildScopes.has(child))
        throw new Error("Invalid SDK child execution scope");
    if(scope.policy.foregroundAgents?.includes(child.agentName)&&
       (child.isMemoryExtraction||!["subtask","hidden"].includes(child.subAgentPresentation)))
        throw new Error("SDK foreground agent cannot be registered as background work");
    if(scope.policy.inheritAgents?.includes(child.agentName)){
        if(!parent.model||typeof parent.model.modelId!=="string"||!parent.model.modelId||
           typeof child.overrideModelForRouting!=="function")
            throw new Error("SDK child cannot inherit the current parent model");
        child.overrideModelForRouting(structuredClone(parent.model));
    }
    // Provenance comes from the engine's actual parent/child registration,
    // never a prompt, event text, or inference from a shared conversation ID.
    kiroSdkChildScopes.set(child,{policy:scope.policy,parent:scope,
        parentExecutionId:parent.executionId,depth:scope.depth+1,revoked:false});
}
function kiroSdkUnbindChild(child){
    const scope=child&&kiroSdkChildScopes.get(child);
    if(scope){scope.revoked=true;scope.cancelStart?.();scope.cancelStop?.();
        for(const cancel of scope.cancelPostTools??[])cancel()}
}
function kiroSdkWaitChild(parent,child){
    const scope=kiroSdkChildScopes.get(child);
    if(scope?.policy.foregroundAgents?.includes(child.agentName)){
        if(!kiroSdkScopeActive(scope)||scope.parentExecutionId!==parent.executionId||
           parent.abortController.signal.aborted||child.subAgentPresentation==="background")
            throw new Error("SDK foreground agent lost its waiting parent");
        scope.foregroundJoined=true;
    }
    return child.waitForCompletion();
}
async function kiroSdkBeginChild(execution){
    if(!kiroSdkSessionSettings?.subagentStart)return;
    const scope=kiroSdkChildScopes.get(execution);
    if(!scope)return;
    if(!kiroSdkScopeActive(scope))throw new Error("SDK child request has ended");
    if(scope.startPromise)return scope.startPromise;
    scope.startPromise=(async()=>{
        const signal=execution.abortController.signal;
        signal.throwIfAborted();
        const callId=Lf();
        const cancel=()=>{
            try{Promise.resolve(kiroSdkSessionConnection.extNotification("_kiro/sdk/cancelHook",{
                sessionId:scope.policy.sessionId,requestId:scope.policy.requestId,callId
            })).catch(()=>{})}catch{}
        };
        let timer,abort;
        try{
            const stopped=new Promise((_,reject)=>{
                timer=setTimeout(()=>{cancel();reject(new Error("SDK SubagentStart timed out"))},60000);
                abort=()=>{cancel();reject(new Error("SDK SubagentStart cancelled"))};
                scope.cancelStart=abort;
                signal.addEventListener("abort",abort,{once:true});
            });
            const response=await Promise.race([fm(kiroSdkSessionConnection,"_kiro/sdk/subagentStart",{
                protocol:1,sessionId:scope.policy.sessionId,requestId:scope.policy.requestId,callId,
                execution:{id:execution.executionId,depth:scope.depth,parentId:scope.parentExecutionId,
                           agentType:execution.agentName??execution.definition.type}
            }),stopped]);
            signal.throwIfAborted();
            if(!kiroSdkScopeActive(scope)||response?.protocol!==1||
               typeof response.additionalContext!=="string"||response.additionalContext.includes("\0")||
               Buffer.byteLength(response.additionalContext,"utf-8")>65536)
                throw new Error("Invalid SDK SubagentStart response");
            scope.additionalContext=response.additionalContext;
        }finally{
            clearTimeout(timer);
            if(abort)signal.removeEventListener("abort",abort);
            scope.cancelStart=undefined;
        }
    })();
    return scope.startPromise;
}
function kiroSdkApplyChildContext(state){
    const scope=kiroSdkChildScopes.get(state.execution);
    if(!scope||!scope.additionalContext)return;
    if(!kiroSdkScopeActive(scope))throw new Error("SDK child request has ended");
    // Add explicit SDK context as a user-context message, not a system role or
    // another user request. This path also precedes any initial summarization.
    state.context=state.context.withNewMessage(jt.fromHuman().withText(scope.additionalContext));
    scope.additionalContext=undefined;
}
async function kiroSdkPreCompact(execution,reason="context"){
    if(!execution||!kiroSdkSessionSettings?.preCompact)return;
    const scope=kiroSdkExecutionScope(execution);
    if(!scope)return;
    if(scope.depth&&kiroSdkSessionSettings.preCompactScope!=="tree")return;
    const policy=scope.policy;
    if(!kiroSdkScopeActive(scope))throw new Error("SDK runtime request has ended");
    const signal=execution.abortController.signal;
    signal.throwIfAborted();
    let timer,abort;
    try{
        const stopped=new Promise((_,reject)=>{
            timer=setTimeout(()=>reject(new Error("SDK PreCompact timed out")),60000);
            abort=()=>reject(new Error("SDK PreCompact cancelled"));
            signal.addEventListener("abort",abort,{once:true});
        });
        const reply=await Promise.race([fm(kiroSdkSessionConnection,"_kiro/sdk/preCompact",{
            protocol:2,sessionId:policy.sessionId,requestId:policy.requestId,trigger:"auto",reason,
            execution:{id:execution.executionId,depth:scope.depth,
                ...(scope.depth?{parentId:scope.parentExecutionId,agentType:execution.agentName??execution.definition.type}:{})}
        }),stopped]);
        signal.throwIfAborted();
        if(reply?.protocol===2&&reply.allow===false){
            if(!scope.depth)policy.compactionBlocked=true;
            const error=new Error("SDK PreCompact rejected compaction");
            execution.abortController.abort(error);
            throw error;
        }
        if(!kiroSdkScopeActive(scope)||reply?.protocol!==2||reply?.allow!==true)
            throw new Error("SDK PreCompact rejected compaction");
    }finally{
        clearTimeout(timer);
        if(abort)signal.removeEventListener("abort",abort);
    }
}
function kiroSdkForkSelection(messages,messageId,userId){
    if(userId===undefined)return mbo(messages,messageId);
    const reject=(reason="unattributed-record")=>{throw new Error(
        "Resume rejected by --resume-drops-turn: "+reason)};
    if(typeof userId!=="string"||!userId||userId.length>128||
       typeof messageId!=="string"||!messageId)reject("invalid-options");
    // Selection and validation use the very same immutable snapshot that the
    // fork service writes. No second SDK history RPC or parent mutation.
    const raw=structuredClone(messages);
    const ids=new Set();
    for(const row of raw){
        if(typeof row.id!=="string"||!row.id||ids.has(row.id)||!row.payload)
            reject("ambiguous-history");
        if(row.payload.type==="tombstone"&&
           (!["summarization","checkpoint_revert"].includes(row.payload.kind)||
            !ids.has(row.payload.effectiveFromMessageId)))reject("invalid-tombstone");
        ids.add(row.id);
    }
    const selected=mbo(raw,messageId);
    const cut=raw.findIndex(row=>row.id===selected.messageId);
    if(cut<0)reject("missing-fork-boundary");
    const prefix=pbo(GVn(raw.slice(0,cut+1)));
    // A later edit to the effective history may not silently alter the prefix
    // we retain. Already-compacted/rewound history before the cut is supported.
    if(prefix.length!==selected.messages.length||prefix.some((r,i)=>
        JSON.stringify(r)!==JSON.stringify(selected.messages[i])))reject("changed-fork-prefix");
    const rows=raw;
    const anchor=rows.findIndex(r=>r.id===userId);
    const target=rows[anchor]?.payload;
    if(anchor<0||target.type!=="user"||![undefined,"chat"].includes(target.source)||
       target._meta?.kiro?.syntheticUserMessageReason||target._meta?.kiro?.notification)reject("invalid-user-prompt");
    let boundary;
    for(let i=anchor+1;i<rows.length;i++){
        const p=rows[i].payload;
        if(p.type==="user")reject("interleaved-user-prompt");
        if(p.type==="turn_start"){boundary=p;break}
    }
    if(!boundary||boundary.boundarySource!=="observed"||
       typeof boundary.turnId!=="string"||!boundary.turnId||
       typeof boundary.executionId!=="string"||!boundary.executionId)reject("missing-observed-boundary");
    const tag=target._meta?.kiro?.userMessageTag;
    if(typeof tag!=="string"||!tag||boundary.userMessageTag!==tag)reject("unmatched-user-boundary");
    if(rows.some(r=>r.payload.type==="turn_start"&&
        r.payload.executionId===boundary.executionId&&r.payload.turnId!==boundary.turnId))reject("ambiguous-execution");
    // Check *raw* suffix records, including any entries Kiro's current-view
    // normalization would hide. A tombstone cannot conceal a queued prompt.
    if(anchor<=cut)reject("invalid-user-boundary");
    // History discarded before this target prompt is not part of its raw
    // suffix. It must nevertheless normalize to exactly the retained prefix.
    const before=pbo(GVn(rows.slice(0,anchor)));
    if(before.length!==selected.messages.length||before.some((r,i)=>
        JSON.stringify(r)!==JSON.stringify(selected.messages[i])))reject("changed-user-prefix");
    const suffix=rows.slice(anchor);
    if(!suffix.length)reject();
    for(const row of suffix){
        const p=row.payload;
        if(row.id===userId)continue;
        if(p.type==="user"||p._meta?.kiro?.notification||
           p.boundarySource==="recovered")reject();
        if(p.type==="session_event"){
            if(p.category!=="session_pause"||p.context?.turnId!==boundary.turnId||
               p.context?.executionId!==boundary.executionId)reject();
            continue;
        }
        if(p.type==="session_metadata"&&["contextUsage","recap","suggestedPrompts","displayError","modelFallback"].includes(p.key)){
            if(p.executionId!==boundary.executionId||(p.turnId&&p.turnId!==boundary.turnId))reject();
            continue;
        }
        if(!["assistant","tool_call","tool_result","turn_start","turn_end","usage_summary",
             "steering_inclusion","model_route"].includes(p.type))reject();
        if(p.turnId!==boundary.turnId||p.executionId!==boundary.executionId)reject();
    }
    return selected;
}
async function kiroSdkForkPoint(server,request){
    if(!kiroSdkSessionSettings||typeof request?.sessionId!=="string"||
       typeof request?.userMessageId!=="string")
        throw new Error("SDK fork-point lookup requires verified session policies");
    const session=server.sessionState(request.sessionId);
    if(!session||session.abortController||session.promptPersistenceInFlightCount>0)
        throw new Error("SDK fork-point lookup requires an idle local session");
    const loaded=await kiroSdkForkReadStorage.run(true,()=>new Szt(
        server.persistence,server.messageStore).loadParentSession(request.sessionId,session.workspacePaths));
    const effective=pbo(GVn(loaded.messages));
    const index=effective.findIndex(row=>row.id===request.userMessageId);
    if(index<=0)throw new Error("No preceding fork point for the requested user message");
    const point=effective[index-1].id;
    const selected=kiroSdkForkSelection(loaded.messages,point,request.userMessageId);
    if(selected.messageId!==point)throw new Error("Kiro extended the requested fork boundary");
    return {protocol:1,sessionId:request.sessionId,messageId:point,userMessageId:request.userMessageId};
}
'''


def runtime_session_patches() -> tuple[tuple[str, str], ...]:
    return (
        ("at=await yho(re.waitForCompletion(),He,re.activity,",
         "at=await yho(kiroSdkWaitChild(l,re),He,re.activity,"),
        ("getConnection(){return this.connection}async initialize(e){await this.initPromise,",
         "getConnection(){return this.connection}async initialize(e){"
         "kiroSdkInitializeSessions(e,this.connection);await this.initPromise,"),
        ("function YHt(t){if(!t.mcpEnabled",
         "function YHt(t){t=kiroSdkMcpInputs(t);if(!t.mcpEnabled"),
        ("function OHo(t,e,n){let r=t.findIndex(a=>a.id===e);",
         "function OHo(t,e,n){let r=kiroSdkHistoryEnd(t,e);"),
        ("registerActiveChild(e){this.activeChildExecutionIds.add(e.executionId)",
         "registerActiveChild(e){kiroSdkBindChild(this,e);this.activeChildExecutionIds.add(e.executionId)"),
        ("unregisterActiveChild(e){this.activeChildExecutionIds.delete(e)",
         "unregisterActiveChild(e){kiroSdkUnbindChild(this.activeChildExecutions.get(e));this.activeChildExecutionIds.delete(e)"),
        ("async invoke(){let e=this.latestInvocationTiming??{turnId:this.executionId};",
         "async invoke(){return kiroSdkExecutionContext.run(this,()=>this.sdkInvokeInContext())}"
         "async sdkInvokeInContext(){let e=this.latestInvocationTiming??{turnId:this.executionId};"),
        ("async invokeInternal(){if(this.result!==void 0)return;if(this.abortController.signal.aborted)",
         "async invokeInternal(){if(this.result!==void 0)return;await kiroSdkBeginChild(this);"
         "if(this.abortController.signal.aborted)"),
        ("async generateCompactionBrief(e){let{fork:n,modelId:r,currentModelId:i,signal:o,caller:s}=e,",
         'async generateCompactionBrief(e){await kiroSdkPreCompact(kiroSdkExecutionContext.getStore(),"model-handoff");'
         'let{fork:n,modelId:r,currentModelId:i,signal:o,caller:s}=e,'),
        ('async extMethod(e,n){Qds(e);',
         'async extMethod(e,n){if(e==="_kiro/sdk/forkPoint")return kiroSdkForkPoint(this,n);Qds(e);'),
        ("async forkSession(e){let n=e.liveParentMemoryConfig&&",
         "async forkSession(e){return kiroSdkForkReadStorage.run(e.sdkResumeDropsTurn!==undefined,"
         "()=>this.sdkGuardedForkSession(e))}async sdkGuardedForkSession(e){let n=e.liveParentMemoryConfig&&"),
        ('async loadMessages(e){try{let n=await To.readFile(e,"utf-8");',
         'async loadMessages(e){if(kiroSdkForkReadStorage.getStore())return kiroSdkReadForkMessages(e);'
         'try{let n=await To.readFile(e,"utf-8");'),
        ("normalizeIfCold(e,n,r,i){if(this.executionState.hasActiveExecution(e))return r;",
         "normalizeIfCold(e,n,r,i){if(kiroSdkForkReadStorage.getStore()||this.executionState.hasActiveExecution(e))return r;"),
        ('w.debug("[SummarizationNode] No summarization result found, continuing normally"),t;let e=Date.now(),n=Lf();',
         'w.debug("[SummarizationNode] No summarization result found, continuing normally"),t;'
         'kiroSdkApplyChildContext(t);await kiroSdkPreCompact(t.execution);let e=Date.now(),n=Lf();'),
        ("o=i.metadata.workspacePaths,s=mbo(i.messages,e.messageId),a=s.messages,c=vzt()",
         "o=i.metadata.workspacePaths,s=kiroSdkForkSelection(i.messages,e.messageId,e.sdkResumeDropsTurn),a=s.messages,c=vzt()"),
        ("let p=await a.forkSession({sessionId:e.sessionId,cwd:",
         "let p=await a.forkSession({sdkResumeDropsTurn:n?.sdkResumeDropsTurn,sessionId:e.sessionId,cwd:"),
        ("return{sessionId:p.metadata.id,...h&&{configOptions:",
         "return{sessionId:p.metadata.id,_meta:{kiro:{sdkResumeDropsTurn:n?.sdkResumeDropsTurn}},...h&&{configOptions:"),
    )
