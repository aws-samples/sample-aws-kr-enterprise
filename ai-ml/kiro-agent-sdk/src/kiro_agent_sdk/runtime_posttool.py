"""Apply native post-tool text changes before delivering a tool state."""

JAVASCRIPT = r'''
const kiroSdkPostToolStorage=new KiroSdkPolicyStorage();
function kiroSdkFailCurrentPost(error){
    const context=kiroSdkPostToolStorage.getStore();
    if(context&&!context.closed)kiroSdkPostFailure(context.execution,error);
}
function kiroSdkPostEnabled(execution){
    return kiroSdkSessionSettings?.postToolOutput===true&&!!kiroSdkExecutionScope(execution);
}
function kiroSdkCheckAuxiliaryFiles(files,execution){
    if(kiroSdkPostEnabled(execution)&&files!=null&&(!Array.isArray(files)||files.length))
        throw new Error("Subagent handoff/context files require ordinary gated read_file calls when SDK post-tool gates are enabled");
}
async function kiroSdkRunPostCallbacks(context){
    if(!kiroSdkSessionSettings?.sdkPostTool)return;
    const {scope,execution,signal}=context,callId=Lf();
    let timer,abort;
    const cancel=()=>{
        try{Promise.resolve(kiroSdkSessionConnection.extNotification("_kiro/sdk/cancelHook",{
            sessionId:scope.policy.sessionId,requestId:scope.policy.requestId,callId
        })).catch(()=>{})}catch{}
    };
    try{
        kiroSdkCheckPostDelivery(execution,signal);
        if(typeof context.original!=="string"||Buffer.byteLength(context.original,"utf8")>1048576)
            throw new Error("SDK post-tool response exceeds limits");
        const stopped=new Promise((_,reject)=>{
            timer=setTimeout(()=>{cancel();reject(new Error("SDK post-tool callback timed out"))},60000);
            abort=()=>{cancel();reject(new Error("SDK post-tool callback cancelled"))};
            (scope.cancelPostTools??=new Set()).add(abort);
            signal.addEventListener("abort",abort,{once:true});
        });
        const reply=await Promise.race([fm(kiroSdkSessionConnection,"_kiro/sdk/postTool",{
            protocol:1,sessionId:scope.policy.sessionId,requestId:scope.policy.requestId,callId,
            execution:{id:execution.executionId,depth:scope.depth,...(scope.depth?
                {parentId:scope.parentExecutionId,agentType:execution.agentName??execution.definition.type}:{})},
            tool:{id:context.id,name:context.toolId,input:context.input,output:context.original,success:context.success}
        }),stopped]);
        kiroSdkCheckPostDelivery(execution,signal);
        if(reply?.protocol!==1||!(reply.updatedToolOutput===null||typeof reply.updatedToolOutput==="string")||
           !(reply.blockReason===null||typeof reply.blockReason==="string")||typeof reply.additionalContext!=="string"||
           (reply.updatedToolOutput??"").includes("\0")||reply.additionalContext.includes("\0")||
           (reply.blockReason??"").includes("\0")||Buffer.byteLength(reply.updatedToolOutput??"","utf8")>1048576||
           Buffer.byteLength(reply.additionalContext,"utf8")>65536||Buffer.byteLength(reply.blockReason??"","utf8")>65536||
           (!context.success&&(reply.updatedToolOutput!==null||reply.blockReason!==null))||
           (reply.blockReason!==null&&(reply.updatedToolOutput!==null||reply.additionalContext!=="")))
            throw new Error("Invalid SDK post-tool callback response");
        if(reply.blockReason!==null){
            context.sdkBlocked=true;
            context.additional=reply.blockReason||"Review the executed tool result as requested by PostToolUse.";
        }else{
            if(reply.updatedToolOutput!==null)context.updated=reply.updatedToolOutput;
            context.additional=reply.additionalContext;
        }
    }finally{
        clearTimeout(timer);
        if(abort){signal.removeEventListener("abort",abort);scope.cancelPostTools?.delete(abort)}
    }
}
function kiroSdkPostFailure(execution,error){
    const scope=kiroSdkExecutionScope(execution);
    if(scope){if(scope.depth)scope.postFailure=error;else scope.policy.postFailure=error}
    execution.abortController.abort(error);
}
function kiroSdkCheckPostFailure(execution){
    const scope=kiroSdkExecutionScope(execution),error=scope?.postFailure??scope?.policy.postFailure;
    if(error)throw error;
}
function kiroSdkCheckPostDelivery(execution,signal){
    kiroSdkCheckPostFailure(execution);
    const scope=kiroSdkExecutionScope(execution);
    if(!scope||!kiroSdkScopeActive(scope)||signal.aborted||execution.abortController.signal!==signal)
        throw new Error("SDK tool delivery scope has ended");
}
async function kiroSdkDeliverPrepared(tool,state,id,prepared,delivered){
    if(!kiroSdkPostEnabled(state.execution))return tool.runPreparedCall(state,id,prepared,delivered);
    // Native synchronous tools can publish a state before PostToolUse. Hold
    // this optional delivery callback until the final post-hook state exists.
    const signal=state.execution.abortController.signal;
    kiroSdkCheckPostDelivery(state.execution,signal);
    const result=await tool.runPreparedCall(state,id,prepared,undefined);
    kiroSdkCheckPostDelivery(state.execution,signal);
    if(delivered)delivered(result);
    return result;
}
async function kiroSdkDeliverDeferred(tool,state,toolUse,id,input,delivered){
    if(!kiroSdkPostEnabled(state.execution))return tool.handleDeferredInvocation(state,toolUse,id,input,delivered);
    const signal=state.execution.abortController.signal;
    kiroSdkCheckPostDelivery(state.execution,signal);
    const result=await tool.handleDeferredInvocation(state,toolUse,id,input,undefined);
    kiroSdkCheckPostDelivery(state.execution,signal);
    if(delivered)delivered(result.state??state);
    return result;
}
function kiroSdkPostWritable(payload){
    const context=kiroSdkPostToolStorage.getStore();
    if(!context||context.closed||!kiroSdkScopeActive(context.scope)||context.signal.aborted||
       context.execution.abortController.signal!==context.signal||
       payload.hook_event_name!=="PostToolUse"||payload.session_id!==context.execution.chatSessionId||
       payload.tool_name!==context.toolId||payload.tool_response!==(context.nativeResponse??context.original)||
       (payload.tool_call_id!==undefined&&payload.tool_call_id!==context.id))return false;
    try{return JSON.stringify(kiroSdkToolInput(payload.tool_input))===JSON.stringify(context.input)}catch{return false}
}
function kiroSdkPostUpdate(payload,value){
    if(!kiroSdkPostWritable(payload))throw new Error("SDK tool output scope has ended");
    const context=kiroSdkPostToolStorage.getStore();
    if(value.sdkPostToolFailure===true){
        const error=new Error("SDK PostToolUse hook failed");
        kiroSdkPostFailure(context.execution,error);throw error;
    }
    if(value.sdkPostToolBlockReason!==undefined){
        const reason=value.sdkPostToolBlockReason;
        if(typeof reason!=="string"||reason.includes("\0")||Buffer.byteLength(reason,"utf8")>65536)
            throw new Error("Invalid SDK PostToolUse block reason");
        context.sdkBlocked=true;context.updated=undefined;
        context.additional=reason||"Review the executed tool result as requested by PostToolUse.";
        return;
    }
    if(context.sdkBlocked)return;
    if(value.sdkUpdatedToolOutput!==undefined){
        const text=value.sdkUpdatedToolOutput;
        if(typeof text!=="string"||text.includes("\0")||Buffer.byteLength(text,"utf8")>1048576)
            throw new Error("Invalid SDK post-tool output");
        context.updated=text;
    }
    if(value.sdkPostToolContext!==undefined){
        const text=value.sdkPostToolContext;
        if(typeof text!=="string"||text.includes("\0"))throw new Error("Invalid SDK post-tool context");
        const combined=context.additional?context.additional+"\n"+text:text;
        if(Buffer.byteLength(combined,"utf8")>65536)throw new Error("SDK post-tool context exceeds limits");
        context.additional=combined;
    }
}
async function kiroSdkPostToolGate(tool,state,id,input,run){
    if(!kiroSdkPostEnabled(state.execution))return run();
    try{return await kiroSdkPostToolGateInner(tool,state,id,input,run)}
    catch(error){kiroSdkPostFailure(state.execution,error);throw error}
}
async function kiroSdkPostToolGateInner(tool,state,id,input,run){
    const execution=state.execution,scope=kiroSdkExecutionScope(execution);
    kiroSdkCheckPostDelivery(execution,execution.abortController.signal);
    const matches=state.context.messages.flatMap(message=>message.entries
        .filter(entry=>entry.type==="toolUseResponse"&&entry.id===id).map(entry=>({message,entry})));
    // An ambiguous/non-text response cannot be replaced. Native hooks still
    // execute, but receive no output-write capability.
    if(matches.length!==1||typeof matches[0].entry.message!=="string"||
       (kiroSdkSessionSettings?.sdkPostTool&&typeof matches[0].entry.success!=="boolean")){
        if(kiroSdkSessionSettings?.sdkPostTool)throw new Error("SDK post-tool response cannot be attributed");
        return run();
    }
    const context={execution,scope,id,toolId:tool.id,input:kiroSdkToolInput(input),
        signal:execution.abortController.signal,original:matches[0].entry.message,
        messageId:matches[0].message.messageId,success:matches[0].entry.success,closed:false,additional:"",updated:undefined};
    return kiroSdkPostToolStorage.run(context,async()=>{
        try{
            await kiroSdkRunPostCallbacks(context);
            context.nativeResponse=context.updated??context.original;
            const result=await run(context.nativeResponse);kiroSdkCheckPostFailure(execution);
            if(context.updated===undefined&&!context.additional)return result;
            if(!kiroSdkScopeActive(scope)||context.signal.aborted||
               execution.abortController.signal!==context.signal||result?.state?.execution!==execution)
                throw new Error("SDK post-tool state has ended");
            let changed=result.state.context;
            const message=changed.messages.find(item=>item.messageId===context.messageId);
            const entries=message?.entries.filter(item=>item.type==="toolUseResponse"&&item.id===id);
            if(entries?.length!==1||entries[0].message!==context.original)
                throw new Error("SDK post-tool response changed before replacement");
            if(context.updated!==undefined){
                const replacement=new jt(message.role,message.entries.map(entry=>entry===entries[0]?
                    {...entry,message:context.updated}:entry),{messageId:message.messageId,
                    forcedRole:message.forcedRole,userAuthored:message.userAuthored});
                changed=changed.withReplacedMessageById(message.messageId,replacement);
            }
            if(context.additional)changed=changed.withNewMessage(jt.fromHuman().withText(context.additional));
            return {...result,state:{...result.state,context:changed}};
        }catch(error){kiroSdkPostFailure(execution,error);throw error}
        finally{context.closed=true}
    });
}
'''


