"""Shared configuration constants for the leaderboard CDK stacks."""

REGION = "us-east-1"
STACK_PREFIX = "leaderboard"

# ElastiCache for Valkey
VALKEY_NODE_TYPE = "cache.r7g.large"
VALKEY_ENGINE_VERSION = "8.0"
# CacheClusterId nodes published by the replication group — ElastiCache EMF
# metrics use CacheClusterId as primary dimension (not ReplicationGroupId).
# Must match num_cache_clusters in DataStack; AWS names them with a 3-digit suffix.
VALKEY_CACHE_CLUSTER_IDS = ["leaderboard-valkey-001", "leaderboard-valkey-002"]

# DynamoDB
DDB_TABLE_NAME = "leaderboard-raw-events"

# SQS
SQS_QUEUE_NAME = "leaderboard-score-events"
SQS_DLQ_NAME = "leaderboard-score-events-dlq"
# Visibility timeout = processor timeout × 6 (AWS recommended multiplier).
SQS_VISIBILITY_TIMEOUT = 360
SQS_MAX_RECEIVE_COUNT = 5

# Lambda — Processor
# Memory 1024 MB → full vCPU + 2× network bandwidth (I/O-bound workload).
PROCESSOR_MEMORY = 1024
PROCESSOR_TIMEOUT = 60
PROCESSOR_RESERVED_CONCURRENCY = 100

# Lambda — Reader
READER_MEMORY = 256
READER_TIMEOUT = 10

# Lambda — Load Generator
LOAD_GEN_MEMORY = 256
LOAD_GEN_TIMEOUT = 300

# Lambda — Load Gen Trigger
LOAD_GEN_TRIGGER_MEMORY = 128
LOAD_GEN_TRIGGER_TIMEOUT = 30

# Event Source Mapping
# Batch 200 @ ~12ms/record → ~2.5s invocation; 20× fewer invocations vs batch 10.
ESM_BATCH_SIZE = 200
ESM_BATCH_WINDOW = 2
