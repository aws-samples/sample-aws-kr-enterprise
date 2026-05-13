#!/usr/bin/env python3
"""Rebuild Valkey leaderboard state from DynamoDB source of truth.

NOTE: This script requires VPC access to the Valkey (ElastiCache) cluster.
It CANNOT be run from a local machine. Run it from:
  - A bastion host / EC2 instance within the VPC
  - AWS Systems Manager Session Manager
  - An admin Lambda function with VPC connectivity
  - AWS CloudShell with VPC access configured

Procedure (per ARCHITECTURE.md section 8.3):
1. Query DynamoDB per gameId
2. Aggregate scores in memory: sum(score) per userId
3. Bulk-load into Valkey with ZADD

Usage:
  python rebuild_from_ddb.py [--games arena-shooter,puzzle-01,racing-mini]
"""

import argparse
import os
import sys
import time

import boto3
import redis

# Add parent to path for shared module access
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.ddb_helpers import aggregate_scores, query_events_by_game

DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME", "leaderboard-raw-events")
VALKEY_ENDPOINT = os.environ.get("VALKEY_ENDPOINT")
VALKEY_SECRET_ARN = os.environ.get("VALKEY_SECRET_ARN")

# Default games to rebuild
DEFAULT_GAMES = ["arena-shooter", "puzzle-01", "racing-mini"]


def get_valkey_password() -> str:
    """Retrieve Valkey AUTH token from Secrets Manager."""
    import json

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=VALKEY_SECRET_ARN)
    secret = response["SecretString"]
    try:
        parsed = json.loads(secret)
        return parsed.get("password", secret)
    except (json.JSONDecodeError, TypeError):
        return secret


def get_valkey_client():
    """Create a Redis/Valkey client for the rebuild operation."""
    password = get_valkey_password()
    return redis.Redis(
        host=VALKEY_ENDPOINT,
        port=6379,
        password=password,
        ssl=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        decode_responses=True,
    )


def rebuild_game(valkey_client, game_id: str) -> dict:
    """Rebuild leaderboard for a single game from DynamoDB.

    Returns:
        Dict with rebuild stats.
    """
    start = time.time()

    # Query all events for this game
    events = query_events_by_game(DDB_TABLE_NAME, game_id)

    # Aggregate scores per user
    user_scores = aggregate_scores(events)

    # Bulk ZADD into Valkey (pipeline for performance)
    valkey_key = f"lb:{game_id}"

    if user_scores:
        # Use ZADD with mapping (redis-py supports dict)
        valkey_client.zadd(valkey_key, user_scores)

    elapsed = time.time() - start

    return {
        "game_id": game_id,
        "events_processed": len(events),
        "users_loaded": len(user_scores),
        "elapsed_seconds": round(elapsed, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Rebuild Valkey from DynamoDB")
    parser.add_argument(
        "--games",
        type=str,
        default=",".join(DEFAULT_GAMES),
        help="Comma-separated list of game IDs to rebuild",
    )
    args = parser.parse_args()

    if not VALKEY_ENDPOINT:
        print("ERROR: VALKEY_ENDPOINT environment variable is required")
        sys.exit(1)
    if not VALKEY_SECRET_ARN:
        print("ERROR: VALKEY_SECRET_ARN environment variable is required")
        sys.exit(1)

    games = [g.strip() for g in args.games.split(",")]
    valkey_client = get_valkey_client()

    print(f"Rebuilding {len(games)} game leaderboards from DynamoDB...")
    print(f"  Table: {DDB_TABLE_NAME}")
    print(f"  Valkey: {VALKEY_ENDPOINT}")
    print()

    total_start = time.time()
    for game_id in games:
        stats = rebuild_game(valkey_client, game_id)
        print(
            f"  {stats['game_id']}: "
            f"{stats['events_processed']} events → "
            f"{stats['users_loaded']} users "
            f"({stats['elapsed_seconds']}s)"
        )

    total_elapsed = time.time() - total_start
    print(f"\nRebuild complete in {total_elapsed:.2f}s")


if __name__ == "__main__":
    main()
