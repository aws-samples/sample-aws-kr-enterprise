"""Request-scoped model controls for the pinned runtime."""

JAVASCRIPT = r'''
function kiroSdkEffortLevel(execution,model){
    const scope=kiroSdkChildScopes.get(execution);
    if(!scope||typeof execution.agentName!=="string"||
       !Object.hasOwn(scope.policy.agentEfforts??{},execution.agentName))return model.effortLevel;
    const effort=scope.policy.agentEfforts?.[execution.agentName];
    if(effort===undefined)return model.effortLevel;
    if(!kiroSdkScopeActive(scope))throw new Error("SDK child request has ended");
    if(!model.effortSchemaPath||!model.effortLevels?.includes(effort))
        throw new Error("Kiro child model does not support the requested effort");
    return effort;
}
function kiroSdkThinkingType(execution,requested,modelId){
    const scope=kiroSdkExecutionScope(execution);
    if(!scope?.policy.thinkingDisabled)return requested;
    if(!kiroSdkScopeActive(scope))throw new Error("SDK model request has ended");
    if(!CMe(modelId))throw new Error("Kiro model cannot confirm disabled thinking");
    return kC;
}
function kiroSdkFallback(execution,original){
    const policy=kiroSdkDefinitionPolicies.get(execution.definition);
    if(!policy?.fallbackModel)return original;
    const target=policy.fallbackModel;
    return {
        isEnabled:trigger=>trigger==="availability",
        isAutoRefusalEnabled:()=>false,
        resolveTarget:async(cursor,current,trigger)=>{
            if(policy.closed||execution.abortController.signal.aborted)
                return {kind:"declined",reason:"raceLost"};
            if(trigger!=="availability")return {kind:"declined",reason:"notModelAttributable"};
            if(policy.fallbackApplied||current===target||cursor.tried.includes(target))
                return {kind:"declined",reason:"chainExhausted"};
            if(!zd().models.some(model=>model.id===target))
                return {kind:"declined",reason:"noServedTarget"};
            return {kind:"resolved",targetModelId:target};
        },
        // Swap only this execution. Never persist an SDK fallback as a new
        // session default or replay completed tools from an earlier model round.
        applySwitch:async()=>"turn",
    };
}
function kiroSdkRecordFallback(execution,result){
    const policy=kiroSdkDefinitionPolicies.get(execution.definition);
    if(policy?.fallbackModel&&result?.kind==="applied"&&result.target?.modelId===policy.fallbackModel)
        policy.fallbackApplied=true;
    return result;
}
'''


def runtime_model_patches() -> tuple[tuple[str, str], ...]:
    return (
        ("buildModelFallbackOption(e){let n=this.sessionServices.modelFallback;if(!n)return{};",
         "buildModelFallbackOption(e){let n=kiroSdkFallback(this,this.sessionServices.modelFallback);if(!n)return{};"),
        ("return{modelFallback:{triggers:r,port:s=>jSo(o,n,s)}}}",
         "return{modelFallback:{triggers:r,port:async s=>kiroSdkRecordFallback(this,await jSo(o,n,s))}}}"),
        ("additionalModelRequestFields(e=this.model){return SEt({effortLevel:e.effortLevel,effortSchemaPath:e.effortSchemaPath,thinkingType:e.thinkingType,effortLevels:e.effortLevels})}",
         "additionalModelRequestFields(e=this.model){return SEt({effortLevel:kiroSdkEffortLevel(this,e),effortSchemaPath:e.effortSchemaPath,"
         "thinkingType:kiroSdkThinkingType(this,e.thinkingType,e.modelId),effortLevels:e.effortLevels})}"),
    )
