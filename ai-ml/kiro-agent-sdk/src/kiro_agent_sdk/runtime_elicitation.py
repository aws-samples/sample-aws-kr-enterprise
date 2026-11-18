"""Pinned Kiro MCP server attribution and bounded embedded input requests."""

JAVASCRIPT = r'''
import {randomUUID as KiroSdkMcpRequestId} from "node:crypto";
let kiroSdkElicitationSettings;
const kiroSdkMcpInputStorage=new KiroSdkPolicyStorage();
const kiroSdkEmbeddedInputStorage=new KiroSdkPolicyStorage();
const kiroSdkMcpInputOwners=new WeakMap();
function kiroSdkInitializeElicitation(request){
    if(typeof kiroSdkBootMcpPolicy!=="undefined"&&kiroSdkBootMcpPolicy&&kiroSdkMcpPolicyReady)
        throw new Error("SDK MCP startup policy was initialized twice");
    const value=request.clientCapabilities?._meta?.kiro?.sdkElicitation;
    if(value===undefined){
        if(typeof kiroSdkReleaseMcpPolicy==="function")kiroSdkReleaseMcpPolicy();
        return;
    }
    if(kiroSdkElicitationSettings!==undefined||!value||Array.isArray(value)||
       !["protocol,timeoutMs","protocol,serverRequests,timeoutMs"].includes(Object.keys(value).sort().join(","))||
       value.protocol!==1||
       !Number.isFinite(value.timeoutMs)||value.timeoutMs<=0||value.timeoutMs>900000)
        throw new Error("Invalid SDK elicitation policy");
    let serverRequests;
    if(value.serverRequests!==undefined){
        const requests=value.serverRequests;
        if(!requests||Array.isArray(requests)||![
           "roots,sampling,servers","roots,sampling,samplingMaxBytes,samplingMode,servers",
           "connection,roots,sampling,servers","connection,roots,sampling,samplingMaxBytes,samplingMode,servers",
           "connection,roots,rootsChanged,sampling,servers",
           "connection,roots,rootsChanged,sampling,samplingMaxBytes,samplingMode,servers"
           ].includes(Object.keys(requests).sort().join(","))||
           typeof requests.roots!=="boolean"||typeof requests.sampling!=="boolean"||
           (!requests.roots&&!requests.sampling)||!Array.isArray(requests.servers)||
           !requests.servers.length||requests.servers.length>32||
           new Set(requests.servers).size!==requests.servers.length||
           requests.servers.some(name=>typeof name!=="string"||!name||name.length>256||
               !name.isWellFormed()||/[\u0000-\u001f\u007f-\u009f]/.test(name)))
            throw new Error("Invalid SDK MCP server request policy");
        const samplingMode=requests.samplingMode??"text",samplingMaxBytes=requests.samplingMaxBytes??65536;
        if(!["text","multimodal","tools"].includes(samplingMode)||
           !Number.isInteger(samplingMaxBytes)||samplingMaxBytes<65536||samplingMaxBytes>4194304||
           ((samplingMode!=="text"||samplingMaxBytes!==65536)&&!requests.sampling))
            throw new Error("Invalid SDK MCP sampling policy");
        const connection=requests.connection;
        if(connection!==undefined&&(!connection||Array.isArray(connection)||
           Object.keys(connection).sort().join(",")!=="id,protocol"||connection.protocol!==1||
           typeof connection.id!=="string"||!/^[0-9a-f]{32}$/.test(connection.id)))
            throw new Error("Invalid SDK MCP connection policy");
        if(requests.rootsChanged!==undefined&&
           (requests.rootsChanged!==true||!requests.roots||connection===undefined))
            throw new Error("Invalid SDK MCP roots notification policy");
        serverRequests=Object.freeze({servers:Object.freeze([...requests.servers]),
            roots:requests.roots,sampling:requests.sampling,
            ...(requests.samplingMode!==undefined?{samplingMode,samplingMaxBytes}:{}),
            ...(connection?{connection:Object.freeze({id:connection.id,protocol:1})}:{}),
            ...(requests.rootsChanged?{rootsChanged:true}:{})});
    }
    kiroSdkElicitationSettings=Object.freeze({protocol:1,timeoutMs:value.timeoutMs,
        ...(serverRequests?{serverRequests}:{})});
    if(typeof kiroSdkReleaseMcpPolicy==="function")kiroSdkReleaseMcpPolicy();
}
function kiroSdkServerRequestEnabled(server,method){
    const policy=kiroSdkElicitationSettings?.serverRequests;
    return policy?.servers.includes(server)&&
        (method==="roots/list"?policy.roots:method==="sampling/createMessage"?policy.sampling:false);
}
function kiroSdkMcpClientOptions(server,options){
    const roots=kiroSdkServerRequestEnabled(server,"roots/list");
    const sampling=kiroSdkServerRequestEnabled(server,"sampling/createMessage");
    if(!roots&&!sampling)return options;
    return {...options,capabilities:{...options.capabilities,
        ...(roots?{roots:{listChanged:kiroSdkElicitationSettings.serverRequests.rootsChanged===true}}:{}),
        ...(sampling?{sampling:kiroSdkElicitationSettings.serverRequests.samplingMode==="tools"?{tools:{}}:{}}:{})}};
}
function kiroSdkMcpRequestLimit(method){
    return method==="sampling/createMessage"?
        (kiroSdkElicitationSettings?.serverRequests?.samplingMaxBytes??65536):65536;
}
function kiroSdkRegisterMcpServerRequests(client,server,owner){
    const connection=kiroSdkElicitationSettings?.serverRequests?.connection?
        kiroSdkRegisterConnectionState(client,server,owner):undefined;
    for(const method of ["roots/list","sampling/createMessage"]){
        if(!kiroSdkServerRequestEnabled(server,method))continue;
        client.setRequestHandler(method,async(request,context)=>{
            const state=kiroSdkMcpInputOwners.get(client);
            if(!state&&connection){
                if(request.method!==method)throw new Error("SDK MCP connection method changed");
                return kiroSdkForwardConnectionRequest(connection,method,request.params??{},context.mcpReq.signal);
            }
            kiroSdkMcpInputCurrent(state);
            if(state.serverName!==server||request.method!==method)
                throw new Error("SDK MCP server request origin changed");
            return kiroSdkMcpInputStorage.run(state,
                ()=>kiroSdkExecutionContext.run(state.execution,
                    ()=>kiroSdkForwardMcpServerRequest(method,request.params??{},context.mcpReq.signal)));
        });
    }
    if(typeof kiroSdkRememberMcpPolicy==="function")
        kiroSdkRememberMcpPolicy(client,server,owner,kiroSdkElicitationSettings);
}
function kiroSdkBindMcpElicitation(client,handler){
    if(!kiroSdkElicitationSettings)return handler;
    const state=kiroSdkMcpInputStorage.getStore();
    kiroSdkMcpInputCurrent(state);
    if(state.client!==client)throw new Error("SDK MCP input handler client changed");
    // Reverse MCP messages arrive in the transport's context. Registration does
    // not itself preserve AsyncLocalStorage; explicitly re-enter the tool scope.
    return async(...args)=>kiroSdkMcpInputStorage.run(state,
        ()=>kiroSdkExecutionContext.run(state.execution,async()=>{
            kiroSdkMcpInputCurrent(state);
            const value=await handler(...args);
            kiroSdkMcpInputCurrent(state);
            return value;
        }));
}
function kiroSdkMcpInputCurrent(state){
    if(!state||state.closed||state.signal.aborted||!kiroSdkScopeActive(state.scope)||
       state.tool.toolConfig.serverName!==state.serverName||
       state.tool.options.getConnection(state.serverName)?.mcpClient!==state.client||
       state.tool.options.getConnection(state.serverName)?.serverIdentity!==state.identity||
       state.tool.toolRevokedDuringPause())
        throw new Error("SDK MCP input scope expired");
    if(state.deadline!==undefined&&performance.now()>=state.deadline)
        throw new Error("SDK MCP input deadline expired");
}
async function kiroSdkDispatchMcp(tool,target,context,run){
    if(!kiroSdkElicitationSettings)return run();
    const execution=kiroSdkExecutionContext.getStore();
    const scope=execution&&kiroSdkExecutionScope(execution);
    const name=tool.toolConfig.serverName;
    if(!scope||typeof name!=="string"||!name||name.length>256||
       !name.isWellFormed()||/[\u0000-\u001f\u007f-\u009f]/.test(name)||
       !context.abortSignal||!target.mcpClient)
        throw new Error("SDK MCP input origin unavailable");
    // Kiro installs one request handler per MCP client. Refuse an overlapping
    // dispatch before it can replace another tool call's handler.
    const parent=kiroSdkMcpInputStorage.getStore();
    if(parent&&kiroSdkMcpInputOwners.get(target.mcpClient)===parent&&
       parent.tool===tool&&parent.operationId===context.actionId){
        kiroSdkMcpInputCurrent(parent);
        return run();
    }
    if(kiroSdkMcpInputOwners.has(target.mcpClient))
        throw new Error("SDK MCP input client already has an active tool call");
    const state={tool,execution,client:target.mcpClient,identity:target.serverIdentity,
        serverName:name,scope,operationId:context.actionId,signal:context.abortSignal,
        closed:false,deadline:undefined,inputCount:0,serverRequestCount:0,pendingServerRequests:0,
        serverRequestCancels:new Set()};
    kiroSdkMcpInputCurrent(state);
    kiroSdkMcpInputOwners.set(target.mcpClient,state);
    try{
        return await kiroSdkMcpInputStorage.run(state,async()=>{
            const result=await run();
            kiroSdkMcpInputCurrent(state);
            return result;
        });
    }finally{
        state.closed=true;
        for(const cancel of state.serverRequestCancels)cancel();
        if(kiroSdkMcpInputOwners.get(target.mcpClient)===state)
            kiroSdkMcpInputOwners.delete(target.mcpClient);
    }
}
function kiroSdkMcpCallOptions(options){
    const state=kiroSdkMcpInputStorage.getStore();
    if(!state)return options;
    kiroSdkMcpInputCurrent(state);
    return {...options,signal:state.signal};
}
function kiroSdkElicitationEnvelope(request,callId=KiroSdkMcpRequestId()){
    if(!kiroSdkElicitationSettings)return {};
    const state=kiroSdkMcpInputStorage.getStore();
    kiroSdkMcpInputCurrent(state);
    if(request.operationId!==state.operationId||request.toolId!==state.tool.id)
        throw new Error("SDK MCP input tool scope changed");
    return {sdkElicitation:{protocol:2,requestId:state.scope.policy.requestId,callId,
        serverName:state.serverName,inputRequired:kiroSdkEmbeddedInputStorage.getStore()===state}};
}
async function kiroSdkForwardElicitation(connection,sessionId,request){
    const params={sessionId,toolCallId:request.operationId,elicitation:request.elicitation};
    if(!kiroSdkElicitationSettings)return fm(connection,"_kiro/mcp/elicitation",params);
    const state=kiroSdkMcpInputStorage.getStore(),callId=KiroSdkMcpRequestId();
    kiroSdkMcpInputCurrent(state);
    const signal=request.signal;
    if(sessionId!==state.scope.policy.sessionId||!signal||signal.aborted||
       state.pendingServerRequests>=8||++state.serverRequestCount>64)
        throw new Error("SDK MCP elicitation capacity or scope expired");
    Object.assign(params,kiroSdkElicitationEnvelope(request,callId));
    if(Buffer.byteLength(JSON.stringify(params),"utf8")>65536)
        throw new Error("SDK MCP elicitation exceeds limits");
    let settled=false,rejectStop;
    const stopped=new Promise((_,reject)=>{rejectStop=reject});
    const abort=()=>{
        if(settled)return;
        settled=true;
        try{Promise.resolve(connection.extNotification("_kiro/sdk/cancelHook",{
            sessionId,requestId:state.scope.policy.requestId,callId})).catch(()=>{})}catch{}
        rejectStop(new Error("SDK MCP elicitation cancelled"));
    };
    const timeout=Math.max(1,Math.min(kiroSdkElicitationSettings.timeoutMs,
        state.deadline===undefined?Infinity:state.deadline-performance.now()));
    state.pendingServerRequests++;
    state.serverRequestCancels.add(abort);
    signal.addEventListener("abort",abort,{once:true});
    state.signal.addEventListener("abort",abort,{once:true});
    const timer=setTimeout(abort,timeout);
    try{
        const value=await Promise.race([fm(connection,"_kiro/mcp/elicitation",params),stopped]);
        kiroSdkMcpInputCurrent(state);
        if(settled||signal.aborted||!value||Array.isArray(value)||typeof value!=="object"||
           !["accept","decline","cancel"].includes(value.action)||
           Buffer.byteLength(JSON.stringify(value),"utf8")>65536)
            throw new Error("SDK MCP elicitation result is unavailable");
        return value;
    }finally{
        settled=true;clearTimeout(timer);
        signal.removeEventListener("abort",abort);state.signal.removeEventListener("abort",abort);
        state.serverRequestCancels.delete(abort);
        state.pendingServerRequests--;
    }
}
async function kiroSdkForwardMcpServerRequest(method,params,signal){
    const state=kiroSdkMcpInputStorage.getStore();
    kiroSdkMcpInputCurrent(state);
    if(!kiroSdkServerRequestEnabled(state.serverName,method)||!signal||signal.aborted||
       state.pendingServerRequests>=8||++state.serverRequestCount>64||
       !params||Array.isArray(params)||typeof params!=="object"||
       Buffer.byteLength(JSON.stringify(params),"utf8")>kiroSdkMcpRequestLimit(method))
        throw new Error("SDK MCP server request is unavailable");
    const callId=KiroSdkMcpRequestId(),requestId=state.scope.policy.requestId,sessionId=state.scope.policy.sessionId;
    let settled=false,timer,rejectStop;
    const stopped=new Promise((_,reject)=>{rejectStop=reject});
    const abort=()=>{
        if(settled)return;
        settled=true;
        try{Promise.resolve(kiroSdkSessionConnection.extNotification("_kiro/sdk/cancelHook",{
            sessionId,requestId,callId})).catch(()=>{})}catch{}
        rejectStop(new Error("SDK MCP server request cancelled"));
    };
    const timeout=Math.max(1,Math.min(kiroSdkElicitationSettings.timeoutMs,
        state.deadline===undefined?Infinity:state.deadline-performance.now()));
    state.pendingServerRequests++;
    state.serverRequestCancels.add(abort);
    signal.addEventListener("abort",abort,{once:true});
    state.signal.addEventListener("abort",abort,{once:true});
    timer=setTimeout(abort,timeout);
    try{
        // Send synchronously before yielding, so cancellation can correlate even
        // if it reaches the Python reader before the request handler starts.
        const request={
            protocol:1,sessionId,requestId,callId,serverName:state.serverName,
            method,params,toolCallId:state.operationId,
            inputRequired:kiroSdkEmbeddedInputStorage.getStore()===state};
        if(Buffer.byteLength(JSON.stringify(request),"utf8")>kiroSdkMcpRequestLimit(method))
            throw new Error("SDK MCP server request envelope exceeds limits");
        const pending=fm(kiroSdkSessionConnection,"_kiro/sdk/mcp/request",request);
        const value=await Promise.race([pending,stopped]);
        kiroSdkMcpInputCurrent(state);
        if(settled||signal.aborted||!value||Array.isArray(value)||typeof value!=="object"||
           Buffer.byteLength(JSON.stringify(value),"utf8")>kiroSdkMcpRequestLimit(method))
            throw new Error("Invalid SDK MCP server request response");
        return value;
    }finally{
        settled=true;clearTimeout(timer);
        signal.removeEventListener("abort",abort);
        state.signal.removeEventListener("abort",abort);
        state.serverRequestCancels.delete(abort);
        state.pendingServerRequests--;
    }
}
function kiroSdkInputPayload(payload){
    if(!payload||Array.isArray(payload)||typeof payload!=="object"||
       (payload.requestState!==undefined&&typeof payload.requestState!=="string"))
        throw new Error("Invalid SDK MCP embedded input payload");
    const requests=payload.inputRequests;
    if(requests!==undefined&&(!requests||Array.isArray(requests)||typeof requests!=="object"))
        throw new Error("Invalid SDK MCP embedded requests");
    const entries=Object.entries(requests??{});
    if(entries.length>8||(!entries.length&&payload.requestState===undefined))
        throw new Error("SDK MCP embedded request count exceeded");
    const payloadLimit=entries.some(([,request])=>request?.method==="sampling/createMessage")?
        kiroSdkMcpRequestLimit("sampling/createMessage"):65536;
    if(Buffer.byteLength(JSON.stringify(payload),"utf8")>payloadLimit)
        throw new Error("SDK MCP embedded input exceeds limits");
    for(const [key,request] of entries){
        if(!key||key.length>256||!request||Array.isArray(request)||typeof request!=="object")
            throw new Error("Invalid SDK MCP embedded request");
        if(Buffer.byteLength(JSON.stringify(request.params??{}),"utf8")>kiroSdkMcpRequestLimit(request.method))
            throw new Error("SDK MCP embedded request exceeds limits");
        if(request.method==="elicitation/create"){
            if(!request.params||Array.isArray(request.params)||typeof request.params!=="object"||
               !["form","url"].includes(request.params.mode??"form"))
                throw new Error("Invalid SDK embedded elicitation");
        }else{
            const state=kiroSdkMcpInputStorage.getStore();
            if(!state||!kiroSdkServerRequestEnabled(state.serverName,request.method)||
               (request.params!==undefined&&(!request.params||Array.isArray(request.params)||
                typeof request.params!=="object")))
                throw new Error("SDK embedded input handler was not explicitly enabled");
        }
    }
    return entries.length;
}
async function kiroSdkBoundInputRequired(flow,run){
    const state=kiroSdkMcpInputStorage.getStore();
    if(!state)return run(flow);
    kiroSdkMcpInputCurrent(state);
    if(state.deadline===undefined)state.deadline=performance.now()+kiroSdkElicitationSettings.timeoutMs;
    const count=value=>{
        kiroSdkMcpInputCurrent(state);
        state.inputCount+=kiroSdkInputPayload(value);
        if(state.inputCount>64)throw new Error("SDK MCP embedded request total exceeded");
    };
    count(flow.firstPayload);
    const remaining=()=>{
        kiroSdkMcpInputCurrent(state);
        return Math.max(1,state.deadline-performance.now());
    };
    const hooks={
        dispatchInputRequest:async(key,request,signal)=>{
            kiroSdkMcpInputCurrent(state);
            const value=await kiroSdkEmbeddedInputStorage.run(state,
                ()=>request.method==="elicitation/create"?flow.hooks.dispatchInputRequest(key,request,signal):
                    kiroSdkForwardMcpServerRequest(request.method,request.params??{},signal));
            kiroSdkMcpInputCurrent(state);
            if(Buffer.byteLength(JSON.stringify(value),"utf8")>kiroSdkMcpRequestLimit(request.method))
                throw new Error("SDK MCP embedded response exceeds limits");
            return value;
        },
        retry:async(params,limits)=>{
            kiroSdkMcpInputCurrent(state);
            if(Buffer.byteLength(JSON.stringify(params),"utf8")>
               Math.max(1048576,kiroSdkMcpRequestLimit("sampling/createMessage")+65536))
                throw new Error("SDK MCP retry input exceeds limits");
            const value=await flow.hooks.retry(params,{...limits,maxTotalTimeout:remaining()});
            kiroSdkMcpInputCurrent(state);
            if(value?.resultType==="input_required")count(value);
            return value;
        }
    };
    const controller=new AbortController();
    const abort=()=>controller.abort(state.signal.reason);
    const timeout=remaining();
    state.signal.addEventListener("abort",abort,{once:true});
    if(state.signal.aborted)abort();
    const timer=setTimeout(()=>controller.abort(new Error("SDK MCP input deadline expired")),timeout);
    try{
        const value=await run({...flow,signal:controller.signal,hooks,
            config:{...flow.config,maxRounds:Math.min(flow.config.maxRounds,10)},
            requestOptions:{...flow.requestOptions,maxTotalTimeout:remaining()},flowStartedAt:Date.now()});
        kiroSdkMcpInputCurrent(state);
        return value;
    }finally{
        clearTimeout(timer);
        state.signal.removeEventListener("abort",abort);
        controller.abort();
    }
}
'''


