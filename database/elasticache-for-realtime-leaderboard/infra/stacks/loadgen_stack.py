"""LoadGenStack — Load generator Lambda + Step Functions state machine."""

import os
import subprocess

import aws_cdk as cdk
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_sqs as sqs
import aws_cdk.aws_stepfunctions as sfn
import aws_cdk.aws_stepfunctions_tasks as tasks
import jsii
from constructs import Construct


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


class LoadGenStack(cdk.NestedStack):
    """Load generator Lambda + Step Functions state machine for traffic patterns."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        queue: sqs.IQueue,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        self._queue = queue

        # Lambda code path
        code_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "app",
        )

        requirements_path = os.path.join(
            code_path, "lambdas", "load_generator", "requirements.txt"
        )

        # --- Load Generator Lambda ---
        self.generator_fn = lambda_.Function(
            self,
            "LoadGeneratorFunction",
            function_name="leaderboard-load-generator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambdas.load_generator.handler.handler",
            code=lambda_.Code.from_asset(
                code_path,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r lambdas/load_generator/requirements.txt -t /asset-output"
                        " && cp -r lambdas /asset-output/lambdas"
                        " && cp -r shared /asset-output/shared",
                    ],
                    local=_LocalBundler(requirements_path, code_path),
                ),
            ),
            memory_size=256,
            timeout=cdk.Duration.seconds(600),
            environment={
                "SQS_QUEUE_URL": queue.queue_url,
            },
        )

        # IAM — SQS SendMessage/SendMessageBatch only
        queue.grant_send_messages(self.generator_fn)

        # --- Step Functions State Machine ---
        # The state machine fans out load-generator invocations via a Map state.
        # Input schema: { "tps": N, "duration_sec": N, "game_ids": [...], "user_pool_size": N }

        # Invoke load-generator Lambda per worker
        invoke_generator = tasks.LambdaInvoke(
            self,
            "InvokeLoadGenerator",
            lambda_function=self.generator_fn,
            payload=sfn.TaskInput.from_json_path_at("$"),
            result_path="$.result",
        )

        # Map state: fan out workers using itemSelector (non-deprecated API)
        map_state = sfn.Map(
            self,
            "FanOutWorkers",
            items_path="$.workers",
            max_concurrency=25,
            item_selector={
                "tps.$": "$$.Map.Item.Value.tps",
                "duration_sec.$": "$$.Map.Item.Value.duration_sec",
                "game_ids.$": "$$.Map.Item.Value.game_ids",
                "user_pool_size.$": "$$.Map.Item.Value.user_pool_size",
            },
        )
        map_state.item_processor(invoke_generator)

        # Use a Lambda to prepare the worker array (simpler than intrinsic functions for division)
        prepare_fn = lambda_.Function(
            self,
            "PrepareWorkersFunction",
            function_name="leaderboard-prepare-workers",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                """
import math
import json

def handler(event, context):
    tps = event["tps"]
    duration_sec = event["duration_sec"]
    game_ids = event["game_ids"]
    user_pool_size = event.get("user_pool_size", 1000)

    worker_tps = 200
    num_workers = math.ceil(tps / worker_tps)

    workers = []
    remaining_tps = tps
    for i in range(num_workers):
        this_worker_tps = min(worker_tps, remaining_tps)
        workers.append({
            "tps": this_worker_tps,
            "duration_sec": duration_sec,
            "game_ids": game_ids,
            "user_pool_size": user_pool_size,
        })
        remaining_tps -= this_worker_tps

    return {"workers": workers}
"""
            ),
            memory_size=128,
            timeout=cdk.Duration.seconds(10),
        )

        prepare_step = tasks.LambdaInvoke(
            self,
            "PrepareWorkers",
            lambda_function=prepare_fn,
            payload=sfn.TaskInput.from_object(
                {
                    "tps": sfn.JsonPath.number_at("$.tps"),
                    "duration_sec": sfn.JsonPath.number_at("$.duration_sec"),
                    "game_ids": sfn.JsonPath.list_at("$.game_ids"),
                    "user_pool_size": sfn.JsonPath.number_at("$.user_pool_size"),
                }
            ),
            output_path="$.Payload",
        )

        # Chain: Prepare → Map(fan-out)
        definition = prepare_step.next(map_state)

        self.state_machine = sfn.StateMachine(
            self,
            "LoadGeneratorSM",
            state_machine_name="leaderboard-load-generator-sm",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=cdk.Duration.minutes(10),
        )
