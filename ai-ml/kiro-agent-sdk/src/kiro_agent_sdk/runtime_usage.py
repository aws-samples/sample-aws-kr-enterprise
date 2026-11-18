"""Reported Kiro stream-attempt token metrics, with explicit coverage limits."""
from __future__ import annotations

from typing import Any

from .errors import ProtocolError

TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
MAX_ATTEMPTS = 128
MAX_INTEGER = 9007199254740991

JAVASCRIPT = r'''
const kiroSdkUsageStorage=new KiroSdkPolicyStorage();
function kiroSdkObserveModelUsage(execution,options,run){
    const policy=kiroSdkDefinitionPolicies.get(execution.definition);
    const scope=kiroSdkExecutionScope(execution);
    const context=policy?.usageMode==="reported"&&scope?.depth===0&&
        execution.chatSessionId===policy.sessionId&&typeof policy.sessionId==="string"&&
        !kiroSdkChildScopes.has(execution)&&kiroSdkScopeActive(scope)&&options?.summaryRequest!==true?
        {policy,scope}:null;
    // Scrub an outer observer when a nested auxiliary stream is not in scope.
    // The stream consumer's async resources retain this context after run returns.
    return kiroSdkUsageStorage.run(context,run);
}
function kiroSdkUsageBegin(model){
    const context=kiroSdkUsageStorage.getStore();
    if(!context||!kiroSdkScopeActive(context.scope))return null;
    const report=context.policy.usageReport;
    report.attempts=Math.min(report.attempts+1,9007199254740991);
    if(report.records.length>=128){report.truncated=true;return null}
    const validModel=typeof model==="string"&&model.length>0&&model.length<=128&&
        !/[\u0000-\u001f\u007f-\u009f]/.test(model)&&model.isWellFormed();
    const row={index:report.attempts,modelId:validModel?model:null,status:"unfinished",httpAttempts:null,
        invalid:model!==undefined&&model!==null&&!validModel,tokens:{
            inputTokens:null,outputTokens:null,cacheReadInputTokens:null,cacheWriteInputTokens:null}};
    report.records.push(row);
    return {context,row,reported:false};
}
function kiroSdkUsageReport(token,metrics,httpAttempts){
    if(!token||!kiroSdkScopeActive(token.context.scope))return;
    if(token.reported){token.row.invalid=true;return}
    token.reported=true;
    const row=token.row;
    for(const field of Object.keys(row.tokens)){
        const value=metrics?.[field];
        if(value===undefined)continue;
        if(Number.isSafeInteger(value)&&value>=0)row.tokens[field]=value;
        else row.invalid=true;
    }
    if(httpAttempts!==undefined){
        if(Number.isSafeInteger(httpAttempts)&&httpAttempts>=1)row.httpAttempts=httpAttempts;
        else row.invalid=true;
    }
}
function kiroSdkUsageFinish(token,status){
    if(token&&kiroSdkScopeActive(token.context.scope)&&token.row.status==="unfinished")
        token.row.status=status;
}
function kiroSdkUsageSnapshot(policy){
    if(policy.usageMode!=="reported")return null;
    const value=policy.usageReport;
    return {protocol:1,scope:"main-stream-attempts",attempts:value.attempts,truncated:value.truncated,
        records:value.records.map(row=>({...row,tokens:{...row.tokens}}))};
}
'''


def runtime_usage_patches() -> tuple[tuple[str, str], ...]:
    return (
        ("async modelStream2(e,n,r,i){",
         "async modelStream2(e,n,r,i){return kiroSdkObserveModelUsage(this,i,"
         "()=>this.kiroSdkModelStream2(e,n,r,i))}async kiroSdkModelStream2(e,n,r,i){"),
        ("async*_streamResponseChunksOnce(n,r,i,o=1,s,a){",
         "async*_streamResponseChunksOnce(n,r,i,o=1,s,a){"
         "const sdkUsage=kiroSdkUsageBegin(this.modelType);let sdkFinished=false;"
         "try{const value=yield* this.kiroSdkStreamResponseChunksOnce(n,r,i,o,s,a,sdkUsage);"
         'sdkFinished=true;return value}catch(error){kiroSdkUsageFinish(sdkUsage,_r(error)?"cancelled":"failed");'
         'throw error}finally{kiroSdkUsageFinish(sdkUsage,sdkFinished?"completed":"cancelled")}}'
         "async*kiroSdkStreamResponseChunksOnce(n,r,i,o=1,s,a,sdkUsage){"),
        ("let Pe=Rte(ve?.message);",
         "let Pe=Rte(ve?.message);kiroSdkUsageReport(sdkUsage,Pe,re.$metadata.attempts);"),
    )


