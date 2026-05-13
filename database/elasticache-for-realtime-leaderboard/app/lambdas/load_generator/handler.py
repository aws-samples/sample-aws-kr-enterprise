"""Load Generator Lambda — sends synthetic score events to SQS at a target TPS.

Invoked by the Step Functions Map state. Each instance is responsible for
producing its share of the total TPS for the configured duration.

Input:
    {
        "tps": 200,           # Events per second this worker should produce
        "duration_sec": 60,   # How long to run
        "game_ids": ["arena-shooter"],
        "user_pool_size": 1000
    }
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from random import choice, randint

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
BATCH_SIZE = 10  # SQS SendMessageBatch max
MAX_RETRIES = 3

sqs = boto3.client("sqs")


def _generate_event(game_ids: list[str], user_pool_size: int) -> dict:
    """Generate a single synthetic score event."""
    return {
        "eventId": str(uuid.uuid4()),
        "userId": f"user_{randint(1, user_pool_size)}",
        "gameId": choice(game_ids),
        "score": randint(1, 100),
        "ts": datetime.now(timezone.utc).isoformat(),
        "sourceGame": "loadgen",
    }


def _send_batch(messages: list[dict]) -> int:
    """Send a batch of messages to SQS. Returns number successfully sent."""
    entries = [
        {
            "Id": str(i),
            "MessageBody": json.dumps(msg),
        }
        for i, msg in enumerate(messages)
    ]

    for attempt in range(MAX_RETRIES):
        try:
            response = sqs.send_message_batch(
                QueueUrl=SQS_QUEUE_URL, Entries=entries
            )
            failed = response.get("Failed", [])
            if failed:
                logger.warning(
                    "Partial batch failure: %d/%d messages failed",
                    len(failed),
                    len(entries),
                )
            return len(entries) - len(failed)
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(
                    "send_message_batch attempt %d failed: %s, retrying",
                    attempt + 1,
                    str(e),
                )
                time.sleep(0.1 * (attempt + 1))
            else:
                logger.error(
                    "send_message_batch failed after %d attempts: %s",
                    MAX_RETRIES,
                    str(e),
                )
                return 0
    return 0


def handler(event: dict, context) -> dict:
    """Main handler — loops for duration_sec sending events at the target TPS."""
    tps = event.get("tps", 0)
    duration_sec = event.get("duration_sec", 0)
    game_ids = event.get("game_ids", [])
    user_pool_size = event.get("user_pool_size", 1000)

    # Input validation
    if tps <= 0 or duration_sec <= 0:
        return {
            "statusCode": 400,
            "error": "tps and duration_sec must be positive integers",
            "total_sent": 0,
        }
    if not game_ids:
        return {
            "statusCode": 400,
            "error": "game_ids must be a non-empty list",
            "total_sent": 0,
        }

    # Calculate timing: how many batches per second needed
    # Each batch has BATCH_SIZE messages
    batches_per_second = tps / BATCH_SIZE

    start_time = time.time()
    end_time = start_time + duration_sec
    total_sent = 0
    total_failed = 0

    logger.info(
        "Starting load generation: tps=%d, duration=%ds, games=%s",
        tps,
        duration_sec,
        game_ids,
    )

    while time.time() < end_time and context.get_remaining_time_in_millis() > 10000:
        loop_start = time.time()

        # Send one second's worth of batches
        batches_this_second = int(batches_per_second)
        remainder = batches_per_second - batches_this_second

        for _ in range(batches_this_second):
            batch = [
                _generate_event(game_ids, user_pool_size)
                for _ in range(BATCH_SIZE)
            ]
            sent = _send_batch(batch)
            total_sent += sent
            total_failed += BATCH_SIZE - sent

        # Handle fractional batch for the remainder
        if remainder > 0:
            partial_size = int(BATCH_SIZE * remainder)
            if partial_size > 0:
                batch = [
                    _generate_event(game_ids, user_pool_size)
                    for _ in range(partial_size)
                ]
                sent = _send_batch(batch)
                total_sent += sent
                total_failed += partial_size - sent

        # Pace to 1 second
        elapsed = time.time() - loop_start
        sleep_time = 1.0 - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

    actual_duration = time.time() - start_time

    logger.info(
        "Load generation complete: sent=%d, failed=%d, duration=%.1fs",
        total_sent,
        total_failed,
        actual_duration,
    )

    return {
        "statusCode": 200,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "duration_sec": duration_sec,
        "target_tps": tps,
        "actual_tps": round(total_sent / max(actual_duration, 1), 1),
    }
