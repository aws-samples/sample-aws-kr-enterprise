"""Leaderboard reader Lambda — serves top-N and user rank via API Gateway.

Routes:
  GET /leaderboard?gameId=X&limit=100&userId=U
  GET /rank/{userId}?gameId=X
  DELETE /admin/flush?gameId=X
  GET /admin/zcard?gameId=X
  GET /admin/info
  GET /admin/metrics
"""

import json
import os
import re
from datetime import datetime, timezone, timedelta

import boto3
from botocore.config import Config
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from shared.valkey_client import get_valkey_client

logger = Logger(service="leaderboard-reader")

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# Input validation patterns
GAME_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
USER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")

# Cached AWS clients (singleton for Lambda execution context reuse)
_cloudwatch_client = None
_sqs_client = None


def _get_cloudwatch_client():
    """Return a cached boto3 CloudWatch client with appropriate timeout."""
    global _cloudwatch_client
    if _cloudwatch_client is None:
        region = os.environ.get("AWS_REGION", "us-east-1")
        cw_config = Config(
            connect_timeout=2,
            read_timeout=3,
            retries={"max_attempts": 1},
        )
        _cloudwatch_client = boto3.client("cloudwatch", region_name=region, config=cw_config)
    return _cloudwatch_client


def _get_sqs_client():
    """Return a cached boto3 SQS client with short timeout (live-depth probe)."""
    global _sqs_client
    if _sqs_client is None:
        region = os.environ.get("AWS_REGION", "us-east-1")
        sqs_config = Config(
            connect_timeout=1,
            read_timeout=2,
            retries={"max_attempts": 1},
        )
        _sqs_client = boto3.client("sqs", region_name=region, config=sqs_config)
    return _sqs_client


def _live_sqs_depth() -> tuple[str, float] | None:
    """Fetch near-real-time SQS queue depth via GetQueueAttributes.

    CloudWatch AWS/SQS metrics lag 1-2 minutes; this polls directly for a
    ~1-second-fresh datapoint that the frontend can surface as 'now'.
    Returns (iso_timestamp, depth) or None if the call fails.
    """
    queue_url = os.environ.get("SQS_QUEUE_URL")
    if not queue_url:
        return None
    try:
        sqs = _get_sqs_client()
        resp = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        depth = float(resp["Attributes"]["ApproximateNumberOfMessages"])
        ts = datetime.now(timezone.utc).isoformat()
        return (ts, depth)
    except Exception as e:
        logger.warning("Failed to fetch live SQS depth", extra={"error": str(e)})
        return None


def _build_response(status_code: int, body: dict) -> dict:
    """Build an API Gateway HTTP API response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _validate_game_id(game_id: str | None) -> dict | None:
    """Validate gameId. Returns error response dict if invalid, None if valid."""
    if not game_id:
        return _build_response(400, {"error": "gameId query parameter is required"})
    if not GAME_ID_PATTERN.match(game_id):
        return _build_response(
            400,
            {"error": "gameId must be 1-64 alphanumeric, dash, or underscore characters"},
        )
    return None


def _validate_user_id(user_id: str | None) -> dict | None:
    """Validate userId. Returns error response dict if invalid, None if valid."""
    if not user_id:
        return _build_response(400, {"error": "userId is required"})
    if not USER_ID_PATTERN.match(user_id):
        return _build_response(
            400,
            {"error": "userId must be 1-128 alphanumeric, dash, underscore, or dot characters"},
        )
    return None


def _handle_leaderboard(params: dict) -> dict:
    """Handle GET /leaderboard — returns top-N players and optionally the caller's rank."""
    game_id = params.get("gameId")
    if err := _validate_game_id(game_id):
        return err

    # Parse limit (default 100, max 500)
    try:
        limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        if limit < 1:
            limit = DEFAULT_LIMIT
    except (ValueError, TypeError):
        limit = DEFAULT_LIMIT

    user_id = params.get("userId")
    if user_id:
        if err := _validate_user_id(user_id):
            return err

    valkey = get_valkey_client()
    valkey_key = f"lb:{game_id}"

    # Get top-N players with scores (descending)
    top_raw = valkey.zrevrange(valkey_key, 0, limit - 1, withscores=True)

    # Build top list
    top = []
    for rank_idx, (member, score) in enumerate(top_raw):
        top.append(
            {
                "userId": member,
                "score": score,
                "rank": rank_idx + 1,
            }
        )

    response_body = {"top": top}

    # If userId provided, get their rank and score
    if user_id:
        rank = valkey.zrevrank(valkey_key, user_id)
        score = valkey.zscore(valkey_key, user_id)

        if rank is not None:
            response_body["me"] = {
                "userId": user_id,
                "rank": rank + 1,  # Convert 0-indexed to 1-indexed
                "score": score,
            }
        else:
            response_body["me"] = None

    return _build_response(200, response_body)


