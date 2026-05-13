"""IngestStack — Processor Lambda with SQS Event Source Mapping."""

import os
import subprocess

import aws_cdk as cdk
import aws_cdk.aws_dynamodb as dynamodb
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_lambda_event_sources as event_sources
import aws_cdk.aws_secretsmanager as secretsmanager
import aws_cdk.aws_sqs as sqs
import jsii
from constructs import Construct

from config import (
    ESM_BATCH_SIZE,
    ESM_BATCH_WINDOW,
    PROCESSOR_MEMORY,
    PROCESSOR_RESERVED_CONCURRENCY,
    PROCESSOR_TIMEOUT,
)


@jsii.implements(cdk.ILocalBundling)
class _LocalBundler:
    """Local bundling implementation for Lambda code packaging."""

    def __init__(self, requirements_path: str, source_path: str):
        self._requirements_path = requirements_path
        self._source_path = source_path

    def try_bundle(self, output_dir: str, *, image, **kwargs) -> bool:
        """Bundle Lambda code locally using pip."""
        import shutil
        import sys

        try:
            pip_executable = os.path.join(
                os.path.dirname(sys.executable), "pip"
            )
            if not os.path.exists(pip_executable):
                pip_executable = "pip"

            # Install dependencies
            subprocess.check_call(
                [
                    pip_executable,
                    "install",
                    "-r",
                    self._requirements_path,
                    "-t",
                    output_dir,
                    "--quiet",
                ],
            )
            # Copy application code
            shutil.copytree(
                os.path.join(self._source_path, "lambdas"),
                os.path.join(output_dir, "lambdas"),
            )
            shutil.copytree(
                os.path.join(self._source_path, "shared"),
                os.path.join(output_dir, "shared"),
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return False


class IngestStack(cdk.NestedStack):
    """Processor Lambda that consumes SQS, writes to DynamoDB, and updates Valkey."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        queue: sqs.IQueue,
        table: dynamodb.ITable,
        valkey_endpoint: str,
        valkey_secret: secretsmanager.ISecret,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # Lambda code path: include both the handler directory and shared module
        code_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "app",
        )

        requirements_path = os.path.join(
            code_path, "lambdas", "processor", "requirements.txt"
        )

        self.processor_fn = lambda_.Function(
            self,
            "ProcessorFunction",
            function_name="leaderboard-score-processor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambdas.processor.handler.handler",
            code=lambda_.Code.from_asset(
                code_path,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r lambdas/processor/requirements.txt -t /asset-output"
                        " && cp -r lambdas /asset-output/lambdas"
                        " && cp -r shared /asset-output/shared",
                    ],
                    local=_LocalBundler(requirements_path, code_path),
                ),
            ),
            memory_size=PROCESSOR_MEMORY,
            timeout=cdk.Duration.seconds(PROCESSOR_TIMEOUT),
            reserved_concurrent_executions=PROCESSOR_RESERVED_CONCURRENCY,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            security_groups=[lambda_sg],
            environment={
                "VALKEY_ENDPOINT": valkey_endpoint,
                "VALKEY_SECRET_ARN": valkey_secret.secret_arn,
                "DDB_TABLE_NAME": table.table_name,
            },
        )

        # SQS Event Source Mapping with partial batch failure reporting
        self.processor_fn.add_event_source(
            event_sources.SqsEventSource(
                queue,
                batch_size=ESM_BATCH_SIZE,
                max_batching_window=cdk.Duration.seconds(ESM_BATCH_WINDOW),
                report_batch_item_failures=True,
            )
        )

        # IAM — Least privilege
        # SQS permissions (receive, delete, change visibility)
        queue.grant_consume_messages(self.processor_fn)

        # DynamoDB — PutItem only
        table.grant(self.processor_fn, "dynamodb:PutItem")

        # Secrets Manager — read Valkey auth token
        valkey_secret.grant_read(self.processor_fn)
