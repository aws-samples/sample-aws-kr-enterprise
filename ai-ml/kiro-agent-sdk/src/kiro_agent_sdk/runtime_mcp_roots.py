"""Prepare and consume exact MCP roots-notification targets without retries."""

JAVASCRIPT = r'''
const kiroSdkRootsTickets=new Map();
const kiroSdkRootsWrites=new Set();
const kiroSdkRootsWritesByClient=new WeakMap();
function kiroSdkTrackRootsWrite(client,promise){
    const record={client,promise};
    kiroSdkRootsWrites.add(record);kiroSdkRootsWritesByClient.set(client,record);
    const done=()=>{
        kiroSdkRootsWrites.delete(record);
        if(kiroSdkRootsWritesByClient.get(client)===record)kiroSdkRootsWritesByClient.delete(client);
    };
    promise.then(done,done);
    return promise;
}
function kiroSdkDropRootsTicket(id){
    const ticket=kiroSdkRootsTickets.get(id);
    if(!ticket)return;
    ticket.cancelled=true;
    ticket.abort?.();
    clearTimeout(ticket.timer);
    kiroSdkRootsTickets.delete(id);
}
async function kiroSdkNotifyRoots(request){
    const policy=kiroSdkElicitationSettings?.serverRequests;
    if(!policy?.rootsChanged||!policy.roots||!policy.connection||
       !request||Array.isArray(request)||request.protocol!==1||
       request.connectionId!==policy.connection.id||
       !policy.servers.includes(request.serverName)||!["prepare","commit","cancel"].includes(request.action))
        throw new Error("SDK roots notification is unavailable");
    const fields=Object.keys(request).sort().join(",");
    if(fields!==(request.action==="prepare"?"action,connectionId,protocol,serverName":
                 "action,connectionId,protocol,serverName,ticket"))
        throw new Error("Invalid SDK roots notification request");
    if(request.action==="prepare"){
        if(kiroSdkRootsTickets.size>=16)throw new Error("SDK roots notification capacity exhausted");
        const targets=[];
        for(const state of kiroSdkMcpLiveConnections){
            if(state.serverName!==request.serverName||!state.owner.isConnected)continue;
            const transport=state.client.transport;
            try{kiroSdkConnectionCurrent(state,transport)}catch{continue}
            targets.push({state,transport});
        }
        if(!targets.length||targets.length>32)throw new Error("No bounded live MCP roots targets");
        targets.sort((a,b)=>a.state.id.localeCompare(b.state.id));
        const id=KiroSdkMcpRequestId();
        const ticket={id,serverName:request.serverName,targets,connectionId:request.connectionId,
            deadline:performance.now()+30000,cancelled:false,committing:false,abort:null,timer:null};
        ticket.timer=setTimeout(()=>kiroSdkDropRootsTicket(id),30000);
        ticket.timer.unref?.();
        kiroSdkRootsTickets.set(id,ticket);
        return {protocol:1,ticket:id,connectionIds:targets.map(target=>target.state.id)};
    }
    const ticket=kiroSdkRootsTickets.get(request.ticket);
    if(request.action==="cancel"){
        if(ticket&&ticket.serverName===request.serverName&&ticket.connectionId===request.connectionId)
            kiroSdkDropRootsTicket(request.ticket);
        return {protocol:1,cancelled:true};
    }
    if(!ticket||ticket.cancelled||ticket.committing||ticket.serverName!==request.serverName||
       ticket.connectionId!==request.connectionId||performance.now()>=ticket.deadline)
        throw new Error("SDK roots notification ticket expired or consumed");
    ticket.committing=true;
    const results=[];
    try{
        for(const {state,transport} of ticket.targets){
            const current=()=>{
                if(ticket.cancelled||performance.now()>=ticket.deadline||
                   policy!==kiroSdkElicitationSettings.serverRequests||!state.owner.isConnected)
                    throw new Error("SDK roots notification scope expired");
                kiroSdkConnectionCurrent(state,transport);
            };
            try{current()}catch{
                results.push({connectionId:state.id,status:"unavailable"});
                continue;
            }
            if(kiroSdkRootsWrites.size>=64||kiroSdkRootsWritesByClient.has(state.client)){
                results.push({connectionId:state.id,status:"unavailable"});
                continue;
            }
            let rejectStopped;
            const stopped=new Promise((_,reject)=>{rejectStopped=reject});
            ticket.abort=()=>rejectStopped(new Error("SDK roots notification cancelled"));
            try{
                const pending=kiroSdkTrackRootsWrite(state.client,Promise.resolve(
                    state.client.notification({method:"notifications/roots/list_changed"})));
                await Promise.race([
                    pending,stopped
                ]);
                current();
                results.push({connectionId:state.id,status:"sent"});
            }catch{
                results.push({connectionId:state.id,status:"unknown"});
            }finally{ticket.abort=null}
        }
        return {protocol:1,ticket:request.ticket,results};
    }finally{kiroSdkDropRootsTicket(request.ticket)}
}
'''


def runtime_mcp_roots_patches() -> tuple[tuple[str, str], ...]:
    return (
        ('async extMethod(e,n){if(e==="_kiro/sdk/forkPoint")',
         'async extMethod(e,n){if(e==="_kiro/sdk/mcp/rootsChanged")return kiroSdkNotifyRoots(n);'
         'if(e==="_kiro/sdk/forkPoint")'),
    )
