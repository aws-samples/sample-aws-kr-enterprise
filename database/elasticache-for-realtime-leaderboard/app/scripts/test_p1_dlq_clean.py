#!/usr/bin/env python3
"""P1-005: DLQ clean test — verify DLQ depth = 0 after processing.

Pass condition:
  - DLQ ApproximateNumberOfMessages = 0
"""

import os
import sys
import time

import boto3

SQS_DLQ_URL = os.environ.get("SQS_DLQ_URL")
SQS_DLQ_NAME = os.environ.get("SQS_DLQ_NAME", "leaderboard-score-events-dlq")
WAIT_SECONDS = 30  # Wait 30 seconds for any in-flight messages to settle


def get_dlq_url() -> str:
    """Get DLQ URL from environment or by name lookup."""
    if SQS_DLQ_URL:
        return SQS_DLQ_URL

    sqs = boto3.client("sqs")
    response = sqs.get_queue_url(QueueName=SQS_DLQ_NAME)
    return response["QueueUrl"]


def main():
    print(f"Waiting {WAIT_SECONDS}s for any in-flight messages to settle...")
    time.sleep(WAIT_SECONDS)

    sqs = boto3.client("sqs")
    dlq_url = get_dlq_url()

    print(f"Checking DLQ depth: {dlq_url}")

    response = sqs.get_queue_attributes(
        QueueUrl=dlq_url,
        AttributeNames=["All"],
    )

    attributes = response.get("Attributes", {})
    visible = int(attributes.get("ApproximateNumberOfMessagesVisible", 0))
    not_visible = int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0))
    total = visible + not_visible

    print(f"  Visible: {visible}")
    print(f"  Not visible: {not_visible}")
    print(f"  Total: {total}")

    if total > 0:
        print(f"FAIL[P1-005]: DLQ depth={total}, expected 0")
        sys.exit(1)

    print("PASS[P1-005]: DLQ is clean (depth=0)")


if __name__ == "__main__":
    main()
