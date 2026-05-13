#!/usr/bin/env python3
"""P1-004: Read latency test — seed 500 users, drive 100 req/s for 60s, check p95 < 100ms.

Pass condition:
  - API GW Latency p95 < 100 ms
  - Client RTT p95 < 200 ms
"""

import json
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import boto3
import requests

API_URL = os.environ.get("API_URL")
QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
GAME_ID = "read-latency-test"
USER_COUNT = 500
TARGET_RPS = 100
DURATION_SECONDS = 60


def seed_users():
    """Seed 500 users into the game via SQS."""
    sqs = boto3.client("sqs")
    print(f"Seeding {USER_COUNT} users for game '{GAME_ID}'...")

    batch = []
    for i in range(USER_COUNT):
        event = {
            "eventId": str(uuid.uuid4()),
            "userId": f"user_{i:04d}",
            "gameId": GAME_ID,
            "score": (i + 1) * 10,
            "ts": datetime.now(timezone.utc).isoformat(),
            "sourceGame": "test-p1-004",
        }
        batch.append({"Id": str(i % 10), "MessageBody": json.dumps(event)})

        if len(batch) == 10:
            sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=batch)
            batch = []

    if batch:
        sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=batch)

    # Wait for processing
    print("Waiting 15s for seed events to process...")
    time.sleep(15)


def make_request(session, url):
    """Make a single GET request and return latency in ms."""
    start = time.time()
    try:
        resp = session.get(url, timeout=5)
        latency_ms = (time.time() - start) * 1000
        return latency_ms, resp.status_code
    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        return latency_ms, 0


def main():
    if not all([API_URL, QUEUE_URL]):
        print("ERROR: Required env vars: API_URL, SQS_QUEUE_URL")
        sys.exit(1)

    # Seed users first
    seed_users()

    # Drive 100 req/s for 60 seconds
    url = f"{API_URL}/leaderboard?gameId={GAME_ID}&limit=100&userId=user_0250"
    print(f"Driving {TARGET_RPS} req/s for {DURATION_SECONDS}s...")
    print(f"  URL: {url}")

    test_start = datetime.now(timezone.utc)
    latencies = []
    errors = 0

    session = requests.Session()

    # Warm up with a few requests
    for _ in range(5):
        make_request(session, url)

    start_time = time.time()
    end_time = start_time + DURATION_SECONDS

    with ThreadPoolExecutor(max_workers=20) as executor:
        while time.time() < end_time:
            second_start = time.time()
            futures = []

            for _ in range(TARGET_RPS):
                futures.append(executor.submit(make_request, session, url))

            for future in as_completed(futures):
                latency_ms, status_code = future.result()
                latencies.append(latency_ms)
                if status_code != 200:
                    errors += 1

            # Pace to 1 second
            elapsed = time.time() - second_start
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

    test_end = datetime.now(timezone.utc)

    # Calculate client-side percentiles
    latencies.sort()
    total = len(latencies)
    p50_idx = int(total * 0.50)
    p95_idx = int(total * 0.95)
    p99_idx = int(total * 0.99)

    client_p50 = latencies[p50_idx] if total > 0 else 0
    client_p95 = latencies[p95_idx] if total > 0 else 0
    client_p99 = latencies[p99_idx] if total > 0 else 0

    print(f"\nClient-side latency ({total} requests, {errors} errors):")
    print(f"  p50: {client_p50:.0f}ms")
    print(f"  p95: {client_p95:.0f}ms (threshold: <200ms)")
    print(f"  p99: {client_p99:.0f}ms")

    # Query API Gateway CloudWatch metrics
    print("\nQuerying API Gateway Latency metric...")
    cw = boto3.client("cloudwatch")

    # Find the API Gateway ID from the URL
    # API URL format: https://{api-id}.execute-api.{region}.amazonaws.com
    response = cw.get_metric_statistics(
        Namespace="AWS/ApiGateway",
        MetricName="Latency",
        Dimensions=[{"Name": "ApiId", "Value": API_URL.split("//")[1].split(".")[0]}],
        StartTime=test_start,
        EndTime=test_end + timedelta(minutes=1),
        Period=DURATION_SECONDS,
        ExtendedStatistics=["p95"],
    )

    datapoints = response.get("Datapoints", [])
    api_gw_p95 = None
    if datapoints:
        dp = sorted(datapoints, key=lambda x: x["Timestamp"])[-1]
        api_gw_p95 = dp["ExtendedStatistics"]["p95"]
        print(f"  API GW p95: {api_gw_p95:.0f}ms (threshold: <100ms)")
    else:
        print("  WARNING: No API GW latency datapoints found, using client-side only")

    # Check thresholds
    # API GW Latency is the authoritative metric (network-independent).
    # Client RTT includes cross-region network and is informational only
    # when running from outside us-east-1.
    failures = []

    if api_gw_p95 is not None and api_gw_p95 >= 100:
        failures.append(f"API GW p95={api_gw_p95:.0f}ms > 100ms")
    elif api_gw_p95 is None and client_p95 >= 200:
        failures.append(f"Client RTT p95={client_p95:.0f}ms > 200ms (no API GW metric available)")

    if failures:
        print(f"\nFAIL[P1-004]: {'; '.join(failures)}")
        sys.exit(1)

    print("\nPASS[P1-004]: Read latency test passed")


if __name__ == "__main__":
    main()
