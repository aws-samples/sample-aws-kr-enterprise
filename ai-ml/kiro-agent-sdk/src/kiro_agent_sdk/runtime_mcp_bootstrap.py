"""Apply the SDK policy before early Kiro MCP clients open their transport."""

JAVASCRIPT = r'''
const kiroSdkBootMcpPolicy=false;
let kiroSdkMcpPolicyReady=false,kiroSdkMcpPolicyWaiters=0;
const kiroSdkMcpPolicyWaiting=new Set();
const kiroSdkMcpPolicyBindings=new WeakMap();
function kiroSdkReleaseMcpPolicy(){
    if(!kiroSdkBootMcpPolicy)return;
    if(kiroSdkMcpPolicyReady)throw new Error("SDK MCP startup policy was initialized twice");
    kiroSdkMcpPolicyReady=true;
    for(const resolve of kiroSdkMcpPolicyWaiting)resolve();
    kiroSdkMcpPolicyWaiting.clear();
}
async function kiroSdkAwaitMcpPolicy(signal){
    if(!kiroSdkBootMcpPolicy||kiroSdkMcpPolicyReady)return;
    if(!signal||signal.aborted||kiroSdkMcpPolicyWaiters>=64)
        throw new Error("SDK MCP startup policy wait is unavailable");
    kiroSdkMcpPolicyWaiters++;
    let timer,abort,resolveReady;
    const ready=new Promise(resolve=>{resolveReady=resolve;kiroSdkMcpPolicyWaiting.add(resolve)});
    const stopped=new Promise((_,reject)=>{
        abort=()=>reject(new Error("SDK MCP startup policy wait cancelled"));
        signal.addEventListener("abort",abort,{once:true});
        timer=setTimeout(()=>reject(new Error("SDK MCP startup policy was not received")),30000);
    });
    try{
        await Promise.race([ready,stopped]);
        if(signal.aborted||!kiroSdkMcpPolicyReady)
            throw new Error("SDK MCP startup policy wait expired");
    }finally{
        clearTimeout(timer);signal.removeEventListener("abort",abort);
        kiroSdkMcpPolicyWaiting.delete(resolveReady);
        kiroSdkMcpPolicyWaiters--;
    }
}
function kiroSdkRememberMcpPolicy(client,server,owner,policy){
    if(!kiroSdkBootMcpPolicy||!kiroSdkMcpPolicyReady)return;
    kiroSdkMcpPolicyBindings.set(client,{server,owner,policy});
}
function kiroSdkPrepareMcpTransport(owner,signal){
    if(!kiroSdkBootMcpPolicy)return;
    signal.throwIfAborted();
    if(!kiroSdkMcpPolicyReady||owner.disposed||!owner.mcpClient)
        throw new Error("SDK MCP startup policy is unavailable");
    const client=owner.mcpClient,server=owner.serverName,policy=kiroSdkElicitationSettings;
    const existing=kiroSdkMcpPolicyBindings.get(client);
    if(existing){
        if(existing.server!==server||existing.owner!==owner||existing.policy!==policy)
            throw new Error("SDK MCP startup binding changed");
        return;
    }
    // This is the native MCP API, which rejects changes after transport
    // attachment. Never reconnect or replay requests to retrofit capabilities.
    if(client.transport||owner.isConnected||owner.connectPromise)
        throw new Error("SDK MCP policy cannot be retrofitted to a connected client");
    const config=kiroSdkMcpClientOptions(server,{capabilities:{}});
    if(Object.keys(config.capabilities).length)client.registerCapabilities(config.capabilities);
    kiroSdkRegisterMcpServerRequests(client,server,owner);
    kiroSdkRememberMcpPolicy(client,server,owner,policy);
}
'''


def runtime_mcp_bootstrap_patches() -> tuple[tuple[str, str], ...]:
    return (
        ("async connectClient(e,n={}){let{fallbackToSSE:r=!1,forceLegacyEra:i=!1,",
         "async connectClient(e,n={}){await kiroSdkAwaitMcpPolicy(e);kiroSdkPrepareMcpTransport(this,e);"
         "let{fallbackToSSE:r=!1,forceLegacyEra:i=!1,"),
    )
