#!/usr/bin/env python3
"""Seed script — injects 1,000 sample events across 3 games.

Games: arena-shooter, puzzle-01, racing-mini
Users: 100 per game (user_001 through user_100)
Events: ~333 per game (1,000 total)
"""

import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3

QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
GAMES = ["arena-shooter", "puzzle-01", "racing-mini"]
USERS_PER_GAME = 100
TOTAL_EVENTS = 1000


def main():
    if not QUEUE_URL:
        print("ERROR: SQS_QUEUE_URL environment variable is required")
        sys.exit(1)

    sqs = boto3.client("sqs")
    events_per_game = TOTAL_EVENTS // len(GAMES)
    remainder = TOTAL_EVENTS % len(GAMES)

    sent_count = 0

    for game_idx, game_id in enumerate(GAMES):
        count = events_per_game + (1 if game_idx < remainder else 0)

        # Send in batches of 10
        batch = []
        for i in range(count):
            user_id = f"user_{(i % USERS_PER_GAME) + 1:03d}"
            score_delta = random.randint(10, 500)

            event = {
                "eventId": str(uuid.uuid4()),
                "userId": user_id,
                "gameId": game_id,
                "score": score_delta,
                "ts": datetime.now(timezone.utc).isoformat(),
                "sourceGame": "seed",
            }

            batch.append(
                {
                    "Id": str(i),
                    "MessageBody": json.dumps(event),
                }
            )

            if len(batch) == 10:
                response = sqs.send_message_batch(
                    QueueUrl=QUEUE_URL, Entries=batch
                )
                sent_count += len(response.get("Successful", []))
                failed = response.get("Failed", [])
                if failed:
                    print(f"  WARNING: {len(failed)} messages failed in batch")
                batch = []

        # Send remaining batch
        if batch:
            response = sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=batch)
            sent_count += len(response.get("Successful", []))

    print(f"Seed complete: {sent_count}/{TOTAL_EVENTS} events sent")
    print(f"  Games: {', '.join(GAMES)}")
    print(f"  Users per game: {USERS_PER_GAME}")
    print(f"  Queue: {QUEUE_URL}")


if __name__ == "__main__":
    main()
