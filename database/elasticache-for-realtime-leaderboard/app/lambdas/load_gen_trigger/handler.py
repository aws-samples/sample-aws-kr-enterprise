"""Load Gen Trigger Lambda — validates input and starts the Step Functions state machine.

Receives POST /demo/start-load from API Gateway and orchestrates a load test
by starting the load-generator-sm Step Functions state machine.

Input body:
    {
        "pattern": "sustain_5k_5min" | "sustain_5k_1min",
        "game_ids": ["arena-shooter", "puzzle-01"],  # optional, defaults to all 3
        "user_pool_size": 1000  # optional
    }
"""

import json
import os
import re
from datetime import datetime, timezone

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger(service="load-gen-trigger")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

sfn = boto3.client("stepfunctions")

# Predefined load patterns
PATTERNS = {
    "sustain_5k_5min": {"tps": 5000, "duration_sec": 300},
    "sustain_5k_1min": {"tps": 5000, "duration_sec": 60},
}

VALID_GAME_IDS = {"arena-shooter", "puzzle-01", "racing-mini"}
DEFAULT_GAME_IDS = ["arena-shooter", "puzzle-01", "racing-mini"]
DEFAULT_USER_POOL_SIZE = 1000

GAME_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _build_response(status_code: int, body: dict) -> dict:
    """Build API Gateway HTTP API response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


@logger.inject_lambda_context
def handler(event: dict, context: LambdaContext) -> dict:
    """Validate input and start Step Functions execution."""
    # Parse body
    body_str = event.get("body", "")
    if not body_str:
        return _build_response(400, {"error": "Request body is required"})

    try:
        body = json.loads(body_str) if isinstance(body_str, str) else body_str
    except (json.JSONDecodeError, TypeError):
        return _build_response(400, {"error": "Invalid JSON in request body"})

    # Validate pattern
    pattern = body.get("pattern")
    if not pattern:
        return _build_response(400, {"error": "pattern field is required"})
    if pattern not in PATTERNS:
        return _build_response(
            400,
            {
                "error": f"Invalid pattern: {pattern}. Valid patterns: {list(PATTERNS.keys())}",
            },
        )

    # Validate game_ids
    game_ids = body.get("game_ids", DEFAULT_GAME_IDS)
    if not isinstance(game_ids, list) or len(game_ids) == 0:
        return _build_response(400, {"error": "game_ids must be a non-empty list"})
    for gid in game_ids:
        if not isinstance(gid, str) or not GAME_ID_PATTERN.match(gid):
            return _build_response(
                400,
                {"error": f"Invalid game_id: {gid}. Must be 1-64 alphanumeric/dash/underscore."},
            )

    # Validate user_pool_size
    user_pool_size = body.get("user_pool_size", DEFAULT_USER_POOL_SIZE)
    if not isinstance(user_pool_size, int) or user_pool_size < 1 or user_pool_size > 1_000_000:
        return _build_response(
            400,
            {"error": "user_pool_size must be an integer between 1 and 1,000,000"},
        )

    # Build Step Functions input
    pattern_config = PATTERNS[pattern]
    sfn_input = {
        "tps": pattern_config["tps"],
        "duration_sec": pattern_config["duration_sec"],
        "game_ids": game_ids,
        "user_pool_size": user_pool_size,
    }

    # Start execution
    execution_name = f"{pattern}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    try:
        try:
            response = sfn.start_execution(
                stateMachineArn=STATE_MACHINE_ARN,
                name=execution_name,
                input=json.dumps(sfn_input),
            )
        except sfn.exceptions.ExecutionAlreadyExists:
            # Append a short suffix to avoid collision
            execution_name = f"{execution_name}-{context.aws_request_id[:8]}"
            response = sfn.start_execution(
                stateMachineArn=STATE_MACHINE_ARN,
                name=execution_name,
                input=json.dumps(sfn_input),
            )
    except Exception as e:
        logger.exception("Failed to start Step Functions execution")
        return _build_response(
            500,
            {"error": f"Failed to start load generation: {type(e).__name__}"},
        )

    logger.info(
        "Started load generation",
        extra={
            "pattern": pattern,
            "execution_arn": response["executionArn"],
            "sfn_input": sfn_input,
        },
    )

    return _build_response(
        200,
        {
            "message": f"Load generation started: {pattern}",
            "executionArn": response["executionArn"],
            "pattern": pattern,
            "config": sfn_input,
        },
    )
