"""Agent Observability API. X-Ray + CloudWatch Logs 기반 trace/span 조회."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/obs", tags=["observability"])
logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
xray = boto3.client("xray", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)

# OTel spans are written to this log group by AgentCore only after agents emit
# traces. Until the first invocation it does not exist, which is a normal empty
# state — callers should get empty results, not a 500.
SPANS_LOG_GROUP = "aws/spans"


async def _run_logs_insights_query(query: str, start_time: int, end_time: int) -> dict:
    """Run a CloudWatch Logs Insights query against the spans log group.

    Returns the completed query result, or an empty result set if the spans
    log group does not exist yet (no traces emitted).
    """
    try:
        query_res = await asyncio.to_thread(
            logs.start_query,
            logGroupName=SPANS_LOG_GROUP,
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
    except logs.exceptions.ResourceNotFoundException:
        logger.info("spans log group %s not found yet — returning empty result", SPANS_LOG_GROUP)
        return {"results": [], "status": "Complete"}

    query_id = query_res["queryId"]
    result: dict = {}
    for _ in range(20):
        await asyncio.sleep(1)
        result = await asyncio.to_thread(logs.get_query_results, queryId=query_id)
        if result["status"] == "Complete":
            break
    return result


@router.get("/sessions")
async def list_sessions(
    hours: int = Query(default=1, description="최근 N시간"),
    agent_id: str = Query(default="", description="특정 Agent로 필터"),
):
    """CloudWatch Logs Insights → invoke_agent span 기반 Agent trace 목록."""
    end_time = int(time.time())
    start_time = end_time - hours * 3600

    filter_clause = "filter name like /invoke_agent/"
    if agent_id:
        filter_clause += f' and attributes.`agent.id` = "{agent_id}"'

    query = f"""{filter_clause}
| fields @timestamp, traceId, name, durationNano, @message
| sort @timestamp desc
| limit 50"""

    result = await _run_logs_insights_query(query, start_time, end_time)

    sessions: dict[str, list] = {}
    for row in result.get("results", []):
        record = {f["field"]: f["value"] for f in row}
        duration_nano = int(record.get("durationNano", 0) or 0)

        attrs: dict = {}
        res_attrs: dict = {}
        msg = record.get("@message", "")
        try:
            span_doc = json.loads(msg)
            attrs = span_doc.get("attributes", {})
            res_attrs = span_doc.get("resource", {}).get("attributes", {})
        except (json.JSONDecodeError, KeyError):
            pass

        service = (
            attrs.get("aws.local.service")
            or res_attrs.get("aws.local.service")
            or res_attrs.get("service.name")
            or "unknown"
        )

        sessions.setdefault(service, []).append(
            {
                "traceId": record.get("traceId", ""),
                "agentName": attrs.get("gen_ai.agent.name", ""),
                "model": attrs.get("gen_ai.request.model", ""),
                "inputTokens": int(attrs.get("gen_ai.usage.input_tokens", 0) or 0),
                "outputTokens": int(attrs.get("gen_ai.usage.output_tokens", 0) or 0),
                "totalTokens": int(attrs.get("gen_ai.usage.total_tokens", 0) or 0),
                "sessionId": attrs.get("session.id", ""),
                "duration": round(duration_nano / 1_000_000_000, 3)
                if duration_nano
                else None,
                "startTime": record.get("@timestamp", ""),
            }
        )
    return {
        "sessions": sessions,
        "totalTraces": sum(len(v) for v in sessions.values()),
    }


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    """X-Ray BatchGetTraces -> span tree 반환."""
    res = await asyncio.to_thread(xray.batch_get_traces, TraceIds=[trace_id])
    spans = []
    for trace in res.get("Traces", []):
        for seg in trace.get("Segments", []):
            doc = json.loads(seg.get("Document", "{}"))
            spans.append(_flatten_segment(doc))
            for sub in doc.get("subsegments", []):
                spans.append(_flatten_segment(sub, parent_id=doc.get("id", "")))
    return {"traceId": trace_id, "spans": spans, "spanCount": len(spans)}


@router.get("/traces/{trace_id}/logs")
async def get_trace_logs(trace_id: str):
    """CloudWatch Logs Insights -> 특정 trace의 전체 OTel span payload 조회."""
    # X-Ray format "1-xxx-yyy" → OTel format "xxxyyy" (하이픈/prefix 제거)
    otel_trace_id = (
        trace_id[2:].replace("-", "") if trace_id.startswith("1-") else trace_id
    )
    query = f"""filter traceId = "{otel_trace_id}"
