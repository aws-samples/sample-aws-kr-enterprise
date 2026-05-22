"""A2A invoke handler. Spec Section 5.5, 3.5, 4.2."""

import json
import logging
import os
import time
from typing import Any, Callable
import boto3
from botocore.config import Config as BotoConfig

from side_channel import SideChannelWriter

logger = logging.getLogger(__name__)

# 요청별 session context — agent_runner.py에서 매 invoke 시 갱신
_request_context: dict = {"session_id": ""}


def create_agent_invoke(tool_config: dict, dynamodb_resource: Any) -> Callable:
    name = tool_config["name"]
    description = tool_config.get("description", "")
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    _client_cache: dict = {}

    def _get_client():
        if "client" not in _client_cache:
            _client_cache["client"] = boto3.client(
                "bedrock-agentcore",
                region_name=region,
                config=BotoConfig(read_timeout=300),
            )
        return _client_cache["client"]

    def tool_fn(
        agent_id: str,
        prompt: str,
        session_id: str = "",
        caller: str = "",
        delegation_depth: int = 0,
    ) -> str:
        """선택된 Domain Agent Runtime을 호출한다.
        내부적으로 boto3 bedrock-agentcore의 invoke_agent_runtime()을 래핑."""
        session_id = session_id or _request_context.get("session_id", "")
        table_name = os.environ.get("DYNAMODB_TABLE", "aiops-v2-dev-platform")
        table = dynamodb_resource.Table(table_name)

        # Side-Channel: a2a_delegation 이벤트 발행 (start)
        writer = None
        caller_agent = os.environ.get("AGENT_ID", "")
        if session_id:
            writer = SideChannelWriter(
                table=table, session_id=session_id, agent_id=caller_agent
            )
            writer.write_event(
                "a2a_delegation",
                {
                    "phase": "start",
                    "caller": caller_agent,
                    "target": agent_id,
                    "prompt_summary": prompt[:100],
                    "depth": delegation_depth + 1,
                    "timestamp": time.time(),
                },
            )

        runtime_info = table.get_item(Key={"PK": f"AGENT#{agent_id}", "SK": "RUNTIME"})
        item = runtime_info.get("Item")
        active_statuses = {"active", "READY"}
        if not item or item.get("status") not in active_statuses:
            if writer:
                writer.write_event(
                    "a2a_delegation",
                    {
                        "phase": "error",
                        "caller": caller_agent,
                        "target": agent_id,
                        "error": f"Agent {agent_id} is not active (status={item.get('status') if item else 'no runtime'})",
                        "timestamp": time.time(),
                    },
                )
            return json.dumps({"error": f"Agent {agent_id} is not active"})

        runtime_arn = item["runtimeArn"]
        payload = json.dumps(
            {
                "prompt": prompt,
                "context": {
                    "sessionId": session_id,
                    "caller": caller_agent or caller,
                    "delegationDepth": delegation_depth + 1,
                },
            }
        ).encode("utf-8")

        ac_client = _get_client()
        invoke_kwargs = {
            "agentRuntimeArn": runtime_arn,
            "contentType": "application/json",
            "accept": "application/json",
            "payload": payload,
        }
        if session_id:
            invoke_kwargs["runtimeSessionId"] = session_id
        response = ac_client.invoke_agent_runtime(**invoke_kwargs)
        body = response.get("response", response.get("body", b""))
        if hasattr(body, "read"):
            body = body.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")

        # Side-Channel: a2a_delegation 이벤트 발행 (end)
        if writer:
            writer.write_event(
                "a2a_delegation",
                {
                    "phase": "end",
                    "caller": caller_agent,
                    "target": agent_id,
                    "result_summary": body[:150] if body else "",
                    "timestamp": time.time(),
                },
            )

        return body

    tool_fn.__name__ = name
    tool_fn.__doc__ = description
    return tool_fn


def create_scoped_agent_invoke(tool_config: dict, dynamodb_resource: Any) -> Callable:
    """delegations용: targetAgent가 closure로 고정된 A2A invoke tool."""
    target_agent_id = tool_config["targetAgent"]
    name = tool_config["name"]
    description = tool_config.get("description", "")
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    _client_cache: dict = {}

    def _get_client():
        if "client" not in _client_cache:
            _client_cache["client"] = boto3.client(
                "bedrock-agentcore",
                region_name=region,
                config=BotoConfig(read_timeout=300),
            )
        return _client_cache["client"]

    def tool_fn(
        prompt: str,
        session_id: str = "",
        caller: str = "",
        delegation_depth: int = 0,
    ) -> str:
        """고정된 대상 Agent Runtime을 호출한다."""
        session_id = session_id or _request_context.get("session_id", "")
        table_name = os.environ.get("DYNAMODB_TABLE", "aiops-v2-dev-platform")
        table = dynamodb_resource.Table(table_name)

        caller_agent = os.environ.get("AGENT_ID", "")
        writer = None
        if session_id:
            writer = SideChannelWriter(
                table=table, session_id=session_id, agent_id=caller_agent
            )
            writer.write_event(
                "a2a_delegation",
                {
                    "phase": "start",
                    "caller": caller_agent,
                    "target": target_agent_id,
                    "prompt_summary": prompt[:100],
                    "depth": delegation_depth + 1,
                    "timestamp": time.time(),
                },
            )

        runtime_info = table.get_item(
            Key={"PK": f"AGENT#{target_agent_id}", "SK": "RUNTIME"}
        )
        item = runtime_info.get("Item")
        active_statuses = {"active", "READY"}
        if not item or item.get("status") not in active_statuses:
            if writer:
                writer.write_event(
                    "a2a_delegation",
                    {
                        "phase": "error",
                        "caller": caller_agent,
                        "target": target_agent_id,
                        "error": f"Agent {target_agent_id} is not active",
                        "timestamp": time.time(),
                    },
                )
            return json.dumps({"error": f"Agent {target_agent_id} is not active"})

        runtime_arn = item["runtimeArn"]
        payload = json.dumps(
            {
                "prompt": prompt,
                "context": {
                    "sessionId": session_id,
                    "caller": caller_agent or caller,
                    "delegationDepth": delegation_depth + 1,
                },
            }
        ).encode("utf-8")

        ac_client = _get_client()
        invoke_kwargs = {
            "agentRuntimeArn": runtime_arn,
            "contentType": "application/json",
            "accept": "application/json",
            "payload": payload,
        }
        if session_id:
            invoke_kwargs["runtimeSessionId"] = session_id
        response = ac_client.invoke_agent_runtime(**invoke_kwargs)
        body = response.get("response", response.get("body", b""))
        if hasattr(body, "read"):
            body = body.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")

        if writer:
            writer.write_event(
                "a2a_delegation",
                {
                    "phase": "end",
                    "caller": caller_agent,
                    "target": target_agent_id,
                    "result_summary": body[:150] if body else "",
                    "timestamp": time.time(),
                },
            )

        return body

    tool_fn.__name__ = name
    tool_fn.__doc__ = description
    return tool_fn
