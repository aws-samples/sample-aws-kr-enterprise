"""boto3 bedrock-agentcore wrapper. Spec Section 9 — 2개 client 구분.
control_client: bedrock-agentcore-control (Create/Delete Runtime)
runtime_client: bedrock-agentcore (Invoke Runtime)"""

import json
import os
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
AGENTCORE_ROLE_ARN = os.environ.get("AGENTCORE_ROLE_ARN", "")


class AgentCoreClient:
    def __init__(self):
        self.control_client = boto3.client(
            "bedrock-agentcore-control",
            region_name=REGION,
            config=BotoConfig(connect_timeout=10, read_timeout=60),
        )
        self.runtime_client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=BotoConfig(connect_timeout=10, read_timeout=300),
        )

    def create_runtime(
        self, agent_id: str, image_uri: str, env_vars: Optional[dict] = None
    ) -> str:
        """Control Plane: AgentCore Runtime 생성."""
        environment = {
            "AGENT_ID": agent_id,
            "DYNAMODB_TABLE": os.environ.get("DYNAMODB_TABLE"),
            "AWS_REGION": REGION,
            "AGENT_OBSERVABILITY_ENABLED": "true",
        }
        # Propagate report-generation config so the Report agent's runtime can
        # write HTML reports to S3/CloudFront. Without these the report_tools
        # import/use fails and reports are never produced.
        for var in ("REPORT_BUCKET", "REPORT_CF_DOMAIN", "INCIDENTS_TABLE"):
            val = os.environ.get(var)
            if val:
                environment[var] = val
        if env_vars:
            environment.update(env_vars)

        import re
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "", agent_id.replace("-", "_"))
        runtime_name = (sanitized if sanitized[0:1].isalpha() else f"a{sanitized}")[:48]

        response = self.control_client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            roleArn=AGENTCORE_ROLE_ARN,
            networkConfiguration={"networkMode": "PUBLIC"},
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": image_uri}
            },
            environmentVariables=environment,
        )
        return response.get("agentRuntimeArn", "")

    def create_runtime_endpoint(self, runtime_id: str) -> dict:
        """Control Plane: AgentCore Runtime Endpoint 생성.
        Returns dict with endpointArn and endpointName."""
        runtime_name = runtime_id.split("/")[-1] if "/" in runtime_id else runtime_id
        endpoint_name = f"{runtime_name}-ep"[:100]

        response = self.control_client.create_agent_runtime_endpoint(
            agentRuntimeId=runtime_id,
            name=endpoint_name,
        )
        return {
            "endpointArn": response.get("agentRuntimeEndpointArn", ""),
            "endpointName": endpoint_name,
            "agentRuntimeId": runtime_id,
        }

    def wait_for_endpoint_active(
        self, runtime_id: str, endpoint_name: str, timeout: int = 120
    ) -> str:
        """Endpoint가 ACTIVE 상태가 될 때까지 polling. 5초 간격."""
        import time

        elapsed = 0
        while elapsed < timeout:
            response = self.control_client.get_agent_runtime_endpoint(
                agentRuntimeId=runtime_id,
                endpointName=endpoint_name,
            )
            status = response.get("status", "CREATING")
            if status == "ACTIVE":
                return "ACTIVE"
            if status in ("FAILED", "DELETING"):
                return status
            time.sleep(5)
            elapsed += 5
        return "TIMEOUT"

    def wait_for_runtime_ready(self, runtime_arn: str, timeout: int = 120) -> str:
        """Runtime이 READY 상태가 될 때까지 polling. 5초 간격."""
        import time

        elapsed = 0
        while elapsed < timeout:
            status = self.get_runtime_status(runtime_arn)
            if status == "READY":
                return "READY"
            if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETING"):
                return status
            time.sleep(5)
            elapsed += 5
        return "TIMEOUT"

    def delete_runtime(self, runtime_arn: str):
        self.control_client.delete_agent_runtime(agentRuntimeId=runtime_arn)

    def get_runtime_status(self, runtime_arn: str) -> str:
        response = self.control_client.get_agent_runtime(agentRuntimeId=runtime_arn)
        return response.get("status", "unknown")

    def invoke_runtime_stream(
        self, runtime_arn: str, prompt: str, context: Optional[dict] = None
    ):
        """Data Plane: Agent Runtime streaming 호출. SSE 라인을 yield."""
        payload = {"prompt": prompt}
        if context:
            payload["context"] = context
        payload_bytes = json.dumps(payload).encode("utf-8")

        invoke_kwargs = {
            "agentRuntimeArn": runtime_arn,
            "contentType": "application/json",
            "accept": "text/event-stream",
            "payload": payload_bytes,
        }
        session_id = (context or {}).get("sessionId", "")
        if session_id and len(session_id) >= 33:
            invoke_kwargs["runtimeSessionId"] = session_id

        response = self.runtime_client.invoke_agent_runtime(**invoke_kwargs)
        content_type = response.get("contentType", "")
        body = response.get("response", response.get("body", b""))

        if "text/event-stream" in content_type:
            for line in body.iter_lines(chunk_size=10):
                if line:
                    yield line.decode("utf-8")
        else:
            # fallback: 동기 응답을 done 이벤트로 래핑
            if hasattr(body, "read"):
                body = body.read()
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            yield f"event: done\ndata: {json.dumps({'content': body})}"

    def invoke_runtime(
        self, runtime_arn: str, prompt: str, context: Optional[dict] = None
    ) -> str:
        """Data Plane: Agent Runtime 호출. 동기적 — 완료까지 블로킹.
        invoke_agent_runtime은 agentRuntimeArn을 사용 (endpoint가 아닌 runtime ARN)."""
        payload = {"prompt": prompt}
        if context:
            payload["context"] = context
        payload_bytes = json.dumps(payload).encode("utf-8")

        invoke_kwargs = {
            "agentRuntimeArn": runtime_arn,
            "contentType": "application/json",
            "accept": "application/json",
            "payload": payload_bytes,
        }
        session_id = (context or {}).get("sessionId", "")
        if session_id:
            invoke_kwargs["runtimeSessionId"] = session_id
        response = self.runtime_client.invoke_agent_runtime(**invoke_kwargs)

        body = response.get("response", response.get("body", b""))
        if hasattr(body, "read"):
            body = body.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return body