def _handle_rank(user_id: str, params: dict) -> dict:
    """Handle GET /rank/{userId} — returns a single user's rank and score."""
    if err := _validate_user_id(user_id):
        return err

    game_id = params.get("gameId")
    if err := _validate_game_id(game_id):
        return err

    valkey = get_valkey_client()
    valkey_key = f"lb:{game_id}"

    rank = valkey.zrevrank(valkey_key, user_id)
    score = valkey.zscore(valkey_key, user_id)

    if rank is None:
        return _build_response(
            404, {"error": f"User '{user_id}' not found in game '{game_id}'"}
        )

    return _build_response(
        200,
        {
            "userId": user_id,
            "rank": rank + 1,  # Convert 0-indexed to 1-indexed
            "score": score,
            "gameId": game_id,
        },
    )


def _handle_admin_flush(params: dict) -> dict:
    """Handle DELETE /admin/flush — removes a game's leaderboard from Valkey."""
    game_id = params.get("gameId")
    if err := _validate_game_id(game_id):
        return err

    valkey = get_valkey_client()
    valkey_key = f"lb:{game_id}"
    deleted = valkey.delete(valkey_key)

    return _build_response(200, {"flushed": game_id, "keysDeleted": deleted})


def _handle_admin_zcard(params: dict) -> dict:
    """Handle GET /admin/zcard — returns the number of members in a game's leaderboard."""
    game_id = params.get("gameId")
    if err := _validate_game_id(game_id):
        return err

    valkey = get_valkey_client()
    valkey_key = f"lb:{game_id}"
    count = valkey.zcard(valkey_key)

    return _build_response(200, {"gameId": game_id, "count": count})


def _handle_admin_info() -> dict:
    """Handle GET /admin/info — returns Valkey server memory info."""
    valkey = get_valkey_client()
    info = valkey.info("memory")

    return _build_response(
        200,
        {
            "used_memory_bytes": info.get("used_memory", 0),
            "used_memory_human": info.get("used_memory_human", "unknown"),
            "maxmemory": info.get("maxmemory", 0),
        },
    )


