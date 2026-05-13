"""ApiStack — Reader Lambda + Load Gen Trigger Lambda + API Gateway HTTP API."""

import os
import subprocess

import aws_cdk as cdk
import aws_cdk.aws_apigatewayv2 as apigwv2
import aws_cdk.aws_apigatewayv2_integrations as integrations
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_secretsmanager as secretsmanager
import aws_cdk.aws_sqs as sqs
import aws_cdk.aws_stepfunctions as sfn
import jsii
from constructs import Construct

from config import (
    READER_MEMORY,
    READER_TIMEOUT,
    SQS_QUEUE_NAME,
    VALKEY_CACHE_CLUSTER_IDS,
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


class ApiStack(cdk.NestedStack):
    """Reader Lambda + Load Gen Trigger behind an API Gateway HTTP API."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        valkey_endpoint: str,
        valkey_secret: secretsmanager.ISecret,
        vpc: ec2.IVpc,
        lambda_sg: ec2.ISecurityGroup,
        queue: sqs.IQueue,
        state_machine: sfn.IStateMachine | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # Lambda code path
        code_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "app",
        )

        requirements_path = os.path.join(
            code_path, "lambdas", "reader", "requirements.txt"
        )

        self.reader_fn = lambda_.Function(
            self,
            "ReaderFunction",
            function_name="leaderboard-reader",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambdas.reader.handler.handler",
            code=lambda_.Code.from_asset(
                code_path,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r lambdas/reader/requirements.txt -t /asset-output"
                        " && cp -r lambdas /asset-output/lambdas"
                        " && cp -r shared /asset-output/shared",
                    ],
                    local=_LocalBundler(requirements_path, code_path),
                ),
            ),
            memory_size=READER_MEMORY,
            timeout=cdk.Duration.seconds(READER_TIMEOUT),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            security_groups=[lambda_sg],
            environment={
                "VALKEY_ENDPOINT": valkey_endpoint,
                "VALKEY_SECRET_ARN": valkey_secret.secret_arn,
                "METRICS_QUEUE_NAME": SQS_QUEUE_NAME,
                "METRICS_PROCESSOR_FN": "leaderboard-score-processor",
                "METRICS_PROCESSOR_SERVICE": "score-processor",
                "METRICS_VALKEY_NODES": ",".join(VALKEY_CACHE_CLUSTER_IDS),
                "SQS_QUEUE_URL": queue.queue_url,
            },
        )

        # IAM — Secrets Manager (Valkey auth token)
        valkey_secret.grant_read(self.reader_fn)

        # IAM — Live SQS depth probe (bypasses 1-2 min CloudWatch lag).
        self.reader_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sqs:GetQueueAttributes"],
                resources=[queue.queue_arn],
                effect=iam.Effect.ALLOW,
            )
        )

        # IAM — CloudWatch GetMetricData for /admin/metrics endpoint.
        # Note: cloudwatch:GetMetricData does not support resource-level permissions;
        # Resource "*" is the only valid value per AWS documentation.
        self.reader_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:GetMetricData"],
                resources=["*"],
                effect=iam.Effect.ALLOW,
            )
        )

        # API Gateway HTTP API
        self.http_api = apigwv2.HttpApi(
            self,
            "LeaderboardApi",
            api_name="leaderboard-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.DELETE,
                ],
                allow_headers=["Content-Type"],
            ),
        )

        reader_integration = integrations.HttpLambdaIntegration(
            "ReaderIntegration",
            self.reader_fn,
        )

        # GET /leaderboard
        self.http_api.add_routes(
            path="/leaderboard",
            methods=[apigwv2.HttpMethod.GET],
            integration=reader_integration,
        )

        # GET /rank/{userId}
        self.http_api.add_routes(
            path="/rank/{userId}",
            methods=[apigwv2.HttpMethod.GET],
            integration=reader_integration,
        )

        # Admin routes (demo — no auth; production would add IAM authorizer)
        self.http_api.add_routes(
            path="/admin/flush",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=reader_integration,
        )

        self.http_api.add_routes(
            path="/admin/zcard",
            methods=[apigwv2.HttpMethod.GET],
            integration=reader_integration,
        )

        self.http_api.add_routes(
            path="/admin/info",
            methods=[apigwv2.HttpMethod.GET],
            integration=reader_integration,
        )

        self.http_api.add_routes(
            path="/admin/metrics",
            methods=[apigwv2.HttpMethod.GET],
            integration=reader_integration,
        )

        # --- Load Gen Trigger Lambda (POST /demo/start-load) ---
        if state_machine is not None:
            trigger_requirements_path = os.path.join(
                code_path, "lambdas", "load_gen_trigger", "requirements.txt"
            )

            self.trigger_fn = lambda_.Function(
                self,
                "LoadGenTriggerFunction",
                function_name="leaderboard-load-gen-trigger",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="lambdas.load_gen_trigger.handler.handler",
                code=lambda_.Code.from_asset(
                    code_path,
                    bundling=cdk.BundlingOptions(
                        image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                        command=[
                            "bash",
                            "-c",
                            "pip install -r lambdas/load_gen_trigger/requirements.txt -t /asset-output"
                            " && cp -r lambdas /asset-output/lambdas"
                            " && cp -r shared /asset-output/shared",
                        ],
                        local=_LocalBundler(trigger_requirements_path, code_path),
                    ),
                ),
                memory_size=128,
                timeout=cdk.Duration.seconds(30),
                environment={
                    "STATE_MACHINE_ARN": state_machine.state_machine_arn,
                },
            )

            # IAM — StartExecution on the specific state machine only
            state_machine.grant_start_execution(self.trigger_fn)

            trigger_integration = integrations.HttpLambdaIntegration(
                "TriggerIntegration",
                self.trigger_fn,
            )

            self.http_api.add_routes(
                path="/demo/start-load",
                methods=[apigwv2.HttpMethod.POST],
                integration=trigger_integration,
            )

        # Expose the API URL for downstream stacks
        self.api_url = self.http_api.api_endpoint
