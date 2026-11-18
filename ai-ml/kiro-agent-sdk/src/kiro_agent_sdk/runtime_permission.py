"""Bind permission inputs to the pinned runtime's actual tool handler."""

JAVASCRIPT = r'''
const kiroSdkToolStorage=new KiroSdkPolicyStorage();
const kiroSdkGateStorage=new KiroSdkPolicyStorage();
const kiroSdkEditedInputs=new WeakSet();
const kiroSdkApprovedInputs=new WeakSet();
function kiroSdkToolInput(value){
    let nodes=0;
    function copy(item,depth){
        if(++nodes>100000||depth>64)throw new Error("SDK tool input exceeds limits");
        if(item===null||typeof item==="boolean"||typeof item==="string")return item;
        if(typeof item==="number"&&Number.isFinite(item))return item;
        if(typeof item!=="object")throw new Error("SDK tool input is not JSON");
        if(Array.isArray(item)){
            const result=[];
            for(let index=0;index<item.length;index++){
                const descriptor=Object.getOwnPropertyDescriptor(item,String(index));
                if(!descriptor)throw new Error("SDK tool input is a sparse array");
                if(!("value" in descriptor))throw new Error("SDK tool input has an accessor");
                result.push(copy(descriptor.value,depth+1));
            }
            return result;
        }
        const prototype=Object.getPrototypeOf(item);
        if(prototype!==Object.prototype&&prototype!==null)throw new Error("SDK tool input is not plain JSON");
        const result=Object.create(null);
        for(const [key,descriptor] of Object.entries(Object.getOwnPropertyDescriptors(item))){
            if(!descriptor.enumerable)continue;
            if(!("value" in descriptor))throw new Error("SDK tool input has an accessor");
            // Native schemas may materialize absent optional object fields.
            // Preserve their JSON representation without null coercion.
            if(descriptor.value===undefined)continue;
            result[key]=copy(descriptor.value,depth+1);
        }
        return result;
    }
    const result=copy(value,0);
    if(result===null||Array.isArray(result)||typeof result!=="object"||
       Buffer.byteLength(JSON.stringify(result),"utf8")>1048576)
        throw new Error("SDK permission input exceeds limits");
    return result;
}
async function kiroSdkHandleTool(tool,request,run){
    const execution=request.state.execution,scope=kiroSdkExecutionScope(execution);
    if(!scope)return run();
    if(!kiroSdkScopeActive(scope))throw new Error("SDK tool request has ended");
    const context={execution,scope,toolId:tool.id,actionId:request.actionId,
        input:request.input,signal:execution.abortController.signal,closed:false,
        allowOnce:request.modelInput!==undefined&&kiroSdkApprovedInputs.has(request.modelInput)};
    if(request.modelInput!==undefined)kiroSdkApprovedInputs.delete(request.modelInput);
    return kiroSdkToolStorage.run(context,async()=>{
        try{return await run()}finally{context.closed=true}
    });
}
async function kiroSdkToolGate(tool,state,actionId,input,run){
    const execution=state.execution,scope=kiroSdkExecutionScope(execution);
    if(!scope)return run();
    if(!kiroSdkScopeActive(scope))throw new Error("SDK tool gate has ended");
    kiroSdkApprovedInputs.delete(input);
    const context={execution,scope,toolId:tool.id,actionId,input,
        signal:execution.abortController.signal,closed:false,additionalContext:""};
    return kiroSdkGateStorage.run(context,async()=>{
        try{
            const result=await run();
            if(!context.additionalContext||result?.interceptMessage!==undefined)return result;
            if(!kiroSdkScopeActive(scope)||context.signal.aborted||
               execution.abortController.signal!==context.signal||!result?.state?.context||
               result.state.execution!==execution)
                throw new Error("SDK tool context is no longer applicable");
            return {...result,state:{...result.state,context:result.state.context.withNewMessage(
                jt.fromHuman().withText(context.additionalContext))}};
        }finally{context.closed=true}
    });
}
function kiroSdkGateWritable(payload){
    const context=kiroSdkGateStorage.getStore();
    if(!context||context.closed||context.signal.aborted||
       context.execution.abortController.signal!==context.signal||!kiroSdkScopeActive(context.scope)||
       payload.hook_event_name!=="PreToolUse"||payload.tool_name!==context.toolId||
       payload.session_id!==context.execution.chatSessionId||
       (payload.tool_call_id!==undefined&&payload.tool_call_id!==context.actionId))return false;
    try{return JSON.stringify(kiroSdkToolInput(payload.tool_input))===
               JSON.stringify(kiroSdkToolInput(context.input))}catch{return false}
}
function kiroSdkApplyInputEdit(payload,updated){
    if(!kiroSdkGateWritable(payload))throw new Error("SDK tool input is no longer writable");
    const context=kiroSdkGateStorage.getStore(),input=kiroSdkToolInput(updated);
    // Stage and validate the replacement before changing the model tool-use
    // arguments. The ordinary tool parser will validate this again before handle.
    for(const key of Object.keys(context.input))delete context.input[key];
    for(const [key,value] of Object.entries(input))
        Object.defineProperty(context.input,key,{value,enumerable:true,writable:true,configurable:true});
    kiroSdkEditedInputs.add(context.input);
}
function kiroSdkApproveInput(payload){
    if(!kiroSdkGateWritable(payload))throw new Error("SDK tool input is no longer approvable");
    kiroSdkApprovedInputs.add(kiroSdkGateStorage.getStore().input);
}
function kiroSdkAppendToolContext(payload,text){
    if(!kiroSdkGateWritable(payload)||typeof text!=="string"||text.includes("\0"))
        throw new Error("SDK tool context is not applicable");
    const context=kiroSdkGateStorage.getStore();
    const combined=context.additionalContext?context.additionalContext+"\n"+text:text;
    if(Buffer.byteLength(combined,"utf8")>65536)throw new Error("SDK tool context exceeds limits");
    context.additionalContext=combined;
}
function kiroSdkPermissionInput(details,execution,sessionId){
    const context=kiroSdkToolStorage.getStore();
    // Compound tools and aliases can have other operation IDs. Do not infer
    // their input from a surrounding call, title, display card or file path.
    if(!context||details.toolId!==context.toolId||details.operationId!==context.actionId||
       execution.executionId!==context.execution.executionId||
       sessionId!==context.execution.chatSessionId)return {};
    if(context.closed||context.signal.aborted||
       context.execution.abortController.signal!==context.signal||!kiroSdkScopeActive(context.scope))
        throw new Error("SDK permission request has ended");
    return {rawInput:kiroSdkToolInput(context.input)};
}
function kiroSdkRequestPermission(connection,details,execution,request){
    const context=kiroSdkToolStorage.getStore();
    if(context?.allowOnce&&kiroSdkPermissionInput(details,execution,request.sessionId).rawInput!==undefined){
        const options=request.options.filter(option=>option.kind==="allow_once");
        if(options.length!==1||typeof options[0].optionId!=="string"||!options[0].optionId)
            throw new Error("SDK hook approval requires one unambiguous allow-once option");
        context.allowOnce=false;
        return Promise.resolve({outcome:{outcome:"selected",optionId:options[0].optionId}});
    }
    return connection.requestPermission(request);
}
'''


