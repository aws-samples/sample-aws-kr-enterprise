#!/usr/bin/env python3
"""P1-001: Smoke test — inject 1,000 events, verify DDB count + Valkey ZSCORE matches.

Pass condition:
  - DDB item count = 1,000 exactly
  - For every (gameId, userId): ZSCORE = SUM(scoreDelta) in DDB, exact match
"""

import json
import os
import random
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import boto3
import requests

QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME", "leaderboard-raw-events")
API_URL = os.environ.get("API_URL")

GAMES = ["arena-shooter", "puzzle-01", "racing-mini"]
USERS_PER_GAME = 100
TOTAL_EVENTS = 1000
WAIT_SECONDS = 30


def main():
    if not all([QUEUE_URL, API_URL]):
        print("ERROR: Required env vars: SQS_QUEUE_URL, API_URL")
        sys.exit(1)

    sqs = boto3.client("sqs")
    ddb = boto3.resource("dynamodb")
    table = ddb.Table(DDB_TABLE_NAME)

    # Track expected scores per (game, user)
    expected_scores = defaultdict(lambda: defaultdict(float))
    all_event_ids = set()

    # Inject 1,000 events
    print(f"Injecting {TOTAL_EVENTS} events across {len(GAMES)} games...")
    events_per_game = TOTAL_EVENTS // len(GAMES)
    remainder = TOTAL_EVENTS % len(GAMES)

    for game_idx, game_id in enumerate(GAMES):
        count = events_per_game + (1 if game_idx < remainder else 0)
        batch = []

        for i in range(count):
            user_id = f"user_{(i % USERS_PER_GAME) + 1:03d}"
            score_delta = random.randint(10, 500)
            event_id = str(uuid.uuid4())

            expected_scores[game_id][user_id] += score_delta
            all_event_ids.add(event_id)

            event = {
                "eventId": event_id,
                "userId": user_id,
                "gameId": game_id,
                "score": score_delta,
                "ts": datetime.now(timezone.utc).isoformat(),
                "sourceGame": "test-p1-001",
            }

            batch.append({"Id": str(i % 10), "MessageBody": json.dumps(event)})

            if len(batch) == 10:
                sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=batch)
                batch = []

        if batch:
            sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=batch)

    # Wait for processing
    print(f"Waiting {WAIT_SECONDS}s for processing...")
    time.sleep(WAIT_SECONDS)

    # Verify DDB count
    print("Verifying DynamoDB item count...")
    ddb_count = 0
    scan_params = {"Select": "COUNT"}
    while True:
        response = table.scan(**scan_params)
        ddb_count += response["Count"]
        if "LastEvaluatedKey" not in response:
            break
        scan_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    if ddb_count != TOTAL_EVENTS:
        # Find missing event IDs
        print(f"FAIL[P1-001]: expected {TOTAL_EVENTS} DDB items, got {ddb_count}")
        sys.exit(1)

    print(f"  DDB count: {ddb_count} (expected {TOTAL_EVENTS}) ✓")

    # Verify Valkey scores via API (scores are in Valkey, served by reader Lambda)
    print("Verifying Valkey scores via API...")
    mismatches = []
    for game_id, users in expected_scores.items():
        for user_id, expected_score in users.items():
            resp = requests.get(
                f"{API_URL}/rank/{user_id}",
                params={"gameId": game_id},
                timeout=5,
            )
            if resp.status_code == 404:
                mismatches.append(
                    f"{game_id}/{user_id}: expected {expected_score}, got None (404)"
                )
                continue
            data = resp.json()
            actual_score = data.get("score")
            if actual_score is None:
                mismatches.append(
                    f"{game_id}/{user_id}: expected {expected_score}, got None"
                )
            elif abs(actual_score - expected_score) > 0.01:
                mismatches.append(
                    f"{game_id}/{user_id}: expected {expected_score}, got {actual_score}"
                )

    if mismatches:
        print(f"FAIL[P1-001]: {len(mismatches)} score mismatches:")
        for m in mismatches[:10]:
            print(f"  {m}")
        sys.exit(1)

    print(f"  Valkey scores: all {sum(len(u) for u in expected_scores.values())} match ✓")
    print("PASS[P1-001]: Smoke test passed")


if __name__ == "__main__":
    main()
