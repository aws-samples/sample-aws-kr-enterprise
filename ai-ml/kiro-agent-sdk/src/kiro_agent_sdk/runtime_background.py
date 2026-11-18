"""Dispatch configured background agents before Kiro creates foreground resources."""

JAVASCRIPT = r'''
function kiroSdkBackgroundProgress(execution,name,id){
    const scope=kiroSdkExecutionScope(execution);
    // An SDK dispatch has no native child execution. Keep the tool's admission
    // progress, but do not publish a prospective native task ID as a real child.
    return {response:"",...(scope?.policy.backgroundAgents?.includes(name)?{}:{subExecutionId:id})};
}
function kiroSdkBackgroundContext(execution,mode){
    if(mode==="none")return null;
    if(mode!=="parent-text")throw new Error("Invalid SDK background context mode");
    const source=execution.state?.context?.messages;
    if(!Array.isArray(source)||source.length>8192)
        throw new Error("SDK background context is unavailable or exceeds scan limits");
    const context={source:"parent-execution-text",messages:[],omitted_messages:0,omitted_entries:0};
    const encode=value=>JSON.stringify(value).replaceAll("<","\\u003c").replaceAll(">","\\u003e").replaceAll("&","\\u0026");
    let scanned=0,full=false;
    for(let index=source.length-1;index>=0;index--){
        const message=source[index];
        if(!message||!Array.isArray(message.entries)||message.entries.length>4096||
           (scanned+=message.entries.length)>65536)
            throw new Error("Invalid SDK background context message or scan limit");
        const role=message.role==="human"&&message.userAuthored===true?"user":
                   message.role==="bot"?"assistant":message.role==="tool"?"tool":null;
        if(!role||message.forcedRole===true||full){
            context.omitted_messages++;context.omitted_entries+=message.entries.length;continue;
        }
        const parts=[];
        let bytes=0,oversize=false,omitted=0;
        for(const entry of message.entries){
            const text=entry?.type==="text"?entry.text:
                       role==="tool"&&entry?.type==="toolUseResponse"?entry.message:undefined;
            if(typeof text!=="string"||!text||text.includes("\0")||!text.isWellFormed()){
                omitted++;continue;
            }
            bytes+=Buffer.byteLength(text,"utf8")+1;
            if(bytes>131072){oversize=true;break}
            parts.push(text);
        }
        if(oversize){
            // Keep a recent suffix instead of skipping a large recent item and
            // silently substituting unrelated, older context.
            full=true;context.omitted_messages++;context.omitted_entries+=message.entries.length;continue;
        }
        if(!parts.length){context.omitted_messages++;context.omitted_entries+=message.entries.length;continue}
        context.messages.unshift({role,text:parts.join("\n")});
        // Reserve space for growing omission counters. Never split a text entry.
        if(Buffer.byteLength(encode(context),"utf8")>130816){
            context.messages.shift();context.omitted_messages++;context.omitted_entries+=message.entries.length;full=true;
        }else{
            context.omitted_entries+=omitted;
            if(context.messages.length===128)full=true;
        }
    }
    return context;
}
async function kiroSdkBackgroundDispatch(request){
    const execution=request.parentExecution,scope=kiroSdkExecutionScope(execution);
    if(!scope?.policy.backgroundAgents?.includes(request.agentName))return;
    const signal=execution.abortController.signal,callId=Lf();
    let timer,abort;
    const cancel=()=>{
        try{Promise.resolve(kiroSdkSessionConnection.extNotification("_kiro/sdk/cancelHook",{
            sessionId:scope.policy.sessionId,requestId:scope.policy.requestId,callId
        })).catch(()=>{})}catch{}
    };
    try{
        if(!kiroSdkScopeActive(scope)||signal.aborted||typeof request.prompt!=="string"||
           !request.prompt.trim()||Buffer.byteLength(request.prompt,"utf8")>262144)
            throw new Error("Invalid SDK background agent dispatch");
        const context=kiroSdkBackgroundContext(execution,scope.policy.backgroundContext);
        const stopped=new Promise((_,reject)=>{
            timer=setTimeout(()=>{cancel();reject(new Error("SDK background agent dispatch timed out"))},60000);
            abort=()=>{cancel();reject(new Error("SDK background agent dispatch cancelled"))};
            (scope.cancelPostTools??=new Set()).add(abort);
            signal.addEventListener("abort",abort,{once:true});
        });
        const reply=await Promise.race([fm(kiroSdkSessionConnection,"_kiro/sdk/backgroundAgent",{
            protocol:2,sessionId:scope.policy.sessionId,requestId:scope.policy.requestId,callId,
            agentName:request.agentName,prompt:request.prompt,modelId:execution.model.modelId,
            context,
            execution:{id:execution.executionId,depth:scope.depth,...(scope.depth?
                {parentId:scope.parentExecutionId,agentType:execution.agentName??execution.definition.type}:{})}
        }),stopped]);
        if(!kiroSdkScopeActive(scope)||signal.aborted||reply?.protocol!==2||
           typeof reply.taskId!=="string"||!/^sdk-task-[0-9a-f]{32}$/.test(reply.taskId))
            throw new Error("Invalid SDK background agent receipt");
        return {kind:"success",files:[],response:"Started an independent SDK background agent task: "+reply.taskId+
            ". This is an admission receipt, not its result. Use the kiro_sdk_agent_tasks status or cancel MCP tools "+
            "with task_id to inspect or cancel it. Its conversation is independent; do not assume completion."};
    }catch(error){
        cancel();
        return {kind:"fault",errorMessage:"SDK background agent dispatch failed",error};
    }finally{
        clearTimeout(timer);
        if(abort){signal.removeEventListener("abort",abort);scope.cancelPostTools?.delete(abort)}
    }
}
'''


def runtime_background_patches() -> tuple[tuple[str, str], ...]:
    return (("async function w5o(t){let{definition:e,",
             "async function w5o(t){const sdkBackground=await kiroSdkBackgroundDispatch(t);"
             "if(sdkBackground!==undefined)return sdkBackground;let{definition:e,"),
            ('output:{response:"",subExecutionId:C},rawInput:e});try{let te=await c(',
             'output:kiroSdkBackgroundProgress(n.execution,d,C),rawInput:e});try{let te=await c('))