def runtime_elicitation_patches() -> tuple[tuple[str, str], ...]:
    return (
        ("kiroSdkInitializeSessions(e,this.connection);await this.initPromise,",
         "kiroSdkInitializeSessions(e,this.connection);kiroSdkInitializeElicitation(e);await this.initPromise,"),
        ("async dispatchWithElicitation(e,n,r,i,o){let s=e.mcpClient;",
         "async dispatchWithElicitation(e,n,r,i,o){return kiroSdkDispatchMcp(this,e,i,"
         "()=>this.sdkDispatchWithElicitation(e,n,r,i,o))}"
         "async sdkDispatchWithElicitation(e,n,r,i,o){let s=e.mcpClient;"),
        ("await s.callTool({name:n,arguments:r},{timeout:864e5})",
         "await s.callTool({name:n,arguments:r},kiroSdkMcpCallOptions({timeout:864e5}))"),
        ("s.setRequestHandler(qar,async(l,d)=>this.handleElicitationRequest(l,i,s,d.mcpReq.signal));",
         "s.setRequestHandler(qar,kiroSdkBindMcpElicitation(s,async(l,d)=>this.handleElicitationRequest(l,i,s,d.mcpReq.signal)));"),
        ('new Bli({name:"kiro",version:"0.0.0"},e);return dBt(n,this.serverName),n}',
         'new Bli({name:"kiro",version:"0.0.0"},kiroSdkMcpClientOptions(this.serverName,e));'
         'return dBt(n,this.serverName),kiroSdkRegisterMcpServerRequests(n,this.serverName,this),n}'),
        ('fm(e,"_kiro/mcp/elicitation",{sessionId:n,toolCallId:xe.operationId,elicitation:xe.elicitation})',
         'kiroSdkForwardElicitation(e,n,xe)'),
        ("this.options.acpElicitationHandler({operationId:n.actionId,toolId:this.id,elicitation:e})",
         "this.options.acpElicitationHandler({operationId:n.actionId,toolId:this.id,elicitation:e,signal:i})"),
        ("async function CLa(t){let{config:e,method:n,originalParams:r,requestOptions:i,hooks:o,signal:s}=t",
         "async function CLa(t){return kiroSdkBoundInputRequired(t,kiroSdkNativeInputRequired)}"
         "async function kiroSdkNativeInputRequired(t){let{config:e,method:n,originalParams:r,requestOptions:i,hooks:o,signal:s}=t"),
    )
