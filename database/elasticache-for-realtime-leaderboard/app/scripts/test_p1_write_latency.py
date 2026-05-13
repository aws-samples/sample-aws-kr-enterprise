#!/usr/bin/env python3
"""P1-003: Write latency test — inject 500 TPS for 3 min, check p95 < 2000ms.

Pass condition:
  - p50 < 800 ms
  - p95 < 2,000 ms
  - p99 < 3,000 ms
"""

import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import boto3

QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
TARGET_TPS = 500
DURATION_SECONDS = 180  # 3 minutes
GAME_ID = "latency-test-game"
USER_POOL = 1000


def main():
    if not QUEUE_URL:
        print("ERROR: SQS_QUEUE_URL environment variable is required")
        sys.exit(1)

    sqs = boto3.client("sqs")
    cw = boto3.client("cloudwatch")

    print(f"Injecting {TARGET_TPS} TPS for {DURATION_SECONDS}s...")
    print(f"  Queue: {QUEUE_URL}")
    print(f"  Game: {GAME_ID}")

    # Record start time for CloudWatch query
    test_start = datetime.now(timezone.utc)

    # Inject events at target TPS using batched sends
    total_sent = 0
    batch_size = 10
    batches_per_second = TARGET_TPS // batch_size  # 50 batches/s

    start_time = time.time()
    end_time = start_time + DURATION_SECONDS

    while time.time() < end_time:
        second_start = time.time()
        batch_count = 0

        for _ in range(batches_per_second):
            if time.time() >= end_time:
                break

            entries = []
            for j in range(batch_size):
                user_id = f"user_{(total_sent + j) % USER_POOL:04d}"
                event = {
                    "eventId": str(uuid.uuid4()),
                    "userId": user_id,
                    "gameId": GAME_ID,
                    "score": 10,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "sourceGame": "test-p1-003",
                }
                entries.append(
                    {"Id": str(j), "MessageBody": json.dumps(event)}
                )

            try:
                sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=entries)
                total_sent += len(entries)
                batch_count += 1
            except Exception as e:
                print(f"  WARNING: batch send failed: {e}")

        # Pace to 1 second
        elapsed = time.time() - second_start
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        # Progress every 30s
        total_elapsed = time.time() - start_time
        if int(total_elapsed) % 30 == 0 and int(total_elapsed) > 0:
            actual_tps = total_sent / total_elapsed
            print(f"  Progress: {int(total_elapsed)}s, {total_sent} sent, ~{actual_tps:.0f} TPS")

    test_end = datetime.now(timezone.utc)
    actual_tps = total_sent / DURATION_SECONDS
    print(f"\nInjection complete: {total_sent} events in {DURATION_SECONDS}s ({actual_tps:.0f} TPS)")

    # Wait for Lambda to finish processing and metrics to propagate
    print("Waiting 60s for metrics propagation...")
    time.sleep(60)

    # Query CloudWatch EMF metric for end_to_end_latency_ms
    print("Querying CloudWatch metrics...")

    response = cw.get_metric_statistics(
        Namespace="Leaderboard",
        MetricName="end_to_end_latency_ms",
        Dimensions=[{"Name": "service", "Value": "score-processor"}],
        StartTime=test_start,
        EndTime=test_end + timedelta(minutes=2),
        Period=DURATION_SECONDS,
        ExtendedStatistics=["p50", "p95", "p99"],
    )

    datapoints = response.get("Datapoints", [])
    if not datapoints:
        print("FAIL[P1-003]: No end_to_end_latency_ms datapoints found")
        sys.exit(1)

    # Use the most recent datapoint that covers our test window
    dp = sorted(datapoints, key=lambda x: x["Timestamp"])[-1]
    p50 = dp["ExtendedStatistics"]["p50"]
    p95 = dp["ExtendedStatistics"]["p95"]
    p99 = dp["ExtendedStatistics"]["p99"]

    print(f"\nLatency results:")
    print(f"  p50: {p50:.0f}ms (threshold: <800ms)")
    print(f"  p95: {p95:.0f}ms (threshold: <2000ms)")
    print(f"  p99: {p99:.0f}ms (threshold: <3000ms)")

    # Check thresholds
    failures = []
    if p50 >= 800:
        failures.append(f"p50={p50:.0f}ms > 800ms")
    if p95 >= 2000:
        failures.append(f"p95={p95:.0f}ms > 2000ms")
    if p99 >= 3000:
        failures.append(f"p99={p99:.0f}ms > 3000ms")

    if failures:
        window = f"{test_start.isoformat()}"
        print(f"\nFAIL[P1-003]: {'; '.join(failures)} over {DURATION_SECONDS}s window ({window})")
        sys.exit(1)

    print("\nPASS[P1-003]: Write latency test passed")


if __name__ == "__main__":
    main()
