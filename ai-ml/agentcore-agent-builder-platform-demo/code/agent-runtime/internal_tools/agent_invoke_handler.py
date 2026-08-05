"""Agent-to-Agent delegation handler via AgentCore InvokeAgent API.

NOTE: This is NOT the Google A2A Protocol (https://github.com/google-a2a/A2A).
It implements agent delegation using AWS Bedrock AgentCore's native InvokeAgent API.
See docs/a2a-implementation.md for details and comparison."""

import json
import logging
import os
import time
from typing import Any, Callable
import boto3
from botocore.config import Config as BotoConfig

from side_channel import SideChannelWriter

logger = logging.getLogger(__name__)

# 요청별 session context — agent_runner.py에서 매 invoke 시 갱신.
# delegation_depth는 이 런타임이 수신한 실제 depth이며, 하류로 보낼 depth 누적의
# 기준이 된다(H3). LLM이 정하는 tool arg가 아니라 이 값을 사용해야 depth가
# hop마다 누적되어 max_depth 가드가 실제로 동작한다.
_request_context: dict = {"session_id": "", "delegation_depth": 0}

# harness.Tier2Harness.max_depth와 동일. 수신 depth + 1 이 이 값을 넘으면
# 비싼 invoke_agent_runtime 호출 전에 즉시 거부한다(delegation cycle 폭주 방지).
MAX_DELEGATION_DEPTH = 2


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
                config=BotoConfig(read_timeout=900),
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
        # 하류로 보낼 depth는 LLM이 정한 delegation_depth 인자가 아니라 이 런타임이
        # 실제로 수신한 depth(_request_context) 기준으로 누적한다(H3).
        outgoing_depth = _request_context.get("delegation_depth", 0) + 1
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
                    "depth": outgoing_depth,
                    "timestamp": time.time(),
                },
            )

        # Depth 가드: 비싼 invoke_agent_runtime(read_timeout=900s) 호출 전에 즉시 거부.
        if outgoing_depth > MAX_DELEGATION_DEPTH:
            if writer:
                writer.write_event(
                    "a2a_delegation",
                    {
                        "phase": "error",
                        "caller": caller_agent,
                        "target": agent_id,
                        "error": f"Delegation depth {outgoing_depth} exceeds maximum {MAX_DELEGATION_DEPTH}",
                        "timestamp": time.time(),
                    },
                )
            return json.dumps(
                {
                    "error": f"Delegation depth {outgoing_depth} exceeds maximum {MAX_DELEGATION_DEPTH}"
                }
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
                    "delegationDepth": outgoing_depth,
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
        # AgentCore requires runtimeSessionId to be >= 33 chars; passing a
        # shorter value raises ValidationException. Only forward it when valid
        # (matches chat.py / agentcore_client.py); otherwise omit it and let
        # AgentCore generate one.
        if session_id and len(session_id) >= 33:
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
                config=BotoConfig(read_timeout=900),
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
        # 하류로 보낼 depth는 LLM이 정한 delegation_depth 인자가 아니라 이 런타임이
        # 실제로 수신한 depth(_request_context) 기준으로 누적한다(H3).
        outgoing_depth = _request_context.get("delegation_depth", 0) + 1
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
                    "depth": outgoing_depth,
                    "timestamp": time.time(),
                },
            )

        # Depth 가드: 비싼 invoke_agent_runtime(read_timeout=900s) 호출 전에 즉시 거부.
        if outgoing_depth > MAX_DELEGATION_DEPTH:
            if writer:
                writer.write_event(
                    "a2a_delegation",
                    {
                        "phase": "error",
                        "caller": caller_agent,
                        "target": target_agent_id,
                        "error": f"Delegation depth {outgoing_depth} exceeds maximum {MAX_DELEGATION_DEPTH}",
                        "timestamp": time.time(),
                    },
                )
            return json.dumps(
                {
                    "error": f"Delegation depth {outgoing_depth} exceeds maximum {MAX_DELEGATION_DEPTH}"
                }
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
                    "delegationDepth": outgoing_depth,
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
        # AgentCore requires runtimeSessionId to be >= 33 chars; passing a
        # shorter value raises ValidationException. Only forward it when valid
        # (matches chat.py / agentcore_client.py); otherwise omit it and let
        # AgentCore generate one.
        if session_id and len(session_id) >= 33:
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