def _handle_admin_metrics() -> dict:
    """Handle GET /admin/metrics — returns CloudWatch metrics for the dashboard.

    Fetches the last 10 minutes of:
    - SQS ApproximateNumberOfMessagesVisible (Period 60s)
    - Lambda Invocations (processor) (Period 60s)
    - Lambda Errors (processor) (Period 60s)
    - ElastiCache EngineCPUUtilization — per-node, aggregated to Maximum (Period 60s)
    - Custom Leaderboard/end_to_end_latency_ms (HighResolution, Period 10s)
    """
    cloudwatch = _get_cloudwatch_client()

    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=10)

    # Read metric dimension values from environment (set by CDK)
    queue_name = os.environ.get("METRICS_QUEUE_NAME", "leaderboard-score-events")
    processor_fn = os.environ.get("METRICS_PROCESSOR_FN", "leaderboard-score-processor")
    processor_service = os.environ.get("METRICS_PROCESSOR_SERVICE", "score-processor")
    valkey_nodes_env = os.environ.get(
        "METRICS_VALKEY_NODES", "leaderboard-valkey-001,leaderboard-valkey-002"
    )
    valkey_nodes = [n.strip() for n in valkey_nodes_env.split(",") if n.strip()]

    # AWS/* standard-resolution metrics — Period 60s
    metric_queries = [
        {
            "Id": "sqs_depth",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/SQS",
                    "MetricName": "ApproximateNumberOfMessagesVisible",
                    "Dimensions": [
                        {"Name": "QueueName", "Value": queue_name}
                    ],
                },
                "Period": 60,
                "Stat": "Average",
            },
            "ReturnData": True,
        },
        {
            "Id": "lambda_invocations",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Invocations",
                    "Dimensions": [
                        {"Name": "FunctionName", "Value": processor_fn}
                    ],
                },
                "Period": 60,
                "Stat": "Sum",
            },
            "ReturnData": True,
        },
        {
            "Id": "lambda_errors",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Errors",
                    "Dimensions": [
                        {"Name": "FunctionName", "Value": processor_fn}
                    ],
                },
                "Period": 60,
                "Stat": "Sum",
            },
            "ReturnData": True,
        },
        # HighResolution custom metric — Period 10s for near-real-time latency
        {
            "Id": "e2e_latency",
            "MetricStat": {
                "Metric": {
                    "Namespace": "Leaderboard",
                    "MetricName": "end_to_end_latency_ms",
                    "Dimensions": [
                        {"Name": "service", "Value": processor_service}
                    ],
                },
                "Period": 10,
                "Stat": "Average",
            },
            "ReturnData": True,
        },
    ]

    # ElastiCache EngineCPUUtilization is published per CacheClusterId node.
    # Query each node separately and aggregate (Maximum) client-side.
    valkey_query_ids: list[str] = []
    for idx, node_id in enumerate(valkey_nodes):
        query_id = f"valkey_cpu_n{idx}"
        valkey_query_ids.append(query_id)
        metric_queries.append(
            {
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/ElastiCache",
                        "MetricName": "EngineCPUUtilization",
                        "Dimensions": [
                            {"Name": "CacheClusterId", "Value": node_id}
                        ],
                    },
                    "Period": 60,
                    "Stat": "Average",
                },
                "ReturnData": True,
            }
        )

    try:
        response = cloudwatch.get_metric_data(
            MetricDataQueries=metric_queries,
            StartTime=start_time,
            EndTime=now,
            ScanBy="TimestampAscending",
        )
    except Exception as e:
        logger.error("Failed to fetch CloudWatch metrics", extra={"error": str(e)})
        return _build_response(502, {"error": "Failed to fetch metrics from CloudWatch"})

    # Build response from metric results
    raw_results: dict[str, dict] = {}
    for metric_result in response.get("MetricDataResults", []):
        metric_id = metric_result["Id"]
        timestamps = [
            ts.isoformat() for ts in metric_result.get("Timestamps", [])
        ]
        values = metric_result.get("Values", [])
        raw_results[metric_id] = {
            "timestamps": timestamps,
            "values": values,
            "label": metric_result.get("Label", metric_id),
        }

    # Aggregate per-node Valkey CPU into a single `valkey_cpu` series (Maximum).
    valkey_series = [raw_results.pop(qid, None) for qid in valkey_query_ids]
    valkey_series = [s for s in valkey_series if s is not None]
    raw_results["valkey_cpu"] = _merge_max_series(valkey_series, label="Valkey CPU (max)")

    # Keep only the 5 canonical metric IDs in the response
    wanted_ids = {
        "sqs_depth",
        "lambda_invocations",
        "lambda_errors",
        "valkey_cpu",
        "e2e_latency",
    }
    results = {k: v for k, v in raw_results.items() if k in wanted_ids}

    # Append a live SQS depth datapoint — CloudWatch AWS/SQS lags 1-2 minutes.
    live = _live_sqs_depth()
    if live is not None:
        ts, depth = live
        sqs_entry = results.setdefault(
            "sqs_depth", {"timestamps": [], "values": [], "label": "SQS Depth (live)"}
        )
        sqs_entry["timestamps"].append(ts)
        sqs_entry["values"].append(depth)
        sqs_entry["label"] = "SQS Depth (live)"

    return _build_response(200, results)


def _merge_max_series(series_list: list[dict], label: str) -> dict:
    """Merge multiple (timestamps, values) series into one via Maximum per timestamp.

    Each input series shares the same Period, so timestamps align (minor drift OK).
    """
    if not series_list:
        return {"timestamps": [], "values": [], "label": label}

    # Aggregate by timestamp → max value across nodes
    bucket: dict[str, float] = {}
    for series in series_list:
        for ts, val in zip(series.get("timestamps", []), series.get("values", [])):
            if ts not in bucket or val > bucket[ts]:
                bucket[ts] = val

    sorted_items = sorted(bucket.items(), key=lambda kv: kv[0])
    return {
        "timestamps": [ts for ts, _ in sorted_items],
        "values": [v for _, v in sorted_items],
        "label": label,
    }


@logger.inject_lambda_context
def handler(event: dict, context: LambdaContext) -> dict:
    """Route API Gateway HTTP API requests to the appropriate handler."""
    route_key = event.get("routeKey", "")
    path_parameters = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    logger.info(
        "Handling request",
        extra={"route_key": route_key, "query_params": query_params},
    )

    if route_key == "GET /leaderboard":
        return _handle_leaderboard(query_params)
    elif route_key == "GET /rank/{userId}":
        user_id = path_parameters.get("userId", "")
        if not user_id:
            return _build_response(400, {"error": "userId path parameter is required"})
        return _handle_rank(user_id, query_params)
    elif route_key == "DELETE /admin/flush":
        return _handle_admin_flush(query_params)
    elif route_key == "GET /admin/zcard":
        return _handle_admin_zcard(query_params)
    elif route_key == "GET /admin/info":
        return _handle_admin_info()
    elif route_key == "GET /admin/metrics":
        return _handle_admin_metrics()
    else:
        return _build_response(404, {"error": f"Route not found: {route_key}"})