| fields @timestamp, name, spanId, parentSpanId, durationNano, kind, status.code, @message
| sort startTimeUnixNano asc
| limit 100"""

    end_time = int(time.time())
    start_time = end_time - 86400 * 3

    result = await _run_logs_insights_query(query, start_time, end_time)

    otel_spans = []
    for row in result.get("results", []):
        record = {f["field"]: f["value"] for f in row}
        msg = record.get("@message", "")
        try:
            span_doc = json.loads(msg)
            attrs = span_doc.get("attributes", {})
            otel_spans.append(
                {
                    "name": span_doc.get("name", ""),
                    "spanId": span_doc.get("spanId", ""),
                    "parentSpanId": span_doc.get("parentSpanId", ""),
                    "startTimeUnixNano": span_doc.get("startTimeUnixNano"),
                    "endTimeUnixNano": span_doc.get("endTimeUnixNano"),
                    "operation": attrs.get("gen_ai.operation.name", ""),
                    "agentName": attrs.get("gen_ai.agent.name", ""),
                    "model": attrs.get("gen_ai.request.model", ""),
                    "toolName": attrs.get("gen_ai.tool.name", ""),
                    "toolCallId": attrs.get("gen_ai.tool.call.id", ""),
                    "toolStatus": attrs.get("gen_ai.tool.status", ""),
                    "inputTokens": _safe_int(attrs.get("gen_ai.usage.input_tokens")),
                    "outputTokens": _safe_int(attrs.get("gen_ai.usage.output_tokens")),
                    "totalTokens": _safe_int(attrs.get("gen_ai.usage.total_tokens")),
                    "ttft": _safe_float(attrs.get("gen_ai.server.time_to_first_token")),
                    "requestDuration": _safe_float(
                        attrs.get("gen_ai.server.request.duration")
                    ),
                    "sessionId": attrs.get("session.id", ""),
                    "service": attrs.get("aws.local.service", ""),
                }
            )
        except (json.JSONDecodeError, KeyError):
            otel_spans.append(
                {
                    "name": record.get("name", "unknown"),
                    "spanId": record.get("spanId", ""),
                    "parentSpanId": record.get("parentSpanId", ""),
                    "startTimeUnixNano": None,
                    "endTimeUnixNano": None,
                    "operation": "",
                    "agentName": "",
                    "model": "",
                    "toolName": "",
                    "toolCallId": "",
                    "toolStatus": "",
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "totalTokens": 0,
                    "ttft": None,
                    "requestDuration": None,
                    "sessionId": "",
                    "service": "",
                    "raw": record,
                }
            )

    return {"traceId": trace_id, "spans": otel_spans, "spanCount": len(otel_spans)}


@router.get("/service-map")
async def get_service_map(hours: int = Query(default=1)):
    """X-Ray ServiceGraph -> agent 간 호출 관계."""
    now = time.time()
    res = await asyncio.to_thread(
        xray.get_service_graph,
        StartTime=datetime.fromtimestamp(now - hours * 3600, tz=timezone.utc),
        EndTime=datetime.fromtimestamp(now, tz=timezone.utc),
    )
    services = []
    for svc in res.get("Services", []):
        services.append(
            {
                "name": svc.get("Name", ""),
                "type": svc.get("Type", ""),
                "edges": [
                    {
                        "target": e.get("ReferenceId"),
                        "aliases": e.get("Aliases", []),
                    }
                    for e in svc.get("Edges", [])
                ],
                "summaryStats": svc.get("SummaryStatistics", {}),
            }
        )
    return {"services": services, "count": len(services)}


@router.get("/metrics")
async def get_metrics(
    agent_id: str = Query(default="", description="Agent Runtime service name"),
    minutes: int = Query(default=30),
):
    """CloudWatch Metrics -> Agent Runtime invocation/latency 메트릭."""
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=minutes)

    queries = [
        {
            "Id": "invocations",
            "MetricStat": {
                "Metric": {
                    "Namespace": "bedrock-agentcore",
                    "MetricName": "Invocations",
                    "Dimensions": [{"Name": "ServiceName", "Value": agent_id}]
                    if agent_id
                    else [],
                },
                "Period": 60,
                "Stat": "Sum",
            },
        },
        {
            "Id": "latency",
            "MetricStat": {
                "Metric": {
                    "Namespace": "bedrock-agentcore",
                    "MetricName": "Latency",
                    "Dimensions": [{"Name": "ServiceName", "Value": agent_id}]
                    if agent_id
                    else [],
                },
                "Period": 60,
                "Stat": "Average",
            },
        },
    ]

    res = await asyncio.to_thread(
        cw.get_metric_data,
        MetricDataQueries=queries,
        StartTime=start_time,
        EndTime=end_time,
    )

    metrics = {}
    for mr in res.get("MetricDataResults", []):
        metrics[mr["Id"]] = {
            "timestamps": [t.isoformat() for t in mr.get("Timestamps", [])],
            "values": mr.get("Values", []),
        }
    return {"metrics": metrics, "agentId": agent_id}


@router.get("/sessions/{session_id}/traces")
async def get_session_traces(session_id: str, hours: int = Query(default=24)):
    """특정 session_id에 속하는 모든 trace를 조회. session.id span attribute 기반."""
    end_time = int(time.time())
    start_time = end_time - hours * 3600

    query = f"""filter attributes.`session.id` = "{session_id}"
| fields @timestamp, traceId, name, spanId, parentSpanId, durationNano, @message
| stats count() as spanCount by traceId
| sort @timestamp desc
| limit 50"""

    result = await _run_logs_insights_query(query, start_time, end_time)

    traces = []
    for row in result.get("results", []):
        record = {f["field"]: f["value"] for f in row}
        traces.append(
            {
                "traceId": record.get("traceId", ""),
                "spanCount": int(record.get("spanCount", 0) or 0),
                "timestamp": record.get("@timestamp", ""),
            }
        )

    return {
        "sessionId": session_id,
        "traces": traces,
        "traceCount": len(traces),
    }


def _safe_int(val: object) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _safe_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _flatten_segment(doc: dict, parent_id: str = "") -> dict:
    """X-Ray segment/subsegment -> flat dict."""
    return {
        "id": doc.get("id", ""),
        "name": doc.get("name", ""),
        "parentId": parent_id,
        "startTime": doc.get("start_time"),
        "endTime": doc.get("end_time"),
        "duration": (doc.get("end_time", 0) - doc.get("start_time", 0))
        if doc.get("end_time") and doc.get("start_time")
        else None,
        "fault": doc.get("fault", False),
        "error": doc.get("error", False),
        "annotations": doc.get("annotations", {}),
        "metadata": doc.get("metadata", {}),
    }