def runtime_permission_patches() -> tuple[tuple[str, str], ...]:
    return (
        ("let f=JSON.stringify(n.args),h=await o(e,n.args);",
         "let f=JSON.stringify(n.args),h=await kiroSdkToolGate(this,e,i,n.args,()=>o(e,n.args));"),
        ("u=JSON.stringify(i),l=await this.runPreToolUseHooks(e,i),d=l.state;",
         "u=JSON.stringify(i),l=await kiroSdkToolGate(this,e,r,i,()=>this.runPreToolUseHooks(e,i)),d=l.state;"),
        ('w.warn("syncTool.gate.rewriteInvalid",{toolId:this.id,error:y instanceof Error?y.message:String(y)})}u();',
         'w.warn("syncTool.gate.rewriteInvalid",{toolId:this.id,error:y instanceof Error?y.message:String(y)});'
         'if(kiroSdkEditedInputs.has(n.args))throw y}u();'),
        ('w.warn("syncTool.gate.rewriteInvalid",{toolId:this.id,error:f instanceof Error?f.message:String(f)})}let p=',
         'w.warn("syncTool.gate.rewriteInvalid",{toolId:this.id,error:f instanceof Error?f.message:String(f)});'
         'if(kiroSdkEditedInputs.has(i))throw f}let p='),
        ("u();let m=await this.handle({actionId:i,input:d,state:e,streamPosition:0,toolUse:n,onDelivered:p}),g=m.state??e;",
         "u();let m=await kiroSdkHandleTool(this,{actionId:i,input:d,state:e,modelInput:n.args},()=>this.handle("
         "{actionId:i,input:d,state:e,streamPosition:0,toolUse:n,onDelivered:p})),g=m.state??e;"),
        ("let p=await this.handle({actionId:r,input:s,state:d,streamPosition:0,toolUse:n,onDelivered:a});",
         "let p=await kiroSdkHandleTool(this,{actionId:r,input:s,state:d,modelInput:i},()=>this.handle("
         "{actionId:r,input:s,state:d,streamPosition:0,toolUse:n,onDelivered:a}));"),
        ("let k=()=>e.requestPermission({sessionId:n,toolCall:{toolCallId:x.toolCallId,status:\"pending\",title:b,",
         "let k=()=>kiroSdkRequestPermission(e,d,p,{sessionId:n,toolCall:{toolCallId:x.toolCallId,status:\"pending\",title:b,"
         "...kiroSdkPermissionInput(d,p,n),"),
    )