def validate_usage_report(value: Any, *, enabled: bool) -> Any:
    if not enabled:
        if value is not None:
            raise ProtocolError("Kiro returned unrequested model usage")
        return None
    if (type(value) is not dict or set(value) != {"protocol", "scope", "attempts", "truncated", "records"} or
            type(value["protocol"]) is not int or value["protocol"] != 1 or
            value["scope"] != "main-stream-attempts" or type(value["attempts"]) is not int or
            not 0 <= value["attempts"] <= MAX_INTEGER or type(value["truncated"]) is not bool or
            type(value["records"]) is not list or len(value["records"]) > MAX_ATTEMPTS or
            (value["truncated"] and (len(value["records"]) != MAX_ATTEMPTS or value["attempts"] <= MAX_ATTEMPTS)) or
            (not value["truncated"] and value["attempts"] != len(value["records"]))):
        raise ProtocolError("Invalid Kiro model usage report")
    records = []
    for index, row in enumerate(value["records"], 1):
        if (type(row) is not dict or
                set(row) != {"index", "modelId", "status", "httpAttempts", "invalid", "tokens"} or
                type(row["index"]) is not int or row["index"] != index or
                type(row["status"]) is not str or row["status"] not in ("unfinished", "completed", "failed", "cancelled") or
                type(row["invalid"]) is not bool or type(row["tokens"]) is not dict or
                set(row["tokens"]) != set(TOKEN_FIELDS) or
                any(number is not None and (type(number) is not int or not 0 <= number <= MAX_INTEGER)
                    for number in row["tokens"].values()) or
                (row["httpAttempts"] is not None and (type(row["httpAttempts"]) is not int or
                                                     not 1 <= row["httpAttempts"] <= MAX_INTEGER))):
            raise ProtocolError("Invalid Kiro model usage attempt")
        model = row["modelId"]
        if model is not None and (
                type(model) is not str or not 0 < len(model) <= 128 or
                any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in model)):
            raise ProtocolError("Invalid Kiro usage model identifier")
        records.append({**row, "tokens": dict(row["tokens"])})
    scope_complete = (bool(records) and not value["truncated"] and
                      all(row["status"] == "completed" and row["httpAttempts"] == 1 and not row["invalid"]
                          for row in records))
    reported, totals = {}, {}
    for field in TOKEN_FIELDS:
        known = [row["tokens"][field] for row in records if row["tokens"][field] is not None]
        total = sum(known)
        reported[field] = total if known and total <= MAX_INTEGER else None
        totals[field] = reported[field] if scope_complete and len(known) == len(records) else None
    return {**value, "records": records, "totals": totals, "reported_totals": reported,
            "complete": all(amount is not None for amount in totals.values())}


def validate_normalized_usage_report(value: Any) -> dict[str, Any]:
    """Recheck a retained report, including all derived sums and coverage flags."""
    raw_keys = ("protocol", "scope", "attempts", "truncated", "records")
    if type(value) is not dict or set(value) != set(raw_keys) | {"totals", "reported_totals", "complete"}:
        raise ValueError("Invalid retained Kiro model usage")
    for key in ("totals", "reported_totals"):
        if (type(value[key]) is not dict or set(value[key]) != set(TOKEN_FIELDS) or
                any(number is not None and (type(number) is not int or not 0 <= number <= MAX_INTEGER)
                    for number in value[key].values())):
            raise ValueError("Invalid retained Kiro usage totals")
    try:
        checked = validate_usage_report({key: value[key] for key in raw_keys}, enabled=True)
    except ProtocolError as exc:
        raise ValueError("Invalid retained Kiro model usage") from exc
    if (type(value["complete"]) is not bool or value["complete"] is not checked["complete"] or
            value["totals"] != checked["totals"] or value["reported_totals"] != checked["reported_totals"]):
        raise ValueError("Retained Kiro usage totals do not match the reported attempts")
    return checked