def runtime_posttool_patches() -> tuple[tuple[str, str], ...]:
    return (
        ("ae=await this.readHandoffFiles(at,n.execution.workspace,G);",
         "ae=await (kiroSdkCheckAuxiliaryFiles(at,n.execution),this.readHandoffFiles(at,n.execution.workspace,G));"),
        ("let he=await this.buildContextFileMessages(m,n.execution),z,",
         "let he=await (kiroSdkCheckAuxiliaryFiles(m,n.execution),this.buildContextFileMessages(m,n.execution)),z,"),
        ("return R=this.runPreparedCall(e,n,x,C),R}}}",
         "return R=kiroSdkDeliverPrepared(this,e,n,x,C),R}}}"),
        ("try{return await this.handleDeferredInvocation(e,n,r,i,o)}finally{",
         "try{return await kiroSdkDeliverDeferred(this,e,n,r,i,o)}finally{"),
        ("u=r??c,l=await e.execution.sessionServices.hooks.executePostToolUseHooks(this.id,this.tags,u,s,a,e),d=l.state;",
         "u=r??c,l=await kiroSdkPostToolGate(this,e,n,u,sdkOutput=>e.execution.sessionServices.hooks.executePostToolUseHooks("
         "this.id,this.tags,u,sdkOutput??s,a,e)),d=l.state;"),
    )
