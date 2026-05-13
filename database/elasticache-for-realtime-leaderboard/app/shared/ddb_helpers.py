"""DynamoDB helper functions for the leaderboard system.

Provides idempotent PutItem, paginated Query, and score aggregation.
"""

from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

_ddb_resource = None


def _get_table(table_name: str):
    """Get a DynamoDB Table resource (cached per cold start)."""
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb")
    return _ddb_resource.Table(table_name)


def put_event_idempotent(table_name: str, event: dict[str, Any]) -> bool:
    """Write a score event to DynamoDB with idempotency guard.

    Uses ConditionExpression="attribute_not_exists(eventId)" to ensure
    each event is written exactly once.

    Args:
        table_name: DynamoDB table name.
        event: Dict with keys: gameId, ts#eventId, eventId, userId, score, ttl, etc.

    Returns:
        True if the item was written (new event).
        False if the item already existed (duplicate).

    Raises:
        ClientError: For errors other than ConditionalCheckFailedException.
    """
    table = _get_table(table_name)

    try:
        table.put_item(
            Item=event,
            ConditionExpression="attribute_not_exists(eventId)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def query_events_by_game(
    table_name: str,
    game_id: str,
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> list[dict[str, Any]]:
    """Query all events for a game, optionally filtered by timestamp range.

    Paginates automatically to retrieve all matching items.

    Args:
        table_name: DynamoDB table name.
        game_id: Partition key value.
        start_ts: Optional lower bound for sort key (inclusive).
        end_ts: Optional upper bound for sort key (inclusive).

    Returns:
        List of event items.
    """
    table = _get_table(table_name)

    key_condition = boto3.dynamodb.conditions.Key("gameId").eq(game_id)

    if start_ts and end_ts:
        key_condition = key_condition & boto3.dynamodb.conditions.Key(
            "ts#eventId"
        ).between(start_ts, end_ts)
    elif start_ts:
        key_condition = key_condition & boto3.dynamodb.conditions.Key(
            "ts#eventId"
        ).gte(start_ts)
    elif end_ts:
        key_condition = key_condition & boto3.dynamodb.conditions.Key(
            "ts#eventId"
        ).lte(end_ts)

    items = []
    response = table.query(KeyConditionExpression=key_condition)
    items.extend(response["Items"])

    while "LastEvaluatedKey" in response:
        response = table.query(
            KeyConditionExpression=key_condition,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response["Items"])

    return items


def aggregate_scores(events: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate score deltas per userId from a list of events.

    Args:
        events: List of DynamoDB items with 'userId' and 'score' fields.

    Returns:
        Dict mapping userId to total cumulative score.
    """
    scores: dict[str, float] = {}
    for event in events:
        user_id = event["userId"]
        score = float(event.get("score", 0))
        if isinstance(event.get("score"), Decimal):
            score = float(event["score"])
        scores[user_id] = scores.get(user_id, 0.0) + score
    return scores
