"""SQS batch processor Lambda — writes events to DynamoDB and updates Valkey.

This handler:
1. Iterates over SQS batch records
2. For each: conditional PutItem to DDB (idempotency) → ZINCRBY in Valkey
3. Reports partial batch failures for retry
4. Emits EMF metrics: end_to_end_latency_ms, duplicate_event_count
"""

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricResolution, MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from shared.ddb_helpers import put_event_idempotent
from shared.valkey_client import get_valkey_client

logger = Logger(service="score-processor")
metrics = Metrics(namespace="Leaderboard", service="score-processor")

DDB_TABLE_NAME = os.environ["DDB_TABLE_NAME"]


def _compute_ttl_epoch(days: int = 90) -> int:
    """Return epoch seconds for TTL (current time + days)."""
    return int(time.time()) + (days * 86400)


def _parse_event_body(body: str) -> dict:
    """Parse SQS message body into an event dict."""
    event = json.loads(body)
    required_fields = {"eventId", "userId", "gameId", "score", "ts"}
    missing = required_fields - set(event.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")
    return event


@metrics.log_metrics(capture_cold_start_metric=True)
@logger.inject_lambda_context
def handler(event: dict, context: LambdaContext) -> dict:
    """Process SQS batch of score events.

    Returns batchItemFailures for partial retry support.
    """
    records = event.get("Records", [])
    batch_item_failures = []

    for record in records:
        message_id = record["messageId"]

        try:
            body = _parse_event_body(record["body"])

            event_id = body["eventId"]
            user_id = body["userId"]
            game_id = body["gameId"]
            score = body["score"]
            event_ts = body["ts"]

            # Build DynamoDB item
            sort_key = f"{event_ts}#{event_id}"
            ddb_item = {
                "gameId": game_id,
                "ts#eventId": sort_key,
                "eventId": event_id,
                "userId": user_id,
                "score": Decimal(str(score)),
                "ingestedAt": datetime.now(timezone.utc).isoformat(),
                "ttl": _compute_ttl_epoch(90),
            }

            # Add optional sourceGame field if present
            if "sourceGame" in body:
                ddb_item["sourceGame"] = body["sourceGame"]

            # Conditional PutItem — idempotency guard
            is_new = put_event_idempotent(DDB_TABLE_NAME, ddb_item)

            if is_new:
                # New event — update Valkey leaderboard
                valkey = get_valkey_client()
                valkey_key = f"lb:{game_id}"
                valkey.zincrby(valkey_key, score, user_id)

                # Emit end-to-end latency metric
                try:
                    event_time = datetime.fromisoformat(
                        event_ts.replace("Z", "+00:00")
                    )
                    now = datetime.now(timezone.utc)
                    latency_ms = (now - event_time).total_seconds() * 1000
                    # HighResolution(1s) publish so dashboard can use 10s Period
                    metrics.add_metric(
                        name="end_to_end_latency_ms",
                        unit=MetricUnit.Milliseconds,
                        value=latency_ms,
                        resolution=MetricResolution.High,
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        "Could not parse event timestamp for latency metric",
                        extra={"event_ts": event_ts},
                    )
            else:
                # Duplicate event — skip Valkey write, emit metric
                metrics.add_metric(
                    name="duplicate_event_count",
                    unit=MetricUnit.Count,
                    value=1,
                )
                logger.info(
                    "Duplicate event skipped",
                    extra={"event_id": event_id, "game_id": game_id},
                )

        except Exception as e:
            logger.error(
                "Failed to process record",
                extra={
                    "message_id": message_id,
                    "error": str(e),
                },
                exc_info=True,
            )
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
