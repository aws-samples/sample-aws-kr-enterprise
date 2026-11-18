"""Pinned-runtime MCP requests authorized independently of a model turn."""

JAVASCRIPT = r'''
const kiroSdkMcpConnections=new WeakMap();
const kiroSdkMcpLiveConnections=new Set();
let kiroSdkMcpConnectionCalls=0,kiroSdkMcpConnectionPending=0;
function kiroSdkRegisterConnectionState(client,server,owner){
    if(!owner||!kiroSdkElicitationSettings?.serverRequests?.connection||
       !(kiroSdkServerRequestEnabled(server,"roots/list")||
         kiroSdkServerRequestEnabled(server,"sampling/createMessage")))return;
    const existing=kiroSdkMcpConnections.get(client);
    if(existing){
        if(existing.owner!==owner||existing.serverName!==server||existing.closed)
            throw new Error("SDK MCP connection registration changed");
        return existing;
    }
    if(kiroSdkElicitationSettings.serverRequests.rootsChanged){
        for(const state of kiroSdkMcpLiveConnections){
            if(state.closed||state.owner.disposed||state.owner.mcpClient!==state.client){
                state.closed=true;
                for(const cancel of state.cancels)cancel();
                kiroSdkMcpLiveConnections.delete(state);
            }
        }
        if(kiroSdkMcpLiveConnections.size>=256)
            throw new Error("SDK MCP connection registry exceeds limits");
    }
    const state={client,serverName:server,owner,id:KiroSdkMcpRequestId(),
        connectionId:kiroSdkElicitationSettings.serverRequests.connection.id,
        closed:false,pending:0,cancels:new Set()};
    kiroSdkMcpConnections.set(client,state);
    if(kiroSdkElicitationSettings.serverRequests.rootsChanged)kiroSdkMcpLiveConnections.add(state);
    return state;
}
function kiroSdkCloseMcpConnection(client){
    const state=kiroSdkMcpConnections.get(client);
    if(!state)return;
    state.closed=true;
    for(const cancel of state.cancels)cancel();
    kiroSdkMcpConnections.delete(client);
    kiroSdkMcpLiveConnections.delete(state);
}
function kiroSdkConnectionCurrent(state,transport){
    if(!state||state.closed||state.owner.disposed||state.owner.mcpClient!==state.client||
       state.owner.serverName!==state.serverName||!transport||state.client.transport!==transport||
       kiroSdkMcpConnections.get(state.client)!==state||
       kiroSdkElicitationSettings?.serverRequests?.connection?.id!==state.connectionId)
        throw new Error("SDK MCP connection scope expired");
}
async function kiroSdkForwardConnectionRequest(state,method,params,signal){
    const transport=state?.client.transport;
    kiroSdkConnectionCurrent(state,transport);
    if(!kiroSdkServerRequestEnabled(state.serverName,method)||!signal||signal.aborted||
       state.pending>=8||kiroSdkMcpConnectionPending>=16||kiroSdkMcpConnectionCalls>=4096||
       !params||Array.isArray(params)||typeof params!=="object")
        throw new Error("SDK MCP connection request unavailable");
    const callId=KiroSdkMcpRequestId();
    const payload={protocol:1,connectionId:state.connectionId,serverConnectionId:state.id,callId,
        serverName:state.serverName,method,params,phase:state.owner.isConnected?"idle":"initializing"};
    if(Buffer.byteLength(JSON.stringify(payload),"utf8")>kiroSdkMcpRequestLimit(method))
        throw new Error("SDK MCP connection request exceeds limits");
    let settled=false,rejectStop;
    const stopped=new Promise((_,reject)=>{rejectStop=reject});
    const abort=()=>{
        if(settled)return;
        settled=true;
        try{Promise.resolve(kiroSdkSessionConnection.extNotification("_kiro/sdk/mcp/cancelConnectionRequest",{
            connectionId:state.connectionId,serverConnectionId:state.id,callId})).catch(()=>{})}catch{}
        rejectStop(new Error("SDK MCP connection request cancelled"));
    };
    state.pending++;kiroSdkMcpConnectionPending++;kiroSdkMcpConnectionCalls++;
    state.cancels.add(abort);
    signal.addEventListener("abort",abort,{once:true});
    const timer=setTimeout(abort,kiroSdkElicitationSettings.timeoutMs);
    try{
        const pending=fm(kiroSdkSessionConnection,"_kiro/sdk/mcp/connectionRequest",payload);
        const value=await Promise.race([pending,stopped]);
        kiroSdkConnectionCurrent(state,transport);
        if(settled||signal.aborted||!value||Array.isArray(value)||typeof value!=="object"||
           Buffer.byteLength(JSON.stringify(value),"utf8")>kiroSdkMcpRequestLimit(method))
            throw new Error("Invalid SDK MCP connection response");
        return value;
    }finally{
        settled=true;clearTimeout(timer);
        signal.removeEventListener("abort",abort);
        state.cancels.delete(abort);
        state.pending--;kiroSdkMcpConnectionPending--;
    }
}
'''


def runtime_mcp_connection_patches() -> tuple[tuple[str, str], ...]:
    return (
        ("async replaceClosedClient(){try{await this.mcpClient.close()}",
         "async replaceClosedClient(){kiroSdkCloseMcpConnection(this.mcpClient);try{await this.mcpClient.close()}"),
        ("async performClose(){this.disposed=!0;for(let e of this.inFlightRefreshes)",
         "async performClose(){kiroSdkCloseMcpConnection(this.mcpClient);this.disposed=!0;"
         "for(let e of this.inFlightRefreshes)"),
    )
