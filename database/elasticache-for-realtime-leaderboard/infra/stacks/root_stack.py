"""Root stack — wires all child stacks with cross-stack property passing."""

import aws_cdk as cdk
import aws_cdk.aws_cloudwatch as cloudwatch
from constructs import Construct

from config import VALKEY_CACHE_CLUSTER_IDS

from .api_stack import ApiStack
from .data_stack import DataStack
from .ingest_stack import IngestStack
from .loadgen_stack import LoadGenStack
from .network_stack import NetworkStack
from .web_stack import WebStack


class LeaderboardApp(cdk.Stack):
    """Root stack that instantiates and wires six nested stacks."""

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        network = NetworkStack(self, "Network")

        data = DataStack(
            self,
            "Data",
            vpc=network.vpc,
            sg=network.valkey_sg,
        )

        IngestStack(
            self,
            "Ingest",
            queue=data.queue,
            table=data.table,
            valkey_endpoint=data.valkey_endpoint,
            valkey_secret=data.valkey_secret,
            vpc=network.vpc,
            lambda_sg=network.lambda_sg,
        )

        loadgen = LoadGenStack(
            self,
            "LoadGen",
            queue=data.queue,
        )

        api = ApiStack(
            self,
            "Api",
            valkey_endpoint=data.valkey_endpoint,
            valkey_secret=data.valkey_secret,
            vpc=network.vpc,
            lambda_sg=network.lambda_sg,
            queue=data.queue,
            state_machine=loadgen.state_machine,
        )

        WebStack(
            self,
            "Web",
            api_url=api.api_url,
        )

        # --- CloudWatch Dashboard for observability widgets ---
        dashboard = cloudwatch.Dashboard(
            self,
            "LeaderboardDashboard",
            dashboard_name="leaderboard-demo",
        )

        # Widget 1: SQS queue depth
        sqs_depth_metric = cloudwatch.Metric(
            namespace="AWS/SQS",
            metric_name="ApproximateNumberOfMessagesVisible",
            dimensions_map={"QueueName": "leaderboard-score-events"},
            period=cdk.Duration.minutes(1),
            statistic="Average",
        )

        # Widget 2: Lambda Invocations (processor)
        lambda_invocations_metric = cloudwatch.Metric(
            namespace="AWS/Lambda",
            metric_name="Invocations",
            dimensions_map={"FunctionName": "leaderboard-score-processor"},
            period=cdk.Duration.minutes(1),
            statistic="Sum",
        )

        # Widget 3: Lambda Errors (processor)
        lambda_errors_metric = cloudwatch.Metric(
            namespace="AWS/Lambda",
            metric_name="Errors",
            dimensions_map={"FunctionName": "leaderboard-score-processor"},
            period=cdk.Duration.minutes(1),
            statistic="Sum",
        )

        # Widget 4: ElastiCache EngineCPUUtilization — per-node (CacheClusterId dimension).
        # ElastiCache publishes EngineCPUUtilization per CacheClusterId, not per ReplicationGroupId.
        valkey_cpu_metrics = [
            cloudwatch.Metric(
                namespace="AWS/ElastiCache",
                metric_name="EngineCPUUtilization",
                dimensions_map={"CacheClusterId": node_id},
                period=cdk.Duration.minutes(1),
                statistic="Average",
                label=node_id,
            )
            for node_id in VALKEY_CACHE_CLUSTER_IDS
        ]

        # Widget 5: Custom end-to-end latency (EMF) — HighResolution (10s period).
        # Powertools attaches {service: "score-processor"} dimension to every metric.
        e2e_latency_metric = cloudwatch.Metric(
            namespace="Leaderboard",
            metric_name="end_to_end_latency_ms",
            dimensions_map={"service": "score-processor"},
            period=cdk.Duration.seconds(10),
            statistic="Average",
        )

        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="SQS Queue Depth",
                left=[sqs_depth_metric],
                width=12,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Lambda Invocations",
                left=[lambda_invocations_metric],
                width=12,
                height=6,
            ),
        )

        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Lambda Errors",
                left=[lambda_errors_metric],
                width=8,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Valkey EngineCPUUtilization",
                left=valkey_cpu_metrics,
                width=8,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="End-to-End Latency (ms)",
                left=[e2e_latency_metric],
                width=8,
                height=6,
            ),
        )

        # Stack outputs
        cdk.CfnOutput(
            self,
            "ApiUrl",
            value=api.api_url,
            description="API Gateway HTTP API endpoint URL",
        )

        cdk.CfnOutput(
            self,
            "QueueUrl",
            value=data.queue.queue_url,
            description="SQS queue URL for score events",
        )

        cdk.CfnOutput(
            self,
            "TableName",
            value=data.table.table_name,
            description="DynamoDB table name for raw events",
        )

        cdk.CfnOutput(
            self,
            "ValkeyEndpoint",
            value=data.valkey_endpoint,
            description="ElastiCache Valkey primary endpoint",
        )
