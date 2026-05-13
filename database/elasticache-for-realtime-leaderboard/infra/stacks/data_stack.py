"""DataStack — SQS, DLQ, DynamoDB table, ElastiCache Valkey, Secrets Manager."""

import aws_cdk as cdk
import aws_cdk.aws_dynamodb as dynamodb
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_elasticache as elasticache
import aws_cdk.aws_secretsmanager as secretsmanager
import aws_cdk.aws_sqs as sqs
from constructs import Construct

from config import (
    DDB_TABLE_NAME,
    SQS_DLQ_NAME,
    SQS_MAX_RECEIVE_COUNT,
    SQS_QUEUE_NAME,
    SQS_VISIBILITY_TIMEOUT,
    VALKEY_ENGINE_VERSION,
    VALKEY_NODE_TYPE,
)


class DataStack(cdk.NestedStack):
    """Provisions the data stores: SQS queues, DynamoDB table, and ElastiCache Valkey."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        vpc: ec2.IVpc,
        sg: ec2.ISecurityGroup,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # --- SQS Dead Letter Queue ---
        self.dlq = sqs.Queue(
            self,
            "ScoreEventsDlq",
            queue_name=SQS_DLQ_NAME,
            retention_period=cdk.Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # --- SQS Main Queue ---
        self.queue = sqs.Queue(
            self,
            "ScoreEventsQueue",
            queue_name=SQS_QUEUE_NAME,
            visibility_timeout=cdk.Duration.seconds(SQS_VISIBILITY_TIMEOUT),
            retention_period=cdk.Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=SQS_MAX_RECEIVE_COUNT,
                queue=self.dlq,
            ),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # --- DynamoDB Table ---
        self.table = dynamodb.Table(
            self,
            "RawEventsTable",
            table_name=DDB_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="gameId", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="ts#eventId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # --- Secrets Manager — Valkey AUTH token ---
        self.valkey_secret = secretsmanager.Secret(
            self,
            "ValkeyAuthSecret",
            secret_name="leaderboard/valkey-auth-token",
            description="AUTH token for ElastiCache Valkey cluster",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True,
                password_length=32,
            ),
        )

        # --- ElastiCache Subnet Group ---
        private_subnet_ids = [
            subnet.subnet_id
            for subnet in vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ).subnets
        ]

        subnet_group = elasticache.CfnSubnetGroup(
            self,
            "ValkeySubnetGroup",
            description="Subnet group for leaderboard Valkey cluster",
            subnet_ids=private_subnet_ids,
            cache_subnet_group_name="leaderboard-valkey-subnets",
        )

        # --- ElastiCache Replication Group (Valkey 8.0) ---
        # Use dynamic reference to avoid exposing secret in template
        self.valkey_cluster = elasticache.CfnReplicationGroup(
            self,
            "ValkeyReplicationGroup",
            replication_group_id="leaderboard-valkey",
            replication_group_description="Leaderboard Valkey cluster",
            engine="valkey",
            engine_version=VALKEY_ENGINE_VERSION,
            cache_node_type=VALKEY_NODE_TYPE,
            num_cache_clusters=2,
            multi_az_enabled=True,
            automatic_failover_enabled=True,
            cache_subnet_group_name=subnet_group.cache_subnet_group_name,
            security_group_ids=[sg.security_group_id],
            transit_encryption_enabled=True,
            auth_token=self.valkey_secret.secret_value.unsafe_unwrap(),
            at_rest_encryption_enabled=True,
            port=6379,
        )
        # Note: unsafe_unwrap() is required for L1 constructs. CDK resolves it
        # as {{resolve:secretsmanager:...}} — secret value never appears in template.

        self.valkey_cluster.add_dependency(subnet_group)

        # Expose the primary endpoint for Lambda environment variables
        self.valkey_endpoint = self.valkey_cluster.attr_primary_end_point_address
